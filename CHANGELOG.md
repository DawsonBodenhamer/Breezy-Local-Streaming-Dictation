# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.3] - Unreleased

### Added
- **Breezy Dictation product identity**
  - Renamed the current app, command, runtime folder, tray labels, and distributable archive to Breezy Dictation while keeping the existing GitHub repository address available for current releases.
  - Upgrades move configuration, corrections, model content, logs, and rollback data from the previous runtime folder into the new authority and leave a recoverable migration marker.
- **Optional automatic punctuation**
  - Added a tray choice that lets each user keep inferred sentence punctuation on or off while preserving punctuation they dictate explicitly and punctuation inside numbers, versions, and contractions.
- **Spoken number formatting**
  - Breezy now rechecks ambiguous integer-only speech to distinguish times such as “five thirty seven” from cardinal numbers such as “five hundred thirty seven,” converts values of 10 or greater to digits, and joins large numbers spoken across short pauses.
- **Explicit digit commands**
  - Say `digit zero` through `digit nine` to type one literal numeral without having it interpreted as a time or larger number, including fluent consecutive commands and commands directly after dictated underscores or other separators.
- **Paragraph and line capitalization controls**
  - Added independent checked tray choices for capitalizing text at empty documents and blank-line paragraphs or after one line break, including spoken `new paragraph` and `new line` commands across streamed phrases.
  - Both choices default on, preserve exact correction output, and leave sentence-after-period capitalization unchanged.
- **Temporary all-caps toggle**
  - Say `caps lock` to turn temporary all caps on, repeat it within two seconds to turn it off, or let it expire automatically after two seconds of silence. Existing `all caps on` and `all caps off` commands remain available.
- **Grouped dictation corrections**
  - One correction can now contain several phrases Breezy may hear and one exact result to type. Suggested groups show every phrase on its own row, including phrases that contain punctuation.
- **Clearer startup and troubleshooting information**
  - The tray now tells you when dictation is still loading or could not start, instead of accepting the shortcut without explaining why nothing happened.
  - Breezy now records more useful troubleshooting logging when it cannot hear you or type into the selected text box. These details never include your audio or dictated words.

### Fixed
- **Startup migration**
  - Preserved an existing automatic-at-logon choice when moving from the legacy runtime folder to the Breezy Dictation runtime, while restoring the previous startup task if the migration cannot complete.
- **Reliable logon startup**
  - Registers automatic startup directly against the Breezy supervisor, so a missing windowless-script wrapper cannot produce a Windows Script Host error or prevent startup.
- **Deterministic phrase spacing**
  - Leaves the caret immediately after dictated content without appending a completion space at the end of a document or before existing same-line or later-line text.
  - Inserts exactly one separator between ordinary phrases and reliably resets IntelliJ Markdown spacing after physical keyboard or mouse input without treating injected dictation input as physical context.
  - Restores Photoshop Text-tool and inline layer-name dictation; selected layer names consume their initial selection once, and pauses between streamed layer-name phrases produce one space.
  - Keeps practical boundaries coherent: `new line second item` and `new paragraph next thought` preserve their multiline layout, `five thirty seven PM` becomes `5:37 PM`, `3,542,308` remains one number, and `caps lock this is temporary` produces `THIS IS TEMPORARY` until the mode expires.
- **Streaming input boundaries**
  - Kept longer spoken number sequences from being partially rewritten as clock times.
  - Correctly distinguished “testing one twenty-three” as `testing 1:23`, “testing one hundred twenty-three” as `testing 123`, and “testing one two three” as unchanged spoken words when speech recognition initially collapses their numeric forms.
  - Kept large-number fragments together across short pauses, committed a completed value after a brief quiet grace, and deferred that commit when speech resumes, preventing delayed fragments such as “five hundred thirty-seven thousand” from leaking into a later phrase without adding seconds of latency.
  - Prevented Breezy from adding a remembered leading space after you press Enter or Shift+Enter between streamed phrases in the same window, and capitalized the next phrase consistently even when the editor reports stale caret context.
- **More reliable dictation**
  - Restored first-attempt insertion after pressing Enter or Shift+Enter instead of dropping the next phrase while resolving its manual line boundary.
  - Removed starter-example text that could occasionally appear in your dictation by mistake.
  - Stopped Breezy from repeatedly typing the same phrases during a single dictation session while still allowing intentional repetition.
- **Spoken punctuation commands**
  - Commands such as `question mark` now work even when speech recognition places an unexpected pause or punctuation mark between the words, and the observed `backtic` spelling now closes a code span correctly.

---

## [1.0.2] - 2026-08-26

### Changed
- **Lighter automatic comma style**
  - Updated the default dictation prompt to reduce unnecessary commas during fast, continuous speech while preserving appropriate commas in expressive speech. I noticed the model was inserting commas all over the place, and that’s not really my style. But if you’re somebody who likes a lot of commas, you can always dictate them manually by saying “comma.”

---

## [1.0.1] - 2026-08-25

### Added
- Automatically reset active capitalization modes (`all caps on`, `caps on`, `cap`) after 2 seconds of silence.

### Fixed
- Insert dictated text into modern Windows Notepad without delayed or corrupted Unicode characters.
- Close smart quotes correctly when `close quote` is split across streamed phrases.

---

## [1.0.0] - 2026-08-25

### Added
- Initial release

---
