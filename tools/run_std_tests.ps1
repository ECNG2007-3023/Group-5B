# Runs every STD test case (P5A + P5B + joint) and writes the evidence.
#
#   1. Start Docker Desktop and build the image once:
#        cd ..\Group-5A ; docker build -t p5a-ghdl ./docker
#   2. Make sure vhdl_ls is on PATH, or set
#        $env:P5B_VHDL_LS_PATH = "C:\tools\vhdl_ls-x86_64-pc-windows-msvc\bin\vhdl_ls.exe"
#   3. From the Group-5B folder:
#        powershell -ExecutionPolicy Bypass -File tools\run_std_tests.ps1
#
# Output:
#   Group-5A\tests\evidence\*.json, junit_p5a.xml, pytest_p5a.txt
#   Group-5B\tests\evidence\*.json, junit_p5b.xml, pytest_p5b.txt
#   Group-5B\STD_TEST_REPORT.md   <- the filled-in test log

$ErrorActionPreference = "Continue"
$B = Split-Path -Parent $PSScriptRoot
$A = Join-Path (Split-Path -Parent $B) "Group-5A"
if ($env:P5A_DIR) { $A = $env:P5A_DIR }

$py = Join-Path $A "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
Write-Host "Python: $py"

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Docker is not running: the Docker tests will be SKIPPED (shown as 'Not run')."
} else {
    docker image inspect p5a-ghdl *> $null
    if ($LASTEXITCODE -ne 0) { Write-Warning "Image p5a-ghdl not built: run  docker build -t p5a-ghdl ./docker  in Group-5A." }
}
$vls = if ($env:P5B_VHDL_LS_PATH) { $env:P5B_VHDL_LS_PATH } else { (Get-Command vhdl_ls -ErrorAction SilentlyContinue).Source }
if (-not $vls) { Write-Warning "vhdl_ls not found: the real VHDL-LS tests will be SKIPPED." }

foreach ($repo in @($A, $B)) {
    $ev = Join-Path $repo "tests\evidence"
    if (Test-Path $ev) { Remove-Item $ev -Recurse -Force }
    New-Item -ItemType Directory $ev | Out-Null
}

Write-Host "`n=== P5A STD tests ===" -ForegroundColor Cyan
Push-Location $A
& $py -m pytest tests/test_std.py -v -rA "--junitxml=tests/evidence/junit_p5a.xml" 2>&1 |
    Tee-Object -FilePath tests\evidence\pytest_p5a.txt
Pop-Location

Write-Host "`n=== P5B + joint STD tests ===" -ForegroundColor Cyan
Push-Location $B
$env:P5A_DIR = $A
& $py -m pytest tests/test_std.py tests/test_std_joint.py -v -rA "--junitxml=tests/evidence/junit_p5b.xml" 2>&1 |
    Tee-Object -FilePath tests\evidence\pytest_p5b.txt
Pop-Location

Write-Host "`n=== Report ===" -ForegroundColor Cyan
& $py (Join-Path $B "tools\std_report.py") $A $B
Write-Host "Open $B\STD_TEST_REPORT.md and screenshot the two pytest windows above for the STD figures."
