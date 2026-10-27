import asyncio
import contextlib
import pathlib
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import flet as ft

from ...core.strings import get_string
from ...models.profile import Profile, natural_name_key
from ...services.monitor import (
    MonitorSnapshot,
    ProfileMonitorInfo,
    _ipv6_prefix_64,
    compute_trust_breakdown,
    extract_proxy_cluster,
    extract_proxy_endpoint,
    extract_proxy_port,
    is_gateway_ipv6_cluster,
)
from ...services.proxy.geo_check import (
    derive_ip_metadata,
    extract_ip_from_check_detail,
    lookup_ip_isp,
    lookup_ip_risk,
)
from ...utils.proxy_checker import check_proxy_sync
from ...utils.proxy_parser import mask_proxy
from ...utils.totp import fetch_hotmail_messages, parse_mail_credentials_line
from ...utils.validation import validate_profile_name, validate_proxy_format
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_MEDIUM, FONT_SEMIBOLD
from ..theme.page import build_os_dropdown
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, OUTLINE_STYLE

_DATA = ft.TextStyle(font_family=FONT_DATA, size=12.5)


def _field(label: str, control: ft.Control) -> ft.Column:
    """Label placed cleanly above its input control (never floating inside the box)."""
    return ft.Column(
        spacing=5,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Text(label, size=12.5, font_family=FONT_SEMIBOLD, color=COLORS["text_sub"]),
            control,
        ],
    )


