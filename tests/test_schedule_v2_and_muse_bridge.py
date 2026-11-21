"""Tests for MuseBridge integration, Keep-Alive 10-14d, Work 5-7d, Dead-Port Freeze & Embryos."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import time

import flet as ft
import pytest

from src.services.profile import manager as pm_mod


@pytest.fixture
def isolated_pm(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "camoufox_data"
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(profiles_file))
    return pm_mod.ProfileManager()


class _DummyPage:
    def __init__(self) -> None:
        self.dialogs: list[ft.AlertDialog] = []

    def show_dialog(self, d: ft.AlertDialog) -> None:
        self.dialogs.append(d)

    def pop_dialog(self) -> None:
        if self.dialogs:
            self.dialogs.pop()

    def update(self) -> None:
        pass


def _write_cookies_db(
    db_path: pathlib.Path,
    *,
    sess_days_left: int = 20,
    has_accountscenter: bool = False,
    meta_cp: bool = False,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now_s = int(time.time())
    exp_sess = now_s + sess_days_left * 86400 + 3600
    exp_gw = now_s + 300 * 86400
    mtime_3d = now_s - 3 * 86400
    last_acc_us = mtime_3d * 1_000_000
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS moz_cookies ("
            "host TEXT, name TEXT, value TEXT, expiry INTEGER, "
            "creationTime INTEGER, lastAccessed INTEGER)"
        )
        rows = [
            (".muse.ai", "hatch_gw", "1", exp_gw, (now_s - 20 * 86400) * 1_000_000, last_acc_us),
            (".muse.ai", "hatch_sess", "1", exp_sess, (now_s - 10 * 86400) * 1_000_000, last_acc_us),
            (".auth.meta.com", "datr", "x", exp_gw, (now_s - 25 * 86400) * 1_000_000, (now_s - 25 * 86400) * 1_000_000),
        ]
        conn.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    os.utime(db_path, (mtime_3d, mtime_3d))
    if has_accountscenter or meta_cp:
        places_path = db_path.parent / "places.sqlite"
        with sqlite3.connect(str(places_path)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS moz_places (url TEXT, last_visit_date INTEGER)")
            if has_accountscenter:
                conn.execute(
                    "INSERT INTO moz_places VALUES (?, ?)",
                    ("https://accountscenter.meta.com/personal_info/contact_points/", last_acc_us),
                )
            if meta_cp:
                conn.execute(
                    "INSERT INTO moz_places VALUES (?, ?)",
                    ("https://auth.meta.com/checkpoints/", last_acc_us),
                )
            conn.commit()
        os.utime(places_path, (mtime_3d, mtime_3d))


def test_t1_muse_bridge_detection_isolation_and_gateway_cap(
    isolated_pm,
    tmp_path,
    monkeypatch,
) -> None:
    from src.services.monitor import MonitorService
    from src.services.scheduler import select_parallel_work_squad
    from src.ui.components.profile_card import build_profile_card

    data_dir = tmp_path / "camoufox_data"
    log_dir = tmp_path / "logs"
    mb_dir = tmp_path / "muse-bridge"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    mb_dir.mkdir(parents=True, exist_ok=True)

    now_ts = time.time()
    (mb_dir / "config.json").write_text(
        json.dumps(
            {
                "profiles": {
                    "muse-13t": {
                        "profile_name": "13T",
                        "max_daily_open_hours": 6.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (mb_dir / "state_13T.json").write_text(
        json.dumps(
            {
                "last_launch_ts": now_ts - 5,
                "runner_pid": os.getpid(),
                "circuit_breaker": {"tripped": False, "reason": "", "url": ""},
                "open_history": [[now_ts - 900, now_ts - 300]],
                "threads": {
                    "t1": {
                        "bot_name": "Waffle",
                        "updated_at": 2000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    isolated_pm.add_profile(
        "13T",
        "http://u:p@209.141.40.174:5401",
        "windows",
        timezone="America/Los_Angeles",
        locale="en-US",
        email="13t@test.com",
        group="4. Rủi ro cao",
        pages=["Theo dõi"],
    )
    _write_cookies_db(data_dir / "13T" / "cookies.sqlite", sess_days_left=18)

    for idx in range(1, 5):
        name = f"GW_Live_{idx}"
        isolated_pm.add_profile(
            name,
            f"http://u:p@209.141.40.174:{5410 + idx}",
            "windows",
            timezone="America/Los_Angeles",
            locale="en-US",
            email=f"gw{idx}@test.com",
            pages=["Live"],
        )
        _write_cookies_db(data_dir / name / "cookies.sqlite", sess_days_left=22)

    isolated_pm.add_profile(
        "USA104_Live",
        "socks5://u:p@sp02-usa104.nguonproxy.com:10051",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
        email="u104@test.com",
        pages=["Live"],
    )
    _write_cookies_db(data_dir / "USA104_Live" / "cookies.sqlite", sess_days_left=20)

    mon = MonitorService(
        data_dir=str(data_dir),
        log_dir=str(log_dir),
        muse_bridge_dir=mb_dir,
    )
    opened_sqlite_uris: list[str] = []
    orig_connect = sqlite3.connect

    def _spy_connect(target: object, *args: object, **kwargs: object):
        opened_sqlite_uris.append(str(target))
        return orig_connect(target, *args, **kwargs)

    monkeypatch.setattr("src.services.monitor.sqlite3.connect", _spy_connect)

    profiles = list(isolated_pm.profiles.values())
    snap = mon.snapshot(profiles, running_names=set())

    assert snap.muse_bridge_bound_count == 1
    assert "13T" in snap.muse_bridge_running_names
    assert snap.muse_bridge_running_gw_clusters.get("209.141.40.174") == 1
    info_13t = snap.profiles["13T"]
    assert info_13t.muse_bridge_running is True
    assert info_13t.muse_bridge_model == "muse-13t"
    assert info_13t.muse_bridge_bot == "Waffle"
    # Critical anti-detect invariant: cookies.sqlite MUST NOT be touched while MuseBridge runs 13T
    assert not any("13T" in u for u in opened_sqlite_uris)
    assert any("USA104_Live" in u for u in opened_sqlite_uris)

    warnings_running = mon.check_prelaunch(
        isolated_pm.profiles["13T"],
        profiles,
        set(),
        snap=snap,
    )
    assert any(w.code == "muse_bridge_running" and w.level == "critical" for w in warnings_running)

    # Gateway /48 cap max_per_gateway=3: 13T uses 1 slot, so at most 2 more 209.141* profiles allowed
    squad = select_parallel_work_squad(
        profiles,
        snap,
        set(),
        count=6,
        mode="rotation",
        max_per_gateway=3,
    )
    picked_names = [c.name for c in squad.candidates]
    assert "13T" not in picked_names
    assert "USA104_Live" in picked_names
    gw_picked = [c for c in squad.candidates if c.name.startswith("GW_Live_")]
    assert len(gw_picked) <= 2

    # Card builds cleanly with MuseBridge pill badge
    card = build_profile_card(
        isolated_pm.profiles["13T"],
        False,
        True,
        lambda _n: None,
        lambda _n: None,
        lambda _n: None,
        monitor_info=info_13t,
    )
    assert card is not None

    # Circuit breaker tripped -> emits critical muse_bridge_tripped prelaunch warning
    (mb_dir / "state_13T.json").write_text(
        json.dumps(
            {
                "last_launch_ts": now_ts - 600,
                "runner_pid": 0,
                "circuit_breaker": {
                    "tripped": True,
                    "reason": "Meta checkpoint detected",
                    "url": "https://auth.meta.com/checkpoints/",
                    "ts": now_ts - 30,
                },
                "open_history": [],
                "threads": {},
            }
        ),
        encoding="utf-8",
    )
    snap2 = mon.snapshot(profiles, running_names=set())
    assert snap2.profiles["13T"].muse_bridge_tripped is True
    warnings_tripped = mon.check_prelaunch(
        isolated_pm.profiles["13T"],
        profiles,
        set(),
        snap=snap2,
    )
    assert any(w.code == "muse_bridge_tripped" and w.level == "critical" for w in warnings_tripped)


def test_t2_keepalive_work_quarantine_solo_and_multiweek_tags(
    isolated_pm,
    tmp_path,
) -> None:
    from src.models.profile import (
        is_quarantine_schedule_tag,
        parse_schedule_shift,
        parse_schedule_tag,
        parse_schedule_week,
        resolve_status_display,
    )
    from src.services.monitor import MonitorService
    from src.services.scheduler import build_safe_schedule, format_schedule_markdown

    data_dir = tmp_path / "camoufox_data"
    log_dir = tmp_path / "logs"
    mb_dir = tmp_path / "muse-bridge"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    mb_dir.mkdir(parents=True, exist_ok=True)
    (mb_dir / "config.json").write_text(
        json.dumps({"profiles": {"muse-bot": {"profile_name": "BotExpiring"}}}),
        encoding="utf-8",
    )
    (mb_dir / "state_BotExpiring.json").write_text(
        json.dumps({"runner_pid": 0, "threads": {"t": {"bot_name": "Maus", "updated_at": 1}}}),
        encoding="utf-8",
    )

    # Create accounts covering every cohort: urgent (6d), mid (14d), healthy (26d),
    # tainted (+19376123124 Quarantine), clean diamond vs legacy orange, and MuseBridge 8d bot.
    specs = [
        ("Urgent_6d", "socks5://u:p@sp02-usa101.nguonproxy.com:10001", 6, False, ""),
        ("Mid_14d", "http://u:p@209.141.40.174:5101", 14, False, ""),
        ("CleanDiamond", "socks5://u:p@sp02-usa102.nguonproxy.com:10002", 18, False, ""),
        ("CleanBuddy", "http://u:p@209.141.40.174:5102", 18, False, ""),
        ("OrangeLegacy", "http://u:p@209.141.40.174:5103", 17, True, "2 tầng"),
        ("Healthy_26d", "socks5://u:p@sp02-usa103.nguonproxy.com:10003", 26, False, ""),
        ("Healthy_27d", "http://u:p@209.141.40.174:5104", 27, False, ""),
        ("Acc62_Taint", "http://u:p@209.141.40.174:5105", 12, True, "+19376123124 @tuan89.site"),
        ("BotExpiring", "http://u:p@209.141.40.174:5106", 8, False, ""),
    ]
    for name, px, exp_d, has_ac, notes in specs:
        isolated_pm.add_profile(
            name,
            px,
            "windows",
            timezone="America/Detroit" if "usa" in px else "America/Los_Angeles",
            locale="en-US",
            email=f"{name.lower()}@hotmail.com",
            notes=notes,
            pages=["Live"],
        )
        isolated_pm.profiles[name].created_at = "2026-10-06"
        _write_cookies_db(
            data_dir / name / "cookies.sqlite",
            sess_days_left=exp_d,
            has_accountscenter=has_ac,
        )

    mon = MonitorService(
        data_dir=str(data_dir),
        log_dir=str(log_dir),
        muse_bridge_dir=mb_dir,
    )
    now_dt = dt.datetime(2026, 11, 16, 10, 0, 0)  # Monday
    for idx, (name, *_rest) in enumerate(specs):
        mon._live_stopped_at[name] = now_dt - dt.timedelta(hours=72 - idx)
        # Inject datr age > 30 so CleanDiamond hits diamond/elite trust_tier

    p_list = list(isolated_pm.profiles.values())
    snap = mon.snapshot(p_list, running_names=set())

    # 1. Keep-Alive 12-day plan
    ka_plan = build_safe_schedule(
        p_list,
        snap,
        cycle_days=12,
        schedule_mode="keepalive",
        now=now_dt,
        shuffle_seed=11,
    )
    assert ka_plan.schedule_mode == "keepalive"
    assert ka_plan.collision_count == 0
    # Healthy >21d deferred automatically
    deferred_names = {a.name for a in ka_plan.healthy_deferred}
    assert {"Healthy_26d", "Healthy_27d"}.issubset(deferred_names)
    # BotExpiring separated from manual schedule and flagged for MausBot ping
    assert [a.name for a in ka_plan.muse_bridge_accounts] == ["BotExpiring"]
    assert [a.name for a in ka_plan.muse_bridge_ping_needed] == ["BotExpiring"]
    # Acc62_Taint is strictly in quarantine_solos and NEVER paired with clean accounts
    assert [a.name for a in ka_plan.quarantine_accounts] == ["Acc62_Taint"]
    for d in ka_plan.days:
        for pr in d.all_pairs:
            assert "Acc62_Taint" not in (
                pr.first.name,
                pr.second.name if pr.second else "",
            )
    ka_tags = ka_plan.status_tag_map()
    assert ka_tags["Healthy_26d"] == ""
    assert is_quarantine_schedule_tag(ka_tags["Acc62_Taint"]) is True

    # Urgent_6d (6d) must appear on an earlier day than Mid_14d (14d) / CleanDiamond (18d)
    ka_day_map = {n: d.day_index for d in ka_plan.days for n in d.account_names}
    assert ka_day_map["Urgent_6d"] <= ka_day_map["CleanDiamond"]

    # Verify W2 tag parsing, display & sort helpers
    assert parse_schedule_tag("W2-T3-C1-S") == ("T3", 1)
    assert parse_schedule_week("W2-T3-C1-S") == 2
    assert parse_schedule_shift("W2-T3-C1-S") == "morning"
    assert is_quarantine_schedule_tag("W2-T5-Q1-T") is True
    disp_w2 = resolve_status_display("W2-T3-C1-S")
    assert disp_w2 is not None and disp_w2[0].startswith("W2·T3")
    disp_q = resolve_status_display("T5-Q1-T")
    assert disp_q is not None and "Cách ly" in disp_q[0]

    # Preserved tag reload across W2 day indices
    isolated_pm.profiles["Urgent_6d"].status = "T2-C1-S"
    isolated_pm.profiles["Mid_14d"].status = "W2-T2-C1-T"
    isolated_pm.profiles["CleanDiamond"].status = "T3-C1-S"
    isolated_pm.profiles["CleanBuddy"].status = "T3-C1-T"
    reloaded = build_safe_schedule(
        list(isolated_pm.profiles.values()),
        snap,
        cycle_days=12,
        schedule_mode="keepalive",
        now=now_dt,
    )
    rl_day_map = {n: d.day_index for d in reloaded.days for n in d.account_names}
    assert rl_day_map["Mid_14d"] == 7  # Day index 7 = Week 2 Monday (T2)
    assert reloaded.status_tag_map()["Mid_14d"].startswith("W2-T2-C")

    # 2. Work 5-day plan (all non-tainted non-bot live accounts scheduled, Morning/Evening balanced)
    for n in ("Urgent_6d", "Mid_14d", "CleanDiamond", "CleanBuddy"):
        isolated_pm.profiles[n].status = None
    work_plan = build_safe_schedule(
        list(isolated_pm.profiles.values()),
        snap,
        cycle_days=5,
        schedule_mode="work",
        now=now_dt,
        shuffle_seed=22,
    )
    assert work_plan.schedule_mode == "work"
    assert work_plan.healthy_deferred == []
    assert [a.name for a in work_plan.quarantine_accounts] == ["Acc62_Taint"]
    md_work = format_schedule_markdown(work_plan)
    assert "LỊCH LÀM VIỆC" in md_work
    assert "Quarantine Solo" in md_work


def test_t3_dead_port_freeze_and_high_risk_asn_firewall(
    isolated_pm,
    tmp_path,
) -> None:
    from src.services.monitor import MonitorService
    from src.ui.dialogs import open_proxy_pool_dialog

    data_dir = tmp_path / "camoufox_data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    today = dt.date.today()
    yesterday_str = (today - dt.timedelta(days=1)).isoformat()
    old_ban_str = (today - dt.timedelta(days=5)).isoformat()

    frz_px = "http://u:p@209.141.40.174:5499"
    unfrz_px = "http://u:p@209.141.40.174:5500"
    usa_px = "socks5://u:p@sp02-usa104.nguonproxy.com:10052"

    isolated_pm.add_profile("DeadRecent", frz_px, "windows", group="Disconnected", pages=["Disconnected"])
    isolated_pm.profiles["DeadRecent"].banned_at = yesterday_str

    isolated_pm.add_profile("SiblingOnFrozen", frz_px, "windows", timezone="America/Los_Angeles", locale="en-US", pages=["Theo dõi"])
    isolated_pm.add_profile("DeadOld", unfrz_px, "windows", group="Disconnected", pages=["Disconnected"])
    isolated_pm.profiles["DeadOld"].banned_at = old_ban_str
    isolated_pm.add_profile("SiblingOnOld", unfrz_px, "windows", timezone="America/Los_Angeles", locale="en-US", pages=["Theo dõi"])

    isolated_pm.add_profile(
        "RiskWatch",
        usa_px,
        "windows",
        timezone="America/Detroit",
        locale="en-US",
        group="4. Rủi ro cao",
        pages=["Theo dõi"],
    )
    isolated_pm.add_profile(
        "RiskCandidate",
        "",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
        group="4. Rủi ro cao",
        pages=["Theo dõi"],
    )
    isolated_pm.add_profile(
        "CleanWatch",
        "",
        "windows",
        timezone="America/Detroit",
        locale="en-US",
        group="1. Sạch nhất",
        pages=["Theo dõi"],
    )

    p_list = list(isolated_pm.profiles.values())
    snap = mon.snapshot(p_list, set())

    assert "209.141.40.174:5499" in snap.frozen_endpoints
    assert "209.141.40.174:5500" not in snap.frozen_endpoints
    assert snap.profiles["SiblingOnFrozen"].dead_port_freeze_hours > 0.0
    assert snap.profiles["SiblingOnOld"].dead_port_freeze_hours == 0.0

    w_frz = mon.check_prelaunch(isolated_pm.profiles["SiblingOnFrozen"], p_list, set(), snap=snap)
    assert any(w.code == "dead_port_freeze" and w.level == "critical" for w in w_frz)

    w_old = mon.check_prelaunch(isolated_pm.profiles["SiblingOnOld"], p_list, set(), snap=snap)
    assert not any(w.code == "dead_port_freeze" for w in w_old)

    w_asn = mon.check_prelaunch(isolated_pm.profiles["RiskWatch"], p_list, set(), snap=snap)
    assert any(w.code == "high_risk_clean_asn" and w.level == "critical" for w in w_asn)

    # Proxy Pool dialog must NOT suggest high-risk profile on clean sp02-usa104
    from src.core.strings import get_string

    dp = _DummyPage()
    usa_px_free = "socks5://u:p@sp02-usa104.nguonproxy.com:10099"
    isolated_pm.add_proxies_to_pool([usa_px_free])
    open_proxy_pool_dialog(
        dp,  # type: ignore[arg-type]
        isolated_pm.list_profiles,
        lambda: set(),
        lambda: {},
        lambda: None,
        lambda _n: None,
        lambda _o, _n: (True, ""),
        get_unassigned=isolated_pm.list_unassigned_proxies,
        on_assign_to_profile=lambda _p, _px: True,
        frozen_endpoints=snap.frozen_endpoints,
    )
    dlg = dp.dialogs[0]

    def _walk_text_labels(ctrl) -> list[str]:
        out: list[str] = []
        if isinstance(ctrl, ft.Text) and ctrl.value:
            out.append(str(ctrl.value))
        content = getattr(ctrl, "content", None)
        if content is not None:
            out.extend(_walk_text_labels(content))
        for child in getattr(ctrl, "controls", None) or []:
            out.extend(_walk_text_labels(child))
        return out

    assign_label = get_string("proxy_pool_assign_btn")

    def _find_assign_btn(ctrl):
        if isinstance(ctrl, ft.OutlinedButton) and assign_label in str(ctrl.content):
            return ctrl
        for child in (getattr(ctrl, "controls", None) or []):
            res = _find_assign_btn(child)
            if res is not None:
                return res
        content = getattr(ctrl, "content", None)
        if content is not None:
            return _find_assign_btn(content)
        return None

    assign_btn = _find_assign_btn(dlg.content)
    assert assign_btn is not None
    assign_btn.on_click(None)
    rendered_labels = _walk_text_labels(dlg.content)
    assert any("CleanWatch" in t for t in rendered_labels)
    assert not any("RiskCandidate" in t for t in rendered_labels)


def test_t4_incubation_engine_and_three_tab_schedule_dialog(
    isolated_pm,
    tmp_path,
) -> None:
    from src.services.monitor import MonitorService, extract_proxy_endpoint
    from src.services.scheduler import build_incubation_recommendations
    from src.ui.dialogs import open_safe_schedule_dialog

    data_dir = tmp_path / "camoufox_data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    today = dt.date.today()
    # Dead account on 209.141:5496 -> triggers CC12 / frozen warning
    dead_ep = "http://u:p@209.141.40.174:5496"
    isolated_pm.add_profile("DeadHost", dead_ep, "windows", group="Disconnected", pages=["Disconnected"])
    isolated_pm.profiles["DeadHost"].banned_at = (today - dt.timedelta(days=1)).isoformat()

    isolated_pm.add_profile("Clean15d", "", "windows", group="1. Sạch nhất", pages=["Theo dõi"])
    isolated_pm.profiles["Clean15d"].created_at = (today - dt.timedelta(days=15)).isoformat()

    isolated_pm.add_profile("Immature3d", "", "windows", group="1. Sạch nhất", pages=["Theo dõi"])
    isolated_pm.profiles["Immature3d"].created_at = (today - dt.timedelta(days=3)).isoformat()

    isolated_pm.add_profile("WarmBotReady", "http://u:p@209.141.40.174:5497", "windows", group="4. Rủi ro cao", pages=["Theo dõi"])
    isolated_pm.profiles["WarmBotReady"].created_at = (today - dt.timedelta(days=5)).isoformat()
    _write_cookies_db(data_dir / "WarmBotReady" / "cookies.sqlite", sess_days_left=19)

    isolated_pm.add_profile("Risk5Ready", "", "windows", group="5. Nguy hiểm", pages=["Theo dõi"])
    isolated_pm.profiles["Risk5Ready"].created_at = (today - dt.timedelta(days=8)).isoformat()

    isolated_pm.add_profile("CC12Watch", dead_ep, "windows", group="2. Khá sạch", pages=["Theo dõi"])
    isolated_pm.profiles["CC12Watch"].created_at = (today - dt.timedelta(days=15)).isoformat()

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    p_list = list(isolated_pm.profiles.values())
    snap = mon.snapshot(p_list, set())

    unassigned = [
        "socks5://u:p@sp02-usa104.nguonproxy.com:10055",
        dead_ep,  # Frozen -> must be blocked and NOT suggested
        "http://u:p@209.141.40.174:5498",
    ]
    inc = build_incubation_recommendations(p_list, snap, unassigned)
    assert [c.name for c in inc.clean_ready] == ["Clean15d"]
    assert extract_proxy_endpoint(inc.clean_ready[0].suggested_proxy) == "usa104:10055"
    bot_names = [c.name for c in inc.bot_standby_ready]
    assert bot_names == ["WarmBotReady", "Risk5Ready"]
    assert "209.141.40.174:5498" in inc.bot_standby_ready[1].suggested_proxy
    assert [c.name for c in inc.immature_holding] == ["Immature3d"]
    assert any("CC12Watch" in w for w in inc.blocked_warnings)
    assert any("5496" in w for w in inc.blocked_warnings)

    # Verify 3-tab dialog switching (Keep-Alive -> Work -> Incubation -> Apply)
    dp = _DummyPage()
    applied = []
    assigned = []
    open_safe_schedule_dialog(
        dp,  # type: ignore[arg-type]
        p_list,
        snap,
        initial_cycle_days=12,
        initial_mode="keepalive",
        unassigned_proxies=unassigned,
        on_assign_proxy=lambda n, px: assigned.append((n, px)),
        on_apply_schedule=lambda pl: applied.append(pl),
        on_clear_schedule=lambda: None,
    )
    dlg = dp.dialogs[0]
    mode_row = dlg.content.content.controls[0]
    assert len(mode_row.controls) == 3

    # Switch to Work tab
    mode_row.controls[1].on_click(None)
    assert len(dlg.actions) == 3
    assert dlg.actions[-1].disabled is False

    # Switch to Incubation tab -> Apply disabled, Assign Port triggers callback
    mode_row.controls[2].on_click(None)
    assert dlg.actions[-1].disabled is True

    # Switch back to Keep-Alive and trigger Apply
    mode_row.controls[0].on_click(None)
    assert dlg.actions[-1].disabled is False
    dlg.actions[-1].on_click(None)
    assert len(applied) == 1
    assert applied[0].schedule_mode == "keepalive"
