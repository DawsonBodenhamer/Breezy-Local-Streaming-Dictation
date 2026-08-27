param(
    [ValidateSet('run', 'start', 'stop', 'restart', 'status', 'disable', 'enable', 'pause-client', 'resume-client', 'install-task', 'remove-task', 'devices', 'tray-devices', 'set-microphone', 'switch-microphone', 'hotkey-health')]
    [string]$Command = 'status',
    [int]$Device,
    [string]$Hotkey
)

$ErrorActionPreference = 'Stop'
$Runtime = Join-Path $env:LOCALAPPDATA 'breezy_local_streaming_dictation'
$RuntimeEnvironment = Join-Path $Runtime 'runtime.env'
if (Test-Path -LiteralPath $RuntimeEnvironment) {
    foreach ($Line in Get-Content -LiteralPath $RuntimeEnvironment -Encoding utf8) {
        if ($Line -notmatch '^(HF_HOME|BREEZY_LOCAL_STREAMING_DICTATION_AUTOHOTKEY|BREEZY_LOCAL_STREAMING_DICTATION_HOTKEY)=(.*)$') {
            continue
        }
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
$Venv = Join-Path $Runtime '.venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$Pythonw = Join-Path $Venv 'Scripts\pythonw.exe'
$ManagerPythonw = $Pythonw
$ClientExe = Join-Path $Venv 'Scripts\faster-whisper-dictation.exe'
$ClientBootstrap = Join-Path $Runtime 'client_bootstrap.pyw'
$MicrophoneLister = Join-Path $Runtime 'list_microphones.py'
$ManagerScript = Join-Path $Runtime 'text_conversion_manager.py'
$Config = Join-Path $Runtime 'config.toml'
$AutoHotkey = if ($env:BREEZY_LOCAL_STREAMING_DICTATION_AUTOHOTKEY) {
    $env:BREEZY_LOCAL_STREAMING_DICTATION_AUTOHOTKEY
} else {
    Join-Path $env:ProgramFiles 'AutoHotkey\v2\AutoHotkey64.exe'
}
$HotkeyScript = Join-Path $Runtime 'win_h.ahk'
$HotkeyCapture = Join-Path $Runtime 'hotkey_capture.ahk'
$HotkeyApply = Join-Path $Runtime 'hotkey_apply.ps1'
$HotkeyReadyFile = Join-Path $Runtime 'hotkey.ready'
$HiddenLauncher = Join-Path $Runtime 'startup_hidden.vbs'
$Logs = Join-Path $Runtime 'logs'
$SupervisorLog = Join-Path $Logs 'supervisor.log'
$ClientOut = Join-Path $Logs 'production_client.stdout.log'
$ClientErr = Join-Path $Logs 'production_client.stderr.log'
$HotkeyOut = Join-Path $Logs 'production_hotkey.stdout.log'
$HotkeyErr = Join-Path $Logs 'production_hotkey.stderr.log'
$PidFile = Join-Path $Runtime 'supervisor.pid'
$StopFile = Join-Path $Runtime 'supervisor.stop'
$DisabledFile = Join-Path $Runtime 'disabled.flag'
$PauseFile = Join-Path $Runtime 'client_paused.flag'
$ClientFailedFile = Join-Path $Runtime 'client_failed.flag'
$ClientReadyFile = Join-Path $Runtime 'client.ready'
$env:BREEZY_LOCAL_DICTATION_READY_FILE = $ClientReadyFile
$TaskName = 'Breezy Local Streaming Dictation'
$TaskPath = '\'
$ScriptPath = $MyInvocation.MyCommand.Path
$DeviceSpecified = $PSBoundParameters.ContainsKey('Device')

function Write-SupervisorLog {
    param([string]$Message)
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null
    Add-Content -LiteralPath $SupervisorLog -Encoding utf8 -Value ("{0:o} {1}" -f (Get-Date), $Message)
}

function Assert-Install {
    foreach ($Path in @($Python, $Pythonw, $ManagerPythonw, $ClientExe, $ClientBootstrap, $MicrophoneLister, $ManagerScript, $Config, $AutoHotkey, $HotkeyScript, $HotkeyCapture, $HotkeyApply, $HiddenLauncher)) {
        if (-not (Test-Path -LiteralPath $Path)) {
            throw "Required path is missing: $Path"
        }
    }
}

function Set-CudaPath {
    $CudaBins = @(
        (Join-Path $Venv 'Lib\site-packages\nvidia\cublas\bin'),
        (Join-Path $Venv 'Lib\site-packages\nvidia\cuda_nvrtc\bin'),
        (Join-Path $Venv 'Lib\site-packages\nvidia\cudnn\bin')
    )
    foreach ($Path in $CudaBins) {
        if (-not (Test-Path -LiteralPath $Path)) {
            throw "Pinned CUDA runtime path is missing: $Path"
        }
    }
    $env:PATH = ($CudaBins -join ';') + ';' + $env:PATH
}

function Test-CudaEngine {
    if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { return $false }
    $Text = Get-Content -LiteralPath $Config -Raw -Encoding utf8
    return $Text -match '(?ms)^\[engine\].*?^device\s*=\s*"cuda"\s*$'
}

function Initialize-EnginePath {
    if (Test-CudaEngine) { Set-CudaPath }
}

function Get-LiveSupervisorPid {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $null
    }
    $Value = Get-Content -LiteralPath $PidFile -Raw -Encoding utf8
    $Parsed = 0
    if (-not [int]::TryParse($Value.Trim(), [ref]$Parsed)) {
        Remove-Item -LiteralPath $PidFile -Force
        return $null
    }
    $Process = Get-Process -Id $Parsed -ErrorAction SilentlyContinue
    if ($null -eq $Process) {
        Remove-Item -LiteralPath $PidFile -Force
        return $null
    }
    return $Parsed
}

function Start-Client {
    Initialize-EnginePath
    Remove-Item -LiteralPath $ClientReadyFile -Force -ErrorAction SilentlyContinue
    Write-SupervisorLog 'Starting dictation client.'
    return Start-Process -FilePath $Pythonw `
        -ArgumentList @($ClientBootstrap, 'start', '--streaming', '--config', $Config) `
        -WindowStyle Hidden `
        -PassThru
}

function Start-Hotkey {
    Remove-Item -LiteralPath $HotkeyReadyFile -Force -ErrorAction SilentlyContinue
    Write-SupervisorLog 'Starting AutoHotkey relay.'
    return Start-Process -FilePath $AutoHotkey `
        -ArgumentList @($HotkeyScript) `
        -WindowStyle Hidden `
        -RedirectStandardOutput $HotkeyOut `
        -RedirectStandardError $HotkeyErr `
        -PassThru
}

function Get-SupervisedClientProcess {
    $Existing = Get-LiveSupervisorPid
    if ($null -eq $Existing) {
        return $null
    }
    $Status = (& $ClientExe status 2>&1) -join "`n"
    if ($Status -notmatch 'Status:\s+running\s+\(PID\s+(\d+)\)') {
        return $null
    }
    $Process = Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue
    if ($null -eq $Process) {
        return $null
    }
    $Identity = Get-CimInstance Win32_Process -Filter "ProcessId = $($Process.Id)"
    $CommandLine = $Identity.CommandLine
    $ExpectedBootstrap = "$ClientBootstrap start --streaming"
    $ExpectedConfig = "--config $Config"
    if (
        $Identity.ExecutablePath -notlike '*\pythonw.exe' -or
        $CommandLine -notlike "*$ExpectedBootstrap*" -or
        $CommandLine -notlike "*$ExpectedConfig*"
    ) {
        throw "Refusing client control for PID $($Process.Id): command identity does not match the supervised dictation client."
    }
    return $Process
}

function Stop-OwnedProcess {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$ExpectedPath
    )
    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    $ActualPath = $Process.MainModule.FileName
    if (-not [string]::Equals($ActualPath, $ExpectedPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to stop PID $($Process.Id): expected $ExpectedPath, found $ActualPath"
    }
    Stop-Process -Id $Process.Id
    $Process.WaitForExit(5000) | Out-Null
}

function Invoke-Run {
    Assert-Install
    if (Test-Path -LiteralPath $DisabledFile) {
        Write-SupervisorLog 'Startup skipped because dictation is disabled.'
        return
    }

    $CreatedNew = $false
    $Mutex = [System.Threading.Mutex]::new($true, 'Local\BreezyLocalStreamingDictationSupervisor', [ref]$CreatedNew)
    if (-not $CreatedNew) {
        Write-SupervisorLog 'Duplicate supervisor invocation exited.'
        $Mutex.Dispose()
        return
    }

    try {
        Set-Content -LiteralPath $PidFile -Encoding ascii -NoNewline -Value $PID
        Remove-Item -LiteralPath $StopFile -Force -ErrorAction SilentlyContinue
        # Only the singleton supervisor may clear session state. A duplicate run must
        # not cancel an intentional pause in the active supervisor.
        Remove-Item -LiteralPath $PauseFile, $ClientFailedFile, $ClientReadyFile -Force -ErrorAction SilentlyContinue
        $Client = Start-Client
        $Hotkey = Start-Hotkey
        $ClientRestarts = [System.Collections.Generic.Queue[datetime]]::new()
        $HotkeyRestarts = [System.Collections.Generic.Queue[datetime]]::new()
        $ClientWasSuppressed = $false

        try {
            while (-not (Test-Path -LiteralPath $StopFile)) {
                Start-Sleep -Seconds 2
                foreach ($Name in @('Client', 'Hotkey')) {
                    if ($Name -eq 'Client') {
                        $Child = $Client
                        $Queue = $ClientRestarts
                        $ClientSuppressed = (
                            (Test-Path -LiteralPath $PauseFile) -or
                            (Test-Path -LiteralPath $ClientFailedFile)
                        )
                        if ($ClientSuppressed) {
                            $ClientWasSuppressed = $true
                            continue
                        }
                        if ($ClientWasSuppressed) {
                            $Queue.Clear()
                            $ClientWasSuppressed = $false
                        }
                    } else {
                        $Child = $Hotkey
                        $Queue = $HotkeyRestarts
                    }
                    if ($null -eq $Child -and $Name -eq 'Hotkey') {
                        throw "$Name process handle is unexpectedly null."
                    }
                    if ($null -ne $Child) {
                        $Child.Refresh()
                        if (-not $Child.HasExited) {
                            continue
                        }
                    }
                    $Now = Get-Date
                    while ($Queue.Count -gt 0 -and ($Now - $Queue.Peek()).TotalMinutes -gt 10) {
                        $null = $Queue.Dequeue()
                    }
                    if ($Queue.Count -ge 5) {
                        if ($Name -eq 'Client') {
                            Set-Content -LiteralPath $ClientFailedFile -Encoding ascii -Value 'failed'
                            Remove-Item -LiteralPath $ClientReadyFile -Force -ErrorAction SilentlyContinue
                            $Client = $null
                            $ClientWasSuppressed = $true
                            Write-SupervisorLog 'Client exceeded five restarts in ten minutes; client stopped while tray remains available.'
                            continue
                        }
                        Write-SupervisorLog 'Hotkey exceeded five restarts in ten minutes; supervisor stopped.'
                        break 2
                    }
                    $Queue.Enqueue($Now)
                    $Delay = [Math]::Min(60, [Math]::Pow(2, $Queue.Count))
                    $ExitCode = if ($null -eq $Child) { 'unavailable' } else { $Child.ExitCode }
                    Write-SupervisorLog "$Name exited with code $ExitCode; restarting after $Delay seconds."
                    Start-Sleep -Seconds $Delay
                    if ($Name -eq 'Client') {
                        if (
                            (Test-Path -LiteralPath $StopFile) -or
                            (Test-Path -LiteralPath $PauseFile) -or
                            (Test-Path -LiteralPath $ClientFailedFile)
                        ) {
                            $ClientWasSuppressed = $true
                            continue
                        }
                        $Client = Start-Client
                    } else {
                        $Hotkey = Start-Hotkey
                    }
                }
            }
        }
        catch {
            Write-SupervisorLog "Supervisor monitor failure: $($_.Exception.ToString())"
            throw
        }
    }
    finally {
        Write-SupervisorLog 'Stopping supervised components.'
        if ($null -ne $Hotkey) {
            Stop-OwnedProcess -Process $Hotkey -ExpectedPath $AutoHotkey
        }
        if ($null -ne $Client) {
            try {
                Initialize-EnginePath
                & $ClientExe stop 2>&1 | ForEach-Object { Write-SupervisorLog "Client stop: $_" }
            }
            catch {
                Write-SupervisorLog "Client stop command failed: $($_.Exception.Message)"
            }
            if (-not $Client.WaitForExit(5000)) {
                $ActualPath = $Client.MainModule.FileName
                if ([string]::Equals($ActualPath, $Pythonw, [System.StringComparison]::OrdinalIgnoreCase)) {
                    Stop-Process -Id $Client.Id
                } else {
                    Write-SupervisorLog "Refused fallback stop for client PID $($Client.Id): unexpected path $ActualPath"
                }
            }
        }
        Remove-Item -LiteralPath $StopFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ClientReadyFile -Force -ErrorAction SilentlyContinue
        $Mutex.ReleaseMutex()
        $Mutex.Dispose()
    }
}

function Register-StartupTask {
    Assert-Install
    $User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$HiddenLauncher`""
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Starts local dictation and its activation hotkey.' -Force | Out-Null
    Write-Output "Installed scheduled task: $TaskName"
}

function Start-Supervisor {
    if (Test-Path -LiteralPath $DisabledFile) {
        throw "Dictation is disabled. Run '$ScriptPath enable' first."
    }
    $Existing = Get-LiveSupervisorPid
    if ($null -ne $Existing) {
        Write-Output "Already running (supervisor PID $Existing)."
        return
    }
    Start-Process -FilePath 'wscript.exe' -ArgumentList @($HiddenLauncher) -WindowStyle Hidden | Out-Null
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        Start-Sleep -Milliseconds 500
        $Existing = Get-LiveSupervisorPid
        if ($null -ne $Existing) {
            Write-Output "Started (supervisor PID $Existing)."
            return
        }
    }
    throw "Supervisor did not start; inspect $SupervisorLog"
}

function Stop-Supervisor {
    $Existing = Get-LiveSupervisorPid
    if ($null -eq $Existing) {
        Write-Output 'Already stopped.'
        return
    }
    Set-Content -LiteralPath $StopFile -Encoding ascii -Value 'stop'
    for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
        Start-Sleep -Milliseconds 500
        if ($null -eq (Get-LiveSupervisorPid)) {
            Write-Output 'Stopped.'
            return
        }
    }
    throw "Supervisor PID $Existing did not stop within ten seconds."
}

function Show-Status {
    $Existing = Get-LiveSupervisorPid
    $Task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
    $TaskState = if ($null -eq $Task) { 'not installed' } else { $Task.State }
    $Enabled = -not (Test-Path -LiteralPath $DisabledFile)
    Write-Output "Supervisor: $(if ($null -eq $Existing) { 'stopped' } else { "running (PID $Existing)" })"
    Write-Output "Startup task: $TaskState"
    Write-Output "Enabled: $Enabled"
    Write-Output "Client paused: $(Test-Path -LiteralPath $PauseFile)"
    Write-Output "Client failed: $(Test-Path -LiteralPath $ClientFailedFile)"
    Write-Output "Client state: $(if (Test-Path -LiteralPath $ClientFailedFile) { 'failed' } elseif (Test-Path -LiteralPath $ClientReadyFile) { 'ready' } else { 'loading' })"
    Write-Output "Config: $Config"
    Write-Output "Log: $SupervisorLog"
    & $ClientExe status
}

function Pause-Client {
    Assert-Install
    if ($null -eq (Get-LiveSupervisorPid)) {
        throw 'Cannot pause client because the supervisor is not running.'
    }
    $Stream = [System.IO.File]::Open(
        $PauseFile,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::Read
    )
    $Stream.Dispose()
    Remove-Item -LiteralPath $ClientFailedFile -Force -ErrorAction SilentlyContinue

    $Process = Get-SupervisedClientProcess
    if ($null -eq $Process) {
        Write-Output 'Client already paused.'
        return
    }
    Initialize-EnginePath
    try {
        & $ClientExe stop 2>&1 | ForEach-Object { Write-SupervisorLog "Pause client stop: $_" }
    }
    catch {
        Write-SupervisorLog "Pause client stop command failed: $($_.Exception.Message)"
    }
    for ($Attempt = 0; $Attempt -lt 4; $Attempt++) {
        $Process.Refresh()
        if ($Process.HasExited) {
            Write-Output 'Client paused; tray remains available.'
            return
        }
        Start-Sleep -Milliseconds 500
    }
    Stop-Process -Id $Process.Id
    $Process.WaitForExit(5000) | Out-Null
    Write-Output 'Client paused; tray remains available.'
}

function Resume-Client {
    Remove-Item -LiteralPath $PauseFile, $ClientFailedFile -Force -ErrorAction SilentlyContinue
    if ($null -eq (Get-LiveSupervisorPid)) {
        Start-Supervisor
    }
    Write-Output 'Client resume requested.'
}

function Set-Microphone {
    if (-not $DeviceSpecified) {
        throw 'set-microphone requires -Device <index>.'
    }
    $Text = Get-Content -LiteralPath $Config -Raw -Encoding utf8
    $DevicePattern = '(?ms)(^\[audio\].*?^device\s*=\s*)[^\r\n]+'
    if (-not [regex]::IsMatch($Text, $DevicePattern)) {
        throw 'Could not locate [audio] device in config.toml.'
    }
    $Updated = [regex]::Replace($Text, $DevicePattern, "`${1}$Device", 1)
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Config, $Updated, $Utf8NoBom)
    Write-Output "Microphone device set to index $Device. Restart dictation to apply."
}

