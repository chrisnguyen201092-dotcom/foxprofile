"""Regression tests for FoxProfile UI click responsiveness, card reuse, and Win32 switching."""

from __future__ import annotations

import asyncio
import ctypes
import dataclasses
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import flet as ft
import msgpack
import pytest
from flet.messaging.protocol import (
    Message,
    MessageAction,
    RegisterClientResponseBody,
    configure_encode_object_for_msgpack,
)
from flet.messaging.session import Session
from flet.pubsub.pubsub_hub import PubSubHub

import src.services.profile.manager as pm_mod
import src.ui.app as app_mod
from src.core.container import Container
from src.core.events import EventBus
from src.core.strings import current_language, set_language
from src.models.profile import Profile
from src.services.browser import window_title as wt_mod
from src.services.browser.launcher import ProfileBusyError
from src.services.monitor import MonitorService
from src.services.profile.manager import ProfileManager
from src.ui.actions.bulk import bulk_delete_profiles
from src.ui.app import App
from src.ui.components.floating_tab_bar import FloatingProfileTabBar, _exceeds_drag_threshold
from src.ui.components.profile_card import ROW_HEIGHT
from src.ui.dialogs.trash import open_trash_dialog
from src.ui.handlers import AppHandlers
from src.ui.state import AppState
from src.ui.theme import styles as styles_mod


class _FakeBL:
    def __init__(self, *, start_succeeds: bool = True, raise_busy: bool = False) -> None:
        self._running: set[str] = set()
        self._pids: dict[str, int] = {}
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.switched: list[tuple[str, bool]] = []
        self.start_succeeds = start_succeeds
        self.raise_busy = raise_busy

    def is_running(self, name: str) -> bool:
        return name in self._running

    def running_count(self) -> int:
        return len(self._running)

    def running_profile_names(self) -> set[str]:
        return set(self._running)

    def pid_for_profile(self, name: str) -> int | None:
        return self._pids.get(name)

    def start_thread(
        self,
        profile,
        log_callback,
        on_start=None,
        on_ready=None,
        on_stop=None,
    ) -> bool:
        if self.raise_busy:
            raise ProfileBusyError(f"Profile {profile.name} busy")
        self.started.append(profile.name)
        if not self.start_succeeds:
            return False
        self._running.add(profile.name)
        return True

    def stop_profile(self, name: str) -> None:
        self.stopped.append(name)
        self._running.discard(name)

    def switch_profile(self, name: str, *, single_stage: bool = True) -> bool:
        self.switched.append((name, single_stage))
        return True

    def arrange_windows(self) -> int:
        return len(self._running)

    def stop_all(self) -> None:
        self._running.clear()


class _FakePS:
    def fetch_ip_info(self, proxy: str, timeout: int = 8) -> dict:
        return {}


class _DummyConn:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.pubsubhub = PubSubHub()
        self.page_url = "http://localhost"
        self.messages: list[Message] = []
        self._enc = configure_encode_object_for_msgpack(ft.BaseControl)

    def send_message(self, msg: Message) -> None:
        msgpack.packb([msg.action, msg.body], default=self._enc)
        self.messages.append(msg)

    def dispose(self) -> None:
        pass


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "camoufox_data"
    data_dir.mkdir()
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_mod, "DATA_DIR", str(data_dir))
    pm = ProfileManager()
    return pm, data_dir, tmp_path


def _create_app(pm: ProfileManager, bl: _FakeBL, tmp_path) -> App:
    c = Container()
    c._instances["pm"] = pm
    c._instances["bl"] = bl
    c._instances["ps"] = _FakePS()
    c._instances["eb"] = EventBus()
    c._instances["mon"] = MonitorService(
        data_dir=str(tmp_path / "camoufox_data"),
        log_dir=str(tmp_path / "logs"),
    )
    return App(container=c)


def _connect_session(loop: asyncio.AbstractEventLoop) -> tuple[_DummyConn, Session]:
    conn = _DummyConn(loop)
    sess = Session(conn)
    conn.send_message(
        Message(
            MessageAction.REGISTER_CLIENT,
            RegisterClientResponseBody(
                session_id=sess.id,
                page_patch=sess.get_page_patch(),
                error="",
            ),
        )
    )
    return conn, sess


