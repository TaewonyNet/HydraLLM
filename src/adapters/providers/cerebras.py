import time
from typing import Any

import httpx
from openai import AsyncOpenAI

from src.core.exceptions import (
    PaymentRequiredError,
    ProviderRateLimitError,
    ProviderServerError,
    RequestTooLargeError,
    parse_retry_after_seconds,
)
from src.core.logging import get_logger
from src.domain.enums import ModelType
from src.domain.interfaces import ILLMProvider
from src.domain.models import ChatMessage, ChatMessageChoice, ChatRequest, ChatResponse

logger = get_logger(__name__)

# llama3.1-70b-versatile 는 Cerebras 에서 더 이상 제공되지 않는다(실측 404). 모델명을
# 하드코딩하지 않고 discover_models()(/models) 결과를 우선 사용하며, 탐색이 아직 안 됐거나
# 실패했을 때만 이 상수를 최후 fallback 으로 쓴다(실측 가용: gpt-oss-120b).
DEFAULT_CEREBRAS_MODEL = "gpt-oss-120b"

# 구체적인 모델명이 아니라 "아무 모델이나/기본값"을 뜻하는 범용 힌트. 이 값들이 들어오면
# 과거처럼 하드코딩된 특정 모델로 보내지 않고 _resolve_model() 로 실제 가용 모델을 정한다.
_GENERIC_MODEL_HINTS = frozenset(
    {
        "",
        "auto",
        "cerebras",
        "cerebras/auto",
        "llama",
        "mllm/auto",
        "llama3",
        "llama3.1",
        "llama3.1-70b-versatile",
    }
)


