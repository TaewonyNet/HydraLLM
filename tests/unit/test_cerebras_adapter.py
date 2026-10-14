"""CerebrasAdapter 예외 정규화 회귀 가드(R1) + 모델 하드코딩 제거 회귀 가드.

실측(2026-10): Cerebras 에서 llama3.1-70b-versatile 는 더 이상 제공되지 않는다(404).
어댑터가 이 모델명을 하드코딩하지 않고 /models 탐색 결과(또는 구체 모델명 명시 시 그 값)를
쓰는지, 그리고 413(Payload too large)이 429/5xx 와 분리되어 RequestTooLargeError 로
분류되는지 검증한다.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.providers.cerebras import (
    DEFAULT_CEREBRAS_MODEL,
    CerebrasAdapter,
)
from src.core.exceptions import (
    PaymentRequiredError,
    ProviderRateLimitError,
    ProviderServerError,
    RequestTooLargeError,
)
from src.domain.models import ChatMessage, ChatRequest

pytestmark = pytest.mark.unit


def _req(model: str = "gpt-oss-120b") -> ChatRequest:
    """기본값으로 구체 모델명을 써서, 에러 분류를 검증하는 테스트가 /models 탐색
    네트워크 호출을 유발하지 않게 한다(탐색 자체는 아래 전용 테스트에서 검증)."""
    return ChatRequest(model=model, messages=[ChatMessage(role="user", content="hi")])


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


@pytest.mark.asyncio
async def test_429_carries_retry_after_from_header():
    """429 응답에 Retry-After 헤더가 있으면 예외에 실어 key_manager 쿨다운에 반영될
    수 있게 한다(실측: Groq 분당 토큰 한도 — 고정 1h 쿨다운보다 짧게 풀리는 게 보통)."""

    class Resp:
        status_code = 429
        headers = {"retry-after": "7"}

    class RateError(Exception):
        response = Resp()

    adapter = CerebrasAdapter(api_key="x")
    adapter._call_cerebras_api = AsyncMock(side_effect=RateError("429"))
    with pytest.raises(ProviderRateLimitError) as exc_info:
        await adapter.generate(_req(), "x")

    assert exc_info.value.retry_after == 7.0


@pytest.mark.asyncio
async def test_429_without_header_has_none_retry_after():
    """Retry-After 헤더가 없으면 None — key_manager 가 기존처럼 기본값으로 대체한다."""

    class Resp:
        status_code = 429
        headers = {}

    class RateError(Exception):
        response = Resp()

    adapter = CerebrasAdapter(api_key="x")
    adapter._call_cerebras_api = AsyncMock(side_effect=RateError("429"))
    with pytest.raises(ProviderRateLimitError) as exc_info:
        await adapter.generate(_req(), "x")

    assert exc_info.value.retry_after is None


@pytest.mark.asyncio
async def test_413_response_maps_to_request_too_large():
    """status_code 413(Payload too large)은 키 실패 분류(429/5xx)와 분리된
    RequestTooLargeError 로 분류된다 — key_manager 가 이를 키 실패로 오인해 쿨다운시키지
    않도록(실측: 과거엔 ProviderServerError 로 뭉뚱그려져 키가 쿨다운당했다)."""

    class Resp:
        status_code = 413

    class TooLargeError(Exception):
        response = Resp()

    adapter = CerebrasAdapter(api_key="x")
    adapter._call_cerebras_api = AsyncMock(side_effect=TooLargeError("413"))
    with pytest.raises(RequestTooLargeError):
        await adapter.generate(_req(), "x")


@pytest.mark.asyncio
async def test_402_response_maps_to_payment_required():
    """status_code 402(Payment required)는 429/413/5xx 와 분리된 PaymentRequiredError
    로 분류된다(실측: Cerebras 키가 /models 는 정상이지만 chat completions 에서 402
    "Payment required ... billing tab" 를 반환). key_manager 가 403 과 동일하게 영구
    제외하도록 전용 타입으로 떨어져야 한다(과거엔 else 분기로 떨어져 ProviderServerError
    → resilience_manager 의 일반 Exception 처리로 새서 같은 키로 3회 재시도됐다)."""

    class Resp:
        status_code = 402

    class PaymentError(Exception):
        response = Resp()

    adapter = CerebrasAdapter(api_key="x")
    adapter._call_cerebras_api = AsyncMock(side_effect=PaymentError("402"))
    with pytest.raises(PaymentRequiredError):
        await adapter.generate(_req(), "x")


@pytest.mark.asyncio
async def test_probe_key_uses_chat_completion_not_models_list():
    """probe_key() 는 /models 목록이 아니라 짧은 chat completion 1회로 확인해야
    한다 — /models 만으론 402(결제 필요)를 못 잡는다(실측). 실패 시 예외를 그대로
    전파해야 한다(내부에서 삼키면 호출부가 분류·영구제외를 못 한다)."""
    adapter = CerebrasAdapter(api_key="x")
    adapter._resolve_model = AsyncMock(return_value="gpt-oss-120b")
    adapter.client.models.list = AsyncMock(
        side_effect=AssertionError("probe_key 가 /models 를 쓰면 안 됨")
    )

    class PaymentError(Exception):
        pass

    adapter.client.chat.completions.create = AsyncMock(side_effect=PaymentError("402"))

    with pytest.raises(PaymentError):
        await adapter.probe_key("x")

    adapter.client.chat.completions.create.assert_awaited_once()
    _, kwargs = adapter.client.chat.completions.create.call_args
    assert kwargs["model"] == "gpt-oss-120b"
    assert kwargs.get("max_tokens") == 1


@pytest.mark.asyncio
async def test_explicit_model_name_is_sent_as_is_without_discovery():
    """구체 모델명이 명시되면 /models 탐색을 거치지 않고 그대로 사용한다."""
    adapter = CerebrasAdapter(api_key="x")
    adapter.discover_models = AsyncMock(side_effect=AssertionError("탐색이 호출되면 안 됨"))

    model = await adapter._resolve_model("qwen-3.8-27b")
    assert model == "qwen-3.8-27b"


@pytest.mark.asyncio
async def test_generic_hint_resolves_via_model_discovery():
    """모델명이 없거나 범용 힌트("auto" 등)면 /models 탐색 결과를 쓰고,
    gpt-oss-120b 가 탐색 목록에 있으면 그것을 기본값으로 선택한다."""
    adapter = CerebrasAdapter(api_key="x")
    adapter.discover_models = AsyncMock(
        return_value=[
            {"id": "qwen-3.8-27b"},
            {"id": "gpt-oss-120b"},
        ]
    )

    model = await adapter._resolve_model("auto")
    assert model == "gpt-oss-120b"
    adapter.discover_models.assert_awaited_once()

    # 캐시되어 두 번째 호출은 탐색을 다시 하지 않는다.
    model_again = await adapter._resolve_model(None)
    assert model_again == "gpt-oss-120b"
    adapter.discover_models.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_failure_falls_back_to_default_constant():
    """llama3.1-70b-versatile 하드코딩 제거 후 최후 fallback 은 DEFAULT_CEREBRAS_MODEL
    (gpt-oss-120b)이어야 한다 — 더 이상 없는 모델로 떨어지지 않는다."""
    adapter = CerebrasAdapter(api_key="x")
    adapter.discover_models = AsyncMock(return_value=[])

    model = await adapter._resolve_model("")
    assert model == DEFAULT_CEREBRAS_MODEL == "gpt-oss-120b"


@pytest.mark.asyncio
async def test_generate_uses_resolved_model_not_hardcoded_llama():
    """generate() 가 실제로 보내는 model 값이 llama3.1-70b-versatile 이 아니라 /models
    탐색(또는 명시 모델)로 정해진 값인지 종단적으로 확인한다."""
    adapter = CerebrasAdapter(api_key="x")
    adapter.discover_models = AsyncMock(return_value=[{"id": "gpt-oss-120b"}])

    captured: dict = {}

    async def fake_call(request: dict):
        captured["model"] = request["model"]
        msg = "short-circuit: 호출 모델만 확인하면 충분"
        raise RuntimeError(msg)

    adapter._call_cerebras_api = fake_call

    with pytest.raises(ProviderServerError):
        await adapter.generate(_req(model="auto"), "x")

    assert captured["model"] == "gpt-oss-120b"
    assert captured["model"] != "llama3.1-70b-versatile"


def _mock_cerebras_response(reasoning_tokens: int | None = None) -> MagicMock:
    mock_choice = MagicMock()
    mock_choice.message.role = "assistant"
    mock_choice.message.content = "ok"
    mock_choice.finish_reason = "stop"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 10
    mock_usage.completion_tokens = 5
    mock_usage.total_tokens = 15
    if reasoning_tokens is not None:
        mock_usage.completion_tokens_details.reasoning_tokens = reasoning_tokens
    else:
        mock_usage.completion_tokens_details = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.id = "test-id"
    mock_response.model = "gpt-oss-120b"
    mock_response.usage = mock_usage
    return mock_response


@pytest.mark.asyncio
async def test_reasoning_tokens_preserved_when_present():
    """gpt-oss 계열은 Cerebras 에서도 completion_tokens_details.reasoning_tokens 를
    돌려줄 수 있다 — Groq 어댑터와 동일하게 가능하면 보존한다(요청으로 reasoning_effort
    를 보내진 않지만, 모델이 자체 반환하는 값은 그대로 전달)."""
    adapter = CerebrasAdapter(api_key="x")
    adapter._call_cerebras_api = AsyncMock(
        return_value=_mock_cerebras_response(reasoning_tokens=9)
    )

    response = await adapter.generate(_req(), "x")

    assert response.usage["reasoning_tokens"] == 9
    assert response.usage["completion_tokens"] == 5
    assert response.choices[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_reasoning_tokens_omitted_when_absent():
    """reasoning_tokens 가 없으면 usage 에 그 키를 억지로 넣지 않는다."""
    adapter = CerebrasAdapter(api_key="x")
    adapter._call_cerebras_api = AsyncMock(
        return_value=_mock_cerebras_response(reasoning_tokens=None)
    )

    response = await adapter.generate(_req(), "x")

    assert "reasoning_tokens" not in response.usage


@pytest.mark.asyncio
async def test_cerebras_request_does_not_include_unverified_fields():
    """response_format/reasoning_effort 는 Cerebras 에서 실측 확인이 안 됐으므로(결제
    문제로 실측 불가) 요청에 포함하지 않는다 — 확인될 때까지 보수적으로 유지."""
    adapter = CerebrasAdapter(api_key="x")
    request = ChatRequest(
        model="gpt-oss-120b",
        messages=[ChatMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
        reasoning_effort="low",
    )

    cerebras_request = await adapter._convert_to_cerebras_request(request)

    assert "response_format" not in cerebras_request
    assert "reasoning_effort" not in cerebras_request
