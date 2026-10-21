import pathlib
from collections.abc import Callable
from typing import Any

import flet as ft

from ...core.strings import get_string
from ...utils.proxy_parser import mask_proxy
from ...utils.validation import validate_profile_name, validate_proxy_format
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_SEMIBOLD
from ..theme.page import build_os_dropdown
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, OUTLINE_STYLE


def open_batch_create_dialog(
    page: ft.Page,
    existing_names: set[str],
    on_create: Callable[[list[tuple[str, str | None]], str, str, str, str, bool], None],
) -> None:
    """Modal dialog to batch-create profiles by pasting names and proxies."""
    lower_existing = {n.casefold() for n in existing_names}

    names_box = ft.TextField(
        label=get_string("batch_names_label"),
        hint_text=get_string("batch_names_hint"),
        multiline=True,
        min_lines=5,
        max_lines=6,
        text_size=12.5,
        expand=1,
        **DLG_FIELD_KWARGS,
    )
    proxies_box = ft.TextField(
        label=get_string("batch_proxies_label"),
        hint_text=get_string("batch_proxies_hint"),
        multiline=True,
        min_lines=5,
        max_lines=6,
        text_size=12.5,
        expand=1,
        **DLG_FIELD_KWARGS,
    )
    repeat_cb = ft.Checkbox(
        label=get_string("batch_repeat_proxy_cb"),
        value=False,
    )
    auto_geo_cb = ft.Checkbox(
        label=get_string("batch_auto_geo_cb"),
        value=True,
    )
    os_dropdown = build_os_dropdown("windows")
    os_dropdown.width = 135

    group_field = ft.TextField(
        label=get_string("group_optional"),
        hint_text=get_string("group_hint"),
        text_size=12.5,
        expand=1,
        **DLG_FIELD_KWARGS,
    )
    tz_field = ft.TextField(
        label=get_string("timezone_optional"),
        hint_text="America/Detroit",
        text_size=12.5,
        expand=1,
        **DLG_FIELD_KWARGS,
    )
    loc_field = ft.TextField(
        label=get_string("locale_optional"),
        hint_text="en-US",
        text_size=12.5,
        expand=1,
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
        height=145,
        scroll=ft.ScrollMode.AUTO,
    )

    def _compute() -> tuple[list[tuple[str, str | None]], int, list[ft.Control]]:
        name_lines = [
            ln.strip()
            for ln in (names_box.value or "").splitlines()
            if ln.strip()
        ]
        proxy_lines = [
            ln.strip()
            for ln in (proxies_box.value or "").splitlines()
            if ln.strip()
        ]
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
                    padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                    border_radius=5,
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
                                width=220,
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
        if preview_col.page is not None:
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

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Text(
            get_string("batch_create_title"),
            size=19,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=620,
            content=ft.Column(
                spacing=8,
                tight=True,
                controls=[
                    ft.Text(
                        get_string("batch_create_subtitle"),
                        size=12.5,
                        color=COLORS["text_sub"],
                    ),
                    ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        controls=[names_box, proxies_box],
                    ),
                    ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[os_dropdown, group_field, tz_field, loc_field],
                    ),
                    repeat_cb,
                    auto_geo_cb,
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
