import logging
import time
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)
trace_ctx: ContextVar[dict[str, Any]] = ContextVar("trace", default={})


class Observability:
    @staticmethod
    def start_trace(request_id: str) -> None:
        trace_ctx.set(
            {"request_id": request_id, "start_time": time.time(), "steps": []}
        )

    @staticmethod
    def record_step(name: str, duration: float, metadata: dict | None = None) -> None:
        trace = trace_ctx.get()
        if trace:
            trace["steps"].append(
                {"name": name, "duration": duration, "metadata": metadata or {}}
            )

    @staticmethod
    def finalize_trace() -> dict[str, Any] | None:
        trace = trace_ctx.get()
        if trace:
            total_duration = time.time() - trace["start_time"]
            logger.info(
                f"Trace {trace['request_id']} completed in {total_duration:.3f}s. Steps: {trace['steps']}"
            )
            return trace
        return None
