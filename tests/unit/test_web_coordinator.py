import pytest
from unittest.mock import AsyncMock, MagicMock
from src.services.web_coordinator import WebEnrichmentCoordinator
from src.domain.models import ChatRequest, ChatMessage

@pytest.mark.asyncio
async def test_web_coordinator_enrich_success():
    mock_web_service = AsyncMock()
    mock_session_manager = AsyncMock()
    
    mock_web_service.enrich_request.return_value = (
        [{"type": "web_search", "query": "test"}],
        "Found content about HydraLLM."
    )
    
    coordinator = WebEnrichmentCoordinator(mock_web_service, mock_session_manager)
    
    request = ChatRequest(messages=[ChatMessage(role="user", content="Tell me about HydraLLM")])
    request.session_id = "test-session"
    
    parts, text = await coordinator.enrich(request, "req-123")
    
    assert text == "Found content about HydraLLM."
    assert len(parts) == 1
    assert len(request.messages) == 2
    assert "REAL-TIME WEB CONTEXT" in request.messages[0].content

@pytest.mark.asyncio
async def test_web_coordinator_enrich_no_result():
    mock_web_service = AsyncMock()
    mock_session_manager = AsyncMock()
    
    mock_web_service.enrich_request.return_value = ([], None)
    
    coordinator = WebEnrichmentCoordinator(mock_web_service, mock_session_manager)
    
    request = ChatRequest(messages=[ChatMessage(role="user", content="Hi")])
    request.session_id = "test-session"
    
    parts, text = await coordinator.enrich(request, "req-456")
    
    assert text is None
    assert parts == []
    assert len(request.messages) == 1
