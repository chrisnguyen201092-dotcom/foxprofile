from collections.abc import Callable
from datetime import datetime

import flet as ft

from ...core.strings import get_string
from ...models.profile import Profile, resolve_status_display
from ...services.monitor import ProfileMonitorInfo
from ...utils.proxy_parser import mask_proxy
from ..theme.colors import COLORS, FONT_DATA, FONT_MEDIUM, FONT_SEMIBOLD
from ..theme.styles import DANGER_ICON_STYLE, ICON_STYLE
from .launch_button import build_launch_button

_OS_LABELS = {"windows": "WIN", "macos": "MAC", "linux": "LIN"}

# One column grid for the header and every row: (expand, fixed width).
COLUMNS = {
    "select": (None, 32),
    # Give profile name ~40% of flexible space for long names, group badges, and notes.
    "name": (16, None),
    "device": (9, None),
    "proxy": (8, None),
    "geo": (7, None),
    # Launch button (100) + gap (4) + four compact icon buttons (4 * 30 = 120).
    "actions": (None, 232),
}
ROW_HEIGHT = 58


def _cell(key: str, content: ft.Control | None) -> ft.Container:
    expand, width = COLUMNS[key]
    return ft.Container(
        content=content,
        expand=expand,
        width=width,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
    )


def build_header_row(
    all_selected: bool,
    any_selected: bool,
    on_select_all: Callable[[bool], None],
    *,
    on_open_proxy_pool: Callable[[], None] | None = None,
    cooldown_chip_label: str = "",
    on_open_cooldown: Callable[[], None] | None = None,
) -> ft.Container:
    def label(key: str) -> ft.Text:
        return ft.Text(
            get_string(key), size=12, font_family=FONT_SEMIBOLD, color=COLORS["text_dim"]
        )

    def _head_chip(
        icon: ft.IconData,
        text: str,
        tooltip: str,
        on_click: Callable[[], None],
    ) -> ft.Container:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=6, vertical=2),
            border_radius=4,
            bgcolor=COLORS["input_bg"],
            border=ft.Border.all(1, COLORS["rule_strong"]),
            ink=True,
            tooltip=tooltip,
            on_click=lambda _: on_click(),
            content=ft.Row(
                spacing=3,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(
                        icon,
                        size=11,
                        color=COLORS["text_sub"],
                    ),
                    ft.Text(
                        text,
                        size=10.5,
                        font_family=FONT_MEDIUM,
                        color=COLORS["text_sub"],
                    ),
                ],
            ),
        )

    box = ft.Checkbox(
        value=all_selected,
        tooltip=get_string("deselect_page") if all_selected else get_string("select_page"),
        on_change=lambda e: on_select_all(not all_selected),
    )
    proxy_header: ft.Control = (
        ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                label("col_proxy"),
                _head_chip(
                    ft.Icons.HUB_OUTLINED,
                    get_string("proxy_pool_btn"),
                    get_string("proxy_pool_title"),
                    on_open_proxy_pool,
                ),
            ],
        )
        if on_open_proxy_pool is not None
        else label("col_proxy")
    )
    geo_header: ft.Control = (
        ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                label("col_geo"),
                _head_chip(
                    ft.Icons.TIMER_OUTLINED,
                    cooldown_chip_label,
                    get_string("cooldown_setting_title"),
                    on_open_cooldown,
                ),
            ],
        )
        if on_open_cooldown is not None and cooldown_chip_label
        else label("col_geo")
    )
    return ft.Container(
        height=40,
        padding=ft.Padding.symmetric(horizontal=14),
        bgcolor=COLORS["bg"],
        border=ft.Border.only(bottom=ft.BorderSide(1, COLORS["card_border"])),
        content=ft.Row(
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                _cell("select", box),
                _cell("name", label("col_profile")),
                _cell("device", label("col_device")),
                _cell("proxy", proxy_header),
                _cell("geo", geo_header),
                _cell("actions", None),
            ],
        ),
    )


