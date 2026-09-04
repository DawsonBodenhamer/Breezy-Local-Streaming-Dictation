[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OldRuntime,
    [Parameter(Mandatory)][string]$NewRuntime,
    [int]$Version = 1,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$OldRuntime = [System.IO.Path]::GetFullPath($OldRuntime)
$NewRuntime = [System.IO.Path]::GetFullPath($NewRuntime)
$ProtectedFiles = @('config.toml', 'text_conversions.json', 'runtime.env')
$PayloadDirectories = @('logs', 'backups', 'models', 'manifests')
$TombstoneName = 'migration.tombstone.json'
$LegacyTaskName = 'Breezy Local Streaming Dictation'
$NewTaskName = 'Breezy Dictation'
$script:LegacyStartupTaskWasPresent = $false
$script:NewStartupTaskWasTouched = $false
$EnvironmentAliases = @{
    'BREEZY_LOCAL_STREAMING_DICTATION_AUTOHOTKEY' = 'BREEZY_DICTATION_AUTOHOTKEY'
    'BREEZY_LOCAL_STREAMING_DICTATION_HOTKEY' = 'BREEZY_DICTATION_HOTKEY'
    'BREEZY_LOCAL_DICTATION_CONFIG_FILE' = 'BREEZY_DICTATION_CONFIG_FILE'
    'BREEZY_LOCAL_DICTATION_READY_FILE' = 'BREEZY_DICTATION_READY_FILE'
}

function Get-FileFingerprint {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '<missing>' }
    $item = Get-Item -LiteralPath $Path
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = [System.BitConverter]::ToString($sha.ComputeHash([System.IO.File]::ReadAllBytes($Path))).Replace('-', '')
    } finally {
        $sha.Dispose()
    }
    return '{0}:{1}' -f $item.Length, $digest
}

function Normalize-RuntimePaths {
    param([Parameter(Mandatory)][string]$Text)
    $oldForward = $OldRuntime.Replace('\', '/')
    $newForward = $NewRuntime.Replace('\', '/')
    $oldEscaped = $OldRuntime.Replace('\', '\\')
    $newEscaped = $NewRuntime.Replace('\', '\\')
    return $Text.Replace($OldRuntime, $NewRuntime).Replace($oldForward, $newForward).Replace($oldEscaped, $newEscaped)
}

function Get-CanonicalRuntimeEnvironment {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $lines = @(Get-Content -LiteralPath $Path -Encoding utf8)
    $canonicalNames = @{}
    foreach ($line in $lines) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$' -and -not $EnvironmentAliases.ContainsKey($Matches[1])) {
            $canonicalNames[$Matches[1]] = $true
        }
    }
    $seenNames = @{}
    $canonicalLines = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $canonicalLines.Add($line)
            continue
        }
        $name = $Matches[1]
        $value = $Matches[2]
        $canonicalName = if ($EnvironmentAliases.ContainsKey($name)) { $EnvironmentAliases[$name] } else { $name }
        if ($EnvironmentAliases.ContainsKey($name) -and $canonicalNames.ContainsKey($canonicalName)) { continue }
        if ($seenNames.ContainsKey($canonicalName)) { continue }
        $seenNames[$canonicalName] = $true
        $canonicalLines.Add("$canonicalName=$value")
    }
    return (Normalize-RuntimePaths -Text (($canonicalLines -join "`n") + "`n"))
}

function Normalize-RuntimeEnvironment {
    param([Parameter(Mandatory)][string]$Path)
    $canonical = Get-CanonicalRuntimeEnvironment -Path $Path
    if ($null -eq $canonical) { return }
    $current = [System.IO.File]::ReadAllText($Path)
    if ($current -eq $canonical) { return }
    [System.IO.File]::WriteAllText(
        $Path,
        $canonical,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Get-ProtectedFingerprint {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Name
    )
    if ($Name -eq 'runtime.env') {
        $canonical = Get-CanonicalRuntimeEnvironment -Path $Path
        if ($null -eq $canonical) { return '<missing>' }
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($canonical)
            $digest = [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '')
        } finally {
            $sha.Dispose()
        }
        return '{0}:{1}' -f $bytes.Length, $digest
    }
    if ($Name -eq 'config.toml' -and (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $canonical = Normalize-RuntimePaths -Text ([System.IO.File]::ReadAllText($Path))
        $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($canonical)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $digest = [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '')
        } finally {
            $sha.Dispose()
        }
        return '{0}:{1}' -f $bytes.Length, $digest
    }
    return Get-FileFingerprint -Path $Path
}

function Get-ProtectedManifest {
    param([Parameter(Mandatory)][string]$Root)
    $manifest = [ordered]@{}
    foreach ($name in $ProtectedFiles) {
        $manifest[$name] = Get-ProtectedFingerprint -Path (Join-Path $Root $name) -Name $name
    }
    return $manifest
}

