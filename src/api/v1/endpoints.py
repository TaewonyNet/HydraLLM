import json
import logging
import traceback
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse

from src.api.v1.dependencies import (
    get_admin_service,
    get_gateway,
    get_key_manager,
    require_admin,
)
from src.core.exceptions import ResourceExhaustedError
from src.core.logging import request_id_ctx
from src.domain.models import ChatRequest
from src.domain.schemas import ModelCapabilities, ModelInfo, ModelListResponse
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
        messages: list[dict[str, Any]] = []
        for item in input_val:
            if isinstance(item, dict):
                role = item.get("role", "user")
                content = item.get("content")
                if content is not None:
                    messages.append(
                        {
                            "role": role,
                            "content": _normalize_responses_content(content),
                        }
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
                    messages.append(
                        {
                            "role": "user",
                            "content": item["text"],
                        }
                    )
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": str(item),
                        }
                    )
            else:
                messages.append({"role": "user", "content": str(item)})
        return messages

    return [{"role": "user", "content": str(input_val)}]


def _build_debug_request(request: ChatRequest) -> dict[str, Any]:
    """UI용 디버그: 실제 gateway에 전달되는 request 스냅샷."""
    msgs = []
    for m in request.messages or []:
        c = m.content
        if isinstance(c, str) and len(c) > 500:
            c = c[:500] + f"... ({len(m.content)} chars)"
        msgs.append({"role": m.role, "content": c})
    return {
        "model": request.model,
        "messages": msgs,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "has_search": request.has_search,
        "web_fetch": request.web_fetch,
        "auto_web_fetch": request.auto_web_fetch,
        "compress_context": request.compress_context,
        "session_id": request.session_id,
        "stream": request.stream,
    }


async def _handle_chat_completion(request: ChatRequest, gateway: Gateway) -> Any:
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    request_id_ctx.set(request_id)

    try:
        logger.info(
            f"Processing chat request: model={request.model}, stream={request.stream}"
        )

        # 디버그용: gateway 처리 전 원본 request 스냅샷
        debug_request_before = _build_debug_request(request)

        response = await gateway.process_request(request, endpoint="chat")

        # 디버그용: gateway 처리 후 실제 전달된 request (web_fetch 결과 포함)
        debug_request_after = _build_debug_request(request)

        if request.stream:

            async def generate() -> Any:
                try:
                    for i, choice in enumerate(response.choices):
                        chunk = {
                            "id": response.id,
                            "object": "chat.completion.chunk",
                            "created": response.created,
                            "model": response.model,
                            "choices": [
                                {
                                    "index": i,
                                    "delta": {"role": "assistant"},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                        content = str(choice.message.content)
                        chunk_size = 50
                        for j in range(0, len(content), chunk_size):
                            chunk = {
                                "id": response.id,
                                "object": "chat.completion.chunk",
                                "created": response.created,
                                "model": response.model,
                                "choices": [
                                    {
                                        "index": i,
                                        "delta": {
                                            "content": content[j : j + chunk_size]
                                        },
                                        "finish_reason": None,
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"

                        final_payload = {
                            "id": response.id,
                            "object": "chat.completion.chunk",
                            "created": response.created,
                            "model": response.model,
                            "choices": [
                                {"index": i, "delta": {}, "finish_reason": "stop"}
                            ],
                        }
                        if response.usage:
                            final_payload["usage"] = response.usage

                        yield f"data: {json.dumps(final_payload)}\n\n"

                    yield "data: [DONE]\n\n"
                except Exception as e:
                    logger.error(f"Error in stream generation: {e}")
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                    yield "data: [DONE]\n\n"

            return StreamingResponse(generate(), media_type="text/event-stream")

        try:
            resp_dict = json.loads(response.model_dump_json())
            resp_dict["_gateway_debug"] = {
                "request_before": debug_request_before,
                "request_after": debug_request_after,
            }
            return resp_dict
        except Exception as e:
            logger.error(f"Error serializing response: {e}")
            return {
                "id": response.id,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": response.choices[0].message.content,
                        }
                    }
                ],
                "usage": response.usage,
                "error": f"Serialization error: {str(e)}",
            }

            return resp_dict
        except Exception as e:
            logger.error(f"Error serializing response: {e}")
            # 최소한의 데이터만이라도 반환 시도
            return {
                "id": response.id,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": response.choices[0].message.content,
                        }
                    }
                ],
                "usage": response.usage,
                "error": f"Serialization error: {str(e)}",
            }

    except Exception as e:
        logger.error(f"Error in LLM processing: {e}")
        if not isinstance(e, ValueError | ResourceExhaustedError):
            logger.error(traceback.format_exc())

        if isinstance(e, ValueError):
            raise HTTPException(status_code=400, detail=str(e)) from e
        if isinstance(e, ResourceExhaustedError):
            raise HTTPException(status_code=503, detail=str(e)) from e

        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/chat/completions", response_model=None)
