param([switch]$Start)

$ErrorActionPreference = 'Stop'
$studioUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$studioPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$studioPhotoCraft = (Get-Command photocraft-cli.exe -CommandType Application).Source
$studioPhotoCraftDir = Split-Path -Parent $studioPhotoCraft
if (-not (Test-Path -LiteralPath $studioPython -PathType Leaf)) {
    throw 'The project Python virtual environment is missing.'
}
$basePython = & $studioPython -B -c 'import sys; print(sys._base_executable)'
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the project Python interpreter.' }
$studioPythonw = Join-Path (Split-Path -Parent $basePython) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $studioPythonw -PathType Leaf)) {
    throw 'The windowless Python interpreter is missing.'
}

$principal = New-ScheduledTaskPrincipal -UserId $studioUser -LogonType Interactive -RunLevel Limited
$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn -User $studioUser
    # A forced process exit can leave the task Ready without scheduling a
    # failure retry. A repeating trigger also covers that case.
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$description = 'Managed by thumbnailfac install_studio_startup.ps1'
$launcher = Join-Path $PSScriptRoot 'start_studio_service.pyw'

foreach ($service in @(
    @{Name = 'Thumbnail Studio MCP'; Script = 'server.py'; Port = 8000},
    @{Name = 'Thumbnail Studio Portal'; Script = 'portal.py'; Port = 8001}
)) {
    $existing = Get-ScheduledTask -TaskName $service.Name -TaskPath '\' -ErrorAction SilentlyContinue
    if ($existing -and $existing.Description -ne $description) {
        throw "An unrelated task already uses the name $($service.Name)."
    }
    $arguments = '-B -S "{0}" "{1}" "{2}"' -f $launcher, $service.Script, $studioPhotoCraftDir
    $action = New-ScheduledTaskAction -Execute $studioPythonw -Argument $arguments -WorkingDirectory $PSScriptRoot
    Register-ScheduledTask -TaskName $service.Name -TaskPath '\' -Description $description `
        -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
    if ($Start -and -not (Get-NetTCPConnection -LocalPort $service.Port -State Listen -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName $service.Name -TaskPath '\'
    }
    Write-Output "Registered $($service.Name) for interactive Windows sign-in."
}
