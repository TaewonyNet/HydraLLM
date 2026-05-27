import time
import logging
from typing import Any
from src.domain.models import ChatMessage, ChatRequest
from src.services.observability import Observability

logger = logging.getLogger(__name__)

class WebEnrichmentCoordinator:
    def __init__(self, web_context_service, session_manager):
        self.web_context = web_context_service
        self.session_manager = session_manager

    async def enrich(self, request: ChatRequest, request_id: str) -> tuple[list[dict[str, Any]], str | None]:
        web_start = time.time()
        web_res = await self.web_context.enrich_request(request)
        web_parts, web_text = web_res if isinstance(web_res, tuple) else ([], None)
        web_latency = time.time() - web_start
        Observability.record_step("web_enrichment", web_latency)

        if web_text:
            await self.session_manager.log_system_event(
                level="INFO",
                category="WEB_ENRICH",
                message=f"Web context enriched for session {request.session_id} ({len(web_text)} chars)",
                metadata={"request_id": request_id, "latency": web_latency},
            )
            injection = ChatMessage(
                role="system",
                content=(
                    "--- REAL-TIME WEB CONTEXT START ---\n"
                    "The following information was retrieved from the web specifically for this turn. "
                    "Use this as the absolute source of truth for the final response.\n\n"
                    f"{web_text}\n"
                    "--- REAL-TIME WEB CONTEXT END ---"
                ),
                name="web_context",
            )
            request.messages.insert(-1, injection)
            logger.info(
                "Web context injected: %d chars into request.messages[-2] (session=%s)",
                len(web_text),
                request.session_id,
            )
        return web_parts, web_text
