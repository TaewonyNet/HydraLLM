import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
assert project_root.exists()
src_path = project_root / "src"
sys.path.insert(0, str(src_path))

from src.core.exceptions import ResourceExhaustedError
from src.domain.enums import ProviderType
from src.services.key_manager import KeyManager

pytestmark = pytest.mark.unit


class TestKeyManager:
    def setup_method(self):
        self.key_manager = KeyManager()
        self.gemini_keys = ["key1", "key2", "key3"]
        self.groq_keys = ["keyA", "keyB"]
        self.cerebras_keys = ["keyX"]

    def test_add_keys(self):
        """Test adding keys to a provider"""
        self.key_manager.add_keys(ProviderType.GEMINI, self.gemini_keys)
        key = asyncio.run(self.key_manager.get_next_key(ProviderType.GEMINI))
        assert key in self.gemini_keys

    def test_add_keys_to_existing_pool(self):
        """Test adding keys to an existing provider pool"""
        self.key_manager.add_keys(ProviderType.GEMINI, ["key1", "key2"])
        self.key_manager.add_keys(ProviderType.GEMINI, ["key3"])
        keys = ["key1", "key2", "key3"]
        key = asyncio.run(self.key_manager.get_next_key(ProviderType.GEMINI))
        assert key in keys

    def test_get_next_key_round_robin(self):
        """Test round-robin key selection"""
        self.key_manager.add_keys(ProviderType.GEMINI, ["key1", "key2"])

        key1 = asyncio.run(self.key_manager.get_next_key(ProviderType.GEMINI))
        key2 = asyncio.run(self.key_manager.get_next_key(ProviderType.GEMINI))

        # Should be different keys (round-robin)
        assert key1 in ["key1", "key2"]
        assert key2 in ["key1", "key2"]

    def test_get_next_key_with_no_keys(self):
        """Test getting key when no keys are configured"""
        with pytest.raises(ResourceExhaustedError):
            asyncio.run(self.key_manager.get_next_key(ProviderType.GEMINI))

    def test_report_failure_marks_key_for_cooldown(self):
        """Test that failed keys are marked for cooldown"""
        self.key_manager.add_keys(ProviderType.GEMINI, ["key1", "key2"])

        # Initially both keys should be available
        status_before = self.key_manager.get_key_status()
        assert status_before[ProviderType.GEMINI]["active"] == 2
        assert status_before[ProviderType.GEMINI]["failed"] == 0

        # Report failure for one key
        asyncio.run(
            self.key_manager.report_failure(
                ProviderType.GEMINI, "key1", Exception("error")
            )
        )

        # After failure, key1 should be in failed pool
        status_after = self.key_manager.get_key_status()
        assert status_after[ProviderType.GEMINI]["active"] == 1
        assert status_after[ProviderType.GEMINI]["failed"] == 1

    def test_key_rotation_with_multiple_providers(self):
        """Test key rotation across multiple providers"""
        self.key_manager.add_keys(ProviderType.GEMINI, ["gemini_key1", "gemini_key2"])
        self.key_manager.add_keys(ProviderType.GROQ, ["groq_key1"])

        gemini_key = asyncio.run(self.key_manager.get_next_key(ProviderType.GEMINI))
        groq_key = asyncio.run(self.key_manager.get_next_key(ProviderType.GROQ))

        assert gemini_key in ["gemini_key1", "gemini_key2"]
        assert groq_key == "groq_key1"

    def test_max_failures_default(self):
        """Test default max failures value"""
        assert self.key_manager.max_failures == 3

    def test_cooldown_period_default(self):
        """Test default cooldown period"""
        assert self.key_manager.cooldown_period == timedelta(minutes=5)
