from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IProfileManager
from ...utils.validation import validate_group
from ..theme.colors import COLORS, FONT_BOLD, FONT_MEDIUM, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, OUTLINE_STYLE


def open_custom_page_dialog(
    page: ft.Page,
    pm: IProfileManager,
    on_saved: Callable[[str], None],
    on_deleted: Callable[[], None] | None = None,
    editing_page: str | None = None,
    preselected_names: set[str] | None = None,
) -> None:
    """Dialog to create or edit a custom page and choose its member profiles."""
    is_edit = editing_page is not None
    all_profiles = pm.list_profiles()

    if is_edit:
        chosen: set[str] = {
            p.name
            for p in all_profiles
            if any(pg.casefold() == editing_page.casefold() for pg in p.pages)
        }
    else:
        chosen = set(preselected_names or set())

    name_field = ft.TextField(
        value=editing_page or "",
        hint_text=get_string("page_name_hint"),
        **DLG_FIELD_KWARGS,
    )
    filter_field = ft.TextField(
        hint_text=get_string("page_filter_profiles"),
        prefix_icon=ft.Icons.SEARCH,
        height=38,
        dense=True,
        border_radius=8,
        bgcolor=COLORS["input_bg"],
        color=COLORS["text_main"],
        text_size=13,
        border_color=COLORS["rule_strong"],
        focused_border_color=COLORS["sidebar"],
    )
    error_text = ft.Text("", size=12, color=COLORS["error"], visible=False)
    count_label = ft.Text(
        get_string("page_select_profiles", count=len(chosen)),
        size=12.5,
        font_family=FONT_SEMIBOLD,
        color=COLORS["text_sub"],
    )
    list_col = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO, height=420)

    def _filtered_profiles() -> list:
        q = (filter_field.value or "").strip().lower()
        if not q:
            return all_profiles
        return [
            p
            for p in all_profiles
            if q in p.name.lower() or q in (p.group or "").lower() or q in (p.notes or "").lower()
        ]

    def _toggle_profile(p_name: str, checked: bool) -> None:
        if checked:
            chosen.add(p_name)
        else:
            chosen.discard(p_name)
        count_label.value = get_string("page_select_profiles", count=len(chosen))
        page.update()

    def _rebuild_list() -> None:
        shown = _filtered_profiles()
        rows: list[ft.Control] = []
        for p in shown:
            is_checked = p.name in chosen
            row_items: list[ft.Control] = [
                ft.Checkbox(
                    value=is_checked,
                    on_change=lambda e, n=p.name: _toggle_profile(n, bool(e.control.value)),
                ),
                ft.Text(
                    p.name,
                    size=13.5,
                    font_family=FONT_MEDIUM,
                    color=COLORS["text_main"],
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    expand=True,
                ),
            ]
            if p.group:
                row_items.append(
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                        border_radius=4,
                        bgcolor=COLORS["input_bg"],
                        border=ft.Border.all(1, COLORS["rule_strong"]),
                        content=ft.Text(p.group, size=10.5, color=COLORS["text_sub"]),
                    )
                )
            rows.append(
                ft.Container(
                    height=34,
                    padding=ft.Padding.symmetric(horizontal=6),
                    border_radius=6,
                    content=ft.Row(
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=row_items,
                    ),
                )
            )
        list_col.controls = rows
        count_label.value = get_string("page_select_profiles", count=len(chosen))

    def _select_visible(select: bool) -> None:
        for p in _filtered_profiles():
            if select:
                chosen.add(p.name)
            else:
                chosen.discard(p.name)
        _rebuild_list()
        page.update()

    filter_field.on_change = lambda _: (_rebuild_list(), page.update())
    _rebuild_list()

    def _on_save(_: ft.ControlEvent) -> None:
        raw_name = (name_field.value or "").strip()
        error_text.visible = False
        if not raw_name:
            error_text.value = get_string("page_empty_name_error")
            error_text.visible = True
            page.update()
            return
        valid, err = validate_group(raw_name)
        if not valid:
            error_text.value = err
            error_text.visible = True
            page.update()
            return
        ordered_chosen = [p.name for p in all_profiles if p.name in chosen]
        if not pm.save_custom_page(raw_name, ordered_chosen, original_name=editing_page):
            error_text.value = get_string("page_exists_error")
            error_text.visible = True
            page.update()
            return
        page.pop_dialog()
        on_saved(raw_name)

    def _on_delete(_: ft.ControlEvent) -> None:
        if editing_page and pm.delete_custom_page(editing_page):
            page.pop_dialog()
            if on_deleted:
                on_deleted()

    actions: list[ft.Control] = []
    if is_edit:
        actions.append(
            ft.TextButton(
                get_string("delete_page_btn"),
                icon=ft.Icons.DELETE_OUTLINE,
                style=ft.ButtonStyle(color=COLORS["error"]),
                on_click=_on_delete,
            )
        )
    actions.extend(
        [
            ft.TextButton(
                get_string("cancel"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
            ft.Button(
                get_string("save_changes") if is_edit else get_string("new_page_btn"),
                icon=ft.Icons.SAVE if is_edit else ft.Icons.ADD,
                style=ACCENT_STYLE,
                on_click=_on_save,
            ),
        ]
    )

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Text(
            get_string("edit_page_title") if is_edit else get_string("create_page_title"),
            size=19,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=640,
            content=ft.Column(
                tight=True,
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Text(
                        get_string("page_name_label"),
                        size=12.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["text_sub"],
                    ),
                    name_field,
                    error_text,
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            count_label,
                            ft.Row(
                                spacing=4,
                                controls=[
                                    ft.TextButton(
                                        get_string("select_all"),
                                        on_click=lambda _: _select_visible(True),
                                    ),
                                    ft.TextButton(
                                        get_string("clear"),
                                        on_click=lambda _: _select_visible(False),
                                    ),
                                ],
                            ),
                        ],
                    ),
                    filter_field,
                    ft.Container(
                        border=ft.Border.all(1, COLORS["rule_strong"]),
                        border_radius=8,
                        bgcolor=COLORS["bg"],
                        padding=ft.Padding.symmetric(vertical=4, horizontal=4),
                        content=list_col,
                    ),
                ],
            ),
        ),
        actions=actions,
    )
    page.show_dialog(dlg)


