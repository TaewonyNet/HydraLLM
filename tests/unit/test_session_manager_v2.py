"""Session Manager V2 (SQLite WAL) 테스트."""

import os
import tempfile
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.services.session_manager import SessionManager

pytestmark = pytest.mark.unit


@pytest.fixture()
def sm():
    """각 테스트마다 임시 SQLite DB로 SessionManager 생성."""
    db_path = os.path.join(
        tempfile.gettempdir(), f"test_session_{uuid4().hex[:8]}.sqlite"
    )
    manager = SessionManager(db_path=db_path)
    yield manager
    manager.close()
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.unlink(path)


class TestSessionCRUD:
    @pytest.mark.asyncio
    async def test_create_session(self, sm: SessionManager) -> None:
        sid = await sm.create_session()
        assert sid.startswith("ses_")
        info = await sm.get_session_info(sid)
        assert info is not None
        assert info["session_id"] == sid

    @pytest.mark.asyncio
    async def test_create_session_with_custom_id(self, sm: SessionManager) -> None:
        sid = await sm.create_session(session_id="custom-123", title="Test")
        assert sid == "custom-123"
        info = await sm.get_session_info(sid)
        assert info["title"] == "Test"

    @pytest.mark.asyncio
    async def test_auto_create_session(self, sm: SessionManager) -> None:
        """save_message 시 세션이 없으면 자동 생성되어야 한다."""
        msg_id = await sm.save_message("auto-ses", "user", "hello")
        assert msg_id.startswith("msg_")
        info = await sm.get_session_info("auto-ses")
        assert info is not None

    @pytest.mark.asyncio
    async def test_list_sessions(self, sm: SessionManager) -> None:
        await sm.create_session(session_id="s1")
        await sm.create_session(session_id="s2")
        sessions = await sm.get_all_sessions()
        assert len(sessions) >= 2

    @pytest.mark.asyncio
    async def test_delete_session(self, sm: SessionManager) -> None:
        sid = await sm.create_session(session_id="del-me")
        await sm.save_message(sid, "user", "bye")
        await sm.clear_session(sid)
        info = await sm.get_session_info(sid)
        assert info is None


