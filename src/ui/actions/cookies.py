import asyncio
import contextlib
import os
import pathlib
from collections.abc import Callable

import flet as ft

from ...core.config import DATA_DIR
from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager
from ...services.browser import cookies, fingerprint
from ...services.browser.launcher import ProfileBusyError
from ..dialogs import open_confirm_dialog, open_cookie_dialog

_EXTENSIONS = {"json": "json", "netscape": "txt"}


def _page_clipboard(page: ft.Page) -> ft.Clipboard:
    existing = next((s for s in getattr(page, "services", []) if isinstance(s, ft.Clipboard)), None)
    if existing is not None:
        return existing
    clip = ft.Clipboard()
    page.services.append(clip)
    page.update()
    return clip


async def _set_clipboard_text(page: ft.Page, text: str) -> None:
    with contextlib.suppress(Exception):
        clip = _page_clipboard(page)
        await clip.set(text)
        return
    with contextlib.suppress(Exception):
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()


async def _get_clipboard_text(page: ft.Page) -> str:
    with contextlib.suppress(Exception):
        clip = _page_clipboard(page)
        val = await clip.get()
        if val:
            return val
    with contextlib.suppress(Exception):
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        val = r.clipboard_get()
        r.destroy()
        return str(val or "")
    return ""


def manage_cookies(
    page: ft.Page,
    name: str,
    file_picker: ft.FilePicker,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    log: Callable[[str], None],
) -> None:
    profile = pm.profiles.get(name)
    if not profile:
        return
    is_running = bl.is_running(name)
    profile_dir = os.path.join(DATA_DIR, name)

    def _locked(fn, arg):
        with bl.exclusive(name):
            return fn(name, profile.os_type, arg, engine=profile.engine)

    def _export_sync(fmt: str) -> tuple[str, int]:
        if bl.is_running(name):
            res = bl.control(name, "cookies_export", timeout=15)
            return cookies.format_cookies(res.get("cookies", []), fmt)
        return _locked(cookies.export_cookies, fmt)

    def _import_sync(text: str) -> int:
        if bl.is_running(name):
            parsed = cookies.parse_cookies(text)
            if not parsed:
                raise cookies.CookieError("No valid cookies found in the input")
            res = bl.control(name, "cookies_import", {"cookies": parsed}, timeout=15)
            return int(res.get("imported", len(parsed)))
        return _locked(cookies.import_cookies, text)

    async def on_export(fmt: str) -> None:
        path = await file_picker.save_file(
            dialog_title=get_string(
                "cookie_export_json" if fmt == "json" else "cookie_export_netscape",
            ),
            file_name=f"{name}_cookies.{_EXTENSIONS[fmt]}",
            allowed_extensions=[_EXTENSIONS[fmt]],
        )
        if not path:
            return
        log(get_string("cookie_working", name=name))
        try:
            text, count = await asyncio.to_thread(_export_sync, fmt)
            pathlib.Path(path).write_text(text, encoding="utf-8")
        except Exception as e:
            log(get_string("cookie_error", name=name, error=e))
            return
        log(get_string("cookie_exported", name=name, count=count, path=path))

    async def on_copy_clipboard() -> None:
        log(get_string("cookie_working", name=name))
        try:
            text, count = await asyncio.to_thread(_export_sync, "json")
            await _set_clipboard_text(page, text)
        except Exception as e:
            log(get_string("cookie_error", name=name, error=e))
            return
        log(get_string("cookie_copied_clipboard", name=name, count=count))

    async def on_import_text(raw_text: str) -> None:
        if not raw_text.strip():
            log(get_string("cookie_clipboard_empty"))
            return
        log(get_string("cookie_working", name=name))
        try:
            count = await asyncio.to_thread(_import_sync, raw_text)
        except Exception as e:
            log(get_string("cookie_error", name=name, error=e))
            return
        log(get_string("cookie_imported", name=name, count=count))

    async def on_paste_clipboard() -> None:
        raw = await _get_clipboard_text(page)
        if not raw.strip():
            log(get_string("cookie_clipboard_empty"))
            return
        await on_import_text(raw)

    async def on_import() -> None:
        files = await file_picker.pick_files(
            dialog_title=get_string("cookie_import"),
            allowed_extensions=["json", "txt"],
        )
        if not files or not files[0].path:
            return
        log(get_string("cookie_working", name=name))
        try:
            text = pathlib.Path(files[0].path).read_text(encoding="utf-8-sig")
            count = await asyncio.to_thread(_import_sync, text)
        except (cookies.CookieError, OSError, UnicodeDecodeError, ProfileBusyError) as e:
            log(get_string("cookie_error", name=name, error=e))
            return
        log(get_string("cookie_imported", name=name, count=count))

    def on_reset_fingerprint() -> None:
        def _do_reset() -> None:
            fingerprint.reset(profile_dir)
            log(get_string("fingerprint_was_reset", name=name))

        open_confirm_dialog(
            page,
            name,
            _do_reset,
            title=get_string("confirm_reset_fingerprint", name=name),
            action_label=get_string("fingerprint_reset"),
            action_icon=ft.Icons.FINGERPRINT,
        )

    info = fingerprint.summary(profile_dir)
    fingerprint_line = (
        get_string(
            "fingerprint_current",
            platform=info["platform"],
            screen=info["screen"],
            cores=info["hardware_concurrency"],
        )
        if info
        else get_string("fingerprint_none")
    )

    live_cookies: list[dict] | None = [] if is_running else None
    if is_running:
        with contextlib.suppress(Exception):
            res = bl.control(name, "cookies_export", timeout=1.5)
            live_cookies = res.get("cookies", [])

    summary = cookies.inspect_cookie_summary(name, live_cookies=live_cookies)
    if summary["total"] > 0:
        dom_str = ", ".join(f"{d} ({c})" for d, c in summary["domains"][:4])
        if summary["has_gw"]:
            dom_str += " · GW ✓"
        cookie_summary_line = get_string(
            "cookie_summary_count",
            count=summary["total"],
            domains=dom_str,
        )
    else:
        cookie_summary_line = get_string("cookie_summary_empty")

    open_cookie_dialog(
        page,
        name,
        fingerprint_line,
        on_export,
        on_import,
        on_reset_fingerprint,
        is_running=is_running,
        cookie_summary_line=cookie_summary_line,
        on_copy_clipboard=on_copy_clipboard,
        on_paste_clipboard=on_paste_clipboard,
        on_import_text=on_import_text,
    )
