import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api.v1.endpoints import router as api_router
from src.core.config import settings
from src.core.logging import get_logger, setup_logging
from src.i18n import set_locale
from src.services.analyzer import ContextAnalyzer
from src.services.compressor import ContextCompressor
from src.services.gateway import Gateway
from src.services.installer import InstallerService
from src.services.intent_classifier import IntentClassifier
from src.services.key_manager import KeyManager
from src.services.keyword_store import KeywordStore
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
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        scraper = app.state.scraper
        await scraper.startup()

        gateway = app.state.gateway
        gateway.initialize_settings()

        intent_classifier = app.state.intent_classifier
        app.state.intent_init_task = asyncio.create_task(intent_classifier.initialize())

        async def run_discovery() -> None:
            logger.info("🚀 Starting initial resource discovery and key probing...")
            try:
                await gateway.discover_all_models()
                await gateway.probe_all_keys()
                logger.info("✅ Background discovery and probing completed")
            except Exception as de:
                logger.error(f"❌ Failed to run initial discovery: {de}")

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
    set_locale(settings.locale)
    app = FastAPI(
        title="HydraLLM",
        version="1.3.0",
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
    installer_service = InstallerService()

    ollama_host = settings.ollama_base_url.rstrip("/")
    if ollama_host.endswith("/v1"):
        ollama_host = ollama_host[:-3]
    data_dir = Path(settings.data_dir)
    if not data_dir.is_absolute():
        project_root = Path(__file__).resolve().parent.parent
        data_dir = project_root / data_dir
    keyword_store = KeywordStore(data_dir=data_dir)
    intent_classifier = IntentClassifier(
        ollama_base_url=ollama_host,
        model=settings.embedding_model,
        keyword_store=keyword_store,
        extraction_model=settings.keyword_extraction_model,
    )

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

    gateway = Gateway(
        analyzer,
        key_manager,
        session_manager,
        scraper,
        compressor,
        intent_classifier=intent_classifier,
    )

    # AdminService 가 온보딩에서 가용 모델 목록을 조회할 수 있도록 gateway 핸들 연결.
    admin_service._gateway = gateway  # noqa: SLF001

    app.state.analyzer = analyzer
    app.state.key_manager = key_manager
    app.state.session_manager = session_manager
    app.state.scraper = scraper
    app.state.gateway = gateway
    app.state.admin_service = admin_service
    app.state.metrics_service = metrics_service
    app.state.intent_classifier = intent_classifier
    app.state.keyword_store = keyword_store
    app.state.installer_service = installer_service

    app.include_router(api_router, prefix="/v1")

    if os.path.exists(STATIC_DIR):
        app.mount("/ui/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/ui")
    async def ui_root() -> Any:
        ui_path = os.path.join(STATIC_DIR, "index.html")
        if not os.path.exists(ui_path):
            raise HTTPException(status_code=404)
        return FileResponse(ui_path)

    @app.get("/")
    async def root() -> Any:
        return {
            "status": "online",
            "message": "HydraLLM API",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "ui": "/ui",
        }

    return app


app = create_app()
