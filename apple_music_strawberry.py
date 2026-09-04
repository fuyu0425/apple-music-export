from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import plistlib
import sqlite3
import subprocess
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from app import connect_read_only

APP = Path("/Applications/strawberry.app")
EXECUTABLE = APP / "Contents/MacOS/strawberry"
SETTING_ERRORS = {
    "Collection/save_ratings": "Collection/save_ratings must be false to prevent source-file tag writes",
    "Collection/overwrite_rating": "Collection/overwrite_rating must be false to preserve imported ratings on rescan",
}


def _rating_key(value: float) -> str:
    return "0" if value <= 0 else str(round(value * 100))


def _matches(observed: float, expected: int) -> bool:
    return observed <= 0 if expected == 0 else round(observed * 100) == expected


def _join_key(path: str) -> str:
    return unicodedata.normalize("NFC", str(Path(path).resolve(strict=True)))


def _decode_target_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "file" or parsed.hostname not in (None, "", "localhost"):
        raise ValueError(f"Unsupported Strawberry song URL: {value}")
    return unquote(parsed.path)


def _settings_errors(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as stream:
            settings = plistlib.load(stream)
    except Exception as exc:  # noqa: BLE001
        return [f"Could not read Strawberry settings: {exc}"]
    errors = []
    for key, message in SETTING_ERRORS.items():
        value = settings.get(key)
        if value is not None and value is not False and value != 0:
            errors.append(message)
    return errors


def _process_error(executable: Path) -> str | None:
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-x", "strawberry"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        pids = [int(value) for value in result.stdout.split()]
        if result.returncode != 0 or len(pids) != 1:
            return f"Expected one primary strawberry process, found {len(pids)}"
        pid = pids[0]
        libproc = ctypes.CDLL("libproc.dylib", use_errno=True)
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        length = proc_pidpath(pid, buffer, 4096)
        if length <= 0:
            return f"Could not resolve executable for strawberry PID {pid}"
        actual = Path(os.fsdecode(buffer.value))
        if actual != executable:
            return f"Primary strawberry executable is {actual}, expected {executable}"
        started = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
        if started.returncode != 0:
            return f"Could not read start time for strawberry PID {pid}"
        process_start = datetime.strptime(
            started.stdout.strip(), "%a %b %d %H:%M:%S %Y"
        ).astimezone()
        if int(process_start.timestamp()) < int(executable.stat().st_mtime):
            return "Primary strawberry process predates the installed executable"
    except Exception as exc:  # noqa: BLE001
        return f"Could not validate primary strawberry process: {exc}"
    return None


def _backup(database: Path, destination: Path) -> None:
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)


def _empty_report(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "mode": "apply" if args.apply else "preview",
        "source": {"path": str(args.snapshot), "sha256": None, "schema_version": None},
        "target": {
            "database": str(args.strawberry_db),
            "settings": str(args.strawberry_settings),
            "collection_root": str(args.collection_root),
            "application": str(args.strawberry_app),
            "schema_version": None,
        },
        "counts": {
            key: 0
            for key in (
                "snapshot_rows",
                "located_rows",
                "unique_snapshot_files",
                "collection_files",
                "matched_files",
                "missing_locations",
                "duplicate_location_groups",
                "folder_only_files",
                "changed_ratings",
                "unchanged_ratings",
                "expected_ratings",
                "observed_ratings",
            )
        },
        "missing_locations": [],
        "duplicate_locations": [],
        "folder_only_files": [],
        "batches": [],
        "preflight_errors": [],
        "command_launch_failures": [],
        "residual_ratings": [],
    }


