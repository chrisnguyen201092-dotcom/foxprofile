import asyncio
import json
import sqlite3
import time

import pytest

from src.models.profile import Profile
from src.services.browser import control, cookies
from src.services.monitor import MonitorService, extract_proxy_port
from src.services.profile import manager as pm_mod
from src.ui.state import AppState


@pytest.fixture
def isolated_pm(tmp_path, monkeypatch):
    data_dir = tmp_path / "camoufox_data"
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(profiles_file))
    return pm_mod.ProfileManager()


def test_clone_profile_creates_fresh_dir_without_fingerprint(isolated_pm, tmp_path):
    assert isolated_pm.add_profile(
        name="Acc 1",
        proxy="http://user:pass@1.2.3.4:44200",
        os_type="windows",
        color="#3B82F6",
        group="Team A",
        notes="VIP account",
        pages=["Page 1"],
    )
    src_dir = tmp_path / "camoufox_data" / "Acc 1"
    (src_dir / "fingerprint.json").write_text('{"screen": "1920x1080"}', encoding="utf-8")

    assert isolated_pm.clone_profile("Acc 1", "Acc 1 Clone", copy_proxy=True)
    cloned = isolated_pm.profiles["Acc 1 Clone"]
    assert cloned.proxy == "http://user:pass@1.2.3.4:44200"
    assert cloned.color == "#3B82F6"
    assert cloned.group == "Team A"
    assert cloned.notes == "VIP account"
    assert cloned.pages == ["Page 1"]
    cloned_dir = tmp_path / "camoufox_data" / "Acc 1 Clone"
    assert cloned_dir.exists()
    assert not (cloned_dir / "fingerprint.json").exists()

    assert isolated_pm.clone_profile("Acc 1", "Acc 1 NoProxy", copy_proxy=False)
    assert isolated_pm.profiles["Acc 1 NoProxy"].proxy is None


def test_trash_and_restore_profile_workflow(isolated_pm, tmp_path):
    assert isolated_pm.add_profile("Acc 2", "http://1.2.3.4:8080", "windows", notes="keep me")
    pdir = tmp_path / "camoufox_data" / "Acc 2"
    (pdir / "tabs.json").write_text('["https://hatch.io/thread/123"]', encoding="utf-8")

    assert isolated_pm.trash_profile("Acc 2")
    assert "Acc 2" not in isolated_pm.profiles
    assert not pdir.exists()

    trashed = isolated_pm.list_trashed_profiles()
    assert len(trashed) == 1
    assert trashed[0]["name"] == "Acc 2"

    # Create a new profile with the same name to verify restore never overwrites live profiles
    assert isolated_pm.add_profile("Acc 2", "", "linux")
    assert isolated_pm.restore_profile("Acc 2")
    assert "Acc 2 (1)" in isolated_pm.profiles
    restored_dir = tmp_path / "camoufox_data" / "Acc 2 (1)"
    assert (restored_dir / "tabs.json").exists()
    assert isolated_pm.list_trashed_profiles() == []


def test_empty_trash_and_delete_forever(isolated_pm):
    isolated_pm.add_profile("T1", "", "windows")
    isolated_pm.add_profile("T2", "", "windows")
    isolated_pm.trash_profile("T1")
    isolated_pm.trash_profile("T2")
    assert len(isolated_pm.list_trashed_profiles()) == 2

    assert isolated_pm.delete_trashed_profile("T1")
    assert len(isolated_pm.list_trashed_profiles()) == 1
    assert isolated_pm.empty_trash() == 1
    assert isolated_pm.list_trashed_profiles() == []


def test_bulk_update_group_and_color(isolated_pm):
    isolated_pm.add_profile("P1", "", "windows")
    isolated_pm.add_profile("P2", "", "windows")
    assert isolated_pm.bulk_update_group(["P1", "P2"], "BatchGroup") == 2
    assert isolated_pm.profiles["P1"].group == "BatchGroup"
    assert isolated_pm.profiles["P2"].group == "BatchGroup"

    assert isolated_pm.bulk_update_color(["P1", "P2"], "#22C55E") == 2
    assert isolated_pm.profiles["P1"].color == "#22C55E"
    assert isolated_pm.bulk_update_group(["P1", "P2"], None) == 2
    assert isolated_pm.profiles["P1"].group is None


def test_state_range_selection():
    state = AppState()
    visible = ["P1", "P2", "P3", "P4", "P5"]
    state.toggle_selection("P2")
    assert state.select_range(visible, "P4") == 3
    assert state.selected_names() == {"P2", "P3", "P4"}

    state.clear_selection()
    state.toggle_selection("P1")
    state.toggle_selection("P5")
    assert state.select_range(visible) == 5
    assert state.selected_names() == {"P1", "P2", "P3", "P4", "P5"}


def test_cookie_header_parsing_and_sqlite_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(cookies, "DATA_DIR", str(tmp_path))
    parsed = cookies.parse_cookies("Cookie: cf_clearance=abc123; session_id=xyz789")
    assert len(parsed) == 2
    assert parsed[0]["name"] == "cf_clearance"
    assert parsed[1]["name"] == "session_id"

    pdir = tmp_path / "AccSQL"
    pdir.mkdir()
    db_file = pdir / "cookies.sqlite"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE moz_cookies ("
        "name TEXT, value TEXT, host TEXT, path TEXT, "
        "expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER)"
    )
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("cf_clearance", "secret_val", ".hatch.io", "/", 2000000000, 1, 1, 1),
    )
    conn.commit()
    conn.close()

    rows = cookies.read_cookies_sqlite_readonly("AccSQL")
    assert rows is not None and len(rows) == 1
    assert rows[0]["name"] == "cf_clearance"
    summary = cookies.inspect_cookie_summary("AccSQL")
    assert summary["total"] == 1
    assert summary["has_gw"] is True
    assert "secret_val" not in json.dumps(summary)


def test_page_controller_live_cookies():
    class FakeContext:
        def __init__(self):
            self.pages = []
            self.stored = [{"name": "sid", "value": "1", "domain": ".hatch.io"}]

        async def cookies(self):
            return list(self.stored)

        async def add_cookies(self, items):
            self.stored.extend(items)

    ctx = FakeContext()
    pc = control.PageController(ctx)

    async def _run():
        exported = await pc.dispatch("cookies_export", {})
        assert len(exported["cookies"]) == 1
        imported = await pc.dispatch(
            "cookies_import",
            {"cookies": [{"name": "b", "value": "2", "domain": ".hatch.io"}]},
        )
        assert imported["imported"] == 1
        assert len(ctx.stored) == 2

    asyncio.run(_run())


def test_monitor_service_collisions_and_cooldown(tmp_path):
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir()
    log_dir.mkdir()

    p1_dir = data_dir / "Acc 1"
    p1_dir.mkdir()
    (p1_dir / "tabs.json").write_text('["https://app.hatch.io/thread/abc"]', encoding="utf-8")

    db_file = p1_dir / "cookies.sqlite"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, expiry INTEGER)")
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?)",
        (".hatch.io", "cf_clearance", int(time.time()) + 7200),
    )
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?)",
        (".hatch.io", "session_token", int(time.time()) + 7200),
    )
    conn.commit()
    conn.close()

    assert extract_proxy_port("http://u:p@1.2.3.4:44200") == ":44200"
    assert extract_proxy_port("sp02-usa102.nguonproxy.com:10620:LF0AI:81379") == ":10620"
    assert extract_proxy_port("Zb3F3:42425@sp02-usa114.nguonproxy.com:10010") == ":10010"

    profiles = [
        Profile(name="Acc 1", proxy="http://u:p@1.2.3.4:44200", os_type="windows"),
        Profile(name="Acc 21", proxy="http://u:p@1.2.3.4:44200", os_type="windows"),
    ]
    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))

    # Both running on the same port -> proxy collision
    snap = mon.snapshot(profiles, {"Acc 1", "Acc 21"})
    assert len(snap.proxy_collisions) == 1
    assert snap.profiles["Acc 1"].busy_by == ["Acc 21"]
    assert snap.profiles["Acc 1"].has_warning is True

    # Acc 1 stops -> Acc 21 stopped sees 15m port cooldown from Acc 1
    mon.record_stopped("Acc 1")
    snap2 = mon.snapshot(profiles, set())
    assert snap2.profiles["Acc 1"].hatch_gw is True
    assert snap2.profiles["Acc 1"].has_thread_url is True
    assert snap2.profiles["Acc 21"].port_cooldown_by is not None
    assert snap2.profiles["Acc 21"].port_cooldown_by[0] == "Acc 1"


def test_prelaunch_safety_guard_four_layers_and_cooldown_config(tmp_path):
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir()
    log_dir.mkdir()

    # Create cookies.sqlite for Acc 1 so it has an active session on :44200
    p1_dir = data_dir / "Acc 1"
    p1_dir.mkdir()
    db_file = p1_dir / "cookies.sqlite"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, expiry INTEGER)")
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?)",
        (".muse.ai", "hatch_gw", int(time.time()) + 7200),
    )
    conn.commit()
    conn.close()

    p1 = Profile(
        name="Acc 1",
        proxy="http://u:p@1.2.3.4:44200",
        os_type="windows",
        timezone="America/Detroit",
        locale="en-US",
    )
    p2 = Profile(
        name="Acc 2",
        proxy="http://u:p@1.2.3.4:44200",
        os_type="windows",
        timezone="America/Detroit",
        locale="en-US",
    )
    p_no_proxy = Profile(name="Acc Direct", proxy=None, os_type="windows")
    p_no_geo = Profile(name="Acc NoGeo", proxy="http://u:p@1.2.3.4:44300", os_type="windows")
    all_profiles = [p1, p2, p_no_proxy, p_no_geo]

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    assert mon.cooldown_minutes == 15

    # Seed last_used_ports via initial snapshot
    mon.snapshot(all_profiles, set())

    # 1. Clean profile (p1) when idle -> 0 warnings
    assert mon.check_prelaunch(p1, all_profiles, set()) == []

    # 2. Layer 1: No proxy -> critical no_proxy warning
    w_noproxy = mon.check_prelaunch(p_no_proxy, all_profiles, set())
    assert any(w.code == "no_proxy" and w.level == "critical" for w in w_noproxy)

    # 3. Layer 2a: Port busy when Acc 1 is running and Acc 2 tries to launch
    w_busy = mon.check_prelaunch(p2, all_profiles, {"Acc 1"})
    assert any(w.code == "port_busy" and w.level == "critical" for w in w_busy)

    # 4. Layer 2b: Port cooldown after Acc 1 stops, plus custom cooldown setting
    mon.record_stopped("Acc 1")
    w_cd = mon.check_prelaunch(p2, all_profiles, set())
    assert any(w.code == "port_cooldown" for w in w_cd)

    mon.set_cooldown_minutes(0)
    assert mon.check_prelaunch(p2, all_profiles, set()) == []

    # Reload MonitorService to verify settings persistence
    mon2 = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    assert mon2.cooldown_minutes == 0
    mon2.set_cooldown_minutes(25)

    # 5. Layer 3: Changing proxy port on Acc 1 while it holds hatch_gw session cookies
    p1.proxy = "http://u:p@1.2.3.4:44999"
    w_changed = mon2.check_prelaunch(p1, all_profiles, set())
    assert any(w.code == "proxy_changed_with_session" and w.level == "critical" for w in w_changed)

    mon2.acknowledge_proxy_change("Acc 1", p1.proxy)
    assert not any(
        w.code == "proxy_changed_with_session"
        for w in mon2.check_prelaunch(p1, all_profiles, set())
    )

    # 6. Layer 4: Missing timezone/locale on proxy profile
    w_geo = mon2.check_prelaunch(p_no_geo, all_profiles, set())
    assert any(w.code == "missing_geo" for w in w_geo)

    # 7. Bulk prelaunch intra-batch port collision
    p1.proxy = "http://u:p@1.2.3.4:44200"
    mon2.acknowledge_proxy_change("Acc 1", p1.proxy)
    mon2.set_cooldown_minutes(0)
    bulk_issues = mon2.check_bulk_prelaunch(["Acc 1", "Acc 2"], all_profiles, set())
    assert "Acc 1" not in bulk_issues
    assert "Acc 2" in bulk_issues
    assert any(w.code == "batch_port_collision" for w in bulk_issues["Acc 2"])


