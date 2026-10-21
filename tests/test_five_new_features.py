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
