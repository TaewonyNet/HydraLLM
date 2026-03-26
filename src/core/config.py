from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    port: int = 8000
    host: str = "0.0.0.0"
    log_level: str = "INFO"
    debug: bool = False

    max_tokens_fast_model: int = 8192
    max_tokens_expensive_model: int = 32768

    gemini_keys: list[str] | str | None = None
    groq_keys: list[str] | str | None = None
    cerebras_keys: list[str] | str | None = None
    ollama_keys: list[str] | str | None = "ollama"  # Default dummy key
    opencode_keys: list[str] | str | None = None
    openclaw_keys: list[str] | str | None = None

    gemini_timeout: int = 30
    groq_timeout: int = 20
    cerebras_timeout: int = 45
    ollama_timeout: int = 60
    opencode_timeout: int = 30
    openclaw_timeout: int = 30

    ollama_base_url: str = "http://localhost:11434/v1"
    opencode_base_url: str = "http://localhost:8080/v1"
    openclaw_base_url: str = "http://localhost:9000/v1"

    max_retries: int = 3
    initial_retry_delay: float = 1.0
    backoff_multiplier: float = 2.0

    enable_key_rotation: bool = True
    enable_rate_limiting: bool = True
    enable_logging: bool = True
    enable_auto_web_fetch: bool = True

    free_models: list[
        str
    ] | str | None = "gemini-1.5-flash,gemini-2.0-flash,llama-3.1-8b,llama3,ollama"
    premium_models: list[
        str
    ] | str | None = "gemini-1.5-pro,gemini-3.1-pro,llama-3.1-70b,gpt-4o"

    default_free_model: str = "gemini-flash-latest"
    default_premium_model: str = "gemini-pro-latest"

    default_scrape_mode: str = "standard"

    onboarding_completed: bool = False
    enabled_models: list[str] | str | None = None

    enable_context_compression: bool = True

    max_sessions: int = 50

    model_config = {
        "env_file": ".env",
        "case_sensitive": False,
        "extra": "allow",  # Allow extra environment variables
    }


settings = Settings()
