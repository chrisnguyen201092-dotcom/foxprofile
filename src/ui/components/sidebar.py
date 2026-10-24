from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...models.profile import resolve_status_display
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_MEDIUM, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, RAIL_STYLE

# (filter id, string key) in display order; counts come from the app.
FILTERS = (
    ("all", "filter_all"),
    ("running", "filter_running"),
    ("ready_launch", "filter_ready_launch"),
    ("pinned", "filter_pinned"),
    ("cooldown", "filter_cooldown"),
    ("warnings", "filter_warnings"),
    ("direct", "filter_direct"),
    ("chrome", "filter_chrome"),
)
_ALWAYS_VISIBLE_FILTERS = {"all", "running", "ready_launch"}


def _filter_row(
    fid: str, label_text: str, count: int, active: str, on_filter: Callable[[str], None]
) -> ft.Container:
    on = fid == active
    return ft.Container(
        height=32,
        border_radius=7,
        padding=ft.Padding.symmetric(horizontal=10),
        bgcolor=COLORS["cover_ink"] if on else None,
        ink=True,
        on_click=lambda _, f=fid: on_filter(f),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(
                    label_text,
                    size=13,
                    font_family=FONT_SEMIBOLD if on else FONT_MEDIUM,
                    color=COLORS["sidebar"] if on else COLORS["cover_ink"],
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    expand=True,
                ),
                ft.Text(
                    str(count),
                    size=11.5,
                    font_family=FONT_DATA,
                    color=COLORS["sidebar"] if on else COLORS["cover_muted"],
                ),
            ],
        ),
    )


def _tag_chip(
    fid: str,
    label_text: str,
    count: int,
    active: str,
    on_filter: Callable[[str], None],
) -> ft.Container:
    on = fid == active
    return ft.Container(
        height=26,
        padding=ft.Padding.symmetric(horizontal=9),
        border_radius=6,
        bgcolor=COLORS["cover_ink"] if on else COLORS["cover_deep"],
        border=ft.Border.all(
            1, COLORS["cover_ink"] if on else ft.Colors.with_opacity(0.22, COLORS["cover_muted"])
        ),
        ink=True,
        on_click=lambda _, f=fid: on_filter(f),
        content=ft.Row(
            spacing=5,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(
                    label_text,
                    size=12,
                    font_family=FONT_SEMIBOLD if on else FONT_MEDIUM,
                    color=COLORS["sidebar"] if on else COLORS["cover_ink"],
                ),
                ft.Text(
                    str(count),
                    size=11,
                    font_family=FONT_DATA,
                    color=COLORS["sidebar"] if on else COLORS["cover_muted"],
                ),
            ],
        ),
    )


def build_filter_buttons(
    active: str,
    counts: dict[str, int],
    on_filter: Callable[[str], None],
    group_counts: dict[str, int] | None = None,
    status_counts: dict[str, int] | None = None,
) -> list[ft.Control]:
    """The rail's filter list; secondary filters with 0 count auto-hide to reduce clutter."""
    controls: list[ft.Control] = [
        _filter_row(fid, get_string(key), counts.get(fid, 0), active, on_filter)
        for fid, key in FILTERS
        if fid in _ALWAYS_VISIBLE_FILTERS or counts.get(fid, 0) > 0 or active == fid
    ]
    if status_counts:
        controls.append(
            ft.Container(
                padding=ft.Padding.only(left=10, top=10, bottom=4),
                content=ft.Text(
                    get_string("status_filter_title"),
                    size=11,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["cover_muted"],
                ),
            )
        )
        status_chips: list[ft.Control] = []
        for st_val, cnt in status_counts.items():
            disp = resolve_status_display(st_val, get_string)
            lbl = disp[0] if disp is not None else st_val
            status_chips.append(_tag_chip(f"status:{st_val}", lbl, cnt, active, on_filter))
        controls.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6),
                content=ft.Row(spacing=6, run_spacing=6, wrap=True, controls=status_chips),
            )
        )
    if group_counts:
        controls.append(
            ft.Container(
                padding=ft.Padding.only(left=10, top=10, bottom=4),
                content=ft.Text(
                    get_string("groups_title"),
                    size=11,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["cover_muted"],
                ),
            )
        )
        group_chips = [
            _tag_chip(f"group:{grp}", grp, cnt, active, on_filter)
            for grp, cnt in group_counts.items()
        ]
        controls.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6),
                content=ft.Row(spacing=6, run_spacing=6, wrap=True, controls=group_chips),
            )
        )
    return controls