def _format_cooldown(mins: int | None) -> str:
    if mins is None:
        return ""
    if mins < 1:
        return get_string("cooldown_just_now")
    if mins < 60:
        return get_string("cooldown_minutes", mins=mins)
    if mins < 1440:
        return get_string("cooldown_hours", hours=mins // 60, mins=mins % 60)
    return get_string("cooldown_days", days=mins // 1440)


def build_profile_card(
    profile: Profile,
    is_loading: bool,
    is_running: bool,
    on_launch: Callable[[str], None],
    on_edit: Callable[[str], None],
    on_delete: Callable[[str], None],
    is_selected: bool = False,
    on_select: Callable[[str], None] | None = None,
    on_cookies: Callable[[str], None] | None = None,
    device: str = "",
    since: str = "",
    failed: bool = False,
    draggable: bool = False,
    *,
    editing_note: bool = False,
    on_start_edit_note: Callable[[str], None] | None = None,
    on_save_note: Callable[[str, str], None] | None = None,
    editing_proxy: bool = False,
    on_start_edit_proxy: Callable[[str], None] | None = None,
    on_save_proxy: Callable[[str, str], None] | None = None,
    on_check_proxy: Callable[[str], None] | None = None,
    on_clone: Callable[[str], None] | None = None,
    on_focus_window: Callable[[str], None] | None = None,
    on_status_click: Callable[[str], None] | None = None,
    on_session_history: Callable[[str], None] | None = None,
    monitor_info: ProfileMonitorInfo | None = None,
    proxy_check_state: tuple[str, str] | None = None,
) -> ft.Container:
    """One profile as a table row: identity, device, proxy, geo/health, actions."""
    if is_running:
        bg = COLORS["card_hover"]
    elif is_selected:
        bg = COLORS["card_selected_bg"]
    else:
        bg = COLORS["card_bg"]

    row_border = (
        ft.Border(
            left=ft.BorderSide(5, profile.color),
            bottom=ft.BorderSide(1, COLORS["card_border"]),
        )
        if profile.color
        else ft.Border.only(bottom=ft.BorderSide(1, COLORS["card_border"]))
    )

    show_failed = failed and not is_loading and not is_running
    status_disp = resolve_status_display(profile.status, get_string)
    has_badges = bool(profile.group or show_failed or status_disp is not None)
    name_tooltip = (
        f"{profile.name}\n({get_string('tooltip_quick_edit_note')})"
        if on_start_edit_note is not None
        else profile.name
    )
    name_text = ft.Text(
        profile.name,
        size=14,
        font_family=FONT_SEMIBOLD,
        color=COLORS["text_main"],
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
        tooltip=name_tooltip,
    )
    name_control: ft.Control = (
        ft.Container(
            expand=has_badges,
            expand_loose=has_badges,
            on_click=lambda _, n=profile.name: on_start_edit_note(n),
            content=name_text,
        )
        if on_start_edit_note is not None
        else (
            ft.Container(expand=True, expand_loose=True, content=name_text)
            if has_badges
            else name_text
        )
    )
    top_line_controls: list[ft.Control] = [name_control]
    if show_failed:
        top_line_controls.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                border_radius=4,
                border=ft.Border.all(1, COLORS["error"]),
                tooltip=get_string("stamp_failed"),
                content=ft.Text(
                    get_string("stamp_failed").upper(),
                    size=10,
                    font_family=FONT_DATA,
                    weight=ft.FontWeight.W_700,
                    color=COLORS["error"],
                ),
            )
        )
    if status_disp is not None:
        st_label, st_color = status_disp
        st_bg = ft.Colors.with_opacity(0.12, st_color)
        top_line_controls.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                border_radius=4,
                bgcolor=st_bg,
                border=ft.Border.all(1, st_color),
                ink=on_status_click is not None,
                on_click=(
                    (lambda _, n=profile.name: on_status_click(n))
                    if on_status_click is not None
                    else None
                ),
                tooltip=(
                    f"{st_label} ({get_string('tooltip_click_status')})"
                    if on_status_click is not None
                    else st_label
                ),
                content=ft.Text(
                    st_label,
                    size=10.5,
                    font_family=FONT_SEMIBOLD,
                    color=st_color,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
            )
        )
    if profile.group:
        top_line_controls.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                border_radius=4,
                bgcolor=COLORS["input_bg"],
                border=ft.Border.all(1, COLORS["rule_strong"]),
                tooltip=profile.group,
                content=ft.Text(
                    profile.group,
                    size=10.5,
                    font_family=FONT_MEDIUM,
                    color=COLORS["text_sub"],
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
            )
        )
    top_line: ft.Control = (
        ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=top_line_controls,
        )
        if len(top_line_controls) > 1
        else name_control
    )

    if editing_note and on_save_note is not None:
        note_input = ft.TextField(
            value=profile.notes or "",
            hint_text=get_string("notes_hint"),
            text_size=11.5,
            height=26,
            content_padding=ft.Padding.symmetric(horizontal=6, vertical=2),
            bgcolor=COLORS["input_bg"],
            border_color=COLORS["accent"],
            border_radius=4,
            autofocus=True,
            on_submit=lambda e, n=profile.name: on_save_note(n, e.control.value or ""),
            on_blur=lambda e, n=profile.name: on_save_note(n, e.control.value or ""),
        )
        text_block: ft.Control = _stack(top_line, note_input)
    elif profile.notes:
        single_line_note = " ".join(profile.notes.splitlines()).strip()
        note_widget: ft.Control = ft.Text(
            single_line_note,
            size=11.5,
            color=COLORS["text_dim"],
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
            tooltip=(
                f"{profile.notes}\n({get_string('tooltip_quick_edit_note')})"
                if on_start_edit_note
                else profile.notes
            ),
        )
        if on_start_edit_note is not None:
            note_widget = ft.Container(
                on_click=lambda _, n=profile.name: on_start_edit_note(n),
                content=note_widget,
            )
        text_block = _stack(top_line, note_widget)
    else:
        text_block = top_line

    name_parts: list[ft.Control] = []
    dot_color = profile.color or (status_disp[1] if status_disp is not None else None)
    if dot_color or on_status_click is not None:
        name_parts.append(
            ft.Container(
                width=10,
                height=10,
                border_radius=5,
                bgcolor=dot_color,
                border=(
                    None
                    if dot_color
                    else ft.Border.all(1.5, COLORS["rule_strong"])
                ),
                ink=on_status_click is not None,
                tooltip=get_string("tooltip_click_status") if on_status_click is not None else None,
                on_click=(
                    (lambda _, n=profile.name: on_status_click(n))
                    if on_status_click is not None
                    else None
                ),
            )
        )
    if name_parts:
        name_parts.append(ft.Container(expand=True, content=text_block))
        name_cell_content: ft.Control = ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=name_parts,
        )
    else:
        name_cell_content = text_block

    def _drag_wrap(ctrl: ft.Control, disable_drag: bool = False) -> ft.Control:
        if not draggable or disable_drag:
            return ctrl
        return ft.ReorderableDragHandle(
            mouse_cursor=ft.MouseCursor.GRAB,
            content=ctrl,
        )

    launch_btn = build_launch_button(profile.name, is_loading, is_running, on_launch)
    launch_btn.width = 100
    if is_running and since:
        launch_btn.tooltip = f"{get_string('stamp_live')} ({since})"

    return ft.Container(
        key=profile.name,
        height=ROW_HEIGHT,
        padding=ft.Padding.only(
            left=10 if profile.color else 14,
            right=14,
        ),
        bgcolor=bg,
        border=row_border,
        content=ft.Row(
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                _cell(
                    "select",
                    ft.Checkbox(
                        value=is_selected,
                        tooltip=get_string("tooltip_select"),
                        on_change=lambda _, n=profile.name: on_select(n) if on_select else None,
                    ),
                ),
                _cell("name", _drag_wrap(name_cell_content, disable_drag=editing_note)),
                _cell("device", _drag_wrap(_device(profile, device))),
                _cell(
                    "proxy",
                    _drag_wrap(
                        _proxy(
                            profile,
                            editing_proxy=editing_proxy,
                            on_start_edit_proxy=on_start_edit_proxy,
                            on_save_proxy=on_save_proxy,
                            on_check_proxy=on_check_proxy,
                            monitor_info=monitor_info,
                            proxy_check_state=proxy_check_state,
                        ),
                        disable_drag=editing_proxy,
                    ),
                ),
                _cell(
                    "geo",
                    _drag_wrap(
                        _geo(
                            profile,
                            monitor_info,
                            is_running=is_running,
                            since=since,
                            on_focus_window=on_focus_window,
                            on_session_history=on_session_history,
                        )
                    ),
                ),
                _cell(
                    "actions",
                    ft.Row(
                        spacing=0,
                        alignment=ft.MainAxisAlignment.END,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            launch_btn,
                            ft.Container(width=4),
                            *_action_buttons(
                                profile.name,
                                is_running,
                                on_edit,
                                on_delete,
                                on_cookies,
                                on_clone,
                            ),
                        ],
                    ),
                ),
            ],
        ),
    )