def open_batch_create_dialog(
    page: ft.Page,
    existing_names: set[str],
    on_create: Callable[[list[tuple[str, str | None]], str, str, str, str, bool], None],
    on_switch_single: Callable[[], None] | None = None,
) -> None:
    """Modal dialog to batch-create profiles by pasting names and proxies."""
    lower_existing = {n.casefold() for n in existing_names}

    names_box = ft.TextField(
        hint_text=get_string("batch_names_hint"),
        multiline=True,
        min_lines=5,
        max_lines=6,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    proxies_box = ft.TextField(
        hint_text=get_string("batch_proxies_hint"),
        multiline=True,
        min_lines=5,
        max_lines=6,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    repeat_cb = ft.Checkbox(
        label=get_string("batch_repeat_proxy_cb"),
        value=False,
        label_style=ft.TextStyle(size=12.5, font_family=FONT_MEDIUM, color=COLORS["text_main"]),
    )
    auto_geo_cb = ft.Checkbox(
        label=get_string("batch_auto_geo_cb"),
        value=True,
        label_style=ft.TextStyle(size=12.5, font_family=FONT_MEDIUM, color=COLORS["text_main"]),
    )
    os_dropdown = build_os_dropdown("windows")
    os_dropdown.label = None
    os_dropdown.text_size = 14
    os_dropdown.expand = True

    group_field = ft.TextField(
        hint_text=get_string("group_hint"),
        **DLG_FIELD_KWARGS,
    )
    tz_field = ft.TextField(
        hint_text="America/Detroit",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    loc_field = ft.TextField(
        hint_text="en-US",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )

    summary_text = ft.Text(
        get_string("batch_preview_ready", valid=0, skipped=0),
        size=12,
        font_family=FONT_SEMIBOLD,
        color=COLORS["text_sub"],
    )
    preview_col = ft.Column(
        spacing=4,
        height=120,
        scroll=ft.ScrollMode.AUTO,
        visible=False,
    )

    def _compute() -> tuple[list[tuple[str, str | None]], int, list[ft.Control]]:
        name_lines = [ln.strip() for ln in (names_box.value or "").splitlines() if ln.strip()]
        proxy_lines = [ln.strip() for ln in (proxies_box.value or "").splitlines() if ln.strip()]
        repeat = bool(repeat_cb.value)
        valid_entries: list[tuple[str, str | None]] = []
        skipped = 0
        seen_batch: set[str] = set()
        rows: list[ft.Control] = []

        for idx, pname in enumerate(name_lines):
            raw_proxy: str | None = None
            if proxy_lines:
                if idx < len(proxy_lines):
                    raw_proxy = proxy_lines[idx]
                elif repeat:
                    raw_proxy = proxy_lines[idx % len(proxy_lines)]

            name_ok, _ = validate_profile_name(pname)
            low = pname.casefold()
            if not name_ok:
                skipped += 1
                st_text = f"✗ {get_string('batch_status_invalid_name')}"
                st_col = COLORS["error"]
            elif low in lower_existing or low in seen_batch:
                skipped += 1
                st_text = f"⚠ {get_string('batch_status_exists')}"
                st_col = COLORS["warning"]
            elif raw_proxy and not validate_proxy_format(raw_proxy)[0]:
                skipped += 1
                st_text = f"✗ {get_string('batch_status_invalid_proxy')}"
                st_col = COLORS["error"]
            else:
                seen_batch.add(low)
                valid_entries.append((pname, raw_proxy))
                masked = mask_proxy(raw_proxy) if raw_proxy else get_string("direct_short")
                st_text = f"✓ {masked}"
                st_col = COLORS["success"] if raw_proxy else COLORS["text_dim"]

            rows.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                    border_radius=6,
                    bgcolor=COLORS["input_bg"],
                    content=ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(
                                f"{idx + 1}. {pname}",
                                size=12,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_main"],
                                width=210,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                st_text,
                                size=11.5,
                                font_family=FONT_DATA,
                                color=st_col,
                                expand=True,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                    ),
                )
            )
        return valid_entries, skipped, rows

    def _refresh_preview(_: ft.ControlEvent | None = None) -> None:
        names_box.error = None
        valid_entries, skipped, rows = _compute()
        summary_text.value = get_string(
            "batch_preview_ready",
            valid=len(valid_entries),
            skipped=skipped,
        )
        preview_col.controls = rows
        preview_col.visible = bool(rows)
        with contextlib.suppress(Exception):
            page.update()

    names_box.on_change = _refresh_preview
    proxies_box.on_change = _refresh_preview
    repeat_cb.on_change = _refresh_preview

    def _submit(_: ft.ControlEvent) -> None:
        valid_entries, _, _ = _compute()
        if not valid_entries:
            names_box.error = get_string("enter_profile_name")
            page.update()
            return
        page.pop_dialog()
        on_create(
            valid_entries,
            os_dropdown.value or "windows",
            (group_field.value or "").strip(),
            (tz_field.value or "").strip(),
            (loc_field.value or "").strip(),
            bool(auto_geo_cb.value),
        )

    def _switch_to_single(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        if on_switch_single is not None:
            on_switch_single()

    header_row = ft.Row(
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Text(
                get_string("batch_create_title"),
                size=19,
                font_family=FONT_BOLD,
                color=COLORS["text_main"],
            ),
            *(
                [
                    ft.OutlinedButton(
                        get_string("create_new_profile"),
                        icon=ft.Icons.PERSON_ADD_ALT_1_OUTLINED,
                        height=32,
                        style=OUTLINE_STYLE,
                        on_click=_switch_to_single,
                    )
                ]
                if on_switch_single is not None
                else []
            ),
        ],
    )

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Column(
            spacing=4,
            tight=True,
            controls=[
                header_row,
                ft.Text(
                    get_string("batch_create_subtitle"),
                    size=13,
                    color=COLORS["text_sub"],
                ),
            ],
        ),
        content=ft.Container(
            width=780,
            content=ft.Column(
                spacing=10,
                tight=True,
                scroll=ft.ScrollMode.AUTO,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Row(
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        controls=[
                            ft.Container(
                                expand=1,
                                content=_field(get_string("batch_names_label"), names_box),
                            ),
                            ft.Container(
                                expand=1,
                                content=_field(get_string("batch_proxies_label"), proxies_box),
                            ),
                        ],
                    ),
                    ft.Row(
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        controls=[
                            ft.Container(
                                expand=2,
                                content=_field(get_string("operating_system"), os_dropdown),
                            ),
                            ft.Container(
                                expand=3,
                                content=_field(get_string("group_optional"), group_field),
                            ),
                        ],
                    ),
                    ft.Row(
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        controls=[
                            ft.Container(
                                expand=1,
                                content=_field(get_string("timezone_optional"), tz_field),
                            ),
                            ft.Container(
                                expand=1,
                                content=_field(get_string("locale_optional"), loc_field),
                            ),
                        ],
                    ),
                    ft.Column(
                        spacing=2,
                        tight=True,
                        controls=[repeat_cb, auto_geo_cb],
                    ),
                    summary_text,
                    preview_col,
                ],
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("cancel"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
            ft.Button(
                get_string("batch_create_submit"),
                icon=ft.Icons.PLAYLIST_ADD_CHECK,
                style=ACCENT_STYLE,
                on_click=_submit,
            ),
        ],
    )
    page.show_dialog(dlg)


def open_backup_restore_dialog(
    page: ft.Page,
    get_backups: Callable[[], list[dict[str, Any]]],
    is_any_running: Callable[[], bool],
    on_restore: Callable[[pathlib.Path], None],
) -> None:
    """Modal dialog to inspect and safely restore from automatic daily config backups."""
    notice_text = ft.Text(
        "",
        size=12.5,
        color=COLORS["error"],
        visible=False,
    )
    list_col = ft.Column(
        spacing=8,
        height=480,
        scroll=ft.ScrollMode.AUTO,
    )

    def _rebuild() -> None:
        items = get_backups()
        list_col.controls.clear()
        if not items:
            list_col.controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=40),
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("backup_empty"),
                        size=13.5,
                        color=COLORS["text_dim"],
                    ),
                )
            )
            return

        for item in items:
            bpath = item["path"]
            fname = str(item.get("name", ""))
            meta_str = get_string(
                "backup_item_meta",
                count=item.get("total", 0),
                missing=item.get("missing", 0),
                changed=item.get("changed", 0),
                mtime=item.get("mtime_str", ""),
            )

            def _make_restore(target_path: pathlib.Path) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    if is_any_running():
                        notice_text.value = get_string("backup_stop_browsers_first")
                        notice_text.visible = True
                        page.update()
                        return
                    page.pop_dialog()
                    on_restore(target_path)

                return _cb

            list_col.controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                    border_radius=8,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(1, COLORS["border"]),
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Column(
                                spacing=2,
                                expand=True,
                                controls=[
                                    ft.Text(
                                        fname,
                                        size=13.5,
                                        font_family=FONT_DATA,
                                        weight=ft.FontWeight.W_600,
                                        color=COLORS["text_main"],
                                    ),
                                    ft.Text(
                                        meta_str,
                                        size=11.5,
                                        color=COLORS["text_sub"],
                                    ),
                                ],
                            ),
                            ft.OutlinedButton(
                                get_string("backup_restore_btn"),
                                icon=ft.Icons.HISTORY,
                                height=34,
                                style=OUTLINE_STYLE,
                                on_click=_make_restore(bpath),
                            ),
                        ],
                    ),
                )
            )

    _rebuild()

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Text(
            get_string("backup_dialog_title"),
            size=19,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=760,
            content=ft.Column(
                spacing=10,
                tight=True,
                controls=[
                    ft.Text(
                        get_string("backup_dialog_subtitle"),
                        size=12.5,
                        color=COLORS["text_sub"],
                    ),
                    notice_text,
                    list_col,
                ],
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    page.show_dialog(dlg)



def open_proxy_pool_dialog(
    page: ft.Page,
    get_profiles: Callable[[], list[Profile]],
    get_running: Callable[[], set[str]],
    get_proxy_status: Callable[[], dict[str, tuple[str, str]]],
    on_check_all: Callable[[], None],
    on_check_one: Callable[[str], None],
    on_replace: Callable[[str, str], tuple[bool, str]],
    *,
    get_unassigned: Callable[[], list[str]] | None = None,
    on_add_to_pool: Callable[[list[str]], tuple[int, int]] | None = None,
    on_remove_from_pool: Callable[[str], bool] | None = None,
    on_assign_to_profile: Callable[[str, str], bool] | None = None,
    get_exit_ips: Callable[[], dict[str, str]] | None = None,
) -> None:
    """Central Proxy Manager dialog: view all unique proxies, spare unassigned proxies, test, assign, or replace."""
    notice_text = ft.Text("", size=12.5, color=COLORS["error"], visible=False)
    stats_text = ft.Text("", size=12.5, font_family=FONT_SEMIBOLD, color=COLORS["text_sub"])
    list_col = ft.Column(spacing=6, height=600, scroll=ft.ScrollMode.AUTO)
    editing_target: list[str | None] = [None]
    assigning_target: list[str | None] = [None]
    adding_open: list[bool] = [False]
    local_px_status: dict[str, tuple[str, str]] = {}

    def _check_single_proxy(px_target: str, first_prof: str | None = None) -> None:
        local_px_status[px_target] = ("checking", f"⏳ {get_string('proxy_checking')}")
        if first_prof is not None:
            on_check_one(first_prof)
        _rebuild()
        with contextlib.suppress(Exception):
            page.update()

        def _worker() -> None:
            ok, detail = check_proxy_sync(px_target, timeout=10)
            local_px_status[px_target] = (
                ("ok", f"✓ {detail}") if ok else ("error", f"✗ {detail}")
            )
            _rebuild()
            with contextlib.suppress(Exception):
                page.update()

        threading.Thread(target=_worker, daemon=True).start()

    def _check_proxy_list(px_targets: list[str], also_call_all: bool = False) -> None:
        if not px_targets:
            return
        for p_item in px_targets:
            local_px_status[p_item] = ("checking", f"⏳ {get_string('proxy_checking')}")
        if also_call_all:
            with contextlib.suppress(Exception):
                on_check_all()
        _rebuild()
        with contextlib.suppress(Exception):
            page.update()

        def _batch_worker() -> None:
            def _one(p_item: str) -> None:
                ok, detail = check_proxy_sync(p_item, timeout=10)
                local_px_status[p_item] = (
                    ("ok", f"✓ {detail}") if ok else ("error", f"✗ {detail}")
                )

            with ThreadPoolExecutor(max_workers=6) as ex:
                list(ex.map(_one, px_targets))
            _rebuild()
            with contextlib.suppress(Exception):
                page.update()

        threading.Thread(target=_batch_worker, daemon=True).start()

    add_input = ft.TextField(
        hint_text=get_string("batch_proxies_hint"),
        multiline=True,
        min_lines=2,
        max_lines=4,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    add_box = ft.Container(
        visible=False,
        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        border_radius=8,
        bgcolor=COLORS["input_bg"],
        border=ft.Border.all(1, COLORS["rule_strong"]),
    )

    def _toggle_add(_: ft.ControlEvent) -> None:
        adding_open[0] = not adding_open[0]
        add_box.visible = adding_open[0]
        notice_text.visible = False
        page.update()

    def _submit_add(_: ft.ControlEvent) -> None:
        if on_add_to_pool is None:
            return
        lines = [ln.strip() for ln in (add_input.value or "").splitlines() if ln.strip()]
        if not lines:
            return
        added, skipped = on_add_to_pool(lines)
        add_input.value = ""
        adding_open[0] = False
        add_box.visible = False
        notice_text.value = get_string("proxy_pool_added_log", added=added, skipped=skipped)
        notice_text.color = COLORS["success"] if added > 0 else COLORS["warning"]
        notice_text.visible = True
        _rebuild()
        page.update()

    add_box.content = ft.Column(
        spacing=8,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Text(
                get_string("proxy_pool_add_label"),
                size=12,
                font_family=FONT_SEMIBOLD,
                color=COLORS["text_sub"],
            ),
            add_input,
            ft.Row(
                alignment=ft.MainAxisAlignment.END,
                spacing=8,
                controls=[
                    ft.TextButton(
                        get_string("cancel"),
                        style=ft.ButtonStyle(color=COLORS["text_sub"]),
                        on_click=_toggle_add,
                    ),
                    ft.Button(
                        get_string("proxy_pool_add_submit"),
                        icon=ft.Icons.ADD,
                        height=34,
                        style=ACCENT_STYLE,
                        on_click=_submit_add,
                    ),
                ],
            ),
        ],
    )

    def _rebuild() -> None:
        profiles = get_profiles()
        running = get_running()
        status_map = get_proxy_status()
        unassigned_list = get_unassigned() if get_unassigned is not None else []
        saved_ips = get_exit_ips() if get_exit_ips is not None else {}

        def _is_prof_disconnected(prof_obj: Profile) -> bool:
            grp = (prof_obj.group or "").strip().lower()
            pgs = [pg.strip().lower() for pg in (prof_obj.pages or [])]
            return bool(prof_obj.banned_at or grp == "disconnected" or "disconnected" in pgs)

        prof_by_name = {p.name: p for p in profiles}
        by_proxy: dict[str, list[str]] = {}
        direct_count = 0
        for p in profiles:
            px = (p.proxy or "").strip()
            if not px:
                direct_count += 1
            else:
                by_proxy.setdefault(px, []).append(p.name)

        # Resolve known exit IP for every proxy and compute exact-IP & /64 subnet separation
        resolved_px_ip: dict[str, str] = {}
        ip_to_eps: dict[str, set[str]] = {}
        v6_to_eps: dict[str, set[str]] = {}
        cl_to_eps: dict[str, set[str]] = {}

        for free_px in unassigned_list:
            ep_key = extract_proxy_endpoint(free_px) or free_px
            cl_k = extract_proxy_cluster(free_px)
            if cl_k and not is_gateway_ipv6_cluster(cl_k):
                cl_to_eps.setdefault(cl_k, set()).add(ep_key)
            f_st = local_px_status.get(free_px)
            raw_ip = extract_ip_from_check_detail(f_st[1]) if (f_st and f_st[0] == "ok") else ""
            if raw_ip:
                resolved_px_ip[free_px] = raw_ip
                ip_to_eps.setdefault(raw_ip.lower(), set()).add(ep_key)
                v6_p = _ipv6_prefix_64(raw_ip)
                if v6_p:
                    v6_to_eps.setdefault(v6_p, set()).add(ep_key)

        for px_str, member_names in by_proxy.items():
            all_disc = all(
                _is_prof_disconnected(prof_by_name[m])
                for m in member_names
                if m in prof_by_name
            )
            ep_key = extract_proxy_endpoint(px_str) or px_str
            cl_k = extract_proxy_cluster(px_str)
            if not all_disc and cl_k and not is_gateway_ipv6_cluster(cl_k):
                cl_to_eps.setdefault(cl_k, set()).add(ep_key)
            first_m = member_names[0]
            st_p = local_px_status.get(px_str) or status_map.get(first_m)
            raw_ip = extract_ip_from_check_detail(st_p[1]) if (st_p and st_p[0] == "ok") else ""
            if not raw_ip:
                for m_name in member_names:
                    if saved_ips.get(m_name):
                        raw_ip = saved_ips[m_name].strip()
                        break
            if raw_ip:
                resolved_px_ip[px_str] = raw_ip
                if not all_disc:
                    ip_to_eps.setdefault(raw_ip.lower(), set()).add(ep_key)
                    v6_p = _ipv6_prefix_64(raw_ip)
                    if v6_p:
                        v6_to_eps.setdefault(v6_p, set()).add(ep_key)

        def _build_subnet_badge(px_str: str, *, is_retired: bool = False) -> ft.Control | None:
            if is_retired:
                return ft.Container(
                    padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                    border_radius=4,
                    bgcolor=ft.Colors.with_opacity(0.12, COLORS["text_dim"]),
                    border=ft.Border.all(1, COLORS["rule_strong"]),
                    content=ft.Text(
                        "Ngưng dùng (Disconnected)",
                        size=10,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["text_dim"],
                    ),
                )
            ip_val = resolved_px_ip.get(px_str, "")
            cl_key = extract_proxy_cluster(px_str)
            is_gw = is_gateway_ipv6_cluster(cl_key)
            cl_cnt = len(cl_to_eps.get(cl_key, set())) if (cl_key and not is_gw) else 0
            if not ip_val:
                if cl_key and not is_gw and cl_cnt > 1:
                    col = COLORS["warning"]
                    return ft.Container(
                        padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                        border_radius=4,
                        bgcolor=ft.Colors.with_opacity(0.13, col),
                        border=ft.Border.all(1, col),
                        tooltip=f"Thuộc máy chủ /64 {cl_key} ({cl_cnt} cổng đang gắn) — hệ thống tự động xếp lịch cách ca",
                        content=ft.Text(
                            f"🔗 Cụm /64 {cl_key} · {cl_cnt} cổng",
                            size=10,
                            font_family=FONT_SEMIBOLD,
                            color=col,
                        ),
                    )
                if cl_key and not is_gw and cl_cnt == 1:
                    col = COLORS["success"]
                    return ft.Container(
                        padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                        border_radius=4,
                        bgcolor=ft.Colors.with_opacity(0.13, col),
                        border=ft.Border.all(1, col),
                        tooltip=f"Máy chủ /64 {cl_key} duy nhất trong kho — mở song song an toàn 100%",
                        content=ft.Text(
                            "🛡️ Độc lập /64",
                            size=10,
                            font_family=FONT_SEMIBOLD,
                            color=col,
                        ),
                    )
                return None
            ip_low = ip_val.lower()
            v6_p = _ipv6_prefix_64(ip_val)
            dup_ip_cnt = len(ip_to_eps.get(ip_low, set()))
            dup_v6_cnt = max(len(v6_to_eps.get(v6_p, set())) if v6_p else 0, cl_cnt)

            if dup_ip_cnt > 1:
                lbl = f"⛔ TRÙNG IP · {dup_ip_cnt} cổng"
                col = COLORS["error"]
                tip = f"Trùng chính xác địa chỉ IP {ip_val} trên {dup_ip_cnt} cổng proxy khác nhau!"
            elif v6_p and is_gw and dup_v6_cnt > 1:
                lbl = f"⚠️ TRÙNG /64 GATEWAY · {dup_v6_cnt} cổng"
                col = COLORS["error"]
                tip = f"Trùng dải /64 ({v6_p}::/64) trên {dup_v6_cnt} cổng Gateway!"
            elif v6_p and dup_v6_cnt <= 1:
                lbl = "🛡️ Độc lập /64"
                col = COLORS["success"]
                tip = f"Dải /64 riêng biệt hoàn toàn: {v6_p}::/64 (mở song song an toàn 100%)"
            elif v6_p and dup_v6_cnt > 1:
                lbl = f"🔗 Cụm /64 {cl_key or v6_p[-4:]} · {dup_v6_cnt} cổng"
                col = COLORS["warning"]
                tip = (
                    f"Chung máy chủ /64 ({v6_p}::/64) nhưng khác IP /128 ({ip_val}) — "
                    "FoxProfile tự động chặn mở cùng lúc và chia khác ca/ngày"
                )
            else:
                lbl = "🛡️ IP Độc lập"
                col = COLORS["success"]
                tip = f"IP riêng biệt: {ip_val}"

            return ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                border_radius=4,
                bgcolor=ft.Colors.with_opacity(0.13, col),
                border=ft.Border.all(1, col),
                tooltip=tip,
                content=ft.Text(
                    lbl,
                    size=10,
                    font_family=FONT_SEMIBOLD,
                    color=col,
                ),
            )

        assigned_count = len(profiles) - direct_count
        stats_text.value = get_string(
            "proxy_pool_stats_full",
            ports=len(by_proxy),
            free=len(unassigned_list),
            assigned=assigned_count,
            direct=direct_count,
        )

        list_col.controls.clear()
        if not by_proxy and not unassigned_list:
            list_col.controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=40),
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("proxy_pool_empty"),
                        size=13.5,
                        color=COLORS["text_dim"],
                    ),
                )
            )
            return

        # 1. Render unassigned (spare) proxies first so user can see/assign them immediately
        for free_px in unassigned_list:
            free_port = extract_proxy_port(free_px) or "PROXY"
            free_masked = mask_proxy(free_px)
            is_assigning = assigning_target[0] == free_px
            free_st = local_px_status.get(free_px)
            free_top_badges: list[ft.Control] = []
            free_sub_controls: list[ft.Control] = [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                    border_radius=4,
                    bgcolor=ft.Colors.with_opacity(0.14, COLORS["success"]),
                    border=ft.Border.all(1, COLORS["success"]),
                    content=ft.Text(
                        get_string("proxy_pool_unassigned_badge"),
                        size=10,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["success"],
                    ),
                ),
                ft.Text(
                    get_string("proxy_pool_unassigned_sub"),
                    size=11.5,
                    color=COLORS["text_sub"],
                ),
            ]
            f_sub_badge = _build_subnet_badge(free_px)
            if f_sub_badge is not None:
                free_top_badges.append(f_sub_badge)
            if free_st is not None:
                f_kind, f_msg = free_st
                f_col = (
                    COLORS["success"]
                    if f_kind == "ok"
                    else (COLORS["accent"] if f_kind == "checking" else COLORS["error"])
                )
                free_sub_controls.extend(
                    [
                        ft.Text("·", size=11.5, color=COLORS["text_dim"]),
                        ft.Text(
                            f_msg,
                            size=11.5,
                            font_family=FONT_DATA,
                            color=f_col,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                    ]
                )

            def _toggle_assign(target_px: str = free_px) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    notice_text.visible = False
                    assigning_target[0] = None if assigning_target[0] == target_px else target_px
                    _rebuild()
                    page.update()

                return _cb

            def _do_remove(target_px: str = free_px) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    if on_remove_from_pool is not None and on_remove_from_pool(target_px):
                        _rebuild()
                        page.update()

                return _cb

            def _do_assign(
                prof_name: str,
                target_px: str = free_px,
                port_lbl: str = free_port,
            ) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    if on_assign_to_profile is not None and on_assign_to_profile(
                        prof_name, target_px
                    ):
                        assigning_target[0] = None
                        notice_text.value = get_string(
                            "proxy_pool_assigned_log",
                            port=port_lbl,
                            name=prof_name,
                        )
                        notice_text.color = COLORS["success"]
                        notice_text.visible = True
                        _rebuild()
                        page.update()

                return _cb

            free_children: list[ft.Control] = [
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                    controls=[
                        ft.Container(
                            expand=True,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                            content=ft.Column(
                                spacing=3,
                                controls=[
                                    ft.Row(
                                        spacing=8,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        controls=[
                                            ft.Container(
                                                padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                                                border_radius=5,
                                                bgcolor=COLORS["accent"],
                                                content=ft.Text(
                                                    free_port,
                                                    size=11.5,
                                                    font_family=FONT_DATA,
                                                    color="#FFFFFF",
                                                ),
                                            ),
                                            ft.Text(
                                                free_masked,
                                                size=12.5,
                                                font_family=FONT_DATA,
                                                color=COLORS["text_main"],
                                                no_wrap=True,
                                                overflow=ft.TextOverflow.ELLIPSIS,
                                            ),
                                            *free_top_badges,
                                        ],
                                    ),
                                    ft.Row(
                                        spacing=6,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        controls=free_sub_controls,
                                    ),
                                ],
                            ),
                        ),
                        ft.Row(
                            spacing=6,
                            tight=True,
                            controls=[
                                ft.OutlinedButton(
                                    get_string("pool_check_one_btn"),
                                    icon=ft.Icons.WIFI_FIND,
                                    height=32,
                                    style=OUTLINE_STYLE,
                                    tooltip=get_string("check_proxy"),
                                    on_click=lambda _, target_px=free_px: _check_single_proxy(
                                        target_px
                                    ),
                                ),
                                *(
                                    [
                                        ft.OutlinedButton(
                                            get_string("proxy_pool_assign_btn"),
                                            icon=ft.Icons.PERSON_ADD_ALT_1_OUTLINED,
                                            height=32,
                                            style=OUTLINE_STYLE,
                                            on_click=_toggle_assign(),
                                        )
                                    ]
                                    if on_assign_to_profile is not None and profiles
                                    else []
                                ),
                                *(
                                    [
                                        ft.IconButton(
                                            icon=ft.Icons.DELETE_OUTLINE,
                                            icon_size=17,
                                            width=32,
                                            height=32,
                                            padding=0,
                                            icon_color=COLORS["text_sub"],
                                            tooltip=get_string("proxy_pool_remove_tooltip"),
                                            on_click=_do_remove(),
                                        )
                                    ]
                                    if on_remove_from_pool is not None
                                    else []
                                ),
                            ],
                        ),
                    ],
                )
            ]

            if is_assigning and profiles:
                # Prioritize profiles without proxy, then other non-running profiles
                candidates = sorted(
                    [p for p in profiles if p.name not in running],
                    key=lambda p: (bool(p.proxy), natural_name_key(p.name)),
                )
                free_children.append(
                    ft.Column(
                        spacing=5,
                        tight=True,
                        controls=[
                            ft.Text(
                                get_string("proxy_pool_assign_hint"),
                                size=11.5,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_sub"],
                            ),
                            ft.Row(
                                wrap=True,
                                spacing=6,
                                run_spacing=6,
                                controls=[
                                    ft.Container(
                                        padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                                        border_radius=5,
                                        bgcolor=COLORS["card_bg"],
                                        border=ft.Border.all(
                                            1,
                                            COLORS["accent"]
                                            if not cand.proxy
                                            else COLORS["rule_strong"],
                                        ),
                                        ink=True,
                                        on_click=_do_assign(cand.name),
                                        content=ft.Text(
                                            cand.name
                                            + (
                                                f" ({get_string('no_proxy_short')})"
                                                if not cand.proxy
                                                else ""
                                            ),
                                            size=11.5,
                                            font_family=FONT_MEDIUM,
                                            color=COLORS["accent"]
                                            if not cand.proxy
                                            else COLORS["text_main"],
                                        ),
                                    )
                                    for cand in candidates[:32]
                                ],
                            ),
                        ],
                    )
                )

            list_col.controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=7),
                    border_radius=8,
                    bgcolor=COLORS["card_bg"],
                    border=ft.Border.all(1, COLORS["accent"]),
                    content=ft.Column(spacing=8, tight=True, controls=free_children),
                )
            )

        sorted_items = sorted(
            by_proxy.items(),
            key=lambda kv: (
                all(
                    _is_prof_disconnected(prof_by_name[m])
                    for m in kv[1]
                    if m in prof_by_name
                ),
                extract_proxy_port(kv[0]) or kv[0],
            ),
        )
        for px_str, member_names in sorted_items:
            port_badge = extract_proxy_port(px_str) or "PROXY"
            masked = mask_proxy(px_str)
            live_members = [m for m in member_names if m in running]
            first_member = member_names[0]
            st_pair = local_px_status.get(px_str) or status_map.get(first_member)
            is_retired_px = all(
                _is_prof_disconnected(prof_by_name[m])
                for m in member_names
                if m in prof_by_name
            )

            top_badges: list[ft.Control] = []
            sub_badge = _build_subnet_badge(px_str, is_retired=is_retired_px)
            if sub_badge is not None:
                top_badges.append(sub_badge)

            bottom_controls: list[ft.Control] = []
            if live_members:
                bottom_controls.append(
                    ft.Text(
                        f"● {get_string('stamp_live')}",
                        size=11.5,
                        font_family=FONT_DATA,
                        color=COLORS["success"],
                    )
                )
            bottom_controls.append(
                ft.Text(
                    f"{len(member_names)} profile: " + ", ".join(member_names),
                    size=11.5,
                    color=COLORS["text_sub"],
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    tooltip=", ".join(member_names),
                )
            )
            if st_pair is not None:
                st_kind, st_msg = st_pair
                st_col = (
                    COLORS["success"]
                    if st_kind == "ok"
                    else (COLORS["accent"] if st_kind == "checking" else COLORS["error"])
                )
                bottom_controls.extend(
                    [
                        ft.Text("·", size=11.5, color=COLORS["text_dim"]),
                        ft.Text(
                            st_msg,
                            size=11.5,
                            font_family=FONT_DATA,
                            color=st_col,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                    ]
                )
            elif resolved_px_ip.get(px_str):
                bottom_controls.extend(
                    [
                        ft.Text("·", size=11.5, color=COLORS["text_dim"]),
                        ft.Text(
                            f"✓ {resolved_px_ip[px_str]}",
                            size=11.5,
                            font_family=FONT_DATA,
                            color=COLORS["text_sub"],
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                    ]
                )

            is_editing = editing_target[0] == px_str
            replace_input = ft.TextField(
                hint_text=get_string("proxy_pool_replace_hint"),
                text_style=_DATA,
                expand=True,
                **DLG_FIELD_KWARGS,
            )

            def _toggle_replace(target_px: str = px_str) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    notice_text.visible = False
                    editing_target[0] = None if editing_target[0] == target_px else target_px
                    _rebuild()
                    page.update()

                return _cb

            def _apply_replace(
                target_px: str = px_str,
                tf: ft.TextField = replace_input,
                live_list: list[str] = live_members,
                override_px: str | None = None,
            ) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    if live_list:
                        notice_text.value = get_string(
                            "proxy_pool_stop_before_replace",
                            names=", ".join(live_list),
                        )
                        notice_text.color = COLORS["error"]
                        notice_text.visible = True
                        page.update()
                        return
                    new_px = (override_px if override_px is not None else (tf.value or "")).strip()
                    if not new_px or not validate_proxy_format(new_px)[0]:
                        tf.error = get_string("validation_invalid_proxy")
                        page.update()
                        return
                    ok, msg = on_replace(target_px, new_px)
                    notice_text.value = msg
                    notice_text.color = COLORS["success"] if ok else COLORS["error"]
                    notice_text.visible = bool(msg)
                    if ok:
                        editing_target[0] = None
                    _rebuild()
                    page.update()

                return _cb

            def _check_this(
                target_px: str = px_str,
                first_prof: str = first_member,
            ) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    _check_single_proxy(target_px, first_prof)

                return _cb

            card_children: list[ft.Control] = [
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Container(
                            expand=True,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                            content=ft.Column(
                                spacing=3,
                                controls=[
                                    ft.Row(
                                        spacing=8,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        controls=[
                                            ft.Container(
                                                padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                                                border_radius=5,
                                                bgcolor=COLORS["sidebar"],
                                                content=ft.Text(
                                                    port_badge,
                                                    size=11.5,
                                                    font_family=FONT_DATA,
                                                    color=COLORS["cover_ink"],
                                                ),
                                            ),
                                            ft.Text(
                                                masked,
                                                size=12.5,
                                                font_family=FONT_DATA,
                                                color=COLORS["text_main"],
                                                no_wrap=True,
                                                overflow=ft.TextOverflow.ELLIPSIS,
                                            ),
                                            *top_badges,
                                        ],
                                    ),
                                    ft.Row(
                                        spacing=6,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        controls=bottom_controls,
                                    ),
                                ],
                            ),
                        ),
                        ft.Row(
                            spacing=6,
                            tight=True,
                            controls=[
                                ft.OutlinedButton(
                                    get_string("pool_check_one_btn"),
                                    icon=ft.Icons.WIFI_FIND,
                                    height=32,
                                    style=OUTLINE_STYLE,
                                    tooltip=get_string("check_proxy"),
                                    on_click=_check_this(),
                                ),
                                ft.OutlinedButton(
                                    get_string("proxy_pool_replace_btn"),
                                    icon=ft.Icons.SWAP_HORIZ,
                                    height=32,
                                    style=OUTLINE_STYLE,
                                    on_click=_toggle_replace(),
                                ),
                            ],
                        ),
                    ],
                ),
            ]
            if is_editing:
                replace_input.on_submit = _apply_replace()
                card_children.append(
                    ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            replace_input,
                            ft.Button(
                                get_string("proxy_pool_replace_apply"),
                                icon=ft.Icons.CHECK,
                                height=38,
                                style=ACCENT_STYLE,
                                on_click=_apply_replace(),
                            ),
                        ],
                    )
                )
                if unassigned_list:
                    card_children.append(
                        ft.Column(
                            spacing=4,
                            tight=True,
                            controls=[
                                ft.Text(
                                    get_string("proxy_pool_pick_spare"),
                                    size=11.5,
                                    color=COLORS["text_sub"],
                                ),
                                ft.Row(
                                    wrap=True,
                                    spacing=6,
                                    run_spacing=6,
                                    controls=[
                                        ft.Container(
                                            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                                            border_radius=5,
                                            bgcolor=COLORS["card_bg"],
                                            border=ft.Border.all(1, COLORS["accent"]),
                                            ink=True,
                                            tooltip=mask_proxy(sp),
                                            on_click=_apply_replace(override_px=sp),
                                            content=ft.Text(
                                                extract_proxy_port(sp) or mask_proxy(sp),
                                                size=11.5,
                                                font_family=FONT_DATA,
                                                color=COLORS["accent"],
                                            ),
                                        )
                                        for sp in unassigned_list[:12]
                                    ],
                                ),
                            ],
                        )
                    )

            list_col.controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=7),
                    border_radius=8,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(
                        1.5 if live_members else 1,
                        COLORS["success"] if live_members else COLORS["border"],
                    ),
                    content=ft.Column(spacing=8, tight=True, controls=card_children),
                )
            )

    _rebuild()

    def _handle_check_all(_: ft.ControlEvent) -> None:
        spares = get_unassigned() if get_unassigned is not None else []
        assigned = sorted(
            {
                (p.proxy or "").strip()
                for p in get_profiles()
                if (p.proxy or "").strip()
            }
        )
        all_px = list(dict.fromkeys([*spares, *assigned]))
        _check_proxy_list(all_px, also_call_all=True)

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=18, vertical=14),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Column(
            spacing=4,
            tight=True,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            get_string("proxy_pool_title"),
                            size=19,
                            font_family=FONT_BOLD,
                            color=COLORS["text_main"],
                        ),
                        ft.Row(
                            spacing=8,
                            tight=True,
                            controls=[
                                *(
                                    [
                                        ft.OutlinedButton(
                                            get_string("proxy_pool_add_btn"),
                                            icon=ft.Icons.ADD,
                                            height=32,
                                            style=OUTLINE_STYLE,
                                            on_click=_toggle_add,
                                        )
                                    ]
                                    if on_add_to_pool is not None
                                    else []
                                ),
                                ft.OutlinedButton(
                                    get_string("bulk_check_proxy"),
                                    icon=ft.Icons.NETWORK_CHECK,
                                    height=32,
                                    style=OUTLINE_STYLE,
                                    on_click=_handle_check_all,
                                ),
                            ],
                        ),
                    ],
                ),
                ft.Text(
                    get_string("proxy_pool_subtitle"),
                    size=12.5,
                    color=COLORS["text_sub"],
                ),
            ],
        ),
        content=ft.Container(
            width=1180,
            content=ft.Column(
                spacing=8,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    stats_text,
                    add_box,
                    notice_text,
                    list_col,
                ],
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    page.show_dialog(dlg)


def open_mail_pool_dialog(
    page: ft.Page,
    get_profiles: Callable[[], list[Profile]],
    *,
    get_unassigned: Callable[[], list[str]],
    on_add_to_pool: Callable[[list[str]], tuple[int, int]],
    on_remove_from_pool: Callable[[str], bool],
    on_assign_to_profile: Callable[[str, str], bool],
    get_all_pool: Callable[[], list[str]] | None = None,
) -> None:
    """Central Mail Pool Manager dialog: view spare Hotmail/OAuth2 accounts, test Inbox, assign or hot-swap."""
    notice_text = ft.Text("", size=12.5, color=COLORS["error"], visible=False)
    stats_text = ft.Text("", size=12.5, font_family=FONT_SEMIBOLD, color=COLORS["text_sub"])
    list_col = ft.Column(spacing=6, height=600, scroll=ft.ScrollMode.AUTO)
    assigning_target: list[str | None] = [None]
    replacing_prof: list[str | None] = [None]
    adding_open: list[bool] = [False]
    mail_status: dict[str, tuple[str, str]] = {}

    add_input = ft.TextField(
        hint_text=get_string("mail_smart_paste_hint"),
        multiline=True,
        min_lines=3,
        max_lines=6,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    add_box = ft.Container(
        visible=False,
        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        border_radius=8,
        bgcolor=COLORS["input_bg"],
        border=ft.Border.all(1, COLORS["rule_strong"]),
    )

    def _toggle_add(_: ft.ControlEvent) -> None:
        adding_open[0] = not adding_open[0]
        add_box.visible = adding_open[0]
        notice_text.visible = False
        page.update()

    def _submit_add(_: ft.ControlEvent) -> None:
        lines = [ln.strip() for ln in (add_input.value or "").splitlines() if ln.strip()]
        if not lines:
            return
        added, skipped = on_add_to_pool(lines)
        add_input.value = ""
        adding_open[0] = False
        add_box.visible = False
        notice_text.value = get_string("mail_pool_added_log", added=added, skipped=skipped)
        notice_text.color = COLORS["success"] if added > 0 else COLORS["warning"]
        notice_text.visible = True
        _rebuild()
        page.update()

    add_box.content = ft.Column(
        spacing=8,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Text(
                get_string("mail_pool_add_label"),
                size=12,
                font_family=FONT_SEMIBOLD,
                color=COLORS["text_sub"],
            ),
            add_input,
            ft.Row(
                alignment=ft.MainAxisAlignment.END,
                spacing=8,
                controls=[
                    ft.TextButton(
                        get_string("cancel"),
                        style=ft.ButtonStyle(color=COLORS["text_sub"]),
                        on_click=_toggle_add,
                    ),
                    ft.Button(
                        get_string("mail_pool_add_submit"),
                        icon=ft.Icons.ADD,
                        height=34,
                        style=ACCENT_STYLE,
                        on_click=_submit_add,
                    ),
                ],
            ),
        ],
    )

    def _pool_token_map() -> dict[str, str]:
        mapping: dict[str, str] = {}
        raw_pool = get_all_pool() if get_all_pool is not None else get_unassigned()
        for ln in raw_pool:
            p_item = parse_mail_credentials_line(ln)
            em_k = (p_item.get("email") or "").strip().lower()
            tk_v = (p_item.get("mail_token") or "").strip()
            if em_k and tk_v:
                mapping[em_k] = tk_v
        return mapping

    def _check_mail_credentials(em: str, tok: str) -> None:
        em = em.strip()
        tok = tok.strip()
        if not em:
            return
        em_key = em.lower()
        mail_status[em_key] = ("checking", "⏳ Đang kiểm tra...")
        _rebuild()
        with contextlib.suppress(Exception):
            page.update()

        def _worker() -> None:
            ok, otp_or_err, msgs = fetch_hotmail_messages(em, tok, top=5, timeout=10)
            if not ok:
                mail_status[em_key] = ("error", f"✗ {otp_or_err[:45]}")
            elif len(msgs) > 0:
                otp_tag = f" · OTP: {otp_or_err}" if otp_or_err else ""
                mail_status[em_key] = (
                    "ok",
                    f"✓ Thông tuyến (Inbox: {len(msgs)} thư{otp_tag})",
                )
            else:
                mail_status[em_key] = ("warn", "⏳ Inbox: 0 thư (Khởi tạo trễ)")
            _rebuild()
            with contextlib.suppress(Exception):
                page.update()

        threading.Thread(target=_worker, daemon=True).start()

    def _check_mail_line(raw_line: str) -> None:
        parsed = parse_mail_credentials_line(raw_line)
        em = (parsed.get("email") or "").strip()
        tok = (parsed.get("mail_token") or "").strip()
        _check_mail_credentials(em, tok)

    def _run_mail_batch(pairs: list[tuple[str, str]]) -> None:
        valid_pairs = [(e.strip(), t.strip()) for e, t in pairs if e.strip()]
        if not valid_pairs:
            return
        for em, _ in valid_pairs:
            mail_status[em.lower()] = ("checking", "⏳ Đang kiểm tra...")
        _rebuild()
        with contextlib.suppress(Exception):
            page.update()

        def _batch_worker() -> None:
            def _one(pair: tuple[str, str]) -> None:
                em, tok = pair
                em_key = em.lower()
                ok, otp_or_err, msgs = fetch_hotmail_messages(em, tok, top=5, timeout=10)
                if not ok:
                    mail_status[em_key] = ("error", f"✗ {otp_or_err[:45]}")
                elif len(msgs) > 0:
                    otp_tag = f" · OTP: {otp_or_err}" if otp_or_err else ""
                    mail_status[em_key] = (
                        "ok",
                        f"✓ Thông tuyến (Inbox: {len(msgs)} thư{otp_tag})",
                    )
                else:
                    mail_status[em_key] = ("warn", "⏳ Inbox: 0 thư (Khởi tạo trễ)")

            with ThreadPoolExecutor(max_workers=6) as ex:
                list(ex.map(_one, valid_pairs))
            _rebuild()
            with contextlib.suppress(Exception):
                page.update()

        threading.Thread(target=_batch_worker, daemon=True).start()

    def _check_all_mails(_: ft.ControlEvent) -> None:
        tok_map = _pool_token_map()
        seen: set[str] = set()
        pairs: list[tuple[str, str]] = []
        for sp in get_unassigned():
            parsed = parse_mail_credentials_line(sp)
            em = (parsed.get("email") or "").strip()
            tok = (parsed.get("mail_token") or "").strip()
            if em and em.lower() not in seen:
                seen.add(em.lower())
                pairs.append((em, tok))
        for p in get_profiles():
            if (p.group or "").strip().lower() == "disconnected" or p.banned_at:
                continue
            em = (p.email or "").strip()
            if not em or em.lower() in seen:
                continue
            tok = (getattr(p, "mail_token", None) or "").strip() or tok_map.get(em.lower(), "")
            if tok:
                seen.add(em.lower())
                pairs.append((em, tok))
        _run_mail_batch(pairs)

    def _rebuild() -> None:
        profiles = get_profiles()
        unassigned_list = get_unassigned()
        live_profiles = [
            p
            for p in profiles
            if (p.group or "").strip().lower() != "disconnected" and not p.banned_at
        ]
        assigned_profiles = [p for p in live_profiles if (p.email or "").strip()]
        empty_profiles = [p for p in live_profiles if not (p.email or "").strip()]

        stats_text.value = get_string(
            "mail_pool_stats_full",
            assigned=len(assigned_profiles),
            free=len(unassigned_list),
            empty=len(empty_profiles),
        )

        list_col.controls.clear()
        if not unassigned_list and not assigned_profiles:
            list_col.controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=40),
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("mail_pool_empty"),
                        size=13.5,
                        color=COLORS["text_dim"],
                    ),
                )
            )
            return

        # 1. Render unassigned spare mails first
        for idx, free_line in enumerate(unassigned_list, 1):
            parsed = parse_mail_credentials_line(free_line)
            em_str = (parsed.get("email") or free_line).strip()
            rec_str = (parsed.get("recovery_email") or "").strip()
            has_tok = bool((parsed.get("mail_token") or "").strip())
            is_assigning = assigning_target[0] == em_str.lower()
            st_pair = mail_status.get(em_str.lower())

            st_controls: list[ft.Control] = []
            if st_pair is not None:
                st_kind, st_msg = st_pair
                st_color = (
                    COLORS["success"]
                    if st_kind == "ok"
                    else (
                        COLORS["warning"]
                        if st_kind == "warn"
                        else (COLORS["accent"] if st_kind == "checking" else COLORS["error"])
                    )
                )
                st_controls.append(
                    ft.Text(st_msg, size=11.5, font_family=FONT_SEMIBOLD, color=st_color)
                )

            def _toggle_assign(target_em: str = em_str.lower()) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    notice_text.visible = False
                    assigning_target[0] = None if assigning_target[0] == target_em else target_em
                    _rebuild()
                    page.update()

                return _cb

            def _do_remove(target_em: str = em_str) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    if on_remove_from_pool(target_em):
                        _rebuild()
                        page.update()

                return _cb

            def _do_assign(
                prof_name: str,
                raw_ln: str = free_line,
                email_lbl: str = em_str,
            ) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    if on_assign_to_profile(prof_name, raw_ln):
                        assigning_target[0] = None
                        notice_text.value = get_string(
                            "mail_pool_assigned_log",
                            email=email_lbl,
                            name=prof_name,
                        )
                        notice_text.color = COLORS["success"]
                        notice_text.visible = True
                        _rebuild()
                        page.update()

                return _cb

            sub_parts = (
                [f"Khôi phục: {rec_str}"]
                if rec_str
                else [get_string("mail_pool_unassigned_sub")]
            )

            free_top_controls: list[ft.Control] = [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                    border_radius=5,
                    bgcolor=COLORS["accent"],
                    content=ft.Text(
                        f"#{idx}",
                        size=11,
                        font_family=FONT_DATA,
                        color="#FFFFFF",
                    ),
                ),
                ft.Text(
                    em_str,
                    size=12.5,
                    font_family=FONT_DATA,
                    color=COLORS["text_main"],
                    selectable=True,
                ),
            ]
            if has_tok:
                free_top_controls.append(
                    ft.Text(
                        "OAuth2 ✓",
                        size=11,
                        font_family=FONT_DATA,
                        color=COLORS["text_sub"],
                    )
                )

            free_bottom_controls: list[ft.Control] = [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                    border_radius=4,
                    bgcolor=ft.Colors.with_opacity(0.14, COLORS["success"]),
                    border=ft.Border.all(1, COLORS["success"]),
                    content=ft.Text(
                        get_string("mail_pool_unassigned_badge"),
                        size=10,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["success"],
                    ),
                ),
                ft.Text(
                    " · ".join(sub_parts),
                    size=11.5,
                    color=COLORS["text_sub"],
                ),
            ]
            if st_controls:
                free_bottom_controls.extend(
                    [
                        ft.Text("·", size=11.5, color=COLORS["text_dim"]),
                        *st_controls,
                    ]
                )

            free_children: list[ft.Control] = [
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                    controls=[
                        ft.Container(
                            expand=True,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                            content=ft.Column(
                                spacing=3,
                                tight=True,
                                controls=[
                                    ft.Row(
                                        spacing=8,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        controls=free_top_controls,
                                    ),
                                    ft.Row(
                                        spacing=6,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        controls=free_bottom_controls,
                                    ),
                                ],
                            ),
                        ),
                        ft.Row(
                            spacing=6,
                            tight=True,
                            controls=[
                                ft.OutlinedButton(
                                    get_string("pool_check_one_btn"),
                                    icon=ft.Icons.MARK_EMAIL_READ_OUTLINED,
                                    height=32,
                                    style=OUTLINE_STYLE,
                                    tooltip=get_string("mail_pool_check_one_tooltip"),
                                    on_click=lambda _, ln=free_line: _check_mail_line(ln),
                                ),
                                ft.OutlinedButton(
                                    get_string("mail_pool_assign_btn"),
                                    icon=ft.Icons.PERSON_ADD_ALT_1_OUTLINED,
                                    height=32,
                                    style=OUTLINE_STYLE,
                                    on_click=_toggle_assign(),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.DELETE_OUTLINE,
                                    icon_size=17,
                                    width=32,
                                    height=32,
                                    padding=0,
                                    icon_color=COLORS["text_sub"],
                                    tooltip=get_string("mail_pool_remove_tooltip"),
                                    on_click=_do_remove(),
                                ),
                            ],
                        ),
                    ],
                )
            ]

            if is_assigning and live_profiles:
                candidates = sorted(
                    live_profiles,
                    key=lambda p: (
                        bool((p.email or "").strip()),
                        not p.name.isdigit(),
                        -(int(p.name) if p.name.isdigit() else 0),
                        p.name.casefold(),
                    ),
                )
                free_children.append(
                    ft.Column(
                        spacing=5,
                        tight=True,
                        controls=[
                            ft.Text(
                                get_string("mail_pool_assign_hint"),
                                size=11.5,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_sub"],
                            ),
                            ft.Row(
                                wrap=True,
                                spacing=6,
                                run_spacing=6,
                                controls=[
                                    ft.Container(
                                        padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                                        border_radius=5,
                                        bgcolor=COLORS["card_bg"],
                                        border=ft.Border.all(
                                            1,
                                            COLORS["accent"]
                                            if not (cand.email or "").strip()
                                            else COLORS["rule_strong"],
                                        ),
                                        ink=True,
                                        tooltip=(cand.email or get_string("mail_pool_no_mail_short")),
                                        on_click=_do_assign(cand.name),
                                        content=ft.Text(
                                            cand.name
                                            + (
                                                f" ({get_string('mail_pool_no_mail_short')})"
                                                if not (cand.email or "").strip()
                                                else ""
                                            ),
                                            size=11.5,
                                            font_family=FONT_MEDIUM,
                                            color=COLORS["accent"]
                                            if not (cand.email or "").strip()
                                            else COLORS["text_main"],
                                        ),
                                    )
                                    for cand in candidates[:36]
                                ],
                            ),
                        ],
                    )
                )

            list_col.controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=7),
                    border_radius=8,
                    bgcolor=COLORS["card_bg"],
                    border=ft.Border.all(1, COLORS["accent"]),
                    content=ft.Column(spacing=8, tight=True, controls=free_children),
                )
            )

        # 2. Render assigned mails on active profiles (newest numeric profiles first)
        tok_map = _pool_token_map()
        sorted_assigned = sorted(
            assigned_profiles,
            key=lambda p: (
                not p.name.isdigit(),
                -(int(p.name) if p.name.isdigit() else 0),
                p.name.casefold(),
            ),
        )
        for prof in sorted_assigned:
            em_val = (prof.email or "").strip()
            tok_val = (getattr(prof, "mail_token", None) or "").strip() or tok_map.get(
                em_val.lower(), ""
            )
            is_rep = replacing_prof[0] == prof.name
            prof_st_pair = mail_status.get(em_val.lower())
            prof_st_controls: list[ft.Control] = []
            if tok_val:
                prof_st_controls.append(
                    ft.Text("OAuth2 ✓", size=11, font_family=FONT_DATA, color=COLORS["text_sub"])
                )
            if prof_st_pair is not None:
                p_kind, p_msg = prof_st_pair
                p_col = (
                    COLORS["success"]
                    if p_kind == "ok"
                    else (
                        COLORS["warning"]
                        if p_kind == "warn"
                        else (COLORS["accent"] if p_kind == "checking" else COLORS["error"])
                    )
                )
                prof_st_controls.append(
                    ft.Text(p_msg, size=11.5, font_family=FONT_SEMIBOLD, color=p_col)
                )

            def _toggle_rep(pname: str = prof.name) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    replacing_prof[0] = None if replacing_prof[0] == pname else pname
                    _rebuild()
                    page.update()

                return _cb

            def _do_swap(
                raw_ln: str,
                pname: str = prof.name,
            ) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    new_em = (parse_mail_credentials_line(raw_ln).get("email") or "").strip()
                    if on_assign_to_profile(pname, raw_ln):
                        replacing_prof[0] = None
                        notice_text.value = get_string(
                            "mail_pool_assigned_log",
                            email=new_em,
                            name=pname,
                        )
                        notice_text.color = COLORS["success"]
                        notice_text.visible = True
                        _rebuild()
                        page.update()

                return _cb

            row_children: list[ft.Control] = [
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=10,
                    controls=[
                        ft.Container(
                            expand=True,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                            content=ft.Row(
                                spacing=8,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=[
                                    ft.Container(
                                        padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                                        border_radius=5,
                                        bgcolor=COLORS["sidebar"],
                                        content=ft.Text(
                                            prof.name,
                                            size=11.5,
                                            font_family=FONT_SEMIBOLD,
                                            color=COLORS["cover_ink"],
                                        ),
                                    ),
                                    ft.Text(
                                        em_val,
                                        size=12.5,
                                        font_family=FONT_DATA,
                                        color=COLORS["text_main"],
                                        selectable=True,
                                    ),
                                    *prof_st_controls,
                                ],
                            ),
                        ),
                        ft.Row(
                            spacing=6,
                            tight=True,
                            controls=[
                                ft.OutlinedButton(
                                    get_string("pool_check_one_btn"),
                                    icon=ft.Icons.MARK_EMAIL_READ_OUTLINED,
                                    height=30,
                                    style=OUTLINE_STYLE,
                                    tooltip=get_string("mail_pool_check_one_tooltip"),
                                    on_click=lambda _, e=em_val, t=tok_val: _check_mail_credentials(
                                        e, t
                                    ),
                                ),
                                ft.OutlinedButton(
                                    get_string("proxy_pool_replace_btn"),
                                    icon=ft.Icons.SWAP_HORIZ,
                                    height=30,
                                    style=OUTLINE_STYLE,
                                    on_click=_toggle_rep(),
                                ),
                            ],
                        ),
                    ],
                )
            ]

            if is_rep and unassigned_list:
                row_children.append(
                    ft.Column(
                        spacing=4,
                        tight=True,
                        controls=[
                            ft.Text(
                                get_string("mail_pool_pick_spare", count=len(unassigned_list)),
                                size=11.5,
                                color=COLORS["text_sub"],
                            ),
                            ft.Row(
                                wrap=True,
                                spacing=6,
                                run_spacing=6,
                                controls=[
                                    ft.Container(
                                        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                                        border_radius=5,
                                        bgcolor=COLORS["card_bg"],
                                        border=ft.Border.all(1, COLORS["accent"]),
                                        ink=True,
                                        on_click=_do_swap(sp_ln),
                                        content=ft.Text(
                                            (
                                                parse_mail_credentials_line(sp_ln).get("email")
                                                or sp_ln
                                            ).strip(),
                                            size=11.5,
                                            font_family=FONT_DATA,
                                            color=COLORS["accent"],
                                        ),
                                    )
                                    for sp_ln in unassigned_list[:14]
                                ],
                            ),
                        ],
                    )
                )

            list_col.controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                    border_radius=8,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(1, COLORS["border"]),
                    content=ft.Column(spacing=6, tight=True, controls=row_children),
                )
            )

    _rebuild()

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=18, vertical=14),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Column(
            spacing=4,
            tight=True,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            get_string("mail_pool_title"),
                            size=19,
                            font_family=FONT_BOLD,
                            color=COLORS["text_main"],
                        ),
                        ft.Row(
                            spacing=8,
                            tight=True,
                            controls=[
                                ft.OutlinedButton(
                                    get_string("mail_pool_add_btn"),
                                    icon=ft.Icons.ADD,
                                    height=32,
                                    style=OUTLINE_STYLE,
                                    on_click=_toggle_add,
                                ),
                                ft.OutlinedButton(
                                    get_string("mail_pool_check_all_btn"),
                                    icon=ft.Icons.MARK_EMAIL_READ_OUTLINED,
                                    height=32,
                                    style=OUTLINE_STYLE,
                                    on_click=_check_all_mails,
                                ),
                            ],
                        ),
                    ],
                ),
                ft.Text(
                    get_string("mail_pool_subtitle"),
                    size=12.5,
                    color=COLORS["text_sub"],
                ),
            ],
        ),
        content=ft.Container(
            width=1180,
            content=ft.Column(
                spacing=8,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    stats_text,
                    add_box,
                    notice_text,
                    list_col,
                ],
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    page.show_dialog(dlg)


def _build_ip_score_chip(
    fraud_score: int | None,
    *,
    scamalytics: int | None = None,
    proxycheck_risk: int | None = None,
    proxy_flag: bool = False,
    isp: str = "",
    asn: str = "",
    on_click: Callable[[], None] | None = None,
) -> ft.Container:
    """Render an artistic IP Fraud / Risk Score badge with symbol and detailed tooltip."""
    if fraud_score is None:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=7, vertical=2),
            border_radius=5,
            bgcolor=COLORS["card_bg"],
            border=ft.Border.all(1, COLORS["rule_strong"]),
            ink=on_click is not None,
            tooltip=get_string("trust_score_all_ips_btn"),
            on_click=(lambda _: on_click()) if on_click is not None else None,
            content=ft.Row(
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.RADAR_ROUNDED, size=12, color=COLORS["accent"]),
                    ft.Text(
                        get_string("ip_score_unscored"),
                        size=10.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["accent"],
                    ),
                ],
            ),
        )

    if fraud_score <= 15:
        col = "#15803D"
        icon = ft.Icons.VERIFIED_OUTLINED
        label = get_string("ip_score_clean", score=fraud_score)
    elif fraud_score <= 35:
        col = "#1D4ED8"
        icon = ft.Icons.SHIELD_OUTLINED
        label = get_string("ip_score_good", score=fraud_score)
    elif fraud_score <= 64:
        col = COLORS["accent"]
        icon = ft.Icons.GPP_MAYBE_OUTLINED
        label = get_string("ip_score_medium", score=fraud_score)
    else:
        col = COLORS["error"]
        icon = ft.Icons.GPP_BAD_OUTLINED
        label = get_string("ip_score_high", score=fraud_score)

    isp_full = " · ".join(x for x in (isp, asn) if x) or "-"
    tip = get_string(
        "ip_score_tooltip",
        score=fraud_score,
        scam=scamalytics if scamalytics is not None else "-",
        pc=proxycheck_risk if proxycheck_risk is not None else "-",
        flag=get_string("ip_flag_yes") if proxy_flag else get_string("ip_flag_no"),
        isp=isp_full,
    )
    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=7, vertical=2),
        border_radius=5,
        bgcolor=ft.Colors.with_opacity(0.11, col),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.55, col)),
        ink=on_click is not None,
        tooltip=tip,
        on_click=(lambda _: on_click()) if on_click is not None else None,
        content=ft.Row(
            spacing=4,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(icon, size=12, color=col),
                ft.Text(
                    label,
                    size=10.5,
                    font_family=FONT_DATA,
                    weight=ft.FontWeight.W_700,
                    color=col,
                ),
            ],
        ),
    )


