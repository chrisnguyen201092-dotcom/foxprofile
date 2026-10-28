from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IProfileManager
from ..refs import UIRefs
from ..theme.colors import COLORS, FONT_DATA, FONT_MEDIUM, FONT_SEMIBOLD

_SORT_CATEGORIES = (
    ("custom", "sort_cat_custom", ft.Icons.DRAG_INDICATOR_ROUNDED, ("custom",)),
    ("schedule", "sort_cat_schedule", ft.Icons.CALENDAR_MONTH_ROUNDED, ("schedule",)),
    (
        "cooldown",
        "sort_cat_cooldown",
        ft.Icons.HOURGLASS_BOTTOM_ROUNDED,
        ("cooldown_desc", "cooldown_asc"),
    ),
    (
        "trust",
        "sort_cat_trust",
        ft.Icons.VERIFIED_USER_OUTLINED,
        ("trust_desc", "trust_asc"),
    ),
    ("age", "sort_cat_age", ft.Icons.EVENT_NOTE_ROUNDED, ("age_desc", "age_asc")),
    ("name", "sort_cat_name", ft.Icons.SORT_BY_ALPHA_ROUNDED, ("name_asc", "name_desc")),
    ("running", "sort_cat_running", ft.Icons.PLAY_CIRCLE_OUTLINE_ROUNDED, ("running",)),
)

_SORT_DIR_KEY_MAP = {
    "cooldown_desc": "sort_dir_cooldown_desc",
    "cooldown_asc": "sort_dir_cooldown_asc",
    "trust_desc": "sort_dir_trust_desc",
    "trust_asc": "sort_dir_trust_asc",
    "age_desc": "sort_dir_age_desc",
    "age_asc": "sort_dir_age_asc",
    "name_asc": "sort_dir_name_asc",
    "name_desc": "sort_dir_name_desc",
}

_SORT_FLIP_MAP = {
    "cooldown_desc": "cooldown_asc",
    "cooldown_asc": "cooldown_desc",
    "trust_desc": "trust_asc",
    "trust_asc": "trust_desc",
    "age_desc": "age_asc",
    "age_asc": "age_desc",
    "name_asc": "name_desc",
    "name_desc": "name_asc",
}

_VALID_SORT_KEYS = {m for _, _, _, modes in _SORT_CATEGORIES for m in modes}


