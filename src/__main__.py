"""Entry point for `python -m src` — starts the ferret HTTP server."""

import logging

import uvicorn

from src.config import Config

config = Config.from_env()
logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
uvicorn.run("src.server:app", host=config.host, port=config.port, log_level=config.log_level.lower())
