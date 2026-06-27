"""Admin 인증 fail-closed 회귀 가드.

ADMIN_API_KEY 미설정 시 --debug 가 아니면 admin 표면을 차단(503)하고, --debug 에서만
개방한다. 키 설정 시에는 X-Admin-Key 헤더를 검증한다. (api/v1/dependencies.py)
"""
import pytest
from fastapi import HTTPException

from src.api.v1.dependencies import verify_admin_auth
from src.core.config import settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_settings():
    saved = (settings.admin_api_key, settings.debug)
    yield
    settings.admin_api_key, settings.debug = saved


@pytest.mark.asyncio
async def test_unset_key_non_debug_blocks_503():
    settings.admin_api_key = None
    settings.debug = False
    with pytest.raises(HTTPException) as exc:
        await verify_admin_auth(None)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_unset_key_debug_opens():
    settings.admin_api_key = None
    settings.debug = True
    assert await verify_admin_auth(None) is True


@pytest.mark.asyncio
async def test_set_key_wrong_header_401():
    settings.admin_api_key = "secret"
    settings.debug = False
    with pytest.raises(HTTPException) as exc:
        await verify_admin_auth("wrong")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_set_key_correct_header_passes():
    settings.admin_api_key = "secret"
    settings.debug = False
    assert await verify_admin_auth("secret") is True