def _build_sort_control(
    sort_by: str = "custom",
    on_sort: Callable[[str], None] | None = None,
) -> ft.Container:
    """Compact 40px sort pill with 7 grouped menu categories and 1-click direction toggle."""
    cur_mode = [sort_by if sort_by in _VALID_SORT_KEYS else "custom"]

    lead_icon = ft.Icon(ft.Icons.SWAP_VERT_ROUNDED, size=15, color=COLORS["text_sub"])
    main_label = ft.Text(
        "",
        size=12.5,
        font_family=FONT_MEDIUM,
        color=COLORS["text_main"],
        no_wrap=True,
    )
    dir_text = ft.Text(
        "",
        size=11,
        font_family=FONT_DATA,
        weight=ft.FontWeight.W_700,
        color=COLORS["accent"],
        no_wrap=True,
    )
    divider_line = ft.Container(
        width=1,
        height=20,
        bgcolor=COLORS["rule_strong"],
        visible=False,
    )
    dir_btn = ft.Container(
        height=38,
        padding=ft.Padding.symmetric(horizontal=9),
        bgcolor=ft.Colors.with_opacity(0.08, COLORS["accent"]),
        alignment=ft.Alignment(0, 0),
        ink=True,
        visible=False,
        tooltip=get_string("sort_flip_tooltip"),
        content=dir_text,
    )

    popup_btn = ft.PopupMenuButton(
        padding=0,
        bgcolor=COLORS["card_bg"],
        elevation=6,
        menu_position=ft.PopupMenuPosition.UNDER,
        menu_padding=ft.Padding.symmetric(vertical=4),
        shape=ft.RoundedRectangleBorder(
            radius=9,
            side=ft.BorderSide(1, COLORS["rule_strong"]),
        ),
        tooltip=get_string("sort_label"),
        content=ft.Container(
            height=38,
            padding=ft.Padding.only(left=11, right=8),
            alignment=ft.Alignment(0, 0),
            content=ft.Row(
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    lead_icon,
                    main_label,
                    ft.Icon(
                        ft.Icons.KEYBOARD_ARROW_DOWN_ROUNDED,
                        size=16,
                        color=COLORS["text_sub"],
                    ),
                ],
            ),
        ),
        items=[],
    )

    pill = ft.Container(
        height=40,
        border_radius=8,
        bgcolor=COLORS["input_bg"],
        border=ft.Border.all(1, COLORS["rule_strong"]),
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        content=ft.Row(
            spacing=0,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                popup_btn,
                divider_line,
                dir_btn,
            ],
        ),
    )

    def _sync_ui(val: str) -> None:
        mode = val if val in _VALID_SORT_KEYS else "custom"
        cur_mode[0] = mode
        pill.value = mode  # type: ignore[attr-defined]

        active_cat = _SORT_CATEGORIES[0]
        for cat in _SORT_CATEGORIES:
            if mode in cat[3]:
                active_cat = cat
                break

        _, title_key, icon_data, _ = active_cat
        is_custom = mode == "custom"
        lead_icon.icon = icon_data
        lead_icon.color = COLORS["text_sub"] if is_custom else COLORS["accent"]
        main_label.value = get_string(title_key)
        main_label.font_family = FONT_MEDIUM if is_custom else FONT_SEMIBOLD

        if mode in _SORT_FLIP_MAP:
            dir_lbl = get_string(_SORT_DIR_KEY_MAP[mode])
            dir_text.value = dir_lbl
            divider_line.visible = True
            dir_btn.visible = True
        else:
            divider_line.visible = False
            dir_btn.visible = False

        menu_items: list[ft.PopupMenuItem] = []
        for _cat_id, c_title_key, c_icon, c_modes in _SORT_CATEGORIES:
            is_act = mode in c_modes

            def _make_pick(modes_tuple: tuple[str, ...] = c_modes) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    cur = cur_mode[0]
                    if cur in modes_tuple and len(modes_tuple) > 1:
                        nxt = _SORT_FLIP_MAP.get(cur, modes_tuple[0])
                    else:
                        nxt = modes_tuple[0]
                    _sync_ui(nxt)
                    if on_sort is not None:
                        on_sort(nxt)

                return _cb

            menu_items.append(
                ft.PopupMenuItem(
                    height=34,
                    padding=ft.Padding.symmetric(horizontal=12, vertical=0),
                    on_click=_make_pick(),
                    content=ft.Container(
                        width=140,
                        content=ft.Row(
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Icon(
                                    c_icon,
                                    size=15,
                                    color=COLORS["accent"] if is_act else COLORS["text_sub"],
                                ),
                                ft.Text(
                                    get_string(c_title_key),
                                    size=12.5,
                                    font_family=FONT_SEMIBOLD if is_act else FONT_MEDIUM,
                                    color=COLORS["accent"] if is_act else COLORS["text_main"],
                                    expand=True,
                                    no_wrap=True,
                                ),
                                *(
                                    [
                                        ft.Icon(
                                            ft.Icons.CHECK_ROUNDED,
                                            size=14,
                                            color=COLORS["accent"],
                                        )
                                    ]
                                    if is_act
                                    else []
                                ),
                            ],
                        ),
                    ),
                )
            )
        popup_btn.items = menu_items

    def _flip_direction(_: ft.ControlEvent) -> None:
        nxt = _SORT_FLIP_MAP.get(cur_mode[0])
        if nxt:
            _sync_ui(nxt)
            if on_sort is not None:
                on_sort(nxt)

    dir_btn.on_click = _flip_direction
    pill.sync_value = _sync_ui  # type: ignore[attr-defined]
    _sync_ui(sort_by)
    return pill


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
    sort_dropdown = _build_sort_control(sort_by=sort_by, on_sort=on_sort)

    def _handle_reorder(e: ft.OnReorderEvent) -> None:
        if on_reorder is not None and e.old_index is not None and e.new_index is not None:
            on_reorder(e.old_index, e.new_index)

    search_clear_btn = ft.Container(
        width=22,
        height=22,
        border_radius=11,
        alignment=ft.Alignment(0, 0),
        ink=True,
        visible=False,
        content=ft.Icon(
            ft.Icons.CLOSE_ROUNDED,
            size=14,
            color=COLORS["text_sub"],
        ),
    )
    search_field = ft.TextField(
        hint_text=get_string("search_hint"),
        prefix_icon=ft.Icons.SEARCH,
        suffix=search_clear_btn,
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
    )

    def _handle_search_input(val: str) -> None:
        search_clear_btn.visible = bool(val.strip())
        on_search(val)

    def _clear_search(_e: object = None) -> None:
        search_field.value = ""
        search_clear_btn.visible = False
        on_search("")

    search_field.on_change = lambda e: _handle_search_input(e.control.value or "")
    search_field.on_submit = lambda e: _handle_search_input(e.control.value or "")
    search_clear_btn.on_click = _clear_search

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
        search_field=search_field,
        sort_dropdown=sort_dropdown,
    )
