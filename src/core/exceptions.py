from enum import Enum


class ErrorCategory(Enum):
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    AUTH_FAILURE = "AUTH_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"


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
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.RESOURCE_EXHAUSTED)


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
    def __init__(self, message: str):
        super().__init__(message, ErrorCategory.RESOURCE_EXHAUSTED)


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
    def __init__(self, message: str):
        super().__init__(message)
        self.category = ErrorCategory.RESOURCE_EXHAUSTED


class ProviderServerError(ProviderError):
    def __init__(self, message: str):
        super().__init__(message)
        self.category = ErrorCategory.SERVICE_UNAVAILABLE
