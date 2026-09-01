# Beets for metadata repair

## Recommendation

Use beets as a read-only candidate and scoring engine. Do not let beets import, move, copy, rename, or write the Apple Music files.

The useful path is:

1. Read tracks and local paths from one exported snapshot.
2. Build transient beets `Item` objects from readable local files.
3. Fingerprint only audit candidates with Chromaprint through the beets `chroma` plugin.
4. Resolve AcoustID recording and release IDs through the beets MusicBrainz plugin.
5. Run beets singleton or album matching to get ranked `TrackMatch` or `AlbumMatch` proposals.
6. Add the proposal, distance components, MusicBrainz IDs, AcoustID score, and source URLs to a review report.
7. Apply only explicitly approved changes through the guarded Apple Music apply command.

This uses the difficult parts that beets already solves. It keeps this project in control of snapshot identity, review status, and Apple Music updates.

## What beets provides

### Acoustic identification

The `chroma` plugin uses `pyacoustid` and Chromaprint. It fingerprints a file,
queries AcoustID, rejects a top result below `0.5`, and extracts MusicBrainz
recording and release IDs. It then asks the MusicBrainz plugin for full
candidates. The plugin limits a singleton lookup to five recordings and an
album lookup to five releases. For missing-album singletons, the matcher scopes
releases to the recording that beets selected and fetches at most five.

Acoustic identity is candidate evidence, not complete release metadata. The same recording can occur on an original release, compilation, remaster, or regional release. Album-level matching must choose the release for strong results.

An empty-album singleton can use a best-effort release probe. The probe scopes
AcoustID releases to the recording that beets selected. It orders releases by
descending AcoustID score and then release ID, and fetches at most five. A
candidate must contain the selected recording and have the exact MusicBrainz
status `Official`. The matcher chooses the lowest beets distance and then
release ID. Evidence includes probed and available counts. An omission reason
describes only the bounded probe. The probe never upgrades the singleton beyond
`needs_review`.

For an exact MusicBrainz release, query the Cover Art Archive for its approved
front image. Record the source URL as review evidence. Add the image only after
the reviewer approves the related metadata entry.

Sources:

