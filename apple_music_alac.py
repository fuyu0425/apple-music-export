from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import selectors
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import app
from apple_music_apply import _require_active_library, _require_music_running, copy_library_package
from apple_music_metadata_apply import _export_snapshot

XLD_EXECUTABLE = Path("/Applications/XLD.app/Contents/MacOS/XLD")
XLD_INFO_PLIST = Path("/Applications/XLD.app/Contents/Info.plist")
XLD_PREFERENCES = Path("~/Library/Preferences/jp.tmkk.XLD.plist").expanduser()
XLD_REQUIRED_VERSION = "20250302"
LOCATION_BATCH_SIZE = 5
TIMEOUT = 1800
DURATION_TOLERANCE_SECONDS = 0.1
FIELDS = ("name", "artist", "album", "rating", "favorited")

JXA_LOCATION_SCRIPT = r"""
const plan = __PLAN__;
const action = __ACTION__;
const music = Application("Music");
function values(t) { return {name:String(t.name()),artist:String(t.artist()),album:String(t.album()),rating:Number(t.rating()),favorited:t.favorited()}; }
function same(a,b) { return String(a.name)===b.name && String(a.artist)===b.artist && String(a.album)===b.album && Number(a.rating)===b.rating && typeof a.favorited==="boolean" && a.favorited===b.favorited; }
function main() {
  const preflight_errors=[], entries=[], failures=[];
  if (String(music.playerState()) !== "stopped") preflight_errors.push("Music playback must be stopped");
  if (music.converting() !== false) preflight_errors.push("Music conversion is active");
  const tracks=music.libraryPlaylists[0].fileTracks;
  for (const c of plan) {
    const r={persistent_id:c.persistent_id,database_id:c.database_id,location:null,name:null,artist:null,album:null,rating:null,favorited:null,observed_after_apply:null,rewritten_fields:[],status:"error"};
    let stage="resolve";
    entries.push(r);
    try {
      const matches=tracks.whose({persistentID:c.persistent_id})();
      if (matches.length!==1) throw new Error("expected exactly one file track, found "+matches.length);
      let t=matches[0]; const db=Number(t.databaseID()), live=values(t), loc=String(t.location());
      Object.assign(r,live); r.location=loc;
      if (db!==c.database_id) throw new Error("database ID changed");
      if (action==="preflight") { if (!same(live,c.expected)) throw new Error("mutable fields changed"); r.status="verified"; continue; }
      if (action==="apply") {
        if (!same(live,c.expected) || loc.normalize("NFC")!==c.live_source.normalize("NFC")) throw new Error("guard changed before apply");
        stage="assign_location";
        t.location.set(Path(c.destination));
        stage="reresolve_after_assign";
        t=tracks.whose({persistentID:c.persistent_id})()[0];
        stage="read_after_assign";
        const after=values(t), afterLoc=String(t.location()); r.observed_after_apply=after; r.location=afterLoc;
        if (afterLoc.normalize("NFC")===c.destination.normalize("NFC") && same(after,c.expected)) { r.status="applied"; continue; }
        if (afterLoc.normalize("NFC")===c.destination.normalize("NFC")) {
          for (const f of ["name","artist","album","rating","favorited"]) if (after[f]!==c.expected[f]) { if (values(t)[f]!==after[f]) throw new Error("field changed during recovery: "+f); t[f]=c.expected[f]; r.rewritten_fields.push(f); }
          if (!same(values(t),c.expected)) throw new Error("metadata recovery failed");
        }
        t.location.set(Path(c.live_source));
        t=tracks.whose({persistentID:c.persistent_id})()[0];
        if (String(t.location()).normalize("NFC")!==c.live_source.normalize("NFC") || !same(values(t),c.expected)) throw new Error("immediate restoration failed");
        r.status="restored_after_error"; failures.push({persistent_id:c.persistent_id,stage:"readback",error:"apply read-back mismatch"}); break;
      }
      if (action==="rollback") {
        const src=loc.normalize("NFC")===c.live_source.normalize("NFC"), dst=loc.normalize("NFC")===c.destination.normalize("NFC");
        if (!src && !dst) throw new Error("track points to a third location");
        if (src) { if (!same(live,c.expected)) throw new Error("source metadata changed"); r.status="already_restored"; continue; }
        for (const f of ["name","artist","album","rating","favorited"]) if (live[f]!==c.expected[f]) { if (!c.observed_after_apply || live[f]!==c.observed_after_apply[f]) throw new Error("unknown rollback value: "+f); t[f]=c.expected[f]; r.rewritten_fields.push(f); }
        if (!same(values(t),c.expected)) throw new Error("metadata rollback failed");
        t.location.set(Path(c.live_source));
        t=tracks.whose({persistentID:c.persistent_id})()[0];
        if (String(t.location()).normalize("NFC")!==c.live_source.normalize("NFC") || !same(values(t),c.expected)) throw new Error("location rollback failed");
        r.status="restored";
      }
    } catch (e) { failures.push({persistent_id:c.persistent_id,stage:stage,error:String(e)}); break; }
  }
  return {mode:action,preflight_errors:preflight_errors,entries:entries,failures:failures};
}
JSON.stringify(main());
"""


