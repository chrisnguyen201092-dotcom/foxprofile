import asyncio
import random
import threading
import time
from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ...services.monitor import MonitorService
from ...services.proxy.geo_check import (
    derive_ip_metadata,
    extract_ip_from_check_detail,
    lookup_ip_isp,
    lookup_ip_risk,
)
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


def format_proxy_badge(
    ok: bool,
    detail: str,
    *,
    ip_type: str = "",
    country: str = "",
) -> tuple[str, str]:
    if ok:
        ip_part = detail.split("IP:", 1)[-1].strip() if "IP:" in detail else detail
        if ":" in ip_part and len(ip_part) > 22:
            # Shorten long IPv6 for compact row display while keeping prefix, suffix & country
            ip_only, _, tail = ip_part.partition(" ")
            segs = ip_only.split(":")
            if len(segs) >= 4:
                ip_part = f"{segs[0]}:..:{segs[-1]}" + (f" {tail}" if tail else "")
        clean_type = (ip_type or "").strip().upper()
        clean_country = (country or "").strip().upper()
        if clean_type:
            if "(" in ip_part and ip_part.endswith(")"):
                base_ip, _, paren = ip_part.rpartition("(")
                inner = paren[:-1].strip()
                if clean_type not in inner:
                    ip_part = f"{base_ip.strip()} ({inner}·{clean_type})"
            elif clean_country:
                ip_part = f"{ip_part} ({clean_country}·{clean_type})"
            else:
                ip_part = f"{ip_part} ({clean_type})"
        return ("ok", f"✓ {ip_part}")
    reason = detail.split(":", 1)[-1].strip() if ":" in detail else detail
    return ("fail", f"✗ {reason[:28]}")


async def bulk_check_proxies(
    names: list[str],
    pm: IProfileManager,
    ps: IProxyService,
    log: Callable[[str], None],
    state: AppState | None = None,
    *,
    bl: IBrowserLauncher | None = None,
    auto_pin_geo: bool = True,
    monitor: MonitorService | None = None,
) -> None:
    proxy_to_names: dict[str, list[str]] = {}
    skip_count = 0
    for name in names:
        p = pm.profiles.get(name)
        if p is not None and p.proxy:
            proxy_to_names.setdefault(p.proxy, []).append(name)
            if state is not None:
                state.proxy_status[name] = ("checking", f"⏳ {get_string('proxy_checking')}")
        elif p is not None:
            skip_count += 1

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
        raw_ip: str | None = None
        country = ""
        auto_tz, auto_loc = (None, None)
        isp_name, ip_type = ("", "")
        if ok:
            raw_ip = extract_ip_from_check_detail(detail)
            if raw_ip and (auto_pin_geo or monitor is not None):
                country, auto_tz, auto_loc = await asyncio.to_thread(
                    derive_ip_metadata,
                    raw_ip,
                    detail,
                )
                isp_name, ip_type = await asyncio.to_thread(lookup_ip_isp, raw_ip)
                risk_data = await asyncio.to_thread(lookup_ip_risk, raw_ip)
                if not isp_name and risk_data.get("isp"):
                    isp_name = str(risk_data["isp"])
                if not ip_type and risk_data.get("ip_type"):
                    ip_type = str(risk_data["ip_type"])
                if monitor is not None and int(risk_data.get("fraud_score", -1)) >= 0:
                    monitor.record_ip_risk(raw_ip, risk_data)
        badge = format_proxy_badge(ok, detail, ip_type=ip_type, country=country)
        for pname in member_names:
            if ok:
                ok_count += 1
                if monitor is not None and raw_ip:
                    monitor.record_exit_ip(
                        pname,
                        raw_ip,
                        country=country,
                        tz=auto_tz or "",
                        proxy=pstr,
                        isp=isp_name,
                        ip_type=ip_type,
                    )
            else:
                fail_count += 1
            if state is not None:
                state.proxy_status[pname] = badge
            log(f"[{pname}] {detail}")
            if (
                ok
                and auto_pin_geo
                and (auto_tz or auto_loc)
                and hasattr(pm, "pin_missing_geo")
                and (bl is None or not bl.is_running(pname))
                and (state is None or not state.is_loading(pname))
                and pm.pin_missing_geo(pname, auto_tz, auto_loc)
            ):
                p_cur = pm.profiles.get(pname)
                log(
                    get_string(
                        "auto_pinned_geo_log",
                        name=pname,
                        tz=(p_cur.timezone if p_cur else auto_tz) or "-",
                        loc=(p_cur.locale if p_cur else auto_loc) or "-",
                    )
                )
        if state is not None:
            state.schedule_refresh()

    await asyncio.gather(*(_one_unique(p, ms) for p, ms in proxy_to_names.items()))
    log(get_string("bulk_proxy_summary", ok=ok_count, fail=fail_count, skip=skip_count))


def bulk_launch_profiles(
    names: list[str],
    pm: IProfileManager,
    bl: IBrowserLauncher,
    state: AppState,
    log: Callable[[str], None],
    *,
    stagger_seconds: tuple[int, int] = (40, 60),
) -> None:
    to_launch = [
        name
        for name in names
        if name in pm.profiles and not bl.is_running(name) and not state.is_loading(name)
    ]
    if not to_launch:
        return

    # Launch the first profile immediately so the user sees instant response
    first = to_launch[0]
    launch_or_stop(first, pm, bl, state, log)

    rest = to_launch[1:]
    if not rest:
        return

    min_s, max_s = stagger_seconds
    if max_s <= 0:
        for name in rest:
            if not bl.is_running(name) and not state.is_loading(name):
                launch_or_stop(name, pm, bl, state, log)
        return

    total = len(to_launch)

    def _stagger_worker() -> None:
        for idx, name in enumerate(rest, start=2):
            delay_s = random.randint(max(1, min_s), max(min_s, max_s))
            log(
                get_string(
                    "bulk_stagger_wait_log",
                    idx=idx,
                    total=total,
                    delay=delay_s,
                    name=name,
                )
            )
            state.schedule_refresh()
            time.sleep(delay_s)
            if name in pm.profiles and not bl.is_running(name) and not state.is_loading(name):
                launch_or_stop(name, pm, bl, state, log)

    threading.Thread(target=_stagger_worker, daemon=True).start()


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
