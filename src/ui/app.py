import asyncio
import os
import re
import threading
import time

import flet as ft

from ..core.config import DATA_DIR
from ..core.container import Container
from ..core.logging import get_logger
from ..core.strings import current_language, get_string, set_language
from ..interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ..models.profile import (
    compute_days_since,
    is_archived_profile,
    is_ban_page,
    is_ban_status,
    natural_name_key,
    parse_schedule_tag,
    schedule_sort_key,
)
from ..services.browser import fingerprint
from ..services.browser.window_title import focus_foxprofile_window
from ..services.monitor import (
    MonitorService,
    MonitorSnapshot,
    extract_proxy_endpoint,
    extract_proxy_port,
)
from ..utils.totp import parse_mail_credentials_line, run_safe_mail_checkpoint_scan
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
from .components.floating_tab_bar import FloatingProfileTabBar
from .components.profile_card import build_header_row
from .components.sidebar import build_filter_buttons
from .dialogs import (
    open_add_to_page_dialog,
    open_custom_page_dialog,
    open_token_burst_alert_dialog,
)
from .dialogs.mcp_guide import open_mcp_guide
from .handlers import AppHandlers
from .refs import UIRefs
from .state import AppState
from .theme import COLORS, configure_page
from .theme.colors import FONT_SEMIBOLD
from .theme.page import ASSETS_DIR, setup_windows_taskbar_icon
from .theme.styles import OUTLINE_STYLE

logger = get_logger("app")

_SEARCH_TOKEN_RE = re.compile(r"[0-9a-z\u00c0-\u024f\u1e00-\u1eff]+")
_SEARCH_EMAIL_DOM_RE = re.compile(r"@[a-z0-9.-]+\.[a-z]{2,}")