async def chat_completion(
    request: ChatRequest, gateway: Gateway = Depends(get_gateway)
) -> Any:
    return await _handle_chat_completion(request, gateway)


@router.post("/responses", response_model=None)
async def responses_alias(
    request_data: Any = Body(...), gateway: Gateway = Depends(get_gateway)
) -> Any:
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    request_id_ctx.set(request_id)

    if not isinstance(request_data, dict):
        request = {"input": request_data}
    else:
        request = request_data

    logger.info(f"Incoming /responses request. Body keys: {list(request.keys())}")

    if not request.get("messages") and request.get("input") is not None:
        input_val = request.pop("input")
        request["messages"] = _convert_responses_input(input_val)
        logger.info(f"Converted 'input' to {len(request['messages'])} messages")

    if not request.get("messages") and request.get("prompt"):
        prompt = request.pop("prompt")
        if isinstance(prompt, list):
            prompt = prompt[0]
        request["messages"] = [{"role": "user", "content": str(prompt)}]

    if request.get("messages"):
        for msg in request["messages"]:
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                msg["content"] = _normalize_responses_content(msg["content"])

    if "max_output_tokens" in request and not request.get("max_tokens"):
        request["max_tokens"] = request.pop("max_output_tokens")
    else:
        request.pop("max_output_tokens", None)

    for unsupported in ("prompt_cache_key", "store", "tools"):
        request.pop(unsupported, None)

    if not request.get("messages"):
        request["messages"] = [
            {"role": "user", "content": "Please provide a status update or help."}
        ]

    if not request.get("model"):
        request["model"] = "auto"

    try:
        model_name = request.get("model", "auto")
        if request.get("stream"):

            async def generate() -> Any:
                resp_id = f"resp_{uuid.uuid4().hex[:24]}"
                msg_id = f"msg_{uuid.uuid4().hex[:24]}"

                yield f"event: response.created\ndata: {json.dumps({'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': model_name, 'output': []})}\n\n"
                yield f"event: response.output_item.added\ndata: {json.dumps({'output_index': 0, 'item': {'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'in_progress', 'content': []}})}\n\n"
                yield f"event: response.content_part.added\ndata: {json.dumps({'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"

                try:
                    chat_request = ChatRequest(**request)
                    response = await gateway.process_request(
                        chat_request, endpoint="responses"
                    )
                    full_content = (
                        str(response.choices[0].message.content)
                        if response.choices
                        else ""
                    )

                    for j in range(0, len(full_content), 50):
                        delta = full_content[j : j + 50]
                        yield f"event: response.output_text.delta\ndata: {json.dumps({'output_index': 0, 'content_index': 0, 'delta': delta})}\n\n"

                    yield f"event: response.output_text.done\ndata: {json.dumps({'output_index': 0, 'content_index': 0, 'text': full_content})}\n\n"
                    yield f"event: response.content_part.done\ndata: {json.dumps({'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': full_content}})}\n\n"

                    done_item = {
                        "type": "message",
                        "id": msg_id,
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": full_content}],
                    }
                    yield f"event: response.output_item.done\ndata: {json.dumps({'output_index': 0, 'item': done_item})}\n\n"

                    raw_usage = response.usage or {}
                    completed = {
                        "id": resp_id,
                        "object": "response",
                        "status": "completed",
                        "model": response.model,
                        "usage": {
                            "input_tokens": raw_usage.get("prompt_tokens", 0),
                            "output_tokens": raw_usage.get("completion_tokens", 0),
                            "total_tokens": raw_usage.get("total_tokens", 0),
                            "gateway_provider": raw_usage.get("gateway_provider"),
                            "gateway_key_index": raw_usage.get("gateway_key_index"),
                        },
                        "output": [done_item],
                    }
                    yield f"event: response.done\ndata: {json.dumps(completed)}\n\n"
                except Exception as e:
                    logger.error(f"Error in Responses stream: {e}")
                    yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

            return StreamingResponse(generate(), media_type="text/event-stream")

        chat_request = ChatRequest(**request)
        return await _handle_chat_completion(chat_request, gateway)
    except Exception as e:
        logger.error(f"Error in responses_alias: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/completions", response_model=None)
