import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status

from src.domain.models import ChatRequest, ChatResponse
from src.services.admin_service import AdminService
from src.services.comm_logger import comm_log_buffer
from src.services.gateway import Gateway
from src.services.installer import InstallerService
from src.services.key_manager import KeyManager

from .dependencies import (
    get_admin_service,
    get_gateway,
    get_installer_service,
    get_key_manager,
    verify_admin_auth,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/chat/completions", response_model=ChatResponse)
async def chat_completion(
    request: ChatRequest,
    gateway: Gateway = Depends(get_gateway),
) -> ChatResponse:
    """Standard OpenAI-compatible chat completion endpoint."""
    try:
        return await gateway.process_request(request)
    except Exception as e:
        logger.error(f"Chat completion error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        ) from e


@router.get("/models")
async def list_models(
    gateway: Gateway = Depends(get_gateway),
):
    """List all supported and discovered models."""
    return {"object": "list", "data": gateway.get_supported_models()}


@router.get("/admin/stats")
async def get_stats(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """Get aggregate usage statistics."""
    return await admin_service.get_stats()


@router.get("/admin/dashboard")
async def get_dashboard(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """Get full dashboard data (stats + recent logs)."""
    return await admin_service.get_dashboard_data()


@router.get("/admin/status")
async def get_status(
    gateway: Gateway = Depends(get_gateway),
    key_manager: KeyManager = Depends(get_key_manager),
    authenticated: bool = Depends(verify_admin_auth),
):
    """Get real-time provider and agent status."""
    status_data = await gateway.get_status()
    # 개별 키의 상세 통계(인덱스, 티어 등) 포함
    status_data["key_statistics"] = key_manager.get_key_status()
    return status_data


@router.get("/admin/logs")
async def get_logs(
    limit: int = 50,
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """Get recent system event logs."""
    return await admin_service.get_logs(limit=limit)


@router.post("/admin/refresh-models")
async def refresh_models(
    gateway: Gateway = Depends(get_gateway),
    authenticated: bool = Depends(verify_admin_auth),
):
    """Force discovery of models from all providers."""
    await gateway.discover_all_models()
    return {"status": "success"}


@router.post("/admin/probe")
async def probe_keys(
    gateway: Gateway = Depends(get_gateway),
    authenticated: bool = Depends(verify_admin_auth),
):
    """Force probe of all API keys."""
    await gateway.recover_failed_keys()
    return {"status": "success"}


@router.post("/admin/keys")
async def add_keys(
    provider: str = Body(...),
    keys: list[str] = Body(...),
    key_manager: KeyManager = Depends(get_key_manager),
    authenticated: bool = Depends(verify_admin_auth),
):
    """Add new API keys at runtime."""
    key_manager.add_keys(provider, keys)
    return {"status": "success", "added_count": len(keys)}


@router.get("/admin/sessions")
async def list_sessions(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """List all persisted sessions."""
    return await admin_service.get_sessions()


@router.post("/admin/sessions/new")
async def create_session(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """Create a new session."""
    session_id = await admin_service.create_session()
    return {"status": "success", "session_id": session_id}


@router.delete("/admin/sessions/{session_id}")
async def delete_session(
    session_id: str,
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """Delete a specific session."""
    await admin_service.delete_session(session_id)
    return {"status": "success"}


@router.post("/admin/logs/clear")
async def clear_logs(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """시스템 로그 전체 초기화."""
    await admin_service.clear_logs()
    return {"status": "success"}


@router.post("/admin/sessions/cleanup")
async def cleanup_sessions(
    days: int | None = None,
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """오래된 세션 정리."""
    count = await admin_service.cleanup_sessions(days)
    return {"status": "success", "deleted_count": count}


@router.get("/admin/onboarding")
async def get_onboarding(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """UI 호환용 온보딩 상태 조회."""
    return await admin_service.get_onboarding_status()


@router.get("/admin/settings")
async def get_settings(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """UI 호환용 설정 정보 조회."""
    return await admin_service.get_settings()


@router.put("/admin/settings")
async def update_settings(
    payload: dict[str, Any] = Body(...),
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """UI 일반 설정 저장 (locale, debug_comm_log 등)."""
    return await admin_service.update_settings(payload)


@router.post("/admin/onboarding")
async def save_onboarding(
    payload: dict[str, Any] = Body(...),
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """초기 온보딩 단계에서 선택한 모델 목록을 저장."""
    return await admin_service.save_onboarding(payload)


@router.get("/admin/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """특정 세션의 메시지 이력 조회 (UI 세션 복원)."""
    return await admin_service.get_session_messages(session_id)


@router.post("/admin/sessions/import")
async def import_session(
    payload: dict[str, Any] = Body(...),
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """외부에서 전달된 session_id 의 메시지를 검증하고 반환."""
    sid = payload.get("session_id") or ""
    if not sid:
        raise HTTPException(status_code=400, detail="session_id required")
    info = await admin_service.get_session_info(sid)
    if info is None:
        raise HTTPException(status_code=404, detail="session not found")
    messages = await admin_service.get_session_messages(sid)
    return {"status": "success", "session_id": sid, "info": info, "messages": messages}


# ─── Local Agent Installer ───


@router.get("/admin/install/status")
async def installer_status(
    installer: InstallerService = Depends(get_installer_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """opencode / openclaw 로컬 에이전트 설치 상태 조회."""
    return await installer.status_all()


@router.post("/admin/install/{tool}")
async def install_tool(
    tool: str,
    installer: InstallerService = Depends(get_installer_service),
    authenticated: bool = Depends(verify_admin_auth),
):
    """로컬 에이전트 설치 트리거."""
    if tool == "openclaw-mllm-auto":
        return await installer.install_openclaw_mllm_auto()
    return await installer.install(tool)


# ─── Debug Comm Log ───


@router.get("/admin/comm-logs")
async def get_comm_logs(
    limit: int = 200,
    authenticated: bool = Depends(verify_admin_auth),
):
    """공급자 통신 디버그 로그(메모리 버퍼) 조회."""
    return {
        "enabled": comm_log_buffer.enabled,
        "entries": comm_log_buffer.snapshot(limit=limit),
    }


@router.delete("/admin/comm-logs")
async def clear_comm_logs(
    authenticated: bool = Depends(verify_admin_auth),
):
    """공급자 통신 디버그 로그 초기화."""
    comm_log_buffer.clear()
    return {"status": "success"}
