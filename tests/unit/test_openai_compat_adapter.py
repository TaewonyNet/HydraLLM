from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.providers.openai_compat import (
    DEFAULT_GROQ_MODEL,
    OpenAICompatAdapter,
)
from src.core.exceptions import (
    PaymentRequiredError,
    RateLimitError,
    RequestTooLargeError,
)
from src.domain.models import ChatMessage, ChatRequest

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_openai_compat_auto_model_mapping():
    # 생성자에 명시된 고정 default_model 은 어떤 값이든(실존 여부 무관) 그대로 쓰인다 —
    # 이 테스트는 "범용 힌트 → default_model" 매핑 메커니즘만 검증한다(실제 Groq 기본값
    # 선택은 아래 _resolve_model 전용 테스트가 검증).
    default_model = "custom-default-model"
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model=default_model,
    )

    mock_choice = MagicMock()
    mock_choice.message.content = "Hello"
    mock_choice.finish_reason = "stop"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 10
    mock_usage.completion_tokens = 5
    mock_usage.total_tokens = 15

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.id = "test-id"
    mock_response.object = "chat.completion"
    mock_response.created = 12345
    mock_response.model = default_model
    mock_response.usage = mock_usage

    adapter.client.chat.completions.create = AsyncMock(return_value=mock_response)

    auto_hints = ["auto", "mllm/auto", "groq", "llama"]

    for hint in auto_hints:
        request = ChatRequest(
            model=hint, messages=[ChatMessage(role="user", content="Hi")]
        )

        await adapter.generate(request, api_key="test-key")

        args, kwargs = adapter.client.chat.completions.create.call_args
        assert kwargs["model"] == default_model, f"Failed for hint: {hint}"
        assert kwargs["model"] != hint


@pytest.mark.asyncio
async def test_openai_compat_explicit_model_preserved():
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model="llama-default",
    )

    mock_choice = MagicMock()
    mock_choice.message.content = "Hello"
    mock_choice.finish_reason = "stop"
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 10
    mock_usage.completion_tokens = 5
    mock_usage.total_tokens = 15

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.id = "test-id"
    mock_response.object = "chat.completion"
    mock_response.created = 12345
    mock_response.model = "llama-3.1-8b-instant"
    mock_response.usage = mock_usage

    adapter.client.chat.completions.create = AsyncMock(return_value=mock_response)

    explicit_model = "llama-3.1-8b-instant"
    request = ChatRequest(
        model=explicit_model, messages=[ChatMessage(role="user", content="Hi")]
    )

    await adapter.generate(request, api_key="test-key")

    args, kwargs = adapter.client.chat.completions.create.call_args
    assert kwargs["model"] == explicit_model


@pytest.mark.asyncio
async def test_413_maps_to_request_too_large_not_rate_limit():
    """413(Request too large)은 429(RateLimitError)와 분리된 RequestTooLargeError 여야
    한다 — 실측: 과거엔 RateLimitError 로 묶여 key_manager 가 키를 실패 처리(쿨다운)하고
    Gemini 로 넘어갔다가 403 → 클라이언트 300초 타임아웃으로 이어졌다."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model="test-model",
    )

    class TooLargeHTTPError(Exception):
        status_code = 413

    adapter.client.chat.completions.create = AsyncMock(
        side_effect=TooLargeHTTPError("request too large for model")
    )

    request = ChatRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="x" * 100)],
    )

    with pytest.raises(RequestTooLargeError):
        await adapter.generate(request, api_key="test-key")


@pytest.mark.asyncio
async def test_429_still_maps_to_rate_limit_error():
    """429 는 여전히 RateLimitError — 413 분리가 기존 분류를 깨지 않았는지 확인."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model="test-model",
    )

    class RateLimitHTTPError(Exception):
        status_code = 429

    adapter.client.chat.completions.create = AsyncMock(
        side_effect=RateLimitHTTPError("slow down")
    )

    request = ChatRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="hi")],
    )

    with pytest.raises(RateLimitError):
        await adapter.generate(request, api_key="test-key")


@pytest.mark.asyncio
async def test_429_carries_retry_after_from_header():
    """429 응답에 Retry-After 헤더가 있으면 RateLimitError.retry_after 에 실린다
    (실측: Groq 분당 토큰 한도 — key_manager 가 고정 1h 대신 이 값을 쿨다운으로 쓴다)."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model="test-model",
    )

    class Resp:
        headers = {"retry-after": "3.5"}

    class RateLimitHTTPError(Exception):
        status_code = 429
        response = Resp()

    adapter.client.chat.completions.create = AsyncMock(
        side_effect=RateLimitHTTPError("slow down")
    )

    request = ChatRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="hi")],
    )

    with pytest.raises(RateLimitError) as exc_info:
        await adapter.generate(request, api_key="test-key")

    assert exc_info.value.retry_after == 3.5


@pytest.mark.asyncio
async def test_402_maps_to_payment_required():
    """402(Payment required)는 429/413 과 분리된 PaymentRequiredError 여야 한다 — 403
    과 동일하게 key_manager 가 영구 제외하고, resilience_manager 가 같은 공급자
    재시도 없이 즉시 다음 공급자로 넘어가게 하기 위함."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model="test-model",
    )

    class PaymentHTTPError(Exception):
        status_code = 402

    adapter.client.chat.completions.create = AsyncMock(
        side_effect=PaymentHTTPError("payment required")
    )

    request = ChatRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="hi")],
    )

    with pytest.raises(PaymentRequiredError):
        await adapter.generate(request, api_key="test-key")


