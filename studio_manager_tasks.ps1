param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Status', 'Start', 'Stop', 'Restart', 'Enable', 'Disable')]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [ValidateSet('MCP', 'Portal')]
    [string]$Service
)

$ErrorActionPreference = 'Stop'
$taskName = if ($Service -eq 'MCP') { 'Thumbnail Studio MCP' } else { 'Thumbnail Studio Portal' }
$expectedDescription = 'Managed by thumbnailfac install_studio_startup.ps1'
$task = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if (-not $task -or $task.Description -ne $expectedDescription) {
    throw "The expected managed task '$taskName' was not found. No task was changed."
}

switch ($Action) {
    'Enable' { Enable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null }
    'Disable' {
        if ($task.State -eq 'Running') {
            Stop-ScheduledTask -TaskName $taskName -TaskPath '\'
            $limit = (Get-Date).AddSeconds(10)
            do {
                Start-Sleep -Milliseconds 250
                $task = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
            } while ($task.State -eq 'Running' -and (Get-Date) -lt $limit)
            if ($task.State -eq 'Running') { throw "Windows did not stop '$taskName' in time." }
        }
        Disable-ScheduledTask -TaskName $taskName -TaskPath '\'
    }
    'Start' {
        if ($task.State -eq 'Disabled') { Enable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null }
        Start-ScheduledTask -TaskName $taskName -TaskPath '\'
    }
    'Stop' {
        if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $taskName -TaskPath '\' }
        Disable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null
    }
    'Restart' {
        if ($task.State -eq 'Running') {
            Stop-ScheduledTask -TaskName $taskName -TaskPath '\'
            $limit = (Get-Date).AddSeconds(10)
            do {
                Start-Sleep -Milliseconds 250
                $task = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
            } while ($task.State -eq 'Running' -and (Get-Date) -lt $limit)
            if ($task.State -eq 'Running') { throw "Windows did not stop '$taskName' in time." }
        }
        if ($task.State -eq 'Disabled') { Enable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null }
        Start-ScheduledTask -TaskName $taskName -TaskPath '\'
    }
}

$task = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
$info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\'
[pscustomobject]@{
    service = $Service
    task_state = [string]$task.State
    enabled = [bool]$task.Settings.Enabled
    last_run = $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss')
    last_result = $info.LastTaskResult
} | ConvertTo-Json -Compress
