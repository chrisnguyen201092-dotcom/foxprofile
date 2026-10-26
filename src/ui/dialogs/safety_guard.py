from collections.abc import Callable

import flet as ft

from ...core.strings import current_language, get_string
from ...models.profile import Profile
from ...services.monitor import MonitorSnapshot, PreLaunchWarning
from ...services.scheduler import (
    ScheduledAccount,
    ScheduledDay,
    ScheduledPair,
    SchedulePlan,
    SquadCandidate,
    WorkSquadPlan,
    build_safe_schedule,
    format_schedule_markdown,
    is_gateway_ipv6_cluster,
    select_parallel_work_squad,
)
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, ERROR_STYLE, OUTLINE_STYLE

_COOLDOWN_PRESETS = (0, 5, 10, 15, 20, 30, 60)
_CYCLE_PRESETS = (4, 5, 6)


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


def _build_quick_launch_logo(
    profile_name: str,
    *,
    is_opened: bool = False,
    on_open_one: Callable[[str], None] | None = None,
) -> ft.Container:
    """Minimalist flat outlined icon button for quick profile launch."""
    if is_opened:
        return ft.Container(
            width=26,
            height=26,
            border_radius=6,
            bgcolor=ft.Colors.with_opacity(0.10, COLORS["success"]),
            border=ft.Border.all(1, COLORS["success"]),
            alignment=ft.Alignment(0, 0),
            tooltip=get_string("schedule_row_opened_tooltip", name=profile_name),
            content=ft.Icon(
                ft.Icons.CHECK_ROUNDED,
                size=14,
                color=COLORS["success"],
            ),
        )

    return ft.Container(
        width=26,
        height=26,
        border_radius=6,
        bgcolor=COLORS["card_bg"],
        border=ft.Border.all(1, COLORS["rule_strong"]),
        alignment=ft.Alignment(0, 0),
        ink=True,
        tooltip=get_string("schedule_row_open_tooltip", name=profile_name),
        on_click=lambda _, n=profile_name: on_open_one(n) if on_open_one else None,
        content=ft.Icon(
            ft.Icons.PLAY_ARROW_ROUNDED,
            size=15,
            color=COLORS["text_sub"],
        ),
    )


def _build_account_pill(
    acc: ScheduledAccount,
    day_idx: int,
    *,
    is_opened: bool = False,
    on_open_one: Callable[[str], None] | None = None,
) -> ft.Container:
    is_gw = is_gateway_ipv6_cluster(acc.cluster)
    cluster_col = "#0284C7" if is_gw else "#059669"
    eff_rest = int(round(acc.rest_hours + day_idx * 24))
    now_rest = int(round(acc.rest_hours))
    display_name = (
        acc.name
        if acc.name == acc.email or not acc.email
        else f"{acc.name} · {acc.email}"
    )

    badges: list[ft.Control] = [
        ft.Container(
            padding=ft.Padding.symmetric(horizontal=6, vertical=2),
            border_radius=4,
            bgcolor=cluster_col,
            content=ft.Text(
                f"{acc.cluster}{acc.port}",
                size=10.5,
                font_family=FONT_DATA,
                color="#FFFFFF",
            ),
        ),
        ft.Container(
            padding=ft.Padding.symmetric(horizontal=6, vertical=2),
            border_radius=4,
            bgcolor=COLORS["card_bg"],
            border=ft.Border.all(
                1,
                "#059669" if acc.completed_today else COLORS["rule_strong"],
            ),
            content=ft.Text(
                get_string("schedule_done_today_badge")
                if acc.completed_today
                else get_string("schedule_rest_badge", now=now_rest, eff=eff_rest),
                size=10.5,
                font_family=FONT_SEMIBOLD if acc.completed_today else FONT_DATA,
                color="#059669" if acc.completed_today else COLORS["text_sub"],
            ),
        ),
    ]
    if acc.is_probation:
        badges.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=4,
                bgcolor=COLORS["card_bg"],
                border=ft.Border.all(1, "#0284C7"),
                content=ft.Text(
                    get_string("schedule_probation_badge"),
                    size=10.0,
                    font_family=FONT_SEMIBOLD,
                    color="#0284C7",
                ),
            )
        )
    if acc.shared_endpoint:
        badges.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=4,
                bgcolor=COLORS["card_bg"],
                border=ft.Border.all(1, COLORS["warning"]),
                content=ft.Text(
                    get_string("schedule_shared_port_badge", port=acc.port),
                    size=10.0,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["warning"],
                ),
            )
        )

    info_col = ft.Column(
        spacing=4,
        tight=True,
        expand=True,
        controls=[
            ft.Text(
                display_name,
                size=12.5,
                font_family=FONT_SEMIBOLD,
                color=COLORS["text_main"],
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
            ft.Row(spacing=5, wrap=True, controls=badges),
        ],
    )

    row_controls: list[ft.Control] = [info_col]
    if is_opened or on_open_one is not None:
        row_controls.append(
            _build_quick_launch_logo(
                acc.name,
                is_opened=is_opened,
                on_open_one=on_open_one,
            )
        )

    return ft.Container(
        expand=True,
        padding=ft.Padding.symmetric(horizontal=10, vertical=7),
        border_radius=7,
        bgcolor=COLORS["input_bg"],
        border=ft.Border.all(
            1,
            ft.Colors.with_opacity(0.65, COLORS["success"])
            if is_opened
            else COLORS["card_border"],
        ),
        content=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=row_controls,
        ),
    )


