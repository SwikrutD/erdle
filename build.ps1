# Build ERDLE.exe. Run from the project root in PowerShell.
#
#   .\build.ps1
#
# Output: dist\ERDLE.exe

$ErrorActionPreference = "Stop"

Write-Host "== checking python ==" -ForegroundColor Cyan
python --version

Write-Host "`n== installing build dependencies ==" -ForegroundColor Cyan
python -m pip install --quiet --upgrade pip
python -m pip install --quiet mss pytesseract Pillow pystray pyinstaller pytest

Write-Host "`n== running tests ==" -ForegroundColor Cyan
python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "tests failed; not building" }

Write-Host "`n== vendoring Tesseract ==" -ForegroundColor Cyan
# Bundled so a new user needs nothing beyond the exe. Detection is
# name-driven: without a reader the app does not lose a feature, it does
# nothing at all.
python tools\vendor_tesseract.py
if ($LASTEXITCODE -ne 0) { throw "could not vendor Tesseract; install it first" }

Write-Host "`n== seeding the glyph atlas ==" -ForegroundColor Cyan
# Promotes the alphabet learned while playing into the one that ships, so
# a new user does not start blind. Not fatal if there is nothing to
# promote yet -- the exe still works, it just leans on Tesseract for
# longer. Prints what it did either way.
python tools\seed_atlas.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "  (continuing without a seeded atlas)" -ForegroundColor Yellow
}

Write-Host "`n== generating icon ==" -ForegroundColor Cyan
python tools\make_icon.py

Write-Host "`n== building ==" -ForegroundColor Cyan
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }
if (Test-Path build) { Remove-Item -Recurse -Force build }
python -m PyInstaller --clean --noconfirm erdle.spec

$exe = "dist\ERDLE.exe"
if (-not (Test-Path $exe)) { throw "build produced no exe" }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash

# The three files a release needs, staged together so publishing is a
# drag-and-drop rather than a scavenger hunt. LICENSE and THIRD_PARTY.md
# are inside the exe too, but a user comparing licences should not have
# to unpack a binary to read them.
$release = "dist\release"
New-Item -ItemType Directory -Force -Path $release | Out-Null
Copy-Item $exe            $release
Copy-Item LICENSE         $release
Copy-Item THIRD_PARTY.md  $release
"$hash  ERDLE.exe" | Out-File -Encoding ascii "$release\SHA256.txt"

Write-Host "`n== done: $exe ($size MB) ==" -ForegroundColor Green
Write-Host "SHA256: $hash"
Write-Host "Release files staged in $release" -ForegroundColor Green
Write-Host "Double-click the exe. It lives in the system tray."
