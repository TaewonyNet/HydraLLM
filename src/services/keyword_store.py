"""웹 사용 판정을 보강하기 위한 언어별 키워드 저장소.

설계 요점:
- 파일 하나(`data/web_keywords.<lang>.json`) 에 언어별 단순 리스트 형태로 적재.
- append-only, 상한 초과 시 FIFO 로 오래된 키워드부터 제거.
- 쿼리에 키워드 하나라도 substring 매치되면 "웹 필요" 로 간주.
- 모든 사용자 쿼리가 아닌 오판정(웹 불요로 판정했으나 실제 웹 필요) 사례의 핵심어만 쌓도록
  호출자(검증/피드백 루프) 가 제어한다.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")
_DEFAULT_MAX_PER_LANG = 200
_SUPPORTED_LANGS: tuple[str, ...] = ("ko", "en")


def detect_language(text: str) -> str:
    """아주 가벼운 언어 감지. 한글 포함 시 'ko', 그 외 'en'."""
    if not text:
        return "en"
    return "ko" if _HANGUL_RE.search(text) else "en"


class KeywordStore:
    """언어별 웹-필요 키워드 JSON 파일 관리자."""

    def __init__(
        self,
        data_dir: Path,
        *,
        max_per_lang: int = _DEFAULT_MAX_PER_LANG,
        supported_langs: tuple[str, ...] = _SUPPORTED_LANGS,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._max_per_lang = max_per_lang
        self._supported_langs = supported_langs
        self._lock = threading.Lock()
        self._cache: dict[str, list[str]] = {lang: self._load(lang) for lang in supported_langs}

    # ------------------------------------------------------------------ paths
    def _path(self, lang: str) -> Path:
        return self._data_dir / f"web_keywords.{lang}.json"

    # ------------------------------------------------------------------ io
    def _load(self, lang: str) -> list[str]:
        p = self._path(lang)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"KeywordStore load failed for {lang}: {exc}")
            return []
        if isinstance(data, list):
            return [str(k) for k in data if isinstance(k, str) and k.strip()]
        return []

    def _flush(self, lang: str) -> None:
        p = self._path(lang)
        tmp = p.with_suffix(p.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(self._cache[lang], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(p)
        except OSError as exc:
            logger.warning(f"KeywordStore flush failed for {lang}: {exc}")

    # ------------------------------------------------------------------ api
    def _norm_lang(self, lang: str | None) -> str:
        if lang and lang in self._supported_langs:
            return lang
        return self._supported_langs[0] if lang == "ko" else "en"

    def list_all(self) -> dict[str, list[str]]:
        with self._lock:
            return {lang: list(items) for lang, items in self._cache.items()}

    def get(self, lang: str) -> list[str]:
        with self._lock:
            return list(self._cache.get(self._norm_lang(lang), []))

    def add(self, lang: str, keywords: list[str]) -> list[str]:
        """키워드 후보들을 정규화해 언어별 리스트에 append.

        반환값: 실제로 새로 추가된 키워드 목록.
        """
        lang = self._norm_lang(lang)
        added: list[str] = []
        with self._lock:
            current = self._cache.setdefault(lang, [])
            seen_lower = {k.lower() for k in current}
            for raw in keywords:
                kw = (raw or "").strip()
                if not kw:
                    continue
                if len(kw) < 2 or len(kw) > 60:
                    continue
                low = kw.lower()
                if low in seen_lower:
                    continue
                current.append(kw)
                seen_lower.add(low)
                added.append(kw)
            # FIFO 상한
            if len(current) > self._max_per_lang:
                drop = len(current) - self._max_per_lang
                del current[:drop]
            if added:
                self._flush(lang)
        return added

    def matches(self, query: str, lang: str | None = None) -> str | None:
        """쿼리에 저장된 키워드가 substring 으로 포함되면 해당 키워드 반환."""
        if not query:
            return None
        lang = self._norm_lang(lang or detect_language(query))
        low = query.lower()
        with self._lock:
            for kw in self._cache.get(lang, []):
                if kw.lower() in low:
                    return kw
        return None
