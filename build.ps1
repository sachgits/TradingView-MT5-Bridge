$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$sourceDir = Join-Path $repoRoot "Trading_Bridge_Source"
$distDir = Join-Path $repoRoot "dist"
$packageName = "TradingBridge-Universal"
$packageDir = Join-Path $repoRoot $packageName
$zipPath = Join-Path $repoRoot "$packageName.zip"

foreach ($path in @($distDir, $packageDir, $zipPath)) {
    if (Test-Path $path) {
        Remove-Item $path -Recurse -Force
    }
}

$venvDir = Join-Path $repoRoot ".venv"
if (-not (Test-Path $venvDir)) {
    python -m venv $venvDir
}
$python = Join-Path $venvDir "Scripts\python.exe"
$pyinstaller = Join-Path $venvDir "Scripts\pyinstaller.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $sourceDir "requirements.txt")

& $pyinstaller --noconfirm --clean --onefile --windowed `
    --name Trading_Bridge `
    --paths $sourceDir `
    (Join-Path $sourceDir "bridge.py")

New-Item -ItemType Directory -Force -Path $packageDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $packageDir "Trading_Bridge_Source") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $packageDir "Trading_Bot") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $packageDir "examples") | Out-Null

Copy-Item (Join-Path $distDir "Trading_Bridge.exe") (Join-Path $packageDir "Trading_Bridge.exe")
Copy-Item (Join-Path $sourceDir "bridge.py") (Join-Path $packageDir "Trading_Bridge_Source\bridge.py")
Copy-Item (Join-Path $sourceDir "command_schema.py") (Join-Path $packageDir "Trading_Bridge_Source\command_schema.py")
Copy-Item (Join-Path $sourceDir "command_validator.py") (Join-Path $packageDir "Trading_Bridge_Source\command_validator.py")
Copy-Item (Join-Path $sourceDir "legacy_parser.py") (Join-Path $packageDir "Trading_Bridge_Source\legacy_parser.py")
Copy-Item (Join-Path $sourceDir "requirements.txt") (Join-Path $packageDir "Trading_Bridge_Source\requirements.txt")
Copy-Item (Join-Path $sourceDir "schema\trade-command-v2.json") (Join-Path $packageDir "Trading_Bridge_Source\schema-trade-command-v2.json")
Copy-Item (Join-Path $repoRoot "Trading_Bot\Trading_Bot.mq5") (Join-Path $packageDir "Trading_Bot\Trading_Bot.mq5")
Copy-Item (Join-Path $repoRoot "README.md") (Join-Path $packageDir "README.md")
Copy-Item (Join-Path $repoRoot "FILE_IO_GUIDE.md") (Join-Path $packageDir "FILE_IO_GUIDE.md")
Copy-Item (Join-Path $repoRoot "AUDIT_AND_COMMANDS.md") (Join-Path $packageDir "AUDIT_AND_COMMANDS.md")
Copy-Item (Join-Path $repoRoot "INSTALL_AND_TEST_GUIDE.md") (Join-Path $packageDir "INSTALL_AND_TEST_GUIDE.md")
Copy-Item (Join-Path $repoRoot "TEST_MATRIX.md") (Join-Path $packageDir "TEST_MATRIX.md")
Copy-Item (Join-Path $repoRoot "DISCLAIMER.md") (Join-Path $packageDir "DISCLAIMER.md")
Copy-Item (Join-Path $repoRoot "LICENSE") (Join-Path $packageDir "LICENSE")
Copy-Item (Join-Path $repoRoot "examples\*") (Join-Path $packageDir "examples") -Recurse

Compress-Archive -Path (Join-Path $packageDir "*") -DestinationPath $zipPath -Force
Write-Host "Build complete: $zipPath"
Write-Host "Compile Trading_Bot\Trading_Bot.mq5 in MetaEditor before attaching it to MT5."
