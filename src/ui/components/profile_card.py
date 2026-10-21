from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...models.profile import Profile
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
    # Launch button (112) + gap (6) + three icon buttons (~108).
    "actions": (None, 232),
}
ROW_HEIGHT = 58


def _cell(key: str, content: ft.Control | None) -> ft.Container:
    expand, width = COLUMNS[key]
    return ft.Container(content=content, expand=expand, width=width)


def build_header_row(
    all_selected: bool, any_selected: bool, on_select_all: Callable[[bool], None]
) -> ft.Container:
    def label(key: str) -> ft.Text:
        return ft.Text(
            get_string(key), size=12, font_family=FONT_SEMIBOLD, color=COLORS["text_dim"]
        )

    box = ft.Checkbox(
        value=all_selected,
        tooltip=get_string("deselect_page") if all_selected else get_string("select_page"),
        on_change=lambda e: on_select_all(not all_selected),
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
                _cell("proxy", label("col_proxy")),
                _cell("geo", label("col_geo")),
                _cell("actions", None),
            ],
        ),
    )


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
) -> ft.Container:
    """One profile as a table row: identity, device, proxy, geo, actions."""
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
    name_text = ft.Text(
        profile.name,
        size=14,
        font_family=FONT_SEMIBOLD,
        color=COLORS["text_main"],
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
        tooltip=profile.name,
        expand=bool(profile.group or show_failed or profile.color or draggable),
    )
    top_line_controls: list[ft.Control] = [name_text]
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
        else name_text
    )
    if profile.notes:
        single_line_note = " ".join(profile.notes.splitlines()).strip()
        note_text = ft.Text(
            single_line_note,
            size=11.5,
            color=COLORS["text_dim"],
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
            tooltip=profile.notes,
        )
        text_block: ft.Control = _stack(top_line, note_text)
    else:
        text_block = top_line

    name_parts: list[ft.Control] = []
    if draggable:
        name_parts.append(ft.Icon(ft.Icons.DRAG_INDICATOR, size=16, color=COLORS["text_dim"]))
    if profile.color:
        name_parts.append(
            ft.Container(
                width=9,
                height=9,
                border_radius=5,
                bgcolor=profile.color,
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

    def _drag_wrap(ctrl: ft.Control) -> ft.Control:
        if not draggable:
            return ctrl
        return ft.ReorderableDragHandle(
            mouse_cursor=ft.MouseCursor.GRAB,
            content=ctrl,
        )

    launch_btn = build_launch_button(profile.name, is_loading, is_running, on_launch)
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
                _cell("name", _drag_wrap(name_cell_content)),
                _cell("device", _drag_wrap(_device(profile, device))),
                _cell("proxy", _drag_wrap(_proxy(profile))),
                _cell("geo", _drag_wrap(_geo(profile))),
                _cell(
                    "actions",
                    ft.Row(
                        spacing=0,
                        alignment=ft.MainAxisAlignment.END,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            launch_btn,
                            ft.Container(width=6),
                            *_action_buttons(
                                profile.name,
                                is_running,
                                on_edit,
                                on_delete,
                                on_cookies,
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


def _proxy(profile: Profile) -> ft.Control:
    server = mask_proxy(profile.proxy)
    if not server:
        return _stack(
            ft.Text(get_string("direct_short"), size=13, color=COLORS["text_sub"]),
            _minor(get_string("no_proxy_short")),
        )
    scheme, _, rest = server.partition("://")
    return _stack(_data(rest), _minor(scheme.upper()))


def _geo(profile: Profile) -> ft.Column:
    follow = get_string("follows_ip")
    tz = (
        _data(profile.timezone)
        if profile.timezone
        else ft.Text(follow, size=13, color=COLORS["text_dim"])
    )
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
) -> list[ft.IconButton]:
    buttons: list[ft.IconButton] = []
    if on_cookies:
        buttons.append(
            ft.IconButton(
                icon=ft.Icons.COOKIE_OUTLINED,
                icon_size=19,
                icon_color=COLORS["rule_strong"] if is_running else COLORS["text_sub"],
                tooltip=get_string("tooltip_cookies"),
                disabled=is_running,
                style=ICON_STYLE,
                on_click=lambda _, n=name: on_cookies(n),
            ),
        )
    return [
        *buttons,
        ft.IconButton(
            icon=ft.Icons.EDIT_OUTLINED,
            icon_size=19,
            icon_color=COLORS["text_sub"],
            tooltip=get_string("tooltip_edit"),
            style=ICON_STYLE,
            on_click=lambda _, n=name: on_edit(n),
        ),
        ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            icon_size=19,
            icon_color=COLORS["rule_strong"] if is_running else COLORS["text_sub"],
            tooltip=get_string("tooltip_delete"),
            disabled=is_running,
            style=DANGER_ICON_STYLE,
            on_click=lambda _, n=name: on_delete(n),
        ),
    ]
