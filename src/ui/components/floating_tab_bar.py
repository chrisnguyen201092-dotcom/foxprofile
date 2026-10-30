"""Always-on-top Floating Mini-Tab Bar for switching running browser profiles on Windows.

Runs in a lightweight daemon thread using Tkinter + Win32 `WS_EX_TOOLWINDOW` so the user
can switch between running Camoufox profile windows in 1 click without returning to the
main FoxProfile window, while keeping every browser process 100% isolated (`SWP_NOSIZE`).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Callable

from ...core.strings import get_string


def _apply_win32_toolwindow(hwnd: int) -> None:
    """Hide the floating bar from the Windows taskbar and Alt+Tab switcher."""
    if sys.platform != "win32" or hwnd <= 0:
        return
    with contextlib.suppress(Exception):
        import ctypes

        user32 = ctypes.windll.user32
        gwl_exstyle = -20
        ws_ex_toolwindow = 0x00000080
        ws_ex_topmost = 0x00000008
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        parent = user32.GetParent(hwnd) or hwnd
        cur = int(get_long(parent, gwl_exstyle))
        set_long(parent, gwl_exstyle, cur | ws_ex_toolwindow | ws_ex_topmost)


class FloatingProfileTabBar:
    """Thread-safe controller for the Always-on-Top Floating Mini-Tab Bar."""

    def __init__(
        self,
        on_select_tab: Callable[[str], None],
        on_close_tab: Callable[[str], None],
        on_toggle_single_stage: Callable[[], None],
        on_focus_foxprofile: Callable[[], None],
    ) -> None:
        self._on_select_tab = on_select_tab
        self._on_close_tab = on_close_tab
        self._on_toggle_single_stage = on_toggle_single_stage
        self._on_focus_foxprofile = on_focus_foxprofile

        self._lock = threading.Lock()
        self._state: tuple[tuple[str, ...], frozenset[str], str | None, bool, bool] = (
            (),
            frozenset(),
            None,
            True,
            True,
        )
        self._thread: threading.Thread | None = None
        self._stop_flag = False

    def sync(
        self,
        running_names: list[str],
        loading_names: set[str],
        active_tab: str | None,
        single_stage_mode: bool,
        enabled: bool,
    ) -> None:
        """Update the floating bar state; lazily starts the UI thread when needed."""
        new_state = (
            tuple(running_names),
            frozenset(loading_names),
            active_tab,
            bool(single_stage_mode),
            bool(enabled),
        )
        with self._lock:
            self._state = new_state
            should_start = (
                self._thread is None
                and bool(running_names)
                and bool(enabled)
                and sys.platform == "win32"
                and "PYTEST_CURRENT_TEST" not in os.environ
            )
            if should_start:
                self._thread = threading.Thread(
                    target=self._run_tk_loop,
                    name="FoxTabFloatingBar",
                    daemon=True,
                )
                self._thread.start()

    def close(self) -> None:
        with self._lock:
            self._stop_flag = True

    def _run_tk_loop(self) -> None:
        try:
            import tkinter as tk
        except Exception:
            return

        try:
            root = tk.Tk()
        except Exception:
            return

        root.withdraw()
        root.overrideredirect(True)
        with contextlib.suppress(Exception):
            root.attributes("-topmost", True)
            root.configure(bg="#0F172A")

        outer = tk.Frame(
            root,
            bg="#0F172A",
            highlightbackground="#334155",
            highlightthickness=1,
            padx=6,
            pady=4,
        )
        outer.pack(fill="both", expand=True)

        drag_state = {"x": 0, "y": 0, "custom_pos": False, "win_x": 0, "win_y": 0}
        collapsed = {"value": False}
        last_rendered: list[object] = [None]

        def _start_drag(event: object) -> None:
            drag_state["x"] = getattr(event, "x_root", 0) - root.winfo_x()
            drag_state["y"] = getattr(event, "y_root", 0) - root.winfo_y()

        def _on_drag(event: object) -> None:
            nx = getattr(event, "x_root", 0) - drag_state["x"]
            ny = max(0, getattr(event, "y_root", 0) - drag_state["y"])
            drag_state["custom_pos"] = True
            drag_state["win_x"] = nx
            drag_state["win_y"] = ny
            root.geometry(f"+{nx}+{ny}")

        def _toggle_collapse() -> None:
            collapsed["value"] = not collapsed["value"]
            last_rendered[0] = None

        def _rebuild(
            running: tuple[str, ...],
            loading: frozenset[str],
            active_tab: str | None,
            single_stage: bool,
        ) -> None:
            for child in outer.winfo_children():
                child.destroy()

            eff_active = active_tab if active_tab in running else (running[0] if running else None)

            # Left draggable badge
            badge_text = (
                f"🦊 {len(running)} Tab ▾"
                if collapsed["value"]
                else get_string("running_tabs_badge", count=len(running))
            )
            badge = tk.Label(
                outer,
                text=badge_text,
                bg="#1E293B",
                fg="#FBBF24",
                font=("Segoe UI Semibold", 9),
                padx=8,
                pady=3,
                cursor="fleur",
            )
            badge.pack(side="left", padx=(0, 4))
            badge.bind("<ButtonPress-1>", _start_drag)
            badge.bind("<B1-Motion>", _on_drag)
            badge.bind("<Double-Button-1>", lambda _e: self._on_focus_foxprofile())

            if not collapsed["value"]:
                for idx, pname in enumerate(running):
                    is_active = pname == eff_active
                    is_loading = pname in loading
                    tab_bg = "#059669" if is_active else "#1E293B"
                    tab_fg = "#FFFFFF" if is_active else "#E2E8F0"
                    dot = "⏳" if is_loading else "●"
                    hk = f" ({idx + 1})" if idx < 9 else ""

                    tab_frame = tk.Frame(
                        outer,
                        bg=tab_bg,
                        highlightbackground="#34D399" if is_active else "#334155",
                        highlightthickness=1,
                    )
                    tab_frame.pack(side="left", padx=2)

                    btn_select = tk.Label(
                        tab_frame,
                        text=f"{dot} {pname}{hk}",
                        bg=tab_bg,
                        fg=tab_fg,
                        font=("Segoe UI Semibold" if is_active else "Segoe UI", 9),
                        padx=8,
                        pady=2,
                        cursor="hand2",
                    )
                    btn_select.pack(side="left")
                    btn_select.bind(
                        "<Button-1>",
                        lambda _e, n=pname: self._on_select_tab(n),
                    )

                    btn_close = tk.Label(
                        tab_frame,
                        text="✕",
                        bg=tab_bg,
                        fg="#CBD5E1" if is_active else "#94A3B8",
                        font=("Segoe UI", 8),
                        padx=5,
                        pady=2,
                        cursor="hand2",
                    )
                    btn_close.pack(side="left")
                    btn_close.bind(
                        "<Button-1>",
                        lambda _e, n=pname: self._on_close_tab(n),
                    )

                # Single-stage mode toggle button
                stage_label = get_string(
                    "single_stage_on" if single_stage else "single_stage_off"
                )
                btn_stage = tk.Label(
                    outer,
                    text=stage_label,
                    bg="#065F46" if single_stage else "#1E293B",
                    fg="#A7F3D0" if single_stage else "#94A3B8",
                    font=("Segoe UI Semibold", 8),
                    padx=7,
                    pady=3,
                    cursor="hand2",
                )
                btn_stage.pack(side="left", padx=(6, 2))
                btn_stage.bind("<Button-1>", lambda _e: self._on_toggle_single_stage())

                # Focus FoxProfile main window button
                btn_home = tk.Label(
                    outer,
                    text=get_string("floating_bar_home_btn"),
                    bg="#1E293B",
                    fg="#E2E8F0",
                    font=("Segoe UI Semibold", 8),
                    padx=7,
                    pady=3,
                    cursor="hand2",
                )
                btn_home.pack(side="left", padx=2)
                btn_home.bind("<Button-1>", lambda _e: self._on_focus_foxprofile())

            # Collapse/Expand button
            btn_fold = tk.Label(
                outer,
                text="+" if collapsed["value"] else "—",
                bg="#1E293B",
                fg="#94A3B8",
                font=("Segoe UI Bold", 8),
                padx=6,
                pady=3,
                cursor="hand2",
            )
            btn_fold.pack(side="left", padx=(2, 0))
            btn_fold.bind("<Button-1>", lambda _e: _toggle_collapse())

            root.update_idletasks()
            req_w = outer.winfo_reqwidth()
            req_h = outer.winfo_reqheight()
            if drag_state["custom_pos"]:
                x = drag_state["win_x"]
                y = drag_state["win_y"]
            else:
                scr_w = root.winfo_screenwidth()
                x = max(8, (scr_w - req_w) // 2)
                y = 0
            root.geometry(f"{req_w}x{req_h}+{x}+{y}")

        def _poll() -> None:
            with self._lock:
                if self._stop_flag:
                    with contextlib.suppress(Exception):
                        root.destroy()
                    return
                running, loading, active_tab, single_stage, enabled = self._state

            if not enabled or not running:
                if last_rendered[0] != "hidden":
                    root.withdraw()
                    last_rendered[0] = "hidden"
                root.after(150, _poll)
                return

            render_key = (running, loading, active_tab, single_stage, collapsed["value"])
            if render_key != last_rendered[0]:
                was_hidden = last_rendered[0] in (None, "hidden")
                _rebuild(running, loading, active_tab, single_stage)
                if was_hidden:
                    root.deiconify()
                    _apply_win32_toolwindow(root.winfo_id())
                with contextlib.suppress(Exception):
                    root.attributes("-topmost", True)
                last_rendered[0] = render_key

            root.after(120, _poll)

        root.after(50, _poll)
        with contextlib.suppress(Exception):
            root.mainloop()