def _build_pair_row(
    pr: ScheduledPair,
    day_idx: int,
    *,
    opened_names: set[str] | None = None,
    on_open_one: Callable[[str], None] | None = None,
) -> ft.Container:
    active_set = opened_names or set()
    second_ctrl: ft.Control
    if pr.second is not None:
        second_ctrl = _build_account_pill(
            pr.second,
            day_idx,
            is_opened=(pr.second.name in active_set),
            on_open_one=on_open_one,
        )
    else:
        second_ctrl = ft.Container(
            expand=True,
            padding=ft.Padding.symmetric(horizontal=10, vertical=7),
            border_radius=7,
            bgcolor=COLORS["input_bg"],
            border=ft.Border.all(1, COLORS["card_border"]),
            content=ft.Text(
                get_string("schedule_solo_badge"),
                size=12,
                color=COLORS["text_sub"],
            ),
        )

    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=4, vertical=2),
        content=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    width=62,
                    padding=ft.Padding.symmetric(horizontal=6, vertical=5),
                    border_radius=6,
                    bgcolor=COLORS["sidebar"],
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("schedule_pair_badge", idx=pr.pair_index),
                        size=11.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["cover_ink"],
                    ),
                ),
                _build_account_pill(
                    pr.first,
                    day_idx,
                    is_opened=(pr.first.name in active_set),
                    on_open_one=on_open_one,
                ),
                ft.Icon(ft.Icons.SYNC_ALT_ROUNDED, size=16, color=COLORS["text_sub"]),
                second_ctrl,
            ],
        ),
    )


def _build_day_card(
    day: ScheduledDay,
    lang: str,
    *,
    opened_names: set[str] | None = None,
    on_open_one: Callable[[str], None] | None = None,
) -> ft.Container:
    day_name = day.weekday_name_vi if lang == "vi" else day.weekday_name_en
    sections: list[ft.Control] = [
        ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=9, vertical=3),
                    border_radius=6,
                    bgcolor=COLORS["sidebar"],
                    content=ft.Text(
                        day.weekday_tag,
                        size=12,
                        font_family=FONT_BOLD,
                        color=COLORS["cover_ink"],
                    ),
                ),
                ft.Text(
                    get_string(
                        "schedule_day_header",
                        idx=day.day_index + 1,
                        day_name=day_name,
                        tag=day.weekday_tag,
                        date=day.date_str,
                        count=day.total_accounts,
                    ),
                    size=13.5,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                    expand=True,
                ),
            ],
        ),
    ]

    if day.morning_pairs:
        sections.append(
            ft.Text(
                get_string("schedule_morning_title", count=len(day.morning_pairs)),
                size=12,
                font_family=FONT_SEMIBOLD,
                color=COLORS["accent"],
            )
        )
        for pr in day.morning_pairs:
            sections.append(
                _build_pair_row(
                    pr,
                    day.day_index,
                    opened_names=opened_names,
                    on_open_one=on_open_one,
                )
            )

    if day.evening_pairs:
        sections.append(
            ft.Text(
                get_string("schedule_evening_title", count=len(day.evening_pairs)),
                size=12,
                font_family=FONT_SEMIBOLD,
                color="#0284C7",
            )
        )
        for pr in day.evening_pairs:
            sections.append(
                _build_pair_row(
                    pr,
                    day.day_index,
                    opened_names=opened_names,
                    on_open_one=on_open_one,
                )
            )

    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=14, vertical=12),
        border_radius=10,
        bgcolor=COLORS["card_bg"],
        border=ft.Border.all(1, COLORS["rule_strong"]),
        content=ft.Column(spacing=6, tight=True, controls=sections),
    )


