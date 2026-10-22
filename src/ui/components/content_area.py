import contextlib
from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...services.monitor import MonitorSnapshot
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_MEDIUM, FONT_SEMIBOLD


def build_monitor_bar(
    snap: MonitorSnapshot,
    total_profiles: int = 0,
    on_check_all_proxies: Callable[[], None] | None = None,
) -> ft.Container | None:
    """Compact alert strip that only appears when an active issue is detected."""
    _ = (total_profiles, on_check_all_proxies)

    def _pill(
        text: str,
        icon: ft.IconData,
        color: str,
        bg: str | None = None,
    ) -> ft.Container:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=9, vertical=4),
            border_radius=6,
            bgcolor=bg or COLORS["card_bg"],
            border=ft.Border.all(1, color if bg else COLORS["card_border"]),
            content=ft.Row(
                spacing=5,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(icon, size=13, color=color),
                    ft.Text(
                        text,
                        size=11.5,
                        font_family=FONT_DATA,
                        color=color,
                    ),
                ],
            ),
        )

    pills: list[ft.Control] = []

    for port, members in snap.proxy_collisions:
        pills.append(
            _pill(
                get_string(
                    "monitor_alert_proxy_collision",
                    port=port,
                    names=", ".join(members),
                ),
                ft.Icons.GPP_MAYBE,
                COLORS["error"],
                COLORS["input_bg"],
            )
        )
    for pname, count in snap.token_bursts:
        pills.append(
            _pill(
                get_string("monitor_alert_token_burst", name=pname, count=count),
                ft.Icons.BOLT,
                COLORS["warning"],
                COLORS["input_bg"],
            )
        )
    if snap.disk_low:
        pills.append(
            _pill(
                get_string("monitor_alert_disk_low", gb=f"{snap.free_gb:.1f}"),
                ft.Icons.WARNING_AMBER_ROUNDED,
                COLORS["error"],
                COLORS["input_bg"],
            )
        )

    if not pills:
        return None

    return ft.Container(
        padding=ft.Padding.only(bottom=8),
        content=ft.Row(
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=pills,
        ),
    )


def build_pages_bar(
    active_page: str,
    total_count: int,
    page_counts: list[tuple[str, int]],
    on_select_page: Callable[[str], None],
    on_add_page: Callable[[], None],
    on_edit_page: Callable[[str], None],
    on_reorder_page: Callable[[str, str], None] | None = None,
    all_tab_index: int = 0,
) -> ft.Row:
    """Horizontal custom pages bar placed right above the profile table."""
    can_drag = len(page_counts) >= 1 and on_reorder_page is not None

    def _tab(
        page_id: str,
        label: str,
        count: int,
        editable: bool = False,
    ) -> ft.Control:
        is_active = active_page == page_id
        bg = COLORS["sidebar"] if is_active else COLORS["card_bg"]
        border_col = COLORS["sidebar"] if is_active else COLORS["rule_strong"]
        text_col = COLORS["cover_ink"] if is_active else COLORS["text_main"]
        count_col = COLORS["cover_muted"] if is_active else COLORS["text_dim"]

        row_controls: list[ft.Control] = [
            ft.Container(
                padding=ft.Padding.only(
                    left=12,
                    right=6 if editable else 12,
                    top=6,
                    bottom=6,
                ),
                on_click=lambda _, pid=page_id: on_select_page(pid),
                content=ft.Row(
                    spacing=6,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            label,
                            size=13,
                            font_family=FONT_SEMIBOLD if is_active else FONT_MEDIUM,
                            color=text_col,
                        ),
                        ft.Text(
                            str(count),
                            size=11.5,
                            font_family=FONT_DATA,
                            color=count_col,
                        ),
                    ],
                ),
            ),
        ]
        if editable:
            row_controls.append(
                ft.Container(
                    padding=ft.Padding.only(left=2, right=8, top=6, bottom=6),
                    tooltip=get_string("tooltip_edit_page"),
                    on_click=lambda _, pg=label: on_edit_page(pg),
                    content=ft.Icon(
                        ft.Icons.EDIT_OUTLINED,
                        size=13.5,
                        color=COLORS["cover_muted"] if is_active else COLORS["text_dim"],
                    ),
                )
            )
        tab_box = ft.Container(
            height=34,
            border_radius=8,
            bgcolor=bg,
            border=ft.Border.all(1, border_col),
            content=ft.Row(
                spacing=0,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=row_controls,
            ),
        )
        if not can_drag:
            return tab_box

        def _on_will_accept(e: ft.DragWillAcceptEvent, box: ft.Container = tab_box) -> None:
            box.border = ft.Border.all(1.5, COLORS["accent"])
            with contextlib.suppress(Exception):
                box.update()

        def _on_leave(
            e: ft.DragTargetLeaveEvent,
            box: ft.Container = tab_box,
            default_border: str = border_col,
        ) -> None:
            box.border = ft.Border.all(1, default_border)
            with contextlib.suppress(Exception):
                box.update()

        def _on_accept(
            e: ft.DragTargetEvent,
            target_id: str = page_id,
            box: ft.Container = tab_box,
            default_border: str = border_col,
        ) -> None:
            box.border = ft.Border.all(1, default_border)
            src_ctrl = getattr(e, "src", None)
            src_id = getattr(src_ctrl, "data", None)
            if (
                isinstance(src_id, str)
                and src_id
                and src_id != target_id
                and on_reorder_page is not None
            ):
                on_reorder_page(src_id, target_id)

        feedback_box = ft.Container(
            height=34,
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            border_radius=8,
            bgcolor=COLORS["sidebar"],
            border=ft.Border.all(1.5, COLORS["accent"]),
            content=ft.Row(
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(
                        label,
                        size=13,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["cover_ink"],
                    ),
                    ft.Text(
                        str(count),
                        size=11.5,
                        font_family=FONT_DATA,
                        color=COLORS["cover_muted"],
                    ),
                ],
            ),
        )
        ghost_box = ft.Container(
            height=34,
            border_radius=8,
            bgcolor=bg,
            opacity=0.4,
            border=ft.Border.all(1, COLORS["accent"]),
            content=ft.Row(
                spacing=0,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=row_controls,
            ),
        )
        return ft.DragTarget(
            group="custom_pages",
            data=page_id,
            on_will_accept=_on_will_accept,
            on_leave=_on_leave,
            on_accept=_on_accept,
            content=ft.Draggable(
                group="custom_pages",
                data=page_id,
                content=tab_box,
                content_feedback=feedback_box,
                content_when_dragging=ghost_box,
            ),
        )

    custom_tabs = [_tab(pg, pg, cnt, editable=True) for pg, cnt in page_counts]
    idx = max(0, min(all_tab_index, len(custom_tabs)))
    custom_tabs.insert(idx, _tab("__all__", get_string("page_all"), total_count, editable=False))
    tabs: list[ft.Control] = [
        *custom_tabs,
        ft.Container(
            height=34,
            padding=ft.Padding.symmetric(horizontal=11),
            border_radius=8,
            bgcolor=COLORS["input_bg"],
            border=ft.Border.all(1, COLORS["rule_strong"]),
            ink=True,
            on_click=lambda _: on_add_page(),
            content=ft.Row(
                spacing=5,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.ADD, size=15, color=COLORS["accent"]),
                    ft.Text(
                        get_string("new_page_btn"),
                        size=12.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["text_main"],
                    ),
                ],
            ),
        ),
    ]
    return ft.Row(
        spacing=8,
        scroll=ft.ScrollMode.AUTO,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=tabs,
    )


