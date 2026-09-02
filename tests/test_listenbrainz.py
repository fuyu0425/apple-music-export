from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock
from urllib import error, request

import apple_music_listenbrainz as listenbrainz


class FakeResponse:
    def __init__(self, body: object, headers: dict[str, str] | None = None) -> None:
        self.body = json.dumps(body).encode()
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self.body


class FakeOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[request.Request, int]] = []

    def open(self, req: request.Request, timeout: int):
        self.calls.append((req, timeout))
        return next(self.responses)


class ListenBrainzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def snapshot(self, tracks: list[tuple[str, str, str, str, str | None]]) -> Path:
        path = self.root / "snapshot.sqlite3"
        connection = sqlite3.connect(path)
        with contextlib.closing(connection):
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE tracks (
                    persistent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    album TEXT NOT NULL,
                    last_played_at TEXT
                );
                INSERT INTO metadata VALUES ('schema_version', '3');
                """
            )
            connection.executemany("INSERT INTO tracks VALUES (?, ?, ?, ?, ?)", tracks)
            connection.commit()
        return path

    def test_maps_dates_exclusions_order_and_optional_album(self) -> None:
        snapshot = self.snapshot(
            [
                ("B", "Later", "Artist", "", "2024-01-02T01:00:00+01:00"),
                ("A", "Earlier", "Artist", "Album", "2024-01-01T00:00:00Z"),
                ("OLD", "Old", "Artist", "Album", "2001-01-01T00:00:00Z"),
                ("NONE", "Never", "Artist", "Album", None),
                ("NOARTIST", "Skipped", "  ", "Album", "2024-01-01T00:00:00Z"),
            ]
        )

        listens, counts = listenbrainz._read_import(snapshot, 2_000_000_000)

        self.assertEqual(
            counts,
            {"total": 5, "unplayed": 1, "missing-artist": 1, "too-old": 1, "ready": 2},
        )
        self.assertEqual(
            [listen["listened_at"] for listen in listens],
            [
                int(datetime(2024, 1, 1, tzinfo=UTC).timestamp()),
                int(datetime(2024, 1, 2, tzinfo=UTC).timestamp()),
            ],
        )
        self.assertEqual(
            listens[0]["track_metadata"],
            {"artist_name": "Artist", "track_name": "Earlier", "release_name": "Album"},
        )
        self.assertEqual(
            listens[1]["track_metadata"], {"artist_name": "Artist", "track_name": "Later"}
        )

    def test_blank_artist_does_not_bypass_date_validation(self) -> None:
        snapshot = self.snapshot([("BAD", "Song", "  ", "Album", "not-a-date")])
        with self.assertRaisesRegex(ValueError, "BAD.*invalid last_played_at"):
            listenbrainz._read_import(snapshot, 2_000_000_000)

    def test_invalid_metadata_stops_before_http(self) -> None:
        snapshot = self.snapshot([("BAD", "  ", "Artist", "Album", "2024-01-01T00:00:00Z")])
        with mock.patch.object(request, "build_opener") as build_opener:
            result = listenbrainz.main(["--snapshot", str(snapshot), "--submit"])
        self.assertEqual(result, 1)
        build_opener.assert_not_called()

    def test_dry_run_does_not_read_token_or_open_http(self) -> None:
        snapshot = self.snapshot([("A", "Song", "Artist", "Album", "2024-01-01T00:00:00Z")])
        original_get = listenbrainz.os.environ.get
        with (
            mock.patch.object(listenbrainz, "load_dotenv") as load_dotenv,
            mock.patch.object(
                listenbrainz.os.environ, "get", side_effect=original_get
            ) as get_token,
            mock.patch.object(request, "build_opener") as build_opener,
        ):
            result = listenbrainz.main(["--snapshot", str(snapshot)])
        self.assertEqual(result, 0)
        load_dotenv.assert_not_called()
        self.assertNotIn(mock.call("LISTENBRAINZ_TOKEN"), get_token.call_args_list)
        build_opener.assert_not_called()

    def test_submits_1001_listens_in_two_import_batches(self) -> None:
        listens = [
            {
                "listened_at": 1_700_000_000 + index,
                "track_metadata": {"artist_name": "Artist", "track_name": f"Song {index}"},
            }
            for index in range(1_001)
        ]
        batches = listenbrainz._encode_batches(listens)
        opener = FakeOpener([FakeResponse({"status": "ok"}), FakeResponse({"status": "ok"})])

        with mock.patch.object(request, "build_opener", return_value=opener):
            listenbrainz._submit_batches(batches, "secret")

        payloads = [json.loads(call[0].data or b"") for call in opener.calls]
        self.assertEqual([len(payload["payload"]) for payload in payloads], [1_000, 1])
        self.assertEqual([payload["listen_type"] for payload in payloads], ["import", "import"])
        self.assertEqual(opener.calls[0][0].full_url, listenbrainz.ENDPOINT)
        self.assertEqual(opener.calls[0][0].get_header("Authorization"), "Token secret")
        self.assertEqual(opener.calls[0][1], 30)

    def test_rejects_non_ok_response_status(self) -> None:
        opener = FakeOpener([FakeResponse({"status": "nope"})])
        with (
            mock.patch.object(request, "build_opener", return_value=opener),
            self.assertRaisesRegex(RuntimeError, "status was not ok"),
        ):
            listenbrainz._submit_batches([b"{}"], "secret")

    def test_rejects_non_object_response(self) -> None:
        opener = FakeOpener([FakeResponse([{"status": "ok"}])])
        with (
            mock.patch.object(request, "build_opener", return_value=opener),
            self.assertRaisesRegex(RuntimeError, "status was not ok"),
        ):
            listenbrainz._submit_batches([b"{}"], "secret")

    def test_waits_when_rate_limit_is_exhausted(self) -> None:
        opener = FakeOpener(
            [
                FakeResponse(
                    {"status": "ok"},
                    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "1.5"},
                ),
                FakeResponse({"status": "ok"}),
            ]
        )
        with (
            mock.patch.object(request, "build_opener", return_value=opener),
            mock.patch.object(listenbrainz.time, "sleep") as sleep,
        ):
            listenbrainz._submit_batches([b"{}", b"{}"], "secret")
        sleep.assert_called_once_with(1.5)

    def test_redirect_handler_rejects_redirect(self) -> None:
        original = request.Request(
            listenbrainz.ENDPOINT,
            headers={"Authorization": "Token secret"},
        )
        with self.assertRaisesRegex(error.HTTPError, "redirect rejected"):
            listenbrainz._NoRedirect().redirect_request(
                original,
                None,
                307,
                "redirect",
                {},
                "https://example.test/steal",
            )


if __name__ == "__main__":
    unittest.main()
