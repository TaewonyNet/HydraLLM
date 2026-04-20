import logging
import time
from collections import deque
from threading import Lock
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)


class CommLogBuffer:
    """디버그 모드에서 공급자 통신(요청/응답) 이벤트를 메모리 버퍼에 순환 저장."""

    def __init__(self, capacity: int = 500) -> None:
        self._capacity = capacity
        self._buffer: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "debug_comm_log", False))

    def record(self, direction: str, provider: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        entry = {
            "ts": time.time(),
            "direction": direction,
            "provider": provider,
            "payload": _truncate(payload),
        }
        with self._lock:
            self._buffer.append(entry)

    def snapshot(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            data = list(self._buffer)
        return data[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


def _truncate(obj: Any, max_chars: int = 4000) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= max_chars else obj[:max_chars] + "...<truncated>"
    if isinstance(obj, dict):
        return {k: _truncate(v, max_chars) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate(v, max_chars) for v in obj[:50]]
    return obj


comm_log_buffer = CommLogBuffer()
