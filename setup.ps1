[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Uninstall,
    [switch]$DeleteConversions
)

$ErrorActionPreference = 'Stop'
$script:StageIndex = 0
$script:TotalStages = 8
$script:Summary = [System.Collections.Generic.List[string]]::new()

function Start-WizardStage {
    param([Parameter(Mandatory)][string]$Name)
    $script:StageIndex++
    Write-Host "`n[$($script:StageIndex)/$($script:TotalStages)] $Name" -ForegroundColor Cyan
}

function Open-WizardUrl {
    param([Parameter(Mandatory)][uri]$Url)
    Write-Host "Open: $Url"
    if (-not $DryRun) { Start-Process $Url.AbsoluteUri }
}

function Read-WizardValue {
    param([Parameter(Mandatory)][string]$Prompt)
    return Read-Host $Prompt
}

function Read-WizardSecret {
    param([Parameter(Mandatory)][string]$Prompt)
    return Read-Host $Prompt -AsSecureString
}

function Confirm-WizardAction {
    param([Parameter(Mandatory)][string]$Description)
    $answer = Read-Host "$Description Type YES to continue"
    return $answer -ceq 'YES'
}

function Set-WizardEnvValue {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Value
    )
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $lines = if (Test-Path -LiteralPath $resolved) { Get-Content -LiteralPath $resolved -Encoding utf8 } else { @() }
    $updated = @($lines | Where-Object { $_ -notmatch ('^{0}=' -f [regex]::Escape($Name)) }) + ("{0}={1}" -f $Name, $Value)
    if ($DryRun) {
        Write-Host "Would update $Name in $resolved"
    } else {
        $updated | Set-Content -LiteralPath $resolved -Encoding utf8
    }
    $script:Summary.Add("Updated $Name in $resolved")
}

function Set-WizardDefaultEnvValue {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Value
    )
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $existing = if (Test-Path -LiteralPath $resolved) { @(Get-Content -LiteralPath $resolved -Encoding utf8) } else { @() }
    if ($existing | Where-Object { $_ -match ('^{0}=' -f [regex]::Escape($Name)) }) {
        $script:Summary.Add("Preserved existing $Name in $resolved")
        return
    }
    Set-WizardEnvValue -Path $resolved -Name $Name -Value $Value
}

