import asyncio
import hashlib
import logging
import os
import tempfile
from typing import Any

from src.domain.interfaces import IContextManager

logger = logging.getLogger(__name__)


class ContextManager(IContextManager):
    """멀티모달 컨텍스트 오프로드/캐싱.

    앱 전역 단일 인스턴스로 공유되므로, 임시파일·캐시 상태를 **요청(asyncio 태스크)별로
    격리**한다. 그렇지 않으면 한 요청의 `cleanup()` 이 동시 진행 중인 다른 요청의 임시파일을
    삭제하고 캐시를 비우는 race 가 발생한다(N1).
    """

    def __init__(self) -> None:
        # 태스크 키 → 상태. 캐시 핸들은 업로드한 API 키에 묶이므로 요청별 분리가 더 정확하다.
        self._file_cache: dict[int, dict[str, Any]] = {}
        self._temp_files: dict[int, list[str]] = {}

    @staticmethod
    def _task_key() -> int:
        """현재 요청(코루틴 태스크) 식별자. 루프 밖이면 0(공유)."""
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        return id(task) if task is not None else 0

    def get_content_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def should_offload(self, content: str, threshold: int = 10000) -> bool:
        return len(content) > threshold

    def prepare_temp_file(self, content: str, suffix: str = ".txt") -> str:
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=suffix, delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(content)
            path = tmp.name
        self._temp_files.setdefault(self._task_key(), []).append(path)
        return path

    def get_cached_file(self, content_hash: str) -> Any | None:
        return self._file_cache.get(self._task_key(), {}).get(content_hash)

    def cache_file(self, content_hash: str, file_handle: Any) -> None:
        self._file_cache.setdefault(self._task_key(), {})[content_hash] = file_handle

    def cleanup(self) -> None:
        """현재 요청(태스크)이 만든 임시파일/캐시만 정리한다(다른 요청 미영향)."""
        key = self._task_key()
        for path in self._temp_files.pop(key, []):
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except Exception as e:
                logger.warning(f"Failed to delete temp file {path}: {e}")
        self._file_cache.pop(key, None)
