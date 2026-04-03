import time
from typing import Any

from openai import AsyncOpenAI

from src.core.exceptions import ProviderRateLimitError, ProviderServerError
from src.core.logging import get_logger
from src.domain.enums import ModelType
from src.domain.interfaces import ILLMProvider
from src.domain.models import ChatMessage, ChatMessageChoice, ChatRequest, ChatResponse

logger = get_logger(__name__)


class CerebrasAdapter(ILLMProvider):
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(
            base_url="https://api.cerebras.net/v1", api_key=api_key
        )
        logger.info("CerebrasAdapter initialized for model: llama3.1-70b-versatile")

    def get_supported_models(self) -> list[ModelType]:
        return [
            ModelType.CEREBRAS_GPT_5_3_CODEX,
            ModelType.CEREBRAS_DEEPSEEK_R1_70B,
            ModelType.CEREBRAS_GLM_4_6,
            ModelType.CEREBRAS_QWEN_3_235B,
            ModelType.CEREBRAS_GPT_OSS_120B,
            ModelType.CEREBRAS_LLAMA_3_3_70B,
        ]

    def is_multimodal(self) -> bool:
        return False

    def get_max_tokens(self) -> int:
        return 32768

    async def generate(self, request: ChatRequest, api_key: str) -> ChatResponse:
        try:
            cerebras_request = self._convert_to_cerebras_request(request)
            logger.info("Sending request to Cerebras with model llama3.1-70b-versatile")
            response = await self._call_cerebras_api(cerebras_request)
            logger.info("Cerebras request succeeded")
            return self._convert_to_chat_response(response)
        except Exception as e:
            logger.error(f"Cerebras request failed: {str(e)}")
            if hasattr(e, "response") and e.response.status_code == 429:
                msg = f"Rate limit exceeded: {str(e)}"
                raise ProviderRateLimitError(msg) from e
            elif hasattr(e, "response") and str(e.response.status_code).startswith("5"):
                msg = f"Server error: {str(e)}"
                raise ProviderServerError(msg) from e
            else:
                msg = f"Unexpected error: {str(e)}"
                raise ProviderServerError(msg) from e

    def _convert_to_cerebras_request(self, request: ChatRequest) -> dict[str, Any]:
        messages = []
        system_message = None

        if request.messages:
            for msg in request.messages:
                if msg.role == "system":
                    system_message = msg.content
                else:
                    messages.append({"role": msg.role, "content": msg.content})

        cerebras_request = {
            "model": "llama3.1-70b-versatile",
            "messages": messages,
            "temperature": request.temperature or 0.7,
            "max_tokens": request.max_tokens,
            "stop": request.stop,
        }

        if system_message:
            cerebras_request["system_prompt"] = system_message

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
                    "description": "Fast Llama model from Cerebras",
                    "tier": "premium" if "70b" in m.id else "standard",
                }
                for m in response.data
            ]
        except Exception as e:
            logger.error(f"Failed to discover Cerebras models: {e}")
            return []

    async def probe_key(self, api_key: str) -> dict[str, Any]:
        """Probe Cerebras key for tier info."""
        try:
            response = await self.client.models.list()
            return {"tier": "standard", "model_count": len(response.data)}
        except Exception as e:
            logger.error(f"Probe failed for Cerebras: {e}")
            return {"tier": "error", "error": str(e)}
