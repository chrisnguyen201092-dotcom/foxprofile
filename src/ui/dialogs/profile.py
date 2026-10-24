import contextlib
import inspect
import threading
from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IProxyService
from ...models.profile import PROFILE_COLOR_PALETTES, Profile
from ...services.monitor import extract_proxy_port
from ...services.proxy.geo_check import GeoCheckResult, check_geo
from ...utils.proxy_parser import mask_proxy
from ...utils.totp import (
    fetch_hotmail_messages,
    format_mail_credentials_line,
    format_oauth_reader_line,
    generate_totp,
    parse_mail_credentials_line,
)
from ...utils.validation import (
    validate_color,
    validate_email,
    validate_group,
    validate_locale,
    validate_mail_token,
    validate_notes,
    validate_password,
    validate_profile_name,
    validate_proxy_format,
    validate_timezone,
    validate_two_fa,
)
from ...utils.warmup_sites import (
    DEFAULT_WARMUP_TEXT,
    load_warmup_text,
    parse_warmup_groups,
    save_warmup_text,
)
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_MEDIUM, FONT_SEMIBOLD
from ..theme.page import build_engine_dropdown, build_os_dropdown
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, OUTLINE_STYLE


def open_profile_dialog(
    page: ft.Page,
    proxy_service: IProxyService,
    on_save: Callable[..., str | None],
    profile: Profile | None = None,
    existing_groups: list[str] | None = None,
    on_batch_create: Callable[[], None] | None = None,
    unassigned_proxies: list[str] | None = None,
) -> None:
    is_edit = profile is not None
    title = get_string("edit_profile") if is_edit else get_string("create_new_profile")
    subtitle = (
        get_string("editing_profile", name=profile.name)
        if profile is not None
        else get_string("configure_identity")
    )
    save_label = get_string("save_changes") if is_edit else get_string("create_profile_btn")
    save_icon = ft.Icons.SAVE if is_edit else ft.Icons.ADD

    name_field = ft.TextField(
        value=profile.name if profile is not None else "",
        hint_text="" if profile is not None else get_string("enter_profile_name"),
        **DLG_FIELD_KWARGS,
    )
    existing_email = (getattr(profile, "email", None) or "") if profile is not None else ""
    existing_recovery_email = (
        (getattr(profile, "recovery_email", None) or "") if profile is not None else ""
    )
    existing_mail_token = (
        (getattr(profile, "mail_token", None) or "") if profile is not None else ""
    )
    password_field = ft.TextField(
        value=(getattr(profile, "password", None) or "") if profile is not None else "",
        hint_text=get_string("password_hint"),
        password=True,
        can_reveal_password=True,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    two_fa_field = ft.TextField(
        value=(getattr(profile, "two_fa", None) or "") if profile is not None else "",
        hint_text=get_string("two_fa_hint"),
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    group_field = ft.TextField(
        value=(profile.group or "") if profile is not None else "",
        hint_text=get_string("group_hint"),
        **DLG_FIELD_KWARGS,
    )
    notes_field = ft.TextField(
        value=(profile.notes or "") if profile is not None else "",
        hint_text=get_string("notes_hint"),
        multiline=True,
        min_lines=1,
        max_lines=2,
        **DLG_FIELD_KWARGS,
    )

    def _pick_group(g: str) -> None:
        group_field.value = g
        page.update()

    group_chips_row: ft.Control | None = None
    if existing_groups:
        group_chips_row = ft.Row(
            spacing=6,
            wrap=True,
            run_spacing=6,
            controls=[
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                    border_radius=5,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(1, COLORS["rule_strong"]),
                    ink=True,
                    on_click=lambda _, g=grp: _pick_group(g),
                    content=ft.Text(
                        grp,
                        size=11.5,
                        font_family=FONT_MEDIUM,
                        color=COLORS["text_sub"],
                    ),
                )
                for grp in existing_groups[:8]
            ],
        )

    proxy_field = ft.TextField(
        value=(profile.proxy or "") if profile is not None else "",
        hint_text="socks5://user:pass@host:port",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )

    def _pick_spare_proxy(px: str) -> None:
        proxy_field.value = px
        page.update()

    spare_proxies_row: ft.Control | None = None
    if unassigned_proxies:
        spare_proxies_row = ft.Column(
            spacing=4,
            tight=True,
            controls=[
                ft.Text(
                    get_string("profile_pick_spare_proxy", count=len(unassigned_proxies)),
                    size=11.5,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_sub"],
                ),
                ft.Row(
                    spacing=6,
                    wrap=True,
                    run_spacing=6,
                    controls=[
                        ft.Container(
                            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                            border_radius=5,
                            bgcolor=COLORS["input_bg"],
                            border=ft.Border.all(1, COLORS["accent"]),
                            ink=True,
                            tooltip=mask_proxy(sp),
                            on_click=lambda _, s=sp: _pick_spare_proxy(s),
                            content=ft.Text(
                                extract_proxy_port(sp) or mask_proxy(sp),
                                size=11.5,
                                font_family=FONT_DATA,
                                color=COLORS["accent"],
                            ),
                        )
                        for sp in unassigned_proxies[:10]
                    ],
                ),
            ],
        )
    os_dropdown = build_os_dropdown(
        profile.os_type if profile is not None else "windows",
    )
    os_dropdown.expand = 2
    # Fixed once created: the engines keep incompatible browser data.
    engine_dropdown = build_engine_dropdown(
        profile.engine if profile is not None else "camoufox",
        disabled=is_edit,
    )
    engine_dropdown.expand = 3
    engine_note = ft.Container(
        visible=engine_dropdown.value == "chrome" and not is_edit,
        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        border_radius=8,
        bgcolor=_WARN_TINT,
        content=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=16, color=COLORS["warning"]),
                ft.Text(
                    get_string("engine_chrome_note"),
                    size=12.5,
                    color=COLORS["warn_text"],
                    expand=True,
                ),
            ],
        ),
    )

    def on_engine_change(_: ft.ControlEvent) -> None:
        engine_note.visible = engine_dropdown.value == "chrome" and not is_edit
        page.update()

    engine_dropdown.on_select = on_engine_change
    timezone_field = ft.TextField(
        value=(profile.timezone or "") if profile is not None else "",
        hint_text="Asia/Ho_Chi_Minh",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    locale_field = ft.TextField(
        value=(profile.locale or "") if profile is not None else "",
        hint_text="vi-VN",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )

    selected_color: list[str | None] = [profile.color if profile is not None else None]
    palettes_open: list[bool] = [False]
    color_section = ft.Column(spacing=8, tight=True)

    def _make_swatch(hex_c: str, cur: str | None, size: int = 24) -> ft.Container:
        active = (cur or "").upper() == hex_c.upper()
        return ft.Container(
            width=size,
            height=size,
            border_radius=size // 2,
            bgcolor=hex_c,
            border=ft.Border.all(2.5, COLORS["text_main"]) if active else None,
            alignment=ft.Alignment(0, 0),
            tooltip=hex_c,
            on_click=lambda _, c=hex_c: _pick_color(c),
            content=ft.Icon(ft.Icons.CHECK, size=13, color="#FFFFFF") if active else None,
        )

    def _pick_color(c: str | None) -> None:
        if c is not None and selected_color[0] and selected_color[0].upper() == c.upper():
            selected_color[0] = None
        else:
            selected_color[0] = c
        _rebuild_color_section()
        page.update()

    def _toggle_palettes(_: ft.ControlEvent) -> None:
        palettes_open[0] = not palettes_open[0]
        _rebuild_color_section()
        page.update()

    def _on_hex_submit(e: ft.ControlEvent) -> None:
        raw = (e.control.value or "").strip()
        if not raw:
            return
        if not raw.startswith("#"):
            raw = f"#{raw}"
        ok, _ = validate_color(raw)
        if ok:
            selected_color[0] = raw.upper()
            _rebuild_color_section()
            page.update()

    def _rebuild_color_section() -> None:
        cur = selected_color[0]
        is_def = cur is None
        quick_colors = PROFILE_COLOR_PALETTES[0][1]
        extra_colors = [c for _, grp in PROFILE_COLOR_PALETTES[1:] for c in grp]
        cur_in_extra = bool(cur and cur.upper() not in {c.upper() for c in quick_colors})
        top_controls: list[ft.Control] = [
            ft.Container(
                height=28,
                padding=ft.Padding.symmetric(horizontal=9),
                border_radius=6,
                bgcolor=COLORS["sidebar"] if is_def else COLORS["card_bg"],
                border=ft.Border.all(1, COLORS["sidebar"] if is_def else COLORS["rule_strong"]),
                alignment=ft.Alignment(0, 0),
                on_click=lambda _: _pick_color(None),
                content=ft.Text(
                    get_string("color_default"),
                    size=11.5,
                    font_family=FONT_MEDIUM,
                    color=COLORS["cover_ink"] if is_def else COLORS["text_main"],
                ),
            ),
            *[_make_swatch(c, cur, size=22) for c in quick_colors],
            ft.Container(
                height=28,
                padding=ft.Padding.symmetric(horizontal=8),
                border_radius=6,
                bgcolor=COLORS["input_bg"] if palettes_open[0] else COLORS["card_bg"],
                border=ft.Border.all(
                    1.5 if palettes_open[0] or cur_in_extra else 1,
                    COLORS["sidebar"] if palettes_open[0] or cur_in_extra else COLORS["rule_strong"],
                ),
                alignment=ft.Alignment(0, 0),
                on_click=_toggle_palettes,
                content=ft.Row(
                    spacing=4,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Container(
                            width=11,
                            height=11,
                            border_radius=6,
                            bgcolor=cur,
                            border=ft.Border.all(1, COLORS["rule_strong"]),
                        )
                        if cur_in_extra
                        else ft.Icon(ft.Icons.PALETTE_OUTLINED, size=14, color=COLORS["text_main"]),
                        ft.Text(
                            cur.upper() if cur_in_extra else get_string("color_more_palettes"),
                            size=11.5,
                            font_family=FONT_DATA if cur_in_extra else FONT_MEDIUM,
                            color=COLORS["text_main"],
                        ),
                        ft.Icon(
                            ft.Icons.EXPAND_LESS if palettes_open[0] else ft.Icons.EXPAND_MORE,
                            size=15,
                            color=COLORS["text_sub"],
                        ),
                    ],
                ),
            ),
        ]

        children: list[ft.Control] = [
            ft.Row(
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=top_controls,
            )
        ]

        if palettes_open[0]:
            children.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                    border_radius=8,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(1, COLORS["rule_strong"]),
                    content=ft.Column(
                        spacing=6,
                        tight=True,
                        controls=[
                            ft.Row(
                                spacing=6,
                                wrap=True,
                                run_spacing=6,
                                controls=[_make_swatch(c, cur, size=22) for c in extra_colors],
                            ),
                            ft.Row(
                                spacing=6,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=[
                                    ft.Text(
                                        get_string("color_custom_hex"),
                                        size=11,
                                        font_family=FONT_MEDIUM,
                                        color=COLORS["text_sub"],
                                    ),
                                    ft.Container(
                                        width=18,
                                        height=18,
                                        border_radius=9,
                                        bgcolor=cur or COLORS["card_bg"],
                                        border=ft.Border.all(1, COLORS["rule_strong"]),
                                    ),
                                    ft.Container(
                                        width=96,
                                        height=26,
                                        padding=ft.Padding.symmetric(horizontal=8),
                                        border_radius=5,
                                        bgcolor=COLORS["card_bg"],
                                        border=ft.Border.all(1, COLORS["rule_strong"]),
                                        alignment=ft.Alignment(0, 0),
                                        content=ft.TextField(
                                            value=cur or "",
                                            hint_text="#RRGGBB",
                                            border=ft.InputBorder.NONE,
                                            collapsed=True,
                                            dense=True,
                                            text_size=11.5,
                                            text_style=_DATA,
                                            color=COLORS["text_main"],
                                            cursor_color=COLORS["accent"],
                                            hint_style=ft.TextStyle(
                                                color=COLORS["text_dim"], size=11
                                            ),
                                            on_submit=_on_hex_submit,
                                            on_blur=_on_hex_submit,
                                        ),
                                    ),
                                ],
                            ),
                        ],
                    ),
                )
            )

        color_section.controls = children

    _rebuild_color_section()

    selected_status: list[str | None] = [profile.status if profile is not None else None]

    geo_error = ft.Text("", size=12, color=COLORS["error"], visible=False)
    # The Check IP report is the only part that can grow; it scrolls on its own
    # so the dialog keeps its height on short windows.
    geo_result = ft.Column(spacing=4, visible=False, scroll=ft.ScrollMode.AUTO)
    name_error = ft.Text("", size=12, color=COLORS["error"], visible=False)
    proxy_error = ft.Text("", size=12, color=COLORS["error"], visible=False)
    check_btn = ft.OutlinedButton(
        get_string("check_proxy"),
        icon=ft.Icons.WIFI_FIND,
        height=38,
        style=OUTLINE_STYLE,
    )

    ip_btn = ft.OutlinedButton(
        get_string("check_ip"),
        icon=ft.Icons.PUBLIC,
        height=38,
        style=OUTLINE_STYLE,
    )
    ip_btn.on_click = lambda _: _do_ip_check(
        page, proxy_field, timezone_field, locale_field, geo_result, ip_btn
    )

    check_btn.on_click = lambda _: _do_proxy_check(
        page,
        proxy_field,
        proxy_error,
        check_btn,
        proxy_service,
    )

    def on_submit(_: ft.ControlEvent) -> None:
        name = (name_field.value or "").strip()
        email = existing_email
        password = (password_field.value or "").strip()
        two_fa = (two_fa_field.value or "").strip()
        recovery_email = existing_recovery_email
        group = (group_field.value or "").strip()
        notes = (notes_field.value or "").strip()
        proxy = (proxy_field.value or "").strip()
        os_type = os_dropdown.value or "windows"
        engine = engine_dropdown.value or "camoufox"
        timezone = (timezone_field.value or "").strip()
        locale = (locale_field.value or "").strip()
        name_error.visible = proxy_error.visible = geo_error.visible = False

        valid_name, name_err = validate_profile_name(name)
        if not valid_name:
            name_error.value = name_err
            name_error.visible = True
            page.update()
            return

        for validate_meta, meta_val in (
            (validate_password, password),
            (validate_two_fa, two_fa),
            (validate_group, group),
            (validate_notes, notes),
        ):
            valid_meta, meta_err = validate_meta(meta_val)
            if not valid_meta:
                name_error.value = meta_err
                name_error.visible = True
                page.update()
                return

        valid_proxy, proxy_err = validate_proxy_format(proxy)
        if not valid_proxy:
            proxy_error.value = proxy_err
            proxy_error.visible = True
            page.update()
            return

        for validate, value in ((validate_timezone, timezone), (validate_locale, locale)):
            valid, geo_err = validate(value)
            if not valid:
                geo_error.value = geo_err
                geo_error.visible = True
                page.update()
                return

        sig = inspect.signature(on_save)
        has_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()
        )
        if has_varargs or len(sig.parameters) >= 15:
            error = on_save(
                name,
                proxy,
                os_type,
                timezone,
                locale,
                engine,
                selected_color[0],
                group or None,
                notes or None,
                selected_status[0],
                email or None,
                password or None,
                two_fa or None,
                recovery_email or None,
                existing_mail_token or None,
            )
        elif len(sig.parameters) >= 14:
            error = on_save(
                name,
                proxy,
                os_type,
                timezone,
                locale,
                engine,
                selected_color[0],
                group or None,
                notes or None,
                selected_status[0],
                email or None,
                password or None,
                two_fa or None,
                recovery_email or None,
            )
        elif len(sig.parameters) >= 13:
            error = on_save(
                name,
                proxy,
                os_type,
                timezone,
                locale,
                engine,
                selected_color[0],
                group or None,
                notes or None,
                selected_status[0],
                email or None,
                password or None,
                two_fa or None,
            )
        elif len(sig.parameters) >= 11:
            error = on_save(
                name,
                proxy,
                os_type,
                timezone,
                locale,
                engine,
                selected_color[0],
                group or None,
                notes or None,
                selected_status[0],
                email or None,
            )
        elif len(sig.parameters) >= 10:
            error = on_save(
                name,
                proxy,
                os_type,
                timezone,
                locale,
                engine,
                selected_color[0],
                group or None,
                notes or None,
                selected_status[0],
            )
        else:
            error = on_save(
                name,
                proxy,
                os_type,
                timezone,
                locale,
                engine,
                selected_color[0],
                group or None,
                notes or None,
            )
        if error:
            name_error.value = error
            name_error.visible = True
            page.update()
        else:
            page.pop_dialog()

    engine_dropdown.label = None
    os_dropdown.label = None
    for dropdown in (engine_dropdown, os_dropdown):
        # Text fields keep Material's 48px height so rows of fields and
        # dropdowns (which cannot go below 48px) line up.
        dropdown.text_size = 14

    def _switch_to_batch(_: ft.ControlEvent) -> None:
        page.pop_dialog()
        if on_batch_create is not None:
            on_batch_create()

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=22, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=4, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=20),
        title=ft.Column(
            spacing=4,
            tight=True,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(title, size=19, font_family=FONT_BOLD, color=COLORS["text_main"]),
                        *(
                            [
                                ft.OutlinedButton(
                                    get_string("batch_create_btn"),
                                    icon=ft.Icons.PLAYLIST_ADD,
                                    height=32,
                                    style=OUTLINE_STYLE,
                                    on_click=_switch_to_batch,
                                )
                            ]
                            if not is_edit and on_batch_create is not None
                            else []
                        ),
                    ],
                ),
                ft.Text(subtitle, size=13.5, color=COLORS["text_sub"]),
            ],
        ),
        content=ft.Container(
            width=620,
            content=ft.Column(
                tight=True,
                spacing=4,
                scroll=ft.ScrollMode.AUTO,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Row(
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        controls=[
                            ft.Container(
                                expand=3,
                                content=_field(get_string("profile_name"), name_field),
                            ),
                            ft.Container(
                                expand=2,
                                content=_field(get_string("group_optional"), group_field),
                            ),
                        ],
                    ),
                    *([group_chips_row] if group_chips_row is not None else []),
                    name_error,
                    ft.Row(
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        controls=[
                            ft.Container(
                                expand=2,
                                content=_field(
                                    get_string("muse_password_optional"),
                                    password_field,
                                ),
                            ),
                            ft.Container(
                                expand=3,
                                content=_field(
                                    get_string("muse_two_fa_optional"),
                                    two_fa_field,
                                ),
                            ),
                        ],
                    ),
                    _field(get_string("notes_optional"), notes_field),
                    _field(get_string("profile_color"), color_section),
                    ft.Row(
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        controls=[
                            ft.Container(
                                expand=3,
                                content=_field(
                                    get_string("browser_engine"),
                                    engine_dropdown,
                                    get_string("engine_locked") if is_edit else None,
                                ),
                            ),
                            ft.Container(
                                expand=2,
                                content=_field(get_string("operating_system"), os_dropdown),
                            ),
                        ],
                    ),
                    engine_note,
                    _field(
                        get_string("proxy_optional"), proxy_field, get_string("proxy_format_hint")
                    ),
                    *([spare_proxies_row] if spare_proxies_row is not None else []),
                    ft.Row(spacing=8, controls=[check_btn, ip_btn]),
                    proxy_error,
                    ft.Row(
                        spacing=12,
                        controls=[
                            ft.Container(
                                expand=3,
                                content=_field(get_string("timezone_optional"), timezone_field),
                            ),
                            ft.Container(
                                expand=2,
                                content=_field(get_string("locale_optional"), locale_field),
                            ),
                        ],
                    ),
                    ft.Text(get_string("geo_follow_hint"), size=12, color=COLORS["text_dim"]),
                    geo_error,
                    geo_result,
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
                save_label,
                icon=save_icon,
                style=ACCENT_STYLE,
                on_click=on_submit,
            ),
        ],
    )
    page.show_dialog(dlg)


