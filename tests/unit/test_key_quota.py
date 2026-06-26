"""KeyManager 쿼터 모니터링(분/일 카운터·뷰) 회귀 테스트.

API 키로 정확한 GCP 잔량을 못 얻는 한계 하에, 자체 카운트가 정확히 증가하고
버킷 경계(분/일)에서 리셋되며 _quota_view 가 한도·리셋·쿨다운을 노출하는지 검증.
"""
import pytest

from src.domain.enums import ProviderType
from src.services import key_manager as km_mod
from src.services.key_manager import KeyManager

GEM = ProviderType.GEMINI
KEY = "AIzaTESTKEY00001"


def _km():
    km = KeyManager()
    km.add_keys("gemini", [KEY])
    return km


@pytest.mark.unit
def test_usage_counts_increment(monkeypatch):
    monkeypatch.setattr(km_mod.time, "time", lambda: 1_000_000.0)
    km = _km()
    for _ in range(3):
        km._record_quota_usage(GEM, KEY)
    view = km._quota_view(GEM, KEY, 1_000_000.0)
    assert view["minute_used"] == 3
    assert view["day_used"] == 3
    assert view["minute_limit"] >= 1 and view["day_limit"] >= 1


@pytest.mark.unit
def test_minute_bucket_resets_day_persists(monkeypatch):
    km = _km()
    monkeypatch.setattr(km_mod.time, "time", lambda: 1_000_000.0)
    km._record_quota_usage(GEM, KEY)
    km._record_quota_usage(GEM, KEY)
    # 같은 날, 다음 분(+60s)으로 이동해 기록 → 분당은 리셋, 일일은 누적 유지
    later = 1_000_060.0
    monkeypatch.setattr(km_mod.time, "time", lambda: later)
    km._record_quota_usage(GEM, KEY)
    v = km._quota_view(GEM, KEY, later)
    assert v["minute_used"] == 1  # 새 분 버킷
    assert v["day_used"] == 3     # 일일 누적 보존


@pytest.mark.unit
def test_quota_view_fields(monkeypatch):
    monkeypatch.setattr(km_mod.time, "time", lambda: 1_000_000.0)
    km = _km()
    v = km._quota_view(GEM, KEY, 1_000_000.0)
    for field in (
        "minute_used", "minute_limit", "day_used", "day_limit", "day_pct",
        "day_reset_in_sec", "minute_reset_in_sec", "cooldown_remaining_sec",
        "is_quota_limit", "is_forbidden", "last_429",
    ):
        assert field in v
    assert 0 <= v["minute_reset_in_sec"] <= 60
    assert v["day_reset_in_sec"] > 0
