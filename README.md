# Apple Music Export

Export Apple Music library state to SQLite, compare snapshots, and recover selected state through Music's scripting interface.

The project targets macOS. It never writes `Library.musicdb` directly.

## Features

- Export tracks, ratings, favorites, playlists, and playlist memberships to timestamped SQLite snapshots.
- Browse a snapshot through a local web interface.
- Merge recoverable state from an older snapshot into a current snapshot by persistent ID.
- Preview live recovery before mutation.
- Back up the active `.musiclibrary` package and current logical state before live recovery.
- Verify an applied recovery with a fresh post-recovery export.

Smart playlist memberships remain read-only. Recovery only adds missing regular playlist memberships.

## Requirements

- macOS with Music installed
- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/)
- Node.js and npm for the snapshot browser
- [just](https://github.com/casey/just) for the provided task commands

Install the Python development tools:

```bash
uv sync
```

## Export a snapshot

Open the intended library in Music, then run:

```bash
just export
```

The exporter writes a timestamped SQLite file under `snapshots/`.

## Browse snapshots

Start the local browser:

```bash
just serve
```

Open <http://127.0.0.1:8000>. The server uses the newest snapshot by default.

To select a snapshot directly:

```bash
uv run python app.py --snapshot snapshots/apple-music-example.sqlite3
```

## Merge snapshots offline

Use the older or healthy snapshot as `--healthy`. Use the latest export as `--current`.

```bash
uv run python apple_music_recover.py \
  --healthy snapshots/healthy.sqlite3 \
  --current snapshots/current.sqlite3 \
  --output snapshots/recovered.sqlite3
```

The merge keeps the current library as its base. It restores:

- The union of favorites.
- A healthy nonzero rating only when the current rating is zero.
- Missing memberships for regular playlists shared by both snapshots.

The merge excludes healthy-only tracks, healthy-only playlists, and smart playlist memberships.

## Recover the live library

Use a new output directory for every run. The default command performs a dry run after both backups and live preflight checks.

```bash
uv run python apple_music_apply.py \
  --restore-from snapshots/recovered.sqlite3 \
  --library "/path/to/Music Library.musiclibrary" \
  --output snapshots/live-recovery-dry-run
```

Review `plan.json` and `result.json`. Then repeat with a new output directory and explicit apply intent:

```bash
uv run python apple_music_apply.py \
  --restore-from snapshots/recovered.sqlite3 \
  --library "/path/to/Music Library.musiclibrary" \
  --output snapshots/live-recovery-apply \
  --apply
```

The command refuses planned favorites that are currently disliked. If you explicitly want to replace those dislikes with favorites, add `--replace-disliked` to the apply command.

Each live run retains its package backup, `current.sqlite3`, `target.sqlite3`, `plan.json`, `result.json`, and completed post-recovery export.

## Checks

```bash
just check
```

## Privacy

Snapshots and live recovery artifacts can contain personal library data. The repository ignores `snapshots/`, virtual environments, dependency directories, and build output.

## Credit

This project used [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp) as a reference for Apple Music scripting and automation behavior.
