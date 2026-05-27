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
            if self.agent_type == "opencode":
                model_to_use = request.model
                if model_to_use:
                    if "/" in model_to_use:
                        parts = model_to_use.split("/")
                        
                        if len(parts) >= 3 and parts[0].upper() == "LOCAL-AGENT" and parts[1].upper() == "OPENCODE":
                            model_to_use = "/".join(parts[2:])
                        elif len(parts) >= 2 and parts[0].upper() == "OPENCODE":
                            model_to_use = "/".join(parts[1:])
                        elif len(parts) >= 2 and parts[0].upper() == "LOCAL-AGENT":
                            model_to_use = "/".join(parts[1:])
                        else:
                            model_to_use = request.model

                    if model_to_use == "opencode" or not model_to_use:
                        model_to_use = "github-copilot/gpt-4o"
                    model_flag = ["-m", model_to_use]
                
                cmd = (
                    [self.binary_path, "run"]
                    + model_flag
                    + [prompt, "--format", "json"]
                )
                if request.session_id:
                    cmd.extend(["--session", request.session_id])
            elif self.agent_type == "openclaw":
                model_to_use = request.model
                if model_to_use:
                    if "/" in model_to_use:
                        parts = model_to_use.split("/")
                        
                        if len(parts) >= 3 and parts[0].upper() == "LOCAL-AGENT" and parts[1].upper() == "OPENCLAW":
                            model_to_use = "/".join(parts[2:])
                        elif len(parts) >= 2 and parts[0].upper() == "OPENCLAW":
                            model_to_use = "/".join(parts[1:])
                        elif len(parts) >= 2 and parts[0].upper() == "LOCAL-AGENT":
                            model_to_use = "/".join(parts[1:])
                        else:
                            model_to_use = request.model
                    
                    if model_to_use == "openclaw" or not model_to_use:
                        model_to_use = "main"
                    model_flag = ["--agent", model_to_use]

                cmd = [
                    self.binary_path,
                    "agent",
                    "--message",
                    prompt,
                    "--json",
                ]
                
                if model_flag:
                    cmd.extend(model_flag)

                if request.session_id:
                    cmd.extend(["--session-id", request.session_id])
                
                if "--agent" not in cmd and not request.session_id:
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
                        error_msg = error_data.get("message") or data.get("error", {}).get("message") or "Unknown error"
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
        models = []
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
                    "agents",
                    "list",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await process.communicate()
                if process.returncode == 0:
                    lines = stdout.decode().splitlines()
                    start_parsing = False
                    for line in lines:
                        line = line.strip()
                        if line == "Agents:":
                            start_parsing = True
                            continue
                        if not start_parsing:
                            continue
                        
                        if line.startswith("- "):
                            agent_id = line[2:].split()[0]
                            display_name = f"LOCAL-AGENT/OPENCLAW/{agent_id}"
                            
                            models.append(
                                {
                                    "id": display_name,
                                    "display_name": display_name,
                                    "owned_by": "local-agent",
                                    "description": f"Agent via {self.agent_type}",
                                    "tier": "standard",
                                    "capabilities": {
                                        "max_tokens": 32768,
                                        "multimodal": False,
                                        "has_search": True,
                                    },
                                }
                            )
            
            if not models:
                models.append({
                    "id": "main" if self.agent_type == "openclaw" else "default",
                    "display_name": f"LOCAL-AGENT/{self.agent_type.upper()}/default",
                    "owned_by": "local-agent",
                    "tier": "free",
                    "description": f"Default entry for {self.agent_type}",
                    "capabilities": {"max_tokens": 32768, "multimodal": False, "has_search": True},
                })
        except Exception as e:
            logger.warning(f"Failed to discover sub-models for {self.agent_type}: {e}")

        return models

    async def probe_key(self, api_key: str) -> dict[str, Any]:
        return {"tier": "free", "status": "active"}
