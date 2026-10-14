"""키 실패 분류·쿨다운·락 복구 회귀 가드.

report_failure 가 예외 '타입'(ErrorCategory)으로 403→24h / quota→1h / 기타→5m 쿨다운을
산정하고, 실패/복구가 활성·실패 풀을 정확히 이동시키는지 검증한다. (services/key_manager.py)
"""
import time

import pytest

from src.core.exceptions import (
    AuthenticationError,
    PaymentRequiredError,
    RateLimitError,
    ServiceUnavailableError,
)
from src.domain.enums import ProviderType
from src.services.key_manager import KeyManager

pytestmark = pytest.mark.unit


def _make_km() -> KeyManager:
    km = KeyManager()
    km.add_keys(ProviderType.GEMINI, ["k_forbidden_x", "k_quota_xxxx", "k_other_xxxx"])
    return km


@pytest.mark.asyncio
async def test_cooldown_classified_by_exception_type():
    """메시지에 403/quota 단어가 없어도 예외 타입만으로 분류된다."""
    km = _make_km()
    await km.report_failure(ProviderType.GEMINI, "k_forbidden_x", AuthenticationError("nope"))
    await km.report_failure(ProviderType.GEMINI, "k_quota_xxxx", RateLimitError("slow down"))
    await km.report_failure(ProviderType.GEMINI, "k_other_xxxx", ServiceUnavailableError("500"))

    now = time.time()

    def remaining(k: str) -> float:
        return km.get_key_metadata(ProviderType.GEMINI, k)["cooldown_until"] - now

    assert 86000 < remaining("k_forbidden_x") <= 86400  # 24h
    assert 3500 < remaining("k_quota_xxxx") <= 3600  # 1h
    assert 250 < remaining("k_other_xxxx") <= 300  # 5m

    assert km.get_key_metadata(ProviderType.GEMINI, "k_forbidden_x")["is_forbidden"] is True
    assert km.get_key_metadata(ProviderType.GEMINI, "k_quota_xxxx")["is_quota_limit"] is True


@pytest.mark.asyncio
async def test_failure_moves_key_to_failed_pool():
    km = _make_km()
    assert km.get_available_keys_count(ProviderType.GEMINI) == 3
    await km.report_failure(ProviderType.GEMINI, "k_quota_xxxx", RateLimitError("x"))
    assert km.get_available_keys_count(ProviderType.GEMINI) == 2
    assert km.get_failed_keys_count(ProviderType.GEMINI) == 1
    assert "k_quota_xxxx" in km.get_failed_keys(ProviderType.GEMINI)


@pytest.mark.asyncio
async def test_success_recovers_key_to_active_pool():
    km = _make_km()
    await km.report_failure(ProviderType.GEMINI, "k_quota_xxxx", RateLimitError("x"))
    await km.report_success(ProviderType.GEMINI, "k_quota_xxxx")
    assert km.get_available_keys_count(ProviderType.GEMINI) == 3
    assert km.get_failed_keys_count(ProviderType.GEMINI) == 0


@pytest.mark.asyncio
async def test_string_heuristic_still_classifies_untyped_errors():
    """타입이 없는 일반 예외도 메시지 휴리스틱으로 분류된다(하위 호환)."""
    km = _make_km()
    await km.report_failure(ProviderType.GEMINI, "k_forbidden_x", Exception("HTTP 403 denied"))
    now = time.time()
    rem = km.get_key_metadata(ProviderType.GEMINI, "k_forbidden_x")["cooldown_until"] - now
    assert 86000 < rem <= 86400  # 문자열 "403"/"denied" → 24h


@pytest.mark.asyncio
async def test_forbidden_failure_is_permanently_excluded():
    """403/forbidden(예: Gemini "project has been denied access")은 시간이 지나도
    저절로 풀리지 않는 설정 문제이므로 permanently_excluded 플래그가 선다(item 5) —
    recovery_task 가 24h 쿨다운이 지났다고 재프로브해 되돌리지 않도록 하는 신호."""
    km = _make_km()
    await km.report_failure(
        ProviderType.GEMINI,
        "k_forbidden_x",
        Exception("403 Your project has been denied access"),
    )
    assert km.is_permanently_excluded(ProviderType.GEMINI, "k_forbidden_x") is True


