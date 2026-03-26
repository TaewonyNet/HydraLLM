from fastapi import Depends

from src.core.config import settings
from src.services.analyzer import ContextAnalyzer
from src.services.gateway import Gateway
from src.services.key_manager import KeyManager


def get_key_manager() -> KeyManager:
    """
    Dependency injection for KeyManager.

    Returns:
        KeyManager instance with configured API keys
    """
    key_manager = KeyManager()

    if settings.gemini_keys:
        keys = settings.gemini_keys
        if isinstance(keys, str):
            keys = keys.split(",")
        key_manager.add_keys("gemini", keys)

    if settings.groq_keys:
        keys = settings.groq_keys
        if isinstance(keys, str):
            keys = keys.split(",")
        key_manager.add_keys("groq", keys)

    if settings.cerebras_keys:
        keys = settings.cerebras_keys
        if isinstance(keys, str):
            keys = keys.split(",")
        key_manager.add_keys("cerebras", keys)

    return key_manager


def get_gateway(key_manager: KeyManager = Depends(get_key_manager)) -> Gateway:
    """
    Dependency injection for Gateway.

    Args:
        key_manager: KeyManager instance from dependency injection

    Returns:
        Gateway instance with configured analyzer and key manager
    """
    analyzer = ContextAnalyzer(max_tokens_fast_model=settings.max_tokens_fast_model)
    return Gateway(
        analyzer, key_manager, session_manager=None, scraper=None, compressor=None
    )


def get_analyzer() -> ContextAnalyzer:
    """
    Dependency injection for ContextAnalyzer.

    Returns:
        ContextAnalyzer instance with configured settings
    """
    return ContextAnalyzer(max_tokens_fast_model=settings.max_tokens_fast_model)
