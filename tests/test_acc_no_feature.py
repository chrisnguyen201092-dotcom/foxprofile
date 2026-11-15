import asyncio
import csv
import json
from types import SimpleNamespace

import flet as ft
import msgpack
import pytest
from fastapi.testclient import TestClient
from flet.messaging.protocol import (
    Message,
    MessageAction,
    RegisterClientResponseBody,
    configure_encode_object_for_msgpack,
)
from flet.messaging.session import Session
from flet.pubsub.pubsub_hub import PubSubHub

import src.api.auth as auth_mod
from src.api.app import create_app
from src.core.container import Container
from src.core.events import EventBus
from src.core.strings import STRINGS, get_string
from src.models.profile import Profile
from src.services.monitor import MonitorService
from src.services.profile import manager as pm_mod
from src.ui.app import App, _search_match_rank
from src.ui.components.profile_card import COLUMNS, build_header_row, build_profile_card
from src.ui.handlers import AppHandlers
from src.ui.state import AppState
from src.ui.theme.colors import COLORS
from src.utils.validation import normalize_acc_no, validate_acc_no


@pytest.fixture
def isolated_pm(tmp_path, monkeypatch):
    data_dir = tmp_path / "camoufox_data"
    profiles_file = tmp_path / "profiles.json"
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(profiles_file))
    return pm_mod.ProfileManager()


class FakeBL:
    def __init__(self) -> None:
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

    def stop_profile(self, name: str, timeout: float = 2.0) -> bool:
        self.running.discard(name)
        return True


class FakePS:
    def check_proxy(self, proxy: str):
        return True, "OK"


def _walk_controls(root: ft.Control) -> list[ft.Control]:
    out: list[ft.Control] = [root]
    content = getattr(root, "content", None)
    if isinstance(content, ft.Control):
        out.extend(_walk_controls(content))
    for attr in ("controls", "leading", "trailing", "title", "subtitle"):
        val = getattr(root, attr, None)
        if isinstance(val, ft.Control):
            out.extend(_walk_controls(val))
        elif isinstance(val, (list, tuple)):
            for child in val:
                if isinstance(child, ft.Control):
                    out.extend(_walk_controls(child))
    return out


# ---------------------------------------------------------------------------
# D2 — Boundary, normalization & validation tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", "1"),
        ("#1", "1"),
        ("  ## 07  ", "07"),
        (10, "10"),
        ("A1", "A1"),
        ("VIP-2", "VIP-2"),
        ("", None),
        ("   ", None),
        ("#", None),
        ("##  ", None),
        (None, None),
        (True, None),
        (False, None),
    ],
)
def test_normalize_acc_no_cases(raw, expected):
    assert normalize_acc_no(raw) == expected


def test_validate_acc_no_boundaries():
    assert validate_acc_no("") == (True, "")
    assert validate_acc_no("#01") == (True, "")
    assert validate_acc_no("123456789012") == (True, "")
    assert validate_acc_no("#123456789012") == (True, "")

    ok_long, err_long = validate_acc_no("1234567890123")
    assert not ok_long
    assert err_long == get_string("validation_acc_no_too_long")

    for bad in ("1\n2", "1\t2", "1\x00"):
        ok_bad, err_bad = validate_acc_no(bad)
        assert not ok_bad
        assert err_bad == get_string("validation_acc_no_invalid")


# ---------------------------------------------------------------------------
# D4 — Persistence, Trash, Backup, CSV & ZIP lifecycle
# ---------------------------------------------------------------------------


