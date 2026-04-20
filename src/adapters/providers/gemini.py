import time
from typing import Any

import google.generativeai as genai

from src.core.exceptions import RateLimitError, ServiceUnavailableError
from src.core.logging import get_logger
from src.domain.enums import ModelType
from src.domain.interfaces import ILLMProvider
from src.domain.models import ChatMessage, ChatMessageChoice, ChatRequest, ChatResponse
from src.services.context_manager import ContextManager

logger = get_logger(__name__)


class GeminiAdapter(ILLMProvider):
    def __init__(self, api_key: str, context_manager: ContextManager | None = None):
        genai.configure(api_key=api_key)
        self._discovered_models: list[dict[str, Any]] = []
        self.context_manager = context_manager
        logger.info("GeminiAdapter initialized")

    def get_supported_models(self) -> list[ModelType]:
        return [
            ModelType.GEMINI_3_PRO,
            ModelType.GEMINI_3_FLASH,
            ModelType.GEMINI_2_5_PRO,
            ModelType.GEMINI_2_5_FLASH,
            ModelType.GEMINI_2_0_PRO,
            ModelType.GEMINI_2_0_FLASH,
        ]

    def is_multimodal(self) -> bool:
        return True

    def get_max_tokens(self) -> int:
        return 32768

    async def generate(self, request: ChatRequest, api_key: str) -> ChatResponse:
        uploaded_files: list[Any] = []
        try:
            genai.configure(api_key=api_key)

            if not self._discovered_models:
                await self.discover_models()

            if not request.messages:
                err_msg = "No messages provided in request"
                raise ServiceUnavailableError(err_msg)

            model_name = self._map_model_name(request.model)

            system_instructions: list[str] = []
            history: list[ChatMessage] = []

            for msg in request.messages:
                if msg.role == "system":
                    system_instructions.append(str(msg.content))
                    continue

                content_str = str(msg.content)
                if self.context_manager and self.context_manager.should_offload(
                    content_str
                ):
                    try:
                        content_hash = self.context_manager.get_content_hash(
                            content_str
                        )
                        file_handle = self.context_manager.get_cached_file(content_hash)

                        if not file_handle:
                            tmp_path = self.context_manager.prepare_temp_file(
                                content_str
                            )
                            file_handle = genai.upload_file(
                                path=tmp_path,
                                display_name=f"ctx_{content_hash[:8]}.txt",
                            )
                            self.context_manager.cache_file(content_hash, file_handle)

                        history.append(
                            ChatMessage(
                                role=msg.role,
                                content=[{"type": "file", "file_handle": file_handle}],
                                name=None,
                            )
                        )
                        continue
                    except Exception as fe:
                        logger.warning(
                            f"Failed to upload context file: {fe}. Falling back to text."
                        )

                history.append(msg)

            combined_system = (
                "\n\n".join(system_instructions) if system_instructions else None
            )
            model = genai.GenerativeModel(
                model_name, system_instruction=combined_system
            )
            contents = self._convert_to_gemini_request(history)

            logger.info(
                f"Sending request to Gemini with model {model_name} (Files: {len(uploaded_files)})"
            )

            # 네이티브 google_search 그라운딩은 현재 SDK 버전과 호환되지 않아
            # tools 필드를 비우고, 웹 컨텍스트는 web_context_service 가 시스템
            # 프롬프트에 주입한 결과를 그대로 사용한다.
            response = await model.generate_content_async(
                contents=contents,
                tools=None,
                generation_config=genai.types.GenerationConfig(
                    temperature=request.temperature
                    if request.temperature is not None
                    else 0.7,
                    max_output_tokens=request.max_tokens,
                    stop_sequences=request.stop,
                ),
            )

            return self._convert_to_chat_response(response, model_name)
        except Exception as e:
            err_str = str(e)
            logger.error(f"Gemini request failed: {err_str}")

            if "403" in err_str:
                # 403 Forbidden is a project/key level failure, not a temporary rate limit
                error_msg = f"Access denied (403): {err_str}"
                raise ServiceUnavailableError(error_msg) from e

            if "429" in err_str:
                error_msg = f"Rate limit exceeded: {err_str}"
                raise RateLimitError(error_msg) from e

            error_msg = f"Unexpected error: {err_str}"
            raise ServiceUnavailableError(error_msg) from e
        finally:
            pass

    def _map_model_name(self, request_model: str | None) -> str:
        if not request_model or request_model.lower() == "auto":
            flash_models: list[str] = [
                str(m["id"])
                for m in self._discovered_models
                if "flash" in str(m["id"]).lower()
            ]
            if flash_models:
                return sorted(flash_models, reverse=True)[0]
            return "gemini-2.5-flash"

        input_clean = request_model.lower().replace("models/", "")

        for m in self._discovered_models:
            m_id_clean = str(m["id"]).lower().replace("models/", "")
            if input_clean == m_id_clean:
                return str(m["id"])

        for m in self._discovered_models:
            if (
                input_clean in str(m["id"]).lower()
                or input_clean in str(m.get("display_name", "")).lower()
            ):
                return str(m["id"])

        return request_model

    def _convert_to_gemini_request(self, messages: list[ChatMessage]) -> list:
        gemini_msgs = []
        for msg in messages:
            role = "user" if msg.role == "user" else "model"
            content = msg.content
            parts = []

            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, dict):
                if content.get("type") == "text":
                    parts.append(content.get("text", ""))
                else:
                    parts.append(str(content))
            else:
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "file":
                        fh = p.get("file_handle")
                        if fh is not None:
                            parts.append(fh)
                    else:
                        parts.append(str(p))

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
            candidate = (
                gemini_response.candidates[0]
                if hasattr(gemini_response, "candidates") and gemini_response.candidates
                else None
            )

            if (
                candidate
                and hasattr(candidate, "content")
                and hasattr(candidate.content, "parts")
            ):
                parts = [
                    p.text
                    for p in candidate.content.parts
                    if hasattr(p, "text") and p.text
                ]
                if parts:
                    response_content = "".join(parts)
                else:
                    if hasattr(candidate, "finish_reason"):
                        fr = candidate.finish_reason
                        fr_val = fr.value if hasattr(fr, "value") else fr
                        if fr_val in [3, 4, 12]:
                            response_content = f"[Request blocked by safety filters (Reason: {fr_val})]"
                        else:
                            response_content = "[Empty response from model]"
            else:
                response_content = "[No valid response parts returned]"

            if candidate:
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
                    except Exception as me:
                        logger.warning(f"Failed to serialize grounding metadata: {me}")
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
            "prompt_tokens": getattr(
                gemini_response.usage_metadata, "prompt_token_count", 0
            )
            if hasattr(gemini_response, "usage_metadata")
            else 0,
            "completion_tokens": getattr(
                gemini_response.usage_metadata, "candidates_token_count", 0
            )
            if hasattr(gemini_response, "usage_metadata")
            else 0,
            "total_tokens": getattr(
                gemini_response.usage_metadata, "total_token_count", 0
            )
            if hasattr(gemini_response, "usage_metadata")
            else 0,
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
            session_id=None,
        )

    async def discover_models(self) -> list[dict[str, Any]]:
        try:
            models = []
            for m in genai.list_models():
                if "generateContent" in m.supported_generation_methods:
                    name = m.name.replace("models/", "")
                    m_info = {
                        "id": name,
                        "display_name": m.display_name,
                        "description": m.description,
                        "input_token_limit": m.input_token_limit,
                        "output_token_limit": m.output_token_limit,
                        "tier": "free"
                        if "flash" in name or "lite" in name
                        else "premium",
                    }
                    models.append(m_info)
            self._discovered_models = models
            return models
        except Exception as e:
            logger.error(f"Failed to discover Gemini models: {e}")
            return []

    async def probe_key(self, api_key: str) -> dict[str, Any]:
        try:
            genai.configure(api_key=api_key)
            test_model_name = "gemini-2.5-flash"
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
                elif "429" in err_msg:
                    logger.warning(f"Key {api_key[:8]}... is still exhausted (429)")
                    raise
                else:
                    tier = "free"

            return {
                "tier": tier,
                "status": "active",
                "can_list_models": True,
            }
        except Exception as e:
            logger.error(f"Probe failed for Gemini key {api_key[:8]}: {e}")
            return {"tier": "error", "status": "failed", "error": str(e)}