def test_bulk_update_proxies_and_rotating_config_backups(isolated_pm):
    isolated_pm.add_profile("P1", "", "windows")
    isolated_pm.add_profile("P2", "", "windows")

    cnt = isolated_pm.bulk_update_proxies(
        {
            "P1": "http://u:p@1.2.3.4:10199",
            "P2": "not a valid proxy://::",
        }
    )
    assert cnt == 1
    assert isolated_pm.profiles["P1"].proxy == "http://u:p@1.2.3.4:10199"
    assert isolated_pm.profiles["P2"].proxy is None

    backups = isolated_pm.list_config_backups()
    assert len(backups) >= 1
    saved_data = json.loads(backups[-1].read_text(encoding="utf-8"))
    assert "P1" in saved_data


def test_window_focus_and_arrange_graceful_fallback():
    from src.services.browser.window_title import arrange_browser_windows, focus_browser_window

    assert focus_browser_window("__nonexistent_profile_xyz__", root_pid=99999999) is False
    assert arrange_browser_windows([("__nonexistent_profile_xyz__", 99999999)]) == 0


def test_ui_dialogs_and_profile_card_focus_render():
    from src.services.monitor import PreLaunchWarning
    from src.ui.components.profile_card import build_profile_card
    from src.ui.dialogs import (
        open_bulk_prelaunch_guard_dialog,
        open_bulk_proxy_dialog,
        open_cooldown_settings_dialog,
        open_export_dialog,
        open_prelaunch_guard_dialog,
        open_token_burst_alert_dialog,
    )

    p = Profile(
        name="Kenji",
        proxy="http://u:p@1.2.3.4:10199",
        os_type="windows",
        timezone="America/Detroit",
        locale="en-US",
    )
    row = build_profile_card(
        p,
        False,
        True,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        since="19:30",
        on_focus_window=lambda _: None,
    )
    assert row is not None

    class DummyPage:
        def __init__(self):
            self.dialogs = []

        def show_dialog(self, dlg):
            self.dialogs.append(dlg)

        def pop_dialog(self):
            if self.dialogs:
                self.dialogs.pop()

        def update(self):
            pass

    page = DummyPage()
    w = PreLaunchWarning(code="no_proxy", level="critical", message="No proxy")
    open_prelaunch_guard_dialog(page, "Kenji", [w], lambda: None)
    open_bulk_prelaunch_guard_dialog(page, ["Levi"], {"Kenji": [w]}, lambda: None, lambda: None)
    open_cooldown_settings_dialog(page, 15, lambda _: None)
    open_bulk_proxy_dialog(
        page,
        ["Kenji", "Levi"],
        {"Kenji": p.proxy, "Levi": None},
        lambda _: None,
        session_ports={"Kenji": ":10199"},
    )
    open_export_dialog(
        page,
        None,  # type: ignore[arg-type]
        [p],
        lambda *_: None,
        preselected_names={"Kenji"},
    )
    open_token_burst_alert_dialog(page, [("Kenji", 14)], on_focus_window=lambda _: None)
    assert len(page.dialogs) == 6


def test_app_handlers_on_launch_and_bulk_launch_with_monitor(isolated_pm, tmp_path):
    from src.ui.handlers import AppHandlers

    isolated_pm.add_profile("DirectAcc", "", "windows")
    isolated_pm.add_profile(
        "SafeAcc",
        "http://u:p@1.2.3.4:10199",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
    )

    class DummyBL:
        def __init__(self):
            self.started = []

        def is_running(self, name):
            return False

        def running_profile_names(self):
            return set()

        def start_thread(self, profile, log_callback, on_start=None, on_ready=None, on_stop=None):
            self.started.append(profile.name)
            return True

    class DummyPage:
        def __init__(self):
            self.dialogs = []

        def show_dialog(self, dlg):
            self.dialogs.append(dlg)

        def pop_dialog(self):
            if self.dialogs:
                self.dialogs.pop()

        def update(self):
            pass

    state = AppState()
    bl = DummyBL()
    page = DummyPage()
    mon = MonitorService(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"))
    h = AppHandlers(
        pm=isolated_pm,
        bl=bl,  # type: ignore[arg-type]
        ps=None,  # type: ignore[arg-type]
        state=state,
        get_page=lambda: page,  # type: ignore[return-value]
        get_refs=lambda: None,
        log_fn=lambda _: None,
        refresh_fn=lambda: None,
        get_page_profiles=lambda: ([], isolated_pm.list_profiles(), 1),
        monitor=mon,
    )

    # SafeAcc launches immediately without dialog
    h.on_launch("SafeAcc")
    assert bl.started == ["SafeAcc"]
    assert len(page.dialogs) == 0

    # DirectAcc (no proxy) opens pre-launch guard dialog instead of launching
    h.on_launch("DirectAcc")
    assert bl.started == ["SafeAcc"]
    assert len(page.dialogs) == 1

    # Bulk launch with DirectAcc selected also opens bulk pre-launch guard dialog
    state.select_all(["DirectAcc"])
    h.on_bulk_launch()
    assert len(page.dialogs) == 2


def test_token_burst_only_counts_running_hatch_token_files(tmp_path):
    import os

    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    entries_dir = data_dir / "AccTok" / "cache2" / "entries"
    entries_dir.mkdir(parents=True)

    now = time.time()
    # Create 60 normal webpage cache files (should NEVER count as token burst)
    for i in range(60):
        f = entries_dir / f"static_{i}"
        f.write_bytes(b"x" * 25_000 + b":https://muse.ai/static/app.js")
        os.utime(f, (now - 10, now - 10))

    # Create 13 genuine /api/hatch/token cache files (20KB..35KB, age >= 2s)
    for i in range(13):
        f = entries_dir / f"tok_{i}"
        f.write_bytes(b":https://muse.ai/api/hatch/token\n" + b"a" * 24_000)
        os.utime(f, (now - 15, now - 15))

    p = Profile(name="AccTok", proxy="http://u:p@1.2.3.4:10001", os_type="windows")
    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))

    # When stopped, burst_10m is always 0 (never alerts on closed profiles)
    snap_stopped = mon.snapshot([p], set())
    assert snap_stopped.profiles["AccTok"].burst_10m == 0
    assert snap_stopped.token_bursts == []

    # When running, only the 13 actual /api/hatch/token files are counted
    snap_running = mon.snapshot([p], {"AccTok"})
    assert snap_running.profiles["AccTok"].burst_10m == 13
    assert snap_running.token_bursts == [("AccTok", 13)]


def test_full_app_ui_and_all_handlers_end_to_end(isolated_pm, tmp_path):
    import flet as ft

    from src.core.container import Container
    from src.core.events import EventBus
    from src.ui.app import App

    isolated_pm.add_profile(
        "P1 (a@hotmail.com)",
        "http://u:p@1.2.3.4:10001",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
        color="#3B82F6",
        group="G1",
        notes="Note 1",
        pages=["CustomPage1"],
    )
    isolated_pm.add_profile(
        "P2 (b@hotmail.com)",
        "http://u:p@1.2.3.4:10001",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
    )

    class FakeBL:
        def __init__(self):
            self.running = {"P1 (a@hotmail.com)"}

        def is_running(self, name):
            return name in self.running

        def running_profile_names(self):
            return set(self.running)

        def running_count(self):
            return len(self.running)

        def start_thread(self, profile, log_callback, on_start=None, on_ready=None, on_stop=None):
            self.running.add(profile.name)
            return True

        def stop_profile(self, name, timeout=2):
            self.running.discard(name)
            return True

        def focus_profile(self, name):
            return True

        def arrange_windows(self, names=None):
            return len(self.running)

        def control(self, name, action, params=None, timeout=60):
            return {"cookies": []}

    class FakePS:
        def check_proxy_sync(self, proxy_str, timeout=10):
            return True, "IP: 1.2.3.4 (US)"

    class FakePage:
        def __init__(self):
            self.controls = []
            self.services = []
            self.dialogs = []
            self.title = ""
            self.theme_mode = None
            self.padding = 0
            self.bgcolor = ""
            self.window = type(
                "W", (), {"width": 1200, "height": 800, "min_width": 800, "min_height": 600}
            )()
            self.fonts = {}
            self.theme = None
            self.on_keyboard_event = None

        def add(self, *ctrls):
            self.controls.extend(ctrls)

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialogs.append(dlg)

        def pop_dialog(self):
            if self.dialogs:
                self.dialogs.pop()

        def run_task(self, coro_fn, *args):
            pass

    c = Container()
    c._instances["pm"] = isolated_pm
    c._instances["bl"] = FakeBL()
    c._instances["ps"] = FakePS()
    c._instances["eb"] = EventBus()

    app = App(c)
    app.monitor = MonitorService(
        data_dir=str(tmp_path / "camoufox_data"), log_dir=str(tmp_path / "logs")
    )
    app.h._monitor = app.monitor
    fake_page = FakePage()
    app.page = fake_page  # type: ignore[assignment]
    app._file_picker = ft.FilePicker()
    app.clipboard = ft.Clipboard()
    fake_page.services.extend([app._file_picker, app.clipboard])
    app._build_ui()

    # Exercise all sort modes
    for s_mode in (
        "custom",
        "cooldown_desc",
        "cooldown_asc",
        "name_asc",
        "name_desc",
        "group",
        "running",
        "proxy",
    ):
        app._on_sort(s_mode)

    # Exercise all filters
    for f_id in ("all", "running", "warnings", "proxy", "direct", "chrome", "group:G1"):
        app._on_filter(f_id)
    app._clear_filters()

    # Exercise selection & bulk bar actions
    app.h.on_select_all_page()
    assert len(app.state.selected_names()) == 2
    app.h.on_open_cooldown_settings()
    app.h.on_focus_window("P1 (a@hotmail.com)")
    app.h.on_arrange_windows()
    app.h.on_start_edit_note("P1 (a@hotmail.com)")
    app.h.on_save_note("P1 (a@hotmail.com)", "Updated note")
    app.h.on_start_edit_proxy("P2 (b@hotmail.com)")
    app.h.on_save_proxy("P2 (b@hotmail.com)", "http://u:p@1.2.3.4:10002")
    app.h.on_bulk_set_group()
    app.h.on_bulk_set_color()
    app.h.on_bulk_set_proxy()
    app.h.on_export_open()
    app.h.on_clone("P1 (a@hotmail.com)")
    app.h.on_cookies("P1 (a@hotmail.com)")
    app.h.on_cookies("P2 (b@hotmail.com)")
    app.h.on_open_trash()
    app.h.open_log_fullscreen()
    app.h.on_deselect_page()
    assert len(app.state.selected_names()) == 0


def test_batch_create_profiles_and_proxy_cycling(isolated_pm):
    created, skipped = isolated_pm.batch_create_profiles(
        entries=[
            ("Batch 1", "http://user:pass@1.2.3.4:10001"),
            ("Batch 2", "http://user:pass@1.2.3.4:10002"),
            ("Batch 3", None),
            ("Batch 1", "http://user:pass@1.2.3.4:10003"),
            ("", None),
        ],
        os_type="windows",
        group="TeamA",
        pages=["PageBatch"],
    )
    assert created == ["Batch 1", "Batch 2", "Batch 3"]
    assert skipped == ["Batch 1", ""]
    assert isolated_pm.profiles["Batch 1"].proxy == "http://user:pass@1.2.3.4:10001"
    assert isolated_pm.profiles["Batch 2"].proxy == "http://user:pass@1.2.3.4:10002"
    assert isolated_pm.profiles["Batch 3"].proxy is None
    assert isolated_pm.profiles["Batch 3"].group == "TeamA"
    assert isolated_pm.profiles["Batch 3"].pages == ["PageBatch"]
    assert "PageBatch" in isolated_pm.list_custom_pages()


