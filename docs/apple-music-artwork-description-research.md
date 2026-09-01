# Apple Music Artwork Description Property: Scripting Semantics

**Date:** 2026-09-01  
**macOS:** 15.7.4 (24G517)  
**Music App Version:** 1.5.6  
**Research Focus:** Whether Music can persist artwork `description` via AppleScript/JXA

## Summary

The installed Music.sdef declares the `artwork` class with a writable `description` property (code `pDes`, text type). However, Music 1.5.6 silently fails to persist artwork descriptions despite accepting the assignment without error. Reading the property after assignment returns `missing value`. The feature is not supported by the current runtime, despite appearing available in the scripting dictionary.

---

## Scripting Dictionary: Primary Evidence

### Source
`/usr/bin/sdef /System/Applications/Music.app` extracted from Music 1.5.6 bundle.

### Artwork Class Declaration

The `artwork` class supports the following properties:

| Property | Type | Access | Code | Notes |
|----------|------|--------|------|-------|
| `data` | picture | writable | `pPCT` | Image data |
| `description` | text | writable | `pDes` | No `r/o` marker |
| `downloaded` | boolean | read-only | `pDlA` | Marked read-only |
| `format` | type | read-only | `pFmt` | Marked read-only |
| `kind` | integer | writable | `pKnd` | Artwork purpose |
| `raw data` | any | writable | `pRaw` | Original image data |

**Key observation:** The sdef declares `description` as **writable** with no `r/o` marker. Per Apple's dictionary guide ([Navigate a Scripting Dictionary](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/NavigateaScriptingDictionary.html)), read-only properties carry an explicit `r/o` indicator. The absence of this marker on `description` indicates the dictionary authors intended it to be writable.

---

## Live Probe Results: Secondary Evidence

Two independent probes tested artwork creation and description persistence on Music 1.5.6.

### Probe 1: Track 7D3CD6F75ACA0B7D (Aqours)

**File:** `snapshots/live-artwork-probe/result.json`

- **Initial state:** 0 artworks
- **Attempt 1 — `make new artwork`:** Rejected with error `-10014` ("handler only handles single objects")
- **Attempt 2 — `set data of artwork 1`:** Succeeded (`status: "added"`, artwork count: 1)
- **Description assignment:** `set description of artwork 1 to "Cover Art Archive release 4f1ff5c0-1a3b-467e-b88b-280fa4647e6a"` — no AppleScript error
- **Description retrieval:** `missing value` (not persisted)
- **Metadata integrity:** Title, artist, and album unchanged
- **Conclusion:** Artwork data writes work; description writes fail silently

### Probe 2: Track 1648DAD9AD0A1C95 (手嶌葵)

**File:** `snapshots/live-artwork-probe-2/result.json`

- **Initial state:** 0 artworks
- **Attempt — `set data of artwork 1`:** Succeeded (`assignment_output: "added"`, artwork count: 1)
- **Description assignment:** The same probe script assigned the expected release marker without an AppleScript error
- **Description retrieval:** `verification.description: null`
- **Metadata integrity:** Artist and album unchanged
- **Conclusion:** Consistent with Probe 1—artwork data persists, but the assigned description does not

**AppleScript command tested:**
```applescript
set description of artwork 1 of targetTrack to "Cover Art Archive release 4f1ff5c0-1a3b-467e-b88b-280fa4647e6a"
```
Result: No runtime error; value silently discarded.

---

## Syntax Reference: Writable and Read-Only Properties

### Correct AppleScript for Writable Properties
```applescript
tell application "Music"
    set targetTrack to item 1 of (get file tracks)
    
    -- Create/assign artwork data
    set imageData to read POSIX file imagePath as picture
    set data of artwork 1 of targetTrack to imageData
    
    -- Attempt to assign description (fails silently in 1.5.6)
    set description of artwork 1 of targetTrack to "metadata string"
end tell
```

### Read-Only Properties (Cannot Assign)
```applescript
-- These will error if you attempt assignment:
-- set downloaded of artwork 1 of targetTrack to true        -- ERROR
-- set format of artwork 1 of targetTrack to JPEG picture   -- ERROR
```

### Access Modes Observed in Runtime

- **`data` property:** Mutation via `set data of artwork 1` created the artwork in both probes.
- **`raw data` property:** JXA read the created artwork bytes, which matched the staged JPEG.
- **`description` property:** Reads returned `missing value` after assignment completed without error.

---

## Root Cause

Apple's published documentation does not explain this mismatch. The installed dictionary advertises a writable property, but Music 1.5.6 discards the assigned value.

The most likely cause is an implementation gap in Music's scripting runtime. This is an inference from the dictionary and repeatable probe results.

The first probe's `make new artwork` error also shows that Music does not fully implement the generic creation path for this element.

---

## Secondary Evidence: Expert Scripts

**Source:** [Doug's AppleScripts: Managing Artwork](https://dougscripts.com/itunes/scripts/scripts13.php)

Current artwork scripts import and export image data. The available documentation does not show a working use of artwork `description`.

---

## Conclusion

**Apple Music 1.5.6 cannot persist artwork `description` via AppleScript.**

The scripting dictionary declares `description` as a writable text property on the `artwork` class, and AppleScript permits assignment syntax without error. However, Music silently discards the value: subsequent reads return `missing value` (AppleScript's representation of an unset property). This behavior is consistent across two independent test tracks and holds even when the assignment command contains no syntax errors and completes without exception.

The feature is **not supported by the current runtime**, despite appearing available in the dictionary. No direct database or media-tag write is available as a workaround within the scripting interface.

---

## Recommendations

### For the Artwork Feature Implementation

1. **Do not use artwork `description` as a source marker.**  
   Music 1.5.6 does not persist it. Store the release ID and image digest in the external result record.

2. **Retest only after a Music update documents or fixes artwork scripting.**  
   Verify the write with an immediate read before production use.

3. **Use the supported image operation only.**  
   `set data of artwork 1 of track to imageData` creates the image. The current plan must not automate deletion without its required marker.

### For Future Investigation

- Test a later Music version only after Apple ships one.
- Keep provenance in the plan and result files.
- Do not use a track field as a hidden marker unless the user approves that metadata change.

---

## References

1. Apple Developer. *Navigate a Scripting Dictionary*.  
   https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/NavigateaScriptingDictionary.html

2. Apple Developer. *Mac Automation Scripting Guide: About Scripting Terminology* (June 13, 2016).  
   https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AboutScriptingTerminology.html

3. Music app scripting dictionary. Extracted via `/usr/bin/sdef /System/Applications/Music.app` on macOS 15.7.4.

4. Live probe recordings:
   - `snapshots/live-artwork-probe/result.json`
   - `snapshots/live-artwork-probe-2/result.json`

5. Doug's AppleScripts. *Managing Artwork*.  
   https://dougscripts.com/itunes/scripts/scripts13.php