def test_profile_manager_acc_no_persistence_and_lifecycle(isolated_pm, tmp_path):
    assert isolated_pm.add_profile("Kayla", "", "windows", acc_no="#01")
    assert isolated_pm.add_profile("Monica", "", "windows", acc_no="2")
    assert isolated_pm.add_profile("Unnumbered", "", "windows")

    # Reload from disk
    reloaded = pm_mod.ProfileManager()
    assert reloaded.profiles["Kayla"].acc_no == "01"
    assert reloaded.profiles["Monica"].acc_no == "2"
    assert reloaded.profiles["Unnumbered"].acc_no is None

    # update_profile preserves acc_no when new_acc_no is omitted (_UNSET)
    assert reloaded.update_profile("Kayla", "Kayla", "", "windows", new_notes="updated note")
    assert reloaded.profiles["Kayla"].acc_no == "01"
    assert reloaded.profiles["Kayla"].notes == "updated note"

    # update_profile rejects invalid >12 chars BEFORE renaming or mutating
    assert not reloaded.update_profile(
        "Kayla",
        "KaylaRenamed",
        "",
        "windows",
        new_acc_no="1234567890123",
    )
    assert "Kayla" in reloaded.profiles
    assert "KaylaRenamed" not in reloaded.profiles
    assert (tmp_path / "camoufox_data" / "Kayla").exists()

    # update_profile can change and clear acc_no
    assert reloaded.update_profile("Kayla", "Kayla", "", "windows", new_acc_no="#15")
    assert reloaded.profiles["Kayla"].acc_no == "15"
    assert reloaded.update_profile("Kayla", "Kayla", "", "windows", new_acc_no="")
    assert reloaded.profiles["Kayla"].acc_no is None

    # update_profile_acc_no quick helper
    assert reloaded.update_profile_acc_no("Kayla", "#09")
    assert reloaded.profiles["Kayla"].acc_no == "09"
    assert not reloaded.update_profile_acc_no("Kayla", "1234567890123")
    assert reloaded.profiles["Kayla"].acc_no == "09"
    assert reloaded.update_profile_acc_no("Kayla", "#")
    assert reloaded.profiles["Kayla"].acc_no is None
    assert reloaded.update_profile_acc_no("Kayla", "09")
    assert not reloaded.update_profile_acc_no("DoesNotExist", "1")

    # Trash + Restore preserves acc_no
    assert reloaded.trash_profile("Kayla")
    assert "Kayla" not in reloaded.profiles
    assert reloaded.restore_profile("Kayla")
    assert reloaded.profiles["Kayla"].acc_no == "09"

    # Backup inspect + restore preserves acc_no and detects acc_no-only diff
    backup_path = tmp_path / "manual_backup.json"
    backup_path.write_text(
        json.dumps({k: v.to_dict() for k, v in reloaded.profiles.items()}),
        encoding="utf-8",
    )
    reloaded.update_profile_acc_no("Kayla", "99")
    meta = reloaded.inspect_config_backup(backup_path)
    assert meta is not None
    assert meta["changed"] == 1
    ok_restore, restored_cnt = reloaded.restore_config_backup(backup_path)
    assert ok_restore and restored_cnt >= 1
    assert reloaded.profiles["Kayla"].acc_no == "09"

    # add_profile rejects invalid >12 chars acc_no cleanly
    assert not reloaded.add_profile("TooLongAccNo", "", "windows", acc_no="1234567890123")
    assert "TooLongAccNo" not in reloaded.profiles


def test_csv_and_zip_acc_no_roundtrip(isolated_pm, tmp_path):
    assert isolated_pm.add_profile("Alpha", "", "windows", acc_no="#04")
    assert isolated_pm.add_profile("Beta", "", "windows", acc_no="12")

    csv_path = tmp_path / "export.csv"
    assert isolated_pm.export_profiles_csv(str(csv_path)) == 2
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    by_name = {r["name"]: r for r in rows}
    assert by_name["Alpha"]["acc_no"] == "04"
    assert by_name["Beta"]["acc_no"] == "12"

    # Modify acc_no and add a new row, then re-import
    isolated_pm.update_profile_acc_no("Alpha", "88")
    created, updated, skipped = isolated_pm.import_profiles_csv(str(csv_path))
    assert (created, updated, skipped) == (0, 2, 0)
    assert isolated_pm.profiles["Alpha"].acc_no == "04"

    # CSV without acc_no column must NOT wipe existing acc_no
    no_acc_csv = tmp_path / "legacy.csv"
    with no_acc_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "os_type", "notes"])
        w.writeheader()
        w.writerow({"name": "Alpha", "os_type": "windows", "notes": "legacy"})
    isolated_pm.import_profiles_csv(str(no_acc_csv))
    assert isolated_pm.profiles["Alpha"].acc_no == "04"
    assert isolated_pm.profiles["Alpha"].notes == "legacy"

    # ZIP export + import preserves acc_no
    export_dir = tmp_path / "zips"
    export_dir.mkdir(parents=True, exist_ok=True)
    ok_zip, zip_file = isolated_pm.export_profile("Beta", str(export_dir), include_data=False)
    assert ok_zip
    isolated_pm.delete_profile("Beta")
    assert "Beta" not in isolated_pm.profiles
    ok_imp, imp_name = isolated_pm.import_profile(zip_file)
    assert ok_imp and imp_name == "Beta"
    assert isolated_pm.profiles["Beta"].acc_no == "12"


