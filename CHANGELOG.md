# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.3] - Unreleased

### Added
- **Clearer startup and troubleshooting information**
  - The tray now tells you when dictation is still loading or could not start, instead of accepting the shortcut without explaining why nothing happened.
  - Breezy now records more useful troubleshooting logging when it cannot hear you or type into the selected text box. These details never include your audio or dictated words.

### Fixed
- **More reliable dictation**
  - Removed starter-example text that could occasionally appear in your dictation by mistake.
  - Stopped Breezy from repeatedly typing the same phrases during a single dictation session while still allowing intentional repetition.
- **Spoken punctuation commands**
  - Commands such as `question mark` now work even when speech recognition places an unexpected pause or punctuation mark between the words.

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
