"""ULID 생성 유틸리티 테스트."""

import time

from src.utils.ulid import (
    generate_message_id,
    generate_part_id,
    generate_session_id,
    generate_ulid,
)


class TestULID:
    def test_generate_ulid_format(self) -> None:
        """ULID은 26자 문자열이어야 한다."""
        ulid = generate_ulid()
        assert len(ulid) == 26
        assert ulid.isalnum()

    def test_ulid_sortability(self) -> None:
        """시간 간격을 둔 ULID은 시간순 정렬되어야 한다."""
        id1 = generate_ulid()
        time.sleep(0.002)
        id2 = generate_ulid()
        assert id1 < id2

    def test_ulid_uniqueness(self) -> None:
        """100개 ULID은 모두 고유해야 한다."""
        ids = {generate_ulid() for _ in range(100)}
        assert len(ids) == 100

    def test_session_id_prefix(self) -> None:
        assert generate_session_id().startswith("ses_")

    def test_message_id_prefix(self) -> None:
        assert generate_message_id().startswith("msg_")

    def test_part_id_prefix(self) -> None:
        assert generate_part_id().startswith("prt_")

    def test_prefixed_id_lengths(self) -> None:
        """prefix + ULID = 4 + 26 = 30자."""
        assert len(generate_session_id()) == 30
        assert len(generate_message_id()) == 30
        assert len(generate_part_id()) == 30
