import asyncio
from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ..dialogs import open_confirm_dialog
from ..state import AppState
from .browser import launch_or_stop


def bulk_delete_profiles(
    page: ft.Page,
    names: list[str],
    pm: IProfileManager,
    log: Callable[[str], None],
    refresh: Callable[[], None],
    on_done: Callable[[], None],
) -> None:
    if not names:
        return

    def do_bulk_trash() -> None:
        for name in names:
            if hasattr(pm, "trash_profile"):
                pm.trash_profile(name)
                log(get_string("moved_to_trash", name=name))
            else:
                pm.delete_profile(name)
                log(get_string("deleted_profile", name=name))
        on_done()
        refresh()

    count = len(names)
    open_confirm_dialog(
        page,
        "",
        do_bulk_trash,
        title=get_string("confirm_bulk_trash", count=count),
        body=get_string("trash_can_restore_note"),
    )


def format_proxy_badge(ok: bool, detail: str) -> tuple[str, str]:
    if ok:
        ip_part = detail.split("IP:", 1)[-1].strip() if "IP:" in detail else detail
        if ":" in ip_part and len(ip_part) > 22:
            # Shorten long IPv6 for compact row display while keeping prefix, suffix & country
            ip_only, _, tail = ip_part.partition(" ")
            segs = ip_only.split(":")
            if len(segs) >= 4:
                ip_part = f"{segs[0]}:..:{segs[-1]}" + (f" {tail}" if tail else "")
        return ("ok", f"✓ {ip_part}")
    reason = detail.split(":", 1)[-1].strip() if ":" in detail else detail
    return ("fail", f"✗ {reason[:28]}")


async def bulk_check_proxies(
    names: list[str],
    pm: IProfileManager,
    ps: IProxyService,
    log: Callable[[str], None],
    state: AppState | None = None,
) -> None:
    proxy_to_names: dict[str, list[str]] = {}
    for name in names:
        p = pm.profiles.get(name)
        if p is not None and p.proxy:
            proxy_to_names.setdefault(p.proxy, []).append(name)
            if state is not None:
                state.proxy_status[name] = ("checking", f"⏳ {get_string('proxy_checking')}")

    if not proxy_to_names:
        return
    if state is not None:
        state.schedule_refresh()

    total_profiles = sum(len(v) for v in proxy_to_names.values())
    log(get_string("bulk_proxy_checking", count=total_profiles))
    ok_count = 0
    fail_count = 0
    sem = asyncio.Semaphore(5)

    async def _one_unique(pstr: str, member_names: list[str]) -> None:
        nonlocal ok_count, fail_count
        async with sem:
            ok, detail = await asyncio.to_thread(ps.check_proxy_sync, pstr, 10)
        badge = format_proxy_badge(ok, detail)
        for pname in member_names:
            if ok:
                ok_count += 1
            else:
                fail_count += 1
            if state is not None:
                state.proxy_status[pname] = badge
            log(f"[{pname}] {detail}")
        if state is not None:
            state.schedule_refresh()

    await asyncio.gather(*(_one_unique(p, ms) for p, ms in proxy_to_names.items()))
    log(get_string("bulk_proxy_summary", ok=ok_count, fail=fail_count))


def bulk_launch_profiles(
    names: list[str],
    pm: IProfileManager,
    bl: IBrowserLauncher,
    state: AppState,
    log: Callable[[str], None],
) -> None:
    for name in names:
        if not bl.is_running(name) and not state.is_loading(name):
            launch_or_stop(name, pm, bl, state, log)


def bulk_stop_profiles(
    names: list[str],
    pm: IProfileManager,
    bl: IBrowserLauncher,
    state: AppState,
    log: Callable[[str], None],
) -> None:
    for name in names:
        if bl.is_running(name):
            launch_or_stop(name, pm, bl, state, log)
