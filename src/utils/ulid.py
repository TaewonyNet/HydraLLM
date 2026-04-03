"""ULID 생성 유틸리티. 외부 의존성 없이 stdlib만 사용."""

import os
import time

_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_time(timestamp_ms: int, length: int = 10) -> str:
    """타임스탬프를 Crockford Base32로 인코딩."""
    s = ""
    for _ in range(length):
        s = _ENCODING[timestamp_ms & 0x1F] + s
        timestamp_ms >>= 5
    return s


def _encode_random(length: int = 16) -> str:
    """랜덤 바이트를 Crockford Base32로 인코딩."""
    rand_bytes = os.urandom(length)
    s = ""
    for b in rand_bytes[:length]:
        s += _ENCODING[b & 0x1F]
    return s[:length]


def generate_ulid() -> str:
    """ULID 생성 (26자, 시간순 정렬 가능)."""
    timestamp_ms = int(time.time() * 1000)
    return _encode_time(timestamp_ms) + _encode_random()


def generate_session_id() -> str:
    """세션 ID 생성. ses_ + ULID."""
    return f"ses_{generate_ulid()}"


def generate_message_id() -> str:
    """메시지 ID 생성. msg_ + ULID."""
    return f"msg_{generate_ulid()}"


def generate_part_id() -> str:
    """파트 ID 생성. prt_ + ULID."""
    return f"prt_{generate_ulid()}"
