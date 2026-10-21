from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IProfileManager
from ..refs import UIRefs
from ..theme.colors import COLORS, FONT_DATA, FONT_MEDIUM


def build_ui_refs(
    pm: IProfileManager,
    on_change_page: Callable[[int], None],
    file_picker: ft.FilePicker,
    on_search: Callable[[str], None],
    sort_by: str = "custom",
    on_sort: Callable[[str], None] | None = None,
    on_reorder: Callable[[int, int], None] | None = None,
) -> UIRefs:
    log_text = ft.Text(
        "",
        size=11,
        font_family=FONT_DATA,
        color=COLORS["cover_muted"],
        selectable=True,
        no_wrap=False,
    )
    log_toggle_btn = ft.TextButton(
        get_string("activity_log"),
        icon=ft.Icons.KEYBOARD_ARROW_DOWN,
        style=ft.ButtonStyle(
            color=COLORS["cover_ink"],
            overlay_color=COLORS["cover_deep"],
            text_style=ft.TextStyle(font_family=FONT_MEDIUM, size=13),
        ),
    )
    sort_dropdown = ft.Dropdown(
        value=sort_by,
        width=205,
        dense=True,
        text_size=13,
        bgcolor=COLORS["input_bg"],
        color=COLORS["text_main"],
        border_color=COLORS["rule_strong"],
        focused_border_color=COLORS["sidebar"],
        border_radius=8,
        options=[
            ft.dropdown.Option("custom", get_string("sort_custom")),
            ft.dropdown.Option("cooldown_desc", get_string("sort_cooldown_desc")),
            ft.dropdown.Option("cooldown_asc", get_string("sort_cooldown_asc")),
            ft.dropdown.Option("name_asc", get_string("sort_name_asc")),
            ft.dropdown.Option("name_desc", get_string("sort_name_desc")),
            ft.dropdown.Option("group", get_string("sort_group")),
            ft.dropdown.Option("running", get_string("sort_running")),
            ft.dropdown.Option("proxy", get_string("sort_proxy")),
        ],
        on_select=lambda e: on_sort(e.control.value or "custom") if on_sort else None,
    )

    def _handle_reorder(e: ft.OnReorderEvent) -> None:
        if on_reorder is not None and e.old_index is not None and e.new_index is not None:
            on_reorder(e.old_index, e.new_index)

    return UIRefs(
        stats_text=ft.Text(
            get_string("total_profiles", count=len(pm.profiles)),
            size=12,
            color=COLORS["cover_muted"],
        ),
        running_text=ft.Text("", size=12, color=COLORS["cover_muted"]),
        log_text=log_text,
        log_column=ft.Container(
            content=ft.Column(
                controls=[log_text],
                spacing=0,
            ),
            visible=False,
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            margin=ft.Margin.symmetric(horizontal=0, vertical=4),
            border_radius=8,
            bgcolor=COLORS["log_bg"],
        ),
        log_toggle_btn=log_toggle_btn,
        content_subtitle=ft.Text("", size=13.5, color=COLORS["text_sub"]),
        profile_list_area=ft.ReorderableListView(
            spacing=0,
            expand=True,
            padding=0,
            show_default_drag_handles=False,
            on_reorder=_handle_reorder,
        ),
        prev_btn=ft.IconButton(
            icon=ft.Icons.CHEVRON_LEFT,
            icon_color=COLORS["text_sub"],
            disabled=True,
            on_click=lambda _: on_change_page(-1),
        ),
        next_btn=ft.IconButton(
            icon=ft.Icons.CHEVRON_RIGHT,
            icon_color=COLORS["text_sub"],
            disabled=True,
            on_click=lambda _: on_change_page(1),
        ),
        page_label=ft.Text(
            get_string("page_of", current=1, total=1),
            size=13,
            color=COLORS["text_sub"],
        ),
        bulk_bar=ft.Row(visible=False, spacing=0, controls=[]),
        file_picker=file_picker,
        search_field=ft.TextField(
            hint_text=get_string("search_hint"),
            prefix_icon=ft.Icons.SEARCH,
            width=280,
            height=40,
            dense=True,
            border_radius=8,
            bgcolor=COLORS["input_bg"],
            color=COLORS["text_main"],
            text_size=14,
            border_color=COLORS["rule_strong"],
            focused_border_color=COLORS["sidebar"],
            cursor_color=COLORS["accent"],
            hint_style=ft.TextStyle(color=COLORS["text_dim"]),
            on_change=lambda e: on_search(e.control.value or ""),
        ),
        sort_dropdown=sort_dropdown,
    )
