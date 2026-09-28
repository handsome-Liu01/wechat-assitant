param(
    [ValidateSet("wechat-data-analysis", "plus", "free")]
    [string]$Edition = "wechat-data-analysis",
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

& $PythonExecutable -m venv .venv
& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install --upgrade pip
if ($Edition -eq "wechat-data-analysis") {
    & "$ProjectRoot\.venv\Scripts\python.exe" -m pip install -e "."
} else {
    $Extra = if ($Edition -eq "plus") { "wechat-plus" } else { "wechat-free" }
    & "$ProjectRoot\.venv\Scripts\python.exe" -m pip install -e ".[$Extra]"
}

if (-not (Test-Path -LiteralPath "$ProjectRoot\config.yaml")) {
    Copy-Item -LiteralPath "$ProjectRoot\config.example.yaml" -Destination "$ProjectRoot\config.yaml"
}

Write-Host "Setup completed. Edit config.yaml, set LLM_API_KEY, then run:"
Write-Host ".\.venv\Scripts\wechat-digest.exe doctor"

