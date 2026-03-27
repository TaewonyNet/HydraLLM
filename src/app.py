import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api.v1.endpoints import router as api_router
from src.core.config import settings
from src.core.logging import get_logger, setup_logging
from src.services.analyzer import ContextAnalyzer
from src.services.compressor import ContextCompressor
from src.services.gateway import Gateway
from src.services.key_manager import KeyManager
from src.services.scraper import WebScraper
from src.services.session_manager import SessionManager

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static"
)

setup_logging()
logger = get_logger(__name__)


async def recovery_task(gateway: Gateway) -> None:
    """
    Background task to periodically attempt recovery of failed keys.
    """
    while True:
        try:
            await asyncio.sleep(60)
            logger.info("📡 Running periodic key recovery task...")
            await gateway.recover_failed_keys()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in recovery task: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore
    """
    Handle application lifespan events.
    """
    try:
        scraper = app.state.scraper
        await scraper.startup()

        gateway = app.state.gateway
        gateway.initialize_settings()

        async def run_discovery() -> None:
            logger.info("🚀 Starting dynamic model discovery and key probing...")
            await gateway.discover_all_models()
            await gateway.probe_all_keys()
            logger.info("✅ Startup initialization completed")

        app.state.discovery_task = asyncio.create_task(run_discovery())
        app.state.recovery_task = asyncio.create_task(recovery_task(gateway))
    except Exception as e:
        logger.error(f"❌ Error during startup: {e}")

    yield

    if hasattr(app.state, "scraper"):
        await app.state.scraper.shutdown()

    if hasattr(app.state, "discovery_task"):
        app.state.discovery_task.cancel()
        try:
            await asyncio.wait_for(app.state.discovery_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    if hasattr(app.state, "recovery_task"):
        app.state.recovery_task.cancel()
        try:
            await asyncio.wait_for(app.state.recovery_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="HydraLLM",
        description="Context-Aware HydraLLM",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.error(f"Validation error: {exc.errors()}")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": exc.errors()},
        )

    # Initialize components
    analyzer = ContextAnalyzer(max_tokens_fast_model=settings.max_tokens_fast_model)
    key_manager = KeyManager()
    session_manager = SessionManager()
    scraper = WebScraper()
    compressor = ContextCompressor()

    # Add keys from settings
    if settings.gemini_keys:
        keys = settings.gemini_keys
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        key_manager.add_keys("gemini", keys)
        logger.info(f"Added {len(keys)} Gemini keys")

    if settings.groq_keys:
        keys = settings.groq_keys
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        key_manager.add_keys("groq", keys)
        logger.info(f"Added {len(keys)} Groq keys")

    if settings.cerebras_keys:
        keys = settings.cerebras_keys
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        key_manager.add_keys("cerebras", keys)
        logger.info(f"Added {len(keys)} Cerebras keys")

    gateway = Gateway(analyzer, key_manager, session_manager, scraper, compressor)

    # Store in app state for use by dependencies and lifespan
    app.state.analyzer = analyzer
    app.state.key_manager = key_manager
    app.state.session_manager = session_manager
    app.state.scraper = scraper
    app.state.gateway = gateway

    # Mount static files
    if os.path.exists(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    else:
        logger.warning(f"Static directory not found at {STATIC_DIR}")

    # Include API router
    app.include_router(api_router, prefix="/v1")

    return app


app = create_app()


@app.get("/ui")
async def ui() -> FileResponse:
    ui_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(ui_path):
        raise HTTPException(status_code=404, detail=f"UI file not found at {ui_path}")
    return FileResponse(ui_path)


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "message": "HydraLLM API",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "ui": "/ui",
    }