_DATA = ft.TextStyle(font_family=FONT_DATA, size=13)
_WARN_TINT = COLORS["warn_tint"]


def _field(label: str, control: ft.Control, hint: str | None = None) -> ft.Column:
    """A label set above its field, with an optional hint below, as in the web panel."""
    return ft.Column(
        spacing=4,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Container(
                padding=ft.Padding.only(top=6),
                content=ft.Text(
                    label, size=12.5, font_family=FONT_SEMIBOLD, color=COLORS["text_sub"]
                ),
            ),
            control,
            *([ft.Text(hint, size=12, color=COLORS["text_dim"])] if hint else []),
        ],
    )


def _do_proxy_check(
    page: ft.Page,
    proxy_field: ft.TextField,
    proxy_error: ft.Text,
    check_btn: ft.OutlinedButton,
    proxy_service: IProxyService,
) -> None:
    proxy = (proxy_field.value or "").strip()
    if not proxy:
        proxy_error.value = get_string("enter_proxy_to_check")
        proxy_error.color = COLORS["warning"]
        proxy_error.visible = True
        page.update()
        return

    check_btn.content = ft.Text(get_string("proxy_checking"))
    check_btn.disabled = True
    proxy_error.visible = False
    page.update()

    def do_check() -> None:
        success, message = proxy_service.check_proxy_sync(proxy)
        check_btn.content = ft.Text(get_string("check_proxy"))
        check_btn.disabled = False
        proxy_error.value = message
        proxy_error.color = COLORS["success"] if success else COLORS["error"]
        proxy_error.visible = True
        with contextlib.suppress(Exception):
            page.update()

    threading.Thread(target=do_check, daemon=True).start()