def build_content_area(
    subtitle: ft.Text,
    profile_list: ft.Column,
    prev_btn: ft.IconButton,
    next_btn: ft.IconButton,
    page_label: ft.Text,
    bulk_bar: ft.Control | None = None,
    search_field: ft.Control | None = None,
    header_slot: ft.Container | None = None,
    sort_control: ft.Control | None = None,
    pages_bar: ft.Control | None = None,
    monitor_bar: ft.Control | None = None,
    check_proxy_btn: ft.Control | None = None,
    cooldown_btn: ft.Control | None = None,
    arrange_btn: ft.Control | None = None,
) -> ft.Container:
    """Main content: title, search, custom pages bar, then the profile sheet."""
    _ = (prev_btn, next_btn, page_label)
    sheet = ft.Container(
        expand=True,
        bgcolor=COLORS["card_bg"],
        border=ft.Border.all(1, COLORS["card_border"]),
        border_radius=10,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        content=ft.Column(
            spacing=0,
            expand=True,
            controls=[
                *([] if bulk_bar is None else [bulk_bar]),
                *([] if header_slot is None else [header_slot]),
                profile_list,
            ],
        ),
    )
    right_head_controls = [
        c
        for c in (arrange_btn, cooldown_btn, check_proxy_btn, sort_control, search_field)
        if c is not None
    ]
    return ft.Container(
        expand=True,
        bgcolor=COLORS["bg"],
        padding=ft.Padding.only(left=32, right=32, top=26, bottom=24),
        content=ft.Column(
            spacing=0,
            expand=True,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.END,
                    controls=[
                        ft.Column(
                            spacing=2,
                            controls=[
                                ft.Text(
                                    get_string("your_profiles"),
                                    size=26,
                                    font_family=FONT_BOLD,
                                    color=COLORS["text_main"],
                                ),
                                subtitle,
                            ],
                        ),
                        *(
                            [
                                ft.Row(
                                    spacing=10,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    controls=right_head_controls,
                                )
                            ]
                            if right_head_controls
                            else []
                        ),
                    ],
                ),
                ft.Container(height=14),
                *([] if monitor_bar is None else [monitor_bar]),
                *([] if pages_bar is None else [pages_bar, ft.Container(height=10)]),
                sheet,
            ],
        ),
    )
