# Strawberry Playlist Import Research

**Date:** 2026-09-03  
**Strawberry Version:** 1.2.28  
**Strawberry Commit:** `3d1288d8d5ecfb077105e3741537eed454fd5b76`  
**Snapshot Schema:** 3  
**Research Focus:** Apple Music snapshot conversion and safe playlist import into Strawberry

## Summary

Strawberry can import one playlist file through **Playlist → Load playlist…**. It cannot map multiple playlist files to separate destination playlists in one native action.

XSPF 1 is the best interchange format. It preserves track order, carries a playlist title, uses legal URI identifiers, and represents local files as explicit file URIs.

Apple Music smart playlists cannot become true Strawberry smart playlists through either supported interface. An opt-in export can preserve their current members only as regular static playlists.

The first implementation should create a previewable bundle. A separate apply action should submit one XSPF file per Strawberry command invocation.

Schema version 3 blocks exact conversion. It stores unique track-playlist pairs without positions, so it loses duplicate entries and explicit source order.

---

## Research Scope and Evidence Rules

This note separates source findings from implementation recommendations. Source findings describe the checked Strawberry commit, the XSPF 1 specification, and the current Apple Music exporter documentation.

Strawberry source paths below use this checkout:

`/Users/fuyu0425/strawberry-test/strawberry`

The runtime checks use this app bundle:

`/Users/fuyu0425/strawberry-test/strawberry/build-home/strawberry.app`

The inspected checkout was at commit `3d1288d8d5ecfb077105e3741537eed454fd5b76`.

---

## Strawberry Import Contract

### Supported playlist formats

**Source finding.** `PlaylistParser` registers XSPF, M3U, PLS, ASX, ASX-INI, CUE, and WPL parsers. XSPF is registered first and becomes the default parser. See `src/playlistparsers/playlistparser.cpp`, `PlaylistParser::PlaylistParser` and `PlaylistParser::AddParser`.

The resulting load extensions are `.xspf`, `.m3u`, `.m3u8`, `.pls`, `.asx`, `.asxini`, `.wpl`, and `.cue`. All parsers except CUE support save. Each parser declares these capabilities through `file_extensions()`, `load_supported()`, and `save_supported()`. See `src/playlistparsers/*parser.h` and `src/playlistparsers/parserbase.h`, `ParserBase`.

### Does the native load action accept multiple playlist files?

**No.** **Playlist → Load playlist…** accepts one file.

**Source finding.** `PlaylistContainer::LoadPlaylist` calls `QFileDialog::getOpenFileName`, not `getOpenFileNames`, and sends the returned filename to `PlaylistManager::Load`. See `src/playlist/playlistcontainer.cpp`, `PlaylistContainer::LoadPlaylist`.

### What does one load create?

**Source finding.** `PlaylistManager::Load(const QString &filename)` always creates a playlist with an empty `special_type`. This makes a regular playlist. Its initial name is the file basename. See `src/playlist/playlistmanager.cpp`, `PlaylistManager::Load`.

`PlaylistBackend::CreatePlaylist` inserts a new row. It has no playlist identity check or name-collision check. Loading the same file again creates another playlist. See `src/playlist/playlistbackend.cpp`, `PlaylistBackend::CreatePlaylist`.

### How does XSPF set the final playlist name?

**Source finding.** `XSPFParser::Load` reads `/playlist/title` and returns it as `LoadResult::playlist_name`. `SongLoader` retains that name. `SongLoaderInserter` passes it to `Playlist::InsertSongsOrCollectionItems`, which emits `Rename` when the name is not empty. See:

- `src/playlistparsers/xspfparser.cpp`, `XSPFParser::Load`
- `src/playlistparsers/parserbase.h`, `ParserBase::LoadResult`
- `src/core/songloader.cpp`, playlist result handling
- `src/playlist/songloaderinserter.cpp`, `SongLoaderInserter::InsertSongs`
- `src/playlist/playlist.cpp`, `Playlist::InsertSongsOrCollectionItems`

Other supported playlist parsers return no embedded playlist name. Their imported playlist therefore keeps the file basename initially.

### What happens to an unusable local location?

**Source finding.** Each parser routes entries through `ParserBase::LoadSong`. Strawberry first checks its collection and then the filesystem. An empty, missing, unreadable, or invalid local location produces an invalid song and an error. The parser excludes invalid songs from its result. See `src/playlistparsers/parserbase.cpp`, `ParserBase::LoadSong`, and `src/playlistparsers/xspfparser.cpp`, `XSPFParser::ParseTrack` and `XSPFParser::Load`.

