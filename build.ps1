$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$distDir = Join-Path $repoRoot "dist"
$packageName = "TradingBridge-FileIO"
$packageDir = Join-Path $repoRoot $packageName
$zipPath = Join-Path $repoRoot "$packageName.zip"

# Clean old outputs
if (Test-Path $distDir) { Remove-Item $distDir -Recurse -Force }
if (Test-Path $packageDir) { Remove-Item $packageDir -Recurse -Force }
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

# Create virtual environment
$venvDir = Join-Path $repoRoot ".venv"
if (-not (Test-Path $venvDir)) {
    python -m venv $venvDir
}
& (Join-Path $venvDir "Scripts\python.exe") -m pip install --upgrade pip
& (Join-Path $venvDir "Scripts\python.exe") -m pip install -r (Join-Path $repoRoot "Trading_Bridge_Source\requirements.txt")

# Build EXE
& (Join-Path $venvDir "Scripts\pyinstaller.exe") `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name Trading_Bridge `
    (Join-Path $repoRoot "Trading_Bridge_Source\bridge.py")

# Package files
New-Item -ItemType Directory -Force -Path $packageDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $packageDir "Trading_Bridge_Source") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $packageDir "Trading_Bot") | Out-Null

Copy-Item (Join-Path $distDir "Trading_Bridge.exe") (Join-Path $packageDir "Trading_Bridge.exe")
Copy-Item (Join-Path $repoRoot "Trading_Bridge_Source\bridge.py") (Join-Path $packageDir "Trading_Bridge_Source\bridge.py")
Copy-Item (Join-Path $repoRoot "Trading_Bridge_Source\requirements.txt") (Join-Path $packageDir "Trading_Bridge_Source\requirements.txt")
Copy-Item (Join-Path $repoRoot "README.md") (Join-Path $packageDir "README.md")
Copy-Item (Join-Path $repoRoot "FILE_IO_GUIDE.md") (Join-Path $packageDir "FILE_IO_GUIDE.md")
Copy-Item (Join-Path $repoRoot "DISCLAIMER.md") (Join-Path $packageDir "DISCLAIMER.md")
Copy-Item (Join-Path $repoRoot "LICENSE") (Join-Path $packageDir "LICENSE")

Get-ChildItem (Join-Path $repoRoot "Trading_Bot") -Recurse | Copy-Item -Destination (Join-Path $packageDir "Trading_Bot") -Recurse

Compress-Archive -Path (Join-Path $packageDir "*") -DestinationPath $zipPath -Force

Write-Host ""
Write-Host "Build complete:"
Write-Host $zipPath
Write-Host ""
Write-Host "Install instructions:"
Write-Host "1. Unzip the package"
Write-Host "2. Run Trading_Bridge.exe"
Write-Host "3. Set the signals folder like C:\Trades\signals"
Write-Host "4. Drop JSON files into that folder"