def path_key(path: Path) -> str:
    return unicodedata.normalize("NFC", str(path.resolve()))


def collision_key(path: Path) -> str:
    return path_key(path).casefold()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_candidates(snapshot: Path) -> list[dict[str, Any]]:
    if not snapshot.is_file():
        raise RuntimeError(f"snapshot is missing: {snapshot}")
    connection = app.connect_read_only(snapshot)
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None or row[0] != "3":
            raise RuntimeError("snapshot schema version must be 3")
        rows = connection.execute(
            "SELECT persistent_id,database_id,name,artist,album,location,rating,favorited FROM tracks WHERE location IS NOT NULL"
        ).fetchall()
    finally:
        connection.close()
    result = []
    pids = set()
    dbids = set()
    for row in rows:
        pid, dbid, name, artist, album, location, rating, favorited = row
        if Path(location).suffix.casefold() not in {".aif", ".aiff"}:
            continue
        if not isinstance(pid, str) or not app.PERSISTENT_ID_PATTERN.fullmatch(pid):
            raise RuntimeError(f"invalid persistent ID: {pid}")
        if type(dbid) is not int or dbid <= 0:
            raise RuntimeError(f"invalid database ID for {pid}")
        if pid in pids or dbid in dbids:
            raise RuntimeError(f"duplicate track identity: {pid}")
        if not all(isinstance(v, str) for v in (name, artist, album)):
            raise RuntimeError(f"invalid text metadata for {pid}")
        if type(rating) is not int or not 0 <= rating <= 100:
            raise RuntimeError(f"invalid rating for {pid}")
        if type(favorited) is not int or favorited not in (0, 1):
            raise RuntimeError(f"invalid favorite state for {pid}")
        pids.add(pid)
        dbids.add(dbid)
        result.append(
            {
                "persistent_id": pid,
                "database_id": dbid,
                "source": location,
                "expected": {
                    "name": name,
                    "artist": artist,
                    "album": album,
                    "rating": rating,
                    "favorited": bool(favorited),
                },
            }
        )
    result.sort(key=lambda c: (c["source"], c["persistent_id"]))
    if not result:
        raise RuntimeError("snapshot has no AIFF candidates")
    return result


def load_xld_settings(info_plist: Path, preferences: Path) -> dict[str, Any]:
    if not info_plist.is_file():
        raise RuntimeError(f"XLD info property list is missing: {info_plist}")
    with info_plist.open("rb") as source:
        info = plistlib.load(source)
    if info.get("CFBundleShortVersionString") != XLD_REQUIRED_VERSION:
        raise RuntimeError(f"XLD version must be {XLD_REQUIRED_VERSION}")
    prefs: dict[str, Any] = {}
    if preferences.exists():
        with preferences.open("rb") as source:
            prefs = plistlib.load(source)
    sample = prefs.get("XLDAlacOutput_Samplerate")
    depth = prefs.get("XLDAlacOutput_BitDepth")
    for name, value in (("sample rate", sample), ("bit depth", depth)):
        if value is not None and (type(value) is not int or value != 0):
            raise RuntimeError(f"XLD ALAC {name} preference must be 0")
    return {
        "version": XLD_REQUIRED_VERSION,
        "samplerate_preference": sample,
        "bit_depth_preference": depth,
    }


