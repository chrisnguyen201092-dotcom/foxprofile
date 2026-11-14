"""Always-on-top Monochrome Floating Mini-Tab Bar with per-profile circular Muse Avatars.

Runs in a lightweight daemon thread using Tkinter + Win32 `WS_EX_TOOLWINDOW` + DWM
rounded corners (`DWMWCP_ROUND`) so the user can switch between running Camoufox profile
windows in 1 click without returning to the main FoxProfile window, while keeping every
browser process 100% isolated (`SWP_NOSIZE`).
"""

from __future__ import annotations

import contextlib
import gc
import os
import sys
import threading
import time
from collections.abc import Callable

from ...core.strings import get_string
from ...services.avatar_store import (
    auto_capture_muse_avatar_from_active_window,
    avatar_version,
    capture_desktop_freeze_frame,
    detect_foreground_running_profile,
    get_avatar_path,
    get_smooth_fox_logo_path,
    get_virtual_screen_metrics,
    logical_rect_to_physical_rect,
    remove_avatar,
    save_avatar_from_bgra_crop,
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

_TABBAR_WIN32_CACHE: tuple[object, object] | None = None


def _tabbar_win32() -> tuple[object | None, object | None]:
    """Return isolated 64-bit-prototyped `(user32, dwmapi)` handles for FoxTab."""
    global _TABBAR_WIN32_CACHE
    if sys.platform != "win32":
        return (None, None)
    if _TABBAR_WIN32_CACHE is not None:
        return _TABBAR_WIN32_CACHE  # type: ignore[return-value]
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND

        if hasattr(user32, "GetWindowLongPtrW"):
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long

        if hasattr(user32, "SetWindowLongPtrW"):
            user32.SetWindowLongPtrW.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_ssize_t,
            ]
            user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long

        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short

        dwmapi = None
        with contextlib.suppress(Exception):
            dwmapi = ctypes.WinDLL("dwmapi")
            dwmapi.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

        _TABBAR_WIN32_CACHE = (user32, dwmapi)
        return (user32, dwmapi)
    except Exception:
        return (None, None)


def _apply_win32_toolwindow(
    hwnd: int,
    *,
    no_activate: bool = True,
    round_corners: bool = True,
) -> int:
    """Hide floating bar from taskbar/Alt+Tab, enforce TOPMOST, and optionally apply DWM corners."""
    if sys.platform != "win32" or hwnd <= 0:
        return 0
    user32, dwmapi = _tabbar_win32()
    if user32 is None:
        return 0
    with contextlib.suppress(Exception):
        import ctypes
        from ctypes import wintypes

        gwl_exstyle = -20
        ws_ex_toolwindow = 0x00000080
        ws_ex_topmost = 0x00000008
        ws_ex_noactivate = 0x08000000
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        parent = int(user32.GetParent(wintypes.HWND(hwnd)) or hwnd)
        parent_hwnd = wintypes.HWND(parent)
        cur = int(get_long(parent_hwnd, gwl_exstyle))
        new_ex = cur | ws_ex_toolwindow | ws_ex_topmost
        if no_activate:
            new_ex |= ws_ex_noactivate
        else:
            new_ex &= ~ws_ex_noactivate
        set_long(parent_hwnd, gwl_exstyle, new_ex)
        # Flush cached Win32 extended styles and enforce TOPMOST without activating or resizing
        # (SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_FRAMECHANGED = 0x0033)
        user32.SetWindowPos(parent_hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0033)

        if round_corners and dwmapi is not None:
            with contextlib.suppress(Exception):
                # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
                pref = ctypes.c_int(2)
                dwmapi.DwmSetWindowAttribute(
                    parent_hwnd,
                    33,
                    ctypes.byref(pref),
                    ctypes.sizeof(pref),
                )
                # DWMWA_BORDER_COLOR = 34, COLORREF 0x00BBGGRR (#17332E -> 0x002E3317)
                border_col = ctypes.c_int(0x002E3317)
                dwmapi.DwmSetWindowAttribute(
                    parent_hwnd,
                    34,
                    ctypes.byref(border_col),
                    ctypes.sizeof(border_col),
                )
        return parent
    return 0


