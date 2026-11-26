$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $ProjectRoot '.segmentation-venv\Scripts\python.exe'

& py -3.14 -c 'import sys; assert sys.version_info[:2] == (3, 14)'
if ($LASTEXITCODE -ne 0) {
    throw 'Python 3.14 is required for the isolated segmentation runtime.'
}
if (-not (Test-Path -LiteralPath $Runtime)) {
    & py -3.14 -m venv (Join-Path $ProjectRoot '.segmentation-venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated segmentation runtime.' }
}
& $Runtime -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot 'requirements-segmentation.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install the local segmentation packages.' }
$env:REMBG_HOME = Join-Path $ProjectRoot 'Models\rembg'
$env:U2NET_HOME = $env:REMBG_HOME
$env:OMP_NUM_THREADS = '4'
Write-Output 'Downloading the general and anime models to this PC (first setup can take several minutes).'
& $Runtime -c "from rembg import new_session; new_session('isnet-general-use'); new_session('isnet-anime')"
if ($LASTEXITCODE -ne 0) { throw 'Could not download the local segmentation models.' }
Write-Output 'Local general and anime cutout models are ready.'