def _data(text: str, size: float = 12.5, color: str | None = None) -> ft.Text:
    return ft.Text(
        text,
        size=size,
        font_family=FONT_DATA,
        color=color or COLORS["text_main"],
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
    )


def _minor(text: str, data: bool = False) -> ft.Text:
    return ft.Text(
        text,
        size=11.5,
        font_family=FONT_DATA if data else None,
        color=COLORS["text_dim"],
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
    )


def _stack(*lines: ft.Control) -> ft.Column:
    return ft.Column(spacing=1, alignment=ft.MainAxisAlignment.CENTER, controls=list(lines))


def _device(profile: Profile, device: str) -> ft.Row:
    chrome = profile.engine == "chrome"
    engine = ft.Text(
        "Chrome" if chrome else "Camoufox",
        size=13,
        font_family=FONT_SEMIBOLD if chrome else None,
        color=COLORS["sidebar"] if chrome else COLORS["text_main"],
    )
    return ft.Row(
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Container(
                width=38,
                height=22,
                border_radius=4,
                border=ft.Border.all(1, COLORS["text_sub"]),
                alignment=ft.Alignment(0, 0),
                tooltip=profile.os_type,
                content=ft.Text(
                    _OS_LABELS.get(profile.os_type, "OS"),
                    size=11,
                    font_family=FONT_DATA,
                    weight=ft.FontWeight.W_600,
                    color=COLORS["text_main"],
                ),
            ),
            ft.Container(
                expand=True,
                # Every row keeps two lines, so heights and baselines match.
                content=_stack(
                    engine,
                    _minor(device, data=True)
                    if device
                    else _minor(get_string("no_fingerprint_short")),
                ),
            ),
        ],
    )


