import contextlib
import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from apple_music_export import write_snapshot
from apple_music_recover import merge_snapshots


class RecoverSnapshotTest(unittest.TestCase):
    def test_merges_recoverable_state_into_current_snapshot(self) -> None:
        def track(
            persistent_id: str, database_id: int, rating: int, favorited: bool
        ) -> dict[str, object]:
            return {
                "persistent_id": persistent_id,
                "database_id": database_id,
                "name": f"Track {persistent_id}",
                "artist": "Artist",
                "album": "Album",
                "location": f"/music/{persistent_id}.m4a",
                "duration": 180.0,
                "rating": rating,
                "favorited": favorited,
            }

        healthy_data = {
            "tracks": [
                track("A", 1, 80, True),
                track("B", 2, 20, False),
                track("D", 4, 0, False),
                track("E", 5, 0, False),
            ],
            "playlists": [
                {
                    "persistent_id": "REGULAR",
                    "name": "Regular",
                    "smart": False,
                    "track_ids": ["A", "B", "E"],
                },
                {
                    "persistent_id": "SMART",
                    "name": "Smart",
                    "smart": True,
                    "track_ids": ["A"],
                },
            ],
        }
        current_data = {
            "tracks": [
                track("A", 10, 0, False),
                track("B", 20, 100, True),
                track("C", 30, 40, False),
                track("D", 40, 0, False),
            ],
            "playlists": [
                {
                    "persistent_id": "REGULAR",
                    "name": "Regular",
                    "smart": False,
                    "track_ids": ["B", "C"],
                },
                {
                    "persistent_id": "SMART",
                    "name": "Smart",
                    "smart": True,
                    "track_ids": ["B"],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated_healthy, _ = write_snapshot(healthy_data, root)
            healthy = generated_healthy.rename(root / "healthy.sqlite3")
            generated_current, _ = write_snapshot(current_data, root)
            current = generated_current.rename(root / "current.sqlite3")
            output = root / "recovered.sqlite3"
            input_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (healthy, current)
            }

            with contextlib.closing(sqlite3.connect(current)) as connection:
                current_only_track = connection.execute(
                    "SELECT * FROM tracks WHERE persistent_id = 'C'"
                ).fetchone()
                current_metadata = dict(connection.execute("SELECT key, value FROM metadata"))

            restored = merge_snapshots(healthy, current, output)

            with contextlib.closing(sqlite3.connect(output)) as connection:
                tracks = {
                    row[0]: row[1:]
                    for row in connection.execute(
                        "SELECT persistent_id, rating, favorited FROM tracks"
                    )
                }
                output_current_only_track = connection.execute(
                    "SELECT * FROM tracks WHERE persistent_id = 'C'"
                ).fetchone()
                memberships = set(
                    connection.execute(
                        "SELECT track_persistent_id, playlist_persistent_id FROM track_playlists"
                    )
                )
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))

            self.assertEqual(restored, (1, 1, 1))
            self.assertEqual(
                tracks,
                {
                    "A": (80, 1),
                    "B": (100, 1),
                    "C": (40, 0),
                    "D": (0, 0),
                },
            )
            self.assertNotIn("E", tracks)
            self.assertEqual(output_current_only_track, current_only_track)
            self.assertEqual(
                memberships,
                {
                    ("A", "REGULAR"),
                    ("B", "REGULAR"),
                    ("C", "REGULAR"),
                    ("B", "SMART"),
                },
            )
            self.assertEqual(metadata["track_count"], "4")
            self.assertEqual(metadata["playlist_count"], "2")
            self.assertEqual(metadata["membership_count"], "4")
            self.assertEqual(metadata["schema_version"], current_metadata["schema_version"])
            self.assertEqual(metadata["exported_at"], current_metadata["exported_at"])
            self.assertEqual(metadata["recovery_healthy_snapshot"], healthy.name)
            self.assertEqual(metadata["recovery_current_snapshot"], current.name)
            datetime.fromisoformat(metadata["recovered_at"])
            self.assertEqual(
                {
                    path: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (healthy, current)
                },
                input_hashes,
            )
            self.assertEqual(
                {path.name for path in root.iterdir()},
                {healthy.name, current.name, output.name},
            )

    def test_refuses_to_replace_existing_output(self) -> None:
        data = {"tracks": [], "playlists": []}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, _ = write_snapshot(data, root)
            output = root / "existing.sqlite3"
            output.write_bytes(b"keep this output")
            original_output = output.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "output already exists"):
                merge_snapshots(snapshot, snapshot, output)

            self.assertEqual(output.read_bytes(), original_output)


if __name__ == "__main__":
    unittest.main()
