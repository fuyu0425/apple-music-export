import sqlite3
import tempfile
import unittest
from pathlib import Path

from apple_music_export import write_snapshot


class SnapshotTest(unittest.TestCase):
    def test_writes_distinct_files_and_playlist_memberships(self) -> None:
        data = {
            "tracks": [
                {
                    "persistent_id": "TRACK1",
                    "database_id": 1,
                    "name": "Same Song",
                    "artist": "Artist",
                    "album": "Album",
                    "location": "/music/one.m4a",
                    "duration": 245.5,
                    "rating": 80,
                    "favorited": True,
                },
                {
                    "persistent_id": "TRACK2",
                    "database_id": 2,
                    "name": "Same Song",
                    "artist": "Artist",
                    "album": "Album",
                    "location": "/music/two.m4a",
                    "duration": 180.0,
                    "rating": 20,
                    "favorited": False,
                },
            ],
            "playlists": [
                {
                    "persistent_id": "PLAYLIST1",
                    "name": "Favorites",
                    "smart": False,
                    "track_ids": ["TRACK1", "TRACK2"],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            path, memberships = write_snapshot(data, Path(directory))
            with sqlite3.connect(path) as connection:
                tracks = connection.execute(
                    "SELECT persistent_id, location, duration, rating, favorited "
                    "FROM tracks ORDER BY persistent_id"
                ).fetchall()
                schema_version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                links = connection.execute(
                    "SELECT track_persistent_id, playlist_persistent_id "
                    "FROM track_playlists ORDER BY track_persistent_id"
                ).fetchall()

        self.assertEqual(
            tracks,
            [
                ("TRACK1", "/music/one.m4a", 245.5, 80, 1),
                ("TRACK2", "/music/two.m4a", 180.0, 20, 0),
            ],
        )
        self.assertEqual(links, [("TRACK1", "PLAYLIST1"), ("TRACK2", "PLAYLIST1")])
        self.assertEqual(schema_version, "2")
        self.assertEqual(memberships, 2)


if __name__ == "__main__":
    unittest.main()