def test_d1_live_session_240_profiles_card_reuse_and_click_responsiveness(isolated_env):
    pm, _, tmp_path = isolated_env
    for i in range(240):
        assert pm.add_profile(
            f"P_{i:03d}",
            f"http://u:p@1.2.3.4:{10100 + i}",
            "windows",
            timezone="America/Detroit",
            locale="en-US",
            pinned=(i < 3),
        )

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        conn, sess = _connect_session(loop)
        await sess.connect(conn)
        bl = _FakeBL()
        app = _create_app(pm, bl, tmp_path)
        try:
            app._main(sess.page)
            assert app.refs.profile_list_area.item_extent == ROW_HEIGHT
            initial_cards = {
                ctrl.key: ctrl for ctrl in app.refs.profile_list_area.controls
            }
            assert len(initial_cards) == 240
            assert all(getattr(ctrl, "on_hover", None) is None for ctrl in initial_cards.values())

            with patch.object(
                app.monitor, "snapshot", side_effect=AssertionError("unexpected snapshot call")
            ):
                # 1. Single checkbox click rebuilds only P_010 while reusing the other 239 cards
                app.h.on_toggle_select("P_010")
                after_select = {
                    ctrl.key: ctrl for ctrl in app.refs.profile_list_area.controls
                }
                assert after_select["P_010"] is not initial_cards["P_010"]
                for k, card in after_select.items():
                    if k != "P_010":
                        assert card is initial_cards[k]

                # 2. Inline acc_no + note quick edit only rebuilds P_005
                before_edit = dict(after_select)
                app.h.on_start_edit_acc_no("P_005")
                app.h.on_save_acc_no("P_005", "#42")
                app.h.on_start_edit_note("P_005")
                app.h.on_save_note("P_005", "FastNote")
                after_edit = {
                    ctrl.key: ctrl for ctrl in app.refs.profile_list_area.controls
                }
                assert pm.profiles["P_005"].acc_no == "42"
                assert pm.profiles["P_005"].notes == "FastNote"
                assert after_edit["P_005"] is not before_edit["P_005"]
                for k, card in after_edit.items():
                    if k != "P_005":
                        assert card is before_edit[k]

            # 3. Launch immediately marks P_025 loading and calls start_thread once
            before_launch = dict(after_edit)
            app.h.on_launch("P_025")
            assert app.state.is_loading("P_025")
            assert bl.started == ["P_025"]
            after_launch = {
                ctrl.key: ctrl for ctrl in app.refs.profile_list_area.controls
            }
            assert after_launch["P_025"] is not before_launch["P_025"]
            for k, card in after_launch.items():
                if k != "P_025":
                    assert card is before_launch[k]

            # 4. Filter shrink -> restore survives frozen reconciliation with zero crashes
            app._on_filter("pinned")
            assert [c.key for c in app.refs.profile_list_area.controls] == [
                "P_000",
                "P_001",
                "P_002",
            ]
            app._on_filter("all")
            assert len(app.refs.profile_list_area.controls) == 240
            crashes = [
                m.body.message for m in conn.messages if m.action == MessageAction.SESSION_CRASHED
            ]
            assert crashes == []
        finally:
            app._shutting_down = True

    asyncio.run(_run())


def test_d2_drag_threshold_empty_extent_and_language_invalidation(isolated_env):
    assert _exceeds_drag_threshold(0, 0) is False
    assert _exceeds_drag_threshold(3, 3) is False
    assert _exceeds_drag_threshold(0, 7) is False
    assert _exceeds_drag_threshold(7, 0) is False
    assert _exceeds_drag_threshold(5, 5) is True
    assert _exceeds_drag_threshold(5, 6) is True
    assert _exceeds_drag_threshold(-7, -1) is True

    pm, _, tmp_path = isolated_env
    assert pm.add_profile("Alpha", "http://u:p@1.2.3.4:10101", "windows")
    prev_lang = current_language()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        conn, sess = _connect_session(loop)
        await sess.connect(conn)
        app = _create_app(pm, _FakeBL(), tmp_path)
        try:
            set_language("en")
            app._main(sess.page)
            assert app.refs.profile_list_area.item_extent == ROW_HEIGHT
            old_alpha = app.refs.profile_list_area.controls[0]

            # Empty search clears fixed extent cleanly and restores cleanly
            app._on_search("__nonexistent_query__")
            assert app.refs.profile_list_area.item_extent is None
            assert [c.key for c in app.refs.profile_list_area.controls] == ["__empty__"]

            app._clear_filters()
            assert app.refs.profile_list_area.item_extent == ROW_HEIGHT
            assert app.refs.profile_list_area.controls[0].key == "Alpha"
            assert app.refs.profile_list_area.controls[0] is not old_alpha

            # Switching language clears caches and rebuilds root window controls
            current_alpha = app.refs.profile_list_area.controls[0]
            app._set_language("vi")
            assert current_language() == "vi"
            rebuilt_alpha = app.refs.profile_list_area.controls[0]
            assert rebuilt_alpha is not current_alpha
            assert "FoxProfile" in sess.page.title
            assert [
                m.body.message for m in conn.messages if m.action == MessageAction.SESSION_CRASHED
            ] == []
        finally:
            app._shutting_down = True
            set_language(prev_lang)

    asyncio.run(_run())


