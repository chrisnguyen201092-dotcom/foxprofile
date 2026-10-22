import datetime as dt
from collections.abc import Callable
from typing import Any

import flet as ft

from ...core.strings import get_string
from ..theme.colors import COLORS, FONT_BOLD, FONT_SEMIBOLD
from ..theme.styles import ERROR_STYLE, OUTLINE_STYLE


def open_trash_dialog(
    page: ft.Page,
    get_trashed: Callable[[], list[dict[str, Any]]],
    on_restore: Callable[[str], None],
    on_delete_forever: Callable[[str], None],
    on_empty_all: Callable[[], None],
    on_open_backups: Callable[[], None] | None = None,
) -> None:
    """Trash Bin dialog to restore or permanently delete soft-deleted profiles."""
    list_col = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, height=320)

    def _rebuild_rows() -> None:
        items = get_trashed()
        list_col.controls.clear()
        if not items:
            list_col.controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=40),
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("trash_empty"),
                        size=14,
                        color=COLORS["text_dim"],
                    ),
                )
            )
            return

        for item in items:
            pname = str(item.get("name", ""))
            proxy = str(item.get("proxy") or get_string("no_proxy_short"))
            deleted_ts = int(item.get("deleted_at") or 0)
            deleted_str = (
                dt.datetime.fromtimestamp(deleted_ts).strftime("%Y-%m-%d %H:%M")
                if deleted_ts > 0
                else ""
            )

            def _make_restore(n: str) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    on_restore(n)
                    _rebuild_rows()
                    page.update()

                return _cb

            def _make_del(n: str) -> Callable[[ft.ControlEvent], None]:
                def _cb(_: ft.ControlEvent) -> None:
                    on_delete_forever(n)
                    _rebuild_rows()
                    page.update()

                return _cb

            list_col.controls.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                    border_radius=8,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(1, COLORS["border"]),
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Column(
                                spacing=2,
                                expand=True,
                                controls=[
                                    ft.Text(
                                        pname,
                                        size=14,
                                        font_family=FONT_SEMIBOLD,
                                        color=COLORS["text_main"],
                                    ),
                                    ft.Text(
                                        f"{proxy}  ·  {deleted_str}".strip(" ·"),
                                        size=11,
                                        color=COLORS["text_dim"],
                                    ),
                                ],
                            ),
                            ft.Row(
                                spacing=6,
                                controls=[
                                    ft.OutlinedButton(
                                        get_string("trash_restore_btn"),
                                        icon=ft.Icons.RESTORE,
                                        height=34,
                                        style=OUTLINE_STYLE,
                                        on_click=_make_restore(pname),
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.DELETE_FOREVER,
                                        icon_size=18,
                                        icon_color=COLORS["error"],
                                        tooltip=get_string("trash_delete_forever_btn"),
                                        on_click=_make_del(pname),
                                    ),
                                ],
                            ),
                        ],
                    ),
                )
            )

    _rebuild_rows()

    def _handle_empty_all(_: ft.ControlEvent) -> None:
        on_empty_all()
        _rebuild_rows()
        page.update()

    def _switch_to_backups(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        if on_open_backups is not None:
            on_open_backups()

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(
                    get_string("trash_title"),
                    size=19,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                *(
                    [
                        ft.OutlinedButton(
                            get_string("backup_btn"),
                            icon=ft.Icons.HISTORY_ROUNDED,
                            height=32,
                            style=OUTLINE_STYLE,
                            on_click=_switch_to_backups,
                        )
                    ]
                    if on_open_backups is not None
                    else []
                ),
            ],
        ),
        content=ft.Container(
            width=540,
            content=ft.Column(
                tight=True,
                spacing=10,
                controls=[
                    ft.Text(
                        get_string("trash_subtitle"),
                        size=13,
                        color=COLORS["text_sub"],
                    ),
                    list_col,
                ],
            ),
        ),
        actions=[
            ft.Button(
                get_string("trash_empty_all_btn"),
                icon=ft.Icons.DELETE_SWEEP,
                style=ERROR_STYLE,
                on_click=_handle_empty_all,
            ),
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    page.show_dialog(dlg)
