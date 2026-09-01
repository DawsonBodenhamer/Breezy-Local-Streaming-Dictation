<h1 align="center">Breezy Dictation</h1>

<p align="center"><strong>Streaming dictation that puts accurate, punctuated text on screen while you speak.</strong></p>

<p align="center">
  <a href="#what-you-need"><img src="https://img.shields.io/badge/Windows-11-3776AB?style=for-the-badge&logo=windows11&logoColor=white" alt="Windows 11"></a>
  <a href="#privacy"><img src="https://img.shields.io/badge/Processing-Local-2E7D32?style=for-the-badge" alt="Local Processing"></a>
  <a href="#what-you-need"><img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-C9A227?style=for-the-badge" alt="MIT License"></a>
</p>

---

<img src="assets/breezy_dictation_icon_hd.png" align="left" width="50%" alt="Gold microphone with streaming voice waves">

## Why I made it

The dictation tools I found either came with another subscription or made me record first and wait for the result. I wanted words to appear while I spoke, with reliable recognition and punctuation I could control, without giving up speed. Breezy also processes microphone audio locally and learns recurring names and terminology through accessible corrections.

<br clear="left">

<table>
  <tr>
    <td width="50%">
      <strong>⚡ Live text</strong><br>
      See words appear as you speak instead of waiting for a recording to finish.
    </td>
    <td width="50%">
      <strong>⌨️ Type and talk</strong><br>
      Keep using the keyboard while dictated text arrives in the same field.
    </td>
  </tr>
  <tr>
    <td width="50%">
      <strong>🔒 Private by default</strong><br>
      Process microphone audio locally instead of sending recordings to a service.
    </td>
    <td width="50%">
      <strong>✍️ Your vocabulary</strong><br>
      Correct names, terminology, and recurring phrases with personal rules.
    </td>
  </tr>
</table>

## Using Breezy

1. Choose a microphone from the tray.
2. Focus an editable text field.
3. Press the activation hotkey (`Win+H` unless you changed it).
4. Speak naturally — the tray icon shows a red dot while Breezy is listening.
5. Press the hotkey again to stop it from listening... or just leave it on! It uses very little resources when you're not speaking.

Text arrives while you speak, and you can keep using the keyboard in the same field between phrases.

### Voice command examples

Examples below start on a fresh line, with the default capitalization choices on.

<table>
  <thead>
    <tr>
      <th width="50%">You say</th>
      <th width="50%">Breezy types</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><code>hello comma world</code></td>
      <td><code>Hello, world</code></td>
    </tr>
    <tr>
      <td><code>i'm not arguing period i'm just explaining why i'm right exclamation mark</code></td>
      <td><code>I'm not arguing. I'm just explaining why I'm right!</code></td>
    </tr>
    <tr>
      <td><code>we're out of pork question mark</code></td>
      <td><code>We're out of pork?</code></td>
    </tr>
    <tr>
      <td><code>my sandwich has three ingredients colon ham comma pork comma and more pork</code></td>
      <td><code>My sandwich has three ingredients: ham, pork, and more pork</code></td>
    </tr>
    <tr>
      <td><code>this is the first line new line and this is the second line</code></td>
      <td><code>This is the first line<br>And this is the second line</code></td>
    </tr>
    <tr>
      <td><code>this is the first paragraph new paragraph and this is the second paragraph</code></td>
      <td><code>This is the first paragraph<br><br>And this is the second paragraph</code></td>
    </tr>
    <tr>
      <td><code>i counted twenty-two rabbits</code></td>
      <td><code>I counted 22 rabbits</code></td>
    </tr>
    <tr>
      <td><code>meet me at five thirty seven</code></td>
      <td><code>Meet me at 5:37</code></td>
    </tr>
    <tr>
      <td><code>that will be five hundred thirty seven ducks</code></td>
      <td><code>That will be 537 ducks</code></td>
    </tr>
    <tr>
      <td><code>the combination is one two three four</code></td>
      <td><code>The combination is one two three four</code> (separate digits less than 10 stay as words)</td>
    </tr>
    <tr>
      <td><code>digit seven</code></td>
      <td><code>7</code>, always a literal numeral</td>
    </tr>
    <tr>
      <td><code>digit one digit two digit three</code></td>
      <td><code>1 2 3</code></td>
    </tr>
    <tr>
      <td><code>quote local first close quote</code></td>
      <td><code>“local first”</code></td>
    </tr>
    <tr>
      <td><code>open parenthesis pork close parenthesis</code></td>
      <td><code>(pork)</code></td>
    </tr>
  </tbody>
</table>

Breezy also understands `semicolon`, brackets, `em dash`, `hyphen`, `slash`, `underscore`, and `backtick`. The tray's **Automatic punctuation** choice controls only the punctuation that the underlying Whisper dictation model infers on its own — it is off by default.

### Temporary all caps

Say `caps lock` anywhere in a phrase to capitalize everything after it: `fresh bread caps lock straight out of the oven` types `fresh bread STRAIGHT OUT OF THE OVEN`. Say `caps lock` a second time to go back to normal, so `caps lock mustard caps lock and mayo please` types `MUSTARD and mayo please`. If you stop speaking for two seconds, the mode expires on its own before your next phrase — you never have to remember to turn it off.

Only the exact command counts: `my caps lock key is broken` types as ordinary text. You can also say `all caps on` and `all caps off` to do same thing, if you're a psychopath.

## Everyday controls

