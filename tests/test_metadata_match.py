from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from apple_music_export import write_snapshot
from apple_music_metadata_match import (
    AUDIT_HEADER,
    BEETS_CONFIG,
    REPORT_HEADER,
    AuditRow,
    BeetsMatcher,
    LoadedInput,
    MatchEvidence,
    SnapshotTrack,
    _classify,
    build_match_groups,
    display_norm,
    generate_report,
    load_input,
    main,
    make_apply_plan,
    make_report_row,
    media_path,
    path_key,
    preflight,
    write_apply_plan,
    write_report,
)


class FakeDistance:
    def __init__(self, value: float = 0.01, penalties: dict[str, float] | None = None) -> None:
        self.value = value
        self.penalties = penalties or {"track_title": value}

    def __float__(self) -> float:
        return self.value

    def __iter__(self):
        return iter(self.penalties.items())


class ScriptedMatcher:
    def __init__(self, _api_key: str, callback) -> None:
        self.callback = callback

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def match_group(self, tracks):
        return self.callback(tracks)


class MetadataMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.media = self.root / "Album"
        self.media.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def wav(self, name: str) -> Path:
        path = self.media / name
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8_000)
            handle.writeframes(b"\0\0" * 800)
        return path

    def track(
        self,
        persistent_id: str,
        *,
        title: str = "Current Title",
        artist: str = "Current Artist",
        album: str = "Current Album",
        duration: float = 10.0,
        location: str | None = None,
    ) -> dict[str, object]:
        return {
            "persistent_id": persistent_id,
            "database_id": int(persistent_id.strip("T") or "1"),
            "name": title,
            "artist": artist,
            "album": album,
            "duration": duration,
            "location": location,
            "rating": 0,
            "favorited": False,
        }

    def snapshot(self, tracks: list[dict[str, object]]) -> Path:
        return write_snapshot({"tracks": tracks, "playlists": []}, self.root)[0]

    def audit_row(self, snapshot: Path, track: dict[str, object], **changes: str) -> dict[str, str]:
        row = {
            "snapshot": str(snapshot),
            "persistent_id": str(track["persistent_id"]),
            "status": "review",
            "reasons": "private audit reason",
            "current_title": str(track["name"]),
            "current_artist": str(track["artist"]),
            "current_album": str(track["album"]),
            "suggested_title": "",
            "suggested_artist": "",
            "suggested_album": "",
            "evidence": "audit evidence",
            "source_urls": "https://example.test/audit",
            "playlists": "Library",
            "duration_seconds": str(track["duration"]),
            "location": "" if track["location"] is None else str(track["location"]),
        }
        row.update(changes)
        return row

    def audit(self, rows: list[dict[str, str]], header: list[str] = AUDIT_HEADER) -> Path:
        path = self.root / f"audit-{len(list(self.root.glob('audit-*')))}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)
        return path

    @staticmethod
    def lookup(acoustid_id: str, recording_id: str, release_ids: list[str], score=0.9):
        return {
            "status": "ok",
            "results": [
                {
                    "id": acoustid_id,
                    "score": score,
                    "recordings": [
                        {
                            "id": recording_id,
                            "releases": [{"id": release_id} for release_id in release_ids],
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def proposal(*candidates, recommendation: str = "strong"):
        return SimpleNamespace(
            candidates=list(candidates), recommendation=SimpleNamespace(name=recommendation)
        )

    def test_loader_rejects_stale_or_changed_audit_rows(self) -> None:
        media = self.wav("one.wav")
        track = self.track("T1", location=media.as_uri())
        snapshot = self.snapshot([track])
        valid = self.audit_row(snapshot, track)

        cases = {
            "stale snapshot": [dict(valid, snapshot=str(self.root / "stale.sqlite3"))],
            "duplicate id": [valid, valid],
            "unknown id": [dict(valid, persistent_id="T9")],
            "changed title": [dict(valid, current_title="Changed")],
            "changed artist": [dict(valid, current_artist="Changed")],
            "changed album": [dict(valid, current_album="Changed")],
            "changed location": [dict(valid, location=str(media))],
            "changed duration": [dict(valid, duration_seconds="11")],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                audit = self.audit(rows)
                with self.assertRaises(ValueError):
                    load_input(snapshot, audit)

        empty = self.audit([])
        with self.assertRaisesRegex(ValueError, "empty"):
            load_input(snapshot, empty)
        malformed = self.audit(
            [{key: value for key, value in valid.items() if key in AUDIT_HEADER[:-1]}],
            AUDIT_HEADER[:-1],
        )
        with self.assertRaisesRegex(ValueError, "header"):
            load_input(snapshot, malformed)

    def test_preflight_rejects_output_collisions(self) -> None:
        snapshot = self.root / "snapshot.sqlite3"
        audit = self.root / "audit.csv"
        output = self.root / "output.csv"
        plan_output = self.root / "plan.json"
        snapshot.touch()
        audit.touch()
        with (
            mock.patch.dict(os.environ, {"ACOUSTID_API_KEY": "key"}),
            mock.patch("apple_music_metadata_match.shutil.which", return_value="/usr/bin/fpcalc"),
        ):
            output.touch()
            with self.assertRaisesRegex(ValueError, f"output already exists: {output}"):
                preflight(snapshot, audit, output, plan_output)
            output.unlink()

            plan_output.touch()
            with self.assertRaisesRegex(ValueError, f"plan output already exists: {plan_output}"):
                preflight(snapshot, audit, output, plan_output)
            plan_output.unlink()

            for report, plan in (
                (output, output),
                (
                    self.root / "métadata.json",
                    self.root / unicodedata.normalize("NFD", "métadata.json"),
                ),
            ):
                with (
                    self.subTest(report=report, plan=plan),
                    self.assertRaisesRegex(ValueError, "report and plan outputs must be different"),
                ):
                    preflight(snapshot, audit, report, plan)

    def test_main_loads_dotenv_before_preflight(self) -> None:
        import dotenv

        snapshot = self.root / "snapshot.sqlite3"
        audit = self.root / "audit.csv"
        output = self.root / "output.csv"
        plan_output = self.root / "plan.json"
        dotenv_file = self.root / ".env"
        snapshot.touch()
        audit.touch()
        dotenv_file.write_text("ACOUSTID_API_KEY=dotenv-key\n", encoding="utf-8")
        generated_rows: list[dict[str, str]] = []
        plan = {"metadata_changes": []}
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "apple_music_metadata_match.load_dotenv",
                side_effect=lambda: dotenv.load_dotenv(dotenv_file),
            ),
            mock.patch("apple_music_metadata_match.shutil.which", return_value="/usr/bin/fpcalc"),
            mock.patch(
                "apple_music_metadata_match.generate_report",
                return_value=generated_rows,
            ) as generate_report_mock,
            mock.patch(
                "apple_music_metadata_match.make_apply_plan", return_value=plan
            ) as make_apply_plan_mock,
            mock.patch("apple_music_metadata_match.write_apply_plan") as write_apply_plan_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = main(
                [
                    "--snapshot",
                    str(snapshot),
                    "--audit",
                    str(audit),
                    "--output",
                    str(output),
                    "--plan-output",
                    str(plan_output),
                ]
            )
        self.assertEqual(result, 0)
        generate_report_mock.assert_called_once_with(snapshot, audit, output, "dotenv-key")
        make_apply_plan_mock.assert_called_once_with(snapshot, audit, output, generated_rows)
        write_apply_plan_mock.assert_called_once_with(plan_output, plan)

    def test_null_and_file_url_locations_bind_before_decoding(self) -> None:
        null_track = self.track("T1", location=None)
        encoded_path = self.wav("encoded name.wav")
        file_track = self.track("T2", location=encoded_path.as_uri(), album="")
        snapshot = self.snapshot([null_track, file_track])
        audit = self.audit(
            [self.audit_row(snapshot, null_track), self.audit_row(snapshot, file_track)]
        )
        output = self.root / "matches.csv"

        import acoustid
        import beets.autotag

        fingerprint_paths: list[Path] = []

        def fingerprint(path):
            fingerprint_paths.append(path)
            return 10.0, b"fingerprint"

        with (
            mock.patch.object(acoustid, "fingerprint_file", side_effect=fingerprint),
            mock.patch.object(acoustid, "lookup", return_value={"status": "ok", "results": []}),
            mock.patch.object(beets.autotag, "tag_item") as tag_item,
        ):
            rows = generate_report(snapshot, audit, output, "key")

        by_id = {row["persistent_id"]: row for row in rows}
        self.assertEqual(by_id["T1"]["match_status"], "unavailable")
        self.assertEqual(by_id["T1"]["acoustid_id"], "")
        self.assertEqual(by_id["T2"]["match_status"], "unmatched")
        self.assertEqual(fingerprint_paths, [encoded_path])
        tag_item.assert_not_called()

    def test_album_uses_release_supported_by_both_tracks(self) -> None:
        paths = [self.wav("one.wav"), self.wav("two.wav")]
        tracks = [
            self.track("T1", title="Old One", location=paths[0].as_uri()),
            self.track("T2", title="Old Two", location=paths[1].as_uri()),
        ]
        snapshot = self.snapshot(tracks)
        audit = self.audit([self.audit_row(snapshot, track) for track in tracks])
        output = self.root / "matches.csv"
        recording_by_name = {"one.wav": "recording-1", "two.wav": "recording-2"}

        import acoustid
        import beets.autotag

        def lookup(_key, fingerprint, _duration, **_kwargs):
            name = fingerprint.decode()
            return self.lookup(f"acoustid-{name}", recording_by_name[name], ["release-common"])

        def tag_album(items, search_ids):
            self.assertEqual(search_ids, ["release-common"])
            infos = [
                SimpleNamespace(
                    title="New One", artist="New Artist", length=10.0, track_id="recording-1"
                ),
                SimpleNamespace(
                    title="New Two", artist="New Artist", length=10.0, track_id="recording-2"
                ),
            ]
            candidate = SimpleNamespace(
                info=SimpleNamespace(album="New Album", album_id="release-common"),
                mapping=dict(zip(items, infos)),
                extra_items=[],
                extra_tracks=[],
                distance=FakeDistance(),
            )
            second_infos = [
                SimpleNamespace(
                    title="Second One",
                    artist="Second Artist",
                    length=10.0,
                    track_id="recording-1",
                ),
                SimpleNamespace(
                    title="Second Two",
                    artist="Second Artist",
                    length=10.0,
                    track_id="recording-2",
                ),
            ]
            second_candidate = SimpleNamespace(
                info=SimpleNamespace(album="Second Album", album_id="release-second"),
                mapping=dict(zip(items, second_infos)),
                extra_items=[],
                extra_tracks=[],
                distance=FakeDistance(0.02),
            )
            return (
                "Current Artist",
                "Current Album",
                self.proposal(candidate, second_candidate),
            )

        before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
        with (
            mock.patch.object(
                acoustid, "fingerprint_file", side_effect=lambda path: (10.0, path.name.encode())
            ),
            mock.patch.object(acoustid, "lookup", side_effect=lookup),
            mock.patch.object(beets.autotag, "tag_album", side_effect=tag_album),
            mock.patch.object(beets.autotag, "tag_item") as tag_item,
        ):
            rows = generate_report(snapshot, audit, output, "key")
        after = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]

        self.assertEqual(before, after)
        self.assertEqual({row["match_status"] for row in rows}, {"strong_candidate"})
        self.assertEqual({row["suggested_album"] for row in rows}, {"New Album"})
        self.assertEqual({row["musicbrainz_release_id"] for row in rows}, {"release-common"})
        self.assertEqual({row["current_title"] for row in rows}, {"Old One", "Old Two"})
        with output.open(encoding="utf-8", newline="") as handle:
            csv_rows = list(csv.DictReader(handle))
        self.assertEqual({row["suggested_title"] for row in csv_rows}, {"New One", "New Two"})
        self.assertEqual({row["suggested_artist"] for row in csv_rows}, {"New Artist"})
        plan = make_apply_plan(snapshot, audit, output, rows)
        self.assertEqual(
            {change["suggested"]["album"] for change in plan["metadata_changes"]},
            {"New Album"},
        )
        self.assertNotIn("Second", json.dumps(plan))
        tag_item.assert_not_called()

    def test_single_track_release_support_falls_back_to_singletons(self) -> None:
        paths = [self.wav("one.wav"), self.wav("two.wav")]
        tracks = [
            self.track("T1", location=paths[0].as_uri()),
            self.track("T2", location=paths[1].as_uri()),
        ]
        snapshot = self.snapshot(tracks)
        audit = self.audit([self.audit_row(snapshot, track) for track in tracks])
        output = self.root / "matches.csv"

        import acoustid
        import beets.autotag

        def lookup(_key, fingerprint, _duration, **_kwargs):
            name = fingerprint.decode()
            releases = ["release-one"] if name == "one.wav" else ["release-two"]
            return self.lookup(f"a-{name}", f"r-{name}", releases)

        def tag_item(item, search_ids):
            info = SimpleNamespace(
                title="First Title",
                artist=item.artist,
                length=item.length,
                track_id=search_ids[0],
            )
            candidate = SimpleNamespace(info=info, distance=FakeDistance())
            second_info = SimpleNamespace(
                title="Second Title",
                artist="Second Artist",
                length=item.length,
                track_id=search_ids[0],
            )
            second_candidate = SimpleNamespace(info=second_info, distance=FakeDistance(0.02))
            return self.proposal(candidate, second_candidate)

        with (
            mock.patch.object(
                acoustid, "fingerprint_file", side_effect=lambda path: (10.0, path.name.encode())
            ),
            mock.patch.object(acoustid, "lookup", side_effect=lookup),
            mock.patch.object(beets.autotag, "tag_album") as tag_album,
            mock.patch.object(beets.autotag, "tag_item", side_effect=tag_item) as tag_item_mock,
        ):
            rows = generate_report(snapshot, audit, output, "key")
        tag_album.assert_not_called()
        self.assertEqual(tag_item_mock.call_count, 2)
        with output.open(encoding="utf-8", newline="") as handle:
            csv_rows = list(csv.DictReader(handle))
        self.assertEqual({row["suggested_title"] for row in csv_rows}, {"First Title"})
        plan = make_apply_plan(snapshot, audit, output, rows)
        self.assertEqual(
            {change["suggested"]["title"] for change in plan["metadata_changes"]},
            {"First Title"},
        )
        self.assertNotIn("Second", json.dumps(plan))

    def test_acoustid_results_are_sorted_deduplicated_cached_and_limited(self) -> None:
        evidence = BeetsMatcher._parse_lookup(
            {
                "status": "ok",
                "results": [
                    {
                        "id": "z",
                        "score": 0.7,
                        "recordings": [{"id": "recording-2", "releases": [{"id": "release-2"}]}],
                    },
                    {
                        "id": "b",
                        "score": 0.9,
                        "recordings": [{"id": "recording-1", "releases": [{"id": "release-b"}]}],
                    },
                    {
                        "id": "a",
                        "score": 0.9,
                        "recordings": [
                            {
                                "id": "recording-1",
                                "releases": [{"id": "release-a"}],
                            },
                            {"id": "recording-3", "releases": []},
                        ],
                    },
                    {"id": "ignored", "score": 0.4, "recordings": []},
                ],
            }
        )
        self.assertEqual(evidence.acoustid_id, "a")
        self.assertEqual(
            evidence.recording_scores,
            (("recording-1", 0.9), ("recording-3", 0.9), ("recording-2", 0.7)),
        )
        self.assertEqual(evidence.top_release_ids, ("release-a",))

        paths = [self.wav("limited-one.wav"), self.wav("limited-two.wav")]
        starts: list[float] = []
        acoustid = SimpleNamespace(
            fingerprint_file=mock.Mock(side_effect=[(10.0, b"one"), (10.0, b"two")]),
            lookup=mock.Mock(
                side_effect=lambda *_args, **_kwargs: (
                    starts.append(time.monotonic()) or {"status": "ok", "results": []}
                )
            ),
        )
        matcher = BeetsMatcher("key")
        matcher._acoustid = acoustid
        tracks = [
            SnapshotTrack(f"T{i}", "Title", "Artist", "", 10.0, str(path))
            for i, path in enumerate(paths)
        ]
        matcher._fingerprint(tracks[0])
        matcher._fingerprint(tracks[1])
        matcher._fingerprint(tracks[0])
        self.assertGreaterEqual(starts[1] - starts[0], 0.33)
        self.assertEqual(acoustid.fingerprint_file.call_count, 2)
        self.assertEqual(acoustid.lookup.call_count, 2)

    def test_group_filters_empty_unknown_large_and_unreadable_siblings(self) -> None:
        path = self.wav("one.wav")
        unknown = self.root / "Unknown Album"
        unknown.mkdir()
        empty_album_tracks = tuple(
            SnapshotTrack(f"E{i}", "Title", "Artist", "", 10.0, str(path)) for i in range(2)
        )
        unknown_tracks = tuple(
            SnapshotTrack(f"U{i}", "Title", "Artist", "Album", 10.0, str(unknown / f"{i}.wav"))
            for i in range(2)
        )
        large_tracks = tuple(
            SnapshotTrack(f"L{i}", "Title", "Artist", "Album", 10.0, str(self.media / f"{i}.wav"))
            for i in range(101)
        )
        unreadable_tracks = (
            SnapshotTrack("R1", "Title", "Artist", "Album", 10.0, str(path)),
            SnapshotTrack("R2", "Title", "Artist", "Album", 10.0, str(self.media / "missing.wav")),
        )
        for tracks in (empty_album_tracks, unknown_tracks, large_tracks, unreadable_tracks):
            loaded = LoadedInput(
                tracks,
                (
                    AuditRow(
                        "snapshot",
                        tracks[0],
                        "review",
                        "",
                        "",
                        "",
                        "",
                        "10",
                        tracks[0].location or "",
                    ),
                ),
            )
            groups = build_match_groups(loaded)
            self.assertEqual(groups, [(tracks[0],)])

    def test_empty_album_singleton_never_suggests_release_metadata(self) -> None:
        track = SnapshotTrack("T1", "Old", "Old Artist", "", 10.0, "file")
        row = AuditRow("snapshot", track, "review", "", "", "", "", "10", "file")
        evidence = MatchEvidence(
            kind="singleton",
            resolved=True,
            candidate_title="New",
            candidate_artist="New Artist",
            candidate_duration=10.0,
            recommendation="strong",
            distance=0.01,
            acoustid_id="acoustid",
            acoustid_score=0.9,
            acoustid_recording_ids=frozenset({"recording"}),
            acoustid_release_ids=("release-a", "release-b"),
            recording_id="recording",
        )
        report = make_report_row(row, evidence)
        self.assertEqual(report["match_status"], "needs_review")
        self.assertEqual(report["suggested_title"], "New")
        self.assertEqual(report["suggested_artist"], "New Artist")
        self.assertEqual(report["suggested_album"], "")
        self.assertEqual(report["musicbrainz_release_id"], "")
        self.assertIn("acoustid_release_ids=release-a,release-b", report["evidence"])
        self.assertIn("https://musicbrainz.org/release/release-a", report["source_urls"])

        unchanged = replace(evidence, candidate_title="Old", candidate_artist="Old Artist")
        self.assertEqual(make_report_row(row, unchanged)["match_status"], "needs_review")

    def test_strong_candidate_requires_every_guard(self) -> None:
        track = SnapshotTrack("T1", "Old", "Artist", "Album", 10.0, "file")
        row = AuditRow("snapshot", track, "review", "", "", "", "", "10", "file")
        evidence = MatchEvidence(
            kind="album",
            resolved=True,
            candidate_title="New",
            candidate_artist="Artist",
            candidate_album="Album",
            candidate_duration=10.0,
            recommendation="strong",
            acoustid_recording_ids=frozenset({"recording"}),
            recording_id="recording",
            album_all_supported=True,
        )
        self.assertEqual(_classify(row, evidence), "strong_candidate")
        broken = (
            replace(evidence, recommendation="medium"),
            replace(evidence, acoustid_recording_ids=frozenset()),
            replace(evidence, candidate_duration=16.0),
            replace(evidence, candidate_title="New Live"),
            replace(evidence, extra_items=1),
            replace(evidence, extra_tracks=1),
            replace(evidence, album_all_supported=False),
        )
        for case in broken:
            self.assertEqual(_classify(row, case), "needs_review")

        album_same = replace(evidence, candidate_title="Old")
        self.assertEqual(_classify(row, album_same), "no_change")
        singleton_same = replace(
            album_same, kind="singleton", candidate_album="", album_all_supported=False
        )
        self.assertEqual(_classify(row, singleton_same), "no_change")
        self.assertEqual(_classify(row, MatchEvidence(kind="singleton")), "unmatched")
        self.assertEqual(
            _classify(row, MatchEvidence(kind="singleton", error="missing")), "unavailable"
        )

    def test_apply_plan_is_exact_deterministic_and_review_gated(self) -> None:
        snapshot = self.root / "snapshot.sqlite3"
        audit = self.root / "audit.csv"
        report = self.root / "report.csv"
        rows = [
            {
                "persistent_id": "T1",
                "match_status": "strong_candidate",
                "current_title": "Café Title",
                "current_artist": "Artist",
                "current_album": "Album",
                "suggested_title": "Café New",
                "suggested_artist": "",
                "suggested_album": "",
            },
            {
                "persistent_id": "T2",
                "match_status": "needs_review",
                "current_title": "Title",
                "current_artist": "Old Artist",
                "current_album": "Old Album",
                "suggested_title": "",
                "suggested_artist": "New Artist",
                "suggested_album": "New Album",
            },
            {
                "persistent_id": "T3",
                "match_status": "needs_review",
                "current_title": "Title",
                "current_artist": "Artist",
                "current_album": "Album",
                "suggested_title": "",
                "suggested_artist": "",
                "suggested_album": "",
            },
            {
                "persistent_id": "T4",
                "match_status": "unmatched",
                "current_title": "Title",
                "current_artist": "Artist",
                "current_album": "Album",
                "suggested_title": "Ignored",
                "suggested_artist": "",
                "suggested_album": "",
            },
        ]

        plan = make_apply_plan(snapshot, audit, report, rows)
        self.assertEqual(
            plan,
            {
                "snapshot": str(snapshot.resolve()),
                "audit": str(audit.resolve()),
                "review_report": str(report.resolve()),
                "metadata_changes": [
                    {
                        "persistent_id": "T1",
                        "approved": False,
                        "match_status": "strong_candidate",
                        "current": {
                            "title": "Café Title",
                            "artist": "Artist",
                            "album": "Album",
                        },
                        "suggested": {"title": "Café New"},
                    },
                    {
                        "persistent_id": "T2",
                        "approved": False,
                        "match_status": "needs_review",
                        "current": {
                            "title": "Title",
                            "artist": "Old Artist",
                            "album": "Old Album",
                        },
                        "suggested": {
                            "artist": "New Artist",
                            "album": "New Album",
                        },
                    },
                ],
            },
        )
        self.assertEqual(make_apply_plan(snapshot, audit, report, rows[2:])["metadata_changes"], [])

        outputs = [self.root / "first.json", self.root / "second.json"]
        for output in outputs:
            write_apply_plan(output, plan)
        payload = outputs[0].read_bytes()
        self.assertEqual(payload, outputs[1].read_bytes())
        self.assertIn("Café".encode(), payload)
        self.assertIn(b'\n  "metadata_changes": [\n', payload)
        self.assertTrue(payload.endswith(b"\n"))

    def test_deterministic_exact_report_and_sorting(self) -> None:
        paths = [self.wav("one.wav"), self.wav("two.wav")]
        tracks = [
            self.track("T1", title="Zulu", artist="B", location=paths[0].as_uri(), album=""),
            self.track("T2", title="Alpha", artist="A", location=paths[1].as_uri(), album=""),
        ]
        snapshot = self.snapshot(tracks)
        audit = self.audit([self.audit_row(snapshot, track) for track in tracks])

        def callback(group):
            track = group[0]
            return {
                track.persistent_id: MatchEvidence(
                    kind="singleton",
                    reason="no MusicBrainz candidate resolved",
                    acoustid_id=f"a-{track.persistent_id}",
                    acoustid_score=0.75,
                    acoustid_release_ids=(f"release-{track.persistent_id}",),
                )
            }

        factory = lambda key: ScriptedMatcher(key, callback)
        outputs = [self.root / "first.csv", self.root / "second.csv"]
        for output in outputs:
            generate_report(snapshot, audit, output, "key", factory)
        self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
        with outputs[0].open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            self.assertEqual(reader.fieldnames, REPORT_HEADER)
        self.assertEqual([row["persistent_id"] for row in rows], ["T2", "T1"])
        self.assertTrue(all(row["acoustid_score"] == "0.750000" for row in rows))
        self.assertTrue(all(row["evidence"] for row in rows))
        self.assertTrue(all(row["match_status"] == "unmatched" for row in rows))

    def test_transport_failures_publish_nothing_and_leave_no_temp(self) -> None:
        paths = [self.wav("one.wav"), self.wav("two.wav")]
        tracks = [
            self.track("T1", location=paths[0].as_uri(), album=""),
            self.track("T2", location=paths[1].as_uri(), album=""),
        ]
        snapshot = self.snapshot(tracks)
        audit = self.audit([self.audit_row(snapshot, track) for track in tracks])
        calls = 0

        def callback(group):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("MusicBrainz transport failed")
            track = group[0]
            return {track.persistent_id: MatchEvidence(kind="singleton", reason="empty")}

        output = self.root / "matches.csv"
        with self.assertRaisesRegex(RuntimeError, "transport"):
            generate_report(
                snapshot,
                audit,
                output,
                "key",
                lambda key: ScriptedMatcher(key, callback),
            )
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(f".{output.name}.*.tmp")), [])

        import acoustid

        acoustid_output = self.root / "acoustid-error.csv"
        with (
            mock.patch.object(acoustid, "fingerprint_file", return_value=(10.0, b"fingerprint")),
            mock.patch.object(
                acoustid, "lookup", side_effect=RuntimeError("AcoustID transport failed")
            ),
            self.assertRaisesRegex(RuntimeError, "AcoustID transport"),
        ):
            generate_report(snapshot, audit, acoustid_output, "key")
        self.assertFalse(acoustid_output.exists())
        self.assertEqual(list(self.root.glob(f".{acoustid_output.name}.*.tmp")), [])

    def test_atomic_writer_cleans_up_and_main_prints_exact_summary(self) -> None:
        output = self.root / "atomic.csv"
        writer = mock.Mock()
        writer.writerows.side_effect = RuntimeError("row failed")
        with (
            mock.patch("apple_music_metadata_match.csv.DictWriter", return_value=writer),
            self.assertRaisesRegex(RuntimeError, "row failed"),
        ):
            write_report(output, [])
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(f".{output.name}.*.tmp")), [])

        plan_output = self.root / "atomic.json"
        with self.assertRaises(TypeError):
            write_apply_plan(plan_output, {"not_serializable": object()})
        self.assertFalse(plan_output.exists())
        self.assertEqual(list(self.root.glob(f".{plan_output.name}.*.tmp")), [])

        failed_report = self.root / "failed-report.csv"
        failed_plan = self.root / "failed-plan.json"

        def publish_report(_snapshot, _audit, report, _key):
            report.write_text("published\n", encoding="utf-8")
            return []

        with (
            mock.patch("apple_music_metadata_match.preflight", return_value="key"),
            mock.patch("apple_music_metadata_match.generate_report", side_effect=publish_report),
            mock.patch(
                "apple_music_metadata_match.make_apply_plan",
                return_value={"metadata_changes": [], "not_serializable": object()},
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = main(
                [
                    "--snapshot",
                    str(self.root / "failed-snapshot.sqlite3"),
                    "--audit",
                    str(self.root / "failed-audit.csv"),
                    "--output",
                    str(failed_report),
                    "--plan-output",
                    str(failed_plan),
                ]
            )
        self.assertEqual(result, 1)
        self.assertFalse(failed_report.exists())
        self.assertFalse(failed_plan.exists())
        self.assertEqual(list(self.root.glob(f".{failed_plan.name}.*.tmp")), [])

        statuses = [
            "strong_candidate",
            "needs_review",
            "no_change",
            "unmatched",
            "unavailable",
        ]
        rows = [
            {
                "persistent_id": f"T{index}",
                "match_status": status,
                "current_title": "Current Title",
                "current_artist": "Current Artist",
                "current_album": "Current Album",
                "suggested_title": "Suggested Title" if index == 1 else "",
                "suggested_artist": "",
                "suggested_album": "",
            }
            for index, status in enumerate(statuses, start=1)
        ]
        snapshot = self.root / "summary-snapshot.sqlite3"
        audit = self.root / "summary-audit.csv"
        output = self.root / "summary-report.csv"
        plan_output = self.root / "summary-plan.json"
        stdout = io.StringIO()
        with (
            mock.patch("apple_music_metadata_match.preflight", return_value="key"),
            mock.patch("apple_music_metadata_match.generate_report", return_value=rows),
            contextlib.redirect_stdout(stdout),
        ):
            result = main(
                [
                    "--snapshot",
                    str(snapshot),
                    "--audit",
                    str(audit),
                    "--output",
                    str(output),
                    "--plan-output",
                    str(plan_output),
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                f"snapshot: {snapshot}",
                "audit rows: 5",
                "strong candidates: 1",
                "needs review: 1",
                "no change: 1",
                "unmatched: 1",
                "unavailable: 1",
                "plan entries: 1",
                f"report: {output}",
                f"apply plan: {plan_output}",
            ],
        )
        self.assertEqual(
            json.loads(plan_output.read_text(encoding="utf-8"))["metadata_changes"][0][
                "persistent_id"
            ],
            "T1",
        )
        self.assertFalse(Path(output.name).exists())
        self.assertFalse(Path(plan_output.name).exists())

    def test_helpers_normalize_without_changing_display_values(self) -> None:
        self.assertEqual(display_norm("  Café\tTITLE "), "café title")
        path = self.root / "folder" / ".." / "Album"
        self.assertEqual(path_key(path), os.path.normpath(path_key(path)))
        self.assertIsNone(media_path(None))
        encoded = (self.root / "a b.wav").as_uri()
        self.assertEqual(media_path(encoded), self.root / "a b.wav")

    def test_beets_config_isolation_ignores_hostile_parent(self) -> None:
        hostile = self.root / "hostile"
        hostile.mkdir()
        (hostile / "config.yaml").write_text(
            "plugins: [chroma]\nraise_on_error: no\nmatch:\n  strong_rec_thresh: 0.0\n"
            "  distance_weights:\n    track_title: 99\npreferred:\n  countries: [XX]\n",
            encoding="utf-8",
        )
        script = """
import json
import os
from unittest import mock
import apple_music_metadata_match as module
with module.BeetsMatcher('key'):
    import beets
    import beets.autotag
    import beets.metadata_plugins
    import beets.plugins
    from beets.autotag import TrackInfo
    from beets.library import Item
    item = Item(title='Title', artist='Artist', album='Album', length=10.0)
    info = TrackInfo(title='New Title', artist='Artist', length=10.0, track_id='recording', data_source='MusicBrainz')
    with mock.patch.object(beets.metadata_plugins, 'tracks_for_ids', return_value=[info]):
        proposal = beets.autotag.tag_item(item, search_ids=['recording'])
    print(json.dumps({
        'recommendation': proposal.recommendation.name,
        'distance': round(float(proposal.candidates[0].distance), 8),
        'raise_on_error': beets.config['raise_on_error'].get(bool),
        'plugins': sorted(plugin.name for plugin in beets.plugins.find_plugins()),
        'config': module.BEETS_CONFIG,
    }, sort_keys=True))
"""

        def run(beetsdir: Path | None) -> str:
            environment = os.environ.copy()
            if beetsdir is None:
                environment.pop("BEETSDIR", None)
            else:
                environment["BEETSDIR"] = str(beetsdir)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).parents[1],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()

        isolated = run(hostile)
        clean = run(None)
        self.assertEqual(isolated, clean)
        payload = json.loads(isolated)
        self.assertTrue(payload["raise_on_error"])
        self.assertEqual(payload["plugins"], ["musicbrainz"])
        self.assertEqual(payload["config"], BEETS_CONFIG)


if __name__ == "__main__":
    unittest.main()
