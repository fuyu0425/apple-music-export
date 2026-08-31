import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from active_music_library import active_music_library
from apple_music_apply import (
    compute_recovery_plan,
    copy_library_package,
    package_manifest,
    run_music_recovery,
)
from apple_music_export import write_snapshot
from apple_music_recover import merge_snapshots


def track(persistent_id: str, database_id: int, rating: int, favorited: bool) -> dict[str, object]:
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


def snapshot(root: Path, name: str, data: dict[str, object]) -> Path:
    generated, _ = write_snapshot(data, root)
    return generated.rename(root / name)


class RecoveryPlanTest(unittest.TestCase):
    def test_derives_sorted_exact_deltas_and_residuals(self) -> None:
        healthy_data = {
            "tracks": [
                track("A", 1, 80, True),
                track("B", 2, 20, False),
                track("E", 5, 100, True),
                track("F", 6, 20, False),
                track("G", 7, 80, True),
            ],
            "playlists": [
                {
                    "persistent_id": "REGULAR",
                    "name": "Regular",
                    "smart": False,
                    "track_ids": ["G", "F", "F", "B", "A"],
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
                track("G", 70, 40, False),
                track("F", 60, 0, False),
                track("D", 40, 40, False),
                track("C", 30, 0, False),
                track("B", 20, 100, True),
                track("A", 10, 0, False),
            ],
            "playlists": [
                {
                    "persistent_id": "SMART",
                    "name": "Smart",
                    "smart": True,
                    "track_ids": ["B"],
                },
                {
                    "persistent_id": "REGULAR",
                    "name": "Regular",
                    "smart": False,
                    "track_ids": ["B"],
                },
            ],
        }
        after_data = {
            "tracks": [
                track("G", 70, 40, False),
                track("F", 60, 0, False),
                track("D", 40, 40, False),
                track("C", 30, 0, False),
                track("B", 20, 100, True),
                track("A", 10, 80, True),
            ],
            "playlists": [
                {
                    "persistent_id": "SMART",
                    "name": "Smart",
                    "smart": True,
                    "track_ids": ["A", "B"],
                },
                {
                    "persistent_id": "REGULAR",
                    "name": "Regular",
                    "smart": False,
                    "track_ids": ["A", "B", "G"],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            healthy = snapshot(root, "healthy.sqlite3", healthy_data)
            current = snapshot(root, "current.sqlite3", current_data)
            target = root / "target.sqlite3"
            merged = merge_snapshots(healthy, current, target)

            plan = compute_recovery_plan(current, target)

            self.assertEqual(merged, (2, 2, 3))
            self.assertEqual(
                (len(plan["favorites"]), len(plan["ratings"]), len(plan["memberships"])),
                merged,
            )
            self.assertEqual([item["persistent_id"] for item in plan["favorites"]], ["A", "G"])
            self.assertEqual([item["persistent_id"] for item in plan["ratings"]], ["A", "F"])
            self.assertEqual(
                [item["track_persistent_id"] for item in plan["memberships"]],
                ["A", "F", "G"],
            )
            self.assertEqual(len(plan["memberships"]), 3)
            self.assertNotIn("E", plan["expected_track_ids"])
            self.assertEqual(
                plan["expected_playlists"],
                [
                    {"persistent_id": "REGULAR", "smart": False},
                    {"persistent_id": "SMART", "smart": True},
                ],
            )

            after = snapshot(root, "after.sqlite3", after_data)
            residual = compute_recovery_plan(after, target)
            self.assertEqual([item["persistent_id"] for item in residual["favorites"]], ["G"])
            self.assertEqual([item["persistent_id"] for item in residual["ratings"]], ["F"])
            self.assertEqual(
                [item["track_persistent_id"] for item in residual["memberships"]],
                ["F"],
            )


class PackageBackupTest(unittest.TestCase):
    def test_accepts_stable_ditto_copy_with_equal_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Music Library.musiclibrary"
            source.mkdir()
            (source / "Library.musicdb").write_bytes(b"database")
            (source / "Extra.musicdb").write_bytes(b"extra")
            destination = root / "Backup.musiclibrary"

            copy_library_package(source, destination)

            self.assertEqual(package_manifest(source), package_manifest(destination))

    def test_refuses_both_attempts_when_source_changes_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Music Library.musiclibrary"
            source.mkdir()
            database = source / "Library.musicdb"
            database.write_bytes(b"database")
            destination = root / "Backup.musiclibrary"
            original_run: Any = subprocess.run
            ditto_calls = 0

            def changing_ditto(
                args: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                nonlocal ditto_calls
                if args[0] != "/usr/bin/ditto":
                    return original_run(args, **kwargs)
                ditto_calls += 1
                shutil.copytree(Path(args[1]), Path(args[2]), symlinks=True)
                with database.open("ab") as file:
                    file.write(str(ditto_calls).encode())
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                mock.patch("apple_music_apply.subprocess.run", side_effect=changing_ditto),
                self.assertRaisesRegex(RuntimeError, "changed during backup"),
            ):
                copy_library_package(source, destination)

            self.assertEqual(ditto_calls, 2)
            self.assertFalse(destination.exists())
            self.assertEqual([path for path in root.iterdir() if path.name.endswith(".tmp")], [])


class ActiveLibraryTest(unittest.TestCase):
    def test_rejects_lsof_failures_ambiguous_results_and_empty_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "First.musiclibrary" / "Library.musicdb"
            second = root / "Second.musiclibrary" / "Library.musicdb"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            cases = [
                subprocess.CompletedProcess([], 1, "", "failed"),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, f"n{first}\nn{second}\n", ""),
            ]

            for result in cases:
                with (
                    self.subTest(returncode=result.returncode, stdout=result.stdout),
                    mock.patch("active_music_library.subprocess.run", return_value=result),
                    self.assertRaises(RuntimeError),
                ):
                    active_music_library()

            first.write_bytes(b"")
            empty_result = subprocess.CompletedProcess([], 0, f"n{first}\n", "")
            with (
                mock.patch("active_music_library.subprocess.run", return_value=empty_result),
                self.assertRaisesRegex(RuntimeError, "empty or missing"),
            ):
                active_music_library()


class JxaRunnerTest(unittest.TestCase):
    def test_parses_reports_fails_closed_and_removes_scripts(self) -> None:
        plan: dict[str, object] = {
            "favorites": [],
            "ratings": [],
            "memberships": [],
            "expected_track_ids": [],
            "expected_playlists": [],
        }
        seen_scripts: list[Path] = []
        seen_contents: list[str] = []

        def report(mode: str) -> str:
            return (
                '{"mode":"' + mode + '","preflight_errors":[],"applied":{"favorites":0,'
                '"ratings":0,"memberships":0},"skipped":{"favorites":0,'
                '"ratings":0,"memberships":0},"failures":[]}'
            )

        def completed(stdout: str, stderr: str = "", returncode: int = 0):
            def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                script = Path(args[-1])
                self.assertTrue(script.is_file())
                seen_scripts.append(script)
                seen_contents.append(script.read_text())
                return subprocess.CompletedProcess(args, returncode, stdout, stderr)

            return run

        with mock.patch(
            "apple_music_apply.subprocess.run", side_effect=completed(report("dry-run"))
        ):
            self.assertEqual(run_music_recovery(plan, apply=False)["mode"], "dry-run")
        with mock.patch("apple_music_apply.subprocess.run", side_effect=completed(report("apply"))):
            self.assertEqual(
                run_music_recovery(plan, apply=True, replace_disliked=True)["mode"],
                "apply",
            )
        with (
            mock.patch(
                "apple_music_apply.subprocess.run",
                side_effect=completed("", "permission denied", 1),
            ),
            self.assertRaisesRegex(RuntimeError, "permission denied"),
        ):
            run_music_recovery(plan, apply=False)
        with (
            mock.patch("apple_music_apply.subprocess.run", side_effect=completed("not json")),
            self.assertRaisesRegex(RuntimeError, "invalid JSON"),
        ):
            run_music_recovery(plan, apply=False)

        self.assertEqual(len(seen_scripts), 4)
        self.assertIn("const replaceDisliked = false;", seen_contents[0])
        self.assertIn("const replaceDisliked = true;", seen_contents[1])
        self.assertTrue(all(not path.exists() for path in seen_scripts))


if __name__ == "__main__":
    unittest.main()