class CerebrasAdapter(ILLMProvider):
    def __init__(self, api_key: str):
        http_client = httpx.AsyncClient(timeout=30)
        self.client = AsyncOpenAI(
            base_url="https://api.cerebras.ai/v1",
            api_key=api_key,
            http_client=http_client,
        )
        # /models 탐색 결과로 지연 채워지는 기본 모델 캐시(생성자에서 네트워크 호출 금지).
        self._resolved_default_model: str | None = None
        logger.info("CerebrasAdapter initialized (default model resolved via /models)")

    async def _resolve_model(self, requested: str | None) -> str:
        """구체 모델명이면 그대로, 범용 힌트/누락이면 /models 탐색 결과에서 고른다.

        우선순위: 탐색된 모델 중 DEFAULT_CEREBRAS_MODEL(gpt-oss-120b) > 탐색된 첫 모델 >
        탐색 자체가 안 되거나 실패하면 DEFAULT_CEREBRAS_MODEL 상수(최후 fallback).
        """
        normalized = (requested or "").strip().lower()
        if requested and normalized not in _GENERIC_MODEL_HINTS:
            return requested

        if self._resolved_default_model:
            return self._resolved_default_model

        try:
            models = await self.discover_models()
        except Exception as e:  # noqa: BLE001 - 탐색 실패해도 fallback 으로 계속
            logger.warning(f"Cerebras 모델 탐색 실패, 기본값으로 대체: {e}")
            models = []

        ids = [m["id"] for m in models]
        if DEFAULT_CEREBRAS_MODEL in ids:
            self._resolved_default_model = DEFAULT_CEREBRAS_MODEL
        elif ids:
            self._resolved_default_model = ids[0]
        else:
            self._resolved_default_model = DEFAULT_CEREBRAS_MODEL
        return self._resolved_default_model

    def get_supported_models(self) -> list[ModelType]:
        # 2026-10 실측(/models): gpt-oss-120b, qwen-3.8-27b 만 실존(나머지는 폐기돼
        # ModelType enum 에서도 제거했다).
        return [
            ModelType.CEREBRAS_GPT_OSS_120B,
            ModelType.CEREBRAS_QWEN_3_8_27B,
        ]

    def is_multimodal(self) -> bool:
        return False

    def get_max_tokens(self) -> int:
        return 32768

    async def generate(self, request: ChatRequest, api_key: str) -> ChatResponse:
        try:
            cerebras_request = await self._convert_to_cerebras_request(request)
            logger.info(
                f"Sending request to Cerebras with model {cerebras_request['model']}"
            )
            response = await self._call_cerebras_api(cerebras_request)
            logger.info("Cerebras request succeeded")
            return self._convert_to_chat_response(response)
        except Exception as e:
            logger.error(f"Cerebras request failed: {str(e)}")
            # response 가 없거나 None 일 수 있으므로 안전하게 status_code 추출(R1).
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code == 429:
                # 분당/일일 토큰 한도(그 키의 한도일 뿐 공급자 장애가 아니다) — 가능하면
                # 공급자가 알려준 Retry-After 를 실어 key_manager 쿨다운에 반영한다.
                msg = f"Rate limit exceeded: {str(e)}"
                raise ProviderRateLimitError(
                    msg, retry_after=parse_retry_after_seconds(e)
                ) from e
            elif status_code == 402:
                # 실측: 같은 키로 /models 는 정상인데 실제 chat completions 는 402
                # "Payment required"(결제/빌링 문제) — 재시도해도 안 풀리므로 429/5xx
                # 와 같은 버킷으로 묶지 않는다. key_manager 가 403 과 동일하게 영구
                # 제외하도록 전용 타입으로 올린다.
                msg = f"Payment required: {str(e)}"
                raise PaymentRequiredError(msg) from e
            elif status_code == 413:
                # 요청이 너무 큼 — 키/계정 문제가 아니므로 429/5xx 와 같은 버킷으로
                # 묶지 않는다(key_manager 가 이 키를 실패 처리하지 않도록 R-413).
                msg = f"Payload too large: {str(e)}"
                raise RequestTooLargeError(msg) from e
            elif status_code is not None and str(status_code).startswith("5"):
                msg = f"Server error: {str(e)}"
                raise ProviderServerError(msg) from e
            else:
                msg = f"Unexpected error: {str(e)}"
                raise ProviderServerError(msg) from e

    async def _convert_to_cerebras_request(self, request: ChatRequest) -> dict[str, Any]:
        messages = []

        if request.messages:
            for msg in request.messages:
                messages.append({"role": msg.role, "content": msg.content})

        model = await self._resolve_model(request.model)

        cerebras_request = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature or 0.7,
            "max_tokens": request.max_tokens,
            "stop": request.stop,
        }

        logger.debug(f"Created Cerebras request with {len(messages)} messages")
        return cerebras_request

    async def _call_cerebras_api(self, request: dict[str, Any]) -> Any:
        response = await self.client.chat.completions.create(
            model=request["model"],
            messages=request["messages"],
            temperature=request["temperature"],
            max_tokens=request["max_tokens"],
            stop=request.get("stop"),
        )
        return response

    def _convert_to_chat_response(self, cerebras_response: Any) -> ChatResponse:
        """Convert Cerebras response to our standardized ChatResponse."""
        choices = []

        for i, choice in enumerate(cerebras_response.choices):
            choices.append(
                ChatMessageChoice(
                    index=i,
                    message=ChatMessage(
                        role=choice.message.role,
                        content=choice.message.content or "",
                        name=None,
                    ),
                    finish_reason=choice.finish_reason,
                    content_filter_results=None,
                )
            )

        logger.debug(
            f"Converted Cerebras response to ChatResponse with {len(choices)} choices"
        )

        usage = {}
        if hasattr(cerebras_response, "usage") and cerebras_response.usage:
            usage = {
                "prompt_tokens": cerebras_response.usage.prompt_tokens,
                "completion_tokens": cerebras_response.usage.completion_tokens,
                "total_tokens": cerebras_response.usage.total_tokens,
            }
            # gpt-oss 계열은 Cerebras 에서도 completion_tokens_details.reasoning_tokens
            # 를 돌려줄 수 있다 — Groq 어댑터와 동일하게 가능하면 보존(request 로
            # reasoning_effort 를 보내진 않지만, 모델이 자체적으로 반환하는 값은 그대로
            # 전달해도 안전하다).
            details = getattr(cerebras_response.usage, "completion_tokens_details", None)
            reasoning_tokens = (
                getattr(details, "reasoning_tokens", None) if details else None
            )
            if reasoning_tokens is not None:
                usage["reasoning_tokens"] = reasoning_tokens

        return ChatResponse(
            id=cerebras_response.id,
            object="chat.completion",
            created=int(time.time()),
            model=cerebras_response.model,
            choices=choices,
            usage=usage,
            session_id=None,
        )

    async def discover_models(self) -> list[dict[str, Any]]:
        """Discover available models from Cerebras with metadata."""
        try:
            response = await self.client.models.list()
            return [
                {
                    "id": m.id,
                    "display_name": m.id,
                    "description": "Fast model from Cerebras",
                    "tier": "premium" if "70b" in m.id else "standard",
                }
                for m in response.data
            ]
        except Exception as e:
            logger.error(f"Failed to discover Cerebras models: {e}")
            return []

    async def probe_key(self, api_key: str) -> dict[str, Any]:
        """Probe Cerebras key for real availability.

        /models 목록 조회만으론 402(결제 필요)를 못 잡는다(실측 2026-10: 같은 키로
        /models 는 200 이지만 실제 chat completions 는 402 "Payment required ...
        Visit your billing tab"). 그래서 아주 짧은 chat completion 1회
        (max_tokens=1)로 실측 가용성을 확인한다. 실패하면 예외를 그대로 전파한다
        (과거처럼 내부에서 삼키지 않음 — 호출부(gateway.probe_active_keys /
        _recover_failed_keys_impl)가 403/402 등을 분류해 키를 영구 제외할 수 있게
        하기 위함)."""
        model = await self._resolve_model(None)
        await self.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        return {"tier": "standard"}
