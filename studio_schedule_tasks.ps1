param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Install', 'Remove')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$scheduleTaskName = 'Thumbnail Studio Daily Schedule'
$scheduleDescription = 'Managed by thumbnailfac install_studio_manager.ps1'
$serviceDescription = 'Managed by thumbnailfac install_studio_startup.ps1'
$manager = Join-Path $root 'thumbnail_studio_manager.pyw'
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$scheduleFile = Join-Path $root 'studio_schedule.json'
$serviceNames = @('Thumbnail Studio MCP', 'Thumbnail Studio Portal')

foreach ($serviceName in $serviceNames) {
    $serviceTask = Get-ScheduledTask -TaskName $serviceName -TaskPath '\' -ErrorAction SilentlyContinue
    if (-not $serviceTask -or $serviceTask.Description -ne $serviceDescription) {
        throw "The expected managed task '$serviceName' was not found. No schedule was changed."
    }
}

$existing = Get-ScheduledTask -TaskName $scheduleTaskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -ne $scheduleDescription) {
    throw "An unrelated task already uses the name '$scheduleTaskName'. No task was changed."
}

if ($Action -eq 'Install') {
    if (-not (Test-Path -LiteralPath $manager -PathType Leaf) -or
        -not (Test-Path -LiteralPath $pythonw -PathType Leaf) -or
        -not (Test-Path -LiteralPath $scheduleFile -PathType Leaf)) {
        throw 'The schedule runner or its settings are missing.'
    }
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $triggers = @(
        New-ScheduledTaskTrigger -AtLogOn -User $user
        New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    )
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $actionSpec = New-ScheduledTaskAction -Execute $pythonw `
        -Argument ('"{0}" --schedule-tick' -f $manager) -WorkingDirectory $root
    Register-ScheduledTask -TaskName $scheduleTaskName -TaskPath '\' -Description $scheduleDescription `
        -Action $actionSpec -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $scheduleTaskName -TaskPath '\'
} else {
    if ($existing -and $existing.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $scheduleTaskName -TaskPath '\'
        $limit = (Get-Date).AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 250
            $existing = Get-ScheduledTask -TaskName $scheduleTaskName -TaskPath '\' -ErrorAction SilentlyContinue
        } while ($existing -and $existing.State -eq 'Running' -and (Get-Date) -lt $limit)
        if ($existing -and $existing.State -eq 'Running') { throw 'Windows did not stop the schedule checker in time.' }
    }
    if ($existing) { Unregister-ScheduledTask -TaskName $scheduleTaskName -TaskPath '\' -Confirm:$false | Out-Null }
    # Restore the normal sign-in/recovery tasks and leave the Studio available now.
    foreach ($serviceName in $serviceNames) {
        Enable-ScheduledTask -TaskName $serviceName -TaskPath '\' | Out-Null
        $serviceTask = Get-ScheduledTask -TaskName $serviceName -TaskPath '\'
        if ($serviceTask.State -ne 'Running') { Start-ScheduledTask -TaskName $serviceName -TaskPath '\' }
    }
}

[pscustomobject]@{ action = $Action; schedule_task = $scheduleTaskName } | ConvertTo-Json -Compress
