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
    assert len(page.dialogs) == 5


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
            self.window = type("W", (), {"width": 1200, "height": 800, "min_width": 800, "min_height": 600})()
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
    app.monitor = MonitorService(data_dir=str(tmp_path / "camoufox_data"), log_dir=str(tmp_path / "logs"))
    app.h._monitor = app.monitor
    fake_page = FakePage()
    app.page = fake_page  # type: ignore[assignment]
    app._file_picker = ft.FilePicker()
    app.clipboard = ft.Clipboard()
    fake_page.services.extend([app._file_picker, app.clipboard])
    app._build_ui()

    # Exercise all sort modes
    for s_mode in ("custom", "cooldown_desc", "cooldown_asc", "name_asc", "name_desc", "group", "running", "proxy"):
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