def _search_match_rank(prof: object, query: str) -> tuple[int, tuple] | None:
    """Return a (relevance_tier, natural_name_key) tuple if `prof` matches `query`, else None.

    Strips generic email domains (@hotmail.com/@gmail.com) unless '@' is in `query`,
    and matches proxy port/endpoint rather than random proxy credentials (:user:pass),
    so typing 1-2 characters immediately surfaces the closest matching profiles.
    """
    q = query.strip().lower()
    if not q:
        return (0, ())
    name_raw = getattr(prof, "name", "") or ""
    name_l = name_raw.lower()
    has_at = "@" in q
    name_clean = name_l if has_at else _SEARCH_EMAIL_DOM_RE.sub("", name_l)
    name_tokens = _SEARCH_TOKEN_RE.findall(name_clean)

    email_raw = (getattr(prof, "email", None) or "").lower()
    email_target = email_raw if has_at else email_raw.split("@", 1)[0]
    rec_raw = (getattr(prof, "recovery_email", None) or "").lower()
    rec_target = rec_raw if has_at else rec_raw.split("@", 1)[0]

    proxy_raw = getattr(prof, "proxy", None) or ""
    port_digits = extract_proxy_port(proxy_raw).lstrip(":")
    endpoint = extract_proxy_endpoint(proxy_raw).lower()
    q_port = q.lstrip(":")

    group_l = (getattr(prof, "group", None) or "").lower()
    status_l = (getattr(prof, "status", None) or "").lower()
    notes_l = (getattr(prof, "notes", None) or "").lower()

    nkey = natural_name_key(name_raw)

    # Tier 0: Exact profile name or exact word/number token in profile name
    if name_clean == q or q in name_tokens:
        return (0, nkey)
    # Tier 1: Profile name or word/number token starts with query
    if name_clean.startswith(q) or any(tok.startswith(q) for tok in name_tokens):
        return (1, nkey)
    # Tier 2: Email username starts with query, or proxy port starts with query
    if (email_target and email_target.startswith(q)) or (
        q_port.isdigit() and len(q_port) >= 2 and port_digits.startswith(q_port)
    ):
        return (2, nkey)
    # Tier 3: Substring in profile name, or token prefix in group / status / notes
    if (
        q in name_clean
        or any(tok.startswith(q) for tok in _SEARCH_TOKEN_RE.findall(group_l))
        or any(tok.startswith(q) for tok in _SEARCH_TOKEN_RE.findall(status_l))
        or any(tok.startswith(q) for tok in _SEARCH_TOKEN_RE.findall(notes_l))
    ):
        return (3, nkey)
    # Tier 4: Substring in email username, recovery email, group, status, notes, or proxy port
    if len(q) >= 2 and (
        q in email_target
        or q in rec_target
        or q in group_l
        or q in status_l
        or q in notes_l
        or (q_port.isdigit() and q_port in port_digits)
    ):
        return (4, nkey)
    # Tier 5: Proxy host / IP / cluster match for >= 3 characters
    if len(q) >= 3 and q in endpoint:
        return (5, nkey)
    return None


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
        init_pages = self.pm.list_custom_pages()
        self.active_page = (
            init_pages[0] if getattr(self.pm, "all_page_index", 0) > 0 and init_pages else "__all__"
        )
        self._live_since: dict[str, str] = {}
        self._staged_launches: set[str] = set()
        self._alerted_token_bursts: set[str] = set()
        self._mail_auto_scan_running: bool = False
        self.monitor: MonitorService = c.monitor_service
        saved_sort = getattr(self.monitor, "sort_by", "custom")
        if isinstance(saved_sort, str) and saved_sort:
            self.state.sort_by = saved_sort
        self._last_monitor_snap: MonitorSnapshot | None = None
        self._filter_column = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO, expand=True)
        self._header_slot = ft.Container()
        self._pages_bar_slot = ft.Container()
        self._monitor_bar_slot = ft.Container()
        self._running_tabs_slot = ft.Container()
        self._floating_tab_bar = FloatingProfileTabBar(
            on_select_tab=self._on_select_browser_tab,
            on_close_tab=self._on_close_browser_tab,
            on_toggle_single_stage=self._on_toggle_single_stage,
            on_focus_foxprofile=self._on_focus_foxprofile,
            on_avatar_changed=self.state.schedule_refresh,
        )
        self._trash_btn = ft.Container(
            width=34,
            height=34,
            border_radius=7,
            bgcolor=COLORS["cover_deep"],
            border=ft.Border.all(1, ft.Colors.with_opacity(0.25, COLORS["cover_muted"])),
            alignment=ft.Alignment(0, 0),
            ink=True,
            tooltip=get_string("trash_button", count=0),
            content=ft.Icon(
                ft.Icons.DELETE_OUTLINE,
                size=16,
                color=COLORS["cover_ink"],
            ),
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
        setup_windows_taskbar_icon()
        try:
            ft.run(self._main, assets_dir=ASSETS_DIR)
        finally:
            self.page = None

    def _main(self, page: ft.Page) -> None:
        self.page = page
        configure_page(page)
        page.on_keyboard_event = self._handle_key_event
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

    def _has_open_dialog(self) -> bool:
        if self.page is None:
            return False
        dialogs = getattr(self.page, "_dialogs", None)
        controls = getattr(dialogs, "controls", None) or ()
        return any(getattr(d, "open", False) for d in controls)

    def _handle_key_event(self, e: ft.KeyboardEvent) -> None:
        self.state.shift_down = bool(e.shift)
        if self._has_open_dialog():
            return
        key = (e.key or "").strip().lower()
        ctrl = bool(e.ctrl) and not bool(e.alt) and not bool(e.meta)
        alt_only = bool(e.alt) and not bool(e.ctrl) and not bool(e.meta)
        no_mod = not bool(e.ctrl) and not bool(e.alt) and not bool(e.meta)
        digit = key.removeprefix("digit")
        if alt_only and len(digit) == 1 and digit in "123456789":
            idx = int(digit) - 1
            running_ordered = self._ordered_running_names()
            if 0 <= idx < len(running_ordered):
                self._on_select_browser_tab(running_ordered[idx])
            return
        if ctrl and key == "f":
            if (
                self.refs is not None
                and self.refs.search_field is not None
                and self.page is not None
                and hasattr(self.page, "run_task")
            ):
                self.page.run_task(self.refs.search_field.focus)
        elif ctrl and key == "n":
            self.h.open_add_dialog(self.active_page)
        elif no_mod and key == "escape":
            if (
                self.state.editing_note is not None
                or self.state.editing_email is not None
                or self.state.editing_proxy is not None
            ):
                self.state.editing_note = None
                self.state.editing_email = None
                self.state.editing_proxy = None
                self._refresh_profiles()
            elif self.state.selected_names():
                self.h.on_clear_selection()
            elif self.state.search_query:
                self.state.search_query = ""
                if self.refs is not None and self.refs.search_field is not None:
                    self.refs.search_field.value = ""
                    if isinstance(self.refs.search_field.suffix, ft.Control):
                        self.refs.search_field.suffix.visible = False
                self._refresh_profiles(reuse_snapshot=True)

    def _ordered_running_names(self) -> list[str]:
        running_set = self.bl.running_profile_names() | self.state.loading
        if not running_set:
            return []
        ordered = [p.name for p in self.pm.list_profiles() if p.name in running_set]
        seen = set(ordered)
        for extra in sorted(running_set):
            if extra not in seen:
                ordered.append(extra)
        return ordered

    def _on_select_browser_tab(self, name: str) -> None:
        self.h.on_focus_window(name)

    def _on_close_browser_tab(self, name: str) -> None:
        if self.bl.is_running(name):
            self.h.on_launch(name)

    def _on_toggle_single_stage(self) -> None:
        self.state.single_stage_mode = True
        if self.state.active_browser_tab:
            self.h.on_focus_window(self.state.active_browser_tab)
        else:
            self.state.schedule_refresh()

    def _on_toggle_floating_bar(self) -> None:
        self.state.floating_tab_bar = not self.state.floating_tab_bar
        self.state.schedule_refresh()

    def _on_focus_foxprofile(self) -> None:
        focus_foxprofile_window(get_string("window_title"), toggle=True)

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
        self._filter_column = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO, expand=True)
        self._header_slot = ft.Container()
        self._pages_bar_slot = ft.Container()
        self._monitor_bar_slot = ft.Container()
        self._running_tabs_slot = ft.Container()
        self._trash_btn = ft.Container(
            width=34,
            height=34,
            border_radius=7,
            bgcolor=COLORS["cover_deep"],
            border=ft.Border.all(1, ft.Colors.with_opacity(0.25, COLORS["cover_muted"])),
            alignment=ft.Alignment(0, 0),
            ink=True,
            tooltip=get_string("trash_button", count=len(self.pm.list_trashed_profiles())),
            on_click=lambda _: self.h.on_open_trash(),
            content=ft.Icon(
                ft.Icons.DELETE_OUTLINE,
                size=16,
                color=COLORS["cover_ink"],
            ),
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

    def _cooldown_chip_text(self) -> str:
        mins = self.monitor.cooldown_minutes
        return f"{mins}p" if mins > 0 else get_string("cooldown_preset_off")

    def _build_root_layout(self, r: UIRefs) -> ft.Row:
        sidebar = build_sidebar(
            r.stats_text,
            r.running_text,
            r.log_text,
            r.log_column,
            r.log_toggle_btn,
            on_new_profile=lambda _: self.h.open_add_dialog(self.active_page),
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
        self._refresh_profiles(reuse_snapshot=True)

    def _on_sort(self, sort_by: str) -> None:
        self.state.sort_by = sort_by
        self.state.current_page = 1
        if hasattr(self.monitor, "set_sort_by"):
            self.monitor.set_sort_by(sort_by)
        if (
            self.refs is not None
            and self.refs.sort_dropdown is not None
            and hasattr(self.refs.sort_dropdown, "sync_value")
        ):
            self.refs.sort_dropdown.sync_value(sort_by)  # type: ignore[attr-defined]
        self._refresh_profiles(reuse_snapshot=True)

    def _on_select_custom_page(self, page_id: str) -> None:
        self.active_page = page_id
        if self.filter == "archived":
            self.filter = "all"
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
        all_idx = max(0, min(getattr(self.pm, "all_page_index", 0), len(pages)))
        tabs_order = [*pages[:all_idx], "__all__", *pages[all_idx:]]
        src_idx = next(
            (i for i, pg in enumerate(tabs_order) if pg.casefold() == src_page.casefold()),
            None,
        )
        target_idx = next(
            (i for i, pg in enumerate(tabs_order) if pg.casefold() == target_page.casefold()),
            None,
        )
        if src_idx is None or target_idx is None or src_idx == target_idx:
            return
        moved = tabs_order.pop(src_idx)
        tabs_order.insert(target_idx, moved)
        if self.pm.reorder_custom_pages(tabs_order):
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

    def _is_archive_view(self) -> bool:
        if self.filter == "archived" or is_ban_page(self.active_page):
            return True
        if self.filter.startswith("group:") and is_ban_page(self.filter.removeprefix("group:")):
            return True
        return bool(
            self.filter.startswith("status:")
            and is_ban_status(self.filter.removeprefix("status:"))
        )

    def _on_filter(self, fid: str) -> None:
        if fid == "all" and is_ban_page(self.active_page):
            self.active_page = "__all__"
        self.filter = fid
        self.state.current_page = 1
        self._refresh_profiles()

    def _filter_tests(self) -> dict:
        snap = self._last_monitor_snap
        cd_target = self.monitor.cooldown_minutes
        return {
            "all": lambda p: True,
            "archived": is_archived_profile,
            "pinned": lambda p: bool(getattr(p, "pinned", False)),
            "running": lambda p: self.bl.is_running(p.name),
            "ready_launch": lambda p: bool(
                p.proxy
                and not self.bl.is_running(p.name)
                and not self.state.is_loading(p.name)
                and not (
                    snap
                    and p.name in snap.profiles
                    and (snap.profiles[p.name].has_warning or snap.profiles[p.name].cluster_busy_by)
                )
            ),
            "cooldown": lambda p: bool(
                snap
                and p.name in snap.profiles
                and not self.bl.is_running(p.name)
                and (
                    snap.profiles[p.name].busy_by
                    or snap.profiles[p.name].port_cooldown_by is not None
                    or (
                        cd_target > 0
                        and snap.profiles[p.name].cooldown_mins is not None
                        and snap.profiles[p.name].cooldown_mins < cd_target
                    )
                )
            ),
            "warnings": lambda p: bool(
                snap and p.name in snap.profiles and snap.profiles[p.name].has_warning
            ),
            "proxy": lambda p: bool(p.proxy),
            "direct": lambda p: not p.proxy,
            "chrome": lambda p: p.engine == "chrome",
        }

    def _get_page_profiles(self) -> tuple[list, list, int]:
        all_profiles = self.pm.list_profiles()
        in_archive = self._is_archive_view()
        if self.filter == "archived":
            all_profiles = [p for p in all_profiles if is_archived_profile(p)]
        else:
            if not in_archive:
                all_profiles = [p for p in all_profiles if not is_archived_profile(p)]
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
            elif self.filter.startswith("status:"):
                target_status = self.filter.removeprefix("status:").casefold()

                def _matches_status(prof: object) -> bool:
                    raw_st = (getattr(prof, "status", None) or "").strip()
                    if raw_st.casefold() == target_status:
                        return True
                    parsed_st = parse_schedule_tag(raw_st)
                    return parsed_st is not None and parsed_st[0].casefold() == target_status

                if not any(_matches_status(p) for p in all_profiles):
                    self.filter = "all"
                else:
                    all_profiles = [p for p in all_profiles if _matches_status(p)]
            else:
                test = self._filter_tests().get(self.filter, lambda p: True)
                all_profiles = [p for p in all_profiles if test(p)]
        if q := self.state.search_query:
            ranked = [
                (rank, p)
                for p in all_profiles
                if (rank := _search_match_rank(p, q)) is not None
            ]
            if not ranked and (self.active_page != "__all__" or self.filter != "all"):
                fallback_pool = [
                    p
                    for p in self.pm.list_profiles()
                    if (is_archived_profile(p) if in_archive else not is_archived_profile(p))
                ]
                ranked = [
                    (rank, p)
                    for p in fallback_pool
                    if (rank := _search_match_rank(p, q)) is not None
                ]
            ranked.sort(key=lambda item: item[0])
            if len(q) <= 2:
                ranked = ranked[:12]
            all_profiles = [p for _, p in ranked]
        else:
            sort_by = self.state.sort_by
            if sort_by == "schedule":
                all_profiles.sort(
                    key=lambda p: (schedule_sort_key(p.status), natural_name_key(p.name))
                )
            elif sort_by == "name_asc":
                all_profiles.sort(key=lambda p: natural_name_key(p.name))
            elif sort_by == "name_desc":
                all_profiles.sort(key=lambda p: natural_name_key(p.name), reverse=True)
            elif sort_by == "group":
                all_profiles.sort(
                    key=lambda p: (
                        not bool(p.group),
                        natural_name_key(p.group or ""),
                        natural_name_key(p.name),
                    )
                )
            elif sort_by == "running":
                all_profiles.sort(
                    key=lambda p: (not self.bl.is_running(p.name), natural_name_key(p.name))
                )
            elif sort_by == "proxy":
                all_profiles.sort(key=lambda p: (not bool(p.proxy), natural_name_key(p.name)))
            elif sort_by in ("cooldown_desc", "cooldown_asc"):
                snap = self._last_monitor_snap or self.monitor.snapshot(
                    self.pm.list_profiles(),
                    self.bl.running_profile_names() | self.state.loading,
                )

                def _cd_desc_key(p: object) -> tuple:
                    pname = getattr(p, "name", "")
                    if self.bl.is_running(pname):
                        return (2, 0, natural_name_key(pname))
                    info = snap.profiles.get(pname)
                    if info is not None and info.cooldown_mins is not None:
                        port_busy = 1 if (info.busy_by or info.port_cooldown_by) else 0
                        return (0, port_busy, -info.cooldown_mins, natural_name_key(pname))
                    return (1, 0, 0, natural_name_key(pname))

                def _cd_asc_key(p: object) -> tuple:
                    pname = getattr(p, "name", "")
                    if self.bl.is_running(pname):
                        return (0, 0, natural_name_key(pname))
                    info = snap.profiles.get(pname)
                    if info is not None and info.cooldown_mins is not None:
                        return (1, info.cooldown_mins, natural_name_key(pname))
                    return (2, 0, natural_name_key(pname))

                all_profiles.sort(key=_cd_desc_key if sort_by == "cooldown_desc" else _cd_asc_key)
            elif sort_by in ("trust_desc", "trust_asc"):
                snap = self._last_monitor_snap or self.monitor.snapshot(
                    self.pm.list_profiles(),
                    self.bl.running_profile_names() | self.state.loading,
                )

                def _trust_key(p: object) -> tuple:
                    pname = getattr(p, "name", "")
                    info = snap.profiles.get(pname)
                    score = info.trust_score if info is not None else 0
                    return (-score if sort_by == "trust_desc" else score, natural_name_key(pname))

                all_profiles.sort(key=_trust_key)
            elif sort_by in ("age_desc", "age_asc"):
                snap = self._last_monitor_snap or self.monitor.snapshot(
                    self.pm.list_profiles(),
                    self.bl.running_profile_names() | self.state.loading,
                )

                def _age_key(p: object) -> tuple:
                    pname = getattr(p, "name", "")
                    info = snap.profiles.get(pname)
                    days = (
                        info.age_days
                        if info is not None
                        else compute_days_since(getattr(p, "created_at", None))
                    )
                    if days is None:
                        return (1, 0, natural_name_key(pname))
                    return (0, -days if sort_by == "age_desc" else days, natural_name_key(pname))

                all_profiles.sort(key=_age_key)

            # Pinned profiles always float to the top while preserving the active sort within each group
            all_profiles.sort(key=lambda p: not bool(getattr(p, "pinned", False)))

        per_page = self.state.items_per_page
        if per_page <= 0:
            self.state.current_page = 1
            return all_profiles, all_profiles, 1
        total = max(1, (len(all_profiles) + per_page - 1) // per_page)
        self.state.current_page = min(self.state.current_page, total)
        start = (self.state.current_page - 1) * per_page
        return all_profiles, all_profiles[start : start + per_page], total

    def _refresh_profiles(self, *, reuse_snapshot: bool = False) -> None:
        r = self.refs
        assert r is not None
        self._update_stats()
        self._flush_log()

        all_names = set(self.pm.profiles)
        for stale in self.state.selected_names() - all_names:
            self.state.toggle_selection(stale)

        running = self.bl.running_profile_names()
        active_or_loading = running | self.state.loading
        self._staged_launches.intersection_update(active_or_loading)
        for name in active_or_loading - self._staged_launches:
            self._staged_launches.add(name)
            self.state.active_browser_tab = name
            if hasattr(self.bl, "switch_profile") and "PYTEST_CURRENT_TEST" not in os.environ:

                def _auto_stage_on_appear(target: str = name) -> None:
                    for _ in range(90):
                        if target not in self._staged_launches:
                            return
                        cur = self.state.active_browser_tab or target
                        if self.bl.switch_profile(cur, single_stage=True):
                            time.sleep(0.6)
                            self.bl.switch_profile(
                                self.state.active_browser_tab or cur,
                                single_stage=True,
                            )
                            return
                        time.sleep(0.3)

                threading.Thread(target=_auto_stage_on_appear, daemon=True).start()

        # A failure mark lasts until the profile runs again or is gone.
        self.state.failed.intersection_update(all_names - running)
        ready_running = running - self.state.loading
        newly_ready = ready_running - set(self._live_since)
        for name in newly_ready:
            self._live_since[name] = time.strftime("%H:%M")
            p_live = self.pm.profiles.get(name)
            self.monitor.record_started(name, p_live.proxy if p_live else None)
            if hasattr(self.bl, "switch_profile"):
                cur_tab = self.state.active_browser_tab or name
                self.bl.switch_profile(cur_tab, single_stage=True)
        stopped_scheduled: list[str] = []
        for name in set(self._live_since) - running:
            del self._live_since[name]
            if name in self.state.failed:
                self.monitor.record_launch_failed(name)
            else:
                self.monitor.record_stopped(name)
                p_done = self.pm.profiles.get(name)
                if p_done is not None and parse_schedule_tag(p_done.status) is not None:
                    stopped_scheduled.append(name)
        if stopped_scheduled:
            self.pm.bulk_update_status(stopped_scheduled, None)
        self._alerted_token_bursts.intersection_update(running)

        everything = self.pm.list_profiles()
        if reuse_snapshot and self._last_monitor_snap is not None:
            snap = self._last_monitor_snap
        else:
            snap = self.monitor.snapshot(everything, running | self.state.loading)
            self._last_monitor_snap = snap
        ordered_running = self._ordered_running_names()
        if ordered_running:
            if self.state.active_browser_tab not in ordered_running:
                self.state.active_browser_tab = ordered_running[0]
        else:
            self.state.active_browser_tab = None

        self._running_tabs_slot.content = None
        self._running_tabs_slot.visible = False
        self._floating_tab_bar.sync(
            ordered_running,
            self.state.loading,
            self.state.active_browser_tab,
            self.state.single_stage_mode,
            self.state.floating_tab_bar,
        )

        alert_bar = build_monitor_bar(
            snap,
            len(everything),
            on_open_mail_pool=self.h.on_open_mail_pool,
        )
        self._monitor_bar_slot.content = alert_bar
        self._monitor_bar_slot.visible = alert_bar is not None
        new_bursts = [
            (pname, cnt)
            for pname, cnt in snap.token_bursts
            if pname in running and pname not in self._alerted_token_bursts
        ]
        if new_bursts and self.page is not None and not self._has_open_dialog():
            for pname, _ in new_bursts:
                self._alerted_token_bursts.add(pname)
            open_token_burst_alert_dialog(
                self.page,
                new_bursts,
                on_focus_window=self.h.on_focus_window,
            )
        self._arrange_btn.visible = len(running) >= 1
        if hasattr(self.pm, "list_trashed_profiles"):
            self._trash_btn.tooltip = get_string(
                "trash_button",
                count=len(self.pm.list_trashed_profiles()),
            )

        all_profiles, page_profiles, total_pages = self._get_page_profiles()
        in_archive = self._is_archive_view()
        active_everything = [p for p in everything if not is_archived_profile(p)]
        archived_everything = [p for p in everything if is_archived_profile(p)]

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
        effective_active_page = "__archived__" if in_archive else self.active_page
        self._pages_bar_slot.content = build_pages_bar(
            effective_active_page,
            len(active_everything),
            page_counts,
            self._on_select_custom_page,
            self._on_add_custom_page,
            self._on_edit_custom_page,
            self._on_reorder_custom_page,
            all_tab_index=getattr(self.pm, "all_page_index", 0),
        )

        tests = self._filter_tests()
        sidebar_pool = archived_everything if in_archive else active_everything
        group_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        for p in sidebar_pool:
            if p.group:
                group_counts[p.group] = group_counts.get(p.group, 0) + 1
            if p.status:
                parsed_st = parse_schedule_tag(p.status)
                st_key = parsed_st[0] if parsed_st is not None else p.status.strip()
                status_counts[st_key] = status_counts.get(st_key, 0) + 1
        group_counts = dict(sorted(group_counts.items(), key=lambda kv: kv[0].casefold()))
        status_counts = dict(
            sorted(status_counts.items(), key=lambda kv: (schedule_sort_key(kv[0]), kv[0].casefold()))
        )
        filter_counts = {
            fid: (
                len(archived_everything)
                if fid == "archived"
                else (
                    len(active_everything)
                    if fid == "all"
                    else sum(1 for p in sidebar_pool if test(p))
                )
            )
            for fid, test in tests.items()
        }
        self._filter_column.controls = build_filter_buttons(
            "archived" if in_archive and self.filter == "all" else self.filter,
            filter_counts,
            self._on_filter,
            group_counts=group_counts,
            status_counts=status_counts,
        )
        names = [p.name for p in page_profiles]
        all_sel = bool(names) and all(self.state.is_selected(n) for n in names)
        any_sel = any(self.state.is_selected(n) for n in names)
        self._header_slot.content = (
            build_header_row(
                all_sel,
                any_sel,
                lambda select: self.h.on_select_all_page() if select else self.h.on_deselect_page(),
                on_open_proxy_pool=self.h.on_open_proxy_pool,
                on_open_mail_pool=self.h.on_open_mail_pool,
                cooldown_chip_label=self._cooldown_chip_text(),
                on_open_cooldown=self.h.on_open_cooldown_settings,
                on_open_warmup_sites=self.h.on_open_warmup_sites,
                on_open_trust_monitor=self.h.on_open_trust_monitor,
                on_open_work_squad=self.h.on_open_work_squad,
                audit_alert=any(
                    f.code not in ("burned_proxy", "burned_ip", "burned_subnet", "burned_batch")
                    for f in snap.audit_findings
                ),
                mail_alert=bool(snap.mail_checkpoint_hits),
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
                    editing_email=self.state.editing_email == p.name,
                    on_start_edit_email=self.h.on_start_edit_email,
                    on_save_email=self.h.on_save_email,
                    editing_proxy=self.state.editing_proxy == p.name,
                    on_start_edit_proxy=self.h.on_start_edit_proxy,
                    on_save_proxy=self.h.on_save_proxy,
                    on_check_proxy=self.h.on_quick_check_proxy if p.proxy else None,
                    on_clone=self.h.on_clone,
                    on_focus_window=self.h.on_focus_window,
                    on_status_click=self.h.on_set_status,
                    on_session_history=self.h.on_open_session_history,
                    on_ip_history=self.h.on_open_ip_history,
                    on_trust_detail=self.h.on_open_trust_detail,
                    on_toggle_pin=self.h.on_toggle_pin,
                    on_edit_dates=self.h.on_edit_dates,
                    on_copy_email=self.h.on_copy_email,
                    on_copy_2fa=self.h.on_copy_2fa,
                    on_copy_password=self.h.on_copy_password,
                    on_copy_recovery=self.h.on_copy_recovery,
                    on_copy_token=self.h.on_copy_token,
                    on_fetch_mail_otp=self.h.on_fetch_mail_otp,
                    on_open_mail_credentials=self.h.on_open_mail_credentials,
                    on_avatar_double_click=self.h.on_open_avatar_dialog,
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
                "toggle_pin": self.h.on_bulk_toggle_pin,
                "set_status": self.h.on_bulk_set_status,
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

    def _device_label(self, name: str) -> str:
        cached = self.monitor._disk_cache.get(name) if hasattr(self, "monitor") else None
        if cached is not None:
            if cached.get("_fp_mtime", 0.0) > 0:
                lbl = cached.get("device_label")
                if isinstance(lbl, str) and lbl:
                    return lbl
            else:
                prof = self.pm.profiles.get(name) if hasattr(self, "pm") else None
                if prof is not None and getattr(prof, "browser_engine", "camoufox") != "chrome":
                    return ""
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
            if isinstance(self.refs.search_field.suffix, ft.Control):
                self.refs.search_field.suffix.visible = False
        self._refresh_profiles(reuse_snapshot=True)

    def _change_page(self, delta: int) -> None:
        self.state.current_page += delta
        self._refresh_profiles(reuse_snapshot=True)

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

    def _maybe_start_daily_mail_scan(self) -> None:
        if (
            self._mail_auto_scan_running
            or not hasattr(self.monitor, "should_auto_scan_mail")
            or not self.monitor.should_auto_scan_mail()
        ):
            return
        pool_map: dict[str, str] = {}
        if hasattr(self.pm, "list_mail_pool"):
            for ln in self.pm.list_mail_pool():
                p_item = parse_mail_credentials_line(ln)
                em_k = (p_item.get("email") or "").strip().lower()
                tk_v = (p_item.get("mail_token") or "").strip()
                if em_k and tk_v:
                    pool_map[em_k] = tk_v
        targets = self.monitor.get_live_mail_scan_targets(
            self.pm.list_profiles(),
            pool_map,
        )
        if not targets:
            return
        self._mail_auto_scan_running = True
        self._log(get_string("mail_auto_scan_started_log", count=len(targets)))

        def _bg_worker() -> None:
            try:
                res = run_safe_mail_checkpoint_scan(
                    targets,
                    delay_range=(3.0, 5.0),
                    top=4,
                    timeout=12,
                    max_consecutive_errors=3,
                )
                self.monitor.record_mail_scan(
                    res["hits"],
                    dead_tokens=res["dead_tokens"],
                    scanned_names=res["scanned_names"],
                    update_timestamp=not res["aborted"],
                )
                if res["hits"]:
                    hit_names = list(res["hits"].keys())
                    self._log(
                        get_string(
                            "mail_auto_scan_hits_log",
                            count=len(hit_names),
                            names=", ".join(hit_names[:8]),
                        )
                    )
                elif not res["aborted"]:
                    self._log(
                        get_string(
                            "mail_auto_scan_clean_log",
                            count=len(res["scanned_names"]),
                        )
                    )
            except Exception as exc:
                logger.error("Error in daily mail auto-scan: %s", exc)
            finally:
                self._mail_auto_scan_running = False
                self.state.schedule_refresh()

        threading.Thread(target=_bg_worker, daemon=True).start()

    async def _ui_reconcile_loop(self) -> None:
        last_minute_tick = time.monotonic()
        while self.page:
            try:
                running_now = self.bl.running_profile_names()
                changed = running_now != self.state._last_running_snapshot
                if changed:
                    self.state._last_running_snapshot = running_now
                now_mono = time.monotonic()
                minute_elapsed = (
                    (now_mono - last_minute_tick) >= 30.0
                    and bool(running_now or self.pm.profiles)
                    and not self._has_open_dialog()
                    and self.state.editing_note is None
                    and self.state.editing_email is None
                    and self.state.editing_proxy is None
                )
                if changed or self.state.consume_refresh() or minute_elapsed:
                    if minute_elapsed:
                        self._maybe_start_daily_mail_scan()
                    last_minute_tick = now_mono
                    self._refresh_profiles()
            except Exception as e:
                logger.error("Error in UI reconcile loop: %s", e)
            await asyncio.sleep(0.12)