def build_sidebar(
    stats_text: ft.Text,
    running_text: ft.Text,
    _log_text: ft.Text,
    log_column: ft.Container,
    log_toggle_btn: ft.TextButton,
    on_new_profile: Callable,
    on_import: Callable,
    on_export: Callable,
    on_toggle_log: Callable,
    on_mcp: Callable,
    on_fullscreen_log: Callable,
    filter_column: ft.Column | None = None,
    language: str = "en",
    on_language: Callable[[str], None] | None = None,
    trash_btn: ft.Control | None = None,
) -> ft.Container:
    log_toggle_btn.on_click = on_toggle_log
    log_icon_btn = ft.Container(
        width=34,
        height=34,
        border_radius=7,
        bgcolor=COLORS["cover_deep"],
        border=ft.Border.all(1, ft.Colors.with_opacity(0.25, COLORS["cover_muted"])),
        alignment=ft.Alignment(0, 0),
        ink=True,
        tooltip=get_string("activity_log"),
        on_click=on_fullscreen_log,
        content=ft.Icon(
            ft.Icons.RECEIPT_LONG_OUTLINED,
            size=16,
            color=COLORS["cover_ink"],
        ),
    )
    mcp_icon_btn = ft.Container(
        width=34,
        height=34,
        border_radius=7,
        bgcolor=COLORS["cover_deep"],
        border=ft.Border.all(1, ft.Colors.with_opacity(0.25, COLORS["cover_muted"])),
        alignment=ft.Alignment(0, 0),
        ink=True,
        tooltip=get_string("mcp_button"),
        on_click=on_mcp,
        content=ft.Icon(
            ft.Icons.SMART_TOY_OUTLINED,
            size=16,
            color=COLORS["cover_ink"],
        ),
    )
    bottom_row_controls: list[ft.Control] = []
    if trash_btn is not None:
        bottom_row_controls.append(trash_btn)
    bottom_row_controls.extend(
        [
            log_icon_btn,
            mcp_icon_btn,
            _language_switch(language, on_language),
        ]
    )
    return ft.Container(
        width=240,
        bgcolor=COLORS["sidebar"],
        # The tile is rendered at 2x; scale 2 draws it at its authored size.
        image=ft.DecorationImage(src="rail-guilloche.png", repeat=ft.ImageRepeat.REPEAT, scale=2),
        padding=ft.Padding.only(left=16, right=16, top=22, bottom=16),
        content=ft.Column(
            spacing=0,
            expand=True,
            controls=[
                ft.Row(
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Image(src="logo.png", width=34, height=34),
                        ft.Column(
                            spacing=0,
                            controls=[
                                ft.Text(
                                    get_string("app_name"),
                                    size=18,
                                    font_family=FONT_BOLD,
                                    color=COLORS["cover_ink"],
                                ),
                                ft.Text(
                                    get_string("app_subtitle"),
                                    size=12,
                                    color=COLORS["cover_muted"],
                                ),
                            ],
                        ),
                    ],
                ),
                ft.Container(height=10),
                stats_text,
                ft.Container(height=2),
                running_text,
                ft.Container(height=10),
                ft.Button(
                    get_string("new_profile"),
                    icon=ft.Icons.ADD,
                    width=208,
                    height=42,
                    style=ACCENT_STYLE,
                    on_click=on_new_profile,
                ),
                ft.Container(height=8),
                ft.Row(
                    spacing=8,
                    controls=[
                        ft.OutlinedButton(
                            get_string("import"),
                            icon=ft.Icons.FILE_DOWNLOAD_OUTLINED,
                            expand=True,
                            height=36,
                            style=RAIL_STYLE,
                            on_click=on_import,
                        ),
                        ft.OutlinedButton(
                            get_string("export"),
                            icon=ft.Icons.FILE_UPLOAD_OUTLINED,
                            expand=True,
                            height=36,
                            style=RAIL_STYLE,
                            on_click=on_export,
                        ),
                    ],
                ),
                ft.Container(height=18),
                ft.Container(
                    padding=ft.Padding.only(left=10, bottom=4),
                    content=ft.Text(
                        get_string("filter_title"),
                        size=11.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["cover_muted"],
                    ),
                ),
                ft.Container(
                    expand=True,
                    content=filter_column or ft.Column(),
                ),
                log_column,
                ft.Container(height=8),
                ft.Row(
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=bottom_row_controls,
                ),
            ],
        ),
    )


def _language_switch(active: str, on_language: Callable[[str], None] | None) -> ft.Container:
    """Compact VI / EN switch pill."""
    next_code = "en" if active == "vi" else "vi"
    return ft.Container(
        height=34,
        padding=ft.Padding.symmetric(horizontal=8),
        border_radius=7,
        bgcolor=COLORS["cover_deep"],
        border=ft.Border.all(1, ft.Colors.with_opacity(0.25, COLORS["cover_muted"])),
        alignment=ft.Alignment(0, 0),
        ink=on_language is not None,
        tooltip="Tiếng Việt / English",
        on_click=None if on_language is None else (lambda _: on_language(next_code)),
        content=ft.Text(
            active.upper(),
            size=11.5,
            font_family=FONT_DATA,
            color=COLORS["cover_ink"],
        ),
    )
