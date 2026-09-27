from __future__ import annotations

import plistlib
import sqlite3
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

import apple_music_alac as alac
from apple_music_export import SCHEMA


class AlacMigrationTest(unittest.TestCase):
    def make_snapshot(self, root: Path, media: Path) -> Path:
        snapshot = root / "source.sqlite3"
        connection = sqlite3.connect(snapshot)
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO metadata VALUES ('schema_version','3')")
        connection.execute(
            "INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("ABCDEF0123456789", 1, "Name", "Artist", "Album", str(media), None, 1.25, 80, 1),
        )
        connection.commit()
        connection.close()
        return snapshot

    def test_load_candidates_preserves_wire_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = alac.load_candidates(self.make_snapshot(root, root / "song.AIFF"))
        self.assertEqual(candidates[0]["database_id"], 1)
        self.assertEqual(
            candidates[0]["expected"],
            {"name": "Name", "artist": "Artist", "album": "Album", "rating": 80, "favorited": True},
        )

    def test_path_and_collision_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            composed = root / "SONG.AIFF"
            decomposed = root / unicodedata.normalize("NFD", "song.aiff")
            self.assertEqual(alac.collision_key(composed), alac.collision_key(decomposed))

    def test_xld_settings_accept_defaults_and_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = root / "Info.plist"
            prefs = root / "prefs.plist"
            info.write_bytes(
                plistlib.dumps({"CFBundleShortVersionString": alac.XLD_REQUIRED_VERSION})
            )
            self.assertEqual(alac.load_xld_settings(info, prefs)["samplerate_preference"], None)
            prefs.write_bytes(
                plistlib.dumps({"XLDAlacOutput_Samplerate": 0, "XLDAlacOutput_BitDepth": 0})
            )
            self.assertEqual(alac.load_xld_settings(info, prefs)["bit_depth_preference"], 0)
            prefs.write_bytes(plistlib.dumps({"XLDAlacOutput_Samplerate": 1}))
            with self.assertRaises(RuntimeError):
                alac.load_xld_settings(info, prefs)

    def test_remove_owned_checks_hash_and_absence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owned.m4a"
            key = frozenset({alac.path_key(path)})
            self.assertTrue(alac.remove_owned(path, None, key))
            path.write_bytes(b"audio")
            self.assertFalse(alac.remove_owned(path, "wrong", key))
            self.assertTrue(path.exists())
            self.assertTrue(alac.remove_owned(path, alac.file_sha256(path), key))

    def test_convert_rejects_encoder_option(self) -> None:
        result = mock.Mock(returncode=0, stderr="Encoder option: non-default\n")
        with (
            mock.patch("subprocess.run", return_value=result),
            self.assertRaises(RuntimeError),
        ):
            alac.convert_to_alac(Path("xld"), Path("in"), Path("out"))

    def test_verify_accepts_approved_duration_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "song.aiff"
            media.write_bytes(b"x")
            source = self.make_snapshot(root, media)
            observed = root / "observed.sqlite3"
            observed.write_bytes(source.read_bytes())
            connection = sqlite3.connect(observed)
            connection.execute("UPDATE tracks SET duration = duration + 0.093")
            connection.commit()
            connection.close()
            entry = alac.load_candidates(source)[0] | {
                "source_sha256": alac.file_sha256(media),
                "destination": str(media),
                "destination_sha256": alac.file_sha256(media),
                "pcm_sha256": "pcm",
            }
            with mock.patch.object(alac, "pcm_digest", return_value="pcm"):
                report = alac.verify_snapshot(
                    source, observed, [entry], {entry["persistent_id"]: media}
                )
            self.assertEqual(report["verification_failures"], [])

    def test_rewritten_destination_updates_only_current_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "song.m4a"
            destination.write_bytes(b"rewritten")
            initial = "initial"
            entry = {
                "persistent_id": "ABCDEF0123456789",
                "destination": str(destination),
                "destination_sha256_initial": initial,
                "destination_sha256": initial,
                "destination_rewritten": False,
                "rewritten_fields": [],
                "pcm_sha256": "pcm",
            }
            report: dict[str, object] = {}
            errors: list[object] = []
            with mock.patch.object(alac, "pcm_digest", return_value="pcm"):
                alac._record_runner_entries(
                    [
                        {
                            "persistent_id": entry["persistent_id"],
                            "rewritten_fields": ["name"],
                        }
                    ],
                    [entry],
                    root,
                    report,
                    errors,
                )
            self.assertEqual(errors, [])
            self.assertEqual(entry["destination_sha256_initial"], initial)
            self.assertEqual(entry["destination_sha256"], alac.file_sha256(destination))
            self.assertTrue(entry["destination_rewritten"])
            self.assertEqual(entry["rewritten_fields"], ["name"])


if __name__ == "__main__":
    unittest.main()