def test_pin_missing_geo_never_overwrites_existing_geo(isolated_pm):
    from src.services.proxy.geo_check import derive_geo_from_ip, extract_ip_from_check_detail

    ip = extract_ip_from_check_detail("IP: 8.8.8.8 (Mountain View, US)")
    assert ip == "8.8.8.8"
    tz, loc = derive_geo_from_ip("8.8.8.8")
    assert tz is not None and loc is not None

    # Profile with missing timezone/locale -> gets pinned
    isolated_pm.add_profile("NoGeo", "http://u:p@1.2.3.4:10001", "windows")
    assert isolated_pm.profiles["NoGeo"].timezone is None
    assert isolated_pm.profiles["NoGeo"].locale is None
    assert isolated_pm.pin_missing_geo("NoGeo", tz, loc) is True
    assert isolated_pm.profiles["NoGeo"].timezone == tz
    assert isolated_pm.profiles["NoGeo"].locale == loc

    # Calling again when already pinned -> returns False and never overwrites
    isolated_pm.add_profile(
        "PinnedGeo",
        "http://u:p@1.2.3.4:10002",
        "windows",
        timezone="America/New_York",
        locale="en-US",
    )
    assert isolated_pm.pin_missing_geo("PinnedGeo", "Europe/London", "en-GB") is False
    assert isolated_pm.profiles["PinnedGeo"].timezone == "America/New_York"
    assert isolated_pm.profiles["PinnedGeo"].locale == "en-US"


def test_backup_inspect_and_restore_preserves_newer_profiles(isolated_pm):
    isolated_pm.add_profile(
        "OldAcc",
        "http://u:p@1.2.3.4:10001",
        "windows",
        group="InitialGroup",
    )
    backups = isolated_pm.list_config_backups()
    assert len(backups) >= 1
    bpath = backups[0]

    info = isolated_pm.inspect_config_backup(bpath)
    assert info is not None
    assert info["total"] == 1

    # Now mutate OldAcc and add NewerAcc
    isolated_pm.update_profile("OldAcc", "OldAcc", "", "windows")
    isolated_pm.bulk_update_group(["OldAcc"], "ChangedGroup")
    isolated_pm.add_profile("NewerAcc", "http://u:p@1.2.3.4:10009", "windows")
    # Overwrite backup file with snapshot of just OldAcc at InitialGroup
    bpath.write_text(
        json.dumps(
            {
                "OldAcc": {
                    "name": "OldAcc",
                    "proxy": "http://u:p@1.2.3.4:10001",
                    "os_type": "windows",
                    "group": "InitialGroup",
                }
            }
        ),
        encoding="utf-8",
    )

    ok, count = isolated_pm.restore_config_backup(bpath, keep_newer_profiles=True)
    assert ok is True
    assert count == 1
    # OldAcc restored to InitialGroup + its proxy, while NewerAcc is preserved
    assert isolated_pm.profiles["OldAcc"].group == "InitialGroup"
    assert isolated_pm.profiles["OldAcc"].proxy == "http://u:p@1.2.3.4:10001"
    assert "NewerAcc" in isolated_pm.profiles


def test_keyboard_shortcuts_and_batch_backup_dialogs(isolated_pm, tmp_path):
    import flet as ft

    from src.core.container import Container
    from src.core.events import EventBus
    from src.ui.app import App

    isolated_pm.add_profile("Acc1", "http://u:p@1.2.3.4:10001", "windows")

    class FakeBL:
        def is_running(self, name):
            return False

        def running_profile_names(self):
            return set()

        def running_count(self):
            return 0

    class FakePS:
        def check_proxy_sync(self, proxy_str, timeout=10):
            return True, "IP: 8.8.8.8 (US)"

    class FakePage:
        def __init__(self):
            self.controls = []
            self.services = []
            self.dialogs = []
            self.tasks = []
            self.title = ""
            self.theme_mode = None
            self.padding = 0
            self.bgcolor = ""
            self.window = type(
                "W", (), {"width": 1200, "height": 800, "min_width": 800, "min_height": 600}
            )()
            self.fonts = {}
            self.theme = None
            self.on_keyboard_event = None

        def add(self, *ctrls):
            self.controls.extend(ctrls)

        def update(self):
            pass

        def show_dialog(self, dlg):
            dlg.open = True
            self.dialogs.append(dlg)

        def pop_dialog(self):
            if self.dialogs:
                d = self.dialogs.pop()
                d.open = False

        def run_task(self, coro_fn, *args):
            self.tasks.append(coro_fn)

    c = Container()
    c._instances["pm"] = isolated_pm
    c._instances["bl"] = FakeBL()
    c._instances["ps"] = FakePS()
    c._instances["eb"] = EventBus()

    app = App(c)
    app.monitor = MonitorService(
        data_dir=str(tmp_path / "camoufox_data"), log_dir=str(tmp_path / "logs")
    )
    app.h._monitor = app.monitor
    fake_page = FakePage()
    app.page = fake_page  # type: ignore[assignment]
    app._file_picker = ft.FilePicker()
    app.clipboard = ft.Clipboard()
    fake_page.services.extend([app._file_picker, app.clipboard])
    app._build_ui()

    def _ev(key: str, ctrl: bool = False, shift: bool = False):
        return type(
            "KE",
            (),
            {"key": key, "ctrl": ctrl, "shift": shift, "alt": False, "meta": False},
        )()

    # 1. Ctrl+F focuses search field via run_task
    app._handle_key_event(_ev("F", ctrl=True))  # type: ignore[arg-type]
    assert len(fake_page.tasks) == 1

    # 2. Escape cancels inline edit first, then clears selection, then clears search
    app.state.editing_note = "Acc1"
    app.state.select_all(["Acc1"])
    app._on_search("acc")
    assert app.state.editing_note == "Acc1"

    app._handle_key_event(_ev("Escape"))  # type: ignore[arg-type]
    assert app.state.editing_note is None
    assert len(app.state.selected_names()) == 1

    app._handle_key_event(_ev("Escape"))  # type: ignore[arg-type]
    assert len(app.state.selected_names()) == 0
    assert app.state.search_query == "acc"

    app._handle_key_event(_ev("Escape"))  # type: ignore[arg-type]
    assert app.state.search_query == ""

    # 3. Ctrl+N opens New Profile dialog
    app._handle_key_event(_ev("N", ctrl=True))  # type: ignore[arg-type]
    assert len(fake_page.dialogs) == 1
    fake_page.pop_dialog()

    # 4. Open batch create dialog & backup restore dialog
    app.h.on_open_batch_create("__all__")
    assert len(fake_page.dialogs) == 1
    fake_page.pop_dialog()

    app.h.on_open_backups()
    assert len(fake_page.dialogs) == 1
    fake_page.pop_dialog()


def test_quick_status_badges_and_bulk_update_status(isolated_pm):
    from src.core.strings import get_string
    from src.models.profile import resolve_status_display

    isolated_pm.add_profile("S1", "http://u:p@1.2.3.4:10001", "windows", status="ready")
    isolated_pm.add_profile("S2", "http://u:p@1.2.3.4:10002", "windows")
    assert isolated_pm.profiles["S1"].status == "ready"
    disp = resolve_status_display("ready", get_string)
    assert disp is not None and disp[0] == get_string("status_ready")

    # Bulk update to custom status
    cnt = isolated_pm.bulk_update_status(["S1", "S2"], "VIP Queue")
    assert cnt == 2
    assert isolated_pm.profiles["S1"].status == "VIP Queue"
    assert isolated_pm.profiles["S2"].status == "VIP Queue"
    custom_disp = resolve_status_display("VIP Queue", get_string)
    assert custom_disp is not None and custom_disp[0] == "VIP Queue"

    # Update profile preserves status when status is not explicitly changed
    isolated_pm.update_profile(
        "S1",
        "S1",
        "http://u:p@1.2.3.4:10001",
        "windows",
        new_notes="hello",
    )
    assert isolated_pm.profiles["S1"].status == "VIP Queue"

    # Clone profile preserves status
    assert isolated_pm.clone_profile("S1", "S1 Clone")
    assert isolated_pm.profiles["S1 Clone"].status == "VIP Queue"

    # Too-long status (>32 chars) is rejected
    assert isolated_pm.bulk_update_status(["S1"], "X" * 40) == 0
    assert isolated_pm.profiles["S1"].status == "VIP Queue"

    # Clear status
    assert isolated_pm.bulk_update_status(["S1", "S2"], None) == 2
    assert isolated_pm.profiles["S1"].status is None

    # Edit default status tag ("ready" -> "Đã kháng", "#06B6D4")
    isolated_pm.bulk_update_status(["S1"], "ready")
    assert isolated_pm.save_status_tag("ready", "Đã kháng", "#06B6D4") is True
    edited_disp = resolve_status_display(isolated_pm.profiles["S1"].status, get_string)
    assert edited_disp == ("Đã kháng", "#06B6D4")

    # Delete default status tag ("ready") -> removes tag and clears S1's status
    assert isolated_pm.delete_status_tag("ready") is True
    assert isolated_pm.profiles["S1"].status is None
    assert not any(t["code"] == "ready" for t in isolated_pm.list_status_tags())

    # Restore default status tags
    restored = isolated_pm.reset_status_tags()
    assert len(restored) == 5
    assert any(t["code"] == "ready" for t in restored)


def test_central_proxy_pool_replace_across_profiles(isolated_pm):
    isolated_pm.add_profile("P1", "http://u:p@1.2.3.4:44200", "windows")
    isolated_pm.add_profile("P2", "http://u:p@1.2.3.4:44200", "windows")
    isolated_pm.add_profile("P3_Running", "http://u:p@1.2.3.4:44200", "windows")
    isolated_pm.add_profile("P4_Other", "http://u:p@1.2.3.4:44300", "windows")

    # Add unassigned (spare) proxies to the central proxy pool
    added, skipped = isolated_pm.add_proxies_to_pool(
        [
            "1.2.3.4:44500:u:p",
            "1.2.3.4:44501:u:p",
            "http://u:p@1.2.3.4:44200",  # Already in use on P1 -> skipped
            "invalid_proxy",
        ]
    )
    assert added == 2
    assert skipped == 2
    unassigned = isolated_pm.list_unassigned_proxies()
    assert len(unassigned) == 2
    assert any(":44500" in px for px in unassigned)

    # Removing 1 spare proxy from pool
    assert isolated_pm.remove_proxy_from_pool(":44501") is True
    assert len(isolated_pm.list_unassigned_proxies()) == 1

    # Replace by port :44200 while skipping P3_Running
    updated = isolated_pm.replace_proxy_across_profiles(
        ":44200",
        "http://u:p@9.9.9.9:55200",
        skip_names={"P3_Running"},
    )
    assert updated == ["P1", "P2"]
    assert isolated_pm.profiles["P1"].proxy == "http://u:p@9.9.9.9:55200"
    assert isolated_pm.profiles["P2"].proxy == "http://u:p@9.9.9.9:55200"
    assert isolated_pm.profiles["P3_Running"].proxy == "http://u:p@1.2.3.4:44200"
    assert isolated_pm.profiles["P4_Other"].proxy == "http://u:p@1.2.3.4:44300"


