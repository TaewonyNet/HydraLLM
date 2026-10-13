from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.exceptions import (
    PaymentRequiredError,
    RateLimitError,
    RequestTooLargeError,
    ResourceExhaustedError,
)
from src.domain.enums import ProviderType
from src.domain.models import ChatRequest, ChatResponse, RoutingDecision
from src.services.analyzer import ContextAnalyzer
from src.services.resilience_manager import ResilienceManager


@pytest.mark.asyncio
async def test_resilience_manager_retry_logic():
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = []

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    mock_adapter = AsyncMock()
    mock_response = MagicMock(spec=ChatResponse)
    mock_response.usage = {}
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Success"
    mock_response.model = "test-model"
    mock_adapter.generate.side_effect = [Exception("api_key_invalid"), mock_response]

    get_adapter = MagicMock(return_value=mock_adapter)
    mock_agent_processor = AsyncMock(return_value=mock_response)
    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=mock_agent_processor
    )


    key_manager.get_next_key = AsyncMock(return_value="test-key")
    key_manager.report_success = AsyncMock()
    key_manager.report_failure = AsyncMock()
    key_manager.get_available_keys_count.return_value = 1

    response, parts = await rm.execute_with_resilience(request=ChatRequest(model="test", messages=[]), decision=RoutingDecision(provider=ProviderType.GEMINI, model_name="gemini-pro", reason="test"))

    assert response == mock_response
    assert mock_adapter.generate.call_count == 2
    key_manager.report_failure.assert_called_once()
    key_manager.report_success.assert_called_once()

@pytest.mark.asyncio
async def test_resilience_manager_fallback_to_next_provider():
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["gemini", "groq"]
    analyzer.get_default_model_for_provider.return_value = "groq-model"

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    gemini_adapter = AsyncMock()
    gemini_adapter.generate.side_effect = Exception("Gemini Down")

    groq_adapter = AsyncMock()
    mock_response = MagicMock(spec=ChatResponse)
    mock_response.usage = {}
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Groq Success"
    mock_response.model = "groq-model"
    groq_adapter.generate.return_value = mock_response

    def get_adapter(provider, api_key):
        if provider == ProviderType.GEMINI:
            return gemini_adapter
        if provider == ProviderType.GROQ:
            return groq_adapter
        return None

    mock_agent_processor = AsyncMock(return_value=mock_response)

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=mock_agent_processor
    )

    key_manager.get_next_key = AsyncMock(return_value="key")
    key_manager.get_available_keys_count.side_effect = [1, 1, 1, 0, 1, 1, 1, 0]

    request = ChatRequest(model="auto", messages=[])
    decision = RoutingDecision(provider=ProviderType.GEMINI, model_name="gemini-pro", reason="test")

    response, parts = await rm.execute_with_resilience(request, decision)

    assert response == mock_response
    assert gemini_adapter.generate.call_count == 3
    assert response.model == "groq-model"


@pytest.mark.asyncio
async def test_no_cloud_keys_falls_back_to_local_agent():
    """클라우드 키가 하나도 없으면 auto 요청은 클라우드 시도를 건너뛰고 즉시
    로컬(Ollama) final_fallback 한다(graceful degradation·로컬 주권)."""
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["gemini", "groq", "cerebras"]

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    cloud_adapter = AsyncMock()  # 호출되면 안 됨
    get_adapter = MagicMock(return_value=cloud_adapter)

    agent_response = MagicMock(spec=ChatResponse)
    agent_response.usage = {}
    agent_response.choices = [MagicMock()]
    agent_response.choices[0].message.content = "local"
    agent_response.model = "LOCAL-AGENT/OLLAMA/llama3"
    mock_agent_processor = AsyncMock(return_value=agent_response)

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=mock_agent_processor,
    )
    key_manager.get_available_keys_count.return_value = 0  # 모든 클라우드 키 부재
    key_manager.get_next_key = AsyncMock()

    request = ChatRequest(model="auto", messages=[])
    decision = RoutingDecision(
        provider=ProviderType.GEMINI, model_name="gemini-pro", reason="test"
    )
    response, _ = await rm.execute_with_resilience(request, decision)

    assert response == agent_response
    mock_agent_processor.assert_awaited()  # final_fallback 경유
    cloud_adapter.generate.assert_not_called()  # 클라우드 시도 건너뜀


