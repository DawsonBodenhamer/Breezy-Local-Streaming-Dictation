[CmdletBinding()]
param(
    [string]$Runtime = (Join-Path $env:LOCALAPPDATA 'breezy_dictation'),
    [Parameter(Mandatory)][string]$HotkeyBinding,
    [ValidateRange(1, 120)][int]$HealthTimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'
$Runtime = [System.IO.Path]::GetFullPath($Runtime)
$Supervisor = Join-Path $Runtime 'supervisor.ps1'
$RuntimeEnvironment = Join-Path $Runtime 'runtime.env'
$ReadyFile = Join-Path $Runtime 'hotkey.ready'
$PendingFile = Join-Path $Runtime 'hotkey_change.pending'
$ResultFile = Join-Path $Runtime 'hotkey_change.result.json'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-AtomicBytes {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][byte[]]$Bytes)
    $temporary = "$Path.tmp"
    [System.IO.File]::WriteAllBytes($temporary, $Bytes)
    if (Test-Path -LiteralPath $Path) {
        $backup = "$Path.bak"
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        [System.IO.File]::Replace($temporary, $Path, $backup)
        Remove-Item -LiteralPath $backup -Force
    } else {
        [System.IO.File]::Move($temporary, $Path)
    }
}

function Set-RuntimeHotkey {
    param([Parameter(Mandatory)][string]$Binding)
    $lines = if (Test-Path -LiteralPath $RuntimeEnvironment) {
        @(Get-Content -LiteralPath $RuntimeEnvironment -Encoding utf8)
    } else {
        @()
    }
    $updated = @($lines | Where-Object { $_ -notmatch '^(BREEZY_DICTATION_HOTKEY|BREEZY_LOCAL_STREAMING_DICTATION_HOTKEY)=' })
    $updated += "BREEZY_DICTATION_HOTKEY=$Binding"
    $text = ($updated -join "`r`n") + "`r`n"
    Write-AtomicBytes -Path $RuntimeEnvironment -Bytes $Utf8NoBom.GetBytes($text)
}

function Get-RuntimeHotkey {
    if (-not (Test-Path -LiteralPath $RuntimeEnvironment)) { return '#h' }
    $line = Get-Content -LiteralPath $RuntimeEnvironment -Encoding utf8 |
        Where-Object { $_ -like 'BREEZY_DICTATION_HOTKEY=*' } |
        Select-Object -Last 1
    if (-not $line) {
        $line = Get-Content -LiteralPath $RuntimeEnvironment -Encoding utf8 |
            Where-Object { $_ -like 'BREEZY_LOCAL_STREAMING_DICTATION_HOTKEY=*' } |
            Select-Object -Last 1
    }
    if (-not $line) { return '#h' }
    return $line.Substring($line.IndexOf('=') + 1)
}

function Restore-RuntimeEnvironment {
    param([bool]$Existed, [byte[]]$Bytes)
    if ($Existed) {
        Write-AtomicBytes -Path $RuntimeEnvironment -Bytes $Bytes
    } else {
        Remove-Item -LiteralPath $RuntimeEnvironment -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Supervisor {
    param([Parameter(Mandatory)][string]$Command)
    $global:LASTEXITCODE = 0
    & $Supervisor $Command
    if ($LASTEXITCODE -notin @($null, 0)) {
        throw "Supervisor command failed: $Command"
    }
}

function Wait-ForHotkeyHealth {
    param([Parameter(Mandatory)][string]$Binding)
    $deadline = [datetime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
    do {
        & $Supervisor hotkey-health -Hotkey $Binding *> $null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Milliseconds 100
    } while ([datetime]::UtcNow -lt $deadline)
    return $false
}

function Write-Result {
    param(
        [Parameter(Mandatory)][string]$Status,
        [Parameter(Mandatory)][string]$ActiveBinding,
        [Parameter(Mandatory)][string]$Message
    )
    $payload = [ordered]@{
        status = $Status
        active_binding = $ActiveBinding
        message = $Message
    } | ConvertTo-Json -Compress
    Write-AtomicBytes -Path $ResultFile -Bytes $Utf8NoBom.GetBytes($payload)
}

if (-not (Test-Path -LiteralPath $Supervisor -PathType Leaf)) {
    throw "Supervisor is missing: $Supervisor"
}

$createdNew = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\BreezyDictationHotkeyChange', [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    throw 'Another shortcut change is already in progress.'
}

$environmentExisted = Test-Path -LiteralPath $RuntimeEnvironment
$priorBytes = if ($environmentExisted) { [System.IO.File]::ReadAllBytes($RuntimeEnvironment) } else { [byte[]]@() }
$priorBinding = Get-RuntimeHotkey
$exitCode = 1

try {
    Remove-Item -LiteralPath $ReadyFile, $ResultFile -Force -ErrorAction SilentlyContinue
    Set-Content -LiteralPath $PendingFile -Encoding ascii -NoNewline -Value 'pending'
    Invoke-Supervisor -Command stop
    Set-RuntimeHotkey -Binding $HotkeyBinding
    Invoke-Supervisor -Command start
    if (-not (Wait-ForHotkeyHealth -Binding $HotkeyBinding)) {
        throw 'The new shortcut did not become healthy before the timeout.'
    }
    Write-Result -Status success -ActiveBinding $HotkeyBinding -Message 'Activation shortcut changed.'
    $exitCode = 0
} catch {
    $applyFailure = $_.Exception.Message
    try { Invoke-Supervisor -Command stop } catch {}
    Restore-RuntimeEnvironment -Existed $environmentExisted -Bytes $priorBytes
    Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue
    try {
        Invoke-Supervisor -Command start
        if (Wait-ForHotkeyHealth -Binding $priorBinding) {
            Write-Result -Status rolled_back -ActiveBinding $priorBinding -Message 'The shortcut was not changed. The previous shortcut was restored.'
        } else {
            Write-Result -Status recovery_failed -ActiveBinding $priorBinding -Message 'The previous shortcut settings were restored, but dictation did not become healthy. Open the logs or restart dictation.'
        }
    } catch {
        Write-Result -Status recovery_failed -ActiveBinding $priorBinding -Message 'The previous shortcut settings were restored, but dictation could not restart. Open the logs or restart dictation.'
    }
    Write-Error $applyFailure
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}

exit $exitCode