@pytest.mark.asyncio
async def test_quota_failure_is_not_permanently_excluded():
    """429(쿼터 소진)는 시간이 지나면 풀리는 정상 상태라 영구 제외 대상이 아니다 —
    403 과 분리되어야 recovery_task 가 1h 뒤 정상적으로 재프로브한다."""
    km = _make_km()
    await km.report_failure(ProviderType.GEMINI, "k_quota_xxxx", RateLimitError("quota exceeded"))
    assert km.is_permanently_excluded(ProviderType.GEMINI, "k_quota_xxxx") is False


def test_is_permanently_excluded_defaults_false_for_unknown_key():
    """실패 기록이 없는 키(또는 알 수 없는 provider/key)는 기본값 False."""
    km = _make_km()
    assert km.is_permanently_excluded(ProviderType.GEMINI, "k_other_xxxx") is False
    assert km.is_permanently_excluded(ProviderType.GROQ, "never_added") is False


@pytest.mark.asyncio
async def test_payment_required_is_classified_as_forbidden_and_permanently_excluded():
    """402(결제 필요)는 403 과 동일하게 24h 쿨다운 + permanently_excluded — 재시도해도
    안 풀리는 계정/결제 설정 문제이기 때문(실측: Cerebras 키가 /models 는 정상인데
    chat completions 는 402 "Payment required ... billing tab")."""
    km = _make_km()
    await km.report_failure(
        ProviderType.GEMINI, "k_forbidden_x", PaymentRequiredError("402 payment required")
    )
    now = time.time()
    rem = km.get_key_metadata(ProviderType.GEMINI, "k_forbidden_x")["cooldown_until"] - now
    assert 86000 < rem <= 86400  # 403 과 동일한 24h
    assert km.get_key_metadata(ProviderType.GEMINI, "k_forbidden_x")["is_forbidden"] is True
    assert km.is_permanently_excluded(ProviderType.GEMINI, "k_forbidden_x") is True
    # 402 는 429 쪽(1h, is_quota_limit) 분류로 새면 안 된다 — "quota"/"billing" 단어가
    # 메시지에 섞여도 is_forbidden 이 먼저 확정돼야 한다.
    assert km.get_key_metadata(ProviderType.GEMINI, "k_forbidden_x")["is_quota_limit"] is False


@pytest.mark.asyncio
async def test_payment_required_string_heuristic_for_untyped_errors():
    """타입이 없는 일반 예외도 "402"/"payment required" 문자열로 영구 제외 분류된다
    (실제 openai SDK 예외가 str(e) 에 "Error code: 402 - {'message': 'Payment
    required...'}" 형태로 담기는 경우를 재현)."""
    km = _make_km()
    await km.report_failure(
        ProviderType.GEMINI,
        "k_quota_xxxx",
        Exception(
            "Error code: 402 - {'message': 'Payment required to access this "
            "resource. Visit your billing tab.', 'type': 'payment_required_error'}"
        ),
    )
    assert km.is_permanently_excluded(ProviderType.GEMINI, "k_quota_xxxx") is True


@pytest.mark.asyncio
async def test_rate_limit_cooldown_uses_provider_retry_after_when_present():
    """429 에 공급자가 알려준 retry_after 가 있으면 고정 1h 대신 그 값을 쿨다운으로
    쓴다(실측: Groq 분당 토큰 한도는 보통 1h 보다 훨씬 짧게 풀린다 — 고정 1h 로 묶으면
    실제보다 훨씬 오래 키를 묵혀 불필요하게 가용 키를 줄인다)."""
    km = _make_km()
    await km.report_failure(
        ProviderType.GEMINI,
        "k_quota_xxxx",
        RateLimitError("429 rate limit", retry_after=12),
    )
    now = time.time()
    rem = km.get_key_metadata(ProviderType.GEMINI, "k_quota_xxxx")["cooldown_until"] - now
    assert 10 < rem <= 12
    meta = km.get_key_metadata(ProviderType.GEMINI, "k_quota_xxxx")
    assert meta["last_429"]["retry_delay"] == 12
    assert meta["last_429"]["retry_after_source"] == "provider"


@pytest.mark.asyncio
async def test_rate_limit_cooldown_falls_back_to_1h_without_retry_after():
    """retry_after 가 없고 메시지에서도 파싱이 안 되면 보수적 기본값(1h) 유지 —
    기존 동작과 동일(회귀 가드)."""
    km = _make_km()
    await km.report_failure(
        ProviderType.GEMINI, "k_quota_xxxx", RateLimitError("429 rate limit")
    )
    now = time.time()
    rem = km.get_key_metadata(ProviderType.GEMINI, "k_quota_xxxx")["cooldown_until"] - now
    assert 3500 < rem <= 3600
