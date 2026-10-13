from enum import Enum


class ProviderType(Enum):
    """
    Supported LLM providers for the gateway.
    """

    GEMINI = "gemini"
    GROQ = "groq"
    CEREBRAS = "cerebras"


class AgentType(Enum):
    """
    Supported Agent Engines.
    """

    OLLAMA = "ollama"
    OPENCODE = "opencode"
    OPENCLAW = "openclaw"


class ModelType(Enum):
    """
    Supported model types for each provider.
    """

    GEMINI_3_1_PRO = "gemini-3.1-pro"
    GEMINI_3_PRO = "gemini-3.0-pro"
    GEMINI_3_FLASH = "gemini-3.0-flash"
    GEMINI_2_5_PRO = "gemini-2.5-pro"
    GEMINI_2_5_FLASH = "gemini-2.5-flash"
    GEMINI_2_0_PRO = "gemini-2.0-pro"
    GEMINI_2_0_FLASH = "gemini-2.0-flash"
    GEMINI_2_0_THINKING = "gemini-2.0-thinking"
    GEMINI_PRO = "gemini-pro"
    GEMINI_PRO_VISION = "gemini-pro-vision"
    GEMINI_FLASH = "gemini-flash"
    GEMINI_FLASH_VISION = "gemini-flash-vision"

    # Groq Models (2026-10 실측 /models 기준. llama-4-*/llama-3.3-70b-versatile/
    # deepseek-* 는 더 이상 Groq 카탈로그에 없어 제거 — 아래 4개만 실존.)
    GROQ_GPT_OSS_120B = "openai/gpt-oss-120b"
    GROQ_GPT_OSS_20B = "openai/gpt-oss-20b"
    GROQ_QWEN_3_8_27B = "qwen/qwen3.8-27b"
    GROQ_ALLAM_2_7B = "allam-2-7b"

    # Cerebras Models (2026-10 실측 /models 기준. gpt-5.3-codex-spark/deepseek-r1-*/
    # glm-4.6-*/qwen-3-235b-instruct/llama-3.3-70b 는 더 이상 Cerebras 카탈로그에
    # 없어 제거 — 아래 2개만 실존.)
    CEREBRAS_GPT_OSS_120B = "gpt-oss-120b"
    CEREBRAS_QWEN_3_8_27B = "qwen-3.8-27b"
    # Other Models
    GROQ_GROQ = "groq"
    CEREBRAS_LLAMA = "llama"
    OLLAMA_MODEL = "ollama"
    OPENCODE_MODEL = "opencode"
    OPENCLAW_MODEL = "openclaw"


class TierType(Enum):
    """
    API key tier classification.
    """

    FREE = "free"
    STANDARD = "standard"
    PREMIUM = "premium"
    EXPERIMENTAL = "experimental"
    UNKNOWN = "unknown"


class PartType(str, Enum):
    """메시지 파트 타입."""

    TEXT = "text"
    WEB_FETCH = "web_fetch"
    WEB_SEARCH = "web_search"
    COMPACTION = "compaction"
    STEP_COST = "step_cost"
    RETRY = "retry"


class RoutingReason(Enum):
    """
    Reasons for routing decisions.
    """

    TOKEN_COUNT = "token_count"
    IMAGE_PRESENT = "image_present"
    MODEL_HINT = "model_hint"
    KEY_AVAILABILITY = "key_availability"
    RATE_LIMIT = "rate_limit"


class ResponseFormat(Enum):
    """
    Supported response formats.
    """

    JSON = "json"
    STREAM = "stream"


class TokenType(Enum):
    """
    Token type for counting.
    """

    TEXT = "text"
    IMAGE = "image"


class SafetySetting(Enum):
    """
    Safety settings for content filtering.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"


class RoutingStrategy(Enum):
    """
    Routing strategies for request distribution.
    """

    TOKEN_COUNT = "token_count"
    IMAGE_PRESENT = "image_present"
    MODEL_HINT = "model_hint"
    KEY_AVAILABILITY = "key_availability"
    RATE_LIMIT = "rate_limit"
    RANDOM = "random"
    ROUND_ROBIN = "round_robin"
    LOAD_BALANCED = "load_balanced"
    COST_OPTIMIZED = "cost_optimized"

    UNKNOWN_MODEL = "unknown"

    SEARCH_REQUIRED = "search_required"
