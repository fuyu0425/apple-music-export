import contextlib
import hashlib
import json
import os
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app import byte_range, decode_artwork, make_handler, newest_snapshot


class CapturingServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs) -> None:
        self.errors: list[BaseException | None] = []
        self.error_event = threading.Event()
        super().__init__(*args, **kwargs)

    def handle_error(self, request, client_address) -> None:
        self.errors.append(sys.exc_info()[1])
        self.error_event.set()


class AppTest(unittest.TestCase):
    def test_read_only_api_and_audio_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "library.sqlite3"
            media = root / "song.m4a"
            media.write_bytes(b"0123456789")
            static = root / "static"
            static.mkdir()
            (static / "index.html").write_text("<main>Music</main>")

            with contextlib.closing(sqlite3.connect(snapshot)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE tracks (
                        persistent_id TEXT PRIMARY KEY, name TEXT, artist TEXT, album TEXT,
                        location TEXT, duration REAL, rating INTEGER, favorited INTEGER
                    );
                    CREATE TABLE playlists (
                        persistent_id TEXT PRIMARY KEY, name TEXT, smart INTEGER
                    );
                    CREATE TABLE track_playlists (
                        track_persistent_id TEXT, playlist_persistent_id TEXT
                    );
                    """
                )
                connection.execute("INSERT INTO metadata VALUES ('track_count', '1')")
                connection.execute(
                    "INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("0123456789ABCDEF", "Song", "Artist", "Album", str(media), 245.5, 80, 1),
                )
                connection.execute("INSERT INTO playlists VALUES ('LIST', 'Favorites', 1)")
                connection.execute(
                    "INSERT INTO track_playlists VALUES ('0123456789ABCDEF', 'LIST')"
                )

            checksum = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(snapshot, static))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(f"{origin}/api/library") as response:
                    library = json.load(response)
                with urllib.request.urlopen(f"{origin}/api/tracks?playlist_id=LIST") as response:
                    tracks = json.load(response)["tracks"]
                with (
                    mock.patch(
                        "app.track_artwork", return_value=(b"\xff\xd8\xffimage", "image/jpeg")
                    ),
                    urllib.request.urlopen(
                        f"{origin}/api/tracks/0123456789ABCDEF/artwork"
                    ) as response,
                ):
                    artwork = response.read()
                    artwork_type = response.headers["Content-Type"]
                request = urllib.request.Request(
                    f"{origin}/api/tracks/0123456789ABCDEF/audio",
                    headers={"Range": "bytes=2-5"},
                )
                with urllib.request.urlopen(request) as response:
                    audio = response.read()
                    content_range = response.headers["Content-Range"]
                    status = response.status
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

            self.assertEqual(library["playlists"][0]["name"], "Favorites")
            self.assertEqual(tracks[0]["persistent_id"], "0123456789ABCDEF")
            self.assertEqual(tracks[0]["duration"], 245.5)
            self.assertEqual(tracks[0]["playable"], 1)
            self.assertEqual((artwork_type, artwork), ("image/jpeg", b"\xff\xd8\xffimage"))
            self.assertEqual((status, content_range, audio), (206, "bytes 2-5/10", b"2345"))
            self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), checksum)

    def test_audio_client_disconnect_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "library.sqlite3"
            media = root / "song.mp3"
            with media.open("wb") as output:
                output.truncate(32 * 1024 * 1024)
            static = root / "static"
            static.mkdir()

            with contextlib.closing(sqlite3.connect(snapshot)) as connection, connection:
                connection.execute(
                    "CREATE TABLE tracks (persistent_id TEXT PRIMARY KEY, location TEXT)"
                )
                connection.execute(
                    "INSERT INTO tracks VALUES (?, ?)", ("0123456789ABCDEF", str(media))
                )

            server = CapturingServer(("127.0.0.1", 0), make_handler(snapshot, static))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = socket.create_connection(("127.0.0.1", server.server_port))
                client.sendall(
                    b"GET /api/tracks/0123456789ABCDEF/audio HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\nRange: bytes=0-\r\n\r\n"
                )
                client.recv(4096)
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                client.close()
                server.error_event.wait(1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

            self.assertEqual(server.errors, [])

    def test_snapshot_selection_and_range_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "older.sqlite3"
            newer = root / "newer.sqlite3"
            older.touch()
            newer.touch()
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))
            self.assertEqual(newest_snapshot(root), newer)

        self.assertEqual(byte_range(None, 10), (0, 9, False))
        self.assertEqual(byte_range("bytes=-3", 10), (7, 9, True))
        with self.assertRaises(ValueError):
            byte_range("bytes=10-20", 10)
        self.assertEqual(decode_artwork("'tdta'($FFD8FF00$)"), (b"\xff\xd8\xff\x00", "image/jpeg"))
        self.assertIsNone(decode_artwork("missing"))


if __name__ == "__main__":
    unittest.main()
