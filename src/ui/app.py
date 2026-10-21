import asyncio
import os
import time

import flet as ft

from ..core.config import DATA_DIR
from ..core.container import Container
from ..core.logging import get_logger
from ..core.strings import current_language, get_string, set_language
from ..interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ..services.browser import fingerprint
from ..services.monitor import MonitorService, MonitorSnapshot
from .components import (
    build_content_area,
    build_empty_state,
    build_pages_bar,
    build_profile_card,
    build_sidebar,
    build_ui_refs,
    rebuild_bulk_bar,
)
from .components.content_area import build_monitor_bar
from .components.profile_card import build_header_row
from .components.sidebar import build_filter_buttons
from .dialogs import open_add_to_page_dialog, open_custom_page_dialog
from .dialogs.mcp_guide import open_mcp_guide
from .handlers import AppHandlers
from .refs import UIRefs
from .state import AppState
from .theme import COLORS, configure_page
from .theme.colors import FONT_SEMIBOLD
from .theme.page import ASSETS_DIR
from .theme.styles import OUTLINE_STYLE, RAIL_STYLE

logger = get_logger("app")


class App:
    def __init__(self, container: Container | None = None) -> None:
        c = container or Container()
        self.pm: IProfileManager = c.profile_manager
        self.bl: IBrowserLauncher = c.browser_launcher
        self.ps: IProxyService = c.proxy_service
        self.state = AppState()
        self.page: ft.Page | None = None
        self._reconcile_started = False
        self.refs: UIRefs | None = None
        self.filter = "all"
        self.active_page = "__all__"
        self._live_since: dict[str, str] = {}
        self.monitor = MonitorService()
        self._last_monitor_snap: MonitorSnapshot | None = None
        self._filter_column = ft.Column(spacing=2)
        self._header_slot = ft.Container()
        self._pages_bar_slot = ft.Container()
        self._monitor_bar_slot = ft.Container()
        self._trash_btn = ft.OutlinedButton(
            get_string("trash_button", count=0),
            icon=ft.Icons.DELETE_OUTLINE,
            width=208,
            height=36,
            style=RAIL_STYLE,
        )
        self._cooldown_btn = ft.OutlinedButton(
            self._cooldown_btn_text(),
            icon=ft.Icons.TIMER_OUTLINED,
            height=40,
            style=OUTLINE_STYLE,
        )
        self._arrange_btn = ft.OutlinedButton(
            get_string("arrange_windows_btn"),
            icon=ft.Icons.GRID_VIEW_ROUNDED,
            height=40,
            style=OUTLINE_STYLE,
            visible=False,
            tooltip=get_string("tooltip_arrange_windows"),
        )
        c.event_bus.subscribe(self.state.schedule_refresh)
        self.h = AppHandlers(
            pm=self.pm,
            bl=self.bl,
            ps=self.ps,
            state=self.state,
            get_page=lambda: self.page,
            get_refs=lambda: self.refs,
            log_fn=self._log,
            refresh_fn=self._refresh_profiles,
            get_page_profiles=self._get_page_profiles,
            monitor=self.monitor,
        )

    def run(self) -> None:
        ft.run(self._main, assets_dir=ASSETS_DIR)

    def _main(self, page: ft.Page) -> None:
        self.page = page
        configure_page(page)

        def _on_key(e: ft.KeyboardEvent) -> None:
            self.state.shift_down = bool(e.shift)

        page.on_keyboard_event = _on_key
        fp = ft.FilePicker()
        page.services.append(fp)
        self.clipboard = ft.Clipboard()
        page.services.append(self.clipboard)
        self._file_picker = fp
        self._build_ui()
        self.state._last_running_snapshot = self.bl.running_profile_names()
        if not self._reconcile_started:
            self._reconcile_started = True
            page.run_task(self._ui_reconcile_loop)

    def _build_ui(self) -> None:
        """(Re)build every control; strings are read at build time."""
        assert self.page is not None
        self.refs = build_ui_refs(
            pm=self.pm,
            on_change_page=self._change_page,
            file_picker=self._file_picker,
            on_search=self._on_search,
            sort_by=self.state.sort_by,
            on_sort=self._on_sort,
            on_reorder=self._on_reorder,
        )
        self._filter_column = ft.Column(spacing=2)
        self._header_slot = ft.Container()
        self._pages_bar_slot = ft.Container()
        self._monitor_bar_slot = ft.Container()
        self._trash_btn = ft.OutlinedButton(
            get_string("trash_button", count=len(self.pm.list_trashed_profiles())),
            icon=ft.Icons.DELETE_OUTLINE,
            width=208,
            height=36,
            style=RAIL_STYLE,
            on_click=lambda _: self.h.on_open_trash(),
        )
        self._cooldown_btn = ft.OutlinedButton(
            self._cooldown_btn_text(),
            icon=ft.Icons.TIMER_OUTLINED,
            height=40,
            style=OUTLINE_STYLE,
            on_click=lambda _: self.h.on_open_cooldown_settings(),
        )
        self._arrange_btn = ft.OutlinedButton(
            get_string("arrange_windows_btn"),
            icon=ft.Icons.GRID_VIEW_ROUNDED,
            height=40,
            style=OUTLINE_STYLE,
            visible=False,
            tooltip=get_string("tooltip_arrange_windows"),
            on_click=lambda _: self.h.on_arrange_windows(),
        )
        self.page.title = get_string("window_title")
        self.page.controls.clear()
        self.page.add(self._build_root_layout(self.refs))
        self._restore_log()
        self._refresh_profiles()

    def _restore_log(self) -> None:
        """Refill a freshly built log pane (flush_log only returns new lines)."""
        r = self.refs
        assert r is not None
        lines = self.state.get_all_log_lines()[-6:]
        r.log_text.value = "\n".join(lines)
        r.log_column.height = max(72, len(lines) * 18 + 20)
        r.log_column.visible = bool(lines) and not self.state.log_collapsed
        r.log_toggle_btn.icon = (
            ft.Icons.KEYBOARD_ARROW_RIGHT
            if self.state.log_collapsed
            else ft.Icons.KEYBOARD_ARROW_DOWN
        )

    def _set_language(self, code: str) -> None:
        if code == current_language():
            return
        set_language(code)
        self.state.search_query = ""
        self._build_ui()

    def _cooldown_btn_text(self) -> str:
        mins = self.monitor.cooldown_minutes
        return (
            get_string("cooldown_btn_label", mins=mins)
            if mins > 0
            else get_string("cooldown_btn_off")
        )

    def _build_root_layout(self, r: UIRefs) -> ft.Row:
        sidebar = build_sidebar(
            r.stats_text,
            r.running_text,
            r.log_text,
            r.log_column,
            r.log_toggle_btn,
            on_new_profile=lambda _: self.h.open_add_dialog(),
            on_import=self.h.on_import,
            on_export=lambda _: self.h.on_export_open(),
            on_mcp=lambda _: open_mcp_guide(self.page, self.clipboard),
            on_toggle_log=lambda _: self.h.toggle_log(),
            on_fullscreen_log=lambda _: self.h.open_log_fullscreen(),
            filter_column=self._filter_column,
            language=current_language(),
            on_language=self._set_language,
            trash_btn=self._trash_btn,
        )
        check_proxy_btn = ft.OutlinedButton(
            get_string("bulk_check_proxy"),
            icon=ft.Icons.NETWORK_CHECK,
            height=40,
            style=OUTLINE_STYLE,
            on_click=lambda _: self.h.on_bulk_check_proxy(),
        )
        content = build_content_area(
            r.content_subtitle,
            r.profile_list_area,
            r.prev_btn,
            r.next_btn,
            r.page_label,
            r.bulk_bar,
            r.search_field,
            header_slot=self._header_slot,
            sort_control=r.sort_dropdown,
            pages_bar=self._pages_bar_slot,
            monitor_bar=self._monitor_bar_slot,
            check_proxy_btn=check_proxy_btn,
            cooldown_btn=self._cooldown_btn,
            arrange_btn=self._arrange_btn,
        )
        return ft.Row(
            expand=True,
            spacing=0,
            controls=[
                sidebar,
                content,
            ],
        )

    def _on_search(self, query: str) -> None:
        self.state.search_query = query.strip().lower()
        self.state.current_page = 1
        self._refresh_profiles()

    def _on_sort(self, sort_by: str) -> None:
        self.state.sort_by = sort_by
        self.state.current_page = 1
        self._refresh_profiles()

    def _on_select_custom_page(self, page_id: str) -> None:
        self.active_page = page_id
        self.state.current_page = 1
        self._refresh_profiles()

    def _on_add_custom_page(self) -> None:
        if self.page is None:
            return
        open_custom_page_dialog(
            self.page,
            self.pm,
            on_saved=self._on_select_custom_page,
            preselected_names=self.state.selected_names(),
        )

    def _on_edit_custom_page(self, page_name: str) -> None:
        if self.page is None:
            return
        open_custom_page_dialog(
            self.page,
            self.pm,
            on_saved=self._on_select_custom_page,
            on_deleted=lambda: self._on_select_custom_page("__all__"),
            editing_page=page_name,
        )

    def _on_bulk_add_to_page(self) -> None:
        if self.page is None:
            return
        selected = list(self.state.selected_names())
        if not selected:
            return

        def _done(pg: str) -> None:
            self.state.clear_selection()
            self._on_select_custom_page(pg)

        open_add_to_page_dialog(self.page, self.pm, selected, on_done=_done)

    def _on_bulk_remove_from_page(self) -> None:
        if self.active_page == "__all__":
            return
        selected = list(self.state.selected_names())
        if not selected:
            return
        if self.pm.remove_profiles_from_page(self.active_page, selected):
            self.state.clear_selection()
            self._refresh_profiles()

    def _on_reorder_custom_page(self, src_page: str, target_page: str) -> None:
        pages = self.pm.list_custom_pages()
        src_idx = next(
            (i for i, pg in enumerate(pages) if pg.casefold() == src_page.casefold()),
            None,
        )
        if src_idx is None:
            return
        if target_page == "__all__":
            target_idx = 0
        else:
            target_idx = next(
                (i for i, pg in enumerate(pages) if pg.casefold() == target_page.casefold()),
                None,
            )
            if target_idx is None or target_idx == src_idx:
                return
        moved = pages.pop(src_idx)
        pages.insert(target_idx, moved)
        if self.pm.reorder_custom_pages(pages):
            self._refresh_profiles()

    def _on_reorder(self, old_index: int, new_index: int) -> None:
        if self.state.sort_by != "custom" or old_index == new_index:
            return
        _, page_profiles, _ = self._get_page_profiles()
        visible_names = [p.name for p in page_profiles]
        if not (0 <= old_index < len(visible_names)):
            return
        moved = visible_names.pop(old_index)
        new_index = max(0, min(new_index, len(visible_names)))
        visible_names.insert(new_index, moved)
        visible_set = set(visible_names)
        it = iter(visible_names)
        full_order = [next(it) if k in visible_set else k for k in self.pm.profiles]
        if self.pm.reorder_profiles(full_order):
            self._refresh_profiles()

    def _on_filter(self, fid: str) -> None:
        self.filter = fid
        self.state.current_page = 1
        self._refresh_profiles()

    def _filter_tests(self) -> dict:
        snap = self._last_monitor_snap
        return {
            "all": lambda p: True,
            "running": lambda p: self.bl.is_running(p.name),
            "warnings": lambda p: bool(
                snap and p.name in snap.profiles and snap.profiles[p.name].has_warning
            ),
            "proxy": lambda p: bool(p.proxy),
            "direct": lambda p: not p.proxy,
            "chrome": lambda p: p.engine == "chrome",
        }

    def _get_page_profiles(self) -> tuple[list, list, int]:
        all_profiles = self.pm.list_profiles()
        if self.active_page != "__all__":
            custom_pages = self.pm.list_custom_pages()
            if not any(pg.casefold() == self.active_page.casefold() for pg in custom_pages):
                self.active_page = "__all__"
            else:
                target_pg = self.active_page.casefold()
                all_profiles = [
                    p for p in all_profiles if any(pg.casefold() == target_pg for pg in p.pages)
                ]
        if self.filter.startswith("group:"):
            target_group = self.filter.removeprefix("group:")
            all_profiles = [p for p in all_profiles if p.group == target_group]
        else:
            test = self._filter_tests().get(self.filter, lambda p: True)
            all_profiles = [p for p in all_profiles if test(p)]
        if q := self.state.search_query:
            all_profiles = [
                p
                for p in all_profiles
                if q in p.name.lower()
                or q in (p.proxy or "").lower()
                or q in (p.group or "").lower()
                or q in (p.notes or "").lower()
            ]
        sort_by = self.state.sort_by
        if sort_by == "name_asc":
            all_profiles.sort(key=lambda p: p.name.casefold())
        elif sort_by == "name_desc":
            all_profiles.sort(key=lambda p: p.name.casefold(), reverse=True)
        elif sort_by == "group":
            all_profiles.sort(
                key=lambda p: (not bool(p.group), (p.group or "").casefold(), p.name.casefold())
            )
        elif sort_by == "running":
            all_profiles.sort(key=lambda p: (not self.bl.is_running(p.name), p.name.casefold()))
        elif sort_by == "proxy":
            all_profiles.sort(key=lambda p: (not bool(p.proxy), p.name.casefold()))
        elif sort_by in ("cooldown_desc", "cooldown_asc"):
            snap = self._last_monitor_snap or self.monitor.snapshot(
                self.pm.list_profiles(),
                self.bl.running_profile_names() | self.state.loading,
            )

            def _cd_desc_key(p: object) -> tuple:
                pname = getattr(p, "name", "")
                if self.bl.is_running(pname):
                    return (2, 0, pname.casefold())
                info = snap.profiles.get(pname)
                if info is not None and info.cooldown_mins is not None:
                    port_busy = 1 if (info.busy_by or info.port_cooldown_by) else 0
                    return (0, port_busy, -info.cooldown_mins, pname.casefold())
                return (1, 0, 0, pname.casefold())

            def _cd_asc_key(p: object) -> tuple:
                pname = getattr(p, "name", "")
                if self.bl.is_running(pname):
                    return (0, 0, pname.casefold())
                info = snap.profiles.get(pname)
                if info is not None and info.cooldown_mins is not None:
                    return (1, info.cooldown_mins, pname.casefold())
                return (2, 0, pname.casefold())

            all_profiles.sort(
                key=_cd_desc_key if sort_by == "cooldown_desc" else _cd_asc_key
            )

        per_page = self.state.items_per_page
        if per_page <= 0:
            self.state.current_page = 1
            return all_profiles, all_profiles, 1
        total = max(1, (len(all_profiles) + per_page - 1) // per_page)
        self.state.current_page = min(self.state.current_page, total)
        start = (self.state.current_page - 1) * per_page
        return all_profiles, all_profiles[start : start + per_page], total

    def _refresh_profiles(self) -> None:
        r = self.refs
        assert r is not None
        self._update_stats()
        self._flush_log()

        all_names = set(self.pm.profiles)
        for stale in self.state.selected_names() - all_names:
            self.state.toggle_selection(stale)

        running = self.bl.running_profile_names()
        # A failure mark lasts until the profile runs again or is gone.
        self.state.failed.intersection_update(all_names - running)
        for name in running - set(self._live_since):
            self._live_since[name] = time.strftime("%H:%M")
            p_live = self.pm.profiles.get(name)
            self.monitor.record_started(name, p_live.proxy if p_live else None)
        for name in set(self._live_since) - running:
            del self._live_since[name]
            self.monitor.record_stopped(name)

        everything = self.pm.list_profiles()
        snap = self.monitor.snapshot(everything, running | self.state.loading)
        self._last_monitor_snap = snap
        alert_bar = build_monitor_bar(snap, len(everything))
        self._monitor_bar_slot.content = alert_bar
        self._monitor_bar_slot.visible = alert_bar is not None
        self._cooldown_btn.content = self._cooldown_btn_text()
        self._arrange_btn.visible = len(running) >= 1
        if hasattr(self.pm, "list_trashed_profiles"):
            self._trash_btn.content = get_string(
                "trash_button",
                count=len(self.pm.list_trashed_profiles()),
            )

        all_profiles, page_profiles, total_pages = self._get_page_profiles()

        custom_pages = self.pm.list_custom_pages()
        page_counts = [
            (
                pg,
                sum(
                    1
                    for p in everything
                    if any(item.casefold() == pg.casefold() for item in p.pages)
                ),
            )
            for pg in custom_pages
        ]
        self._pages_bar_slot.content = build_pages_bar(
            self.active_page,
            len(everything),
            page_counts,
            self._on_select_custom_page,
            self._on_add_custom_page,
            self._on_edit_custom_page,
            self._on_reorder_custom_page,
        )

        tests = self._filter_tests()
        group_counts: dict[str, int] = {}
        for p in everything:
            if p.group:
                group_counts[p.group] = group_counts.get(p.group, 0) + 1
        group_counts = dict(sorted(group_counts.items(), key=lambda kv: kv[0].casefold()))
        self._filter_column.controls = build_filter_buttons(
            self.filter,
            {fid: sum(1 for p in everything if test(p)) for fid, test in tests.items()},
            self._on_filter,
            group_counts=group_counts,
        )
        names = [p.name for p in page_profiles]
        all_sel = bool(names) and all(self.state.is_selected(n) for n in names)
        any_sel = any(self.state.is_selected(n) for n in names)
        self._header_slot.content = (
            build_header_row(
                all_sel,
                any_sel,
                lambda select: self.h.on_select_all_page() if select else self.h.on_deselect_page(),
            )
            if page_profiles
            else None
        )

        is_custom_sort = self.state.sort_by == "custom"

        r.profile_list_area.controls = (
            [
                build_profile_card(
                    p,
                    self.state.is_loading(p.name),
                    self.bl.is_running(p.name),
                    self.h.on_launch,
                    self.h.on_edit,
                    self.h.on_delete,
                    is_selected=self.state.is_selected(p.name),
                    on_select=self.h.on_toggle_select,
                    on_cookies=self.h.on_cookies,
                    device=self._device_label(p.name),
                    since=self._live_since.get(p.name, ""),
                    failed=p.name in self.state.failed,
                    draggable=is_custom_sort,
                    editing_note=self.state.editing_note == p.name,
                    on_start_edit_note=self.h.on_start_edit_note,
                    on_save_note=self.h.on_save_note,
                    editing_proxy=self.state.editing_proxy == p.name,
                    on_start_edit_proxy=self.h.on_start_edit_proxy,
                    on_save_proxy=self.h.on_save_proxy,
                    on_check_proxy=self.h.on_quick_check_proxy if p.proxy else None,
                    on_clone=self.h.on_clone,
                    on_focus_window=self.h.on_focus_window,
                    monitor_info=snap.profiles.get(p.name),
                    proxy_check_state=self.state.proxy_status.get(p.name),
                )
                for p in page_profiles
            ]
            if page_profiles
            else [self._build_empty_or_no_results()]
        )
        rebuild_bulk_bar(
            r.bulk_bar,
            self.state,
            page_profiles,
            {
                "launch": self.h.on_bulk_launch,
                "stop": self.h.on_bulk_stop,
                "delete": self.h.on_bulk_delete,
                "select_range": self.h.on_bulk_select_range,
                "set_group": self.h.on_bulk_set_group,
                "set_color": self.h.on_bulk_set_color,
                "set_proxy": self.h.on_bulk_set_proxy,
                "check_proxy": self.h.on_bulk_check_proxy,
                "export_zip": self.h.on_export_open,
                "add_to_page": self._on_bulk_add_to_page,
                "remove_from_page": self._on_bulk_remove_from_page,
                "select_page": self.h.on_select_all_page,
                "deselect_page": self.h.on_deselect_page,
                "clear": self.h.on_clear_selection,
            },
            active_page=self.active_page,
        )
        self._set_subtitle(r.content_subtitle)
        r.page_label.value = get_string(
            "page_of",
            current=self.state.current_page,
            total=total_pages,
        )
        r.prev_btn.disabled = self.state.current_page <= 1
        r.next_btn.disabled = self.state.current_page >= total_pages
        self._safe_update()

    @staticmethod
    def _device_label(name: str) -> str:
        info = fingerprint.summary(os.path.join(DATA_DIR, name))
        if not info:
            return ""
        cpu = f"{info['hardware_concurrency']} CPU"
        return f"{info['screen']} · {cpu}" if info.get("screen") else cpu

    def _build_empty_or_no_results(self) -> ft.Control:
        if self.pm.profiles:
            return ft.Container(
                key="__empty__",
                padding=ft.Padding.symmetric(vertical=56, horizontal=24),
                alignment=ft.Alignment(0, 0),
                content=ft.Column(
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                    controls=[
                        ft.Text(
                            get_string("no_search_results", query=self.state.search_query)
                            if self.state.search_query
                            else get_string("no_filter_results"),
                            size=14,
                            color=COLORS["text_sub"],
                        ),
                        ft.OutlinedButton(
                            get_string("clear_filters"),
                            on_click=lambda _: self._clear_filters(),
                        ),
                    ],
                ),
            )
        empty = build_empty_state(lambda _: self.h.open_add_dialog())
        empty.key = "__empty__"
        return empty

    def _clear_filters(self) -> None:
        self.filter = "all"
        self.active_page = "__all__"
        self.state.search_query = ""
        if self.refs and self.refs.search_field is not None:
            self.refs.search_field.value = ""
        self._refresh_profiles()

    def _change_page(self, delta: int) -> None:
        self.state.current_page += delta
        self._refresh_profiles()

    def _set_subtitle(self, text: ft.Text) -> None:
        """ "10 profile · 2 đang chạy", the running count in stamp violet."""
        c, r = len(self.pm.profiles), self.bl.running_count()
        head = get_string("profiles_configured", count=c) + " · "
        text.value = None
        text.spans = (
            [
                ft.TextSpan(head),
                ft.TextSpan(str(r), ft.TextStyle(color=COLORS["live"], font_family=FONT_SEMIBOLD)),
                ft.TextSpan(" " + get_string("profiles_running_suffix")),
            ]
            if r
            else [ft.TextSpan(head + get_string("profiles_none_running"))]
        )

    def _update_stats(self) -> None:
        r = self.refs
        if r:
            cnt = self.bl.running_count()
            r.stats_text.value = get_string(
                "total_profiles",
                count=len(self.pm.profiles),
            )
            r.running_text.value = (
                get_string("browsers_running", count=cnt)
                if cnt
                else get_string("no_active_sessions")
            )

    def _log(self, message: str) -> None:
        logger.info(message)
        if self.state.add_log(message):
            self.state.schedule_refresh()

    def _flush_log(self) -> None:
        text = self.state.flush_log()
        if text is not None and self.refs:
            lines = text.split("\n")
            sidebar_lines = lines[-6:]
            self.refs.log_text.value = "\n".join(sidebar_lines)
            self.refs.log_column.height = max(72, len(sidebar_lines) * 18 + 20)
            self.refs.log_column.visible = bool(sidebar_lines) and not self.state.log_collapsed

    def _safe_update(self) -> None:
        if not self.page:
            return
        try:
            with self.state._ui_update_lock:
                self.page.update()
        except Exception as e:
            logger.error("Error updating UI: %s", e)

    async def _ui_reconcile_loop(self) -> None:
        while self.page:
            try:
                running_now = self.bl.running_profile_names()
                changed = running_now != self.state._last_running_snapshot
                if changed:
                    self.state._last_running_snapshot = running_now
                if changed or self.state.consume_refresh():
                    self._refresh_profiles()
            except Exception as e:
                logger.error("Error in UI reconcile loop: %s", e)
            await asyncio.sleep(0.12)