def test_d3_launch_debounce_rollback_floating_tab_bar_and_window_queue(isolated_env, monkeypatch):
    pm, _, tmp_path = isolated_env
    assert pm.add_profile(
        "P1",
        "http://u:p@1.2.3.4:10101",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
    )
    assert pm.add_profile(
        "P2",
        "http://u:p@1.2.3.4:10102",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
    )
    mon = MonitorService(data_dir=str(tmp_path / "camoufox_data"), log_dir=str(tmp_path / "logs"))
    state = AppState()
    bl = _FakeBL()
    refreshes: list[bool] = []
    h = AppHandlers(
        pm=pm,
        bl=bl,  # type: ignore[arg-type]
        ps=_FakePS(),  # type: ignore[arg-type]
        state=state,
        get_page=lambda: None,
        get_refs=lambda: None,
        log_fn=lambda _: None,
        refresh_fn=lambda *, reuse_snapshot=False: refreshes.append(reuse_snapshot),
        get_page_profiles=lambda: ([], pm.list_profiles(), 1),
        monitor=mon,
    )

    # Rapid double-click while loading triggers start_thread once
    h.on_launch("P1")
    h.on_launch("P1")
    assert bl.started == ["P1"]
    assert state.is_loading("P1") is True
    assert refreshes == [True]

    # Failed / busy launch clears loading flag immediately
    bl_busy = _FakeBL(raise_busy=True)
    state_busy = AppState()
    h_busy = AppHandlers(
        pm=pm,
        bl=bl_busy,  # type: ignore[arg-type]
        ps=_FakePS(),  # type: ignore[arg-type]
        state=state_busy,
        get_page=lambda: None,
        get_refs=lambda: None,
        log_fn=lambda _: None,
        refresh_fn=lambda *, reuse_snapshot=False: None,
        get_page_profiles=lambda: ([], pm.list_profiles(), 1),
        monitor=mon,
    )
    h_busy.on_launch("P2")
    assert state_busy.is_loading("P2") is False

    # FloatingProfileTabBar optimistically switches active tab in _state immediately
    selected: list[str] = []
    bar = FloatingProfileTabBar(
        on_select_tab=selected.append,
        on_close_tab=lambda _: None,
        on_toggle_single_stage=lambda: None,
        on_focus_foxprofile=lambda: None,
    )
    bar.sync(["P1", "P2"], set(), "P1", True, True)
    bar._dispatch_select_tab("P2")
    assert bar._state[2] == "P2"
    assert selected == ["P2"]

    # enqueue_window_action executes tasks in strict FIFO order on daemon thread outside pytest sync shortcut
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    wt_mod._WINDOW_ACTION_WORKER_STARTED = False
    order: list[tuple[int, str]] = []
    done = threading.Event()
    for idx in range(5):

        def _job(i: int = idx) -> None:
            time.sleep(0.005)
            order.append((i, threading.current_thread().name))
            if i == 4:
                done.set()

        wt_mod.enqueue_window_action(_job)

    assert done.wait(timeout=3.0)
    assert [i for i, _ in order] == [0, 1, 2, 3, 4]
    assert all(tname == "fox-window-action" for _, tname in order)


