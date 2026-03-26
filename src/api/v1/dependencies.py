from typing import cast

from fastapi import Request

from src.services.gateway import Gateway
from src.services.key_manager import KeyManager


async def get_gateway(request: Request) -> Gateway:
    """Get Gateway from app state."""
    return cast(Gateway, request.app.state.gateway)


async def get_key_manager(request: Request) -> KeyManager:
    """Get KeyManager from app state."""
    return cast(KeyManager, request.app.state.key_manager)