def _build_trust_score_chip(
    info: ProfileMonitorInfo,
    *,
    on_click: Callable[[], None] | None = None,
) -> ft.Container:
    """Render an Account Warmup & Trust Score badge with tier symbol and breakdown tooltip."""
    tier = info.trust_tier
    score = info.trust_score
    if tier == "banned":
        col = COLORS["error"]
        label = get_string("trust_tier_banned")
    elif tier == "diamond":
        col = "#1D4ED8"
        label = get_string("trust_tier_diamond", score=score)
    elif tier == "elite":
        col = "#15803D"
        label = get_string("trust_tier_elite", score=score)
    elif tier == "platinum":
        col = "#7C3AED"
        label = get_string("trust_tier_platinum", score=score)
    elif tier == "trusted":
        col = "#B45309"
        label = get_string("trust_tier_trusted", score=score)
    elif tier == "warming":
        col = COLORS["text_dim"]
        label = get_string("trust_tier_warming", score=score)
    else:
        col = "#F87171"
        label = get_string("trust_tier_cold", score=score)

    sess_status = (
        get_string("trust_badge_gw_ok")
        if (info.hatch_gw and info.hatch_vml)
        else (
            get_string("trust_yes")
            if info.hatch_gw
            else (
                get_string("trust_badge_no_gw")
                if info.hatch_sess
                else get_string("trust_no")
            )
        )
    )
    tip = get_string(
        "trust_breakdown_tooltip",
        score=score,
        datr_age=f"{info.datr_age_days:.1f}" if info.datr_age_days > 0 else str(info.age_days),
        datr_doms=info.datr_domains,
        sess=sess_status,
        cookies=info.cookie_count,
        ps_l=get_string("trust_yes") if info.has_ps_l else get_string("trust_no"),
        ext_doms=info.ext_domains,
        hpages=info.history_pages,
        chk_scar=(
            get_string("trust_chk_scar")
            if info.has_meta_checkpoint
            else get_string("trust_chk_clean")
        ),
        cache2=(
            get_string("trust_yes")
            if (info.has_cache2 and info.cookie_count > 0)
            else get_string("trust_no")
        ),
        act_days=info.active_days,
        sess_cnt=info.session_count,
        mins=info.total_minutes,
    )
    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=8, vertical=2),
        border_radius=5,
        bgcolor=ft.Colors.with_opacity(0.09, col),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.45, col)),
        ink=on_click is not None,
        on_click=(lambda _: on_click()) if on_click is not None else None,
        tooltip=tip,
        content=ft.Text(
            label,
            size=10.5,
            font_family=FONT_DATA,
            weight=ft.FontWeight.W_700,
            color=col,
        ),
    )