async def legacy_completion(
    request: dict[str, Any] = Body(...), gateway: Gateway = Depends(get_gateway)
) -> Any:
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    request_id_ctx.set(request_id)

    if "prompt" in request and "messages" not in request:
        prompt = request.pop("prompt")
        if isinstance(prompt, list):
            prompt = prompt[0]
        request["messages"] = [{"role": "user", "content": str(prompt)}]

    if request.get("stream"):

        async def generate() -> Any:
            try:
                chat_request = ChatRequest(**request)
                response = await gateway.process_request(
                    chat_request, endpoint="legacy"
                )
                full_content = (
                    str(response.choices[0].message.content) if response.choices else ""
                )

                for j in range(0, len(full_content), 50):
                    chunk = {
                        "id": response.id,
                        "object": "text_completion",
                        "created": response.created,
                        "model": response.model,
                        "choices": [
                            {
                                "text": full_content[j : j + 50],
                                "index": 0,
                                "logprobs": None,
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"

                final_chunk = {
                    "id": response.id,
                    "object": "text_completion",
                    "created": response.created,
                    "model": response.model,
                    "choices": [
                        {
                            "text": "",
                            "index": 0,
                            "logprobs": None,
                            "finish_reason": "stop",
                        }
                    ],
                }
                if response.usage:
                    final_chunk["usage"] = response.usage

                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    chat_request = ChatRequest(**request)
    return await _handle_chat_completion(chat_request, gateway)


@router.get("/models", response_model=ModelListResponse)
async def list_models(gateway: Gateway = Depends(get_gateway)) -> ModelListResponse:
    models_data = gateway.get_supported_models()
    model_infos = []
    for m in models_data:
        model_infos.append(
            ModelInfo(
                id=m["id"],
                display_name=m.get("display_name"),
                owned_by=m["owned_by"],
                tier=m.get("tier", "standard"),
                description=m.get("description"),
                capabilities=ModelCapabilities(
                    max_tokens=m["capabilities"]["max_tokens"],
                    multimodal=m["capabilities"]["multimodal"],
                    has_search=m["capabilities"].get("has_search", False),
                    cost_per_token=m["capabilities"].get("cost_per_token"),
                ),
            )
        )
    return ModelListResponse(data=model_infos)


@router.get("/admin/status", response_model=dict[str, Any])
async def get_system_status(
    _: None = Depends(require_admin),
    gateway: Gateway = Depends(get_gateway),
) -> dict[str, Any]:
    status_data = await gateway.get_status()
    key_stats = gateway.key_manager.get_key_status()
    serializable_key_stats = {
        provider.value if hasattr(provider, "value") else str(provider): data
        for provider, data in key_stats.items()
    }
    return {"status": status_data, "key_statistics": serializable_key_stats}


@router.get("/admin/onboarding", response_model=dict[str, Any])
async def get_onboarding_status(
    _: None = Depends(require_admin),
    gateway: Gateway = Depends(get_gateway),
) -> dict[str, Any]:
    from src.core.config import settings

    models = gateway.get_all_models()
    if not models:
        models = gateway.analyzer._get_virtual_models()

    return {
        "completed": settings.onboarding_completed,
        "available_models": models,
    }


@router.post("/admin/onboarding", response_model=dict[str, Any])
async def complete_onboarding(
    selection: dict[str, Any],
    _: None = Depends(require_admin),
    gateway: Gateway = Depends(get_gateway),
) -> dict[str, Any]:
    from src.core.config import settings

    enabled_models = selection.get("enabled_models", [])
    settings.onboarding_completed = True
    settings.enabled_models = enabled_models
    await gateway.session_manager.set_setting("onboarding_completed", True)
    await gateway.session_manager.set_setting("enabled_models", enabled_models)
    return {"status": "success", "message": "Onboarding completed"}


@router.get("/admin/sessions", response_model=list[dict[str, Any]])
async def list_sessions(
    _: None = Depends(require_admin),
    gateway: Gateway = Depends(get_gateway),
) -> list[dict[str, Any]]:
    return await gateway.session_manager.get_all_sessions()


@router.post("/admin/sessions/new", response_model=dict[str, Any])
async def create_new_session(
    _: None = Depends(require_admin),
    gateway: Gateway = Depends(get_gateway),
) -> dict[str, Any]:
    session_id = await gateway.session_manager.create_session()
    return {"session_id": session_id}


@router.get("/admin/sessions/{session_id}", response_model=dict[str, Any])
async def get_session_detail(
    session_id: str,
    _: None = Depends(require_admin),
    gateway: Gateway = Depends(get_gateway),
) -> dict[str, Any]:
    """세션 상세 정보 (메시지 수, 토큰 추정치 등)."""
    info = await gateway.session_manager.get_session_info(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="Session not found")
    return info


@router.delete("/admin/sessions/{session_id}")
async def delete_session(
    session_id: str,
    _: None = Depends(require_admin),
    gateway: Gateway = Depends(get_gateway),
) -> dict[str, Any]:
    await gateway.session_manager.clear_session(session_id)
    return {"status": "success", "message": f"Session {session_id} deleted"}


@router.post("/admin/sessions/{session_id}/fork", response_model=dict[str, Any])
async def fork_session(
    session_id: str,
    body: dict[str, Any] = Body(default={}),
    _: None = Depends(require_admin),
    gateway: Gateway = Depends(get_gateway),
) -> dict[str, Any]:
    """세션 분기: 특정 메시지 지점까지 복사하여 독립 세션 생성."""
    message_id = body.get("message_id")
    new_session_id = await gateway.session_manager.fork_session(session_id, message_id)
    return {"session_id": new_session_id, "forked_from": session_id}


@router.post("/admin/keys", response_model=dict[str, Any])
async def add_runtime_keys(
    payload: dict[str, Any],
    _: None = Depends(require_admin),
    key_manager: KeyManager = Depends(get_key_manager),
) -> dict[str, Any]:
    """런타임에 API 키를 추가/갱신한다. {"provider": "gemini", "keys": ["key1", "key2"]}"""
    provider = payload.get("provider")
    keys = payload.get("keys", [])
    if not provider or not keys:
        raise HTTPException(
            status_code=400, detail="'provider' and 'keys' are required"
        )
    key_manager.add_keys(provider, keys)
    return {
        "status": "success",
        "message": f"Added {len(keys)} keys for provider {provider}",
    }


@router.post("/admin/probe", response_model=dict[str, Any])
async def force_probe_keys(
    _: None = Depends(require_admin),
    gateway: Gateway = Depends(get_gateway),
) -> dict[str, Any]:
    await gateway.probe_all_keys()
    return {"status": "success", "message": "Key probing triggered"}


@router.post("/admin/refresh-models", response_model=dict[str, Any])
async def refresh_models(gateway: Gateway = Depends(get_gateway)) -> dict[str, Any]:
    await gateway.discover_all_models()
    from src.core.config import settings

    return {
        "status": "success",
        "message": "Model discovery triggered",
        "current_defaults": {
            "free": settings.default_free_model,
            "premium": settings.default_premium_model,
        },
    }


@router.get("/admin/dashboard", response_model=dict[str, Any])
async def get_admin_dashboard(
    _: None = Depends(require_admin),
    admin_service: AdminService = Depends(get_admin_service),
) -> dict[str, Any]:
    data = await admin_service.get_dashboard_data()
    return data


@router.get("/admin/logs", response_model=list[dict[str, Any]])
async def get_system_logs(
    limit: int = 50,
    _: None = Depends(require_admin),
    admin_service: AdminService = Depends(get_admin_service),
) -> list[dict[str, Any]]:
    return await admin_service.session_manager.get_recent_logs(limit=limit)
