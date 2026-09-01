from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import apple_music_metadata_apply as metadata_apply


class Response:
    def __init__(self, body: bytes, content_length: str | None = None) -> None:
        self.body = body
        self.offset = 0
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self, size: int) -> bytes:
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class MetadataApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.library = self.root / "Music Library.musiclibrary"
        self.library.mkdir()
        self.plan_path = self.root / "reviewed.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def change(self, *, artwork: bool = False, approved: object = True) -> dict[str, object]:
        change: dict[str, object] = {
            "persistent_id": "0123456789ABCDEF",
            "approved": approved,
            "match_status": "needs_review",
            "current": {"title": "Old", "artist": "Artist", "album": "Album"},
            "suggested": {"title": "New"},
        }
        if artwork:
            change["artwork"] = {
                "source": "cover_art_archive",
                "release_id": "release-a",
                "url": "https://coverartarchive.org/release/release-a/front.jpg",
            }
        return change

    def write_plan(self, changes: list[dict[str, object]]) -> None:
        self.plan_path.write_text(
            json.dumps(
                {
                    "snapshot": "/tmp/snapshot.sqlite3",
                    "audit": "/tmp/audit.csv",
                    "review_report": "/tmp/report.csv",
                    "metadata_changes": changes,
                }
            ),
            encoding="utf-8",
        )

    def args(self, output: Path, *, apply: bool = False) -> list[str]:
        args = [
            "--plan",
            str(self.plan_path),
            "--library",
            str(self.library),
            "--output",
            str(output),
        ]
        if apply:
            args.append("--apply")
        return args

    @staticmethod
    def report(action: str, status: str) -> dict[str, object]:
        return {
            "mode": action,
            "preflight_errors": [],
            "entries": [
                {
                    "persistent_id": "0123456789ABCDEF",
                    "metadata": {"title": status},
                }
            ],
            "failures": [],
        }

    @staticmethod
    def make_snapshot(path: Path, title: str) -> Path:
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE tracks (persistent_id TEXT PRIMARY KEY, name TEXT, artist TEXT, album TEXT)"
            )
            connection.execute(
                "INSERT INTO tracks VALUES (?, ?, ?, ?)",
                ("0123456789ABCDEF", title, "Artist", "Album"),
            )
            connection.commit()
        finally:
            connection.close()
        return path

    def test_validate_plan_is_strict_and_requires_approved_change(self) -> None:
        valid = {
            "snapshot": "s",
            "audit": "a",
            "review_report": "r",
            "metadata_changes": [self.change()],
        }
        approved, errors = metadata_apply.validate_plan(valid)
        self.assertEqual(len(approved), 1)
        self.assertEqual(errors, [])

        invalid = json.loads(json.dumps(valid))
        invalid["metadata_changes"][0]["approved"] = 1
        invalid["metadata_changes"][0]["artwork"] = {
            "source": "wrong",
            "release_id": "release-a",
            "url": "http://coverartarchive.org/release/release-a/front.jpg",
        }
        invalid["metadata_changes"].append(json.loads(json.dumps(invalid["metadata_changes"][0])))
        approved, errors = metadata_apply.validate_plan(invalid)
        self.assertEqual(approved, [])
        self.assertTrue(any("approved must be a boolean" in error for error in errors))
        self.assertTrue(any("duplicate" in error for error in errors))
        self.assertTrue(any("artwork source" in error for error in errors))
        self.assertTrue(any("no approved" in error for error in errors))

    def test_music_not_running_writes_result_before_backup_or_runners(self) -> None:
        self.write_plan([self.change(artwork=True)])
        output = self.root / "not-running"
        with (
            mock.patch.object(
                metadata_apply, "_require_music_running", side_effect=RuntimeError("Music stopped")
            ),
            mock.patch.object(metadata_apply, "copy_library_package") as backup,
            mock.patch.object(metadata_apply, "_download_artwork") as download,
            mock.patch.object(metadata_apply, "run_metadata") as runner,
            mock.patch.object(metadata_apply, "run_artwork") as artwork_runner,
        ):
            self.assertEqual(metadata_apply.main(self.args(output)), 1)
        result = json.loads((output / "result.json").read_text())
        self.assertIn("Music stopped", result["preflight_errors"])
        backup.assert_not_called()
        download.assert_not_called()
        runner.assert_not_called()
        artwork_runner.assert_not_called()

    def test_backup_failure_stops_before_download_and_runner(self) -> None:
        self.write_plan([self.change(artwork=True)])
        output = self.root / "backup-failure"
        with (
            mock.patch.object(metadata_apply, "_require_music_running"),
            mock.patch.object(metadata_apply, "_require_active_library", return_value=self.library),
            mock.patch.object(
                metadata_apply,
                "copy_library_package",
                side_effect=RuntimeError("stable backup failed"),
            ),
            mock.patch.object(metadata_apply, "_download_artwork") as download,
            mock.patch.object(metadata_apply, "run_metadata") as runner,
        ):
            self.assertEqual(metadata_apply.main(self.args(output)), 1)
        result = json.loads((output / "result.json").read_text())
        self.assertIn("stable backup failed", result["preflight_errors"])
        download.assert_not_called()
        runner.assert_not_called()

    def test_dry_run_stages_art_and_reports_planned_without_mutation(self) -> None:
        self.write_plan([self.change(artwork=True)])
        output = self.root / "dry-run"
        staged = self.root / "staged.jpg"
        staged.write_bytes(b"\xff\xd8\xffimage")
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        with (
            mock.patch.object(metadata_apply, "_require_music_running"),
            mock.patch.object(metadata_apply, "_require_active_library", return_value=self.library),
            mock.patch.object(metadata_apply, "copy_library_package"),
            mock.patch.object(
                metadata_apply, "_export_snapshot", return_value=self.root / "current.sqlite3"
            ),
            mock.patch.object(
                metadata_apply,
                "_download_artwork",
                return_value={
                    "path": str(staged),
                    "bytes": staged.stat().st_size,
                    "sha256": digest,
                    "pre_add_count": None,
                },
            ),
            mock.patch.object(
                metadata_apply,
                "run_metadata",
                return_value=self.report("preflight", "planned"),
            ) as runner,
            mock.patch.object(
                metadata_apply, "run_artwork", return_value="missing"
            ) as artwork_runner,
        ):
            self.assertEqual(metadata_apply.main(self.args(output)), 0)
        result = json.loads((output / "result.json").read_text())
        self.assertEqual(result["entries"][0]["metadata"], {"title": "planned"})
        self.assertEqual(result["entries"][0]["artwork"], "planned")
        self.assertEqual(result["entries"][0]["staged_artwork"]["pre_add_count"], 0)
        runner.assert_called_once()
        artwork_runner.assert_called_once_with("inspect", "0123456789ABCDEF", None)
        self.assertFalse((output / "after.sqlite3").exists())

    def test_late_library_change_stops_after_staging_before_runners(self) -> None:
        self.write_plan([self.change(artwork=True)])
        output = self.root / "late-library"
        with (
            mock.patch.object(metadata_apply, "_require_music_running"),
            mock.patch.object(
                metadata_apply,
                "_require_active_library",
                side_effect=[self.library, RuntimeError("active library changed")],
            ),
            mock.patch.object(metadata_apply, "copy_library_package"),
            mock.patch.object(
                metadata_apply, "_export_snapshot", return_value=self.root / "current.sqlite3"
            ),
            mock.patch.object(
                metadata_apply,
                "_download_artwork",
                return_value={"path": "x.jpg", "bytes": 1, "sha256": "0", "pre_add_count": None},
            ),
            mock.patch.object(metadata_apply, "run_metadata") as runner,
            mock.patch.object(metadata_apply, "run_artwork") as artwork_runner,
        ):
            self.assertEqual(metadata_apply.main(self.args(output)), 1)
        result = json.loads((output / "result.json").read_text())
        self.assertIn("active library changed", result["preflight_errors"])
        runner.assert_not_called()
        artwork_runner.assert_not_called()

    def test_existing_artwork_is_skipped(self) -> None:
        self.write_plan([self.change(artwork=True)])
        output = self.root / "existing"
        with (
            mock.patch.object(metadata_apply, "_require_music_running"),
            mock.patch.object(metadata_apply, "_require_active_library", return_value=self.library),
            mock.patch.object(metadata_apply, "copy_library_package"),
            mock.patch.object(
                metadata_apply, "_export_snapshot", return_value=self.root / "current.sqlite3"
            ),
            mock.patch.object(
                metadata_apply,
                "_download_artwork",
                return_value={"path": "x.jpg", "bytes": 1, "sha256": "0", "pre_add_count": None},
            ),
            mock.patch.object(
                metadata_apply,
                "run_metadata",
                return_value=self.report("preflight", "already_applied"),
            ),
            mock.patch.object(metadata_apply, "run_artwork", return_value="existing"),
        ):
            self.assertEqual(metadata_apply.main(self.args(output)), 0)
        result = json.loads((output / "result.json").read_text())
        self.assertEqual(result["entries"][0]["artwork"], "skipped_existing")

    def test_successful_apply_verifies_metadata_and_added_artwork(self) -> None:
        self.write_plan([self.change(artwork=True)])
        output = self.root / "apply"
        current = self.root / "current.sqlite3"
        after = self.make_snapshot(self.root / "after.sqlite3", "New")
        image = b"\xff\xd8\xffimage"
        staged = self.root / "staged.jpg"
        staged.write_bytes(image)
        digest = hashlib.sha256(image).hexdigest()

        def metadata_runner(_changes, action):
            return self.report(action, "planned" if action == "preflight" else "applied")

        with (
            mock.patch.object(metadata_apply, "_require_music_running"),
            mock.patch.object(metadata_apply, "_require_active_library", return_value=self.library),
            mock.patch.object(metadata_apply, "copy_library_package"),
            mock.patch.object(metadata_apply, "_export_snapshot", side_effect=[current, after]),
            mock.patch.object(
                metadata_apply,
                "_download_artwork",
                return_value={
                    "path": str(staged),
                    "bytes": len(image),
                    "sha256": digest,
                    "pre_add_count": None,
                },
            ),
            mock.patch.object(metadata_apply, "run_metadata", side_effect=metadata_runner),
            mock.patch.object(metadata_apply, "run_artwork", side_effect=["missing", "added"]),
            mock.patch.object(
                metadata_apply.app, "track_artwork", return_value=(image, "image/jpeg")
            ),
        ):
            self.assertEqual(metadata_apply.main(self.args(output, apply=True)), 0)
        result = json.loads((output / "result.json").read_text())
        self.assertEqual(result["entries"][0]["metadata"], {"title": "applied"})
        self.assertEqual(result["entries"][0]["artwork"], "added")
        self.assertEqual(result["verification_failures"], [])
        self.assertEqual(result["post_snapshot"], str(after))

    def test_artwork_failure_rolls_back_applied_metadata(self) -> None:
        self.write_plan([self.change(artwork=True)])
        output = self.root / "rollback"
        current = self.root / "current.sqlite3"
        after = self.make_snapshot(self.root / "rollback-after.sqlite3", "Old")
        staged = self.root / "staged.jpg"
        staged.write_bytes(b"\xff\xd8\xffimage")
        reports = {
            "preflight": self.report("preflight", "planned"),
            "apply": self.report("apply", "applied"),
            "rollback": self.report("rollback", "restored"),
        }
        with (
            mock.patch.object(metadata_apply, "_require_music_running"),
            mock.patch.object(metadata_apply, "_require_active_library", return_value=self.library),
            mock.patch.object(metadata_apply, "copy_library_package"),
            mock.patch.object(metadata_apply, "_export_snapshot", side_effect=[current, after]),
            mock.patch.object(
                metadata_apply,
                "_download_artwork",
                return_value={
                    "path": str(staged),
                    "bytes": staged.stat().st_size,
                    "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                    "pre_add_count": None,
                },
            ),
            mock.patch.object(
                metadata_apply, "run_metadata", side_effect=lambda _changes, action: reports[action]
            ) as runner,
            mock.patch.object(
                metadata_apply,
                "run_artwork",
                side_effect=["missing", RuntimeError("art add failed")],
            ),
        ):
            self.assertEqual(metadata_apply.main(self.args(output, apply=True)), 1)
        result = json.loads((output / "result.json").read_text())
        self.assertEqual(result["entries"][0]["metadata"], {"title": "rolled_back"})
        self.assertEqual(result["entries"][0]["artwork"], "error")
        self.assertEqual(result["artwork_errors"][0]["operation"], "add-if-missing")
        self.assertEqual(
            [call.args[1] for call in runner.call_args_list], ["preflight", "apply", "rollback"]
        )

    def test_safe_delete_requires_recorded_zero_count_and_matching_digest(self) -> None:
        image = b"\xff\xd8\xffimage"
        staged = self.root / "staged.jpg"
        staged.write_bytes(image)
        entry = {
            "persistent_id": "0123456789ABCDEF",
            "artwork": "added",
            "staged_artwork": {
                "path": str(staged),
                "bytes": len(image),
                "sha256": hashlib.sha256(image).hexdigest(),
                "pre_add_count": 0,
            },
        }
        with (
            mock.patch.object(
                metadata_apply, "run_artwork", side_effect=["existing", "deleted"]
            ) as runner,
            mock.patch.object(
                metadata_apply.app, "track_artwork", return_value=(image, "image/jpeg")
            ),
        ):
            metadata_apply._safe_delete_created({}, entry, self.root / "snapshot.sqlite3")
        self.assertEqual(entry["artwork"], "rolled_back")
        self.assertEqual(runner.call_args_list[-1].args[0], "delete-created")

        entry["artwork"] = "added"
        entry["staged_artwork"]["pre_add_count"] = None
        with (
            mock.patch.object(metadata_apply, "run_artwork") as runner,
            self.assertRaisesRegex(RuntimeError, "zero-artwork"),
        ):
            metadata_apply._safe_delete_created({}, entry, self.root / "snapshot.sqlite3")
        runner.assert_not_called()

    def test_download_validates_host_size_magic_and_redirects(self) -> None:
        artwork = {
            "release_id": "release-a",
            "url": "https://coverartarchive.org/release/release-a/front.jpg",
        }
        opener = mock.Mock()
        opener.open.return_value = Response(b"\xff\xd8\xffimage", "8")
        with mock.patch.object(urllib.request, "build_opener", return_value=opener):
            staged = metadata_apply._download_artwork(artwork, self.root / "cover")
        self.assertEqual(staged["bytes"], 8)
        self.assertTrue(Path(staged["path"]).name.endswith(".jpg"))

        bad = {**artwork, "url": "https://example.com/release/release-a/front.jpg"}
        with self.assertRaisesRegex(RuntimeError, "exact Cover Art Archive"):
            metadata_apply._download_artwork(bad, self.root / "bad")

        opener.open.return_value = Response(b"not-an-image")
        with (
            mock.patch.object(urllib.request, "build_opener", return_value=opener),
            self.assertRaisesRegex(RuntimeError, "not JPEG or PNG"),
        ):
            metadata_apply._download_artwork(artwork, self.root / "magic")

        opener.open.return_value = Response(b"", str(metadata_apply.MAX_ARTWORK_BYTES + 1))
        with (
            mock.patch.object(urllib.request, "build_opener", return_value=opener),
            self.assertRaisesRegex(RuntimeError, "10 MiB"),
        ):
            metadata_apply._download_artwork(artwork, self.root / "large")

        handler = metadata_apply._ArtworkRedirectHandler()
        request = urllib.request.Request(artwork["url"])
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(
                request,
                None,
                302,
                "redirect",
                {},
                "https://example.com/evil.jpg",
            )


if __name__ == "__main__":
    unittest.main()
