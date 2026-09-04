from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import sqlite3
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import quote

from apple_music_strawberry import APP, import_library


class StrawberryImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "Music"
        self.root.mkdir()
        self.nfd = self.root / unicodedata.normalize("NFD", "夏影.m4a")
        self.nfd.touch()
        self.clear = self.root / "clear.m4a"
        self.clear.touch()
        self.folder_only = self.root / "folder-only.m4a"
        self.folder_only.touch()
        self.snapshot = self.base / "snapshot.sqlite3"
        self.database = self.base / "strawberry.db"
        self.settings = self.base / "settings.plist"
        self._make_snapshot()
        self._make_target()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_snapshot(self) -> None:
        with sqlite3.connect(self.snapshot) as connection:
            connection.executescript(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);"
                "CREATE TABLE tracks(persistent_id TEXT PRIMARY KEY, name TEXT, artist TEXT, rating INTEGER, location TEXT);"
            )
            connection.execute("INSERT INTO metadata VALUES ('schema_version', '3')")
            connection.executemany(
                "INSERT INTO tracks VALUES (?, ?, ?, ?, ?)",
                [
                    ("A", "夏影", "麻枝准", 80, str(self.nfd)),
                    ("B", "夏影", "麻枝准", 80, str(self.nfd)),
                    ("C", "Clear", "Artist", 0, str(self.clear)),
                    ("D", "Missing", "Artist", 20, None),
                ],
            )

    def _make_target(self, *, omit_clear: bool = False, duplicate: bool = False) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                "CREATE TABLE schema_version(version INTEGER);"
                "INSERT INTO schema_version VALUES (23);"
                "CREATE TABLE directories(path TEXT, subdirs INTEGER);"
                "CREATE TABLE songs(url TEXT, rating REAL, unavailable INTEGER DEFAULT 0);"
            )
            connection.execute("INSERT INTO directories VALUES (?, 1)", (str(self.root),))
            target_nfc = unicodedata.normalize("NFC", str(self.nfd))
            rows = [(Path(target_nfc).as_uri(), 0.799, 0), (self.folder_only.as_uri(), 0.2, 0)]
            if not omit_clear:
                rows.append((self.clear.as_uri(), 0.6, 0))
            if duplicate:
                rows.append((Path(target_nfc).as_uri(), -1, 0))
            connection.executemany("INSERT INTO songs VALUES (?, ?, ?)", rows)

    def _args(self, name: str, *, apply: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            snapshot=self.snapshot,
            snapshot_sha256=hashlib.sha256(self.snapshot.read_bytes()).hexdigest(),
            strawberry_db=self.database,
            strawberry_settings=self.settings,
            collection_root=self.root,
            strawberry_app=APP,
            output=self.base / name,
            apply=apply,
        )

    def _report(self, name: str) -> dict:
        return json.loads((self.base / name / "result.json").read_text())

    def test_join_collapse_report_preserve_clear_and_poll(self) -> None:
        calls: list[tuple[int, list[str]]] = []

        def sender(rating: int, files: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append((rating, files))
            with sqlite3.connect(self.database) as connection:
                for path in files:
                    connection.execute(
                        "UPDATE songs SET rating = ? WHERE url = ?",
                        (rating / 100, "file://" + quote(path)),
                    )
            return subprocess.CompletedProcess([], 0, "", "")

        status = import_library(
            self._args("audit", apply=True),
            sender=sender,
            process_check=lambda _path: None,
            poll_seconds=0.2,
        )
        self.assertEqual(status, 0)
        report = self._report("audit")
        self.assertEqual(report["counts"]["matched_files"], 2)
        self.assertEqual(report["counts"]["missing_locations"], 1)
        self.assertEqual(report["counts"]["duplicate_location_groups"], 1)
        self.assertEqual(report["folder_only_files"], [str(self.folder_only)])
        self.assertEqual(calls[0], (0, [str(self.clear)]))
        self.assertEqual(report["duplicate_locations"][0]["persistent_ids"], ["A", "B"])
        with sqlite3.connect(self.database) as connection:
            folder_rating = connection.execute(
                "SELECT rating FROM songs WHERE url = ?", (self.folder_only.as_uri(),)
            ).fetchone()[0]
        self.assertEqual(folder_rating, 0.2)
        self.assertTrue((self.base / "audit/strawberry-before.sqlite3").exists())
        self.assertTrue((self.base / "audit/strawberry-after.sqlite3").exists())

    def test_absent_source_fails_without_sender(self) -> None:
        self.clear.unlink()
        calls: list[object] = []
        status = import_library(self._args("absent"), sender=lambda *args: calls.append(args))
        self.assertEqual(status, 1)
        self.assertFalse(calls)

    def test_missing_target_fails_without_sender(self) -> None:
        self.database.unlink()
        self._make_target(omit_clear=True)
        calls: list[object] = []
        status = import_library(self._args("missing"), sender=lambda *args: calls.append(args))
        self.assertEqual(status, 1)
        self.assertFalse(calls)

    def test_duplicate_target_fails_without_sender(self) -> None:
        self.database.unlink()
        self._make_target(duplicate=True)
        calls: list[object] = []
        status = import_library(self._args("duplicate"), sender=lambda *args: calls.append(args))
        self.assertEqual(status, 1)
        self.assertFalse(calls)

    def test_conflicting_source_ratings_fail_without_sender(self) -> None:
        with sqlite3.connect(self.snapshot) as connection:
            connection.execute("UPDATE tracks SET rating = 100 WHERE persistent_id = 'B'")
        args = self._args("conflict")
        args.snapshot_sha256 = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        calls: list[object] = []
        status = import_library(args, sender=lambda *args: calls.append(args))
        self.assertEqual(status, 1)
        self.assertFalse(calls)

    def test_unsafe_settings_fail_without_sender(self) -> None:
        for index, key in enumerate(("Collection/save_ratings", "Collection/overwrite_rating")):
            with self.subTest(key=key):
                with self.settings.open("wb") as stream:
                    plistlib.dump({key: True}, stream)
                calls: list[object] = []
                status = import_library(
                    self._args(f"setting-{index}"),
                    sender=lambda *args, calls=calls: calls.append(args),
                )
                self.assertEqual(status, 1)
                self.assertFalse(calls)

    def test_stale_process_fails_before_sender(self) -> None:
        calls: list[object] = []
        status = import_library(
            self._args("stale", apply=True),
            sender=lambda *args: calls.append(args),
            process_check=lambda _path: (
                "Primary strawberry process predates the installed executable"
            ),
        )
        self.assertEqual(status, 1)
        self.assertFalse(calls)

    def test_delivery_error_and_timeout_stop_after_first_batch(self) -> None:
        target_nfc = Path(unicodedata.normalize("NFC", str(self.nfd))).as_uri()
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE songs SET rating = 0.1 WHERE url = ?", (target_nfc,))
        for name, sender in (
            (
                "delivery",
                lambda *_args: subprocess.CompletedProcess(
                    [], 0, "Could not send message to primary instance.", ""
                ),
            ),
            (
                "timeout",
                lambda *_args: (_ for _ in ()).throw(subprocess.TimeoutExpired("sender", 15)),
            ),
        ):
            with self.subTest(name=name):
                status = import_library(
                    self._args(name, apply=True),
                    sender=sender,
                    process_check=lambda _path: None,
                )
                self.assertEqual(status, 1)
                report = self._report(name)
                self.assertEqual(len(report["command_launch_failures"]), 1)
                self.assertIsNone(report["batches"][1]["status"])


if __name__ == "__main__":
    unittest.main()