`SongLoaderInserter` forwards loader errors even when the remaining playlist parsed successfully. See `src/playlist/songloaderinserter.cpp`, `SongLoaderInserter::Load`.

---

## Recommended Interchange Format: XSPF 1

### Source findings

Strawberry registers XSPF first, which makes it the default parser. Strawberry supports both XSPF load and save. See `src/playlistparsers/playlistparser.cpp`, `PlaylistParser::PlaylistParser`, and `src/playlistparsers/xspfparser.h`.

The XSPF 1 specification defines a playlist as an ordered sequence. Its `trackList` contains ordered `track` elements. The format uses XML, and its example declares UTF-8. See the official [XSPF 1 specification](https://xspf.org/spec), sections 1.1, 3.1, and 4.1.1.2.14.

XSPF defines a playlist `title`, a playlist `date`, and legal URI `identifier` values. It also defines track `location`, `identifier`, `title`, `creator`, `album`, and integer-millisecond `duration`. See the [XSPF 1 element definitions](https://xspf.org/spec#4-element-definitions).

Strawberry reads the XSPF playlist title and the listed track metadata fields. It percent-decodes `location` and treats `duration` as milliseconds. See `src/playlistparsers/xspfparser.cpp`, `XSPFParser::Load` and `XSPFParser::ParseTrack`.

Strawberry 1.2.25 and later recursively expands local `.m3u` and `.m3u8` references. It appends every nested file's tracks to one `SongList`. This process flattens playlist boundaries and cannot preserve separate playlist identities. See `src/playlistparsers/m3uparser.cpp`, `M3UParser::ParsePlaylistData` and `M3UParser::LoadNested`.

### Recommendation

Create one XSPF file for each Apple Music playlist. Put the exact Apple Music playlist name in `/playlist/title`.

Use this filename form:

```text
<sanitized-name>--<persistent_id>.xspf
```

Replace these characters with `_`:

```text
< > : " / \ | ? *
```

Also replace control characters. Replace an empty sanitized basename with `_`. Preserve all other Unicode characters.

The persistent ID prevents filename collisions after sanitization. The XSPF title remains the source of the user-visible name.

---

## Exact Future Conversion Mapping

This section is an implementation recommendation. It follows the XSPF 1 elements defined by the official [XSPF 1 specification](https://xspf.org/spec#4-element-definitions).

| Snapshot value | XSPF destination | Rule |
|---|---|---|
| `playlists.name` | `/playlist/title` | Preserve the exact string. |
| `playlists.persistent_id` | `/playlist/identifier` | Use `https://github.com/fuyu0425/apple-music-export/ids/playlists/<persistent_id>`. |
| `metadata.exported_at` | `/playlist/date` | Write the stored XML Schema date-time value. |
| Each stored playlist entry | `/playlist/trackList/track` | Emit one track in source order. |
| `tracks.location` | `track/location` | Write a percent-encoded absolute `file:` URI. |
| `tracks.persistent_id` | `track/identifier` | Use `https://github.com/fuyu0425/apple-music-export/ids/tracks/<persistent_id>`. |
| `tracks.name` | `track/title` | Preserve the stored value. |
| `tracks.artist` | `track/creator` | Preserve the stored value. |
| `tracks.album` | `track/album` | Preserve the stored value. |
| `tracks.duration` | `track/duration` | Round `duration * 1000` to integer milliseconds. |

Omit a track entry from the XSPF file when its location is absent. Also omit it when the referenced file does not exist at export time.

Record every omission in the bundle manifest. Use `missing_location` when the snapshot lacks a location. Use `missing_file` when the path does not exist.

The current snapshot contains 2,505 tracks, 49 playlists, and 9,817 memberships. It contains 30 regular playlists and 19 smart playlists. Four tracks lack locations and affect 17 memberships. All 2,501 stored file paths currently exist.

## Song Ratings and Favorites

### Does playlist import copy Apple Music ratings or favorites?

**No.** The XSPF bundle imports track references and selected descriptive metadata. It does not import Apple Music ratings or favorites.

**Source finding.** **Load playlist…** creates playlist items. It does not add the referenced files to Strawberry's collection. Collection membership still depends on Strawberry's configured library directories and scanner.

**Source finding.** Apple Music exposes `rating` as an integer from 0 through 100. Its common star values are 0, 20, 40, 60, 80, and 100. It exposes `favorited` as a separate Boolean property. See `docs/apple-music-applescript.md`, **Ratings and preferences**.

Snapshot schema version 3 stores both values independently as `tracks.rating` and `tracks.favorited`. See `apple_music_export.py`, `JXA_SCRIPT` and `SCHEMA`.

The XSPF 1 specification defines no standard rating or favorite element. Strawberry's `XSPFParser::ParseTrack` consumes `location`, `title`, `creator`, `album`, `image`, `duration`, and `trackNum`. It does not consume a rating or favorite value. See `src/playlistparsers/xspfparser.cpp`, `XSPFParser::ParseTrack`.

When a location matches Strawberry's collection, `ParserBase::LoadSong` returns the existing collection song. The imported playlist therefore shows Strawberry's existing rating. Playlist import does not update that rating. See `src/playlistparsers/parserbase.cpp`, `ParserBase::LoadSong`.

For a file outside the collection, Strawberry can read a rating from a supported file tag. This behavior does not transfer the snapshot value unless another tool first writes that value to the file. See `src/playlistparsers/parserbase.cpp`, `ParserBase::LoadSong`, and `src/tagreader/tagreadertaglib.cpp`, rating tag readers.

### Exact rating conversion for a future metadata bridge

Strawberry stores a song rating as a float from 0.0 through 1.0. Its rating widget uses that range, and its collection update writes the float to the `rating` column. See `src/widgets/ratingwidget.cpp`, `RatingWidget`, and `src/collection/collectionbackend.cpp`, `CollectionBackend::UpdateSongsRating`.

Use this exact conversion:

```text
strawberry_rating = apple_music_rating / 100
```

Thus, Apple Music 0, 20, 40, 60, 80, and 100 become Strawberry 0.0, 0.2, 0.4, 0.6, 0.8, and 1.0.

Do not convert `favorited` into a rating. Apple Music stores favorite and rating independently, while Strawberry has no separate track-favorite field in `Song`. Mapping a favorite to five stars would overwrite distinct user data.

A future supported metadata bridge should retain `rating` and `favorited` separately by Apple Music persistent ID. It may apply `rating / 100` to a matched Strawberry collection row. It must keep `favorited` as source provenance until Strawberry adds a separate supported field.

The current snapshot has 2,149 unrated tracks and 356 rated tracks. It has 947 favorite tracks. Of those favorites, 644 have rating 0 and 303 have a positive rating. These values confirm that favorite cannot safely serve as a rating alias.

Direct Strawberry database writes remain unsupported. A future bridge needs a supported Strawberry metadata API or a controlled Strawberry change before it applies ratings.

---


## Schema Version 3 Order Blocker

### Source finding

The Music scripting query returns each playlist's `track_ids` as an ordered array. See `apple_music_export.py`, `JXA_SCRIPT`.

Schema version 3 stores memberships in `track_playlists` with this key:

```sql
PRIMARY KEY (track_persistent_id, playlist_persistent_id)
```

It has no position column. `write_snapshot` also uses `INSERT OR IGNORE`. See `apple_music_export.py`, `SCHEMA` and `write_snapshot`.

This representation removes repeated track IDs from one playlist. It also provides no explicit order contract.

The current snapshot has contiguous `track_playlists.rowid` values for each playlist. `ORDER BY track_playlists.rowid` can recover insertion order only as a **best-effort one-time path** for this snapshot.

It cannot recover duplicate entries that schema version 3 already removed. This note does not claim exact order or duplicate preservation for schema version 3 snapshots.

### Required schema change before production conversion

Store a zero-based `position` in `track_playlists`. Use this key:

```sql
PRIMARY KEY (playlist_persistent_id, position)
```

Keep `track_persistent_id` as a non-unique foreign key. This permits one playlist to contain the same track more than once.

Raise the snapshot schema version. Update every schema-version consumer in the same change.

---

## Bulk Import

### Can Strawberry import many files as separate playlists in one native action?

**No.** Strawberry has no native multi-playlist load action.

**Source finding.** The load action accepts one filename. The separate **Add file…** action calls `QFileDialog::getOpenFileNames`, but it places all selected URLs into one `MimeData` destination. See `src/playlist/playlistcontainer.cpp`, `PlaylistContainer::LoadPlaylist`, and `src/core/mainwindow.cpp`, `MainWindow::AddFile` and `MainWindow::ApplyAddBehaviour`.

The command line supports one `--create <name>` option and a list of URL arguments. One invocation therefore creates one named destination playlist. See `src/core/commandlineoptions.cpp`, `CommandlineOptions::PrintHelp` and option parsing, plus `src/core/mainwindow.cpp`, `MainWindow::CommandlineOptionsReceived`.

When Strawberry already runs, the new process sends serialized command-line options to the existing process. The sender can report only whether it attempted or failed message delivery. It cannot observe the asynchronous parser completion inside the receiving process. See `src/core/main.cpp` and `src/core/application.cpp`, command-line message delivery and receipt.

### Recommendation

A bridge can invoke this shape once per XSPF file:

```text
strawberry --create <name> <file>
```

Report each accepted command launch as `submitted`, not `imported`. Report process launch or delivery errors separately.

Repeated submission creates duplicate playlists because Strawberry performs no identity or name-collision check. Preview the complete list by default. Require an explicit `--apply` flag before submission.

Do not write Strawberry's private SQLite database or QSettings files. Their schemas are internal. Direct writes can race the running application and bypass application invariants.

## Strawberry CLI Contract

### Available playlist options

The checked Strawberry binary prints this command shape:

```text
Usage: strawberry [options] [URL(s)]
```

Its playlist options include:

| Option | Strawberry help text | Import meaning |
|---|---|---|
| `-c, --create <name>` | `Create a new playlist with files` | Create one named playlist for all URL arguments in this invocation. |
| `-a, --append` | `Append files/URLs to the playlist` | Add every URL argument to the current destination playlist. |
| `-l, --load` | `Loads files/URLs, replacing current playlist` | Replace the current playlist contents. |
| `-i, --play-playlist <name>` | `Play given playlist` | Select playback by name. It does not import a playlist. |

**Source finding.** `CommandlineOptions` stores only one `playlist_name_` value. `MainWindow::CommandlineOptionsReceived` puts that value in `MimeData::name_for_new_playlist_` and applies `OpenInNew` for `--create`. All URL arguments go to that one operation. See `src/core/commandlineoptions.cpp`, option parsing, and `src/core/mainwindow.cpp`, `MainWindow::CommandlineOptionsReceived`.

### Command for this build

Use the bundle executable, not an installed Strawberry application:

```bash
DYLD_LIBRARY_PATH=/Users/fuyu0425/strawberry-test/opt/strawberry_macos_arm64_release/lib:/Users/fuyu0425/strawberry-test/strawberry/build-home/strawberry.app/Contents/Frameworks \
  /Users/fuyu0425/strawberry-test/strawberry/build-home/strawberry.app/Contents/MacOS/strawberry \
  --create "Playlist name" "/absolute/path/to/playlist.xspf"
```

This undeployed binary needs the shown `DYLD_LIBRARY_PATH`. Without it, the process fails before it can send the command.

The import bridge must require Strawberry to be open before `--apply`. A new Strawberry process can otherwise become the primary application instance instead of acting only as a command sender.

### Result semantics

A successful sender exit does not prove that Strawberry finished parsing the XSPF file. The receiving application handles playlist loading asynchronously.

Record a successful command attempt as `submitted`. Report process start or delivery errors separately. Do not report `imported` without a future completion API.

One command can contain multiple file arguments, but `--create` still provides only one destination name. Therefore, submit one command per XSPF file to preserve playlist boundaries.

---

## Smart Playlists

### Can Apple Music smart playlists become true Strawberry smart playlists?

**No.** Neither supported interface exposes enough information for a true conversion.

### Source findings

Music scripting exposes the `smart` flag and each smart playlist's current resolved members. It does not expose the smart criteria tree. See `docs/apple-music-applescript.md`, **Smart playlist limits**, which records the installed Music scripting dictionary and runtime probe.

Strawberry stores smart playlist definitions in QSettings under `SerializedSmartPlaylists`. It stores each generator's serialized `QByteArray`. `PlaylistQueryGenerator::Save` writes the search and dynamic flag through `QDataStream`. See:

- `src/smartplaylists/smartplaylistsmodel.cpp`, `SmartPlaylistsModel::kSettingsGroup`, `AddGenerator`, and `SaveGenerator`
- `src/smartplaylists/playlistquerygenerator.cpp`, `PlaylistQueryGenerator::Save`

Strawberry's external playlist parsers return song lists and an optional playlist name. They do not accept a smart-rule model. See `src/playlistparsers/parserbase.h`, `ParserBase::LoadResult`.

### Recommendation

Skip smart playlists by default. Add this opt-in export flag:

```text
--include-smart-as-static
```

When enabled, export each current member list as a regular XSPF playlist. Name it:

```text
<Apple Music name> [Smart snapshot]
```

Set its manifest `kind` to `smart_snapshot`.

Keep smart playlists source-owned. A later two-way sync must not interpret edits to a static Strawberry copy as Apple Music rule changes.

---

## Recommended First Implementation

`apple_music_strawberry.py` now provides only the `import-library` command for local-library ratings. It does not provide the researched playlist `export` or `import --bundle` commands below.

The following section defines a later playlist implementation.

### Export command

```bash
uv run python apple_music_strawberry.py export \
  --snapshot SNAPSHOT \
  --output DIRECTORY \
  [--include-smart-as-static]
```

This command only creates a bundle. It does not start Strawberry or import a playlist.

### Import command

```bash
uv run python apple_music_strawberry.py import \
  --bundle DIRECTORY \
  --strawberry-app /Users/fuyu0425/strawberry-test/strawberry/build-home/strawberry.app \
  [--apply]
```

The command previews by default. With `--apply`, it requires this Strawberry bundle to be open. It submits one XSPF file at a time.

For this undeployed build, set:

```bash
DYLD_LIBRARY_PATH=/Users/fuyu0425/strawberry-test/opt/strawberry_macos_arm64_release/lib:/Users/fuyu0425/strawberry-test/strawberry/build-home/strawberry.app/Contents/Frameworks
```

Direct executable invocation without that value fails before command delivery.

### Bundle layout

```text
DIRECTORY/
├── manifest.json
└── playlists/
    └── <sanitized-name>--<persistent_id>.xspf
```

The manifest starts with top-level `format_version: 1`.

Its `source` block contains:

- `snapshot`
- `sha256`
- `schema_version`
- `exported_at`

Each manifest playlist entry contains:

- `persistent_id`
- `name`
- `kind`
- `file`
- `source_entry_count`
- `exported_entry_count`
- `omissions`

Each omission identifies the affected source entry and gives `missing_location` or `missing_file`.

### Command summary

The export summary separates:

- Regular playlists
- Static smart snapshots
- Exported entries
- Omitted entries

The import summary separates:

- Submission attempts
- Command-launch failures

Every summary prints the bundle path and the exact next command. The importer never labels a submitted playlist as imported because it cannot observe asynchronous parser completion.

---

## Seam for Later Two-Way Sync

XSPF identifiers and the manifest retain Apple Music playlist and track identities outside Strawberry. The XSPF specification permits these legal URI identifiers.

Strawberry discards XSPF `identifier` elements during import. `XSPFParser::ParseTrack` does not read track identifiers, and `XSPFParser::Load` does not retain the playlist identifier. See `src/playlistparsers/xspfparser.cpp`.

A controlled importer must later record each Apple Music playlist ID against the created Strawberry playlist rowid. The alternative is a Strawberry external-ID API.

A later sync must read Strawberry state through that supported bridge. It must write only regular Apple Music playlists through Music scripting.

This seam preserves identity and provenance only. Conflict detection, merge policy, and automatic bidirectional synchronization remain outside this research.

---

## Verification Record

On 2026-09-03, a read-only query of `snapshots/apple-music-20260902T011332.159242-0400.sqlite3` returned:

```text
2505|49|9817
0|30
1|19
4|17
```

These values confirm the track, playlist, membership, regular playlist, smart playlist, missing-location track, and affected-membership counts.

The local Strawberry help command used the required `DYLD_LIBRARY_PATH`. It printed exactly one `--create <name>` option, accepted URL arguments, exposed no bulk playlist-name mapping, and exited with status 1 as expected.

## Primary Sources

1. Strawberry source at commit `3d1288d8d5ecfb077105e3741537eed454fd5b76`, under `/Users/fuyu0425/strawberry-test/strawberry/src`.
2. [XSPF Version 1 specification](https://xspf.org/spec).
3. `apple_music_export.py`, especially `JXA_SCRIPT`, `SCHEMA`, and `write_snapshot`.
4. `docs/apple-music-applescript.md`, especially **Smart playlist limits**.
