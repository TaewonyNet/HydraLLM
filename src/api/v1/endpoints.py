import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from src.core.exceptions import (
    AuthenticationError,
    GatewayTimeoutError,
    InvalidRequestError,
    RateLimitError,
    RequestTooLargeError,
    RequestValidationError,
    ResourceExhaustedError,
    ServiceUnavailableError,
)
from src.domain.models import ChatRequest, ChatResponse
from src.services.admin_service import AdminService
from src.services.comm_logger import comm_log_buffer
from src.services.gateway import Gateway
from src.services.installer import InstallerService
from src.services.intent_classifier import IntentClassifier
from src.services.key_manager import KeyManager
from src.services.keyword_store import KeywordStore

from .dependencies import (
    get_admin_service,
    get_gateway,
    get_installer_service,
    get_intent_classifier,
    get_key_manager,
    get_keyword_store,
    verify_admin_auth,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def _stream_chat_response(response: ChatResponse) -> AsyncIterator[bytes]:
    # ChatResponse 를 OpenAI SSE (`chat.completion.chunk`) 포맷으로 분해 전송.
    base = response.model_dump()
    choices = base.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}
    content = message.get("content") or ""

    role_chunk = {
        "id": base["id"],
        "object": "chat.completion.chunk",
        "created": base["created"],
        "model": base["model"],
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n".encode()

    if content:
        delta_chunk = {
            "id": base["id"],
            "object": "chat.completion.chunk",
            "created": base["created"],
            "model": base["model"],
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": None}
            ],
        }
        yield f"data: {json.dumps(delta_chunk, ensure_ascii=False)}\n\n".encode()

    done_chunk = {
        "id": base["id"],
        "object": "chat.completion.chunk",
        "created": base["created"],
        "model": base["model"],
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": first.get("finish_reason") or "stop",
            }
        ],
    }
    yield f"data: {json.dumps(done_chunk, ensure_ascii=False)}\n\n".encode()
    yield b"data: [DONE]\n\n"


