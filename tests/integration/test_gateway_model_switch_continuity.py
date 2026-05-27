"""모델 변경 시 이전 대화 컨텍스트가 유지되는지 검증하는 통합 테스트.

시나리오:
    1. session_id=S로 Groq에 질의 → 답변이 세션에 저장됨.
    2. 같은 session_id=S로 model만 Gemini로 바꿔 재질의.
    3. Gemini 어댑터에 전달되는 request.messages에 1회차의 user/assistant 내용이
       포함되어야 한다 (= 이전 맥락 유지).
"""

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
        id="cmpl-test",
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
            "gateway_provider": "test",
            "gateway_model": model,
        },
    )


@pytest.fixture()
def isolated_session_manager():
    db_path = os.path.join(
        tempfile.gettempdir(), f"test_switch_{uuid4().hex[:8]}.sqlite"
    )
    sm = SessionManager(db_path=db_path)
    yield sm
    sm.close()
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.unlink(p)


@pytest.mark.asyncio
async def test_context_preserved_across_model_switch(isolated_session_manager):
    gateway = Gateway()
    gateway.session_manager = isolated_session_manager
    gateway.sessions.session_manager = isolated_session_manager

    gateway.key_manager.get_available_keys_count = MagicMock(return_value=1)
    gateway.key_manager.get_next_key = AsyncMock(return_value="test-key")
    gateway.key_manager.report_success = AsyncMock()
    gateway.key_manager.report_failure = AsyncMock()
    gateway.analyzer._provider_priority = ["groq", "gemini"]

    decision_groq = MagicMock()
    decision_groq.provider = ProviderType.GROQ
    decision_groq.model_name = "llama-3.3-70b-versatile"
    decision_groq.reason = "test"

    decision_gemini = MagicMock()
    decision_gemini.provider = ProviderType.GEMINI
    decision_gemini.model_name = "gemini-2.5-flash"
    decision_gemini.reason = "test"

    gateway.analyzer.analyze = AsyncMock(
        side_effect=[decision_groq, decision_gemini]
    )

    # URL 기반 웹 보강 비활성화
    gateway.web_context.enrich_request = AsyncMock(return_value=([], None))

    groq_adapter = AsyncMock()
    groq_adapter.generate = AsyncMock(
        return_value=_make_response("서울의 수도는 서울입니다.", "llama-3.3-70b-versatile")
    )

    gemini_adapter = AsyncMock()
    gemini_adapter.generate = AsyncMock(
        return_value=_make_response("이전 질문은 한국 수도에 관한 것이었습니다.", "gemini-2.5-flash")
    )

    def get_adapter(provider, key):
        if provider == ProviderType.GROQ:
            return groq_adapter
        if provider == ProviderType.GEMINI:
            return gemini_adapter
        return MagicMock()

    gateway._get_provider_adapter = MagicMock(side_effect=get_adapter)

    session_id = f"switch-{uuid4().hex[:8]}"

    first_request = ChatRequest(
        model="groq/llama-3.3-70b-versatile",
        session_id=session_id,
        messages=[ChatMessage(role="user", content="한국의 수도는 어디야?")],
    )
    first_response = await gateway.process_request(first_request)
    assert first_response.choices[0].message.content.startswith("서울")

    # 1회차 저장 확인
    stored = await isolated_session_manager.load_context(session_id)
    assert len(stored) == 2
    assert stored[0].role == "user"
    assert "한국의 수도" in stored[0].content
    assert stored[1].role == "assistant"

    # 2회차: 동일 session_id, model만 변경
    second_request = ChatRequest(
        model="gemini/gemini-2.5-flash",
        session_id=session_id,
        messages=[ChatMessage(role="user", content="내가 방금 뭘 물어봤지?")],
    )
    second_response = await gateway.process_request(second_request)
    assert second_response.choices[0].message.content

    # Gemini 어댑터가 받은 요청에 1회차 대화가 포함되어야 한다
    gemini_call_args, _ = gemini_adapter.generate.call_args
    forwarded_request: ChatRequest = gemini_call_args[0]
    forwarded_contents = [str(m.content) for m in forwarded_request.messages]
    joined = "\n".join(forwarded_contents)

    assert "한국의 수도" in joined, (
        "모델 변경 후에도 이전 user 질문이 컨텍스트에 유지되어야 함"
    )
    assert "서울" in joined, (
        "모델 변경 후에도 이전 assistant 응답이 컨텍스트에 유지되어야 함"
    )
    assert "내가 방금 뭘 물어봤지" in joined, "현재 턴의 user 질문도 포함되어야 함"

    user_turns = [m for m in forwarded_request.messages if m.role == "user"]
    assistant_turns = [m for m in forwarded_request.messages if m.role == "assistant"]
    assert len(user_turns) >= 2
    assert len(assistant_turns) >= 1
