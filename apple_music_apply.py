from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import app
from active_music_library import active_music_library
from apple_music_export import collect_library, write_snapshot
from apple_music_recover import merge_snapshots

JXA_RECOVERY_SCRIPT = r"""
const plan = __PLAN__;
const applyChanges = __APPLY__;
const replaceDisliked = __REPLACE_DISLIKED__;
const music = Application("Music");

function values(collection, propertyName, fallback) {
    let result;
    try {
        result = Array.from(collection[propertyName]());
    } catch (bulkError) {
        result = Array.from(collection()).map(item => {
            try {
                return item[propertyName]();
            } catch (itemError) {
                return fallback;
            }
        });
    }
    if (result.length !== collection.length) {
        throw new Error(propertyName + " returned " + result.length +
                        " values for " + collection.length + " items");
    }
    return result;
}

function counts() {
    return {favorites: 0, ratings: 0, memberships: 0};
}

function failure(kind, ids, error) {
    const number = Number(error.number);
    return Object.assign({kind: kind}, ids, {
        error_number: Number.isFinite(number) ? number : 0,
        detail: String(error.message || error)
    });
}

function report(mode, preflightErrors, applied, skipped, failures) {
    return {
        mode: mode,
        preflight_errors: preflightErrors,
        applied: applied,
        skipped: skipped,
        failures: failures
    };
}

function main() {
    const preflightErrors = [];
    const library = music.libraryPlaylists[0];
    const tracks = library.tracks;
    const trackItems = Array.from(tracks());
    const trackIds = values(tracks, "persistentID", "").map(String);
    const trackById = new Map();
    for (let i = 0; i < trackItems.length; i++) {
        trackById.set(trackIds[i], trackItems[i]);
    }

    const playlists = music.userPlaylists;
    const playlistItems = Array.from(playlists());
    const playlistIds = values(playlists, "persistentID", "").map(String);
    const playlistSmart = values(playlists, "smart", false).map(Boolean);
    const playlistById = new Map();
    const livePlaylists = [];
    for (let i = 0; i < playlistItems.length; i++) {
        playlistById.set(playlistIds[i], playlistItems[i]);
        livePlaylists.push({persistent_id: playlistIds[i], smart: playlistSmart[i]});
    }

    const liveTrackIds = Array.from(new Set(trackIds)).sort();
    livePlaylists.sort((a, b) => a.persistent_id.localeCompare(b.persistent_id));
    if (JSON.stringify(liveTrackIds) !== JSON.stringify(plan.expected_track_ids)) {
        preflightErrors.push("active Music track identities do not match the current snapshot");
    }
    if (JSON.stringify(livePlaylists) !== JSON.stringify(plan.expected_playlists)) {
        preflightErrors.push("active Music playlist identities do not match the current snapshot");
    }

    for (const item of plan.favorites) {
        const track = trackById.get(item.persistent_id);
        if (!track) {
            preflightErrors.push("missing favorite track " + item.persistent_id);
            continue;
        }
        try {
            if (Boolean(track.disliked()) && !replaceDisliked) {
                preflightErrors.push("favorite track is disliked: " + item.persistent_id);
            }
        } catch (error) {
            preflightErrors.push("could not read disliked for " + item.persistent_id + ": " + error);
        }
    }

    for (const item of plan.ratings) {
        if (!trackById.has(item.persistent_id)) {
            preflightErrors.push("missing rating track " + item.persistent_id);
        }
    }

    const membershipPlaylists = new Map();
    for (const item of plan.memberships) {
        if (!trackById.has(item.track_persistent_id)) {
            preflightErrors.push("missing membership track " + item.track_persistent_id);
        }
        const playlist = playlistById.get(item.playlist_persistent_id);
        if (!playlist) {
            preflightErrors.push("missing membership playlist " + item.playlist_persistent_id);
            continue;
        }
        try {
            if (Boolean(playlist.smart())) {
                preflightErrors.push("membership playlist is smart: " + item.playlist_persistent_id);
            }
            if (String(playlist.specialKind()) !== "none") {
                preflightErrors.push("membership playlist is special: " + item.playlist_persistent_id);
            }
        } catch (error) {
            preflightErrors.push("could not inspect playlist " + item.playlist_persistent_id + ": " + error);
        }
        membershipPlaylists.set(item.playlist_persistent_id, playlist);
    }

    const applied = counts();
    const skipped = counts();
    const failures = [];
    const mode = applyChanges ? "apply" : "dry-run";
    if (preflightErrors.length > 0 || !applyChanges) {
        return report(mode, preflightErrors, applied, skipped, failures);
    }

    for (const item of plan.favorites) {
        const track = trackById.get(item.persistent_id);
        try {
            if (Boolean(track.favorited())) {
                skipped.favorites++;
            } else {
                if (Boolean(track.disliked())) {
                    if (!replaceDisliked) {
                        throw new Error("favorite track became disliked");
                    }
                    track.disliked = false;
                }
                track.favorited = true;
                applied.favorites++;
            }
        } catch (error) {
            failures.push(failure("favorite", {persistent_id: item.persistent_id}, error));
        }
    }

    for (const item of plan.ratings) {
        const track = trackById.get(item.persistent_id);
        try {
            if (Number(track.rating()) !== 0) {
                skipped.ratings++;
            } else {
                track.rating = item.rating;
                applied.ratings++;
            }
        } catch (error) {
            failures.push(failure("rating", {persistent_id: item.persistent_id}, error));
        }
    }

    const membershipIds = new Map();
    for (const [playlistId, playlist] of membershipPlaylists) {
        try {
            membershipIds.set(
                playlistId,
                new Set(values(playlist.tracks, "persistentID", "").map(String))
            );
        } catch (error) {
            for (const item of plan.memberships) {
                if (item.playlist_persistent_id === playlistId) {
                    failures.push(failure("membership", {
                        track_persistent_id: item.track_persistent_id,
                        playlist_persistent_id: playlistId
                    }, error));
                }
            }
        }
    }

    for (const item of plan.memberships) {
        const ids = membershipIds.get(item.playlist_persistent_id);
        if (!ids) {
            continue;
        }
        if (ids.has(item.track_persistent_id)) {
            skipped.memberships++;
            continue;
        }
        try {
            music.duplicate(trackById.get(item.track_persistent_id), {
                to: membershipPlaylists.get(item.playlist_persistent_id)
            });
            ids.add(item.track_persistent_id);
            applied.memberships++;
        } catch (error) {
            failures.push(failure("membership", {
                track_persistent_id: item.track_persistent_id,
                playlist_persistent_id: item.playlist_persistent_id
            }, error));
        }
    }

    return report(mode, preflightErrors, applied, skipped, failures);
}

JSON.stringify(main());
"""


