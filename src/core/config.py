from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    port: int = 8000
    host: str = "0.0.0.0"
    log_level: str = "INFO"
    debug: bool = False
    locale: str = "ko"
    embedding_model: str = "bge-m3:latest"
    # 웹 키워드 추출용 로컬 LLM (Ollama 모델명). 비워두면 정규식 폴백만 사용.
    keyword_extraction_model: str | None = None
    # 키워드/라이브 레퍼런스 파일 디렉터리 (프로젝트 루트 기준 상대 경로 허용)
    data_dir: str = "data"

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

    # 요청 전체(공급자 재시도 전부 포함) 마감 시간(초). 클라이언트가 무기한 대기하지
    # 않도록 Gateway.process_request 가 이 시간으로 asyncio.wait_for 를 건다(실측: 413을
    # 키 실패로 오인해 Gemini 로 넘어갔다가 403을 받고 클라이언트가 300초 타임아웃 당한
    # 사고 재발 방지).
    request_deadline_seconds: int = 90

    enable_key_rotation: bool = True
    enable_rate_limiting: bool = True
    enable_logging: bool = True
    enable_auto_web_fetch: bool = True
    web_cache_ttl_hours: int = 24

    # 웹 검색 엔진 폴백 순서(쉼표 구분). 사용 가능: duckduckgo, bing, openserp.
    # 앞 엔진이 0건이면 다음 엔진으로 넘어간다. openserp 는 별도 서버
    # (https://github.com/karust/openserp)가 떠 있어야 하므로 기본값에는 넣지 않는다.
    search_providers: list[str] | str = "duckduckgo,bing"
    # openserp 서버 주소와 엔진 경로(google|bing|duck|yandex|baidu|mega).
    # mega 는 여러 엔진을 섞어 baidu 등 언어가 안 맞는 결과가 상위에 오기 쉬워 기본은 duck.
    openserp_base_url: str = "http://127.0.0.1:7000"
    openserp_engine: str = "duck"
    # openserp lang 파라미터(예: KO, EN). 비워두면 openserp 기본값을 쓴다.
    openserp_lang: str = ""
    openserp_timeout: float = 30.0
    # SearXNG 주소(JSON API 사용, settings.yml 의 search.formats 에 json 필요).
    searxng_base_url: str = "http://127.0.0.1:8890"
    # SearXNG language 파라미터(예: ko, en). 비워두면 SearXNG 기본값.
    searxng_language: str = ""
    searxng_timeout: float = 30.0

    # 웹 검색·본문 추출을 어디서 수행할지. inprocess = 게이트웨이 내부 WebScraper(기존),
    # mcp = 검색 MCP 서버(search_mcp/server.py) 호출. 엔진 선택(SEARCH_PROVIDERS)은
    # mcp 모드에서는 MCP 서버 쪽 설정이 적용된다.
    web_backend: str = "inprocess"
    search_mcp_url: str = "http://127.0.0.1:8790/mcp"
    # 검색+본문 추출이 수십 초 걸릴 수 있어 request_deadline_seconds 보다 약간 짧게.
    search_mcp_timeout: float = 75.0
    # MCP 서버 호출이 실패하면 게이트웨이 내부 WebScraper 로 대신 수행한다.
    search_mcp_fallback: bool = True
    # 설정 시 MCP 서버·클라이언트가 Authorization: Bearer 토큰으로 인증한다.
    search_mcp_token: str = ""
    # 검색 MCP 서버 바인드 주소(서버 프로세스용).
    search_mcp_host: str = "127.0.0.1"
    search_mcp_port: int = 8790

    # Admin API 인증 키 (설정하지 않으면 인증 비활성화)
    admin_api_key: str | None = None

    provider_priority: list[str] | str = "gemini,groq,cerebras,ollama,opencode,openclaw"

    # 2026-10 실측 가용 무료 모델로 갱신(llama-3.1-8b/llama3 는 Groq 에서 더 이상 제공되지
    # 않아 제거). Groq: openai/gpt-oss-120b·openai/gpt-oss-20b·qwen/qwen3.8-27b,
    # Cerebras: gpt-oss-120b·qwen-3.8-27b (부분 문자열 매칭이라 "openai/" 접두사 있어도
    # 매칭된다). tier 분류(_build_model_info)에서 참조.
    free_models: list[
        str
    ] | str | None = (
        "gemini-1.5-flash,gemini-2.0-flash,gemini-flash-latest,"
        "gpt-oss-120b,gpt-oss-20b,qwen3.8-27b,qwen-3.8-27b,ollama"
    )
    premium_models: list[
        str
    ] | str | None = "gemini-1.5-pro,gemini-3.1-pro,llama-3.1-70b,gpt-4o"

    default_free_model: str = "gemini-1.5-flash"
    default_premium_model: str = "gemini-1.5-flash"

    default_scrape_mode: str = "standard"

    onboarding_completed: bool = False
    enabled_models: list[str] | str | None = None

    enable_context_compression: bool = True
    debug_comm_log: bool = False

    database_path: str = "gateway_sessions.sqlite"

    max_sessions: int = 50
    session_retention_days: int = 30  # 세션 보관 기간 (기본 30일, 최대 365일)
    session_compact_threshold: int = 6000  # 토큰 추정치 초과 시 compaction 트리거
    session_recent_window: int = 4  # compaction 시 유지할 최근 메시지 수

    # 무료 티어 쿼터 모니터링 근사 한도(제공자 문서 기준, 변동 시 .env 로 조정).
    # GCP 의 정확한 실시간 잔량은 API 키만으로 조회 불가하므로 자체 카운트와 비교용.
    quota_rpm_free: int = 10  # 분당 요청 한도(무료, 대표값)
    quota_rpd_free: int = 250  # 일일 요청 한도(무료, 대표값)

    model_config = {
        "env_file": ".env",
        "case_sensitive": False,
        "extra": "allow",  # Allow extra environment variables
    }


settings = Settings()