def open_add_to_page_dialog(
    page: ft.Page,
    pm: IProfileManager,
    profile_names: list[str],
    on_done: Callable[[str], None],
) -> None:
    """Quick dialog from the Bulk Bar to add selected profiles to an existing or new page."""
    existing_pages = pm.list_custom_pages()
    new_page_field = ft.TextField(
        hint_text=get_string("page_name_hint"),
        **DLG_FIELD_KWARGS,
    )
    error_text = ft.Text("", size=12, color=COLORS["error"], visible=False)

    def _apply_page(target_page: str) -> None:
        clean = target_page.strip()
        if not clean:
            error_text.value = get_string("page_empty_name_error")
            error_text.visible = True
            page.update()
            return
        valid, err = validate_group(clean)
        if not valid:
            error_text.value = err
            error_text.visible = True
            page.update()
            return
        if pm.add_profiles_to_page(clean, profile_names):
            page.pop_dialog()
            on_done(clean)

    controls: list[ft.Control] = []
    if existing_pages:
        controls.append(
            ft.Text(
                get_string("pick_existing_page"),
                size=12.5,
                font_family=FONT_SEMIBOLD,
                color=COLORS["text_sub"],
            )
        )
        controls.append(
            ft.Row(
                spacing=8,
                run_spacing=8,
                wrap=True,
                controls=[
                    ft.OutlinedButton(
                        pg,
                        icon=ft.Icons.FOLDER_OPEN_OUTLINED,
                        height=34,
                        style=OUTLINE_STYLE,
                        on_click=lambda _, p_name=pg: _apply_page(p_name),
                    )
                    for pg in existing_pages
                ],
            )
        )

    controls.extend(
        [
            ft.Container(
                padding=ft.Padding.only(top=8 if existing_pages else 0),
                content=ft.Text(
                    get_string("or_create_new_page")
                    if existing_pages
                    else get_string("page_name_label"),
                    size=12.5,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_sub"],
                ),
            ),
            new_page_field,
            error_text,
        ]
    )

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Text(
            get_string("add_to_page_title", count=len(profile_names)),
            size=18,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=540,
            content=ft.Column(
                tight=True,
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=controls,
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("cancel"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
            ft.Button(
                get_string("add_btn"),
                icon=ft.Icons.ADD,
                style=ACCENT_STYLE,
                on_click=lambda _: _apply_page(new_page_field.value or ""),
            ),
        ],
    )
    page.show_dialog(dlg)
