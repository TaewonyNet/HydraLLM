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
    GEMINI_3_1_ULTRA = "gemini-3.1-ultra"
    GEMINI_3_PRO = "gemini-3.0-pro"
    GEMINI_3_FLASH = "gemini-3.0-flash"
    GEMINI_3_FLASH_LITE = "gemini-3.0-flash-lite"
    GEMINI_3_1_FLASH_LITE = "gemini-3.1-flash-lite"
    GEMINI_2_5_FLASH = "gemini-2.5-flash"
    GEMINI_2_0_PRO = "gemini-2.0-pro"
    GEMINI_2_0_FLASH = "gemini-2.0-flash"
    GEMINI_2_0_THINKING = "gemini-2.0-thinking"
    GEMINI_1_5_PRO = "gemini-1.5-pro"
    GEMINI_1_5_FLASH = "gemini-1.5-flash"
    GEMINI_PRO = "gemini-pro"
    GEMINI_PRO_VISION = "gemini-pro-vision"
    GEMINI_FLASH = "gemini-flash"
    GEMINI_FLASH_VISION = "gemini-flash-vision"

    # Groq Models
    GROQ_LLAMA_4_70B = "llama-4-70b"
    GROQ_LLAMA_4_8B = "llama-4-8b"
    GROQ_LLAMA_3_3_70B = "llama-3.3-70b-versatile"
    GROQ_DEEPSEEK_V3_1 = "deepseek-v3.1"
    GROQ_DEEPSEEK_R1_70B = "deepseek-r1-distill-llama-70b"
    GROQ_DEEPSEEK_R1_32B = "deepseek-r1-distill-qwen-32b"

    # Cerebras Models
    CEREBRAS_GPT_5_3_CODEX = "gpt-5.3-codex-spark"
    CEREBRAS_DEEPSEEK_R1_70B = "deepseek-r1-distill-llama-70b"
    CEREBRAS_GLM_4_6 = "glm-4.6-reap-252b"
    CEREBRAS_QWEN_3_235B = "qwen-3-235b-instruct"
    CEREBRAS_GPT_OSS_120B = "gpt-oss-120b"
    CEREBRAS_LLAMA_3_3_70B = "llama-3.3-70b"
    # Other Models
    GROQ_GROQ = "groq"
    CEREBRAS_LLAMA = "llama"
    OLLAMA_MODEL = "ollama"
    OPENCODE_MODEL = "opencode"
    OPENCLAW_MODEL = "openclaw"


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
