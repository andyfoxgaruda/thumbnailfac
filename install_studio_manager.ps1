$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\pythonw.exe'
$manager = Join-Path $root 'thumbnail_studio_manager.pyw'
$taskHelper = Join-Path $root 'studio_manager_tasks.ps1'
foreach ($required in @($python, $manager, $taskHelper)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required Thumbnail Studio manager file is missing: $required"
    }
}

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Thumbnail Studio.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $python
$shortcut.Arguments = ('"{0}"' -f $manager)
$shortcut.WorkingDirectory = $root
$shortcut.Description = 'Open the local Thumbnail Studio control panel'
$shortcut.IconLocation = "$env:SystemRoot\System32\imageres.dll,67"
$shortcut.Save()
Write-Output "Created desktop shortcut: $shortcutPath"