def _do_ip_check(
    page: ft.Page,
    proxy_field: ft.TextField,
    timezone_field: ft.TextField,
    locale_field: ft.TextField,
    geo_result: ft.Column,
    ip_btn: ft.OutlinedButton,
) -> None:
    proxy = (proxy_field.value or "").strip()
    timezone = (timezone_field.value or "").strip()
    locale = (locale_field.value or "").strip()
    for validate, value in (
        (validate_proxy_format, proxy),
        (validate_timezone, timezone),
        (validate_locale, locale),
    ):
        valid, err = validate(value)
        if not valid:
            geo_result.controls = [ft.Text(err, size=12, color=COLORS["error"])]
            geo_result.visible = True
            page.update()
            return

    ip_btn.content = ft.Text(get_string("ip_checking"))
    ip_btn.disabled = True
    geo_result.visible = False
    page.update()

    def use_locale(value: str) -> None:
        locale_field.value = value
        page.update()

    def do_check() -> None:
        try:
            result = check_geo(proxy or None, timezone or None, locale or None)
            geo_result.controls = _geo_lines(result, use_locale)
            geo_result.height = min(160, 22 * len(geo_result.controls))
        except Exception as e:
            geo_result.controls = [
                ft.Text(get_string("geo_check_failed", error=e), size=12, color=COLORS["error"])
            ]
        ip_btn.content = ft.Text(get_string("check_ip"))
        ip_btn.disabled = False
        geo_result.visible = True
        with contextlib.suppress(Exception):
            page.update()

    threading.Thread(target=do_check, daemon=True).start()


