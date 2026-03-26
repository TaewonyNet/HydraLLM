class ProviderError(Exception):
    """Base exception for provider errors."""

    pass


class ResourceExhaustedError(Exception):
    """Raised when all resources are exhausted."""

    pass


class ConfigurationError(Exception):
    """Raised when configuration is invalid."""

    pass


class RoutingError(Exception):
    """Raised when routing fails."""

    pass


class AuthenticationError(Exception):
    """Raised when authentication fails."""

    pass


class RateLimitError(Exception):
    """Raised when rate limit is exceeded."""

    pass


class ModelNotFoundError(Exception):
    """Raised when requested model is not found."""

    pass


class RequestValidationError(Exception):
    """Raised when request validation fails."""

    pass


class ResponseFormatError(Exception):
    """Raised when response format is invalid."""

    pass


class ProviderConnectionError(Exception):
    """Raised when connection to provider fails."""

    pass


class ProviderTimeoutError(Exception):
    """Raised when request times out."""

    pass


class UnsupportedFeatureError(Exception):
    """Raised when requested feature is not supported."""

    pass


class ContentFilterError(Exception):
    """Raised when content filtering fails."""

    pass


class InvalidRequestError(Exception):
    """Raised when request is invalid."""

    pass


class ServiceUnavailableError(Exception):
    """Raised when service is unavailable."""

    pass


class ProviderRateLimitError(ProviderError):
    """Raised when a provider returns rate limit error."""

    pass


class ProviderServerError(ProviderError):
    """Raised when a provider returns server error."""

    pass
