"""OS-level window title prefixer and safe window manager for running browser profiles.

- Updates the native Win32 window frame caption (`SetWindowTextW`) to start with
  `[<profile_name>] ` without touching `document.title` inside the browser DOM.
- Focuses (`SetForegroundWindow`) or arranges (`SetWindowPos` with `SWP_NOSIZE`)
  running browser windows at the OS level without altering viewport dimensions.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time


def _child_pids_win32(root_pid: int) -> set[int]:
    if sys.platform != "win32" or root_pid <= 0:
        return set()
    try:
        import ctypes
        from ctypes import wintypes

        th32cs_snapprocess = 0x00000002
        kernel32 = ctypes.windll.kernel32

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snap = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
        if snap == -1:
            return set()
        parent_map: dict[int, list[int]] = {}
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            ok = kernel32.Process32First(snap, ctypes.byref(entry))
            while ok:
                parent_map.setdefault(int(entry.th32ParentProcessID), []).append(
                    int(entry.th32ProcessID)
                )
                ok = kernel32.Process32Next(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)

        descendants: set[int] = {root_pid}
        queue = [root_pid]
        while queue:
            cur = queue.pop()
            for child in parent_map.get(cur, []):
                if child not in descendants:
                    descendants.add(child)
                    queue.append(child)
        return descendants
    except Exception:
        return set()


def _find_profile_hwnds_win32(profile_name: str, root_pid: int | None = None) -> list[int]:
    """Return visible top-level HWNDs belonging to `profile_name`, main window first."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        pids = _child_pids_win32(root_pid) if root_pid else set()
        prefix = f"[{profile_name}] "
        user32 = ctypes.windll.user32
        candidates: list[tuple[int, int, int]] = []

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        enum_windows_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )

        def _callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            is_prefix_match = title.startswith(prefix)
            is_pid_match = False
            if not is_prefix_match and pids:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                is_pid_match = bool(pid.value in pids and title)
            if not (is_prefix_match or is_pid_match):
                return True

            is_iconic = bool(user32.IsIconic(hwnd))
            rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = int(rect.right - rect.left)
            h = int(rect.bottom - rect.top)
            if not is_iconic and (w < 200 or h < 150):
                return True
            area = 1_000_000 if is_iconic else (w * h)
            priority = 2 if is_prefix_match else 1
            candidates.append((priority, area, int(hwnd)))
            return True

        user32.EnumWindows(enum_windows_proc(_callback), 0)
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [hwnd for _, _, hwnd in candidates]
    except Exception:
        return []


_locked_hwnd_sizes: dict[int, tuple[int, int]] = {}
_shield_lock = threading.Lock()
_shield_started = False


def _is_point_on_maximize_zone_win32(hwnd: int, x: int, y: int, is_double_click: bool = False) -> bool:
    """Return True if screen point `(x, y)` is on `hwnd`'s `[ □ ]` Maximize button (or caption dblclick)."""
    if sys.platform != "win32" or hwnd <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        frect = RECT()
        got_dwm = False
        with contextlib.suppress(Exception):
            dwmapi = ctypes.windll.dwmapi
            dwmwa_extended_frame_bounds = 9
            res = dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd),
                dwmwa_extended_frame_bounds,
                ctypes.byref(frect),
                ctypes.sizeof(RECT),
            )
            got_dwm = res == 0

        if not got_dwm:
            if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(frect)):
                return False
            frect.left += 7
            frect.right -= 7

        dpi = 96
        with contextlib.suppress(Exception):
            dpi = int(user32.GetDpiForWindow(wintypes.HWND(hwnd))) or 96
        scale = max(1.0, float(dpi) / 96.0)

        top_band = int(frect.top) + int(42 * scale)
        if not (int(frect.top) <= y <= top_band):
            return False

        # Top-right caption buttons in Firefox/Camoufox on Windows:
        # [ — ] (right - 138..right - 92), [ □ ] (right - 92..right - 46), [ ✕ ] (right - 46..right)
        max_btn_left = int(frect.right) - int(95 * scale)
        max_btn_right = int(frect.right) - int(45 * scale)
        if max_btn_left <= x <= max_btn_right:
            return True

        # Also query WM_NCHITTEST for HTMAXBUTTON (9) or double-click on HTCAPTION (2)
        wm_nchittest = 0x0084
        smto_abortifhung = 0x0002
        ht = wintypes.DWORD(0)
        lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        if user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd),
            wm_nchittest,
            0,
            lparam,
            smto_abortifhung,
            15,
            ctypes.byref(ht),
        ):
            if int(ht.value) == 9:  # HTMAXBUTTON / HTZOOM
                return True
            if is_double_click and int(ht.value) == 2:  # HTCAPTION double-click maximize
                return True
        return False
    except Exception:
        return False