def test_csv_export_and_import_roundtrip_and_running_protection(isolated_pm, tmp_path):
    isolated_pm.add_profile(
        "CSV1",
        "http://u:p@1.2.3.4:10001",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
        color="#3B82F6",
        group="TeamCSV",
        notes="Initial note",
        pages=["PageA"],
        status="ready",
    )
    isolated_pm.add_profile(
        "RunningProf",
        "http://u:p@1.2.3.4:10002",
        "windows",
        notes="Do not touch",
    )

    csv_file = tmp_path / "profiles_export.csv"
    exported = isolated_pm.export_profiles_csv(str(csv_file))
    assert exported == 2
    raw_text = csv_file.read_text(encoding="utf-8-sig")
    assert "CSV1" in raw_text and "TeamCSV" in raw_text

    # Append a new profile row and modify CSV1 + RunningProf in the CSV
    lines = raw_text.strip().splitlines()
    lines.append(
        "CSV_New,working,GroupNew,PageB,http://u:p@1.2.3.4:10099,windows,America/Chicago,en-US,#22C55E,Added via CSV"
    )
    modified_csv = (
        "\n".join(lines)
        .replace("Initial note", "Updated via CSV")
        .replace("Do not touch", "Should be skipped")
    )
    csv_file.write_text(modified_csv, encoding="utf-8-sig")

    created, updated, skipped = isolated_pm.import_profiles_csv(
        str(csv_file),
        running_names={"RunningProf"},
    )
    assert created == 1
    assert updated == 1
    assert skipped == 1
    assert "CSV_New" in isolated_pm.profiles
    assert isolated_pm.profiles["CSV_New"].status == "working"
    assert isolated_pm.profiles["CSV_New"].pages == ["PageB"]
    assert isolated_pm.profiles["CSV1"].notes == "Updated via CSV"
    # RunningProf was protected and NOT modified
    assert isolated_pm.profiles["RunningProf"].notes == "Do not touch"


def test_per_profile_session_history_and_new_dialogs(isolated_pm, tmp_path):
    import flet as ft

    from src.core.container import Container
    from src.core.events import EventBus
    from src.ui.app import App

    isolated_pm.add_profile(
        "HistAcc",
        "http://u:p@1.2.3.4:44200",
        "windows",
        status="working",
    )

    mon = MonitorService(
        data_dir=str(tmp_path / "camoufox_data"),
        log_dir=str(tmp_path / "logs"),
    )
    mon.record_started("HistAcc", "http://u:p@1.2.3.4:44200")
    sessions_live = mon.get_profile_sessions("HistAcc")
    assert len(sessions_live) == 1
    assert sessions_live[0]["is_live"] is True
    assert sessions_live[0]["port"] == ":44200"

    mon.record_stopped("HistAcc")
    sessions_done = mon.get_profile_sessions("HistAcc")
    assert len(sessions_done) >= 1
    assert sessions_done[0]["is_live"] is False
    assert sessions_done[0]["duration_mins"] is not None

    class FakeBL:
        def is_running(self, name):
            return False

        def running_profile_names(self):
            return set()

        def running_count(self):
            return 0

    class FakePS:
        def check_proxy_sync(self, proxy_str, timeout=10):
            return True, "IP: 8.8.8.8 (US)"

    class FakePage:
        def __init__(self):
            self.controls = []
            self.services = []
            self.dialogs = []
            self.title = ""
            self.theme_mode = None
            self.padding = 0
            self.bgcolor = ""
            self.window = type(
                "W", (), {"width": 1200, "height": 800, "min_width": 800, "min_height": 600}
            )()
            self.fonts = {}
            self.theme = None
            self.on_keyboard_event = None

        def add(self, *ctrls):
            self.controls.extend(ctrls)

        def update(self):
            pass

        def show_dialog(self, dlg):
            dlg.open = True
            self.dialogs.append(dlg)

        def pop_dialog(self):
            if self.dialogs:
                d = self.dialogs.pop()
                d.open = False

        def run_task(self, coro_fn, *args):
            pass

    c = Container()
    c._instances["pm"] = isolated_pm
    c._instances["bl"] = FakeBL()
    c._instances["ps"] = FakePS()
    c._instances["eb"] = EventBus()

    app = App(c)
    app.monitor = mon
    app.h._monitor = mon
    fake_page = FakePage()
    app.page = fake_page  # type: ignore[assignment]
    app._file_picker = ft.FilePicker()
    app.clipboard = ft.Clipboard()
    fake_page.services.extend([app._file_picker, app.clipboard])
    app._build_ui()

    # Filter by status:working
    app._on_filter("status:working")
    all_p, _, _ = app._get_page_profiles()
    assert len(all_p) == 1 and all_p[0].name == "HistAcc"
    app._clear_filters()

    # Open single-profile status dialog
    app.h.on_set_status("HistAcc")
    assert len(fake_page.dialogs) == 1
    fake_page.pop_dialog()

    # Open bulk status dialog
    app.h.on_select_all_page()
    app.h.on_bulk_set_status()
    assert len(fake_page.dialogs) == 1
    fake_page.pop_dialog()

    # Open Proxy Pool dialog
    app.h.on_open_proxy_pool()
    assert len(fake_page.dialogs) == 1
    fake_page.pop_dialog()

    # Open Session History dialog
    app.h.on_open_session_history("HistAcc")
    assert len(fake_page.dialogs) == 1
    fake_page.pop_dialog()


def test_exit_ip_history_and_country_mismatch_warning(tmp_path):
    from src.services.proxy.geo_check import extract_country_from_check_detail

    assert extract_country_from_check_detail("Proxy working. IP: 172.58.12.34 (US)") == "US"
    assert extract_country_from_check_detail("Proxy working. IP: 1.2.3.4 (XX)") == ""

    data_dir = tmp_path / "camoufox_data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)

    p = Profile(
        name="TicketAcc",
        proxy="http://u:p@1.2.3.4:10001",
        os_type="windows",
        timezone="America/Chicago",
        locale="en-US",
    )

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    assert mon.get_ip_history("TicketAcc") == []

    # 1. Record initial US exit IP
    rec1 = mon.record_exit_ip(
        "TicketAcc",
        "172.58.12.34",
        country="US",
        tz="America/Chicago",
        proxy=p.proxy,
    )
    assert rec1 is not None
    assert rec1["ip"] == "172.58.12.34"
    assert rec1["country"] == "US"
    assert rec1["port"] == ":10001"

    # Re-checking the same IP on the same port updates timestamp without duplicating
    mon.record_exit_ip(
        "TicketAcc",
        "172.58.12.34",
        country="US",
        tz="America/Chicago",
        proxy=p.proxy,
    )
    assert len(mon.get_ip_history("TicketAcc")) == 1

    snap1 = mon.snapshot([p], set())
    info1 = snap1.profiles["TicketAcc"]
    assert info1.last_exit_ip == "172.58.12.34"
    assert info1.last_exit_country == "US"
    assert info1.ip_country_changed is False
    assert mon.check_prelaunch(p, [p], set(), snap=snap1) == []

    # 2. Record a rotated US exit IP (same country -> no country warning)
    mon.record_exit_ip(
        "TicketAcc",
        "172.58.99.88",
        country="US",
        tz="America/Chicago",
        proxy=p.proxy,
    )
    assert len(mon.get_ip_history("TicketAcc")) == 2
    snap2 = mon.snapshot([p], set())
    assert snap2.profiles["TicketAcc"].ip_country_changed is False

    # 3. Record an exit IP in a different country (DE) -> triggers ip_country_changed warning!
    mon.record_exit_ip(
        "TicketAcc",
        "88.198.1.2",
        country="DE",
        tz="Europe/Berlin",
        proxy=p.proxy,
    )
    snap3 = mon.snapshot([p], set())
    info3 = snap3.profiles["TicketAcc"]
    assert info3.ip_country_changed is True
    assert info3.last_exit_country == "DE"
    assert info3.prev_exit_country == "US"
    assert info3.has_warning is True

    warnings = mon.check_prelaunch(p, [p], set(), snap=snap3)
    assert any(w.code == "ip_country_changed" for w in warnings)

    # Verify persistence across MonitorService reload
    mon_reloaded = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    assert len(mon_reloaded.get_ip_history("TicketAcc")) == 3

    # Acknowledging proxy change clears the country jump warning
    mon_reloaded.acknowledge_proxy_change("TicketAcc", p.proxy)
    snap4 = mon_reloaded.snapshot([p], set())
    assert snap4.profiles["TicketAcc"].ip_country_changed is False

    # Clearing IP history removes all entries
    mon_reloaded.clear_ip_history("TicketAcc")
    assert mon_reloaded.get_ip_history("TicketAcc") == []


def test_pin_profiles_and_smart_operational_filters(isolated_pm, tmp_path):
    import flet as ft

    from src.core.container import Container
    from src.core.events import EventBus
    from src.ui.app import App

    isolated_pm.add_profile(
        "Alpha",
        "http://u:p@1.2.3.4:10001",
        "windows",
        timezone="America/Chicago",
        locale="en-US",
    )
    isolated_pm.add_profile(
        "Beta",
        "http://u:p@1.2.3.4:10002",
        "windows",
        timezone="America/Chicago",
        locale="en-US",
    )
    isolated_pm.add_profile(
        "GammaDirect",
        "",
        "windows",
    )

    # Pin Beta -> Beta must float above Alpha in all sort modes
    assert isolated_pm.toggle_pin_profile("Beta") is True
    assert isolated_pm.profiles["Beta"].pinned is True

    mon = MonitorService(
        data_dir=str(tmp_path / "camoufox_data"),
        log_dir=str(tmp_path / "logs"),
    )

    class FakeBL:
        def is_running(self, name):
            return False

        def running_profile_names(self):
            return set()

        def running_count(self):
            return 0

    class FakePS:
        def check_proxy_sync(self, proxy_str, timeout=10):
            return True, "IP: 8.8.8.8 (US)"

    class FakePage:
        def __init__(self):
            self.controls = []
            self.services = []
            self.dialogs = []
            self.title = ""
            self.theme_mode = None
            self.padding = 0
            self.bgcolor = ""
            self.window = type(
                "W", (), {"width": 1200, "height": 800, "min_width": 800, "min_height": 600}
            )()
            self.fonts = {}
            self.theme = None
            self.on_keyboard_event = None

        def add(self, *ctrls):
            self.controls.extend(ctrls)

        def update(self):
            pass

        def show_dialog(self, dlg):
            dlg.open = True
            self.dialogs.append(dlg)

        def pop_dialog(self):
            if self.dialogs:
                d = self.dialogs.pop()
                d.open = False

        def run_task(self, coro_fn, *args):
            pass

    c = Container()
    c._instances["pm"] = isolated_pm
    c._instances["bl"] = FakeBL()
    c._instances["ps"] = FakePS()
    c._instances["eb"] = EventBus()

    app = App(c)
    app.monitor = mon
    app.h._monitor = mon
    fake_page = FakePage()
    app.page = fake_page  # type: ignore[assignment]
    app._file_picker = ft.FilePicker()
    app.clipboard = ft.Clipboard()
    fake_page.services.extend([app._file_picker, app.clipboard])
    app._build_ui()

    # Check pinned profile floats to top even in name_asc sort
    app._on_sort("name_asc")
    ordered, _, _ = app._get_page_profiles()
    assert [p.name for p in ordered] == ["Beta", "Alpha", "GammaDirect"]

    # Filter: pinned
    app._on_filter("pinned")
    pinned_list, _, _ = app._get_page_profiles()
    assert [p.name for p in pinned_list] == ["Beta"]

    # Filter: ready_launch (has proxy, not running, no warnings)
    app._on_filter("ready_launch")
    ready_list, _, _ = app._get_page_profiles()
    assert [p.name for p in ready_list] == ["Beta", "Alpha"]

    # Simulate Alpha just closed -> Alpha enters cooldown filter
    mon.record_stopped("Alpha")
    app._refresh_profiles()
    app._on_filter("cooldown")
    cd_list, _, _ = app._get_page_profiles()
    assert [p.name for p in cd_list] == ["Alpha"]

    app._clear_filters()

    # Open IP history dialog directly from Proxy cell callback
    app.h.on_open_ip_history("Beta")
    assert len(fake_page.dialogs) == 1
    fake_page.pop_dialog()

    # Bulk toggle pin: select Alpha & GammaDirect (unpinned) -> pins both
    app.state.select_all(["Alpha", "GammaDirect"])
    app.h.on_bulk_toggle_pin()
    assert isolated_pm.profiles["Alpha"].pinned is True
    assert isolated_pm.profiles["GammaDirect"].pinned is True

    # Selecting all 3 (all pinned) -> bulk toggle unpins all 3
    app.state.select_all(["Alpha", "Beta", "GammaDirect"])
    app.h.on_bulk_toggle_pin()
    assert isolated_pm.profiles["Beta"].pinned is False


