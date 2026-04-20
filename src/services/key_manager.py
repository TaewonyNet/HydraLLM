import asyncio
import logging
import random
import time
from datetime import timedelta
from typing import Any

from src.core.exceptions import ResourceExhaustedError
from src.domain.enums import ProviderType, TierType
from src.domain.interfaces import IKeyManager


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
        error_msg = str(error).lower()
        is_quota_error = "quota" in error_msg or "billing" in error_msg
        is_forbidden = "403" in error_msg or "denied" in error_msg

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

            self.update_key_metadata(
                provider,
                api_key,
                {
                    "failed_at": time.time(),
                    "error": error_msg,
                    "is_quota_limit": is_quota_error,
                    "is_forbidden": is_forbidden,
                    "cooldown_until": time.time() + cooldown_seconds,
                },
            )

            log_level = logging.ERROR if is_forbidden else logging.WARNING
            self._logger.log(
                log_level,
                f"Key {api_key[:8]}... failed for provider {provider.value} (Forbidden: {is_forbidden}): {error_msg[:100]}",
            )

    def get_key_status(self) -> dict[ProviderType, dict[str, Any]]:
        """Get current key status for all providers."""
        status = {}
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

    async def get_all_supported_models(self) -> list[dict[str, Any]]:
        all_models = []
        for provider, keys in self._active_keys.items():
            if keys:
                if provider == ProviderType.GEMINI:
                    all_models.append(
                        {
                            "id": "gemini-1.5-flash",
                            "display_name": "Gemini 1.5 Flash",
                            "owned_by": "google",
                            "tier": "free",
                            "capabilities": {
                                "max_tokens": 1000000,
                                "multimodal": True,
                                "has_search": True,
                            },
                        }
                    )
                elif provider == ProviderType.GROQ:
                    all_models.append(
                        {
                            "id": "llama-3.3-70b-versatile",
                            "display_name": "Llama 3.3 70B",
                            "owned_by": "groq",
                            "tier": "standard",
                            "capabilities": {
                                "max_tokens": 8192,
                                "multimodal": False,
                                "has_search": False,
                            },
                        }
                    )
        return all_models

    def get_next_key_sync(self, provider: ProviderType) -> str:
        return asyncio.run(self.get_next_key(provider))