- [Beets chroma plugin source](https://github.com/beetbox/beets/blob/master/beetsplug/chroma.py)
- [Beets chroma plugin documentation](https://github.com/beetbox/beets/blob/master/docs/plugins/chroma.rst)
- [Chromaprint](https://acoustid.org/chromaprint)
- [AcoustID web service](https://acoustid.org/webservice)

### MusicBrainz candidates

The MusicBrainz plugin returns structured track and album candidates. Beets can search by text or fetch a recording or release by an existing MusicBrainz ID. This gives titles, artist credits, releases, dates, countries, labels, catalog numbers, track positions, and stable IDs.

MusicBrainz requests need a descriptive user agent. The beets MusicBrainz client also applies request throttling.

Sources:

- [Beets MusicBrainz plugin source](https://github.com/beetbox/beets/blob/master/beetsplug/musicbrainz.py)
- [Beets MusicBrainz API wrapper](https://github.com/beetbox/beets/blob/master/beetsplug/_utils/musicbrainz.py)
- [MusicBrainz API documentation](https://musicbrainz.org/doc/MusicBrainz_API)
- [MusicBrainz rate limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)

### Candidate scoring

`tag_item` and `tag_album` return a `Proposal`. Each proposal contains ranked candidates and a recommendation of `strong`, `medium`, `low`, or `none`. Beets scores title, artist, album, duration, track position, release identity, and missing or extra tracks. Album matching also assigns local files to release tracks.

The default strong distance threshold is `0.04`. A fingerprint match adds a recording-ID penalty when the MusicBrainz recording does not match the AcoustID result. Missing or unmatched tracks can cap the recommendation.

Sources:

- [Autotag matching source](https://github.com/beetbox/beets/blob/master/beets/autotag/match.py)
- [Distance calculation source](https://github.com/beetbox/beets/blob/master/beets/autotag/distance.py)
- [Matching configuration](https://github.com/beetbox/beets/blob/master/docs/reference/config.rst#autotagger-matching-options)

## Safe integration boundary

Use candidate generation only:

- `beets.autotag.tag_item(...)`
- `beets.autotag.tag_album(...)`
- `MetadataSourcePlugin.item_candidates(...)`
- `MetadataSourcePlugin.candidates(...)`
- `MusicBrainzPlugin.track_for_id(...)`
- `MusicBrainzPlugin.album_for_id(...)`

Do not call `TrackMatch.apply_metadata()` or `AlbumMatch.apply_metadata()`. These methods update beets items. Do not run a normal `beet import` against the media root. Beets defaults can copy files and write tags.

The Python plugin setup and the chroma plugin's fingerprint cache use internal beets APIs. Pin the beets version if this project adds an adapter. Keep all beets-specific code behind one small module.

Sources:

- [Metadata source plugin contract](https://github.com/beetbox/beets/blob/master/beets/metadata_plugins.py)
- [Match application methods](https://github.com/beetbox/beets/blob/master/beets/autotag/match.py)
- [Importer write, copy, and move defaults](https://github.com/beetbox/beets/blob/master/docs/reference/config.rst#importer-options)

## Confirmation policy

A beets `strong` recommendation alone must not change metadata. Confirm a repair only when all required evidence agrees:

- AcoustID identifies a MusicBrainz recording above the plugin threshold.
- The MusicBrainz recording duration matches the snapshot duration.
- The selected release contains that recording at the expected track position.
- Album-level matching has no unexplained missing or extra tracks.
- The current file is not a live, cover, remix, acoustic, instrumental, or karaoke variant of the candidate.
- The proposed artist credit and title come from the selected release.

Keep other results as `needs_review`. Store the candidate distance and each penalty. A single numeric confidence value hides why a match failed.

For native-script repairs, leave the beets `languages` preference empty during
identification. Beets documents that a language preference can select aliases
or transliterations. Evidence records the selected release status, language,
and script. It also preserves canonical MusicBrainz and credited release values.

## Minimal first implementation

Start with a read-only command for the existing audit candidates. Do not add a full beets-managed library.

The command should produce a second proposal CSV or extend the audit report with:

```text
persistent_id,acoustid_id,acoustid_score,mb_recording_id,mb_release_id,
beets_recommendation,beets_distance,distance_penalties,proposed_title,
proposed_artist,proposed_album,evidence,source_urls
```

Process album directories as albums when at least two snapshot tracks share the
same album and directory. Process the remaining tracks as singletons. After
beets selects a recording for an empty-album singleton, scope AcoustID releases
to that recording. Order releases by descending AcoustID score and then release
ID, and fetch at most five. Require the selected recording and the exact
MusicBrainz status `Official`. Choose the lowest beets distance and then release
ID. Record probed and available counts. Limit an omission reason to the bounded
probe. Cache fingerprints and API results by normalized path and file size plus
modification time.

Install beets and chroma support as an optional tool dependency. The chroma documentation requires `beets[chroma]` and Chromaprint or `fpcalc`. On macOS, Homebrew provides `chromaprint`.

The reviewed apply command uses the existing recovery flow's backup, dry-run,
live preflight, rollback, and post-export checks. It updates Music through its
scripting interface and never edits `Library.musicdb` directly. Artwork is
optional. The command adds it only when the track has no artwork and verifies
the added bytes against the staged SHA-256 digest.

## What to skip

- Skip a permanent beets library. The SQLite snapshot remains the source of truth.
- Skip `beet import` against the media root. Its defaults can copy and write files.
- Skip fingerprinting all tracks first. Start with unresolved audit candidates.
- Skip automatic acceptance based only on AcoustID score or beets recommendation.
- Skip direct file-tag writes. Apple Music can retain library metadata that differs from embedded tags.