def _proxy(
    profile: Profile,
    *,
    editing_proxy: bool = False,
    on_start_edit_proxy: Callable[[str], None] | None = None,
    on_save_proxy: Callable[[str, str], None] | None = None,
    on_check_proxy: Callable[[str], None] | None = None,
    monitor_info: ProfileMonitorInfo | None = None,
    proxy_check_state: tuple[str, str] | None = None,
) -> ft.Control:
    _ = on_check_proxy
    if editing_proxy and on_save_proxy is not None:
        return ft.TextField(
            value=profile.proxy or "",
            hint_text=get_string("proxy_hint"),
            text_size=11.5,
            height=30,
            content_padding=ft.Padding.symmetric(horizontal=6, vertical=2),
            bgcolor=COLORS["input_bg"],
            border_color=COLORS["accent"],
            border_radius=4,
            autofocus=True,
            on_submit=lambda e, n=profile.name: on_save_proxy(n, e.control.value or ""),
            on_blur=lambda e, n=profile.name: on_save_proxy(n, e.control.value or ""),
        )

    server = mask_proxy(profile.proxy)
    if not server:
        base_col: ft.Control = _stack(
            ft.Text(get_string("direct_short"), size=13, color=COLORS["text_sub"]),
            _minor(get_string("no_proxy_short")),
        )
        if on_start_edit_proxy is not None:
            return ft.Container(
                on_click=lambda _, n=profile.name: on_start_edit_proxy(n),
                tooltip=get_string("tooltip_quick_edit_proxy"),
                content=base_col,
            )
        return base_col

    scheme, _, rest = server.partition("://")
    sub_label = scheme.upper()
    sub_color = COLORS["text_dim"]
    if proxy_check_state is not None and proxy_check_state[0] in ("checking", "fail"):
        st_kind, st_msg = proxy_check_state
        sub_label = st_msg
        sub_color = COLORS["accent"] if st_kind == "checking" else COLORS["error"]
    elif monitor_info is not None and monitor_info.busy_by:
        sub_label = get_string("proxy_busy_by", other=monitor_info.busy_by[0])
        sub_color = COLORS["error"]
    elif monitor_info is not None and monitor_info.port_cooldown_by is not None:
        other, left = monitor_info.port_cooldown_by
        sub_label = get_string("proxy_cooldown_by", left=left, other=other)
        sub_color = COLORS["warning"]
    elif monitor_info is not None and monitor_info.proxy_port_changed:
        sub_label = get_string("proxy_changed_badge", old_port=monitor_info.last_used_port)
        sub_color = COLORS["error"]
    elif proxy_check_state is not None:
        _, st_msg = proxy_check_state
        sub_label = st_msg
        sub_color = COLORS["success"]

    host_text = _data(rest)
    host_text.tooltip = (
        f"{rest} ({get_string('tooltip_quick_edit_proxy')})"
        if on_start_edit_proxy is not None
        else rest
    )
    line1: ft.Control = (
        ft.Container(
            on_click=lambda _, n=profile.name: on_start_edit_proxy(n),
            content=host_text,
        )
        if on_start_edit_proxy is not None
        else host_text
    )
    line2 = ft.Text(
        sub_label,
        size=11.5,
        font_family=FONT_DATA,
        color=sub_color,
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
        tooltip=sub_label,
    )
    return _stack(line1, line2)


