import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class InstallerService:
    """opencode / openclaw 로컬 에이전트 CLI 설치 및 검사."""

    OPENCLAW_NPM_PACKAGE = "openclaw"
    OPENCODE_INSTALL_URL = "https://opencode.ai/install"
    OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
    HYDRA_PROVIDER_BASE_URL = "http://127.0.0.1:8000/v1"

    async def check_installed(self, tool: str) -> dict[str, Any]:
        which = shutil.which(tool)
        if not which:
            return {"installed": False, "path": None, "version": None}
        version = await self._run_version(tool, which)
        return {"installed": True, "path": which, "version": version}

    async def status_all(self) -> dict[str, dict[str, Any]]:
        return {
            "opencode": await self.check_installed("opencode"),
            "openclaw": await self.check_installed("openclaw"),
        }

    async def install(self, tool: str) -> dict[str, Any]:
        if tool == "openclaw":
            return await self._install_openclaw()
        if tool == "opencode":
            return await self._install_opencode()
        return {"success": False, "message": f"Unknown tool: {tool}"}

    async def install_openclaw_mllm_auto(self) -> dict[str, Any]:
        """openclaw 설치 후 'mllm-auto' 에이전트를 HydraLLM 게이트웨이로 라우팅되도록 등록."""
        openclaw_status = await self.check_installed("openclaw")
        if not openclaw_status["installed"]:
            result = await self._install_openclaw()
            if not result["success"]:
                return result

        provider_changed = self._ensure_openai_provider_in_config()
        if provider_changed:
            await self._openclaw_gateway_restart()

        existing = await self._openclaw_agents_list()
        if "mllm-auto" in existing:
            return {
                "success": True,
                "message": "mllm-auto agent already registered"
                + (" (provider config updated)" if provider_changed else ""),
            }

        workspace_dir = "/home/tide/.openclaw/workspace-mllm-auto"
        cmd = [
            "openclaw", "agents", "add", "mllm-auto",
            "--non-interactive",
            "--workspace", workspace_dir,
            "--model", "openai/mllm/auto",
            "--json",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            ok = proc.returncode == 0
            return {
                "success": ok,
                "message": (stdout or stderr).decode(errors="ignore").strip()[-600:]
                or ("registered" if ok else "failed"),
            }
        except TimeoutError:
            return {"success": False, "message": "openclaw agents add timed out"}
        except FileNotFoundError:
            return {"success": False, "message": "openclaw binary not found after install"}

    def _ensure_openai_provider_in_config(self) -> bool:
        """openclaw.json 의 models.providers.openai 를 Hydra 로컬 엔드포인트로 등록/갱신.

        변경이 발생한 경우 True 를 반환한다.
        """
        config_path = self.OPENCLAW_CONFIG_PATH
        if not config_path.exists():
            return False
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"openclaw.json read failed: {exc}")
            return False

        desired_provider = {
            "api": "openai-completions",
            "apiKey": "hydra-local",
            "baseUrl": self.HYDRA_PROVIDER_BASE_URL,
            "models": [
                {
                    "id": "mllm/auto",
                    "name": "mllm/auto",
                    "contextWindow": 131072,
                    "cost": {"cacheRead": 0, "cacheWrite": 0, "input": 0, "output": 0},
                    "input": ["text"],
                    "maxTokens": 8192,
                }
            ],
        }

        models = data.setdefault("models", {})
        providers = models.setdefault("providers", {})
        existing = providers.get("openai")
        if existing == desired_provider:
            return False
        providers["openai"] = desired_provider

        try:
            config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning(f"openclaw.json write failed: {exc}")
            return False
        logger.info("openclaw openai provider updated to Hydra endpoint")
        return True

    async def _openclaw_gateway_restart(self) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "openclaw", "gateway", "restart",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=20)
        except (TimeoutError, FileNotFoundError) as exc:
            logger.warning(f"openclaw gateway restart failed: {exc}")

    async def _openclaw_agents_list(self) -> set[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "openclaw", "agents", "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (TimeoutError, FileNotFoundError):
            return set()
        names: set[str] = set()
        for line in stdout.decode(errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                name = stripped[2:].split(" ")[0]
                if name:
                    names.add(name)
        return names

    async def _install_openclaw(self) -> dict[str, Any]:
        if not shutil.which("npm"):
            return {"success": False, "message": "npm not found; install Node.js first"}
        cmd = ["npm", "install", "-g", self.OPENCLAW_NPM_PACKAGE]
        return await self._run_install(cmd, timeout=300)

    async def _install_opencode(self) -> dict[str, Any]:
        if not shutil.which("curl") or not shutil.which("bash"):
            return {"success": False, "message": "curl or bash not found"}
        shell_cmd = f"curl -fsSL {self.OPENCODE_INSTALL_URL} | bash"
        return await self._run_install(["bash", "-c", shell_cmd], timeout=300)

    async def _run_install(self, cmd: list[str], timeout: int) -> dict[str, Any]:
        logger.info(f"Installer running: {' '.join(cmd)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = (stdout.decode(errors="ignore") + stderr.decode(errors="ignore")).strip()
            ok = proc.returncode == 0
            logger.info(f"Installer finished ok={ok} code={proc.returncode}")
            return {"success": ok, "message": output[-800:] if output else ("ok" if ok else "failed")}
        except TimeoutError:
            return {"success": False, "message": "install timed out"}

    async def _run_version(self, tool: str, path: str) -> str | None:
        for flag in ("--version", "-v", "version"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    path, flag,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode == 0:
                    out = stdout.decode(errors="ignore").strip()
                    if out:
                        return out.splitlines()[0][:120]
            except (TimeoutError, FileNotFoundError):
                continue
        return None
