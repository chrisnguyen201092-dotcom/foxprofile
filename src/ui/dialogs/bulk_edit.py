import contextlib
import inspect
from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...models.profile import (
    PROFILE_COLORS,
    compute_days_since,
    format_date_dmy,
    get_active_status_tags,
    parse_flexible_date,
    resolve_tag_label,
    today_ymd,
)
from ...services.monitor import extract_proxy_port
from ...utils.proxy_parser import mask_proxy
from ...utils.validation import validate_proxy_format, validate_status
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_MEDIUM, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, OUTLINE_STYLE

_DATA = ft.TextStyle(font_family=FONT_DATA, size=12.5)


def open_bulk_group_dialog(
    page: ft.Page,
    count: int,
    existing_groups: list[str],
    on_apply: Callable[[str | None], None],
) -> None:
    """Dialog to assign or clear a group for all selected profiles."""
    group_field = ft.TextField(
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
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Text(
            get_string("bulk_group_title", count=count),
            size=19,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=560,
            content=ft.Column(
                tight=True,
                spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Text(
                        get_string("group_optional"),
                        size=12.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["text_sub"],
                    ),
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
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Text(
            get_string("bulk_color_title", count=count),
            size=19,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=500,
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
        hint_text=get_string("bulk_proxy_input_hint"),
        multiline=True,
        min_lines=5,
        max_lines=7,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    repeat_cb = ft.Checkbox(
        label=get_string("bulk_proxy_repeat_cb"),
        value=False,
        label_style=ft.TextStyle(size=12.5, font_family=FONT_MEDIUM, color=COLORS["text_main"]),
    )
    auto_geo_cb = ft.Checkbox(
        label=get_string("auto_pin_geo_cb"),
        value=True,
        label_style=ft.TextStyle(size=12.5, font_family=FONT_MEDIUM, color=COLORS["text_main"]),
    )
    preview_col = ft.Column(
        spacing=4,
        height=280,
        scroll=ft.ScrollMode.AUTO,
    )

    def _compute_mapping() -> tuple[dict[str, str | None], list[ft.Control]]:
        lines = [ln.strip() for ln in (proxy_box.value or "").splitlines() if ln.strip()]
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
        with contextlib.suppress(Exception):
            preview_col.update()

    _, initial_rows = _compute_mapping()
    preview_col.controls = initial_rows
    proxy_box.on_change = _refresh_preview
    repeat_cb.on_change = _refresh_preview

    def _call_apply(assignments: dict[str, str | None], auto_geo: bool) -> None:
        sig = inspect.signature(on_apply)
        has_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()
        )
        if has_varargs or len(sig.parameters) >= 2:
            on_apply(assignments, auto_geo)
        else:
            on_apply(assignments)  # type: ignore[call-arg]

    def _clear_all(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        _call_apply({n: None for n in profile_names}, False)

    def _save(_: ft.ControlEvent) -> None:
        valid_assignments, _ = _compute_mapping()
        if not valid_assignments:
            proxy_box.error = get_string("bulk_proxy_none_valid")
            page.update()
            return
        page.pop_dialog()
        _call_apply(valid_assignments, bool(auto_geo_cb.value))

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Text(
            get_string("bulk_proxy_title", count=len(profile_names)),
            size=18,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=720,
            content=ft.Column(
                spacing=8,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Text(
                        get_string("bulk_proxy_input_label", count=len(profile_names)),
                        size=12.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["text_sub"],
                    ),
                    proxy_box,
                    ft.Column(spacing=2, tight=True, controls=[repeat_cb, auto_geo_cb]),
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


_STATUS_PALETTE = (
    "#16A34A",
    "#2563EB",
    "#7C3AED",
    "#D97706",
    "#DC2626",
    "#06B6D4",
    "#EC4899",
    "#475569",
)


def open_status_dialog(
    page: ft.Page,
    title_text: str,
    current_status: str | None,
    on_apply: Callable[[str | None], None],
    *,
    on_save_tag: Callable[[str, str, str], bool] | None = None,
    on_delete_tag: Callable[[str], bool] | None = None,
    on_reset_tags: Callable[[], list[dict[str, str]]] | None = None,
) -> None:
    """Quick 1-click status picker + editable/deletable status tag manager."""
    manage_mode: list[bool] = [False]
    new_color: list[str] = [_STATUS_PALETTE[1]]

    tags_now = get_active_status_tags()
    known_codes = {
        s.casefold()
        for t in tags_now
        for s in (t.get("code") or "", resolve_tag_label(t, get_string))
        if s
    }
    init_custom = (
        current_status
        if current_status and current_status.strip().casefold() not in known_codes
        else ""
    )
    status_field = ft.TextField(
        value=init_custom,
        hint_text=get_string("status_hint"),
        expand=True,
        **DLG_FIELD_KWARGS,
    )
    err_text = ft.Text("", size=12, color=COLORS["error"], visible=False)
    body_col = ft.Column(
        spacing=10,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    manage_btn = ft.OutlinedButton(
        get_string("status_manage_btn"),
        icon=ft.Icons.EDIT_NOTE,
        height=32,
        style=OUTLINE_STYLE,
    )

    def _pick(val: str | None) -> None:
        if val is not None:
            ok, err = validate_status(val)
            if not ok:
                err_text.value = err
                err_text.visible = True
                page.update()
                return
            if on_save_tag is not None:
                cur_tags = get_active_status_tags()
                exists = any(
                    val.casefold()
                    in {
                        (t.get("code") or "").casefold(),
                        resolve_tag_label(t, get_string).casefold(),
                    }
                    for t in cur_tags
                )
                if not exists:
                    on_save_tag("", val, new_color[0])
        page.pop_dialog()
        on_apply(val)

    status_field.on_submit = lambda _: _pick((status_field.value or "").strip() or None)

    def _cycle_new_color(_: ft.ControlEvent) -> None:
        idx = _STATUS_PALETTE.index(new_color[0]) if new_color[0] in _STATUS_PALETTE else 0
        new_color[0] = _STATUS_PALETTE[(idx + 1) % len(_STATUS_PALETTE)]
        _rebuild_body()
        page.update()

    def _toggle_manage(_: ft.ControlEvent) -> None:
        manage_mode[0] = not manage_mode[0]
        err_text.visible = False
        manage_btn.content = ft.Text(
            get_string("status_done_manage_btn")
            if manage_mode[0]
            else get_string("status_manage_btn")
        )
        _rebuild_body()
        page.update()

    manage_btn.on_click = _toggle_manage

    def _rebuild_body() -> None:
        active_tags = get_active_status_tags()
        body_col.controls.clear()

        if manage_mode[0]:
            edit_rows: list[ft.Control] = []
            for t in active_tags:
                t_code = t.get("code") or ""
                t_lbl = resolve_tag_label(t, get_string)
                t_col = t.get("color") or "#0284C7"
                row_color: list[str] = [t_col]
                tf = ft.TextField(
                    value=t_lbl,
                    expand=True,
                    **DLG_FIELD_KWARGS,
                )

                def _make_cycle_col(
                    code_val: str = t_code,
                    tf_ctrl: ft.TextField = tf,
                    col_ref: list[str] = row_color,
                ) -> Callable[[ft.ControlEvent], None]:
                    def _cb(_: ft.ControlEvent) -> None:
                        idx = (
                            _STATUS_PALETTE.index(col_ref[0])
                            if col_ref[0] in _STATUS_PALETTE
                            else 0
                        )
                        col_ref[0] = _STATUS_PALETTE[(idx + 1) % len(_STATUS_PALETTE)]
                        if on_save_tag is not None:
                            on_save_tag(
                                code_val,
                                (tf_ctrl.value or "").strip() or code_val,
                                col_ref[0],
                            )
                        _rebuild_body()
                        page.update()

                    return _cb

                def _make_save_tag(
                    code_val: str = t_code,
                    tf_ctrl: ft.TextField = tf,
                    col_ref: list[str] = row_color,
                ) -> Callable[[ft.ControlEvent], None]:
                    def _cb(_: ft.ControlEvent) -> None:
                        new_lbl = (tf_ctrl.value or "").strip()
                        ok, err = validate_status(new_lbl)
                        if not new_lbl or not ok:
                            err_text.value = err or get_string("validation_status_too_long")
                            err_text.visible = True
                            page.update()
                            return
                        if on_save_tag is not None:
                            on_save_tag(code_val, new_lbl, col_ref[0])
                        err_text.visible = False
                        _rebuild_body()
                        page.update()

                    return _cb

                def _make_del_tag(code_val: str = t_code) -> Callable[[ft.ControlEvent], None]:
                    def _cb(_: ft.ControlEvent) -> None:
                        if on_delete_tag is not None:
                            on_delete_tag(code_val)
                        _rebuild_body()
                        page.update()

                    return _cb

                tf.on_submit = _make_save_tag()
                edit_rows.append(
                    ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Container(
                                width=24,
                                height=24,
                                border_radius=12,
                                bgcolor=row_color[0],
                                ink=True,
                                tooltip=get_string("bulk_set_color"),
                                on_click=_make_cycle_col(),
                            ),
                            tf,
                            ft.IconButton(
                                icon=ft.Icons.CHECK,
                                icon_size=18,
                                width=32,
                                height=32,
                                padding=0,
                                icon_color=COLORS["success"],
                                tooltip=get_string("status_save_tooltip"),
                                on_click=_make_save_tag(),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_size=18,
                                width=32,
                                height=32,
                                padding=0,
                                icon_color=COLORS["error"],
                                tooltip=get_string("status_delete_tooltip"),
                                on_click=_make_del_tag(),
                            ),
                        ],
                    )
                )

            def _do_reset(_: ft.ControlEvent) -> None:
                if on_reset_tags is not None:
                    on_reset_tags()
                _rebuild_body()
                page.update()

            body_col.controls.extend(
                [
                    ft.Column(
                        spacing=8,
                        height=min(260, max(90, len(edit_rows) * 52)),
                        scroll=ft.ScrollMode.AUTO,
                        controls=edit_rows,
                    ),
                    *(
                        [
                            ft.Row(
                                alignment=ft.MainAxisAlignment.START,
                                controls=[
                                    ft.OutlinedButton(
                                        get_string("status_reset_defaults"),
                                        icon=ft.Icons.RESTORE,
                                        height=32,
                                        style=OUTLINE_STYLE,
                                        on_click=_do_reset,
                                    )
                                ],
                            )
                        ]
                        if on_reset_tags is not None
                        else []
                    ),
                    err_text,
                ]
            )
            return

        preset_pills: list[ft.Control] = []
        cur_low = (current_status or "").strip().casefold()
        for t in active_tags:
            code = t.get("code") or ""
            lbl = resolve_tag_label(t, get_string)
            hex_col = t.get("color") or "#0284C7"
            is_active = bool(cur_low and cur_low in {code.casefold(), lbl.casefold()})
            preset_pills.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=7),
                    border_radius=8,
                    bgcolor=ft.Colors.with_opacity(0.14, hex_col)
                    if is_active
                    else COLORS["input_bg"],
                    border=ft.Border.all(1.5 if is_active else 1, hex_col),
                    ink=True,
                    on_click=lambda _, c=code: _pick(c),
                    content=ft.Row(
                        spacing=6,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Container(
                                width=8,
                                height=8,
                                border_radius=4,
                                bgcolor=hex_col,
                            ),
                            ft.Text(
                                lbl,
                                size=12.5,
                                font_family=FONT_SEMIBOLD,
                                color=hex_col,
                            ),
                        ],
                    ),
                )
            )

        preset_pills.append(
            ft.OutlinedButton(
                get_string("status_clear"),
                icon=ft.Icons.CLEAR,
                height=34,
                style=OUTLINE_STYLE,
                on_click=lambda _: _pick(None),
            )
        )

        body_col.controls.extend(
            [
                ft.Row(wrap=True, spacing=8, run_spacing=8, controls=preset_pills),
                ft.Text(
                    get_string("status_add_new_label"),
                    size=12.5,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_sub"],
                ),
                ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Container(
                            width=26,
                            height=26,
                            border_radius=13,
                            bgcolor=new_color[0],
                            ink=True,
                            tooltip=get_string("bulk_set_color"),
                            on_click=_cycle_new_color,
                        ),
                        status_field,
                    ],
                ),
                err_text,
            ]
        )

    _rebuild_body()

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(
                    title_text,
                    size=19,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                manage_btn,
            ],
        ),
        content=ft.Container(
            width=600,
            content=body_col,
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
                on_click=lambda _: _pick((status_field.value or "").strip() or None),
            ),
        ],
    )
    page.show_dialog(dlg)