@pytest.mark.asyncio
async def test_413_switches_to_cerebras_without_failing_the_key():
    """Groq 가 413(요청 과대)을 내면 키를 실패 처리하지 않고 더 큰 한도 공급자
    (Cerebras)로 즉시 전환해야 한다(실측 사고: 이전엔 413 을 RateLimitError 로 묶어
    키를 쿨다운시키고 Gemini 로 넘어갔다가 403 → 클라이언트 300초 타임아웃)."""
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["groq", "cerebras"]
    analyzer.get_default_model_for_provider.return_value = "gpt-oss-120b"

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    groq_adapter = AsyncMock()
    groq_adapter.generate.side_effect = RequestTooLargeError("413 payload too large")

    cerebras_adapter = AsyncMock()
    mock_response = MagicMock(spec=ChatResponse)
    mock_response.usage = {}
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Cerebras Success"
    mock_response.model = "gpt-oss-120b"
    cerebras_adapter.generate.return_value = mock_response

    def get_adapter(provider, api_key):
        if provider == ProviderType.GROQ:
            return groq_adapter
        if provider == ProviderType.CEREBRAS:
            return cerebras_adapter
        return None

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=AsyncMock(),
    )

    key_manager.get_next_key = AsyncMock(return_value="key")
    key_manager.report_success = AsyncMock()
    key_manager.report_failure = AsyncMock()
    key_manager.get_available_keys_count = MagicMock(
        side_effect=lambda p: {
            ProviderType.GEMINI: 0,
            ProviderType.GROQ: 1,
            ProviderType.CEREBRAS: 1,
        }.get(p, 0)
    )

    request = ChatRequest(model="auto", messages=[])
    decision = RoutingDecision(provider=ProviderType.GROQ, model_name="groq-model", reason="test")

    response, _ = await rm.execute_with_resilience(request, decision)

    assert response == mock_response
    groq_adapter.generate.assert_awaited_once()
    cerebras_adapter.generate.assert_awaited_once()
    key_manager.report_failure.assert_not_called()  # 413 은 키 실패가 아니다
    breakers[ProviderType.GROQ].report_failure.assert_not_called()


