import asyncio
import json
import logging
import time
from typing import Any

from src.core.exceptions import ServiceUnavailableError
from src.domain.enums import ModelType
from src.domain.interfaces import ILLMProvider
from src.domain.models import ChatMessage, ChatMessageChoice, ChatRequest, ChatResponse

logger = logging.getLogger(__name__)


class LocalCLIAdapter(ILLMProvider):
    def __init__(self, binary_path: str, agent_type: str):
        self.binary_path = binary_path
        self.agent_type = agent_type
        logger.info(f"LocalCLIAdapter initialized for: {agent_type}")

    def get_supported_models(self) -> list[ModelType]:
        if self.agent_type == "opencode":
            return [ModelType.OPENCODE_MODEL]
        elif self.agent_type == "openclaw":
            return [ModelType.OPENCLAW_MODEL]
        return []

    def is_multimodal(self) -> bool:
        return False

    def get_max_tokens(self) -> int:
        return 32768

    async def generate(self, request: ChatRequest, api_key: str) -> ChatResponse:
        prompt = ""
        if request.messages:
            for msg in reversed(request.messages):
                if msg.role == "user":
                    prompt = str(msg.content)
                    break

        if not prompt:
            error_msg = "No user message found in request"
            raise ServiceUnavailableError(error_msg)

        try:
            model_flag = []
            if request.model:
                if self.agent_type == "opencode":
                    model_to_use = request.model
                    if "/" in model_to_use and model_to_use.startswith(
                        self.agent_type.upper()
                    ):
                        model_to_use = model_to_use.split("/", 1)[1]

                    if model_to_use == "opencode":
                        model_to_use = "github-copilot/gpt-4o"
                    model_flag = ["-m", model_to_use]
                elif self.agent_type == "openclaw":
                    model_flag = ["--agent", request.model]

            if self.agent_type == "opencode":
                cmd = (
                    [self.binary_path, "run"]
                    + model_flag
                    + [prompt, "--format", "json"]
                )
                if request.session_id:
                    cmd.extend(["--session", request.session_id])
            elif self.agent_type == "openclaw":
                cmd = [
                    self.binary_path,
                    "agent",
                    "--message",
                    prompt,
                    "--json",
                ] + model_flag
                if request.session_id:
                    cmd.extend(["--session-id", request.session_id])
                if "--agent" not in " ".join(cmd):
                    cmd.extend(["--agent", "main"])
            else:
                error_msg = f"Unsupported local agent: {self.agent_type}"
                raise ServiceUnavailableError(error_msg)

            logger.info(f"Executing local CLI: {' '.join(cmd)}")

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            logger.debug(f"CLI Exit Code: {process.returncode}")
            if stdout:
                logger.debug(f"CLI Stdout length: {len(stdout)}")
            if stderr:
                logger.debug(f"CLI Stderr: {stderr.decode().strip()}")

            if process.returncode != 0:
                error_msg = stderr.decode().strip()
                logger.error(f"CLI execution failed: {error_msg}")
                raise ServiceUnavailableError(error_msg)

            output = stdout.decode().strip()
            result = self._parse_cli_output(output, request.model or self.agent_type)
            return result

        except Exception as e:
            error_msg = f"Local CLI request failed: {str(e)}"
            logger.error(error_msg)
            raise ServiceUnavailableError(error_msg) from e

    def _parse_cli_output(self, output: str, model_name: str) -> ChatResponse:
        try:
            lines = output.splitlines()
            full_content = ""
            found_json = False

            if self.agent_type == "opencode":
                for line in lines:
                    try:
                        data = json.loads(line)
                        found_json = True
                        if data.get("type") == "text":
                            full_content += data.get("part", {}).get("text", "")
                        elif data.get("type") == "error":
                            error_data = data.get("error", {}).get("data", {})
                            error_msg = error_data.get("message", "Unknown error")
                            raise ValueError(error_msg)
                    except json.JSONDecodeError:
                        continue

            elif self.agent_type == "openclaw":
                for line in reversed(lines):
                    try:
                        data = json.loads(line)
                        found_json = True
                        content = data.get("response", {}).get("text")
                        if content:
                            full_content = content
                            break
                    except json.JSONDecodeError:
                        continue

            if not found_json or not full_content:
                return self._raw_output_to_response(output, model_name)

            choices = [
                ChatMessageChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant",
                        content=full_content,
                        name=None,
                    ),
                    finish_reason="stop",
                    content_filter_results=None,
                )
            ]

            return ChatResponse(
                id=f"cli-{int(time.time())}",
                object="chat.completion",
                created=int(time.time()),
                model=model_name,
                choices=choices,
                usage={
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                session_id=None,
            )

        except Exception as e:
            logger.error(f"Failed to parse CLI output: {e}")
            return self._raw_output_to_response(output, model_name)

    def _raw_output_to_response(self, output: str, model_name: str) -> ChatResponse:
        choices = [
            ChatMessageChoice(
                index=0,
                message=ChatMessage(
                    role="assistant",
                    content=output,
                    name=None,
                ),
                finish_reason="stop",
                content_filter_results=None,
            )
        ]

        return ChatResponse(
            id=f"cli-raw-{int(time.time())}",
            object="chat.completion",
            created=int(time.time()),
            model=model_name,
            choices=choices,
            usage={},
            session_id=None,
        )

    async def discover_models(self) -> list[dict[str, Any]]:
        models = [
            {
                "id": self.agent_type,
                "display_name": f"LOCAL-AGENT/{self.agent_type}",
                "owned_by": "local-agent",
                "tier": "free",
                "description": f"Main entry for {self.agent_type}",
                "capabilities": {
                    "max_tokens": 32768,
                    "multimodal": False,
                    "has_search": True,
                },
            }
        ]

        try:
            if self.agent_type == "opencode":
                process = await asyncio.create_subprocess_exec(
                    self.binary_path,
                    "models",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await process.communicate()
                if process.returncode == 0:
                    for line in stdout.decode().splitlines():
                        line = line.strip()
                        if line and "/" in line:
                            tier = (
                                "free"
                                if "copilot" in line.lower() or "google" in line.lower()
                                else "standard"
                            )
                            models.append(
                                {
                                    "id": line,
                                    "display_name": f"{self.agent_type.upper()}/{line}",
                                    "owned_by": "local-agent",
                                    "description": f"Model via {self.agent_type}",
                                    "tier": tier,
                                    "capabilities": {
                                        "max_tokens": 32768,
                                        "multimodal": "vision" in line.lower(),
                                        "has_search": True,
                                    },
                                }
                            )
            elif self.agent_type == "openclaw":
                process = await asyncio.create_subprocess_exec(
                    self.binary_path,
                    "models",
                    "list",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await process.communicate()
                if process.returncode == 0:
                    lines = stdout.decode().splitlines()
                    for line in lines[1:]:
                        parts = line.split()
                        if parts:
                            model_id = parts[0]
                            models.append(
                                {
                                    "id": model_id,
                                    "display_name": f"{self.agent_type.upper()}/{model_id}",
                                    "owned_by": "local-agent",
                                    "description": f"Model via {self.agent_type}",
                                    "tier": "standard",
                                    "capabilities": {
                                        "max_tokens": 32768,
                                        "multimodal": False,
                                        "has_search": True,
                                    },
                                }
                            )
        except Exception as e:
            logger.warning(f"Failed to discover sub-models for {self.agent_type}: {e}")

        return models

    async def probe_key(self, api_key: str) -> dict[str, Any]:
        return {"tier": "free", "status": "active"}