def test_d4_card_cache_invalidates_across_mutations_and_archive(isolated_env):
    pm, _, tmp_path = isolated_env
    assert pm.add_profile("Alpha", "http://u:p@1.2.3.4:10101", "windows")
    assert pm.add_profile("Beta", "http://u:p@1.2.3.4:10102", "windows")
    assert pm.add_profile("Gamma", "http://u:p@1.2.3.4:10103", "windows")

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        conn, sess = _connect_session(loop)
        await sess.connect(conn)
        app = _create_app(pm, _FakeBL(), tmp_path)
        try:
            app._main(sess.page)

            def _cards():
                return {c.key: c for c in app.refs.profile_list_area.controls}

            # Verify each field mutation invalidates ONLY Alpha and leaves Beta/Gamma untouched
            mutations = [
                lambda: app.h.on_save_acc_no("Alpha", "77")
                if not setattr(app.state, "editing_acc_no", "Alpha")
                else None,
                lambda: pm.update_profile(
                    "Alpha",
                    "Alpha",
                    "http://u:p@1.2.3.4:10101",
                    "windows",
                    "America/Detroit",
                    "en-US",
                    None,
                    None,
                    "Updated note",
                    [],
                ),
                lambda: pm.update_profile_email("Alpha", "alpha@example.com"),
                lambda: pm.update_profile(
                    "Alpha",
                    "Alpha",
                    "http://u:p@1.2.3.4:10199",
                    "windows",
                    "America/Detroit",
                    "en-US",
                    None,
                    None,
                    "Updated note",
                    [],
                ),
                lambda: pm.bulk_update_status(["Alpha"], "active"),
                lambda: pm.bulk_update_pinned(["Alpha"], True),
            ]
            for mutate in mutations:
                before = _cards()
                mutate()
                app._refresh_profiles(reuse_snapshot=True)
                after = _cards()
                assert after["Alpha"] is not before["Alpha"]
                assert after["Beta"] is before["Beta"]
                assert after["Gamma"] is before["Gamma"]

            # Moving Alpha to archive group removes it from Active view and shows in Archived view
            pm.bulk_update_group(["Alpha"], "Banned")
            app._refresh_profiles(reuse_snapshot=True)
            assert [c.key for c in app.refs.profile_list_area.controls] == ["Beta", "Gamma"]
            app._on_filter("archived")
            assert [c.key for c in app.refs.profile_list_area.controls] == ["Alpha"]

            # Fast refresh schedule does not set full_snapshot flag
            app.state.consume_refresh()
            app.state.consume_full_snapshot()
            app.state.schedule_refresh(full_snapshot=False)
            assert app.state.consume_refresh() is True
            assert app.state.consume_full_snapshot() is False
        finally:
            app._shutting_down = True

    asyncio.run(_run())


