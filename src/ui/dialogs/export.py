from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...models.profile import Profile
from ..theme.colors import COLORS, FONT_BOLD
from ..theme.styles import ACCENT_STYLE, OUTLINE_STYLE


def open_export_dialog(
    page: ft.Page,
    file_picker: ft.FilePicker,
    profiles: list[Profile],
    on_complete: Callable[[list[str], str, bool], None],
    *,
    preselected_names: set[str] | None = None,
    on_export_csv: Callable[[list[str], str], None] | None = None,
) -> None:
    if not profiles:
        return

    presel = preselected_names or set()
    names = [p.name for p in profiles]
    checkboxes = [ft.Checkbox(label=n, value=(n in presel), shape=ft.CircleBorder()) for n in names]
    include_data_cb = ft.Checkbox(label=get_string("include_browser_data"), value=True)

    async def on_export(_: ft.ControlEvent) -> None:
        selected = [cb.label for cb in checkboxes if cb.value]
        if not selected:
            return
        include_data = bool(include_data_cb.value)
        page.pop_dialog()
        dir_path = await file_picker.get_directory_path(
            dialog_title=get_string("select_export_dir"),
        )
        if dir_path:
            on_complete(selected, dir_path, include_data)

    async def on_csv(_: ft.ControlEvent) -> None:
        if on_export_csv is None:
            return
        selected = [cb.label for cb in checkboxes if cb.value]
        if not selected:
            selected = list(names)
        page.pop_dialog()
        csv_path = await file_picker.save_file(
            dialog_title=get_string("csv_save_dialog_title"),
            file_name="foxprofile_profiles.csv",
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["csv"],
        )
        if csv_path:
            if not csv_path.lower().endswith(".csv"):
                csv_path = f"{csv_path}.csv"
            on_export_csv(selected, csv_path)

    def sync_select_all() -> None:
        select_all_cb.value = all(cb.value for cb in checkboxes)
        select_all_cb.tristate = any(cb.value for cb in checkboxes) and not all(
            cb.value for cb in checkboxes
        )

    def on_profile_change(_: ft.ControlEvent) -> None:
        sync_select_all()
        page.update()

    for cb in checkboxes:
        cb.on_change = on_profile_change

    def toggle_all(e: ft.ControlEvent) -> None:
        target = e.control.value
        if target is None:
            target = True
        for cb in checkboxes:
            cb.value = target
        select_all_cb.value = target
        select_all_cb.tristate = False
        page.update()

    select_all_cb = ft.Checkbox(
        label=get_string("select_all"),
        value=bool(checkboxes) and all(cb.value for cb in checkboxes),
        tristate=any(cb.value for cb in checkboxes) and not all(cb.value for cb in checkboxes),
        shape=ft.CircleBorder(),
        on_change=toggle_all,
    )

    actions: list[ft.Control] = [
        ft.TextButton(
            get_string("cancel"),
            style=ft.ButtonStyle(color=COLORS["text_sub"]),
            on_click=lambda _: page.pop_dialog(),
        ),
    ]
    if on_export_csv is not None:
        actions.append(
            ft.OutlinedButton(
                get_string("csv_export_btn"),
                icon=ft.Icons.TABLE_CHART_OUTLINED,
                style=OUTLINE_STYLE,
                on_click=on_csv,
            )
        )
    actions.append(
        ft.Button(
            get_string("export"),
            icon=ft.Icons.UPLOAD,
            style=ACCENT_STYLE,
            on_click=on_export,
        )
    )

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Text(
            get_string("export_profiles"),
            size=19,
            font_family=FONT_BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=600,
            content=ft.Column(
                tight=True,
                spacing=8,
                controls=[
                    ft.Text(
                        get_string("choose_profiles_to_export"),
                        size=12.5,
                        color=COLORS["text_sub"],
                    ),
                    ft.Container(height=2),
                    select_all_cb,
                    ft.Divider(height=1, color=COLORS["border"]),
                    ft.Column(
                        spacing=4,
                        height=min(460, max(120, len(checkboxes) * 34)),
                        scroll=ft.ScrollMode.AUTO,
                        controls=checkboxes,
                    ),
                    ft.Container(height=4),
                    include_data_cb,
                ],
            ),
        ),
        actions=actions,
    )
    page.show_dialog(dlg)