@router.post("/chat/completions", response_model=None)
async def chat_completion(
    request: ChatRequest,
    gateway: Gateway = Depends(get_gateway),
) -> ChatResponse | StreamingResponse:
    """Standard OpenAI-compatible chat completion endpoint."""
    try:
        result = await gateway.process_request(request)
    except (InvalidRequestError, RequestValidationError, ValueError) as e:
        logger.error(f"Chat completion error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except RateLimitError as e:
        # 429(레이트리밋)는 공급자 장애가 아니라 그 키의 한도 문제(실측: Groq 분당
        # 토큰 한도 — 같은 시각 직접 호출은 200). strict 모드라 resilience_manager 가
        # 교차 폴백 없이 재-raise 한 경우 여기로 온다. HTTP 429 + Retry-After 로
        # "언제 다시 시도하면 될지" 알려준다 — 공급자 값이 있으면 그 값, 없으면
        # 분당 한도 기준 계산값(60s)으로 대체(과거엔 이 except 가 없어 500 으로
        # 떨어졌다).
        logger.error(f"Chat completion error: {e}")
        retry_after = getattr(e, "retry_after", None)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(e),
            headers={"Retry-After": str(max(1, int(retry_after or 60)))},
        ) from e
    except ResourceExhaustedError as e:
        # Circuit OPEN 거부(strict) 등. retry_after 가 있으면(breaker 의 남은 복구
        # 시간) Retry-After 헤더로 전달 — 없으면(예: 키 고갈) 헤더 생략.
        logger.error(f"Chat completion error: {e}")
        retry_after = getattr(e, "retry_after", None)
        headers = {"Retry-After": str(max(1, int(retry_after)))} if retry_after else None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
            headers=headers,
        ) from e
    except ServiceUnavailableError as e:
        logger.error(f"Chat completion error: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from e
    except AuthenticationError as e:
        # 키 단위 영구 거부(예: Gemini "project has been denied access") — strict
        # 라우팅에서 교차 공급자 폴백 없이 재-raise 된 경우 여기로 온다. 공급자
        # 장애(503)가 아니라 그 키/계정 문제이므로 403으로 그대로 전달한다.
        logger.error(f"Chat completion error: {e}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        ) from e
    except RequestTooLargeError as e:
        # 413 은 키/계정 문제가 아니라 요청 크기 문제 — 더 큰 한도 공급자도 없으면
        # 명확한 413 을 바로 돌려준다(Gemini 폴백 → 403 → 타임아웃 재발 방지).
        logger.error(f"Chat completion error: {e}")
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(e),
        ) from e
    except GatewayTimeoutError as e:
        # 전체 요청 마감 시간(REQUEST_DEADLINE_SECONDS) 초과 — 클라이언트 무기한 대기 방지.
        logger.error(f"Chat completion error: {e}")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(e),
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        # 예상치 못한 예외의 원문(str(e))은 SDK/내부 엔드포인트 정보를 흘릴 수 있으므로
        # 클라이언트에는 일반 메시지만 노출하고, 상세는 서버 로그에만 남긴다(SPEC §4/§10).
        logger.error(f"Chat completion error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from e

    if request.stream:
        return StreamingResponse(
            _stream_chat_response(result),
            media_type="text/event-stream",
        )
    return result


@router.get("/models")
async def list_models(
    gateway: Gateway = Depends(get_gateway),
) -> dict[str, Any]:
    """List all supported and discovered models."""
    return {"object": "list", "data": gateway.get_supported_models()}


@router.get("/admin/stats")
async def get_stats(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """Get aggregate usage statistics."""
    return await admin_service.get_stats()


@router.get("/admin/dashboard")
async def get_dashboard(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """Get full dashboard data (stats + recent logs)."""
    return await admin_service.get_dashboard_data()


@router.get("/admin/status")
async def get_status(
    gateway: Gateway = Depends(get_gateway),
    key_manager: KeyManager = Depends(get_key_manager),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """Get real-time provider and agent status."""
    status_data = await gateway.get_status()
    status_data["key_statistics"] = key_manager.get_key_status()
    return status_data


@router.get("/admin/logs")
async def get_logs(
    limit: int = 50,
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> list[dict[str, Any]]:
    """Get recent system event logs."""
    return await admin_service.get_logs(limit=limit)


@router.post("/admin/refresh-models")
async def refresh_models(
    gateway: Gateway = Depends(get_gateway),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """Force discovery of models from all providers."""
    await gateway.discover_all_models()
    return {"status": "success"}


@router.post("/admin/probe")
async def probe_keys(
    active: bool = False,
    gateway: Gateway = Depends(get_gateway),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """키 probe. 기본은 실패 키 복구만. active=true 면 활성 키도 실측 probe 한다.

    active=true 는 외부 사용(다른 도구/키 공유)으로 인한 쿼터 소진까지 실측 반영하나,
    probe 자체가 무료 쿼터를 소모하므로 필요시에만 호출할 것.
    """
    await gateway.recover_failed_keys()
    if active:
        probe_result = await gateway.probe_active_keys()
        return {"status": "success", "active_probe": probe_result}
    return {"status": "success"}


@router.post("/admin/keys")
async def add_keys(
    provider: str = Body(...),
    keys: list[str] = Body(...),
    key_manager: KeyManager = Depends(get_key_manager),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """Add new API keys at runtime."""
    key_manager.add_keys(provider, keys)
    return {"status": "success", "added_count": len(keys)}


@router.get("/admin/sessions")
async def list_sessions(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> list[dict[str, Any]]:
    """List all persisted sessions."""
    return await admin_service.get_sessions()


@router.post("/admin/sessions/new")
async def create_session(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """Create a new session."""
    session_id = await admin_service.create_session()
    return {"status": "success", "session_id": session_id}


@router.delete("/admin/sessions/{session_id}")
async def delete_session(
    session_id: str,
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """Delete a specific session."""
    await admin_service.delete_session(session_id)
    return {"status": "success"}


@router.post("/admin/logs/clear")
async def clear_logs(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """시스템 로그 전체 초기화."""
    await admin_service.clear_logs()
    return {"status": "success"}


@router.post("/admin/sessions/cleanup")
async def cleanup_sessions(
    days: int | None = None,
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """오래된 세션 정리."""
    count = await admin_service.cleanup_sessions(days)
    return {"status": "success", "deleted_count": count}


@router.get("/admin/onboarding")
async def get_onboarding(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """UI 호환용 온보딩 상태 조회."""
    return await admin_service.get_onboarding_status()


@router.get("/admin/settings")
async def get_settings(
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """UI 호환용 설정 정보 조회."""
    return await admin_service.get_settings()


@router.put("/admin/settings")
async def update_settings(
    payload: dict[str, Any] = Body(...),
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """UI 일반 설정 저장 (locale, debug_comm_log 등)."""
    return await admin_service.update_settings(payload)


@router.post("/admin/onboarding")
async def save_onboarding(
    payload: dict[str, Any] = Body(...),
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """초기 온보딩 단계에서 선택한 모델 목록을 저장."""
    return await admin_service.save_onboarding(payload)


@router.get("/admin/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> list[dict[str, Any]]:
    """특정 세션의 메시지 이력 조회 (UI 세션 복원)."""
    return await admin_service.get_session_messages(session_id)


@router.post("/admin/sessions/import")
async def import_session(
    payload: dict[str, Any] = Body(...),
    admin_service: AdminService = Depends(get_admin_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
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
) -> dict[str, Any]:
    """opencode / openclaw 로컬 에이전트 설치 상태 조회."""
    return await installer.status_all()


@router.post("/admin/install/{tool}")
async def install_tool(
    tool: str,
    installer: InstallerService = Depends(get_installer_service),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """로컬 에이전트 설치 트리거."""
    if tool == "openclaw-mllm-auto":
        return await installer.install_openclaw_mllm_auto()
    return await installer.install(tool)


# ─── Debug Comm Log ───


@router.get("/admin/comm-logs")
async def get_comm_logs(
    limit: int = 200,
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """공급자 통신 디버그 로그(메모리 버퍼) 조회."""
    return {
        "enabled": comm_log_buffer.enabled,
        "entries": comm_log_buffer.snapshot(limit=limit),
    }


@router.delete("/admin/comm-logs")
async def clear_comm_logs(
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """공급자 통신 디버그 로그 초기화."""
    comm_log_buffer.clear()
    return {"status": "success"}


# ─── Web Intent Keyword Store ───


@router.get("/admin/intent/keywords")
async def list_intent_keywords(
    keyword_store: KeywordStore = Depends(get_keyword_store),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, list[str]]:
    """언어별 웹-인텐트 키워드 전체 조회."""
    return keyword_store.list_all()


@router.post("/admin/intent/keywords")
async def add_intent_keywords(
    payload: dict[str, Any] = Body(...),
    keyword_store: KeywordStore = Depends(get_keyword_store),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """언어별 웹-인텐트 키워드 수동 등록. body: {lang, keywords[]}."""
    lang = payload.get("lang")
    keywords = payload.get("keywords")
    if not isinstance(lang, str) or not lang.strip():
        raise HTTPException(status_code=400, detail="lang (string) is required")
    if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
        raise HTTPException(status_code=400, detail="keywords (list[str]) is required")
    added = await asyncio.to_thread(keyword_store.add, lang, keywords)
    return {"status": "success", "lang": lang, "added": added}


@router.post("/admin/intent/keywords/learn")
async def learn_intent_keywords(
    payload: dict[str, Any] = Body(...),
    intent_classifier: IntentClassifier = Depends(get_intent_classifier),
    authenticated: bool = Depends(verify_admin_auth),
) -> dict[str, Any]:
    """false-negative 쿼리에서 키워드를 추출해 저장. body: {query}."""
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="query (string) is required")
    added = await intent_classifier.learn_from_missed_query(query)
    return {"status": "success", "query": query, "added": added}
