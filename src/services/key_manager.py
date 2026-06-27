import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core.exceptions import ErrorCategory, ResourceExhaustedError
from src.domain.enums import ProviderType, TierType
from src.domain.interfaces import IKeyManager

# Gemini 무료 티어 일일 쿼터는 태평양시간(PT) 자정에 리셋된다. zoneinfo 가 없으면
# 표준시(PST, UTC-8)로 근사한다(서머타임 1시간 오차는 모니터링 용도상 허용).
try:
    from zoneinfo import ZoneInfo

    _PT_TZ: timezone | Any = ZoneInfo("America/Los_Angeles")
except Exception:  # noqa: BLE001 - zoneinfo/tzdata 부재 환경 폴백
    _PT_TZ = timezone(timedelta(hours=-8))


def _pt_day_key(now: float) -> str:
    return datetime.fromtimestamp(now, _PT_TZ).strftime("%Y-%m-%d")


def _pt_day_reset_remaining(now: float) -> int:
    """PT 자정(일일 쿼터 리셋)까지 남은 초."""
    dt = datetime.fromtimestamp(now, _PT_TZ)
    nxt = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((nxt - dt).total_seconds())


class KeyManager(IKeyManager):
    """
    Manages API key rotation and pool management for providers.
    """

    def __init__(self) -> None:
        self._key_pools: dict[ProviderType, list[str]] = {}
        self._active_keys: dict[ProviderType, list[str]] = {}
        self._failed_keys: dict[ProviderType, list[str]] = {}
        self._key_usage: dict[ProviderType, dict[str, int]] = {}
        self._key_metadata: dict[ProviderType, dict[str, dict[str, Any]]] = {}
        self._logger = logging.getLogger(__name__)
        self._lock = asyncio.Lock()  # Atomic lock for key pool manipulation
        self.max_failures = 3
        self.cooldown_period = timedelta(minutes=5)

    async def get_next_key(
        self, provider: ProviderType, min_tier: TierType = TierType.FREE
    ) -> str:
        async with self._lock:
            active_keys = self._active_keys.get(provider, [])

            if min_tier == TierType.PREMIUM:
                premium_candidates = [
                    k
                    for k in active_keys
                    if self._key_metadata.get(provider, {}).get(k, {}).get("tier")
                    in [TierType.PREMIUM, TierType.STANDARD, TierType.UNKNOWN]
                ]
                if premium_candidates:
                    active_keys = premium_candidates

            if not active_keys:
                msg = f"No available {min_tier} keys for provider {provider.value}"
                raise ResourceExhaustedError(msg)

            # Randomly select a key from the active pool
            selected_key = random.choice(active_keys)

            # Update usage count
            if provider not in self._key_usage:
                self._key_usage[provider] = {}
            if selected_key not in self._key_usage[provider]:
                self._key_usage[provider][selected_key] = 0
            self._key_usage[provider][selected_key] += 1

            # 분/일(PT) 슬라이딩 카운터 갱신 — 쿼터 모니터링 근사용.
            self._record_quota_usage(provider, selected_key)

            self._logger.info(
                f"Selected key index {self.get_key_index(provider, selected_key)} ({selected_key[:8]}...) for provider {provider.value}. Active keys remaining: {len(active_keys)}"
            )
            return selected_key

    async def report_success(self, provider: ProviderType, api_key: str) -> None:
        """
        Report a successful API call for the key.

        Args:
            provider: The provider type
            api_key: The API key that succeeded
        """
        # 활성/실패 풀 변경은 get_next_key 와 동일한 락으로 보호한다(원자성).
        async with self._lock:
            # Move key back to active pool if it was in failed pool
            if api_key in self._failed_keys.get(provider, []):
                self._failed_keys[provider].remove(api_key)
                if provider not in self._active_keys:
                    self._active_keys[provider] = []
                self._active_keys[provider].append(api_key)
                self._logger.info(
                    f"Key {api_key[:8]}... recovered for provider {provider.value}"
                )

    async def report_failure(
        self, provider: ProviderType, api_key: str, error: Exception
    ) -> None:
        # 어댑터가 정규화한 예외 카테고리를 우선 신뢰하고, 비정형 예외는 메시지
        # 휴리스틱으로 보강한다(타입이 누락돼도 안전하게 동작).
        category = getattr(error, "category", None)
        error_msg = str(error).lower()
        is_forbidden = (
            category == ErrorCategory.AUTH_FAILURE
            or "403" in error_msg
            or "denied" in error_msg
            or "forbidden" in error_msg
        )
        is_quota_error = not is_forbidden and (
            category == ErrorCategory.RESOURCE_EXHAUSTED
            or "quota" in error_msg
            or "billing" in error_msg
        )

        # 활성/실패 풀 변경은 get_next_key 와 동일한 락으로 보호한다(원자성).
        async with self._lock:
            if api_key in self._active_keys.get(provider, []):
                self._active_keys[provider].remove(api_key)
                if provider not in self._failed_keys:
                    self._failed_keys[provider] = []
                self._failed_keys[provider].append(api_key)

                if is_forbidden:
                    cooldown_seconds = 86400
                elif is_quota_error:
                    cooldown_seconds = 3600
                else:
                    cooldown_seconds = int(self.cooldown_period.total_seconds())

                meta_update: dict[str, Any] = {
                    "failed_at": time.time(),
                    "error": error_msg,
                    "is_quota_limit": is_quota_error,
                    "is_forbidden": is_forbidden,
                    "cooldown_until": time.time() + cooldown_seconds,
                }
                if is_quota_error:
                    # 429 응답에서 retryDelay(초) 와 quotaId 를 best-effort 파싱한다.
                    rd = re.search(r"retry[_-]?delay['\":\s]*?(\d+)", error_msg)
                    qid = re.search(r"quota(?:id|metric)['\":\s]*?([a-z0-9_-]{6,})", error_msg)
                    meta_update["last_429"] = {
                        "ts": time.time(),
                        "retry_delay": int(rd.group(1)) if rd else None,
                        "quota_id": qid.group(1) if qid else None,
                    }
                self.update_key_metadata(provider, api_key, meta_update)

                log_level = logging.ERROR if is_forbidden else logging.WARNING
                self._logger.log(
                    log_level,
                    f"Key {api_key[:8]}... failed for provider {provider.value} (Forbidden: {is_forbidden}): {error_msg[:100]}",
                )

    def get_active_keys(self, provider: ProviderType) -> list[str]:
        """활성 키 사본 반환(실측 probe 순회용)."""
        return list(self._active_keys.get(provider, []))

    def _record_quota_usage(self, provider: ProviderType, key: str) -> None:
        """선택된 키의 분/일(PT) 요청 카운터를 갱신한다(버킷 경계 넘으면 리셋)."""
        now = time.time()
        md = self._key_metadata.setdefault(provider, {}).setdefault(key, {})
        minute_key = int(now // 60)
        mb = md.get("minute_bucket")
        if not mb or mb[0] != minute_key:
            md["minute_bucket"] = [minute_key, 0]
        md["minute_bucket"][1] += 1
        day_key = _pt_day_key(now)
        db = md.get("day_bucket")
        if not db or db[0] != day_key:
            md["day_bucket"] = [day_key, 0]
        md["day_bucket"][1] += 1

    def _quota_view(self, provider: ProviderType, key: str, now: float) -> dict[str, Any]:
        """키별 쿼터 모니터링 뷰(분/일 사용량·쿨다운·마지막 429)를 직렬화한다."""
        md = self._key_metadata.get(provider, {}).get(key, {})
        mb = md.get("minute_bucket")
        db = md.get("day_bucket")
        minute_used = mb[1] if mb and mb[0] == int(now // 60) else 0
        day_used = db[1] if db and db[0] == _pt_day_key(now) else 0
        cu = md.get("cooldown_until")
        from src.core.config import settings

        rpm_limit = settings.quota_rpm_free
        rpd_limit = settings.quota_rpd_free
        return {
            "minute_used": minute_used,
            "minute_limit": rpm_limit,
            "day_used": day_used,
            "day_limit": rpd_limit,
            "day_pct": round(100 * day_used / rpd_limit, 1) if rpd_limit else 0,
            "day_reset_in_sec": _pt_day_reset_remaining(now),
            "minute_reset_in_sec": 60 - int(now % 60),
            "cooldown_remaining_sec": max(0, int(cu - now)) if cu else 0,
            "is_quota_limit": bool(md.get("is_quota_limit")),
            "is_forbidden": bool(md.get("is_forbidden")),
            "last_429": md.get("last_429"),
            # 실측 probe 결과(외부 사용 포함 현재 가용성). None=아직 probe 안 함.
            "last_probe": md.get("last_probe"),
        }

    def get_key_status(self) -> dict[ProviderType, dict[str, Any]]:
        """Get current key status for all providers."""
        status = {}
        now = time.time()
        for provider in ProviderType:
            active = self._active_keys.get(provider, [])
            failed = self._failed_keys.get(provider, [])
            pools = self._key_pools.get(provider, [])
            usage = self._key_usage.get(provider, {})

            # 관리자 응답에는 원본 API 키를 절대 포함하지 않는다.
            # usage 맵도 인덱스 기반으로 직렬화한다.
            status[provider] = {
                "total": len(pools),
                "active": len(active),
                "failed": len(failed),
                "keys": [
                    {
                        "index": i,
                        "id": k[:8] + "...",
                        "status": "active" if k in active else "failed",
                        "tier": self._get_tier_value(provider, k),
                        "usage": usage.get(k, 0),
                        "quota": self._quota_view(provider, k, now),
                    }
                    for i, k in enumerate(pools)
                ],
                "usage": {str(i): usage.get(k, 0) for i, k in enumerate(pools)},
            }

        return status

    def _get_tier_value(self, provider: ProviderType, key: str) -> str:
        """Get tier as string value for serialization with fallback logic."""
        metadata = self._key_metadata.get(provider, {}).get(key, {})
        tier = metadata.get("tier")

        if tier and isinstance(tier, TierType):
            return tier.value
        if tier:
            return str(tier)

        # Robust comparison using string value to avoid Enum instance mismatch
        p_val = provider.value if hasattr(provider, "value") else str(provider)
        if p_val == "gemini":
            return "free (estimated)"
        if p_val == "groq":
            return "standard (estimated)"

        return TierType.UNKNOWN.value

    def get_available_keys_count(self, provider: ProviderType) -> int:
        """Get count of available keys for a provider."""
        return len(self._active_keys.get(provider, []))

    def get_failed_keys_count(self, provider: ProviderType) -> int:
        """Get count of failed keys for a provider."""
        return len(self._failed_keys.get(provider, []))

    def reset_key_pool(self, provider: ProviderType) -> None:
        """Reset key pool for a provider."""
        if provider in self._key_pools:
            self._active_keys[provider] = self._key_pools[provider].copy()
            self._failed_keys[provider] = []
            self._key_usage[provider] = {key: 0 for key in self._key_pools[provider]}
            self._logger.info(f"Reset key pool for provider {provider.value}")

    def get_failed_keys(self, provider: ProviderType) -> list[str]:
        """Get list of failed keys for a provider."""
        return self._failed_keys.get(provider, []).copy()

    def add_keys(self, provider: ProviderType | str, keys: list[str]) -> None:
        """Add new keys to the pool for a provider."""
        if isinstance(provider, str):
            try:
                provider = ProviderType(provider.lower())
            except ValueError:
                self._logger.error(f"Invalid provider: {provider}")
                return

        if provider not in self._key_pools:
            self._key_pools[provider] = []
            self._active_keys[provider] = []
            self._failed_keys[provider] = []
            self._key_usage[provider] = {}
            self._key_metadata[provider] = {}

        for key in keys:
            if key not in self._key_pools[provider]:
                self._key_pools[provider].append(key)
                self._active_keys[provider].append(key)
                self._key_usage[provider][key] = 0
                self._key_metadata[provider][key] = {
                    "tier": TierType.UNKNOWN,
                    "last_probed": None,
                }

        self._logger.info(f"Added {len(keys)} keys for provider {provider.value}")

    def update_key_metadata(
        self, provider: ProviderType, api_key: str, metadata: dict[str, Any]
    ) -> None:
        """Update metadata for a specific key."""
        if provider in self._key_metadata and api_key in self._key_metadata[provider]:
            self._key_metadata[provider][api_key].update(metadata)

    def get_key_metadata(self, provider: ProviderType, api_key: str) -> dict[str, Any]:
        """Get metadata for a specific key."""
        return self._key_metadata.get(provider, {}).get(api_key, {})

    def get_key_index(self, provider: ProviderType, api_key: str) -> int:
        pool = self._key_pools.get(provider, [])
        try:
            return pool.index(api_key)
        except ValueError:
            return -1

