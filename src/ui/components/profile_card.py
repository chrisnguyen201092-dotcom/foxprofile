import contextlib
import time
from collections.abc import Callable
from datetime import datetime

import flet as ft

from ...core.strings import get_string
from ...models.profile import (
    Profile,
    compute_days_since,
    format_date_dmy,
    resolve_status_display,
)
from ...services.avatar_store import get_avatar_path
from ...services.monitor import ProfileMonitorInfo
from ...utils.proxy_parser import mask_proxy
from ...utils.totp import generate_totp
from ..theme.colors import COLORS, FONT_DATA, FONT_MEDIUM, FONT_SEMIBOLD
from ..theme.styles import DANGER_ICON_STYLE, ICON_STYLE
from .launch_button import build_launch_button

_OS_LABELS = {"windows": "WIN", "macos": "MAC", "linux": "LIN"}
_LAST_AVATAR_CLICK: list[object] = ["", 0.0]

# One column grid for the header and every row: (expand, fixed width).
COLUMNS = {
    "select": (None, 32),
    # Give profile name ~40% of flexible space for long names, group badges, and notes.
    "name": (16, None),
    "device": (9, None),
    "proxy": (8, None),
    "geo": (7, None),
    # Launch button (100) + gap (4) + five compact icon buttons (5 * 28 = 140).
    "actions": (None, 248),
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
    on_open_mail_pool: Callable[[], None] | None = None,
    cooldown_chip_label: str = "",
    on_open_cooldown: Callable[[], None] | None = None,
    on_open_warmup_sites: Callable[[], None] | None = None,
    on_open_trust_monitor: Callable[[], None] | None = None,
    on_open_safe_schedule: Callable[[], None] | None = None,
    on_open_work_squad: Callable[[], None] | None = None,
    audit_alert: bool = False,
    mail_alert: bool = False,
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
        *,
        alert: bool = False,
    ) -> ft.Container:
        full_tip = f"{text}: {tooltip}" if text and text not in tooltip else tooltip
        chip_color = COLORS["warning"] if alert else COLORS["text_sub"]
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=5, vertical=3),
            border_radius=4,
            bgcolor=(
                ft.Colors.with_opacity(0.14, COLORS["warning"])
                if alert
                else COLORS["input_bg"]
            ),
            border=ft.Border.all(
                1,
                COLORS["warning"] if alert else COLORS["rule_strong"],
            ),
            ink=True,
            tooltip=full_tip,
            on_click=lambda _: on_click(),
            content=ft.Icon(
                icon,
                size=13,
                color=chip_color,
            ),
        )

    box = ft.Checkbox(
        value=all_selected,
        tooltip=get_string("deselect_page") if all_selected else get_string("select_page"),
        on_change=lambda e: on_select_all(not all_selected),
    )
    prof_chips: list[ft.Control] = [label("col_profile")]
    if on_open_warmup_sites is not None:
        prof_chips.append(
            _head_chip(
                ft.Icons.PUBLIC_ROUNDED,
                get_string("warmup_sites_btn"),
                get_string("warmup_sites_title"),
                on_open_warmup_sites,
            )
        )
    if on_open_safe_schedule is not None:
        prof_chips.append(
            _head_chip(
                ft.Icons.CALENDAR_MONTH_ROUNDED,
                get_string("schedule_btn"),
                get_string("tooltip_schedule_btn"),
                on_open_safe_schedule,
            )
        )
    if on_open_work_squad is not None:
        prof_chips.append(
            _head_chip(
                ft.Icons.GROUPS_ROUNDED,
                get_string("work_squad_btn"),
                get_string("tooltip_work_squad_btn"),
                on_open_work_squad,
            )
        )
    if on_open_proxy_pool is not None:
        prof_chips.append(
            _head_chip(
                ft.Icons.HUB_OUTLINED,
                get_string("proxy_pool_btn"),
                get_string("proxy_pool_title"),
                on_open_proxy_pool,
            )
        )
    if on_open_mail_pool is not None:
        prof_chips.append(
            _head_chip(
                ft.Icons.MARK_EMAIL_UNREAD_OUTLINED,
                get_string("mail_pool_btn"),
                get_string("mail_pool_title"),
                on_open_mail_pool,
                alert=mail_alert,
            )
        )
    if on_open_cooldown is not None and cooldown_chip_label:
        prof_chips.append(
            _head_chip(
                ft.Icons.TIMER_OUTLINED,
                cooldown_chip_label,
                get_string("cooldown_setting_title"),
                on_open_cooldown,
            )
        )
    if on_open_trust_monitor is not None:
        prof_chips.append(
            _head_chip(
                ft.Icons.VERIFIED_USER_OUTLINED,
                get_string("trust_monitor_btn"),
                get_string("trust_monitor_title"),
                on_open_trust_monitor,
                alert=audit_alert,
            )
        )
    profile_header: ft.Control = (
        ft.Row(
            spacing=5,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=prof_chips,
        )
        if len(prof_chips) > 1
        else label("col_profile")
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
                _cell("name", profile_header),
                _cell("device", label("col_device")),
                _cell("proxy", label("col_proxy")),
                _cell("geo", label("col_geo")),
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


def _format_running(mins: int | None, since: str = "") -> str:
    if mins is None and since and ":" in since:
        with contextlib.suppress(ValueError):
            hh, mm = (int(x) for x in since.split(":", 1))
            now = datetime.now()
            start_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            diff_m = int((now - start_dt).total_seconds() // 60)
            if diff_m < 0:
                diff_m += 1440
            mins = max(0, diff_m)
    if mins is None:
        return ""
    if mins < 1:
        return get_string("running_just_now")
    if mins < 60:
        return get_string("running_minutes", mins=mins)
    return get_string("running_hours", hours=mins // 60, mins=mins % 60)


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
    editing_email: bool = False,
    on_start_edit_email: Callable[[str], None] | None = None,
    on_save_email: Callable[[str, str], None] | None = None,
    editing_proxy: bool = False,
    on_start_edit_proxy: Callable[[str], None] | None = None,
    on_save_proxy: Callable[[str, str], None] | None = None,
    on_check_proxy: Callable[[str], None] | None = None,
    on_clone: Callable[[str], None] | None = None,
    on_focus_window: Callable[[str], None] | None = None,
    on_status_click: Callable[[str], None] | None = None,
    on_session_history: Callable[[str], None] | None = None,
    on_ip_history: Callable[[str], None] | None = None,
    on_trust_detail: Callable[[str], None] | None = None,
    on_toggle_pin: Callable[[str], None] | None = None,
    on_edit_dates: Callable[[str], None] | None = None,
    on_copy_email: Callable[[str], None] | None = None,
    on_copy_2fa: Callable[[str], None] | None = None,
    on_copy_password: Callable[[str], None] | None = None,
    on_copy_recovery: Callable[[str], None] | None = None,
    on_copy_token: Callable[[str], None] | None = None,
    on_fetch_mail_otp: Callable[[str], None] | None = None,
    on_open_mail_credentials: Callable[[str], None] | None = None,
    on_avatar_double_click: Callable[[str], None] | None = None,
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
    is_pinned = bool(getattr(profile, "pinned", False))
    has_badges = bool(is_pinned or profile.group or show_failed or status_disp is not None)
    primary_quick_edit = on_start_edit_email or on_start_edit_note
    quick_edit_tip_key = (
        "tooltip_quick_edit_email" if on_start_edit_email is not None else "tooltip_quick_edit_note"
    )
    name_tooltip = (
        f"{profile.name}\n({get_string(quick_edit_tip_key)})"
        if primary_quick_edit is not None
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
            on_click=lambda _, n=profile.name, fn=primary_quick_edit: fn(n),
            content=name_text,
        )
        if primary_quick_edit is not None
        else (
            ft.Container(expand=True, expand_loose=True, content=name_text)
            if has_badges
            else name_text
        )
    )
    top_line_controls: list[ft.Control] = []
    if is_pinned:
        top_line_controls.append(
            ft.Container(
                ink=on_toggle_pin is not None,
                border_radius=4,
                tooltip=get_string("tooltip_unpin"),
                on_click=(
                    (lambda _, n=profile.name: on_toggle_pin(n))
                    if on_toggle_pin is not None
                    else None
                ),
                content=ft.Icon(
                    ft.Icons.PUSH_PIN_ROUNDED,
                    size=13,
                    color=COLORS["accent"],
                ),
            )
        )
    top_line_controls.append(name_control)
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
        st_bg = ft.Colors.with_opacity(0.11, st_color)
        top_line_controls.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                border_radius=4,
                bgcolor=st_bg,
                border=ft.Border.all(1, ft.Colors.with_opacity(0.55, st_color)),
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

    email_val = (getattr(profile, "email", None) or "").strip()
    password_val = (getattr(profile, "password", None) or "").strip()
    two_fa_val = (getattr(profile, "two_fa", None) or "").strip()
    recovery_val = (getattr(profile, "recovery_email", None) or "").strip()
    token_val = (getattr(profile, "mail_token", None) or "").strip()
    totp_pair = generate_totp(two_fa_val) if two_fa_val else None
    if editing_email and on_save_email is not None:
        email_input = ft.TextField(
            value=email_val,
            hint_text=get_string("email_hint"),
            text_size=11.5,
            height=26,
            content_padding=ft.Padding.symmetric(horizontal=6, vertical=2),
            bgcolor=COLORS["input_bg"],
            border_color=COLORS["accent"],
            border_radius=4,
            autofocus=True,
            on_submit=lambda e, n=profile.name: on_save_email(n, e.control.value or ""),
            on_blur=lambda e, n=profile.name: on_save_email(n, e.control.value or ""),
        )
        text_block: ft.Control = _stack(top_line, email_input)
    elif editing_note and on_save_note is not None:
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
        text_block = _stack(top_line, note_input)
    elif (
        email_val
        or password_val
        or totp_pair is not None
        or recovery_val
        or token_val
        or profile.notes
        or on_open_mail_credentials is not None
    ):
        sub_parts: list[ft.Control] = []
        if email_val:
            email_tip = (
                get_string("tooltip_copy_email", email=email_val)
                if on_copy_email is not None
                else (
                    f"{email_val}\n({get_string('tooltip_quick_edit_email')})"
                    if on_start_edit_email
                    else email_val
                )
            )
            email_widget: ft.Control = ft.Text(
                email_val,
                size=11.5,
                font_family=FONT_DATA,
                color=COLORS["text_dim"],
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
                tooltip=email_tip,
            )
            email_click_fn = on_copy_email or on_start_edit_email
            if email_click_fn is not None:
                email_widget = ft.Container(
                    ink=True,
                    border_radius=3,
                    on_click=lambda _, n=profile.name, fn=email_click_fn: fn(n),
                    content=email_widget,
                )
            sub_parts.append(email_widget)
        if totp_pair is not None:
            otp_code, otp_rem = totp_pair
            totp_chip = ft.Container(
                padding=ft.Padding.symmetric(horizontal=5, vertical=0.5),
                border_radius=4,
                bgcolor=COLORS["input_bg"],
                border=ft.Border.all(1, COLORS["accent"]),
                ink=on_copy_2fa is not None,
                tooltip=get_string("tooltip_copy_2fa", code=otp_code, rem=otp_rem),
                on_click=(
                    (lambda _, n=profile.name: on_copy_2fa(n)) if on_copy_2fa is not None else None
                ),
                content=ft.Text(
                    f"2FA {otp_code}",
                    size=10,
                    font_family=FONT_DATA,
                    weight=ft.FontWeight.W_600,
                    color=COLORS["accent"],
                ),
            )
            sub_parts.append(totp_chip)
        if on_open_mail_credentials is not None:
            vault_chip = ft.Container(
                padding=ft.Padding.symmetric(horizontal=4, vertical=2),
                border_radius=4,
                bgcolor=COLORS["input_bg"],
                border=ft.Border.all(1, COLORS["rule_strong"]),
                ink=True,
                tooltip=get_string("tooltip_mail_credentials"),
                on_click=lambda _, n=profile.name: on_open_mail_credentials(n),
                content=ft.Icon(
                    ft.Icons.MAIL_LOCK_OUTLINED,
                    size=12,
                    color=COLORS["text_dim"],
                ),
            )
            sub_parts.append(vault_chip)
        if profile.notes:
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
            if sub_parts:
                sub_parts.append(ft.Text("·", size=11.5, color=COLORS["text_dim"]))
            sub_parts.append(note_widget)
        sub_line: ft.Control = (
            sub_parts[0]
            if len(sub_parts) == 1
            else ft.Row(
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=sub_parts,
            )
        )
        text_block = _stack(top_line, sub_line)
    else:
        text_block = top_line

    name_parts: list[ft.Control] = []
    dot_color = profile.color or (status_disp[1] if status_disp is not None else None)
    avatar_path = get_avatar_path(profile.name, 64)
    has_av_action = on_avatar_double_click is not None or on_status_click is not None
    av_tip = (
        get_string("tooltip_double_click_avatar")
        if on_avatar_double_click is not None
        else (get_string("tooltip_click_status") if on_status_click is not None else profile.name)
    )

    def _handle_avatar_click(e: ft.ControlEvent, n: str = profile.name) -> None:
        if on_avatar_double_click is not None:
            now = time.monotonic()
            prev_name = str(_LAST_AVATAR_CLICK[0])
            prev_ts = float(_LAST_AVATAR_CLICK[1])  # type: ignore[arg-type]
            if prev_name == n and (now - prev_ts) <= 0.48:
                _LAST_AVATAR_CLICK[1] = 0.0
                on_avatar_double_click(n)
                return
            _LAST_AVATAR_CLICK[0] = n
            _LAST_AVATAR_CLICK[1] = now
            ctrl = getattr(e, "control", None)
            if ctrl is not None:
                with contextlib.suppress(Exception):
                    ctrl.scale = 1.18 if getattr(ctrl, "scale", 1.0) <= 1.1 else 1.0
                    ctrl.rotate = 0.08 if getattr(ctrl, "rotate", 0.0) <= 0.0 else -0.07
                    ctrl.update()
            return
        if on_status_click is not None:
            on_status_click(n)

    if avatar_path:
        ring_color = (
            COLORS["success"]
            if is_running and not profile.color
            else (dot_color or COLORS["rule_strong"])
        )

        def _on_avatar_hover(e: ft.ControlEvent) -> None:
            ctrl = getattr(e, "control", None)
            if ctrl is None:
                return
            hovered = str(getattr(e, "data", "")).lower() == "true" or getattr(e, "data", None) is True
            with contextlib.suppress(Exception):
                ctrl.scale = 1.16 if hovered else 1.0
                ctrl.rotate = -0.07 if hovered else 0.0
                ctrl.update()

        name_parts.append(
            ft.Container(
                width=36,
                height=36,
                border_radius=18,
                padding=1.5,
                bgcolor=COLORS["input_bg"],
                border=ft.Border.all(2.0 if is_running else 1.5, ring_color),
                scale=1.0,
                rotate=0.0,
                animate_scale=ft.Animation(220, ft.AnimationCurve.EASE_OUT_BACK),
                animate_rotation=ft.Animation(220, ft.AnimationCurve.EASE_OUT_BACK),
                ink=has_av_action,
                tooltip=av_tip,
                on_hover=_on_avatar_hover,
                on_click=_handle_avatar_click if has_av_action else None,
                content=ft.Container(
                    width=33,
                    height=33,
                    border_radius=17,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    content=ft.Image(
                        src=avatar_path,
                        width=33,
                        height=33,
                        gapless_playback=True,
                    ),
                ),
            )
        )
    elif dot_color or has_av_action:
        name_parts.append(
            ft.Container(
                width=10,
                height=10,
                border_radius=5,
                bgcolor=dot_color,
                border=(None if dot_color else ft.Border.all(1.5, COLORS["rule_strong"])),
                ink=has_av_action,
                tooltip=av_tip,
                on_click=_handle_avatar_click if has_av_action else None,
            )
        )
    if name_parts:
        name_parts.append(ft.Container(expand=True, content=text_block))
        name_cell_content: ft.Control = ft.Row(
            spacing=8 if avatar_path else 6,
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
                _cell(
                    "device",
                    _drag_wrap(
                        _device(
                            profile,
                            device,
                            on_edit_dates=on_edit_dates,
                            monitor_info=monitor_info,
                        )
                    ),
                ),
                _cell(
                    "proxy",
                    _drag_wrap(
                        _proxy(
                            profile,
                            editing_proxy=editing_proxy,
                            on_start_edit_proxy=on_start_edit_proxy,
                            on_save_proxy=on_save_proxy,
                            on_check_proxy=on_check_proxy,
                            on_ip_history=on_ip_history,
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
                            on_trust_detail=on_trust_detail,
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
                                is_pinned=is_pinned,
                                on_toggle_pin=on_toggle_pin,
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


def _device(
    profile: Profile,
    device: str,
    *,
    on_edit_dates: Callable[[str], None] | None = None,
    monitor_info: ProfileMonitorInfo | None = None,
) -> ft.Row:
    chrome = profile.engine == "chrome"
    engine_name = "Chrome" if chrome else "Camoufox"
    raw_created = getattr(profile, "created_at", None)
    cal_days = compute_days_since(raw_created)
    if monitor_info is not None and raw_created is not None:
        age_days: int | None = monitor_info.age_days
    else:
        age_days = cal_days
    ban_days = compute_days_since(getattr(profile, "banned_at", None))

    if age_days is not None or ban_days is not None:
        c_date_str = format_date_dmy(profile.created_at) or "—"
        age_str = get_string("age_days_badge", days=age_days if age_days is not None else 0)
        if ban_days is not None:
            b_date_str = format_date_dmy(profile.banned_at) or "—"
            ban_str = get_string("ban_days_badge", days=ban_days)
            label_str = f"{age_str} · {ban_str}"
            tip = get_string(
                "tooltip_banned_date",
                c_date=c_date_str,
                b_date=b_date_str,
                days=ban_days,
            )
        else:
            label_str = age_str
            tip = get_string("tooltip_created_date", date=c_date_str, days=age_days)

        line1: ft.Control = ft.Container(
            on_click=(
                (lambda _, n=profile.name: on_edit_dates(n)) if on_edit_dates is not None else None
            ),
            tooltip=tip,
            content=ft.Text(
                label_str,
                size=11.5,
                font_family=FONT_DATA,
                color=COLORS["text_sub"],
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
        )
    else:
        line1 = ft.Text(
            engine_name,
            size=12.5,
            font_family=FONT_SEMIBOLD if chrome else None,
            color=COLORS["sidebar"] if chrome else COLORS["text_main"],
            no_wrap=True,
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
                tooltip=f"{profile.os_type} · {engine_name}",
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
                    line1,
                    _minor(device, data=True)
                    if device
                    else _minor(get_string("no_fingerprint_short")),
                ),
            ),
        ],
    )


def _shorten_ip_label(ip: str, country: str = "", ip_type: str = "") -> str:
    clean_ip = (ip or "").strip()
    if ":" in clean_ip and len(clean_ip) > 18:
        segs = clean_ip.split(":")
        if len(segs) >= 4:
            clean_ip = f"{segs[0]}:..:{segs[-1]}"
    c_str = (country or "").strip().upper()
    t_str = (ip_type or "").strip().upper()
    tag = "·".join(x for x in (c_str, t_str) if x)
    return f"{clean_ip} ({tag})" if tag else clean_ip


def _proxy(
    profile: Profile,
    *,
    editing_proxy: bool = False,
    on_start_edit_proxy: Callable[[str], None] | None = None,
    on_save_proxy: Callable[[str, str], None] | None = None,
    on_check_proxy: Callable[[str], None] | None = None,
    on_ip_history: Callable[[str], None] | None = None,
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
    elif monitor_info is not None and getattr(monitor_info, "cluster_busy_by", None):
        sub_label = get_string(
            "proxy_cluster_busy_by",
            cluster=getattr(monitor_info, "cluster_collision_label", "")
            or getattr(monitor_info, "cluster", "")
            or "/64",
            other=monitor_info.cluster_busy_by[0],
        )
        sub_color = COLORS["warning"]
    elif monitor_info is not None and monitor_info.proxy_port_changed:
        sub_label = get_string("proxy_changed_badge", old_port=monitor_info.last_used_port)
        sub_color = COLORS["error"]
    elif monitor_info is not None and monitor_info.ip_country_changed:
        sub_label = get_string(
            "proxy_country_changed_badge",
            old=monitor_info.prev_exit_country,
            new=monitor_info.last_exit_country,
        )
        sub_color = COLORS["warning"]
    elif proxy_check_state is not None:
        _, st_msg = proxy_check_state
        sub_label = st_msg
        sub_color = COLORS["success"]
    elif monitor_info is not None and getattr(monitor_info, "can_open_parallel", False):
        if monitor_info.last_exit_ip:
            short_ip = _shorten_ip_label(
                monitor_info.last_exit_ip,
                monitor_info.last_exit_country,
                getattr(monitor_info, "last_exit_ip_type", ""),
            )
            sub_label = f"{get_string('proxy_parallel_ok')} · {short_ip}"
        else:
            sub_label = get_string("proxy_parallel_ok")
        sub_color = COLORS["success"]
    elif monitor_info is not None and monitor_info.last_exit_ip:
        short_ip = _shorten_ip_label(
            monitor_info.last_exit_ip,
            monitor_info.last_exit_country,
            getattr(monitor_info, "last_exit_ip_type", ""),
        )
        sub_label = f"{scheme.upper()} · {short_ip}"
        sub_color = COLORS["text_sub"]

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
    isp_detail = getattr(monitor_info, "last_exit_isp", "") if monitor_info is not None else ""
    base_tip = f"{sub_label} · {isp_detail}" if isp_detail else sub_label
    line2_text = ft.Text(
        sub_label,
        size=11.5,
        font_family=FONT_DATA,
        color=sub_color,
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
        tooltip=(
            f"{base_tip} ({get_string('tooltip_ip_history')})"
            if on_ip_history is not None
            else base_tip
        ),
    )
    line2: ft.Control = (
        ft.Container(
            ink=True,
            border_radius=4,
            on_click=lambda _, n=profile.name: on_ip_history(n),
            content=line2_text,
        )
        if on_ip_history is not None
        else line2_text
    )
    return _stack(line1, line2)


def _geo_score_badge(
    monitor_info: ProfileMonitorInfo | None,
    *,
    on_click: Callable[[], None] | None = None,
) -> ft.Control | None:
    if monitor_info is None:
        return None
    t_score = int(getattr(monitor_info, "trust_score", 0) or 0)
    t_tier = str(getattr(monitor_info, "trust_tier", "cold") or "cold")
    is_banned = bool(getattr(monitor_info, "is_banned", False) or t_tier == "banned")
    sess_days = getattr(monitor_info, "sess_exp_days", None)
    audit_codes = list(getattr(monitor_info, "audit_codes", ()) or ())
    tainted_codes = [
        c
        for c in audit_codes
        if c in ("burned_proxy", "burned_ip", "burned_subnet", "burned_batch")
    ]
    if t_score <= 0 and not is_banned and not tainted_codes and sess_days is None:
        return None

    datr_d = float(getattr(monitor_info, "datr_age_days", 0.0) or 0.0)
    f_score = getattr(monitor_info, "ip_fraud_score", None)
    ip_part = f" · IP Risk {f_score}/100" if f_score is not None and f_score >= 0 else ""

    days_val = (
        max(0, int(sess_days))
        if sess_days is not None
        else max(0, int(round(datr_d)))
    )
    text_val = f"{days_val}d"

    # 3-stage expiry warning colors: <= 3d (Red urgent), <= 7d (1 week Orange), <= 14d (2 weeks Amber)
    fg_col = COLORS["text_sub"]
    bg_col = COLORS["input_bg"]
    bd_col = COLORS["rule_strong"]
    bd_width: float = 1.0
    if not is_banned and sess_days is not None:
        if sess_days <= 3:
            fg_col = COLORS["error"]
            bg_col = ft.Colors.with_opacity(0.16, COLORS["error"])
            bd_col = COLORS["error"]
            bd_width = 1.5
        elif sess_days <= 7:
            fg_col = "#EA580C"
            bg_col = ft.Colors.with_opacity(0.14, "#EA580C")
            bd_col = "#EA580C"
            bd_width = 1.2
        elif sess_days <= 14:
            fg_col = "#B45309"
            bg_col = ft.Colors.with_opacity(0.12, "#D97706")
            bd_col = "#D97706"
            bd_width = 1.0

    if is_banned:
        tip = f"Tài khoản đã bị khóa / Disconnected{ip_part}"
    elif tainted_codes:
        reasons: list[str] = []
        if "burned_proxy" in tainted_codes:
            reasons.append("Trùng cổng Proxy từng có acc Ban")
        if "burned_ip" in tainted_codes:
            reasons.append("Trùng IPv6 128-bit lịch sử với acc Ban")
        if "burned_batch" in tainted_codes:
            reasons.append("Chung lô phôi @tuan89.site (đợt 63..121)")
        if "burned_subnet" in tainted_codes and not reasons:
            reasons.append("Chung dải mạng /64 với acc Ban")
        reason_str = " + ".join(reasons)
        sess_tip = f" · Phiên Muse còn {sess_days}d" if (sess_days is not None and sess_days >= 0) else ""
        tip = f"Liên đới lịch sử ({reason_str}) — Cách ly, không gộp chung đội mở song song{sess_tip}{ip_part}"
    elif sess_days is not None:
        if sess_days < 0:
            tip = f"🚨 Phiên Muse (hatch_sess) đã hết hạn — Cần đăng nhập lại{ip_part}"
        elif sess_days <= 3:
            tip = (
                f"🚨 KHẨN CẤP (≤ 3 ngày): Phiên Muse chỉ còn {sess_days} ngày — "
                f"Hãy mở muse.ai NGAY để trượt hạn +30 ngày (tránh phải login lại Meta)! · datr {datr_d:.1f}d{ip_part}"
            )
        elif sess_days <= 7:
            tip = (
                f"⚠️ CẢNH BÁO 1 TUẦN (≤ 7 ngày): Phiên Muse còn {sess_days} ngày — "
                f"Hãy mở muse.ai sớm để tự động trượt hạn thêm +30 ngày · datr {datr_d:.1f}d{ip_part}"
            )
        elif sess_days <= 14:
            tip = (
                f"⏳ NHẮC NHỞ 2 TUẦN (≤ 14 ngày): Phiên Muse còn {sess_days} ngày — "
                f"Nên xếp lịch mở muse.ai để trượt hạn lại +30 ngày · datr {datr_d:.1f}d{ip_part}"
            )
        else:
            tip = (
                f"Sạch (Độc lập IP & Phôi) · Hạn phiên Muse (hatch_sess): còn {sess_days} ngày "
                f"· datr {datr_d:.1f}d{ip_part}"
            )
    else:
        tip = f"Sạch (Độc lập IP & Phôi) · datr {datr_d:.1f}d{ip_part}"

    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=5, vertical=1),
        border_radius=4,
        bgcolor=bg_col,
        border=ft.Border.all(bd_width, bd_col),
        ink=on_click is not None,
        on_click=(lambda _: on_click()) if on_click is not None else None,
        tooltip=tip,
        content=ft.Text(
            text_val,
            size=10,
            font_family=FONT_DATA,
            weight=ft.FontWeight.W_700 if (sess_days is not None and sess_days <= 14) else ft.FontWeight.W_600,
            color=fg_col,
            no_wrap=True,
        ),
    )


def _geo(
    profile: Profile,
    monitor_info: ProfileMonitorInfo | None = None,
    *,
    is_running: bool = False,
    since: str = "",
    on_focus_window: Callable[[str], None] | None = None,
    on_session_history: Callable[[str], None] | None = None,
    on_trust_detail: Callable[[str], None] | None = None,
) -> ft.Column:
    follow = get_string("follows_ip")
    tz: ft.Control = (
        _data(profile.timezone)
        if profile.timezone
        else ft.Text(follow, size=12.5, color=COLORS["text_dim"])
    )
    trust_cb = on_trust_detail or on_session_history
    score_chip = _geo_score_badge(
        monitor_info,
        on_click=(lambda n=profile.name: trust_cb(n)) if trust_cb is not None else None,
    )
    if is_running:
        run_mins = monitor_info.running_mins if monitor_info is not None else None
        dur = _format_running(run_mins, since)
        head = f"● {get_string('stamp_live')} {dur}" if dur else f"● {get_string('stamp_live')}"
        run_label = f"{head} · {since}" if since else head
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
            focus_ctrl = ft.Container(
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
            )
            return _stack(
                tz,
                ft.Row(
                    spacing=5,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[score_chip, focus_ctrl],
                )
                if score_chip is not None
                else focus_ctrl,
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
        cd_ctrl: ft.Control = (
            ft.Container(
                ink=True,
                border_radius=4,
                on_click=lambda _, n=profile.name: on_session_history(n),
                content=cd_text,
            )
            if on_session_history is not None
            else cd_text
        )
        line2_ctrl: ft.Control = (
            ft.Row(
                spacing=5,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[score_chip, cd_ctrl],
            )
            if score_chip is not None
            else cd_ctrl
        )
        return _stack(tz, line2_ctrl)
    locale = _minor(profile.locale, data=True) if profile.locale else _minor(follow)
    loc_ctrl: ft.Control = (
        ft.Container(
            ink=True,
            border_radius=4,
            tooltip=get_string("session_hist_btn"),
            on_click=lambda _, n=profile.name: on_session_history(n),
            content=locale,
        )
        if on_session_history is not None
        else locale
    )
    idle_line2: ft.Control = (
        ft.Row(
            spacing=5,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[score_chip, loc_ctrl],
        )
        if score_chip is not None
        else loc_ctrl
    )
    return _stack(tz, idle_line2)


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
    *,
    is_pinned: bool = False,
    on_toggle_pin: Callable[[str], None] | None = None,
) -> list[ft.IconButton]:
    buttons: list[ft.IconButton] = []
    if on_toggle_pin is not None:
        buttons.append(
            ft.IconButton(
                icon=ft.Icons.PUSH_PIN_ROUNDED if is_pinned else ft.Icons.PUSH_PIN_OUTLINED,
                icon_size=16,
                width=28,
                height=28,
                padding=0,
                icon_color=COLORS["accent"] if is_pinned else COLORS["text_sub"],
                tooltip=get_string("tooltip_unpin") if is_pinned else get_string("tooltip_pin"),
                style=ICON_STYLE,
                on_click=lambda _, n=name: on_toggle_pin(n),
            ),
        )
    if on_cookies:
        buttons.append(
            ft.IconButton(
                icon=ft.Icons.COOKIE_OUTLINED,
                icon_size=17,
                width=28,
                height=28,
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
                width=28,
                height=28,
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
            width=28,
            height=28,
            padding=0,
            icon_color=COLORS["text_sub"],
            tooltip=get_string("tooltip_edit"),
            style=ICON_STYLE,
            on_click=lambda _, n=name: on_edit(n),
        ),
        ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            icon_size=17,
            width=28,
            height=28,
            padding=0,
            icon_color=COLORS["rule_strong"] if is_running else COLORS["text_sub"],
            tooltip=get_string("tooltip_delete"),
            disabled=is_running,
            style=DANGER_ICON_STYLE,
            on_click=lambda _, n=name: on_delete(n),
        ),
    ]