function Get-ConversionFingerprint {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $item = Get-Item -LiteralPath $Path
    return "{0}:{1}" -f $item.Length, (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

function Assert-ConversionsUnchanged {
    param([string]$Before, [string]$Path)
    if ($null -ne $Before -and $Before -ne (Get-ConversionFingerprint -Path $Path)) {
        throw "Setup stopped because $Path changed. Restore it from your backup before continuing."
    }
}

function Invoke-Checked {
    param([Parameter(Mandatory)][string]$Description, [Parameter(Mandatory)][scriptblock]$Action)
    if ($DryRun) { Write-Host "Would: $Description"; return }
    $global:LASTEXITCODE = 0
    & $Action
    $exitCode = $global:LASTEXITCODE
    if ($exitCode -notin @($null, 0)) { throw "$Description failed with exit code $exitCode" }
    $script:Summary.Add($Description)
}

function Resolve-RuntimeAuthority {
    param(
        [Parameter(Mandatory)][string]$NewRuntime,
        [Parameter(Mandatory)][string]$LegacyRuntime
    )
    if (Test-Path -LiteralPath $NewRuntime -PathType Container) {
        return $NewRuntime
    }
    if (Test-Path -LiteralPath $LegacyRuntime -PathType Container) {
        $tombstonePath = Join-Path $LegacyRuntime 'migration.tombstone.json'
        if (Test-Path -LiteralPath $tombstonePath -PathType Leaf) {
            try {
                $tombstone = Get-Content -LiteralPath $tombstonePath -Raw -Encoding utf8 | ConvertFrom-Json
                if ([System.IO.Path]::GetFullPath([string]$tombstone.target) -eq [System.IO.Path]::GetFullPath($NewRuntime)) {
                    throw "Legacy runtime is migrated, but the new authority is missing: $NewRuntime"
                }
            } catch [System.Management.Automation.RuntimeException] {
                throw
            } catch {
                throw "Cannot read the legacy runtime migration tombstone: $tombstonePath"
            }
        }
        return $LegacyRuntime
    }
    return $NewRuntime
}

function Invoke-Uninstall {
    $newRuntime = Join-Path $env:LOCALAPPDATA 'breezy_dictation'
    $legacyRuntime = Join-Path $env:LOCALAPPDATA 'breezy_local_streaming_dictation'
    $runtime = Resolve-RuntimeAuthority -NewRuntime $newRuntime -LegacyRuntime $legacyRuntime
    $conversions = Join-Path $runtime 'text_conversions.json'
    if (-not (Confirm-WizardAction "Stop dictation and remove wizard-owned files from ${runtime}? Conversion rules will be preserved.")) { return }
    if (-not $DryRun -and (Test-Path -LiteralPath (Join-Path $runtime 'supervisor.ps1'))) {
        & (Join-Path $runtime 'supervisor.ps1') stop
        & (Join-Path $runtime 'supervisor.ps1') remove-task
    }
    $conversionBefore = Get-ConversionFingerprint -Path $conversions
    $ownedFiles = @(
        'client_bootstrap.pyw', 'config.toml', 'hotkey_apply.ps1', 'hotkey_capture.ahk',
        'hotkey.ready', 'hotkey_change.pending', 'hotkey_change.result.json',
        'manual_line_break.signal', 'physical_context_reset.signal', 'caret_displacement.signal',
        'physical_context.generation', 'client.ready', 'client_failed.flag',
        'formatting_config.py', 'list_microphones.py', 'runtime.env', 'startup_hidden.vbs', 'supervisor.ps1',
        'physical_context_signal.ahk', 'text_conversion_manager.py', 'win_h.ahk'
    )
    $ownedDirectories = @('.venv', 'assets', 'logs', 'manifests', 'models')
    if (-not $DryRun) {
        foreach ($name in $ownedFiles) {
            $path = Join-Path $runtime $name
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
        }
        foreach ($name in $ownedDirectories) {
            $path = Join-Path $runtime $name
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
        }
        Assert-ConversionsUnchanged -Before $conversionBefore -Path $conversions
    }
    if ($DeleteConversions) {
        $exact = Read-Host "Type DELETE text_conversions.json to delete $conversions"
        if ($exact -ceq 'DELETE text_conversions.json' -and -not $DryRun) {
            Remove-Item -LiteralPath $conversions -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not $DryRun -and (Test-Path -LiteralPath $runtime) -and -not (Get-ChildItem -LiteralPath $runtime -Force | Select-Object -First 1)) {
        Remove-Item -LiteralPath $runtime -Force
    }
    if (-not $DryRun -and $runtime -eq $newRuntime -and (Test-Path -LiteralPath $legacyRuntime -PathType Container)) {
        $legacyTombstone = Join-Path $legacyRuntime 'migration.tombstone.json'
        if (Test-Path -LiteralPath $legacyTombstone -PathType Leaf) {
            Remove-Item -LiteralPath $legacyRuntime -Recurse -Force
        }
    }
    if (-not $DryRun) {
        Unregister-ScheduledTask -TaskName 'Breezy Local Streaming Dictation' -TaskPath '\' -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Host 'Uninstall complete. Conversion rules were preserved unless separately deleted.' -ForegroundColor Green
}

if ($Uninstall) { Invoke-Uninstall; return }

$sourceRoot = $PSScriptRoot
$runtimeDefault = Join-Path $env:LOCALAPPDATA 'breezy_dictation'
$legacyRuntimeDefault = Join-Path $env:LOCALAPPDATA 'breezy_local_streaming_dictation'
$conversionPath = Join-Path $runtimeDefault 'text_conversions.json'
$conversionBefore = Get-ConversionFingerprint -Path $conversionPath

Start-WizardStage 'Review requirements'
Write-Host 'Windows 11 x86-64, Python 3.12 with Tcl/Tk, AutoHotkey v2, and a microphone are required.'
Write-Host 'Dry run prints intended actions. It performs no downloads, installs, file writes, or startup changes.'
if ([Environment]::OSVersion.Version.Build -lt 22000) { throw 'Windows 11 build 22000 or newer is required.' }
$gpu = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
$nvidiaReady = $false
if ($gpu) {
    try {
        $gpuDetails = @(& $gpu.Source --query-gpu=name,memory.total --format=csv,noheader 2>$null)
        $nvidiaReady = $LASTEXITCODE -eq 0 -and $gpuDetails.Count -gt 0
        if ($nvidiaReady) { $gpuDetails | ForEach-Object { Write-Host $_ } }
    } catch {
        $nvidiaReady = $false
    }
}
if (-not $nvidiaReady) {
    Write-Host 'NVIDIA tools were not found. Automatic mode will use supported CPU INT8 inference.'
}
$computeChoice = Read-WizardValue 'Compute mode: Automatic, NVIDIA GPU (CUDA), or CPU [Automatic]'
if ([string]::IsNullOrWhiteSpace($computeChoice) -or $computeChoice -ieq 'Automatic') {
    $resolvedCompute = if ($nvidiaReady) { 'cuda' } else { 'cpu' }
} elseif ($computeChoice -ieq 'NVIDIA GPU (CUDA)') {
    if (-not $nvidiaReady) { throw 'NVIDIA GPU (CUDA) mode requires a usable NVIDIA driver and nvidia-smi.' }
    $resolvedCompute = 'cuda'
} elseif ($computeChoice -ieq 'CPU') {
    $resolvedCompute = 'cpu'
} else {
    throw 'Choose Automatic, NVIDIA GPU (CUDA), or CPU.'
}
$computeType = if ($resolvedCompute -eq 'cuda') { 'float16' } else { 'int8' }
$engineDeviceLine = if ($resolvedCompute -eq 'cuda') { 'device = "cuda"' } else { 'device = "cpu"' }
$engineComputeTypeLine = if ($resolvedCompute -eq 'cuda') { 'compute_type = "float16"' } else { 'compute_type = "int8"' }
Write-Host "Resolved inference: device = `"$resolvedCompute`", compute_type = `"$computeType`""

Start-WizardStage 'Choose installation folder'
$runtimeInput = Read-WizardValue "Installation folder [$runtimeDefault]"
$runtime = if ([string]::IsNullOrWhiteSpace($runtimeInput)) { $runtimeDefault } else { [System.IO.Path]::GetFullPath($runtimeInput) }
if ([System.IO.Path]::GetFullPath($runtime) -eq [System.IO.Path]::GetFullPath($runtimeDefault) -and (Test-Path -LiteralPath $legacyRuntimeDefault -PathType Container)) {
    Invoke-Checked 'Migrate the legacy Breezy Local Streaming Dictation runtime' {
        & (Join-Path $sourceRoot 'windows\migrate_runtime.ps1') -OldRuntime $legacyRuntimeDefault -NewRuntime $runtime
    }
}
$conversionPath = Join-Path $runtime 'text_conversions.json'
$conversionBefore = Get-ConversionFingerprint -Path $conversionPath
$drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($runtime))
Write-Host ("Available disk space: {0:N1} GB" -f ($drive.AvailableFreeSpace / 1GB))
if ($drive.AvailableFreeSpace -lt 15GB) {
    Write-Warning 'At least 15 GB of free space is recommended for the runtime and default model.'
    if (-not $DryRun) { throw 'Choose an installation drive with at least 15 GB free.' }
}

Start-WizardStage 'Check Python and AutoHotkey'
$python = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
$autoHotkey = Join-Path $env:ProgramFiles 'AutoHotkey\v2\AutoHotkey64.exe'
$pythonReady = $false
if ($python) {
    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        & $python -3.12 -c "import tkinter; tkinter.Tcl().eval('info patchlevel')" *> $null
        $pythonReady = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorPreference
    }
}
if (-not $pythonReady) { Write-Warning 'Install 64-bit Python 3.12 with Tcl/Tk.'; Open-WizardUrl 'https://www.python.org/downloads/windows/' }
if (-not (Test-Path -LiteralPath $autoHotkey)) { Write-Warning 'AutoHotkey v2 not found.'; Open-WizardUrl 'https://www.autohotkey.com/' }
if (-not $DryRun -and (-not $pythonReady -or -not (Test-Path -LiteralPath $autoHotkey))) { throw 'Install the missing prerequisite, then rerun setup.' }

Start-WizardStage 'Create isolated runtime and install dependencies'
if (-not (Confirm-WizardAction "Create or update the isolated runtime at ${runtime}? This writes local files and downloads Python packages.")) { Write-Warning 'Setup cancelled before installation.'; return }
Invoke-Checked 'Create the installation folder' { New-Item -ItemType Directory -Force -Path $runtime | Out-Null }
Invoke-Checked 'Create the Python 3.12 virtual environment' { & $python -3.12 -m venv (Join-Path $runtime '.venv') }
$runtimePython = Join-Path $runtime '.venv\Scripts\python.exe'
Invoke-Checked 'Install pinned Python dependencies' { & $runtimePython -m pip install --require-hashes -r (Join-Path $sourceRoot 'requirements.lock') }
if ($resolvedCompute -eq 'cuda') {
    Invoke-Checked 'Install pinned NVIDIA CUDA dependencies' { & $runtimePython -m pip install --require-hashes -r (Join-Path $sourceRoot 'requirements.cuda.lock') }
}
Invoke-Checked 'Install Breezy Dictation' { & $runtimePython -m pip install --no-build-isolation --no-deps --force-reinstall $sourceRoot }
Assert-ConversionsUnchanged -Before $conversionBefore -Path $conversionPath

Start-WizardStage 'Choose model and microphone'
$model = Read-WizardValue 'Model name or local model path [large-v3-turbo]'
if ([string]::IsNullOrWhiteSpace($model)) { $model = 'large-v3-turbo' }
$modelStorageDefault = Join-Path $runtime 'models'
$modelStorageInput = Read-WizardValue "Model download/cache folder [$modelStorageDefault]"
$modelStorage = if ([string]::IsNullOrWhiteSpace($modelStorageInput)) { $modelStorageDefault } else { [System.IO.Path]::GetFullPath($modelStorageInput) }
$runtimePython = Join-Path $runtime '.venv\Scripts\python.exe'
if (-not $DryRun -and (Test-Path -LiteralPath $runtimePython)) {
    $microphones = @(& $runtimePython (Join-Path $sourceRoot 'windows\list_microphones.py'))
    if ($LASTEXITCODE -ne 0 -or $microphones.Count -eq 0) { throw 'No usable microphone inputs were found.' }
    $microphones | ForEach-Object { Write-Host $_ }
}
$deviceText = Read-WizardValue 'Microphone device number [-1 for system default]'
$device = -1
if (-not [string]::IsNullOrWhiteSpace($deviceText) -and -not [int]::TryParse($deviceText, [ref]$device)) { throw 'Microphone device must be a number.' }
Write-Host 'Activation hotkey: Win+H by default. Change it later from the tray by pressing the desired chord.'

Start-WizardStage 'Write machine-local configuration'
if (-not (Confirm-WizardAction "Write config.toml under $runtime without replacing text_conversions.json?")) { Write-Warning 'Setup cancelled before configuration.'; return }
$escapedModel = $model.Replace('\', '\\').Replace('"', '\"')
$config = (Get-Content -LiteralPath (Join-Path $sourceRoot 'config\config.example.toml') -Raw -Encoding utf8)
$existingConfigPath = Join-Path $runtime 'config.toml'
if (Test-Path -LiteralPath $existingConfigPath) {
    $existingConfig = Get-Content -LiteralPath $existingConfigPath -Raw -Encoding utf8
    $automaticPunctuation = if ($existingConfig -match '(?ms)^\[formatting\].*?^automatic_punctuation\s*=\s*(true|false)') { $Matches[1] } else { 'true' }
    $capitalizeNewParagraphs = if ($existingConfig -match '(?ms)^\[formatting\].*?^capitalize_new_paragraphs\s*=\s*(true|false)') { $Matches[1] } else { 'true' }
    $capitalizeNewLines = if ($existingConfig -match '(?ms)^\[formatting\].*?^capitalize_new_lines\s*=\s*(true|false)') { $Matches[1] } else { 'true' }
    $config = $config -replace 'automatic_punctuation = false', "automatic_punctuation = $automaticPunctuation"
    $config = $config -replace 'capitalize_new_paragraphs = true', "capitalize_new_paragraphs = $capitalizeNewParagraphs"
    $config = $config -replace 'capitalize_new_lines = true', "capitalize_new_lines = $capitalizeNewLines"
}
$config = $config -replace 'model = ""', ('model = "{0}"' -f $escapedModel)
$config = $config -replace 'device = -1', ('device = {0}' -f $device)
$config = $config -replace 'compute_type = "float16"', $engineComputeTypeLine
$config = $config -replace 'device = "cuda"', $engineDeviceLine
if ($DryRun) { Write-Host "Would write $runtime\config.toml" } else { $config | Set-Content -LiteralPath (Join-Path $runtime 'config.toml') -Encoding utf8 }
Set-WizardEnvValue -Path (Join-Path $runtime 'runtime.env') -Name 'HF_HOME' -Value $modelStorage
Set-WizardEnvValue -Path (Join-Path $runtime 'runtime.env') -Name 'BREEZY_DICTATION_AUTOHOTKEY' -Value $autoHotkey
Set-WizardDefaultEnvValue -Path (Join-Path $runtime 'runtime.env') -Name 'BREEZY_DICTATION_HOTKEY' -Value '#h'
$script:Summary.Add('Generated machine-local configuration')
Assert-ConversionsUnchanged -Before $conversionBefore -Path $conversionPath

Start-WizardStage 'Install Windows integration files'
if (-not (Confirm-WizardAction "Copy application launchers, tray assets, and supervision files into ${runtime}?")) { Write-Warning 'Setup cancelled before Windows integration.'; return }
if (-not $DryRun) {
    Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'windows') | Copy-Item -Destination $runtime -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $sourceRoot 'assets') -Destination $runtime -Recurse -Force
}
$script:Summary.Add('Installed Windows integration files')
if (-not (Test-Path -LiteralPath $conversionPath) -and (Confirm-WizardAction "Initialize a new empty correction file at ${conversionPath}?")) {
    if (-not $DryRun) { '{"version":2,"corrections":[]}' | Set-Content -LiteralPath $conversionPath -Encoding utf8 }
    $script:Summary.Add('Initialized a new conversion file')
}
Assert-ConversionsUnchanged -Before $conversionBefore -Path $conversionPath

Start-WizardStage 'Health check and optional startup'
if (-not $DryRun) { & (Join-Path $runtime 'supervisor.ps1') status }
if (Confirm-WizardAction 'Register Breezy Dictation to start at logon? This changes Windows Task Scheduler.' ) {
    Invoke-Checked 'Register startup task' { & (Join-Path $runtime 'supervisor.ps1') install-task }
}
if (Confirm-WizardAction 'Start dictation now? The selected model may download from the internet on first use.' ) {
    Invoke-Checked 'Start dictation' { & (Join-Path $runtime 'supervisor.ps1') start }
}
Assert-ConversionsUnchanged -Before $conversionBefore -Path $conversionPath

Write-Host "`nSummary" -ForegroundColor Green
$script:Summary | ForEach-Object { Write-Host "- $_" }
Write-Host "Installation: $runtime"
Write-Host 'Sensitive values and conversion contents are not displayed.'
