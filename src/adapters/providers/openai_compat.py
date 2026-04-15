from typing import Any

from openai import AsyncOpenAI

from src.core.exceptions import RateLimitError, ServiceUnavailableError
from src.core.logging import get_logger
from src.domain.enums import ModelType
from src.domain.interfaces import ILLMProvider
from src.domain.models import ChatMessage, ChatMessageChoice, ChatRequest, ChatResponse

logger = get_logger(__name__)


class OpenAICompatAdapter(ILLMProvider):
    def __init__(self, base_url: str, api_key: str, default_model: str | None = None):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30)
        self.default_model = default_model
        logger.info(f"OpenAICompatAdapter initialized for base_url: {base_url}")

    def get_supported_models(self) -> list[ModelType]:
        return [
            ModelType.GROQ_LLAMA_4_70B,
            ModelType.GROQ_LLAMA_4_8B,
            ModelType.GROQ_LLAMA_3_3_70B,
            ModelType.GROQ_DEEPSEEK_V3_1,
            ModelType.GROQ_DEEPSEEK_R1_70B,
            ModelType.GROQ_DEEPSEEK_R1_32B,
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

            msg_dict = {"role": msg.role, "content": str(msg_content)}
            if msg.name:
                msg_dict["name"] = msg.name
            messages.append(msg_dict)
            logger.debug(
                f"Added message to request: role={msg.role}, content={str(msg_content)[:50]}..."
            )

        model = request.model
        generic_hints = [
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
        ]
        if not model or model.lower() in generic_hints:
            if self.default_model:
                model = self.default_model
            else:
                try:
                    available = await self.discover_models()
                    if available:
                        model = available[0]["id"]
                        self.default_model = model
                except Exception:
                    pass

        if not model:
            error_msg = "No model specified and discovery failed"
            raise ServiceUnavailableError(error_msg)

        logger.info(f"Sending request to {self.client.base_url} with model {model}")

        from src.core.config import settings

        if settings.debug:
            logger.debug(f"OPENAI COMPAT REQUEST: {request.model_dump_json(indent=2)}")

        try:
            tools = None
            if request.has_search and "groq" in str(self.client.base_url).lower():
                tools = [{"type": "web_search"}]
                logger.info(f"Enabling Web Search tool for model {model}")

            # Make the API call
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore
                tools=tools,  # type: ignore
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                frequency_penalty=request.frequency_penalty,
                presence_penalty=request.presence_penalty,
                stop=request.stop,
            )
            logger.info(f"Request succeeded with model {model}")
            # Convert to our standardized response format
            return self._convert_to_chat_response(response)

        except Exception as e:
            logger.error(f"Request failed: {str(e)}")
            if hasattr(e, "status_code"):
                if e.status_code == 429:
                    error_msg = f"Rate limit exceeded: {str(e)}"
                    raise RateLimitError(error_msg) from e
                if e.status_code == 413:
                    error_msg = f"Payload too large: {str(e)}"
                    raise RateLimitError(error_msg) from e

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
        return ChatResponse(
            id=openai_response.id,
            object=openai_response.object,
            created=openai_response.created,
            model=openai_response.model,
            choices=choices,
            usage={
                "prompt_tokens": openai_response.usage.prompt_tokens,
                "completion_tokens": openai_response.usage.completion_tokens,
                "total_tokens": openai_response.usage.total_tokens,
            },
            session_id=None,
        )

    async def discover_models(self) -> list[dict[str, Any]]:
        try:
            response = await self.client.models.list()
            all_models = []
            for m in response.data:
                m_id = m.id.lower()
                if any(x in m_id for x in ["embed", "rerank", "vision-adapter"]):
                    continue

                all_models.append(
                    {
                        "id": m.id,
                        "display_name": m.id,
                        "description": f"Model from {self.client.base_url}",
                        "tier": "standard",
                    }
                )
            return all_models
        except Exception as e:
            logger.error(f"Failed to discover models from {self.client.base_url}: {e}")
            return []

    async def probe_key(self, api_key: str) -> dict[str, Any]:
        """Probe OpenAI-compatible key for tier info."""
        try:
            # We use model listing as a basic check
            response = await self.client.models.list()
            return {
                "tier": "standard",
                "model_count": len(response.data),
                "base_url": str(self.client.base_url),
            }
        except Exception as e:
            logger.error(f"Probe failed for {self.client.base_url}: {e}")
            return {"tier": "error", "error": str(e)}