def _geo(
    profile: Profile,
    monitor_info: ProfileMonitorInfo | None = None,
    *,
    is_running: bool = False,
    since: str = "",
    on_focus_window: Callable[[str], None] | None = None,
    on_session_history: Callable[[str], None] | None = None,
) -> ft.Column:
    follow = get_string("follows_ip")
    tz = (
        _data(profile.timezone)
        if profile.timezone
        else ft.Text(follow, size=12.5, color=COLORS["text_dim"])
    )
    if is_running:
        run_label = f"● {get_string('stamp_live')}" + (f" · {since}" if since else "")
        run_text = ft.Text(
            run_label,
            size=11.5,
            font_family=FONT_DATA,
            color=COLORS["success"],
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
            tooltip=get_string("tooltip_focus_window") if on_focus_window else run_label,
        )
        if on_focus_window is not None:
            return _stack(
                tz,
                ft.Container(
                    ink=True,
                    border_radius=4,
                    tooltip=get_string("tooltip_focus_window"),
                    on_click=lambda _, n=profile.name: on_focus_window(n),
                    content=ft.Row(
                        spacing=4,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            run_text,
                            ft.Icon(
                                ft.Icons.OPEN_IN_NEW,
                                size=11.5,
                                color=COLORS["success"],
                            ),
                        ],
                    ),
                ),
            )
        return _stack(tz, run_text)
    if monitor_info is not None and monitor_info.last_closed is not None:
        cd = _format_cooldown(monitor_info.cooldown_mins)
        is_today = monitor_info.last_closed.date() == datetime.now().date()
        short_ts = (
            monitor_info.last_closed.strftime("%H:%M")
            if is_today
            else monitor_info.last_closed.strftime("%d/%m")
        )
        full_ts = monitor_info.last_closed.strftime("%H:%M %d/%m")
        sub_str = f"{cd} · {short_ts}" if cd else short_ts
        tooltip_str = f"{cd} · {full_ts}" if cd else full_ts
        cd_text = ft.Text(
            sub_str,
            size=11.5,
            font_family=FONT_DATA,
            color=COLORS["text_sub"],
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
            tooltip=(
                f"{tooltip_str} ({get_string('session_hist_btn')})"
                if on_session_history is not None
                else tooltip_str
            ),
        )
        if on_session_history is not None:
            return _stack(
                tz,
                ft.Container(
                    ink=True,
                    border_radius=4,
                    on_click=lambda _, n=profile.name: on_session_history(n),
                    content=cd_text,
                ),
            )
        return _stack(tz, cd_text)
    locale = _minor(profile.locale, data=True) if profile.locale else _minor(follow)
    return _stack(tz, locale)


