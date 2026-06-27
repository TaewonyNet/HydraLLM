"""GeminiAdapter.find_working_model 자동 모델 탐색 회귀 테스트.

수동 검증(models.list 후보 → generateContent 실측 403/429/200 구분)을 코드화한
startup 자동 교체 로직의 핵심: 403/404 스킵, 200/429 채택, 우선순위 준수.
"""
import pytest

from src.adapters.providers.gemini import GeminiAdapter


def _adapter(discovered_ids):
    # __init__(genai.configure 등) 우회하고 probe 대상 목록만 주입.
    a = GeminiAdapter.__new__(GeminiAdapter)
    a._discovered_models = [{"id": i} for i in discovered_ids]
    return a


def _patch_genai(monkeypatch, behavior):
    """behavior: {model_name: "ok" | "raise:<errmsg>"} 로 generate 응답을 흉내낸다."""
    calls = []

    class FakeModel:
        def __init__(self, name):
            self.name = name

        async def generate_content_async(self, *args, **kwargs):
            calls.append(self.name)
            act = behavior.get(self.name, "ok")
            if act.startswith("raise:"):
                raise RuntimeError(act.split(":", 1)[1])
            return object()

    monkeypatch.setattr("src.adapters.providers.gemini.genai.GenerativeModel", FakeModel)
    monkeypatch.setattr(
        "src.adapters.providers.gemini.genai.configure", lambda **k: None
    )
    return calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skips_403_and_picks_next(monkeypatch):
    a = _adapter(["gemini-2.5-flash", "gemini-2.0-flash"])
    calls = _patch_genai(
        monkeypatch,
        {"gemini-2.5-flash": "raise:403 PERMISSION_DENIED", "gemini-2.0-flash": "ok"},
    )
    result = await a.find_working_model("key", "free")
    assert result == "gemini-2.0-flash"  # 403 스킵 후 다음 후보 채택
    assert calls == ["gemini-2.5-flash", "gemini-2.0-flash"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_accepts_429_as_valid(monkeypatch):
    a = _adapter(["gemini-2.5-flash", "gemini-2.0-flash"])
    calls = _patch_genai(
        monkeypatch, {"gemini-2.5-flash": "raise:429 RESOURCE_EXHAUSTED"}
    )
    result = await a.find_working_model("key", "free")
    assert result == "gemini-2.5-flash"  # 429=권한OK·쿼터소진 → 채택, 즉시 중단
    assert calls == ["gemini-2.5-flash"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_priority_order_first_success(monkeypatch):
    a = _adapter(["gemini-2.5-flash", "gemini-2.0-flash"])
    _patch_genai(monkeypatch, {})  # 전부 ok
    result = await a.find_working_model("key", "free")
    assert result == "gemini-2.5-flash"  # 우선순위 1순위 즉시 채택


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_denied_returns_none(monkeypatch):
    a = _adapter(["gemini-2.5-flash", "gemini-2.0-flash"])
    _patch_genai(
        monkeypatch,
        {
            "gemini-2.5-flash": "raise:403 PERMISSION_DENIED",
            "gemini-2.0-flash": "raise:404 not found",
        },
    )
    result = await a.find_working_model("key", "free")
    assert result is None  # 사용 가능 모델 없음 → None(기존 DEFAULT 유지)