def test_account_age_and_ban_days_tracking_and_dialog(isolated_pm):
    from datetime import date, timedelta

    from src.models.profile import (
        compute_days_since,
        format_date_dmy,
        has_ban_page,
        is_ban_page,
        is_ban_status,
        parse_flexible_date,
        today_ymd,
    )
    from src.ui.components.profile_card import build_profile_card
    from src.ui.dialogs import open_profile_dates_dialog

    ref_today = date(2026, 10, 22)
    assert parse_flexible_date("2026-10-10", today=ref_today) == "2026-10-10"
    assert parse_flexible_date("10/10/2026", today=ref_today) == "2026-10-10"
    assert parse_flexible_date("12", today=ref_today) == "2026-10-10"
    assert parse_flexible_date("invalid-date", today=ref_today) is None
    assert compute_days_since("2026-10-10", today=ref_today) == 12
    assert format_date_dmy("2026-10-10") == "10/10/2026"
    assert is_ban_status("hold") is True
    assert is_ban_status("Banned") is True
    assert is_ban_status("ready") is False
    assert is_ban_page("Disconnected") is True
    assert has_ban_page(["Live", "Disconnected"]) is True
    assert has_ban_page(["Live"]) is False

    # 1. New profile defaults created_at to today, banned_at to None
    isolated_pm.add_profile("AccAge1", "http://1.2.3.4:8080", "windows", pages=["Live"])
    p = isolated_pm.profiles["AccAge1"]
    assert p.created_at == today_ymd()
    assert p.banned_at is None

    # 2. Moving to Disconnected page automatically records banned_at
    isolated_pm.add_profiles_to_page("Disconnected", ["AccAge1"])
    isolated_pm.remove_profiles_from_page("Live", ["AccAge1"])
    assert p.banned_at == today_ymd()

    # 3. Removing from Disconnected page clears banned_at
    isolated_pm.remove_profiles_from_page("Disconnected", ["AccAge1"])
    assert p.banned_at is None

    # 4. Setting status to 'hold' sets banned_at; setting to 'ready' clears it
    isolated_pm.bulk_update_status(["AccAge1"], "hold")
    assert p.banned_at == today_ymd()
    isolated_pm.bulk_update_status(["AccAge1"], "ready")
    assert p.banned_at is None

    # 5. Manual update via update_profile_dates (accepts days or DD/MM/YYYY)
    ten_days_ago = (date.today() - timedelta(days=10)).isoformat()
    four_days_ago = (date.today() - timedelta(days=4)).isoformat()
    assert isolated_pm.update_profile_dates("AccAge1", "10", format_date_dmy(four_days_ago)) is True
    assert p.created_at == ten_days_ago
    assert p.banned_at == four_days_ago

    # 6. Build profile card and verify age and ban badges render without error
    clicked: list[str] = []
    card = build_profile_card(
        p,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        device="1920x1080 · 8 CPU",
        on_edit_dates=lambda n: clicked.append(n),
    )
    assert card is not None

    # 7. Test open_profile_dates_dialog interaction
    class FakePage:
        def __init__(self):
            self.dialogs = []

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialogs.append(dlg)

        def pop_dialog(self):
            if self.dialogs:
                self.dialogs.pop()

    fp = FakePage()
    applied: list[tuple[str | None, str | None]] = []
    open_profile_dates_dialog(
        fp,  # type: ignore[arg-type]
        profile_name="AccAge1",
        created_at=p.created_at,
        banned_at=p.banned_at,
        on_apply=lambda c_ymd, b_ymd: applied.append((c_ymd, b_ymd)),
    )
    assert len(fp.dialogs) == 1
    dlg = fp.dialogs[0]
    save_btn = dlg.actions[1]
    save_btn.on_click(None)
    assert len(applied) == 1
    assert applied[0] == (ten_days_ago, four_days_ago)