@pytest.mark.asyncio
async def test_413_without_bigger_provider_raises_immediately():
    """더 큰 한도 공급자(Cerebras)가 없거나 이미 소진됐으면, Gemini/Ollama 폴백 체인으로
    계속 넘기지 않고 즉시 RequestTooLargeError 를 전파해야 한다(무기한 폴백 →
    클라이언트 타임아웃 방지)."""
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["groq", "cerebras"]
    analyzer.get_default_model_for_provider.return_value = "gpt-oss-120b"

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    groq_adapter = AsyncMock()
    groq_adapter.generate.side_effect = RequestTooLargeError("413 payload too large")
    cerebras_adapter = AsyncMock()  # 키 없음 → 호출되면 안 됨

    def get_adapter(provider, api_key):
        if provider == ProviderType.GROQ:
            return groq_adapter
        if provider == ProviderType.CEREBRAS:
            return cerebras_adapter
        return None

    mock_agent_processor = AsyncMock()

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=mock_agent_processor,
    )

    key_manager.get_next_key = AsyncMock(return_value="key")
    key_manager.get_available_keys_count = MagicMock(
        side_effect=lambda p: {
            ProviderType.GEMINI: 0,
            ProviderType.GROQ: 1,
            ProviderType.CEREBRAS: 0,  # 큰 한도 공급자도 키가 없음
        }.get(p, 0)
    )

    request = ChatRequest(model="auto", messages=[])
    decision = RoutingDecision(provider=ProviderType.GROQ, model_name="groq-model", reason="test")

    with pytest.raises(RequestTooLargeError):
        await rm.execute_with_resilience(request, decision)

    cerebras_adapter.generate.assert_not_called()
    mock_agent_processor.assert_not_awaited()  # final_fallback(Ollama) 으로 새지 않음
    key_manager.report_failure.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "groq_model,expected_cerebras_model",
    [
        # Cerebras 기본 모델(gpt-oss-120b)과 "우연히 같은" 경우.
        ("openai/gpt-oss-120b", "gpt-oss-120b"),
        # Cerebras 기본 모델과 다른 경우 — 기본값으로 덮어쓰는 낡은 동작이면 여기서
        # 걸린다(회귀 가드의 핵심: qwen 요청이 gpt-oss-120b 로 바뀌면 안 됨).
        ("qwen/qwen3.8-27b", "qwen-3.8-27b"),
    ],
)
async def test_413_switch_translates_groq_model_to_cerebras_equivalent(
    groq_model, expected_cerebras_model
):
    """Groq 모델로 413 → Cerebras 전환 시, Cerebras 의 고정 기본 모델이 아니라 "같은
    모델"의 Cerebras 이름으로 가야 한다(회귀 가드: 이전엔 provider 전환 시 항상
    analyzer.get_default_model_for_provider() 의 고정 기본값으로 덮어써서, qwen 처럼
    기본값과 다른 모델을 요청했을 때도 엉뚱한 모델로 바뀌었다). 실제 ContextAnalyzer
    (목이 아님)를 써서 get_equivalent_model_for_provider 매핑이 실제로 적용되는지
    종단적으로 확인한다."""
    key_manager = MagicMock()
    analyzer = ContextAnalyzer(max_tokens_fast_model=8192)
    analyzer._provider_priority = ["groq", "cerebras"]
    analyzer.provider_priority = ["groq", "cerebras"]

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    groq_adapter = AsyncMock()
    groq_adapter.generate.side_effect = RequestTooLargeError("413 payload too large")

    cerebras_adapter = AsyncMock()
    mock_response = MagicMock(spec=ChatResponse)
    mock_response.usage = {}
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Cerebras Success"
    mock_response.model = expected_cerebras_model
    cerebras_adapter.generate.return_value = mock_response

    def get_adapter(provider, api_key):
        if provider == ProviderType.GROQ:
            return groq_adapter
        if provider == ProviderType.CEREBRAS:
            return cerebras_adapter
        return None

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=AsyncMock(),
    )

    key_manager.get_next_key = AsyncMock(return_value="key")
    key_manager.report_success = AsyncMock()
    key_manager.report_failure = AsyncMock()
    key_manager.get_available_keys_count = MagicMock(
        side_effect=lambda p: {
            ProviderType.GEMINI: 0,
            ProviderType.GROQ: 1,
            ProviderType.CEREBRAS: 1,
        }.get(p, 0)
    )

    # MovieSpoiler 수집기 v2 가 실제로 보내는 그대로: provider 접두사 없는 Groq 실측
    # 모델명.
    request = ChatRequest(model=groq_model, messages=[])
    decision = RoutingDecision(
        provider=ProviderType.GROQ, model_name=groq_model, reason="model_hint"
    )

    response, _ = await rm.execute_with_resilience(request, decision)

    assert response == mock_response
    cerebras_adapter.generate.assert_awaited_once()
    forwarded_request = cerebras_adapter.generate.call_args[0][0]
    assert forwarded_request.model == expected_cerebras_model
    assert decision.model_name == expected_cerebras_model


@pytest.mark.asyncio
async def test_payment_required_excludes_key_without_retry_and_moves_on():
    """402(결제 필요)는 403 과 동일하게 해당 키를 영구 제외하고, 같은 공급자의 다른
    키/재시도 없이 즉시 다음 공급자로 넘어가야 한다(3회 재시도 금지). 실측 사고:
    이전엔 402 가 분류되지 않아 같은 키로 3회(1초 간격) 낭비 재시도됐다."""
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["cerebras", "groq"]
    analyzer.get_equivalent_model_for_provider.return_value = "openai/gpt-oss-120b"

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    cerebras_adapter = AsyncMock()
    cerebras_adapter.generate.side_effect = PaymentRequiredError("402 payment required")

    groq_adapter = AsyncMock()
    mock_response = MagicMock(spec=ChatResponse)
    mock_response.usage = {}
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Groq Success"
    mock_response.model = "openai/gpt-oss-120b"
    groq_adapter.generate.return_value = mock_response

    def get_adapter(provider, api_key):
        if provider == ProviderType.CEREBRAS:
            return cerebras_adapter
        if provider == ProviderType.GROQ:
            return groq_adapter
        return None

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=AsyncMock(),
    )

    key_manager.get_next_key = AsyncMock(return_value="cerebras-key")
    key_manager.report_success = AsyncMock()
    key_manager.report_failure = AsyncMock()
    key_manager.get_available_keys_count = MagicMock(
        side_effect=lambda p: {
            ProviderType.GEMINI: 0,
            ProviderType.GROQ: 1,
            ProviderType.CEREBRAS: 1,
        }.get(p, 0)
    )

    request = ChatRequest(model="gpt-oss-120b", messages=[])
    decision = RoutingDecision(
        provider=ProviderType.CEREBRAS, model_name="gpt-oss-120b", reason="model_hint"
    )

    response, _ = await rm.execute_with_resilience(request, decision)

    assert response == mock_response
    # 같은 공급자(Cerebras) 재시도 없이 1회만 호출 — 3회 재시도 금지.
    cerebras_adapter.generate.assert_awaited_once()
    groq_adapter.generate.assert_awaited_once()
    key_manager.report_failure.assert_called_once()
    breakers[ProviderType.CEREBRAS].report_failure.assert_called_once()