def convert_to_alac(executable: Path, source: Path, destination: Path) -> None:
    try:
        result = subprocess.run(
            [
                str(executable),
                "--cmdline",
                "-f",
                "alac",
                "--keep-timestamp",
                "-o",
                str(destination),
                str(source),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("XLD conversion timed out") from error
    if result.returncode or any(
        line.startswith("Encoder option:") for line in result.stderr.splitlines()
    ):
        raise RuntimeError(result.stderr.strip() or "XLD conversion failed")


def pcm_digest(executable: Path, path: Path) -> str:
    deadline = time.monotonic() + TIMEOUT
    digest = hashlib.sha256()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [str(executable), "--cmdline", "-f", "raw_little", "--stdout", str(path)],
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            eof = False
            while not eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                events = selector.select(remaining)
                if not events and process.poll() is None:
                    raise TimeoutError
                for key, _ in events:
                    chunk = os.read(key.fd, 1024 * 1024)
                    if chunk:
                        digest.update(chunk)
                    else:
                        eof = True
                        selector.unregister(process.stdout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            code = process.wait(timeout=remaining)
        except (TimeoutError, subprocess.TimeoutExpired) as error:
            process.kill()
            process.wait()
            raise RuntimeError("XLD PCM decode timed out") from error
        finally:
            selector.close()
            process.stdout.close()
        if code:
            errors.seek(0)
            raise RuntimeError(
                errors.read().decode(errors="replace").strip() or "XLD PCM decode failed"
            )
    return digest.hexdigest()


def remove_owned(path: Path, expected_sha256: str | None, owned_keys: frozenset[str]) -> bool:
    try:
        if path_key(path) not in owned_keys:
            return False
        if not os.path.lexists(path):
            return True
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
        if expected_sha256 is not None and file_sha256(path) != expected_sha256:
            return False
        path.unlink()
        return not os.path.lexists(path)
    except OSError:
        return False


def _run_jxa(changes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    script = JXA_LOCATION_SCRIPT.replace(
        "__PLAN__", json.dumps(changes, ensure_ascii=False)
    ).replace("__ACTION__", json.dumps("preflight"))
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as out:
            out.write(script)
            path = Path(out.name)
        result = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Music location action timed out") from error
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Music location action failed")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Music location action returned invalid JSON") from error
    if set(report) != {"mode", "preflight_errors", "entries", "failures"}:
        raise RuntimeError("Music location action returned an invalid report")
    return report


def _stop_music() -> None:
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", 'tell application "Music" to stop'],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "could not stop Music playback")


def _set_location(persistent_id: str, location: str) -> None:
    script = """
on run argv
  set persistentId to item 1 of argv
  set newLocation to item 2 of argv
  tell application "Music"
    set matches to every file track of library playlist 1 whose persistent ID is persistentId
    if (count of matches) is not 1 then error "expected exactly one file track"
    set location of item 1 of matches to POSIX file newLocation
  end tell
end run
"""
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script, persistent_id, location],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Music location assignment failed")


def run_locations(changes: Sequence[dict[str, Any]], action: str) -> dict[str, Any]:
    if action not in {"preflight", "apply", "rollback"}:
        raise ValueError(f"unsupported location action: {action}")
    _stop_music()
    before = _run_jxa(changes)
    if action == "preflight" or before["preflight_errors"] or before["failures"]:
        before["mode"] = action
        return before
    by_id = {entry["persistent_id"]: entry for entry in before["entries"]}
    changed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        for change in changes:
            live = by_id[change["persistent_id"]]
            source = path_key(Path(change["live_source"]))
            destination = path_key(Path(change["destination"]))
            current = path_key(Path(live["location"]))
            if action == "apply":
                if current != source:
                    raise RuntimeError("live location changed before apply")
                target = change["destination"]
            else:
                if current == source:
                    live["status"] = "already_restored"
                    continue
                if current != destination:
                    raise RuntimeError("track points to a third location")
                target = change["live_source"]
            _set_location(change["persistent_id"], target)
            changed.append(change)
    except Exception as error:  # noqa: BLE001 - report and restore a partial batch
        failures.append(
            {
                "persistent_id": change["persistent_id"],
                "stage": action,
                "error": str(error),
            }
        )
        if action == "apply":
            for applied in reversed(changed):
                try:
                    _set_location(applied["persistent_id"], applied["live_source"])
                except Exception as rollback_error:  # noqa: BLE001
                    failures.append(
                        {
                            "persistent_id": applied["persistent_id"],
                            "stage": "restored_after_error",
                            "error": str(rollback_error),
                        }
                    )
    _stop_music()
    after = _run_jxa(changes)
    after["mode"] = action
    after["failures"].extend(failures)
    expected_key = "destination" if action == "apply" else "live_source"
    for entry, change in zip(after["entries"], changes, strict=True):
        if path_key(Path(entry["location"])) != path_key(Path(change[expected_key])):
            after["failures"].append(
                {
                    "persistent_id": change["persistent_id"],
                    "stage": "readback",
                    "error": "location read-back mismatch",
                }
            )
            entry["status"] = "error"
        elif action == "apply":
            entry["observed_after_apply"] = {field: entry[field] for field in FIELDS}
            entry["status"] = "applied"
        else:
            entry["status"] = "restored"
    return after


def _snapshot_rows(path: Path) -> tuple[dict[str, sqlite3.Row], dict[str, set[str]]]:
    connection = app.connect_read_only(path)
    connection.row_factory = sqlite3.Row
    try:
        tracks = {row["persistent_id"]: row for row in connection.execute("SELECT * FROM tracks")}
        memberships = {pid: set() for pid in tracks}
        for row in connection.execute(
            "SELECT tp.track_persistent_id,p.persistent_id,p.smart FROM track_playlists tp JOIN playlists p ON p.persistent_id=tp.playlist_persistent_id"
        ):
            memberships[row[0]].add(("smart:" if row[2] else "regular:") + row[1])
        return tracks, memberships
    finally:
        connection.close()


def verify_snapshot(
    source_snapshot: Path,
    observed_snapshot: Path,
    entries: Sequence[dict[str, Any]],
    expected_locations: Mapping[str, Path],
) -> dict[str, list[dict[str, Any]]]:
    failures = []
    smart = []
    history = []
    source, source_members = _snapshot_rows(source_snapshot)
    observed, observed_members = _snapshot_rows(observed_snapshot)
    snap = observed_snapshot.name

    def add(target: list[dict[str, Any]], pid: str, field: str, expected: Any, actual: Any) -> None:
        target.append(
            {
                "snapshot": snap,
                "persistent_id": pid,
                "field": field,
                "expected": expected,
                "observed": actual,
            }
        )

    for entry in entries:
        pid = entry["persistent_id"]
        before = source.get(pid)
        after = observed.get(pid)
        if before is None or after is None:
            add(failures, pid, "track", bool(before), bool(after))
            continue
        for field in ("database_id", "name", "artist", "album", "rating", "favorited"):
            if before[field] != after[field]:
                add(failures, pid, field, before[field], after[field])
        if abs(before["duration"] - after["duration"]) > DURATION_TOLERANCE_SECONDS:
            add(failures, pid, "duration", before["duration"], after["duration"])
        expected = path_key(expected_locations[pid])
        actual = path_key(Path(after["location"])) if after["location"] else None
        if expected != actual:
            add(failures, pid, "location", expected, actual)
        breg = {x for x in source_members[pid] if x.startswith("regular:")}
        areg = {x for x in observed_members[pid] if x.startswith("regular:")}
        if breg != areg:
            add(failures, pid, "regular_playlists", sorted(breg), sorted(areg))
        bsmart = {x for x in source_members[pid] if x.startswith("smart:")}
        asmart = {x for x in observed_members[pid] if x.startswith("smart:")}
        if bsmart != asmart:
            add(smart, pid, "smart_playlists", sorted(bsmart), sorted(asmart))
        if before["last_played_at"] != after["last_played_at"]:
            add(history, pid, "last_played_at", before["last_played_at"], after["last_played_at"])
        try:
            if file_sha256(Path(entry["source"])) != entry["source_sha256"]:
                add(failures, pid, "source_sha256", entry["source_sha256"], "mismatch")
            if file_sha256(Path(entry["destination"])) != entry["destination_sha256"]:
                add(failures, pid, "destination_sha256", entry["destination_sha256"], "mismatch")
            if entry["pcm_sha256"] is not None and (
                pcm_digest(XLD_EXECUTABLE, Path(entry["source"])) != entry["pcm_sha256"]
                or pcm_digest(XLD_EXECUTABLE, Path(entry["destination"])) != entry["pcm_sha256"]
            ):
                add(failures, pid, "pcm_sha256", entry["pcm_sha256"], "mismatch")
        except (OSError, RuntimeError) as error:
            add(failures, pid, "media", entry["pcm_sha256"], str(error))
    return {
        "verification_failures": failures,
        "smart_membership_drift": smart,
        "play_history_drift": history,
    }


def write_result(output: Path, report: dict[str, Any]) -> None:
    temporary = output / ".result.json.tmp"
    with temporary.open("w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, output / "result.json")


def _changes(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            k: e[k]
            for k in (
                "persistent_id",
                "database_id",
                "live_source",
                "destination",
                "expected",
                "observed_after_apply",
            )
        }
        for e in entries
    ]


def _record_runner_entries(
    runner_entries: Sequence[dict[str, Any]],
    entries: Sequence[dict[str, Any]],
    output: Path,
    report: dict[str, Any],
    errors: list[Any],
) -> None:
    by_id = {entry["persistent_id"]: entry for entry in entries}
    for item in runner_entries:
        entry = by_id[item["persistent_id"]]
        if item.get("observed_after_apply") is not None:
            entry["observed_after_apply"] = item["observed_after_apply"]
        rewritten = item.get("rewritten_fields", [])
        if not rewritten:
            continue
        try:
            destination = Path(entry["destination"])
            if not stat.S_ISREG(destination.lstat().st_mode):
                raise RuntimeError("rewritten destination is not a regular file")
            # The user explicitly approved trusting XLD without decoded PCM proof.
            entry["destination_sha256"] = file_sha256(destination)
            entry["destination_rewritten"] = True
            entry["rewritten_fields"] = sorted(set(entry["rewritten_fields"]) | set(rewritten))
            write_result(output, report)
        except Exception as error:  # noqa: BLE001 - record a blocking runner failure
            errors.append(
                {
                    "persistent_id": entry["persistent_id"],
                    "stage": "rewritten_destination",
                    "error": str(error),
                }
            )


def _blocking(report: dict[str, Any]) -> bool:
    return bool(
        report["fatal_error"]
        or any(
            report[k]
            for k in (
                "preflight_errors",
                "conversion_errors",
                "reattach_failures",
                "rollback_failures",
                "verification_failures",
            )
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview or migrate Apple Music AIFF files to ALAC."
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    output = args.output
    if os.path.lexists(output):
        print(f"output already exists: {output}", file=sys.stderr)
        return 1
    output.mkdir(parents=True)
    report = {
        "mode": "apply" if args.apply else "dry-run",
        "source_snapshot": str(args.snapshot),
        "source_snapshot_sha256": None,
        "library": str(args.library),
        "xld_version": None,
        "xld_samplerate_preference": None,
        "xld_bit_depth_preference": None,
        "pcm_verification": "skipped_by_user",
        "duration_tolerance_seconds": DURATION_TOLERANCE_SECONDS,
        "entries": [],
        "active_batch": [],
        "completed_batches": [],
        "preflight_errors": [],
        "conversion_errors": [],
        "reattach_failures": [],
        "rollback_failures": [],
        "verification_failures": [],
        "smart_membership_drift": [],
        "play_history_drift": [],
        "canary_snapshot": None,
        "canary_rollback_snapshot": None,
        "rollback_snapshot": None,
        "post_snapshot": None,
        "fatal_error": None,
    }
    owned = frozenset()
    try:
        if not args.library.is_dir():
            raise RuntimeError(f"library is missing: {args.library}")
        report["source_snapshot_sha256"] = file_sha256(args.snapshot)
        settings = load_xld_settings(XLD_INFO_PLIST, XLD_PREFERENCES)
        report["xld_version"] = settings["version"]
        report["xld_samplerate_preference"] = settings["samplerate_preference"]
        report["xld_bit_depth_preference"] = settings["bit_depth_preference"]
        candidates = load_candidates(args.snapshot)
        source_keys = set()
        destination_keys = set()
        entries = []
        for c in candidates:
            source = Path(c["source"])
            destination = source.with_suffix(".m4a")
            temporary = source.with_name(f".{c['persistent_id']}.apple-music-alac.tmp.m4a")
            if (
                not source.is_absolute()
                or source.is_symlink()
                or not source.is_file()
                or not os.access(source, os.R_OK)
            ):
                raise RuntimeError(f"invalid AIFF source: {source}")
            skey = collision_key(source)
            dkey = collision_key(destination)
            if skey in source_keys or dkey in destination_keys:
                raise RuntimeError(f"duplicate media path: {source}")
            children = {collision_key(p) for p in source.parent.iterdir()}
            if dkey in children or collision_key(temporary) in children:
                raise RuntimeError(f"destination or temporary path exists for {source}")
            source_keys.add(skey)
            destination_keys.add(dkey)
            entries.append(
                {
                    **c,
                    "source": str(source),
                    "source_key": path_key(source),
                    "live_source": None,
                    "destination": str(destination),
                    "destination_key": path_key(destination),
                    "temporary": str(temporary),
                    "observed_after_apply": None,
                    "source_sha256": None,
                    "destination_sha256_initial": None,
                    "destination_sha256": None,
                    "pcm_sha256": None,
                    "destination_rewritten": False,
                    "rewritten_fields": [],
                    "conversion": "planned",
                    "reattach": "planned",
                    "canary": "not_canary",
                }
            )
        report["entries"] = entries
        owned = frozenset(
            path_key(Path(e[k])) for e in entries for k in ("destination", "temporary")
        )
        by_device: dict[int, tuple[Path, int]] = {}
        for e in entries:
            p = Path(e["source"])
            dev = p.parent.stat().st_dev
            root, total = by_device.get(dev, (p.parent, 0))
            by_device[dev] = (root, total + p.stat().st_size)
        for root, total in by_device.values():
            if shutil.disk_usage(root).free <= 2 * total:
                raise RuntimeError(f"insufficient free space on {root}")
        _require_music_running()
        _require_active_library(args.library)
        pre = run_locations(_changes(entries), "preflight")
        report["preflight_errors"].extend(pre["preflight_errors"])
        report["preflight_errors"].extend(pre["failures"])
        by_id = {e["persistent_id"]: e for e in entries}
        for item in pre["entries"]:
            if item["status"] == "verified":
                by_id[item["persistent_id"]]["live_source"] = item["location"]
        for e in entries:
            if e["live_source"] is None or path_key(Path(e["live_source"])) != e["source_key"]:
                report["preflight_errors"].append(
                    {
                        "persistent_id": e["persistent_id"],
                        "error": "live source differs from snapshot",
                    }
                )
        write_result(output, report)
        if _blocking(report) or not args.apply:
            return 1 if _blocking(report) else 0
        copied = output / "source.sqlite3"
        shutil.copy2(args.snapshot, copied)
        if file_sha256(copied) != report["source_snapshot_sha256"]:
            raise RuntimeError("copied snapshot hash mismatch")
        copy_library_package(args.library, output / args.library.name)
        _require_active_library(args.library)
        for e in entries:
            e["source_sha256"] = file_sha256(Path(e["source"]))
        write_result(output, report)
        for e in entries:
            try:
                src = Path(e["source"])
                tmp = Path(e["temporary"])
                convert_to_alac(XLD_EXECUTABLE, src, tmp)
                if not stat.S_ISREG(tmp.lstat().st_mode) or tmp.stat().st_size <= 0:
                    raise RuntimeError("XLD output is not a regular nonempty file")
                if file_sha256(src) != e["source_sha256"]:
                    raise RuntimeError("source changed during conversion")
                # The user explicitly approved trusting XLD without decoded PCM proof.
                e["pcm_sha256"] = None
                e["destination_sha256_initial"] = e["destination_sha256"] = file_sha256(tmp)
                e["conversion"] = "verified"
                write_result(output, report)
            except Exception as error:  # noqa: BLE001 - persist controlled failure state
                e["conversion"] = "error"
                report["conversion_errors"].append(
                    {"persistent_id": e["persistent_id"], "error": str(error)}
                )
                for item in entries:
                    remove_owned(Path(item["temporary"]), item["destination_sha256"], owned)
                write_result(output, report)
                return 1
        for e in entries:
            try:
                tmp = Path(e["temporary"])
                dst = Path(e["destination"])
                if os.path.lexists(dst) or file_sha256(tmp) != e["destination_sha256"]:
                    raise RuntimeError("destination collision or temporary changed")
                os.link(tmp, dst)
                if file_sha256(dst) != e["destination_sha256"]:
                    raise RuntimeError("installed hash mismatch")
                tmp.unlink()
                write_result(output, report)
            except Exception as error:  # noqa: BLE001 - persist controlled failure state
                report["conversion_errors"].append(
                    {"persistent_id": e["persistent_id"], "error": str(error)}
                )
                write_result(output, report)
                return 1
        _require_music_running()
        _require_active_library(args.library)
        second = run_locations(_changes(entries), "preflight")
        if second["preflight_errors"] or second["failures"]:
            report["reattach_failures"].extend(second["preflight_errors"] + second["failures"])
            write_result(output, report)
            return 1
        canary = entries[0]
        report["active_batch"] = [canary["persistent_id"]]
        write_result(output, report)
        _require_music_running()
        _require_active_library(args.library)
        applied = run_locations(_changes([canary]), "apply")
        if applied["preflight_errors"] or applied["failures"]:
            raise RuntimeError(
                f"canary apply failed: {applied['preflight_errors'] + applied['failures']}"
            )
        _record_runner_entries(
            applied["entries"], entries, output, report, report["reattach_failures"]
        )
        if report["reattach_failures"]:
            raise RuntimeError("canary rewritten destination verification failed")
        canary["observed_after_apply"] = applied["entries"][0]["observed_after_apply"]
        canary["canary"] = "applied"
        write_result(output, report)
        canary_path = _export_snapshot(output, "canary.sqlite3")
        report["canary_snapshot"] = str(canary_path)
        verification = verify_snapshot(
            args.snapshot,
            canary_path,
            entries,
            {
                e["persistent_id"]: Path(e["destination"] if e is canary else e["source"])
                for e in entries
            },
        )
        report["verification_failures"].extend(verification["verification_failures"])
        report["smart_membership_drift"].extend(verification["smart_membership_drift"])
        report["play_history_drift"].extend(verification["play_history_drift"])
        if report["verification_failures"]:
            raise RuntimeError("canary snapshot verification failed")
        _require_music_running()
        _require_active_library(args.library)
        rolled = run_locations(_changes([canary]), "rollback")
        if rolled["preflight_errors"] or rolled["failures"]:
            raise RuntimeError(
                f"canary rollback failed: {rolled['preflight_errors'] + rolled['failures']}"
            )
        _record_runner_entries(
            rolled["entries"], entries, output, report, report["rollback_failures"]
        )
        if report["rollback_failures"]:
            raise RuntimeError("canary rollback rewrite verification failed")
        rollback_path = _export_snapshot(output, "canary-rollback.sqlite3")
        report["canary_rollback_snapshot"] = str(rollback_path)
        check = verify_snapshot(
            args.snapshot,
            rollback_path,
            entries,
            {e["persistent_id"]: Path(e["source"]) for e in entries},
        )
        if check["verification_failures"]:
            raise RuntimeError("canary rollback verification failed")
        canary["canary"] = "restored"
        report["active_batch"] = []
        write_result(output, report)
        for start in range(0, len(entries), LOCATION_BATCH_SIZE):
            batch = entries[start : start + LOCATION_BATCH_SIZE]
            report["active_batch"] = [e["persistent_id"] for e in batch]
            write_result(output, report)
            _require_music_running()
            _require_active_library(args.library)
            result = run_locations(_changes(batch), "apply")
            if result["preflight_errors"] or result["failures"]:
                raise RuntimeError(
                    f"batch apply failed: {result['preflight_errors'] + result['failures']}"
                )
            _record_runner_entries(
                result["entries"], entries, output, report, report["reattach_failures"]
            )
            if report["reattach_failures"]:
                raise RuntimeError("batch rewritten destination verification failed")
            reports = {r["persistent_id"]: r for r in result["entries"]}
            for e in batch:
                e["observed_after_apply"] = reports[e["persistent_id"]]["observed_after_apply"]
                e["reattach"] = "applied"
            report["completed_batches"].append(report["active_batch"])
            report["active_batch"] = []
            write_result(output, report)
        after = _export_snapshot(output, "after.sqlite3")
        report["post_snapshot"] = str(after)
        final = verify_snapshot(
            args.snapshot,
            after,
            entries,
            {e["persistent_id"]: Path(e["destination"]) for e in entries},
        )
        report["verification_failures"].extend(final["verification_failures"])
        report["smart_membership_drift"].extend(final["smart_membership_drift"])
        report["play_history_drift"].extend(final["play_history_drift"])
        write_result(output, report)
        if report["verification_failures"]:
            raise RuntimeError("final verification failed")
        return 0
    except Exception as error:  # noqa: BLE001 - persist uncategorized failure state
        if report["active_batch"] or any(e.get("reattach") == "applied" for e in report["entries"]):
            try:
                _require_music_running()
                _require_active_library(args.library)
                rollback = run_locations(_changes(report["entries"]), "rollback")
                report["rollback_failures"].extend(
                    rollback["preflight_errors"] + rollback["failures"]
                )
                _record_runner_entries(
                    rollback["entries"],
                    report["entries"],
                    output,
                    report,
                    report["rollback_failures"],
                )
                snapshot = _export_snapshot(output, "rollback.sqlite3")
                report["rollback_snapshot"] = str(snapshot)
                check = verify_snapshot(
                    args.snapshot,
                    snapshot,
                    report["entries"],
                    {e["persistent_id"]: Path(e["source"]) for e in report["entries"]},
                )
                report["rollback_failures"].extend(check["verification_failures"])
                if not report["rollback_failures"]:
                    for e in report["entries"]:
                        if (
                            e["reattach"] == "applied"
                            or e["persistent_id"] in report["active_batch"]
                        ):
                            e["reattach"] = "rolled_back"
                    report["active_batch"] = []
                    for entry in report["entries"]:
                        destination = Path(entry["destination"])
                        if not remove_owned(destination, entry["destination_sha256"], owned):
                            report["rollback_failures"].append(
                                {
                                    "persistent_id": entry["persistent_id"],
                                    "stage": "cleanup",
                                    "error": f"refused generated file: {destination}",
                                }
                            )
            except Exception as rollback_error:  # noqa: BLE001 - preserve rollback failure
                report["rollback_failures"].append({"error": str(rollback_error)})
        if (
            not report["rollback_failures"]
            and not report["reattach_failures"]
            and not report["verification_failures"]
            and not report["conversion_errors"]
            and not report["preflight_errors"]
        ):
            report["fatal_error"] = str(error)
        try:
            write_result(output, report)
        except Exception as write_error:  # noqa: BLE001 - stderr is the last available sink
            print(f"could not write result: {write_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
