import re
from enum import Enum


class ErrorCategory(Enum):
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    AUTH_FAILURE = "AUTH_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    # 요청 페이로드가 공급자 한도를 초과(HTTP 413). 키/계정 문제가 아니므로 키 쿨다운을
    # 유발하지 않고, 더 큰 한도 공급자로 전환하거나 클라이언트에 즉시 알려야 한다.
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    # 게이트웨이 전체 요청 마감 시간(REQUEST_DEADLINE_SECONDS) 초과.
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"
    # HTTP 402 Payment Required — 결제/빌링 문제. 429(쿼터, 시간이 지나면 풀림)와 달리
    # 재시도해도 안 풀리는 계정 설정 문제라 403 과 동일하게 키를 영구 제외해야 한다.
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"


class BaseAppError(Exception):
    def __init__(
        self, message: str, category: ErrorCategory = ErrorCategory.INTERNAL_ERROR
    ):
        self.message = message
        self.category = category
        super().__init__(self.message)


class ProviderError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.SERVICE_UNAVAILABLE)


class ResourceExhaustedError(BaseAppError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message, ErrorCategory.RESOURCE_EXHAUSTED)
        # Circuit OPEN 거부 시 "언제 다시 시도하면 될지"(남은 복구 시간)를 실어
        # endpoints.py 가 HTTP 503 의 Retry-After 헤더로 전달한다. 키 고갈 등 다른
        # 원인이면 None(모른다는 뜻 — endpoints.py 가 헤더를 생략한다).
        self.retry_after = retry_after


class ConfigurationError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.CONFIG_ERROR)


class RoutingError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.INTERNAL_ERROR)


class AuthenticationError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.AUTH_FAILURE)


class RateLimitError(BaseAppError):
    """HTTP 429 Rate Limit Exceeded.

    실측(2026-10): Groq 가 분당 토큰 한도로 429 를 냈는데 같은 시각 직접 호출은 200
    (키 2개·모델 3개 전부 정상, 일일 잔여 997~999/일)이었다 — 즉 429 는 "그 키가 지금
    분당 한도에 걸렸다"는 뜻일 뿐 공급자 장애가 아니다. circuit_breaker 를 OPEN 시키지
    않고 키 단위 쿨다운만 걸어야 한다(resilience_manager 전용 처리)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message, ErrorCategory.RESOURCE_EXHAUSTED)
        # 공급자가 Retry-After 헤더/본문으로 알려준 대기 시간(초). 모르면 None —
        # key_manager 는 이 값이 있으면 쿨다운에, endpoints.py 는 HTTP 429 응답의
        # Retry-After 헤더에 반영한다(없으면 계산값으로 대체).
        self.retry_after = retry_after


class ModelNotFoundError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.MODEL_NOT_FOUND)


class RequestValidationError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.VALIDATION_ERROR)


class ResponseFormatError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.INTERNAL_ERROR)


class ProviderConnectionError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.SERVICE_UNAVAILABLE)


class ProviderTimeoutError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.SERVICE_UNAVAILABLE)


class ServiceUnavailableError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.SERVICE_UNAVAILABLE)


class InvalidRequestError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.VALIDATION_ERROR)


class UnsupportedFeatureError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.VALIDATION_ERROR)


class ContentFilterError(BaseAppError):
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.SERVICE_UNAVAILABLE)


class ProviderRateLimitError(ProviderError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.category = ErrorCategory.RESOURCE_EXHAUSTED
        self.retry_after = retry_after


class ProviderServerError(ProviderError):
    def __init__(self, message: str):
        super().__init__(message)
        self.category = ErrorCategory.SERVICE_UNAVAILABLE


class RequestTooLargeError(BaseAppError):
    """요청 페이로드가 공급자 한도를 초과(HTTP 413).

    429(쿼터)·403(권한)과는 원인이 다르다 — 키/계정은 정상이고 "이 요청이 이 공급자
    한도보다 크다"는 사실만 의미하므로, key_manager.report_failure 로 키를 쿨다운시키지
    않는다(실측: 이전엔 RateLimitError 로 뭉쳐 분류돼 키가 실패 처리 → Gemini 로 전환 →
    403 → 클라이언트 300초 타임아웃을 유발했다). resilience_manager 는 이 예외를 보면
    더 큰 한도의 공급자(Cerebras)로 즉시 넘기거나, 더 큰 한도 공급자도 없으면 바로
    클라이언트에 413 을 돌려준다."""

    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.PAYLOAD_TOO_LARGE)


class GatewayTimeoutError(BaseAppError):
    """게이트웨이 전체 요청 처리 시간이 settings.request_deadline_seconds 를 초과."""

    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.GATEWAY_TIMEOUT)


class PaymentRequiredError(BaseAppError):
    """HTTP 402 Payment Required.

    실측(2026-10): Cerebras 키가 /models 는 정상 응답하면서 실제 chat completions 는
    402 "Payment required ... Visit your billing tab" 를 반환했다. 429(쿼터 소진,
    시간이 지나면 자연 회복)와 원인이 다르다 — 결제/빌링 설정 문제라 재시도해도 안
    풀린다. 403 과 동일하게 key_manager 가 해당 키를 영구 제외(permanently_excluded)
    하고, resilience_manager 는 같은 공급자의 다른 키도 시도하지 않고 즉시 다음
    공급자로 넘어가야 한다(3회 재시도 금지)."""

    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.PAYMENT_REQUIRED)


def parse_retry_after_seconds(error: Exception) -> float | None:
    """429 예외에서 "언제 다시 시도하면 될지"(초)를 best-effort 로 뽑아낸다.

    우선순위: 1) httpx 응답의 Retry-After 헤더(초 또는 HTTP-date 는 다루지 않음 —
    공급자들이 실무에서 숫자초만 보내는 걸 전제), 2) 메시지 본문의 "try again in Xs"/
    "retry after Xs" 류 안내 문구. 둘 다 없으면 None(모른다는 뜻 — 호출부가 계산값으로
    대체한다). Groq/Cerebras 모두 openai SDK(httpx 기반)를 쓰므로 어댑터 양쪽에서
    공용으로 쓴다."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        raw = None
        try:
            raw = headers.get("retry-after")
        except Exception:  # noqa: BLE001 - 헤더 접근 방식이 다른 타입이어도 안전하게
            raw = None
        if raw:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass

    msg = str(error)
    match = re.search(
        r"(?:try again in|retry[\s_-]?after)\s*:?\s*(\d+(?:\.\d+)?)\s*s",
        msg,
        re.IGNORECASE,
    )
    if match:
        try:
            return max(0.0, float(match.group(1)))
        except (TypeError, ValueError):
            pass
    return None
