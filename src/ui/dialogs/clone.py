from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...utils.validation import validate_profile_name
from ..theme.colors import COLORS
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS


def open_clone_dialog(
    page: ft.Page,
    source_name: str,
    existing_names: set[str],
    on_clone: Callable[[str, bool], None],
) -> None:
    """Dialog to clone a profile's settings with a fresh device fingerprint."""
    idx = 1
    lower_set = {k.casefold() for k in existing_names}
    default_name = f"{source_name} - Copy"
    while default_name.casefold() in lower_set:
        idx += 1
        default_name = f"{source_name} - Copy {idx}"

    name_field = ft.TextField(
        label=get_string("clone_new_name_label"),
        value=default_name,
        **DLG_FIELD_KWARGS,
    )
    keep_proxy_cb = ft.Checkbox(
        label=get_string("clone_keep_proxy"),
        value=True,
        fill_color={ft.ControlState.SELECTED: COLORS["accent"]},
    )
    error_text = ft.Text("", size=12, color=COLORS["error"], visible=False)

    def _submit(_: ft.ControlEvent) -> None:
        new_name = (name_field.value or "").strip()
        ok, err = validate_profile_name(new_name)
        if not ok:
            error_text.value = err
            error_text.visible = True
            page.update()
            return
        if new_name.casefold() in lower_set:
            error_text.value = get_string("profile_exists")
            error_text.visible = True
            page.update()
            return
        page.pop_dialog()
        on_clone(new_name, bool(keep_proxy_cb.value))

    name_field.on_submit = _submit

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        title=ft.Text(
            get_string("clone_profile_title"),
            size=20,
            weight=ft.FontWeight.BOLD,
            color=COLORS["text_main"],
        ),
        content=ft.Container(
            width=440,
            content=ft.Column(
                tight=True,
                spacing=12,
                controls=[
                    ft.Text(
                        get_string("clone_profile_subtitle", name=source_name),
                        size=13,
                        color=COLORS["text_sub"],
                    ),
                    name_field,
                    keep_proxy_cb,
                    error_text,
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
                get_string("clone_btn"),
                icon=ft.Icons.CONTENT_COPY,
                style=ACCENT_STYLE,
                on_click=_submit,
            ),
        ],
    )
    page.show_dialog(dlg)
