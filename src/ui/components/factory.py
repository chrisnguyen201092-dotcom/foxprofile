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
    opt_style = ft.ButtonStyle(
        padding=ft.Padding.symmetric(horizontal=12, vertical=6),
        visual_density=ft.VisualDensity.COMPACT,
        color=COLORS["text_main"],
        overlay_color=COLORS["input_bg"],
        text_style=ft.TextStyle(font_family=FONT_MEDIUM, size=12.5),
    )
    sort_keys = (
        ("custom", "sort_custom"),
        ("schedule", "sort_schedule"),
        ("cooldown_desc", "sort_cooldown_desc"),
        ("cooldown_asc", "sort_cooldown_asc"),
        ("trust_desc", "sort_trust_desc"),
        ("trust_asc", "sort_trust_asc"),
        ("age_desc", "sort_age_desc"),
        ("age_asc", "sort_age_asc"),
        ("name_asc", "sort_name_asc"),
        ("name_desc", "sort_name_desc"),
        ("running", "sort_running"),
    )
    valid_sort_keys = {k for k, _ in sort_keys}
    sort_dropdown = ft.Dropdown(
        value=sort_by if sort_by in valid_sort_keys else "custom",
        width=190,
        menu_width=200,
        dense=True,
        filled=True,
        fill_color=COLORS["input_bg"],
        bgcolor=COLORS["input_bg"],
        color=COLORS["text_main"],
        text_size=12.5,
        text_style=ft.TextStyle(
            font_family=FONT_MEDIUM,
            size=12.5,
            color=COLORS["text_main"],
        ),
        content_padding=ft.Padding.only(left=12, right=6, top=8, bottom=8),
        border_color=COLORS["rule_strong"],
        focused_border_color=COLORS["sidebar"],
        border_radius=8,
        trailing_icon=ft.Icon(
            ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED,
            size=18,
            color=COLORS["text_sub"],
        ),
        selected_trailing_icon=ft.Icon(
            ft.Icons.KEYBOARD_ARROW_UP_ROUNDED,
            size=18,
            color=COLORS["text_main"],
        ),
        menu_style=ft.MenuStyle(
            bgcolor=COLORS["card_bg"],
            elevation=6,
            padding=ft.Padding.symmetric(vertical=4),
            side=ft.BorderSide(1, COLORS["rule_strong"]),
            shape=ft.RoundedRectangleBorder(radius=8),
            visual_density=ft.VisualDensity.COMPACT,
        ),
        options=[
            ft.dropdown.Option(k, get_string(label_key), style=opt_style)
            for k, label_key in sort_keys
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
