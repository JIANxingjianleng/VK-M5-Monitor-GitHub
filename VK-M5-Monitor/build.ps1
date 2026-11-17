# VK M5 Monitor build helper (Windows / Python 3.11)
# Usage:  .\build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Installing dependencies..."
python -m pip install -r requirements.txt

Write-Host "Building VK_M5_v24.exe..."
pyinstaller VK_M5_v24.spec --noconfirm

Write-Host "Done: dist\VK_M5_v24.exe"
