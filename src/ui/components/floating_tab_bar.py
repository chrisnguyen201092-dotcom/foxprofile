"""Always-on-top Monochrome Floating Mini-Tab Bar with per-profile circular Muse Avatars.

Runs in a lightweight daemon thread using Tkinter + Win32 `WS_EX_TOOLWINDOW` + DWM
rounded corners (`DWMWCP_ROUND`) so the user can switch between running Camoufox profile
windows in 1 click without returning to the main FoxProfile window, while keeping every
browser process 100% isolated (`SWP_NOSIZE`).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable

from ...core.strings import get_string
from ...services.avatar_store import (
    auto_capture_muse_avatar_from_active_window,
    avatar_version,
    get_avatar_path,
    get_smooth_fox_logo_path,
    remove_avatar,
    save_avatar_from_clipboard,
    save_avatar_from_file,
    save_avatar_from_screen_rect,
)

_ASSETS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets"))

# FoxProfile Signature Two-Tone Palette (Passport Forest Rail #17332E + Security Paper #F3F4EF)
_BG_RAIL = "#17332E"
_BG_RAIL_DEEP = "#102622"
_FG_RAIL = "#E8EFEA"
_FG_RAIL_MUTED = "#A3BAB0"
_BORDER_RAIL = "#2C524A"

_BG_BAR = "#F3F4EF"
_BORDER_BAR = "#BCC3B6"
_BG_ACTIVE = "#17332E"
_BORDER_ACTIVE = "#17332E"
_FG_ACTIVE = "#E8EFEA"
_CLOSE_ACTIVE = "#A3BAB0"
_CLOSE_ACTIVE_HOVER = "#FFFFFF"

_BG_INACTIVE = "#FBFBF8"
_BG_INACTIVE_HOVER = "#E4EBE2"
_BORDER_INACTIVE = "#BCC3B6"
_FG_INACTIVE = "#1E2422"
_CLOSE_INACTIVE = "#66706A"
_CLOSE_INACTIVE_HOVER = "#B42330"


def _apply_win32_toolwindow(hwnd: int) -> None:
    """Hide floating bar from taskbar/Alt+Tab and enable Windows 11 DWM rounded corners."""
    if sys.platform != "win32" or hwnd <= 0:
        return
    with contextlib.suppress(Exception):
        import ctypes

        user32 = ctypes.windll.user32
        gwl_exstyle = -20
        ws_ex_toolwindow = 0x00000080
        ws_ex_topmost = 0x00000008
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.GetWindowLongW)
        parent = user32.GetParent(hwnd) or hwnd
        cur = int(get_long(parent, gwl_exstyle))
        set_long(parent, gwl_exstyle, cur | ws_ex_toolwindow | ws_ex_topmost)

        with contextlib.suppress(Exception):
            dwmapi = ctypes.windll.dwmapi
            # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
            pref = ctypes.c_int(2)
            dwmapi.DwmSetWindowAttribute(
                parent,
                33,
                ctypes.byref(pref),
                ctypes.sizeof(pref),
            )
            # DWMWA_BORDER_COLOR = 34, COLORREF 0x00BBGGRR (#17332E -> 0x002E3317)
            border_col = ctypes.c_int(0x002E3317)
            dwmapi.DwmSetWindowAttribute(
                parent,
                34,
                ctypes.byref(border_col),
                ctypes.sizeof(border_col),
            )


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
    """Thread-safe controller for the Always-on-Top Monochrome Floating Mini-Tab Bar."""

    def __init__(
        self,
        on_select_tab: Callable[[str], None],
        on_close_tab: Callable[[str], None],
        on_toggle_single_stage: Callable[[], None],
        on_focus_foxprofile: Callable[[], None],
        on_avatar_changed: Callable[[], None] | None = None,
    ) -> None:
        self._on_select_tab = on_select_tab
        self._on_close_tab = on_close_tab
        self._on_toggle_single_stage = on_toggle_single_stage
        self._on_focus_foxprofile = on_focus_foxprofile
        self._on_avatar_changed = on_avatar_changed

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
            from tkinter import filedialog
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
            root.configure(bg=_BG_BAR)

        logo_img = None
        logo_path = os.path.join(_ASSETS_DIR, "logo.png")
        if os.path.isfile(logo_path):
            with contextlib.suppress(Exception):
                smooth_path = get_smooth_fox_logo_path(logo_path, size=26, bg_rgb=(23, 51, 46))
                if smooth_path and os.path.isfile(smooth_path):
                    logo_img = tk.PhotoImage(file=smooth_path)
                else:
                    raw_logo = tk.PhotoImage(file=logo_path)
                    factor = max(1, raw_logo.width() // 26)
                    logo_img = raw_logo.subsample(factor, factor)

        outer = tk.Frame(
            root,
            bg=_BG_BAR,
            highlightbackground=_BG_RAIL,
            highlightthickness=1,
            padx=0,
            pady=0,
        )
        outer.pack(fill="both", expand=True)

        drag_state = {
            "x": 0,
            "y": 0,
            "start_root_x": 0,
            "start_root_y": 0,
            "moved": False,
            "dragging": False,
            "custom_pos": False,
            "win_x": 0,
            "win_y": 0,
            "scr_w": 1920,
            "scr_h": 1080,
            "cur_w": 200,
            "cur_h": 44,
            "top_hwnd": 0,
            "dock_right": False,
            "right_x": 0,
            "orientation": "horizontal",
        }
        collapsed = {"value": False}
        last_rendered: list[object] = [None]
        photo_cache: dict[tuple[str, float], object] = {}
        active_menu: list[object] = [None]
        poll_tick: list[int] = [0]
        last_av_click: dict[str, object] = {"name": "", "ts": 0.0}

        def _close_popup_menu() -> None:
            if active_menu[0] is not None:
                with contextlib.suppress(Exception):
                    active_menu[0].destroy()  # type: ignore[attr-defined]
                active_menu[0] = None

        def _notify_avatar_updated() -> None:
            last_rendered[0] = None
            if self._on_avatar_changed is not None:
                with contextlib.suppress(Exception):
                    self._on_avatar_changed()

        def _get_avatar_photo(pname: str) -> object | None:
            path = get_avatar_path(pname, 22)
            if not path:
                return None
            ver = avatar_version(pname)
            cache_key = (pname, ver)
            cached = photo_cache.get(cache_key)
            if cached is not None:
                return cached
            with contextlib.suppress(Exception):
                img = tk.PhotoImage(file=path)
                photo_cache[cache_key] = img
                return img
            return None

        def _open_screen_snipper(pname: str) -> None:
            """Interactive full-screen crosshair selector to snip the Muse avatar from screen."""
            _close_popup_menu()
            self._on_select_tab(pname)

            def _launch_overlay() -> None:
                snip = tk.Toplevel(root)
                snip.overrideredirect(True)
                scr_w = root.winfo_screenwidth()
                scr_h = root.winfo_screenheight()
                snip.geometry(f"{scr_w}x{scr_h}+0+0")
                with contextlib.suppress(Exception):
                    snip.attributes("-topmost", True)
                    snip.attributes("-alpha", 0.28)
                snip.configure(bg="#09090B", cursor="crosshair")

                canvas = tk.Canvas(
                    snip,
                    bg="#09090B",
                    highlightthickness=0,
                    cursor="crosshair",
                )
                canvas.pack(fill="both", expand=True)

                # Top instruction banner
                canvas.create_rectangle(
                    scr_w // 2 - 310,
                    16,
                    scr_w // 2 + 310,
                    52,
                    fill="#000000",
                    outline="#FFFFFF",
                    width=1,
                )
                canvas.create_text(
                    scr_w // 2,
                    34,
                    text=get_string("avatar_snip_hint", name=pname),
                    fill="#FFFFFF",
                    font=("Segoe UI Semibold", 10),
                )

                coords = {"x1": 0, "y1": 0, "rect": None, "oval": None}

                def _cancel(_e: object = None) -> None:
                    with contextlib.suppress(Exception):
                        snip.destroy()

                def _on_press(e: object) -> None:
                    coords["x1"] = int(getattr(e, "x_root", 0))
                    coords["y1"] = int(getattr(e, "y_root", 0))
                    if coords["rect"]:
                        canvas.delete(coords["rect"])
                    if coords["oval"]:
                        canvas.delete(coords["oval"])
                    coords["rect"] = canvas.create_rectangle(
                        coords["x1"],
                        coords["y1"],
                        coords["x1"],
                        coords["y1"],
                        outline="#FFFFFF",
                        width=2,
                        dash=(4, 2),
                    )
                    coords["oval"] = canvas.create_oval(
                        coords["x1"],
                        coords["y1"],
                        coords["x1"],
                        coords["y1"],
                        outline="#FFFFFF",
                        width=2,
                    )

                def _on_motion(e: object) -> None:
                    if not coords["rect"]:
                        return
                    x2 = int(getattr(e, "x_root", 0))
                    y2 = int(getattr(e, "y_root", 0))
                    canvas.coords(coords["rect"], coords["x1"], coords["y1"], x2, y2)
                    if coords["oval"]:
                        canvas.coords(coords["oval"], coords["x1"], coords["y1"], x2, y2)

                def _on_release(e: object) -> None:
                    x1 = int(coords["x1"])
                    y1 = int(coords["y1"])
                    x2 = int(getattr(e, "x_root", 0))
                    y2 = int(getattr(e, "y_root", 0))
                    with contextlib.suppress(Exception):
                        snip.destroy()
                    root.update_idletasks()
                    if (
                        abs(x2 - x1) >= 10
                        and abs(y2 - y1) >= 10
                        and save_avatar_from_screen_rect(pname, x1, y1, x2, y2)
                    ):
                        _notify_avatar_updated()

                canvas.bind("<ButtonPress-1>", _on_press)
                canvas.bind("<B1-Motion>", _on_motion)
                canvas.bind("<ButtonRelease-1>", _on_release)
                canvas.bind("<Button-3>", _cancel)
                snip.bind("<Escape>", _cancel)
                snip.focus_force()

            root.after(120, _launch_overlay)

        def _open_avatar_menu(event: object, pname: str) -> None:
            """Show FoxProfile-themed popup menu to set/change `pname`'s circular avatar."""
            _close_popup_menu()
            rx = int(getattr(event, "x_root", root.winfo_x()))
            ry = int(getattr(event, "y_root", root.winfo_y() + root.winfo_height()))

            pop = tk.Toplevel(root)
            pop.overrideredirect(True)
            with contextlib.suppress(Exception):
                pop.attributes("-topmost", True)
            pop.configure(bg=_BG_RAIL)
            active_menu[0] = pop

            box = tk.Frame(
                pop,
                bg=_BG_RAIL,
                highlightbackground=_BORDER_RAIL,
                highlightthickness=1,
                padx=4,
                pady=4,
            )
            box.pack(fill="both", expand=True)

            hdr = tk.Label(
                box,
                text=f"Avatar: {pname}",
                bg=_BG_RAIL,
                fg="#9BB8B0",
                font=("Segoe UI Semibold", 8),
                anchor="w",
                padx=8,
                pady=3,
            )
            hdr.pack(fill="x")

            def _add_item(label: str, action: Callable[[], None], *, danger: bool = False) -> None:
                fg_norm = "#FF8080" if danger else _FG_RAIL
                item = tk.Label(
                    box,
                    text=label,
                    bg=_BG_RAIL,
                    fg=fg_norm,
                    font=("Segoe UI", 9),
                    anchor="w",
                    padx=10,
                    pady=5,
                    cursor="hand2",
                )
                item.pack(fill="x")
                item.bind("<Enter>", lambda _e: item.configure(bg="#20433D"))
                item.bind("<Leave>", lambda _e: item.configure(bg=_BG_RAIL))
                item.bind("<Button-1>", lambda _e: (_close_popup_menu(), action()))

            _add_item(
                get_string("avatar_menu_snip"),
                lambda: _open_screen_snipper(pname),
            )

            def _do_paste() -> None:
                if save_avatar_from_clipboard(pname):
                    _notify_avatar_updated()

            _add_item(get_string("avatar_menu_paste"), _do_paste)

            def _do_file() -> None:
                fpath = filedialog.askopenfilename(
                    parent=root,
                    title=f"Avatar - {pname}",
                    filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp *.gif")],
                )
                if fpath and save_avatar_from_file(pname, fpath):
                    _notify_avatar_updated()

            _add_item(get_string("avatar_menu_file"), _do_file)

            if get_avatar_path(pname, 22):
                def _do_remove() -> None:
                    if remove_avatar(pname):
                        _notify_avatar_updated()

                _add_item(get_string("avatar_menu_remove"), _do_remove, danger=True)

            pop.update_idletasks()
            pw = pop.winfo_reqwidth()
            ph = pop.winfo_reqheight()
            scr_w = root.winfo_screenwidth()
            scr_h = root.winfo_screenheight()
            px = max(6, min(rx, scr_w - pw - 6))
            py = max(6, min(ry + 8, scr_h - ph - 6))
            pop.geometry(f"{pw}x{ph}+{px}+{py}")
            _apply_win32_toolwindow(pop.winfo_id())
            pop.bind("<FocusOut>", lambda _e: _close_popup_menu())

        win32_user32 = None
        if sys.platform == "win32":
            with contextlib.suppress(Exception):
                import ctypes

                win32_user32 = ctypes.windll.user32

        def _start_drag(event: object) -> None:
            _close_popup_menu()
            rx = int(getattr(event, "x_root", 0))
            ry = int(getattr(event, "y_root", 0))
            drag_state["start_root_x"] = rx
            drag_state["start_root_y"] = ry
            drag_state["moved"] = False
            drag_state["dragging"] = True
            drag_state["scr_w"] = root.winfo_screenwidth()
            drag_state["scr_h"] = root.winfo_screenheight()
            drag_state["cur_w"] = max(40, root.winfo_width())
            drag_state["cur_h"] = max(28, root.winfo_height())
            drag_state["x"] = rx - root.winfo_x()
            drag_state["y"] = ry - root.winfo_y()
            if win32_user32 is not None:
                with contextlib.suppress(Exception):
                    hwnd = root.winfo_id()
                    drag_state["top_hwnd"] = int(win32_user32.GetParent(hwnd) or hwnd)

        def _on_drag(event: object) -> None:
            rx = int(getattr(event, "x_root", 0))
            ry = int(getattr(event, "y_root", 0))
            if (
                not drag_state["moved"]
                and abs(rx - int(drag_state["start_root_x"]))
                + abs(ry - int(drag_state["start_root_y"]))
                > 3
            ):
                drag_state["moved"] = True
            scr_w = int(drag_state["scr_w"])
            scr_h = int(drag_state["scr_h"])
            cur_w = int(drag_state["cur_w"])
            cur_h = int(drag_state["cur_h"])
            nx = max(0, min(rx - int(drag_state["x"]), scr_w - cur_w))
            ny = max(0, min(ry - int(drag_state["y"]), scr_h - cur_h))
            drag_state["custom_pos"] = True
            drag_state["win_x"] = nx
            drag_state["win_y"] = ny
            top_hwnd = int(drag_state["top_hwnd"])
            if win32_user32 is not None and top_hwnd:
                # SWP_NOSIZE (0x0001) | SWP_NOACTIVATE (0x0010) | SWP_NOOWNERZORDER (0x0200)
                win32_user32.SetWindowPos(top_hwnd, -1, nx, ny, 0, 0, 0x0211)
            else:
                root.geometry(f"+{nx}+{ny}")

        def _end_drag(_event: object, *, click_toggle_home: bool = False) -> None:
            drag_state["dragging"] = False
            if not drag_state["moved"]:
                if click_toggle_home:
                    self._on_focus_foxprofile()
                return
            nx = int(drag_state["win_x"])
            ny = int(drag_state["win_y"])
            root.geometry(f"+{nx}+{ny}")

        def _bind_drag(widget: object, *, click_toggle_home: bool = False) -> None:
            widget.bind("<ButtonPress-1>", _start_drag)  # type: ignore[attr-defined]
            widget.bind("<B1-Motion>", _on_drag)  # type: ignore[attr-defined]
            widget.bind(  # type: ignore[attr-defined]
                "<ButtonRelease-1>",
                lambda e: _end_drag(e, click_toggle_home=click_toggle_home),
            )

        _bind_drag(outer, click_toggle_home=False)

        def _toggle_collapse() -> None:
            _close_popup_menu()
            collapsed["value"] = not collapsed["value"]
            last_rendered[0] = None

        def _make_logo(parent: object) -> object:
            rail = tk.Frame(
                parent,
                bg=_BG_RAIL,
                padx=12,
                pady=8,
                cursor="fleur",
            )
            if logo_img is not None:
                ic = tk.Label(
                    rail,
                    image=logo_img,
                    bg=_BG_RAIL,
                    bd=0,
                    padx=0,
                    pady=0,
                    cursor="fleur",
                )
                ic.image = logo_img  # type: ignore[attr-defined]
                ic.pack(side="left", padx=(0, 7))
                _bind_drag(ic, click_toggle_home=True)

            title_lbl = tk.Label(
                rail,
                text="FoxProfile",
                bg=_BG_RAIL,
                fg=_FG_RAIL,
                font=("Segoe UI Bold", 10),
                padx=0,
                pady=1,
                cursor="fleur",
            )
            title_lbl.pack(side="left")
            _bind_drag(rail, click_toggle_home=True)
            _bind_drag(title_lbl, click_toggle_home=True)
            return rail

        def _make_fold_btn(parent: object, *, on_rail: bool = False) -> object:
            bg_norm = _BG_RAIL_DEEP if on_rail else "#E9ECE4"
            bg_hov = "#20433D" if on_rail else "#D5DACF"
            fg_norm = _FG_RAIL if on_rail else _BG_RAIL
            border_c = _BORDER_RAIL if on_rail else _BORDER_INACTIVE
            fold_frame = tk.Frame(
                parent,
                bg=bg_norm,
                highlightbackground=border_c,
                highlightthickness=1,
            )
            btn_fold = tk.Label(
                fold_frame,
                text="+" if collapsed["value"] else "—",
                bg=bg_norm,
                fg=fg_norm,
                font=("Segoe UI Bold", 9),
                padx=9,
                pady=4,
                cursor="hand2",
            )
            btn_fold.pack()
            btn_fold.bind("<Enter>", lambda _e: btn_fold.configure(bg=bg_hov))
            btn_fold.bind("<Leave>", lambda _e: btn_fold.configure(bg=bg_norm))
            btn_fold.bind("<Button-1>", lambda _e: _toggle_collapse())
            return fold_frame

        def _make_tab_pill(
            parent: object,
            pname: str,
            is_active: bool,
            is_loading: bool,
            vertical: bool,
        ) -> object:
            tab_bg = _BG_ACTIVE if is_active else _BG_INACTIVE
            tab_border = _BORDER_ACTIVE if is_active else _BORDER_INACTIVE
            tab_fg = _FG_ACTIVE if is_active else _FG_INACTIVE
            close_fg = _CLOSE_ACTIVE if is_active else _CLOSE_INACTIVE
            close_hover_fg = _CLOSE_ACTIVE_HOVER if is_active else _CLOSE_INACTIVE_HOVER

            tab_frame = tk.Frame(
                parent,
                bg=tab_bg,
                highlightbackground=tab_border,
                highlightthickness=1,
                padx=5,
                pady=3,
            )

            # Left Circular Muse Avatar (or subtle avatar placeholder)
            avatar_img = _get_avatar_photo(pname)
            if avatar_img is not None:
                av_lbl = tk.Label(
                    tab_frame,
                    image=avatar_img,
                    bg=tab_bg,
                    bd=0,
                    padx=3,
                    pady=2,
                    cursor="hand2",
                )
                av_lbl.image = avatar_img  # type: ignore[attr-defined]
            else:
                badge_txt = "⏳" if is_loading else "⊕"
                av_lbl = tk.Label(
                    tab_frame,
                    text=badge_txt,
                    bg=tab_bg,
                    fg=_FG_ACTIVE if is_active else "#5E6B64",
                    font=("Segoe UI Semibold", 10),
                    padx=5,
                    pady=2,
                    cursor="hand2",
                )
            def _on_avatar_click(event: object, n: str = pname) -> None:
                now = time.monotonic()
                prev_ts = float(last_av_click.get("ts", 0.0))  # type: ignore[arg-type]
                if last_av_click.get("name") == n and (now - prev_ts) <= 0.45:
                    last_av_click["ts"] = 0.0
                    _open_avatar_menu(event, n)
                    return
                last_av_click["name"] = n
                last_av_click["ts"] = now
                _close_popup_menu()
                self._on_select_tab(n)

            def _on_avatar_double_click(event: object, n: str = pname) -> None:
                last_av_click["ts"] = 0.0
                if active_menu[0] is None:
                    _open_avatar_menu(event, n)

            av_lbl.pack(side="left", padx=(2, 2))
            av_lbl.bind("<Button-1>", _on_avatar_click)
            av_lbl.bind("<Double-Button-1>", _on_avatar_double_click)

            btn_select = tk.Label(
                tab_frame,
                text=pname,
                bg=tab_bg,
                fg=tab_fg,
                font=("Segoe UI Semibold" if is_active else "Segoe UI", 9),
                padx=7,
                pady=4,
                anchor="w" if vertical else "center",
                cursor="hand2",
            )
            btn_select.pack(side="left", fill="x" if vertical else "none", expand=vertical)
            btn_select.bind(
                "<Button-1>",
                lambda _e, n=pname: (_close_popup_menu(), self._on_select_tab(n)),
            )

            btn_close = tk.Label(
                tab_frame,
                text="✕",
                bg=tab_bg,
                fg=close_fg,
                font=("Segoe UI", 9),
                padx=6,
                pady=4,
                cursor="hand2",
            )
            btn_close.pack(side="right" if vertical else "left")
            btn_close.bind("<Enter>", lambda _e: btn_close.configure(fg=close_hover_fg))
            btn_close.bind("<Leave>", lambda _e: btn_close.configure(fg=close_fg))
            btn_close.bind(
                "<Button-1>",
                lambda _e, n=pname: (_close_popup_menu(), self._on_close_tab(n)),
            )

            if not is_active:
                def _on_enter(_e: object) -> None:
                    tab_frame.configure(bg=_BG_INACTIVE_HOVER, highlightbackground="#9BA89E")
                    av_lbl.configure(bg=_BG_INACTIVE_HOVER)
                    btn_select.configure(bg=_BG_INACTIVE_HOVER, fg=_BG_RAIL)
                    btn_close.configure(bg=_BG_INACTIVE_HOVER)

                def _on_leave(_e: object) -> None:
                    tab_frame.configure(bg=_BG_INACTIVE, highlightbackground=_BORDER_INACTIVE)
                    av_lbl.configure(bg=_BG_INACTIVE)
                    btn_select.configure(bg=_BG_INACTIVE, fg=_FG_INACTIVE)
                    btn_close.configure(bg=_BG_INACTIVE)

                tab_frame.bind("<Enter>", _on_enter)
                tab_frame.bind("<Leave>", _on_leave)
                btn_select.bind("<Enter>", _on_enter)
                btn_select.bind("<Leave>", _on_leave)

            return tab_frame

        def _rebuild(
            running: tuple[str, ...],
            loading: frozenset[str],
            active_tab: str | None,
            _single_stage: bool,
        ) -> None:
            for child in outer.winfo_children():
                child.destroy()

            scr_w = root.winfo_screenwidth()
            scr_h = root.winfo_screenheight()
            orient = "horizontal"
            drag_state["orientation"] = orient
            eff_active = active_tab if active_tab in running else (running[0] if running else None)

            if collapsed["value"]:
                logo_lbl = _make_logo(outer)
                logo_lbl.pack(side="left", fill="y")  # type: ignore[attr-defined]
                sep = tk.Frame(outer, bg=_BORDER_INACTIVE, width=1)
                sep.pack(side="left", fill="y")
                body = tk.Frame(outer, bg=_BG_BAR, padx=7, pady=6)
                body.pack(side="left", fill="both", expand=True)
                _bind_drag(body, click_toggle_home=False)
                fold_btn = _make_fold_btn(body, on_rail=False)
                fold_btn.pack(side="left")  # type: ignore[attr-defined]
            elif orient == "vertical":
                header_row = tk.Frame(outer, bg=_BG_RAIL, padx=5, pady=4)
                header_row.pack(side="top", fill="x")
                _bind_drag(header_row, click_toggle_home=False)

                logo_lbl = _make_logo(header_row)
                logo_lbl.pack(side="left")  # type: ignore[attr-defined]
                fold_btn = _make_fold_btn(header_row, on_rail=True)
                fold_btn.pack(side="right", padx=(4, 2))  # type: ignore[attr-defined]

                sep = tk.Frame(outer, bg=_BORDER_INACTIVE, height=1)
                sep.pack(side="top", fill="x")

                body = tk.Frame(outer, bg=_BG_BAR, padx=7, pady=6)
                body.pack(side="top", fill="both", expand=True)
                _bind_drag(body, click_toggle_home=False)

                for pname in running:
                    pill = _make_tab_pill(
                        body,
                        pname,
                        pname == eff_active,
                        pname in loading,
                        vertical=True,
                    )
                    pill.pack(side="top", fill="x", pady=3)  # type: ignore[attr-defined]
            else:
                logo_lbl = _make_logo(outer)
                logo_lbl.pack(side="left", fill="y")  # type: ignore[attr-defined]

                sep = tk.Frame(outer, bg=_BORDER_INACTIVE, width=1)
                sep.pack(side="left", fill="y")

                body = tk.Frame(outer, bg=_BG_BAR, padx=8, pady=6)
                body.pack(side="left", fill="both", expand=True)
                _bind_drag(body, click_toggle_home=False)

                for pname in running:
                    pill = _make_tab_pill(
                        body,
                        pname,
                        pname == eff_active,
                        pname in loading,
                        vertical=False,
                    )
                    pill.pack(side="left", padx=3)  # type: ignore[attr-defined]

                fold_btn = _make_fold_btn(body, on_rail=False)
                fold_btn.pack(side="left", padx=(5, 0))  # type: ignore[attr-defined]

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
                y = 4
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
                    _close_popup_menu()
                    root.withdraw()
                    last_rendered[0] = "hidden"
                root.after(150, _poll)
                return

            if drag_state["dragging"]:
                root.after(80, _poll)
                return

            poll_tick[0] += 1
            eff_cur = active_tab if active_tab in running else (running[0] if running else None)
            if (
                eff_cur
                and poll_tick[0] % 10 == 0
                and get_avatar_path(eff_cur, 22) is None
                and auto_capture_muse_avatar_from_active_window(eff_cur)
            ):
                _notify_avatar_updated()

            cur_orient = "horizontal"
            av_sig = tuple(avatar_version(n) for n in running)
            render_key = (
                running,
                loading,
                active_tab,
                single_stage,
                collapsed["value"],
                cur_orient,
                av_sig,
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
