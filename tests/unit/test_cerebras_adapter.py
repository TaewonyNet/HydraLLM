"""CerebrasAdapter 예외 정규화 회귀 가드(R1)."""
from unittest.mock import AsyncMock

import pytest

from src.adapters.providers.cerebras import CerebrasAdapter
from src.core.exceptions import ProviderRateLimitError, ProviderServerError
from src.domain.models import ChatMessage, ChatRequest

pytestmark = pytest.mark.unit


def _req() -> ChatRequest:
    return ChatRequest(model="auto", messages=[ChatMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_error_with_none_response_does_not_crash():
    """e.response 가 None 이어도 AttributeError 대신 ProviderServerError 로 정규화(R1)."""

    class FakeError(Exception):
        response = None  # response 속성은 존재하나 값이 None

    adapter = CerebrasAdapter(api_key="x")
    adapter._call_cerebras_api = AsyncMock(side_effect=FakeError("boom"))
    with pytest.raises(ProviderServerError):
        await adapter.generate(_req(), "x")


@pytest.mark.asyncio
async def test_error_without_response_attr_normalized():
    """response 속성 자체가 없는 일반 예외도 ProviderServerError 로 정규화."""
    adapter = CerebrasAdapter(api_key="x")
    adapter._call_cerebras_api = AsyncMock(side_effect=Exception("plain"))
    with pytest.raises(ProviderServerError):
        await adapter.generate(_req(), "x")


@pytest.mark.asyncio
async def test_429_response_maps_to_rate_limit():
    """status_code 429 는 ProviderRateLimitError 로 분류."""

    class Resp:
        status_code = 429

    class RateError(Exception):
        response = Resp()

    adapter = CerebrasAdapter(api_key="x")
    adapter._call_cerebras_api = AsyncMock(side_effect=RateError("429"))
    with pytest.raises(ProviderRateLimitError):
        await adapter.generate(_req(), "x")
