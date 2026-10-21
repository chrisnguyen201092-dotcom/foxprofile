from collections.abc import Awaitable, Callable

import flet as ft

from ...core.strings import get_string
from ..theme.colors import COLORS
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, OUTLINE_STYLE


def open_cookie_dialog(
    page: ft.Page,
    profile_name: str,
    fingerprint_line: str,
    on_export: Callable[[str], Awaitable[None]],
    on_import: Callable[[], Awaitable[None]],
    on_reset_fingerprint: Callable[[], None],
    *,
    is_running: bool = False,
    cookie_summary_line: str = "",
    on_copy_clipboard: Callable[[], Awaitable[None]] | None = None,
    on_paste_clipboard: Callable[[], Awaitable[None]] | None = None,
    on_import_text: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Dialog for cookie export/import (live or offline), clipboard paste, and fingerprint reset."""

    def action_row(
        label: str,
        hint: str,
        icon: str,
        handler: Callable[[ft.ControlEvent], object],
        disabled: bool = False,
    ) -> ft.Row:
        return ft.Row(
            spacing=14,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.OutlinedButton(
                    label,
                    icon=icon,
                    width=205,
                    height=38,
                    style=OUTLINE_STYLE,
                    on_click=handler,
                    disabled=disabled,
                ),
                ft.Text(hint, size=12, color=COLORS["text_dim"], expand=True),
            ],
        )

    async def export_json(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        await on_export("json")

    async def export_netscape(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        await on_export("netscape")

    async def copy_json(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        if on_copy_clipboard:
            await on_copy_clipboard()

    async def paste_clip(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        if on_paste_clipboard:
            await on_paste_clipboard()

    async def do_import(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        await on_import()

    paste_field = ft.TextField(
        label=get_string("cookie_paste_box_label"),
        hint_text=get_string("cookie_paste_box_hint"),
        multiline=True,
        min_lines=2,
        max_lines=4,
        text_size=12,
        **DLG_FIELD_KWARGS,
    )

    async def do_import_text(_: ft.ControlEvent) -> None:
        raw = (paste_field.value or "").strip()
        if not raw:
            return
        page.pop_dialog()
        if on_import_text:
            await on_import_text(raw)

    def reset_fp(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        on_reset_fingerprint()

    hint_key = "cookie_dialog_live_hint" if is_running else "cookie_dialog_hint"
    controls: list[ft.Control] = [
        ft.Text(
            get_string(hint_key, name=profile_name),
            size=13,
            color=COLORS["success"] if is_running else COLORS["text_sub"],
        ),
    ]
    if cookie_summary_line:
        controls.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                border_radius=8,
                bgcolor=COLORS["input_bg"],
                border=ft.Border.all(1, COLORS["border"]),
                content=ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(ft.Icons.COOKIE_OUTLINED, size=16, color=COLORS["accent"]),
                        ft.Text(
                            cookie_summary_line,
                            size=12,
                            color=COLORS["text_main"],
                            expand=True,
                        ),
                    ],
                ),
            )
        )

    if on_copy_clipboard is not None:
        controls.append(
            action_row(
                get_string("cookie_copy_json"),
                get_string("cookie_copy_json_hint"),
                ft.Icons.CONTENT_COPY,
                copy_json,
            )
        )
    if on_paste_clipboard is not None:
        controls.append(
            action_row(
                get_string("cookie_paste_clipboard"),
                get_string("cookie_paste_clipboard_hint"),
                ft.Icons.CONTENT_PASTE_GO,
                paste_clip,
            )
        )

    controls.extend(
        [
            action_row(
                get_string("cookie_export_json"),
                get_string("cookie_export_json_hint"),
                ft.Icons.DATA_OBJECT,
                export_json,
            ),
            action_row(
                get_string("cookie_export_netscape"),
                get_string("cookie_export_netscape_hint"),
                ft.Icons.DESCRIPTION,
                export_netscape,
            ),
            action_row(
                get_string("cookie_import"),
                get_string("cookie_import_hint"),
                ft.Icons.FILE_OPEN,
                do_import,
            ),
        ]
    )

    if on_import_text is not None:
        controls.append(
            ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Container(content=paste_field, expand=True),
                    ft.Button(
                        get_string("cookie_paste_import_btn"),
                        icon=ft.Icons.DOWNLOAD_DONE,
                        height=40,
                        style=ACCENT_STYLE,
                        on_click=do_import_text,
                    ),
                ],
            )
        )

    controls.extend(
        [
            ft.Divider(height=14, color=COLORS["border"]),
            ft.Text(fingerprint_line, size=12, color=COLORS["text_sub"]),
            action_row(
                get_string("fingerprint_reset"),
                get_string("fingerprint_reset_hint"),
                ft.Icons.FINGERPRINT,
                reset_fp,
                disabled=is_running,
            ),
        ]
    )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Text(
            get_string("cookie_dialog_title"),
            size=20,
            weight=ft.FontWeight.BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=560,
            content=ft.Column(
                tight=True,
                spacing=10,
                controls=controls,
            ),
        ),
        actions=[
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    page.show_dialog(dlg)
