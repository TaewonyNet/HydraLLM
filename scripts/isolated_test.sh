#!/usr/bin/env bash
# 임시 폴더에 현재 소스를 복제 → 데이터/로그/캐시 제외 → venv 생성 →
# README 설치 절차 수행 → .env 복사 → 전수 pytest 실행 → 테스트 수 검증.
#
# Usage:
#   scripts/isolated_test.sh                  # 기본 실행
#   scripts/isolated_test.sh --keep           # 테스트 후 임시 폴더 유지 (기본: 유지)
#   scripts/isolated_test.sh --clean          # 테스트 후 임시 폴더 삭제
#   EXPECTED_TESTS=96 scripts/isolated_test.sh # 최소 기대 통과 수 지정
#
# 종료 코드:
#   0  전수 통과 + (EXPECTED_TESTS 지정 시) 최소 기대치 충족
#   1  pytest 실패 또는 기대 테스트 수 미달
#   2  설치/환경 준비 실패

set -euo pipefail

# ─── 경로 및 옵션 ───
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP_BASE="${TMPDIR:-/tmp}"
TARGET_DIR="${TMP_BASE}/hydrallm_test_$(date +%s)_$$"
EXPECTED_TESTS="${EXPECTED_TESTS:-0}"
CLEANUP="keep"

for arg in "$@"; do
    case "$arg" in
        --clean) CLEANUP="clean" ;;
        --keep)  CLEANUP="keep" ;;
        --help|-h)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
    esac
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  HydraLLM 격리 환경 전수 테스트"
echo "  SOURCE : ${SRC_DIR}"
echo "  TARGET : ${TARGET_DIR}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── 1. 소스 복사 (데이터·로그·캐시 제외) ───
echo "[1/5] 소스 복사 중…"
rsync -a \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='.mypy_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.log' \
    --exclude='*.sqlite' \
    --exclude='*.sqlite-wal' \
    --exclude='*.sqlite-shm' \
    --exclude='data/' \
    "${SRC_DIR}/" "${TARGET_DIR}/"

# .env 복사 (없으면 경고만)
if [[ -f "${SRC_DIR}/.env" ]]; then
    cp "${SRC_DIR}/.env" "${TARGET_DIR}/.env"
    echo "      .env 복사 완료"
else
    echo "      WARN: .env 가 원본에 없음. 테스트가 키 관련 경로에서 실패할 수 있음."
fi

# 데이터·로그 제거 검증
LEFTOVER=$(find "${TARGET_DIR}" -maxdepth 2 \
    \( -name '*.log' -o -name '*.sqlite*' -o -path "${TARGET_DIR}/data" \) 2>/dev/null || true)
if [[ -n "${LEFTOVER}" ]]; then
    echo "      WARN: 잔존 데이터/로그 감지 → 제거"
    echo "${LEFTOVER}" | xargs -r rm -rf
fi
echo "      OK (데이터/로그 없음)"

# ─── 2. venv 생성 ───
echo "[2/5] venv 생성 중…"
cd "${TARGET_DIR}"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

# ─── 3. 패키지 설치 (README Step 2A) ───
echo "[3/5] 의존성 설치 중…"
pip install --upgrade pip -q || { echo "pip upgrade 실패"; exit 2; }
pip install '.[dev]' -q || { echo "pip install .[dev] 실패 (pyproject.toml)"; exit 2; }

# Playwright chromium (scraper에 필요)
if ! python -m playwright install chromium >/dev/null 2>&1; then
    echo "      WARN: playwright chromium 설치 실패 (스크래퍼 테스트는 모킹이라 영향 없음)"
fi

PYTEST_VER=$(python -m pytest --version 2>&1 | head -1)
ASYNCIO_VER=$(python -c "import pytest_asyncio; print(pytest_asyncio.__version__)" 2>/dev/null || echo "?")
echo "      ${PYTEST_VER} / pytest-asyncio ${ASYNCIO_VER}"

# ─── 4. 전수 pytest 실행 ───
echo "[4/5] 전수 테스트 실행 중…"
LOG_FILE="${TARGET_DIR}/pytest_output.log"
set +e
python -m pytest --tb=short 2>&1 | tee "${LOG_FILE}"
PYTEST_STATUS=${PIPESTATUS[0]}
set -e

# ─── 5. 결과 검증 ───
echo "[5/5] 결과 검증…"
# pytest 최종 요약 라인은 "N passed, M failed, ... in X.XXs" 형태이며,
# 마지막에서 역순으로 "passed" 혹은 "failed"/"error" 키워드가 들어간 라인을 찾는다.
SUMMARY_LINE=$(grep -E '(passed|failed|error)' "${LOG_FILE}" | grep -E 'in [0-9.]+s' | tail -1 || true)
extract_count() {
    local pattern="$1"
    local line="$2"
    echo "${line}" | grep -oE "[0-9]+ ${pattern}" | head -1 | grep -oE '[0-9]+' || true
}
PASSED=$(extract_count "passed" "${SUMMARY_LINE}")
FAILED=$(extract_count "failed" "${SUMMARY_LINE}")
ERRORS=$(extract_count "error" "${SUMMARY_LINE}")
SKIPPED=$(extract_count "skipped" "${SUMMARY_LINE}")
PASSED=${PASSED:-0}
FAILED=${FAILED:-0}
ERRORS=${ERRORS:-0}
SKIPPED=${SKIPPED:-0}

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  최종 결과"
echo "    passed  : ${PASSED}"
echo "    failed  : ${FAILED}"
echo "    errors  : ${ERRORS}"
echo "    skipped : ${SKIPPED}"
echo "    target  : ${TARGET_DIR}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

EXIT=0
if [[ "${PYTEST_STATUS}" -ne 0 ]] || [[ "${FAILED}" -gt 0 ]] || [[ "${ERRORS}" -gt 0 ]]; then
    echo "❌ 테스트 실패."
    EXIT=1
fi

if [[ "${EXPECTED_TESTS}" -gt 0 ]]; then
    if [[ "${PASSED}" -lt "${EXPECTED_TESTS}" ]]; then
        echo "❌ 기대 통과 수 미달: ${PASSED} < ${EXPECTED_TESTS}"
        EXIT=1
    else
        echo "✅ 기대 통과 수 충족: ${PASSED} ≥ ${EXPECTED_TESTS}"
    fi
fi

if [[ "${EXIT}" -eq 0 ]]; then
    echo "✅ 전수 통과."
fi

# ─── 정리 ───
if [[ "${CLEANUP}" == "clean" ]]; then
    echo "임시 폴더 삭제: ${TARGET_DIR}"
    rm -rf "${TARGET_DIR}"
else
    echo "임시 폴더 유지: ${TARGET_DIR}"
fi

exit "${EXIT}"