def _write_report(output: Path, report: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _rewrite_report(output: Path, report: dict[str, Any]) -> None:
    (output / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _read_observed(database: Path, paths: list[str]) -> dict[str, float]:
    wanted = set(paths)
    observed: dict[str, float] = {}
    with closing(connect_read_only(database)) as connection:
        for row in connection.execute("SELECT url, rating FROM songs WHERE unavailable = 0"):
            try:
                path = _decode_target_url(row["url"])
            except ValueError:
                continue
            if path in wanted:
                observed[path] = float(row["rating"] if row["rating"] is not None else -1)
    return observed


def import_library(
    args: argparse.Namespace,
    *,
    sender: Callable[[int, list[str]], subprocess.CompletedProcess[str]] | None = None,
    process_check: Callable[[Path], str | None] = _process_error,
    poll_seconds: float = 60,
) -> int:
    if args.strawberry_app != APP:
        raise ValueError(f"--strawberry-app must equal {APP}")
    executable = args.strawberry_app / "Contents/MacOS/strawberry"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"Strawberry executable is missing or not executable: {executable}")
    if args.output.exists():
        raise FileExistsError(f"Output path already exists: {args.output}")

    report = _empty_report(args)
    errors: list[str] = report["preflight_errors"]
    snapshot_rows: list[sqlite3.Row] = []
    targets: dict[str, dict[str, Any]] = {}
    root_key: str | None = None

    try:
        digest = hashlib.sha256(args.snapshot.read_bytes()).hexdigest()
        report["source"]["sha256"] = digest
        if digest != args.snapshot_sha256:
            errors.append(f"Snapshot SHA-256 is {digest}, expected {args.snapshot_sha256}")
        with closing(connect_read_only(args.snapshot)) as source:
            source_version = int(
                source.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
            )
            report["source"]["schema_version"] = source_version
            if source_version != 3:
                errors.append(f"Snapshot schema version is {source_version}, expected 3")
            snapshot_rows = list(
                source.execute(
                    "SELECT persistent_id, name, artist, rating, location FROM tracks ORDER BY persistent_id"
                )
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Could not read snapshot: {exc}")

    try:
        root_key = _join_key(str(args.collection_root))
        with closing(connect_read_only(args.strawberry_db)) as target:
            target_version = int(target.execute("SELECT version FROM schema_version").fetchone()[0])
            report["target"]["schema_version"] = target_version
            if target_version != 23:
                errors.append(f"Strawberry schema version is {target_version}, expected 23")
            columns = {row["name"] for row in target.execute("PRAGMA table_info(songs)")}
            missing_columns = {"url", "rating", "unavailable"} - columns
            if missing_columns:
                errors.append(
                    f"Strawberry songs table lacks columns: {', '.join(sorted(missing_columns))}"
                )
            directory_matches = 0
            for row in target.execute("SELECT path FROM directories"):
                try:
                    if (
                        row["path"] == str(args.collection_root)
                        or _join_key(row["path"]) == root_key
                    ):
                        directory_matches += 1
                except (OSError, ValueError):
                    continue
            if directory_matches != 1:
                errors.append(
                    f"Expected one equivalent collection directory, found {directory_matches}"
                )
            if not missing_columns:
                for row in target.execute("SELECT rowid, url, rating, unavailable FROM songs"):
                    if row["unavailable"]:
                        continue
                    try:
                        path = _decode_target_url(row["url"])
                        key = _join_key(path)
                    except (OSError, ValueError) as exc:
                        errors.append(str(exc))
                        continue
                    if key != root_key and not key.startswith(root_key + os.sep):
                        continue
                    if key in targets:
                        errors.append(f"Duplicate available Strawberry target: {path}")
                    targets[key] = {
                        "rowid": row["rowid"],
                        "path": path,
                        "rating": float(row["rating"] if row["rating"] is not None else -1),
                    }
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Could not read Strawberry database: {exc}")

    errors.extend(_settings_errors(args.strawberry_settings))
    counts = report["counts"]
    counts["snapshot_rows"] = len(snapshot_rows)
    counts["located_rows"] = sum(row["location"] is not None for row in snapshot_rows)

    sources: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in snapshot_rows:
        if row["location"] is None:
            report["missing_locations"].append(
                {key: row[key] for key in ("persistent_id", "name", "artist", "rating")}
            )
            continue
        try:
            sources[_join_key(row["location"])].append(row)
        except (OSError, ValueError) as exc:
            errors.append(f"Source file is absent: {row['location']}: {exc}")

    expected: dict[str, int] = {}
    for key, rows in sources.items():
        ratings = {int(row["rating"]) for row in rows}
        if len(ratings) != 1:
            errors.append(f"Conflicting source ratings for {rows[0]['location']}")
            continue
        if key not in targets:
            report["missing_locations"].extend(
                {field: row[field] for field in ("persistent_id", "name", "artist", "rating")}
                for row in rows
            )
            errors.append(
                f"Snapshot location has no available Strawberry target: {rows[0]['location']}"
            )
            continue
        expected[targets[key]["path"]] = ratings.pop()
        if len(rows) > 1:
            report["duplicate_locations"].append(
                {
                    "path": targets[key]["path"],
                    "persistent_ids": [row["persistent_id"] for row in rows],
                    "rating": expected[targets[key]["path"]],
                }
            )

    matched_keys = {key for key in sources if key in targets}
    folder_only = sorted(value["path"] for key, value in targets.items() if key not in matched_keys)
    report["folder_only_files"] = folder_only
    counts.update(
        {
            "unique_snapshot_files": len(sources),
            "collection_files": len(targets),
            "matched_files": len(expected),
            "missing_locations": len(report["missing_locations"]),
            "duplicate_location_groups": len(report["duplicate_locations"]),
            "folder_only_files": len(folder_only),
        }
    )
    expected_counts = Counter(str(value) for value in expected.values())
    observed_counts = Counter(_rating_key(targets[key]["rating"]) for key in matched_keys)
    counts["expected_ratings"] = dict(
        sorted(expected_counts.items(), key=lambda item: int(item[0]))
    )
    counts["observed_ratings"] = dict(
        sorted(observed_counts.items(), key=lambda item: int(item[0]))
    )

    groups: dict[int, list[str]] = defaultdict(list)
    for path, rating in expected.items():
        key = _join_key(path)
        if _matches(targets[key]["rating"], rating):
            counts["unchanged_ratings"] += 1
        else:
            counts["changed_ratings"] += 1
            groups[rating].append(path)
    for rating in sorted(groups):
        files = sorted(groups[rating])
        for index, start in enumerate(range(0, len(files), 200)):
            report["batches"].append(
                {
                    "rating": rating,
                    "batch_index": index,
                    "file_count": len(files[start : start + 200]),
                    "files": files[start : start + 200],
                    "status": None,
                    "stdout": None,
                    "stderr": None,
                    "failure_reason": None,
                }
            )

    if errors:
        _write_report(args.output, report)
        return 1
    if not args.apply:
        _write_report(args.output, report)
        return 0

    errors.extend(_settings_errors(args.strawberry_settings))
    process_error = process_check(executable)
    if process_error:
        errors.append(process_error)
    if errors:
        _write_report(args.output, report)
        return 1
    try:
        args.output.mkdir(parents=True, exist_ok=False)
        _backup(args.strawberry_db, args.output / "strawberry-before.sqlite3")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Could not create before backup: {exc}")
        if not args.output.exists():
            args.output.mkdir(parents=True)
        _rewrite_report(args.output, report)
        return 1

    if sender is None:

        def sender(rating: int, files: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(executable), "--set-rating", str(rating), *files],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

    for batch in report["batches"]:
        check_errors = _settings_errors(args.strawberry_settings)
        process_error = process_check(executable)
        if process_error:
            check_errors.append(process_error)
        if check_errors:
            errors.extend(check_errors)
            _rewrite_report(args.output, report)
            return 1
        try:
            result = sender(batch["rating"], batch["files"])
            batch["status"] = result.returncode
            batch["stdout"] = result.stdout
            batch["stderr"] = result.stderr
            delivery_error = "Could not send message to primary instance." in (
                result.stdout + result.stderr
            )
            if result.returncode != 0 or delivery_error:
                batch["failure_reason"] = (
                    "Could not send message to primary instance."
                    if delivery_error
                    else f"Sender exited with status {result.returncode}"
                )
        except subprocess.TimeoutExpired as exc:
            batch["stdout"] = exc.stdout
            batch["stderr"] = exc.stderr
            batch["failure_reason"] = "Sender timed out after 15 seconds"
        except Exception as exc:  # noqa: BLE001
            batch["failure_reason"] = str(exc)
        if batch["failure_reason"]:
            report["command_launch_failures"].append(batch["failure_reason"])
            _rewrite_report(args.output, report)
            return 1

        deadline = time.monotonic() + poll_seconds
        while True:
            observed = _read_observed(args.strawberry_db, batch["files"])
            residual = [
                {
                    "path": path,
                    "expected_rating": batch["rating"],
                    "observed_rating": observed.get(path),
                }
                for path in batch["files"]
                if path not in observed or not _matches(observed[path], batch["rating"])
            ]
            if not residual:
                break
            if time.monotonic() >= deadline:
                report["residual_ratings"] = residual
                _rewrite_report(args.output, report)
                return 1
            time.sleep(0.1)

    try:
        observed = _read_observed(args.strawberry_db, list(expected))
        report["counts"]["observed_ratings"] = dict(
            sorted(
                Counter(_rating_key(value) for value in observed.values()).items(),
                key=lambda item: int(item[0]),
            )
        )
        _backup(args.strawberry_db, args.output / "strawberry-after.sqlite3")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Could not create final audit: {exc}")
        _rewrite_report(args.output, report)
        return 1
    _rewrite_report(args.output, report)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("import-library")
    command.add_argument("--snapshot", type=Path, required=True)
    command.add_argument("--snapshot-sha256", required=True)
    command.add_argument("--strawberry-db", type=Path, required=True)
    command.add_argument("--strawberry-settings", type=Path, required=True)
    command.add_argument("--collection-root", type=Path, required=True)
    command.add_argument("--strawberry-app", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return import_library(args)
    except (FileExistsError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