def open_profile_dates_dialog(
    page: ft.Page,
    profile_name: str,
    created_at: str | None,
    banned_at: str | None,
    on_apply: Callable[[str | None, str | None], None],
) -> None:
    """Dialog to view/edit account creation date (age) and ban date."""
    created_field = ft.TextField(
        value=format_date_dmy(created_at) if created_at else format_date_dmy(today_ymd()),
        hint_text=get_string("dates_hint_created"),
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    banned_field = ft.TextField(
        value=format_date_dmy(banned_at) if banned_at else "",
        hint_text=get_string("dates_hint_banned"),
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    created_preview = ft.Text("", size=11.5, font_family=FONT_MEDIUM, color=COLORS["text_sub"])
    banned_preview = ft.Text("", size=11.5, font_family=FONT_MEDIUM, color=COLORS["error"])

    def _update_previews(_: ft.ControlEvent | None = None) -> None:
        created_field.error = None
        banned_field.error = None
        c_ymd = parse_flexible_date(created_field.value)
        if c_ymd:
            c_days = compute_days_since(c_ymd) or 0
            created_preview.value = (
                f"{get_string('age_days_badge', days=c_days)} ({format_date_dmy(c_ymd)})"
            )
            created_preview.color = COLORS["accent"]
        else:
            created_preview.value = ""

        raw_b = (banned_field.value or "").strip()
        b_ymd = parse_flexible_date(raw_b) if raw_b else None
        if b_ymd:
            b_days = compute_days_since(b_ymd) or 0
            banned_preview.value = (
                f"{get_string('ban_days_badge', days=b_days)} ({format_date_dmy(b_ymd)})"
            )
            banned_preview.color = COLORS["text_sub"]
        else:
            banned_preview.value = ""
        with contextlib.suppress(Exception):
            page.update()

    created_field.on_change = _update_previews
    banned_field.on_change = _update_previews
    _update_previews()

    def _set_banned_today(_: ft.ControlEvent) -> None:
        banned_field.value = format_date_dmy(today_ymd())
        _update_previews()

    def _clear_banned(_: ft.ControlEvent) -> None:
        banned_field.value = ""
        _update_previews()

    def _save(_: ft.ControlEvent) -> None:
        raw_c = (created_field.value or "").strip()
        c_ymd = parse_flexible_date(raw_c) if raw_c else today_ymd()
        if raw_c and c_ymd is None:
            created_field.error = get_string("validation_invalid_date")
            page.update()
            return
        raw_b = (banned_field.value or "").strip()
        b_ymd = parse_flexible_date(raw_b) if raw_b else None
        if raw_b and b_ymd is None:
            banned_field.error = get_string("validation_invalid_date")
            page.update()
            return
        page.pop_dialog()
        on_apply(c_ymd, b_ymd)

    created_field.on_submit = _save
    banned_field.on_submit = _save

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=12, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Text(
            get_string("dates_dialog_title", name=profile_name),
            size=18,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=560,
            content=ft.Column(
                spacing=8,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(
                                get_string("created_date_label"),
                                size=12.5,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_sub"],
                            ),
                            created_preview,
                        ],
                    ),
                    created_field,
                    ft.Container(height=4),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(
                                get_string("banned_date_label"),
                                size=12.5,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_sub"],
                            ),
                            banned_preview,
                        ],
                    ),
                    banned_field,
                    ft.Row(
                        spacing=8,
                        controls=[
                            ft.OutlinedButton(
                                get_string("mark_banned_today_btn"),
                                icon=ft.Icons.BLOCK,
                                height=32,
                                style=OUTLINE_STYLE,
                                on_click=_set_banned_today,
                            ),
                            ft.OutlinedButton(
                                get_string("clear_banned_btn"),
                                icon=ft.Icons.CHECK_CIRCLE_OUTLINE,
                                height=32,
                                style=OUTLINE_STYLE,
                                on_click=_clear_banned,
                            ),
                        ],
                    ),
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
                on_click=_save,
            ),
        ],
    )
    page.show_dialog(dlg)
