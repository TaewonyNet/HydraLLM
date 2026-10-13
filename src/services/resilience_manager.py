import asyncio
import logging
from typing import Any

from src.core.exceptions import (
    PaymentRequiredError,
    RateLimitError,
    RequestTooLargeError,
    ResourceExhaustedError,
    ServiceUnavailableError,
)
from src.domain.enums import AgentType, ProviderType
from src.domain.models import ChatRequest, ChatResponse, RoutingDecision
from src.services.comm_logger import comm_log_buffer

logger = logging.getLogger(__name__)

class ResilienceManager:
    def __init__(
        self,
        key_manager,
        analyzer,
        breakers,
        session_manager,
        max_retries: int = 3,
        get_provider_adapter: Any = None,
        process_with_agent: Any = None,
    ):
        self.key_manager = key_manager
        self.analyzer = analyzer
        self.breakers = breakers
        self.session_manager = session_manager
        self.max_retries = max_retries
        self._get_provider_adapter = get_provider_adapter
        self._process_with_agent = process_with_agent

    async def execute_with_resilience(
        self, request: ChatRequest, decision: RoutingDecision
    ) -> tuple[ChatResponse, list[dict[str, Any]]]:
        retry_parts: list[dict[str, Any]] = []

        # strict 판정은 analyzer 가 원본 힌트로 결정해 decision 에 실어준다(S1). 여기서
        # request.model(이미 decision.model_name 으로 덮어쓰임)을 재파싱하지 않는다.
        is_strict = decision.strict
        if is_strict:
            logger.info(
                f"Using STRICT routing (decision.strict) for "
                f"provider={decision.provider} agent={decision.agent}"
            )

        if is_strict and decision.agent:
            try:
                return await self._process_with_agent(request, decision), retry_parts
            except Exception as e:
                await self.session_manager.log_system_event(
                    level="ERROR",
                    category="AGENT_EXECUTION",
                    message=f"Strict agent execution failed: {str(e)[:100]}",
                    metadata={
                        "error": str(e),
                        "agent": decision.agent.value,
                        "model": decision.model_name,
                        "session_id": request.session_id
                    }
                )
                raise

        if not is_strict and decision.agent:
            try:
                return await self._process_with_agent(request, decision), retry_parts
            except Exception as e:
                await self.session_manager.log_system_event(
                    level="WARNING",
                    category="AGENT_EXECUTION",
                    message=f"Agent execution failed, falling back to cloud: {str(e)[:100]}",
                    metadata={
                        "error": str(e),
                        "agent": decision.agent.value,
                        "model": decision.model_name,
                        "session_id": request.session_id
                    }
                )
                logger.error(f"Agent execution failed, attempting cloud fallback: {e}")
                decision.agent = None

        providers_to_try = []
        if decision.provider:
            providers_to_try.append(decision.provider)


        # 비-strict(auto/별칭) 요청은 클라우드 키가 전무하면 로컬로 우아하게 저하한다.
        if not providers_to_try or not is_strict:
            has_any_cloud_active = False
            for p in [ProviderType.GEMINI, ProviderType.GROQ, ProviderType.CEREBRAS]:
                if self.key_manager.get_available_keys_count(p) > 0:
                    has_any_cloud_active = True
                    break
            if not has_any_cloud_active and not is_strict:
                logger.warning("No cloud keys available. Skipping cloud attempts.")
                return await self.final_fallback(request, decision), retry_parts

        if not is_strict:
            for p_name in self.analyzer.provider_priority:
                try:
                    p_type = ProviderType(p_name)
                    if p_type not in providers_to_try:
                        providers_to_try.append(p_type)
                except (ValueError, KeyError):
                    continue

        if decision.agent:
            try:
                return await self._process_with_agent(request, decision), retry_parts
            except Exception as e:
                if is_strict:
                    raise
                logger.error(f"Agent execution failed, attempting cloud fallback: {e}")

        # 413(요청 과대)이 났을 때 "더 큰 한도 공급자(Cerebras)를 이미 시도했는지"
        # 판단하기 위한 기록(R-413). providers_to_try 재배치와 무관하게 유지된다.
        attempted_providers: set[ProviderType] = set()

        for provider_type in providers_to_try:
            attempted_providers.add(provider_type)
            if not self.breakers[provider_type].is_available():
                logger.warning(f"Skipping {provider_type.value}: Circuit is OPEN")
                if is_strict:
                    # 남은 복구 시간을 실어 클라이언트가 Retry-After 로 "언제 다시
                    # 시도하면 될지" 알 수 있게 한다(endpoints.py 가 HTTP 헤더로 반영).
                    remaining = self.breakers[provider_type].remaining_recovery_seconds()
                    msg = f"Strict provider {provider_type.value} is currently unavailable (Circuit OPEN)"
                    raise ResourceExhaustedError(msg, retry_after=remaining)
                continue

            if provider_type != decision.provider:
                # "같은 모델을 다른 공급자 이름으로" 유지(가능하면) — 예: Groq
                # openai/gpt-oss-120b 로 413 → Cerebras gpt-oss-120b. 동일 모델이 없으면
                # 해당 공급자의 기본 모델로 대체(기존 동작과 동일한 최후 폴백).
                new_model = self.analyzer.get_equivalent_model_for_provider(
                    request.model, decision.provider, provider_type
                )
                logger.info(f"Switching provider to {provider_type.value}, model to {new_model}")
                request.model = new_model
                decision.model_name = new_model
                decision.provider = provider_type

            if not is_strict and decision.agent and provider_type == providers_to_try[0]:
                continue

            for attempt in range(self.max_retries):
                api_key: str | None = None
                try:
                    api_key = await self.key_manager.get_next_key(provider_type)
                    adapter = self._get_provider_adapter(provider_type, api_key)

                    comm_log_buffer.record(
                        "request",
                        provider_type.value,
                        {
                            "model": request.model,
                            "messages_count": len(request.messages or []),
                            "has_search": request.has_search,
                            "stream": request.stream,
                        },
                    )

                    response = await adapter.generate(request, api_key)
                    if response and response.choices and len(response.choices) > 0:
                        comm_log_buffer.record(
                            "response",
                            provider_type.value,
                            {
                                "model": response.model,
                                "finish_reasons": [
                                    c.finish_reason for c in (response.choices or [])
                                ],
                                "usage": response.usage,
                            },
                        )

                        self.breakers[provider_type].report_success()
                        await self.key_manager.report_success(provider_type, api_key)
                        self.enrich_response_usage(response, provider_type, api_key, decision)
                        return response, retry_parts
                    else:
                        msg = "Empty response from adapter"
                        raise RuntimeError(msg)

                except RateLimitError as e:
                    # 429(레이트리밋)는 공급자 "장애"가 아니라 그 키의 분당/일일 한도
                    # 문제다(실측 2026-10: Groq 가 429 를 낸 같은 시각 직접 호출(키
                    # 2개·모델 3개)은 전부 200 이었다). 그래서 circuit breaker 를
                    # 건드리지 않는다 — breaker.report_failure() 를 호출하면 HALF_OPEN
                    # 프로브도 같은 429 를 맞아 즉시 재OPEN 되는 루프에 빠져(실측:
                    # 10분 넘게 막힘) 공급자가 실제로는 정상인데도 영구히 거부된다.
                    # 키 단위 쿨다운만(key_manager.report_failure, retry_after 반영).
                    if api_key:
                        await self.key_manager.report_failure(provider_type, api_key, e)

                    if is_strict:
                        raise e

                    if self.key_manager.get_available_keys_count(provider_type) == 0:
                        logger.warning(f"Provider {provider_type.value} exhausted. Checking fallback...")
                        break

                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(1)

                except ResourceExhaustedError as e:
                    # get_next_key 가 "활성 키 0개"로 못 뽑은 경우(공급자 호출 자체가
                    # 없었음) — 지금 이 공급자에 쓸 수 있는 키가 없다는 사실 자체는
                    # breaker 에도 유효한 신호라 기존처럼 집계한다(429 와는 원인이
                    # 다르다: 이건 "자원이 없음", 429 는 "그 키가 지금 한도에 걸림").
                    self.breakers[provider_type].report_failure()
                    if api_key:
                        await self.key_manager.report_failure(provider_type, api_key, e)

                    if is_strict:
                        raise e

                    if self.key_manager.get_available_keys_count(provider_type) == 0:
                        logger.warning(f"Provider {provider_type.value} exhausted. Checking fallback...")
                        break

                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(1)

                except RequestTooLargeError as e:
                    # 413 은 키/계정 문제가 아니라 "이 요청이 이 공급자 한도보다 크다"는
                    # 뜻이다 — key_manager.report_failure 를 호출하지 않고(키는 정상),
                    # breaker 도 건드리지 않는다(실측 사고: 이전엔 키를 실패 처리해
                    # Gemini 로 넘어갔다가 403 → 클라이언트 300초 타임아웃).
                    logger.warning(f"{provider_type.value} 페이로드 과대(413): {e}")

                    if is_strict:
                        raise e

                    bigger_limit_provider = ProviderType.CEREBRAS
                    if (
                        bigger_limit_provider not in attempted_providers
                        and bigger_limit_provider in providers_to_try
                        and self.key_manager.get_available_keys_count(
                            bigger_limit_provider
                        )
                        > 0
                    ):
                        # 더 큰 한도 공급자(Cerebras)를 바로 다음 순서로 당겨와 즉시
                        # 전환한다(providers_to_try 를 제자리에서 재배치 — for 루프는
                        # 인덱스 기반이라 다음 반복에 반영된다).
                        idx = providers_to_try.index(provider_type)
                        if bigger_limit_provider in providers_to_try[idx + 1 :]:
                            providers_to_try.remove(bigger_limit_provider)
                            providers_to_try.insert(idx + 1, bigger_limit_provider)
                        logger.info(
                            f"{provider_type.value} 413 → 더 큰 한도 공급자 "
                            f"{bigger_limit_provider.value} 로 즉시 전환"
                        )
                        break  # 같은 공급자 재시도는 무의미 → 다음(=더 큰 한도) 공급자로

                    # 더 큰 한도 공급자가 없거나 이미 413 이었다 → 폴백 체인(Gemini/Ollama)
                    # 으로 계속 넘기지 않고 즉시 클라이언트에 명확한 오류를 돌려준다
                    # (무기한 폴백 → 클라이언트 타임아웃 방지).
                    raise e

                except PaymentRequiredError as e:
                    # 402(결제/빌링 문제)는 403 과 동일하게 재시도해도 안 풀리는 계정
                    # 설정 문제다(실측: Cerebras 키가 /models 는 정상, chat
                    # completions 는 402 "Payment required"). 같은 키 재시도는 물론
                    # 같은 공급자의 다른 키도 시도하지 않고(해당 공급자를 즉시
                    # attempted 로 간주), 바로 다음 공급자로 넘어간다 — 계정 단위
                    # 결제 문제라 같은 계정의 다른 키도 막혀 있을 가능성이 높다.
                    logger.error(
                        f"{provider_type.value} 결제 필요(402) — 키 영구 제외, "
                        f"같은 공급자 재시도 없이 다음 공급자로: {e}"
                    )
                    self.breakers[provider_type].report_failure()
                    if api_key:
                        await self.key_manager.report_failure(provider_type, api_key, e)

                    if is_strict:
                        raise e

                    break  # 3회 재시도 금지 — 같은 provider 건너뛰고 다음으로

                except ServiceUnavailableError as e:
                    logger.error(f"Service error with {provider_type.value}: {e}")
                    self.breakers[provider_type].report_failure()
                    if api_key:
                        await self.key_manager.report_failure(provider_type, api_key, e)

                    if is_strict:
                        raise e

                    if self.key_manager.get_available_keys_count(provider_type) == 0:
                        break
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(0.01)

                except Exception as e:
                    logger.error(f"Unexpected error with {provider_type.value}: {e}")
                    err_msg = str(e).lower()
                    if api_key and (
                        "403" in err_msg
                        or "denied" in err_msg
                        or "api_key_invalid" in err_msg
                        or "unauthorized" in err_msg
                    ):
                        await self.key_manager.report_failure(provider_type, api_key, e)

                    if is_strict:
                        raise e

                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(1)
                    else:
                        break

        return await self.final_fallback(request, decision), retry_parts

    async def final_fallback(
        self, request: ChatRequest, decision: RoutingDecision
    ) -> ChatResponse:
        logger.warning("All primary paths failed. Triggering final local fallback.")
        decision.agent = AgentType.OLLAMA
        decision.model_name = ""
        request.model = ""
        return await self._process_with_agent(request, decision)

    def enrich_response_usage(
        self,
        response: ChatResponse,
        provider: ProviderType,
        key: str,
        decision: RoutingDecision,
    ) -> None:
        if not response.usage:
            response.usage = {}
        idx = self.key_manager.get_key_index(provider, key)

        reason_map = {
            "model_hint": "USER_HINT",
            "token_count": "TOKEN_OPTIMIZED",
            "image_present": "MULTIMODAL_ANALYSIS",
            "WEB_INTENT_REQUIRE_INTELLIGENCE": "WEB_INTENT_SEARCH",
            "key_availability": "KEY_AVAILABILITY",
        }
        display_reason = reason_map.get(decision.reason, str(decision.reason).upper())

        response.usage.update(
            {
                "gateway_provider": provider.value,
                "gateway_key_index": idx,
                "gateway_model": decision.model_name,
                "routing_reason": display_reason,
            }
        )
