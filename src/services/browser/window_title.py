"""OS-level window title prefixer for running browser profiles.

Updates the native Win32 window frame caption (`SetWindowTextW`) to start with
`[<profile_name>] ` without touching `document.title` inside the browser DOM,
keeping anti-detect stealth 100% intact. Uses pure Win32 Toolhelp32 APIs with
zero external dependencies.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys


def _child_pids_win32(root_pid: int) -> set[int]:
    if sys.platform != "win32":
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

        descendants: set[int] = set()
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
