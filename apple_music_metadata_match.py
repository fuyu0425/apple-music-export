from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

import app

AUDIT_HEADER = [
    "snapshot",
    "persistent_id",
    "status",
    "reasons",
    "current_title",
    "current_artist",
    "current_album",
    "suggested_title",
    "suggested_artist",
    "suggested_album",
    "evidence",
    "source_urls",
    "playlists",
    "duration_seconds",
    "location",
]
REPORT_HEADER = [
    "snapshot",
    "persistent_id",
    "audit_status",
    "reasons",
    "match_status",
    "current_title",
    "current_artist",
    "current_album",
    "suggested_title",
    "suggested_artist",
    "suggested_album",
    "acoustid_id",
    "acoustid_score",
    "musicbrainz_recording_id",
    "musicbrainz_release_id",
    "cover_art_url",
    "beets_recommendation",
    "beets_distance",
    "distance_penalties",
    "evidence",
    "source_urls",
    "playlists",
    "duration_seconds",
    "location",
]
MATCH_STATUS_PRIORITY = {
    "strong_candidate": 0,
    "needs_review": 1,
    "no_change": 2,
    "unmatched": 3,
    "unavailable": 4,
}
UNKNOWN_ALBUM_DIRECTORIES = {"unknown album", "unknown", "untitled", "n/a", "none"}
QUALIFIERS = ("live", "cover", "remix", "instrumental", "acoustic", "karaoke")
BEETS_CONFIG = """plugins: [musicbrainz]
raise_on_error: yes
import:
  languages: []
"""
_ACOUSTID_LOCK = threading.Lock()
_ACOUSTID_LAST_START = 0.0


@dataclass(frozen=True)
class SnapshotTrack:
    persistent_id: str
    title: str
    artist: str
    album: str
    duration: float
    location: str | None


@dataclass(frozen=True)
class AuditRow:
    snapshot: str
    track: SnapshotTrack
    status: str
    reasons: str
    evidence: str
    source_urls: str
    playlists: str
    duration_seconds: str
    location: str


@dataclass(frozen=True)
class _FingerprintEvidence:
    acoustid_id: str = ""
    score: float | None = None
    recording_scores: tuple[tuple[str, float], ...] = ()
    release_scores: tuple[tuple[str, float], ...] = ()
    recording_release_scores: tuple[tuple[str, tuple[tuple[str, float], ...]], ...] = ()
    top_release_ids: tuple[str, ...] = ()
    error: str = ""

    @property
    def recording_ids(self) -> frozenset[str]:
        return frozenset(recording_id for recording_id, _ in self.recording_scores)

    def release_scores_for(self, recording_id: str) -> tuple[tuple[str, float], ...]:
        for candidate_id, release_scores in self.recording_release_scores:
            if candidate_id == recording_id:
                return release_scores
        return ()


@dataclass(frozen=True)
class MatchEvidence:
    kind: str
    resolved: bool = False
    error: str = ""
    reason: str = ""
    candidate_title: str = ""
    candidate_artist: str = ""
    candidate_album: str = ""
    candidate_duration: float | None = None
    recommendation: str = ""
    distance: float | None = None
    penalties: tuple[tuple[str, float], ...] = ()
    acoustid_id: str = ""
    acoustid_score: float | None = None
    acoustid_recording_ids: frozenset[str] = field(default_factory=frozenset)
    acoustid_release_ids: tuple[str, ...] = ()
    recording_id: str = ""
    release_id: str = ""
    cover_art_url: str = ""
    cover_art_status: str = ""
    release_status: str = ""
    release_language: str = ""
    release_script: str = ""
    release_probe_available: int | None = None
    release_probe_count: int | None = None
    extra_items: int = 0
    extra_tracks: int = 0
    album_all_supported: bool = False
    diagnostic: str = ""
    item_mapping: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LoadedInput:
    tracks: tuple[SnapshotTrack, ...]
    audit_rows: tuple[AuditRow, ...]