@pytest.mark.asyncio
async def test_probe_key_uses_chat_completion_when_model_resolvable():
    """모델을 특정할 수 있으면(Groq 처럼 default_model 보유) probe_key() 는 /models
    목록이 아니라 짧은 chat completion 1회로 확인한다 — /models 만으론 402(결제
    필요)를 못 잡는다(Cerebras 실측과 동일 우려). 실패 시 예외를 그대로 전파한다."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="k",
        default_model="openai/gpt-oss-120b",
    )
    adapter.client.models.list = AsyncMock(
        side_effect=AssertionError("모델을 특정할 수 있으면 /models 를 쓰면 안 됨")
    )

    class PaymentError(Exception):
        pass

    adapter.client.chat.completions.create = AsyncMock(side_effect=PaymentError("402"))

    with pytest.raises(PaymentError):
        await adapter.probe_key("k")

    adapter.client.chat.completions.create.assert_awaited_once()
    _, kwargs = adapter.client.chat.completions.create.call_args
    assert kwargs["model"] == "openai/gpt-oss-120b"
    assert kwargs.get("max_tokens") == 1


@pytest.mark.asyncio
async def test_probe_key_falls_back_to_models_list_when_model_unresolvable():
    """모델을 특정할 수 없으면(Ollama 처럼 discovery 도 실패) /models 목록 조회로
    최소한의 키 유효성만 확인한다."""
    adapter = OpenAICompatAdapter(base_url="http://localhost:11434/v1", api_key="ollama")
    adapter.discover_models = AsyncMock(return_value=[])  # discovery 도 빈 목록
    mock_response = MagicMock()
    mock_response.data = []
    adapter.client.models.list = AsyncMock(return_value=mock_response)
    adapter.client.chat.completions.create = AsyncMock(
        side_effect=AssertionError("모델을 못 정했으면 chat completion 을 쓰면 안 됨")
    )

    result = await adapter.probe_key("ollama")

    adapter.client.models.list.assert_awaited_once()
    assert result["model_count"] == 0


@pytest.mark.asyncio
async def test_explicit_model_name_is_sent_as_is_without_discovery():
    """구체 모델명이 명시되면 /models 탐색을 거치지 않고 그대로 사용한다(Cerebras 어댑터와
    동일 패턴)."""
    adapter = OpenAICompatAdapter(base_url="https://api.groq.com/openai/v1", api_key="k")
    adapter.discover_models = AsyncMock(side_effect=AssertionError("탐색이 호출되면 안 됨"))

    model = await adapter._resolve_model("qwen/qwen3.8-27b")
    assert model == "qwen/qwen3.8-27b"


@pytest.mark.asyncio
async def test_generic_hint_resolves_via_discovery_preferring_preferred_default():
    """default_model 이 없고 preferred_default_model 이 탐색 결과에 있으면 그것을
    쓴다(실측 가용 모델 openai/gpt-oss-120b 검증 없이 하드코딩하던 문제 회귀 가드)."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="k",
        preferred_default_model=DEFAULT_GROQ_MODEL,
    )
    adapter.discover_models = AsyncMock(
        return_value=[
            {"id": "allam-2-7b"},
            {"id": "openai/gpt-oss-120b"},
            {"id": "qwen/qwen3.8-27b"},
        ]
    )

    model = await adapter._resolve_model("auto")
    assert model == "openai/gpt-oss-120b" == DEFAULT_GROQ_MODEL
    adapter.discover_models.assert_awaited_once()

    # 캐시되어 두 번째 호출은 탐색을 다시 하지 않는다.
    model_again = await adapter._resolve_model(None)
    assert model_again == "openai/gpt-oss-120b"
    adapter.discover_models.assert_awaited_once()


