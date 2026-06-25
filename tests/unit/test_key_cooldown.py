"""키 실패 분류·쿨다운·락 복구 회귀 가드.

report_failure 가 예외 '타입'(ErrorCategory)으로 403→24h / quota→1h / 기타→5m 쿨다운을
산정하고, 실패/복구가 활성·실패 풀을 정확히 이동시키는지 검증한다. (services/key_manager.py)
"""
import time

import pytest

from src.core.exceptions import (
    AuthenticationError,
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