def display_norm(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def path_key(path: Path) -> str:
    return unicodedata.normalize("NFD", str(path.resolve()))


def media_path(location: str | None) -> Path | None:
    if not location:
        return None
    if location.startswith("file://"):
        return Path(unquote(urlparse(location).path))
    return Path(location)


def _is_readable(path: Path | None) -> bool:
    return path is not None and path.is_file() and os.access(path, os.R_OK)


def load_input(snapshot: Path, audit: Path) -> LoadedInput:
    with contextlib.closing(app.connect_read_only(snapshot)) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if metadata.get("schema_version") != "2":
            raise ValueError("snapshot schema_version must be 2")
        rows = connection.execute(
            "SELECT persistent_id, name, artist, album, duration, location FROM tracks"
        )
        tracks = tuple(
            SnapshotTrack(
                persistent_id=row["persistent_id"],
                title=row["name"],
                artist=row["artist"],
                album=row["album"],
                duration=row["duration"],
                location=row["location"],
            )
            for row in rows
        )

    by_id = {track.persistent_id: track for track in tracks}
    audit_rows: list[AuditRow] = []
    seen_ids: set[str] = set()
    snapshot_cell: str | None = None
    with audit.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != AUDIT_HEADER:
            raise ValueError("audit report header does not match required fields")
        for row in reader:
            persistent_id = row["persistent_id"]
            if persistent_id in seen_ids:
                raise ValueError(f"duplicate persistent_id in audit: {persistent_id}")
            seen_ids.add(persistent_id)
            track = by_id.get(persistent_id)
            if track is None:
                raise ValueError(f"persistent_id is not in snapshot: {persistent_id}")

            row_snapshot = row["snapshot"]
            if snapshot_cell is None:
                snapshot_cell = row_snapshot
            elif row_snapshot != snapshot_cell:
                raise ValueError("audit contains mixed snapshot values")
            if Path(row_snapshot).resolve() != snapshot.resolve():
                raise ValueError(f"audit snapshot does not match --snapshot: {row_snapshot}")

            bindings = (
                ("current_title", track.title),
                ("current_artist", track.artist),
                ("current_album", track.album),
            )
            for field_name, expected in bindings:
                if row[field_name] != expected:
                    raise ValueError(f"audit {field_name} changed for {persistent_id}")

            try:
                audit_duration = float(row["duration_seconds"])
            except ValueError as error:
                raise ValueError(f"invalid duration_seconds for {persistent_id}") from error
            if audit_duration != track.duration:
                raise ValueError(f"audit duration_seconds changed for {persistent_id}")

            raw_location = track.location or ""
            if row["location"] != raw_location:
                raise ValueError(f"audit location changed for {persistent_id}")

            audit_rows.append(
                AuditRow(
                    snapshot=row_snapshot,
                    track=track,
                    status=row["status"],
                    reasons=row["reasons"],
                    evidence=row["evidence"],
                    source_urls=row["source_urls"],
                    playlists=row["playlists"],
                    duration_seconds=row["duration_seconds"],
                    location=row["location"],
                )
            )

    if not audit_rows:
        raise ValueError("audit report is empty")
    return LoadedInput(tracks, tuple(audit_rows))


def _possible_group_key(track: SnapshotTrack) -> tuple[str, str] | None:
    path = media_path(track.location)
    album = display_norm(track.album)
    if path is None or not album:
        return None
    if display_norm(path.parent.name) in UNKNOWN_ALBUM_DIRECTORIES:
        return None
    return path_key(path.parent), album


def build_match_groups(loaded: LoadedInput) -> list[tuple[SnapshotTrack, ...]]:
    possible_groups: dict[tuple[str, str], list[SnapshotTrack]] = defaultdict(list)
    for track in loaded.tracks:
        if (key := _possible_group_key(track)) is not None:
            possible_groups[key].append(track)

    groups: list[tuple[SnapshotTrack, ...]] = []
    emitted_groups: set[tuple[str, str]] = set()
    for audit_row in loaded.audit_rows:
        track = audit_row.track
        key = _possible_group_key(track)
        siblings = possible_groups.get(key, []) if key is not None else []
        if (
            key is not None
            and 2 <= len(siblings) <= 100
            and all(_is_readable(media_path(sibling.location)) for sibling in siblings)
        ):
            if key not in emitted_groups:
                groups.append(tuple(siblings))
                emitted_groups.add(key)
        else:
            groups.append((track,))
    return groups


class BeetsMatcher:
    def __init__(self, acoustid_api_key: str) -> None:
        self._api_key = acoustid_api_key
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._old_beetsdir: str | None = None
        self._had_beetsdir = False
        self._acoustid: Any = None
        self._autotag: Any = None
        self._item_class: Any = None
        self._fingerprints: dict[str, _FingerprintEvidence] = {}
        self._items: dict[str, Any] = {}
        self._cover_art: dict[str, tuple[str, str]] = {}

    def __enter__(self) -> Self:
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="apple-music-beets-")
        directory = Path(self._temporary_directory.name)
        (directory / "config.yaml").write_text(BEETS_CONFIG, encoding="utf-8")
        self._had_beetsdir = "BEETSDIR" in os.environ
        self._old_beetsdir = os.environ.get("BEETSDIR")
        os.environ["BEETSDIR"] = str(directory)
        try:
            import acoustid
            import beets
            import beets.autotag
            import beets.plugins
            from beets.library import Item

            beets.plugins.load_plugins()
            self._acoustid = acoustid
            self._autotag = beets.autotag
            self._item_class = Item
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._had_beetsdir:
            assert self._old_beetsdir is not None
            os.environ["BEETSDIR"] = self._old_beetsdir
        else:
            os.environ.pop("BEETSDIR", None)
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    def match_group(self, tracks: Sequence[SnapshotTrack]) -> dict[str, MatchEvidence]:
        if len(tracks) == 1:
            track = tracks[0]
            return {track.persistent_id: self._match_single(track)}

        fingerprints = {track.persistent_id: self._fingerprint(track) for track in tracks}
        support: Counter[str] = Counter()
        for fingerprint in fingerprints.values():
            support.update(release_id for release_id, _ in fingerprint.release_scores)
        release_ids = [
            release_id
            for release_id, count in sorted(support.items(), key=lambda pair: (-pair[1], pair[0]))
            if count / len(tracks) > 0.6
        ][:5]
        if not release_ids:
            return {track.persistent_id: self._match_single(track) for track in tracks}
        return self._match_album(tracks, fingerprints, release_ids)

    def _item(self, track: SnapshotTrack) -> Any:
        path = media_path(track.location)
        if not _is_readable(path):
            raise ValueError("media file is unavailable")
        assert path is not None
        key = path_key(path)
        item = self._items.get(key)
        if item is None:
            item = self._item_class.from_path(os.fsencode(path))
            self._items[key] = item
        item.title = track.title
        item.artist = track.artist
        item.album = track.album
        item.length = track.duration
        return item

    def _fingerprint(self, track: SnapshotTrack) -> _FingerprintEvidence:
        path = media_path(track.location)
        if not _is_readable(path):
            return _FingerprintEvidence(error="media file is unavailable")
        assert path is not None
        key = path_key(path)
        if key in self._fingerprints:
            return self._fingerprints[key]
        try:
            duration, fingerprint = self._acoustid.fingerprint_file(path)
        except Exception as error:  # noqa: BLE001 - every fingerprint failure is a row outcome.
            evidence = _FingerprintEvidence(error=str(error) or type(error).__name__)
            self._fingerprints[key] = evidence
            return evidence

        global _ACOUSTID_LAST_START
        with _ACOUSTID_LOCK:
            delay = 0.34 - (time.monotonic() - _ACOUSTID_LAST_START)
            if delay > 0:
                time.sleep(delay)
            _ACOUSTID_LAST_START = time.monotonic()
        response = self._acoustid.lookup(
            self._api_key,
            fingerprint,
            duration,
            meta="recordings releases",
            timeout=10,
        )
        evidence = self._parse_lookup(response)
        self._fingerprints[key] = evidence
        return evidence

    @staticmethod
    def _parse_lookup(response: Any) -> _FingerprintEvidence:
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise RuntimeError("AcoustID lookup failed")
        retained: list[tuple[float, str, tuple[dict[str, Any], ...]]] = []
        for raw_result in response.get("results", []):
            score = raw_result.get("score")
            acoustid_id = raw_result.get("id")
            recordings = raw_result.get("recordings", [])
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or score < 0.5
                or not isinstance(acoustid_id, str)
            ):
                continue
            retained.append(
                (
                    float(score),
                    acoustid_id,
                    tuple(recording for recording in recordings if isinstance(recording, dict)),
                )
            )
        retained.sort(key=lambda result: (-result[0], result[1]))
        if not retained:
            return _FingerprintEvidence()

        recording_scores: dict[str, float] = {}
        release_scores: dict[str, float] = {}
        recording_release_scores: dict[str, dict[str, float]] = {}
        top_release_ids: set[str] = set()
        for result_index, (score, _acoustid_id, recordings) in enumerate(retained):
            for recording in recordings:
                recording_id = recording.get("id")
                if isinstance(recording_id, str):
                    recording_scores[recording_id] = max(
                        score, recording_scores.get(recording_id, 0.0)
                    )
                    scoped_release_scores = recording_release_scores.setdefault(recording_id, {})
                else:
                    scoped_release_scores = None
                for release in recording.get("releases", []):
                    release_id = release.get("id") if isinstance(release, dict) else None
                    if isinstance(release_id, str):
                        release_scores[release_id] = max(score, release_scores.get(release_id, 0.0))
                        if scoped_release_scores is not None:
                            scoped_release_scores[release_id] = max(
                                score, scoped_release_scores.get(release_id, 0.0)
                            )
                        if result_index == 0:
                            top_release_ids.add(release_id)

        top_score, top_id, _ = retained[0]
        return _FingerprintEvidence(
            acoustid_id=top_id,
            score=top_score,
            recording_scores=tuple(
                sorted(recording_scores.items(), key=lambda pair: (-pair[1], pair[0]))
            ),
            release_scores=tuple(
                sorted(release_scores.items(), key=lambda pair: (-pair[1], pair[0]))
            ),
            recording_release_scores=tuple(
                (
                    recording_id,
                    tuple(sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))),
                )
                for recording_id, scores in sorted(recording_release_scores.items())
            ),
            top_release_ids=tuple(sorted(top_release_ids)),
        )

    @staticmethod
    def _penalties(candidate: Any) -> tuple[tuple[str, float], ...]:
        return tuple(sorted((key, round(float(value), 6)) for key, value in candidate.distance))

    @staticmethod
    def _recommendation(proposal: Any) -> str:
        name = getattr(proposal.recommendation, "name", str(proposal.recommendation))
        return name if name in {"strong", "medium", "low", "none"} else "none"

    def _lookup_cover_art(self, release_id: str) -> tuple[str, str]:
        cached = self._cover_art.get(release_id)
        if cached is not None:
            return cached

        request = urllib.request.Request(
            f"https://coverartarchive.org/release/{release_id}/",
            headers={
                "Accept": "application/json",
                "User-Agent": "apple-music-export/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            result = ("", "not_found" if error.code == 404 else "unavailable")
        except (OSError, ValueError, TypeError):
            result = ("", "unavailable")
        else:
            result = ("", "not_found")
            images = payload.get("images") if isinstance(payload, dict) else None
            if not isinstance(images, list):
                result = ("", "unavailable")
            else:
                for image in images:
                    if (
                        not isinstance(image, dict)
                        or image.get("front") is not True
                        or image.get("approved") is not True
                    ):
                        continue
                    thumbnails = image.get("thumbnails")
                    url = (
                        thumbnails.get("1200")
                        if isinstance(thumbnails, dict) and isinstance(thumbnails.get("1200"), str)
                        else image.get("image")
                    )
                    if not isinstance(url, str):
                        result = ("", "unavailable")
                        break
                    parsed = urlparse(url)
                    if parsed.scheme == "http" and parsed.hostname == "coverartarchive.org":
                        url = parsed._replace(scheme="https").geturl()
                        parsed = urlparse(url)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname != "coverartarchive.org"
                        or not parsed.path.startswith(f"/release/{release_id}/")
                    ):
                        result = ("", "unavailable")
                    else:
                        result = (url, "front")
                    break

        self._cover_art[release_id] = result
        return result

    @staticmethod
    def _base(kind: str, fingerprint: _FingerprintEvidence) -> dict[str, Any]:
        return {
            "kind": kind,
            "acoustid_id": fingerprint.acoustid_id,
            "acoustid_score": fingerprint.score,
            "acoustid_recording_ids": fingerprint.recording_ids,
            "acoustid_release_ids": fingerprint.top_release_ids,
        }

    def _match_single(self, track: SnapshotTrack) -> MatchEvidence:
        fingerprint = self._fingerprint(track)
        base = self._base("singleton", fingerprint)
        if fingerprint.error:
            return MatchEvidence(error=fingerprint.error, **base)
        recording_ids = [recording_id for recording_id, _ in fingerprint.recording_scores][:5]
        if not recording_ids:
            return MatchEvidence(reason="no AcoustID result reached 0.5", **base)
        item = self._item(track)
        proposal = self._autotag.tag_item(item, search_ids=recording_ids)
        if not proposal.candidates:
            return MatchEvidence(reason="no MusicBrainz candidate resolved", **base)
        candidate = proposal.candidates[0]
        info = candidate.info
        candidate_album = ""
        release_id = ""
        reason = ""
        release_status = ""
        release_language = ""
        release_script = ""
        release_probe_available = None
        release_probe_count = None
        if not track.album:
            base["acoustid_release_ids"] = base["acoustid_release_ids"][:5]
            recording_release_scores = fingerprint.release_scores_for(info.track_id or "")
            release_probe_available = len(recording_release_scores)
            release_ids = [release_id for release_id, _ in recording_release_scores[:5]]
            release_probe_count = len(release_ids)
            if not release_ids:
                reason = "no AcoustID release supports the matched recording"
            else:
                base["acoustid_release_ids"] = tuple(release_ids)
                try:
                    _, _, album_proposal = self._autotag.tag_album([item], search_ids=release_ids)
                    eligible_candidates = [
                        album_candidate
                        for album_candidate in album_proposal.candidates
                        if getattr(album_candidate.info, "albumstatus", None) == "Official"
                        and any(
                            getattr(release_track, "track_id", "") == info.track_id
                            for release_track in (getattr(album_candidate.info, "tracks", ()) or ())
                        )
                    ]
                    if eligible_candidates:
                        album_candidate = min(
                            eligible_candidates,
                            key=lambda candidate: (
                                float(candidate.distance),
                                getattr(candidate.info, "album_id", "") or "",
                            ),
                        )
                        candidate_album = getattr(album_candidate.info, "album", "") or ""
                        release_id = getattr(album_candidate.info, "album_id", "") or ""
                        release_status = getattr(album_candidate.info, "albumstatus", "") or ""
                        release_language = getattr(album_candidate.info, "language", "") or ""
                        release_script = getattr(album_candidate.info, "script", "") or ""
                    else:
                        reason = (
                            "no official MusicBrainz release found in "
                            f"{release_probe_count} of {release_probe_available} "
                            "recording-scoped candidates"
                        )
                except (OSError, RuntimeError, ValueError) as error:
                    reason = f"album lookup unavailable: {error}"
            if release_id:
                base["acoustid_release_ids"] = tuple(
                    dict.fromkeys((release_id, *base["acoustid_release_ids"]))
                )[:5]
        cover_art_url, cover_art_status = (
            self._lookup_cover_art(release_id) if release_id else ("", "")
        )
        return MatchEvidence(
            resolved=True,
            reason=reason,
            candidate_title=info.title or "",
            candidate_artist=info.artist or "",
            candidate_album=candidate_album,
            candidate_duration=info.length,
            recommendation=self._recommendation(proposal),
            distance=float(candidate.distance),
            penalties=self._penalties(candidate),
            recording_id=info.track_id or "",
            release_id=release_id,
            release_status=release_status,
            release_language=release_language,
            release_script=release_script,
            cover_art_url=cover_art_url,
            cover_art_status=cover_art_status,
            release_probe_available=release_probe_available,
            release_probe_count=release_probe_count,
            **base,
        )

    def _match_album(
        self,
        tracks: Sequence[SnapshotTrack],
        fingerprints: dict[str, _FingerprintEvidence],
        release_ids: list[str],
    ) -> dict[str, MatchEvidence]:
        items = [self._item(track) for track in tracks]
        item_tracks = {id(item): track for item, track in zip(items, tracks)}
        cur_artist, cur_album, proposal = self._autotag.tag_album(items, search_ids=release_ids)
        if not proposal.candidates:
            return {
                track.persistent_id: MatchEvidence(
                    reason="no MusicBrainz candidate resolved",
                    **self._base("album", fingerprints[track.persistent_id]),
                )
                for track in tracks
            }

        candidate = proposal.candidates[0]
        mapped = {item_tracks[id(item)]: info for item, info in candidate.mapping.items()}
        all_supported = all(
            info.track_id in fingerprints[track.persistent_id].recording_ids
            for track, info in mapped.items()
        )
        item_mapping = tuple(
            sorted((track.persistent_id, info.track_id or "") for track, info in mapped.items())
        )
        release_id = candidate.info.album_id or ""
        cover_art_url, cover_art_status = (
            self._lookup_cover_art(release_id) if release_id else ("", "")
        )
        results: dict[str, MatchEvidence] = {}
        for track in tracks:
            fingerprint = fingerprints[track.persistent_id]
            base = self._base("album", fingerprint)
            if fingerprint.error:
                results[track.persistent_id] = MatchEvidence(error=fingerprint.error, **base)
                continue
            info = mapped.get(track)
            if info is None:
                results[track.persistent_id] = MatchEvidence(
                    reason="release candidate did not map this track", **base
                )
                continue
            results[track.persistent_id] = MatchEvidence(
                resolved=True,
                candidate_title=info.title or "",
                candidate_artist=info.artist or "",
                candidate_album=candidate.info.album or "",
                candidate_duration=info.length,
                recommendation=self._recommendation(proposal),
                distance=float(candidate.distance),
                penalties=self._penalties(candidate),
                recording_id=info.track_id or "",
                release_id=release_id,
                cover_art_url=cover_art_url,
                cover_art_status=cover_art_status,
                extra_items=len(candidate.extra_items),
                extra_tracks=len(candidate.extra_tracks),
                album_all_supported=all_supported,
                diagnostic=f"album_context={cur_artist} - {cur_album}",
                item_mapping=item_mapping,
                **base,
            )
        return results


def _qualifiers_agree(row: AuditRow, evidence: MatchEvidence) -> bool:
    current = f"{row.track.title} {row.track.artist}"
    candidate = f"{evidence.candidate_title} {evidence.candidate_artist}"
    if evidence.kind == "album":
        current += f" {row.track.album}"
        candidate += f" {evidence.candidate_album}"
    for qualifier in QUALIFIERS:
        pattern = rf"\b{re.escape(qualifier)}\b"
        if bool(re.search(pattern, current, re.IGNORECASE)) != bool(
            re.search(pattern, candidate, re.IGNORECASE)
        ):
            return False
    return True


def _classify(row: AuditRow, evidence: MatchEvidence) -> str:
    if evidence.error:
        return "unavailable"
    if not evidence.resolved:
        return "unmatched"

    title_equal = display_norm(row.track.title) == display_norm(evidence.candidate_title)
    artist_equal = display_norm(row.track.artist) == display_norm(evidence.candidate_artist)
    album_equal = display_norm(row.track.album) == display_norm(evidence.candidate_album)
    if evidence.kind == "album" and title_equal and artist_equal and album_equal:
        return "no_change"
    if evidence.kind == "singleton" and title_equal and artist_equal and row.track.album:
        return "no_change"
    if evidence.kind == "singleton" and not row.track.album:
        return "needs_review"

    differs = not (title_equal and artist_equal)
    if evidence.kind == "album":
        differs = differs or not album_equal
    strong = (
        evidence.recommendation == "strong"
        and evidence.recording_id in evidence.acoustid_recording_ids
        and evidence.candidate_duration is not None
        and abs(row.track.duration - evidence.candidate_duration) <= 5
        and _qualifiers_agree(row, evidence)
        and (
            evidence.kind != "album"
            or (
                evidence.extra_items == 0
                and evidence.extra_tracks == 0
                and evidence.album_all_supported
            )
        )
        and differs
    )
    return "strong_candidate" if strong else "needs_review"


def _stable_parts(existing: str, additions: Sequence[str]) -> str:
    values: list[str] = []
    seen: set[str] = set()
    for value in [*existing.split(" | "), *additions]:
        value = value.strip()
        if value and value not in seen:
            values.append(value)
            seen.add(value)
    return " | ".join(values)


def _evidence_tokens(evidence: MatchEvidence) -> list[str]:
    tokens: list[str] = []
    if evidence.error:
        tokens.append(f"unavailable={evidence.error}")
    elif evidence.reason:
        tokens.append(evidence.reason)
    if evidence.acoustid_id:
        tokens.append(f"acoustid_id={evidence.acoustid_id}")
    if evidence.acoustid_release_ids:
        tokens.append(f"acoustid_release_ids={','.join(evidence.acoustid_release_ids)}")
    if evidence.recording_id:
        tokens.append(f"musicbrainz_recording_id={evidence.recording_id}")
    if evidence.release_id:
        tokens.append(f"musicbrainz_release_id={evidence.release_id}")
    if evidence.release_status:
        tokens.append(f"musicbrainz_release_status={evidence.release_status}")
    if evidence.release_language:
        tokens.append(f"musicbrainz_release_language={evidence.release_language}")
    if evidence.release_script:
        tokens.append(f"musicbrainz_release_script={evidence.release_script}")
    if evidence.cover_art_status:
        tokens.append(f"cover_art_archive={evidence.cover_art_status}")
    if evidence.release_probe_count is not None and evidence.release_probe_available is not None:
        tokens.append(
            "musicbrainz_release_probe="
            f"{evidence.release_probe_count}/{evidence.release_probe_available}"
        )
    if evidence.kind == "album" and evidence.resolved:
        tokens.extend(
            (
                f"extra_items={evidence.extra_items}",
                f"extra_tracks={evidence.extra_tracks}",
                evidence.diagnostic,
                "item_mapping="
                + ",".join(
                    f"{persistent_id}:{recording_id}"
                    for persistent_id, recording_id in evidence.item_mapping
                ),
            )
        )
    return tokens


def _source_urls(evidence: MatchEvidence) -> list[str]:
    urls: list[str] = []
    if evidence.acoustid_id:
        urls.append(f"https://acoustid.org/track/{evidence.acoustid_id}")
    if evidence.recording_id:
        urls.append(f"https://musicbrainz.org/recording/{evidence.recording_id}")
    if evidence.release_id:
        urls.append(f"https://musicbrainz.org/release/{evidence.release_id}")
        if evidence.cover_art_url:
            urls.append(evidence.cover_art_url)
    urls.extend(
        f"https://musicbrainz.org/release/{release_id}"
        for release_id in evidence.acoustid_release_ids
        if release_id != evidence.release_id
    )
    return urls


def make_report_row(row: AuditRow, evidence: MatchEvidence) -> dict[str, str]:
    status = _classify(row, evidence)
    title_differs = display_norm(row.track.title) != display_norm(evidence.candidate_title)
    artist_differs = display_norm(row.track.artist) != display_norm(evidence.candidate_artist)
    album_differs = display_norm(row.track.album) != display_norm(evidence.candidate_album)
    can_suggest = status in {"strong_candidate", "needs_review"} and evidence.resolved
    suggested_title = evidence.candidate_title if can_suggest and title_differs else ""
    suggested_artist = evidence.candidate_artist if can_suggest and artist_differs else ""
    suggested_album = (
        evidence.candidate_album
        if (can_suggest and album_differs and (evidence.kind == "album" or not row.track.album))
        else ""
    )
    penalties = {key: value for key, value in evidence.penalties}
    return {
        "snapshot": row.snapshot,
        "persistent_id": row.track.persistent_id,
        "audit_status": row.status,
        "reasons": row.reasons,
        "match_status": status,
        "current_title": row.track.title,
        "current_artist": row.track.artist,
        "current_album": row.track.album,
        "suggested_title": suggested_title,
        "suggested_artist": suggested_artist,
        "suggested_album": suggested_album,
        "acoustid_id": evidence.acoustid_id,
        "acoustid_score": (
            f"{evidence.acoustid_score:.6f}" if evidence.acoustid_score is not None else ""
        ),
        "musicbrainz_recording_id": evidence.recording_id,
        "musicbrainz_release_id": evidence.release_id,
        "cover_art_url": evidence.cover_art_url,
        "beets_recommendation": evidence.recommendation,
        "beets_distance": f"{evidence.distance:.6f}" if evidence.distance is not None else "",
        "distance_penalties": (
            json.dumps(penalties, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if penalties
            else ""
        ),
        "evidence": _stable_parts(row.evidence, _evidence_tokens(evidence)),
        "source_urls": _stable_parts(row.source_urls, _source_urls(evidence)),
        "playlists": row.playlists,
        "duration_seconds": row.duration_seconds,
        "location": row.location,
    }


def _sort_report_rows(rows: list[dict[str, str]]) -> None:
    rows.sort(
        key=lambda row: (
            MATCH_STATUS_PRIORITY[row["match_status"]],
            display_norm(row["current_artist"]),
            display_norm(row["current_album"]),
            display_norm(row["current_title"]),
            row["persistent_id"],
        )
    )


def make_apply_plan(
    snapshot: Path,
    audit: Path,
    report: Path,
    rows: Sequence[dict[str, str]],
) -> dict[str, Any]:
    metadata_changes = []
    for row in rows:
        suggested = {
            field: row[f"suggested_{field}"]
            for field in ("title", "artist", "album")
            if row[f"suggested_{field}"]
        }
        if row["match_status"] not in {"strong_candidate", "needs_review"} or not suggested:
            continue
        change: dict[str, Any] = {
            "persistent_id": row["persistent_id"],
            "approved": False,
            "match_status": row["match_status"],
            "current": {
                "title": row["current_title"],
                "artist": row["current_artist"],
                "album": row["current_album"],
            },
            "suggested": suggested,
        }
        cover_art_url = row.get("cover_art_url", "")
        if cover_art_url:
            change["artwork"] = {
                "source": "cover_art_archive",
                "release_id": row["musicbrainz_release_id"],
                "url": cover_art_url,
            }
        metadata_changes.append(change)
    return {
        "snapshot": str(snapshot.resolve()),
        "audit": str(audit.resolve()),
        "review_report": str(report.resolve()),
        "metadata_changes": metadata_changes,
    }


def write_apply_plan(output: Path, plan: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def write_report(output: Path, rows: Sequence[dict[str, str]]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REPORT_HEADER)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def generate_report(
    snapshot: Path,
    audit: Path,
    output: Path,
    acoustid_api_key: str,
    matcher_factory: Callable[
        [str], contextlib.AbstractContextManager[BeetsMatcher]
    ] = BeetsMatcher,
) -> list[dict[str, str]]:
    loaded = load_input(snapshot, audit)
    audit_ids = {row.track.persistent_id for row in loaded.audit_rows}
    matched: dict[str, MatchEvidence] = {}
    with matcher_factory(acoustid_api_key) as matcher:
        for group in build_match_groups(loaded):
            for persistent_id, evidence in matcher.match_group(group).items():
                if persistent_id in audit_ids:
                    matched[persistent_id] = evidence
    rows = [make_report_row(row, matched[row.track.persistent_id]) for row in loaded.audit_rows]
    _sort_report_rows(rows)
    write_report(output, rows)
    return rows


def preflight(snapshot: Path, audit: Path, output: Path, plan_output: Path) -> str:
    api_key = os.environ.get("ACOUSTID_API_KEY", "")
    if not api_key:
        raise ValueError("ACOUSTID_API_KEY is required")
    if shutil.which("fpcalc") is None:
        raise ValueError("fpcalc is required; install Chromaprint")
    if not snapshot.is_file():
        raise ValueError(f"snapshot is not a file: {snapshot}")
    if not audit.is_file():
        raise ValueError(f"audit report is not a file: {audit}")
    if path_key(output) == path_key(plan_output):
        raise ValueError("report and plan outputs must be different")
    if os.path.lexists(output):
        raise ValueError(f"output already exists: {output}")
    if os.path.lexists(plan_output):
        raise ValueError(f"plan output already exists: {plan_output}")
    return api_key


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Match Apple Music snapshot metadata with AcoustID and MusicBrainz."
    )
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plan-output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv()
    report_published = False
    try:
        api_key = preflight(args.snapshot, args.audit, args.output, args.plan_output)
        rows = generate_report(args.snapshot, args.audit, args.output, api_key)
        report_published = True
        plan = make_apply_plan(args.snapshot, args.audit, args.output, rows)
        write_apply_plan(args.plan_output, plan)
    except Exception as error:  # noqa: BLE001 - failures must not leave a report without its plan.
        if report_published:
            args.output.unlink(missing_ok=True)
        print(f"error: {error}", file=sys.stderr)
        return 1

    counts = Counter(row["match_status"] for row in rows)
    print(f"snapshot: {args.snapshot}")
    print(f"audit rows: {len(rows)}")
    print(f"strong candidates: {counts['strong_candidate']}")
    print(f"needs review: {counts['needs_review']}")
    print(f"no change: {counts['no_change']}")
    print(f"unmatched: {counts['unmatched']}")
    print(f"unavailable: {counts['unavailable']}")
    print(f"plan entries: {len(plan['metadata_changes'])}")
    print(f"report: {args.output}")
    print(f"apply plan: {args.plan_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