def _ensure_maximize_shield_win32() -> None:
    """Start a lightweight out-of-process WH_MOUSE_LL guard that blocks clicks on `[ □ ]`."""
    global _shield_started
    if sys.platform != "win32" or "PYTEST_CURRENT_TEST" in os.environ:
        return
    with _shield_lock:
        if _shield_started:
            return
        _shield_started = True

    def _hook_loop() -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            wh_mouse_ll = 14
            wm_lbuttondown = 0x0201
            wm_lbuttonup = 0x0202
            wm_lbuttondblclk = 0x0203
            wm_nclbuttondown = 0x00A1
            wm_nclbuttondblclk = 0x00A3
            ga_root = 2

            class POINT(ctypes.Structure):
                _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

            class MSLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [
                    ("pt", POINT),
                    ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_ulonglong),
                ]

            c_ssize_t = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
            hook_proto = ctypes.WINFUNCTYPE(
                c_ssize_t,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            user32.CallNextHookEx.argtypes = [
                wintypes.HHOOK,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.CallNextHookEx.restype = c_ssize_t
            user32.WindowFromPoint.argtypes = [POINT]
            user32.WindowFromPoint.restype = wintypes.HWND
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND

            last_click: list[float] = [0.0, -9999.0, -9999.0, 0.0]  # [ts, x, y, swallowed_down]

            def _low_level_mouse_proc(n_code: int, w_param: int, l_param: int) -> int:
                if n_code >= 0 and _locked_hwnd_sizes:
                    if w_param == wm_lbuttonup and last_click[3] == 1.0:
                        last_click[3] = 0.0
                        return 1
                    if w_param in (
                        wm_lbuttondown,
                        wm_lbuttondblclk,
                        wm_nclbuttondown,
                        wm_nclbuttondblclk,
                    ):
                        info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        px, py = int(info.pt.x), int(info.pt.y)
                        hwnd = user32.WindowFromPoint(info.pt)
                        if hwnd:
                            root = user32.GetAncestor(wintypes.HWND(hwnd), ga_root) or hwnd
                            root_id = int(root)
                            if root_id in _locked_hwnd_sizes:
                                now = time.monotonic()
                                is_dbl = w_param in (wm_lbuttondblclk, wm_nclbuttondblclk) or (
                                    (now - last_click[0]) < 0.45
                                    and abs(px - last_click[1]) <= 6
                                    and abs(py - last_click[2]) <= 6
                                )
                                last_click[0] = now
                                last_click[1] = float(px)
                                last_click[2] = float(py)
                                if _is_point_on_maximize_zone_win32(root_id, px, py, is_dbl):
                                    last_click[3] = 1.0
                                    return 1
                return int(user32.CallNextHookEx(None, n_code, w_param, l_param))

            cb = hook_proto(_low_level_mouse_proc)
            hook_id = user32.SetWindowsHookExW(wh_mouse_ll, cb, None, 0)
            if not hook_id:
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            return

    def _size_watchdog_loop() -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            while True:
                for h in list(_locked_hwnd_sizes):
                    if not user32.IsWindow(wintypes.HWND(h)):
                        _locked_hwnd_sizes.pop(h, None)
                    else:
                        _lock_window_size_win32(h)
                time.sleep(0.2)
        except Exception:
            return

    threading.Thread(target=_hook_loop, name="CamoufoxMaximizeShield", daemon=True).start()
    threading.Thread(target=_size_watchdog_loop, name="CamoufoxSizeWatchdog", daemon=True).start()


def _lock_window_size_win32(hwnd: int) -> None:
    """Disable Win32 Maximize box, System Menu Size/Maximize, and enforce initial window size."""
    if sys.platform != "win32" or hwnd <= 0:
        return
    with contextlib.suppress(Exception):
        import ctypes
        from ctypes import wintypes

        _ensure_maximize_shield_win32()
        user32 = ctypes.windll.user32
        gwl_style = -16
        ws_maximizebox = 0x00010000
        sc_size = 0xF000
        sc_maximize = 0xF030
        mf_bycommand = 0x0000
        sw_restore = 9
        swp_nomove = 0x0002
        swp_nozorder = 0x0004
        swp_noactivate = 0x0010

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        cur_style = int(get_long(wintypes.HWND(hwnd), gwl_style))
        if cur_style & ws_maximizebox:
            set_long(wintypes.HWND(hwnd), gwl_style, cur_style & ~ws_maximizebox)

        hmenu = user32.GetSystemMenu(wintypes.HWND(hwnd), False)
        if hmenu:
            user32.DeleteMenu(hmenu, sc_maximize, mf_bycommand)
            user32.DeleteMenu(hmenu, sc_size, mf_bycommand)

        if user32.IsIconic(wintypes.HWND(hwnd)):
            return
        if user32.IsZoomed(wintypes.HWND(hwnd)):
            user32.ShowWindow(wintypes.HWND(hwnd), sw_restore)

        rect = RECT()
        if user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            w = int(rect.right - rect.left)
            h = int(rect.bottom - rect.top)
            if w >= 300 and h >= 200:
                locked = _locked_hwnd_sizes.get(int(hwnd))
                if locked is None:
                    _locked_hwnd_sizes[int(hwnd)] = (w, h)
                elif (w, h) != locked:
                    user32.SetWindowPos(
                        wintypes.HWND(hwnd),
                        wintypes.HWND(0),
                        0,
                        0,
                        locked[0],
                        locked[1],
                        swp_nomove | swp_nozorder | swp_noactivate,
                    )


def _raise_and_move_hwnd_win32(
    hwnd: int,
    x: int | None = None,
    y: int | None = None,
    *,
    lock_size: bool = True,
) -> None:
    """Bring `hwnd` in front of the Flet GUI window using SWP_NOSIZE (never alters viewport)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    sw_restore = 9
    swp_nosize = 0x0001
    swp_nomove = 0x0002
    swp_showwindow = 0x0040
    hwnd_topmost = wintypes.HWND(-1)
    hwnd_notopmost = wintypes.HWND(-2)

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, sw_restore)
    if lock_size:
        _lock_window_size_win32(hwnd)

    fg_hwnd = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    cur_tid = kernel32.GetCurrentThreadId()
    attached = False
    if fg_tid and cur_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))

    try:
        flags = swp_nosize | swp_showwindow
        if x is None or y is None:
            flags |= swp_nomove
            px, py = 0, 0
        else:
            px, py = int(x), int(y)

        # Promote to TOPMOST then immediately demote to NOTOPMOST with SWP_NOSIZE
        # so the browser window rises above the Flet desktop process window without
        # ever changing window.innerWidth / window.innerHeight.
        user32.SetWindowPos(wintypes.HWND(hwnd), hwnd_topmost, px, py, 0, 0, flags)
        user32.SetWindowPos(wintypes.HWND(hwnd), hwnd_notopmost, px, py, 0, 0, flags)
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


def focus_browser_window(profile_name: str, root_pid: int | None = None) -> bool:
    """Bring a running profile's browser window to the foreground without resizing it."""
    if sys.platform != "win32":
        return False
    hwnds = _find_profile_hwnds_win32(profile_name, root_pid)
    if not hwnds:
        return False
    try:
        for hwnd in reversed(hwnds):
            _raise_and_move_hwnd_win32(hwnd)
        return True
    except Exception:
        return False


def switch_browser_window(
    target_name: str,
    profile_pids: list[tuple[str, int | None]],
    *,
    single_stage: bool = True,
    top_margin: int = 42,
) -> bool:
    """Switch to `target_name`'s browser window in Single-Stage mode using SWP_NOSIZE.

    When `single_stage=True`, centers `target_name`'s window in the desktop work area
    (preserving its exact width and height via `SWP_NOSIZE`) and minimizes the windows
    of all other running profiles so only one browser window is shown on screen.
    """
    if sys.platform != "win32":
        return False
    target_pid = next((pid for pname, pid in profile_pids if pname == target_name), None)
    if not single_stage:
        return focus_browser_window(target_name, target_pid)

    target_hwnds = _find_profile_hwnds_win32(target_name, target_pid)
    if not target_hwnds:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        spi_getworkarea = 0x0030
        sw_restore = 9
        sw_showminnoactive = 7
        sw_forceminimize = 11

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        work = RECT()
        if not user32.SystemParametersInfoW(spi_getworkarea, 0, ctypes.byref(work), 0):
            work.left, work.top, work.right, work.bottom = 0, 0, 1920, 1040

        stage_top = int(work.top) + max(0, int(top_margin))
        work_w = max(800, int(work.right - work.left))
        work_h = max(600, int(work.bottom) - stage_top)

        target_set = set(target_hwnds)
        # 1. Minimize all other running profile browser windows FIRST so windows never stack
        for pname, pid in profile_pids:
            if pname == target_name:
                continue
            for other_hwnd in _find_profile_hwnds_win32(pname, pid):
                _lock_window_size_win32(other_hwnd)
                if other_hwnd not in target_set and not user32.IsIconic(other_hwnd):
                    user32.ShowWindowAsync(wintypes.HWND(other_hwnd), sw_showminnoactive)
                    user32.ShowWindow(wintypes.HWND(other_hwnd), sw_forceminimize)

        # 2. Restore, center (preserving exact SWP_NOSIZE viewport), and focus target window
        for hwnd in reversed(target_hwnds):
            if user32.IsIconic(hwnd):
                user32.ShowWindow(wintypes.HWND(hwnd), sw_restore)
            win_rect = RECT()
            user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(win_rect))
            win_w = max(400, int(win_rect.right - win_rect.left))
            win_h = max(300, int(win_rect.bottom - win_rect.top))

            x = int(work.left) + max(0, (work_w - win_w) // 2)
            y = stage_top + max(0, (work_h - win_h) // 2)
            max_x = max(int(work.left), int(work.right) - min(win_w, work_w))
            max_y = max(stage_top, int(work.bottom) - min(win_h, work_h))
            x = max(int(work.left), min(x, max_x))
            y = max(stage_top, min(y, max_y))
            _raise_and_move_hwnd_win32(hwnd, x=x, y=y)

        return True
    except Exception:
        return False


def focus_foxprofile_window(
    window_title: str = "FoxProfile",
    *,
    toggle: bool = False,
) -> bool:
    """Bring the main FoxProfile desktop window to the foreground using SWP_NOSIZE.

    When `toggle=True`, minimizes (hides) the FoxProfile window if it is already
    visible and in front of running browser windows.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gwl_exstyle = -20
        ws_ex_topmost = 0x00000008
        ws_ex_toolwindow = 0x00000080
        sw_showminnoactive = 7
        sw_forceminimize = 11
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        pids = _child_pids_win32(os.getpid())
        matches: list[int] = []
        covered_by_other = False

        enum_windows_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )

        def _callback(hwnd: int, _lparam: int) -> bool:
            nonlocal covered_by_other
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = (buf.value or "").strip()
            if not title:
                return True
            if not title.startswith("["):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if title == window_title or (
                    pid.value in pids and window_title.lower() in title.lower()
                ):
                    matches.append(int(hwnd))
                    return True
            if not matches and not user32.IsIconic(hwnd):
                exstyle = int(get_long(wintypes.HWND(hwnd), gwl_exstyle))
                if not (exstyle & (ws_ex_topmost | ws_ex_toolwindow)):
                    rect = RECT()
                    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
                    if (rect.right - rect.left) >= 300 and (rect.bottom - rect.top) >= 200:
                        covered_by_other = True
            return True

        user32.EnumWindows(enum_windows_proc(_callback), 0)
        if not matches:
            return False

        is_minimized = any(bool(user32.IsIconic(wintypes.HWND(h))) for h in matches)
        if toggle and not is_minimized and not covered_by_other:
            for hwnd in matches:
                user32.ShowWindowAsync(wintypes.HWND(hwnd), sw_showminnoactive)
                user32.ShowWindow(wintypes.HWND(hwnd), sw_forceminimize)
            return True

        for hwnd in reversed(matches):
            _raise_and_move_hwnd_win32(hwnd, lock_size=False)
        return True
    except Exception:
        return False


def arrange_browser_windows(profile_pids: list[tuple[str, int | None]]) -> int:
    """Arrange running browser windows across the desktop work area using SWP_NOSIZE.

    Preserves each browser window's exact width and height (`SWP_NOSIZE`) so
    `window.innerWidth`/`innerHeight` never change and no `resize` event fires.
    """
    if sys.platform != "win32" or not profile_pids:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        spi_getworkarea = 0x0030
        sw_restore = 9

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        work = RECT()
        if not user32.SystemParametersInfoW(spi_getworkarea, 0, ctypes.byref(work), 0):
            work.left, work.top, work.right, work.bottom = 0, 0, 1920, 1040

        work_w = max(800, int(work.right - work.left))
        work_h = max(600, int(work.bottom - work.top))

        targets: list[int] = []
        seen_hwnds: set[int] = set()
        for pname, pid in profile_pids:
            for hwnd in _find_profile_hwnds_win32(pname, pid):
                if hwnd not in seen_hwnds:
                    seen_hwnds.add(hwnd)
                    targets.append(hwnd)
                    break

        count = len(targets)
        if count == 0:
            return 0

        cols = 1 if count == 1 else (2 if count <= 4 else 3)
        rows = max(1, (count + cols - 1) // cols)
        slot_w = max(320, work_w // cols)
        slot_h = max(240, work_h // rows)

        for idx, hwnd in enumerate(targets):
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, sw_restore)
            win_rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(win_rect))
            win_w = max(400, int(win_rect.right - win_rect.left))
            win_h = max(300, int(win_rect.bottom - win_rect.top))

            col = idx % cols
            row = idx // cols
            if count == 1:
                x = int(work.left) + max(0, (work_w - win_w) // 2)
                y = int(work.top) + max(0, (work_h - win_h) // 2)
            else:
                slot_x = int(work.left) + col * slot_w
                slot_y = int(work.top) + row * slot_h
                x = slot_x + max(0, (slot_w - win_w) // 2)
                y = slot_y + max(0, (slot_h - win_h) // 2)
            # Clamp so the window stays inside the visible work area
            max_x = max(int(work.left), int(work.right) - min(win_w, work_w))
            max_y = max(int(work.top), int(work.bottom) - min(win_h, work_h))
            x = max(int(work.left), min(x, max_x))
            y = max(int(work.top), min(y, max_y))

            _raise_and_move_hwnd_win32(hwnd, x=x, y=y)

        return count
    except Exception:
        return 0


def _prefix_child_windows_win32(root_pid: int, prefix: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        pids = _child_pids_win32(root_pid)
        if not pids:
            return

        user32 = ctypes.windll.user32
        enum_windows_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )

        def _callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if title and not title.startswith(prefix):
                user32.SetWindowTextW(hwnd, f"{prefix}{title}")
            _lock_window_size_win32(int(hwnd))
            return True

        user32.EnumWindows(enum_windows_proc(_callback), 0)
    except Exception:
        return


async def keep_window_title_prefixed(profile_name: str, stop: asyncio.Event) -> None:
    """Periodically ensure the OS browser window title starts with `[profile_name] `."""
    if sys.platform != "win32":
        return
    prefix = f"[{profile_name}] "
    root_pid = os.getpid()
    while not stop.is_set():
        await asyncio.to_thread(_prefix_child_windows_win32, root_pid, prefix)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1.5)


def guard_profile_windows_loop(
    root_pid: int,
    profile_name: str,
    stop_event: threading.Event,
) -> None:
    """Continuously enforce window title prefix and fixed viewport size for `root_pid`."""
    if sys.platform != "win32" or "PYTEST_CURRENT_TEST" in os.environ:
        return
    prefix = f"[{profile_name}] "
    while not stop_event.is_set():
        _prefix_child_windows_win32(root_pid, prefix)
        if stop_event.wait(0.25):
            break
