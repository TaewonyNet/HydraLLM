from pydantic import BaseModel

from .enums import AgentType, ProviderType


class ProviderStatus(BaseModel):
    """
    Status information for a provider.
    """

    provider: ProviderType
    available_keys: int
    healthy: bool
    last_error: str | None
    total_requests: int
    successful_requests: int
    failed_requests: int
    average_response_time: float


class AgentStatus(BaseModel):
    """
    Status information for an agent.
    """

    agent: AgentType
    base_url: str
    healthy: bool
    last_error: str | None
    total_requests: int
    successful_requests: int
    failed_requests: int


class ModelCapabilities(BaseModel):
    """
    Capabilities and constraints of an LLM model.
    """

    max_tokens: int
    multimodal: bool
    has_search: bool = False
    cost_per_token: float | None = None


class ModelInfo(BaseModel):
    """
    Information about an LLM model.
    """

    id: str  # noqa: A003
    display_name: str | None = None
    object: str = "model"  # noqa: A003
    created: int = 1677610602  # Placeholder timestamp
    owned_by: str
    tier: str = "standard"  # free, standard, premium, experimental
    description: str | None = None
    capabilities: ModelCapabilities


class ModelListResponse(BaseModel):
    """
    Response model for listing models.
    """

    object: str = "list"  # noqa: A003
    data: list[ModelInfo]
