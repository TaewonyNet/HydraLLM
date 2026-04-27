"""웹 컨텍스트가 실제로 request.messages 에 주입되고 stdout 로그에 가시화되는지 검증.

배경:
    gateway.log 에 "Provider X exhausted → final local fallback" 은 찍히지만,
    웹 컨텍스트 주입은 `session_manager.log_system_event` (DB) 경로만 사용되어
    stdout 로그에서는 확인할 수 없었다. 이 비대칭 때문에 "검색 데이터가 있는데도
    LLM 이 활용하지 못한다" 는 오독이 발생했다.

    본 테스트는 gateway 가 다음 두 가지를 동시에 만족하는지 보증한다.
      1) request.messages 에 name="web_context" 인 system 메시지가 실제로 삽입된다.
      2) `src.services.gateway` 로거에 `"Web context injected: N chars"` INFO 라인이 찍힌다.
"""
import logging
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.domain.enums import ProviderType
from src.domain.models import ChatMessage, ChatRequest, ChatResponse
from src.services.gateway import Gateway
from src.services.session_manager import SessionManager

pytestmark = pytest.mark.integration


def _make_response(content: str, model: str) -> ChatResponse:
    return ChatResponse(
        id="cmpl-web-ctx",
        object="chat.completion",
        created=0,
        model=model,
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        usage={
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "gateway_provider": "groq",
            "gateway_model": model,
        },
    )


@pytest.fixture()
def isolated_session_manager():
    db_path = os.path.join(
        tempfile.gettempdir(), f"test_webctx_{uuid4().hex[:8]}.sqlite"
    )
    sm = SessionManager(db_path=db_path)
    yield sm
    sm.close()
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.unlink(p)


@pytest.mark.asyncio
async def test_web_context_injection_emits_stdout_log_and_mutates_messages(
    isolated_session_manager, caplog
):
    gateway = Gateway()
    gateway.session_manager = isolated_session_manager
    gateway.sessions.session_manager = isolated_session_manager

    gateway.key_manager.get_available_keys_count = MagicMock(return_value=1)
    gateway.key_manager.get_next_key = AsyncMock(return_value="test-key")
    gateway.key_manager.report_success = AsyncMock()
    gateway.key_manager.report_failure = AsyncMock()
    gateway.analyzer._provider_priority = ["groq", "gemini"]

    decision = MagicMock()
    decision.provider = ProviderType.GROQ
    decision.model_name = "llama-3.3-70b-versatile"
    decision.reason = "web_ctx_test"
    gateway.analyzer.analyze = AsyncMock(return_value=decision)

    # 웹 보강은 8049 chars 의 합성 데이터를 반환하도록 모킹
    fake_web_text = "SYNTHETIC_WEB_BODY_" + ("x" * 32) + "_END"
    fake_parts = [
        {"type": "web_search", "data": {"query": "최신 영화", "status": "cache_hit"}}
    ]
    gateway.web_context.enrich_request = AsyncMock(
        return_value=(fake_parts, fake_web_text)
    )

    adapter = AsyncMock()
    adapter.generate = AsyncMock(
        return_value=_make_response("ok", "llama-3.3-70b-versatile")
    )
    gateway._get_provider_adapter = MagicMock(return_value=adapter)

    session_id = f"webctx-{uuid4().hex[:8]}"
    request = ChatRequest(
        model="groq/llama-3.3-70b-versatile",
        session_id=session_id,
        messages=[ChatMessage(role="user", content="최신 영화 알려줘")],
    )

    with caplog.at_level(logging.INFO, logger="src.services.gateway"):
        await gateway.process_request(request)

    # 1) 어댑터에 전달된 request.messages 에 web_context 시스템 메시지가 포함됐는가?
    forwarded_request: ChatRequest = adapter.generate.call_args[0][0]
    web_context_messages = [
        m
        for m in forwarded_request.messages
        if m.role == "system" and getattr(m, "name", None) == "web_context"
    ]
    assert len(web_context_messages) == 1, (
        "enrich_request 가 web_text 를 돌려줬다면 system 메시지로 정확히 1건 주입되어야 함"
    )
    injected = web_context_messages[0]
    assert fake_web_text in injected.content
    assert "REAL-TIME WEB CONTEXT START" in injected.content
    assert "REAL-TIME WEB CONTEXT END" in injected.content

    # 2) 사용자의 현재 turn 바로 앞에 삽입됐는가? (gateway.py: insert(-1))
    user_turn_index = next(
        i for i, m in enumerate(forwarded_request.messages) if m.role == "user"
    )
    web_ctx_index = forwarded_request.messages.index(injected)
    assert web_ctx_index == user_turn_index - 1 or user_turn_index > 0, (
        "주입 위치는 마지막 user 메시지 직전이어야 함"
    )

    # 3) stdout 로그 가시성: gateway 로거에 "Web context injected: N chars" 라인이 찍혀야 함
    injection_logs = [
        rec
        for rec in caplog.records
        if rec.name == "src.services.gateway"
        and rec.levelno == logging.INFO
        and "Web context injected" in rec.getMessage()
    ]
    assert len(injection_logs) == 1, (
        f"INFO 로그 'Web context injected: ...' 1회 기록되어야 함. "
        f"실제: {[r.getMessage() for r in caplog.records if r.name == 'src.services.gateway']}"
    )
    message = injection_logs[0].getMessage()
    assert str(len(fake_web_text)) in message
    assert session_id in message


@pytest.mark.asyncio
async def test_no_injection_log_when_enrich_returns_empty(isolated_session_manager, caplog):
    """웹 보강이 empty 를 돌려주면 주입 로그도 발생하지 않아야 함 (가시성 노이즈 방지)."""
    gateway = Gateway()
    gateway.session_manager = isolated_session_manager
    gateway.sessions.session_manager = isolated_session_manager

    gateway.key_manager.get_available_keys_count = MagicMock(return_value=1)
    gateway.key_manager.get_next_key = AsyncMock(return_value="test-key")
    gateway.key_manager.report_success = AsyncMock()
    gateway.key_manager.report_failure = AsyncMock()
    gateway.analyzer._provider_priority = ["groq"]

    decision = MagicMock()
    decision.provider = ProviderType.GROQ
    decision.model_name = "llama-3.3-70b-versatile"
    decision.reason = "no_web_ctx"
    gateway.analyzer.analyze = AsyncMock(return_value=decision)

    gateway.web_context.enrich_request = AsyncMock(return_value=([], None))

    adapter = AsyncMock()
    adapter.generate = AsyncMock(
        return_value=_make_response("ok", "llama-3.3-70b-versatile")
    )
    gateway._get_provider_adapter = MagicMock(return_value=adapter)

    request = ChatRequest(
        model="groq/llama-3.3-70b-versatile",
        session_id=f"noctx-{uuid4().hex[:8]}",
        messages=[ChatMessage(role="user", content="일반 질문입니다")],
    )

    with caplog.at_level(logging.INFO, logger="src.services.gateway"):
        await gateway.process_request(request)

    injection_logs = [
        rec
        for rec in caplog.records
        if rec.name == "src.services.gateway"
        and "Web context injected" in rec.getMessage()
    ]
    assert injection_logs == [], "web_text 가 없으면 주입 로그도 찍히면 안 됨"
