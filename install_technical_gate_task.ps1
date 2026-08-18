param(
    [string]$TaskName = "MT5 Technical Gate Hourly",
    [int]$Minute = 5
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$publisher = Join-Path $projectRoot "publish_technical_gate.py"
$python = (& python -c "import sys; print(sys.executable)").Trim()
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python interpreter missing: $python"
}
if (-not (Test-Path -LiteralPath $publisher -PathType Leaf)) {
    throw "Publisher missing: $publisher"
}

$now = Get-Date
$firstRun = Get-Date -Hour $now.Hour -Minute $Minute -Second 0
if ($firstRun -le $now) {
    $firstRun = $firstRun.AddHours(1)
}
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument ('-X utf8 -u "{0}"' -f $publisher) `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $firstRun `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Generate IBKR H1 Technical Gate before the analysis feed and publish material changes." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
