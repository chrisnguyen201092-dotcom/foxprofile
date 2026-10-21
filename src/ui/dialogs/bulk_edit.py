from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...models.profile import PROFILE_COLORS
from ..theme.colors import COLORS
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, OUTLINE_STYLE


def open_bulk_group_dialog(
    page: ft.Page,
    count: int,
    existing_groups: list[str],
    on_apply: Callable[[str | None], None],
) -> None:
    """Dialog to assign or clear a group for all selected profiles."""
    group_field = ft.TextField(
        label=get_string("group_optional"),
        hint_text=get_string("group_hint"),
        **DLG_FIELD_KWARGS,
    )

    def _apply(val: str | None) -> None:
        page.pop_dialog()
        on_apply(val)

    chips: list[ft.Control] = [
        ft.OutlinedButton(
            get_string("bulk_clear_group"),
            icon=ft.Icons.CLEAR,
            height=32,
            style=OUTLINE_STYLE,
            on_click=lambda _: _apply(None),
        )
    ]
    for grp in existing_groups:
        chips.append(
            ft.OutlinedButton(
                grp,
                icon=ft.Icons.FOLDER_OUTLINED,
                height=32,
                style=OUTLINE_STYLE,
                on_click=lambda _, g=grp: _apply(g),
            )
        )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Text(
            get_string("bulk_group_title", count=count),
            size=19,
            weight=ft.FontWeight.BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=440,
            content=ft.Column(
                tight=True,
                spacing=12,
                controls=[
                    group_field,
                    ft.Row(wrap=True, spacing=8, run_spacing=8, controls=chips),
                ],
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("cancel"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
            ft.Button(
                get_string("save_changes"),
                icon=ft.Icons.CHECK,
                style=ACCENT_STYLE,
                on_click=lambda _: _apply((group_field.value or "").strip() or None),
            ),
        ],
    )
    page.show_dialog(dlg)


def open_bulk_color_dialog(
    page: ft.Page,
    count: int,
    on_apply: Callable[[str | None], None],
) -> None:
    """Dialog to assign or clear a color tag for all selected profiles."""

    def _pick(color_name: str | None) -> None:
        page.pop_dialog()
        on_apply(color_name)

    swatches: list[ft.Control] = [
        ft.GestureDetector(
            on_tap=lambda _: _pick(None),
            content=ft.Container(
                width=36,
                height=36,
                border_radius=18,
                bgcolor=COLORS["input_bg"],
                border=ft.Border.all(1, COLORS["border"]),
                alignment=ft.Alignment(0, 0),
                tooltip=get_string("color_default"),
                content=ft.Icon(ft.Icons.CLOSE, size=16, color=COLORS["text_sub"]),
            ),
        )
    ]
    for c_name in PROFILE_COLORS:
        swatches.append(
            ft.GestureDetector(
                on_tap=lambda _, cn=c_name: _pick(cn),
                content=ft.Container(
                    width=32,
                    height=32,
                    border_radius=16,
                    bgcolor=c_name,
                    tooltip=c_name,
                ),
            )
        )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Text(
            get_string("bulk_color_title", count=count),
            size=19,
            weight=ft.FontWeight.BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=400,
            padding=ft.Padding.symmetric(vertical=8),
            content=ft.Row(
                wrap=True,
                spacing=12,
                run_spacing=12,
                controls=swatches,
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("cancel"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    page.show_dialog(dlg)