# ---------------------------------------------------------------------------
# D1/D3 — FastAPI & Web Panel E2E tests
# ---------------------------------------------------------------------------


def test_api_acc_no_crud_and_web_panel(isolated_pm, tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "API_TOKEN", "")
    c = Container()
    c._instances["pm"] = isolated_pm
    c._instances["bl"] = FakeBL()
    c._instances["ps"] = FakePS()
    c._instances["eb"] = EventBus()
    c._instances["mon"] = MonitorService(
        data_dir=str(tmp_path / "camoufox_data"),
        log_dir=str(tmp_path / "logs"),
    )
    client = TestClient(create_app(c), base_url="http://127.0.0.1:8000")

    # POST with leading '#'
    r_create = client.post(
        "/api/v1/profiles",
        json={"name": "WebAcc", "os_type": "windows", "acc_no": "#03"},
    )
    assert r_create.status_code == 201
    assert r_create.json()["acc_no"] == "03"

    # GET list and monitor overview expose acc_no
    r_list = client.get("/api/v1/profiles")
    assert r_list.status_code == 200
    assert r_list.json()["profiles"][0]["acc_no"] == "03"

    r_mon = client.get("/api/v1/profiles/monitor/overview")
    assert r_mon.status_code == 200
    assert r_mon.json()["profiles"][0]["acc_no"] == "03"

    # PATCH omitting acc_no preserves existing value
    r_patch_omit = client.patch("/api/v1/profiles/WebAcc", json={"notes": "kept"})
    assert r_patch_omit.status_code == 200
    assert r_patch_omit.json()["acc_no"] == "03"
    assert r_patch_omit.json()["notes"] == "kept"

    # PATCH updating acc_no normalizes '#12' -> '12'
    r_patch_set = client.patch("/api/v1/profiles/WebAcc", json={"acc_no": "#12"})
    assert r_patch_set.status_code == 200
    assert r_patch_set.json()["acc_no"] == "12"

    # PATCH clearing acc_no with '' sets None
    r_patch_clear = client.patch("/api/v1/profiles/WebAcc", json={"acc_no": ""})
    assert r_patch_clear.status_code == 200
    assert r_patch_clear.json()["acc_no"] is None

    # PATCH invalid >12 chars returns 422
    r_patch_bad = client.patch("/api/v1/profiles/WebAcc", json={"acc_no": "1234567890123"})
    assert r_patch_bad.status_code == 422

    # Web Panel serves borderless inline acc_no styling & markers (Option 2)
    r_web = client.get("/")
    assert r_web.status_code == 200
    for marker in (
        ".acc-pill",
        ".acc-pill.ghost",
        ".acc-input",
        "background: transparent",
        "acc_dblclick_tip",
        "acc_no_asc",
        "editingAccName",
    ):
        assert marker in r_web.text
    assert ".acc-box" not in r_web.text


# ---------------------------------------------------------------------------
# D1/D3 — Flet Desktop Card, Handlers, App Sort & Search
# ---------------------------------------------------------------------------


