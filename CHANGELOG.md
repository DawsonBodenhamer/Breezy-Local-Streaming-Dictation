# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] - Unreleased

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
