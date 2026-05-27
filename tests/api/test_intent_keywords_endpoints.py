"""`/v1/admin/intent/keywords*` 엔드포인트 3종 계약 검증.

README.md Line 72-74 에 명시된 관리자 API 가 실제 구현되어 있는지,
KeywordStore / IntentClassifier DI 와 입력 검증이 기대대로 동작하는지 확인.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import main
from src.api.v1.dependencies import get_intent_classifier, get_keyword_store
from src.services.keyword_store import KeywordStore

pytestmark = pytest.mark.integration


class TestIntentKeywordsEndpoints:
    def setup_method(self):
        self.client = TestClient(main.app)
        # 파일 IO 를 건드리지 않도록 tmp 경로 기반 KeywordStore 를 주입
        self._tmp_dir = Path("./data_test_intent_keywords_api")
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self.keyword_store = KeywordStore(data_dir=self._tmp_dir)

        self.intent_classifier = MagicMock()
        self.intent_classifier.learn_from_missed_query = AsyncMock(
            return_value=["오늘 환율"]
        )

        main.app.dependency_overrides[get_keyword_store] = lambda: self.keyword_store
        main.app.dependency_overrides[get_intent_classifier] = (
            lambda: self.intent_classifier
        )

    def teardown_method(self):
        main.app.dependency_overrides.clear()
        # tmp 파일 정리
        for f in self._tmp_dir.glob("*.json"):
            f.unlink()
        try:
            self._tmp_dir.rmdir()
        except OSError:
            pass

    def test_list_keywords_empty_initially(self):
        resp = self.client.get("/v1/admin/intent/keywords")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert data.get("ko") == []
        assert data.get("en") == []

    def test_add_keywords_success_and_listed(self):
        resp = self.client.post(
            "/v1/admin/intent/keywords",
            json={"lang": "ko", "keywords": ["실시간 주가", "최신 환율"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["lang"] == "ko"
        assert body["added"] == ["실시간 주가", "최신 환율"]

        listed = self.client.get("/v1/admin/intent/keywords").json()
        assert listed["ko"] == ["실시간 주가", "최신 환율"]

    def test_add_keywords_deduplicates_case_insensitive(self):
        self.client.post(
            "/v1/admin/intent/keywords",
            json={"lang": "en", "keywords": ["Latest News"]},
        )
        resp = self.client.post(
            "/v1/admin/intent/keywords",
            json={"lang": "en", "keywords": ["latest news", "live price"]},
        )
        assert resp.status_code == 200
        assert resp.json()["added"] == ["live price"]

    def test_add_keywords_rejects_invalid_lang(self):
        resp = self.client.post(
            "/v1/admin/intent/keywords",
            json={"keywords": ["foo"]},
        )
        assert resp.status_code == 400

    def test_add_keywords_rejects_non_list_keywords(self):
        resp = self.client.post(
            "/v1/admin/intent/keywords",
            json={"lang": "ko", "keywords": "not-a-list"},
        )
        assert resp.status_code == 400

    def test_learn_invokes_intent_classifier(self):
        resp = self.client.post(
            "/v1/admin/intent/keywords/learn",
            json={"query": "오늘 원/달러 환율 어때?"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["query"] == "오늘 원/달러 환율 어때?"
        assert body["added"] == ["오늘 환율"]
        self.intent_classifier.learn_from_missed_query.assert_awaited_once_with(
            "오늘 원/달러 환율 어때?"
        )

    def test_learn_rejects_missing_query(self):
        resp = self.client.post("/v1/admin/intent/keywords/learn", json={})
        assert resp.status_code == 400

    def test_learn_rejects_empty_query(self):
        resp = self.client.post(
            "/v1/admin/intent/keywords/learn", json={"query": "   "}
        )
        assert resp.status_code == 400
