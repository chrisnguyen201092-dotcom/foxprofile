from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...models.profile import Profile
from ..state import AppState
from ..theme.colors import COLORS, FONT_MEDIUM, FONT_SEMIBOLD

BulkCallbacks = dict[str, Callable[[], None]]

_BULK_SHAPE = ft.RoundedRectangleBorder(radius=8)
_BULK_PAD = ft.Padding.symmetric(horizontal=12, vertical=0)

_BULK_PRIMARY_STYLE = ft.ButtonStyle(
    shape=_BULK_SHAPE,
    side=ft.BorderSide(1, COLORS["accent"]),
    bgcolor={
        ft.ControlState.DEFAULT: COLORS["accent"],
        ft.ControlState.HOVERED: COLORS["accent_hover"],
    },
    color="#FFFFFF",
    icon_color="#FFFFFF",
    text_style=ft.TextStyle(font_family=FONT_SEMIBOLD, size=13),
    padding=_BULK_PAD,
    elevation=0,
)

_BULK_OUTLINE_STYLE = ft.ButtonStyle(
    shape=_BULK_SHAPE,
    side=ft.BorderSide(1, COLORS["rule_strong"]),
    bgcolor=COLORS["card_bg"],
    color=COLORS["text_main"],
    icon_color=COLORS["text_main"],
    text_style=ft.TextStyle(font_family=FONT_MEDIUM, size=13),
    padding=_BULK_PAD,
    elevation=0,
)


def rebuild_bulk_bar(
    bulk_bar: ft.Row,
    state: AppState,
    page_profiles: list[Profile],
    cbs: BulkCallbacks,
    active_page: str = "__all__",
) -> None:
    count = len(state.selected_names())
    if count == 0:
        bulk_bar.visible = False
        bulk_bar.controls = []
        return
    sel_indices = [
        i for i, p in enumerate(page_profiles) if state.is_selected(p.name)
    ]
    range_span = (
        (sel_indices[-1] - sel_indices[0] + 1)
        if len(sel_indices) >= 2
        else 0
    )
    range_count = range_span if range_span > len(sel_indices) else 0
    bulk_bar.visible = True
    bulk_bar.controls = [
        _build_container(count, cbs, active_page, range_count=range_count)
    ]


def _build_container(
    count: int,
    cbs: BulkCallbacks,
    active_page: str,
    *,
    range_count: int = 0,
) -> ft.Container:
    def btn(label: str, icon: ft.IconData, key: str, primary: bool = False) -> ft.Control:
        return ft.OutlinedButton(
            label,
            icon=icon,
            height=32,
            style=_BULK_PRIMARY_STYLE if primary else _BULK_OUTLINE_STYLE,
            on_click=lambda _: cbs[key](),
        )

    extra_btns: list[ft.Control] = []
    if "select_range" in cbs and range_count > 0:
        extra_btns.append(
            btn(
                get_string("bulk_select_range", count=range_count),
                ft.Icons.LOW_PRIORITY,
                "select_range",
            )
        )
    if "set_group" in cbs:
        extra_btns.append(
            btn(
                get_string("bulk_set_group"),
                ft.Icons.FOLDER_OUTLINED,
                "set_group",
            )
        )
    if "set_color" in cbs:
        extra_btns.append(
            btn(
                get_string("bulk_set_color"),
                ft.Icons.PALETTE_OUTLINED,
                "set_color",
            )
        )
    if "set_proxy" in cbs:
        extra_btns.append(
            btn(
                get_string("bulk_set_proxy"),
                ft.Icons.VPN_KEY_OUTLINED,
                "set_proxy",
            )
        )
    if "check_proxy" in cbs:
        extra_btns.append(
            btn(
                get_string("bulk_check_proxy"),
                ft.Icons.NETWORK_CHECK,
                "check_proxy",
            )
        )
    if "export_zip" in cbs:
        extra_btns.append(
            btn(
                get_string("export"),
                ft.Icons.FILE_UPLOAD_OUTLINED,
                "export_zip",
            )
        )
    if "add_to_page" in cbs:
        extra_btns.append(
            btn(
                get_string("bulk_add_to_page"),
                ft.Icons.BOOKMARK_ADD_OUTLINED,
                "add_to_page",
            )
        )
    if active_page != "__all__" and "remove_from_page" in cbs:
        extra_btns.append(
            btn(
                get_string("bulk_remove_from_page"),
                ft.Icons.REMOVE_CIRCLE_OUTLINE,
                "remove_from_page",
            )
        )

    return ft.Container(
        expand=True,
        bgcolor=COLORS["paper_sunk"],
        padding=ft.Padding.symmetric(horizontal=14, vertical=9),
        border=ft.Border.only(bottom=ft.BorderSide(1, COLORS["card_border"])),
        content=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(
                    get_string("selected_count", count=count),
                    size=13.5,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_main"],
                ),
                ft.Container(width=6),
                btn(get_string("launch"), ft.Icons.PLAY_ARROW_ROUNDED, "launch", primary=True),
                btn(get_string("stop"), ft.Icons.STOP_ROUNDED, "stop"),
                btn(get_string("delete"), ft.Icons.DELETE_OUTLINE, "delete"),
                *extra_btns,
                ft.Container(expand=True),
                ft.TextButton(
                    get_string("clear"),
                    on_click=lambda _: cbs["clear"](),
                    style=ft.ButtonStyle(color=COLORS["text_sub"]),
                ),
            ],
        ),
    )
