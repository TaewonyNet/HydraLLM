from typing import Any

from pydantic import BaseModel, Field

from src.domain.enums import AgentType, ProviderType


class ChatMessage(BaseModel):
    role: str = Field(
        ..., description="Role of the message sender (user, assistant, system)"
    )
    content: str | dict[str, Any] | list[Any] = Field(
        ..., description="Content of the message"
    )
    name: str | None = Field(None, description="Optional name of the sender")

    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "example": {"role": "user", "content": "Hello, how are you?"}
        },
    }


class ChatRequest(BaseModel):
    model: str | None = Field(None, description="Model name to use for this request")
    messages: list[ChatMessage] = Field(
        ..., description="List of messages in the conversation"
    )
    prompt: str | None = Field(None, description="Legacy prompt string")
    temperature: float | None = Field(0.7, description="Sampling temperature")
    max_tokens: int | None = Field(None, description="Maximum tokens to generate")
    top_p: float | None = Field(1.0, description="Nucleus sampling parameter")
    frequency_penalty: float | None = Field(0.0, description="Frequency penalty")
    presence_penalty: float | None = Field(0.0, description="Presence penalty")
    has_search: bool | None = Field(
        False, description="Whether to enable internet search"
    )
    stop: list[str] | None = Field(None, description="Stop sequences")
    stream: bool | None = Field(False, description="Whether to stream the response")
    session_id: str | None = Field(
        None, description="Optional session ID for continuity"
    )
    web_fetch: str | None = Field(
        None, description="Optional URL to fetch and include in context"
    )
    auto_web_fetch: bool | None = Field(
        None,
        description="Auto-detect URLs in prompt and fetch content (None=server default)",
    )
    compress_context: bool | None = Field(
        None,
        description="Compress session history with LLMLingua-2 (None=server default)",
    )
    tools: list[dict[str, Any]] | None = Field(
        None, description="List of tools available"
    )
    tool_choice: str | dict[str, Any] | None = Field(None, description="Tool choice")

    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "example": {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Hello!"}],
                "temperature": 0.7,
            }
        },
    }

    def estimate_token_count(self) -> int:
        """Estimate the total token count for this request."""
        if not self.messages:
            return 0
        prompt_tokens = sum(self._estimate_message_tokens(msg) for msg in self.messages)
        response_tokens = self.max_tokens if self.max_tokens else 0
        return prompt_tokens + response_tokens

    def _estimate_message_tokens(self, message: ChatMessage) -> int:
        """Estimate token count for a single message."""
        content = message.content
        if isinstance(content, dict) and content.get("type") == "image_url":
            return 1
        if isinstance(content, str):
            return max(1, len(content) // 4)
        return 1

    def has_images(self) -> bool:
        """Check if the request contains any image messages."""
        if not self.messages:
            return False
        for msg in self.messages:
            content = msg.content
            if isinstance(content, dict) and content.get("type") == "image_url":
                return True
            if isinstance(content, str):
                # Check if content contains image URL patterns
                if (
                    "image_url" in content
                    or content.startswith("http")
                    and any(
                        ext in content.lower()
                        for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]
                    )
                ):
                    return True
        return False


class ChatMessageChoice(BaseModel):
    index: int = Field(..., description="Index of the choice")
    message: ChatMessage = Field(..., description="Generated message")
    finish_reason: str | None = Field(None, description="Reason generation finished")
    content_filter_results: dict[str, Any] | None = Field(
        None, description="Content filter results"
    )

    model_config = {
        "extra": "allow",
    }


# Alias for backward compatibility
ChatChoice = ChatMessageChoice


class ChatResponse(BaseModel):
    id: str = Field(..., description="Unique identifier for this completion")  # noqa: A003
    object: str = Field("chat.completion", description="Object type")  # noqa: A003
    created: int = Field(..., description="Timestamp when the completion was created")
    model: str = Field(..., description="Model name that generated this completion")
    choices: list[ChatMessageChoice] = Field(
        ..., description="List of generated choices"
    )
    usage: dict[str, Any] | None = Field(
        None, description="Usage statistics and metadata"
    )

    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "example": {
                "id": "chatcmpl-123456789",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "llama-3.1-8b-instant",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Hello! How can I help you?",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
        },
    }


class RoutingDecision(BaseModel):
    model_config = {"protected_namespaces": ()}
    provider: ProviderType | None = Field(None, description="Selected provider type")
    agent: AgentType | None = Field(None, description="Selected agent type")
    model_name: str = Field(..., description="Selected model name")
    reason: str = Field(..., description="Reason for this routing decision")
    confidence: float | None = Field(
        None, description="Confidence score for this decision"
    )
    web_search_required: bool = Field(
        False, description="Whether web search/fetch is needed"
    )
