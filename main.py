#!/usr/bin/env python3.10

import argparse

import uvicorn

from src.app import app
from src.core.config import settings

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HydraLLM Server")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument(
        "--port", type=int, default=settings.port, help="Port to run on"
    )
    args = parser.parse_args()

    if args.debug:
        settings.debug = True
        from src.core.logging import setup_logging

        setup_logging()
        print("🔧 Debug mode enabled via CLI")

    print("🚀 Initializing HydraLLM...")
    uvicorn.run(app, host="0.0.0.0", port=args.port)
