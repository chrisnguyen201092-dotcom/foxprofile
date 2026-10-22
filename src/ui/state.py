import threading
import time

from ..core.strings import get_string

ITEMS_PER_PAGE = 0


class AppState:
    def __init__(self) -> None:
        self.current_page: int = 1
        self.items_per_page: int = ITEMS_PER_PAGE
        self.sort_by: str = "custom"
        self.search_query: str = ""
        self.log_collapsed: bool = False

        self._log_lines: list[str] = []
        self._loading_profiles: set[str] = set()
        # Profiles whose last launch exited before it was ready.
        self.failed: set[str] = set()
        self._loading_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._last_log_ui_update: float = 0.0
        self._pending_log_flush: bool = False
        self._refresh_requested = threading.Event()
        self._last_running_snapshot: set[str] = set()
        self._ui_update_lock = threading.Lock()
        self._selected_profiles: set[str] = set()
        self._selection_lock = threading.Lock()
        self.last_selected_name: str | None = None
        self.shift_down: bool = False
        self.editing_note: str | None = None
        self.editing_proxy: str | None = None
        # profile_name -> ("checking" | "ok" | "fail", short_label)
        self.proxy_status: dict[str, tuple[str, str]] = {}

    @property
    def loading(self) -> set[str]:
        with self._loading_lock:
            return set(self._loading_profiles)

    def loading_names(self) -> set[str]:
        with self._loading_lock:
            return set(self._loading_profiles)

    def is_loading(self, name: str) -> bool:
        with self._loading_lock:
            return name in self._loading_profiles

    def set_loading(self, name: str, value: bool) -> None:
        with self._loading_lock:
            if value:
                self._loading_profiles.add(name)
            else:
                self._loading_profiles.discard(name)

    def schedule_refresh(self) -> None:
        self._refresh_requested.set()

    def consume_refresh(self) -> bool:
        if self._refresh_requested.is_set():
            self._refresh_requested.clear()
            return True
        return False

    def add_log(self, message: str) -> bool:
        force = (
            message == get_string("browser_started")
            or message.startswith(get_string("session_ended", name=""))
            or "LAUNCH_FAILED:" in message
            or "Error" in message
        )
        now = time.monotonic()
        with self._log_lock:
            self._log_lines.append(f"> {message}")
            if force or now - self._last_log_ui_update >= 0.15:
                self._last_log_ui_update = now
                self._pending_log_flush = True
                return True
        return False

    def flush_log(self) -> str | None:
        with self._log_lock:
            if not self._pending_log_flush:
                return None
            self._pending_log_flush = False
            return "\n".join(self._log_lines[-50:])

    def get_all_log_lines(self) -> list[str]:
        with self._log_lock:
            return list(self._log_lines)

    def toggle_selection(self, name: str) -> None:
        with self._selection_lock:
            if name in self._selected_profiles:
                self._selected_profiles.discard(name)
            else:
                self._selected_profiles.add(name)
            self.last_selected_name = name

    def select_range(
        self,
        visible_names: list[str],
        target_name: str | None = None,
    ) -> int:
        with self._selection_lock:
            if target_name is not None:
                anchor = self.last_selected_name
                if anchor in visible_names and target_name in visible_names:
                    i1 = visible_names.index(anchor)
                    i2 = visible_names.index(target_name)
                    lo, hi = min(i1, i2), max(i1, i2)
                    span = visible_names[lo : hi + 1]
                    self._selected_profiles.update(span)
                    self.last_selected_name = target_name
                    return len(span)
                self._selected_profiles.add(target_name)
                self.last_selected_name = target_name
                return 1

            indices = [idx for idx, n in enumerate(visible_names) if n in self._selected_profiles]
            if len(indices) < 2:
                return len(indices)
            lo, hi = min(indices), max(indices)
            span = visible_names[lo : hi + 1]
            self._selected_profiles.update(span)
            return len(span)

    def is_selected(self, name: str) -> bool:
        with self._selection_lock:
            return name in self._selected_profiles

    def selected_names(self) -> set[str]:
        with self._selection_lock:
            return set(self._selected_profiles)

    def clear_selection(self) -> None:
        with self._selection_lock:
            self._selected_profiles.clear()

    def select_all(self, names: list[str]) -> None:
        with self._selection_lock:
            self._selected_profiles.update(names)
