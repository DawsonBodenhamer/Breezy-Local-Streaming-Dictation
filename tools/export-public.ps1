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

$files = @(
    '.gitignore', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'pyproject.toml', 'requirements.in', 'requirements.lock',
    'requirements.cuda.in', 'requirements.cuda.lock', 'setup.ps1'
)
$directories = @('assets', 'config', 'docs', 'licenses', 'src', 'windows')

foreach ($relative in $files) {
    Copy-Item -LiteralPath (Join-Path $source $relative) -Destination (Join-Path $destinationPath $relative)
}
foreach ($relative in $directories) {
    Copy-Item -LiteralPath (Join-Path $source $relative) -Destination (Join-Path $destinationPath $relative) -Recurse
}
Get-ChildItem -LiteralPath $destinationPath -Recurse -Directory -Filter '__pycache__' -Force |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $destinationPath -Recurse -Directory -Filter '*.egg-info' -Force |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $destinationPath -Recurse -File -Include '*.pyc', '*.pyo' -Force |
    Remove-Item -Force
New-Item -ItemType Directory -Path (Join-Path $destinationPath 'tools') | Out-Null
Copy-Item -LiteralPath (Join-Path $source 'tools\verify-public-tree.ps1') -Destination (Join-Path $destinationPath 'tools\verify-public-tree.ps1')
Copy-Item -LiteralPath (Join-Path $source 'tools\export-public.ps1') -Destination (Join-Path $destinationPath 'tools\export-public.ps1')

& (Join-Path $destinationPath 'tools\verify-public-tree.ps1') -Root $destinationPath
Write-Host "Public export created at $destinationPath" -ForegroundColor Green
