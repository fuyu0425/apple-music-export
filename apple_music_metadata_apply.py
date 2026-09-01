from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import app
from apple_music_apply import (
    _require_active_library,
    _require_music_running,
    copy_library_package,
)
from apple_music_export import collect_library, write_snapshot

USER_AGENT = "apple-music-export/0.1"
MAX_ARTWORK_BYTES = 10 * 1024 * 1024
METADATA_FIELDS = ("title", "artist", "album")

JXA_METADATA_SCRIPT = r"""
const plan = __PLAN__;
const action = __ACTION__;
const music = Application("Music");

function entry(id) {
    return {persistent_id: id, metadata: {}};
}

function main() {
    const preflightErrors = [];
    const entries = [];
    const failures = [];
    const tracks = music.libraryPlaylists[0].fileTracks;

    for (const change of plan) {
        const report = entry(change.persistent_id);
        entries.push(report);
        const matches = tracks.whose({persistentID: change.persistent_id})();
        if (matches.length !== 1) {
            preflightErrors.push("expected exactly one file track for " +
                                 change.persistent_id + ", found " + matches.length);
            continue;
        }
        const track = matches[0];
        if (action === "rollback") {
            for (const field of change.rollback_fields) {
                try {
                    const property = field === "title" ? "name" : field;
                    const live = String(track[property]());
                    if (live === change.current[field]) {
                        report.metadata[field] = "already_restored";
                    } else if (live === change.suggested[field]) {
                        track[property] = change.current[field];
                        report.metadata[field] = "restored";
                    } else {
                        failures.push({persistent_id: change.persistent_id,
                                       field: field,
                                       error: "live value differs from rollback values"});
                    }
                } catch (error) {
                    failures.push({persistent_id: change.persistent_id,
                                   field: field, error: String(error)});
                }
            }
            continue;
        }
        for (const field of Object.keys(change.suggested)) {
            try {
                const property = field === "title" ? "name" : field;
                const live = String(track[property]());
                if (live === change.suggested[field]) {
                    report.metadata[field] = "already_applied";
                } else if (live === change.current[field]) {
                    report.metadata[field] = "planned";
                } else {
                    preflightErrors.push("live " + field + " differs from both current and " +
                                         "suggested for " + change.persistent_id);
                }
            } catch (error) {
                preflightErrors.push("could not read " + field + " for " +
                                     change.persistent_id + ": " + String(error));
            }
        }
    }

    if (action !== "apply" || preflightErrors.length) {
        return {mode: action, preflight_errors: preflightErrors,
                entries: entries, failures: failures};
    }

    for (let i = 0; i < plan.length; i++) {
        const change = plan[i];
        const report = entries[i];
        const track = tracks.whose({persistentID: change.persistent_id})()[0];
        for (const field of Object.keys(change.suggested)) {
            try {
                const property = field === "title" ? "name" : field;
                const live = String(track[property]());
                if (live === change.suggested[field]) {
                    report.metadata[field] = "already_applied";
                } else if (live === change.current[field]) {
                    track[property] = change.suggested[field];
                    report.metadata[field] = "applied";
                } else {
                    failures.push({persistent_id: change.persistent_id, field: field,
                                   error: "live value changed after preflight"});
                }
            } catch (error) {
                failures.push({persistent_id: change.persistent_id,
                               field: field, error: String(error)});
            }
        }
    }
    return {mode: action, preflight_errors: preflightErrors,
            entries: entries, failures: failures};
}

JSON.stringify(main());
"""

ARTWORK_SCRIPT = r"""
on run argv
    set operation to item 1 of argv
    set persistentId to item 2 of argv
    set imagePath to item 3 of argv

    tell application "Music"
        set matches to every file track of library playlist 1 whose persistent ID is persistentId
        if (count of matches) is not 1 then error "expected exactly one file track, found " & (count of matches)
        set targetTrack to item 1 of matches
        set artworkCount to count of artworks of targetTrack

        if operation is "inspect" then
            if artworkCount is 0 then return "missing"
            return "existing"
        else if operation is "add-if-missing" then
            if artworkCount is not 0 then return "existing"
            set imageData to read POSIX file imagePath as picture
            set data of artwork 1 of targetTrack to imageData
            return "added"
        else if operation is "delete-created" then
            if artworkCount is 0 then return "missing"
            if artworkCount is not 1 then error "expected exactly one artwork, found " & artworkCount
            delete artwork 1 of targetTrack
            return "deleted"
        else
            error "unsupported operation: " & operation
        end if
    end tell
end run
"""


