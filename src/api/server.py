from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..core.config import API_HOST, API_PORT
from ..core.logging import get_logger

if TYPE_CHECKING:
    import uvicorn
    from fastapi import FastAPI

logger = get_logger("api.server")


class APIServer:
    """Runs the FastAPI app in a background daemon thread."""

    def __init__(self, app: FastAPI | Callable[[], FastAPI]) -> None:
        self._app = app
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested = False

    def _run(self) -> None:
        import uvicorn

        app_instance = (
            self._app()
            if callable(self._app) and not hasattr(self._app, "router")
            else self._app
        )
        if self._stop_requested:
            return
        config = uvicorn.Config(
            app=app_instance,
            host=API_HOST,
            port=API_PORT,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        if self._stop_requested:
            return
        logger.info("API server started on http://%s:%s", API_HOST, API_PORT)
        self._server.run()

    def start(self) -> None:
        self._stop_requested = False
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="foxprofile-api",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested = True
        if self._server:
            self._server.should_exit = True
            logger.info("API server shutdown requested")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
