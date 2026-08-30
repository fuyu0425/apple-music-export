from __future__ import annotations

import argparse
import contextlib
import functools
import json
import mimetypes
import re
import sqlite3
import subprocess
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).parent
SNAPSHOTS = ROOT / "snapshots"
STATIC_DIR = ROOT / "frontend" / "dist"
RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)")
ARTWORK_DATA_PATTERN = re.compile(r"\(\$([0-9A-Fa-f]+)\$\)$")
PERSISTENT_ID_PATTERN = re.compile(r"[0-9A-F]{16}")
ARTWORK_SLOTS = threading.BoundedSemaphore(2)


def newest_snapshot(directory: Path = SNAPSHOTS) -> Path:
    snapshots = list(directory.glob("*.sqlite3"))
    if not snapshots:
        raise FileNotFoundError(f"No SQLite snapshots found in {directory}")
    return max(snapshots, key=lambda path: path.stat().st_mtime)


def connect_read_only(snapshot: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def library_payload(snapshot: Path) -> dict[str, Any]:
    with contextlib.closing(connect_read_only(snapshot)) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        playlists = [
            dict(row)
            for row in connection.execute(
                "SELECT persistent_id, name, smart FROM playlists ORDER BY name COLLATE NOCASE"
            )
        ]
    return {"snapshot": snapshot.name, "metadata": metadata, "playlists": playlists}


def tracks_payload(snapshot: Path, playlist_id: str | None = None) -> list[dict[str, Any]]:
    parameters: tuple[str, ...] = ()
    membership_join = ""
    if playlist_id:
        membership_join = (
            " JOIN track_playlists AS tp ON tp.track_persistent_id = t.persistent_id"
            " WHERE tp.playlist_persistent_id = ?"
        )
        parameters = (playlist_id,)

    with contextlib.closing(connect_read_only(snapshot)) as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(tracks)")}
        duration = "t.duration" if "duration" in columns else "0.0"
        rows = connection.execute(
            "SELECT t.persistent_id, t.name, t.artist, t.album, t.rating, t.favorited,"
            f" {duration} AS duration, t.location IS NOT NULL AS playable FROM tracks AS t"
            f"{membership_join}"
            " ORDER BY t.artist COLLATE NOCASE, t.album COLLATE NOCASE, t.name COLLATE NOCASE",
            parameters,
        )
        return [dict(row) for row in rows]


def track_media(snapshot: Path, persistent_id: str) -> Path | None:
    with contextlib.closing(connect_read_only(snapshot)) as connection:
        row = connection.execute(
            "SELECT location FROM tracks WHERE persistent_id = ?", (persistent_id,)
        ).fetchone()
    if row is None or row["location"] is None:
        return None

    location = row["location"]
    if location.startswith("file://"):
        location = unquote(urlparse(location).path)
    path = Path(location)
    return path if path.is_file() else None


def decode_artwork(raw_data: str) -> tuple[bytes, str] | None:
    match = ARTWORK_DATA_PATTERN.search(raw_data.strip())
    if match is None:
        return None
    data = bytes.fromhex(match.group(1))
    if data.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        media_type = "image/gif"
    else:
        media_type = "application/octet-stream"
    return data, media_type


# ponytail: cache 96 full-size covers; add thumbnail generation if memory or reload cost matters.
@functools.lru_cache(maxsize=96)
def track_artwork(snapshot: Path, persistent_id: str) -> tuple[bytes, str] | None:
    if PERSISTENT_ID_PATTERN.fullmatch(persistent_id) is None:
        return None
    with contextlib.closing(connect_read_only(snapshot)) as connection:
        exists = connection.execute(
            "SELECT 1 FROM tracks WHERE persistent_id = ?", (persistent_id,)
        ).fetchone()
    if exists is None:
        return None

    script = f"""
const music = Application("Music");
const matches = music.libraryPlaylists[0].tracks.whose({{persistentID: {json.dumps(persistent_id)}}})();
const track = matches[0];
track && track.artworks.length ? String(track.artworks[0].rawData()) : "";
"""
    try:
        with ARTWORK_SLOTS:
            result = subprocess.run(
                ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return decode_artwork(result.stdout) if result.returncode == 0 else None


def byte_range(header: str | None, size: int) -> tuple[int, int, bool]:
    if header is None:
        return 0, size - 1, False

    match = RANGE_PATTERN.fullmatch(header.strip())
    if match is None or not any(match.groups()):
        raise ValueError("Invalid byte range")

    start_text, end_text = match.groups()
    if start_text:
        start = int(start_text)
        end = min(int(end_text), size - 1) if end_text else size - 1
    else:
        length = min(int(end_text), size)
        if length == 0:
            raise ValueError("Invalid byte range")
        start, end = size - length, size - 1

    if start >= size or start > end:
        raise ValueError("Unsatisfied byte range")
    return start, end, True


def make_handler(snapshot: Path, static_dir: Path = STATIC_DIR) -> type[SimpleHTTPRequestHandler]:
    class MusicHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(static_dir), **kwargs)

        def do_GET(self) -> None:
            self._route(send_body=True)

        def do_HEAD(self) -> None:
            self._route(send_body=False)

        def _route(self, send_body: bool) -> None:
            request = urlparse(self.path)
            if request.path == "/api/library":
                self._json(library_payload(snapshot), send_body)
                return
            if request.path == "/api/tracks":
                playlist_id = parse_qs(request.query).get("playlist_id", [None])[0]
                self._json({"tracks": tracks_payload(snapshot, playlist_id)}, send_body)
                return
            if request.path.startswith("/api/tracks/") and request.path.endswith("/artwork"):
                persistent_id = unquote(
                    request.path.removeprefix("/api/tracks/").removesuffix("/artwork")
                )
                self._artwork(persistent_id, send_body)
                return
            if request.path.startswith("/api/tracks/") and request.path.endswith("/audio"):
                persistent_id = unquote(
                    request.path.removeprefix("/api/tracks/").removesuffix("/audio")
                )
                self._audio(persistent_id, send_body)
                return
            if request.path.startswith("/api/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if request.path != "/" and (static_dir / request.path.lstrip("/")).is_file():
                if send_body:
                    super().do_GET()
                else:
                    super().do_HEAD()
                return
            self.path = "/index.html"
            if send_body:
                super().do_GET()
            else:
                super().do_HEAD()

        def _json(self, payload: Any, send_body: bool) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _artwork(self, persistent_id: str, send_body: bool) -> None:
            artwork = track_artwork(snapshot, persistent_id)
            if artwork is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Track has no artwork")
                return
            data, media_type = artwork
            self.send_response(HTTPStatus.OK)
            self.send_header("Cache-Control", "private, max-age=86400")
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if send_body:
                self.wfile.write(data)

        def _audio(self, persistent_id: str, send_body: bool) -> None:
            media = track_media(snapshot, persistent_id)
            if media is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Track has no available local file")
                return

            size = media.stat().st_size
            try:
                start, end, partial = byte_range(self.headers.get("Range"), size)
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Type", mimetypes.guess_type(media.name)[0] or "audio/mpeg")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not send_body:
                return

            with media.open("rb") as source:
                source.seek(start)
                remaining = length
                while remaining:
                    chunk = source.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(chunk)

    return MusicHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Browse an Apple Music SQLite snapshot.")
    parser.add_argument("--snapshot", type=Path, help="Snapshot path. Defaults to the newest file.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = args.snapshot or newest_snapshot()
    if not STATIC_DIR.is_dir():
        raise RuntimeError("frontend/dist is missing. Run npm --prefix frontend run build.")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(snapshot))
    print(f"Apple Music library: http://{args.host}:{args.port}")
    print(f"Snapshot: {snapshot}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
