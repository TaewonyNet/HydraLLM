import logging
import re
from typing import Any, cast

from src.core.config import settings
from src.domain.enums import AgentType, ModelType, ProviderType, RoutingReason
from src.domain.interfaces import IContextAnalyzer
from src.domain.models import ChatRequest, RoutingDecision
from src.i18n import t

logger = logging.getLogger(__name__)


class ContextAnalyzer(IContextAnalyzer):
    def __init__(self, max_tokens_fast_model: int = 8192):
        self._logger = logging.getLogger(__name__)
        self._max_tokens_fast_model = max_tokens_fast_model

        self._free_models = self._parse_config_list(settings.free_models)
        self._premium_models = self._parse_config_list(settings.premium_models)
        self._default_free_model = settings.default_free_model
        self._default_premium_model = settings.default_premium_model
        self._provider_priority = self._parse_config_list(settings.provider_priority)
        # gateway 등 외부에서 read-only 로 참조할 수 있도록 공용 속성도 노출.
        self.provider_priority: list[str] = list(self._provider_priority)

        self._model_mapping: dict[str, ModelType | str] = {
            "mllm/auto": "auto",
            "opencode": "opencode",
            "openclaw": "openclaw",
            "gpt-3.5-turbo": "gemini-2.5-flash",
            "gpt-4": "gemini-2.5-pro",
            "gpt-4o": "gemini-2.5-pro",
            "gpt-4o-mini": "gemini-2.5-flash",
            "claude-3-opus": "gemini-2.5-pro",
            "claude-3-sonnet": "gemini-2.5-pro",
            "claude-3-haiku": "gemini-2.5-flash",
        }

        self._provider_limits = {
            ProviderType.GEMINI: 32768,
            ProviderType.GROQ: max_tokens_fast_model,
            ProviderType.CEREBRAS: max_tokens_fast_model,
        }

        self._agent_limits = {
            AgentType.OLLAMA: 32768,
            AgentType.OPENCODE: 32768,
            AgentType.OPENCLAW: 32768,
        }

        self._token_costs = {
            ProviderType.GEMINI: 0.00005,
            ProviderType.GROQ: 0.000025,
            ProviderType.CEREBRAS: 0.00001,
        }

    def _parse_config_list(self, value: list[str] | str | None) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [m.strip() for m in value.split(",") if m.strip()]
        return value

    async def analyze(
        self,
        request: ChatRequest,
        available_tiers: dict[ProviderType, set[str]] | None = None,
    ) -> RoutingDecision:
        model_hint = request.model.lower() if request.model else "auto"
        preferred_provider = None

        if "/" in model_hint:
            parts = model_hint.split("/")
            p_name = parts[0]
            if p_name in [p.value for p in ProviderType]:
                preferred_provider = ProviderType(p_name)
                model_hint = parts[1]
            elif p_name in [a.value for a in AgentType]:
                return RoutingDecision(
                    agent=AgentType(p_name),
                    model_name=parts[1],
                    reason=RoutingReason.MODEL_HINT.value,
                    confidence=1.0,
                )

        web_required = self.detect_web_intent(request)
        estimated_tokens = request.estimate_token_count()
        has_images = request.has_images()

        if model_hint in ["mllm/auto", "auto", "default"]:
            model_hint = "auto"
            if estimated_tokens > 7000:
                preferred_provider = ProviderType.GEMINI
            elif not preferred_provider and available_tiers:
                if (
                    ProviderType.GROQ in available_tiers
                    and available_tiers[ProviderType.GROQ]
                ):
                    preferred_provider = ProviderType.GROQ
                elif (
                    ProviderType.CEREBRAS in available_tiers
                    and available_tiers[ProviderType.CEREBRAS]
                ):
                    preferred_provider = ProviderType.CEREBRAS

        if model_hint == "opencode":
            model_list = self.get_supported_models_info()
            working_model = next(
                (m["id"] for m in model_list if "github-copilot" in m["id"]),
                next(
                    (
                        m["id"]
                        for m in model_list
                        if "/" in m["id"] and "opencode/" not in m["id"]
                    ),
                    None,
                ),
            )
            if working_model:
                model_hint = working_model

        requested_model = self._parse_model_hint(model_hint)

        routing_strategy = self._determine_strategy(
            estimated_tokens,
            has_images,
            requested_model,
            available_tiers,
            preferred_provider,
            web_required=web_required,
        )
        _ = self._calculate_cost(routing_strategy.get("provider"), estimated_tokens)

        model = routing_strategy["model"]
        model_name = model.value if hasattr(model, "value") else str(model)

        decision = RoutingDecision(
            provider=routing_strategy.get("provider"),
            agent=routing_strategy.get("agent"),
            model_name=model_name,
            reason=routing_strategy["reason"],
            confidence=1.0,
            web_search_required=web_required,
        )

        return decision

    def detect_web_intent(self, request: ChatRequest) -> bool:
        if request.has_search or request.web_fetch:
            return True

        content_text = self.extract_last_user_content(request)
        if not content_text:
            return False

        # Detect URL
        if re.search(r"https?://[^\s/$.?#].[^\sㄱ-ㅎㅏ-ㅣ가-힣]*", content_text):
            return True

        # Detect requests for real-time info or facts likely to change
        dynamic_keywords = [
            "뉴스",
            "소식",
            "날씨",
            "주가",
            "코인",
            "가격",
            "순위",
            "결과",
            "일정",
            "오늘",
            "지금",
            "현재",
            "news",
            "weather",
            "price",
            "stock",
            "score",
            "schedule",
            "current",
            "latest",
            "today",
        ]
        if any(kw in content_text.lower() for kw in dynamic_keywords):
            return True

        return False

    @staticmethod
    def extract_last_user_content(request: ChatRequest) -> str:
        if not request.messages:
            return ""
        last_msg = request.messages[-1]
        if isinstance(last_msg.content, str):
            return last_msg.content
        if isinstance(last_msg.content, list):
            parts = []
            for part in last_msg.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
            return "".join(parts)
        return ""

    def _parse_model_hint(self, model_hint: str | None) -> ModelType | str | None:
        if not model_hint:
            return None

        return self._model_mapping.get(model_hint)

    def _determine_strategy(
        self,
        estimated_tokens: int,
        has_images: bool,
        requested_model: ModelType | str | None,
        available_tiers: dict[ProviderType, set[str]] | None = None,
        preferred_provider: ProviderType | None = None,
        web_required: bool = False,
    ) -> dict[str, Any]:
        if requested_model:
            target = self._get_target_for_model(requested_model)
            result: dict[str, Any] = {
                "model": requested_model,
                "reason": RoutingReason.MODEL_HINT.value,
            }
            if isinstance(target, ProviderType):
                result["provider"] = target
            else:
                result["agent"] = target
            return result

        if preferred_provider:
            if preferred_provider == ProviderType.GEMINI:
                has_premium = available_tiers and "premium" in available_tiers.get(
                    ProviderType.GEMINI, set()
                )
                model = (
                    self._default_premium_model
                    if has_premium
                    else self._default_free_model
                )
                return {
                    "provider": ProviderType.GEMINI,
                    "model": model,
                    "reason": RoutingReason.MODEL_HINT.value,
                }
            elif preferred_provider == ProviderType.GROQ:
                return {
                    "provider": ProviderType.GROQ,
                    "model": ModelType.GROQ_LLAMA_3_3_70B,
                    "reason": RoutingReason.MODEL_HINT.value,
                }
            elif preferred_provider == ProviderType.CEREBRAS:
                return {
                    "provider": ProviderType.CEREBRAS,
                    "model": ModelType.CEREBRAS_LLAMA,
                    "reason": RoutingReason.MODEL_HINT.value,
                }

        if has_images:
            has_premium_gemini = available_tiers and "premium" in available_tiers.get(
                ProviderType.GEMINI, set()
            )
            return {
                "provider": ProviderType.GEMINI,
                "model": self._default_premium_model
                if has_premium_gemini
                else self._default_free_model,
                "reason": RoutingReason.IMAGE_PRESENT.value,
            }

        if web_required:
            has_premium_gemini = available_tiers and "premium" in available_tiers.get(
                ProviderType.GEMINI, set()
            )
            return {
                "provider": ProviderType.GEMINI,
                "model": self._default_premium_model
                if has_premium_gemini
                else self._default_free_model,
                "reason": "WEB_INTENT_REQUIRE_INTELLIGENCE",
            }

        preferred_external = [
            p for p in self._provider_priority if p in ["gemini", "groq", "cerebras"]
        ]
        fast_threshold = self._max_tokens_fast_model

        if estimated_tokens < fast_threshold:
            if "groq" in self._provider_priority:
                has_groq = available_tiers and available_tiers.get(ProviderType.GROQ)
                if has_groq:
                    return {
                        "provider": ProviderType.GROQ,
                        "model": ModelType.GROQ_LLAMA_3_3_70B,
                        "reason": RoutingReason.TOKEN_COUNT.value,
                    }

            if "cerebras" in self._provider_priority:
                has_cerebras = available_tiers and available_tiers.get(
                    ProviderType.CEREBRAS
                )
                if has_cerebras:
                    return {
                        "provider": ProviderType.CEREBRAS,
                        "model": ModelType.CEREBRAS_LLAMA,
                        "reason": RoutingReason.TOKEN_COUNT.value,
                    }

            for p_name in preferred_external:
                p_type = ProviderType(p_name)
                if available_tiers and available_tiers.get(p_type):
                    target_model = (
                        self._default_free_model
                        if p_type == ProviderType.GEMINI
                        else self._get_default_model_for_provider(p_type)
                    )
                    return {
                        "provider": p_type,
                        "model": target_model,
                        "reason": RoutingReason.TOKEN_COUNT.value,
                    }

            return {
                "provider": ProviderType.GROQ,
                "model": ModelType.GROQ_LLAMA_3_3_70B,
                "reason": RoutingReason.TOKEN_COUNT.value,
            }
        else:
            if available_tiers is not None and "gemini" in self._provider_priority:
                has_gemini = available_tiers.get(ProviderType.GEMINI)
                if has_gemini:
                    has_premium_gemini = "premium" in available_tiers.get(
                        ProviderType.GEMINI, set()
                    )

                    target_model = (
                        self._default_premium_model
                        if has_premium_gemini
                        else self._default_free_model
                    )
                    return {
                        "provider": ProviderType.GEMINI,
                        "model": target_model,
                        "reason": RoutingReason.TOKEN_COUNT.value,
                    }

            for p_name in preferred_external:
                p_type = ProviderType(p_name)
                if available_tiers and available_tiers.get(p_type):
                    return {
                        "provider": p_type,
                        "model": self._get_default_model_for_provider(p_type),
                        "reason": RoutingReason.TOKEN_COUNT.value,
                    }

            return {
                "provider": ProviderType.GEMINI,
                "model": self._default_free_model,
                "reason": RoutingReason.TOKEN_COUNT.value,
            }

    def _get_default_model_for_provider(self, provider: ProviderType) -> str:
        if provider == ProviderType.GEMINI:
            return self._default_free_model
        elif provider == ProviderType.GROQ:
            return "llama-3.3-70b-versatile"
        else:
            return "llama3.1-70b"

    def get_default_model_for_provider(self, provider: ProviderType) -> str:
        # gateway fallback 루프에서 호출하는 공용 API.
        return self._get_default_model_for_provider(provider)

    def _get_target_for_model(self, model: ModelType | str) -> ProviderType | AgentType:
        """
        Determines the provider/agent strictly based on string patterns.
        Prioritizes dynamic targets and explicit prefixes.
        """
        # 1. Resolve to string
        model_id = model.value if hasattr(model, "value") else str(model)

        # 2. Check dynamically registered models (from discover_models)
        if hasattr(self, "_dynamic_targets"):
            clean_id = model_id.replace("models/", "")
            if clean_id in self._dynamic_targets:
                return self._dynamic_targets[clean_id]
            if model_id in self._dynamic_targets:
                return self._dynamic_targets[model_id]

        # 3. Explicit provider/agent prefix handling (e.g. 'gemini/custom-model')
        if "/" in model_id:
            prefix = model_id.split("/")[0].lower()
            prefix_map = {
                "gemini": ProviderType.GEMINI,
                "groq": ProviderType.GROQ,
                "cerebras": ProviderType.CEREBRAS,
                "ollama": AgentType.OLLAMA,
                "opencode": AgentType.OPENCODE,
                "openclaw": AgentType.OPENCLAW,
            }
            if prefix in prefix_map:
                return prefix_map[prefix]

        # 4. Pattern-based routing
        model_lower = model_id.lower()
        if "gemini" in model_lower:
            return ProviderType.GEMINI
        if "groq" in model_lower or "llama" in model_lower or "deepseek" in model_lower:
            if "ollama" in model_lower:
                return AgentType.OLLAMA
            return ProviderType.GROQ
        if "cerebras" in model_lower:
            return ProviderType.CEREBRAS
        if (
            "ollama" in model_lower
            or "gemma" in model_lower
            or "qwen" in model_lower
            or "phi" in model_lower
        ):
            return AgentType.OLLAMA
        if "opencode" in model_lower:
            return AgentType.OPENCODE
        if "openclaw" in model_lower:
            return AgentType.OPENCLAW

        # 5. Default fallback
        return ProviderType.GEMINI

    def _map_model_name(self, request_model: str | None) -> str:
        """
        Simply returns the requested model name, stripping any provider prefix.
        """
        if not request_model or request_model.lower() == "auto":
            return self._default_free_model

        if "/" in request_model:
            parts = request_model.split("/")
            if len(parts) > 1:
                return parts[1]

        return request_model

    def _calculate_cost(self, provider: ProviderType | None, tokens: int) -> float:
        if not provider:
            return 0.0
        cost_per_token = self._token_costs.get(provider, 0.00005)
        return tokens * cost_per_token

    def get_provider_limits(self) -> dict[ProviderType, int]:
        return self._provider_limits.copy()

    def _get_virtual_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "auto",
                "display_name": "GATEWAY/auto",
                "owned_by": "gateway",
                "tier": "free",
                "description": t("model.auto_desc"),
                "capabilities": {
                    "max_tokens": 1000000,
                    "multimodal": True,
                    "has_search": True,
                    "cost_per_token": 0.0,
                },
            },
            {
                "id": "gemini/auto",
                "display_name": "GEMINI/auto",
                "owned_by": "gemini",
                "tier": "free",
                "description": t("model.gemini_auto_desc"),
                "capabilities": {
                    "max_tokens": 1000000,
                    "multimodal": True,
                    "has_search": True,
                    "cost_per_token": 0.0,
                },
            },
            {
                "id": "groq/auto",
                "display_name": "GROQ/auto",
                "owned_by": "groq",
                "tier": "free",
                "description": t("model.groq_auto_desc"),
                "capabilities": {
                    "max_tokens": 32768,
                    "multimodal": False,
                    "has_search": True,
                    "cost_per_token": 0.0,
                },
            },
            {
                "id": "cerebras/auto",
                "display_name": "CEREBRAS/auto",
                "owned_by": "cerebras",
                "tier": "free",
                "description": t("model.cerebras_auto_desc"),
                "capabilities": {
                    "max_tokens": 32768,
                    "multimodal": False,
                    "has_search": False,
                    "cost_per_token": 0.0,
                },
            },
        ]

    def _build_model_info(
        self, model_name: str, model_type: ModelType | str
    ) -> dict[str, Any]:
        target = self._get_target_for_model(model_type)

        max_tokens = 0
        if isinstance(target, ProviderType):
            max_tokens = self._provider_limits.get(target, 0)
            owned_by = target.value if hasattr(target, "value") else str(target)
            cost = self._token_costs.get(target)
        else:
            max_tokens = self._agent_limits.get(target, 0)
            owned_by = "local-agent"
            cost = None

        is_multimodal = False
        if isinstance(target, ProviderType) and target == ProviderType.GEMINI:
            is_multimodal = any(
                v in model_name.lower() for v in ["vision", "pro", "flash"]
            )

        has_search = False
        if isinstance(target, ProviderType):
            if target == ProviderType.GEMINI:
                has_search = any(
                    v in model_name for v in ["1.5", "2.0", "2.5", "3.0", "3.1"]
                ) or any(v in model_name.lower() for v in ["pro", "flash", "ultra"])
            elif target == ProviderType.GROQ:
                has_search = "gpt-oss" in model_name.lower()

        meta = {}
        if hasattr(self, "_model_metadata"):
            meta = self._model_metadata.get(model_name, {})

        tier = meta.get("tier", "free")
        if any(m in model_name for m in self._premium_models):
            tier = "premium"
        elif any(m in model_name for m in self._free_models):
            tier = "free"

        return {
            "id": model_name,
            "display_name": f"{owned_by.upper()}/{model_name}",
            "owned_by": owned_by,
            "tier": tier,
            "description": meta.get("description"),
            "capabilities": {
                "max_tokens": meta.get("input_token_limit") or max_tokens,
                "multimodal": is_multimodal,
                "has_search": has_search,
                "cost_per_token": cost,
            },
        }

    def get_supported_models_info(self) -> list[dict[str, Any]]:
        enabled_list = self._parse_config_list(settings.enabled_models)

        models_info = self._get_virtual_models()
        for model_name, model_type in self._model_mapping.items():
            if (
                settings.onboarding_completed
                and enabled_list
                and model_name not in enabled_list
            ):
                continue
            models_info.append(self._build_model_info(model_name, model_type))
        return models_info

    def get_all_discovered_models_info(self) -> list[dict[str, Any]]:
        models_info = self._get_virtual_models()
        for model_name, model_type in self._model_mapping.items():
            models_info.append(self._build_model_info(model_name, model_type))
        return models_info

    def register_model(
        self,
        model_name: str,
        provider: ProviderType | AgentType,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._model_mapping[model_name] = model_name

        if not hasattr(self, "_dynamic_targets"):
            self._dynamic_targets: dict[str, ProviderType | AgentType] = {}
        self._dynamic_targets[model_name] = provider

        if not hasattr(self, "_model_metadata"):
            self._model_metadata: dict[str, dict[str, Any]] = {}
        if metadata:
            self._model_metadata[model_name] = metadata