def test_flet_profile_card_and_header_acc_no():
    assert list(COLUMNS.keys()) == ["select", "name", "device", "proxy", "geo", "actions"]

    hdr_idle = build_header_row(
        all_selected=False,
        any_selected=False,
        on_select_all=lambda _: None,
        sort_by="custom",
        on_sort_acc_no=lambda: None,
    )
    idle_acc_hdrs = [
        c
        for c in _walk_controls(hdr_idle.content.controls[1])
        if isinstance(c, ft.Container)
        and c.on_click is not None
        and any(isinstance(x, ft.Text) and x.value == get_string("col_acc_no") for x in _walk_controls(c))
    ]
    assert len(idle_acc_hdrs) == 1
    assert idle_acc_hdrs[0].width == 24
    assert idle_acc_hdrs[0].height == 21

    sort_clicks = []
    hdr = build_header_row(
        all_selected=False,
        any_selected=False,
        on_select_all=lambda _: None,
        sort_by="acc_no_asc",
        on_sort_acc_no=lambda: sort_clicks.append("clicked"),
    )
    assert len(hdr.content.controls) == 6
    hdr_nodes = _walk_controls(hdr.content.controls[1])
    acc_hdr_texts = [
        c for c in hdr_nodes if isinstance(c, ft.Text) and c.value == get_string("col_acc_no")
    ]
    assert len(acc_hdr_texts) == 1
    clickable_hdrs = [
        c
        for c in hdr_nodes
        if isinstance(c, ft.Container)
        and c.on_click is not None
        and any(isinstance(x, ft.Text) and x.value == get_string("col_acc_no") for x in _walk_controls(c))
    ]
    assert len(clickable_hdrs) == 1
    assert clickable_hdrs[0].height == 21
    assert clickable_hdrs[0].width is None
    clickable_hdrs[0].on_click(SimpleNamespace(control=clickable_hdrs[0]))
    assert sort_clicks == ["clicked"]

    # Non-editing numbered card with draggable=True: GestureDetector & expand must NOT be inside ReorderableDragHandle
    started: list[str] = []
    p_num = Profile(name="AccFive", acc_no="5")
    card_num = build_profile_card(
        p_num,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        draggable=True,
        on_start_edit_acc_no=started.append,
    )
    assert len(card_num.content.controls) == 6
    name_cell = card_num.content.controls[1]
    num_texts = [c for c in _walk_controls(name_cell) if isinstance(c, ft.Text) and c.value == "#5"]
    assert len(num_texts) == 1
    assert num_texts[0].size == 12.5
    assert num_texts[0].weight == ft.FontWeight.W_600
    assert num_texts[0].color == COLORS["text_dim"]
    nodes_num = _walk_controls(card_num)
    drag_handles = [c for c in nodes_num if isinstance(c, ft.ReorderableDragHandle)]
    for dh in drag_handles:
        dh_nodes = _walk_controls(dh)
        assert not any(isinstance(x, ft.GestureDetector) for x in dh_nodes)
        assert not bool(getattr(dh.content, "expand", False))
    for dh in [c for c in _walk_controls(name_cell) if isinstance(c, ft.ReorderableDragHandle)]:
        assert not any(bool(getattr(x, "expand", False)) for x in _walk_controls(dh))
    gds = [c for c in nodes_num if isinstance(c, ft.GestureDetector) and c.on_double_tap is not None]
    assert len(gds) == 1
    assert isinstance(gds[0].content, ft.Container)
    assert gds[0].content.bgcolor is None
    assert gds[0].content.border is None
    assert card_num.on_hover is None
    gds[0].on_double_tap(SimpleNamespace(control=gds[0]))
    assert started == ["AccFive"]

    # Unnumbered card hides placeholder at rest and forwards pure hover callbacks without mutating controls
    started_empty: list[str] = []
    hover_events: list[tuple[str, bool]] = []
    p_empty = Profile(name="AccNone", acc_no=None)
    card_empty = build_profile_card(
        p_empty,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        on_start_edit_acc_no=started_empty.append,
        row_hovered=False,
        on_row_hover=lambda n, h: hover_events.append((n, h)),
    )
    empty_name_nodes = _walk_controls(card_empty.content.controls[1])
    assert not any(isinstance(c, ft.Text) and c.value == "—" for c in empty_name_nodes)
    ghost_wraps = [
        c
        for c in empty_name_nodes
        if isinstance(c, ft.Container)
        and c.opacity == 0.0
        and any(isinstance(x, ft.Text) and x.value == "+#" for x in _walk_controls(c))
    ]
    assert len(ghost_wraps) == 1
    ghost_wrap = ghost_wraps[0]
    ghost_texts = [c for c in _walk_controls(ghost_wrap) if isinstance(c, ft.Text) and c.value == "+#"]
    assert len(ghost_texts) == 1
    assert ghost_texts[0].size == 11
    assert ghost_texts[0].weight == ft.FontWeight.W_600
    assert ghost_texts[0].color == COLORS["text_dim"]
    assert card_empty.on_hover is not None
    card_empty.on_hover(SimpleNamespace(data=True))
    card_empty.on_hover(SimpleNamespace(data="false"))
    assert hover_events == [("AccNone", True), ("AccNone", False)]
    assert ghost_wrap.opacity == 0.0

    # Declarative hovered state sets opacity=1.0 without runtime mutation
    card_empty_hovered = build_profile_card(
        p_empty,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        on_start_edit_acc_no=started_empty.append,
        row_hovered=True,
        on_row_hover=lambda n, h: hover_events.append((n, h)),
    )
    hovered_ghosts = [
        c
        for c in _walk_controls(card_empty_hovered.content.controls[1])
        if isinstance(c, ft.Container)
        and c.opacity == 1.0
        and c.animate_opacity == 120
        and any(isinstance(x, ft.Text) and x.value == "+#" for x in _walk_controls(c))
    ]
    assert len(hovered_ghosts) == 1
    ghost_gds = [
        c
        for c in _walk_controls(ghost_wrap)
        if isinstance(c, ft.GestureDetector) and c.on_double_tap is not None
    ]
    assert len(ghost_gds) == 1
    assert isinstance(ghost_gds[0].content, ft.Container)
    assert ghost_gds[0].content.bgcolor is None
    assert ghost_gds[0].content.border is None
    ghost_gds[0].on_double_tap(SimpleNamespace(control=ghost_gds[0]))
    assert started_empty == ["AccNone"]

    # Editing mode renders compact inline TextField inside 'name' cell without max_length counter overflow
    saved: list[tuple[str, str]] = []
    card_edit = build_profile_card(
        p_num,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        editing_acc_no=True,
        on_save_acc_no=lambda n, v: saved.append((n, v)),
    )
    fields = [c for c in _walk_controls(card_edit.content.controls[1]) if isinstance(c, ft.TextField)]
    assert len(fields) == 1
    assert fields[0].width == 46
    assert fields[0].height == 22
    assert fields[0].value == "5"
    assert fields[0].max_length is None
    fields[0].value = "#14"
    fields[0].on_submit(SimpleNamespace(control=fields[0]))
    assert saved == [("AccFive", "#14")]


