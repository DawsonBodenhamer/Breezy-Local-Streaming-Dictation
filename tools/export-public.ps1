[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Destination
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if ($destinationPath.StartsWith($source + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'The public export must be outside the private source repository.'
}
if (Test-Path -LiteralPath $destinationPath) {
    if ((Get-ChildItem -LiteralPath $destinationPath -Force | Select-Object -First 1)) {
        throw 'The public export destination must be empty.'
    }
} else {
    New-Item -ItemType Directory -Path $destinationPath | Out-Null
}

$rootFiles = @(
    '.gitignore', 'README.md', 'CHANGELOG.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'pyproject.toml', 'requirements.in', 'requirements.lock',
    'requirements.cuda.in', 'requirements.cuda.lock', 'setup.ps1'
)
$directoryNames = @('assets', 'config', 'docs', 'licenses', 'src', 'windows')

foreach ($relative in $rootFiles) {
    $sourcePath = Join-Path $source $relative
    $targetPath = Join-Path $destinationPath $relative
    [System.IO.File]::Copy($sourcePath, $targetPath, $true)
}
foreach ($relative in $directoryNames) {
    Copy-Item -LiteralPath (Join-Path $source $relative) -Destination $destinationPath -Recurse -Force
}
Get-ChildItem -LiteralPath $destinationPath -Recurse -Directory -Filter '__pycache__' -Force |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $destinationPath -Recurse -Directory -Filter '*.egg-info' -Force |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $destinationPath -Recurse -File -Force |
    Where-Object { $_.Extension.ToLowerInvariant() -in @('.pyc', '.pyo') } |
    Remove-Item -Force
New-Item -ItemType Directory -Path (Join-Path $destinationPath 'tools') | Out-Null
Copy-Item -LiteralPath (Join-Path $source 'tools\verify-public-tree.ps1') -Destination (Join-Path $destinationPath 'tools\verify-public-tree.ps1')
Copy-Item -LiteralPath (Join-Path $source 'tools\export-public.ps1') -Destination (Join-Path $destinationPath 'tools\export-public.ps1')

& (Join-Path $destinationPath 'tools\verify-public-tree.ps1') -Root $destinationPath
Write-Host "Public export created at $destinationPath" -ForegroundColor Green
