"""Event handlers extracted from App for Single Responsibility compliance."""

import asyncio
from collections.abc import Callable

import flet as ft

from ..core.strings import get_string
from ..interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ..services.monitor import MonitorService
from ..services.proxy.geo_check import (
    derive_ip_metadata,
    extract_ip_from_check_detail,
    lookup_ip_isp,
    lookup_ip_risk,
)
from ..services.scheduler import SchedulePlan
from ..utils.totp import (
    format_oauth_reader_line,
    generate_totp,
    parse_mail_credentials_line,
)
from ..utils.validation import validate_email, validate_notes, validate_proxy_format
from .actions.browser import launch_or_stop
from .actions.bulk import (
    bulk_check_proxies,
    bulk_delete_profiles,
    bulk_launch_profiles,
    bulk_stop_profiles,
    format_proxy_badge,
)
from .actions.cookies import _set_clipboard_text, manage_cookies
from .actions.profile import add_profile, delete_profile, edit_profile
from .actions.transfer import export_profile, import_profile
from .dialogs import (
    open_backup_restore_dialog,
    open_batch_create_dialog,
    open_bulk_color_dialog,
    open_bulk_group_dialog,
    open_bulk_prelaunch_guard_dialog,
    open_bulk_proxy_dialog,
    open_clone_dialog,
    open_cooldown_settings_dialog,
    open_log_dialog,
    open_mail_credentials_dialog,
    open_prelaunch_guard_dialog,
    open_profile_dates_dialog,
    open_proxy_pool_dialog,
    open_safe_schedule_dialog,
    open_session_history_dialog,
    open_status_dialog,
    open_trash_dialog,
    open_trust_monitor_dialog,
    open_warmup_sites_dialog,
    open_work_squad_dialog,
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
        monitor: MonitorService | None = None,
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
        self._monitor = monitor

    def on_launch(self, name: str) -> None:
        if self._bl.is_running(name):
            launch_or_stop(name, self._pm, self._bl, self._state, self._log)
            return
        profile = self._pm.profiles.get(name)
        if not profile:
            return
        page = self._get_page()
        if self._monitor is not None and page is not None:
            warnings = self._monitor.check_prelaunch(
                profile,
                self._pm.list_profiles(),
                self._bl.running_profile_names(),
                set(self._state.loading),
            )
            if warnings:

                def _force() -> None:
                    if self._monitor is not None:
                        self._monitor.acknowledge_proxy_change(name, profile.proxy)
                    launch_or_stop(name, self._pm, self._bl, self._state, self._log)

                open_prelaunch_guard_dialog(page, name, warnings, _force)
                return
        launch_or_stop(name, self._pm, self._bl, self._state, self._log)

    def on_open_cooldown_settings(self) -> None:
        page = self._get_page()
        if page is None or self._monitor is None:
            return

        def _save(mins: int) -> None:
            if self._monitor is not None:
                self._monitor.set_cooldown_minutes(mins)
                self._log(get_string("cooldown_saved_log", mins=mins))
                self._refresh()

        open_cooldown_settings_dialog(page, self._monitor.cooldown_minutes, _save)

    def on_focus_window(self, name: str) -> None:
        if hasattr(self._bl, "focus_profile"):
            self._bl.focus_profile(name)

    def on_arrange_windows(self) -> None:
        if hasattr(self._bl, "arrange_windows"):
            ordered = [p.name for p in self._pm.list_profiles() if self._bl.is_running(p.name)]
            cnt = self._bl.arrange_windows(ordered)
            if cnt > 0:
                self._log(get_string("arranged_windows_log", count=cnt))

    def on_delete(self, name: str) -> None:
        page = self._get_page()
        assert page is not None
        delete_profile(page, name, self._pm, self._log, self._refresh)

    def on_edit(self, name: str) -> None:
        page = self._get_page()
        assert page is not None

        def _after_edit() -> None:
            self._state.proxy_status.pop(name, None)
            self._refresh()

        edit_profile(
            page,
            name,
            self._pm,
            self._bl,
            self._ps,
            self._log,
            _after_edit,
            monitor=self._monitor,
        )

    def on_cookies(self, name: str) -> None:
        page = self._get_page()
        refs = self._get_refs()
        assert page is not None and refs is not None
        manage_cookies(
            page,
            name,
            refs.file_picker,
            self._pm,
            self._bl,
            self._log,
            on_session_history=lambda: self.on_open_session_history(name),
        )

    def on_open_session_history(self, name: str, initial_tab: str = "sessions") -> None:
        page = self._get_page()
        if page is None or self._monitor is None:
            return
        p = self._pm.profiles.get(name)
        sessions = self._monitor.get_profile_sessions(
            name,
            is_running=self._bl.is_running(name),
            current_proxy=p.proxy if p is not None else None,
        )
        ip_hist = self._monitor.get_ip_history(name)
        snap = self._monitor.snapshot(
            self._pm.list_profiles(),
            self._bl.running_profile_names() | set(self._state.loading),
        )
        info = snap.profiles.get(name)
        today_mins = info.today_minutes if info is not None else 0
        last_port = (info.last_used_port or info.port) if info is not None else ""

        def _clear_ips() -> None:
            if self._monitor is not None:
                self._monitor.clear_ip_history(name)
                self._refresh()

        def _score_ip(ip_addr: str, risk_data: dict) -> None:
            if self._monitor is not None:
                self._monitor.record_ip_risk(ip_addr, risk_data)
                self._state.schedule_refresh()

        open_session_history_dialog(
            page,
            name,
            sessions,
            today_mins=today_mins,
            last_port=last_port,
            ip_history=ip_hist,
            initial_tab=initial_tab,
            on_check_ip_now=(
                (lambda: self.on_quick_check_proxy(name)) if p is not None and p.proxy else None
            ),
            on_clear_ip_history=_clear_ips,
            monitor_info=info,
            on_score_ip=_score_ip,
            on_open_trust_monitor=lambda: self.on_open_trust_monitor(),
        )

    def on_open_ip_history(self, name: str) -> None:
        self.on_open_session_history(name, initial_tab="ips")

    def on_open_trust_monitor(self, initial_tab: str = "scores") -> None:
        page = self._get_page()
        if page is None or self._monitor is None:
            return

        def _get_snap():
            assert self._monitor is not None
            return self._monitor.snapshot(
                self._pm.list_profiles(),
                self._bl.running_profile_names() | set(self._state.loading),
            )

        def _record_risk(ip_addr: str, risk_data: dict) -> None:
            if self._monitor is not None:
                self._monitor.record_ip_risk(ip_addr, risk_data)

        open_trust_monitor_dialog(
            page,
            self._pm.list_profiles,
            _get_snap,
            _record_risk,
            check_proxy_fn=self._ps.check_proxy_sync,
            on_record_exit_ip=self._monitor.record_exit_ip,
            on_log=self._log,
            on_refresh_main=self._state.schedule_refresh,
            initial_tab=initial_tab,
        )

    def on_toggle_pin(self, name: str) -> None:
        if not hasattr(self._pm, "toggle_pin_profile"):
            return
        pinned = self._pm.toggle_pin_profile(name)
        if pinned is not None:
            msg_key = "pinned_profile_log" if pinned else "unpinned_profile_log"
            self._log(get_string(msg_key, name=name))
            self._refresh()

    def _status_tag_callbacks(self) -> dict:
        def _save_tag(code_or_new: str, label: str, color: str) -> bool:
            ok = self._pm.save_status_tag(code_or_new, label, color)
            if ok:
                self._refresh()
            return ok

        def _del_tag(code: str) -> bool:
            ok = self._pm.delete_status_tag(code)
            if ok:
                self._refresh()
            return ok

        def _reset_tags() -> list[dict[str, str]]:
            res = self._pm.reset_status_tags()
            self._refresh()
            return res

        return {
            "on_save_tag": _save_tag,
            "on_delete_tag": _del_tag,
            "on_reset_tags": _reset_tags,
        }

    def on_set_status(self, name: str) -> None:
        page = self._get_page()
        p = self._pm.profiles.get(name)
        if page is None or p is None:
            return

        def _apply(st: str | None) -> None:
            cnt = self._pm.bulk_update_status([name], st)
            self._log(get_string("bulk_status_updated", count=cnt))
            self._refresh()

        open_status_dialog(
            page,
            get_string("status_single_title", name=name),
            p.status,
            _apply,
            **self._status_tag_callbacks(),
        )

    def on_open_proxy_pool(self) -> None:
        page = self._get_page()
        if page is None:
            return

        def _get_running() -> set[str]:
            return self._bl.running_profile_names() | set(self._state.loading)

        def _replace(old_target: str, new_proxy: str) -> tuple[bool, str]:
            updated = self._pm.replace_proxy_across_profiles(
                old_target,
                new_proxy,
                skip_names=_get_running(),
            )
            if not updated:
                return False, get_string("validation_invalid_proxy")
            for pname in updated:
                self._state.proxy_status.pop(pname, None)
            msg = get_string(
                "proxy_pool_replaced_log",
                count=len(updated),
                names=", ".join(updated),
            )
            self._log(msg)
            self._refresh()
            return True, msg

        def _add_to_pool(lines: list[str]) -> tuple[int, int]:
            added, skipped = self._pm.add_proxies_to_pool(lines)
            if added:
                self._log(get_string("proxy_pool_added_log", added=added, skipped=skipped))
                self._refresh()
            return added, skipped

        def _remove_from_pool(px: str) -> bool:
            ok = self._pm.remove_proxy_from_pool(px)
            if ok:
                self._refresh()
            return ok

        def _assign_to_profile(prof_name: str, px: str) -> bool:
            if self._bl.is_running(prof_name) or self._state.is_loading(prof_name):
                return False
            cnt = self._pm.bulk_update_proxies({prof_name: px})
            if cnt > 0:
                self._state.proxy_status.pop(prof_name, None)
                self._log(get_string("quick_saved_proxy", name=prof_name))
                self._refresh()
                return True
            return False

        open_proxy_pool_dialog(
            page,
            self._pm.list_profiles,
            _get_running,
            lambda: dict(self._state.proxy_status),
            self.on_bulk_check_proxy,
            self.on_quick_check_proxy,
            _replace,
            get_unassigned=self._pm.list_unassigned_proxies,
            on_add_to_pool=_add_to_pool,
            on_remove_from_pool=_remove_from_pool,
            on_assign_to_profile=_assign_to_profile,
        )

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
            on_open_backups=self.on_open_backups,
        )

    def on_open_batch_create(self, active_page: str = "__all__") -> None:
        page = self._get_page()
        assert page is not None
        target_pages = [active_page] if active_page != "__all__" else None

        def _do_batch(
            entries: list[tuple[str, str | None]],
            os_type: str,
            group: str,
            tz: str,
            loc: str,
            auto_geo: bool,
        ) -> None:
            created, skipped = self._pm.batch_create_profiles(
                entries,
                os_type=os_type,
                timezone=tz or None,
                locale=loc or None,
                group=group or None,
                pages=target_pages,
            )
            self._log(
                get_string(
                    "batch_created_log",
                    count=len(created),
                    skipped=len(skipped),
                )
            )
            self._refresh()
            if auto_geo and created and page is not None:
                with_proxy = [
                    n for n in created if n in self._pm.profiles and self._pm.profiles[n].proxy
                ]
                if with_proxy:

                    async def _check_created() -> None:
                        await bulk_check_proxies(
                            with_proxy,
                            self._pm,
                            self._ps,
                            self._log,
                            state=self._state,
                            bl=self._bl,
                            auto_pin_geo=True,
                            monitor=self._monitor,
                        )

                    page.run_task(_check_created)

        open_batch_create_dialog(
            page,
            set(self._pm.profiles.keys()),
            _do_batch,
            on_switch_single=lambda: self.open_add_dialog(active_page),
        )

    def on_open_backups(self) -> None:
        page = self._get_page()
        assert page is not None

        def _list_meta() -> list[dict]:
            if not hasattr(self._pm, "list_config_backups"):
                return []
            paths = list(reversed(self._pm.list_config_backups()))
            out: list[dict] = []
            for bp in paths:
                meta = self._pm.inspect_config_backup(bp)
                if meta is not None:
                    out.append(meta)
            return out

        def _is_any_running() -> bool:
            return bool(self._bl.running_profile_names() or self._state.loading)

        def _do_restore(bpath) -> None:
            ok, cnt = self._pm.restore_config_backup(bpath, keep_newer_profiles=True)
            if ok:
                self._log(
                    get_string(
                        "backup_restored_log",
                        file=getattr(bpath, "name", str(bpath)),
                        count=cnt,
                    )
                )
                self._refresh()

        open_backup_restore_dialog(
            page,
            _list_meta,
            _is_any_running,
            _do_restore,
        )

    # --- Inline quick-edit & proxy check handlers ---

    def on_start_edit_note(self, name: str) -> None:
        self._state.editing_proxy = None
        self._state.editing_email = None
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

    def on_start_edit_email(self, name: str) -> None:
        self._state.editing_proxy = None
        self._state.editing_note = None
        self._state.editing_email = name
        self._refresh()

    def on_save_email(self, name: str, raw_email: str) -> None:
        if self._state.editing_email != name:
            return
        self._state.editing_email = None
        p = self._pm.profiles.get(name)
        if p is None:
            self._refresh()
            return
        clean = raw_email.strip()
        if "|" in clean or "\t" in clean:
            parsed = parse_mail_credentials_line(clean)
            em = parsed["email"]
            pw = parsed["password"]
            tfa = parsed["two_fa"]
            rec = parsed["recovery_email"]
            tok = parsed.get("mail_token", "")
            if em and validate_email(em)[0] and hasattr(self._pm, "update_profile_credentials"):
                self._pm.update_profile_credentials(
                    name,
                    email=em,
                    password=pw or getattr(p, "password", None),
                    two_fa=tfa or getattr(p, "two_fa", None),
                    recovery_email=rec or getattr(p, "recovery_email", None),
                    mail_token=tok or getattr(p, "mail_token", None),
                )
                self._log(get_string("mail_saved_log", name=name))
                self._refresh()
                return
        if clean and not validate_email(clean)[0]:
            self._refresh()
            return
        if (clean or None) != getattr(p, "email", None):
            if hasattr(self._pm, "update_profile_email"):
                self._pm.update_profile_email(name, clean or None)
            else:
                p.email = clean or None
            self._log(get_string("quick_saved_email", name=name))
        self._refresh()

    def on_copy_email(self, name: str) -> None:
        p = self._pm.profiles.get(name)
        if p is None or not getattr(p, "email", None):
            return
        em = str(p.email).strip()
        page = self._get_page()
        if page is not None and hasattr(page, "run_task"):
            page.run_task(_set_clipboard_text, page, em)
        self._log(get_string("copied_email_log", name=name, email=em))

    def on_copy_2fa(self, name: str) -> None:
        p = self._pm.profiles.get(name)
        if p is None:
            return
        pair = generate_totp(getattr(p, "two_fa", None))
        if pair is None:
            return
        code, rem = pair
        page = self._get_page()
        if page is not None and hasattr(page, "run_task"):
            page.run_task(_set_clipboard_text, page, code)
        self._log(get_string("copied_2fa_log", name=name, code=code, rem=rem))
        self._refresh()

    def on_copy_password(self, name: str) -> None:
        p = self._pm.profiles.get(name)
        if p is None or not getattr(p, "password", None):
            return
        page = self._get_page()
        if page is not None and hasattr(page, "run_task"):
            page.run_task(_set_clipboard_text, page, str(p.password))
        self._log(get_string("copied_password_log", name=name))

    def on_copy_recovery(self, name: str) -> None:
        p = self._pm.profiles.get(name)
        if p is None or not getattr(p, "recovery_email", None):
            return
        rec = str(p.recovery_email).strip()
        page = self._get_page()
        if page is not None and hasattr(page, "run_task"):
            page.run_task(_set_clipboard_text, page, rec)
        self._log(get_string("copied_recovery_log", name=name, email=rec))

    def on_copy_token(self, name: str) -> None:
        p = self._pm.profiles.get(name)
        if p is None or not getattr(p, "mail_token", None):
            return
        oauth_str = format_oauth_reader_line(
            getattr(p, "email", None),
            getattr(p, "password", None),
            getattr(p, "mail_token", None),
        )
        page = self._get_page()
        if page is not None and hasattr(page, "run_task"):
            page.run_task(_set_clipboard_text, page, oauth_str)
        self._log(get_string("copied_token_log", name=name))

    def on_fetch_mail_otp(self, name: str) -> None:
        self.on_open_mail_credentials(name, initial_tab="inbox")

    def on_open_mail_credentials(self, name: str, initial_tab: str = "single") -> None:
        page = self._get_page()
        p = self._pm.profiles.get(name)
        if page is None or p is None:
            return

        def _save_single(
            email: str | None,
            password: str | None,
            two_fa: str | None,
            recovery_email: str | None,
            notes: str | None,
            mail_token: str | None = None,
        ) -> bool:
            ok = False
            if hasattr(self._pm, "update_profile_credentials"):
                ok = self._pm.update_profile_credentials(
                    name,
                    email=email,
                    password=password,
                    two_fa=two_fa,
                    recovery_email=recovery_email,
                    notes=notes,
                    mail_token=mail_token,
                )
            if ok:
                self._log(get_string("mail_saved_log", name=name))
                self._refresh()
            return ok

        def _bulk_import(raw_lines: str) -> tuple[int, int]:
            if not hasattr(self._pm, "bulk_import_credentials"):
                return (0, 0)
            updated, skipped = self._pm.bulk_import_credentials(raw_lines)
            if updated > 0:
                self._log(
                    get_string("mail_bulk_saved_log", updated=updated, skipped=skipped)
                )
                self._refresh()
            return (updated, skipped)

        def _copy_text(val: str, log_msg: str) -> None:
            if page is not None and hasattr(page, "run_task"):
                page.run_task(_set_clipboard_text, page, val)
            self._log(log_msg)

        open_mail_credentials_dialog(
            page,
            p,
            _save_single,
            on_bulk_import=_bulk_import,
            on_copy_text=_copy_text,
            initial_tab=initial_tab,
        )

    def on_open_warmup_sites(self) -> None:
        page = self._get_page()
        if page is None:
            return

        def _copy_text(val: str, log_msg: str) -> None:
            if page is not None and hasattr(page, "run_task"):
                page.run_task(_set_clipboard_text, page, val)
            self._log(log_msg)

        open_warmup_sites_dialog(page, on_copy_text=_copy_text)

    def on_start_edit_proxy(self, name: str) -> None:
        self._state.editing_note = None
        self._state.editing_email = None
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
            self._state.proxy_status.pop(name, None)
            self._log(get_string("quick_saved_proxy", name=name))
        self._refresh()

    def on_quick_check_proxy(self, name: str) -> None:
        page = self._get_page()
        p = self._pm.profiles.get(name)
        if page is None or p is None or not p.proxy:
            return
        proxy_str = p.proxy
        siblings = [prof.name for prof in self._pm.profiles.values() if prof.proxy == proxy_str]
        for sib in siblings:
            self._state.proxy_status[sib] = ("checking", f"⏳ {get_string('proxy_checking')}")
        self._refresh()
        self._log(f"[{name}] {get_string('proxy_checking')}")

        async def _check() -> None:
            ok, detail = await asyncio.to_thread(self._ps.check_proxy_sync, proxy_str, 10)
            raw_ip: str | None = None
            country = ""
            auto_tz, auto_loc = (None, None)
            isp_name, ip_type = ("", "")
            if ok:
                raw_ip = extract_ip_from_check_detail(detail)
                if raw_ip:
                    country, auto_tz, auto_loc = await asyncio.to_thread(
                        derive_ip_metadata,
                        raw_ip,
                        detail,
                    )
                    isp_name, ip_type = await asyncio.to_thread(lookup_ip_isp, raw_ip)
                    risk_data = await asyncio.to_thread(lookup_ip_risk, raw_ip)
                    if not isp_name and risk_data.get("isp"):
                        isp_name = str(risk_data["isp"])
                    if not ip_type and risk_data.get("ip_type"):
                        ip_type = str(risk_data["ip_type"])
                    if self._monitor is not None and int(risk_data.get("fraud_score", -1)) >= 0:
                        self._monitor.record_ip_risk(raw_ip, risk_data)
            badge = format_proxy_badge(ok, detail, ip_type=ip_type, country=country)
            for sib in siblings:
                self._state.proxy_status[sib] = badge
                if ok and raw_ip and self._monitor is not None:
                    self._monitor.record_exit_ip(
                        sib,
                        raw_ip,
                        country=country,
                        tz=auto_tz or "",
                        proxy=proxy_str,
                        isp=isp_name,
                        ip_type=ip_type,
                    )
                if (
                    ok
                    and (auto_tz or auto_loc)
                    and hasattr(self._pm, "pin_missing_geo")
                    and not self._bl.is_running(sib)
                    and not self._state.is_loading(sib)
                    and self._pm.pin_missing_geo(sib, auto_tz, auto_loc)
                ):
                    p_cur = self._pm.profiles.get(sib)
                    self._log(
                        get_string(
                            "auto_pinned_geo_log",
                            name=sib,
                            tz=(p_cur.timezone if p_cur else auto_tz) or "-",
                            loc=(p_cur.locale if p_cur else auto_loc) or "-",
                        )
                    )
            self._log(f"[{name}] {detail}")
            self._state.schedule_refresh()

        page.run_task(_check)

    def open_add_dialog(self, active_page: str = "__all__") -> None:
        page = self._get_page()
        assert page is not None
        add_profile(
            page,
            self._pm,
            self._ps,
            self._log,
            self._refresh,
            on_batch_create=lambda: self.on_open_batch_create(active_page),
            active_page=active_page,
        )

    def on_move(self, name: str, direction: int) -> None:
        if self._pm.move_profile(name, direction):
            self._refresh()

    async def on_import(self, _: ft.ControlEvent | None = None) -> None:
        refs = self._get_refs()
        assert refs is not None
        await import_profile(
            refs.file_picker,
            self._pm,
            self._log,
            self._refresh,
            running_names=self._bl.running_profile_names() | set(self._state.loading),
        )

    def on_export_open(self) -> None:
        page = self._get_page()
        refs = self._get_refs()
        assert page is not None and refs is not None
        export_profile(
            page,
            refs.file_picker,
            self._pm,
            self._log,
            preselected_names=self._state.selected_names(),
        )

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
        names = [
            n
            for n in self._state.selected_names()
            if not self._bl.is_running(n) and not self._state.is_loading(n)
        ]
        if not names:
            return
        page = self._get_page()
        if self._monitor is not None and page is not None:
            issues = self._monitor.check_bulk_prelaunch(
                names,
                self._pm.list_profiles(),
                self._bl.running_profile_names(),
                set(self._state.loading),
            )
            if issues:
                safe_names = [n for n in names if n not in issues]

                def _launch_safe() -> None:
                    if safe_names:
                        bulk_launch_profiles(safe_names, self._pm, self._bl, self._state, self._log)

                def _force_all() -> None:
                    if self._monitor is not None:
                        for n in names:
                            p = self._pm.profiles.get(n)
                            if p is not None:
                                self._monitor.acknowledge_proxy_change(n, p.proxy)
                    bulk_launch_profiles(names, self._pm, self._bl, self._state, self._log)

                open_bulk_prelaunch_guard_dialog(
                    page,
                    safe_names,
                    issues,
                    _launch_safe,
                    _force_all,
                )
                return
        bulk_launch_profiles(names, self._pm, self._bl, self._state, self._log)

    def on_bulk_stop(self) -> None:
        if names := list(self._state.selected_names()):
            bulk_stop_profiles(names, self._pm, self._bl, self._state, self._log)

    def on_bulk_toggle_pin(self) -> None:
        if not hasattr(self._pm, "bulk_update_pinned"):
            return
        selected = [n for n in self._state.selected_names() if n in self._pm.profiles]
        if not selected:
            return
        all_currently_pinned = all(
            bool(getattr(self._pm.profiles[n], "pinned", False)) for n in selected
        )
        target_pinned = not all_currently_pinned
        cnt = self._pm.bulk_update_pinned(selected, target_pinned)
        msg_key = "bulk_pinned_log" if target_pinned else "bulk_unpinned_log"
        self._log(get_string(msg_key, count=cnt or len(selected)))
        self._state.clear_selection()
        self._refresh()

    def on_bulk_set_status(self) -> None:
        page = self._get_page()
        assert page is not None
        selected = list(self._state.selected_names())
        if not selected:
            return
        cur_statuses = {self._pm.profiles[n].status for n in selected if n in self._pm.profiles}
        cur_status = next(iter(cur_statuses)) if len(cur_statuses) == 1 else None

        def _apply(st: str | None) -> None:
            cnt = self._pm.bulk_update_status(selected, st)
            self._log(get_string("bulk_status_updated", count=cnt))
            self._state.clear_selection()
            self._refresh()

        open_status_dialog(
            page,
            get_string("bulk_status_title", count=len(selected)),
            cur_status,
            _apply,
            **self._status_tag_callbacks(),
        )

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

    def on_bulk_set_proxy(self) -> None:
        page = self._get_page()
        assert page is not None
        selected_set = self._state.selected_names()
        if not selected_set:
            return
        _, page_profiles, _ = self._get_page_profiles()
        ordered: list[str] = [p.name for p in page_profiles if p.name in selected_set]
        seen = set(ordered)
        for p in self._pm.list_profiles():
            if p.name in selected_set and p.name not in seen:
                ordered.append(p.name)
                seen.add(p.name)
        current_proxies = {n: self._pm.profiles[n].proxy for n in ordered if n in self._pm.profiles}
        session_ports: dict[str, str] = {}
        if self._monitor is not None:
            snap = self._monitor.snapshot(
                self._pm.list_profiles(),
                self._bl.running_profile_names(),
            )
            for n in ordered:
                info = snap.profiles.get(n)
                if info and (info.hatch_gw or info.hatch_sess) and info.last_used_port:
                    session_ports[n] = info.last_used_port

        def _apply(assignments: dict[str, str | None], auto_geo: bool = False) -> None:
            cnt = self._pm.bulk_update_proxies(assignments)
            for pname in assignments:
                self._state.proxy_status.pop(pname, None)
            self._log(get_string("bulk_proxy_updated", count=cnt))
            self._state.clear_selection()
            self._refresh()
            if auto_geo and cnt > 0 and page is not None:
                need_geo = [
                    pname
                    for pname, pval in assignments.items()
                    if pval
                    and pname in self._pm.profiles
                    and (
                        not self._pm.profiles[pname].timezone or not self._pm.profiles[pname].locale
                    )
                ]
                if need_geo:

                    async def _auto_check() -> None:
                        await bulk_check_proxies(
                            need_geo,
                            self._pm,
                            self._ps,
                            self._log,
                            state=self._state,
                            bl=self._bl,
                            auto_pin_geo=True,
                            monitor=self._monitor,
                        )

                    page.run_task(_auto_check)

        open_bulk_proxy_dialog(
            page,
            ordered,
            current_proxies,
            _apply,
            session_ports=session_ports,
        )

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
                bl=self._bl,
                auto_pin_geo=True,
                monitor=self._monitor,
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

    def on_edit_dates(self, name: str) -> None:
        page = self._get_page()
        assert page is not None
        p = self._pm.profiles.get(name)
        if p is None:
            return

        def _apply(created_at: str | None, banned_at: str | None) -> None:
            if self._pm.update_profile_dates(name, created_at, banned_at):
                self._log(get_string("dates_updated_log", name=name))
                self._refresh()

        open_profile_dates_dialog(
            page,
            profile_name=name,
            created_at=getattr(p, "created_at", None),
            banned_at=getattr(p, "banned_at", None),
            on_apply=_apply,
        )

    def on_open_safe_schedule(self) -> None:
        page = self._get_page()
        assert page is not None
        snap = self._monitor.snapshot(
            list(self._pm.profiles.values()),
            self._bl.running_profile_names() | self._state.loading_names(),
        )

        def _apply_plan(plan: SchedulePlan) -> None:
            tag_map = plan.status_tag_map()
            cnt = self._pm.apply_schedule_tags(tag_map)
            self._monitor.record_schedule_applied(
                plan.pairing_records(),
                plan.shift_records(),
                plan.cycle_days,
            )
            self._state.sort_by = "schedule"
            self._state.current_page = 1
            refs = self._get_refs()
            if refs is not None and refs.sort_dropdown is not None:
                refs.sort_dropdown.value = "schedule"
            self._log(
                get_string(
                    "schedule_applied_log",
                    count=cnt,
                    days=plan.cycle_days,
                )
            )
            self._refresh()

        def _copy_md(md_text: str) -> None:
            _set_clipboard_text(page, md_text)
            self._log(get_string("schedule_copied_log"))

        def _clear_tags() -> None:
            cnt = self._pm.clear_schedule_tags()
            if self._state.sort_by == "schedule":
                self._state.sort_by = "custom"
                refs = self._get_refs()
                if refs is not None and refs.sort_dropdown is not None:
                    refs.sort_dropdown.value = "custom"
            self._log(get_string("schedule_cleared_log", count=cnt))
            self._refresh()

        open_safe_schedule_dialog(
            page,
            list(self._pm.profiles.values()),
            snap,
            initial_cycle_days=self._monitor.schedule_cycle_days,
            last_pairings=self._monitor.last_pairings,
            last_shifts=self._monitor.last_shifts,
            on_apply_schedule=_apply_plan,
            on_copy_schedule=_copy_md,
            on_clear_schedule=_clear_tags,
        )

    def on_open_work_squad(self) -> None:
        page = self._get_page()
        assert page is not None
        running_names = self._bl.running_profile_names() | self._state.loading_names()
        snap = self._monitor.snapshot(
            list(self._pm.profiles.values()),
            running_names,
        )

        def _mode_label(mode: str) -> str:
            if mode == "fixed":
                return get_string("work_squad_mode_fixed")
            if mode == "prime":
                return get_string("work_squad_mode_prime")
            return get_string("work_squad_mode_rotation")

        def _apply_squad(selected_names: list[str], mode: str) -> None:
            self._state.clear_selection()
            for n in selected_names:
                self._state.toggle_selection(n)
            self._state.sort_by = "parallel_ready"
            self._state.current_page = 1
            refs = self._get_refs()
            if refs is not None and refs.sort_dropdown is not None:
                refs.sort_dropdown.value = "parallel_ready"
            self._log(
                get_string(
                    "work_squad_applied_log",
                    count=len(selected_names),
                    mode=_mode_label(mode),
                )
            )
            self._refresh()

        def _launch_one(name: str) -> None:
            if not self._bl.is_running(name) and not self._state.is_loading(name):
                launch_or_stop(name, self._pm, self._bl, self._state, self._log)
                self._refresh()

        def _save_fixed_squad(key: str, names: list[str] | None, team_idx: int) -> None:
            self._monitor.set_fixed_work_squad(key, names)
            if names:
                self._log(
                    get_string(
                        "work_squad_pinned_log",
                        idx=team_idx,
                        count=len(names),
                    )
                )
            else:
                self._log(get_string("work_squad_unpinned_log", idx=team_idx))

        def _stagger_launch_squad(selected_names: list[str], mode: str) -> None:
            self._state.clear_selection()
            for n in selected_names:
                self._state.toggle_selection(n)
            self._state.sort_by = "parallel_ready"
            self._state.current_page = 1
            refs = self._get_refs()
            if refs is not None and refs.sort_dropdown is not None:
                refs.sort_dropdown.value = "parallel_ready"
            to_open = [
                n
                for n in selected_names
                if not self._bl.is_running(n) and not self._state.is_loading(n)
            ]
            self._log(
                get_string(
                    "work_squad_stagger_started_log",
                    count=len(to_open),
                    mode=_mode_label(mode),
                )
            )
            self._refresh()
            if to_open:
                bulk_launch_profiles(
                    to_open,
                    self._pm,
                    self._bl,
                    self._state,
                    self._log,
                    stagger_seconds=(40, 60),
                    shuffle_order=(mode == "fixed"),
                )

        open_work_squad_dialog(
            page,
            list(self._pm.profiles.values()),
            snap,
            running_names=running_names,
            on_select_squad=_apply_squad,
            on_launch_single=_launch_one,
            on_launch_staggered=_stagger_launch_squad,
            pinned_squads=self._monitor.fixed_work_squads,
            on_save_fixed_squad=_save_fixed_squad,
        )