def _build_trust_breakdown_panel(
    info: ProfileMonitorInfo,
    profile: Profile | None = None,
    *,
    on_score_ip: Callable[[], None] | None = None,
) -> ft.Control:
    """Build a detailed item-by-item Trust Score calculation and Hard Gate checklist panel."""
    bd = compute_trust_breakdown(info, profile)
    pos_total = int(bd["positive_total"])
    pen_total = int(bd["penalty_total"])
    raw_score = int(bd["raw_score"])
    final_score = int(bd["final_score"])
    stage_cap = bd["stage_cap"]
    capped_by = int(bd["capped_by"])
    stage_label = str(bd["stage_label"])
    tier = str(bd["trust_tier"])

    tier_col = (
        COLORS["error"]
        if tier == "banned"
        else (
            "#1D4ED8"
            if tier == "diamond"
            else (
                "#15803D"
                if tier == "elite"
                else (
                    "#7C3AED"
                    if tier == "platinum"
                    else (
                        "#B45309"
                        if tier == "trusted"
                        else (COLORS["text_dim"] if tier == "warming" else "#F87171")
                    )
                )
            )
        )
    )

    def _stat_box(title: str, val_str: str, sub_str: str, col: str) -> ft.Container:
        return ft.Container(
            expand=True,
            padding=ft.Padding.symmetric(horizontal=10, vertical=7),
            border_radius=7,
            bgcolor=COLORS["card_bg"],
            border=ft.Border.all(1, ft.Colors.with_opacity(0.45, col)),
            content=ft.Column(
                spacing=1,
                tight=True,
                controls=[
                    ft.Text(title, size=10.5, font_family=FONT_SEMIBOLD, color=COLORS["text_dim"]),
                    ft.Text(
                        val_str,
                        size=14,
                        font_family=FONT_DATA,
                        weight=ft.FontWeight.W_700,
                        color=col,
                    ),
                    ft.Text(
                        sub_str,
                        size=10.5,
                        color=COLORS["text_sub"],
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
            ),
        )

    cap_val_str = f"Tối đa {stage_cap}đ" if stage_cap is not None else "150đ (Mở khóa)"
    cap_col = (
        COLORS["warning"]
        if capped_by > 0
        else ("#15803D" if stage_cap is None else COLORS["text_main"])
    )
    cap_sub_str = f"Giữ lại -{capped_by}đ" if capped_by > 0 else "Không bị chạm trần"

    if capped_by > 0:
        cap_banner_text = (
            f"🔒 {stage_label} — Điểm gốc {raw_score}đ đang bị giữ ở mức {final_score}đ "
            f"(trừ tạm -{capped_by}đ) cho đến khi hoàn thành các điều kiện mở trần bên dưới."
        )
        cap_banner_col = COLORS["warning"]
    elif stage_cap is not None:
        cap_banner_text = (
            f"🛡️ {stage_label} · Điểm gốc hiện tại ({raw_score}đ) chưa chạm mức trần ({stage_cap}đ)."
        )
        cap_banner_col = COLORS["text_sub"]
    else:
        cap_banner_text = f"✦ {stage_label} · Tài khoản đã vượt qua toàn bộ cổng kiểm duyệt."
        cap_banner_col = "#15803D"

    formula_card = ft.Container(
        padding=ft.Padding.symmetric(horizontal=12, vertical=9),
        border_radius=8,
        bgcolor=COLORS["input_bg"],
        border=ft.Border.all(1, COLORS["border"]),
        content=ft.Column(
            spacing=7,
            tight=True,
            controls=[
                ft.Row(
                    spacing=8,
                    controls=[
                        _stat_box(
                            "1. TỔNG 6 HẠNG MỤC",
                            f"+{pos_total} / 150đ",
                            "Điểm tích lũy thành phần",
                            "#1D4ED8" if pos_total >= 56 else COLORS["text_main"],
                        ),
                        _stat_box(
                            "2. ĐIỂM PHẠT RỦI RO",
                            f"{pen_total}đ",
                            "0 lỗi vi phạm" if pen_total == 0 else f"{len(bd['penalties'])} mục trừ điểm",
                            "#15803D" if pen_total == 0 else COLORS["error"],
                        ),
                        _stat_box(
                            "3. ĐIỂM GỐC (RAW)",
                            f"{raw_score} / 150đ",
                            f"= max(0, {pos_total} {pen_total:+d})",
                            COLORS["text_main"],
                        ),
                        _stat_box(
                            "4. TRẦN GIAI ĐOẠN",
                            cap_val_str,
                            cap_sub_str,
                            cap_col,
                        ),
                        _stat_box(
                            "5. ĐIỂM CHÍNH THỨC",
                            f"{final_score} / 150đ",
                            f"= min({raw_score}đ, Trần {stage_cap}đ)"
                            if stage_cap is not None
                            else "= Điểm gốc",
                            tier_col,
                        ),
                    ],
                ),
                ft.Text(
                    cap_banner_text,
                    size=11.5,
                    font_family=FONT_MEDIUM,
                    color=cap_banner_col,
                ),
            ],
        ),
    )

    # Build 6 Pillar cards in a 2-column grid (3 rows x 2 cols)
    def _pillar_card(pil: dict[str, Any]) -> ft.Container:
        earned = int(pil["earned"])
        max_pts = int(pil["max_pts"])
        ratio = earned / max(1, max_pts)
        p_col = (
            "#15803D"
            if earned >= max_pts
            else (
                "#1D4ED8"
                if ratio >= 0.6
                else (COLORS["accent"] if earned > 0 else COLORS["text_dim"])
            )
        )
        body_rows: list[ft.Control] = []
        sub_items = pil.get("sub_items") or []
        if sub_items:
            for sub in sub_items:
                s_ok = bool(sub["ok"])
                s_earned = int(sub["earned"])
                s_max = int(sub["max_pts"])
                s_col = "#15803D" if s_ok else (COLORS["accent"] if s_earned > 0 else COLORS["text_dim"])
                s_sym = "✓" if s_ok else ("◐" if s_earned > 0 else "○")
                body_rows.append(
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(
                                f"{s_sym} {sub['label']}",
                                size=11,
                                color=COLORS["text_main"] if s_earned > 0 else COLORS["text_sub"],
                                expand=True,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                tooltip=f"{sub['label']}: {sub['detail']}",
                            ),
                            ft.Text(
                                str(sub["detail"]),
                                size=10.5,
                                font_family=FONT_DATA,
                                color=COLORS["text_sub"],
                            ),
                            ft.Container(
                                padding=ft.Padding.symmetric(horizontal=5, vertical=1),
                                border_radius=4,
                                bgcolor=ft.Colors.with_opacity(0.10, s_col),
                                content=ft.Text(
                                    f"+{s_earned}/{s_max}đ",
                                    size=10.5,
                                    font_family=FONT_DATA,
                                    weight=ft.FontWeight.W_700,
                                    color=s_col,
                                ),
                            ),
                        ],
                    )
                )
            if (
                pil.get("key") == "network"
                and info.ip_fraud_score is None
                and on_score_ip is not None
                and (info.last_exit_ip or info.has_proxy or info.port)
            ):
                body_rows.append(
                    ft.Row(
                        alignment=ft.MainAxisAlignment.END,
                        controls=[
                            ft.Container(
                                padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                                border_radius=4,
                                bgcolor=ft.Colors.with_opacity(0.12, COLORS["accent"]),
                                border=ft.Border.all(1, COLORS["accent"]),
                                ink=True,
                                on_click=lambda _: on_score_ip(),
                                content=ft.Text(
                                    "✦ Chấm điểm độ sạch IP ngay để nhận tối đa +11đ",
                                    size=10.5,
                                    font_family=FONT_SEMIBOLD,
                                    color=COLORS["accent"],
                                ),
                            )
                        ],
                    )
                )
        else:
            body_rows.append(
                ft.Text(
                    f"Hiện tại: {pil['current_str']}",
                    size=11.5,
                    font_family=FONT_DATA,
                    weight=ft.FontWeight.W_600,
                    color=COLORS["text_main"],
                )
            )
            body_rows.append(
                ft.Text(
                    str(pil["next_hint"]),
                    size=11,
                    color="#15803D" if earned >= max_pts else COLORS["accent"],
                )
            )
            if pil.get("tiers_str"):
                body_rows.append(
                    ft.Text(
                        str(pil["tiers_str"]),
                        size=10,
                        font_family=FONT_DATA,
                        color=COLORS["text_dim"],
                    )
                )

        return ft.Container(
            expand=True,
            padding=ft.Padding.symmetric(horizontal=11, vertical=8),
            border_radius=7,
            bgcolor=COLORS["card_bg"],
            border=ft.Border.all(1, COLORS["border"]),
            content=ft.Column(
                spacing=4,
                tight=True,
                controls=[
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(
                                str(pil["title"]),
                                size=12,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_main"],
                            ),
                            ft.Container(
                                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                                border_radius=4,
                                bgcolor=ft.Colors.with_opacity(0.12, p_col),
                                border=ft.Border.all(1, ft.Colors.with_opacity(0.5, p_col)),
                                content=ft.Text(
                                    f"+{earned} / {max_pts}đ",
                                    size=11,
                                    font_family=FONT_DATA,
                                    weight=ft.FontWeight.W_700,
                                    color=p_col,
                                ),
                            ),
                        ],
                    ),
                    ft.ProgressBar(
                        value=min(1.0, max(0.0, ratio)),
                        height=4,
                        border_radius=2,
                        color=p_col,
                        bgcolor=COLORS["border"],
                    ),
                    *body_rows,
                ],
            ),
        )

    pillars = bd["pillars"]
    pillar_grid_rows: list[ft.Control] = []
    for i in range(0, len(pillars), 2):
        pair = pillars[i : i + 2]
        pillar_grid_rows.append(
            ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[_pillar_card(p_item) for p_item in pair],
            )
        )

    # Risk Penalties section
    penalties = bd["penalties"]
    if penalties:
        pen_rows: list[ft.Control] = [
            ft.Text(
                f"⚠ CÁC KHOẢN BỊ TRỪ ĐIỂM RỦI RO ({pen_total}đ):",
                size=11.5,
                font_family=FONT_SEMIBOLD,
                color=COLORS["error"],
            )
        ]
        for pen in penalties:
            pen_rows.append(
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.Text(f"• {pen['label']}", size=11.5, color=COLORS["text_main"]),
                        ft.Text(
                            f"{pen['points']}đ",
                            size=11.5,
                            font_family=FONT_DATA,
                            weight=ft.FontWeight.W_700,
                            color=COLORS["error"],
                        ),
                    ],
                )
            )
        penalties_ctrl: ft.Control = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            border_radius=7,
            bgcolor=ft.Colors.with_opacity(0.07, COLORS["error"]),
            border=ft.Border.all(1, COLORS["error"]),
            content=ft.Column(spacing=3, tight=True, controls=pen_rows),
        )
    else:
        penalties_ctrl = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            border_radius=7,
            bgcolor=ft.Colors.with_opacity(0.07, "#15803D"),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.4, "#15803D")),
            content=ft.Text(
                "✓ Không có điểm phạt rủi ro nào (0đ) — Môi trường Cookie, Proxy & Nhịp mở phiên an toàn.",
                size=11.5,
                font_family=FONT_MEDIUM,
                color="#15803D",
            ),
        )

    # Hard Gates Checklist section
    gates = bd["gates"]
    passed_cnt = sum(1 for g in gates if g["passed"])
    gate_items_ui: list[ft.Control] = []
    for g in gates:
        g_ok = bool(g["passed"])
        g_col = "#15803D" if g_ok else COLORS["warning"]
        g_sym = "✓" if g_ok else "⚠"
        gate_items_ui.append(
            ft.Container(
                expand=True,
                padding=ft.Padding.symmetric(horizontal=9, vertical=5),
                border_radius=6,
                bgcolor=COLORS["card_bg"],
                border=ft.Border.all(
                    1,
                    ft.Colors.with_opacity(0.45, g_col),
                ),
                content=ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            f"{g_sym} {g['label']}",
                            size=11,
                            font_family=FONT_MEDIUM if g_ok else FONT_SEMIBOLD,
                            color=COLORS["text_main"],
                            expand=True,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        ft.Text(
                            str(g["current"]),
                            size=10.5,
                            font_family=FONT_DATA,
                            weight=ft.FontWeight.W_600,
                            color=g_col,
                        ),
                    ],
                ),
            )
        )

    gate_rows: list[ft.Control] = []
    for i in range(0, len(gate_items_ui), 2):
        pair_g = gate_items_ui[i : i + 2]
        if len(pair_g) == 1:
            pair_g.append(ft.Container(expand=True))
        gate_rows.append(ft.Row(spacing=6, controls=pair_g))

    gates_ctrl = ft.Container(
        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        border_radius=8,
        bgcolor=COLORS["input_bg"],
        border=ft.Border.all(1, COLORS["border"]),
        content=ft.Column(
            spacing=6,
            tight=True,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            f"🎯 {bd['next_tier_title']}",
                            size=12,
                            font_family=FONT_SEMIBOLD,
                            color=COLORS["text_main"],
                        ),
                        ft.Text(
                            f"Đã đạt {passed_cnt}/{len(gates)} điều kiện",
                            size=11,
                            font_family=FONT_DATA,
                            weight=ft.FontWeight.W_700,
                            color="#15803D" if passed_cnt == len(gates) else COLORS["warning"],
                        ),
                    ],
                ),
                *gate_rows,
            ],
        ),
    )

    return ft.Column(
        spacing=8,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            formula_card,
            *pillar_grid_rows,
            penalties_ctrl,
            gates_ctrl,
        ],
    )