function Assert-ProtectedStateEquivalent {
    param(
        [Parameter(Mandatory)][string]$Left,
        [Parameter(Mandatory)][string]$Right
    )
    foreach ($name in $ProtectedFiles) {
        $leftPath = Join-Path $Left $name
        $rightPath = Join-Path $Right $name
        $leftFingerprint = Get-ProtectedFingerprint -Path $leftPath -Name $name
        $rightFingerprint = Get-ProtectedFingerprint -Path $rightPath -Name $name
        if ($leftFingerprint -ne '<missing>' -and $rightFingerprint -ne '<missing>' -and $leftFingerprint -ne $rightFingerprint) {
            throw "Protected runtime state diverges for $name. Both runtime trees were preserved."
        }
    }
}

function Copy-FilePreservingDestination {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
        return
    }
    if ((Get-FileFingerprint -Path $Source) -eq (Get-FileFingerprint -Path $Destination)) {
        return
    }
    $hash = (Get-FileFingerprint -Path $Source).Split(':')[1].Substring(0, 12).ToLowerInvariant()
    $legacy = "$Destination.legacy.$hash"
    if (-not (Test-Path -LiteralPath $legacy -PathType Leaf)) {
        Copy-Item -LiteralPath $Source -Destination $legacy -Force
    }
}

function Copy-LegacyFiles {
    param(
        [Parameter(Mandatory)][string]$SourceRoot,
        [Parameter(Mandatory)][string]$DestinationRoot
    )
    foreach ($file in Get-ChildItem -LiteralPath $SourceRoot -Recurse -File -Force) {
        $relative = $file.FullName.Substring($SourceRoot.Length + 1)
        $parts = $relative -split '[\\/]'
        if ($parts -contains '.venv' -or $relative -eq $TombstoneName) { continue }
        $destination = Join-Path $DestinationRoot $relative
        if ($ProtectedFiles -contains $relative -and (Test-Path -LiteralPath $destination -PathType Leaf)) { continue }
        Copy-FilePreservingDestination -Source $file.FullName -Destination $destination
    }
}

function Get-MigrationStagingPaths {
    $parent = Split-Path -Parent $OldRuntime
    $leaf = Split-Path -Leaf $OldRuntime
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { return @() }
    return @(Get-ChildItem -LiteralPath $parent -Directory -Force |
        Where-Object { $_.Name -like "$leaf.migration-staging.*" })
}

function Test-StartupTaskPresent {
    param([Parameter(Mandatory)][string]$TaskName)
    return $null -ne (Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue)
}

function Register-StartupTaskForRoot {
    param(
        [Parameter(Mandatory)][string]$TaskName,
        [Parameter(Mandatory)][string]$Root
    )
    $supervisor = Join-Path $Root 'supervisor.ps1'
    $startupLauncher = Join-Path $Root 'startup_hidden.vbs'
    if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
        throw "Cannot register startup task $TaskName because its supervisor is missing: $supervisor"
    }
    if (-not (Test-Path -LiteralPath $startupLauncher -PathType Leaf)) {
        throw "Cannot register startup task $TaskName because its windowless launcher is missing: $startupLauncher"
    }
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $arguments = '"{0}"' -f $startupLauncher
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Starts local dictation and its activation hotkey.' -Force | Out-Null
}

function Remove-StartupTask {
    param([Parameter(Mandatory)][string]$TaskName)
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false -ErrorAction SilentlyContinue
}

function Restore-StartupAuthority {
    $errors = [System.Collections.Generic.List[string]]::new()
    if ($script:NewStartupTaskWasTouched) {
        try {
            Remove-StartupTask -TaskName $NewTaskName
        } catch {
            [void]$errors.Add("new task removal failed: $($_.Exception.Message)")
        }
    }
    if ($script:LegacyStartupTaskWasPresent) {
        try {
            Register-StartupTaskForRoot -TaskName $LegacyTaskName -Root $OldRuntime
        } catch {
            [void]$errors.Add("legacy task restoration failed: $($_.Exception.Message)")
        }
    }
    if ($errors.Count -gt 0) {
        throw "Startup authority rollback failed: $($errors -join '; ')"
    }
}

function Stop-LegacyRuntime {
    param([Parameter(Mandatory)][string]$Root)
    $script:LegacyStartupTaskWasPresent = Test-StartupTaskPresent -TaskName $LegacyTaskName
    $supervisor = Join-Path $Root 'supervisor.ps1'
    if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
        if ($script:LegacyStartupTaskWasPresent) {
            throw "Legacy startup task exists but its supervisor is missing: $supervisor"
        }
        return
    }
    if ($DryRun) {
        if ($script:LegacyStartupTaskWasPresent) {
            Write-Output "Would stop $Root and transfer its logon startup task to $NewRuntime"
        } else {
            Write-Output "Would stop and detach the legacy runtime at $Root without changing logon startup"
        }
        return
    }
    & $supervisor stop
    & $supervisor remove-task
}