def test_d5_prelaunch_safety_guards_and_win32_window_safety(isolated_env, monkeypatch):
    pm, _, tmp_path = isolated_env
    assert pm.add_profile("NoProxy", "", "windows")
    assert pm.add_profile(
        "BusyB",
        "http://u:p@1.2.3.4:10199",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
    )
    assert pm.add_profile(
        "BusyA",
        "http://u:p@1.2.3.4:10199",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
    )
    assert pm.add_profile("NoGeo", "http://u:p@1.2.3.4:10250", "windows")
    assert pm.add_profile(
        "PortChanged",
        "http://u:p@1.2.3.4:10301",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
    )
    # Seed data/cookies/email on PortChanged so port change warning triggers
    data_dir = tmp_path / "camoufox_data" / "PortChanged"
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "cookies.sqlite")
    try:
        conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, expiry INTEGER)")
        conn.execute(
            "INSERT INTO moz_cookies VALUES (?, ?, ?)",
            (".muse.ai", "hatch_sess", int(time.time()) + 86400),
        )
        conn.commit()
    finally:
        conn.close()
    pm.update_profile_email("PortChanged", "user@example.com")

    mon = MonitorService(data_dir=str(tmp_path / "camoufox_data"), log_dir=str(tmp_path / "logs"))
    mon.record_started("PortChanged", "http://u:p@1.2.3.4:10300")
    mon.record_stopped("PortChanged")

    class _FakePage:
        def __init__(self) -> None:
            self.dialogs: list[object] = []

        def show_dialog(self, dlg: object) -> None:
            self.dialogs.append(dlg)

        def pop_dialog(self) -> None:
            if self.dialogs:
                self.dialogs.pop()

        def update(self) -> None:
            pass

    bl = _FakeBL()
    bl._running.add("BusyA")
    page = _FakePage()
    state = AppState()
    h = AppHandlers(
        pm=pm,
        bl=bl,  # type: ignore[arg-type]
        ps=_FakePS(),  # type: ignore[arg-type]
        state=state,
        get_page=lambda: page,  # type: ignore[return-value]
        get_refs=lambda: None,
        log_fn=lambda _: None,
        refresh_fn=lambda *, reuse_snapshot=False: None,
        get_page_profiles=lambda: ([], pm.list_profiles(), 1),
        monitor=mon,
    )

    for target in ("NoProxy", "BusyB", "NoGeo", "PortChanged"):
        before_dlgs = len(page.dialogs)
        h.on_launch(target)
        assert len(page.dialogs) == before_dlgs + 1
        assert target not in bl.started

    # Verify Win32 SWP_NOSIZE + single-pass EnumWindows + GetForegroundWindow toggle
    if sys.platform != "win32":
        pytest.skip("Win32 ctypes test")

    windows = {
        101: {"title": "[P1] Browser", "visible": True, "iconic": False, "rect": (10, 10, 1290, 730), "pid": 111},
        102: {"title": "[P2] Browser", "visible": True, "iconic": False, "rect": (20, 20, 1300, 740), "pid": 222},
        103: {"title": "Hung Window", "visible": True, "iconic": False, "rect": (0, 0, 800, 600), "pid": 333, "hung": True},
        104: {"title": "FoxProfile", "visible": True, "iconic": False, "rect": (50, 50, 1050, 750), "pid": 444},
    }
    enum_calls = 0
    show_async_calls: list[tuple[int, int]] = []
    set_window_pos_calls: list[tuple[int, int, int, int]] = []
    fg_holder = [101]

    def _unwrap(h_val):
        return int(getattr(h_val, "value", h_val))

    def _enum_windows(cb, _lparam):
        nonlocal enum_calls
        enum_calls += 1
        for hwnd in windows:
            if not cb(hwnd, 0):
                break
        return 1

    def _internal_title(hwnd, buf, cch):
        t = windows.get(_unwrap(hwnd), {}).get("title", "")
        buf.value = t[: cch - 1]
        return len(buf.value)

    def _get_rect(hwnd, rect_ptr):
        left, top, right, bottom = windows[_unwrap(hwnd)]["rect"]
        rect_ptr._obj.left = left
        rect_ptr._obj.top = top
        rect_ptr._obj.right = right
        rect_ptr._obj.bottom = bottom
        return 1

    def _get_pid(hwnd, pid_ptr):
        pid_val = windows.get(_unwrap(hwnd), {}).get("pid", 0)
        if pid_ptr is not None:
            pid_ptr._obj.value = pid_val
        return pid_val or 1

    def _set_window_pos(hwnd, _after, x, y, cx, cy, flags):
        set_window_pos_calls.append((_unwrap(hwnd), int(x), int(y), int(flags)))
        return 1

    fake_user32 = SimpleNamespace(
        EnumWindows=_enum_windows,
        IsWindowVisible=lambda h: bool(windows.get(_unwrap(h), {}).get("visible", False)),
        InternalGetWindowText=_internal_title,
        IsHungAppWindow=lambda h: bool(windows.get(_unwrap(h), {}).get("hung", False)),
        GetWindowTextLengthW=lambda h: 0
        if windows.get(_unwrap(h), {}).get("hung")
        else len(windows.get(_unwrap(h), {}).get("title", "")),
        GetWindowTextW=lambda h, buf, n: setattr(
            buf, "value", windows[_unwrap(h)]["title"][: n - 1]
        )
        or 1,
        IsIconic=lambda h: bool(windows.get(_unwrap(h), {}).get("iconic", False)),
        GetWindowRect=_get_rect,
        GetWindowThreadProcessId=_get_pid,
        ShowWindowAsync=lambda h, cmd: show_async_calls.append((_unwrap(h), int(cmd))) or 1,
        ShowWindow=lambda h, cmd: 1,
        SetWindowPos=_set_window_pos,
        GetForegroundWindow=lambda: fg_holder[0],
        GetWindowLongW=lambda h, idx: 0,
        SetWindowLongW=lambda h, idx, v: 0,
        GetWindowThreadProcessId_orig=None,
        GetCurrentThreadId=lambda: 1,
        AttachThreadInput=lambda a, b, c: 1,
        SetForegroundWindow=lambda h: fg_holder.__setitem__(0, _unwrap(h)) or 1,
        BringWindowToTop=lambda h: 1,
        keybd_event=lambda *a: None,
    )
    fake_windll = SimpleNamespace(
        user32=fake_user32,
        kernel32=ctypes.windll.kernel32,
    )
    monkeypatch.setattr(ctypes, "windll", fake_windll)

    found = wt_mod._find_all_profile_hwnds_win32([("P1", 111), ("P2", 222)])
    assert enum_calls == 1
    assert found == {"P1": [101], "P2": [102]}

    assert wt_mod.switch_browser_window("P2", [("P1", 111), ("P2", 222)], single_stage=True) is True
    assert enum_calls == 2
    assert any(h == 101 and cmd == 11 for h, cmd in show_async_calls)
    assert any(h == 102 and (flags & 0x0001) == 0x0001 for h, _, _, flags in set_window_pos_calls)

    # Toggle FoxProfile: when covered (foreground=102), brings 104 to foreground without minimizing
    fg_holder[0] = 102
    show_async_calls.clear()
    set_window_pos_calls.clear()
    assert wt_mod.focus_foxprofile_window("FoxProfile", toggle=True) is True
    assert show_async_calls == []
    assert any(h == 104 and (flags & 0x0001) == 0x0001 for h, _, _, flags in set_window_pos_calls)

    # Toggle FoxProfile: when foreground == 104, minimizes via ShowWindowAsync
    fg_holder[0] = 104
    show_async_calls.clear()
    assert wt_mod.focus_foxprofile_window("FoxProfile", toggle=True) is True
    assert any(h == 104 and cmd == 11 for h, cmd in show_async_calls)