def _compute_orientation(
    win_x: int,
    win_y: int,
    scr_w: int,
    scr_h: int,
    custom_pos: bool,
    win_w: int = 0,
) -> str:
    """Return 'vertical' when placed on left/right sides, 'horizontal' when near top/bottom/center."""
    if not custom_pos or scr_w <= 0 or scr_h <= 0:
        return "horizontal"
    right_edge = win_x + max(0, win_w)
    if win_x <= 260 or win_x >= scr_w - 300 or (win_w > 0 and right_edge >= scr_w - 220):
        return "vertical"
    if win_y <= 80 or win_y >= scr_h - 100:
        return "horizontal"
    dist_x = min(max(0, win_x), max(0, scr_w - max(win_x, right_edge)))
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
        self._pending_snip: tuple[str, int] | None = None

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
                (self._thread is None or not self._thread.is_alive())
                and bool(running_names)
                and bool(enabled)
                and sys.platform == "win32"
                and "PYTEST_CURRENT_TEST" not in os.environ
            )
            if should_start:
                self._stop_flag = False
                self._thread = threading.Thread(
                    target=self._run_tk_loop,
                    name="FoxTabFloatingBar",
                    daemon=True,
                )
                self._thread.start()

    def request_screen_snip(self, profile_name: str, *, delay_sec: int = 0) -> None:
        """Thread-safely queue opening the interactive screen snipper for `profile_name`."""
        with self._lock:
            self._pending_snip = (profile_name, max(0, int(delay_sec)))

    def close(self) -> None:
        with self._lock:
            self._stop_flag = True

    def _run_tk_loop(self) -> None:
        try:
            self._run_tk_loop_impl()
        finally:
            # Break closure reference cycles while still on the Tk thread so Tcl/Tk
            # C handles are freed here instead of main-thread shutdown (Tcl_AsyncDelete).
            gc.collect()

    def _run_tk_loop_impl(self) -> None:
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
                    del raw_logo

        logo_holder: list[object] = [logo_img]
        del logo_img
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
        active_menu_bounds: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0]
        snip_active: list[bool] = [False]
        snip_phase: list[str] = [""]
        active_snip_win: list[object] = [None]
        snip_cancel_cb: list[Callable[[], None] | None] = [None]
        snip_refreeze_cb: list[Callable[[], None] | None] = [None]
        snip_opened_at: list[float] = [0.0]
        file_dialog_active: list[bool] = [False]
        poll_tick: list[int] = [0]
        last_av_click: dict[str, object] = {"name": "", "ts": 0.0, "after_id": None}

        win32_user32, _win32_dwm = _tabbar_win32()
        win32_hwnd_type = None
        if sys.platform == "win32":
            with contextlib.suppress(Exception):
                from ctypes import wintypes

                win32_hwnd_type = wintypes.HWND

        def _clear_snip_state() -> None:
            snip_active[0] = False
            snip_phase[0] = ""
            active_snip_win[0] = None
            snip_cancel_cb[0] = None
            snip_refreeze_cb[0] = None
            last_rendered[0] = None

        def _abort_active_snip() -> None:
            cb = snip_cancel_cb[0]
            win = active_snip_win[0]
            snip_cancel_cb[0] = None
            snip_refreeze_cb[0] = None
            snip_phase[0] = ""
            if cb is not None:
                with contextlib.suppress(Exception):
                    cb()
            if win is not None:
                with contextlib.suppress(Exception):
                    if win.winfo_exists():  # type: ignore[attr-defined]
                        win.destroy()  # type: ignore[attr-defined]
            _clear_snip_state()

        def _cancel_pending_tab_select() -> None:
            aid = last_av_click.get("after_id")
            if aid is not None:
                with contextlib.suppress(Exception):
                    root.after_cancel(aid)  # type: ignore[arg-type]
                last_av_click["after_id"] = None

        def _close_popup_menu() -> None:
            if active_menu[0] is not None:
                with contextlib.suppress(Exception):
                    active_menu[0].destroy()  # type: ignore[attr-defined]
                active_menu[0] = None
                active_menu_bounds[:] = [0.0, 0.0, 0.0, 0.0, 0.0]

        def _notify_avatar_updated() -> None:
            photo_cache.clear()
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

        def _open_screen_snipper(pname: str, *, delay_sec: int = 0) -> None:
            """Interactive freeze-frame crosshair selector to snip the Muse avatar accurately."""
            _cancel_pending_tab_select()
            _close_popup_menu()
            snip_active[0] = True
            snip_phase[0] = "delay"
            snip_opened_at[0] = time.monotonic()
            active_snip_win[0] = None
            snip_cancel_cb[0] = None
            snip_refreeze_cb[0] = None
            with self._lock:
                running_snap = self._state[0]
            fg_prof = detect_foreground_running_profile(running_snap)

            def _restore_bar_after_snip() -> None:
                _clear_snip_state()

            def _show_snip_canvas(frame: object | None) -> None:
                if not snip_active[0]:
                    return
                if frame is not None:
                    vx = int(getattr(frame, "log_x", 0))
                    vy = int(getattr(frame, "log_y", 0))
                    vw = int(getattr(frame, "log_w", root.winfo_screenwidth()))
                    vh = int(getattr(frame, "log_h", root.winfo_screenheight()))
                else:
                    vx, vy, vw, vh = get_virtual_screen_metrics(physical=False)

                try:
                    snip = tk.Toplevel(root)
                    snip.withdraw()
                    snip.overrideredirect(True)
                    snip.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
                    with contextlib.suppress(Exception):
                        snip.attributes("-topmost", True)
                        if frame is None:
                            snip.attributes("-alpha", 0.28)
                    snip.configure(bg="#09090B", cursor="crosshair")
                except Exception:
                    _restore_bar_after_snip()
                    return

                canvas = tk.Canvas(
                    snip,
                    bg="#09090B",
                    highlightthickness=0,
                    cursor="crosshair",
                )
                canvas.pack(fill="both", expand=True)

                if frame is not None and getattr(frame, "dimmed_ppm", b""):
                    with contextlib.suppress(Exception):
                        freeze_photo = tk.PhotoImage(data=frame.dimmed_ppm)  # type: ignore[attr-defined]
                        canvas.create_image(0, 0, anchor="nw", image=freeze_photo)
                        canvas._freeze_photo = freeze_photo  # type: ignore[attr-defined]

                banner_cx = max(340, min(vw - 340, (-vx) + root.winfo_screenwidth() // 2))
                canvas.create_rectangle(
                    banner_cx - 330,
                    16,
                    banner_cx + 330,
                    54,
                    fill="#102622",
                    outline="#FFFFFF",
                    width=1,
                )
                hint_id = canvas.create_text(
                    banner_cx,
                    35,
                    text=get_string("avatar_snip_hint", name=pname),
                    fill="#FFFFFF",
                    font=("Segoe UI Semibold", 10),
                )

                coords: dict[str, object] = {
                    "x1": 0,
                    "y1": 0,
                    "rect": None,
                    "oval": None,
                    "dragging": False,
                }

                def _clear_selection_shapes() -> None:
                    if coords["rect"]:
                        canvas.delete(coords["rect"])
                        coords["rect"] = None
                    if coords["oval"]:
                        canvas.delete(coords["oval"])
                        coords["oval"] = None
                    coords["dragging"] = False

                def _cancel(_e: object = None) -> None:
                    snip_phase[0] = ""
                    snip_cancel_cb[0] = None
                    snip_refreeze_cb[0] = None
                    active_snip_win[0] = None
                    with contextlib.suppress(Exception):
                        canvas._freeze_photo = None  # type: ignore[attr-defined]
                        snip.destroy()
                    _restore_bar_after_snip()

                def _re_freeze(_e: object = None) -> None:
                    snip_phase[0] = "delay"
                    snip_cancel_cb[0] = None
                    snip_refreeze_cb[0] = None
                    active_snip_win[0] = None
                    with contextlib.suppress(Exception):
                        canvas._freeze_photo = None  # type: ignore[attr-defined]
                        snip.destroy()
                    _open_screen_snipper(pname, delay_sec=3)

                def _on_snip_destroy(ev: object) -> None:
                    if getattr(ev, "widget", None) is snip and snip_phase[0] == "canvas":
                        _restore_bar_after_snip()

                def _on_press(e: object) -> None:
                    _clear_selection_shapes()
                    cx = int(getattr(e, "x_root", 0)) - vx
                    cy = int(getattr(e, "y_root", 0)) - vy
                    coords["x1"] = cx
                    coords["y1"] = cy
                    coords["dragging"] = True
                    coords["rect"] = canvas.create_rectangle(
                        cx,
                        cy,
                        cx,
                        cy,
                        outline="#FFFFFF",
                        width=2,
                        dash=(4, 2),
                    )
                    coords["oval"] = canvas.create_oval(
                        cx,
                        cy,
                        cx,
                        cy,
                        outline="#FFFFFF",
                        width=2,
                    )

                def _on_motion(e: object) -> None:
                    if not coords["dragging"] or not coords["rect"]:
                        return
                    x1 = int(coords["x1"])  # type: ignore[arg-type]
                    y1 = int(coords["y1"])  # type: ignore[arg-type]
                    x2 = int(getattr(e, "x_root", 0)) - vx
                    y2 = int(getattr(e, "y_root", 0)) - vy
                    canvas.coords(coords["rect"], x1, y1, x2, y2)
                    if coords["oval"]:
                        left, right = min(x1, x2), max(x1, x2)
                        top, bottom = min(y1, y2), max(y1, y2)
                        w = right - left
                        h = bottom - top
                        side = min(w, h)
                        cx1 = left + (w - side) // 2
                        cy1 = top + (h - side) // 2
                        canvas.coords(coords["oval"], cx1, cy1, cx1 + side, cy1 + side)

                def _on_release(e: object) -> None:
                    if not coords["dragging"]:
                        return
                    coords["dragging"] = False
                    x1 = int(coords["x1"])  # type: ignore[arg-type]
                    y1 = int(coords["y1"])  # type: ignore[arg-type]
                    x2 = int(getattr(e, "x_root", 0)) - vx
                    y2 = int(getattr(e, "y_root", 0)) - vy
                    if abs(x2 - x1) < 10 or abs(y2 - y1) < 10:
                        _clear_selection_shapes()
                        return

                    if frame is not None:
                        px1, py1, px2, py2 = logical_rect_to_physical_rect(
                            vx + x1,
                            vy + y1,
                            vx + x2,
                            vy + y2,
                            logical_bounds=(
                                int(getattr(frame, "log_x", vx)),
                                int(getattr(frame, "log_y", vy)),
                                int(getattr(frame, "log_w", vw)),
                                int(getattr(frame, "log_h", vh)),
                            ),
                            physical_bounds=(
                                int(getattr(frame, "phys_x", 0)),
                                int(getattr(frame, "phys_y", 0)),
                                int(getattr(frame, "phys_w", vw)),
                                int(getattr(frame, "phys_h", vh)),
                            ),
                        )
                        phys_x = int(getattr(frame, "phys_x", 0))
                        phys_y = int(getattr(frame, "phys_y", 0))
                        phys_w = int(getattr(frame, "phys_w", vw))
                        phys_h = int(getattr(frame, "phys_h", vh))
                        ok = save_avatar_from_bgra_crop(
                            pname,
                            getattr(frame, "raw_bgra", b""),
                            phys_w,
                            phys_h,
                            px1 - phys_x,
                            py1 - phys_y,
                            px2 - phys_x,
                            py2 - phys_y,
                            reject_uniform=True,
                        )
                        if ok:
                            snip_phase[0] = ""
                            snip_cancel_cb[0] = None
                            snip_refreeze_cb[0] = None
                            active_snip_win[0] = None
                            with contextlib.suppress(Exception):
                                canvas._freeze_photo = None  # type: ignore[attr-defined]
                                snip.destroy()
                            _restore_bar_after_snip()
                            _notify_avatar_updated()
                        else:
                            if coords["rect"]:
                                canvas.itemconfigure(coords["rect"], outline="#FF6B6B")
                            if coords["oval"]:
                                canvas.itemconfigure(coords["oval"], outline="#FF6B6B")
                            canvas.itemconfigure(
                                hint_id,
                                text=get_string("avatar_snip_retry", name=pname),
                                fill="#FFB4B4",
                            )
                        return

                    snip_phase[0] = "capturing"
                    snip_opened_at[0] = time.monotonic()
                    snip_cancel_cb[0] = None
                    snip_refreeze_cb[0] = None
                    active_snip_win[0] = None
                    with contextlib.suppress(Exception):
                        canvas.delete("all")
                        snip.withdraw()
                        snip.destroy()
                    with contextlib.suppress(Exception):
                        root.update_idletasks()
                        root.update()

                    def _do_capture() -> None:
                        try:
                            if save_avatar_from_screen_rect(
                                pname,
                                vx + x1,
                                vy + y1,
                                vx + x2,
                                vy + y2,
                                reject_uniform=True,
                            ):
                                _notify_avatar_updated()
                        finally:
                            _restore_bar_after_snip()

                    root.after(80, _do_capture)

                canvas.bind("<ButtonPress-1>", _on_press)
                canvas.bind("<B1-Motion>", _on_motion)
                canvas.bind("<ButtonRelease-1>", _on_release)
                canvas.bind("<Button-3>", _cancel)
                snip.bind("<Escape>", _cancel)
                snip.bind("<space>", _re_freeze)
                snip.bind("<Destroy>", _on_snip_destroy)
                with contextlib.suppress(Exception):
                    snip.protocol("WM_DELETE_WINDOW", _cancel)

                active_snip_win[0] = snip
                snip_phase[0] = "canvas"
                snip_opened_at[0] = time.monotonic()
                snip_cancel_cb[0] = _cancel
                snip_refreeze_cb[0] = _re_freeze

                snip.deiconify()
                snip.update_idletasks()
                with contextlib.suppress(Exception):
                    snip.attributes("-topmost", True)
                _apply_win32_toolwindow(snip.winfo_id(), no_activate=False, round_corners=False)
                with contextlib.suppress(Exception):
                    snip.lift()
                    snip.focus_force()

            def _grab_and_open_canvas() -> None:
                wait_token = {"cancelled": False}
                snip_phase[0] = "delay"
                snip_opened_at[0] = time.monotonic()
                snip_cancel_cb[0] = lambda: wait_token.__setitem__("cancelled", True)
                snip_refreeze_cb[0] = None
                with contextlib.suppress(Exception):
                    root.withdraw()
                    root.update_idletasks()
                    root.update()
                last_rendered[0] = "hidden"

                def _open_now() -> None:
                    if wait_token["cancelled"] or not snip_active[0]:
                        return
                    _show_snip_canvas(capture_desktop_freeze_frame())

                root.after(35, _open_now)

            if delay_sec > 0:
                if fg_prof != pname:
                    self._on_select_tab(pname)
                banner = None
                lbl = None
                try:
                    banner = tk.Toplevel(root)
                    banner.withdraw()
                    banner.overrideredirect(True)
                    with contextlib.suppress(Exception):
                        banner.attributes("-topmost", True)
                    banner.configure(bg=_BG_RAIL)
                    lbl = tk.Label(
                        banner,
                        text=get_string("avatar_snip_countdown", name=pname, sec=delay_sec),
                        bg=_BG_RAIL,
                        fg="#FFFFFF",
                        font=("Segoe UI Semibold", 10),
                        padx=16,
                        pady=8,
                    )
                    lbl.pack()
                    banner.update_idletasks()
                    bw = banner.winfo_reqwidth()
                    bh = banner.winfo_reqheight()
                    bx = max(8, (root.winfo_screenwidth() - bw) // 2)
                    banner.geometry(f"{bw}x{bh}+{bx}+54")
                    banner.deiconify()
                    banner.update_idletasks()
                    with contextlib.suppress(Exception):
                        banner.attributes("-topmost", True)
                    _apply_win32_toolwindow(banner.winfo_id(), no_activate=True)
                except Exception:
                    banner = None
                    lbl = None

                deadline = time.monotonic() + float(delay_sec)
                cancelled = {"flag": False}

                def _cancel_countdown() -> None:
                    cancelled["flag"] = True
                    if banner is not None:
                        with contextlib.suppress(Exception):
                            banner.destroy()
                    _restore_bar_after_snip()

                active_snip_win[0] = banner
                snip_phase[0] = "delay"
                snip_opened_at[0] = time.monotonic()
                snip_cancel_cb[0] = _cancel_countdown
                snip_refreeze_cb[0] = None

                if banner is not None:
                    banner.bind("<Button-3>", lambda _e: _cancel_countdown())

                def _tick_countdown() -> None:
                    if cancelled["flag"] or not snip_active[0]:
                        return
                    if win32_user32 is not None and (win32_user32.GetAsyncKeyState(0x1B) & 0x8000):
                        _cancel_countdown()
                        return
                    rem_f = deadline - time.monotonic()
                    if rem_f <= 0.0:
                        active_snip_win[0] = None
                        if banner is not None:
                            with contextlib.suppress(Exception):
                                banner.destroy()
                        _grab_and_open_canvas()
                        return
                    rem = max(1, int(rem_f + 0.99))
                    if lbl is not None:
                        with contextlib.suppress(Exception):
                            lbl.configure(
                                text=get_string("avatar_snip_countdown", name=pname, sec=rem)
                            )
                    root.after(100, _tick_countdown)

                root.after(100, _tick_countdown)
                return

            if fg_prof == pname:
                _grab_and_open_canvas()
            else:
                self._on_select_tab(pname)
                wait_switch = {"cancelled": False}
                snip_phase[0] = "delay"
                snip_opened_at[0] = time.monotonic()
                snip_cancel_cb[0] = lambda: wait_switch.__setitem__("cancelled", True)
                snip_refreeze_cb[0] = None
                with contextlib.suppress(Exception):
                    root.withdraw()
                    root.update_idletasks()
                last_rendered[0] = "hidden"

                def _open_after_switch() -> None:
                    if wait_switch["cancelled"] or not snip_active[0]:
                        return
                    _show_snip_canvas(capture_desktop_freeze_frame())

                root.after(260, _open_after_switch)

        def _open_avatar_menu(event: object, pname: str) -> None:
            """Show FoxProfile-themed popup menu to set/change `pname`'s circular avatar."""
            _cancel_pending_tab_select()
            _close_popup_menu()
            rx = int(getattr(event, "x_root", root.winfo_x()))
            ry = int(getattr(event, "y_root", root.winfo_y() + root.winfo_height()))

            pop = tk.Toplevel(root)
            pop.withdraw()
            pop.overrideredirect(True)
            with contextlib.suppress(Exception):
                pop.attributes("-topmost", True)
            pop.configure(bg=_BG_RAIL)
            active_menu[0] = pop
            opened_at = time.monotonic()

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
                lambda: _open_screen_snipper(pname, delay_sec=0),
            )
            _add_item(
                get_string("avatar_menu_snip_delay"),
                lambda: _open_screen_snipper(pname, delay_sec=3),
            )

            def _do_paste() -> None:
                if save_avatar_from_clipboard(pname):
                    _notify_avatar_updated()

            _add_item(get_string("avatar_menu_paste"), _do_paste)

            def _do_file() -> None:
                file_dialog_active[0] = True
                try:
                    fpath = filedialog.askopenfilename(
                        parent=root,
                        title=f"Avatar - {pname}",
                        filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp *.bmp *.gif")],
                    )
                    if fpath and save_avatar_from_file(pname, fpath):
                        _notify_avatar_updated()
                finally:
                    file_dialog_active[0] = False
                    last_rendered[0] = None

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
            pop.deiconify()
            pop.update_idletasks()
            with contextlib.suppress(Exception):
                pop.attributes("-topmost", True)
                pop.lift()
            _apply_win32_toolwindow(pop.winfo_id(), no_activate=True)
            active_menu_bounds[:] = [float(px), float(py), float(px + pw), float(py + ph), opened_at]
            pop.bind("<Escape>", lambda _e: _close_popup_menu())

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
            cur_wx = int(drag_state["win_x"]) if drag_state["custom_pos"] else root.winfo_x()
            cur_wy = int(drag_state["win_y"]) if drag_state["custom_pos"] else root.winfo_y()
            drag_state["x"] = rx - cur_wx
            drag_state["y"] = ry - cur_wy
            if win32_user32 is not None and win32_hwnd_type is not None:
                with contextlib.suppress(Exception):
                    hwnd = root.winfo_id()
                    drag_state["top_hwnd"] = int(win32_user32.GetParent(win32_hwnd_type(hwnd)) or hwnd)

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
            if not drag_state["moved"]:
                return
            scr_w = int(drag_state["scr_w"])
            scr_h = int(drag_state["scr_h"])
            cur_w = int(drag_state["cur_w"])
            cur_h = int(drag_state["cur_h"])
            nx = max(0, min(rx - int(drag_state["x"]), max(0, scr_w - cur_w)))
            ny = max(0, min(ry - int(drag_state["y"]), max(0, scr_h - cur_h)))
            drag_state["custom_pos"] = True
            drag_state["win_x"] = nx
            drag_state["win_y"] = ny
            top_hwnd = int(drag_state["top_hwnd"])
            if win32_user32 is not None and win32_hwnd_type is not None and top_hwnd:
                # SWP_NOSIZE (0x0001) | SWP_NOACTIVATE (0x0010) | SWP_NOOWNERZORDER (0x0200)
                win32_user32.SetWindowPos(
                    win32_hwnd_type(top_hwnd),
                    win32_hwnd_type(-1),
                    nx,
                    ny,
                    0,
                    0,
                    0x0211,
                )
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
            scr_w = int(drag_state["scr_w"])
            scr_h = int(drag_state["scr_h"])
            cur_w = int(drag_state["cur_w"])
            drag_state["right_x"] = nx + cur_w
            drag_state["dock_right"] = (nx + cur_w) >= (scr_w - 220) or nx >= (scr_w - 300)
            root.geometry(f"+{nx}+{ny}")
            new_orient = _compute_orientation(nx, ny, scr_w, scr_h, True, cur_w)
            if new_orient != drag_state["orientation"]:
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
            cur_logo = logo_holder[0]
            if cur_logo is not None:
                ic = tk.Label(
                    rail,
                    image=cur_logo,  # type: ignore[arg-type]
                    bg=_BG_RAIL,
                    bd=0,
                    padx=0,
                    pady=0,
                    cursor="fleur",
                )
                ic.image = cur_logo  # type: ignore[attr-defined]
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
            def _on_avatar_click(
                event: object,
                n: str = pname,
                direct_menu: bool = (avatar_img is None or is_active),
            ) -> None:
                if direct_menu:
                    _cancel_pending_tab_select()
                    last_av_click["ts"] = 0.0
                    _open_avatar_menu(event, n)
                    return
                now = time.monotonic()
                prev_ts = float(last_av_click.get("ts", 0.0))  # type: ignore[arg-type]
                if last_av_click.get("name") == n and (now - prev_ts) <= 0.45:
                    _cancel_pending_tab_select()
                    last_av_click["ts"] = 0.0
                    _open_avatar_menu(event, n)
                    return
                last_av_click["name"] = n
                last_av_click["ts"] = now
                _cancel_pending_tab_select()
                _close_popup_menu()

                def _delayed_select() -> None:
                    last_av_click["after_id"] = None
                    if active_menu[0] is None and not snip_active[0]:
                        self._on_select_tab(n)

                last_av_click["after_id"] = root.after(220, _delayed_select)

            def _on_avatar_double_click(event: object, n: str = pname) -> None:
                _cancel_pending_tab_select()
                last_av_click["ts"] = 0.0
                if active_menu[0] is None:
                    _open_avatar_menu(event, n)

            av_lbl.pack(side="left", padx=(2, 2))
            av_lbl.bind("<Button-1>", _on_avatar_click)
            av_lbl.bind("<Double-Button-1>", _on_avatar_double_click)
            av_lbl.bind("<Button-3>", lambda e, n=pname: _open_avatar_menu(e, n))

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
                lambda _e, n=pname: (_cancel_pending_tab_select(), _close_popup_menu(), self._on_select_tab(n)),
            )
            btn_select.bind(
                "<Button-3>",
                lambda e, n=pname: _open_avatar_menu(e, n),
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
            orient = _compute_orientation(
                int(drag_state["win_x"]),
                int(drag_state["win_y"]),
                scr_w,
                scr_h,
                bool(drag_state["custom_pos"]),
                int(drag_state["cur_w"]),
            )
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
            drag_state["cur_w"] = req_w
            drag_state["cur_h"] = req_h
            if drag_state["custom_pos"]:
                max_x = max(0, scr_w - req_w)
                max_y = max(0, scr_h - req_h)
                if drag_state["dock_right"] and int(drag_state["right_x"]) > 0:
                    x = max(0, min(int(drag_state["right_x"]) - req_w, max(0, scr_w - req_w - 4)))
                else:
                    x = max(0, min(int(drag_state["win_x"]), max(0, scr_w - req_w - 4)))
                y = max(0, min(int(drag_state["win_y"]), max(0, scr_h - req_h - 4)))
                x = max(0, min(x, max_x))
                y = max(0, min(y, max_y))
                drag_state["win_x"] = x
                drag_state["win_y"] = y
                drag_state["right_x"] = x + req_w
            else:
                x = max(0, min(max(8, (scr_w - req_w) // 2), max(0, scr_w - req_w)))
                y = max(0, min(4, max(0, scr_h - req_h)))
            root.geometry(f"{req_w}x{req_h}+{x}+{y}")

        def _poll() -> None:
            next_delay = 120
            with self._lock:
                stopping = self._stop_flag
                running, loading, active_tab, single_stage, enabled = self._state
                pending_snip = self._pending_snip
                self._pending_snip = None

            if stopping:
                with contextlib.suppress(Exception):
                    _cancel_pending_tab_select()
                    _close_popup_menu()
                    _abort_active_snip()
                    photo_cache.clear()
                    logo_holder[0] = None
                    last_rendered[0] = None
                    for child in outer.winfo_children():
                        child.destroy()
                    root.quit()
                    root.destroy()
                return

            try:
                if not enabled or not running:
                    if snip_active[0]:
                        _abort_active_snip()
                    if last_rendered[0] != "hidden":
                        _close_popup_menu()
                        root.withdraw()
                        last_rendered[0] = "hidden"
                    next_delay = 150
                elif pending_snip is not None and not snip_active[0] and not drag_state["dragging"]:
                    _open_screen_snipper(pending_snip[0], delay_sec=pending_snip[1])
                    next_delay = 80
                elif active_menu[0] is not None and not file_dialog_active[0]:
                    if win32_user32 is not None and (time.monotonic() - active_menu_bounds[4]) >= 0.22:
                        if win32_user32.GetAsyncKeyState(0x1B) & 0x8000:
                            _close_popup_menu()
                        elif (win32_user32.GetAsyncKeyState(0x01) & 0x8000) or (
                            win32_user32.GetAsyncKeyState(0x02) & 0x8000
                        ):
                            mx = root.winfo_pointerx()
                            my = root.winfo_pointery()
                            if not (
                                active_menu_bounds[0] - 4 <= mx <= active_menu_bounds[2] + 4
                                and active_menu_bounds[1] - 4 <= my <= active_menu_bounds[3] + 4
                            ):
                                _close_popup_menu()
                    next_delay = 60
                else:
                    if snip_active[0]:
                        if snip_phase[0] == "canvas":
                            cur_snip = active_snip_win[0]
                            exists = False
                            if cur_snip is not None:
                                with contextlib.suppress(Exception):
                                    exists = bool(cur_snip.winfo_exists())  # type: ignore[attr-defined]
                            if not exists:
                                _abort_active_snip()
                            elif (
                                win32_user32 is not None
                                and (time.monotonic() - snip_opened_at[0]) >= 0.22
                            ):
                                if win32_user32.GetAsyncKeyState(0x1B) & 0x8000:
                                    _abort_active_snip()
                                elif (
                                    win32_user32.GetAsyncKeyState(0x20) & 0x8000
                                    and snip_refreeze_cb[0] is not None
                                ):
                                    cb_rf = snip_refreeze_cb[0]
                                    cb_rf()
                        elif (
                            snip_phase[0] in ("delay", "capturing")
                            and (time.monotonic() - snip_opened_at[0]) > 15.0
                        ):
                            _abort_active_snip()

                    if drag_state["dragging"] or snip_active[0] or file_dialog_active[0]:
                        next_delay = 80
                    else:
                        poll_tick[0] += 1
                        if poll_tick[0] % 5 == 0 and any(get_avatar_path(n, 22) is None for n in running):
                            fg_prof = detect_foreground_running_profile(running)
                            if (
                                fg_prof
                                and get_avatar_path(fg_prof, 22) is None
                                and auto_capture_muse_avatar_from_active_window(
                                    fg_prof,
                                    allow_unprefixed_single=(len(running) == 1),
                                )
                            ):
                                _notify_avatar_updated()

                        cur_orient = _compute_orientation(
                            int(drag_state["win_x"]),
                            int(drag_state["win_y"]),
                            root.winfo_screenwidth(),
                            root.winfo_screenheight(),
                            bool(drag_state["custom_pos"]),
                            int(drag_state["cur_w"]),
                        )
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
                            root.update_idletasks()
                            with contextlib.suppress(Exception):
                                root.attributes("-topmost", True)
                            parent = _apply_win32_toolwindow(root.winfo_id(), no_activate=True)
                            if (
                                win32_user32 is not None
                                and win32_hwnd_type is not None
                                and parent > 0
                                and not win32_user32.IsWindowVisible(win32_hwnd_type(parent))
                            ):
                                # SW_SHOWNOACTIVATE (4), then SWP_SHOWWINDOW | SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE (0x0053)
                                win32_user32.ShowWindow(win32_hwnd_type(parent), 4)
                                win32_user32.SetWindowPos(
                                    win32_hwnd_type(parent),
                                    win32_hwnd_type(-1),
                                    0,
                                    0,
                                    0,
                                    0,
                                    0x0053,
                                )
                            last_rendered[0] = render_key
                        elif (
                            poll_tick[0] % 8 == 0
                            and win32_user32 is not None
                            and win32_hwnd_type is not None
                        ):
                            with contextlib.suppress(Exception):
                                hwnd = root.winfo_id()
                                parent = int(
                                    win32_user32.GetParent(win32_hwnd_type(hwnd)) or hwnd
                                )
                                if parent > 0:
                                    parent_hwnd = win32_hwnd_type(parent)
                                    if not win32_user32.IsWindowVisible(parent_hwnd):
                                        last_rendered[0] = None
                                    else:
                                        # Re-assert HWND_TOPMOST without moving, resizing, or activating (0x0013)
                                        win32_user32.SetWindowPos(
                                            parent_hwnd,
                                            win32_hwnd_type(-1),
                                            0,
                                            0,
                                            0,
                                            0,
                                            0x0013,
                                        )
            except Exception:
                next_delay = 150

            with contextlib.suppress(Exception):
                root.after(next_delay, _poll)

        root.after(50, _poll)
        try:
            root.mainloop()
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                _cancel_pending_tab_select()
            with contextlib.suppress(Exception):
                _close_popup_menu()
            with contextlib.suppress(Exception):
                _abort_active_snip()
            photo_cache.clear()
            logo_holder[0] = None
            last_rendered[0] = None
            last_av_click.clear()
            drag_state.clear()
            with contextlib.suppress(Exception):
                outer.destroy()
            with contextlib.suppress(Exception):
                root.quit()
            with contextlib.suppress(Exception):
                root.destroy()
            if getattr(tk, "_default_root", None) is root:
                tk._default_root = None
