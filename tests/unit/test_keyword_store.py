import json
from pathlib import Path

from src.services.keyword_store import KeywordStore, detect_language


def test_detect_language_hangul():
    assert detect_language("오늘 날씨 어때?") == "ko"


def test_detect_language_english():
    assert detect_language("what is the weather like today?") == "en"


def test_detect_language_empty_defaults_english():
    assert detect_language("") == "en"


def test_keyword_store_add_and_match(tmp_path: Path):
    store = KeywordStore(data_dir=tmp_path)
    added = store.add("ko", ["실시간 주가", "오늘 환율"])
    assert added == ["실시간 주가", "오늘 환율"]

    file = tmp_path / "web_keywords.ko.json"
    assert file.exists()
    assert json.loads(file.read_text()) == ["실시간 주가", "오늘 환율"]

    assert store.matches("현재 실시간 주가 알려줘") == "실시간 주가"
    assert store.matches("한국 역사 알려줘") is None


def test_keyword_store_dedup_case_insensitive(tmp_path: Path):
    store = KeywordStore(data_dir=tmp_path)
    store.add("en", ["today weather"])
    added = store.add("en", ["Today Weather", "latest release"])
    assert added == ["latest release"]
    assert store.get("en") == ["today weather", "latest release"]


def test_keyword_store_fifo_cap(tmp_path: Path):
    store = KeywordStore(data_dir=tmp_path, max_per_lang=3)
    store.add("en", ["a-alpha", "b-bravo", "c-charlie"])
    store.add("en", ["d-delta", "e-echo"])
    assert store.get("en") == ["c-charlie", "d-delta", "e-echo"]


def test_keyword_store_reload_from_disk(tmp_path: Path):
    store1 = KeywordStore(data_dir=tmp_path)
    store1.add("ko", ["최신 뉴스"])
    store2 = KeywordStore(data_dir=tmp_path)
    assert store2.get("ko") == ["최신 뉴스"]


def test_keyword_store_ignores_too_short_or_long(tmp_path: Path):
    store = KeywordStore(data_dir=tmp_path)
    long_kw = "x" * 61
    added = store.add("en", ["", "   ", "a", long_kw, "valid phrase"])
    assert added == ["valid phrase"]


def test_keyword_store_language_isolation(tmp_path: Path):
    store = KeywordStore(data_dir=tmp_path)
    store.add("ko", ["실시간 가격"])
    store.add("en", ["live price"])
    assert store.matches("live price now", lang="en") == "live price"
    assert store.matches("live price now", lang="ko") is None
