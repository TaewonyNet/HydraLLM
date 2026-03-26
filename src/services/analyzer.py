import logging
from typing import Any, cast

from src.domain.enums import AgentType, ModelType, ProviderType, RoutingReason
from src.domain.interfaces import IContextAnalyzer
from src.domain.models import ChatRequest, RoutingDecision

logger = logging.getLogger(__name__)


class ContextAnalyzer(IContextAnalyzer):
    def __init__(self, max_tokens_fast_model: int = 8192):
        self._logger = logging.getLogger(__name__)
        self._max_tokens_fast_model = max_tokens_fast_model

        from src.core.config import settings

        self._free_models = self._parse_config_list(settings.free_models)
        self._premium_models = self._parse_config_list(settings.premium_models)
        self._default_free_model = settings.default_free_model
        self._default_premium_model = settings.default_premium_model

        self._model_mapping: dict[str, ModelType | str] = {
            "gemini-3.1-pro": ModelType.GEMINI_3_1_PRO,
            "gemini-3.1-ultra": ModelType.GEMINI_3_1_ULTRA,
            "gemini-3.0-pro": ModelType.GEMINI_3_PRO,
            "gemini-3.0-flash": ModelType.GEMINI_3_FLASH,
            "gemini-3.0-flash-lite": ModelType.GEMINI_3_FLASH_LITE,
            "gemini-3.1-flash-lite": ModelType.GEMINI_3_1_FLASH_LITE,
            "gemini-2.5-flash": ModelType.GEMINI_2_5_FLASH,
            "gemini-2.0-pro": ModelType.GEMINI_2_0_PRO,
            "gemini-2.0-flash": ModelType.GEMINI_2_0_FLASH,
            "gemini-2.0-thinking": ModelType.GEMINI_2_0_THINKING,
            "gemini-1.5-pro": ModelType.GEMINI_1_5_PRO,
            "gemini-1.5-flash": ModelType.GEMINI_1_5_FLASH,
            "gemini-pro": ModelType.GEMINI_PRO,
            "gemini-flash": ModelType.GEMINI_FLASH,
            "gemini-pro-vision": ModelType.GEMINI_PRO_VISION,
            "llama-4-70b": ModelType.GROQ_LLAMA_4_70B,
            "llama-4-8b": ModelType.GROQ_LLAMA_4_8B,
            "llama-3.3-70b": ModelType.GROQ_LLAMA_3_3_70B,
            "deepseek-v3.1": ModelType.GROQ_DEEPSEEK_V3_1,
            "deepseek-r1-70b": ModelType.GROQ_DEEPSEEK_R1_70B,
            "deepseek-r1-32b": ModelType.GROQ_DEEPSEEK_R1_32B,
            "groq": ModelType.GROQ_GROQ,
            "gpt-5.3": ModelType.CEREBRAS_GPT_5_3_CODEX,
            "qwen-3": ModelType.CEREBRAS_QWEN_3_235B,
            "glm-4.6": ModelType.CEREBRAS_GLM_4_6,
            "gpt-oss": ModelType.CEREBRAS_GPT_OSS_120B,
            "llama-3.3-cerebras": ModelType.CEREBRAS_LLAMA_3_3_70B,
            "llama": ModelType.CEREBRAS_LLAMA,
            "ollama": ModelType.OLLAMA_MODEL,
            "llama3": ModelType.OLLAMA_MODEL,
            "mllm/auto": "auto",
            "opencode": ModelType.OPENCODE_MODEL,
            "openclaw": ModelType.OPENCLAW_MODEL,
            "gpt-3.5-turbo": ModelType.GEMINI_1_5_FLASH,
            "gpt-4": ModelType.GEMINI_1_5_PRO,
            "gpt-4o": ModelType.GEMINI_1_5_PRO,
            "gpt-4o-mini": ModelType.GEMINI_1_5_FLASH,
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

        if model_hint == "mllm/auto":
            model_hint = "auto"

        preferred_provider = None
        if model_hint.endswith("/auto"):
            provider_str = model_hint.split("/")[0]
            model_hint = "auto"
            try:
                preferred_provider = ProviderType(provider_str)
            except ValueError:
                pass

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
                self._logger.info(
                    f"Auto-mapping generic 'opencode' to working sub-model: {working_model}"
                )

        requested_model = self._parse_model_hint(model_hint)
        estimated_tokens = request.estimate_token_count()
        has_images = request.has_images()
        routing_strategy = self._determine_strategy(
            estimated_tokens,
            has_images,
            requested_model,
            available_tiers,
            preferred_provider,
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
        )

        return decision

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
                if has_images:
                    model = (
                        self._default_premium_model
                        if has_premium
                        else self._default_free_model
                    )
                else:
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

        # 토큰 기반 2단계 라우팅: 짧은 요청→GROQ, 장문→GEMINI
        # GROQ/Cerebras 모두 context limit이 8192이므로 동일 기준 적용
        fast_threshold = self._max_tokens_fast_model

        if estimated_tokens < fast_threshold:
            return {
                "provider": ProviderType.GROQ,
                "model": ModelType.GROQ_LLAMA_3_3_70B,
                "reason": RoutingReason.TOKEN_COUNT.value,
            }
        else:
            has_premium_gemini = available_tiers and "premium" in available_tiers.get(
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

    def _get_target_for_model(
        self, model: ModelType | str
    ) -> ProviderType | AgentType:
        model_to_target: dict[ModelType, ProviderType | AgentType] = {
            ModelType.GEMINI_3_1_PRO: ProviderType.GEMINI,
            ModelType.GEMINI_3_1_ULTRA: ProviderType.GEMINI,
            ModelType.GEMINI_3_PRO: ProviderType.GEMINI,
            ModelType.GEMINI_3_FLASH: ProviderType.GEMINI,
            ModelType.GEMINI_3_FLASH_LITE: ProviderType.GEMINI,
            ModelType.GEMINI_3_1_FLASH_LITE: ProviderType.GEMINI,
            ModelType.GEMINI_2_5_FLASH: ProviderType.GEMINI,
            ModelType.GEMINI_2_0_PRO: ProviderType.GEMINI,
            ModelType.GEMINI_2_0_FLASH: ProviderType.GEMINI,
            ModelType.GEMINI_2_0_THINKING: ProviderType.GEMINI,
            ModelType.GEMINI_1_5_PRO: ProviderType.GEMINI,
            ModelType.GEMINI_1_5_FLASH: ProviderType.GEMINI,
            ModelType.GEMINI_PRO: ProviderType.GEMINI,
            ModelType.GEMINI_PRO_VISION: ProviderType.GEMINI,
            ModelType.GEMINI_FLASH: ProviderType.GEMINI,
            ModelType.GEMINI_FLASH_VISION: ProviderType.GEMINI,
            ModelType.GROQ_LLAMA_4_70B: ProviderType.GROQ,
            ModelType.GROQ_LLAMA_4_8B: ProviderType.GROQ,
            ModelType.GROQ_LLAMA_3_3_70B: ProviderType.GROQ,
            ModelType.GROQ_DEEPSEEK_V3_1: ProviderType.GROQ,
            ModelType.GROQ_DEEPSEEK_R1_70B: ProviderType.GROQ,
            ModelType.GROQ_DEEPSEEK_R1_32B: ProviderType.GROQ,
            ModelType.GROQ_GROQ: ProviderType.GROQ,
            ModelType.CEREBRAS_GPT_5_3_CODEX: ProviderType.CEREBRAS,
            ModelType.CEREBRAS_DEEPSEEK_R1_70B: ProviderType.CEREBRAS,
            ModelType.CEREBRAS_GLM_4_6: ProviderType.CEREBRAS,
            ModelType.CEREBRAS_GPT_OSS_120B: ProviderType.CEREBRAS,
            ModelType.CEREBRAS_LLAMA_3_3_70B: ProviderType.CEREBRAS,
            ModelType.CEREBRAS_LLAMA: ProviderType.CEREBRAS,
            ModelType.OLLAMA_MODEL: AgentType.OLLAMA,
            ModelType.OPENCODE_MODEL: AgentType.OPENCODE,
            ModelType.OPENCLAW_MODEL: AgentType.OPENCLAW,
        }

        if isinstance(model, ModelType):
            return model_to_target.get(model, ProviderType.GEMINI)

        if hasattr(self, "_dynamic_targets") and model in self._dynamic_targets:
            return self._dynamic_targets[model]

        model_lower = str(model).lower()
        if "llama" in model_lower or "groq" in model_lower:
            return ProviderType.GROQ

        if "ollama" in model_lower:
            return AgentType.OLLAMA

        return cast(ProviderType | AgentType, ProviderType.GEMINI)

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
                "description": "Automatically selects the best model based on context.",
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
                "description": "Automatically selects the best Gemini model.",
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
                "description": "Automatically selects the best Groq model.",
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
                "description": "Automatically selects the best Cerebras model.",
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
        from src.core.config import settings

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