class _ArtworkRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (
            host == "coverartarchive.org" or host == "archive.org" or host.endswith(".archive.org")
        ):
            raise urllib.error.URLError(f"artwork redirect is not allowed: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _result(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "preflight_errors": [],
        "entries": [],
        "failures": [],
        "rollback_failures": [],
        "subprocess_error": None,
        "artwork_errors": [],
        "post_snapshot": None,
        "post_export_error": None,
        "verification_failures": [],
    }


def _write_result(output: Path, result: dict[str, Any]) -> None:
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _error(result: dict[str, Any], message: str) -> None:
    result["preflight_errors"].append(message)


def _validate_artwork(artwork: object, persistent_id: str) -> str | None:
    if artwork is None:
        return None
    if not isinstance(artwork, dict) or set(artwork) != {"source", "release_id", "url"}:
        return f"invalid artwork for {persistent_id}"
    artwork_dict = cast(dict[str, Any], artwork)
    if artwork_dict.get("source") != "cover_art_archive":
        return f"invalid artwork source for {persistent_id}"
    release_id = artwork_dict.get("release_id")
    url = artwork_dict.get("url")
    if not isinstance(release_id, str) or not release_id:
        return f"invalid artwork release_id for {persistent_id}"
    if not isinstance(url, str) or urlparse(url).scheme != "https":
        return f"invalid artwork url for {persistent_id}"
    parsed = urlparse(url)
    if parsed.hostname != "coverartarchive.org" or not parsed.path.startswith(
        f"/release/{release_id}/"
    ):
        return f"artwork url does not match release for {persistent_id}"
    return None


def validate_plan(plan: object) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    if not isinstance(plan, dict):
        return [], ["apply plan must contain a JSON object"]
    plan_dict = cast(dict[str, Any], plan)
    for key in ("snapshot", "audit", "review_report"):
        if not isinstance(plan_dict.get(key), str):
            errors.append(f"apply plan {key} must be a string")
    changes = plan_dict.get("metadata_changes")
    if not isinstance(changes, list):
        return [], [*errors, "apply plan metadata_changes must be an array"]

    ids: set[str] = set()
    approved: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            errors.append("apply plan metadata change must be an object")
            continue
        persistent_id = change.get("persistent_id")
        if not isinstance(persistent_id, str) or not persistent_id:
            errors.append("apply plan persistent_id must be a nonempty string")
            continue
        if persistent_id in ids:
            errors.append(f"duplicate apply plan persistent_id: {persistent_id}")
        ids.add(persistent_id)
        if change.get("approved") not in (True, False) or not isinstance(
            change.get("approved"), bool
        ):
            errors.append(f"apply plan approved must be a boolean for {persistent_id}")
        if change.get("match_status") not in ("strong_candidate", "needs_review"):
            errors.append(f"invalid match_status for {persistent_id}")
        current = change.get("current")
        suggested = change.get("suggested")
        if not isinstance(current, dict) or any(
            not isinstance(current.get(field), str) for field in METADATA_FIELDS
        ):
            errors.append(f"invalid current metadata for {persistent_id}")
        if (
            not isinstance(suggested, dict)
            or not suggested
            or any(
                field not in METADATA_FIELDS or not isinstance(value, str) or not value
                for field, value in suggested.items()
            )
        ):
            errors.append(f"invalid suggested metadata for {persistent_id}")
        artwork_error = _validate_artwork(change.get("artwork"), persistent_id)
        if artwork_error:
            errors.append(artwork_error)
        if change.get("approved") is True:
            approved.append(change)
    if not approved:
        errors.append("apply plan has no approved metadata changes")
    return approved, errors


