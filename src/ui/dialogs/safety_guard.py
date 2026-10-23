from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...services.monitor import PreLaunchWarning
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, ERROR_STYLE, OUTLINE_STYLE

_COOLDOWN_PRESETS = (0, 5, 10, 15, 20, 30, 60)


def _warning_card(w: PreLaunchWarning) -> ft.Container:
    is_crit = w.level == "critical"
    col = COLORS["error"] if is_crit else COLORS["warning"]
    icon = ft.Icons.GPP_BAD_ROUNDED if is_crit else ft.Icons.WARNING_AMBER_ROUNDED
    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=12, vertical=9),
        border_radius=8,
        bgcolor=COLORS["input_bg"],
        border=ft.Border.all(1, col),
        content=ft.Row(
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[
                ft.Icon(icon, size=18, color=col),
                ft.Text(
                    w.message,
                    size=13,
                    color=COLORS["text_main"],
                    expand=True,
                ),
            ],
        ),
    )


def open_prelaunch_guard_dialog(
    page: ft.Page,
    profile_name: str,
    warnings: list[PreLaunchWarning],
    on_force_launch: Callable[[], None],
) -> None:
    """Modal safety interlock shown before launching a profile with detected risks."""

    def _force(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        on_force_launch()

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(ft.Icons.SHIELD_OUTLINED, size=22, color=COLORS["error"]),
                ft.Text(
                    get_string("guard_dialog_title", name=profile_name),
                    size=17,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                    expand=True,
                ),
            ],
        ),
        content=ft.Container(
            width=460,
            content=ft.Column(
                spacing=8,
                tight=True,
                controls=[
                    ft.Text(
                        get_string("guard_dialog_subtitle"),
                        size=12.5,
                        color=COLORS["text_sub"],
                    ),
                    *[_warning_card(w) for w in warnings],
                ],
            ),
        ),
        actions=[
            ft.Button(
                get_string("guard_cancel_safe"),
                icon=ft.Icons.SHIELD,
                style=ACCENT_STYLE,
                on_click=lambda _: page.pop_dialog(),
            ),
            ft.Button(
                get_string("guard_force_launch"),
                icon=ft.Icons.WARNING_AMBER_ROUNDED,
                style=ERROR_STYLE,
                on_click=_force,
            ),
        ],
    )
    page.show_dialog(dlg)


def open_bulk_prelaunch_guard_dialog(
    page: ft.Page,
    safe_names: list[str],
    issues: dict[str, list[PreLaunchWarning]],
    on_launch_safe_only: Callable[[], None],
    on_force_launch_all: Callable[[], None],
) -> None:
    """Modal safety interlock shown before bulk-launching when 1+ profiles have warnings."""
    total_count = len(safe_names) + len(issues)
    rows: list[ft.Control] = []
    for pname, wlist in issues.items():
        has_crit = any(w.level == "critical" for w in wlist)
        col = COLORS["error"] if has_crit else COLORS["warning"]
        rows.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                border_radius=7,
                bgcolor=COLORS["input_bg"],
                border=ft.Border.all(1, col),
                content=ft.Column(
                    spacing=3,
                    tight=True,
                    controls=[
                        ft.Text(
                            pname,
                            size=13,
                            font_family=FONT_SEMIBOLD,
                            color=col,
                        ),
                        *[
                            ft.Text(
                                f"• {w.message}",
                                size=12,
                                color=COLORS["text_main"],
                            )
                            for w in wlist
                        ],
                    ],
                ),
            )
        )

    def _safe_only(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        on_launch_safe_only()

    def _force_all(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        on_force_launch_all()

    actions: list[ft.Control] = [
        ft.TextButton(get_string("cancel"), on_click=lambda _: page.pop_dialog()),
    ]
    if safe_names:
        actions.append(
            ft.Button(
                get_string("guard_bulk_launch_safe", count=len(safe_names)),
                icon=ft.Icons.SHIELD,
                style=ACCENT_STYLE,
                on_click=_safe_only,
            )
        )
    actions.append(
        ft.Button(
            get_string("guard_bulk_force_all", count=total_count),
            icon=ft.Icons.WARNING_AMBER_ROUNDED,
            style=ERROR_STYLE,
            on_click=_force_all,
        )
    )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(ft.Icons.SHIELD_OUTLINED, size=22, color=COLORS["error"]),
                ft.Text(
                    get_string("guard_bulk_title", warn=len(issues), total=total_count),
                    size=17,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                    expand=True,
                ),
            ],
        ),
        content=ft.Container(
            width=480,
            content=ft.Column(
                spacing=8,
                tight=True,
                controls=[
                    ft.Text(
                        get_string("guard_bulk_subtitle"),
                        size=12.5,
                        color=COLORS["text_sub"],
                    ),
                    ft.Column(
                        spacing=6,
                        height=min(280, max(90, len(rows) * 68)),
                        scroll=ft.ScrollMode.AUTO,
                        controls=rows,
                    ),
                ],
            ),
        ),
        actions=actions,
    )
    page.show_dialog(dlg)


