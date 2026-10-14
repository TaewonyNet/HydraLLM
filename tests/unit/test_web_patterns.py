"""정규식 기반 검색 의도 판정(data/web_patterns.*.json) 회귀 가드. (services/keyword_store.py)

"검색/search" 가 검색 요청(동사형)일 때만 웹 필요로 보고, 검색이 구현·설명 대상인
문장(검색 기능, 이진 탐색, search engine 등)은 걸러내는지 확인한다.
"""
import json
import shutil
from pathlib import Path

import pytest

from src.services.keyword_store import KeywordStore

pytestmark = pytest.mark.unit

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture
def pattern_store(tmp_path: Path) -> KeywordStore:
    # 학습 키워드 영향 없이 패턴만 평가하도록 패턴 파일만 복사한다.
    for lang in ("ko", "en"):
        shutil.copy(_DATA_DIR / f"web_patterns.{lang}.json", tmp_path)
    return KeywordStore(data_dir=tmp_path)


@pytest.mark.parametrize(
    "query",
    [
        # ko:search_request — 검색해서 / 검색 좀 해줘 / 구글링해서
        "파이썬 3.15 새 기능 검색해서 정리해줘",
        "엔비디아 실적 검색 좀 해줘",
        "구글링해서 찾아줘",
        # ko:search_by_means / ko:web_source_lookup / ko:search_english_ko
        "검색으로 확인해줘",
        "인터넷에서 찾아봐 최신 버전",
        "React 19 변경점 search 해줘",
        # en:search_the_web / en:imperative_search / en:look_it_up / en:web_search_noun
        "search the web for python 3.15 release notes",
        "please search for the latest FastAPI version",
        "Can you look up today's exchange rate?",
        "look it up please",
        "do a web search on Rust 2026 edition",
    ],
)
def test_search_requests_detected(pattern_store: KeywordStore, query: str):
    assert pattern_store.match_pattern(query) is not None


@pytest.mark.parametrize(
    "query",
    [
        # include 패턴에 걸리지만 exclude 가 막아야 하는 문장(제외 규칙 실검증)
        "binary search 해줘",
        "이분 검색 해줘",
        "Search engine optimization tips please",
        "google search bar component 만들어줘",
        "please search algorithm 비교",
        # 명사형이라 include 자체가 걸리면 안 되는 문장
        "검색 기능 구현해줘",
        "implement a binary search in Rust",
    ],
)
def test_search_as_topic_not_detected(pattern_store: KeywordStore, query: str):
    assert pattern_store.match_pattern(query) is None


def test_matches_falls_back_to_regex_after_keywords(pattern_store: KeywordStore):
    pattern_store.add("ko", ["실시간 주가"])
    assert pattern_store.matches("실시간 주가 알려줘") == "실시간 주가"
    assert pattern_store.matches("주가 검색해서 알려줘") == "regex:ko:search_request"


def test_exclude_vetoes_and_invalid_pattern_skipped(tmp_path: Path):
    (tmp_path / "web_patterns.ko.json").write_text(
        json.dumps(
            {
                "include": [
                    {"id": "broken", "pattern": "(검색"},
                    {"id": "req", "pattern": "검색해"},
                ],
                "exclude": [{"id": "feat", "pattern": "기능"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = KeywordStore(data_dir=tmp_path)
    assert store.match_pattern("검색해줘") == "regex:ko:req"
    assert store.match_pattern("검색 기능 검색해줘") is None


def test_no_pattern_files_means_no_regex_layer(tmp_path: Path):
    assert KeywordStore(data_dir=tmp_path).match_pattern("검색해줘") is None
