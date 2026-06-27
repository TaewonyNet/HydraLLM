from typing import cast

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from src.core.config import settings
from src.services.admin_service import AdminService
from src.services.gateway import Gateway
from src.services.installer import InstallerService
from src.services.intent_classifier import IntentClassifier
from src.services.key_manager import KeyManager
from src.services.keyword_store import KeywordStore

_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def get_gateway(request: Request) -> Gateway:
    """Get Gateway from app state."""
    return cast(Gateway, request.app.state.gateway)


async def get_key_manager(request: Request) -> KeyManager:
    """Get KeyManager from app state."""
    return cast(KeyManager, request.app.state.key_manager)


async def get_admin_service(request: Request) -> AdminService:
    return cast(AdminService, request.app.state.admin_service)


async def get_installer_service(request: Request) -> InstallerService:
    return cast(InstallerService, request.app.state.installer_service)


async def get_intent_classifier(request: Request) -> IntentClassifier:
    return cast(IntentClassifier, request.app.state.intent_classifier)


async def get_keyword_store(request: Request) -> KeywordStore:
    return cast(KeywordStore, request.app.state.keyword_store)


async def verify_admin_auth(
    api_key: str | None = Security(_admin_key_header),
) -> bool:
    """Admin 엔드포인트 접근 권한 검증.

    ADMIN_API_KEY 미설정 시: --debug(또는 DEBUG=true) 로컬 개발에서만 개방하고,
    그 외(운영 추정)에는 fail-closed 로 차단한다. host 가 0.0.0.0 으로 바인드되므로
    키 없이 개방하면 LAN 에 admin 표면(키 주입·설치·세션)이 노출되기 때문이다.
    """
    if settings.admin_api_key is None:
        if settings.debug:
            return True
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Admin API is disabled: set ADMIN_API_KEY to enable admin endpoints, "
                "or run with --debug for local development."
            ),
        )
    if not api_key or api_key != settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Admin-Key header",
        )
    return True


async def require_admin(
    api_key: str | None = Security(_admin_key_header),
) -> None:
    """Admin 엔드포인트 접근 시 X-Admin-Key 헤더를 검증한다.

    admin_api_key가 설정되지 않으면 인증을 건너뛴다 (개발 환경).
    """
    await verify_admin_auth(api_key)