@pytest.mark.asyncio
async def test_generic_hint_falls_back_to_first_discovered_when_preferred_missing():
    """preferred_default_model 이 탐색 결과에 없으면(아직 없는 모델을 희망값으로 줬거나
    공급자가 그 사이 모델을 뺐을 때) 탐색된 첫 모델로 대체한다."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="k",
        preferred_default_model="not-a-real-model",
    )
    adapter.discover_models = AsyncMock(return_value=[{"id": "allam-2-7b"}])

    model = await adapter._resolve_model("llama")
    assert model == "allam-2-7b"


@pytest.mark.asyncio
async def test_discovery_failure_falls_back_to_preferred_default_constant():
    """탐색 자체가 실패해도(네트워크 오류 등) preferred_default_model 상수로는 떨어진다 —
    과거처럼 "존재하지 않는 모델명을 하드코딩"하는 상태로 되돌아가지 않는다."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="k",
        preferred_default_model=DEFAULT_GROQ_MODEL,
    )
    adapter.discover_models = AsyncMock(return_value=[])

    model = await adapter._resolve_model("")
    assert model == DEFAULT_GROQ_MODEL == "openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_old_stale_model_name_is_treated_as_generic_hint():
    """더 이상 존재하지 않는 구 모델명(llama-3.3-70b-versatile)을 명시적으로 요청해도
    그대로 전송하지 않고 범용 힌트로 취급해 discovery 로 넘긴다."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="k",
        preferred_default_model=DEFAULT_GROQ_MODEL,
    )
    adapter.discover_models = AsyncMock(return_value=[{"id": "openai/gpt-oss-120b"}])

    model = await adapter._resolve_model("llama-3.3-70b-versatile")
    assert model == "openai/gpt-oss-120b"
    assert model != "llama-3.3-70b-versatile"


def _mock_openai_response(
    content: str = "ok", reasoning_tokens: int | None = None
) -> MagicMock:
    mock_choice = MagicMock()
    mock_choice.message.content = content
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
    mock_response.object = "chat.completion"
    mock_response.created = 12345
    mock_response.model = "openai/gpt-oss-120b"
    mock_response.usage = mock_usage
    return mock_response


@pytest.mark.asyncio
async def test_groq_passes_through_response_format_and_reasoning_effort():
    """실측(2026-10): Groq 는 response_format(json_object)·reasoning_effort 를
    지원한다(실제 API 호출 성공 + reasoning_tokens 응답 확인). MovieSpoiler 수집기
    v2 가 요청 바디에 넣는 그대로 전달돼야 한다."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model="openai/gpt-oss-120b",
    )
    adapter.client.chat.completions.create = AsyncMock(
        return_value=_mock_openai_response()
    )

    request = ChatRequest(
        model="openai/gpt-oss-120b",
        messages=[ChatMessage(role="user", content="hi")],
        max_tokens=50,
        response_format={"type": "json_object"},
        reasoning_effort="low",
    )

    await adapter.generate(request, api_key="test-key")

    _, kwargs = adapter.client.chat.completions.create.call_args
    assert kwargs["max_tokens"] == 50
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_non_groq_endpoint_does_not_receive_unverified_fields():
    """Groq 가 아닌 base_url(예: Ollama, 같은 어댑터 클래스 공유)에는
    response_format/reasoning_effort 지원이 실측되지 않았으므로 보내지 않는다 —
    "모르는 필드를 공급자별로 지원할 때만 전달"."""
    adapter = OpenAICompatAdapter(
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        default_model="llama3",
    )
    adapter.client.chat.completions.create = AsyncMock(
        return_value=_mock_openai_response()
    )

    request = ChatRequest(
        model="llama3",
        messages=[ChatMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
        reasoning_effort="low",
    )

    await adapter.generate(request, api_key="ollama")

    _, kwargs = adapter.client.chat.completions.create.call_args
    assert kwargs["response_format"] is None
    assert kwargs["reasoning_effort"] is None


@pytest.mark.asyncio
async def test_reasoning_tokens_preserved_in_usage_when_present():
    """실측: reasoning_effort 로 추론을 켠 gpt-oss 응답은
    usage.completion_tokens_details.reasoning_tokens 를 돌려준다 — 클라이언트 응답의
    usage 에 그대로 보존돼야 한다."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model="openai/gpt-oss-120b",
    )
    adapter.client.chat.completions.create = AsyncMock(
        return_value=_mock_openai_response(reasoning_tokens=7)
    )

    request = ChatRequest(
        model="openai/gpt-oss-120b",
        messages=[ChatMessage(role="user", content="hi")],
        reasoning_effort="low",
    )

    response = await adapter.generate(request, api_key="test-key")

    assert response.usage["reasoning_tokens"] == 7
    assert response.usage["completion_tokens"] == 5
    assert response.choices[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_usage_omits_reasoning_tokens_when_absent():
    """reasoning_tokens 가 없는(일반 모델) 응답은 usage 에 그 키를 억지로 넣지
    않는다(None 오염 방지)."""
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model="openai/gpt-oss-120b",
    )
    adapter.client.chat.completions.create = AsyncMock(
        return_value=_mock_openai_response(reasoning_tokens=None)
    )

    request = ChatRequest(
        model="openai/gpt-oss-120b",
        messages=[ChatMessage(role="user", content="hi")],
    )

    response = await adapter.generate(request, api_key="test-key")

    assert "reasoning_tokens" not in response.usage