def test_app_handlers_acc_no_state_and_idempotency(isolated_pm, tmp_path):
    assert isolated_pm.add_profile("P1", "", "windows", acc_no="1")
    state = AppState()
    logs: list[str] = []
    refreshes = [0]
    mon = MonitorService(data_dir=str(tmp_path / "camoufox_data"), log_dir=str(tmp_path / "logs"))
    h = AppHandlers(
        pm=isolated_pm,
        bl=FakeBL(),  # type: ignore[arg-type]
        ps=FakePS(),  # type: ignore[arg-type]
        state=state,
        get_page=lambda: None,
        get_refs=lambda: None,
        log_fn=logs.append,
        refresh_fn=lambda: refreshes.__setitem__(0, refreshes[0] + 1),
        get_page_profiles=lambda: ([], isolated_pm.list_profiles(), 1),
        monitor=mon,
    )

    h.on_start_edit_note("P1")
    assert state.editing_note == "P1"
    h.on_start_edit_acc_no("P1")
    assert state.editing_acc_no == "P1"
    assert state.editing_note is None

    # Starting email edit clears editing_acc_no
    h.on_start_edit_email("P1")
    assert state.editing_acc_no is None
    assert state.editing_email == "P1"

    # Submit + immediate blur only saves once (idempotent guard)
    h.on_start_edit_acc_no("P1")
    logs.clear()
    h.on_save_acc_no("P1", "#07")
    h.on_save_acc_no("P1", "#07")
    assert isolated_pm.profiles["P1"].acc_no == "07"
    assert len(logs) == 1

    # Invalid input keeps previous value and logs validation error
    h.on_start_edit_acc_no("P1")
    logs.clear()
    h.on_save_acc_no("P1", "1234567890123")
    assert isolated_pm.profiles["P1"].acc_no == "07"
    assert any(get_string("validation_acc_no_too_long") in msg for msg in logs)