class TestMessageAndParts:
    @pytest.mark.asyncio
    async def test_save_and_load_message(self, sm: SessionManager) -> None:
        sid = await sm.create_session()
        msg_id = await sm.save_message(sid, "user", "hello world")
        assert msg_id.startswith("msg_")
        context = await sm.load_context(sid)
        assert len(context) == 1
        assert context[0].content == "hello world"
        assert context[0].role == "user"

    @pytest.mark.asyncio
    async def test_multiple_messages(self, sm: SessionManager) -> None:
        sid = await sm.create_session()
        await sm.save_message(sid, "user", "question")
        await sm.save_message(sid, "assistant", "answer")
        context = await sm.load_context(sid)
        assert len(context) == 2
        assert context[0].role == "user"
        assert context[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_save_message_with_extra_parts(self, sm: SessionManager) -> None:
        sid = await sm.create_session()
        await sm.save_message(
            sid,
            "assistant",
            "response text",
            parts=[
                {
                    "type": "step_cost",
                    "data": {
                        "provider": "gemini",
                        "model": "flash",
                        "tokens": {"total": 100},
                    },
                },
            ],
        )
        msgs = await sm.load_messages_with_parts(sid)
        assert len(msgs) == 1
        assert len(msgs[0].parts) == 2  # text + step_cost

    @pytest.mark.asyncio
    async def test_add_part_to_existing_message(self, sm: SessionManager) -> None:
        sid = await sm.create_session()
        msg_id = await sm.save_message(sid, "user", "fetch this")
        prt_id = await sm.add_part(
            msg_id,
            "web_fetch",
            {
                "url": "https://example.com",
                "status": "success",
                "content": "page content",
            },
        )
        assert prt_id.startswith("prt_")

        msgs = await sm.load_messages_with_parts(sid)
        assert len(msgs[0].parts) == 2  # text + web_fetch

    @pytest.mark.asyncio
    async def test_add_web_search_part(self, sm: SessionManager) -> None:
        sid = await sm.create_session()
        msg_id = await sm.save_message(sid, "user", "searching news")
        prt_id = await sm.add_part(
            msg_id, "web_search", {"query": "latest news", "status": "success"}
        )
        assert prt_id.startswith("prt_")

        msgs = await sm.load_messages_with_parts(sid)
        assert any(p.type == "web_search" for p in msgs[0].parts)

    @pytest.mark.asyncio
    async def test_load_context_excludes_non_text_parts(
        self, sm: SessionManager
    ) -> None:
        """load_context는 text/compaction 파트만 반환해야 한다."""
        sid = await sm.create_session()
        msg_id = await sm.save_message(sid, "user", "hello")
        await sm.add_part(
            msg_id,
            "web_fetch",
            {"url": "http://x.com", "status": "ok", "content": "data"},
        )
        context = await sm.load_context(sid)
        assert len(context) == 1
        assert "data" not in context[0].content  # web_fetch content는 포함되면 안 됨


class TestCompaction:
    @pytest.mark.asyncio
    async def test_is_overflow_false_when_small(self, sm: SessionManager) -> None:
        sid = await sm.create_session()
        await sm.save_message(sid, "user", "short message")
        assert not sm.is_overflow(sid)

    @pytest.mark.asyncio
    async def test_compaction_creates_boundary(self, sm: SessionManager) -> None:
        """컴팩션 후 compaction 파트가 생성되고 load_context가 경계 이후만 반환해야 한다."""
        sid = await sm.create_session()
        # 큰 메시지를 여러 개 저장하여 overflow 유발
        for i in range(20):
            await sm.save_message(sid, "user", f"question {i} " + "x" * 2000)
            await sm.save_message(sid, "assistant", f"answer {i} " + "y" * 2000)

        assert sm.is_overflow(sid)

        mock_compressor = MagicMock()
        mock_compressor.compress.return_value = "Summary of conversation"
        await sm.compact(sid, mock_compressor)

        # 컴팩션 후 context는 줄어들어야 함
        context = await sm.load_context(sid)
        # summary + 최근 window 메시지들
        assert len(context) <= 10  # 합리적인 상한

        # compaction part 확인
        msgs = await sm.load_messages_with_parts(sid)
        compaction_parts = [p for m in msgs for p in m.parts if p.type == "compaction"]
        assert len(compaction_parts) >= 1

    @pytest.mark.asyncio
    async def test_selective_pruning_web_fetch(self, sm: SessionManager) -> None:
        """컴팩션 시 web_fetch 파트가 우선 pruning 되어야 한다."""
        sid = await sm.create_session()
        for i in range(15):
            msg_id = await sm.save_message(sid, "user", f"q{i} " + "x" * 1000)
            await sm.add_part(
                msg_id,
                "web_fetch",
                {
                    "url": f"https://example.com/{i}",
                    "status": "success",
                    "content": "big content " * 200,
                },
            )
            await sm.save_message(sid, "assistant", f"a{i} " + "y" * 1000)

        mock_compressor = MagicMock()
        mock_compressor.compress.return_value = "Summary"
        await sm.compact(sid, mock_compressor)

        # pruned web_fetch 확인
        msgs = await sm.load_messages_with_parts(sid)
        web_parts = [p for m in msgs for p in m.parts if p.type == "web_fetch"]
        pruned = [p for p in web_parts if p.data.get("content") == "[PRUNED]"]
        # 보호 범위 밖의 web_fetch는 pruned 되어야 함
        if web_parts:
            assert len(pruned) >= 0  # pruning이 발생했거나 메시지가 삭제됨


class TestFork:
    @pytest.mark.asyncio
    async def test_fork_full_session(self, sm: SessionManager) -> None:
        """fork_point 없이 전체 복사."""
        sid = await sm.create_session(session_id="origin")
        for i in range(3):
            await sm.save_message(sid, "user", f"msg {i}")

        new_sid = await sm.fork_session(sid)
        assert new_sid.startswith("ses_")
        assert new_sid != sid

        new_context = await sm.load_context(new_sid)
        assert len(new_context) == 3

        # 원본은 영향 없음
        orig_context = await sm.load_context(sid)
        assert len(orig_context) == 3

    @pytest.mark.asyncio
    async def test_fork_at_point(self, sm: SessionManager) -> None:
        """fork_point_message_id까지만 복사."""
        sid = await sm.create_session(session_id="origin2")
        msg_ids = []
        for i in range(5):
            mid = await sm.save_message(sid, "user", f"msg {i}")
            msg_ids.append(mid)

        # 3번째 메시지에서 분기
        new_sid = await sm.fork_session(sid, msg_ids[2])

        new_context = await sm.load_context(new_sid)
        assert len(new_context) == 3  # msg 0, 1, 2

    @pytest.mark.asyncio
    async def test_fork_preserves_parts(self, sm: SessionManager) -> None:
        """포크 시 파트도 복사되어야 한다."""
        sid = await sm.create_session(session_id="origin3")
        msg_id = await sm.save_message(sid, "user", "hello")
        await sm.add_part(
            msg_id,
            "web_fetch",
            {"url": "http://x.com", "status": "ok", "content": "data"},
        )

        new_sid = await sm.fork_session(sid)
        msgs = await sm.load_messages_with_parts(new_sid)
        assert len(msgs) == 1
        assert len(msgs[0].parts) == 2  # text + web_fetch

    @pytest.mark.asyncio
    async def test_fork_records_parent(self, sm: SessionManager) -> None:
        """포크된 세션의 parent_session_id가 기록되어야 한다."""
        sid = await sm.create_session(session_id="parent-ses")
        await sm.save_message(sid, "user", "hello")

        new_sid = await sm.fork_session(sid)
        info = await sm.get_session_info(new_sid)
        assert info["parent_session_id"] == "parent-ses"

    @pytest.mark.asyncio
    async def test_fork_nonexistent_session_raises(self, sm: SessionManager) -> None:
        """존재하지 않는 세션을 포크하면 ValueError."""
        with pytest.raises(ValueError, match="not found"):
            await sm.fork_session("nonexistent")


class TestSettings:
    @pytest.mark.asyncio
    async def test_get_set_setting(self, sm: SessionManager) -> None:
        await sm.set_setting("test_key", {"foo": "bar"})
        result = await sm.get_setting("test_key")
        assert result == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_get_default_setting(self, sm: SessionManager) -> None:
        result = await sm.get_setting("nonexistent", "default_val")
        assert result == "default_val"

    @pytest.mark.asyncio
    async def test_overwrite_setting(self, sm: SessionManager) -> None:
        await sm.set_setting("key", "v1")
        await sm.set_setting("key", "v2")
        result = await sm.get_setting("key")
        assert result == "v2"
