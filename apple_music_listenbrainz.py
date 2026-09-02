from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, request

from dotenv import load_dotenv

import app

ENDPOINT = "https://api.listenbrainz.org/1/submit-listens"
MIN_LISTENED_AT = 1_033_410_600
MAX_LISTEN_BYTES = 10_240
MAX_BODY_BYTES = 10_240_000
BATCH_SIZE = 1_000


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


def _read_import(snapshot: Path, now: int) -> tuple[list[dict[str, object]], dict[str, int]]:
    if not snapshot.is_file():
        raise ValueError(f"snapshot is not a regular file: {snapshot}")

    with contextlib.closing(app.connect_read_only(snapshot)) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if metadata.get("schema_version") != "3":
            raise ValueError("snapshot schema_version must be 3; run just export")
        rows = connection.execute(
            "SELECT persistent_id, name, artist, album, last_played_at FROM tracks"
        ).fetchall()

    counts = {"total": len(rows), "unplayed": 0, "missing-artist": 0, "too-old": 0, "ready": 0}
    listens: list[dict[str, object]] = []
    for row in rows:
        persistent_id = row["persistent_id"]
        played_at = row["last_played_at"]
        if played_at is None:
            counts["unplayed"] += 1
            continue

        name = row["name"].strip()
        artist = row["artist"].strip()
        album = row["album"].strip()
        if not name or "\0" in name:
            raise ValueError(f"track {persistent_id}: invalid name")
        if "\0" in artist:
            raise ValueError(f"track {persistent_id}: invalid artist")
        if "\0" in album:
            raise ValueError(f"track {persistent_id}: invalid album")
        try:
            parsed = datetime.fromisoformat(played_at)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"track {persistent_id}: invalid last_played_at") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"track {persistent_id}: last_played_at requires a timezone")
        listened_at = int(parsed.timestamp())
        if listened_at < MIN_LISTENED_AT:
            counts["too-old"] += 1
            continue
        if listened_at >= now + 3_600:
            raise ValueError(f"track {persistent_id}: last_played_at is in the future")
        if not artist:
            counts["missing-artist"] += 1
            continue

        track_metadata: dict[str, object] = {"artist_name": artist, "track_name": name}
        if album:
            track_metadata["release_name"] = album
        listens.append(
            {
                "listened_at": listened_at,
                "track_metadata": track_metadata,
                "_persistent_id": persistent_id,
            }
        )

    listens.sort(key=lambda listen: (listen["listened_at"], listen["_persistent_id"]))
    for listen in listens:
        del listen["_persistent_id"]
    counts["ready"] = len(listens)
    return listens, counts


def _encode_batches(listens: Sequence[dict[str, object]]) -> list[bytes]:
    batches: list[bytes] = []
    for offset in range(0, len(listens), BATCH_SIZE):
        payload = list(listens[offset : offset + BATCH_SIZE])
        for listen in payload:
            encoded_listen = json.dumps(listen, ensure_ascii=False, separators=(",", ":")).encode()
            if len(encoded_listen) > MAX_LISTEN_BYTES:
                raise ValueError("one listen exceeds 10240 bytes")
        body = json.dumps(
            {"listen_type": "import", "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(body) > min(MAX_BODY_BYTES, len(payload) * MAX_LISTEN_BYTES):
            raise ValueError("one request body exceeds the ListenBrainz size limit")
        batches.append(body)
    return batches


def _response_detail(response: Any) -> str:
    detail = response.read(512).decode("utf-8", errors="replace").strip()
    return detail or "no response detail"


def _submit_batches(batches: Sequence[bytes], token: str) -> None:
    opener = request.build_opener(_NoRedirect())
    for index, body in enumerate(batches):
        http_request = request.Request(
            ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Token {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "apple-music-export/0.1.0",
            },
            method="POST",
        )
        try:
            with opener.open(http_request, timeout=30) as response:
                result = json.load(response)
                if not isinstance(result, dict) or result.get("status") != "ok":
                    raise RuntimeError("ListenBrainz response status was not ok")
                remaining = response.headers.get("X-RateLimit-Remaining")
                reset_in = response.headers.get("X-RateLimit-Reset-In")
        except error.HTTPError as exc:
            if exc.code in {400, 401}:
                raise RuntimeError(
                    f"ListenBrainz HTTP {exc.code}: {_response_detail(exc)}"
                ) from exc
            if exc.code == 429:
                reset = exc.headers.get("X-RateLimit-Reset-In", "missing")
                raise RuntimeError(f"ListenBrainz HTTP 429; X-RateLimit-Reset-In: {reset}") from exc
            raise RuntimeError(f"ListenBrainz HTTP {exc.code}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"ListenBrainz transport error: {exc.reason}") from exc

        if index + 1 < len(batches) and remaining == "0":
            try:
                delay = float(reset_in)
                if delay < 0:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "ListenBrainz rate limit reset header is missing or invalid"
                ) from exc
            time.sleep(delay)
    # ponytail: server dedup makes whole-import reruns safe; add retries only if failures become common.


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit Apple Music play dates to ListenBrainz.")
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--submit", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        listens, counts = _read_import(args.snapshot, int(time.time()))
        batches = _encode_batches(listens)
        print(f"snapshot: {args.snapshot}")
        for name in ("total", "unplayed", "missing-artist", "too-old", "ready"):
            print(f"{name}: {counts[name]}")
        print(f"batches: {len(batches)}")
        if not batches:
            print("no listens ready")
            return 0
        if not args.submit:
            print("dry run: no listens submitted")
            return 0

        load_dotenv(override=False)
        token = os.environ.get("LISTENBRAINZ_TOKEN")
        if not token:
            raise ValueError("LISTENBRAINZ_TOKEN is required with --submit")
        _submit_batches(batches, token)
    except (ValueError, RuntimeError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"accepted listens: {counts['ready']}")
    print(f"accepted batches: {len(batches)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
