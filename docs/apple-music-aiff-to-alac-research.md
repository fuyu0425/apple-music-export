# Apple Music AIFF to ALAC migration research

## Inspected inventory

The inspected baseline is `snapshots/apple-music-20260902T011332.159242-0400.sqlite3` (schema 3). It references 50 distinct, readable regular AIFF files in 11 source directories. The files total 1,914,818,964 bytes. The inventory had no missing files, symbolic links, duplicate paths, or sibling M4A destinations.

The migration must use a fresh export because the inspected snapshot was two days old when this plan was approved.

## XLD evidence

XLD's [official page](https://tmkk.undo.jp/xld/index_e.html) documents its lossless audio conversion role. The installed release is `20250302`. The release-pinned source is [revision 651](https://sourceforge.net/p/xld/code/651/tree/trunk/).

The command-line entry points and output selection are in [`XLD/main.m`](https://sourceforge.net/p/xld/code/651/tree/trunk/XLD/main.m) and [`XLD/XLD_cmdline.m`](https://sourceforge.net/p/xld/code/651/tree/trunk/XLD/XLD_cmdline.m). ALAC settings and output behavior are in [`XLDAlacOutput/XLDAlacOutput.m`](https://sourceforge.net/p/xld/code/651/tree/trunk/XLDAlacOutput/XLDAlacOutput.m), [`XLDAlacOutput/XLDAlacOutputTask.m`](https://sourceforge.net/p/xld/code/651/tree/trunk/XLDAlacOutput/XLDAlacOutputTask.m), and [`XLDAlacOutput/English.lproj/XLDAlacOutput.xib`](https://sourceforge.net/p/xld/code/651/tree/trunk/XLDAlacOutput/English.lproj/XLDAlacOutput.xib).

The exact conversion command is:

```text
/Applications/XLD.app/Contents/MacOS/XLD --cmdline -f alac --keep-timestamp -o OUTPUT INPUT
```

XLD reads `XLDAlacOutput_Samplerate` and `XLDAlacOutput_BitDepth`. Index `0` means **Same as original** for both settings. The observed values are `0` and `0`. An absent preference file or key also selects the nib's index-`0` default. The migration rejects other values and any `Encoder option:` warning.

The CLI ALAC writer uses `kAudioFileFlags_EraseFile`. The migration therefore rejects existing destination and temporary paths before XLD can run.

The planned lossless proof decoded both files through XLD and compared streaming PCM SHA-256 digests. The installed XLD reported `PCM (little endian) output plugin not loaded`. On 2026-09-04, the user explicitly approved trusting XLD without this proof. The audit records `pcm_verification` as `skipped_by_user`. File SHA-256, conversion success, metadata, location, and snapshot checks remain active.

The first complete apply found one 4096-frame ALAC packet of duration drift for `01 Rain.aiff`. On 2026-09-04, the user approved this XLD output. Verification therefore permits at most 0.1 seconds of duration drift and records that limit in the audit.

## Music automation evidence

The local dictionary at `/System/Applications/Music.app/Contents/Resources/com.apple.Music.sdef` declares `file track.location` writable. It declares identity fields read-only. The inspected Music version is `1.5.6`, full version `1.5.6.11`.

Apple documents JXA file paths and `Path` objects in [Referencing Files and Folders](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/ReferenceFilesandFolders.html).

The dictionary does not guarantee metadata preservation after a location change. Full identity and metadata preservation is a tested postcondition. The migration does not use Music's `add`, `delete`, `convert`, or `refresh` commands.
