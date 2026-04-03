import logging
from typing import Any

from src.domain.interfaces import ISessionManager
from src.domain.models import ChatMessage, ChatRequest, ChatResponse
from src.services.compressor import ContextCompressor

logger = logging.getLogger(__name__)


class SessionOrchestrator:
    def __init__(self, session_manager: ISessionManager, compressor: ContextCompressor):
        self.session_manager = session_manager
        self.compressor = compressor

    async def load_history(self, request: ChatRequest) -> list[ChatMessage]:
        if not request.session_id:
            return []
        return await self.session_manager.load_context(request.session_id)

    async def save_user_message(self, request: ChatRequest) -> str | None:
        if not request.session_id:
            return None
        new_user_msgs = [m for m in request.messages if m.role == "user"]
        if new_user_msgs:
            last_user = new_user_msgs[-1]
            return await self.session_manager.save_message(
                request.session_id, last_user.role, last_user.content
            )
        return None

    async def save_assistant_response(
        self,
        request: ChatRequest,
        response: ChatResponse,
        extra_parts: list[dict[str, Any]] | None = None,
    ) -> None:
        if not (request.session_id and response and response.choices):
            return

        await self.session_manager.save_message(
            request.session_id,
            "assistant",
            response.choices[0].message.content,
            parts=extra_parts,
        )

        if self.session_manager.is_overflow(request.session_id):
            logger.info(f"Compacting session {request.session_id}")
            await self.session_manager.compact(request.session_id, self.compressor)
