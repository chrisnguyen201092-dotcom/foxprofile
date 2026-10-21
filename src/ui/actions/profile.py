from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ...services.browser import chrome_prefetch
from ..dialogs import open_confirm_dialog, open_profile_dialog


def delete_profile(
    page: ft.Page,
    name: str,
    pm: IProfileManager,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    def do_delete() -> None:
        if hasattr(pm, "trash_profile"):
            pm.trash_profile(name)
            log(get_string("moved_to_trash", name=name))
        else:
            pm.delete_profile(name)
            log(get_string("deleted_profile", name=name))
        refresh()

    open_confirm_dialog(
        page,
        name,
        do_delete,
        title=get_string("confirm_trash_msg", name=name),
        body=get_string("trash_can_restore_note"),
    )


def _existing_groups(pm: IProfileManager) -> list[str]:
    return sorted({p.group for p in pm.profiles.values() if p.group}, key=str.casefold)


def edit_profile(
    page: ft.Page,
    name: str,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    ps: IProxyService,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    profile = pm.profiles.get(name)
    if not profile:
        return
    original = profile.name

    def on_save(
        new_name: str,
        new_proxy: str,
        new_os: str,
        timezone: str,
        locale: str,
        _engine: str,
        color: str | None = None,
        group: str | None = None,
        notes: str | None = None,
    ) -> str | None:
        if new_name != original and bl.is_running(original):
            return get_string("stop_before_rename")
        if pm.update_profile(
            original,
            new_name,
            new_proxy,
            new_os,
            timezone,
            locale,
            color,
            group,
            notes,
        ):
            log(get_string("updated_profile", old=original, new=new_name))
            refresh()
            return None
        return get_string("update_failed")

    open_profile_dialog(page, ps, on_save, profile, existing_groups=_existing_groups(pm))


def add_profile(
    page: ft.Page,
    pm: IProfileManager,
    ps: IProxyService,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    def on_save(
        name: str,
        proxy: str,
        os_type: str,
        timezone: str,
        locale: str,
        engine: str,
        color: str | None = None,
        group: str | None = None,
        notes: str | None = None,
    ) -> str | None:
        if engine == "chrome" and (reason := chrome_prefetch.unsupported_reason()):
            return get_string("chrome_engine_unsupported", reason=reason)
        if pm.add_profile(name, proxy, os_type, timezone, locale, engine, color, group, notes):
            log(get_string("created_profile", name=name))
            if engine == "chrome":
                chrome_prefetch.start(log)
            refresh()
            return None
        return get_string("profile_exists")

    open_profile_dialog(page, ps, on_save, existing_groups=_existing_groups(pm))