def test_app_sort_search_and_escape_acc_no(isolated_pm, tmp_path):
    assert isolated_pm.add_profile("Zed", "", "windows", acc_no="10")
    assert isolated_pm.add_profile("Amy", "", "windows", acc_no="2")
    assert isolated_pm.add_profile("Bob", "", "windows", acc_no="1")
    assert isolated_pm.add_profile("NoNumB", "", "windows")
    assert isolated_pm.add_profile("NoNumA", "", "windows", pinned=True)

    c = Container()
    c._instances["pm"] = isolated_pm
    c._instances["bl"] = FakeBL()
    c._instances["ps"] = FakePS()
    c._instances["eb"] = EventBus()
    c._instances["mon"] = MonitorService(
        data_dir=str(tmp_path / "camoufox_data"),
        log_dir=str(tmp_path / "logs"),
    )
    app = App(container=c)
    app.state.items_per_page = 0

    app.state.sort_by = "acc_no_asc"
    ordered_asc = [p.name for p in app._get_page_profiles()[0]]
    assert ordered_asc == ["NoNumA", "Bob", "Amy", "Zed", "NoNumB"]

    app.state.sort_by = "acc_no_desc"
    ordered_desc = [p.name for p in app._get_page_profiles()[0]]
    assert ordered_desc == ["NoNumA", "Zed", "Amy", "Bob", "NoNumB"]

    # Search matches #10 and 10 cleanly
    assert _search_match_rank(isolated_pm.profiles["Zed"], "#10")[0] == 0
    assert _search_match_rank(isolated_pm.profiles["Zed"], "10")[0] == 0
    assert _search_match_rank(isolated_pm.profiles["Bob"], "#1")[0] == 0

    # Escape cancels active editing_acc_no
    refreshed = []
    app._refresh_profiles = lambda **_: refreshed.append(True)  # type: ignore[method-assign]
    app.state.editing_acc_no = "Bob"
    app._handle_key_event(SimpleNamespace(key="Escape", ctrl=False, meta=False, alt=False, shift=False))
    assert app.state.editing_acc_no is None
    assert refreshed == [True]


def test_strings_parity_has_acc_no_keys():
    assert set(STRINGS["en"]) == set(STRINGS["vi"])
    expected_keys = {
        "col_acc_no",
        "tooltip_sort_acc_no",
        "tooltip_edit_acc_no",
        "tooltip_add_acc_no",
        "acc_no_optional",
        "acc_no_hint",
        "quick_saved_acc_no",
        "quick_cleared_acc_no",
        "validation_acc_no_too_long",
        "validation_acc_no_invalid",
        "sort_cat_acc_no",
        "sort_dir_acc_no_asc",
        "sort_dir_acc_no_desc",
        "sort_acc_no_asc",
        "sort_acc_no_desc",
    }
    assert expected_keys <= set(STRINGS["en"])


