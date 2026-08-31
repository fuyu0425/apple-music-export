from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import app


def merge_snapshots(healthy: Path, current: Path, output: Path) -> tuple[int, int, int]:
    if os.path.lexists(output):
        raise RuntimeError(f"output already exists: {output}")

    healthy_schema_version: str | None = None
    current_schema_version: str | None = None
    current_counts: tuple[int, int, int] | None = None

    for snapshot, is_healthy in ((healthy, True), (current, False)):
        connection: sqlite3.Connection | None = None
        try:
            connection = app.connect_read_only(snapshot)
            schema_version = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if schema_version is None:
                raise RuntimeError(f"{snapshot}: missing metadata schema_version")
            if is_healthy:
                healthy_schema_version = schema_version[0]
            else:
                current_schema_version = schema_version[0]
                current_counts = (
                    connection.execute("SELECT count(*) FROM tracks").fetchone()[0],
                    connection.execute("SELECT count(*) FROM playlists").fetchone()[0],
                    connection.execute("SELECT count(*) FROM track_playlists").fetchone()[0],
                )
        except sqlite3.Error as error:
            raise RuntimeError(f"{snapshot}: {error}") from error
        finally:
            if connection is not None:
                connection.close()

    if healthy_schema_version != current_schema_version:
        raise RuntimeError(
            "schema version mismatch: "
            f"{healthy} has {healthy_schema_version}, {current} has {current_schema_version}"
        )
    if current_counts is None:
        raise RuntimeError(f"{current}: missing snapshot counts")

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary_connection: sqlite3.Connection | None = None

    try:
        source_connection = app.connect_read_only(current)
        destination_connection: sqlite3.Connection | None = None
        try:
            destination_connection = sqlite3.connect(temporary)
            source_connection.backup(destination_connection)
        finally:
            if destination_connection is not None:
                destination_connection.close()
            source_connection.close()

        temporary_connection = sqlite3.connect(f"{temporary.resolve().as_uri()}?mode=rw", uri=True)
        temporary_connection.execute(
            "ATTACH DATABASE ? AS healthy",
            (f"{healthy.resolve().as_uri()}?mode=ro",),
        )
        temporary_connection.execute("PRAGMA foreign_keys = ON")
        temporary_connection.execute("BEGIN")

        favorite_cursor = temporary_connection.execute(
            """
            UPDATE main.tracks
            SET favorited = 1
            WHERE main.tracks.favorited = 0
              AND EXISTS (
                  SELECT 1
                  FROM healthy.tracks
                  WHERE healthy.tracks.persistent_id = main.tracks.persistent_id
                    AND healthy.tracks.favorited = 1
              )
            """
        )
        rating_cursor = temporary_connection.execute(
            """
            UPDATE main.tracks
            SET rating = (
                SELECT healthy.tracks.rating
                FROM healthy.tracks
                WHERE healthy.tracks.persistent_id = main.tracks.persistent_id
            )
            WHERE main.tracks.rating = 0
              AND EXISTS (
                  SELECT 1
                  FROM healthy.tracks
                  WHERE healthy.tracks.persistent_id = main.tracks.persistent_id
                    AND healthy.tracks.rating > 0
              )
            """
        )
        membership_cursor = temporary_connection.execute(
            """
            INSERT OR IGNORE INTO main.track_playlists (
                track_persistent_id, playlist_persistent_id
            )
            SELECT healthy.track_playlists.track_persistent_id,
                   healthy.track_playlists.playlist_persistent_id
            FROM healthy.track_playlists
            JOIN main.tracks
              ON main.tracks.persistent_id = healthy.track_playlists.track_persistent_id
            JOIN main.playlists
              ON main.playlists.persistent_id = healthy.track_playlists.playlist_persistent_id
             AND main.playlists.smart = 0
            JOIN healthy.playlists
              ON healthy.playlists.persistent_id = healthy.track_playlists.playlist_persistent_id
             AND healthy.playlists.smart = 0
            """
        )
        restored_favorites = favorite_cursor.rowcount
        restored_ratings = rating_cursor.rowcount
        restored_memberships = membership_cursor.rowcount

        output_counts = (
            temporary_connection.execute("SELECT count(*) FROM main.tracks").fetchone()[0],
            temporary_connection.execute("SELECT count(*) FROM main.playlists").fetchone()[0],
            temporary_connection.execute("SELECT count(*) FROM main.track_playlists").fetchone()[0],
        )
        temporary_connection.executemany(
            """
            INSERT INTO main.metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (
                ("track_count", str(output_counts[0])),
                ("playlist_count", str(output_counts[1])),
                ("membership_count", str(output_counts[2])),
                ("recovered_at", datetime.now().astimezone().isoformat()),
                ("recovery_healthy_snapshot", healthy.name),
                ("recovery_current_snapshot", current.name),
            ),
        )

        if output_counts[:2] != current_counts[:2]:
            raise RuntimeError("recovered track or playlist count changed")
        if output_counts[2] != current_counts[2] + restored_memberships:
            raise RuntimeError("recovered membership count does not match inserted rows")
        integrity_result = temporary_connection.execute("PRAGMA main.integrity_check").fetchone()
        if integrity_result is None or integrity_result[0] != "ok":
            raise RuntimeError("recovered snapshot failed integrity_check")
        if temporary_connection.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
            raise RuntimeError("recovered snapshot failed foreign_key_check")

        temporary_connection.commit()
        temporary_connection.close()
        temporary_connection = None

        # The hard link prevents a concurrent process from replacing an existing output.
        os.link(temporary, output)
        temporary.unlink()
        temporary = None
        return restored_favorites, restored_ratings, restored_memberships
    finally:
        if temporary_connection is not None:
            temporary_connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover Apple Music status between SQLite snapshots."
    )
    parser.add_argument("--healthy", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        restored_favorites, restored_ratings, restored_memberships = merge_snapshots(
            args.healthy, args.current, args.output
        )
    except (OSError, RuntimeError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(args.output)
    print(f"favorites restored: {restored_favorites}")
    print(f"ratings restored: {restored_ratings}")
    print(f"regular memberships restored: {restored_memberships}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
