<h1 align="center">Breezy Dictation</h1>

<p align="center"><strong>Streaming dictation that puts accurate, punctuated text on screen while you speak.</strong></p>

<p align="center">
  <a href="#requirements"><img src="https://img.shields.io/badge/Windows-11-3776AB?style=for-the-badge&logo=windows11&logoColor=white" alt="Windows 11"></a>
  <a href="#privacy-and-troubleshooting"><img src="https://img.shields.io/badge/Processing-Local-2E7D32?style=for-the-badge" alt="Local Processing"></a>
  <a href="#requirements"><img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-C9A227?style=for-the-badge" alt="MIT License"></a>
</p>

---

<img src="assets/breezy_dictation_icon_hd.png" align="left" width="50%" alt="Gold microphone with streaming voice waves">

## Purpose

Breezy Dictation turns speech into editable text continuously. Focus a supported text field, press the activation hotkey, speak, and press the hotkey again when finished. Recognition runs locally, while corrections let you teach Breezy names and terminology that matter to you.

## Installation

Download the latest release from the [GitHub Releases page](https://github.com/DawsonBodenhamer/Breezy-Local-Streaming-Dictation/releases), then download the `Breezy-Dictation-v<version>.zip` asset and extract it to a local folder.

Preview setup first:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -DryRun
```

Install when ready:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Setup selects the install folder, model storage, compute mode, microphone, and optional logon startup. New installs use `%LOCALAPPDATA%\breezy_dictation`. Existing installations are migrated from `%LOCALAPPDATA%\breezy_local_streaming_dictation` with configuration, corrections, model content, logs, and rollback data preserved. The old folder becomes a versioned migration tombstone after verification.

The default activation hotkey is `Win+H`. Change it from the tray. Automatic punctuation is off for new installations; spoken punctuation remains available.

## Using Breezy

1. Choose a microphone from the tray.
2. Focus an editable text field.
3. Press the selected activation hotkey and speak naturally.
4. Press the hotkey again to finish.

Breezy inserts only dictated content. It never appends a completion space or moves the caret past an automatic separator. Consecutive phrases receive exactly one separator when needed. Physical keyboard or mouse input resets the conservative fallback context used by editors that do not expose caret text; Breezy's own injected Unicode input cannot create that reset. When you rename a Photoshop layer, a pause between dictated phrases produces one space without adding a leading space to the selected name.

### Voice-command examples

| Say | Result |
|---|---|
| `hello comma world` | `Hello, world` |
| `new line second item` | A new line followed by `Second item` when line capitalization is enabled |
| `new paragraph next thought` | A blank line followed by `Next thought` when paragraph capitalization is enabled |
| `five thirty seven PM` | `5:37 PM` |
| `three million five hundred forty two thousand three hundred eight` | `3,542,308` |
| `caps lock this is temporary` | `THIS IS TEMPORARY`; the mode expires after two seconds of silence |
| `open quote local first close quote` | `“local first”` |
| `open parenthesis ready close parenthesis` | `(ready)` |

`comma`, `period`, `question mark`, `exclamation mark`, `semicolon`, `colon`, `open quote`, `close quote`, `em dash`, `hyphen`, `slash`, `underscore`, `backtick`, `new line`, and `new paragraph` are spoken punctuation and layout commands. Automatic punctuation controls inferred marks only.

### Corrections

Open **Dictation corrections…** from the tray to map one or more phrases Breezy may hear to the exact text it should type. Choose whether matching is case-sensitive and whether phrases must appear as complete words. Existing correction files remain active during an upgrade and are written to the new runtime after migration.

### Capitalization and tray controls

The **Capitalization** submenu independently controls the first word after a blank-line paragraph and after one line break. `all caps on`, `all caps off`, `caps on`, `caps off`, and `cap` remain available. `caps lock` toggles temporary all caps inline; repeating it within two seconds turns it off.

The tray also controls microphone selection, automatic punctuation, activation hotkey, restart, local logs, and session-only disablement of dictation and AutoHotkey.

## Performance and requirements

| Requirement | Supported setup |
|---|---|
| Operating system | 64-bit Windows 11 |
| Runtime | Python 3.12 with Tcl/Tk |
| Hotkey relay | AutoHotkey v2 |
| Compute | Automatic, NVIDIA CUDA, or CPU INT8 |
| Hardware | Working microphone and enough space for the chosen model |

`large-v3-turbo` is the default model. Approximate GPU memory needs:

| Model | Approximate VRAM | Tradeoff |
|---|---:|---|
| `large-v3-turbo` | 6 GB | Best default balance of quality and speed |
| `medium.en` | 5 GB | High English accuracy, slower |
| `small.en` | 2 GB | Lighter and faster, lower accuracy |
| `base.en` | 1 GB | Fastest, largest accuracy tradeoff |

NVIDIA CUDA is optional. AMD, Intel, and systems without a dedicated GPU use the supported CPU INT8 path; Breezy does not currently provide built-in AMD or Intel GPU acceleration.

## Privacy and troubleshooting

Microphone audio is processed on your computer. Dictated text and correction rules stay local. Troubleshooting logs are stored in `%LOCALAPPDATA%\breezy_dictation\logs`; they do not contain audio or dictated words.

| Symptom | Action |
|---|---|
| No tray icon | Rerun setup and choose **Start dictation now**. |
| Wrong microphone | Select **Refresh microphone list**, then choose the input again. |
| No text appears | Confirm the destination is editable and focused, then inspect the local logs. |
| CUDA or model error | Update the NVIDIA driver, or rerun setup and choose **CPU**. |
| CPU transcription is too slow | Rerun setup with a smaller English model. |
| Hotkey does nothing | Confirm AutoHotkey v2 is installed, then restart Breezy Dictation. |

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Uninstall
```

Uninstall removes wizard-owned runtime files and startup registration while preserving `text_conversions.json`. The migration tombstone is removed with the new runtime. Delete correction rules only with the separate `-DeleteConversions` option and its exact confirmation.

## Credits

Released under the [MIT License](LICENSE). This project includes modified source from [`faster-whisper-dictation`](THIRD_PARTY_NOTICES.md); see the third-party notices for attribution and license text.
