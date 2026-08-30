from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

JXA_SCRIPT = r"""
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

const library = music.libraryPlaylists[0];
const tracks = library.tracks;
const ids = values(tracks, "persistentID", "");
const databaseIds = values(tracks, "databaseID", 0);
const names = values(tracks, "name", "");
const artists = values(tracks, "artist", "");
const albums = values(tracks, "album", "");
const durations = values(tracks, "duration", 0);
const ratings = values(tracks, "rating", 0);
const favorites = values(tracks, "favorited", false);

const fileTracks = library.fileTracks;
const fileIds = values(fileTracks, "persistentID", "");
const fileLocations = values(fileTracks, "location", null);
const locations = new Map();
for (let i = 0; i < fileIds.length; i++) {
    locations.set(fileIds[i], fileLocations[i] === null ? null : String(fileLocations[i]));
}

const trackRows = ids.map((id, i) => ({
    persistent_id: id,
    database_id: databaseIds[i],
    name: names[i],
    artist: artists[i],
    album: albums[i],
    duration: durations[i],
    location: locations.get(id) || null,
    rating: ratings[i],
    favorited: favorites[i]
}));

const playlistRows = music.userPlaylists().map(playlist => ({
    persistent_id: playlist.persistentID(),
    name: playlist.name(),
    smart: playlist.smart(),
    track_ids: values(playlist.tracks, "persistentID", "")
}));

JSON.stringify({tracks: trackRows, playlists: playlistRows});
"""

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
CREATE TABLE tracks (
    persistent_id TEXT PRIMARY KEY,
    database_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    location TEXT,
    duration REAL NOT NULL CHECK (duration >= 0),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 0 AND 100),
    favorited INTEGER NOT NULL CHECK (favorited IN (0, 1))
) STRICT;
CREATE TABLE playlists (
    persistent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    smart INTEGER NOT NULL CHECK (smart IN (0, 1))
) STRICT;
CREATE TABLE track_playlists (
    track_persistent_id TEXT NOT NULL REFERENCES tracks(persistent_id),
    playlist_persistent_id TEXT NOT NULL REFERENCES playlists(persistent_id),
    PRIMARY KEY (track_persistent_id, playlist_persistent_id)
) STRICT;
"""


def collect_library() -> dict[str, Any]:
    osascript = shutil.which("osascript")
    if osascript is None:
        raise RuntimeError("osascript is unavailable. Run this exporter on macOS.")

    try:
        result = subprocess.run(
            [osascript, "-l", "JavaScript", "-e", JXA_SCRIPT],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Apple Music export timed out after 300 seconds.") from error

    if result.returncode != 0:
        detail = result.stderr.strip() or "Apple Music returned no error message."
        raise RuntimeError(f"Apple Music export failed: {detail}")

    try:
        data: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Apple Music returned invalid JSON.") from error
    return data


def write_snapshot(data: dict[str, Any], output_dir: Path) -> tuple[Path, int]:
    tracks = data["tracks"]
    playlists = data["playlists"]
    exported_at = datetime.now().astimezone()
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"apple-music-{exported_at:%Y%m%dT%H%M%S.%f%z}.sqlite3"
    final_path = output_dir / filename
    temporary_path = final_path.with_suffix(".tmp")

    membership_count = 0
    connection = sqlite3.connect(temporary_path)
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            """
            INSERT INTO tracks (
                persistent_id, database_id, name, artist, album, location, duration, rating,
                favorited
            ) VALUES (
                :persistent_id, :database_id, :name, :artist, :album, :location, :duration,
                :rating, :favorited
            )
            """,
            tracks,
        )
        connection.executemany(
            "INSERT INTO playlists (persistent_id, name, smart) VALUES (?, ?, ?)",
            (
                (playlist["persistent_id"], playlist["name"], playlist["smart"])
                for playlist in playlists
            ),
        )
        for playlist in playlists:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO track_playlists (
                    track_persistent_id, playlist_persistent_id
                ) VALUES (?, ?)
                """,
                ((track_id, playlist["persistent_id"]) for track_id in playlist["track_ids"]),
            )
            membership_count += connection.total_changes - before
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            (
                ("schema_version", "2"),
                ("exported_at", exported_at.isoformat()),
                ("track_count", str(len(tracks))),
                ("playlist_count", str(len(playlists))),
                ("membership_count", str(membership_count)),
            ),
        )
        connection.commit()
    except BaseException:
        connection.close()
        temporary_path.unlink(missing_ok=True)
        raise
    else:
        connection.close()

    os.replace(temporary_path, final_path)
    return final_path, membership_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Apple Music status to SQLite.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("snapshots"),
        help="snapshot directory (default: snapshots)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        data = collect_library()
        path, memberships = write_snapshot(data, args.output_dir)
    except (KeyError, OSError, RuntimeError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(path)
    print(f"tracks: {len(data['tracks'])}")
    print(f"playlists: {len(data['playlists'])}")
    print(f"memberships: {memberships}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
