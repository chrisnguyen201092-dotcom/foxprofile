"""Comprehensive verification tests for the 4 Evolution Pillars (#1, #2, #3, #4)."""

from __future__ import annotations

import pathlib
from typing import Any

import flet as ft
import pytest
from fastapi.testclient import TestClient

from src.services.profile import manager as pm_mod


@pytest.fixture
def isolated_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "camoufox_data"
    log_dir = tmp_path / "logs"
    profiles_file = tmp_path / "profiles.json"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(profiles_file))
    return pm_mod.ProfileManager(), data_dir, log_dir, profiles_file


class _DummyPage:
    def __init__(self) -> None:
        self.dialogs: list[ft.AlertDialog] = []

    def show_dialog(self, dlg: ft.AlertDialog) -> None:
        self.dialogs.append(dlg)

    def pop_dialog(self) -> None:
        if self.dialogs:
            self.dialogs.pop()

    def update(self) -> None:
        pass

    def run_task(self, coro_fn, *args, **kwargs) -> None:  # pragma: no cover - test stub
        pass


class _DummyBL:
    def __init__(self) -> None:
        self.running: set[str] = set()
        self.launched: list[str] = []
        self.control_calls: list[tuple[str, str, dict[str, Any]]] = []

    def is_running(self, name: str) -> bool:
        return name in self.running

    def running_profile_names(self) -> set[str]:
        return set(self.running)

    def start_thread(self, profile, log, on_ready=None, on_stop=None) -> bool:
        if profile.name in self.running:
            return False
        self.running.add(profile.name)
        self.launched.append(profile.name)
        if on_ready:
            on_ready()
        return True

    def stop_profile(self, name: str) -> bool:
        self.running.discard(name)
        return True

    def wait_for_launch(self, name: str, timeout: float = 30.0) -> tuple[bool, str]:
        return True, ""

    def control(self, name: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.control_calls.append((name, action, dict(params)))
        return {"ok": True, "url": params.get("url", "https://muse.ai/")}


class _DummyPS:
    def check_proxy_sync(self, proxy_str: str, timeout: int = 10) -> tuple[bool, str]:
        return True, "IP: 198.51.100.77 (US)"


def _make_test_client(pm, bl, data_dir: pathlib.Path, log_dir: pathlib.Path):
    from src.api.app import create_app
    from src.core.container import Container
    from src.core.events import EventBus
    from src.services.monitor import MonitorService

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    c = Container()
    c._instances["pm"] = pm
    c._instances["bl"] = bl
    c._instances["ps"] = _DummyPS()
    c._instances["eb"] = EventBus()
    c._instances["mon"] = mon
    client = TestClient(create_app(c), base_url="http://127.0.0.1:8000")
    return client, mon


def test_part1_prelaunch_blocks_checkpoint_and_banned_and_overview_ready(isolated_env) -> None:
    pm, data_dir, log_dir, _ = isolated_env
    bl = _DummyBL()
    client, mon = _make_test_client(pm, bl, data_dir, log_dir)

    pm.add_profile(
        "CPAcc",
        "http://u:p@198.51.100.10:5001",
        "windows",
        timezone="America/New_York",
        locale="en-US",
        pages=["Live"],
    )
    pm.add_profile(
        "BannedAcc",
        "http://u:p@198.51.100.11:5002",
        "windows",
        timezone="America/New_York",
        locale="en-US",
        pages=["Disconnected"],
        status="check",
    )
    pm.add_profile(
        "CleanAcc",
        "http://u:p@198.51.100.12:5003",
        "windows",
        timezone="America/New_York",
        locale="en-US",
        pages=["Live"],
    )
    mon.record_mail_checkpoint_hit(
        "CPAcc",
        {"email": "cp@mail.example", "subject": "Action needed on your Meta Account"},
    )

    snap = mon.snapshot(pm.list_profiles(), set())
    assert snap.profiles["CPAcc"].has_warning is True
    assert snap.profiles["BannedAcc"].has_warning is True
    assert snap.profiles["CleanAcc"].has_warning is False

    w_cp = mon.check_prelaunch(pm.profiles["CPAcc"], pm.list_profiles(), set(), snap=snap)
    assert any(w.code == "meta_checkpoint_banned" and w.level == "critical" for w in w_cp)

    w_ban = mon.check_prelaunch(pm.profiles["BannedAcc"], pm.list_profiles(), set(), snap=snap)
    assert any(w.code == "profile_banned" and w.level == "critical" for w in w_ban)

    # API overview?only_ready=true must exclude CPAcc and BannedAcc
    ov_ready = client.get("/api/v1/profiles/monitor/overview?only_ready=true").json()
    ready_names = [it["name"] for it in ov_ready["profiles"]]
    assert ready_names == ["CleanAcc"]

    # Launching CPAcc or BannedAcc over REST must return 409
    assert client.post("/api/v1/browser/CPAcc/launch").status_code == 409
    assert client.post("/api/v1/browser/BannedAcc/launch").status_code == 409


def test_part1_page_navigation_blocks_accountscenter_only(isolated_env) -> None:
    pm, data_dir, log_dir, _ = isolated_env
    bl = _DummyBL()
    client, _ = _make_test_client(pm, bl, data_dir, log_dir)
    pm.add_profile("NavAcc", "http://u:p@198.51.100.13:5004", "windows")
    bl.running.add("NavAcc")

    for bad_url in (
        "https://accountscenter.meta.com/personal_info/contact_points/",
        "HTTPS://ACCOUNTSCENTER.META.COM:443/",
        "https://m.accountscenter.meta.com/",
    ):
        r_nav = client.post("/api/v1/browser/NavAcc/page/navigate", json={"url": bad_url})
        assert r_nav.status_code == 403
        assert "accountscenter.meta.com" in r_nav.json()["detail"]
        r_tab = client.post("/api/v1/browser/NavAcc/page/tabs", json={"url": bad_url})
        assert r_tab.status_code == 403

    assert len(bl.control_calls) == 0

    r_ok = client.post(
        "/api/v1/browser/NavAcc/page/navigate",
        json={"url": "https://muse.ai/"},
    )
    assert r_ok.status_code == 200
    assert len(bl.control_calls) == 1


def test_part1_proxy_assign_enforces_freeze_asn_and_session_lock(isolated_env) -> None:
    pm, data_dir, log_dir, _ = isolated_env
    bl = _DummyBL()
    client, mon = _make_test_client(pm, bl, data_dir, log_dir)

    frozen_px = "http://u:p@198.51.100.20:5101"
    clean_usa_px = "http://u:p@sp02-usa101.proxy.example:10101"
    alt_px = "http://u:p@198.51.100.21:5102"

    pm.add_profile("DeadOwner", frozen_px, "windows", pages=["Disconnected"])
    pm.update_profile_dates("DeadOwner", None, "2026-11-16")
    pm.add_profile("TargetLive", "http://u:p@198.51.100.22:5103", "windows", pages=["Live"])
    pm.add_profile("RiskWatch", "", "windows", group="4. High Risk", pages=["Theo dõi"])
    pm.add_profile("SessionAcc", "http://u:p@198.51.100.23:5104", "windows", pages=["Live"])

    mon.record_started("DeadOwner", frozen_px)
    mon.record_stopped("DeadOwner")
    mon.record_started("SessionAcc", "http://u:p@198.51.100.23:5104")
    mon.record_stopped("SessionAcc")
    # Seed a hatch_sess flag inside _disk_cache for SessionAcc
    mon._disk_cache["SessionAcc"] = {"hatch_sess": True, "hatch_gw": True, "_is_running": False}

    r_frz = client.post(
        "/api/v1/proxy/pool/assign",
        json={"profile": "TargetLive", "new_proxy": frozen_px},
    )
    assert r_frz.status_code == 409
    assert "Freeze" in r_frz.json()["detail"]

    r_asn = client.post(
        "/api/v1/proxy/pool/assign",
        json={"profile": "RiskWatch", "new_proxy": clean_usa_px},
    )
    assert r_asn.status_code == 409
    assert "ASN Firewall" in r_asn.json()["detail"]

    r_sess = client.post(
        "/api/v1/proxy/pool/assign",
        json={"profile": "SessionAcc", "new_proxy": alt_px},
    )
    assert r_sess.status_code == 409
    assert "active saved session" in r_sess.json()["detail"]

    # Explicit force=true succeeds
    r_force = client.post(
        "/api/v1/proxy/pool/assign",
        json={"profile": "SessionAcc", "new_proxy": alt_px, "force": True},
    )
    assert r_force.status_code == 200
    assert pm.profiles["SessionAcc"].proxy == alt_px


def test_part1_mail_otp_route_detects_checkpoint_and_mail_pool_fallback(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm, data_dir, log_dir, _ = isolated_env
    bl = _DummyBL()
    client, mon = _make_test_client(pm, bl, data_dir, log_dir)

    pm.add_profile("MailAcc", "http://u:p@198.51.100.30:5110", "windows", email="alice@mail.example")
    pm.add_mails_to_pool(["alice@mail.example|MailPass1|M.C512_FAKE_REFRESH_TOKEN_LONG_ENOUGH_12345|9e5f94bc-e8a4-4e73-b8be-63364c29d753"])

    captured: list[tuple[str | None, str | None]] = []

    def _fake_fetch(email, mail_token, top=8, timeout=15, password=None):
        captured.append((email, mail_token))
        return True, "", [
            {
                "subject": "Action needed on your Meta Account",
                "sender": "security@mail.meta.com",
                "time": "2026-11-17T08:00:00Z",
                "preview": "Confirm you're human to continue",
            }
        ]

    from src.api.routes import profiles as profiles_route

    monkeypatch.setattr(profiles_route, "fetch_hotmail_messages", _fake_fetch)
    res = client.post("/api/v1/profiles/MailAcc/mail-otp").json()
    assert res["success"] is True
    assert res["checkpoint_detected"] is True
    assert "MailAcc" in mon.mail_checkpoint_hits
    assert captured and captured[0][1] and "M.C512_FAKE_REFRESH_TOKEN" in captured[0][1]


def test_part1_ui_work_squad_schedule_and_stagger_worker_enforce_guards(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.strings import get_string
    from src.services.monitor import MonitorService
    from src.ui.actions import bulk as bulk_mod
    from src.ui.dialogs.safety_guard import open_work_squad_dialog
    from src.ui.handlers import AppHandlers
    from src.ui.state import AppState

    pm, data_dir, log_dir, _ = isolated_env
    pm.add_profile("NoPx1", "", "windows", pages=["Live"])
    pm.add_profile("Busy1", "http://u:p@sp02-usa101.proxy.example:10101", "windows", timezone="America/New_York", locale="en-US", email="b1@mail.example", pages=["Live"])
    pm.add_profile("Busy2", "http://u:p@sp02-usa101.proxy.example:10102", "windows", timezone="America/New_York", locale="en-US", email="b2@mail.example", pages=["Live"])

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    bl = _DummyBL()
    state = AppState()
    page = _DummyPage()
    logs: list[str] = []

    # 1. open_work_squad_dialog._apply must NOT fall back to conflicting candidates
    launched_squads: list[tuple[list[str], str]] = []
    snap_busy = mon.snapshot(pm.list_profiles(), {"Busy1"})
    open_work_squad_dialog(
        page,  # type: ignore[arg-type]
        pm.list_profiles(),
        snap_busy,
        {"Busy1"},
        on_select_squad=lambda ns, md: launched_squads.append((ns, md)),
        on_launch_staggered=lambda ns, md: launched_squads.append((ns, md)),
        pinned_squads={"4:0": ["Busy2"]},
    )
    dlg = page.dialogs[-1]
    controls_row = dlg.content.content.controls[0]
    fixed_chip = next(
        ctrl
        for ctrl in controls_row.controls
        if isinstance(ctrl, ft.Container)
        and isinstance(ctrl.content, ft.Text)
        and ctrl.content.value == get_string("work_squad_mode_fixed")
    )
    fixed_chip.on_click(None)
    apply_btn = dlg.actions[-1]
    apply_btn.on_click(None)
    assert launched_squads == []

    # 2. AppHandlers Schedule/Squad launch routes through Pre-Launch Guard dialog
    handlers = AppHandlers(
        pm,
        bl,  # type: ignore[arg-type]
        _DummyPS(),  # type: ignore[arg-type]
        state,
        lambda: page,  # type: ignore[arg-type]
        lambda: None,
        lambda m: logs.append(m),
        lambda **_kw: None,
        lambda: ([], pm.list_profiles()),
        monitor=mon,
    )
    handlers._guarded_bulk_launch(["NoPx1"], stagger_seconds=(0, 0))
    assert len(page.dialogs) >= 1
    assert bl.launched == []

    # 3. _stagger_worker aborts delayed launch if port becomes busy during sleep
    launched_from_worker: list[str] = []
    monkeypatch.setattr(
        bulk_mod,
        "launch_or_stop",
        lambda n, *_a, **_kw: launched_from_worker.append(n) or bl.running.add(n),
    )

    def _fake_sleep(_sec: float) -> None:
        # Simulate Busy1 opening on the shared port while Busy2 is sleeping
        bl.running.add("Busy1")

    monkeypatch.setattr(bulk_mod.time, "sleep", _fake_sleep)

    class _SyncThread:
        def __init__(self, target, daemon=True) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(bulk_mod.threading, "Thread", _SyncThread)
    bulk_mod.bulk_launch_profiles(
        ["Busy1", "Busy2"],
        pm,
        bl,  # type: ignore[arg-type]
        state,
        lambda m: logs.append(m),
        stagger_seconds=(1, 1),
        monitor=mon,
    )
    assert launched_from_worker == ["Busy1"]
    assert any("Busy2" in line for line in logs)


def test_part2_exfat_mtime_bump_never_resurrects_cleared_fields(isolated_env) -> None:
    import os

    pm, _data_dir, _log_dir, profiles_file = isolated_env
    pm.add_profile(
        "ResAcc",
        "http://u:p@198.51.100.50:5201",
        "windows",
        group="Watch",
        notes="OldNote",
        color="#22C55E",
        email="res@mail.example",
        password="OldPass123",
    )

    # Simulate coarse exFAT mtime drift (+5s) before clearing group, notes, color, credentials
    st = profiles_file.stat()
    os.utime(profiles_file, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert pm.bulk_update_group(["ResAcc"], None) == 1
    assert pm.profiles["ResAcc"].group is None

    st2 = profiles_file.stat()
    os.utime(profiles_file, ns=(st2.st_atime_ns, st2.st_mtime_ns + 5_000_000_000))
    assert pm.update_profile_credentials("ResAcc", email=None, password=None, notes=None) is True
    assert pm.profiles["ResAcc"].email is None
    assert pm.profiles["ResAcc"].password is None
    assert pm.profiles["ResAcc"].notes is None

    st3 = profiles_file.stat()
    os.utime(profiles_file, ns=(st3.st_atime_ns, st3.st_mtime_ns + 5_000_000_000))
    assert (
        pm.update_profile(
            "ResAcc",
            "ResAcc",
            "http://u:p@198.51.100.50:5201",
            "windows",
            new_color=None,
            new_group=None,
            new_notes=None,
        )
        is True
    )
    assert pm.profiles["ResAcc"].color is None

    # Reload from disk to prove cleared values persisted cleanly
    pm2 = pm_mod.ProfileManager()
    assert pm2.profiles["ResAcc"].group is None
    assert pm2.profiles["ResAcc"].email is None
    assert pm2.profiles["ResAcc"].notes is None
    assert pm2.profiles["ResAcc"].color is None


def test_part2_monitor_multithread_latest_wins_and_no_dict_race(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import threading
    import time

    from src.services.monitor import MonitorService

    pm, data_dir, log_dir, _ = isolated_env
    for i in range(12):
        pm.add_profile(f"Thr_{i}", f"http://u:p@198.51.100.{60+i}:530{i}", "windows", pages=["Live"])

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    errors: list[Exception] = []
    profiles_list = pm.list_profiles()

    def _worker(idx: int) -> None:
        try:
            for j in range(6):
                pname = f"Thr_{(idx + j) % 12}"
                mon.record_started(pname, pm.profiles[pname].proxy)
                mon.snapshot(profiles_list, {pname}, fast_init=True)
                if j % 2 == 0:
                    mon.record_mail_checkpoint_hit(pname, {"subject": f"CP_{idx}_{j}"})
                mon.record_stopped(pname)
        except Exception as exc:  # pragma: no cover - assertion catches any failure
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    settings_path = mon._settings_file()
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    assert isinstance(raw["mail_checkpoint_hits"], dict)

    # Verify Latest-Wins coalescing: an older async write cannot overwrite a newer sync hit
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    gate = threading.Event()
    orig_save_seq = mon._save_seq

    # Simulate an older thread (seq = orig + 1) stalling before writing while a newer hit (seq = orig + 2) saves
    mon._mail_checkpoint_hits["Thr_0"] = {"subject": "STALE_CP"}
    mon._save_seq = orig_save_seq + 1
    stale_seq = mon._save_seq

    def _delayed_older_writer() -> None:
        gate.wait(timeout=2.0)
        with mon._save_lock:
            if stale_seq < mon._written_seq:
                return
            raise AssertionError("Stale seq should have been dropped by Latest-Wins check")

    t_stale = threading.Thread(target=_delayed_older_writer)
    t_stale.start()
    mon.record_mail_checkpoint_hit("Thr_0", {"subject": "FRESH_CP"})
    gate.set()
    t_stale.join()

    time.sleep(0.02)
    reloaded = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    assert reloaded.mail_checkpoint_hits["Thr_0"]["subject"] == "FRESH_CP"


def test_part2_rename_migrates_checkpoint_hits_ui_and_api(isolated_env) -> None:
    from src.services.monitor import MonitorService

    pm, data_dir, log_dir, _ = isolated_env
    bl = _DummyBL()
    client, mon = _make_test_client(pm, bl, data_dir, log_dir)

    px = "http://u:p@198.51.100.75:5401"
    pm.add_profile("OldCP", px, "windows", pages=["Live"])
    mon.record_started("OldCP", px)
    mon.record_stopped("OldCP")
    mon.record_exit_ip("OldCP", "198.51.100.75", country="US", tz="America/Chicago", proxy=px)
    mon.record_mail_checkpoint_hit("OldCP", {"email": "old@mail.example", "subject": "Action needed"})

    # API PATCH rename must migrate MonitorService checkpoint hits, ip_history, and ports
    r_patch = client.patch("/api/v1/profiles/OldCP", json={"name": "NewCP"})
    assert r_patch.status_code == 200
    assert "NewCP" in mon.mail_checkpoint_hits
    assert "OldCP" not in mon.mail_checkpoint_hits
    assert len(mon.get_ip_history("NewCP")) == 1

    # Reload MonitorService from disk to prove migration was persisted
    mon2 = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    assert "NewCP" in mon2.mail_checkpoint_hits
    assert "OldCP" not in mon2.mail_checkpoint_hits
    assert mon2._last_used_ports.get("NewCP") == ":5401"


def test_part3_quarantine_and_swap_preserves_banned_forensics_and_prevents_self_taint(
    isolated_env,
) -> None:
    import asyncio

    from src.mcp_server.server import mcp
    from src.ui.dialogs.batch_and_backup import open_proxy_pool_dialog, open_trust_monitor_dialog

    pm, data_dir, log_dir, _ = isolated_env
    bl = _DummyBL()
    client, mon = _make_test_client(pm, bl, data_dir, log_dir)

    bad_px = "http://u:p@198.51.100.10:5100"
    clean_spare_1 = "http://u:p@198.51.100.20:5200"
    clean_spare_2 = "http://u:p@198.51.100.21:5201"
    usa101 = "http://u:p@sp02-usa101.proxy.example:10778"
    usa109 = "http://u:p@sp02-usa109.proxy.example:10778"
    frozen_spare = "http://u:p@198.51.100.29:5299"

    pm.add_profile("BannedSib", bad_px, "windows", pages=["Disconnected"], status="check")
    pm.add_profile("LiveSib", bad_px, "windows", timezone="America/Los_Angeles", locale="en-US", pages=["Live"])
    pm.add_profile("FrozenOwner", frozen_spare, "windows", pages=["Disconnected"])
    pm.update_profile_dates("FrozenOwner", None, "2026-11-16")
    mon.record_started("FrozenOwner", frozen_spare)
    mon.record_stopped("FrozenOwner")

    pm.add_profile("Host101", usa101, "windows", pages=["Live"])
    pm.add_profile("Host109", usa109, "windows", pages=["Live"])

    added_cnt, _ = pm.add_proxies_to_pool([frozen_spare, clean_spare_1, clean_spare_2])
    assert added_cnt == 2  # frozen_spare already assigned to FrozenOwner -> skipped

    # Before swap: LiveSib should flag burned_proxy because it shares bad_px with BannedSib
    snap_before = mon.snapshot(pm.list_profiles(), set())
    assert "burned_proxy" in snap_before.profiles["LiveSib"].audit_codes

    # Execute Quarantine & Clean Swap via REST API without explicit new_proxy -> auto-picks cleanest spare
    r_swap = client.post(
        "/api/v1/proxy/pool/quarantine-swap",
        json={"old_target": bad_px, "reason": "burned_by_sib"},
    )
    assert r_swap.status_code == 200
    body = r_swap.json()
    assert body["success"] is True
    assert body["updated_profiles"] == ["LiveSib"]
    # BannedSib MUST keep its original forensic proxy; LiveSib gets clean_spare_1
    assert pm.profiles["BannedSib"].proxy == bad_px
    assert pm.profiles["LiveSib"].proxy == clean_spare_1

    # Re-run snapshot: LiveSib MUST have ZERO burned_proxy finding (zero self-contamination!)
    snap_after = mon.snapshot(pm.list_profiles(), set())
    assert "burned_proxy" not in snap_after.profiles["LiveSib"].audit_codes

    # Re-importing bad_px into proxy_pool must be blocked while quarantined
    re_add, re_skip = pm.add_proxies_to_pool([bad_px, "http://u:p@198.51.100.31:5301"])
    assert re_add == 1
    assert re_skip == 1

    # Quarantine listing + standalone pool quarantine + unquarantine DELETE route
    dead_pool_only = "http://u:p@198.51.100.15:5105"
    assert pm.quarantine_proxy(dead_pool_only, reason="dead_check") is True
    assert pm.add_proxies_to_pool([dead_pool_only]) == (0, 1)

    q_list = client.get("/api/v1/proxy/pool/quarantine").json()
    assert "198.51.100.10:5100" in q_list["quarantined"]
    assert "198.51.100.15:5105" in q_list["quarantined"]
    assert client.delete("/api/v1/proxy/pool/quarantine", params={"endpoint": "198.51.100.15:5105"}).json()["success"] is True
    re_add2, _ = pm.add_proxies_to_pool([dead_pool_only])
    assert re_add2 == 1

    # Host-qualified replacement: swapping usa101:10778 must NOT overwrite usa109:10778
    swapped_hosts = pm.replace_proxy_across_profiles(usa101, clean_spare_2)
    assert swapped_hosts == ["Host101"]
    assert pm.profiles["Host109"].proxy == usa109

    # refresh_profile_geo updates previously pinned timezone/locale after regional swap
    assert pm.refresh_profile_geo("LiveSib", "America/New_York", "en-US") is True
    assert pm.profiles["LiveSib"].timezone == "America/New_York"

    # rank_clean_spare_proxies excludes frozen + quarantined endpoints
    ranked = mon.rank_clean_spare_proxies(
        [frozen_spare, bad_px, clean_spare_2],
        pm.list_profiles(),
        set(),
        quarantined_endpoints={"198.51.100.10:5100"},
    )
    assert frozen_spare not in ranked
    assert bad_px not in ranked

    # Verify MCP tool registration + UI dialogs render with new Quarantine callbacks
    tool_names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "quarantine_and_swap_proxy" in tool_names

    page = _DummyPage()
    open_proxy_pool_dialog(
        page,  # type: ignore[arg-type]
        pm.list_profiles,
        set,
        dict,
        lambda: None,
        lambda _: None,
        lambda old_t, new_px: (True, f"{old_t}->{new_px}"),
        get_unassigned=pm.list_unassigned_proxies,
        get_clean_spares=lambda px: mon.rank_clean_spare_proxies(pm.list_unassigned_proxies(), pm.list_profiles(), set()),
        on_quarantine_swap=lambda old_t, new_px: (True, f"q:{old_t}"),
        get_quarantined=pm.list_quarantined_proxies,
        on_unquarantine=pm.unquarantine_proxy,
    )
    assert len(page.dialogs) == 1

    open_trust_monitor_dialog(
        page,  # type: ignore[arg-type]
        pm.list_profiles,
        lambda: snap_before,
        lambda _ip, _r: None,
        initial_tab="audit",
        on_quarantine_swap_finding=lambda profs: (True, f"swapped:{','.join(profs)}"),
    )
    assert len(page.dialogs) == 2


def test_part4_keepalive_scheduler_pins_short_hatch_sess_deadlines() -> None:
    import datetime as dt

    from src.models.profile import Profile
    from src.services.monitor import MonitorSnapshot, ProfileMonitorInfo
    from src.services.scheduler import build_safe_schedule

    now_dt = dt.datetime(2026, 11, 17, 12, 0, 0)
    snap = MonitorSnapshot()
    profiles: list[Profile] = []

    clusters = [
        "sp02-usa101.proxy.example",
        "sp02-usa102.proxy.example",
        "sp02-usa103.proxy.example",
        "sp02-usa104.proxy.example",
        "198.51.100.10",
    ]
    for i in range(18):
        cl = clusters[i % len(clusters)]
        name = f"Healthy_{i}"
        profiles.append(
            Profile(
                name=name,
                proxy=f"http://u:p@{cl}:100{i:02d}",
                os_type="windows",
                timezone="America/New_York",
                locale="en-US",
                email=f"h{i}@mail.example",
                pages=["Live"],
                created_at="2026-10-01",
            )
        )
        snap.profiles[name] = ProfileMonitorInfo(
            name=name,
            last_closed=now_dt - dt.timedelta(hours=72 + i * 2),
            sess_exp="Còn 18d",
            sess_exp_days=18,
            hatch_sess=True,
            hatch_gw=True,
        )

    for pname, exp_d, rest_h, audit in (
        ("Urgent2d", 2, 24, []),
        ("Urgent3d", 3, 20, []),
        ("Red62", 3, 20, ["burned_batch"]),
    ):
        profiles.append(
            Profile(
                name=pname,
                proxy=f"http://u:p@sp02-usa101.proxy.example:1099{len(profiles)}",
                os_type="windows",
                timezone="America/New_York",
                locale="en-US",
                email=f"{pname.lower()}@mail.example",
                pages=["Live"],
                created_at="2026-10-01",
            )
        )
        snap.profiles[pname] = ProfileMonitorInfo(
            name=pname,
            last_closed=now_dt - dt.timedelta(hours=rest_h),
            sess_exp=f"Còn {exp_d}d",
            sess_exp_days=exp_d,
            hatch_sess=True,
            hatch_gw=True,
            audit_codes=audit,
        )

    plan = build_safe_schedule(
        profiles,
        snap,
        cycle_days=12,
        schedule_mode="keepalive",
        now=now_dt,
    )
    day_by_name = {
        name: day.day_index
        for day in plan.days
        for name in day.account_names
    }
    assert day_by_name["Urgent2d"] == 0
    assert day_by_name["Urgent3d"] <= 1
    assert day_by_name["Red62"] <= 1


def test_part4_offline_oidc_cookie_inspection_readonly_and_subnet_drift(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    import time

    from src.services.monitor import MonitorService

    pm, data_dir, log_dir, _ = isolated_env
    px = "http://u:p@sp02-usa106.proxy.example:11339"
    pm.add_profile(
        "OIDCAcc",
        px,
        "windows",
        timezone="America/Los_Angeles",
        locale="en-US",
        email="oidc@hotmail.com",
        pages=["Live"],
    )
    pdir = data_dir / "OIDCAcc"
    pdir.mkdir(parents=True, exist_ok=True)
    db_path = pdir / "cookies.sqlite"
    now_ts = int(time.time())
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, creationTime INTEGER, expiry INTEGER)")
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
        (".auth.meta.com", "datr", (now_ts - 864000) * 1_000_000, now_ts + 86400 * 300),
    )
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
        (".auth.meta.com", "fs", (now_ts - 864000) * 1_000_000, now_ts + 86400 * 300),
    )
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
        (".muse.ai", "hatch_sess", (now_ts - 86400 * 31) * 1_000_000, now_ts - 3600),
    )
    conn.commit()
    conn.close()

    mtime_before = db_path.stat().st_mtime_ns
    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    mon.record_exit_ip("OIDCAcc", "2001:db8:40:2ca::1", country="US", proxy=px)
    mon.record_exit_ip("OIDCAcc", "2001:db8:40:2cf::9", country="US", proxy=px)

    snap = mon.snapshot(pm.list_profiles(), set())
    assert db_path.stat().st_mtime_ns == mtime_before
    assert not (pdir / "cookies.sqlite-wal").exists()
    assert not (pdir / "cookies.sqlite-shm").exists()

    info = snap.profiles["OIDCAcc"]
    assert info.oidc_ready is True
    assert info.requires_meta_oidc is True
    assert info.oidc_subnet_drifted is True

    warnings = mon.check_prelaunch(pm.profiles["OIDCAcc"], pm.list_profiles(), set(), snap=snap)
    codes = {w.code for w in warnings}
    assert "oidc_subnet_drifted" in codes
    assert "oidc_preflight_unverified" in codes

    # While OIDCAcc is running, sqlite3.connect must NEVER be called for it
    real_connect = sqlite3.connect
    opened_uris: list[str] = []

    def _spy_connect(uri, *a, **kw):
        opened_uris.append(str(uri))
        return real_connect(uri, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    mon.snapshot(pm.list_profiles(), {"OIDCAcc"})
    assert not any("OIDCAcc" in u for u in opened_uris)


def test_part4_cp0_preflight_interlock_aborts_on_hit_and_launches_on_clean(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import sqlite3
    import time

    from src.core.strings import get_string
    from src.services.monitor import MonitorService
    from src.ui import handlers as handlers_mod
    from src.ui.handlers import AppHandlers
    from src.ui.state import AppState

    pm, data_dir, log_dir, _ = isolated_env
    shared_clean_px = "http://u:p@198.51.100.88:5501"
    used_px = "http://u:p@198.51.100.89:5502"
    clean_px = "http://u:p@sp02-usa102.proxy.example:10202"

    pm.add_profile(
        "WatchHit",
        shared_clean_px,
        "windows",
        timezone="America/New_York",
        locale="en-US",
        email="watchhit@mail.example",
        group="1. Sạch nhất",
        pages=["Theo dõi"],
    )
    pm.add_profile(
        "SiblingFree",
        shared_clean_px,
        "windows",
        timezone="America/New_York",
        locale="en-US",
        pages=["Live"],
    )
    pm.add_profile(
        "HitExp",
        used_px,
        "windows",
        timezone="America/New_York",
        locale="en-US",
        email="hit1@mail.example",
        pages=["Live"],
    )
    pm.add_profile(
        "CleanEmbryo",
        clean_px,
        "windows",
        timezone="America/New_York",
        locale="en-US",
        email="clean1@mail.example",
        group="1. Sạch nhất",
        pages=["Theo dõi"],
    )

    # Seed expired hatch_sess + .auth.meta.com cookies on HitExp
    hit_dir = data_dir / "HitExp"
    hit_dir.mkdir(parents=True, exist_ok=True)
    now_ts = int(time.time())
    conn = sqlite3.connect(str(hit_dir / "cookies.sqlite"))
    conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, creationTime INTEGER, expiry INTEGER)")
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
        (".auth.meta.com", "datr", (now_ts - 864000) * 1_000_000, now_ts + 86400 * 300),
    )
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
        (".auth.meta.com", "fs", (now_ts - 864000) * 1_000_000, now_ts + 86400 * 300),
    )
    conn.execute(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
        (".muse.ai", "hatch_sess", (now_ts - 86400 * 31) * 1_000_000, now_ts - 3600),
    )
    conn.commit()
    conn.close()
    old_mtime = float(now_ts - 86400 * 5)
    os.utime(hit_dir / "cookies.sqlite", (old_mtime, old_mtime))
    hit_mtime_before = (hit_dir / "cookies.sqlite").stat().st_mtime_ns

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    bl = _DummyBL()
    state = AppState()
    page = _DummyPage()
    logs: list[str] = []
    handlers = AppHandlers(
        pm,
        bl,  # type: ignore[arg-type]
        _DummyPS(),  # type: ignore[arg-type]
        state,
        lambda: page,  # type: ignore[arg-type]
        lambda: None,
        lambda m: logs.append(m),
        lambda **_kw: None,
        lambda: ([], pm.list_profiles()),
        monitor=mon,
    )

    class _SyncThread:
        def __init__(self, target, daemon=True) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(handlers_mod.threading, "Thread", _SyncThread)

    def _fake_scan(targets, **_kw):
        pname, em, _tok = targets[0]
        if pname in ("WatchHit", "HitExp"):
            return {
                "hits": {
                    pname: {
                        "email": em,
                        "subject": "Action needed on your Meta Account",
                        "time": "2026-11-17 10:00",
                        "preview": "Confirm you're human",
                        "resolved": "false",
                    }
                },
                "dead_tokens": set(),
                "scanned_names": {pname},
                "email_status": {em.lower(): ("error", "CP")},
            }
        return {
            "hits": {},
            "dead_tokens": set(),
            "scanned_names": {pname},
            "email_status": {em.lower(): ("ok", "CP=0")},
        }

    monkeypatch.setattr(handlers_mod, "run_safe_mail_checkpoint_scan", _fake_scan)
    monkeypatch.setattr(
        handlers_mod,
        "launch_or_stop",
        lambda n, *_a, **_kw: bl.launched.append(n) or bl.running.add(n) or state.set_loading(n, False),
    )

    # 1a. WatchHit (unlaunched embryo) fails CP=0 -> aborts, quarantines, detaches untouched proxy!
    handlers.on_launch("WatchHit")
    assert len(page.dialogs) == 1
    dlg_watch_hit = page.dialogs[-1]
    verify_btn_watch = next(
        b for b in dlg_watch_hit.actions if getattr(b, "content", None) == get_string("guard_verify_cp0_launch_btn")
    )
    verify_btn_watch.on_click(None)
    assert bl.launched == []
    assert pm.profiles["WatchHit"].status == "check"
    assert "Disconnected" in pm.profiles["WatchHit"].pages
    assert pm.profiles["WatchHit"].proxy is None
    snap_after_watch = mon.snapshot(pm.list_profiles(), set())
    assert snap_after_watch.profiles["SiblingFree"].dead_port_freeze_hours == 0.0
    assert "burned_proxy" not in snap_after_watch.profiles["SiblingFree"].audit_codes

    # 1b. HitExp (previously launched with cookies.sqlite) fails CP=0 -> aborts, preserves forensic proxy!
    handlers.on_launch("HitExp")
    assert len(page.dialogs) == 1
    dlg_hit = page.dialogs[-1]
    verify_btn_hit = next(
        b for b in dlg_hit.actions if getattr(b, "content", None) == get_string("guard_verify_cp0_launch_btn")
    )
    verify_btn_hit.on_click(None)
    assert bl.launched == []
    assert (hit_dir / "cookies.sqlite").stat().st_mtime_ns == hit_mtime_before
    assert "Disconnected" in pm.profiles["HitExp"].pages
    assert pm.profiles["HitExp"].proxy == used_px

    # 2. Clean branch: CleanEmbryo passes CP=0 -> launches browser + caches last_clean_mail_ts
    handlers.on_launch("CleanEmbryo")
    assert len(page.dialogs) == 1
    dlg_clean = page.dialogs[-1]
    verify_btn_clean = next(
        b for b in dlg_clean.actions if getattr(b, "content", None) == get_string("guard_verify_cp0_launch_btn")
    )
    verify_btn_clean.on_click(None)
    assert bl.launched == ["CleanEmbryo"]
    snap_clean = mon.snapshot(pm.list_profiles(), set())
    assert snap_clean.profiles["CleanEmbryo"].last_clean_mail_ts > 0.0
    assert mon.check_prelaunch(pm.profiles["CleanEmbryo"], pm.list_profiles(), set(), snap=snap_clean) == []

    # Rename preserves clean mail check timestamp across MonitorService reloads
    mon.rename_profile("CleanEmbryo", "CleanEmbryoRenamed")
    mon2 = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    assert "CleanEmbryoRenamed" in mon2._mail_clean_checked_ts
    assert "CleanEmbryo" not in mon2._mail_clean_checked_ts