def run_metadata(changes: Sequence[dict[str, Any]], action: str) -> dict[str, Any]:
    if action not in {"preflight", "apply", "rollback"}:
        raise ValueError(f"invalid metadata action: {action}")
    script = JXA_METADATA_SCRIPT.replace(
        "__PLAN__", json.dumps(changes, ensure_ascii=False)
    ).replace("__ACTION__", json.dumps(action))
    descriptor, name = tempfile.mkstemp(prefix="apple-music-metadata-", suffix=".js")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(script)
        try:
            completed = subprocess.run(
                ["/usr/bin/osascript", "-l", "JavaScript", str(path)],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Apple Music metadata runner timed out after 300 seconds") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "Apple Music returned no error message"
            raise RuntimeError(f"Apple Music metadata runner failed: {detail}")
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Apple Music metadata runner returned invalid JSON") from error
        if not isinstance(report, dict) or set(report) != {
            "mode",
            "preflight_errors",
            "entries",
            "failures",
        }:
            raise RuntimeError("Apple Music metadata runner returned an invalid report")
        return report
    finally:
        path.unlink(missing_ok=True)


def run_artwork(operation: str, persistent_id: str, staged: Path | None) -> str:
    allowed = {
        "inspect": {"missing", "existing"},
        "add-if-missing": {"added", "existing"},
        "delete-created": {"deleted", "missing"},
    }
    if operation not in allowed:
        raise ValueError(f"invalid artwork operation: {operation}")
    try:
        completed = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                ARTWORK_SCRIPT,
                operation,
                persistent_id,
                str(staged) if staged is not None else "",
                "",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"artwork {operation} timed out after 30 seconds") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "Music returned no artwork error message"
        raise RuntimeError(f"artwork {operation} failed: {detail}")
    token = completed.stdout.strip()
    if token not in allowed[operation]:
        raise RuntimeError(f"artwork {operation} returned invalid output: {token!r}")
    return token


def _download_artwork(artwork: dict[str, str], destination: Path) -> dict[str, Any]:
    release_id = artwork["release_id"]
    url = artwork["url"]
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "coverartarchive.org"
        or not parsed.path.startswith(f"/release/{release_id}/")
    ):
        raise RuntimeError("artwork URL is not an exact Cover Art Archive release URL")
    opener = urllib.request.build_opener(_ArtworkRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=30) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > MAX_ARTWORK_BYTES:
                raise RuntimeError("artwork exceeds 10 MiB")
            body = bytearray()
            while chunk := response.read(min(64 * 1024, MAX_ARTWORK_BYTES + 1 - len(body))):
                body.extend(chunk)
                if len(body) > MAX_ARTWORK_BYTES:
                    raise RuntimeError("artwork exceeds 10 MiB")
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise RuntimeError(str(error) or type(error).__name__) from error
    if body.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif body.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    else:
        raise RuntimeError("artwork is not JPEG or PNG")
    path = destination.with_suffix(suffix)
    path.write_bytes(body)
    return {
        "path": str(path),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "pre_add_count": None,
    }


def _entry_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["persistent_id"]: entry for entry in result["entries"]}


def _merge_metadata(result: dict[str, Any], report: dict[str, Any]) -> None:
    entries = _entry_map(result)
    for report_entry in report["entries"]:
        entries[report_entry["persistent_id"]]["metadata"] = report_entry["metadata"]
    result["preflight_errors"].extend(report["preflight_errors"])
    result["failures"].extend(report["failures"])


def _artwork_error(
    result: dict[str, Any], persistent_id: str, operation: str, error: object
) -> None:
    result["artwork_errors"].append(
        {"persistent_id": persistent_id, "operation": operation, "error": str(error)}
    )


def _safe_delete_created(result: dict[str, Any], entry: dict[str, Any], snapshot: Path) -> None:
    persistent_id = entry["persistent_id"]
    staged = entry["staged_artwork"]
    if not staged or staged.get("pre_add_count") != 0:
        raise RuntimeError("missing recorded zero-artwork precondition")
    if run_artwork("inspect", persistent_id, None) != "existing":
        raise RuntimeError("created artwork is no longer present")
    app.track_artwork.cache_clear()
    live = app.track_artwork(snapshot, persistent_id)
    if live is None or hashlib.sha256(live[0]).hexdigest() != staged["sha256"]:
        raise RuntimeError("live artwork digest does not match the staged file")
    token = run_artwork("delete-created", persistent_id, Path(staged["path"]))
    if token not in {"deleted", "missing"}:
        raise RuntimeError(f"unexpected delete result: {token}")
    entry["artwork"] = "rolled_back"