def _format_duration_mins(mins: int) -> str:
    m = max(0, int(mins))
    if m < 60:
        return get_string("running_minutes", mins=m)
    h, rem = divmod(m, 60)
    if rem == 0:
        return f"{h}h"
    return get_string("running_hours", hours=h, mins=rem)


def open_session_history_dialog(
    page: ft.Page,
    profile_name: str,
    sessions: list[dict[str, Any]],
    today_mins: int = 0,
    last_port: str = "",
    *,
    ip_history: list[dict[str, Any]] | None = None,
    initial_tab: str = "sessions",
    on_check_ip_now: Callable[[], None] | None = None,
    on_clear_ip_history: Callable[[], None] | None = None,
    monitor_info: ProfileMonitorInfo | None = None,
    profile: Profile | None = None,
    on_score_ip: Callable[[str, dict[str, Any]], None] | None = None,
    on_open_trust_monitor: Callable[[], None] | None = None,
) -> None:
    """Modal dialog showing browser session history, exit IP log, IP fraud score, and Trust Breakdown."""
    ip_items: list[dict[str, Any]] = list(ip_history or [])
    init_key = (
        "trust"
        if (initial_tab == "trust" and monitor_info is not None)
        else ("ips" if initial_tab == "ips" else "sessions")
    )
    active_tab: list[str] = [init_key]
    tab_bar = ft.Row(spacing=8)
    summary_badges_row = ft.Row(spacing=6, wrap=True)
    list_col = ft.Column(spacing=6, height=520, scroll=ft.ScrollMode.AUTO)
    clear_ip_btn = ft.TextButton(
        get_string("ip_hist_clear_btn"),
        icon=ft.Icons.DELETE_SWEEP_OUTLINED,
        visible=active_tab[0] == "ips" and bool(ip_items) and on_clear_ip_history is not None,
        style=ft.ButtonStyle(color=COLORS["error"]),
    )

    def _score_single_ip(target_ip: str) -> None:
        clean = (target_ip or "").strip()
        if not clean or clean == "-":
            return

        async def _task() -> None:
            risk = await asyncio.to_thread(lookup_ip_risk, clean)
            if risk.get("fraud_score") is not None:
                if on_score_ip is not None:
                    on_score_ip(clean, risk)
                for item in ip_items:
                    if item.get("ip") == clean:
                        item["fraud_score"] = risk["fraud_score"]
                        item["scamalytics"] = risk.get("scamalytics")
                        item["proxycheck_risk"] = risk.get("proxycheck_risk")
                        item["proxy_flag"] = bool(risk.get("proxy_flag"))
                        if risk.get("isp") and not item.get("isp"):
                            item["isp"] = risk["isp"]
                        if risk.get("ip_type") and not item.get("ip_type"):
                            item["ip_type"] = risk["ip_type"]
                        if risk.get("asn"):
                            item["asn"] = risk["asn"]
                if monitor_info is not None and monitor_info.last_exit_ip == clean:
                    monitor_info.ip_fraud_score = int(risk["fraud_score"])
                    monitor_info.ip_scamalytics = risk.get("scamalytics")
                    monitor_info.ip_proxycheck_risk = risk.get("proxycheck_risk")
                    monitor_info.ip_proxy_flag = bool(risk.get("proxy_flag"))
                    if risk.get("asn"):
                        monitor_info.ip_asn = str(risk["asn"])
                _rebuild_view()
                with contextlib.suppress(Exception):
                    page.update()

        if hasattr(page, "run_task"):
            page.run_task(_task)

    def _build_session_rows() -> list[ft.Control]:
        if not sessions:
            return [
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=40),
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("session_hist_empty"),
                        size=13.5,
                        color=COLORS["text_dim"],
                    ),
                )
            ]
        rows: list[ft.Control] = []
        for idx, s in enumerate(sessions, start=1):
            is_live = bool(s.get("is_live"))
            start_str = str(s.get("start", ""))
            end_str = str(s.get("end", ""))
            dur_mins = int(s.get("duration_mins") or 0)
            port_str = str(s.get("port") or "-")
            row_label = (
                f"● {get_string('session_hist_live', start=start_str, mins=dur_mins)}"
                if is_live
                else f"{idx}. {get_string('session_hist_row', start=start_str, end=end_str, mins=dur_mins)}"
            )
            rows.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                    border_radius=7,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(
                        1.5 if is_live else 1,
                        COLORS["success"] if is_live else COLORS["border"],
                    ),
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(
                                row_label,
                                size=12.5,
                                font_family=FONT_DATA,
                                color=COLORS["success"] if is_live else COLORS["text_main"],
                                expand=True,
                            ),
                            ft.Container(
                                padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                                border_radius=5,
                                bgcolor=COLORS["card_bg"],
                                border=ft.Border.all(1, COLORS["rule_strong"]),
                                content=ft.Text(
                                    port_str,
                                    size=11.5,
                                    font_family=FONT_DATA,
                                    color=COLORS["text_sub"],
                                ),
                            ),
                        ],
                    ),
                )
            )
        return rows

    def _build_ip_rows() -> list[ft.Control]:
        if not ip_items:
            return [
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=40, horizontal=16),
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("ip_hist_empty"),
                        size=13,
                        text_align=ft.TextAlign.CENTER,
                        color=COLORS["text_dim"],
                    ),
                )
            ]
        rows: list[ft.Control] = []
        for idx, item in enumerate(ip_items):
            ip_val = str(item.get("ip") or "-")
            country_val = str(item.get("country") or "").strip().upper()
            tz_val = str(item.get("tz") or "").strip()
            port_val = str(item.get("port") or "-")
            ts_val = str(item.get("ts") or "")
            isp_val = str(item.get("isp") or "").strip()
            ip_type_val = str(item.get("ip_type") or "").strip().upper()
            raw_fraud = item.get("fraud_score")
            fraud_val = int(raw_fraud) if raw_fraud is not None else None
            scam_val = item.get("scamalytics")
            pc_val = item.get("proxycheck_risk")
            flag_val = bool(item.get("proxy_flag"))
            asn_val = str(item.get("asn") or "").strip()
            older = ip_items[idx + 1] if idx + 1 < len(ip_items) else None
            older_country = str(older.get("country") or "").strip().upper() if older else ""
            older_ip = str(older.get("ip") or "").strip() if older else ""

            badge_text = ""
            badge_col = COLORS["success"]
            if country_val and older_country and country_val != older_country:
                badge_text = get_string(
                    "ip_hist_badge_country_changed",
                    old=older_country,
                    new=country_val,
                )
                badge_col = COLORS["error"]
            elif idx == 0:
                badge_text = get_string("ip_hist_badge_current")
                badge_col = COLORS["success"]
            elif older_ip and ip_val != older_ip:
                badge_text = get_string("ip_hist_badge_ip_changed")
                badge_col = COLORS["accent"]

            meta_parts = [p for p in (country_val, isp_val, tz_val, ts_val) if p]
            meta_line = " · ".join(meta_parts)

            top_controls: list[ft.Control] = [
                ft.Text(
                    f"{idx + 1}. {ip_val}",
                    size=13,
                    font_family=FONT_DATA,
                    weight=ft.FontWeight.W_600,
                    color=COLORS["text_main"],
                ),
            ]
            if country_val:
                top_controls.append(
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                        border_radius=4,
                        bgcolor=COLORS["card_bg"],
                        border=ft.Border.all(1, COLORS["rule_strong"]),
                        content=ft.Text(
                            country_val,
                            size=10.5,
                            font_family=FONT_DATA,
                            weight=ft.FontWeight.W_700,
                            color=COLORS["text_main"],
                        ),
                    )
                )
            if ip_type_val:
                top_controls.append(
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                        border_radius=4,
                        bgcolor=COLORS["card_bg"],
                        border=ft.Border.all(1, COLORS["rule_strong"]),
                        tooltip=isp_val or ip_type_val,
                        content=ft.Text(
                            ip_type_val,
                            size=10,
                            font_family=FONT_DATA,
                            weight=ft.FontWeight.W_600,
                            color=COLORS["text_sub"],
                        ),
                    )
                )
            top_controls.append(
                _build_ip_score_chip(
                    fraud_val,
                    scamalytics=int(scam_val) if scam_val is not None else None,
                    proxycheck_risk=int(pc_val) if pc_val is not None else None,
                    proxy_flag=flag_val,
                    isp=isp_val,
                    asn=asn_val,
                    on_click=lambda target=ip_val: _score_single_ip(target),
                )
            )
            if badge_text:
                top_controls.append(
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                        border_radius=4,
                        bgcolor=ft.Colors.with_opacity(0.12, badge_col),
                        border=ft.Border.all(1, badge_col),
                        content=ft.Text(
                            badge_text,
                            size=10,
                            font_family=FONT_SEMIBOLD,
                            color=badge_col,
                        ),
                    )
                )

            rows.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                    border_radius=7,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(
                        1.5 if idx == 0 else 1,
                        badge_col if idx == 0 else COLORS["border"],
                    ),
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Column(
                                spacing=2,
                                tight=True,
                                expand=True,
                                controls=[
                                    ft.Row(
                                        spacing=6,
                                        wrap=True,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        controls=top_controls,
                                    ),
                                    ft.Text(
                                        meta_line,
                                        size=11.5,
                                        font_family=FONT_DATA,
                                        color=COLORS["text_sub"],
                                    ),
                                ],
                            ),
                            ft.Container(
                                padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                                border_radius=5,
                                bgcolor=COLORS["card_bg"],
                                border=ft.Border.all(1, COLORS["rule_strong"]),
                                content=ft.Text(
                                    port_val,
                                    size=11.5,
                                    font_family=FONT_DATA,
                                    color=COLORS["text_sub"],
                                ),
                            ),
                        ],
                    ),
                )
            )
        return rows

    def _switch_tab(tab_key: str) -> None:
        active_tab[0] = tab_key
        _rebuild_view()
        with contextlib.suppress(Exception):
            page.update()

    def _seg_btn(tab_key: str, label: str, icon: ft.IconData) -> ft.Container:
        on = active_tab[0] == tab_key
        return ft.Container(
            height=32,
            padding=ft.Padding.symmetric(horizontal=12),
            border_radius=7,
            bgcolor=COLORS["sidebar"] if on else COLORS["input_bg"],
            border=ft.Border.all(1, COLORS["sidebar"] if on else COLORS["rule_strong"]),
            ink=True,
            on_click=lambda _, k=tab_key: _switch_tab(k),
            content=ft.Row(
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(
                        icon,
                        size=14,
                        color=COLORS["cover_ink"] if on else COLORS["text_sub"],
                    ),
                    ft.Text(
                        label,
                        size=12.5,
                        font_family=FONT_SEMIBOLD if on else FONT_MEDIUM,
                        color=COLORS["cover_ink"] if on else COLORS["text_main"],
                    ),
                ],
            ),
        )

    def _rebuild_view() -> None:
        summary_badges: list[ft.Control] = []
        if monitor_info is not None:
            summary_badges.append(
                _build_trust_score_chip(
                    monitor_info,
                    on_click=lambda: _switch_tab("trust"),
                )
            )
            if monitor_info.last_exit_ip:
                summary_badges.append(
                    _build_ip_score_chip(
                        monitor_info.ip_fraud_score,
                        scamalytics=monitor_info.ip_scamalytics,
                        proxycheck_risk=monitor_info.ip_proxycheck_risk,
                        proxy_flag=monitor_info.ip_proxy_flag,
                        isp=monitor_info.last_exit_isp,
                        asn=monitor_info.ip_asn,
                        on_click=lambda: _score_single_ip(monitor_info.last_exit_ip),
                    )
                )
        summary_badges_row.controls = summary_badges
        summary_badges_row.visible = bool(summary_badges)

        tabs_list: list[ft.Control] = [
            _seg_btn(
                "sessions",
                get_string("session_hist_tab", count=len(sessions)),
                ft.Icons.HISTORY,
            ),
            _seg_btn(
                "ips",
                get_string("ip_hist_tab", count=len(ip_items)),
                ft.Icons.PUBLIC,
            ),
        ]
        if monitor_info is not None:
            tabs_list.append(
                _seg_btn(
                    "trust",
                    get_string("trust_hist_tab", score=monitor_info.trust_score),
                    ft.Icons.VERIFIED_USER_OUTLINED,
                )
            )
        tab_bar.controls = tabs_list
        clear_ip_btn.visible = (
            active_tab[0] == "ips" and bool(ip_items) and on_clear_ip_history is not None
        )
        if active_tab[0] == "trust" and monitor_info is not None:
            list_col.controls = [
                _build_trust_breakdown_panel(
                    monitor_info,
                    profile,
                    on_score_ip=(
                        (lambda: _score_single_ip(monitor_info.last_exit_ip))
                        if monitor_info.last_exit_ip
                        else on_check_ip_now
                    ),
                )
            ]
        elif active_tab[0] == "ips":
            list_col.controls = _build_ip_rows()
        else:
            list_col.controls = _build_session_rows()

    def _handle_clear_ip(_: ft.ControlEvent) -> None:
        if on_clear_ip_history is not None:
            on_clear_ip_history()
            ip_items.clear()
            _rebuild_view()
            page.update()

    def _handle_check_ip(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        if on_check_ip_now is not None:
            on_check_ip_now()

    def _handle_open_full_monitor(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        if on_open_trust_monitor is not None:
            on_open_trust_monitor()

    clear_ip_btn.on_click = _handle_clear_ip
    _rebuild_view()

    top_btns: list[ft.Control] = []
    if on_open_trust_monitor is not None:
        top_btns.append(
            ft.OutlinedButton(
                get_string("trust_open_full_btn"),
                icon=ft.Icons.VERIFIED_USER_ROUNDED,
                height=32,
                style=OUTLINE_STYLE,
                on_click=_handle_open_full_monitor,
            )
        )
    if on_check_ip_now is not None:
        top_btns.append(
            ft.OutlinedButton(
                get_string("ip_hist_check_now"),
                icon=ft.Icons.NETWORK_CHECK,
                height=32,
                style=OUTLINE_STYLE,
                on_click=_handle_check_ip,
            )
        )

    header_row = ft.Row(
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Text(
                get_string("session_hist_title", name=profile_name),
                size=19,
                font_family=FONT_BOLD,
                color=COLORS["text_main"],
                expand=True,
            ),
            *(
                [ft.Row(spacing=6, tight=True, controls=top_btns)]
                if top_btns
                else []
            ),
        ],
    )

    sum_sess_mins = sum(max(0, int(s.get("duration_mins") or 0)) for s in sessions)
    total_mins = (
        max(sum_sess_mins, monitor_info.total_minutes)
        if monitor_info is not None
        else sum_sess_mins
    )
    total_count = (
        max(len(sessions), monitor_info.session_count)
        if monitor_info is not None
        else len(sessions)
    )

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Column(
            spacing=6,
            tight=True,
            controls=[
                header_row,
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            get_string(
                                "session_hist_subtitle",
                                count=total_count,
                                total=_format_duration_mins(total_mins),
                                today=_format_duration_mins(today_mins),
                                port=last_port or "-",
                            ),
                            size=12.5,
                            color=COLORS["text_sub"],
                        ),
                        summary_badges_row,
                    ],
                ),
            ],
        ),
        content=ft.Container(
            width=840,
            content=ft.Column(
                spacing=10,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    tab_bar,
                    list_col,
                ],
            ),
        ),
        actions=[
            clear_ip_btn,
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    page.show_dialog(dlg)


def open_trust_monitor_dialog(
    page: ft.Page,
    get_profiles: Callable[[], list[Profile]],
    get_snapshot: Callable[[], MonitorSnapshot],
    on_record_ip_risk: Callable[[str, dict[str, Any]], None],
    *,
    check_proxy_fn: Callable[[str, int], tuple[bool, str]] | None = None,
    on_record_exit_ip: Callable[..., Any] | None = None,
    on_log: Callable[[str], None] | None = None,
    on_refresh_main: Callable[[], None] | None = None,
    initial_tab: str = "scores",
) -> None:
    """Modal dashboard for IP Fraud Score, Account Warmup Trust Score, and Cross-Profile Audit."""
    active_tab: list[str] = ["audit" if initial_tab == "audit" else "scores"]
    active_filter: list[str] = ["all"]
    scoring_busy: list[bool] = [False]
    expanded_profile: list[str | None] = [None]

    tab_bar = ft.Row(
        spacing=6,
        wrap=True,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    table_header_slot = ft.Container()
    status_banner = ft.Text("", size=12, color=COLORS["accent"], visible=False)
    list_col = ft.Column(spacing=4, height=580, scroll=ft.ScrollMode.AUTO)
    score_all_btn = ft.Button(
        get_string("trust_score_all_ips_btn"),
        icon=ft.Icons.RADAR_ROUNDED,
        height=34,
        style=ACCENT_STYLE,
    )

    async def _resolve_proxy_exit_ip(proxy_str: str) -> str:
        if not proxy_str or check_proxy_fn is None:
            return ""
        ok, detail = await asyncio.to_thread(check_proxy_fn, proxy_str, 10)
        if not ok:
            return ""
        raw_ip = extract_ip_from_check_detail(detail) or ""
        if not raw_ip:
            return ""
        country, auto_tz, _ = await asyncio.to_thread(derive_ip_metadata, raw_ip, detail)
        isp_name, ip_type = await asyncio.to_thread(lookup_ip_isp, raw_ip)
        if on_record_exit_ip is not None:
            for prof in get_profiles():
                if prof.proxy == proxy_str:
                    on_record_exit_ip(
                        prof.name,
                        raw_ip,
                        country=country,
                        tz=auto_tz or "",
                        proxy=proxy_str,
                        isp=isp_name,
                        ip_type=ip_type,
                    )
        return raw_ip

    def _score_ip_async(target_ip: str, proxy_str: str = "") -> None:
        clean = (target_ip or "").strip()
        px = (proxy_str or "").strip()
        if not clean and not px:
            return

        async def _one() -> None:
            status_banner.value = get_string("trust_scoring_ips")
            status_banner.visible = True
            with contextlib.suppress(Exception):
                page.update()
            try:
                ip_to_score = clean
                if not ip_to_score and px:
                    ip_to_score = await _resolve_proxy_exit_ip(px)
                if ip_to_score:
                    risk = await asyncio.to_thread(lookup_ip_risk, ip_to_score)
                    if risk.get("fraud_score") is not None:
                        on_record_ip_risk(ip_to_score, risk)
            finally:
                status_banner.visible = False
                if on_refresh_main is not None:
                    on_refresh_main()
                _rebuild()
                with contextlib.suppress(Exception):
                    page.update()

        if hasattr(page, "run_task"):
            page.run_task(_one)

    def _score_all_ips(_: ft.ControlEvent) -> None:
        if scoring_busy[0]:
            return
        profiles_now = get_profiles()
        snap = get_snapshot()
        missing_proxies = sorted(
            {
                p.proxy
                for p in profiles_now
                if p.proxy
                and not (snap.profiles.get(p.name) and snap.profiles[p.name].is_banned)
                and not (snap.profiles.get(p.name) and snap.profiles[p.name].last_exit_ip)
            }
        )
        known_ips = sorted(
            {
                info.last_exit_ip
                for info in snap.profiles.values()
                if info.last_exit_ip and not info.is_banned
            }
        )
        if not missing_proxies and not known_ips:
            return
        scoring_busy[0] = True
        score_all_btn.disabled = True
        status_banner.value = get_string("trust_scoring_ips")
        status_banner.visible = True
        with contextlib.suppress(Exception):
            page.update()

        async def _batch() -> None:
            sem = asyncio.Semaphore(5)
            scored = 0
            resolved_ips: set[str] = set(known_ips)

            async def _resolve_worker(px_str: str) -> None:
                async with sem:
                    ip_out = await _resolve_proxy_exit_ip(px_str)
                if ip_out:
                    resolved_ips.add(ip_out)

            async def _score_worker(ip_addr: str) -> None:
                nonlocal scored
                async with sem:
                    risk = await asyncio.to_thread(lookup_ip_risk, ip_addr)
                if risk.get("fraud_score") is not None:
                    scored += 1
                    on_record_ip_risk(ip_addr, risk)

            try:
                if missing_proxies and check_proxy_fn is not None:
                    await asyncio.gather(*(_resolve_worker(px) for px in missing_proxies))
                if resolved_ips:
                    await asyncio.gather(*(_score_worker(ip) for ip in sorted(resolved_ips)))
            finally:
                scoring_busy[0] = False
                score_all_btn.disabled = False
                status_banner.visible = False
                if scored > 0 and on_log is not None:
                    on_log(get_string("trust_scored_ips_log", count=scored))
                if on_refresh_main is not None:
                    on_refresh_main()
                _rebuild()
                with contextlib.suppress(Exception):
                    page.update()

        if hasattr(page, "run_task"):
            page.run_task(_batch)

    score_all_btn.on_click = _score_all_ips

    def _switch_tab(tab_key: str) -> None:
        active_tab[0] = tab_key
        _rebuild()
        with contextlib.suppress(Exception):
            page.update()

    def _switch_filter(fid: str) -> None:
        active_filter[0] = fid
        _rebuild()
        with contextlib.suppress(Exception):
            page.update()

    def _toggle_expand(pname: str) -> None:
        expanded_profile[0] = None if expanded_profile[0] == pname else pname
        _rebuild()
        with contextlib.suppress(Exception):
            page.update()

    def _seg_btn(tab_key: str, label: str, icon: ft.IconData, alert: bool = False) -> ft.Container:
        on = active_tab[0] == tab_key
        col = (
            COLORS["cover_ink"]
            if on
            else (COLORS["error"] if alert else COLORS["text_main"])
        )
        return ft.Container(
            height=34,
            padding=ft.Padding.symmetric(horizontal=13),
            border_radius=8,
            bgcolor=COLORS["sidebar"] if on else COLORS["input_bg"],
            border=ft.Border.all(
                1,
                COLORS["sidebar"]
                if on
                else (COLORS["error"] if alert else COLORS["rule_strong"]),
            ),
            ink=True,
            on_click=lambda _, k=tab_key: _switch_tab(k),
            content=ft.Row(
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(icon, size=15, color=col),
                    ft.Text(
                        label,
                        size=12.5,
                        font_family=FONT_SEMIBOLD if on else FONT_MEDIUM,
                        color=col,
                    ),
                ],
            ),
        )

    def _fchip(fid: str, label: str) -> ft.Container:
        on = active_filter[0] == fid
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border_radius=6,
            bgcolor=COLORS["sidebar"] if on else COLORS["input_bg"],
            border=ft.Border.all(1, COLORS["sidebar"] if on else COLORS["rule_strong"]),
            ink=True,
            on_click=lambda _, k=fid: _switch_filter(k),
            content=ft.Text(
                label,
                size=11.5,
                font_family=FONT_SEMIBOLD if on else FONT_MEDIUM,
                color=COLORS["cover_ink"] if on else COLORS["text_main"],
            ),
        )

    def _rebuild() -> None:
        profiles = get_profiles()
        snap = get_snapshot()
        infos = [
            (p, snap.profiles.get(p.name) or ProfileMonitorInfo(name=p.name))
            for p in profiles
        ]
        c_diamond = sum(1 for _, i in infos if i.trust_tier == "diamond")
        c_elite = sum(1 for _, i in infos if i.trust_tier == "elite")
        c_platinum = sum(1 for _, i in infos if i.trust_tier == "platinum")
        c_trusted = sum(1 for _, i in infos if i.trust_tier == "trusted")
        c_warming = sum(1 for _, i in infos if i.trust_tier in ("warming", "cold"))

        left_toolbar: list[ft.Control] = [
            _seg_btn(
                "scores",
                f"Điểm Trust & IP ({len(profiles)})",
                ft.Icons.VERIFIED_USER_OUTLINED,
            ),
            _seg_btn(
                "audit",
                f"Kiểm toán ({len(snap.audit_findings)})",
                ft.Icons.SHIELD_OUTLINED,
                alert=bool(snap.audit_findings),
            ),
        ]
        if active_tab[0] == "scores":
            left_toolbar.extend(
                [
                    ft.Container(
                        width=1,
                        height=20,
                        bgcolor=COLORS["rule_strong"],
                        margin=ft.Margin.symmetric(horizontal=2),
                    ),
                    _fchip("all", get_string("trust_filter_all", count=len(infos))),
                    _fchip("diamond", get_string("trust_filter_diamond", count=c_diamond)),
                    _fchip("elite", get_string("trust_filter_elite", count=c_elite)),
                    _fchip("platinum", get_string("trust_filter_platinum", count=c_platinum)),
                    _fchip("trusted", get_string("trust_filter_trusted", count=c_trusted)),
                    _fchip("warming", get_string("trust_filter_warming", count=c_warming)),
                ]
            )

        tab_bar.controls = left_toolbar

        if active_tab[0] == "audit":
            table_header_slot.visible = False
            if not snap.audit_findings:
                list_col.controls = [
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=24, vertical=44),
                        border_radius=10,
                        bgcolor=ft.Colors.with_opacity(0.07, "#059669"),
                        border=ft.Border.all(1, "#059669"),
                        alignment=ft.Alignment(0, 0),
                        content=ft.Column(
                            spacing=6,
                            tight=True,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Icon(
                                    ft.Icons.VERIFIED_ROUNDED,
                                    size=36,
                                    color="#059669",
                                ),
                                ft.Text(
                                    get_string("audit_all_clear_title"),
                                    size=15,
                                    font_family=FONT_BOLD,
                                    color="#059669",
                                    text_align=ft.TextAlign.CENTER,
                                ),
                                ft.Text(
                                    get_string("audit_all_clear_desc"),
                                    size=12.5,
                                    color=COLORS["text_sub"],
                                    text_align=ft.TextAlign.CENTER,
                                ),
                            ],
                        ),
                    )
                ]
            else:
                audit_rows: list[ft.Control] = []
                for f in snap.audit_findings:
                    is_crit = f.level == "critical"
                    col = COLORS["error"] if is_crit else COLORS["warning"]
                    icon = (
                        ft.Icons.GPP_BAD_ROUNDED
                        if is_crit
                        else ft.Icons.WARNING_AMBER_ROUNDED
                    )
                    audit_rows.append(
                        ft.Container(
                            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                            border_radius=8,
                            bgcolor=COLORS["input_bg"],
                            border=ft.Border.all(1, col),
                            content=ft.Row(
                                spacing=10,
                                vertical_alignment=ft.CrossAxisAlignment.START,
                                controls=[
                                    ft.Icon(icon, size=19, color=col),
                                    ft.Column(
                                        spacing=2,
                                        tight=True,
                                        expand=True,
                                        controls=[
                                            ft.Text(
                                                f.title,
                                                size=13,
                                                font_family=FONT_SEMIBOLD,
                                                color=COLORS["text_main"],
                                            ),
                                            ft.Text(
                                                f.detail,
                                                size=12,
                                                color=COLORS["text_sub"],
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                        )
                    )
                list_col.controls = audit_rows
            return

        table_header_slot.visible = True
        table_header_slot.content = ft.Column(
            spacing=5,
            tight=True,
            controls=[
                ft.Text(
                    get_string("trust_expand_hint"),
                    size=11.5,
                    color=COLORS["text_sub"],
                ),
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                    border_radius=6,
                    bgcolor=COLORS["bg"],
                    border=ft.Border.all(1, COLORS["border"]),
                    content=ft.Row(
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(
                                "PROFILE",
                                width=212,
                                size=11,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_dim"],
                            ),
                            ft.Text(
                                "ĐỘ CHÍN TRUST",
                                width=142,
                                size=11,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_dim"],
                            ),
                            ft.Text(
                                "GỢI Ý TĂNG ĐIỂM",
                                expand=True,
                                size=11,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_dim"],
                            ),
                            ft.Text(
                                "ĐỊA CHỈ IP & ĐỘ SẠCH",
                                width=215,
                                size=11,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_dim"],
                                text_align=ft.TextAlign.RIGHT,
                            ),
                        ],
                    ),
                ),
            ],
        )

        flt = active_filter[0]
        if flt == "diamond":
            infos = [(p, i) for p, i in infos if i.trust_tier == "diamond"]
        elif flt == "elite":
            infos = [(p, i) for p, i in infos if i.trust_tier == "elite"]
        elif flt == "platinum":
            infos = [(p, i) for p, i in infos if i.trust_tier == "platinum"]
        elif flt == "trusted":
            infos = [(p, i) for p, i in infos if i.trust_tier == "trusted"]
        elif flt == "warming":
            infos = [(p, i) for p, i in infos if i.trust_tier in ("warming", "cold")]

        infos.sort(
            key=lambda pair: (
                pair[1].is_banned,
                -pair[1].trust_score,
                natural_name_key(pair[0].name),
            )
        )

        score_rows: list[ft.Control] = []
        for p, info in infos:
            # Build concise, actionable next-steps suggestion for Col 3
            action_color = COLORS["text_sub"]
            if info.is_banned:
                summary_str = "Đang tạm ngưng / Ban"
                action_color = COLORS["error"]
            elif info.trust_tier == "diamond":
                summary_str = "✦ Hạng Kim Cương (≥45d) — Đạt đỉnh tín nhiệm an toàn"
                action_color = "#1D4ED8"
            elif info.trust_tier == "elite":
                m_steps: list[str] = []
                if info.active_days < 28:
                    m_steps.append(f"Mở thêm {28 - info.active_days} ngày")
                elif info.session_count < 50:
                    m_steps.append(f"Thêm {50 - info.session_count} phiên")
                if info.total_minutes < 2700:
                    m_steps.append(f"Treo thêm {_format_duration_mins(2700 - info.total_minutes)}")
                if info.datr_age_days < 45.0 and len(m_steps) < 3:
                    m_steps.append(f"Đợi cookie +{max(1, int(round(45.0 - info.datr_age_days)))}d")
                if not info.has_recovery_armor and len(m_steps) < 3:
                    m_steps.append("Bổ sung 2FA hoặc Pass/Mail phụ")
                m_tail = " · ".join(m_steps[:2]) if m_steps else "Sẵn sàng lên ✦ Kim Cương"
                summary_str = f"🛡️ Tin cậy (Đủ tuổi cài 2FA) → {m_tail}"
                action_color = "#15803D"
            elif info.trust_tier == "platinum":
                d_steps: list[str] = []
                if info.active_days < 20:
                    d_steps.append(f"Mở thêm {20 - info.active_days} ngày")
                elif info.session_count < 35:
                    d_steps.append(f"Thêm {35 - info.session_count} phiên")
                if info.total_minutes < 1800:
                    d_steps.append(f"Treo thêm {_format_duration_mins(1800 - info.total_minutes)}")
                if info.datr_age_days < 30.0 and len(d_steps) < 3:
                    d_steps.append(f"Đợi cookie +{max(1, int(round(30.0 - info.datr_age_days)))}d")
                if not info.has_recovery_key and len(d_steps) < 3:
                    d_steps.append("Lưu mail_token/2FA")
                d_tail = " · ".join(d_steps[:2]) if d_steps else "Sẵn sàng lên 🛡️ Tin cậy"
                summary_str = f"❖ Bền vững (Cày việc tốt) → {d_tail}"
                action_color = "#7C3AED"
            elif info.cookie_count == 0 and info.session_count == 0 and info.history_pages == 0:
                summary_str = "Mở trình duyệt · Đăng nhập Muse & Meta"
                action_color = COLORS["text_dim"]
            else:
                steps: list[str] = []
                if info.has_meta_checkpoint:
                    steps.append("⚠ Vết Checkpoint")
                    action_color = COLORS["error"]
                if info.ip_country_changed:
                    steps.append("⚠ Khác quốc gia IP cũ")
                    action_color = COLORS["warning"]
                if info.proxy_port_changed:
                    steps.append("⚠ Đổi port proxy")
                    action_color = COLORS["warning"]

                if info.trust_tier == "trusted":
                    if info.has_meta and info.datr_domains < 4:
                        steps.append("Mở Facebook/Instagram đồng bộ (≥4 miền)")
                    if info.active_days < 14:
                        steps.append(f"Mở thêm {14 - info.active_days} ngày")
                    elif info.session_count < 25:
                        steps.append(f"Thêm {25 - info.session_count} phiên")
                    if info.total_minutes < 1200:
                        steps.append(
                            f"Treo thêm {_format_duration_mins(1200 - info.total_minutes)}"
                        )
                    elif (
                        info.total_minutes / max(1, info.session_count)
                    ) < 20.0 and info.total_minutes < 1800:
                        steps.append("Treo phiên dài ≥25p")
                    if info.datr_age_days < 21.0 and len(steps) < 3:
                        steps.append(
                            f"Đợi cookie +{max(1, int(round(21.0 - info.datr_age_days)))}d"
                        )
                    if (
                        info.ip_fraud_score is not None
                        and info.ip_fraud_score >= 65
                        and len(steps) < 3
                    ):
                        steps.append("Đổi IP sạch (<65pt)")
                    if not steps and not info.has_thread_url:
                        steps.append("Mở 1 hội thoại Muse")
                    if not steps:
                        steps.append("Kiểm tra độ sạch IP (≤25pt) để lên Kim Cương")
                else:
                    if not p.proxy:
                        steps.append("Gắn Proxy")
                    elif not (p.timezone and p.locale):
                        steps.append("Điền Timezone/Locale")
                    if not info.hatch_gw:
                        steps.append("Đăng nhập Muse")
                    if info.has_meta and not info.has_ps_l:
                        steps.append("Mở Facebook/Meta lấy ps_l")
                    elif info.has_meta and info.datr_domains < 3:
                        steps.append("Mở Facebook/Instagram đồng bộ (≥3 miền)")
                    if (
                        info.session_count >= 4
                        and (info.total_minutes / max(1, info.session_count)) < 15.0
                        and info.total_minutes < 900
                    ):
                        steps.append("Giữ phiên ≥20p (tránh tắt nhanh)")
                    if info.active_days < 7:
                        steps.append(f"Mở thêm {7 - info.active_days} ngày")
                    elif info.session_count < 12:
                        steps.append(f"Thêm {12 - info.session_count} phiên")
                    if info.total_minutes < 480:
                        steps.append(
                            f"Treo thêm {_format_duration_mins(480 - info.total_minutes)}"
                        )
                    if info.datr_age_days < 12.0 and len(steps) < 3:
                        steps.append(
                            f"Đợi cookie +{max(1, int(round(12.0 - info.datr_age_days)))}d"
                        )
                    if not steps and not info.has_thread_url:
                        steps.append("Mở 1 hội thoại Muse")
                summary_str = " · ".join(steps[:3]) if steps else "Tiếp tục duy trì phiên"

            detail_tip = (
                f"Cần làm: {summary_str}\n"
                f"Hiện tại: Cookie {info.datr_age_days:.1f}d ({info.datr_domains} miền Meta/Muse) · "
                f"{info.active_days}d mở ({info.session_count} phiên, {_format_duration_mins(info.total_minutes)})"
            )

            right_controls: list[ft.Control] = []
            if info.last_exit_ip:
                ip_short = info.last_exit_ip
                if ":" in ip_short and len(ip_short) > 16:
                    segs = ip_short.split(":")
                    if len(segs) >= 4:
                        ip_short = f"{segs[0]}:..:{segs[-1]}"
                ip_lbl = (
                    f"{ip_short} ({info.last_exit_country})"
                    if info.last_exit_country
                    else ip_short
                )
                right_controls.append(
                    ft.Text(
                        ip_lbl,
                        size=11,
                        font_family=FONT_DATA,
                        color=COLORS["text_sub"],
                        tooltip=f"{info.last_exit_ip} · {info.last_exit_isp or '-'}",
                    )
                )
                right_controls.append(
                    _build_ip_score_chip(
                        info.ip_fraud_score,
                        scamalytics=info.ip_scamalytics,
                        proxycheck_risk=info.ip_proxycheck_risk,
                        proxy_flag=info.ip_proxy_flag,
                        isp=info.last_exit_isp,
                        asn=info.ip_asn,
                        on_click=lambda target=info.last_exit_ip, px=p.proxy or "": _score_ip_async(
                            target, px
                        ),
                    )
                )
            elif p.proxy:
                right_controls.append(
                    ft.Text(
                        info.port or mask_proxy(p.proxy),
                        size=11,
                        font_family=FONT_DATA,
                        color=COLORS["text_dim"],
                    )
                )
                right_controls.append(
                    _build_ip_score_chip(
                        None,
                        on_click=lambda px=p.proxy or "": _score_ip_async("", px),
                    )
                )
            else:
                right_controls.append(
                    ft.Text(
                        get_string("no_proxy_short"),
                        size=11,
                        color=COLORS["text_dim"],
                    )
                )

            sub_email = (
                p.email
                if (getattr(p, "email", None) and p.email not in p.name)
                else (info.port or "")
            )
            is_exp = expanded_profile[0] == p.name
            row_children: list[ft.Control] = [
                ft.Row(
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Container(
                            expand=True,
                            ink=True,
                            border_radius=5,
                            tooltip=get_string("tooltip_trust_breakdown"),
                            on_click=lambda _, n=p.name: _toggle_expand(n),
                            content=ft.Row(
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=[
                                    ft.Container(
                                        width=212,
                                        content=ft.Row(
                                            spacing=4,
                                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                            controls=[
                                                ft.Icon(
                                                    ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED
                                                    if is_exp
                                                    else ft.Icons.KEYBOARD_ARROW_RIGHT_ROUNDED,
                                                    size=16,
                                                    color=COLORS["accent"]
                                                    if is_exp
                                                    else COLORS["text_dim"],
                                                ),
                                                ft.Text(
                                                    p.name,
                                                    size=12.5,
                                                    font_family=FONT_SEMIBOLD,
                                                    color=COLORS["text_main"],
                                                    no_wrap=True,
                                                    overflow=ft.TextOverflow.ELLIPSIS,
                                                ),
                                                *(
                                                    [
                                                        ft.Text(
                                                            sub_email,
                                                            size=10.5,
                                                            font_family=FONT_DATA,
                                                            color=COLORS["text_dim"],
                                                            no_wrap=True,
                                                            overflow=ft.TextOverflow.ELLIPSIS,
                                                            expand=True,
                                                        )
                                                    ]
                                                    if sub_email
                                                    else []
                                                ),
                                            ],
                                        ),
                                    ),
                                    ft.Container(
                                        width=142,
                                        alignment=ft.Alignment(-1, 0),
                                        content=_build_trust_score_chip(info),
                                    ),
                                    ft.Text(
                                        summary_str,
                                        expand=True,
                                        size=11.5,
                                        color=action_color,
                                        no_wrap=True,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                        tooltip=detail_tip,
                                    ),
                                ],
                            ),
                        ),
                        ft.Container(
                            width=215,
                            alignment=ft.Alignment(1, 0),
                            content=ft.Row(
                                spacing=5,
                                tight=True,
                                alignment=ft.MainAxisAlignment.END,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=right_controls,
                            ),
                        ),
                    ],
                )
            ]
            if is_exp:
                row_children.append(
                    ft.Container(
                        padding=ft.Padding.only(top=6, bottom=4),
                        content=_build_trust_breakdown_panel(
                            info,
                            p,
                            on_score_ip=lambda target=info.last_exit_ip, px=p.proxy or "": _score_ip_async(
                                target, px
                            ),
                        ),
                    )
                )
            score_rows.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=7),
                    border_radius=7,
                    bgcolor=COLORS["card_bg"] if is_exp else COLORS["input_bg"],
                    border=ft.Border.all(
                        1.5 if is_exp else 1,
                        COLORS["accent"] if is_exp else COLORS["border"],
                    ),
                    content=ft.Column(spacing=4, tight=True, controls=row_children),
                )
            )
        list_col.controls = score_rows

    _rebuild()

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=18, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=6, bottom=16),
        title=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Column(
                    spacing=2,
                    tight=True,
                    controls=[
                        ft.Text(
                            get_string("trust_monitor_title"),
                            size=17.5,
                            font_family=FONT_BOLD,
                            color=COLORS["text_main"],
                        ),
                        status_banner,
                    ],
                ),
                score_all_btn,
            ],
        ),
        content=ft.Container(
            width=860,
            content=ft.Column(
                spacing=8,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    tab_bar,
                    table_header_slot,
                    list_col,
                ],
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    page.show_dialog(dlg)

