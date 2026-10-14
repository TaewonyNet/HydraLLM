"""요청 전체 마감 시간(REQUEST_DEADLINE_SECONDS) 회귀 가드.

배경(실측): Groq 가 413 을 내자 키 실패로 처리되어 Gemini 로 넘어갔다가 403 을 받고,
클라이언트가 300초 타임아웃을 당했다. 공급자 재시도 루프 자체가 무기한으로 걸릴 수
있었던 것이 근본 원인이므로, Gateway.process_request 가 전체 요청에 마감 시간을 걸어
초과 시 asyncio.TimeoutError 대신 명확한 GatewayTimeoutError 를 즉시 돌려주는지
검증한다.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import settings
from src.core.exceptions import GatewayTimeoutError
from src.domain.enums import ProviderType
from src.domain.models import ChatMessage, ChatRequest
from src.services.gateway import Gateway

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_slow_resilience_hits_deadline_and_raises_gateway_timeout(monkeypatch):
    """resilience.execute_with_resilience 가 마감 시간보다 오래 걸리면, 응답을 끝까지
    기다리지 않고 GatewayTimeoutError 를 즉시 던진다(클라이언트 무기한 대기 방지)."""
    monkeypatch.setattr(settings, "request_deadline_seconds", 0.05)

    gateway = Gateway()
    gateway.web_context.enrich_request = AsyncMock(return_value=([], None))

    decision = MagicMock()
    decision.provider = ProviderType.GROQ
    decision.agent = None
    decision.model_name = "llama-3.3-70b-versatile"
    decision.reason = "test"
    decision.strict = False
    gateway.analyzer.analyze = AsyncMock(return_value=decision)

    async def _hangs_forever(*_args, **_kwargs):
        await asyncio.sleep(5)  # 마감 시간(0.05s)보다 훨씬 길게 걸리는 공급자 체인을 흉내
        msg = "데드라인이 동작했다면 여기까지 오면 안 됨"
        raise AssertionError(msg)

    gateway.resilience.execute_with_resilience = _hangs_forever

    request = ChatRequest(
        model="auto", messages=[ChatMessage(role="user", content="hi")]
    )

    with pytest.raises(GatewayTimeoutError):
        await gateway.process_request(request)


@pytest.mark.asyncio
async def test_fast_resilience_is_unaffected_by_deadline(monkeypatch):
    """대조군: 마감 시간 안에 끝나면 정상 응답을 그대로 돌려준다(이번 수정이 정상
    경로를 깨지 않았는지 확인)."""
    monkeypatch.setattr(settings, "request_deadline_seconds", 5)

    gateway = Gateway()
    gateway.web_context.enrich_request = AsyncMock(return_value=([], None))

    decision = MagicMock()
    decision.provider = ProviderType.GROQ
    decision.agent = None
    decision.model_name = "llama-3.3-70b-versatile"
    decision.reason = "test"
    decision.strict = False
    gateway.analyzer.analyze = AsyncMock(return_value=decision)

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "ok"
    mock_response.usage = {}

    gateway.resilience.execute_with_resilience = AsyncMock(
        return_value=(mock_response, [])
    )

    request = ChatRequest(
        model="auto", messages=[ChatMessage(role="user", content="hi")]
    )

    response = await gateway.process_request(request)
    assert response.choices[0].message.content == "ok"
