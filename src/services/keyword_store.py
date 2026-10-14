"""웹 사용 판정을 보강하기 위한 언어별 키워드 저장소.

설계 요점:
- 파일 하나(`data/web_keywords.<lang>.json`) 에 언어별 단순 리스트 형태로 적재.
- append-only, 상한 초과 시 FIFO 로 오래된 키워드부터 제거.
- 쿼리에 키워드 하나라도 substring 매치되면 "웹 필요" 로 간주.
- 정규식 패턴(`data/web_patterns.<lang>.json`)은 키워드로 표현하기 어려운 검색 요청을
  세분화해 판정한다. include 패턴이 맞아도 exclude 패턴(검색이 구현·설명 대상인 문장 등)이
  맞으면 정규식 판정은 무효. 패턴도 소스가 아니라 JSON 이 원본이다.
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

_CompiledPatterns = list[tuple[str, re.Pattern[str]]]


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
        self._include_patterns: _CompiledPatterns = []
        self._exclude_patterns: _CompiledPatterns = []
        self.reload_patterns()

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

    def _patterns_path(self, lang: str) -> Path:
        return self._data_dir / f"web_patterns.{lang}.json"

    def _load_patterns(self, lang: str) -> tuple[_CompiledPatterns, _CompiledPatterns]:
        """언어별 패턴 파일을 읽어 (include, exclude) 컴파일 목록으로. 잘못된 정규식은 건너뛴다."""
        p = self._patterns_path(lang)
        if not p.exists():
            return [], []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"KeywordStore pattern load failed for {lang}: {exc}")
            return [], []
        if not isinstance(data, dict):
            return [], []

        def compile_group(group: str) -> _CompiledPatterns:
            compiled: _CompiledPatterns = []
            for idx, entry in enumerate(data.get(group) or []):
                if not isinstance(entry, dict) or not isinstance(entry.get("pattern"), str):
                    continue
                pid = f"{lang}:{entry.get('id') or f'{group}{idx}'}"
                try:
                    compiled.append((pid, re.compile(entry["pattern"], re.IGNORECASE)))
                except re.error as exc:
                    logger.warning(f"KeywordStore invalid pattern {pid}: {exc}")
            return compiled

        return compile_group("include"), compile_group("exclude")

    def reload_patterns(self) -> None:
        """패턴 파일을 다시 읽는다. 혼용 문장(한국어 + search)이 흔해 언어 구분 없이 모두 적용."""
        include: _CompiledPatterns = []
        exclude: _CompiledPatterns = []
        for lang in self._supported_langs:
            inc, exc = self._load_patterns(lang)
            include.extend(inc)
            exclude.extend(exc)
        with self._lock:
            self._include_patterns = include
            self._exclude_patterns = exclude
        if include or exclude:
            logger.info(
                f"KeywordStore patterns loaded: include={len(include)}, exclude={len(exclude)}"
            )

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
        """쿼리에 저장된 키워드가 substring 으로 포함되면 해당 키워드 반환.

        키워드가 없으면 정규식 패턴을 검사해 ``"regex:<lang>:<id>"`` 를 반환한다.
        """
        if not query:
            return None
        lang = self._norm_lang(lang or detect_language(query))
        low = query.lower()
        with self._lock:
            for kw in self._cache.get(lang, []):
                if kw.lower() in low:
                    return kw
        return self.match_pattern(query)

    def match_pattern(self, query: str) -> str | None:
        """include 패턴이 맞고 exclude 패턴이 하나도 안 맞으면 ``"regex:<id>"`` 반환."""
        if not query:
            return None
        with self._lock:
            include = list(self._include_patterns)
            exclude = list(self._exclude_patterns)
        hit = next((pid for pid, rx in include if rx.search(query)), None)
        if hit is None:
            return None
        veto = next((pid for pid, rx in exclude if rx.search(query)), None)
        if veto is not None:
            logger.debug(f"Intent regex {hit} vetoed by {veto}: {query!r}")
            return None
        return f"regex:{hit}"
