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
- Emacs 29.1 or later for the metadata review package
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

For a singleton with an empty current album, the matcher scopes AcoustID
releases to the recording that beets selected. It orders releases by descending
AcoustID score and then release ID, and fetches at most five. A candidate must
contain the selected recording and have the exact MusicBrainz status `Official`.
The matcher chooses the lowest beets distance and then release ID. The report
includes probed and available counts. An omission describes only the bounded
probe. The row remains `needs_review`.

This extra probe does not replace the singleton title, artist, score, or
distance. If the release lookup fails, the report keeps the singleton result
and records the lookup error as evidence.

For an exact MusicBrainz release match, the matcher queries the Cover Art
Archive. It accepts the first approved front image and records the image URL.
An unavailable image does not remove the metadata proposal.

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
- `cover_art_url` identifies the exact-release front image when one is available.

A `strong_candidate` is still a review proposal. The command never applies the suggestion.

### Read the apply plan

The JSON plan keeps the report order. It includes each `strong_candidate` or `needs_review` row that has at least one nonempty suggestion.

Each entry includes all current metadata fields. Its `suggested` object includes only nonempty `title`, `artist`, and `album` changes.

When an exact-release front image is available, the entry also includes an
`artwork` object with its Cover Art Archive source, MusicBrainz release ID, and
HTTPS URL.

Every entry has `"approved": false`, including each strong candidate. A run with no eligible suggestion writes an empty `metadata_changes` list.

The plan supports manual review only. It does not apply metadata or artwork.

### Review needs-review entries in Emacs

Install the standalone package:

```text
M-x package-install-file
apple-music-metadata-review.el
```

Open the generated plan:

```text
M-x apple-music-metadata-review-open
/path/to/metadata-apply-plan.json
```

The package reads the linked review report and displays only the plan's `needs_review` entries. It leaves strong candidates untouched.

The overview uses separate current and suggested columns for title, artist, and album. Use a window about 150 columns wide. Approval counts stay in the mode line.
The detail buffer shows the artwork action and Cover Art Archive link when the
plan contains artwork.

Approval changes the whole entry's `approved` value. Saving writes an Emacs backup before it updates the plan.

The package never applies metadata. It does not change Apple Music, media files, snapshots, audits, or review reports.

| Key | Overview | Detail buffer |
| --- | --- | --- |
| `RET` | Open the selected entry | — |
| `SPC` | Toggle approval | Toggle approval |
| `a` | Approve the entry | Approve the entry |
| `!` | Approve, then select the next entry | Approve, then show the next entry |
| `u` | Unapprove the entry | Unapprove the entry |
| `n` | — | Show the next entry |
| `p` | — | Show the previous entry |
| `C-x C-s` | Save the plan | Save through the overview |
| `C-c C-c` | Save approvals and exit | Save approvals and exit |
| `C-c C-k` | Discard unsaved approvals and exit | Discard unsaved approvals and exit |
| `g` | Reload the plan and report | — |
| `q` | — | Close the detail window |

When Evil is loaded, both review buffers use motion state. Use `gr` to reload in Evil. The other review keys stay the same.

Use these actions when the package reports a review error:

| Error | Action |
| --- | --- |
| `Review report is not readable: PATH` | Restore the linked report at `PATH`, then open or reload the plan. |
| `Apply plan and review report disagree for persistent_id: ID` | Regenerate a matching plan and report pair. |
| `Apply plan changed; press g to reload before saving` | Press `g` in the overview to load the external plan change. |

### Dry-run approved metadata and artwork

Keep Music open with the intended library active. Use the same package path for
`--library` that Music currently uses.

Run the command without `--apply` first:

```bash
uv run --group metadata python apple_music_metadata_apply.py \
  --plan snapshots/reviewed-metadata-apply-plan.json \
  --library "/path/to/Music Library.musiclibrary" \
  --output snapshots/metadata-apply-dry-run
```

The command rejects an empty approved set. It then backs up the library package,
exports current state, stages approved artwork, and checks every live value.
The dry run does not change Music.

Review `result.json`. Use a new output directory and add `--apply` only after
the dry run has no error:

```bash
uv run --group metadata python apple_music_metadata_apply.py \
  --plan snapshots/reviewed-metadata-apply-plan.json \
  --library "/path/to/Music Library.musiclibrary" \
  --output snapshots/metadata-apply-live \
  --apply
```

The apply command updates only approved fields whose live values still match
the plan. It adds artwork only when the track has none. It verifies added image
bytes by SHA-256 and exports `after.sqlite3`.

If a later step fails, the command restores changed metadata. It deletes
created artwork only when the recorded pre-add count was zero, one live artwork
remains, and its bytes match the staged SHA-256 digest.

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

This command runs the ERT package suite. Emacs 29.1 or later is required.

## Privacy

Snapshots and live recovery artifacts can contain personal library data. The repository ignores `snapshots/`, virtual environments, dependency directories, and build output.

## Credit

This project used [epheterson/applemusic-mcp](https://github.com/epheterson/applemusic-mcp) as a reference for Apple Music scripting and automation behavior.

## Import the library into Strawberry

`apple_music_strawberry.py import-library` matches snapshot locations to Strawberry collection rows and copies Apple ratings. Strawberry indexes the existing Google Drive files in place. It stores paths and metadata in its database without moving or changing audio files.

Preview the import first:

```bash
uv run python apple_music_strawberry.py import-library \
  --snapshot snapshots/apple-music-20260902T011332.159242-0400.sqlite3 \
  --snapshot-sha256 045f6586c4a45712b9fc45156cae1dd9c287502f5b3653782e95b29068a5f26c \
  --strawberry-db '/Users/fuyu0425/Library/Application Support/Strawberry/Strawberry/strawberry.db' \
  --strawberry-settings '/Users/fuyu0425/Library/Preferences/org.strawberrymusicplayer.Strawberry.plist' \
  --collection-root '/Users/fuyu0425/GoogleDrive/music/iTunes Media/Music' \
  --strawberry-app /Applications/strawberry.app \
  --output snapshots/strawberry-library-import-preview
```

Apply the reviewed import to a new audit directory:

```bash
uv run python apple_music_strawberry.py import-library \
  --snapshot snapshots/apple-music-20260902T011332.159242-0400.sqlite3 \
  --snapshot-sha256 045f6586c4a45712b9fc45156cae1dd9c287502f5b3653782e95b29068a5f26c \
  --strawberry-db '/Users/fuyu0425/Library/Application Support/Strawberry/Strawberry/strawberry.db' \
  --strawberry-settings '/Users/fuyu0425/Library/Preferences/org.strawberrymusicplayer.Strawberry.plist' \
  --collection-root '/Users/fuyu0425/GoogleDrive/music/iTunes Media/Music' \
  --strawberry-app /Applications/strawberry.app \
  --output snapshots/strawberry-library-import-apply \
  --apply
```

The import omits the four snapshot rows without locations. It maps Apple ratings from 0 through 100 to Strawberry ratings from 0 through 1. Strawberry can store an unrated value as 0 or -1.

Apply refuses unsafe Strawberry settings. Keep **Save ratings to song tags when possible** and **Overwrite database rating when songs are re-read from disk** unchecked.
