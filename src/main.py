import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.auth import check_bind_safety
from src.api.server import APIServer
from src.core.config import API_HOST, API_PORT, API_TOKEN
from src.core.container import Container
from src.core.logging import get_logger
from src.ui.app import App

logger = get_logger("main")


def _build_api_app(container: Container):
    from src.api.app import create_app

    return create_app(container)


def main() -> None:
    check_bind_safety(API_HOST, API_TOKEN)
    container = Container()
    ui_ready = threading.Event()

    def _deferred_api_app():
        # Wait for the initial Flet UI frame to finish rendering (up to 3s fallback)
        # so FastAPI/MCP imports don't compete for GIL and HDD I/O during window startup.
        ui_ready.wait(timeout=3.0)
        return _build_api_app(container)

    api_server = APIServer(_deferred_api_app)
    api_server.start()
    logger.info("API available at http://%s:%s/api/v1", API_HOST, API_PORT)

    try:
        gui = App(container, on_ui_ready=ui_ready.set)
        gui.run()
    finally:
        ui_ready.set()
        api_server.stop()


if __name__ == "__main__":
    main()
