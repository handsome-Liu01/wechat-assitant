param(
    [string]$TaskName = "WeChatIssueDigest"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = "$ProjectRoot\.venv\Scripts\pythonw.exe"
$ConfigPath = "$ProjectRoot\config.yaml"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Virtual environment not found. Run scripts\setup.ps1 first."
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "config.yaml not found. Complete the configuration first."
}

$Arguments = "-m wechat_digest --config `"$ConfigPath`" run"
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Create a daily issue digest from a selected WeChat group" -Force
Write-Host "Task $TaskName installed. It will start when the current user logs on."

