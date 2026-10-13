import asyncio
import time
from typing import Any

import google.generativeai as genai

from src.core.exceptions import AuthenticationError, RateLimitError, ServiceUnavailableError
from src.core.logging import get_logger
from src.domain.enums import ModelType
from src.domain.interfaces import IContextManager, ILLMProvider
from src.domain.models import ChatMessage, ChatMessageChoice, ChatRequest, ChatResponse

logger = get_logger(__name__)


class GeminiAdapter(ILLMProvider):
    def __init__(self, api_key: str, context_manager: IContextManager | None = None):
        genai.configure(api_key=api_key)
        self._discovered_models: list[dict[str, Any]] = []
        self.context_manager = context_manager
        self._discover_lock = asyncio.Lock()  # 첫 요청들의 중복 discover 방지
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
            # 주의(R9, 알려진 한계): google.generativeai 의 configure 는 '모듈 전역' 키를
            # 설정한다. 서로 다른 키의 동시 요청이 이 전역을 덮어쓰면 키가 뒤섞일 수 있다.
            # 근본 해결은 client-instance API(google-genai) 이관이며 별도 과제로 추적한다.
            genai.configure(api_key=api_key)

            if not self._discovered_models:
                async with self._discover_lock:
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
                            # 동기 블로킹 SDK 호출은 스레드에서 실행(이벤트루프 보호, R8).
                            file_handle = await asyncio.to_thread(
                                genai.upload_file,
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

            # has_search=True 일 때 Gemini 네이티브 google_search grounding tool 을 전달.
            # web_context_service 주입 방식과 별개로, 호출자가 명시적으로 검색을 요청한 경우에만 활성화.
            tools: list[dict[str, Any]] | None = (
                [{"google_search": {}}] if request.has_search else None
            )
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

            return self._convert_to_chat_response(response, model_name)
        except Exception as e:
            err_str = str(e)
            logger.error(f"Gemini request failed: {err_str}")

            if "403" in err_str:
                # 403 Forbidden은 프로젝트/키 단위 영구 거부다(실측: "project has
                # been denied access"). 과거엔 ServiceUnavailableError로 분류해
                # resilience_manager가 이를 "공급자 장애"로 보고 circuit breaker
                # 실패로도 집계했다(실측 2026-10-13: 거부된 키 12/13개가 반복
                # 걸려 서킷이 통째로 OPEN되고, 아직 살아있는 키 1개까지 막혔다).
                # AuthenticationError로 분류하면 resilience_manager의 일반
                # Exception 경로를 타 key_manager.report_failure(그 키만 영구
                # 제외)는 그대로 호출되면서도 breaker.report_failure()는 호출되지
                # 않는다 — 공급자 전체가 아니라 "그 키"만 막힌 문제이므로 공급자
                # 자체를 격리할 근거가 없다.
                error_msg = f"Access denied (403): {err_str}"
                raise AuthenticationError(error_msg) from e

            if "429" in err_str:
                error_msg = f"Rate limit exceeded: {err_str}"
                raise RateLimitError(error_msg) from e

            if "503" in err_str or "overloaded" in err_str.lower():
                # 503("model is overloaded"/high demand)도 429와 같은 일시적
                # 신호다 — ServiceUnavailableError로 두면 역시 circuit breaker가
                # "공급자 장애"로 오인해 OPEN된다. RateLimitError로 분류해 짧게
                # 대기 후 재시도하게 한다(보통 수십 초 안에 회복되는 일시 과부하라
                # RateLimitError 기본 1시간 쿨다운 대신 짧은 retry_after를 준다).
                error_msg = f"Gemini overloaded (503): {err_str}"
                raise RateLimitError(error_msg, retry_after=15.0) from e

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
            # genai.list_models() 는 동기 블로킹이므로 스레드에서 수집(R8).
            discovered = await asyncio.to_thread(lambda: list(genai.list_models()))
            for m in discovered:
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

    # 무료/프리미엄 모델 우선순위(앞쪽 우선). 첫 사용 가능(200/429) 모델을 채택한다.
    _FREE_MODEL_PRIORITY = (
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-flash-latest",
        "gemini-1.5-flash",
    )
    _PREMIUM_MODEL_PRIORITY = (
        "gemini-2.5-pro",
        "gemini-pro-latest",
        "gemini-1.5-pro",
    )

    async def find_working_model(self, api_key: str, tier: str = "free") -> str | None:
        """우선순위 후보를 경량 probe(max_output_tokens=1)해 사용 가능한 첫 모델 반환.

        200(정상)·429(권한 OK·쿼터 소진)는 채택, 403/404(권한 없음·미존재)는 스킵한다.
        discover_models 로 발견된 목록과 교집합만 시도해 불필요한 404 probe 를 줄인다.
        후보를 우선순위대로 돌며 첫 성공에서 즉시 중단해 무료 쿼터 소모를 최소화한다.
        """
        genai.configure(api_key=api_key)
        priority = (
            self._FREE_MODEL_PRIORITY if tier == "free" else self._PREMIUM_MODEL_PRIORITY
        )
        discovered = {m["id"] for m in (getattr(self, "_discovered_models", None) or [])}
        candidates = [m for m in priority if not discovered or m in discovered]
        for model_name in candidates:
            try:
                model = genai.GenerativeModel(model_name)
                await model.generate_content_async(
                    "hi", generation_config={"max_output_tokens": 1}
                )
                logger.info("사용 가능 모델 확인: %s (tier=%s)", model_name, tier)
                return model_name
            except Exception as e:
                err = str(e).lower()
                if "429" in err or "resource_exhausted" in err or "quota" in err:
                    logger.info("모델 %s 권한 OK·쿼터 소진(429) → 채택", model_name)
                    return model_name
                if any(s in err for s in ("403", "permission", "404", "not found")):
                    logger.info("모델 %s 사용 불가 → 스킵", model_name)
                    continue
                logger.warning("모델 %s probe 오류 → 스킵: %s", model_name, str(e)[:60])
                continue
        return None

    async def probe_key(self, api_key: str) -> dict[str, Any]:
        """실패하면 예외를 그대로 전파한다 — `openai_compat.py`의 같은 메서드와
        동일한 원칙(그 파일 docstring 참고: "과거처럼 내부에서 삼키지 않음 —
        호출부가 403/402 등을 분류해 키를 영구 제외할 수 있게").

        실측(2026-10-13): 과거 이 메서드는 이중으로 삼켰다 — 안쪽에서 429만
        다시 raise하고 403(거부)은 "else: tier=free"로 거짓 성공 처리했고,
        바깥 try/except가 그 재-raise 된 429조차 다시 삼켜 `{"status":
        "failed", ...}` 딕셔너리로 바꿔 돌려줬다. 둘 다 `gateway.
        probe_active_keys`의 예외 핸들러(403/429/402 분류 → `key_manager.
        report_failure` → 영구 제외)에 도달하지 못해, denied 키(13개 중 12개,
        "project has been denied access") 가 활성 풀에 그대로 남았다. 그 결과
        실제 채팅 호출에서 매번 403이 났고(`generate()`), 그게 반복 집계돼
        circuit breaker가 OPEN됐다."""
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
            else:
                # 429(쿼터 소진)·403(거부)·402 등은 전부 그대로 전파한다 —
                # 호출부가 분류해 쿨다운/영구 제외를 건다.
                logger.warning(f"Key {api_key[:8]}... probe failed: {err_msg[:150]}")
                raise

        return {
            "tier": tier,
            "status": "active",
            "can_list_models": True,
        }