def open_safe_schedule_dialog(
    page: ft.Page,
    profiles: list[Profile],
    snap: MonitorSnapshot,
    *,
    initial_cycle_days: int = 5,
    last_pairings: list[list[str]] | None = None,
    last_shifts: dict[str, str] | None = None,
    running_names: set[str] | None = None,
    on_launch_single: Callable[[str], None] | None = None,
    on_apply_schedule: Callable[[SchedulePlan], None],
    on_copy_schedule: Callable[[str], None] | None = None,
    on_clear_schedule: Callable[[], None] | None = None,
) -> None:
    """Modal dialog displaying the multi-day paired safe farming schedule & tag applicator."""
    lang = current_language()
    launched_in_dialog: set[str] = set(running_names or set())
    state = {
        "cycle_days": max(4, min(6, int(initial_cycle_days or 5))),
        "seed_offset": 0,
        "selected_day": -1,  # -1 = all days
        "plan": None,
    }

    cycle_row = ft.Row(spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    day_filter_row = ft.Row(spacing=6, wrap=True)
    days_list_col = ft.Column(spacing=10, scroll=ft.ScrollMode.AUTO, height=440)

    def _recompute() -> SchedulePlan:
        base_seed = 20261024 + state["seed_offset"] * 97
        plan = build_safe_schedule(
            profiles,
            snap,
            cycle_days=state["cycle_days"],
            last_pairings=last_pairings,
            last_shifts=last_shifts,
            shuffle_seed=base_seed if state["seed_offset"] > 0 else None,
        )
        state["plan"] = plan
        return plan

    def _set_cycle(days_val: int) -> None:
        state["cycle_days"] = days_val
        state["selected_day"] = -1
        _refresh_view()
        page.update()

    def _set_day_filter(d_idx: int) -> None:
        state["selected_day"] = d_idx
        _refresh_view(recompute=False)
        page.update()

    def _reshuffle(_: ft.ControlEvent) -> None:
        state["seed_offset"] += 1
        _refresh_view()
        page.update()

    def _copy_md(_: ft.ControlEvent) -> None:
        plan: SchedulePlan | None = state["plan"]
        if plan is not None and on_copy_schedule is not None:
            on_copy_schedule(format_schedule_markdown(plan))

    def _apply(_: ft.ControlEvent) -> None:
        plan: SchedulePlan | None = state["plan"]
        page.pop_dialog()
        if plan is not None:
            on_apply_schedule(plan)

    def _clear(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        if on_clear_schedule is not None:
            on_clear_schedule()

    def _handle_open_one(name: str) -> None:
        if name in launched_in_dialog:
            return
        launched_in_dialog.add(name)
        if on_launch_single is not None:
            on_launch_single(name)
        _refresh_view(recompute=False)
        page.update()

    def _refresh_view(*, recompute: bool = True) -> None:
        plan: SchedulePlan = _recompute() if (recompute or state["plan"] is None) else state["plan"]

        # 1. Cycle selector pills
        cyc_ctrls: list[ft.Control] = [
            ft.Text(
                get_string("schedule_cycle_label"),
                size=12.5,
                font_family=FONT_SEMIBOLD,
                color=COLORS["text_sub"],
            )
        ]
        for cyc in _CYCLE_PRESETS:
            is_on = cyc == state["cycle_days"]
            cyc_ctrls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                    border_radius=6,
                    bgcolor=COLORS["sidebar"] if is_on else COLORS["input_bg"],
                    border=ft.Border.all(
                        1,
                        COLORS["sidebar"] if is_on else COLORS["rule_strong"],
                    ),
                    ink=True,
                    on_click=lambda _, c=cyc: _set_cycle(c),
                    content=ft.Text(
                        get_string("schedule_cycle_days_opt", days=cyc),
                        size=12,
                        font_family=FONT_SEMIBOLD if is_on else FONT_DATA,
                        color=COLORS["cover_ink"] if is_on else COLORS["text_main"],
                    ),
                )
            )
        cyc_ctrls.append(
            ft.OutlinedButton(
                get_string("schedule_reshuffle_btn"),
                icon=ft.Icons.SHUFFLE_ROUNDED,
                height=32,
                style=OUTLINE_STYLE,
                on_click=_reshuffle,
            )
        )
        if on_copy_schedule is not None:
            cyc_ctrls.append(
                ft.OutlinedButton(
                    get_string("schedule_copy_md_btn"),
                    icon=ft.Icons.CONTENT_COPY_ROUNDED,
                    height=32,
                    style=OUTLINE_STYLE,
                    on_click=_copy_md,
                )
            )
        cycle_row.controls = cyc_ctrls

        # 2. Day filter tabs
        df_ctrls: list[ft.Control] = [
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                border_radius=6,
                bgcolor=COLORS["sidebar"] if state["selected_day"] == -1 else COLORS["input_bg"],
                border=ft.Border.all(
                    1,
                    COLORS["sidebar"] if state["selected_day"] == -1 else COLORS["rule_strong"],
                ),
                ink=True,
                on_click=lambda _: _set_day_filter(-1),
                content=ft.Text(
                    f"{get_string('filter_all')} ({plan.total_live})",
                    size=11.5,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["cover_ink"]
                    if state["selected_day"] == -1
                    else COLORS["text_main"],
                ),
            )
        ]
        for d in plan.days:
            is_sel = state["selected_day"] == d.day_index
            df_ctrls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                    border_radius=6,
                    bgcolor=COLORS["sidebar"] if is_sel else COLORS["input_bg"],
                    border=ft.Border.all(
                        1,
                        COLORS["sidebar"] if is_sel else COLORS["rule_strong"],
                    ),
                    ink=True,
                    on_click=lambda _, idx=d.day_index: _set_day_filter(idx),
                    content=ft.Text(
                        f"{d.weekday_tag} · {d.date_str[:5]} ({d.total_accounts})",
                        size=11.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["cover_ink"] if is_sel else COLORS["text_main"],
                    ),
                )
            )
        day_filter_row.controls = df_ctrls

        # 4. Day cards
        visible_days = (
            plan.days
            if state["selected_day"] == -1
            else [d for d in plan.days if d.day_index == state["selected_day"]]
        )
        days_list_col.controls = [
            _build_day_card(
                d,
                lang,
                opened_names=launched_in_dialog,
                on_open_one=_handle_open_one if on_launch_single is not None else None,
            )
            for d in visible_days
        ]

    _refresh_view(recompute=True)

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title_padding=ft.Padding.only(left=24, right=24, top=20, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=18),
        title=ft.Column(
            spacing=4,
            tight=True,
            controls=[
                ft.Text(
                    get_string("schedule_dialog_title"),
                    size=17,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                ft.Text(
                    get_string("schedule_dialog_subtitle"),
                    size=12,
                    color=COLORS["text_sub"],
                ),
            ],
        ),
        content=ft.Container(
            width=820,
            content=ft.Column(
                spacing=10,
                tight=True,
                controls=[
                    cycle_row,
                    day_filter_row,
                    days_list_col,
                ],
            ),
        ),
        actions=[
            ft.OutlinedButton(
                get_string("close_btn"),
                style=OUTLINE_STYLE,
                on_click=lambda _: page.pop_dialog(),
            ),
            *(
                [
                    ft.OutlinedButton(
                        get_string("schedule_clear_tags_btn"),
                        icon=ft.Icons.DELETE_SWEEP_OUTLINED,
                        style=OUTLINE_STYLE,
                        on_click=_clear,
                    )
                ]
                if on_clear_schedule is not None
                else []
            ),
            ft.Button(
                get_string("schedule_apply_tags_btn"),
                icon=ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED,
                style=ACCENT_STYLE,
                on_click=_apply,
            ),
        ],
    )
    page.show_dialog(dlg)


