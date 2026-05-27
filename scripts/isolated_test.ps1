# 한글 Windows(cp949) 환경용 격리 테스트 스크립트.
# bash 버전(scripts/isolated_test.sh)과 동일 흐름을 PowerShell 로 구현.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\isolated_test.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\isolated_test.ps1 -Clean
#   $env:EXPECTED_TESTS = 96; powershell -ExecutionPolicy Bypass -File scripts\isolated_test.ps1

[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Keep
)

$ErrorActionPreference = "Stop"

# ─── 한글 Windows(cp949) → UTF-8 강제 ───
try { chcp 65001 | Out-Null } catch {}
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# ─── 경로 ───
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SrcDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$TmpBase = if ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
$Stamp = [int][double]::Parse((Get-Date -UFormat %s))
$TargetDir = Join-Path $TmpBase "hydrallm_test_$Stamp`_$PID"
$ExpectedTests = if ($env:EXPECTED_TESTS) { [int]$env:EXPECTED_TESTS } else { 0 }
$Cleanup = if ($Clean) { "clean" } elseif ($Keep) { "keep" } else { "keep" }

Write-Host "===================================================="
Write-Host "  HydraLLM 격리 환경 전수 테스트 (PowerShell)"
Write-Host "  SOURCE : $SrcDir"
Write-Host "  TARGET : $TargetDir"
Write-Host "===================================================="

# ─── 1. 소스 복사 ───
Write-Host "[1/5] 소스 복사 중..."
$excludeDirs = @(".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "data")
$excludePatterns = @("*.pyc", "*.log", "*.sqlite", "*.sqlite-wal", "*.sqlite-shm")
# robocopy: /MIR 미사용(원본 보존), /XD 디렉터리 제외, /XF 파일 제외
$xdArgs = @()
foreach ($d in $excludeDirs) { $xdArgs += "/XD"; $xdArgs += (Join-Path $SrcDir $d) }
$xfArgs = @()
foreach ($p in $excludePatterns) { $xfArgs += "/XF"; $xfArgs += $p }
& robocopy $SrcDir $TargetDir /E /NFL /NDL /NJH /NJS /NP @xdArgs @xfArgs | Out-Null
# robocopy 는 성공 시 0~7 반환. 8 이상이면 실패.
if ($LASTEXITCODE -ge 8) {
    Write-Host "robocopy 실패 (exit=$LASTEXITCODE)"
    exit 2
}

# .env 복사: 원본 .env 우선, 없으면 .env.example 로 fallback
$envSrc = Join-Path $SrcDir ".env"
$envExample = Join-Path $SrcDir ".env.example"
$envDst = Join-Path $TargetDir ".env"
if (Test-Path $envSrc) {
    Copy-Item $envSrc $envDst -Force
    Write-Host "      .env 복사 완료 (원본 .env)"
} elseif (Test-Path $envExample) {
    Copy-Item $envExample $envDst -Force
    Write-Host "      .env 생성 완료 (.env.example 로 fallback — README Step 1 수행)"
} else {
    Write-Host "      WARN: .env / .env.example 둘 다 없음 — 키 관련 경로 실패 가능"
}

# 잔존 데이터/로그 제거 재확인
Get-ChildItem -Path $TargetDir -Recurse -Include "*.log", "*.sqlite*" -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
$dataDir = Join-Path $TargetDir "data"
if (Test-Path $dataDir) { Remove-Item $dataDir -Recurse -Force -ErrorAction SilentlyContinue }
Write-Host "      OK (데이터/로그 없음)"

# ─── 2. venv 생성 ───
Write-Host "[2/5] venv 생성 중..."
Push-Location $TargetDir
try {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "venv 생성 실패"; exit 2 }

    $venvPython = Join-Path $TargetDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        $venvPython = Join-Path $TargetDir ".venv\bin\python"
    }

    # ─── 3. 의존성 설치 (pyproject.toml dev extra) ───
    Write-Host "[3/5] 의존성 설치 중..."
    & $venvPython -m pip install --upgrade pip -q
    if ($LASTEXITCODE -ne 0) { Write-Host "pip upgrade 실패"; exit 2 }

    & $venvPython -m pip install -e ".[dev]" -q
    if ($LASTEXITCODE -ne 0) {
        Write-Host "pip install .[dev] 실패"
        exit 2
    }

    # Playwright chromium (모킹이라 실패해도 치명적이지 않음)
    & $venvPython -m playwright install chromium *> $null

    $pytestVer = (& $venvPython -m pytest --version 2>&1 | Select-Object -First 1)
    $asyncioVer = (& $venvPython -c "import pytest_asyncio; print(pytest_asyncio.__version__)" 2>$null)
    Write-Host "      $pytestVer / pytest-asyncio $asyncioVer"

    # ─── 4. 전수 pytest ───
    Write-Host "[4/5] 전수 테스트 실행 중..."
    $logFile = Join-Path $TargetDir "pytest_output.log"
    # Tee-Object 는 PS 5.1 에서 UTF-16 LE 로 저장하므로 직접 수집해 UTF-8 로 기록.
    $outputLines = New-Object System.Collections.Generic.List[string]
    & $venvPython -m pytest --tb=short 2>&1 | ForEach-Object {
        $line = [string]$_
        Write-Host $line
        $outputLines.Add($line)
    }
    $pytestStatus = $LASTEXITCODE
    [System.IO.File]::WriteAllLines($logFile, $outputLines, (New-Object System.Text.UTF8Encoding $false))

    # ─── 5. 결과 검증 ───
    Write-Host "[5/5] 결과 검증..."
    # 인-메모리 로그에서 마지막 "N passed, M failed, ... in X.XXs" 요약 라인 탐색.
    $summaryLine = ($outputLines |
        Where-Object { $_ -match '(passed|failed|error)' -and $_ -match 'in [0-9.]+s' } |
        Select-Object -Last 1)

    function Get-Count($pattern, $line) {
        if ($line -match "(\d+)\s+$pattern") { return [int]$matches[1] } else { return 0 }
    }
    $passed  = Get-Count 'passed' $summaryLine
    $failed  = Get-Count 'failed' $summaryLine
    $errors  = Get-Count 'error'  $summaryLine
    $skipped = Get-Count 'skipped' $summaryLine

    Write-Host ""
    Write-Host "===================================================="
    Write-Host "  최종 결과"
    Write-Host "    passed  : $passed"
    Write-Host "    failed  : $failed"
    Write-Host "    errors  : $errors"
    Write-Host "    skipped : $skipped"
    Write-Host "    target  : $TargetDir"
    Write-Host "===================================================="

    $exit = 0
    if ($pytestStatus -ne 0 -or $failed -gt 0 -or $errors -gt 0) {
        Write-Host "[FAIL] 테스트 실패."
        $exit = 1
    }
    if ($ExpectedTests -gt 0) {
        if ($passed -lt $ExpectedTests) {
            Write-Host "[FAIL] 기대 통과 수 미달: $passed < $ExpectedTests"
            $exit = 1
        } else {
            Write-Host "[OK] 기대 통과 수 충족: $passed >= $ExpectedTests"
        }
    }
    if ($exit -eq 0) { Write-Host "[OK] 전수 통과." }
}
finally {
    Pop-Location
}

if ($Cleanup -eq "clean") {
    Write-Host "임시 폴더 삭제: $TargetDir"
    Remove-Item $TargetDir -Recurse -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "임시 폴더 유지: $TargetDir"
}

exit $exit