def test_d6_subtree_isolation_patch_size_and_live_event_dispatch(isolated_env):
    pm, data_dir, tmp_path = isolated_env
    for i in range(29):
        n = f"Live_{i:02d}"
        pm.profiles[n] = Profile(
            name=n,
            proxy=f"http://u:p@1.2.3.4:{10100 + i}",
            os_type="windows",
            timezone="America/Detroit",
            locale="en-US",
        )
        (data_dir / n).mkdir(exist_ok=True)
    for i in range(118):
        n = f"Watch_{i:03d}"
        pm.profiles[n] = Profile(
            name=n,
            proxy=f"http://u:p@1.2.3.4:{11000 + i}",
            os_type="windows",
            timezone="America/Detroit",
            locale="en-US",
            group="Theo dõi",
        )
        (data_dir / n).mkdir(exist_ok=True)
    pm.save_profiles()

    def _find_control(node, pred):
        if pred(node):
            return node
        for attr in ("content", "leading", "trailing", "title", "subtitle"):
            child = getattr(node, attr, None)
            if isinstance(child, ft.BaseControl):
                found = _find_control(child, pred)
                if found is not None:
                    return found
        for child in getattr(node, "controls", None) or []:
            if isinstance(child, ft.BaseControl):
                found = _find_control(child, pred)
                if found is not None:
                    return found
        return None

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        conn, sess = _connect_session(loop)
        await sess.connect(conn)
        app = _create_app(pm, _FakeBL(), tmp_path)

        def _patch_bytes(start_idx: int) -> int:
            return sum(
                len(msgpack.packb([m.action, m.body], default=conn._enc))
                for m in conn.messages[start_idx:]
            )

        try:
            app._main(sess.page)
            assert len(app.refs.profile_list_area.controls) == 147
            assert all(
                getattr(ctrl, "_isolated", False) is True
                for ctrl in app.refs.profile_list_area.controls
            )

            # 1. Single-row checkbox select emits a small isolated patch (< 12 KB) and unselect works
            m0 = len(conn.messages)
            app.h.on_toggle_select("Watch_010")
            assert app.state.is_selected("Watch_010")
            assert _patch_bytes(m0) < 12_000
            m1 = len(conn.messages)
            app.h.on_toggle_select("Watch_010")
            assert not app.state.is_selected("Watch_010")
            assert _patch_bytes(m1) < 12_000

            # 2. Real Flet dispatch_event double_tap -> apply_patch -> submit on isolated card
            live02 = next(c for c in app.refs.profile_list_area.controls if c.key == "Live_02")
            gd = _find_control(
                live02,
                lambda x: isinstance(x, ft.GestureDetector)
                and getattr(x, "on_double_tap", None) is not None,
            )
            assert gd is not None and gd._i in sess.index
            await sess.dispatch_event(gd._i, "double_tap", {})
            assert app.state.editing_acc_no == "Live_02"

            live02_edit = next(
                c for c in app.refs.profile_list_area.controls if c.key == "Live_02"
            )
            tf = _find_control(live02_edit, lambda x: isinstance(x, ft.TextField))
            assert tf is not None and tf._i in sess.index
            sess.apply_patch(tf._i, {"value": "19"})
            await sess.dispatch_event(tf._i, "submit", "19")
            assert pm.profiles["Live_02"].acc_no == "19"

            # 3. Background log flush emits tiny patch (< 4 KB) without scanning mounted rows
            m2 = len(conn.messages)
            app.state.add_log("Browser started")
            app._flush_log()
            app._safe_update()
            assert _patch_bytes(m2) < 4_000

            assert not [
                m for m in conn.messages if m.action == MessageAction.SESSION_CRASHED
            ]
        finally:
            app._shutting_down = True

    asyncio.run(_run())


def _find_descendant(node, pred):
    if pred(node):
        return node
    for attr in ("content", "leading", "trailing", "title", "subtitle"):
        child = getattr(node, attr, None)
        if isinstance(child, ft.BaseControl):
            found = _find_descendant(child, pred)
            if found is not None:
                return found
    for attr in ("controls", "actions"):
        for child in getattr(node, attr, None) or []:
            if isinstance(child, ft.BaseControl):
                found = _find_descendant(child, pred)
                if found is not None:
                    return found
    return None


