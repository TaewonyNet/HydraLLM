import time
from typing import Any

import google.generativeai as genai

from src.core.config import settings
from src.core.exceptions import RateLimitError, ServiceUnavailableError
from src.core.logging import get_logger
from src.domain.enums import ModelType
from src.domain.interfaces import ILLMProvider
from src.domain.models import ChatMessage, ChatMessageChoice, ChatRequest, ChatResponse

logger = get_logger(__name__)


class GeminiAdapter(ILLMProvider):
    def __init__(self, api_key: str):
        genai.configure(api_key=api_key)
        logger.info("GeminiAdapter initialized")

    def get_supported_models(self) -> list[ModelType]:
        return [
            ModelType.GEMINI_3_1_PRO,
            ModelType.GEMINI_3_1_ULTRA,
            ModelType.GEMINI_3_PRO,
            ModelType.GEMINI_3_FLASH,
            ModelType.GEMINI_3_1_FLASH_LITE,
            ModelType.GEMINI_2_5_FLASH,
            ModelType.GEMINI_2_0_PRO,
            ModelType.GEMINI_2_0_FLASH,
            ModelType.GEMINI_2_0_THINKING,
            ModelType.GEMINI_1_5_PRO,
            ModelType.GEMINI_1_5_FLASH,
        ]

    def is_multimodal(self) -> bool:
        return True

    def get_max_tokens(self) -> int:
        return 32768

    async def generate(self, request: ChatRequest, api_key: str) -> ChatResponse:
        try:
            genai.configure(api_key=api_key)

            if not request.messages:
                error_msg = "No messages provided in request"
                raise ServiceUnavailableError(error_msg)

            model_name = self._map_model_name(request.model)

            system_instruction = None
            history = []
            for msg in request.messages:
                if msg.role == "system":
                    system_instruction = str(msg.content)
                else:
                    history.append(msg)

            model = genai.GenerativeModel(
                model_name, system_instruction=system_instruction
            )

            contents = self._convert_to_gemini_request(history)

            logger.debug(f"Gemini Request Contents (turns: {len(contents)})")
            if system_instruction:
                logger.debug(
                    f"Gemini System Instruction length: {len(system_instruction)}"
                )

            logger.info(f"Sending request to Gemini with model {model_name}")

            if settings.debug:
                logger.debug(
                    f"GEMINI ADAPTER REQUEST: {request.model_dump_json(indent=2)}"
                )

            tools = None
            if request.has_search:
                tools = [
                    {
                        "google_search_retrieval": {
                            "dynamic_retrieval_config": {
                                "mode": "DYNAMIC",
                                "dynamic_threshold": 0.3,
                            }
                        }
                    }
                ]
                logger.info(f"Enabling Google Search grounding for model {model_name}")

            response = await model.generate_content_async(
                contents=contents,
                tools=tools,
                generation_config=genai.types.GenerationConfig(
                    temperature=request.temperature
                    if request.temperature is not None
                    else 0.7,
                    max_output_tokens=request.max_tokens,
                    stop_sequences=request.stop,
                ),
            )

            logger.info(f"Gemini request succeeded for model {model_name}")
            return self._convert_to_chat_response(response, model_name)
        except Exception as e:
            logger.error(f"Gemini request failed: {str(e)}")
            if "429" in str(e):
                err_msg = f"Rate limit exceeded: {str(e)}"
                raise RateLimitError(err_msg) from e
            else:
                err_msg = f"Unexpected error: {str(e)}"
                raise ServiceUnavailableError(err_msg) from e

    def _map_model_name(self, request_model: str | None) -> str:
        mapping = {
            "gemini-3.1-pro": "gemini-3.1-pro-preview",
            "gemini-3.1-ultra": "gemini-3.1-ultra-preview",
            "gemini-3.0-pro": "gemini-3.0-pro-preview",
            "gemini-3.0-flash": "gemini-3.0-flash-preview",
            "gemini-3.0-flash-lite": "gemini-3.1-flash-lite-preview",
            "gemini-3.1-flash-lite": "gemini-3.1-flash-lite-preview",
            "gemini-2.5-flash": "gemini-2.5-flash",
            "gemini-2.0-pro": "gemini-2.0-pro-exp",
            "gemini-2.0-flash": "gemini-2.0-flash",
            "gemini-2.0-thinking": "gemini-2.0-flash-thinking-exp",
            "gemini-1.5-pro": "gemini-pro-latest",
            "gemini-1.5-flash": "gemini-flash-latest",
            "gemini-pro": "gemini-pro-latest",
            "gemini-flash": "gemini-flash-latest",
            "gemini-last-flash": "gemini-flash-latest",
        }

        if not request_model:
            return "gemini-flash-latest"

        if request_model in mapping:
            return mapping[request_model]

        return request_model

    def _convert_to_gemini_request(self, messages: list[ChatMessage]) -> list:
        gemini_msgs = []
        for msg in messages:
            role = "user" if msg.role == "user" else "model"
            content = msg.content

            if isinstance(content, str):
                parts = [content]
            elif isinstance(content, dict):
                if content.get("type") == "text":
                    parts = [content.get("text", "")]
                else:
                    parts = [str(content)]
            else:
                parts = [str(p) for p in content]

            gemini_msgs.append({"role": role, "parts": parts})
        return gemini_msgs

    def _convert_to_chat_response(
        self, gemini_response: Any, model_name: str
    ) -> ChatResponse:
        choices = []
        grounding_metadata = None
        finish_reason = "stop"
        response_content = "[No response content]"

        try:
            if hasattr(gemini_response, "candidates") and gemini_response.candidates:
                candidate = gemini_response.candidates[0]

                if hasattr(candidate, "content") and hasattr(
                    candidate.content, "parts"
                ):
                    parts = [
                        p.text for p in candidate.content.parts if hasattr(p, "text")
                    ]
                    if parts:
                        response_content = "".join(parts)

                if hasattr(candidate, "finish_reason") and candidate.finish_reason:
                    reason_map = {
                        1: "stop",
                        2: "length",
                        3: "content_filter",
                        4: "content_filter",
                        5: "other",
                    }
                    val = candidate.finish_reason
                    if hasattr(val, "value"):
                        val = val.value
                    finish_reason = reason_map.get(val, "stop")

                if (
                    hasattr(candidate, "grounding_metadata")
                    and candidate.grounding_metadata
                ):
                    try:
                        if hasattr(candidate.grounding_metadata, "to_dict"):
                            grounding_metadata = candidate.grounding_metadata.to_dict()
                        else:
                            pass

                    except Exception as me:
                        logger.warning(f"Failed to serialize grounding metadata: {me}")
            else:
                response_content = "[No candidates returned]"
        except Exception as e:
            logger.error(f"Failed to parse Gemini response: {e}")
            response_content = f"[Error parsing response: {str(e)}]"

        choices.append(
            ChatMessageChoice(
                index=0,
                message=ChatMessage(
                    role="assistant", content=response_content, name=None
                ),
                finish_reason=finish_reason,
                content_filter_results=None,
            )
        )

        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        if grounding_metadata:
            usage["grounding_metadata"] = grounding_metadata

        return ChatResponse(
            id=f"gemini-{int(time.time())}",
            object="chat.completion",
            created=int(time.time()),
            model=model_name,
            choices=choices,
            usage=usage,
        )

    async def discover_models(self) -> list[dict[str, Any]]:
        try:
            models = []
            for m in genai.list_models():
                if "generateContent" in m.supported_generation_methods:
                    name = m.name.replace("models/", "")
                    if "lite" in name or "flash" in name:
                        tier = "free"
                    elif "pro" in name or "ultra" in name:
                        tier = "premium"
                    elif "exp" in name or "preview" in name:
                        tier = "experimental"
                    else:
                        tier = "standard"

                    models.append(
                        {
                            "id": name,
                            "display_name": m.display_name,
                            "description": m.description,
                            "input_token_limit": m.input_token_limit,
                            "output_token_limit": m.output_token_limit,
                            "tier": tier,
                        }
                    )
            return models
        except Exception as e:
            logger.error(f"Failed to discover Gemini models: {e}")
            return []

    async def probe_key(self, api_key: str) -> dict[str, Any]:
        try:
            genai.configure(api_key=api_key)
            test_model_name = "gemini-3.1-pro-preview"
            try:
                model = genai.GenerativeModel(test_model_name)
                await model.generate_content_async(
                    "hi", generation_config={"max_output_tokens": 1}
                )
                tier = "premium"
            except Exception as e:
                err_msg = str(e).lower()
                if "limit: 0" in err_msg or "free_tier" in err_msg or "404" in err_msg:
                    tier = "free"
                else:
                    raise

            return {
                "tier": tier,
                "can_list_models": True,
            }
        except Exception as e:
            logger.error(f"Probe failed for Gemini key {api_key[:8]}: {e}")
            return {"tier": "error", "error": str(e)}