def open_cooldown_settings_dialog(
    page: ft.Page,
    current_minutes: int,
    on_save: Callable[[int], None],
) -> None:
    """Dialog to customize the proxy port cooldown duration (in minutes)."""
    tf = ft.TextField(
        value=str(current_minutes),
        keyboard_type=ft.KeyboardType.NUMBER,
        text_size=13.5,
        **DLG_FIELD_KWARGS,
    )
    preset_row = ft.Row(spacing=6, wrap=True)

    def _select_preset(mins: int) -> None:
        tf.value = str(mins)
        _rebuild_presets(mins)
        page.update()

    def _rebuild_presets(active_val: int) -> None:
        chips: list[ft.Control] = []
        for p in _COOLDOWN_PRESETS:
            is_on = p == active_val
            label = get_string("cooldown_preset_off") if p == 0 else f"{p}p"
            chips.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                    border_radius=6,
                    bgcolor=COLORS["sidebar"] if is_on else COLORS["input_bg"],
                    border=ft.Border.all(
                        1,
                        COLORS["sidebar"] if is_on else COLORS["rule_strong"],
                    ),
                    ink=True,
                    on_click=lambda _, m=p: _select_preset(m),
                    content=ft.Text(
                        label,
                        size=12,
                        font_family=FONT_DATA,
                        weight=ft.FontWeight.W_600 if is_on else ft.FontWeight.W_400,
                        color=COLORS["cover_ink"] if is_on else COLORS["text_main"],
                    ),
                )
            )
        preset_row.controls = chips

    _rebuild_presets(current_minutes)

    def _save(_: ft.ControlEvent) -> None:
        raw = (tf.value or "").strip()
        try:
            mins = max(0, min(1440, int(raw)))
        except ValueError:
            tf.error = get_string("cooldown_setting_invalid")
            page.update()
            return
        page.pop_dialog()
        on_save(mins)

    tf.on_submit = _save

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Text(
            get_string("cooldown_setting_title"),
            size=17,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=400,
            content=ft.Column(
                spacing=10,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Text(
                        get_string("cooldown_setting_desc"),
                        size=12.5,
                        color=COLORS["text_sub"],
                    ),
                    preset_row,
                    ft.Text(
                        get_string("cooldown_setting_input_label"),
                        size=12.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["text_sub"],
                    ),
                    tf,
                ],
            ),
        ),
        actions=[
            ft.OutlinedButton(
                get_string("cancel"),
                style=OUTLINE_STYLE,
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


def open_token_burst_alert_dialog(
    page: ft.Page,
    bursts: list[tuple[str, int]],
    on_focus_window: Callable[[str], None] | None = None,
) -> None:
    """Modal warning dialog shown on FoxProfile when a running profile exceeds token refresh burst threshold."""
    rows: list[ft.Control] = []
    for pname, count in bursts:
        row_ctrls: list[ft.Control] = [
            ft.Icon(ft.Icons.BOLT, size=18, color=COLORS["warning"]),
            ft.Text(
                get_string("token_burst_dialog_item", name=pname, count=count),
                size=13,
                font_family=FONT_SEMIBOLD,
                color=COLORS["text_main"],
                expand=True,
            ),
        ]
        if on_focus_window is not None:
            row_ctrls.append(
                ft.OutlinedButton(
                    get_string("token_burst_focus_btn"),
                    icon=ft.Icons.OPEN_IN_NEW,
                    height=30,
                    style=OUTLINE_STYLE,
                    on_click=lambda _, n=pname: on_focus_window(n),
                )
            )
        rows.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                border_radius=8,
                bgcolor=COLORS["input_bg"],
                border=ft.Border.all(1, COLORS["warning"]),
                content=ft.Row(
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=row_ctrls,
                ),
            )
        )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=22, color=COLORS["warning"]),
                ft.Text(
                    get_string("token_burst_dialog_title"),
                    size=17,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                    expand=True,
                ),
            ],
        ),
        content=ft.Container(
            width=460,
            content=ft.Column(
                spacing=10,
                tight=True,
                controls=[
                    ft.Text(
                        get_string("token_burst_dialog_desc"),
                        size=12.5,
                        color=COLORS["text_sub"],
                    ),
                    *rows,
                ],
            ),
        ),
        actions=[
            ft.Button(
                get_string("token_burst_dismiss_btn"),
                icon=ft.Icons.CHECK,
                style=ACCENT_STYLE,
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    page.show_dialog(dlg)