def _collect_frozen_objects(root, seen=None, found=None):
    if seen is None:
        seen = set()
    if found is None:
        found = []
    if root is None:
        return found
    oid = id(root)
    if oid in seen:
        return found
    if dataclasses.is_dataclass(root):
        seen.add(oid)
        if hasattr(root, "_frozen"):
            found.append(type(root).__name__)
        for field in dataclasses.fields(root):
            if field.name == "_parent" or field.metadata.get("skip", False):
                continue
            _collect_frozen_objects(getattr(root, field.name, None), seen, found)
    elif isinstance(root, (list, tuple)):
        seen.add(oid)
        for item in root:
            _collect_frozen_objects(item, seen, found)
    elif isinstance(root, dict):
        seen.add(oid)
        for val in root.values():
            _collect_frozen_objects(val, seen, found)
    return found


def test_d7_frozen_controls_never_leak_across_keyed_rebuilds_dialogs_and_styles(isolated_env):
    pm, _, tmp_path = isolated_env
    for i in range(6):
        assert pm.add_profile(f"P{i}", f"http://u:p@1.2.3.4:{10100 + i}", "windows")
    pm.trash_profile("P5")

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        conn, sess = _connect_session(loop)
        await sess.connect(conn)
        app = _create_app(pm, _FakeBL(), tmp_path)
        try:
            app._main(sess.page)

            def _assert_unfrozen() -> None:
                assert _collect_frozen_objects(sess.page) == []
                style_frozen = [
                    name
                    for name, val in vars(styles_mod).items()
                    if dataclasses.is_dataclass(val) and _collect_frozen_objects(val)
                ]
                assert style_frozen == []

            _assert_unfrozen()

            # Keyed card replacement during inline acc_no edit & checkbox toggle
            app.h.on_start_edit_acc_no("P1")
            _assert_unfrozen()
            app.h.on_save_acc_no("P1", "88")
            _assert_unfrozen()
            app.h.on_toggle_select("P2")
            _assert_unfrozen()

            # Imperative mutation on a child control inside a previously replaced card must never raise
            p1_card = next(c for c in app.refs.profile_list_area.controls if c.key == "P1")
            p1_cb = _find_descendant(p1_card, lambda c: isinstance(c, ft.Checkbox))
            assert p1_cb is not None
            p1_cb.value = True
            p1_cb.update()
            _assert_unfrozen()

            # Empty transition and dialog updates keep everything unfrozen
            app._on_search("__no_such_profile__")
            _assert_unfrozen()
            app._clear_filters()
            _assert_unfrozen()

            app.h.on_open_trash()
            _assert_unfrozen()
            sess.page.pop_dialog()
            _assert_unfrozen()

            assert not [
                m for m in conn.messages if m.action == MessageAction.SESSION_CRASHED
            ]
        finally:
            app._shutting_down = True

    asyncio.run(_run())


def test_d8_empty_trash_delete_trashed_and_bulk_trash_are_nonblocking_and_virtualized(
    isolated_env, monkeypatch
):
    pm, data_dir, tmp_path = isolated_env
    names: list[str] = []
    for i in range(120):
        name = f"Bulk_{i:03d}"
        names.append(name)
        assert pm.add_profile(name, f"http://u:p@1.2.3.4:{10100 + i}", "windows")
        entries_dir = data_dir / name / "cache2" / "entries"
        entries_dir.mkdir(parents=True, exist_ok=True)
        (entries_dir / "item.bin").write_bytes(b"cache-entry")

    save_calls = 0
    orig_save = pm.save_profiles

    def _counting_save() -> None:
        nonlocal save_calls
        save_calls += 1
        orig_save()

    monkeypatch.setattr(pm, "save_profiles", _counting_save)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        conn, sess = _connect_session(loop)
        await sess.connect(conn)

        logs: list[str] = []
        refreshes = 0

        def _on_refresh() -> None:
            nonlocal refreshes
            refreshes += 1

        bulk_delete_profiles(
            sess.page,
            names,
            pm,
            logs.append,
            _on_refresh,
            lambda: None,
        )
        confirm_dlg = sess.page._dialogs.controls[-1]
        confirm_btn = _find_descendant(
            confirm_dlg,
            lambda c: isinstance(c, ft.Button) and getattr(c, "on_click", None) is not None,
        )
        assert confirm_btn is not None
        confirm_btn.on_click(SimpleNamespace(control=confirm_btn))
        sess.page.pop_dialog()

        assert save_calls == 1
        assert len(pm.profiles) == 0
        assert len(pm.list_trashed_profiles()) == 120
        assert len(logs) == 1 and "120" in logs[0]
        assert refreshes == 1

        # Deleting a single trashed item finishes fast without UI blocking
        t0 = time.perf_counter()
        assert pm.delete_trashed_profile("Bulk_000") is True
        assert (time.perf_counter() - t0) < 0.5
        assert len(pm.list_trashed_profiles()) == 119

        # Open trash dialog -> uses virtualized ListView(item_extent=62) and empty guard
        empty_calls = 0

        def _on_empty() -> None:
            nonlocal empty_calls
            empty_calls += 1
            t_start = time.perf_counter()
            removed = pm.empty_trash()
            assert (time.perf_counter() - t_start) < 0.25
            assert removed == 119

        open_trash_dialog(
            sess.page,
            pm.list_trashed_profiles,
            pm.restore_profile,
            pm.delete_trashed_profile,
            _on_empty,
            ui_lock=AppState()._ui_update_lock,
        )
        trash_dlg = sess.page._dialogs.controls[-1]
        lv = _find_descendant(trash_dlg, lambda c: isinstance(c, ft.ListView))
        assert lv is not None
        assert lv.item_extent == 62
        assert len(lv.controls) == 119

        empty_btn = _find_descendant(
            trash_dlg,
            lambda c: isinstance(c, ft.Button)
            and getattr(c, "icon", None) == ft.Icons.DELETE_SWEEP,
        )
        assert empty_btn is not None and not empty_btn.disabled

        # Trigger empty + redundant rapid clicks -> only executes once and .trash vanishes instantly
        empty_btn.on_click(SimpleNamespace(control=empty_btn))
        empty_btn.on_click(SimpleNamespace(control=empty_btn))
        assert empty_calls == 1
        assert empty_btn.disabled is True
        assert lv.item_extent is None
        assert not pm._trash_data_dir().exists()
        assert pm.list_trashed_profiles() == []

        pm.wait_for_trash_purge(timeout=5.0)
        assert not list(data_dir.glob(".trash_purging_*"))

    asyncio.run(_run())


