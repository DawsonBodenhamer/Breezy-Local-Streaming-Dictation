# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.5] - Unreleased

---

## [1.0.4] - 2026-09-03

### Fixed
- **Reliable tray restart on Windows**
  - Fixed **Restart dictation** leaving Breezy unable to start again after the existing dictation process stopped successfully.

---

## [1.0.3] - 2026-08-31

### Added
- **Optional automatic punctuation**
  - Added a tray choice that lets each user keep inferred sentence punctuation on or off while preserving punctuation they dictate explicitly and punctuation inside numbers, versions, and contractions. The choice is off by default, so nothing is added unless you say it — `hello comma world` types `Hello, world` whether the choice is on or off.
- **Spoken number formatting**
  - Breezy now converts spoken numbers of ten or greater to digits, so `twenty-two rabbits` becomes `22 rabbits` while single-digit counts stay words.
  - Ambiguous number speech is rechecked to tell times from cardinals: `five thirty seven` types `5:37` and `five hundred thirty seven` types `537`. Long numbers stay together even when you pause between the chunks, so `three million five hundred forty two thousand three hundred eight` arrives as one `3,542,308`, even if you have to take a breath right after "three million". But if you pause long enough, it will come out as two separate numbers like `3,000,000` `542,308`.
- **Explicit digit commands**
  - Say `digit zero` through `digit nine` to type one literal numeral without having it interpreted as a time or larger number, including fluent consecutive commands and commands directly after dictated underscores or other separators. For example, `digit seven` types `7`, and `digit one digit two digit three` types `1 2 3`.
- **Paragraph and line capitalization controls**
  - Added new tray menu settings for capitalizing text at empty documents and blank-line paragraphs or after one line break, including spoken `new paragraph` and `new line` commands across streamed phrases. Saying `new paragraph hello` types a blank line followed by `Hello` when the paragraph choice is on, and `new line hello` types the next line as `Hello` when the line choice is on.
  - Both choices default on.
- **Temporary all-caps toggle**
  - Say `caps lock` to turn temporary all caps on, repeat it within two seconds to turn it off, or let it expire automatically after two seconds of silence. Existing `all caps on` and `all caps off` commands remain available but are harder to pronounce so that's why I replaced them. For example, `fresh bread caps lock straight out of the oven` types `fresh bread STRAIGHT OUT OF THE OVEN`.
- **Grouped dictation corrections**
  - One correction can now contain several phrases Breezy may hear and one exact result to type instead, so a single correction can cover both `jon` and `john` and always type `John` instead.
- **Clearer startup and troubleshooting information**
  - The tray now tells you when dictation is still loading or could not start, instead of accepting the shortcut without explaining why nothing happened.

### Changed
- **Breezy Dictation name**
  - Renamed the app to Breezy Dictation. The original name was a mouthful lol

### Fixed
- **Repeated-phrase handling**
  - Stopped Breezy from repeatedly typing the same phrases during a single dictation session while still allowing intentional repetition, so saying `pork` once types it once, but saying it twice on purpose still types it twice.
- **Removed stray starter text**
  - Removed starter-example text that could occasionally appear in your dictation by mistake. I had tried inserting this text to help reduce commas, and it kind of worked but it also caused the model to frequently insert the example phrase into the output, which is a worse trade-off.
- **Reliable logon startup**
  - Registers automatic startup directly against the Breezy supervisor, so a missing windowless-script wrapper cannot produce a Windows Script Host error or prevent startup.

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