@pytest.mark.asyncio
async def test_payment_required_is_strict_reraises_without_fallback():
    """strict 라우팅(명시적 provider/model)이면 402 도 교차 폴백 없이 그대로 전파."""
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["cerebras"]

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    cerebras_adapter = AsyncMock()
    cerebras_adapter.generate.side_effect = PaymentRequiredError("402 payment required")

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=lambda p, k: cerebras_adapter,
        process_with_agent=AsyncMock(),
    )

    key_manager.get_next_key = AsyncMock(return_value="cerebras-key")
    key_manager.report_failure = AsyncMock()
    key_manager.get_available_keys_count = MagicMock(return_value=1)

    request = ChatRequest(model="cerebras/gpt-oss-120b", messages=[])
    decision = RoutingDecision(
        provider=ProviderType.CEREBRAS,
        model_name="gpt-oss-120b",
        reason="model_hint",
        strict=True,
    )

    with pytest.raises(PaymentRequiredError):
        await rm.execute_with_resilience(request, decision)

    cerebras_adapter.generate.assert_awaited_once()
    key_manager.report_failure.assert_called_once()  # strict 여도 키는 영구 제외 기록


@pytest.mark.asyncio
async def test_413_does_not_switch_to_cerebras_when_already_payment_excluded():
    """Cerebras 키가 (앞선 요청에서) 실제로 402 로 영구 제외된 뒤라면, 413 전환
    대상에서 빠지고 즉시 413 을 반환해야 한다 — 결제 막힌 키로 전환하는 건
    무의미하다. 실제 KeyManager 를 써서 "402 영구 제외 → 활성 키 0 → 413 전환
    차단"이 끝까지 이어지는지 종단적으로 확인한다(목으로 카운트만 흉내내는 게
    아니라 진짜 report_failure 가 만든 상태로 검증)."""
    from src.services.key_manager import KeyManager

    key_manager = KeyManager()
    key_manager.add_keys(ProviderType.GROQ, ["groq-key"])
    key_manager.add_keys(ProviderType.CEREBRAS, ["cerebras-key"])

    # 앞선 요청에서 이미 402 를 맞아 영구 제외된 상태를 실제로 만든다.
    await key_manager.report_failure(
        ProviderType.CEREBRAS, "cerebras-key", PaymentRequiredError("402 payment required")
    )
    assert key_manager.get_available_keys_count(ProviderType.CEREBRAS) == 0
    assert key_manager.is_permanently_excluded(ProviderType.CEREBRAS, "cerebras-key")

    analyzer = MagicMock()
    analyzer.provider_priority = ["groq", "cerebras"]

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    groq_adapter = AsyncMock()
    groq_adapter.generate.side_effect = RequestTooLargeError("413 payload too large")
    cerebras_adapter = AsyncMock()  # 호출되면 안 됨(활성 키 0)

    def get_adapter(provider, api_key):
        if provider == ProviderType.GROQ:
            return groq_adapter
        if provider == ProviderType.CEREBRAS:
            return cerebras_adapter
        return None

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=AsyncMock(),
    )

    request = ChatRequest(model="openai/gpt-oss-120b", messages=[])
    decision = RoutingDecision(
        provider=ProviderType.GROQ, model_name="openai/gpt-oss-120b", reason="model_hint"
    )

    with pytest.raises(RequestTooLargeError):
        await rm.execute_with_resilience(request, decision)

    cerebras_adapter.generate.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_429_does_not_trip_circuit_breaker():
    """429(레이트리밋)는 공급자 장애가 아니다 — circuit breaker 를 OPEN 시키지 않고
    키 단위 쿨다운만 걸어야 한다(실측 2026-10: Groq 429 가 난 같은 시각 직접 호출(키
    2개·모델 3개)은 전부 200). breaker.report_failure() 가 호출되면 HALF_OPEN 프로브도
    같은 429 를 맞아 즉시 재OPEN 되는 루프에 빠져 10분 넘게 막혔던 사고의 직접 원인."""
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["groq"]

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    groq_adapter = AsyncMock()
    groq_adapter.generate.side_effect = RateLimitError("429 rate limit", retry_after=5)

    mock_agent_processor = AsyncMock()
    response = MagicMock()
    mock_agent_processor.return_value = response

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=lambda p, k: groq_adapter,
        process_with_agent=mock_agent_processor,
    )

    key_manager.get_next_key = AsyncMock(return_value="groq-key")
    key_manager.report_failure = AsyncMock()
    key_manager.get_available_keys_count = MagicMock(return_value=1)

    request = ChatRequest(model="auto", messages=[])
    decision = RoutingDecision(provider=ProviderType.GROQ, model_name="groq-model", reason="test")

    # max_retries(3)만큼 전부 429 → final_fallback(Ollama) 으로 떨어짐.
    result, _ = await rm.execute_with_resilience(request, decision)

    assert result == response
    assert groq_adapter.generate.await_count == 3  # 3회 재시도(기존 동작 유지)
    key_manager.report_failure.assert_called()  # 키 단위 쿨다운은 여전히 기록
    breakers[ProviderType.GROQ].report_failure.assert_not_called()  # breaker 는 무관