def test_profile_email_inline_and_monitor_rename(isolated_pm, tmp_path):
    """Test profile email persistence, inline quick-edit, card rendering, and MonitorService rename."""
    from src.services.monitor import MonitorService
    from src.ui.components.profile_card import build_profile_card

    isolated_pm.add_profile(
        "Monica",
        "http://1.2.3.4:10783",
        "windows",
        email="vnacc2@gmail.com",
    )
    p = isolated_pm.profiles["Monica"]
    assert p.email == "vnacc2@gmail.com"

    # Update email via update_profile_email
    assert isolated_pm.update_profile_email("Monica", "monica.new@gmail.com") is True
    assert p.email == "monica.new@gmail.com"

    # Render card in normal and editing_email modes
    card_normal = build_profile_card(
        p,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        on_start_edit_email=lambda _: None,
        on_save_email=lambda _n, _v: None,
    )
    assert card_normal is not None

    card_editing = build_profile_card(
        p,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        editing_email=True,
        on_start_edit_email=lambda _: None,
        on_save_email=lambda _n, _v: None,
    )
    assert card_editing is not None

    # MonitorService.rename_profile migrates port & IP history
    mon = MonitorService(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"))
    mon.record_exit_ip(
        "Monica", "1.2.3.4", country="US", tz="America/Detroit", proxy="http://1.2.3.4:10783"
    )
    assert len(mon.get_ip_history("Monica")) == 1
    mon.rename_profile("Monica", "Monica2")
    assert len(mon.get_ip_history("Monica")) == 0
    assert len(mon.get_ip_history("Monica2")) == 1


def test_session_history_log_parsing_renames_and_persistence(tmp_path):
    """Test real log line parsing, profile rename & legacy Name (email) migration, and settings persistence."""
    import datetime as dt

    data_dir = tmp_path / "camoufox_data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir()
    log_dir.mkdir()

    today_str = dt.datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"foxprofile_{today_str.replace('-', '')}.log"
    log_file.write_text(
        "\n".join(
            [
                f"{today_str} 08:00:00 - INFO - foxprofile.browser.launcher - Starting browser for profile: 1",
                f"{today_str} 08:00:01 - INFO - foxprofile.app - [1] Starting browser for 1...",
                f"{today_str} 08:00:08 - INFO - foxprofile.browser.launcher - Browser started for profile: 1",
                f"{today_str} 08:15:10 - INFO - foxprofile.browser.launcher - Browser close detected for profile: 1",
                f"{today_str} 08:15:10 - INFO - foxprofile.app - Đã đóng: 1",
                f"{today_str} 08:15:10 - INFO - foxprofile.browser.launcher - Session ended for profile: 1",
                f"{today_str} 08:15:10 - INFO - foxprofile.browser.process - Browser 1 terminated gracefully",
                f"{today_str} 08:16:00 - INFO - foxprofile.profile.manager - Updated profile: 1 -> Monica (vnacc2)",
                f"{today_str} 08:30:00 - INFO - foxprofile.browser.launcher - Starting browser for profile: Monica",
                f"{today_str} 08:30:05 - INFO - foxprofile.browser.launcher - Browser started for profile: Monica",
                f"{today_str} 08:50:05 - INFO - foxprofile.browser.launcher - Browser close detected for profile: Monica",
                f"{today_str} 08:50:05 - INFO - foxprofile.app - Đã đóng: Monica",
                f"{today_str} 08:50:05 - INFO - foxprofile.browser.launcher - Session ended for profile: Monica",
            ]
        ),
        encoding="utf-8",
    )

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    p_monica = Profile(name="Monica", proxy="http://u:p@1.2.3.4:46227", os_type="windows")

    sessions = mon.get_profile_sessions(
        "Monica",
        is_running=False,
        current_proxy=p_monica.proxy,
    )
    assert len(sessions) == 2
    assert sessions[0]["start"] == f"{today_str} 08:30:00"
    assert sessions[0]["end"] == f"{today_str} 08:50:05"
    assert sessions[0]["duration_mins"] == 20
    assert sessions[0]["port"] == ":46227"
    assert sessions[1]["start"] == f"{today_str} 08:00:00"
    assert sessions[1]["duration_mins"] == 15

    snap = mon.snapshot([p_monica], set())
    assert snap.profiles["Monica"].today_minutes == 35
    assert snap.profiles["Monica"].last_closed is not None

    # Live session persistence across MonitorService restart
    mon.record_started("Monica", p_monica.proxy)
    mon.record_stopped("Monica")
    mon2 = MonitorService(data_dir=str(data_dir), log_dir=str(tmp_path / "empty_logs"))
    persisted = mon2.get_profile_sessions("Monica", is_running=False)
    assert len(persisted) == 1
    assert persisted[0]["port"] == ":46227"


def test_totp_2fa_and_password_profile_workflow(isolated_pm, tmp_path):
    """Test offline RFC 6238 TOTP generation, password/2FA persistence, CSV sync, and card badges."""
    from src.ui.components.profile_card import build_profile_card
    from src.utils.totp import extract_totp_params, generate_totp, normalize_totp_secret
    from src.utils.validation import validate_password, validate_two_fa

    # 1. TOTP normalization & RFC 6238 deterministic verification
    raw_secret = "jbsw y3dp-ehpk 3pxp"
    assert normalize_totp_secret(raw_secret) == "JBSWY3DPEHPK3PXP"
    uri = "otpauth://totp/Meta:vnacc2@gmail.com?secret=JBSWY3DPEHPK3PXP&issuer=Meta&period=30&digits=6"
    sec, digits, period = extract_totp_params(uri)
    assert (sec, digits, period) == ("JBSWY3DPEHPK3PXP", 6, 30)
    assert normalize_totp_secret("invalid_base32_1890!!!") == ""

    pair_t0 = generate_totp(raw_secret, timestamp=59.0)
    assert pair_t0 is not None
    code_59, rem_59 = pair_t0
    assert len(code_59) == 6 and code_59.isdigit()
    assert rem_59 == 1
    # URI and raw secret must produce the exact same 6-digit code at the same timestamp
    assert generate_totp(uri, timestamp=59.0) == (code_59, rem_59)

    assert validate_two_fa(raw_secret)[0] is True
    assert validate_two_fa(uri)[0] is True
    assert validate_two_fa("not-base32-0189")[0] is False
    assert validate_password("MyPass#2026")[0] is True

    # 2. Profile creation, update, clone, and CSV export/import with password & two_fa
    assert isolated_pm.add_profile(
        name="Acc2FA",
        proxy="http://1.2.3.4:10783",
        os_type="windows",
        email="acc2fa@gmail.com",
        password="SecretPass!123",
        two_fa=raw_secret,
    )
    p = isolated_pm.profiles["Acc2FA"]
    assert p.password == "SecretPass!123"
    assert p.two_fa == "JBSWY3DPEHPK3PXP"

    assert isolated_pm.clone_profile("Acc2FA", "Acc2FA Clone")
    cloned = isolated_pm.profiles["Acc2FA Clone"]
    assert cloned.password == "SecretPass!123"
    assert cloned.two_fa == "JBSWY3DPEHPK3PXP"

    csv_file = tmp_path / "profiles_2fa.csv"
    assert isolated_pm.export_profiles_csv(str(csv_file), names=["Acc2FA"]) == 1
    isolated_pm.update_profile(
        "Acc2FA",
        "Acc2FA",
        "http://1.2.3.4:10783",
        "windows",
        new_password="",
        new_two_fa="",
    )
    assert isolated_pm.profiles["Acc2FA"].password is None
    assert isolated_pm.profiles["Acc2FA"].two_fa is None

    created, updated, skipped = isolated_pm.import_profiles_csv(str(csv_file))
    assert (created, updated, skipped) == (0, 1, 0)
    assert isolated_pm.profiles["Acc2FA"].password == "SecretPass!123"
    assert isolated_pm.profiles["Acc2FA"].two_fa == "JBSWY3DPEHPK3PXP"

    # 3. Profile card renders 2FA & Pass copy chips and triggers callbacks
    copied_events: list[tuple[str, str]] = []
    card = build_profile_card(
        isolated_pm.profiles["Acc2FA"],
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        on_copy_2fa=lambda n: copied_events.append(("2fa", n)),
        on_copy_password=lambda n: copied_events.append(("pass", n)),
    )
    assert card is not None


def test_isp_and_datacenter_ip_type_detection_without_blocking_guard(tmp_path):
    """Test ISP/DC classification, proxy badge formatting, IP history persistence, and non-blocking guard."""
    import flet as ft

    from src.services.monitor import MonitorService
    from src.services.proxy.geo_check import classify_isp_type
    from src.ui.actions.bulk import format_proxy_badge
    from src.ui.dialogs import open_session_history_dialog

    # 1. Classify ISP vs Datacenter
    assert classify_isp_type("The Constant Company, LLC (Vultr)", True) == "DC"
    assert classify_isp_type("DigitalOcean, LLC", None) == "DC"
    assert classify_isp_type("AT&T Services, Inc.", False) == "ISP"
    assert classify_isp_type("", None) == ""

    # 2. Proxy badge formatting with DC / ISP tag
    assert format_proxy_badge(True, "Proxy hoạt động. IP: 45.76.12.34 (US)") == (
        "ok",
        "✓ 45.76.12.34 (US)",
    )
    assert format_proxy_badge(
        True,
        "Proxy hoạt động. IP: 45.76.12.34 (US)",
        ip_type="DC",
        country="US",
    ) == ("ok", "✓ 45.76.12.34 (US·DC)")

    # 3. MonitorService records ISP & IP type and persists them across reloads
    data_dir = tmp_path / "camoufox_data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir()
    log_dir.mkdir()
    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    p_dc = Profile(
        name="Monica",
        proxy="http://u:p@45.76.12.34:46227",
        os_type="windows",
        timezone="America/Chicago",
        locale="en-US",
    )
    mon.record_exit_ip(
        "Monica",
        "45.76.12.34",
        country="US",
        tz="America/Chicago",
        proxy=p_dc.proxy,
        isp="Choopa, LLC",
        ip_type="DC",
    )
    snap = mon.snapshot([p_dc], set())
    info = snap.profiles["Monica"]
    assert info.last_exit_ip == "45.76.12.34"
    assert info.last_exit_country == "US"
    assert info.last_exit_isp == "Choopa, LLC"
    assert info.last_exit_ip_type == "DC"

    # Datacenter IP must NOT trigger any pre-launch blocking warning
    assert mon.check_prelaunch(p_dc, [p_dc], set(), snap=snap) == []

    # Reload MonitorService and verify ISP/IP type persisted in monitor_settings.json
    mon2 = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    hist = mon2.get_ip_history("Monica")
    assert len(hist) == 1
    assert hist[0]["isp"] == "Choopa, LLC"
    assert hist[0]["ip_type"] == "DC"

    # Render IP history dialog with ISP and DC badge
    class FakePage:
        def __init__(self):
            self.dialogs: list[ft.AlertDialog] = []

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialogs.append(dlg)

        def pop_dialog(self):
            if self.dialogs:
                self.dialogs.pop()

    fp = FakePage()
    open_session_history_dialog(
        fp,  # type: ignore[arg-type]
        "Monica",
        [],
        ip_history=hist,
        initial_tab="ips",
    )
    assert len(fp.dialogs) == 1


def test_mail_credentials_storage_bulk_import_and_dialog(isolated_pm, tmp_path):
    """Test parsing mail|pass|2fa|recovery, update_profile_credentials, bulk_import_credentials, and dialog."""
    import flet as ft

    from src.ui.components.profile_card import build_profile_card
    from src.ui.dialogs import open_mail_credentials_dialog
    from src.utils.totp import (
        extract_otp_from_text,
        format_mail_credentials_line,
        parse_mail_credentials_line,
        split_mail_token,
    )

    assert extract_otp_from_text("Your verification code is 849201", "Hello") == "849201"
    assert extract_otp_from_text("Mã xác nhận tài khoản", "Mã OTP của bạn là: 583920") == "583920"
    assert (
        extract_otp_from_text("New app(s) connected to your Microsoft account", "2026-10-02") == ""
    )
    rt_part, cid_part = split_mail_token("M.C540_BAY_TOKEN|9e5f94bc-e8a4-4e73-b8be-63364c29d753")
    assert rt_part == "M.C540_BAY_TOKEN"
    assert cid_part == "9e5f94bc-e8a4-4e73-b8be-63364c29d753"

    # 1. Parse and format single-line mail credentials
    parsed1 = parse_mail_credentials_line(
        "user1@gmail.com|MyPass#1|JBSWY3DPEHPK3PXP|rec1@outlook.com"
    )
    assert (
        parsed1["email"],
        parsed1["password"],
        parsed1["two_fa"],
        parsed1["recovery_email"],
    ) == (
        "user1@gmail.com",
        "MyPass#1",
        "JBSWY3DPEHPK3PXP",
        "rec1@outlook.com",
    )
    assert (
        format_mail_credentials_line(
            parsed1["email"],
            parsed1["password"],
            parsed1["two_fa"],
            parsed1["recovery_email"],
        )
        == "user1@gmail.com|MyPass#1|JBSWY3DPEHPK3PXP|rec1@outlook.com"
    )

    # Smart detection when 3rd field is recovery email instead of 2FA
    parsed2 = parse_mail_credentials_line("user2@gmail.com|Pass2|backup@yahoo.com")
    assert (
        parsed2["email"],
        parsed2["password"],
        parsed2["two_fa"],
        parsed2["recovery_email"],
    ) == (
        "user2@gmail.com",
        "Pass2",
        "",
        "backup@yahoo.com",
    )

    # 1b. Parse 5-part Hotmail OAuth2 line: email|pass|refresh_token|client_id|recovery_email
    sample_hotmail = (
        "dpicjfq@hotmail.com|Juslti5%|"
        "M.C540_BAY.0.U.MsaArtifacts.-Cm9lgCyy*2tAKfgnqTimM9vTUG6AFyb7d*6rB45*iejOYLM34PhJ76WeCLKKeICZdfpDeSS7MHPj5S!MVAwFDqp0h*KTOPTdbAjZba9aRrOt*CKdK2wzpzPO2lFxVcqtNP90knZgoCFKfStWilr1P8Eevu3uwcPn28d3PLfi2xdf*3u5LGHxLEy973R9UzEvlSiDblLIEdyQCelRCpex29pmDTMqWfZ5wNvXWAeSND53ic!*WcTMapBhx9ETo!Q68jJpfDZoPmsK4jnGyiL*91f7sM!v76Xru1QHw*ZiED4*zvCm7uF7IKTT0FGoQ21e2YmHxu5xXF4nAznmbQPYLBAfu8xtqDmqW0Nj0l1JitRT0Dbdp7q0yzW1L54qfmt4fA$$|"
        "9e5f94bc-e8a4-4e73-b8be-63364c29d753|bbf209@smvmail.com"
    )
    parsed_hm = parse_mail_credentials_line(sample_hotmail)
    assert parsed_hm["email"] == "dpicjfq@hotmail.com"
    assert parsed_hm["password"] == "Juslti5%"
    assert parsed_hm["recovery_email"] == "bbf209@smvmail.com"
    assert parsed_hm["two_fa"] == ""
    assert parsed_hm["client_id"] == "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    assert parsed_hm["refresh_token"].startswith("M.C540_BAY.0.U.MsaArtifacts.")
    assert parsed_hm["mail_token"].endswith("|9e5f94bc-e8a4-4e73-b8be-63364c29d753")
    assert parsed_hm["notes"] == ""
    assert (
        format_mail_credentials_line(
            parsed_hm["email"],
            parsed_hm["password"],
            parsed_hm["two_fa"],
            parsed_hm["recovery_email"],
            parsed_hm["mail_token"],
        )
        == sample_hotmail
    )

    # 2. Single profile credentials update & persistence
    isolated_pm.add_profile("MailAcc1", "http://1.2.3.4:10783", "windows", email="user1@gmail.com")
    isolated_pm.add_profile("MailAcc2", "http://1.2.3.4:10784", "windows", email="user2@gmail.com")

    assert isolated_pm.update_profile_credentials(
        "MailAcc1",
        email="user1@gmail.com",
        password="MyPass#1",
        two_fa="JBSWY3DPEHPK3PXP",
        recovery_email="rec1@outlook.com",
        notes="VIP Gmail",
    )
    p1 = isolated_pm.profiles["MailAcc1"]
    assert p1.email == "user1@gmail.com"
    assert p1.password == "MyPass#1"
    assert p1.two_fa == "JBSWY3DPEHPK3PXP"
    assert p1.recovery_email == "rec1@outlook.com"
    assert p1.notes == "VIP Gmail"

    # 3. Bulk import credentials matching by email or profile name
    bulk_text = "\n".join(
        [
            "user2@gmail.com|PassTwo!|JBSWY3DPEHPK3PXP|rec2@hotmail.com",
            "MailAcc1|user1@gmail.com|NewPass#1|JBSWY3DPEHPK3PXP|rec1_new@outlook.com",
            "unknown@gmail.com|Nope|JBSWY3DPEHPK3PXP",
        ]
    )
    updated, skipped = isolated_pm.bulk_import_credentials(bulk_text)
    assert (updated, skipped) == (2, 1)
    assert isolated_pm.profiles["MailAcc2"].password == "PassTwo!"
    assert isolated_pm.profiles["MailAcc2"].recovery_email == "rec2@hotmail.com"
    assert isolated_pm.profiles["MailAcc1"].password == "NewPass#1"
    assert isolated_pm.profiles["MailAcc1"].recovery_email == "rec1_new@outlook.com"

    # 4. CSV export/import preserves recovery_email
    csv_file = tmp_path / "profiles_mail.csv"
    assert isolated_pm.export_profiles_csv(str(csv_file), names=["MailAcc1"]) == 1
    isolated_pm.update_profile_credentials("MailAcc1", recovery_email="")
    assert isolated_pm.profiles["MailAcc1"].recovery_email is None
    c_cnt, u_cnt, s_cnt = isolated_pm.import_profiles_csv(str(csv_file))
    assert (c_cnt, u_cnt, s_cnt) == (0, 1, 0)
    assert isolated_pm.profiles["MailAcc1"].recovery_email == "rec1_new@outlook.com"

    # 5. Profile card & open_mail_credentials_dialog
    card = build_profile_card(
        isolated_pm.profiles["MailAcc1"],
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        on_copy_email=lambda _: None,
        on_copy_2fa=lambda _: None,
        on_copy_password=lambda _: None,
        on_copy_recovery=lambda _: None,
        on_open_mail_credentials=lambda _: None,
    )
    assert card is not None

    class FakePage:
        def __init__(self):
            self.dialogs: list[ft.AlertDialog] = []

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialogs.append(dlg)

        def pop_dialog(self):
            if self.dialogs:
                self.dialogs.pop()

    fp = FakePage()
    open_mail_credentials_dialog(
        fp,  # type: ignore[arg-type]
        isolated_pm.profiles["MailAcc1"],
        lambda em, pw, tfa, rec, nt, tok=None: True,
        on_bulk_import=isolated_pm.bulk_import_credentials,
        on_copy_text=lambda val, msg: None,
        initial_tab="inbox",
        preloaded_messages=[
            {
                "subject": "Your verification code is 849201",
                "sender": "security@facebookmail.com",
                "time": "2026-10-23 23:00",
                "preview": "Use code 849201 to verify your login.",
                "otp": "849201",
            }
        ],
    )
    assert len(fp.dialogs) == 1


def test_warmup_sites_vault_and_dialog(tmp_path):
    """Test warmup sites parser, random picker, persistence, and UI dialog."""
    import flet as ft

    from src.ui.dialogs import open_warmup_sites_dialog
    from src.utils.warmup_sites import (
        DEFAULT_WARMUP_TEXT,
        load_warmup_text,
        parse_warmup_groups,
        pick_random_warmup_site,
        save_warmup_text,
    )

    groups = parse_warmup_groups(DEFAULT_WARMUP_TEXT)
    assert len(groups) == 3
    assert groups[0]["title"] == "Web vào Meta không bị chặn"
    assert len(groups[0]["items"]) == 1  # type: ignore[arg-type]
    assert len(groups[1]["items"]) >= 4  # type: ignore[arg-type]
    assert len(groups[2]["items"]) >= 3  # type: ignore[arg-type]

    picked = pick_random_warmup_site(DEFAULT_WARMUP_TEXT)
    assert picked is not None
    assert picked["url"].startswith("https://")

    custom_file = tmp_path / "warmup_sites.json"
    assert load_warmup_text(custom_file) == DEFAULT_WARMUP_TEXT
    assert save_warmup_text("Nhóm Test\nhttps://example.com — Trang test", custom_file)
    assert "https://example.com" in load_warmup_text(custom_file)

    class FakePage:
        def __init__(self):
            self.dialogs: list[ft.AlertDialog] = []

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialogs.append(dlg)

        def pop_dialog(self):
            if self.dialogs:
                self.dialogs.pop()

    fp = FakePage()
    open_warmup_sites_dialog(fp, on_copy_text=lambda u, m: None)  # type: ignore[arg-type]
    assert len(fp.dialogs) == 1


def test_mcp_and_api_anti_detect_ops_endpoints(isolated_pm, tmp_path, monkeypatch):
    """Test upgraded MCP tools & API endpoints: Pre-Launch Safety Guard, Monitor Overview, TOTP, Pool, Bulk Ops."""
    from fastapi.testclient import TestClient

    import src.api.auth as auth_mod
    from src.api.app import create_app
    from src.core.container import Container
    from src.core.events import EventBus
    from src.mcp_server.server import mcp
    from src.services.monitor import MonitorService

    monkeypatch.setattr(auth_mod, "API_TOKEN", "")

    isolated_pm.add_profile(
        "ReadyAcc",
        "http://u:p@1.2.3.4:10001",
        "windows",
        timezone="America/Chicago",
        locale="en-US",
        status="ready",
        pages=["Live"],
        two_fa="JBSWY3DPEHPK3PXP",
        email="ready@hotmail.com",
    )
    isolated_pm.add_profile(
        "SamePortAcc",
        "http://u:p@1.2.3.4:10001",
        "windows",
        timezone="America/Chicago",
        locale="en-US",
        status="ready",
        pages=["Live"],
    )
    isolated_pm.add_profile(
        "NoProxyAcc",
        "",
        "windows",
        pages=["Live"],
    )

    class FakeBL:
        def __init__(self):
            self.running: set[str] = set()

        def is_running(self, name: str) -> bool:
            return name in self.running

        def running_profile_names(self) -> set[str]:
            return set(self.running)

        def running_count(self) -> int:
            return len(self.running)

        def start_thread(self, profile, log_callback, on_start=None, on_ready=None, on_stop=None):
            self.running.add(profile.name)
            if on_ready:
                on_ready()
            return True

        def wait_for_launch(self, name: str, timeout: float = 30.0):
            return True, None

        def stop_profile(self, name: str, timeout: float = 2.0):
            self.running.discard(name)
            return True

        def arrange_windows(self, names=None) -> int:
            return len(self.running)

        def focus_profile(self, name: str) -> bool:
            return name in self.running

    class FakePS:
        def check_proxy_sync(self, proxy_str: str, timeout: int = 10):
            return True, "IP: 8.8.8.8 (US)"

    c = Container()
    c._instances["pm"] = isolated_pm
    c._instances["bl"] = FakeBL()
    c._instances["ps"] = FakePS()
    c._instances["eb"] = EventBus()
    c._instances["mon"] = MonitorService(
        data_dir=str(tmp_path / "camoufox_data"),
        log_dir=str(tmp_path / "logs"),
    )

    app = create_app(c)
    client = TestClient(app, base_url="http://127.0.0.1:8000")

    # 1. Verify all new MCP tools are registered
    tool_names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {
        "get_monitor_overview",
        "check_prelaunch_safety",
        "get_profile_2fa",
        "fetch_hotmail_otp",
        "get_profile_history",
        "list_warmup_sites",
        "list_proxy_pool",
        "add_proxies_to_pool",
        "assign_proxy_from_pool",
        "bulk_update_profiles",
        "arrange_browser_windows",
        "focus_browser_window",
    } <= tool_names

    # 2. Pre-launch check & Safety Guard on launch
    r_check_noproxy = client.get("/api/v1/browser/NoProxyAcc/prelaunch-check")
    assert r_check_noproxy.status_code == 200
    assert r_check_noproxy.json()["safe"] is False

    # Launching NoProxyAcc is blocked by default (409)
    r_launch_blocked = client.post("/api/v1/browser/NoProxyAcc/launch")
    assert r_launch_blocked.status_code == 409
    assert "no_proxy" in r_launch_blocked.json()["detail"]

    # Launching ReadyAcc succeeds (200)
    r_launch_ok = client.post("/api/v1/browser/ReadyAcc/launch")
    assert r_launch_ok.status_code == 200
    assert r_launch_ok.json()["success"] is True

    # Launching SamePortAcc while ReadyAcc is running on :10001 is blocked (409 port_busy)
    r_collision = client.post("/api/v1/browser/SamePortAcc/launch")
    assert r_collision.status_code == 409
    assert "port_busy" in r_collision.json()["detail"]

    # Arrange & focus running windows
    assert client.post("/api/v1/browser/arrange").json()["arranged"] == 1
    assert client.post("/api/v1/browser/ReadyAcc/focus").json()["success"] is True

    # Stop ReadyAcc -> SamePortAcc now blocked by 15m port_cooldown unless force=true
    assert client.post("/api/v1/browser/ReadyAcc/stop").status_code == 200
    r_cooldown = client.post("/api/v1/browser/SamePortAcc/launch")
    assert r_cooldown.status_code == 409
    assert "port_cooldown" in r_cooldown.json()["detail"]

    r_force = client.post("/api/v1/browser/SamePortAcc/launch?force=true")
    assert r_force.status_code == 200
    client.post("/api/v1/browser/SamePortAcc/stop")

    # 3. Monitor overview, TOTP, Mail OTP, History, Warmup sites
    ov = client.get("/api/v1/profiles/monitor/overview?page=Live").json()
    assert ov["total_matched"] == 3

    totp_res = client.get("/api/v1/profiles/ReadyAcc/totp").json()
    assert totp_res["has_2fa"] is True
    assert len(totp_res["code"]) == 6

    mail_res = client.post("/api/v1/profiles/ReadyAcc/mail-otp").json()
    assert mail_res["success"] is False  # No refresh token configured yet

    hist_res = client.get("/api/v1/profiles/ReadyAcc/history").json()
    assert len(hist_res["sessions"]) >= 1

    warmup_res = client.get("/api/v1/profiles/warmup-sites").json()
    assert warmup_res["total_groups"] >= 1

    # 4. Proxy Pool & Bulk Ops
    pool_add = client.post(
        "/api/v1/proxy/pool",
        json={"proxies": ["1.2.3.4:20001:u:p", "1.2.3.4:20002:u:p"]},
    ).json()
    assert pool_add["added"] == 2
    assert client.get("/api/v1/proxy/pool").json()["unassigned_count"] == 2

    assign_res = client.post(
        "/api/v1/proxy/pool/assign",
        json={"profile": "NoProxyAcc", "new_proxy": "http://u:p@1.2.3.4:20001"},
    ).json()
    assert assign_res["success"] is True
    assert isolated_pm.profiles["NoProxyAcc"].proxy == "http://u:p@1.2.3.4:20001"

    bulk_res = client.post(
        "/api/v1/profiles/bulk-ops",
        json={
            "names": ["NoProxyAcc"],
            "status": "hold",
            "group": "TeamHold",
            "pinned": True,
            "add_to_page": "Disconnected",
        },
    ).json()
    assert bulk_res["success"] is True
    assert isolated_pm.profiles["NoProxyAcc"].status == "hold"
    assert isolated_pm.profiles["NoProxyAcc"].group == "TeamHold"
    assert isolated_pm.profiles["NoProxyAcc"].pinned is True
    assert "Disconnected" in isolated_pm.profiles["NoProxyAcc"].pages


def test_lookup_ip_risk_multi_source_scoring(monkeypatch):
    """Test lookup_ip_risk combining Scamalytics, ProxyCheck.io, and ip-api.com."""
    import requests

    import src.services.proxy.geo_check as gc

    gc._IP_RISK_MEMO.clear()
    gc._ISP_MEMO.clear()

    class _FakeResp:
        def __init__(self, status_code: int, text: str = "", json_data=None):
            self.status_code = status_code
            self.text = text
            self._json = json_data or {}

        def json(self):
            return self._json

    def _fake_get(url: str, *args, **kwargs):
        if "scamalytics.com" in url:
            return _FakeResp(200, text="<div>Fraud Score: 12</div>")
        if "proxycheck.io" in url:
            return _FakeResp(
                200,
                json_data={
                    "status": "ok",
                    "24.6.173.1": {
                        "proxy": "no",
                        "type": "Business",
                        "risk": 0,
                        "asn": "AS7922",
                        "provider": "Comcast Cable",
                    },
                },
            )
        return _FakeResp(
            200,
            json_data={
                "status": "success",
                "isp": "Comcast Cable Communications",
                "org": "Comcast",
                "as": "AS7922 Comcast",
                "mobile": False,
                "proxy": False,
                "hosting": False,
            },
        )

    monkeypatch.setattr(requests, "get", _fake_get)

    res = gc.lookup_ip_risk("24.6.173.1")
    assert res["ip"] == "24.6.173.1"
    assert res["scamalytics"] == 12
    assert res["proxycheck_risk"] == 0
    assert res["fraud_score"] == 12
    assert res["proxy_flag"] is False
    assert res["ip_type"] == "ISP"
    assert res["asn"] == "AS7922"
    assert "Comcast" in res["isp"]


def test_monitor_trust_score_and_anti_correlation_auditor(tmp_path):
    """Test Account Warmup Trust Score, IP Risk persistence, and Cross-Profile Correlation Auditor."""
    import sqlite3

    from src.models.profile import Profile
    from src.services.monitor import MonitorService
    from src.ui.components.profile_card import _geo_score_badge
    from src.ui.dialogs.batch_and_backup import (
        _build_ip_score_chip,
        _build_trust_score_chip,
    )

    data_dir = tmp_path / "camoufox_data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir()
    log_dir.mkdir()

    # Create DiamondAcc with rich cookies, _GRECAPTCHA, cf_clearance (hatch_gw), places.sqlite, cache2
    p1_dir = data_dir / "DiamondAcc"
    p1_dir.mkdir()
    (p1_dir / "cache2").mkdir()
    (p1_dir / "tabs.json").write_text(
        json.dumps(["https://app.hatch.canvas/thread/123"]),
        encoding="utf-8",
    )
    fp_payload = {
        "os": "windows",
        "fingerprint": {
            "navigator": {"platform": "Win32", "hardwareConcurrency": 8},
            "screen": {"width": 1920, "height": 1080},
            "videoCard": {"renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630)"},
        },
    }
    (p1_dir / "fingerprint.json").write_text(json.dumps(fp_payload), encoding="utf-8")

    conn = sqlite3.connect(str(p1_dir / "cookies.sqlite"))
    conn.execute(
        "CREATE TABLE moz_cookies (host TEXT, name TEXT, creationTime INTEGER, expiry INTEGER)"
    )
    now_exp = int(time.time()) + 86400 * 30
    aged_ctime = int((time.time() - 86400 * 12) * 1_000_000)
    rows = [
        (".google.com", "_GRECAPTCHA", aged_ctime, now_exp),
        ("muse.ai", "hatch_gw", aged_ctime, now_exp),
        ("muse.ai", "hatch_vml", aged_ctime, now_exp),
        (".muse.ai", "hatch_sess", aged_ctime, now_exp),
        (".facebook.com", "datr", aged_ctime, now_exp),
        (".auth.meta.com", "datr", aged_ctime, now_exp),
        (".accountscenter.meta.com", "datr", aged_ctime, now_exp),
        (".facebook.com", "ps_l", aged_ctime, now_exp),
        (".live.com", "MSPAuth", aged_ctime, now_exp),
    ]
    for i in range(60):
        rows.append((f".site{i % 15}.com", f"ck_{i}", aged_ctime, now_exp))
    conn.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()

    pconn = sqlite3.connect(str(p1_dir / "places.sqlite"))
    pconn.execute("CREATE TABLE moz_places (url TEXT, rev_host TEXT, visit_count INTEGER)")
    prows = [
        (f"https://domain{i % 16}.com/page{i}", f"moc.{i % 16}niamod.", 2)
        for i in range(55)
    ]
    pconn.executemany("INSERT INTO moz_places VALUES (?, ?, ?)", prows)
    pconn.commit()
    pconn.close()

    # Create CloneAcc with the exact same fingerprint.json and same email/2FA/token + sharing port with BannedAcc
    p2_dir = data_dir / "CloneAcc"
    p2_dir.mkdir()
    (p2_dir / "fingerprint.json").write_text(json.dumps(fp_payload), encoding="utf-8")

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    mon.record_exit_ip(
        "DiamondAcc",
        "2600:1700:ef01:9999::111",
        country="US",
        proxy="http://u:p@1.2.3.4:10001",
        isp="AT&T Services",
        ip_type="ISP",
    )
    mon.record_ip_risk(
        "2600:1700:ef01:9999::111",
        {
            "fraud_score": 0,
            "scamalytics": 0,
            "proxycheck_risk": 0,
            "proxy_flag": False,
            "asn": "AS7018",
            "isp": "AT&T Services",
            "ip_type": "ISP",
        },
    )
    # Record a burned IPv6 in same /64 subnet on BannedAcc (so DiamondAcc triggers burned_subnet)
    # and same port :10002 as CloneAcc (so CloneAcc triggers burned_proxy)
    mon.record_exit_ip(
        "BannedAcc",
        "2600:1700:ef01:9999::abc",
        country="US",
        proxy="http://u:p@1.2.3.4:10002",
    )
    mon.record_exit_ip(
        "CloneAcc",
        "2600:1700:abcd:2222::def",
        country="US",
        proxy="http://u:p@1.2.3.4:10002",
    )
    mon.record_ip_risk(
        "2600:1700:abcd:2222::def",
        {
            "fraud_score": 82,
            "scamalytics": 82,
            "proxycheck_risk": 75,
            "proxy_flag": True,
            "asn": "AS9009",
        },
    )

    long_rt = "M.C501_BAY.0.U.-CiB1234567890abcdefghijklmnop"
    profiles = [
        Profile(
            name="DiamondAcc",
            proxy="http://u:p@1.2.3.4:10001",
            timezone="America/Chicago",
            locale="en-US",
            created_at="01/01/2025",
            email="shared@hotmail.com",
            two_fa="JBSWY3DPEHPK3PXP",
            mail_token=f"{long_rt}|9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        ),
        Profile(
            name="CloneAcc",
            proxy="http://u:p@1.2.3.4:10002",
            timezone="America/Chicago",
            locale="en-US",
            email="shared@hotmail.com",
            two_fa="JBSWY3DPEHPK3PXP",
            mail_token=f"{long_rt}|9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        ),
        Profile(
            name="BannedAcc",
            proxy="http://u:p@1.2.3.4:10002",
            status="ban",
            banned_at="01/02/2026",
        ),
    ]

    snap = mon.snapshot(profiles, running_names=set())
    d_info = snap.profiles["DiamondAcc"]
    c_info = snap.profiles["CloneAcc"]
    b_info = snap.profiles["BannedAcc"]

    assert d_info.has_grecaptcha is True
    assert d_info.hatch_gw is True
    assert d_info.has_cache2 is True
    assert d_info.cookie_count >= 60
    assert d_info.history_pages >= 50
    assert d_info.ip_fraud_score == 0
    assert d_info.trust_score >= 85
    assert d_info.trust_tier == "diamond"
    assert snap.diamond_count == 1
    assert snap.clean_ip_count == 1

    assert b_info.is_banned is True
    assert b_info.trust_tier == "banned"

    finding_codes = {f.code for f in snap.audit_findings}
    assert "burned_proxy" in finding_codes
    assert "burned_subnet" in finding_codes
    assert "duplicate_email" in finding_codes
    assert "duplicate_2fa" in finding_codes
    assert "duplicate_token" in finding_codes
    assert "duplicate_fingerprint" in finding_codes
    assert "high_fraud_ip" in finding_codes
    assert "burned_proxy" in c_info.audit_codes

    # Verify UI chip builders execute cleanly without error
    assert _build_ip_score_chip(0, scamalytics=0, proxycheck_risk=0, proxy_flag=False) is not None
    assert _build_ip_score_chip(82, scamalytics=82, proxycheck_risk=75, proxy_flag=True) is not None
    assert _build_trust_score_chip(d_info) is not None
    assert _geo_score_badge(d_info) is not None


def test_trust_score_consistency_root_domains_and_tier_gates(tmp_path):
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir()
    log_dir.mkdir()

    now_ts = time.time()
    now_exp = int(now_ts + 86400 * 30)
    datr_10d = int((now_ts - 86400 * 10.2) * 1_000_000)

    # 1. Unopened profile (has proxy + geo, but 0 cookies / 0 sessions / 0 history) -> 0 score, no geo badge
    (data_dir / "Unopened").mkdir()

    # 2. Workspace profile with __Host-hatch_one_tap + layout_state + /thread/ + duplicate www subdomains + scanners
    ws_dir = data_dir / "WorkspaceAcc"
    ws_dir.mkdir()
    (ws_dir / "cache2").mkdir()
    (ws_dir / "tabs.json").write_text('{"tabs": ["https://muse.ai/thread/123"]}', encoding="utf-8")
    ck_conn = sqlite3.connect(str(ws_dir / "cookies.sqlite"))
    ck_conn.execute(
        "CREATE TABLE moz_cookies (host TEXT, name TEXT, creationTime INTEGER, expiry INTEGER)"
    )
    ck_conn.executemany(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
        [
            ("muse.ai", "__Host-hatch_one_tap", datr_10d, now_exp),
            (".muse.ai", "layout_state", datr_10d, now_exp),
            (".muse.ai", "hatch_sess", datr_10d, now_exp),
            (".muse.ai", "datr", datr_10d, now_exp),
            (".auth.meta.com", "datr", datr_10d, now_exp),
            (".accountscenter.meta.com", "datr", datr_10d, now_exp),
            (".facebook.com", "datr", datr_10d, now_exp),
            (".facebook.com", "ps_l", datr_10d, now_exp),
        ],
    )
    ck_conn.commit()
    ck_conn.close()

    pl_conn = sqlite3.connect(str(ws_dir / "places.sqlite"))
    pl_conn.execute("CREATE TABLE moz_places (url TEXT, rev_host TEXT, visit_count INTEGER)")
    # amazon.com + www.amazon.com should deduplicate to 1 root domain; browserscan & mozilla should be ignored
    pl_conn.executemany(
        "INSERT INTO moz_places VALUES (?, ?, ?)",
        [
            ("https://amazon.com/", "moc.nozama.", 1),
            ("https://www.amazon.com/dp/1", "moc.nozama.www.", 2),
            ("https://www.cnet.com/news", "moc.tenc.www.", 1),
            ("https://www.cntraveler.com/", "moc.relevartnc.www.", 1),
            ("https://www.self.com/", "moc.fles.www.", 1),
            ("https://www.browserscan.net/", "ten.nacsresworb.www.", 3),
            ("https://addons.mozilla.org/", "gro.allizom.snodda.", 1),
        ],
    )
    pl_conn.commit()
    pl_conn.close()

    # 3. Third-party Cloudflare cookie (__cf_bm on .jezebel.com) must NOT count as hatch_gw
    cf_dir = data_dir / "ThirdPartyCF"
    cf_dir.mkdir()
    cf_conn = sqlite3.connect(str(cf_dir / "cookies.sqlite"))
    cf_conn.execute(
        "CREATE TABLE moz_cookies (host TEXT, name TEXT, creationTime INTEGER, expiry INTEGER)"
    )
    cf_conn.executemany(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
        [
            (".jezebel.com", "__cf_bm", datr_10d, now_exp),
            (".muse.ai", "hatch_sess", datr_10d, now_exp),
        ],
    )
    cf_conn.commit()
    cf_conn.close()

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    # Seed 6 active days, 10 sessions, 300 total minutes for WorkspaceAcc
    mon._live_sessions["WorkspaceAcc"] = [
        {
            "start": f"2026-10-{10 + (idx % 6):02d} 10:{idx:02d}:00",
            "end": f"2026-10-{10 + (idx % 6):02d} 10:{idx:02d}:00",
            "duration_mins": 30,
            "port": ":10001",
            "is_live": False,
        }
        for idx in range(10)
    ]
    mon.record_exit_ip("WorkspaceAcc", "1.2.3.4", country="US", proxy="http://u:p@1.2.3.4:10001")
    mon.record_ip_risk("1.2.3.4", {"fraud_score": 51, "proxy_flag": True})

    profiles = [
        Profile(
            name="Unopened",
            proxy="http://u:p@1.2.3.4:10001",
            timezone="America/Chicago",
            locale="en-US",
        ),
        Profile(
            name="WorkspaceAcc",
            proxy="http://u:p@1.2.3.4:10001",
            timezone="America/Chicago",
            locale="en-US",
        ),
        Profile(
            name="ThirdPartyCF",
            proxy="http://u:p@1.2.3.4:10002",
            timezone="America/Chicago",
            locale="en-US",
        ),
    ]

    snap = mon.snapshot(profiles, running_names=set())
    u_info = snap.profiles["Unopened"]
    w_info = snap.profiles["WorkspaceAcc"]
    cf_info = snap.profiles["ThirdPartyCF"]

    from src.ui.components.profile_card import _geo_score_badge

    assert u_info.trust_score == 0
    assert u_info.trust_tier == "cold"
    assert _geo_score_badge(u_info) is None

    assert w_info.hatch_gw is True
    assert w_info.ext_domains == 4
    assert w_info.active_days == 6
    assert w_info.session_count == 10
    assert w_info.total_minutes == 300
    assert w_info.trust_score >= 80
    assert w_info.trust_tier == "diamond"

    assert cf_info.hatch_gw is False
    assert cf_info.trust_score <= 45