<table>
  <thead>
    <tr>
      <th>Tray menu choice</th>
      <th>What it does</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><strong>Microphone</strong></td>
      <td>Switch the active input.</td>
    </tr>
    <tr>
      <td><strong>Automatic punctuation</strong></td>
      <td>Let Breezy infer commas and question marks. Leave it off when you prefer to dictate punctuation yourself.</td>
    </tr>
    <tr>
      <td><strong>Capitalization</strong></td>
      <td>Choose independently whether text after a new paragraph or one new line begins with a capital letter.</td>
    </tr>
    <tr>
      <td><strong>Dictation corrections…</strong></td>
      <td>Map one or more phrases Breezy may hear to the exact text it should type instead.</td>
    </tr>
    <tr>
      <td><strong>Change activation hotkey…</strong></td>
      <td>Record a new keyboard shortcut to toggle dictation.</td>
    </tr>
    <tr>
      <td><strong>Restart dictation</strong></td>
      <td>Start a fresh dictation session. Useful if things seem to be borky.</td>
    </tr>
    <tr>
      <td><strong>Open logs</strong></td>
      <td>Open local troubleshooting information.</td>
    </tr>
    <tr>
      <td><strong>Disable dictation &amp; AutoHotkey</strong></td>
      <td>Turn dictation off until the next logon. This frees up your GPU vram and turns off Autohotkey, which is good because some FPS games flag that as potentially trying to cheat.</td>
    </tr>
  </tbody>
</table>

## Performance

WhisperLiveKit's default `large-v3-turbo` model is the reason this feels both quick and accurate: it offers excellent transcription quality without the slower pace of the full large model. NVIDIA CUDA is optional acceleration; systems without a compatible NVIDIA GPU can run the same transcription model through the supported CPU INT8 fallback. CPU inference is usually slower, so setup also accepts smaller English models when responsiveness or available memory matters more than maximum accuracy.

| Model and approximate VRAM | What to expect |
|---|---|
| **`large-v3-turbo`** — default, 6 GB | Excellent quality with fast transcription |
| **`medium.en`** — 5 GB | High English accuracy with slower transcription |
| **`small.en`** — 2 GB | A strong balance for limited hardware |
| **`base.en`** — 1 GB | Faster and lighter, with a larger accuracy tradeoff |

Memory figures are approximate and real usage varies by hardware. See the [WhisperLiveKit model guide](https://github.com/QuentinFuxa/WhisperLiveKit/blob/main/docs/default_and_custom_models.md) for the upstream model comparisons. The VRAM figures apply to GPU acceleration; CPU mode uses system memory instead.

## Start in five steps

> [!TIP]
> Download the latest release, then preview setup. A dry run makes no changes to your computer.

1. Open the project's GitHub **Releases** page and select the latest release.
2. Download the `Breezy-Dictation-v<version>.zip` asset and extract it to a local folder.
3. Open PowerShell in that folder.
4. Preview the setup:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -DryRun
   ```

5. Install when you are ready:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```

Setup guides you through the install location, model storage, compute mode, microphone, and startup preference. **Automatic** uses NVIDIA CUDA when it is available and falls back to CPU INT8 otherwise; you can also select either mode explicitly. The activation hotkey starts as `Win+H` and can be changed from the tray. Automatic punctuation starts off on a fresh installation. Running setup again keeps your dictation corrections and punctuation choice intact.

## What you need

| Requirement | Supported setup |
|---|---|
| Operating system | 64-bit Windows 11 |
| Compute | Automatic, NVIDIA CUDA, or CPU INT8 |
| NVIDIA acceleration | Optional; requires a compatible NVIDIA GPU with current drivers |
| CPU fallback | Supported on compatible x86-64 systems with AMD, Intel, or no dedicated GPU |
| Recommended VRAM | 8 GB or more for the default model when using NVIDIA CUDA |
| Runtime | Python 3.12 with Tcl/Tk |
| Hotkey | AutoHotkey v2 |
| Hardware | Working microphone and enough space for the chosen model |

## AMD or Intel GPU?

Breezy supports machines with AMD or Intel graphics through CPU INT8; it does not currently accelerate transcription on those GPUs. Advanced users can use the upstream [whisper.cpp Vulkan instructions](https://github.com/ggml-org/whisper.cpp#vulkan-gpu-support) as a theoretical cross-vendor starting point. This route is unsupported and unvalidated here: it would require you to replace Breezy's transcription backend and model integration, not select another built-in compute option.

## Privacy

Your microphone audio is processed on your computer. Dictated text and personal conversion rules stay local. Logs are stored in `%LOCALAPPDATA%\breezy_dictation\logs` and never contain audio or dictated words.

<details>
<summary><strong>Common fixes</strong></summary>

| Symptom | Try this |
|---|---|
| No tray icon | Rerun setup and choose **Start dictation now** at the final step. |
| Wrong microphone | Select **Refresh microphone list**, then choose the input again. |
| No text appears | Confirm the destination is editable and focused, then inspect the local logs. |
| Quiet or spotty pickup | Run `breezy-dictation mic-diagnostic` and compare your speech level across the printed trials; move the microphone closer if speech rarely crosses the threshold. |
| CUDA or model error | Update the NVIDIA driver, or rerun setup and choose **CPU**. |
| CPU transcription is too slow | Rerun setup with a smaller English model. |
| Hotkey does nothing | Confirm AutoHotkey v2 is installed, then restart Breezy Dictation. |

</details>

## Uninstall safely

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Uninstall
```

Normal uninstall keeps `text_conversions.json` and removes the startup registration along with the runtime the setup wizard owns, including any migration marker from a previous folder. Deleting your correction rules requires the separate `-DeleteConversions` option and a confirmation that names the file.

## Credits

Released under the [MIT License](LICENSE). This project includes modified source from [`faster-whisper-dictation`](THIRD_PARTY_NOTICES.md); see the third-party notices for attribution and license text.