function Read-Tombstone {
    param([Parameter(Mandatory)][string]$Root)
    $path = Join-Path $Root $TombstoneName
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try {
        $value = Get-Content -LiteralPath $path -Raw -Encoding utf8 | ConvertFrom-Json
    } catch {
        throw "Legacy runtime tombstone is malformed: $path"
    }
    if ([int]$value.version -ne $Version -or [string]::IsNullOrWhiteSpace([string]$value.target)) {
        throw "Legacy runtime tombstone has an unsupported version or target: $path"
    }
    return $value
}

function Write-Tombstone {
    param([Parameter(Mandatory)][string]$Root)
    $manifest = Get-ProtectedManifest -Root $NewRuntime
    $payload = [ordered]@{
        schema = 'breezy-dictation-runtime-migration'
        version = $Version
        target = $NewRuntime
        migrated_at_utc = [datetime]::UtcNow.ToString('o')
        protected_manifest = $manifest
    } | ConvertTo-Json -Depth 5
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    [System.IO.File]::WriteAllText(
        (Join-Path $Root $TombstoneName),
        $payload + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Invoke-Migration {
    if ($OldRuntime -eq $NewRuntime) { throw 'Old and new runtime paths must differ.' }
    $stagingPaths = Get-MigrationStagingPaths
    if ($stagingPaths.Count -gt 0) {
        $paths = ($stagingPaths | ForEach-Object FullName) -join ', '
        throw "An interrupted migration staging tree exists; both authority paths were preserved for recovery: $paths"
    }
    $oldExists = Test-Path -LiteralPath $OldRuntime -PathType Container
    $newExists = Test-Path -LiteralPath $NewRuntime -PathType Container

    if ($oldExists) {
        $tombstone = Read-Tombstone -Root $OldRuntime
        if ($null -ne $tombstone) {
            if ([System.IO.Path]::GetFullPath([string]$tombstone.target) -ne $NewRuntime) {
                throw "Legacy runtime tombstone points to a different authority: $($tombstone.target)"
            }
            if (-not $newExists) { throw 'Legacy runtime tombstone exists but its new authority is missing.' }
            Write-Output "Legacy runtime already migrated to $NewRuntime"
            return
        }
    }

    if (-not $oldExists) {
        if ($newExists) {
            Write-Output "New Breezy Dictation runtime is already authoritative: $NewRuntime"
        } else {
            Write-Output 'No legacy or new runtime exists; setup will create the new authority.'
        }
        return
    }

    if ($newExists) {
        Assert-ProtectedStateEquivalent -Left $OldRuntime -Right $NewRuntime
    } else {
        if ($DryRun) {
            Write-Output "Would create $NewRuntime and preserve protected state from $OldRuntime"
        } else {
            New-Item -ItemType Directory -Force -Path $NewRuntime | Out-Null
        }
    }

    if (-not $DryRun) {
        $staging = "$OldRuntime.migration-staging.$PID"
        try {
            Stop-LegacyRuntime -Root $OldRuntime
            Copy-LegacyFiles -SourceRoot $OldRuntime -DestinationRoot $NewRuntime
            Normalize-RuntimeEnvironment -Path (Join-Path $NewRuntime 'runtime.env')
            $newConfig = Join-Path $NewRuntime 'config.toml'
            if (Test-Path -LiteralPath $newConfig -PathType Leaf) {
                $configText = Normalize-RuntimePaths -Text ([System.IO.File]::ReadAllText($newConfig))
                [System.IO.File]::WriteAllText($newConfig, $configText, [System.Text.UTF8Encoding]::new($false))
            }
            Assert-ProtectedStateEquivalent -Left $OldRuntime -Right $NewRuntime
            if ($script:LegacyStartupTaskWasPresent) {
                $script:NewStartupTaskWasTouched = $true
                Register-StartupTaskForRoot -TaskName $NewTaskName -Root $NewRuntime
                Write-Output "Transferred logon startup task to $NewTaskName"
            }
            if (Test-Path -LiteralPath $staging) { throw "Migration staging path already exists: $staging" }
            Move-Item -LiteralPath $OldRuntime -Destination $staging
            try {
                Write-Tombstone -Root $OldRuntime
                Remove-Item -LiteralPath $staging -Recurse -Force
            } catch {
                if (Test-Path -LiteralPath $OldRuntime) { Remove-Item -LiteralPath $OldRuntime -Recurse -Force }
                if (Test-Path -LiteralPath $staging) { Move-Item -LiteralPath $staging -Destination $OldRuntime }
                throw
            }
        } catch {
            try {
                Restore-StartupAuthority
            } catch {
                throw "Migration failed and startup authority rollback failed: $($_.Exception.Message)"
            }
            throw
        }
    }
    Write-Output "Migrated $OldRuntime to $NewRuntime with a versioned tombstone."
}

Invoke-Migration
