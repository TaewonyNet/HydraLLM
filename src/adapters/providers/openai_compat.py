from typing import Any

import httpx
from openai import AsyncOpenAI

from src.core.exceptions import (
    PaymentRequiredError,
    RateLimitError,
    RequestTooLargeError,
    ServiceUnavailableError,
    parse_retry_after_seconds,
)
from src.core.logging import get_logger
from src.domain.enums import ModelType
from src.domain.interfaces import ILLMProvider
from src.domain.models import ChatMessage, ChatMessageChoice, ChatRequest, ChatResponse

logger = get_logger(__name__)

# Groq 에서 llama-3.3-70b-versatile 등 llama/deepseek 계열이 더 이상 제공되지 않는다
# (실측 404 — /models 에 없음). 모델명을 하드코딩하지 않고 discover_models()(/models)
# 결과를 우선 쓰며, 탐색이 안 됐거나 실패했을 때만 이 상수를 최후 fallback 으로 쓴다
# (2026-10 실측 가용: openai/gpt-oss-120b). Ollama 등 다른 base_url 에는 적용되지 않도록
# 호출자가 preferred_default_model 로 명시해야만 쓰인다(Cerebras 어댑터와 동일 패턴).
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"


class OpenAICompatAdapter(ILLMProvider):
    # 구체적인 모델명이 아니라 "아무 모델이나/기본값"을 뜻하는 범용 힌트. 이 값들이
    # 들어오면 하드코딩된 특정 모델로 보내지 않고 _resolve_model() 로 실제 가용 모델을
    # 정한다(Cerebras 어댑터와 동일 패턴).
    _GENERIC_MODEL_HINTS = frozenset(
        {
            "",
            "groq",
            "llama",
            "cerebras",
            "auto",
            "mllm/auto",
            "ollama",
            "opencode",
            "openclaw",
            "llama3",
            "llama3.1",
            "llama3.2",
            # 더 이상 존재하지 않는 구 Groq 모델명도 범용 힌트로 취급해 discovery 로 넘긴다.
            "llama-3.3-70b-versatile",
        }
    )

    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str | None = None,
        preferred_default_model: str | None = None,
    ):
        # 자체 httpx.AsyncClient 주입으로 openai==1.2.1 과 httpx>=0.28 의
        # proxies kwarg 호환성 문제를 우회한다.
        http_client = httpx.AsyncClient(timeout=60)
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=60,
            http_client=http_client,
        )
        # default_model: 호출자가 이미 검증한 고정 모델명(예: 로컬 Ollama 는 discovery
        # 로 바로 뽑으므로 보통 None). 명시되면 탐색 없이 그대로 쓴다.
        self.default_model = default_model
        # preferred_default_model: "이 모델을 쓰고 싶다"는 희망값이지만, discover_models()
        # 결과에 실제로 있는지 검증한 뒤에만 쓰고, 없으면 탐색된 첫 모델로 대체한다
        # (Groq 호출자가 DEFAULT_GROQ_MODEL 을 넘겨 하드코딩 없이 실측 검증받는다).
        self.preferred_default_model = preferred_default_model
        self._resolved_default_model: str | None = None
        logger.info(f"OpenAICompatAdapter initialized for base_url: {base_url}")

    async def _resolve_model(self, requested: str | None) -> str | None:
        """구체 모델명이면 그대로, 범용 힌트/누락이면 default_model → 탐색 순으로 정한다."""
        normalized = (requested or "").strip().lower()
        if requested and normalized not in self._GENERIC_MODEL_HINTS:
            return requested

        if self.default_model:
            return self.default_model

        if self._resolved_default_model:
            return self._resolved_default_model

        try:
            available = await self.discover_models()
        except Exception as e:  # noqa: BLE001 - 탐색 실패해도 fallback 으로 계속
            logger.warning(f"{self.client.base_url} 모델 탐색 실패, 기본값으로 대체: {e}")
            available = []

        ids = [m["id"] for m in available]
        if self.preferred_default_model and self.preferred_default_model in ids:
            self._resolved_default_model = self.preferred_default_model
        elif ids:
            self._resolved_default_model = ids[0]
        elif self.preferred_default_model:
            self._resolved_default_model = self.preferred_default_model

        return self._resolved_default_model

    def get_supported_models(self) -> list[ModelType]:
        # 2026-10 실측(/models): llama-4-*/llama-3.3-70b-versatile/deepseek-* 는 더
        # 이상 없어 ModelType enum 에서도 제거했다. 실존 모델만 나열.
        return [
            ModelType.GROQ_GPT_OSS_120B,
            ModelType.GROQ_GPT_OSS_20B,
            ModelType.GROQ_QWEN_3_8_27B,
            ModelType.GROQ_ALLAM_2_7B,
        ]

    def is_multimodal(self) -> bool:
        return False

    def get_max_tokens(self) -> int:
        return 8192

    async def generate(self, request: ChatRequest, api_key: str) -> ChatResponse:
        messages = []
        if not request.messages:
            error_msg = "No messages provided in request"
            raise ServiceUnavailableError(error_msg)

        for msg in request.messages:
            msg_content = msg.content
            if isinstance(msg_content, list):
                text_parts = []
                for part in msg_content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                msg_content = " ".join(text_parts)
            elif isinstance(msg_content, dict):
                msg_content = msg_content.get("text", str(msg_content))

            if not str(msg_content).strip():
                continue

            msg_dict = {"role": msg.role, "content": str(msg_content)}
            if msg.name:
                msg_dict["name"] = msg.name
            messages.append(msg_dict)
            logger.debug(
                f"Added message to request: role={msg.role}, content={str(msg_content)[:50]}..."
            )

        model = await self._resolve_model(request.model)

        if not model:
            error_msg = "No model specified and discovery failed"
            raise ServiceUnavailableError(error_msg)

        logger.info(f"Sending request to {self.client.base_url} with model {model}")

        from src.core.config import settings

        if settings.debug:
            logger.debug(f"OPENAI COMPAT REQUEST: {request.model_dump_json(indent=2)}")

        try:
            is_groq = "groq" in str(self.client.base_url).lower()

            tools = None
            if request.has_search and is_groq:
                tools = [{"type": "web_search"}]
                logger.info(f"Enabling Web Search tool for model {model}")

            # response_format/reasoning_effort 는 실측(2026-10)으로 Groq 지원이
            # 확인됐다(json_object 응답 + reasoning_effort=low 로 실제 호출 성공,
            # usage.completion_tokens_details.reasoning_tokens 도 돌아옴). 다른
            # 공급자(Ollama 등 이 어댑터를 공유하는 로컬 엔드포인트)는 지원 여부가
            # 확인되지 않아 보내지 않는다(모르는 필드로 400 을 유발하지 않기 위함) —
            # 공급자별로 지원이 확인될 때만 조건을 넓힌다.
            response_format = request.response_format if is_groq else None
            reasoning_effort = request.reasoning_effort if is_groq else None

            # Make the API call
            # __init__ 의 단일 클라이언트를 재사용한다. 매 요청 새 httpx 클라이언트를
            # self.client.http_client 에 재대입하면 동시 요청이 서로의 클라이언트를 닫힌
            # 인스턴스로 교체하는 race + 원본 클라이언트 누수가 발생한다(R2).
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore
                tools=tools,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                frequency_penalty=request.frequency_penalty,
                presence_penalty=request.presence_penalty,
                stop=request.stop,
                response_format=response_format,
                reasoning_effort=reasoning_effort,
            )
            logger.info(f"Request succeeded with model {model}")
            # Convert to our standardized response format
            return self._convert_to_chat_response(response)

        except Exception as e:
            logger.error(f"Request failed: {str(e)}")
            if hasattr(e, "status_code"):
                if e.status_code == 429:
                    # 실측(2026-10): Groq 분당 토큰 한도로 429 가 났는데 같은 시각
                    # 직접 호출(키 2개·모델 3개)은 전부 200 이었다 — 공급자 장애가
                    # 아니라 그 키의 한도 문제다. 가능하면 공급자의 Retry-After 를
                    # 실어 key_manager 쿨다운에 반영한다(resilience_manager 가 이
                    # 타입은 circuit breaker 를 OPEN 시키지 않도록 분리 처리한다).
                    error_msg = f"Rate limit exceeded: {str(e)}"
                    raise RateLimitError(
                        error_msg, retry_after=parse_retry_after_seconds(e)
                    ) from e
                if e.status_code == 402:
                    # 결제/빌링 문제(실측: Cerebras 에서 /models 는 정상인데 chat
                    # completions 가 402 — 같은 계정 구조의 Groq 도 동일하게 다룬다).
                    # 재시도해도 안 풀리므로 429/5xx 와 다른 버킷으로 분리해 403 과
                    # 동일하게 키를 영구 제외한다.
                    error_msg = f"Payment required: {str(e)}"
                    raise PaymentRequiredError(error_msg) from e
                if e.status_code == 413:
                    # 413 은 키/계정 쿼터 문제가 아니라 "이 요청이 이 공급자 한도보다
                    # 크다"는 뜻이다. RateLimitError 로 묶으면 key_manager 가 키를
                    # 실패 처리(쿨다운)해 Gemini 로 넘어갔다가 403 → 클라이언트 타임아웃
                    # 으로 이어진다(실측 사고). 별도 타입으로 분리해 키는 그대로 두고
                    # resilience_manager 가 더 큰 한도 공급자로 전환하게 한다.
                    error_msg = f"Payload too large: {str(e)}"
                    raise RequestTooLargeError(error_msg) from e

            error_msg = f"Unexpected error: {str(e)}"
            raise ServiceUnavailableError(error_msg) from e

    def _convert_to_chat_response(self, openai_response: Any) -> ChatResponse:
        """Convert OpenAI response to our standardized ChatResponse."""
        choices = []
        for idx, choice in enumerate(openai_response.choices):
            choices.append(
                ChatMessageChoice(
                    index=idx,
                    message=ChatMessage(
                        role="assistant",
                        content=choice.message.content or "",
                        name=None,
                    ),
                    finish_reason=choice.finish_reason,
                    content_filter_results=None,
                )
            )

        logger.debug(f"Converted {len(choices)} choices to ChatResponse")
        usage = {
            "prompt_tokens": openai_response.usage.prompt_tokens,
            "completion_tokens": openai_response.usage.completion_tokens,
            "total_tokens": openai_response.usage.total_tokens,
        }
        # reasoning_effort 로 추론을 켠 gpt-oss 계열은 completion_tokens_details 에
        # reasoning_tokens 를 함께 돌려준다(실측 확인) — 클라이언트가 "실제 추론에 쓴
        # 토큰"을 볼 수 있게 가능하면 보존한다.
        details = getattr(openai_response.usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None) if details else None
        if reasoning_tokens is not None:
            usage["reasoning_tokens"] = reasoning_tokens

        return ChatResponse(
            id=openai_response.id,
            object=openai_response.object,
            created=openai_response.created,
            model=openai_response.model,
            choices=choices,
            usage=usage,
            session_id=None,
        )

    _NON_CHAT_MODEL_MARKERS: tuple[str, ...] = (
        "embed",
        "embedding",
        "rerank",
        "vision-adapter",
        "bge-",
        "bge",
        "gte-",
        "gte",
        "e5-",
        "nomic-embed",
        "mxbai-embed",
        "jina-embed",
        "snowflake-arctic-embed",
        "whisper",
        "clip",
        # 2026-10 실측 Groq /models 에서 처음 확인된 비-chat 모델(TTS/가드레일) — 범용
        # 힌트 fallback(available[0])이 이런 모델을 잘못 고르지 않도록 제외.
        "orpheus",
        "prompt-guard",
        "guard",
    )

    async def discover_models(self) -> list[dict[str, Any]]:
        try:
            response = await self.client.models.list()
            all_models: list[dict[str, Any]] = []
            for m in response.data:
                m_id = m.id.lower()
                if any(marker in m_id for marker in self._NON_CHAT_MODEL_MARKERS):
                    continue

                all_models.append(
                    {
                        "id": m.id,
                        "display_name": m.id,
                        "description": f"Model from {self.client.base_url}",
                        "tier": "standard",
                    }
                )

            # instruct나 chat이 포함된 모델을 우선순위에 둔다
            def sort_key(model_dict: dict[str, Any]) -> int:
                m_id = model_dict["id"].lower()
                if "instruct" in m_id:
                    return 0
                if "chat" in m_id:
                    return 1
                return 10

            all_models.sort(key=sort_key)
            return all_models
        except Exception as e:
            logger.error(f"Failed to discover models from {self.client.base_url}: {e}")
            return []

    async def probe_key(self, api_key: str) -> dict[str, Any]:
        """Probe OpenAI-compatible key for real availability.

        /models 목록 조회만으론 402(결제 필요) 같은 완료-전용 문제를 못 잡는다(Cerebras
        실측과 동일 우려 — Groq 도 계정별 결제 상태가 있을 수 있어 동일하게 다룬다).
        쓸 모델을 특정할 수 있으면(Groq 처럼 default_model/preferred_default_model 로
        확정 가능) 아주 짧은 chat completion 1회(max_tokens=1)로 확인하고, 특정할 수
        없으면(예: Ollama 처럼 모델이 다양한 범용 엔드포인트이고 discovery 도 안 되는
        경우) /models 목록 조회로 최소한의 키 유효성만 확인한다. 실패하면 예외를 그대로
        전파한다(과거처럼 내부에서 삼키지 않음 — 호출부가 403/402 등을 분류해 키를
        영구 제외할 수 있게 하기 위함)."""
        model = await self._resolve_model(None)
        if model:
            await self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            return {"tier": "standard", "base_url": str(self.client.base_url)}

        response = await self.client.models.list()
        return {
            "tier": "standard",
            "model_count": len(response.data),
            "base_url": str(self.client.base_url),
        }
