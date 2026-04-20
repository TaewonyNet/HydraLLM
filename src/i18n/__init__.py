import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCALE_DIR = Path(__file__).parent
_cache: dict[str, dict[str, Any]] = {}
_current_locale: str = "ko"


def set_locale(locale: str) -> None:
    global _current_locale
    if locale not in ("ko", "en"):
        logger.warning(f"Unsupported locale '{locale}', falling back to 'ko'")
        locale = "ko"
    _current_locale = locale


def get_locale() -> str:
    return _current_locale


def _load(locale: str) -> dict[str, Any]:
    if locale in _cache:
        return _cache[locale]
    path = _LOCALE_DIR / f"{locale}.json"
    if not path.exists():
        logger.error(f"Locale file not found: {path}")
        return {}
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    _cache[locale] = data
    return data


def t(key: str, locale: str | None = None, **kwargs: Any) -> Any:
    loc = locale or _current_locale
    data = _load(loc)
    parts = key.split(".")
    current: Any = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return key
    if current is None:
        if loc != "ko":
            return t(key, locale="ko", **kwargs)
        return key
    if isinstance(current, str) and kwargs:
        return current.format(**kwargs)
    return current


def t_list(key: str, locale: str | None = None) -> list[str]:
    result = t(key, locale)
    if isinstance(result, list):
        return result
    return []


def t_patterns(key: str, locale: str | None = None) -> list[str]:
    return t_list(key, locale)
