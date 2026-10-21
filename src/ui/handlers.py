"""Event handlers extracted from App for Single Responsibility compliance."""

import asyncio
from collections.abc import Callable

import flet as ft

from ..core.strings import get_string
from ..interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ..utils.validation import validate_notes, validate_proxy_format
from .actions.browser import launch_or_stop
from .actions.bulk import (
    bulk_check_proxies,
    bulk_delete_profiles,
    bulk_launch_profiles,
    bulk_stop_profiles,
    format_proxy_badge,
)
from .actions.cookies import manage_cookies
from .actions.profile import add_profile, delete_profile, edit_profile
from .actions.transfer import export_profile, import_profile
from .dialogs import (
    open_bulk_color_dialog,
    open_bulk_group_dialog,
    open_clone_dialog,
    open_log_dialog,
    open_trash_dialog,
)
from .refs import UIRefs
from .state import AppState


class AppHandlers:
    """Decoupled event-handler object wired to the App's dependencies."""

    def __init__(
        self,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        ps: IProxyService,
        state: AppState,
        get_page: Callable[[], ft.Page | None],
        get_refs: Callable[[], UIRefs | None],
        log_fn: Callable[[str], None],
        refresh_fn: Callable[[], None],
        get_page_profiles: Callable[[], tuple],
    ) -> None:
        self._pm = pm
        self._bl = bl
        self._ps = ps
        self._state = state
        self._get_page = get_page
        self._get_refs = get_refs
        self._log = log_fn
        self._refresh = refresh_fn
        self._get_page_profiles = get_page_profiles

    def on_launch(self, name: str) -> None:
        launch_or_stop(name, self._pm, self._bl, self._state, self._log)

    def on_delete(self, name: str) -> None:
        page = self._get_page()
        assert page is not None
        delete_profile(page, name, self._pm, self._log, self._refresh)

    def on_edit(self, name: str) -> None:
        page = self._get_page()
        assert page is not None
        edit_profile(
            page,
            name,
            self._pm,
            self._bl,
            self._ps,
            self._log,
            self._refresh,
        )

    def on_cookies(self, name: str) -> None:
        page = self._get_page()
        refs = self._get_refs()
        assert page is not None and refs is not None
        manage_cookies(page, name, refs.file_picker, self._pm, self._bl, self._log)

    def on_clone(self, name: str) -> None:
        page = self._get_page()
        assert page is not None
        if name not in self._pm.profiles:
            return

        def _do_clone(new_name: str, copy_proxy: bool) -> None:
            if self._pm.clone_profile(name, new_name, copy_proxy=copy_proxy):
                self._log(get_string("cloned_profile", src=name, dst=new_name))
                self._refresh()

        open_clone_dialog(page, name, set(self._pm.profiles.keys()), _do_clone)

    def on_open_trash(self) -> None:
        page = self._get_page()
        assert page is not None

        def _restore(n: str) -> None:
            if self._pm.restore_profile(n):
                self._log(get_string("restored_from_trash", name=n))
                self._refresh()

        def _del_forever(n: str) -> None:
            if self._pm.delete_trashed_profile(n):
                self._log(get_string("deleted_profile", name=n))
                self._refresh()

        def _empty_all() -> None:
            self._pm.empty_trash()
            self._refresh()

        open_trash_dialog(
            page,
            self._pm.list_trashed_profiles,
            _restore,
            _del_forever,
            _empty_all,
        )

    # --- Inline quick-edit & proxy check handlers ---

    def on_start_edit_note(self, name: str) -> None:
        self._state.editing_proxy = None
        self._state.editing_note = name
        self._refresh()

    def on_save_note(self, name: str, raw_note: str) -> None:
        if self._state.editing_note != name:
            return
        self._state.editing_note = None
        p = self._pm.profiles.get(name)
        if p is None:
            self._refresh()
            return
        clean = raw_note.strip()
        if clean and not validate_notes(clean)[0]:
            self._refresh()
            return
        if (clean or None) != p.notes:
            self._pm.update_profile(
                p.name,
                p.name,
                p.proxy or "",
                p.os_type,
                p.timezone,
                p.locale,
                p.color,
                p.group,
                clean or None,
                p.pages,
            )
            self._log(get_string("quick_saved_notes", name=name))
        self._refresh()

    def on_start_edit_proxy(self, name: str) -> None:
        self._state.editing_note = None
        self._state.editing_proxy = name
        self._refresh()

    def on_save_proxy(self, name: str, raw_proxy: str) -> None:
        if self._state.editing_proxy != name:
            return
        self._state.editing_proxy = None
        p = self._pm.profiles.get(name)
        if p is None:
            self._refresh()
            return
        clean = raw_proxy.strip()
        ok, err = validate_proxy_format(clean)
        if not ok:
            self._log(f"[{name}] {err}")
            self._refresh()
            return
        if (clean or None) != p.proxy:
            self._pm.update_profile(
                p.name,
                p.name,
                clean,
                p.os_type,
                p.timezone,
                p.locale,
                p.color,
                p.group,
                p.notes,
                p.pages,
            )
            self._log(get_string("quick_saved_proxy", name=name))
        self._refresh()

    def on_quick_check_proxy(self, name: str) -> None:
        page = self._get_page()
        p = self._pm.profiles.get(name)
        if page is None or p is None or not p.proxy:
            return
        proxy_str = p.proxy
        siblings = [
            prof.name
            for prof in self._pm.profiles.values()
            if prof.proxy == proxy_str
        ]
        for sib in siblings:
            self._state.proxy_status[sib] = ("checking", f"⏳ {get_string('proxy_checking')}")
        self._refresh()
        self._log(f"[{name}] {get_string('proxy_checking')}")

        async def _check() -> None:
            ok, detail = await asyncio.to_thread(self._ps.check_proxy_sync, proxy_str, 10)
            badge = format_proxy_badge(ok, detail)
            for sib in siblings:
                self._state.proxy_status[sib] = badge
            self._log(f"[{name}] {detail}")
            self._state.schedule_refresh()

        page.run_task(_check)

    def open_add_dialog(self) -> None:
        page = self._get_page()
        assert page is not None
        add_profile(page, self._pm, self._ps, self._log, self._refresh)

    def on_move(self, name: str, direction: int) -> None:
        if self._pm.move_profile(name, direction):
            self._refresh()

    async def on_import(self, _: ft.ControlEvent | None = None) -> None:
        refs = self._get_refs()
        assert refs is not None
        await import_profile(refs.file_picker, self._pm, self._log, self._refresh)

    def on_export_open(self) -> None:
        page = self._get_page()
        refs = self._get_refs()
        assert page is not None and refs is not None
        export_profile(page, refs.file_picker, self._pm, self._log)

    # --- Selection handlers ---

    def on_toggle_select(self, name: str) -> None:
        if self._state.shift_down and self._state.last_selected_name:
            _, page_profiles, _ = self._get_page_profiles()
            visible_names = [p.name for p in page_profiles]
            self._state.select_range(visible_names, name)
        else:
            self._state.toggle_selection(name)
        self._state.schedule_refresh()

    def on_bulk_select_range(self) -> None:
        _, page_profiles, _ = self._get_page_profiles()
        visible_names = [p.name for p in page_profiles]
        self._state.select_range(visible_names)
        self._state.schedule_refresh()

    def on_select_all_page(self) -> None:
        _, page_profiles, _ = self._get_page_profiles()
        self._state.select_all([p.name for p in page_profiles])
        self._state.schedule_refresh()

    def on_deselect_page(self) -> None:
        _, page_profiles, _ = self._get_page_profiles()
        for p in page_profiles:
            if self._state.is_selected(p.name):
                self._state.toggle_selection(p.name)
        self._state.schedule_refresh()

    def on_clear_selection(self) -> None:
        self._state.clear_selection()
        self._state.schedule_refresh()

    # --- Bulk action handlers ---

    def on_bulk_delete(self) -> None:
        page = self._get_page()
        assert page is not None
        selected = self._state.selected_names()
        # Like the web panel and the API: a running profile is never deleted.
        if running := [n for n in selected if self._bl.is_running(n)]:
            self._log(get_string("bulk_skip_running", count=len(running)))
        if names := [n for n in selected if not self._bl.is_running(n)]:
            bulk_delete_profiles(
                page,
                names,
                self._pm,
                self._log,
                self._refresh,
                on_done=self._state.clear_selection,
            )

    def on_bulk_launch(self) -> None:
        if names := list(self._state.selected_names()):
            bulk_launch_profiles(names, self._pm, self._bl, self._state, self._log)

    def on_bulk_stop(self) -> None:
        if names := list(self._state.selected_names()):
            bulk_stop_profiles(names, self._pm, self._bl, self._state, self._log)

    def on_bulk_set_group(self) -> None:
        page = self._get_page()
        assert page is not None
        selected = list(self._state.selected_names())
        if not selected:
            return
        existing = sorted(
            {p.group for p in self._pm.profiles.values() if p.group},
            key=str.casefold,
        )

        def _apply(grp: str | None) -> None:
            cnt = self._pm.bulk_update_group(selected, grp)
            self._log(get_string("bulk_group_updated", count=cnt))
            self._state.clear_selection()
            self._refresh()

        open_bulk_group_dialog(page, len(selected), existing, _apply)

    def on_bulk_set_color(self) -> None:
        page = self._get_page()
        assert page is not None
        selected = list(self._state.selected_names())
        if not selected:
            return

        def _apply(col: str | None) -> None:
            cnt = self._pm.bulk_update_color(selected, col)
            self._log(get_string("bulk_color_updated", count=cnt))
            self._state.clear_selection()
            self._refresh()

        open_bulk_color_dialog(page, len(selected), _apply)

    def on_bulk_check_proxy(self) -> None:
        page = self._get_page()
        assert page is not None
        selected = list(self._state.selected_names())
        if not selected:
            _, page_profiles, _ = self._get_page_profiles()
            selected = [p.name for p in page_profiles]
        if not selected:
            return

        async def _task() -> None:
            await bulk_check_proxies(
                selected,
                self._pm,
                self._ps,
                self._log,
                state=self._state,
            )

        page.run_task(_task)

    # --- Log handlers ---

    def toggle_log(self) -> None:
        page = self._get_page()
        refs = self._get_refs()
        assert refs is not None and page is not None
        self._state.log_collapsed = not self._state.log_collapsed
        has_content = bool(refs.log_text.value)
        refs.log_column.visible = has_content and not self._state.log_collapsed
        refs.log_toggle_btn.icon = (
            ft.Icons.KEYBOARD_ARROW_RIGHT
            if self._state.log_collapsed
            else ft.Icons.KEYBOARD_ARROW_DOWN
        )
        page.update()

    def open_log_fullscreen(self) -> None:
        page = self._get_page()
        assert page is not None
        open_log_dialog(page, self._state.get_all_log_lines())
