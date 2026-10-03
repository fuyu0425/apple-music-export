import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from apple_music_export import JXA_SCRIPT, write_snapshot


@unittest.skipUnless(shutil.which("osascript"), "Requires macOS JavaScript for Automation")
class PlaylistTrackExportTest(unittest.TestCase):
    def test_preserves_union_of_library_and_playlist_tracks(self) -> None:
        # Invariant: every referenced track survives once, with all memberships.
        for count in (0, 1, 8):
            with self.subTest(playlist_only_tracks=count):
                local = self.track("LOCAL", "/music/local.m4a")
                cloud = [self.track(f"CLOUD{i}", None) for i in range(count)]
                fixture = {"library": [local], "playlists": [cloud, [local, *cloud]]}
                script = (
                    "(() => { const fixture = "
                    + json.dumps(fixture)
                    + ";\n"
                    + r"""
function collection(rows) {
    const result = {length: rows.length};
    for (const key of ["persistentID", "databaseID", "name", "artist", "album",
                       "duration", "rating", "favorited", "playedDate", "location"]) {
        result[key] = () => rows.map(row => row[key]);
    }
    return result;
}
function container(rows) {
    return {tracks: collection(rows), fileTracks: collection(rows.filter(row => row.location))};
}
function Application() {
    return {
        libraryPlaylists: [container(fixture.library)],
        userPlaylists: () => fixture.playlists.map((rows, i) => Object.assign(container(rows), {
            persistentID: () => "PLAYLIST" + i, name: () => "Playlist " + i, smart: () => true
        }))
    };
}
"""
                    + "return eval("
                    + json.dumps(JXA_SCRIPT)
                    + "); })();"
                )
                result = subprocess.run(
                    ["osascript", "-l", "JavaScript", "-e", script],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                data = json.loads(result.stdout)
                with tempfile.TemporaryDirectory() as directory:
                    path, memberships = write_snapshot(data, Path(directory))
                    with closing(sqlite3.connect(path)) as connection:
                        rows = connection.execute(
                            "SELECT persistent_id, location, favorited FROM tracks"
                        ).fetchall()
                        self.assertEqual(
                            set(rows),
                            {("LOCAL", "/music/local.m4a", 1)}
                            | {(track["persistentID"], None, 1) for track in cloud},
                        )
                        self.assertEqual(len(rows), count + 1)
                        self.assertEqual(memberships, 2 * count + 1)
                        self.assertEqual(
                            connection.execute("PRAGMA foreign_key_check").fetchall(), []
                        )

    @staticmethod
    def track(persistent_id: str, location: str | None) -> dict[str, object]:
        return {
            "persistentID": persistent_id,
            "databaseID": 1,
            "name": persistent_id,
            "artist": "Artist",
            "album": "Album",
            "duration": 100,
            "rating": 0,
            "favorited": True,
            "playedDate": None,
            "location": location,
        }
