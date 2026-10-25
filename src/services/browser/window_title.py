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


def _raise_and_move_hwnd_win32(
    hwnd: int,
    x: int | None = None,
    y: int | None = None,
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