function Test-HotkeyHealth {
    if ([string]::IsNullOrWhiteSpace($Hotkey)) { return $false }
    if ($null -eq (Get-LiveSupervisorPid)) { return $false }
    if (-not (Test-Path -LiteralPath $HotkeyReadyFile -PathType Leaf)) { return $false }
    if ((Get-Content -LiteralPath $HotkeyReadyFile -Raw -Encoding utf8).Trim() -ne $Hotkey) { return $false }
    $status = (& $ClientExe status 2>&1) -join "`n"
    return $status -match 'Status:\s+running\s+\(PID\s+\d+\)'
}

switch ($Command) {
    'run' { Invoke-Run }
    'start' { Start-Supervisor }
    'stop' { Stop-Supervisor }
    'restart' { Stop-Supervisor; Start-Supervisor }
    'status' { Show-Status }
    'disable' {
        Set-Content -LiteralPath $DisabledFile -Encoding ascii -Value 'disabled'
        Stop-Supervisor
        Write-Output 'Disabled.'
    }
    'enable' {
        Remove-Item -LiteralPath $DisabledFile -Force -ErrorAction SilentlyContinue
        Start-Supervisor
        Write-Output 'Enabled.'
    }
    'pause-client' { Pause-Client }
    'resume-client' { Resume-Client }
    'install-task' { Register-StartupTask }
    'remove-task' {
        Stop-Supervisor
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false -ErrorAction SilentlyContinue
        Write-Output "Removed scheduled task: $TaskName"
    }
    'devices' { & $ClientExe devices }
    'tray-devices' { & $Python $MicrophoneLister }
    'set-microphone' { Set-Microphone }
    'hotkey-health' { if (Test-HotkeyHealth) { exit 0 } else { exit 1 } }
    'switch-microphone' {
        Set-Microphone
        Stop-Supervisor
        Start-Supervisor
    }
}