def _stamp(is_loading: bool, is_running: bool, since: str, failed: bool = False) -> ft.Control:
    """The entry stamp: a rotated rubber-stamp mark while a profile is live.

    State reads by form as well as ink: live is a double rule, starting a single
    thin rule set straight, failure a struck single rule in red.
    """
    if failed and not is_loading and not is_running:
        # Same footprint as the live stamp so the state that needs action is not
        # the smallest one on screen; a thin strike keeps the letters legible.
        return ft.Container(
            rotate=ft.Rotate(-0.07),
            width=86,
            padding=ft.Padding.symmetric(horizontal=9, vertical=5),
            border_radius=6,
            border=ft.Border.all(1.5, COLORS["error"]),
            tooltip=get_string("stamp_failed"),
            content=ft.Text(
                get_string("stamp_failed").upper(),
                size=10.5,
                text_align=ft.TextAlign.CENTER,
                font_family=FONT_DATA,
                weight=ft.FontWeight.W_700,
                color=COLORS["error"],
                style=ft.TextStyle(
                    decoration=ft.TextDecoration.LINE_THROUGH, decoration_thickness=0.8
                ),
            ),
        )
    if is_loading:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=9, vertical=3),
            border_radius=6,
            border=ft.Border.all(1, COLORS["text_sub"]),
            content=ft.Text(
                get_string("stamp_starting").upper(),
                size=10.5,
                font_family=FONT_DATA,
                weight=ft.FontWeight.W_700,
                color=COLORS["text_sub"],
            ),
        )
    if not is_running:
        return ft.Text(get_string("stamp_idle"), size=13, color=COLORS["text_dim"])
    ink = COLORS["live"]
    lines: list[ft.Control] = [
        ft.Text(
            get_string("stamp_live").upper(),
            size=10.5,
            font_family=FONT_DATA,
            weight=ft.FontWeight.W_700,
            color=ink,
        )
    ]
    if since:
        lines.append(ft.Text(since, size=10, font_family=FONT_DATA, color=ink))
    # Double rule: an outer ring around the inked frame.
    return ft.Container(
        rotate=ft.Rotate(-0.07),
        padding=2,
        border_radius=8,
        border=ft.Border.all(1, ink),
        content=ft.Container(
            padding=ft.Padding.symmetric(horizontal=9, vertical=2),
            border_radius=6,
            border=ft.Border.all(1.5, ink),
            bgcolor=ft.Colors.with_opacity(0.35, "#FFFFFF"),
            content=ft.Column(
                spacing=0,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=lines,
            ),
        ),
    )


def _action_buttons(
    name: str,
    is_running: bool,
    on_edit: Callable[[str], None],
    on_delete: Callable[[str], None],
    on_cookies: Callable[[str], None] | None,
    on_clone: Callable[[str], None] | None = None,
) -> list[ft.IconButton]:
    buttons: list[ft.IconButton] = []
    if on_cookies:
        buttons.append(
            ft.IconButton(
                icon=ft.Icons.COOKIE_OUTLINED,
                icon_size=17,
                width=30,
                height=30,
                padding=0,
                icon_color=COLORS["success"] if is_running else COLORS["text_sub"],
                tooltip=get_string("tooltip_cookies"),
                disabled=False,
                style=ICON_STYLE,
                on_click=lambda _, n=name: on_cookies(n),
            ),
        )
    if on_clone:
        buttons.append(
            ft.IconButton(
                icon=ft.Icons.CONTENT_COPY,
                icon_size=16,
                width=30,
                height=30,
                padding=0,
                icon_color=COLORS["text_sub"],
                tooltip=get_string("tooltip_clone"),
                style=ICON_STYLE,
                on_click=lambda _, n=name: on_clone(n),
            ),
        )
    return [
        *buttons,
        ft.IconButton(
            icon=ft.Icons.EDIT_OUTLINED,
            icon_size=17,
            width=30,
            height=30,
            padding=0,
            icon_color=COLORS["text_sub"],
            tooltip=get_string("tooltip_edit"),
            style=ICON_STYLE,
            on_click=lambda _, n=name: on_edit(n),
        ),
        ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            icon_size=17,
            width=30,
            height=30,
            padding=0,
            icon_color=COLORS["rule_strong"] if is_running else COLORS["text_sub"],
            tooltip=get_string("tooltip_delete"),
            disabled=is_running,
            style=DANGER_ICON_STYLE,
            on_click=lambda _, n=name: on_delete(n),
        ),
    ]
