from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...models.profile import PROFILE_COLORS
from ...services.monitor import extract_proxy_port
from ...utils.proxy_parser import mask_proxy
from ...utils.validation import validate_proxy_format
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, OUTLINE_STYLE


def open_bulk_group_dialog(
    page: ft.Page,
    count: int,
    existing_groups: list[str],
    on_apply: Callable[[str | None], None],
) -> None:
    """Dialog to assign or clear a group for all selected profiles."""
    group_field = ft.TextField(
        label=get_string("group_optional"),
        hint_text=get_string("group_hint"),
        **DLG_FIELD_KWARGS,
    )

    def _apply(val: str | None) -> None:
        page.pop_dialog()
        on_apply(val)

    chips: list[ft.Control] = [
        ft.OutlinedButton(
            get_string("bulk_clear_group"),
            icon=ft.Icons.CLEAR,
            height=32,
            style=OUTLINE_STYLE,
            on_click=lambda _: _apply(None),
        )
    ]
    for grp in existing_groups:
        chips.append(
            ft.OutlinedButton(
                grp,
                icon=ft.Icons.FOLDER_OUTLINED,
                height=32,
                style=OUTLINE_STYLE,
                on_click=lambda _, g=grp: _apply(g),
            )
        )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Text(
            get_string("bulk_group_title", count=count),
            size=19,
            weight=ft.FontWeight.BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=440,
            content=ft.Column(
                tight=True,
                spacing=12,
                controls=[
                    group_field,
                    ft.Row(wrap=True, spacing=8, run_spacing=8, controls=chips),
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
                get_string("save_changes"),
                icon=ft.Icons.CHECK,
                style=ACCENT_STYLE,
                on_click=lambda _: _apply((group_field.value or "").strip() or None),
            ),
        ],
    )
    page.show_dialog(dlg)


