"""Gemini "project denied access"(403) 키의 영구 제외 회귀 가드(item 5).

배경(실측): Gemini 키 13개 중 0번만 생성 가능, 나머지는 403 "Your project has been
denied access". 이런 키는 시간이 지나도 저절로 풀리지 않는 프로젝트/계정 설정 문제라,
24h 쿨다운이 지났다고 recovery_task(60초 폴링)가 재프로브해 되돌리면 요청마다/주기마다
같은 403 을 반복하고 컨텍스트 업로드를 재시도하는 낭비가 생긴다.

검증:
  1) startup 탐색(probe_active_keys)에서 403 키는 즉시 영구 제외된다.
  2) `providers` 로 좁히면 지정한 공급자만 probe 한다(Groq/Cerebras 불필요 쿼터 소모 방지).
  3) 영구 제외된 키는 쿨다운이 지나도 gateway._recover_failed_keys_impl 이 재프로브하지
     않는다(활성 풀로 복귀 금지).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.enums import ProviderType
from src.services.gateway import Gateway

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_probe_active_keys_permanently_excludes_denied_gemini_key():
    gateway = Gateway()
    gateway.key_manager.add_keys(ProviderType.GEMINI, ["denied_key_x"])

    fake_adapter = AsyncMock()
    fake_adapter.probe_key = AsyncMock(
        side_effect=Exception("403 Your project has been denied access")
    )
    gateway._get_provider_adapter = MagicMock(return_value=fake_adapter)

    result = await gateway.probe_active_keys(providers=(ProviderType.GEMINI,))

    assert result["denied"] == 1
    assert gateway.key_manager.is_permanently_excluded(ProviderType.GEMINI, "denied_key_x")
    assert "denied_key_x" not in gateway.key_manager.get_active_keys(ProviderType.GEMINI)


@pytest.mark.asyncio
async def test_probe_active_keys_providers_filter_skips_other_providers():
    gateway = Gateway()
    gateway.key_manager.add_keys(ProviderType.GEMINI, ["gkey"])
    gateway.key_manager.add_keys(ProviderType.GROQ, ["qkey"])
    gateway.key_manager.add_keys(ProviderType.CEREBRAS, ["ckey"])

    probed_providers: list[ProviderType] = []

    def get_adapter(provider, api_key):
        adapter = AsyncMock()

        async def _probe(_key):
            probed_providers.append(provider)
            return {"tier": "standard"}

        adapter.probe_key = _probe
        return adapter

    gateway._get_provider_adapter = MagicMock(side_effect=get_adapter)

    result = await gateway.probe_active_keys(providers=(ProviderType.GEMINI,))

    assert probed_providers == [ProviderType.GEMINI]
    assert result["probed"] == 1


@pytest.mark.asyncio
async def test_recovery_skips_permanently_excluded_key_even_after_cooldown():
    """영구 제외된 키는 cooldown_until 이 과거라도 재프로브하지 않는다."""
    import time

    gateway = Gateway()
    gateway.session_manager = AsyncMock()  # 실제 DB 쓰기 회피
    gateway.key_manager.add_keys(ProviderType.GEMINI, ["denied_key_y"])

    await gateway.key_manager.report_failure(
        ProviderType.GEMINI,
        "denied_key_y",
        Exception("403 Your project has been denied access"),
    )
    assert gateway.key_manager.is_permanently_excluded(ProviderType.GEMINI, "denied_key_y")

    # 24h 쿨다운이 이미 지난 것처럼 과거로 돌려놓는다(순수 쿨다운 체크만으론 재프로브될
    # 상황을 재현).
    gateway.key_manager.update_key_metadata(
        ProviderType.GEMINI, "denied_key_y", {"cooldown_until": time.time() - 10}
    )

    fake_adapter = AsyncMock()
    fake_adapter.probe_key = AsyncMock(return_value={"tier": "standard"})
    gateway._get_provider_adapter = MagicMock(return_value=fake_adapter)

    await gateway.recover_failed_keys()

    fake_adapter.probe_key.assert_not_called()
    assert "denied_key_y" in gateway.key_manager.get_failed_keys(ProviderType.GEMINI)
    assert "denied_key_y" not in gateway.key_manager.get_active_keys(ProviderType.GEMINI)


@pytest.mark.asyncio
async def test_probe_active_keys_permanently_excludes_payment_required_key():
    """402(결제 필요)로 probe 가 실패하면 "denied"가 아니라 "payment_required" 로
    집계되고, 403 과 동일하게 영구 제외된다(실측: Cerebras 키가 /models 는 정상인데
    chat completions 는 402 "Payment required")."""
    gateway = Gateway()
    gateway.key_manager.add_keys(ProviderType.CEREBRAS, ["billing_blocked_key"])

    fake_adapter = AsyncMock()
    fake_adapter.probe_key = AsyncMock(
        side_effect=Exception(
            "Error code: 402 - {'message': 'Payment required to access this "
            "resource. Visit your billing tab.'}"
        )
    )
    gateway._get_provider_adapter = MagicMock(return_value=fake_adapter)

    result = await gateway.probe_active_keys(providers=(ProviderType.CEREBRAS,))

    assert result["payment_required"] == 1
    assert result["denied"] == 0
    assert gateway.key_manager.is_permanently_excluded(
        ProviderType.CEREBRAS, "billing_blocked_key"
    )
    assert "billing_blocked_key" not in gateway.key_manager.get_active_keys(
        ProviderType.CEREBRAS
    )
