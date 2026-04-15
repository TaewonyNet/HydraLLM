import asyncio
import os
from contextlib import asynccontextmanager

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
    while True:
        try:
            await asyncio.sleep(60)
            await gateway.recover_failed_keys()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in recovery task: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        scraper = app.state.scraper
        await scraper.startup()

        gateway = app.state.gateway
        gateway.initialize_settings()

        async def run_discovery() -> None:
            await gateway.discover_all_models()
            await gateway.probe_all_keys()

        app.state.discovery_task = asyncio.create_task(run_discovery())
        app.state.recovery_task = asyncio.create_task(recovery_task(gateway))
    except Exception as e:
        logger.error(f"Error during startup: {e}")

    yield

    if hasattr(app.state, "scraper"):
        await app.state.scraper.shutdown()
    if hasattr(app.state, "discovery_task"):
        app.state.discovery_task.cancel()
    if hasattr(app.state, "recovery_task"):
        app.state.recovery_task.cancel()
    if hasattr(app.state, "session_manager"):
        app.state.session_manager.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="HydraLLM",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": exc.errors()},
        )

    from src.services.admin_service import AdminService
    from src.services.metrics_service import MetricsService

    analyzer = ContextAnalyzer(max_tokens_fast_model=settings.max_tokens_fast_model)
    key_manager = KeyManager()
    session_manager = SessionManager()
    scraper = WebScraper()
    compressor = ContextCompressor()
    metrics_service = MetricsService(session_manager)
    admin_service = AdminService(session_manager)

    if settings.gemini_keys:
        keys = settings.gemini_keys
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        key_manager.add_keys("gemini", keys)

    if settings.groq_keys:
        keys = settings.groq_keys
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        key_manager.add_keys("groq", keys)

    if settings.cerebras_keys:
        keys = settings.cerebras_keys
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        key_manager.add_keys("cerebras", keys)

    gateway = Gateway(analyzer, key_manager, session_manager, scraper, compressor)

    app.state.analyzer = analyzer
    app.state.key_manager = key_manager
    app.state.session_manager = session_manager
    app.state.scraper = scraper
    app.state.gateway = gateway
    app.state.admin_service = admin_service
    app.state.metrics_service = metrics_service

    app.include_router(api_router, prefix="/v1")

    if os.path.exists(STATIC_DIR):
        app.mount("/ui/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/ui")
    async def ui_root():
        ui_path = os.path.join(STATIC_DIR, "index.html")
        if not os.path.exists(ui_path):
            raise HTTPException(status_code=404)
        return FileResponse(ui_path)

    @app.get("/")
    async def root():
        return {
            "status": "online",
            "message": "HydraLLM API",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "ui": "/ui",
        }

    return app


app = create_app()
