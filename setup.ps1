param([switch]$InstallSegmentation)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    py -3.15 -m venv (Join-Path $root '.venv')
}
& $python -m pip install -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }

$secretFile = Join-Path $root 'secrets.json'
if (-not (Test-Path -LiteralPath $secretFile -PathType Leaf)) {
    $script = @'
import json, secrets, sys
with open(sys.argv[1], 'x', encoding='utf-8') as file:
    json.dump({'mcp_token': secrets.token_urlsafe(48),
               'portal_password': secrets.token_urlsafe(32)}, file)
'@
    & $python -c $script $secretFile
    if ($LASTEXITCODE -ne 0) { throw 'Could not create local credentials.' }
    Write-Output 'Created local secrets.json. Its values were not displayed.'
}

$networkFile = Join-Path $root 'studio_network.json'
if (-not (Test-Path -LiteralPath $networkFile -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $root 'studio_network.example.json') -Destination $networkFile
    Write-Output 'Created loopback-only network settings. Edit studio_network.json for a private remote address.'
}

if ($InstallSegmentation) {
    & (Join-Path $root 'install_segmentation.ps1')
}

Write-Output 'Setup complete. Install PhotoCraft and ensure photocraft-cli.exe is on PATH before starting the services.'