def _geo_lines(result: GeoCheckResult, use_locale: Callable[[str], None]) -> list[ft.Control]:
    def line(text: str, color: str = COLORS["text_sub"]) -> ft.Text:
        return ft.Text(text, size=12, color=color, selectable=True)

    if result.exit_ip is None:
        return [line(get_string("geo_check_failed", error=result.error), COLORS["error"])]

    mode = {True: get_string("geo_pinned"), False: get_string("geo_auto")}
    lines: list[ft.Control] = [
        line(
            get_string("geo_exit", ip=result.exit_ip, country=result.country or "?"),
            COLORS["text_main"],
        ),
        line(
            get_string(
                "geo_timezone", value=result.timezone or "?", mode=mode[result.timezone_pinned]
            )
        ),
        line(
            get_string(
                "geo_locale",
                value=result.locale or f"~{result.suggested_locale or '?'}",
                mode=mode[True] if result.locale_pinned else get_string("geo_locale_random"),
            )
        ),
    ]
    for s in result.sources:
        if s.error:
            lines.append(
                line(
                    get_string("geo_source_error", source=s.source, error=s.error),
                    COLORS["text_dim"],
                )
            )
        else:
            src_str = get_string(
                "geo_source",
                source=s.source,
                ip=s.ip or "?",
                country=s.country or "?",
                city=s.city or "",
                timezone=s.timezone or "-",
            )
            if getattr(s, "isp", None):
                src_str = f"{src_str} · {s.isp}"
            lines.append(line(src_str, COLORS["text_dim"]))
    if result.error:
        lines.append(line(result.error, COLORS["error"]))
    for w in result.warnings:
        lines.append(line(f"\u26a0 {w.message}", COLORS["warning"]))
    if not result.warnings and not result.error:
        lines.append(line(f"\u2713 {get_string('geo_all_good')}", COLORS["success"]))
    if result.suggested_locale and not result.locale_pinned:
        lines.append(
            ft.TextButton(
                get_string("geo_use_suggested", value=result.suggested_locale),
                on_click=lambda _, v=result.suggested_locale: use_locale(v),
            )
        )
    return lines


