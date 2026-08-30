# Apple Music AppleScript Findings

This document describes the AppleScript interface in Music 1.5.6 on macOS.

The primary source is the installed scripting dictionary:

```text
/System/Applications/Music.app/Contents/Resources/com.apple.Music.sdef
```

A read-only runtime probe confirmed that the active library exposes tracks, user playlists, ratings, favorites, play counts, comments, file locations, and smart-playlist status.

No live metadata writes were used during this exploration. The writable lists below come from the scripting dictionary.

## Active library behavior

AppleScript talks to the Music application, not directly to `Library.musicdb`. Music applies commands to the library that is active in the application.

AppleScript does not expose the active `.musiclibrary` path. On the tested system, `AMPLibraryAgent` holds the active `Library.musicdb` file open. Inspecting that file descriptor can reveal the active package while the library is loaded.

This process name and behavior are private implementation details. They can change between macOS versions.

A fixed path is not reliable because users can move libraries and keep multiple library packages.

## Permissions

Library writes use the `com.apple.Music.library.read-write` access group. macOS must grant Automation permission to the process that runs the script.

A property declared writable can still reject a value. Cloud, shared, protected, and subscription tracks can impose additional restrictions.

## Track properties declared writable

The dictionary does not mark the following track properties as read-only.

### Basic metadata

- `name`
- `album`
- `album artist`
- `artist`
- `composer`
- `genre`
- `category`
- `comment`
- `description`
- `long description`
- `grouping`
- `lyrics`
- `show`
- `work`
- `movement`

### Ratings and preferences

- `rating`
- `album rating`
- `favorited`
- `disliked`
- `album favorited`
- `album disliked`
- `enabled`
- `shufflable`
- `unplayed`

The dictionary defines `rating` and `album rating` as integers from `0` through `100`. The Music interface commonly uses these star values:

| Stars | Rating |
| ---: | ---: |
| Unrated | `0` |
| 1 | `20` |
| 2 | `40` |
| 3 | `60` |
| 4 | `80` |
| 5 | `100` |

The schema permits other integer values in this range. Code must not assume that only 20-point values exist.

Example:

```applescript
tell application "Music"
    set t to first track of library playlist 1 whose persistent ID is "TRACK_ID"
    set rating of t to 80
end tell
```

### Playback and history

- `bookmark`
- `bookmarkable`
- `start`
- `finish`
- `played count`
- `played date`
- `skipped count`
- `skipped date`
- `volume adjustment`
- `gapless`

### Numbering and classification

- `bpm`
- `compilation`
- `disc count`
- `disc number`
- `episode ID`
- `episode number`
- `media kind`
- `movement count`
- `movement number`
- `season number`
- `track count`
- `track number`
- `year`

### Sorting and equalizer

- `EQ`
- `sort album`
- `sort album artist`
- `sort artist`
- `sort composer`
- `sort name`
- `sort show`

### Track subtype properties

A `file track` declares `location` writable. A `URL track` declares `address` writable.

Artwork declares these properties writable:

- `data`
- `raw data`
- `description`
- `kind`

## Playlist properties declared writable

A playlist declares these properties writable:

- `name`
- `description`
- `favorited`
- `disliked`

A user playlist also declares `shared` writable.

Create a normal playlist:

```applescript
tell application "Music"
    set p to make new user playlist with properties {name:"Export Review"}
    set description of p to "Created through AppleScript"
end tell
```

Add an existing track to a normal playlist:

```applescript
tell application "Music"
    set t to first track of library playlist 1 whose persistent ID is "TRACK_ID"
    duplicate t to user playlist "Export Review"
end tell
```

The standard suite also declares commands to delete, duplicate, and move playlists.

## Smart playlist limits

The `smart` and `genius` properties are read-only.

AppleScript can read a smart playlist and its current resolved tracks. The scripting dictionary does not expose its rule definitions.

AppleScript cannot use the declared interface to:

- Read a smart playlist criteria tree.
- Change smart playlist criteria.
- Create a smart playlist with criteria.
- Convert a normal playlist into a smart playlist.

## Important read-only track properties

The dictionary marks these properties read-only:

- `persistent ID`
- `database ID`
- `date added`
- `modification date`
- `duration`
- `size`
- `time`
- `bit rate`
- `sample rate`
- `kind`
- `cloud status`
- `release date`
- `rating kind`
- `album rating kind`
- `purchaser account`
- `purchaser name`
- `downloader account`
- `downloader name`

## Library mutation commands

The Music suite declares these library commands:

- `add`: Import files into Music or a playlist.
- `convert`: Convert files or tracks with the selected encoder.
- `download`: Download a cloud track or playlist.
- `refresh`: Reload a file track's information from its media file.

The standard suite also provides `make`, `duplicate`, `move`, and `delete` commands for supported objects.

## Runtime controls

The dictionary also declares writable playback and interface properties. These properties are not track metadata:

- Current AirPlay devices
- Current encoder
- Current EQ preset
- Current visual
- EQ enabled
- Fixed indexing
- Frontmost state
- Full-screen state
- Mute
- Player position
- Shuffle enabled
- Shuffle mode
- Song repeat
- Sound volume
- Visuals enabled

## Direct database access

Direct writes to `Library.musicdb` are not recommended. The schema, transactions, caches, and synchronization behavior are private to Apple.

Use AppleScript for supported changes. Use exported SQLite snapshots for queries and history.