def test_d9_cross_tab_card_cache_remount_and_nonblocking_handlers(isolated_env):
    pm, _, tmp_path = isolated_env
    for i in range(5):
        assert pm.add_profile(
            f"Live_{i}", f"http://u:p@1.2.3.4:{10100 + i}", "windows", pages=["Live"]
        )
    for i in range(8):
        assert pm.add_profile(
            f"Watch_{i}", f"http://u:p@1.2.3.4:{10200 + i}", "windows", pages=["Watch"]
        )
    assert pm.add_profile(
        "Shared_X",
        "http://u:p@1.2.3.4:10300",
        "windows",
        pages=["Live", "Watch"],
        two_fa="JBSWY3DPEHPK3PXP",
    )

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        conn, sess = _connect_session(loop)
        await sess.connect(conn)
        app = _create_app(pm, _FakeBL(), tmp_path)
        try:
            app._main(sess.page)
            app._on_select_custom_page("Live")
            live_cards = {c.key: c for c in app.refs.profile_list_area.controls}
            assert set(live_cards) == {f"Live_{i}" for i in range(5)} | {"Shared_X"}

            # Switch to Watch and back to Live -> cached cards are retained and reused
            app._on_select_custom_page("Watch")
            watch_cards = {c.key: c for c in app.refs.profile_list_area.controls}
            assert watch_cards["Shared_X"] is live_cards["Shared_X"]

            app._on_select_custom_page("Live")
            remounted_live = {c.key: c for c in app.refs.profile_list_area.controls}
            for k, card in live_cards.items():
                assert remounted_live[k] is card

            # Remounted card's child controls stay indexed and dispatch real events
            cb_l2 = _find_descendant(remounted_live["Live_2"], lambda c: isinstance(c, ft.Checkbox))
            assert cb_l2 is not None and cb_l2._i in sess.index
            await sess.dispatch_event(cb_l2._i, "change", True)
            assert app.state.is_selected("Live_2")

            # Interactive handlers refresh UI via cached snapshot without blocking on monitor.snapshot
            with patch.object(
                app.monitor, "snapshot", side_effect=AssertionError("blocking snapshot call")
            ):
                app.state.consume_full_snapshot()
                app.h.on_toggle_pin("Live_1")
                assert pm.profiles["Live_1"].pinned is True
                assert app.state.consume_full_snapshot() is True

                app.h.on_copy_2fa("Shared_X")
                app.h.on_start_edit_proxy("Live_3")
                app.h.on_save_proxy("Live_3", "http://u:p@1.2.3.4:10999")
                assert pm.profiles["Live_3"].proxy == "http://u:p@1.2.3.4:10999"
                assert app.state.consume_full_snapshot() is True

            assert not [
                m for m in conn.messages if m.action == MessageAction.SESSION_CRASHED
            ]
        finally:
            app._shutting_down = True

    asyncio.run(_run())
