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

## Review metadata matches

Use `apple_music_metadata_match.py` to compare suspect metadata with AcoustID and MusicBrainz. One run creates a detailed review CSV and an unapproved JSON apply plan.

It never changes Apple Music, media tags, media paths, or a beets library. Neither artifact applies metadata or changes media files.

### What you need

- A schema-version-2 snapshot from `just export`.
- A private metadata audit CSV from the separate library-audit process.
- Chromaprint with the `fpcalc` command.
- An AcoustID application key.
- The optional metadata dependency group.

This repository does not generate the metadata audit CSV. The audit must describe the same snapshot passed to `--snapshot`.

The CSV must use this exact header:

```text
snapshot,persistent_id,status,reasons,current_title,current_artist,current_album,suggested_title,suggested_artist,suggested_album,evidence,source_urls,playlists,duration_seconds,location
```

The command validates every audit row before matching. It rejects stale metadata, duplicate IDs, unknown IDs, changed durations, and changed locations.

### Set up the matcher

1. Install Chromaprint.

   ```bash
   brew install chromaprint
   ```

2. Install the optional Python dependencies.

   ```bash
   uv sync --group metadata
   ```

3. Copy the environment template.

   ```bash
   cp .env.example .env
   ```

4. Add the AcoustID application key to `.env`.

   ```dotenv
   ACOUSTID_API_KEY=your-application-key
   ```

The command loads `.env` before preflight checks. An existing environment variable takes precedence over the file.

The command does not place the key in process arguments or report output.

### Run a metadata review

1. Choose the snapshot and its matching audit CSV.

2. Choose new paths for the review CSV and JSON apply plan.

3. Run the matcher.

   ```bash
   uv run --group metadata python apple_music_metadata_match.py \
     --snapshot snapshots/apple-music-20260831T192045.003847-0400.sqlite3 \
     --audit snapshots/apple-music-20260831T192045.003847-0400-metadata-audit.csv \
     --output snapshots/apple-music-20260831T192045.003847-0400-metadata-matches.csv \
     --plan-output snapshots/apple-music-20260831T192045.003847-0400-metadata-apply-plan.json
   ```

Both output paths must be new and must identify different files. Unicode-normalization-equivalent names identify the same file.

The command fingerprints readable media files with `fpcalc`. It sends fingerprints and durations to AcoustID, then queries MusicBrainz through beets.

The command publishes both artifacts only after all matching succeeds. A network or plan-writing failure does not leave a partial command result.

### Read the report

The report contains one row for each audit row. It sorts rows by match status, artist, album, title, and persistent ID.

| `match_status` | Meaning |
| --- | --- |
| `strong_candidate` | The proposal passed every strict identity and quality check. Review it before any manual change. |
| `needs_review` | The command found a candidate, but one or more strong checks did not pass. |
| `no_change` | The canonical candidate matches the current metadata under display normalization. |
| `unmatched` | AcoustID or MusicBrainz did not return a usable candidate. |
| `unavailable` | The media location was empty, unreadable, or could not produce a fingerprint. |

Use these fields during review:

- `suggested_title`, `suggested_artist`, and `suggested_album` contain source-backed changes only.
- `acoustid_id` and `acoustid_score` identify the selected fingerprint result.
- `musicbrainz_recording_id` and `musicbrainz_release_id` identify canonical MusicBrainz records.
- `beets_recommendation`, `beets_distance`, and `distance_penalties` explain the beets ranking.
- `evidence` combines the private audit evidence with matching diagnostics.
- `source_urls` links to the relevant AcoustID and MusicBrainz records.

A `strong_candidate` is still a review proposal. The command never applies the suggestion.

### Read the apply plan

The JSON plan keeps the report order. It includes each `strong_candidate` or `needs_review` row that has at least one nonempty suggestion.

Each entry includes all current metadata fields. Its `suggested` object includes only nonempty `title`, `artist`, and `album` changes.

Every entry has `"approved": false`, including each strong candidate. A run with no eligible suggestion writes an empty `metadata_changes` list.

The plan supports manual review only. It does not apply metadata.

### Fix setup errors

| Error | Action |
| --- | --- |
| `ACOUSTID_API_KEY is required` | Set a nonempty `ACOUSTID_API_KEY` environment variable. |
| `fpcalc is required; install Chromaprint` | Install Chromaprint and verify that `fpcalc` is on `PATH`. |
| `snapshot is not a file: PATH` | Pass an existing snapshot file to `--snapshot`. |
| `audit report is not a file: PATH` | Pass an existing audit CSV to `--audit`. |
| `output already exists: PATH` | Choose a new output path. |
| `plan output already exists: PATH` | Choose a new plan output path. |
| `report and plan outputs must be different` | Give `--output` and `--plan-output` paths that do not name the same file, including Unicode-normalization-equivalent names. |

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
