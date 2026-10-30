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

_ASSETS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets"))


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


def _compute_orientation(
    win_x: int,
    win_y: int,
    scr_w: int,
    scr_h: int,
    custom_pos: bool,
) -> str:
    """Return 'horizontal' when near top/bottom edges, 'vertical' when near left/right sides."""
    if not custom_pos or scr_w <= 0 or scr_h <= 0:
        return "horizontal"
    if win_y <= 80 or win_y >= scr_h - 100:
        return "horizontal"
    if win_x <= 240 or win_x >= scr_w - 280:
        return "vertical"
    dist_x = min(max(0, win_x), max(0, scr_w - win_x))
    dist_y = min(max(0, win_y), max(0, scr_h - win_y))
    return "vertical" if dist_x < dist_y else "horizontal"


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
            root.configure(bg="#FFFFFF")

        logo_img = None
        logo_path = os.path.join(_ASSETS_DIR, "logo.png")
        if os.path.isfile(logo_path):
            with contextlib.suppress(Exception):
                raw_logo = tk.PhotoImage(file=logo_path)
                factor = max(1, raw_logo.width() // 20)
                logo_img = raw_logo.subsample(factor, factor)

        outer = tk.Frame(
            root,
            bg="#FFFFFF",
            highlightbackground="#CBD5E1",
            highlightthickness=1,
            padx=5,
            pady=4,
        )
        outer.pack(fill="both", expand=True)

        drag_state = {
            "x": 0,
            "y": 0,
            "start_root_x": 0,
            "start_root_y": 0,
            "moved": False,
            "custom_pos": False,
            "win_x": 0,
            "win_y": 0,
            "dock_right": False,
            "right_x": 0,
            "orientation": "horizontal",
        }
        collapsed = {"value": False}
        last_rendered: list[object] = [None]

        def _start_drag(event: object) -> None:
            rx = int(getattr(event, "x_root", 0))
            ry = int(getattr(event, "y_root", 0))
            drag_state["start_root_x"] = rx
            drag_state["start_root_y"] = ry
            drag_state["moved"] = False
            drag_state["x"] = rx - root.winfo_x()
            drag_state["y"] = ry - root.winfo_y()

        def _on_drag(event: object) -> None:
            rx = int(getattr(event, "x_root", 0))
            ry = int(getattr(event, "y_root", 0))
            if (
                abs(rx - int(drag_state["start_root_x"]))
                + abs(ry - int(drag_state["start_root_y"]))
                > 4
            ):
                drag_state["moved"] = True
            scr_w = root.winfo_screenwidth()
            scr_h = root.winfo_screenheight()
            cur_w = max(40, root.winfo_width())
            cur_h = max(28, root.winfo_height())
            nx = max(0, min(rx - int(drag_state["x"]), scr_w - cur_w))
            ny = max(0, min(ry - int(drag_state["y"]), scr_h - cur_h))
            drag_state["custom_pos"] = True
            drag_state["win_x"] = nx
            drag_state["win_y"] = ny
            probe_x = rx if (rx <= 240 or rx >= scr_w - 240) else nx
            new_orient = _compute_orientation(probe_x, ny, scr_w, scr_h, True)
            drag_state["dock_right"] = bool(
                new_orient == "vertical" and (rx >= scr_w // 2 or nx + cur_w >= scr_w - 240)
            )
            if drag_state["dock_right"]:
                drag_state["right_x"] = min(scr_w - 4, max(nx + cur_w, rx + 16))
            root.geometry(f"+{nx}+{ny}")

        def _end_drag(event: object, *, click_toggle_home: bool = False) -> None:
            if not drag_state["moved"]:
                if click_toggle_home:
                    self._on_focus_foxprofile()
                return
            scr_w = root.winfo_screenwidth()
            scr_h = root.winfo_screenheight()
            rx = int(getattr(event, "x_root", drag_state["win_x"]))
            ny = int(drag_state["win_y"])
            probe_x = rx if (rx <= 240 or rx >= scr_w - 240) else int(drag_state["win_x"])
            new_orient = _compute_orientation(probe_x, ny, scr_w, scr_h, True)
            if new_orient == "vertical" and rx >= scr_w // 2:
                drag_state["dock_right"] = True
                drag_state["right_x"] = min(scr_w - 4, max(rx + 20, scr_w - 8))
                drag_state["win_x"] = max(4, int(drag_state["right_x"]) - 120)
            elif new_orient == "vertical":
                drag_state["dock_right"] = False
                drag_state["win_x"] = min(int(drag_state["win_x"]), rx)
            else:
                drag_state["dock_right"] = False
            if new_orient != drag_state["orientation"]:
                drag_state["orientation"] = new_orient
                last_rendered[0] = None

        def _bind_drag(widget: object, *, click_toggle_home: bool = False) -> None:
            widget.bind("<ButtonPress-1>", _start_drag)  # type: ignore[attr-defined]
            widget.bind("<B1-Motion>", _on_drag)  # type: ignore[attr-defined]
            widget.bind(  # type: ignore[attr-defined]
                "<ButtonRelease-1>",
                lambda e: _end_drag(e, click_toggle_home=click_toggle_home),
            )

        _bind_drag(outer, click_toggle_home=False)

        def _toggle_collapse() -> None:
            collapsed["value"] = not collapsed["value"]
            last_rendered[0] = None

        def _make_logo(parent: object) -> object:
            if logo_img is not None:
                lbl = tk.Label(
                    parent,
                    image=logo_img,
                    bg="#FFFFFF",
                    bd=0,
                    padx=3,
                    pady=2,
                    cursor="fleur",
                )
                lbl.image = logo_img  # type: ignore[attr-defined]
            else:
                lbl = tk.Label(
                    parent,
                    text="🦊",
                    bg="#FFFFFF",
                    fg="#C2510F",
                    font=("Segoe UI", 11),
                    padx=4,
                    pady=1,
                    cursor="fleur",
                )
            _bind_drag(lbl, click_toggle_home=True)
            return lbl

        def _make_fold_btn(parent: object) -> object:
            fold_frame = tk.Frame(
                parent,
                bg="#F1F5F9",
                highlightbackground="#CBD5E1",
                highlightthickness=1,
            )
            btn_fold = tk.Label(
                fold_frame,
                text="+" if collapsed["value"] else "—",
                bg="#F1F5F9",
                fg="#334155",
                font=("Segoe UI Bold", 8),
                padx=6,
                pady=2,
                cursor="hand2",
            )
            btn_fold.pack()
            btn_fold.bind("<Button-1>", lambda _e: _toggle_collapse())
            return fold_frame

        def _make_tab_pill(
            parent: object,
            pname: str,
            is_active: bool,
            is_loading: bool,
            vertical: bool,
        ) -> object:
            tab_bg = "#5B3FA8" if is_active else "#F8FAFC"
            tab_border = "#4C3294" if is_active else "#CBD5E1"
            tab_fg = "#FFFFFF" if is_active else "#1E293B"
            dot = "⏳" if is_loading else "●"

            tab_frame = tk.Frame(
                parent,
                bg=tab_bg,
                highlightbackground=tab_border,
                highlightthickness=1,
            )

            btn_select = tk.Label(
                tab_frame,
                text=f"{dot} {pname}",
                bg=tab_bg,
                fg=tab_fg,
                font=("Segoe UI Semibold" if is_active else "Segoe UI", 9),
                padx=7,
                pady=2,
                anchor="w" if vertical else "center",
                cursor="hand2",
            )
            btn_select.pack(side="left", fill="x" if vertical else "none", expand=vertical)
            btn_select.bind(
                "<Button-1>",
                lambda _e, n=pname: self._on_select_tab(n),
            )

            btn_close = tk.Label(
                tab_frame,
                text="✕",
                bg=tab_bg,
                fg="#DDD6FE" if is_active else "#94A3B8",
                font=("Segoe UI", 8),
                padx=5,
                pady=2,
                cursor="hand2",
            )
            btn_close.pack(side="right" if vertical else "left")
            btn_close.bind(
                "<Button-1>",
                lambda _e, n=pname: self._on_close_tab(n),
            )
            return tab_frame

        def _make_action_pill(
            parent: object,
            text: str,
            bg: str,
            fg: str,
            border: str,
            on_click: Callable[[], None],
        ) -> object:
            frame = tk.Frame(
                parent,
                bg=bg,
                highlightbackground=border,
                highlightthickness=1,
            )
            lbl = tk.Label(
                frame,
                text=text,
                bg=bg,
                fg=fg,
                font=("Segoe UI Semibold", 8),
                padx=7,
                pady=2,
                cursor="hand2",
            )
            lbl.pack(fill="x", expand=True)
            lbl.bind("<Button-1>", lambda _e: on_click())
            return frame

        def _rebuild(
            running: tuple[str, ...],
            loading: frozenset[str],
            active_tab: str | None,
            single_stage: bool,
        ) -> None:
            for child in outer.winfo_children():
                child.destroy()

            scr_w = root.winfo_screenwidth()
            scr_h = root.winfo_screenheight()
            orient = _compute_orientation(
                int(drag_state["win_x"]),
                int(drag_state["win_y"]),
                scr_w,
                scr_h,
                bool(drag_state["custom_pos"]),
            )
            drag_state["orientation"] = orient
            eff_active = active_tab if active_tab in running else (running[0] if running else None)

            if collapsed["value"]:
                logo_lbl = _make_logo(outer)
                logo_lbl.pack(side="left", padx=(1, 3))  # type: ignore[attr-defined]
                fold_btn = _make_fold_btn(outer)
                fold_btn.pack(side="left", padx=(1, 0))  # type: ignore[attr-defined]
            elif orient == "vertical":
                # Vertical layout (docked on left or right side -> expands downwards)
                header_row = tk.Frame(outer, bg="#FFFFFF")
                header_row.pack(side="top", fill="x", pady=(0, 3))
                _bind_drag(header_row, click_toggle_home=False)

                logo_lbl = _make_logo(header_row)
                logo_lbl.pack(side="left", padx=(1, 2))  # type: ignore[attr-defined]
                fold_btn = _make_fold_btn(header_row)
                fold_btn.pack(side="right", padx=(2, 0))  # type: ignore[attr-defined]

                for pname in running:
                    pill = _make_tab_pill(
                        outer,
                        pname,
                        pname == eff_active,
                        pname in loading,
                        vertical=True,
                    )
                    pill.pack(side="top", fill="x", pady=1)  # type: ignore[attr-defined]

                btn_home = _make_action_pill(
                    outer,
                    get_string("floating_bar_home_btn"),
                    bg="#F8FAFC",
                    fg="#1E293B",
                    border="#CBD5E1",
                    on_click=self._on_focus_foxprofile,
                )
                btn_home.pack(side="top", fill="x", pady=(3, 0))  # type: ignore[attr-defined]
            else:
                # Horizontal layout (top or bottom of screen -> expands horizontally)
                logo_lbl = _make_logo(outer)
                logo_lbl.pack(side="left", padx=(1, 4))  # type: ignore[attr-defined]

                for pname in running:
                    pill = _make_tab_pill(
                        outer,
                        pname,
                        pname == eff_active,
                        pname in loading,
                        vertical=False,
                    )
                    pill.pack(side="left", padx=2)  # type: ignore[attr-defined]

                btn_home = _make_action_pill(
                    outer,
                    get_string("floating_bar_home_btn"),
                    bg="#F8FAFC",
                    fg="#1E293B",
                    border="#CBD5E1",
                    on_click=self._on_focus_foxprofile,
                )
                btn_home.pack(side="left", padx=(5, 2))  # type: ignore[attr-defined]

                fold_btn = _make_fold_btn(outer)
                fold_btn.pack(side="left", padx=(2, 0))  # type: ignore[attr-defined]

            root.update_idletasks()
            req_w = outer.winfo_reqwidth()
            req_h = outer.winfo_reqheight()
            if drag_state["custom_pos"]:
                if drag_state["dock_right"] and int(drag_state["right_x"]) > 0:
                    x = max(4, min(int(drag_state["right_x"]) - req_w, scr_w - req_w - 4))
                else:
                    x = max(4, min(int(drag_state["win_x"]), scr_w - req_w - 4))
                y = max(0, min(int(drag_state["win_y"]), scr_h - req_h - 4))
                drag_state["win_x"] = x
                drag_state["win_y"] = y
            else:
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

            scr_w = root.winfo_screenwidth()
            scr_h = root.winfo_screenheight()
            cur_orient = _compute_orientation(
                int(drag_state["win_x"]),
                int(drag_state["win_y"]),
                scr_w,
                scr_h,
                bool(drag_state["custom_pos"]),
            )
            render_key = (
                running,
                loading,
                active_tab,
                single_stage,
                collapsed["value"],
                cur_orient,
            )
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