def test_flet_session_frozen_reconciliation_hover_and_edit_acc_no(isolated_pm, tmp_path):
    assert isolated_pm.add_profile("Kayla", "", "windows", acc_no="1")
    assert isolated_pm.add_profile("Monica", "", "windows")
    assert isolated_pm.add_profile("Sienna", "", "windows")

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

    def _ghost_opacity_for(app_obj: App, profile_name: str) -> float | None:
        cards = [
            c
            for c in app_obj.refs.profile_list_area.controls
            if getattr(c, "key", None) == profile_name
        ]
        if not cards:
            return None
        for node in _walk_controls(cards[0].content.controls[1]):
            if (
                isinstance(node, ft.Container)
                and isinstance(node.content, ft.GestureDetector)
                and any(isinstance(x, ft.Text) and x.value == "+#" for x in _walk_controls(node))
            ):
                return node.opacity
        return None

    async def _run_session_flow() -> None:
        loop = asyncio.get_running_loop()
        c = Container()
        c._instances["pm"] = isolated_pm
        c._instances["bl"] = FakeBL()
        c._instances["ps"] = FakePS()
        c._instances["eb"] = EventBus()
        c._instances["mon"] = MonitorService(
            data_dir=str(tmp_path / "camoufox_data"),
            log_dir=str(tmp_path / "logs"),
        )

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
        await sess.connect(conn)

        app = App(container=c)
        try:
            app._main(sess.page)
            initial_cards = {
                getattr(ctrl, "key", None): ctrl
                for ctrl in app.refs.profile_list_area.controls
                if getattr(ctrl, "key", None) in {"Kayla", "Monica", "Sienna"}
            }
            # Subsequent unchanged refresh reuses exact mounted row instances instead of rebuilding
            app._refresh_profiles(reuse_snapshot=True)

            row_cards = [
                ctrl
                for ctrl in app.refs.profile_list_area.controls
                if getattr(ctrl, "key", None) in {"Kayla", "Monica", "Sienna"}
            ]
            assert len(row_cards) == 3
            assert all(ctrl is initial_cards[ctrl.key] for ctrl in row_cards)
            assert all(getattr(card, "on_hover", None) is None for card in row_cards)
            assert _ghost_opacity_for(app, "Monica") == 0.30
            assert _ghost_opacity_for(app, "Sienna") == 0.30

            # Live App rows keep hover disabled so moving across rows cannot rebuild widgets mid-click
            hover_targets = [
                ctrl for ctrl in list(sess.index.values()) if getattr(ctrl, "on_hover", None) is not None
            ]
            assert hover_targets == []

            # Double-tap Monica's '+#' ghost, survive a frozen refresh while editing, submit '#15'
            monica_row = next(
                ctrl for ctrl in app.refs.profile_list_area.controls if getattr(ctrl, "key", None) == "Monica"
            )
            monica_gds = [
                node
                for node in _walk_controls(monica_row)
                if isinstance(node, ft.GestureDetector) and node.on_double_tap is not None
            ]
            assert len(monica_gds) == 1
            await sess.dispatch_event(monica_gds[0]._i, "double_tap", None)
            assert app.state.editing_acc_no == "Monica"

            app._refresh_profiles(reuse_snapshot=True)
            acc_fields = [
                ctrl
                for ctrl in list(sess.index.values())
                if isinstance(ctrl, ft.TextField) and getattr(ctrl, "hint_text", None) == "#"
            ]
            assert len(acc_fields) == 1
            assert hasattr(acc_fields[0], "_frozen")

            sess.apply_patch(acc_fields[0]._i, {"value": "#15"})
            await sess.dispatch_event(acc_fields[0]._i, "submit", "#15")
            assert app.state.editing_acc_no is None
            assert isolated_pm.profiles["Monica"].acc_no == "15"

            crashes = [m.body.message for m in conn.messages if m.action == MessageAction.SESSION_CRASHED]
            assert crashes == []
        finally:
            app._shutting_down = True

    asyncio.run(_run_session_flow())

