import contextlib
import pathlib
from collections.abc import Callable
from typing import Any

import flet as ft

from ...core.strings import get_string
from ...models.profile import Profile
from ...services.monitor import extract_proxy_port
from ...utils.proxy_parser import mask_proxy
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
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
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
            width=560,
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
        height=300,
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
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
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
            width=540,
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
) -> None:
    """Central Proxy Manager dialog: view all unique proxies, spare unassigned proxies, test, assign, or replace."""
    notice_text = ft.Text("", size=12.5, color=COLORS["error"], visible=False)
    stats_text = ft.Text("", size=12.5, font_family=FONT_SEMIBOLD, color=COLORS["text_sub"])
    list_col = ft.Column(spacing=6, height=540, scroll=ft.ScrollMode.AUTO)
    editing_target: list[str | None] = [None]
    assigning_target: list[str | None] = [None]
    adding_open: list[bool] = [False]

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

        by_proxy: dict[str, list[str]] = {}
        direct_count = 0
        for p in profiles:
            px = (p.proxy or "").strip()
            if not px:
                direct_count += 1
            else:
                by_proxy.setdefault(px, []).append(p.name)

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
                    controls=[
                        ft.Column(
                            spacing=3,
                            expand=True,
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
                                    ],
                                ),
                                ft.Text(
                                    get_string("proxy_pool_unassigned_sub"),
                                    size=11.5,
                                    color=COLORS["text_sub"],
                                ),
                            ],
                        ),
                        ft.Row(
                            spacing=6,
                            tight=True,
                            controls=[
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
                    key=lambda p: (bool(p.proxy), p.name.casefold()),
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
            key=lambda kv: extract_proxy_port(kv[0]) or kv[0],
        )
        for px_str, member_names in sorted_items:
            port_badge = extract_proxy_port(px_str) or "PROXY"
            masked = mask_proxy(px_str)
            live_members = [m for m in member_names if m in running]
            first_member = member_names[0]
            st_pair = status_map.get(first_member)

            status_controls: list[ft.Control] = []
            if live_members:
                status_controls.append(
                    ft.Text(
                        f"● {get_string('stamp_live')} ({', '.join(live_members)})",
                        size=11.5,
                        font_family=FONT_DATA,
                        color=COLORS["success"],
                    )
                )
            if st_pair is not None:
                st_kind, st_msg = st_pair
                st_col = (
                    COLORS["success"]
                    if st_kind == "ok"
                    else (COLORS["accent"] if st_kind == "checking" else COLORS["error"])
                )
                status_controls.append(
                    ft.Text(
                        st_msg,
                        size=11.5,
                        font_family=FONT_DATA,
                        color=st_col,
                    )
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

            def _check_this(first_prof: str = first_member) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    on_check_one(first_prof)
                    _rebuild()
                    page.update()

                return _cb

            card_children: list[ft.Control] = [
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Column(
                            spacing=3,
                            expand=True,
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
                                        *status_controls,
                                    ],
                                ),
                                ft.Text(
                                    f"{len(member_names)} profile: " + ", ".join(member_names),
                                    size=11.5,
                                    color=COLORS["text_sub"],
                                    no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    tooltip=", ".join(member_names),
                                ),
                            ],
                        ),
                        ft.Row(
                            spacing=6,
                            tight=True,
                            controls=[
                                ft.IconButton(
                                    icon=ft.Icons.WIFI_FIND,
                                    icon_size=17,
                                    width=32,
                                    height=32,
                                    padding=0,
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
        page.pop_dialog()
        on_check_all()

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
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
            width=740,
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
) -> None:
    """Modal dialog showing browser session history and exit IP log for a profile."""
    ip_items: list[dict[str, Any]] = list(ip_history or [])
    active_tab: list[str] = ["ips" if initial_tab == "ips" else "sessions"]
    tab_bar = ft.Row(spacing=8)
    list_col = ft.Column(spacing=6, height=280, scroll=ft.ScrollMode.AUTO)
    clear_ip_btn = ft.TextButton(
        get_string("ip_hist_clear_btn"),
        icon=ft.Icons.DELETE_SWEEP_OUTLINED,
        visible=active_tab[0] == "ips" and bool(ip_items) and on_clear_ip_history is not None,
        style=ft.ButtonStyle(color=COLORS["error"]),
    )

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

            meta_parts = [p for p in (country_val, tz_val, ts_val) if p]
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
        tab_bar.controls = [
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
        clear_ip_btn.visible = (
            active_tab[0] == "ips" and bool(ip_items) and on_clear_ip_history is not None
        )
        list_col.controls = _build_ip_rows() if active_tab[0] == "ips" else _build_session_rows()

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

    clear_ip_btn.on_click = _handle_clear_ip
    _rebuild_view()

    header_row = ft.Row(
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Text(
                get_string("session_hist_title", name=profile_name),
                size=19,
                font_family=FONT_BOLD,
                color=COLORS["text_main"],
            ),
            *(
                [
                    ft.OutlinedButton(
                        get_string("ip_hist_check_now"),
                        icon=ft.Icons.NETWORK_CHECK,
                        height=32,
                        style=OUTLINE_STYLE,
                        on_click=_handle_check_ip,
                    )
                ]
                if on_check_ip_now is not None
                else []
            ),
        ],
    )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Column(
            spacing=4,
            tight=True,
            controls=[
                header_row,
                ft.Text(
                    get_string(
                        "session_hist_subtitle",
                        count=len(sessions),
                        today=today_mins,
                        port=last_port or "-",
                    ),
                    size=12.5,
                    color=COLORS["text_sub"],
                ),
            ],
        ),
        content=ft.Container(
            width=540,
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