def open_bulk_color_dialog(
    page: ft.Page,
    count: int,
    on_apply: Callable[[str | None], None],
) -> None:
    """Dialog to assign or clear a color tag for all selected profiles."""

    def _pick(color_name: str | None) -> None:
        page.pop_dialog()
        on_apply(color_name)

    swatches: list[ft.Control] = [
        ft.GestureDetector(
            on_tap=lambda _: _pick(None),
            content=ft.Container(
                width=36,
                height=36,
                border_radius=18,
                bgcolor=COLORS["input_bg"],
                border=ft.Border.all(1, COLORS["border"]),
                alignment=ft.Alignment(0, 0),
                tooltip=get_string("color_default"),
                content=ft.Icon(ft.Icons.CLOSE, size=16, color=COLORS["text_sub"]),
            ),
        )
    ]
    for c_name in PROFILE_COLORS:
        swatches.append(
            ft.GestureDetector(
                on_tap=lambda _, cn=c_name: _pick(cn),
                content=ft.Container(
                    width=32,
                    height=32,
                    border_radius=16,
                    bgcolor=c_name,
                    tooltip=c_name,
                ),
            )
        )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Text(
            get_string("bulk_color_title", count=count),
            size=19,
            weight=ft.FontWeight.BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=400,
            padding=ft.Padding.symmetric(vertical=8),
            content=ft.Row(
                wrap=True,
                spacing=12,
                run_spacing=12,
                controls=swatches,
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("cancel"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    page.show_dialog(dlg)


def open_bulk_proxy_dialog(
    page: ft.Page,
    profile_names: list[str],
    current_proxies: dict[str, str | None],
    on_apply: Callable[..., None],
    session_ports: dict[str, str] | None = None,
) -> None:
    """Dialog to paste a list of proxies and map them 1-to-1 (or cyclic) to selected profiles."""
    sess_map = session_ports or {}
    proxy_box = ft.TextField(
        label=get_string("bulk_proxy_input_label", count=len(profile_names)),
        hint_text=get_string("bulk_proxy_input_hint"),
        multiline=True,
        min_lines=5,
        max_lines=7,
        text_size=12.5,
        **DLG_FIELD_KWARGS,
    )
    repeat_cb = ft.Checkbox(
        label=get_string("bulk_proxy_repeat_cb"),
        value=False,
    )
    auto_geo_cb = ft.Checkbox(
        label=get_string("auto_pin_geo_cb"),
        value=True,
    )
    preview_col = ft.Column(
        spacing=4,
        height=175,
        scroll=ft.ScrollMode.AUTO,
    )

    def _compute_mapping() -> tuple[dict[str, str | None], list[ft.Control]]:
        lines = [
            ln.strip()
            for ln in (proxy_box.value or "").splitlines()
            if ln.strip()
        ]
        repeat = bool(repeat_cb.value)
        valid_assignments: dict[str, str | None] = {}
        rows: list[ft.Control] = []

        for idx, pname in enumerate(profile_names):
            raw_line: str | None = None
            if lines:
                if idx < len(lines):
                    raw_line = lines[idx]
                elif repeat:
                    raw_line = lines[idx % len(lines)]

            if raw_line is None:
                cur_masked = mask_proxy(current_proxies.get(pname)) or get_string("direct_short")
                status_text = get_string("bulk_proxy_keep_current", current=cur_masked)
                status_col = COLORS["text_dim"]
            else:
                ok, _err = validate_proxy_format(raw_line)
                if not ok:
                    status_text = f"✗ {get_string('bulk_proxy_invalid_line')}"
                    status_col = COLORS["error"]
                else:
                    valid_assignments[pname] = raw_line
                    masked = mask_proxy(raw_line)
                    new_port = extract_proxy_port(raw_line)
                    old_port = sess_map.get(pname, "")
                    if old_port and new_port and new_port != old_port:
                        status_text = (
                            f"⚠ {masked} ({get_string('proxy_changed_badge', old_port=old_port)})"
                        )
                        status_col = COLORS["error"]
                    else:
                        status_text = f"✓ {masked}"
                        status_col = COLORS["success"]

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
                                width=165,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                status_text,
                                size=11.5,
                                font_family=FONT_DATA,
                                color=status_col,
                                expand=True,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                    ),
                )
            )
        return valid_assignments, rows

    def _refresh_preview(_: ft.ControlEvent | None = None) -> None:
        proxy_box.error = None
        _, rows = _compute_mapping()
        preview_col.controls = rows
        if preview_col.page is not None:
            preview_col.update()

    _, initial_rows = _compute_mapping()
    preview_col.controls = initial_rows
    proxy_box.on_change = _refresh_preview
    repeat_cb.on_change = _refresh_preview

    def _clear_all(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        try:
            on_apply({n: None for n in profile_names}, False)
        except TypeError:
            on_apply({n: None for n in profile_names})

    def _save(_: ft.ControlEvent) -> None:
        valid_assignments, _ = _compute_mapping()
        if not valid_assignments:
            proxy_box.error = get_string("bulk_proxy_none_valid")
            page.update()
            return
        page.pop_dialog()
        try:
            on_apply(valid_assignments, bool(auto_geo_cb.value))
        except TypeError:
            on_apply(valid_assignments)

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Text(
            get_string("bulk_proxy_title", count=len(profile_names)),
            size=18,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=500,
            content=ft.Column(
                spacing=10,
                tight=True,
                controls=[
                    proxy_box,
                    repeat_cb,
                    auto_geo_cb,
                    ft.Text(
                        get_string("bulk_proxy_preview_label"),
                        size=12,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["text_sub"],
                    ),
                    preview_col,
                ],
            ),
        ),
        actions=[
            ft.OutlinedButton(
                get_string("bulk_clear_proxy"),
                icon=ft.Icons.CLEAR,
                style=OUTLINE_STYLE,
                on_click=_clear_all,
            ),
            ft.TextButton(
                get_string("cancel"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
            ft.Button(
                get_string("save_changes"),
                icon=ft.Icons.CHECK,
                style=ACCENT_STYLE,
                on_click=_save,
            ),
        ],
    )
    page.show_dialog(dlg)