_SQUAD_SIZES = (2, 4, 6, 8, 10)


def _build_squad_candidate_row(
    idx: int,
    cand: SquadCandidate,
    *,
    is_opened: bool = False,
    on_open_one: Callable[[str], None] | None = None,
) -> ft.Container:
    is_gw = is_gateway_ipv6_cluster(cand.cluster)
    cluster_col = "#0284C7" if is_gw else "#059669"
    tier_sym = {
        "diamond": "✦",
        "elite": "🛡️",
        "platinum": "❖",
        "trusted": "◈",
        "warming": "◇",
        "cold": "○",
    }.get(cand.trust_tier, "◈")
    tier_col = (
        "#1D4ED8"
        if cand.trust_tier == "diamond"
        else (
            "#15803D"
            if cand.trust_tier == "elite"
            else (
                "#7C3AED"
                if cand.trust_tier == "platinum"
                else (
                    "#B45309"
                    if cand.trust_tier == "trusted"
                    else (COLORS["text_dim"] if cand.trust_tier == "warming" else "#F472B6")
                )
            )
        )
    )
    display_name = (
        cand.name
        if cand.name == cand.email or not cand.email
        else f"{cand.name} · {cand.email}"
    )
    rest_h = int(round(cand.rest_hours))

    right_badges: list[ft.Control] = []
    if cand.schedule_label:
        right_badges.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=4,
                bgcolor=ft.Colors.with_opacity(0.12, "#2563EB"),
                border=ft.Border.all(1, ft.Colors.with_opacity(0.55, "#2563EB")),
                content=ft.Text(
                    f"📅 {cand.schedule_label}",
                    size=10.5,
                    font_family=FONT_SEMIBOLD,
                    color="#2563EB",
                ),
            )
        )
    right_badges.extend(
        [
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                border_radius=4,
                bgcolor=cluster_col,
                content=ft.Text(
                    f"{cand.cluster}{cand.port}",
                    size=11,
                    font_family=FONT_DATA,
                    color="#FFFFFF",
                ),
            ),
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=4,
                bgcolor=ft.Colors.with_opacity(0.11, tier_col),
                border=ft.Border.all(1, ft.Colors.with_opacity(0.55, tier_col)),
                content=ft.Text(
                    f"{tier_sym} {cand.trust_score}",
                    size=11,
                    font_family=FONT_DATA,
                    weight=ft.FontWeight.W_700,
                    color=tier_col,
                ),
            ),
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                border_radius=4,
                bgcolor=COLORS["card_bg"],
                border=ft.Border.all(1, COLORS["rule_strong"]),
                content=ft.Text(
                    get_string("work_squad_rest_badge", hours=rest_h),
                    size=11,
                    font_family=FONT_DATA,
                    color=COLORS["text_sub"],
                ),
            ),
        ]
    )
    if cand.is_conflict:
        right_badges.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=4,
                bgcolor=ft.Colors.with_opacity(0.12, COLORS["warning"]),
                border=ft.Border.all(1, COLORS["warning"]),
                content=ft.Text(
                    get_string("work_squad_busy_cluster_badge"),
                    size=10.5,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["warning"],
                ),
            )
        )
    elif cand.today_minutes >= 15:
        right_badges.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=4,
                bgcolor=COLORS["card_bg"],
                border=ft.Border.all(1, "#0284C7"),
                content=ft.Text(
                    get_string("work_squad_done_today_badge", mins=cand.today_minutes),
                    size=10.5,
                    font_family=FONT_SEMIBOLD,
                    color="#0284C7",
                ),
            )
        )
    elif cand.is_rising_star:
        right_badges.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=4,
                bgcolor=COLORS["card_bg"],
                border=ft.Border.all(1, COLORS["accent"]),
                content=ft.Text(
                    get_string("work_squad_rising_badge"),
                    size=10.5,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["accent"],
                ),
            )
        )

    if is_opened:
        right_badges.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=9, vertical=4),
                border_radius=5,
                bgcolor=ft.Colors.with_opacity(0.14, COLORS["success"]),
                border=ft.Border.all(1, COLORS["success"]),
                content=ft.Text(
                    get_string("work_squad_row_opened_badge"),
                    size=11,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["success"],
                ),
            )
        )
    elif on_open_one is not None and not cand.is_conflict:
        right_badges.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                border_radius=5,
                bgcolor=COLORS["sidebar"],
                ink=True,
                on_click=lambda _, n=cand.name: on_open_one(n),
                content=ft.Text(
                    get_string("work_squad_row_open_btn"),
                    size=11,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["cover_ink"],
                ),
            )
        )

    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        border_radius=8,
        bgcolor=COLORS["input_bg"],
        border=ft.Border.all(1, COLORS["card_border"]),
        content=ft.Row(
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    width=28,
                    height=24,
                    border_radius=5,
                    bgcolor=COLORS["sidebar"],
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        f"#{idx}",
                        size=11,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["cover_ink"],
                    ),
                ),
                ft.Text(
                    display_name,
                    size=13,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_main"],
                    expand=True,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                ft.Row(spacing=6, tight=True, controls=right_badges),
            ],
        ),
    )


