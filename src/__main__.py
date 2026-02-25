"""Entry point for `python -m src` — starts the ferret HTTP server."""

import uvicorn

from src.config import Config

config = Config.from_env()
uvicorn.run("src.server:app", host=config.host, port=config.port, log_level="info")
