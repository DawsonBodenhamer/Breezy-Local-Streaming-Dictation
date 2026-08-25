[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Root
)

$ErrorActionPreference = 'Stop'
$resolved = (Resolve-Path -LiteralPath $Root).Path
$required = @(
    'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'pyproject.toml',
    'requirements.in', 'requirements.lock', 'requirements.cuda.in',
    'requirements.cuda.lock', 'setup.ps1',
    'config\config.example.toml', 'config\runtime.example.env', 'windows\supervisor.ps1',
    'windows\win_h.ahk', 'windows\hotkey_capture.ahk', 'windows\hotkey_apply.ps1',
    'windows\client_bootstrap.pyw',
    'windows\list_microphones.py', 'windows\startup_hidden.vbs',
    'windows\text_conversion_manager.py',
    'assets\breezy_local_streaming_dictation_icon_hd.png',
    'assets\local_dictation_start.wav',
    'assets\local_dictation_stop.wav',
    'assets\tray_menu.png',
    'licenses\faster-whisper-dictation-MIT.txt',
    'src\whisper_dictation\__init__.py',
    'src\whisper_dictation\engine\local.py',
    'src\whisper_dictation\hotkey\listener.py'
)

$errors = [System.Collections.Generic.List[string]]::new()
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $resolved $relative) -PathType Leaf)) {
        $errors.Add("Missing required file: $relative")
    }
}

$forbiddenNames = '(?i)(^|[\\/])(tests?|probes?|recordings?|logs?|backups?|rollback|machine[_-]?inventory|__pycache__)([\\/._-]|$)|(?i)(?:\.egg-info(?:[\\/]|$)|\.py[co]$)'
$textExtensions = @('.md', '.py', '.pyw', '.ps1', '.ahk', '.vbs', '.toml', '.json', '.txt', '.in', '.lock')
$privatePathPatterns = @(
    '(?i)\b[A-Z]:\\',
    '(?i)\bdevice\s*=\s*[0-9]+',
    '(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}'
)

foreach ($file in Get-ChildItem -LiteralPath $resolved -Recurse -File -Force) {
    $relative = $file.FullName.Substring($resolved.Length + 1)
    if ($relative -match $forbiddenNames -or $relative -eq '.git') {
        $errors.Add("Forbidden public path: $relative")
    }
    if ($textExtensions -contains $file.Extension.ToLowerInvariant() -or $file.Name -in @('requirements.in', 'requirements.lock')) {
        $content = Get-Content -LiteralPath $file.FullName -Raw -Encoding utf8
        foreach ($pattern in $privatePathPatterns) {
            if ($content -match $pattern) { $errors.Add("Private or machine-specific text in ${relative}: $pattern") }
        }
    }
}

$ignoreFile = Join-Path $resolved '.gitignore'
if (Test-Path -LiteralPath $ignoreFile) {
    $ignoreText = Get-Content -LiteralPath $ignoreFile -Raw -Encoding utf8
    if ($ignoreText -match '(?im)^.*(?:test|probe|private[_-]?validation).*$') {
        $errors.Add('Tracked ignore rules disclose excluded development-only paths.')
    }
}

$requirementsText = Get-Content -LiteralPath (Join-Path $resolved 'requirements.lock') -Raw -Encoding utf8
foreach ($dependency in @('faster-whisper-dictation', 'whisperlivekit', 'uiautomation', 'setuptools', 'wheel')) {
    if ($requirementsText -notmatch ('(?im)^' + [regex]::Escape($dependency) + '==')) {
        $errors.Add("Missing locked dependency: $dependency")
    }
}
$cudaRequirementsText = Get-Content -LiteralPath (Join-Path $resolved 'requirements.cuda.lock') -Raw -Encoding utf8
foreach ($dependency in @('nvidia-cublas-cu12', 'nvidia-cuda-nvrtc-cu12', 'nvidia-cudnn-cu12')) {
    if ($cudaRequirementsText -notmatch ('(?im)^' + [regex]::Escape($dependency) + '==')) {
        $errors.Add("Missing locked CUDA dependency: $dependency")
    }
}

if (Test-Path -LiteralPath (Join-Path $resolved '.git')) {
    $errors.Add('A public export must be verified before Git is initialized.')
}
if ($errors.Count -gt 0) {
    $errors | Sort-Object -Unique | ForEach-Object { Write-Error $_ }
    throw "Public-tree verification failed with $($errors.Count) finding(s)."
}

Write-Host "Verified public tree: $resolved" -ForegroundColor Green
Write-Host "Files: $((Get-ChildItem -LiteralPath $resolved -Recurse -File).Count)"
