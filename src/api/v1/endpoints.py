import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse

from src.api.v1.dependencies import (
    get_admin_service,
    get_gateway,
    get_key_manager,
)
from src.core.exceptions import ResourceExhaustedError
from src.core.logging import request_id_ctx
from src.domain.models import ChatRequest
from src.services.admin_service import AdminService
from src.services.gateway import Gateway
from src.services.key_manager import KeyManager

router = APIRouter()
logger = logging.getLogger(__name__)


def _normalize_responses_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("input_text", "output_text", "text"):
                    parts.append(part.get("text", ""))
                elif part.get("type") in ("image_url", "image"):
                    return content
                else:
                    parts.append(part.get("text", str(part)))
            else:
                parts.append(str(part))
        return "\n".join(parts) if parts else ""
    return str(content)


def _convert_responses_input(input_val: Any) -> list[dict[str, Any]]:
    if isinstance(input_val, str):
        return [{"role": "user", "content": input_val}]
    if isinstance(input_val, list):
        messages = []
        for item in input_val:
            if isinstance(item, dict):
                role = item.get("role", "user")
                content = item.get("content")
                if content is not None:
                    messages.append(
                        {"role": role, "content": _normalize_responses_content(content)}
                    )
                elif item.get("type") == "message":
                    messages.append(
                        {
                            "role": role,
                            "content": _normalize_responses_content(
                                item.get("content", "")
                            ),
                        }
                    )
                elif "text" in item:
                    messages.append({"role": "user", "content": item["text"]})
                else:
                    messages.append({"role": "user", "content": str(item)})
            else:
                messages.append({"role": "user", "content": str(item)})
        return messages
    return [{"role": "user", "content": str(input_val)}]


async def _handle_chat_completion(request: ChatRequest, gateway: Gateway) -> Any:
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    request_id_ctx.set(request_id)

    try:
        if request.stream:

            async def generate_stream() -> AsyncIterator[str]:
                chunk_id = f"chatcmpl-{request_id}"
                ts_now = int(time.time())
                role_chunk = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": ts_now,
                    "model": request.model or "auto",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant"},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"

                try:
                    response = await gateway.process_request(request, endpoint="chat")

                    if not response or not response.choices:
                        yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': '[No response from model]'}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    for choice in response.choices:
                        content = (
                            str(choice.message.content)
                            if choice.message.content is not None
                            else "[Empty]"
                        )
                        text_chunk = {
                            "id": response.id,
                            "object": "chat.completion.chunk",
                            "created": response.created,
                            "model": response.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": content},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                        yield f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n"

                    yield "data: [DONE]\n\n"
                except Exception as inner_e:
                    logger.error(f"Stream error: {inner_e}")
                    yield "data: [DONE]\n\n"

            return StreamingResponse(
                generate_stream(),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
            )

        response = await gateway.process_request(request, endpoint="chat")
        if response is None:
            raise HTTPException(status_code=500, detail="No response from gateway")
        return json.loads(response.model_dump_json())

    except Exception as e:
        logger.error(f"Error in LLM processing: {e}")
        if isinstance(e, ValueError):
            raise HTTPException(status_code=400, detail=str(e))
        if isinstance(e, ResourceExhaustedError):
            raise HTTPException(status_code=503, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/completions", response_model=None)
async def chat_completion(
    request: ChatRequest, gateway: Gateway = Depends(get_gateway)
) -> Any:
    return await _handle_chat_completion(request, gateway)


@router.get("/admin/sessions")
async def list_sessions(admin_service: AdminService = Depends(get_admin_service)):
    try:
        return await admin_service.get_sessions()
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/sessions/new")
async def create_session(admin_service: AdminService = Depends(get_admin_service)):
    try:
        session_id = await admin_service.create_session()
        return {"session_id": session_id}
    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/logs")
async def get_logs(
    limit: int = 50, admin_service: AdminService = Depends(get_admin_service)
):
    try:
        return await admin_service.get_logs(limit=limit)
    except Exception as e:
        logger.error(f"Failed to get logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/stats")
async def get_stats(admin_service: AdminService = Depends(get_admin_service)):
    try:
        return await admin_service.get_stats()
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/dashboard")
async def get_dashboard(admin_service: AdminService = Depends(get_admin_service)):
    try:
        return await admin_service.get_dashboard_data()
    except Exception as e:
        logger.error(f"Failed to get dashboard: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/status")
async def get_system_status(gateway: Gateway = Depends(get_gateway)):
    try:
        status_data = await gateway.get_status()
        return {
            "status": status_data,
            "key_statistics": gateway.key_manager.get_key_status(),
        }
    except Exception as e:
        logger.error(f"Failed to get system status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/refresh-models")
async def refresh_models(gateway: Gateway = Depends(get_gateway)):
    try:
        await gateway.discover_all_models()
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Failed to refresh models: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/models")
async def list_models(gateway: Gateway = Depends(get_gateway)):
    try:
        models = gateway.get_all_models()
        return {"data": models}
    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/probe")
async def probe_keys(gateway: Gateway = Depends(get_gateway)):
    try:
        await gateway.probe_all_keys()
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Failed to probe keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/keys")
async def add_keys(
    provider: str,
    keys: list[str] = Body(...),
    key_manager: KeyManager = Depends(get_key_manager),
):
    try:
        key_manager.add_keys(provider, keys)
        return {"status": "success", "count": len(keys)}
    except Exception as e:
        logger.error(f"Failed to add keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/onboarding")
async def save_onboarding(
    data: dict = Body(...), gateway: Gateway = Depends(get_gateway)
):
    try:
        from src.core.config import settings

        enabled_models = data.get("enabled_models", [])
        settings.onboarding_completed = True
        settings.enabled_models = enabled_models
        await gateway.session_manager.set_setting("onboarding_completed", True)
        await gateway.session_manager.set_setting(
            "enabled_models", json.dumps(enabled_models)
        )
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Failed to save onboarding: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/admin/sessions/{session_id}")
async def delete_session(
    session_id: str, admin_service: AdminService = Depends(get_admin_service)
):
    try:
        await admin_service.delete_session(session_id)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Failed to delete session: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/onboarding")
async def get_onboarding_status(key_manager: KeyManager = Depends(get_key_manager)):
    from src.core.config import settings

    all_models = await key_manager.get_all_supported_models()
    return {"completed": settings.onboarding_completed, "available_models": all_models}
