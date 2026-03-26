import asyncio
import logging
import random
from datetime import timedelta
from typing import Any

from src.core.exceptions import ResourceExhaustedError
from src.domain.enums import ProviderType
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
        self.max_failures = 3
        self.cooldown_period = timedelta(minutes=5)

    async def initialize_key_pools(
        self, key_config: dict[ProviderType, list[str]]
    ) -> None:
        """
        Initialize key pools from configuration.

        Args:
            key_config: Dictionary of provider to list of API keys
        """
        for provider, keys in key_config.items():
            if keys:
                self._key_pools[provider] = keys.copy()
                self._active_keys[provider] = keys.copy()
                self._failed_keys[provider] = []
                self._key_usage[provider] = {key: 0 for key in keys}
                self._logger.info(
                    f"Initialized {len(keys)} keys for provider {provider.value}"
                )
            else:
                self._logger.warning(f"No keys provided for provider {provider.value}")

    async def get_next_key(self, provider: ProviderType, min_tier: str = "free") -> str:
        active_keys = self._active_keys.get(provider, [])

        if min_tier == "premium":
            premium_candidates = [
                k
                for k in active_keys
                if self._key_metadata.get(provider, {}).get(k, {}).get("tier")
                in ["premium", "standard", "unknown"]
            ]
            if premium_candidates:
                active_keys = premium_candidates

        if not active_keys:
            msg = f"No available {min_tier} keys for provider {provider.value}"
            raise ResourceExhaustedError(msg)

        # Randomly select a key from the active pool
        selected_key = random.choice(active_keys)

        # Update usage count
        if selected_key not in self._key_usage[provider]:
            self._key_usage[provider][selected_key] = 0
        self._key_usage[provider][selected_key] += 1

        self._logger.info(
            f"Selected key index {active_keys.index(selected_key)} ({selected_key[:8]}...) for provider {provider.value}. Active keys remaining: {len(active_keys)}"
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
        """
        Report a failed API call for the key.

        Args:
            provider: The provider type
            api_key: The API key that failed
            error: The error that occurred
        """
        # Move key to failed pool
        if api_key in self._active_keys.get(provider, []):
            self._active_keys[provider].remove(api_key)
            if provider not in self._failed_keys:
                self._failed_keys[provider] = []
            self._failed_keys[provider].append(api_key)
            self._logger.warning(
                f"Key {api_key[:8]}... failed for provider {provider.value}: {str(error)}"
            )

    def get_key_status(self) -> dict[ProviderType, dict[str, Any]]:
        """Get current key status for all providers."""
        status = {}
        for provider in ProviderType:
            active = self._active_keys.get(provider, [])
            failed = self._failed_keys.get(provider, [])
            pools = self._key_pools.get(provider, [])
            usage = self._key_usage.get(provider, {})

            status[provider] = {
                "total": len(pools),
                "active": len(active),
                "failed": len(failed),
                "keys": [
                    {
                        "id": k[:8] + "...",
                        "status": "active" if k in active else "failed",
                        "tier": self._key_metadata.get(provider, {})
                        .get(k, {})
                        .get("tier", "unknown"),
                        "usage": usage.get(k, 0),
                    }
                    for k in pools
                ],
                "usage": usage,
            }

        return status

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
                    "tier": "unknown",
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

    def get_next_key_sync(self, provider: ProviderType) -> str:
        """Synchronous version of get_next_key for testing."""
        return asyncio.run(self.get_next_key(provider))