def open_mail_credentials_dialog(
    page: ft.Page,
    profile: Profile,
    on_save_single: Callable[..., bool],
    on_bulk_import: Callable[[str], tuple[int, int]] | None = None,
    on_copy_text: Callable[[str, str], None] | None = None,
    *,
    initial_tab: str = "single",
    preloaded_messages: list[dict[str, str]] | None = None,
) -> None:
    """Dedicated Mail Login Credentials manager dialog (single profile + inbox OTP + bulk import)."""
    active_tab: list[str] = [initial_tab if initial_tab in ("single", "inbox", "bulk") else "single"]

    email_field = ft.TextField(
        value=(getattr(profile, "email", None) or ""),
        hint_text=get_string("email_hint"),
        text_style=_DATA,
        expand=True,
        **DLG_FIELD_KWARGS,
    )
    password_field = ft.TextField(
        value=(getattr(profile, "password", None) or ""),
        hint_text=get_string("password_hint"),
        password=True,
        can_reveal_password=True,
        text_style=_DATA,
        expand=True,
        **DLG_FIELD_KWARGS,
    )
    mail_token_field = ft.TextField(
        value=(getattr(profile, "mail_token", None) or ""),
        hint_text=get_string("mail_token_hint"),
        text_style=_DATA,
        expand=True,
        **DLG_FIELD_KWARGS,
    )
    two_fa_field = ft.TextField(
        value=(getattr(profile, "two_fa", None) or ""),
        hint_text=get_string("two_fa_hint"),
        text_style=_DATA,
        expand=True,
        **DLG_FIELD_KWARGS,
    )
    recovery_field = ft.TextField(
        value=(getattr(profile, "recovery_email", None) or ""),
        hint_text=get_string("recovery_email_hint"),
        text_style=_DATA,
        expand=True,
        **DLG_FIELD_KWARGS,
    )
    notes_field = ft.TextField(
        value=(profile.notes or ""),
        hint_text=get_string("notes_hint"),
        multiline=True,
        min_lines=1,
        max_lines=2,
        **DLG_FIELD_KWARGS,
    )
    quick_paste_field = ft.TextField(
        value="",
        hint_text=get_string("mail_smart_paste_hint"),
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    bulk_field = ft.TextField(
        value="",
        hint_text=get_string("mail_bulk_hint"),
        multiline=True,
        min_lines=10,
        max_lines=14,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )

    feedback_text = ft.Text("", size=12, color=COLORS["success"], visible=False)
    otp_preview_slot = ft.Container()

    def _copy_val(val: str, log_msg: str) -> None:
        clean = (val or "").strip()
        if not clean:
            return
        if on_copy_text is not None:
            on_copy_text(clean, log_msg)
        feedback_text.value = f"✓ {log_msg}"
        feedback_text.color = COLORS["success"]
        feedback_text.visible = True
        page.update()

    def _refresh_otp_preview() -> None:
        raw_secret = (two_fa_field.value or "").strip()
        pair = generate_totp(raw_secret) if raw_secret else None
        if pair is None:
            otp_preview_slot.content = None
            otp_preview_slot.visible = False
            return
        code, rem = pair
        otp_preview_slot.visible = True
        otp_preview_slot.content = ft.Container(
            height=42,
            padding=ft.Padding.symmetric(horizontal=10),
            border_radius=8,
            bgcolor=COLORS["input_bg"],
            border=ft.Border.all(1.5, COLORS["accent"]),
            ink=True,
            tooltip=get_string("tooltip_copy_2fa", code=code, rem=rem),
            on_click=lambda _: _copy_val(
                code,
                get_string("copied_2fa_log", name=profile.name, code=code, rem=rem),
            ),
            content=ft.Row(
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.KEY_ROUNDED, size=14, color=COLORS["accent"]),
                    ft.Text(
                        f"{code} ({rem}s)",
                        size=13,
                        font_family=FONT_DATA,
                        weight=ft.FontWeight.W_700,
                        color=COLORS["accent"],
                    ),
                ],
            ),
        )

    def _on_two_fa_change(_: ft.ControlEvent) -> None:
        _refresh_otp_preview()
        page.update()

    two_fa_field.on_change = _on_two_fa_change
    _refresh_otp_preview()

    def _copy_icon_btn(get_val: Callable[[], str], log_msg_fn: Callable[[], str]) -> ft.IconButton:
        return ft.IconButton(
            icon=ft.Icons.CONTENT_COPY_ROUNDED,
            icon_size=16,
            tooltip="Copy",
            style=OUTLINE_STYLE,
            on_click=lambda _: _copy_val(get_val(), log_msg_fn()),
        )

    two_fa_section = _field(
        get_string("two_fa_optional"),
        ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                two_fa_field,
                otp_preview_slot,
                _copy_icon_btn(
                    lambda: two_fa_field.value or "",
                    lambda: f"[{profile.name}] Copied 2FA Secret",
                ),
            ],
        ),
    )
    two_fa_section.visible = bool((two_fa_field.value or "").strip())

    def _on_quick_paste_change(_: ft.ControlEvent) -> None:
        raw = (quick_paste_field.value or "").strip()
        if not raw:
            return
        if "|" in raw or "\t" in raw or ";" in raw or (":" in raw and "@" in raw):
            parsed = parse_mail_credentials_line(raw)
            if parsed["email"]:
                email_field.value = parsed["email"]
            if parsed["password"]:
                password_field.value = parsed["password"]
            if parsed["two_fa"]:
                two_fa_field.value = parsed["two_fa"]
                two_fa_section.visible = True
            if parsed.get("mail_token"):
                mail_token_field.value = parsed["mail_token"]
            if parsed["recovery_email"]:
                recovery_field.value = parsed["recovery_email"]
            if parsed["notes"] and not (notes_field.value or "").strip():
                notes_field.value = parsed["notes"]
            quick_paste_field.value = ""
            _refresh_otp_preview()
            em_v = (email_field.value or "").strip() or None
            pw_v = (password_field.value or "").strip() or None
            tfa_v = (two_fa_field.value or "").strip() or None
            rec_v = (recovery_field.value or "").strip() or None
            tok_v = (mail_token_field.value or "").strip() or None
            nt_v = (notes_field.value or "").strip() or None
            sig_qp = inspect.signature(on_save_single)
            if len(sig_qp.parameters) >= 6:
                on_save_single(em_v, pw_v, tfa_v, rec_v, nt_v, tok_v)
            else:
                on_save_single(em_v, pw_v, tfa_v, rec_v, nt_v)
            feedback_text.value = f"✓ {get_string('mail_saved_log', name=profile.name)}"
            feedback_text.color = COLORS["success"]
            feedback_text.visible = True
            page.update()

    quick_paste_field.on_change = _on_quick_paste_change

    def _copy_full_line(_: ft.ControlEvent) -> None:
        full_str = format_mail_credentials_line(
            email_field.value,
            password_field.value,
            two_fa_field.value,
            recovery_field.value,
            mail_token_field.value,
        )
        if not full_str.strip("|"):
            return
        _copy_val(
            full_str,
            get_string("copied_full_mail_log", name=profile.name),
        )

    single_body = ft.Column(
        tight=True,
        spacing=6,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            _field(get_string("mail_smart_paste_label"), quick_paste_field),
            _field(
                get_string("email_optional"),
                ft.Row(
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        email_field,
                        _copy_icon_btn(
                            lambda: email_field.value or "",
                            lambda: get_string(
                                "copied_email_log",
                                name=profile.name,
                                email=(email_field.value or "").strip(),
                            ),
                        ),
                    ],
                ),
            ),
            _field(
                get_string("mail_password_optional"),
                ft.Row(
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        password_field,
                        _copy_icon_btn(
                            lambda: password_field.value or "",
                            lambda: get_string("copied_password_log", name=profile.name),
                        ),
                    ],
                ),
            ),
            _field(
                get_string("mail_token_optional"),
                ft.Row(
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        mail_token_field,
                        _copy_icon_btn(
                            lambda: format_oauth_reader_line(
                                email_field.value,
                                password_field.value,
                                mail_token_field.value,
                            ),
                            lambda: get_string("copied_token_log", name=profile.name),
                        ),
                    ],
                ),
            ),
            _field(
                get_string("recovery_email_optional"),
                ft.Row(
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        recovery_field,
                        _copy_icon_btn(
                            lambda: recovery_field.value or "",
                            lambda: get_string(
                                "copied_recovery_log",
                                name=profile.name,
                                email=(recovery_field.value or "").strip(),
                            ),
                        ),
                    ],
                ),
            ),
            two_fa_section,
        ],
    )

    inbox_list_col = ft.Column(
        height=440,
        spacing=8,
        scroll=ft.ScrollMode.AUTO,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    refresh_inbox_btn = ft.OutlinedButton(
        get_string("mail_read_inbox_btn"),
        icon=ft.Icons.REFRESH_ROUNDED,
        height=34,
        style=OUTLINE_STYLE,
        on_click=lambda _: _trigger_read_inbox(),
    )
    inbox_body = ft.Column(
        tight=True,
        spacing=8,
        visible=False,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(
                        email_field.value or profile.email or "",
                        size=12.5,
                        font_family=FONT_DATA,
                        color=COLORS["text_sub"],
                    ),
                    refresh_inbox_btn,
                ],
            ),
            inbox_list_col,
        ],
    )

    def _render_inbox_messages(msgs: list[dict[str, str]]) -> None:
        if not msgs:
            inbox_list_col.controls = [
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=24, horizontal=12),
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("mail_inbox_empty"),
                        size=12.5,
                        color=COLORS["text_dim"],
                    ),
                )
            ]
            return
        cards: list[ft.Control] = []
        for m in msgs:
            otp_code = m.get("otp") or ""
            subj_str = m.get("subject") or ""
            sender_str = m.get("sender") or ""
            time_str = m.get("time") or ""
            prev_str = m.get("preview") or ""
            top_row_controls: list[ft.Control] = [
                ft.Text(
                    subj_str,
                    size=12.5,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_main"],
                    expand=True,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    tooltip=subj_str,
                )
            ]
            if otp_code:
                top_row_controls.append(
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                        border_radius=5,
                        bgcolor=COLORS["input_bg"],
                        border=ft.Border.all(1.5, COLORS["accent"]),
                        ink=True,
                        on_click=lambda _, c=otp_code, s=subj_str: _copy_val(
                            c,
                            get_string(
                                "mail_otp_found_log",
                                name=profile.name,
                                otp=c,
                                subject=s[:40],
                            ),
                        ),
                        content=ft.Text(
                            get_string("mail_copy_otp_pill", otp=otp_code),
                            size=11.5,
                            font_family=FONT_DATA,
                            weight=ft.FontWeight.W_700,
                            color=COLORS["accent"],
                        ),
                    )
                )
            cards.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                    border_radius=8,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(
                        1.5 if otp_code else 1,
                        COLORS["accent"] if otp_code else COLORS["rule_strong"],
                    ),
                    content=ft.Column(
                        tight=True,
                        spacing=3,
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                        controls=[
                            ft.Row(
                                spacing=8,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=top_row_controls,
                            ),
                            ft.Text(
                                f"{sender_str} · {time_str}".strip(" ·"),
                                size=11,
                                font_family=FONT_DATA,
                                color=COLORS["text_dim"],
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            *(
                                [
                                    ft.Text(
                                        prev_str,
                                        size=11.5,
                                        color=COLORS["text_sub"],
                                        selectable=True,
                                    )
                                ]
                                if prev_str
                                else []
                            ),
                        ],
                    ),
                )
            )
        inbox_list_col.controls = cards

    def _trigger_read_inbox() -> None:
        if active_tab[0] != "inbox":
            _switch_tab("inbox", auto_fetch=False)
        em_addr = (email_field.value or "").strip()
        tok_str = (mail_token_field.value or "").strip()
        feedback_text.value = get_string("mail_reading_inbox")
        feedback_text.color = COLORS["accent"]
        feedback_text.visible = True
        refresh_inbox_btn.disabled = True
        page.update()

        def _worker() -> None:
            ok, otp_or_err, msgs = fetch_hotmail_messages(em_addr, tok_str, top=8, timeout=12)
            refresh_inbox_btn.disabled = False
            if not ok:
                feedback_text.value = otp_or_err
                feedback_text.color = COLORS["error"]
                feedback_text.visible = True
            else:
                _render_inbox_messages(msgs)
                if otp_or_err:
                    first_subj = next(
                        (m.get("subject", "") for m in msgs if m.get("otp") == otp_or_err),
                        "",
                    )
                    log_msg = get_string(
                        "mail_otp_found_log",
                        name=profile.name,
                        otp=otp_or_err,
                        subject=first_subj[:40],
                    )
                    if on_copy_text is not None:
                        on_copy_text(otp_or_err, log_msg)
                    feedback_text.value = f"✓ {log_msg}"
                    feedback_text.color = COLORS["success"]
                    feedback_text.visible = True
                else:
                    feedback_text.value = (
                        get_string("mail_inbox_no_otp", count=len(msgs))
                        if msgs
                        else get_string("mail_inbox_empty")
                    )
                    feedback_text.color = COLORS["text_sub"]
                    feedback_text.visible = True
            with contextlib.suppress(Exception):
                page.update()

        threading.Thread(target=_worker, daemon=True).start()

    if preloaded_messages is not None:
        _render_inbox_messages(preloaded_messages)

    bulk_body = ft.Column(
        tight=True,
        spacing=8,
        visible=False,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Text(
                get_string("mail_bulk_label"),
                size=12,
                color=COLORS["text_sub"],
            ),
            bulk_field,
        ],
    )

    copy_all_btn = ft.OutlinedButton(
        get_string("mail_copy_line_btn"),
        icon=ft.Icons.COPY_ALL_ROUNDED,
        height=36,
        style=OUTLINE_STYLE,
        on_click=_copy_full_line,
    )

    save_btn = ft.Button(
        get_string("save_changes"),
        icon=ft.Icons.SAVE,
        style=ACCENT_STYLE,
    )

    tab_row_slot = ft.Container()

    def _switch_tab(tab: str, *, auto_fetch: bool = True) -> None:
        active_tab[0] = tab
        single_body.visible = tab == "single"
        inbox_body.visible = tab == "inbox"
        bulk_body.visible = tab == "bulk"
        copy_all_btn.visible = tab == "single"
        save_btn.visible = tab in ("single", "bulk")
        save_btn.content = ft.Text(
            get_string("save_changes") if tab == "single" else get_string("mail_bulk_apply_btn")
        )
        feedback_text.visible = False
        _rebuild_tabs()
        page.update()
        if tab == "inbox" and auto_fetch and not inbox_list_col.controls:
            _trigger_read_inbox()

    def _rebuild_tabs() -> None:
        cur = active_tab[0]

        def _tab_pill(code: str, label_str: str, icon: ft.IconData) -> ft.Container:
            is_act = cur == code
            return ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                border_radius=6,
                bgcolor=COLORS["sidebar"] if is_act else COLORS["input_bg"],
                border=ft.Border.all(
                    1, COLORS["sidebar"] if is_act else COLORS["rule_strong"]
                ),
                ink=True,
                on_click=lambda _, c=code: _switch_tab(c),
                content=ft.Row(
                    spacing=5,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(
                            icon,
                            size=14,
                            color=COLORS["cover_ink"] if is_act else COLORS["text_sub"],
                        ),
                        ft.Text(
                            label_str,
                            size=11.5,
                            font_family=FONT_SEMIBOLD if is_act else FONT_MEDIUM,
                            color=COLORS["cover_ink"] if is_act else COLORS["text_main"],
                        ),
                    ],
                ),
            )

        pills: list[ft.Control] = [
            _tab_pill(
                "single",
                get_string("mail_tab_single", name=profile.name),
                ft.Icons.LOCK_OUTLINE,
            ),
            _tab_pill(
                "inbox",
                get_string("mail_tab_inbox"),
                ft.Icons.MARK_EMAIL_UNREAD_OUTLINED,
            ),
        ]
        if on_bulk_import is not None:
            pills.append(
                _tab_pill("bulk", get_string("mail_tab_bulk"), ft.Icons.PLAYLIST_ADD_CHECK)
            )
        tab_row_slot.content = ft.Row(spacing=6, wrap=True, controls=pills)

    _switch_tab(active_tab[0], auto_fetch=False)

    def _on_submit(_: ft.ControlEvent) -> None:
        if active_tab[0] == "bulk" and on_bulk_import is not None:
            raw_bulk = (bulk_field.value or "").strip()
            if not raw_bulk:
                return
            updated, skipped = on_bulk_import(raw_bulk)
            feedback_text.value = (
                f"✓ {get_string('mail_bulk_saved_log', updated=updated, skipped=skipped)}"
            )
            feedback_text.color = COLORS["success"] if updated > 0 else COLORS["warning"]
            feedback_text.visible = True
            page.update()
            return

        em = (email_field.value or "").strip()
        pw = (password_field.value or "").strip()
        tfa = (two_fa_field.value or "").strip()
        rec = (recovery_field.value or "").strip()
        tok = (mail_token_field.value or "").strip()
        nt = (notes_field.value or "").strip()
        for validate_fn, val in (
            (validate_email, em),
            (validate_password, pw),
            (validate_two_fa, tfa),
            (validate_email, rec),
            (validate_mail_token, tok),
            (validate_notes, nt),
        ):
            ok, err = validate_fn(val)
            if not ok:
                feedback_text.value = err
                feedback_text.color = COLORS["error"]
                feedback_text.visible = True
                page.update()
                return

        sig_single = inspect.signature(on_save_single)
        if len(sig_single.parameters) >= 6:
            saved_ok = on_save_single(
                em or None,
                pw or None,
                tfa or None,
                rec or None,
                nt or None,
                tok or None,
            )
        else:
            saved_ok = on_save_single(
                em or None,
                pw or None,
                tfa or None,
                rec or None,
                nt or None,
            )
        if saved_ok:
            page.pop_dialog()

    save_btn.on_click = _on_submit

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=20, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=18),
        title=ft.Column(
            spacing=6,
            tight=True,
            controls=[
                ft.Text(
                    get_string("mail_dlg_title", name=profile.name),
                    size=18,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                ft.Text(
                    get_string("mail_dlg_subtitle"),
                    size=12.5,
                    color=COLORS["text_sub"],
                ),
                tab_row_slot,
            ],
        ),
        content=ft.Container(
            width=720,
            content=ft.Column(
                tight=True,
                spacing=6,
                scroll=ft.ScrollMode.AUTO,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    single_body,
                    inbox_body,
                    bulk_body,
                    feedback_text,
                ],
            ),
        ),
        actions=[
            copy_all_btn,
            ft.TextButton(
                get_string("cancel"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
            save_btn,
        ],
    )
    page.show_dialog(dlg)
    if (
        active_tab[0] == "inbox"
        and preloaded_messages is None
        and (mail_token_field.value or "").strip()
    ):
        _trigger_read_inbox()


def open_warmup_sites_dialog(
    page: ft.Page,
    on_copy_text: Callable[[str, str], None] | None = None,
) -> None:
    """Dialog to browse, 1-click copy, and customize human-trust warmup sites."""
    active_tab: list[str] = ["list"]
    current_text: list[str] = [load_warmup_text()]

    feedback_text = ft.Text("", size=12, color=COLORS["success"], visible=False)
    groups_col = ft.Column(
        tight=True,
        spacing=10,
        scroll=ft.ScrollMode.AUTO,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    edit_field = ft.TextField(
        value=current_text[0],
        multiline=True,
        min_lines=12,
        max_lines=16,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )

    def _copy_url(url: str, custom_msg: str | None = None) -> None:
        clean_url = (url or "").strip()
        if not clean_url:
            return
        log_msg = custom_msg or get_string("warmup_copied_log", url=clean_url)
        if on_copy_text is not None:
            on_copy_text(clean_url, log_msg)
        feedback_text.value = f"✓ {log_msg}"
        feedback_text.color = COLORS["success"]
        feedback_text.visible = True
        page.update()

    def _rebuild_groups_list() -> None:
        groups = parse_warmup_groups(current_text[0])
        blocks: list[ft.Control] = []
        for grp in groups:
            title_str = str(grp.get("title") or "")
            sub_str = str(grp.get("subtitle") or "")
            items = grp.get("items") or []
            item_rows: list[ft.Control] = []
            if isinstance(items, list):
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    u = str(it.get("url") or "")
                    n = str(it.get("note") or "")
                    item_rows.append(
                        ft.Container(
                            padding=ft.Padding.symmetric(horizontal=10, vertical=7),
                            border_radius=7,
                            bgcolor=COLORS["card_bg"],
                            border=ft.Border.all(1, COLORS["rule_strong"]),
                            ink=True,
                            tooltip=u,
                            on_click=lambda _, url_val=u: _copy_url(url_val),
                            content=ft.Row(
                                spacing=8,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=[
                                    ft.Column(
                                        spacing=2,
                                        tight=True,
                                        expand=True,
                                        controls=[
                                            ft.Text(
                                                u,
                                                size=12.5,
                                                font_family=FONT_DATA,
                                                weight=ft.FontWeight.W_600,
                                                color=COLORS["accent"],
                                                no_wrap=True,
                                                overflow=ft.TextOverflow.ELLIPSIS,
                                            ),
                                            *(
                                                [
                                                    ft.Text(
                                                        n,
                                                        size=11.5,
                                                        color=COLORS["text_sub"],
                                                    )
                                                ]
                                                if n
                                                else []
                                            ),
                                        ],
                                    ),
                                    ft.Container(
                                        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                                        border_radius=5,
                                        bgcolor=COLORS["input_bg"],
                                        border=ft.Border.all(1, COLORS["accent"]),
                                        content=ft.Row(
                                            spacing=4,
                                            tight=True,
                                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                            controls=[
                                                ft.Icon(
                                                    ft.Icons.CONTENT_COPY_ROUNDED,
                                                    size=12,
                                                    color=COLORS["accent"],
                                                ),
                                                ft.Text(
                                                    get_string("warmup_copy_pill"),
                                                    size=11,
                                                    font_family=FONT_SEMIBOLD,
                                                    color=COLORS["accent"],
                                                ),
                                            ],
                                        ),
                                    ),
                                ],
                            ),
                        )
                    )
            blocks.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                    border_radius=8,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(1, COLORS["rule_strong"]),
                    content=ft.Column(
                        tight=True,
                        spacing=6,
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                        controls=[
                            ft.Text(
                                title_str,
                                size=13,
                                font_family=FONT_BOLD,
                                color=COLORS["text_main"],
                            ),
                            *(
                                [
                                    ft.Text(
                                        sub_str,
                                        size=11.5,
                                        color=COLORS["text_dim"],
                                    )
                                ]
                                if sub_str
                                else []
                            ),
                            *item_rows,
                        ],
                    ),
                )
            )
        groups_col.controls = blocks

    _rebuild_groups_list()

    list_body = ft.Column(
        tight=True,
        spacing=8,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Container(
                height=560,
                content=groups_col,
            ),
        ],
    )

    edit_body = ft.Column(
        tight=True,
        spacing=8,
        visible=False,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Text(
                get_string("warmup_edit_hint"),
                size=12,
                color=COLORS["text_sub"],
            ),
            edit_field,
        ],
    )

    reset_btn = ft.OutlinedButton(
        get_string("warmup_reset_default_btn"),
        icon=ft.Icons.RESTORE_ROUNDED,
        height=36,
        style=OUTLINE_STYLE,
        visible=False,
    )
    save_btn = ft.Button(
        get_string("save_changes"),
        icon=ft.Icons.SAVE,
        style=ACCENT_STYLE,
        visible=False,
    )
    tab_row_slot = ft.Container()

    def _switch_tab(tab: str) -> None:
        active_tab[0] = tab
        list_body.visible = tab == "list"
        edit_body.visible = tab == "edit"
        reset_btn.visible = tab == "edit"
        save_btn.visible = tab == "edit"
        feedback_text.visible = False
        _rebuild_tabs()
        page.update()

    def _rebuild_tabs() -> None:
        cur = active_tab[0]

        def _tab_pill(code: str, label_str: str, icon: ft.IconData) -> ft.Container:
            is_act = cur == code
            return ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                border_radius=6,
                bgcolor=COLORS["sidebar"] if is_act else COLORS["input_bg"],
                border=ft.Border.all(
                    1, COLORS["sidebar"] if is_act else COLORS["rule_strong"]
                ),
                ink=True,
                on_click=lambda _, c=code: _switch_tab(c),
                content=ft.Row(
                    spacing=5,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(
                            icon,
                            size=14,
                            color=COLORS["cover_ink"] if is_act else COLORS["text_sub"],
                        ),
                        ft.Text(
                            label_str,
                            size=11.5,
                            font_family=FONT_SEMIBOLD if is_act else FONT_MEDIUM,
                            color=COLORS["cover_ink"] if is_act else COLORS["text_main"],
                        ),
                    ],
                ),
            )

        tab_row_slot.content = ft.Row(
            spacing=6,
            controls=[
                _tab_pill("list", get_string("warmup_tab_list"), ft.Icons.PUBLIC_ROUNDED),
                _tab_pill("edit", get_string("warmup_tab_edit"), ft.Icons.EDIT_NOTE_ROUNDED),
            ],
        )

    def _on_reset(_: ft.ControlEvent) -> None:
        edit_field.value = DEFAULT_WARMUP_TEXT
        current_text[0] = DEFAULT_WARMUP_TEXT
        save_warmup_text(DEFAULT_WARMUP_TEXT)
        _rebuild_groups_list()
        feedback_text.value = f"✓ {get_string('warmup_saved_log')}"
        feedback_text.color = COLORS["success"]
        feedback_text.visible = True
        page.update()

    def _on_save(_: ft.ControlEvent) -> None:
        new_txt = (edit_field.value or "").strip() or DEFAULT_WARMUP_TEXT
        current_text[0] = new_txt
        save_warmup_text(new_txt)
        _rebuild_groups_list()
        _switch_tab("list")
        feedback_text.value = f"✓ {get_string('warmup_saved_log')}"
        feedback_text.color = COLORS["success"]
        feedback_text.visible = True
        page.update()

    reset_btn.on_click = _on_reset
    save_btn.on_click = _on_save
    _rebuild_tabs()

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        inset_padding=ft.Padding.symmetric(horizontal=24, vertical=18),
        title_padding=ft.Padding.only(left=24, right=24, top=20, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=18),
        title=ft.Column(
            spacing=6,
            tight=True,
            controls=[
                ft.Text(
                    get_string("warmup_sites_title"),
                    size=18,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                ft.Text(
                    get_string("warmup_sites_subtitle"),
                    size=12.5,
                    color=COLORS["text_sub"],
                ),
                tab_row_slot,
            ],
        ),
        content=ft.Container(
            width=840,
            content=ft.Column(
                tight=True,
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    list_body,
                    edit_body,
                    feedback_text,
                ],
            ),
        ),
        actions=[
            reset_btn,
            ft.TextButton(
                get_string("cancel"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
            save_btn,
        ],
    )
    page.show_dialog(dlg)