def _rollback(
    result: dict[str, Any],
    approved: list[dict[str, Any]],
    snapshot: Path,
    *,
    uncertain_metadata: bool = False,
) -> None:
    entries = _entry_map(result)
    for change in reversed(approved):
        entry = entries[change["persistent_id"]]
        if entry["artwork"] != "added":
            continue
        try:
            _safe_delete_created(result, entry, snapshot)
        except (OSError, RuntimeError) as error:
            result["rollback_failures"].append(
                {
                    "persistent_id": change["persistent_id"],
                    "operation": "delete-created",
                    "error": str(error),
                }
            )
    rollback = []
    for change in approved:
        fields = (
            list(change["suggested"])
            if uncertain_metadata
            else [
                field
                for field, status in entries[change["persistent_id"]]["metadata"].items()
                if status == "applied"
            ]
        )
        if fields:
            rollback.append(
                {
                    "persistent_id": change["persistent_id"],
                    "current": change["current"],
                    "suggested": change["suggested"],
                    "rollback_fields": fields,
                }
            )
    if rollback:
        try:
            report = run_metadata(rollback, "rollback")
        except (OSError, RuntimeError) as error:
            result["rollback_failures"].append(
                {"persistent_id": "", "operation": "metadata", "error": str(error)}
            )
        else:
            result["rollback_failures"].extend(report["preflight_errors"])
            result["rollback_failures"].extend(report["failures"])
            for report_entry in report["entries"]:
                for field, status in report_entry["metadata"].items():
                    if status in {"restored", "already_restored"}:
                        entries[report_entry["persistent_id"]]["metadata"][field] = "rolled_back"


def _export_snapshot(output: Path, name: str) -> Path:
    generated, _ = write_snapshot(collect_library(), output)
    return generated.rename(output / name)