def open_work_squad_dialog(
    page: ft.Page,
    profiles: list[Profile],
    snap: MonitorSnapshot,
    running_names: set[str],
    *,
    on_select_squad: Callable[[list[str], str], None],
    on_launch_single: Callable[[str], None] | None = None,
    on_launch_staggered: Callable[[list[str], str], None] | None = None,
    pinned_squads: dict[str, list[str]] | None = None,
    on_save_fixed_squad: Callable[[str, list[str] | None, int], None] | None = None,
) -> None:
    """Compact modal to pick and launch a conflict-free parallel work squad across distinct /64 subnets."""
    launched_in_dialog: set[str] = set()
    local_pinned: dict[str, list[str]] = {
        k: list(v) for k, v in (pinned_squads or {}).items()
    }
    state: dict[str, object] = {
        "mode": "rotation",
        "size": 4,
        "seed": 0,
        "squad_idx": 0,
        "plan": None,
    }

    controls_row = ft.Row(spacing=7, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    squad_bar_slot = ft.Container(visible=False)
    banner_slot = ft.Container(visible=False)
    list_col = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=310)
    select_only_btn = ft.OutlinedButton(
        "",
        icon=ft.Icons.CHECKLIST_ROUNDED,
        style=OUTLINE_STYLE,
    )
    apply_btn = ft.Button(
        "",
        icon=ft.Icons.ROCKET_LAUNCH_ROUNDED,
        style=ACCENT_STYLE,
    )

    def _pill_btn(label_str: str, active: bool, on_click_fn: Callable[[], None]) -> ft.Container:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=5),
            border_radius=6,
            bgcolor=COLORS["sidebar"] if active else COLORS["input_bg"],
            border=ft.Border.all(
                1,
                COLORS["sidebar"] if active else COLORS["rule_strong"],
            ),
            ink=True,
            on_click=lambda _: on_click_fn(),
            content=ft.Text(
                label_str,
                size=12,
                font_family=FONT_SEMIBOLD if active else FONT_DATA,
                color=COLORS["cover_ink"] if active else COLORS["text_main"],
            ),
        )

    def _set_mode(m: str) -> None:
        state["mode"] = m
        _refresh_squad(recompute=True)
        page.update()

    def _set_size(sz: int) -> None:
        state["size"] = sz
        state["squad_idx"] = 0
        _refresh_squad(recompute=True)
        page.update()

    def _set_squad_idx(idx: int) -> None:
        state["squad_idx"] = idx
        _refresh_squad(recompute=True)
        page.update()

    def _shuffle(_: ft.ControlEvent) -> None:
        state["seed"] = int(state["seed"]) + 1  # type: ignore[arg-type]
        _refresh_squad(recompute=True)
        page.update()

    def _toggle_pin_current(_: ft.ControlEvent) -> None:
        plan: WorkSquadPlan | None = state["plan"]  # type: ignore[assignment]
        if plan is None or not plan.candidates:
            return
        sz = int(state["size"])  # type: ignore[arg-type]
        if state["mode"] != "fixed":
            # Find first unpinned slot (or slot 0) for this size and switch to fixed mode
            target_idx = 0
            for idx_cand in range(12):
                if f"{sz}:{idx_cand}" not in local_pinned:
                    target_idx = idx_cand
                    break
            key = f"{sz}:{target_idx}"
            names = [c.name for c in plan.candidates]
            local_pinned[key] = names
            state["mode"] = "fixed"
            state["squad_idx"] = target_idx
            if on_save_fixed_squad is not None:
                on_save_fixed_squad(key, names, target_idx + 1)
        else:
            s_idx = int(plan.squad_index)
            key = f"{sz}:{s_idx}"
            if plan.is_pinned:
                local_pinned.pop(key, None)
                if on_save_fixed_squad is not None:
                    on_save_fixed_squad(key, None, s_idx + 1)
            else:
                names = [c.name for c in plan.candidates]
                local_pinned[key] = names
                if on_save_fixed_squad is not None:
                    on_save_fixed_squad(key, names, s_idx + 1)
        _refresh_squad(recompute=True)
        page.update()

    def _handle_open_one(name: str) -> None:
        if name in launched_in_dialog:
            return
        launched_in_dialog.add(name)
        if on_launch_single is not None:
            on_launch_single(name)
        _refresh_squad(recompute=False)
        page.update()

    def _select_only(_: ft.ControlEvent) -> None:
        plan: WorkSquadPlan | None = state["plan"]  # type: ignore[assignment]
        page.pop_dialog()
        if plan is not None and plan.candidates:
            on_select_squad([c.name for c in plan.candidates], str(state["mode"]))

    def _apply(_: ft.ControlEvent) -> None:
        plan: WorkSquadPlan | None = state["plan"]  # type: ignore[assignment]
        page.pop_dialog()
        if plan is not None and plan.candidates:
            names = [
                c.name
                for c in plan.candidates
                if c.name not in (running_names | launched_in_dialog) and not c.is_conflict
            ]
            if not names:
                names = [c.name for c in plan.candidates]
            mode_str = str(state["mode"])
            if on_launch_staggered is not None:
                on_launch_staggered(names, mode_str)
            else:
                on_select_squad(names, mode_str)

    select_only_btn.on_click = _select_only
    apply_btn.on_click = _apply

    def _refresh_squad(*, recompute: bool = True) -> None:
        active_all = running_names | launched_in_dialog
        plan: WorkSquadPlan | None = state["plan"]  # type: ignore[assignment]
        if recompute or plan is None:
            plan = select_parallel_work_squad(
                profiles,
                snap,
                active_all,
                count=int(state["size"]),  # type: ignore[arg-type]
                mode=str(state["mode"]),
                shuffle_seed=int(state["seed"]) * 53,  # type: ignore[arg-type]
                squad_index=int(state["squad_idx"]),  # type: ignore[arg-type]
                pinned_squads=local_pinned,
            )
            state["plan"] = plan
            state["squad_idx"] = plan.squad_index

        bar_items: list[ft.Control] = [
            ft.Text(
                get_string("work_squad_mode_label"),
                size=12,
                font_family=FONT_SEMIBOLD,
                color=COLORS["text_sub"],
            ),
            _pill_btn(
                get_string("work_squad_mode_rotation"),
                state["mode"] == "rotation",
                lambda: _set_mode("rotation"),
            ),
            _pill_btn(
                get_string("work_squad_mode_prime"),
                state["mode"] == "prime",
                lambda: _set_mode("prime"),
            ),
            _pill_btn(
                get_string("work_squad_mode_fixed"),
                state["mode"] == "fixed",
                lambda: _set_mode("fixed"),
            ),
            ft.Container(width=4),
            ft.Text(
                get_string("work_squad_size_label"),
                size=12,
                font_family=FONT_SEMIBOLD,
                color=COLORS["text_sub"],
            ),
            *[
                _pill_btn(str(sz), state["size"] == sz, lambda s=sz: _set_size(s))
                for sz in _SQUAD_SIZES
            ],
            ft.OutlinedButton(
                get_string("schedule_reshuffle_btn"),
                icon=ft.Icons.SHUFFLE_ROUNDED,
                height=30,
                style=OUTLINE_STYLE,
                on_click=_shuffle,
            ),
        ]
        if state["mode"] != "fixed" and plan.candidates:
            bar_items.append(
                ft.OutlinedButton(
                    get_string("work_squad_save_as_fixed_btn"),
                    icon=ft.Icons.PUSH_PIN_OUTLINED,
                    height=30,
                    style=OUTLINE_STYLE,
                    on_click=_toggle_pin_current,
                )
            )
        controls_row.controls = bar_items

        if state["mode"] == "fixed" and plan.total_squads > 0:
            total_acc = sum(len(sq) for sq in plan.all_squads)
            team_pills: list[ft.Control] = [
                ft.Text(
                    f"{get_string('work_squad_team_label')} ({plan.total_squads} · {total_acc} acc)",
                    size=12,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_sub"],
                ),
            ]
            for s_idx in range(plan.total_squads):
                is_pinned_tab = s_idx in plan.pinned_indices
                lbl = get_string(
                    "work_squad_team_pinned_pill" if is_pinned_tab else "work_squad_team_pill",
                    idx=s_idx + 1,
                )
                team_pills.append(
                    _pill_btn(
                        lbl,
                        plan.squad_index == s_idx,
                        lambda target=s_idx: _set_squad_idx(target),
                    )
                )
            pin_btn_ctrl = ft.OutlinedButton(
                get_string("work_squad_unpin_btn" if plan.is_pinned else "work_squad_pin_btn"),
                icon=ft.Icons.PUSH_PIN_ROUNDED if plan.is_pinned else ft.Icons.PUSH_PIN_OUTLINED,
                height=30,
                style=OUTLINE_STYLE,
                on_click=_toggle_pin_current,
            )
            squad_bar_slot.visible = True
            squad_bar_slot.content = ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=7),
                border_radius=8,
                bgcolor=COLORS["bg"],
                border=ft.Border.all(1, COLORS["card_border"]),
                content=ft.Column(
                    spacing=5,
                    tight=True,
                    controls=[
                        ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Row(
                                    spacing=5,
                                    wrap=True,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    expand=True,
                                    controls=team_pills,
                                ),
                                pin_btn_ctrl,
                            ],
                        ),
                        ft.Text(
                            get_string("work_squad_fixed_hint"),
                            size=11,
                            color=COLORS["text_sub"],
                        ),
                    ],
                ),
            )
        else:
            squad_bar_slot.visible = False

        if plan.busy_clusters:
            locked_str = ", ".join(
                f"{cl} ({', '.join(names)})"
                for cl, names in sorted(plan.busy_clusters.items())
            )
            banner_slot.visible = True
            banner_slot.content = ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                border_radius=6,
                bgcolor=ft.Colors.with_opacity(0.11, COLORS["warning"]),
                border=ft.Border.all(1, COLORS["warning"]),
                content=ft.Row(
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(ft.Icons.LOCK_CLOCK_OUTLINED, size=14, color=COLORS["warning"]),
                        ft.Text(
                            get_string("work_squad_busy_banner", clusters=locked_str),
                            size=11.5,
                            color=COLORS["text_main"],
                            expand=True,
                        ),
                    ],
                ),
            )
        else:
            banner_slot.visible = False

        if plan.candidates:
            list_col.controls = [
                _build_squad_candidate_row(
                    i + 1,
                    c,
                    is_opened=(c.name in active_all),
                    on_open_one=_handle_open_one,
                )
                for i, c in enumerate(plan.candidates)
            ]
            remaining_cnt = sum(
                1
                for c in plan.candidates
                if c.name not in active_all and not c.is_conflict
            )
            select_only_btn.disabled = False
            select_only_btn.content = get_string("work_squad_select_only_btn", count=len(plan.candidates))
            apply_btn.disabled = remaining_cnt == 0
            apply_btn.content = get_string(
                "work_squad_apply_btn",
                count=remaining_cnt if remaining_cnt > 0 else len(plan.candidates),
            )
        else:
            list_col.controls = [
                ft.Container(
                    padding=20,
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("work_squad_empty"),
                        size=13,
                        color=COLORS["text_sub"],
                    ),
                )
            ]
            select_only_btn.disabled = True
            select_only_btn.content = get_string("work_squad_select_only_btn", count=0)
            apply_btn.disabled = True
            apply_btn.content = get_string("work_squad_apply_btn", count=0)

    _refresh_squad(recompute=True)

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title_padding=ft.Padding.only(left=24, right=24, top=20, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=18),
        title=ft.Column(
            spacing=4,
            tight=True,
            controls=[
                ft.Text(
                    get_string("work_squad_dialog_title"),
                    size=17,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                ft.Text(
                    get_string("work_squad_dialog_subtitle"),
                    size=12,
                    color=COLORS["text_sub"],
                ),
            ],
        ),
        content=ft.Container(
            width=780,
            content=ft.Column(
                spacing=10,
                tight=True,
                controls=[
                    controls_row,
                    squad_bar_slot,
                    banner_slot,
                    list_col,
                ],
            ),
        ),
        actions=[
            ft.OutlinedButton(
                get_string("close_btn"),
                style=OUTLINE_STYLE,
                on_click=lambda _: page.pop_dialog(),
            ),
            select_only_btn,
            apply_btn,
        ],
    )
    page.show_dialog(dlg)
