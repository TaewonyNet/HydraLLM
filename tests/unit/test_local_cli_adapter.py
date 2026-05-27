import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from src.adapters.providers.local_cli import LocalCLIAdapter
from src.domain.models import ChatRequest, ChatMessage

@pytest.mark.asyncio
async def test_opencode_parsing_success():
    adapter = LocalCLIAdapter("opencode", "opencode")
    
    # Given: opencode output with JSON objects per line
    mock_stdout = (
        b'{"type":"text","part":{"text":"Hello"}}\n'
        b'{"type":"text","part":{"text":" World!"}}\n'
    )
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (mock_stdout, b'')
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc
        
        request = ChatRequest(messages=[ChatMessage(role="user", content="Hi")])
        response = await adapter.generate(request, "key")
        
        assert response.choices[0].message.content == "Hello World!"

@pytest.mark.asyncio
async def test_openclaw_parsing_success():
    adapter = LocalCLIAdapter("openclaw", "openclaw")
    
    # Given: openclaw output with a single response JSON
    mock_stdout = b'{"response":{"text":"I am OpenClaw"}}\n'
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (mock_stdout, b'')
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc
        
        request = ChatRequest(messages=[ChatMessage(role="user", content="Hi")])
        response = await adapter.generate(request, "key")
        
        assert response.choices[0].message.content == "I am OpenClaw"

@pytest.mark.asyncio
async def test_opencode_error_handling():
    adapter = LocalCLIAdapter("opencode", "opencode")
    
    # Given: opencode reports an internal API error in JSON
    mock_stdout = b'{"type":"error","error":{"data":{"message":"Invalid API key."}}}\n'
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (mock_stdout, b'')
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc
        
        from src.core.exceptions import ServiceUnavailableError
        with pytest.raises(ServiceUnavailableError) as excinfo:
            request = ChatRequest(messages=[ChatMessage(role="user", content="Hi")])
            await adapter.generate(request, "key")
        
        assert "Invalid API key." in str(excinfo.value)