def _extended_attributes(path: Path, symlink: bool) -> list[tuple[str, bytes]]:
    options = ["-s"] if symlink else []
    result = subprocess.run(
        ["/usr/bin/xattr", *options, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "xattr returned no error message."
        raise RuntimeError(f"could not list extended attributes for {path}: {detail}")

    attributes = []
    for name in sorted(result.stdout.splitlines()):
        value_result = subprocess.run(
            ["/usr/bin/xattr", *options, "-p", "-x", name, str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if value_result.returncode != 0:
            detail = value_result.stderr.strip() or "xattr returned no error message."
            raise RuntimeError(f"could not read extended attribute {name} for {path}: {detail}")
        try:
            value = bytes.fromhex(value_result.stdout)
        except ValueError as error:
            raise RuntimeError(
                f"extended attribute {name} for {path} was not valid hexadecimal"
            ) from error
        attributes.append((name, value))
    return attributes


def package_manifest(package: Path) -> dict[str, str]:
    paths = [package, *package.rglob("*")]
    manifest: dict[str, str] = {}
    for path in sorted(paths, key=lambda item: item.relative_to(package).as_posix()):
        relative = "." if path == package else path.relative_to(package).as_posix()
        metadata = path.lstat()
        digest = hashlib.sha256()

        if stat.S_ISREG(metadata.st_mode):
            entry_type = b"file"
        elif stat.S_ISDIR(metadata.st_mode):
            entry_type = b"directory"
        elif stat.S_ISLNK(metadata.st_mode):
            entry_type = b"symlink"
        else:
            entry_type = b"other"
        digest.update(entry_type + b"\0" + str(stat.S_IMODE(metadata.st_mode)).encode() + b"\0")

        if stat.S_ISREG(metadata.st_mode):
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif stat.S_ISLNK(metadata.st_mode):
            digest.update(os.readlink(path).encode())

        for name, value in _extended_attributes(path, stat.S_ISLNK(metadata.st_mode)):
            encoded_name = name.encode()
            digest.update(len(encoded_name).to_bytes(8, "big"))
            digest.update(encoded_name)
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)

        manifest[relative] = digest.hexdigest()
    return manifest


def copy_library_package(source: Path, destination: Path) -> None:
    database = source / "Library.musicdb"
    if source.suffix != ".musiclibrary" or not source.is_dir():
        raise RuntimeError(f"not a Music library package: {source}")
    if not database.is_file() or database.stat().st_size == 0:
        raise RuntimeError(f"Music library database is empty or missing: {database}")
    if os.path.lexists(destination):
        raise RuntimeError(f"output already exists: {destination}")
    if not destination.parent.is_dir():
        raise RuntimeError(f"destination parent does not exist: {destination.parent}")

    last_error = "library package did not stay stable"
    for _ in range(2):
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        if os.path.lexists(temporary):
            raise RuntimeError(f"temporary backup path already exists: {temporary}")
        try:
            before = package_manifest(source)
            result = subprocess.run(
                ["/usr/bin/ditto", str(source), str(temporary)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or "ditto returned no error message."
                raise RuntimeError(f"ditto failed: {detail}")
            copied = package_manifest(temporary)
            after = package_manifest(source)
            if before != copied or before != after:
                raise RuntimeError("library package changed during backup")
            if os.path.lexists(destination):
                raise RuntimeError(f"output already exists: {destination}")
            temporary.rename(destination)
            return
        except (OSError, RuntimeError) as error:
            last_error = str(error)
            if temporary.is_dir() and not temporary.is_symlink():
                shutil.rmtree(temporary)
            else:
                temporary.unlink(missing_ok=True)

    raise RuntimeError(f"Could not make a stable library package backup: {last_error}")


def compute_recovery_plan(current: Path, target: Path) -> dict[str, Any]:
    current_connection = app.connect_read_only(current)
    try:
        current_connection.execute(
            "ATTACH DATABASE ? AS target", (f"{target.resolve().as_uri()}?mode=ro",)
        )
        expected_track_ids = [
            row[0]
            for row in current_connection.execute(
                "SELECT persistent_id FROM tracks ORDER BY persistent_id"
            )
        ]
        expected_playlists = [
            {"persistent_id": row[0], "smart": bool(row[1])}
            for row in current_connection.execute(
                "SELECT persistent_id, smart FROM playlists ORDER BY persistent_id"
            )
        ]
        favorites = [
            {
                "persistent_id": row[0],
                "name": row[1],
                "artist": row[2],
                "album": row[3],
            }
            for row in current_connection.execute(
                """
                SELECT current.persistent_id, current.name, current.artist, current.album
                FROM tracks AS current
                JOIN target.tracks AS wanted USING (persistent_id)
                WHERE current.favorited = 0 AND wanted.favorited = 1
                ORDER BY current.persistent_id
                """
            )
        ]
        ratings = [
            {
                "persistent_id": row[0],
                "name": row[1],
                "artist": row[2],
                "album": row[3],
                "rating": row[4],
            }
            for row in current_connection.execute(
                """
                SELECT current.persistent_id, current.name, current.artist, current.album,
                       wanted.rating
                FROM tracks AS current
                JOIN target.tracks AS wanted USING (persistent_id)
                WHERE current.rating = 0 AND wanted.rating > 0
                ORDER BY current.persistent_id
                """
            )
        ]
        memberships = [
            {
                "track_persistent_id": row[0],
                "track_name": row[1],
                "playlist_persistent_id": row[2],
                "playlist_name": row[3],
            }
            for row in current_connection.execute(
                """
                SELECT wanted.track_persistent_id, tracks.name,
                       wanted.playlist_persistent_id, playlists.name
                FROM target.track_playlists AS wanted
                JOIN tracks ON tracks.persistent_id = wanted.track_persistent_id
                JOIN playlists ON playlists.persistent_id = wanted.playlist_persistent_id
                LEFT JOIN track_playlists AS current
                  ON current.track_persistent_id = wanted.track_persistent_id
                 AND current.playlist_persistent_id = wanted.playlist_persistent_id
                WHERE current.track_persistent_id IS NULL AND playlists.smart = 0
                ORDER BY wanted.playlist_persistent_id, wanted.track_persistent_id
                """
            )
        ]
    finally:
        current_connection.close()

    return {
        "favorites": favorites,
        "ratings": ratings,
        "memberships": memberships,
        "expected_track_ids": expected_track_ids,
        "expected_playlists": expected_playlists,
    }


def run_music_recovery(
    plan: dict[str, Any], apply: bool, replace_disliked: bool = False
) -> dict[str, Any]:
    script = (
        JXA_RECOVERY_SCRIPT.replace("__PLAN__", json.dumps(plan, ensure_ascii=False))
        .replace("__APPLY__", "true" if apply else "false")
        .replace("__REPLACE_DISLIKED__", "true" if replace_disliked else "false")
    )
    descriptor, temporary_name = tempfile.mkstemp(suffix=".js", prefix="apple-music-recovery-")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(script)
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-l", "JavaScript", str(temporary)],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Apple Music recovery timed out after 300 seconds.") from error
        if result.returncode != 0:
            detail = result.stderr.strip() or "Apple Music returned no error message."
            raise RuntimeError(f"Apple Music recovery failed: {detail}")
        try:
            report: dict[str, Any] = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError("Apple Music recovery returned invalid JSON.") from error
        expected_keys = {"mode", "preflight_errors", "applied", "skipped", "failures"}
        if not isinstance(report, dict) or set(report) != expected_keys:
            raise RuntimeError("Apple Music recovery returned an invalid report.")
        return report
    finally:
        temporary.unlink(missing_ok=True)


def _metadata(snapshot: Path, key: str) -> str:
    connection = app.connect_read_only(snapshot)
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError(f"{snapshot}: missing metadata {key}")
    return str(row[0])


def _smart_membership_count(snapshot: Path) -> int:
    connection = app.connect_read_only(snapshot)
    try:
        row = connection.execute(
            """
            SELECT count(*)
            FROM track_playlists
            JOIN playlists
              ON playlists.persistent_id = track_playlists.playlist_persistent_id
            WHERE playlists.smart = 1
            """
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError(f"{snapshot}: could not count smart memberships")
    return int(row[0])


def _require_music_running() -> None:
    result = subprocess.run(
        ["/usr/bin/pgrep", "-x", "Music"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Music is not running. Open the intended library before recovery.")


def _require_active_library(expected: Path) -> Path:
    actual = active_music_library()
    if actual != expected:
        raise RuntimeError(f"active Music library is {actual}, expected {expected}")
    return actual


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _print_plan(plan: dict[str, Any]) -> None:
    for item in plan["favorites"]:
        print(
            f"favorite: {item['name']} | {item['artist']} | {item['album']} "
            f"[{item['persistent_id']}]"
        )
    for item in plan["ratings"]:
        print(
            f"rating {item['rating']}: {item['name']} | {item['artist']} | "
            f"{item['album']} [{item['persistent_id']}]"
        )
    for item in plan["memberships"]:
        print(
            f"membership: {item['track_name']} -> {item['playlist_name']} "
            f"[{item['track_persistent_id']} -> {item['playlist_persistent_id']}]"
        )
    counts = plan["merged_counts"]
    print(f"favorites: {counts['favorites']}")
    print(f"ratings: {counts['ratings']}")
    print(f"memberships: {counts['memberships']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up and recover live Apple Music state through its scripting interface."
    )
    parser.add_argument("--restore-from", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--replace-disliked",
        action="store_true",
        help="clear disliked before restoring a planned favorite",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    expected_library = args.library.resolve()
    restore_from = args.restore_from.resolve()

    try:
        if os.path.lexists(output):
            raise RuntimeError(f"output already exists: {output}")
        _require_music_running()
        active_library = _require_active_library(expected_library)

        output.mkdir(parents=True)
        package_backup = output / active_library.name
        copy_library_package(active_library, package_backup)

        generated_current, _ = write_snapshot(collect_library(), output)
        current_snapshot = generated_current.rename(output / "current.sqlite3")
        target_snapshot = output / "target.sqlite3"
        merged = merge_snapshots(restore_from, current_snapshot, target_snapshot)
        derived = compute_recovery_plan(current_snapshot, target_snapshot)
        derived_counts = (
            len(derived["favorites"]),
            len(derived["ratings"]),
            len(derived["memberships"]),
        )
        if derived_counts != merged:
            raise RuntimeError(
                f"derived recovery counts {derived_counts} do not match merged counts {merged}"
            )

        merged_counts = {
            "favorites": merged[0],
            "ratings": merged[1],
            "memberships": merged[2],
        }
        plan = {
            "active_library": str(active_library),
            "package_backup": str(package_backup),
            "restore_from": str(restore_from),
            "current_snapshot": str(current_snapshot),
            "target_snapshot": str(target_snapshot),
            "restore_exported_at": _metadata(restore_from, "exported_at"),
            "current_exported_at": _metadata(current_snapshot, "exported_at"),
            "merged_counts": merged_counts,
            "favorites": derived["favorites"],
            "ratings": derived["ratings"],
            "memberships": derived["memberships"],
            "expected_track_ids": derived["expected_track_ids"],
            "expected_playlists": derived["expected_playlists"],
        }
        _write_json(output / "plan.json", plan)
        _print_plan(plan)

        _require_active_library(expected_library)
        if not args.apply:
            report = run_music_recovery(plan, apply=False, replace_disliked=args.replace_disliked)
            _write_json(output / "result.json", report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if report["preflight_errors"] or report["failures"] else 0

        report: dict[str, Any] | None = None
        subprocess_error: str | None = None
        post_export_error: str | None = None
        after_snapshot: Path | None = None
        try:
            report = run_music_recovery(plan, apply=True, replace_disliked=args.replace_disliked)
        except (OSError, RuntimeError) as error:
            subprocess_error = str(error)
        finally:
            try:
                generated_after, _ = write_snapshot(collect_library(), output)
                after_snapshot = generated_after.rename(output / "after.sqlite3")
            except (KeyError, OSError, RuntimeError, sqlite3.Error) as error:
                post_export_error = str(error)

        residual: dict[str, Any] | None = None
        after_smart_memberships: int | None = None
        if after_snapshot is not None:
            residual = compute_recovery_plan(after_snapshot, target_snapshot)
            after_smart_memberships = _smart_membership_count(after_snapshot)

        result = {
            "mode": "apply",
            "jxa_report": report,
            "residual": residual,
            "post_snapshot": str(after_snapshot) if after_snapshot is not None else None,
            "subprocess_error": subprocess_error,
            "post_export_error": post_export_error,
            "smart_memberships": {
                "before": _smart_membership_count(current_snapshot),
                "after": after_smart_memberships,
            },
        }
        _write_json(output / "result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))

        has_residual = residual is None or any(
            residual[key] for key in ("favorites", "ratings", "memberships")
        )
        has_report_error = report is None or bool(report["preflight_errors"] or report["failures"])
        return (
            1
            if (
                has_report_error
                or has_residual
                or subprocess_error is not None
                or post_export_error is not None
            )
            else 0
        )
    except (KeyError, OSError, RuntimeError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
