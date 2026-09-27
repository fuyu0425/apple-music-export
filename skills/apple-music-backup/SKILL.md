---
name: apple-music-backup
description: Back up the active Apple Music library with this repository's scripts, including logical metadata snapshots and verified library packages.
---

# Apple Music backup

Run commands from the `apple-music-export` repository root. Read the export section in `README.md` before execution.

## Identify the source

1. Run `uv run python active_music_library.py` to identify the package that Music currently uses.
2. If detection fails, stop and ask the user to open the intended library in Music.
3. Record the detected absolute path as the source for the package backup.

The active library previously used `~/Music/音樂/Music Library.musiclibrary`. Treat this as history, not a default.
`~/Music/Music` and `~/Music/iTunes` can contain older libraries. Their existence does not identify the active library.

## Create the backup

1. Select a new timestamped destination outside the source package.
2. Check available space for the selected backup scope before copying.
3. Export metadata directly into the destination:

   ```bash
   uv run python apple_music_export.py --output-dir "$backup"
   ```

4. Copy the detected package with the repository's verified helper:

   ```bash
   uv run python -c 'from pathlib import Path; import sys; from apple_music_apply import copy_library_package; copy_library_package(Path(sys.argv[1]), Path(sys.argv[2]))' "$active_library" "$backup/Music Library.musiclibrary"
   ```

5. Confirm that the active library path still matches the recorded source.

Set `$backup` and `$active_library` to the selected absolute paths before these commands.
The exporter creates the destination directory. Use its printed SQLite path for verification.
For an existing backup set, add a new snapshot and a distinct package destination without replacing earlier files.

The helper checks file content, permissions, and extended attributes before and after copying.
If the source changes during copying, report the failure rather than claiming a verified package backup.
Keep the source library unchanged. Backup authorization does not authorize recovery or metadata edits.

## Verify and report

1. Open the resulting SQLite snapshot in read-only mode.
2. Run `PRAGMA integrity_check` and require `ok`.
3. Run `PRAGMA foreign_key_check` and require no rows.
4. Read the schema version from `metadata` and compare it with the current exporter.
5. Count `tracks`, `playlists`, and `track_playlists` and compare them with the exporter output.
6. Report the absolute snapshot path, package path, counts, and verification results.

The snapshot preserves the fields defined by `SCHEMA` in `apple_music_export.py`, including ratings, favorites, and playlist memberships.
It does not contain audio files or artwork bytes. The package copy is separate from the logical snapshot.

## Media scope

For a full media backup, inspect every nonempty `tracks.location` in the fresh snapshot.
Copy referenced local media with metadata preservation after checking destination capacity.
Verify copied media with hashes and report missing files, unavailable volumes, and cloud-only tracks separately.
Inspect other local music folders when the user requests all local music, not only tracks in the active library.

A package and snapshot alone are not a complete audio backup.
A backup on the same disk does not protect against disk failure. State this limitation in the result.