def _verify(result: dict[str, Any], approved: list[dict[str, Any]], snapshot: Path) -> None:
    entries = _entry_map(result)
    connection = app.connect_read_only(snapshot)
    try:
        for change in approved:
            persistent_id = change["persistent_id"]
            row = connection.execute(
                "SELECT name, artist, album FROM tracks WHERE persistent_id = ?",
                (persistent_id,),
            ).fetchone()
            if row is None:
                result["verification_failures"].append(
                    f"missing track in post snapshot: {persistent_id}"
                )
                continue
            live = dict(zip(METADATA_FIELDS, row))
            entry = entries[persistent_id]
            for field, suggested in change["suggested"].items():
                expected = (
                    change["current"][field]
                    if entry["metadata"].get(field) == "rolled_back"
                    else suggested
                )
                if live[field] != expected:
                    result["verification_failures"].append(
                        f"metadata mismatch for {persistent_id} {field}"
                    )
            if entry["artwork"] == "planned":
                result["verification_failures"].append(
                    f"residual planned artwork for {persistent_id}"
                )
            if entry["artwork"] in {"added", "rolled_back"}:
                app.track_artwork.cache_clear()
                live_art = app.track_artwork(snapshot, persistent_id)
                staged = entry["staged_artwork"]
                mismatch = (
                    live_art is not None
                    if entry["artwork"] == "rolled_back"
                    else live_art is None
                    or hashlib.sha256(live_art[0]).hexdigest() != staged["sha256"]
                )
                if mismatch:
                    result["verification_failures"].append(f"artwork mismatch for {persistent_id}")
    finally:
        connection.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up and apply approved Apple Music metadata and missing artwork."
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output.resolve()
    if os.path.lexists(output):
        print(f"error: output already exists: {output}", file=sys.stderr)
        return 1
    try:
        output.mkdir(parents=True)
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    result = _result("apply" if args.apply else "dry-run")
    try:
        try:
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            _error(result, f"could not read apply plan: {error}")
            return 1
        approved, errors = validate_plan(plan)
        result["preflight_errors"].extend(errors)
        if errors:
            return 1

        expected_library = args.library.resolve()
        try:
            _require_music_running()
            active_library = _require_active_library(expected_library)
        except (OSError, RuntimeError) as error:
            _error(result, str(error))
            return 1

        try:
            copy_library_package(active_library, output / active_library.name)
            current_snapshot = _export_snapshot(output, "current.sqlite3")
            shutil.copy2(args.plan, output / "plan.json")
        except (OSError, RuntimeError, sqlite3.Error) as error:
            _error(result, str(error))
            return 1

        result["entries"] = [
            {
                "persistent_id": change["persistent_id"],
                "metadata": {},
                "artwork": "not_planned" if "artwork" not in change else "staged",
                "staged_artwork": None,
            }
            for change in approved
        ]
        entries = _entry_map(result)
        artwork_directory = output / "artwork"
        if any("artwork" in change for change in approved):
            artwork_directory.mkdir()
        for change in approved:
            artwork = change.get("artwork")
            if artwork is None:
                continue
            try:
                entries[change["persistent_id"]]["staged_artwork"] = _download_artwork(
                    artwork, artwork_directory / change["persistent_id"]
                )
            except (OSError, RuntimeError) as error:
                entries[change["persistent_id"]]["artwork"] = "error"
                _artwork_error(result, change["persistent_id"], "download", error)
        if result["artwork_errors"]:
            return 1

        try:
            _require_active_library(expected_library)
        except (OSError, RuntimeError) as error:
            _error(result, str(error))
            return 1

        try:
            metadata_preflight = run_metadata(approved, "preflight")
        except (OSError, RuntimeError) as error:
            result["subprocess_error"] = str(error)
            return 1
        _merge_metadata(result, metadata_preflight)
        for change in approved:
            if "artwork" not in change:
                continue
            entry = entries[change["persistent_id"]]
            try:
                token = run_artwork("inspect", change["persistent_id"], None)
            except (OSError, RuntimeError) as error:
                entry["artwork"] = "error"
                _artwork_error(result, change["persistent_id"], "inspect", error)
            else:
                entry["artwork"] = "planned" if token == "missing" else "skipped_existing"
                if token == "missing":
                    entry["staged_artwork"]["pre_add_count"] = 0
        if result["preflight_errors"] or result["failures"] or result["artwork_errors"]:
            return 1
        if not args.apply:
            return 0

        try:
            metadata_apply = run_metadata(approved, "apply")
        except (OSError, RuntimeError) as error:
            result["subprocess_error"] = str(error)
            _rollback(
                result,
                approved,
                current_snapshot,
                uncertain_metadata=True,
            )
            return 1
        _merge_metadata(result, metadata_apply)
        if result["preflight_errors"] or result["failures"]:
            _rollback(result, approved, current_snapshot)
        else:
            for change in approved:
                entry = entries[change["persistent_id"]]
                if entry["artwork"] != "planned":
                    continue
                try:
                    token = run_artwork(
                        "add-if-missing",
                        change["persistent_id"],
                        Path(entry["staged_artwork"]["path"]),
                    )
                except (OSError, RuntimeError) as error:
                    entry["artwork"] = "error"
                    _artwork_error(result, change["persistent_id"], "add-if-missing", error)
                    break
                entry["artwork"] = "added" if token == "added" else "skipped_existing"
                if token == "added":
                    app.track_artwork.cache_clear()
                    live_art = app.track_artwork(current_snapshot, change["persistent_id"])
                    if (
                        live_art is None
                        or hashlib.sha256(live_art[0]).hexdigest()
                        != entry["staged_artwork"]["sha256"]
                    ):
                        _artwork_error(
                            result,
                            change["persistent_id"],
                            "add-if-missing",
                            RuntimeError("added artwork digest does not match staged file"),
                        )
                        break
            if result["artwork_errors"]:
                _rollback(result, approved, current_snapshot)

        try:
            after_snapshot = _export_snapshot(output, "after.sqlite3")
            result["post_snapshot"] = str(after_snapshot)
        except (OSError, RuntimeError, sqlite3.Error) as error:
            result["post_export_error"] = str(error)
        else:
            _verify(result, approved, after_snapshot)
            if result["verification_failures"]:
                _rollback(result, approved, after_snapshot)

        return (
            1
            if any(
                (
                    result["preflight_errors"],
                    result["failures"],
                    result["rollback_failures"],
                    result["subprocess_error"],
                    result["artwork_errors"],
                    result["post_export_error"],
                    result["verification_failures"],
                )
            )
            else 0
        )
    finally:
        _write_result(output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