@pytest.mark.asyncio
async def test_rate_limit_429_strict_reraises_with_retry_after():
    """strict 모드에서 429 는 교차 폴백 없이 재-raise 되고, retry_after 가 보존돼야
    한다(endpoints.py 가 HTTP 429 Retry-After 헤더로 쓴다)."""
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["groq"]

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    groq_adapter = AsyncMock()
    groq_adapter.generate.side_effect = RateLimitError("429 rate limit", retry_after=12.5)

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=lambda p, k: groq_adapter,
        process_with_agent=AsyncMock(),
    )

    key_manager.get_next_key = AsyncMock(return_value="groq-key")
    key_manager.report_failure = AsyncMock()
    key_manager.get_available_keys_count = MagicMock(return_value=1)

    request = ChatRequest(model="groq/openai/gpt-oss-120b", messages=[])
    decision = RoutingDecision(
        provider=ProviderType.GROQ,
        model_name="openai/gpt-oss-120b",
        reason="model_hint",
        strict=True,
    )

    with pytest.raises(RateLimitError) as exc_info:
        await rm.execute_with_resilience(request, decision)

    assert exc_info.value.retry_after == 12.5
    groq_adapter.generate.assert_awaited_once()  # strict 라 교차 폴백/재시도 없이 1회
    breakers[ProviderType.GROQ].report_failure.assert_not_called()


@pytest.mark.asyncio
async def test_strict_circuit_open_rejection_carries_remaining_retry_after():
    """strict 모드에서 Circuit OPEN 이면 ResourceExhaustedError 가 breaker 의 남은
    복구 시간을 retry_after 로 싣고 올라간다(endpoints.py 가 503 Retry-After 로 반영)."""
    from src.services.circuit_breaker import CircuitBreaker

    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["groq"]

    real_breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    real_breaker.report_failure()  # OPEN 으로 전환
    assert real_breaker.state == "OPEN"

    breakers = {p: MagicMock() for p in ProviderType}
    breakers[ProviderType.GROQ] = real_breaker

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=lambda p, k: AsyncMock(),
        process_with_agent=AsyncMock(),
    )

    key_manager.get_available_keys_count = MagicMock(return_value=1)

    request = ChatRequest(model="groq/openai/gpt-oss-120b", messages=[])
    decision = RoutingDecision(
        provider=ProviderType.GROQ,
        model_name="openai/gpt-oss-120b",
        reason="model_hint",
        strict=True,
    )

    with pytest.raises(ResourceExhaustedError) as exc_info:
        await rm.execute_with_resilience(request, decision)

    assert exc_info.value.retry_after is not None
    assert 55 < exc_info.value.retry_after <= 60
