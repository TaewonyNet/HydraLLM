import hashlib
import logging
import os
import tempfile
from typing import Any

logger = logging.getLogger(__name__)


class ContextManager:
    def __init__(self) -> None:
        self._file_cache: dict[str, Any] = {}
        self._temp_files: list[str] = []

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
        self._temp_files.append(path)
        return path

    def get_cached_file(self, content_hash: str) -> Any | None:
        return self._file_cache.get(content_hash)

    def cache_file(self, content_hash: str, file_handle: Any) -> None:
        self._file_cache[content_hash] = file_handle

    def cleanup(self) -> None:
        for path in self._temp_files:
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except Exception as e:
                logger.warning(f"Failed to delete temp file {path}: {e}")
        self._temp_files.clear()
        self._file_cache.clear()
