"""The browser engine a profile runs on: stored, validated, and routed to its runner."""

import json
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.container import Container
from src.models.profile import Profile
from src.services.browser import cookies, fingerprint
from src.services.browser.process import runner_command
from src.services.monitor import MonitorService
from src.services.profile.manager import ProfileManager


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    # profiles.json, camoufox_data/ and logs/ are relative to the working directory.
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def client(workdir, monkeypatch):
    from src.services.browser import chrome_prefetch

    # Creating a Chrome profile must not start a real ~150 MB download in tests.
    monkeypatch.setattr(chrome_prefetch, "start", lambda log=None: False)
    # The token-less API only answers loopback Host headers.
    return TestClient(create_app(Container()), base_url="http://127.0.0.1")


def test_engine_defaults_to_camoufox_for_old_profiles(workdir):
    (workdir / "profiles.json").write_text(
        json.dumps({"old": {"name": "old", "proxy": None, "os_type": "macos"}}), encoding="utf-8"
    )
    assert ProfileManager().profiles["old"].engine == "camoufox"


def test_unknown_engine_in_profiles_file_falls_back(workdir):
    (workdir / "profiles.json").write_text(
        json.dumps({"x": {"name": "x", "os_type": "linux", "engine": "netscape"}}),
        encoding="utf-8",
    )
    assert ProfileManager().profiles["x"].engine == "camoufox"


def test_engine_survives_save_and_reload(workdir):
    ProfileManager().add_profile("c", "", "macos", engine="chrome")
    assert ProfileManager().profiles["c"].engine == "chrome"


def test_runner_command_follows_the_engine():
    camoufox = runner_command(Profile(name="a", os_type="windows"))
    chrome = runner_command(Profile(name="b", proxy="1.2.3.4:80", os_type="macos", engine="chrome"))
    assert camoufox[0] == chrome[0] == sys.executable
    assert camoufox[1].endswith("runner.py")
    assert chrome[1:3] == ["-m", "chrome_engine.runner"]
    # Same positional protocol for both runners.
    assert chrome[3:] == ["b", "1.2.3.4:80", "macos", "", ""]


def test_api_creates_and_reports_the_engine(client):
    r = client.post("/api/v1/profiles", json={"name": "c1", "os_type": "macos", "engine": "chrome"})
    assert r.status_code == 201
    assert r.json()["engine"] == "chrome"
    assert client.get("/api/v1/profiles/c1").json()["engine"] == "chrome"
    assert client.post("/api/v1/profiles", json={"name": "f1"}).json()["engine"] == "camoufox"


def test_api_rejects_unknown_engine(client):
    r = client.post("/api/v1/profiles", json={"name": "x", "engine": "netscape"})
    assert r.status_code == 400


def test_api_refuses_to_change_the_engine(client):
    client.post("/api/v1/profiles", json={"name": "c2", "engine": "chrome"})
    r = client.patch("/api/v1/profiles/c2", json={"engine": "camoufox"})
    assert r.status_code == 400
    # Sending the current value along with other edits is fine.
    r = client.patch("/api/v1/profiles/c2", json={"engine": "chrome", "proxy": "1.2.3.4:8080"})
    assert r.status_code == 200
    assert r.json()["engine"] == "chrome"


def test_fingerprint_summary_reads_the_chrome_persona(tmp_path):
    (tmp_path / fingerprint.CHROME_PERSONA_FILE).write_text(
        json.dumps({"seed": 7, "platform": "macos", "hardware_concurrency": 12}), encoding="utf-8"
    )
    assert fingerprint.summary(str(tmp_path)) == {
        "os": "macos",
        "platform": "MacIntel",
        "screen": None,
        "hardware_concurrency": 12,
    }
    assert fingerprint.reset(str(tmp_path))
    assert not (tmp_path / fingerprint.CHROME_PERSONA_FILE).exists()
    assert fingerprint.summary(str(tmp_path)) is None


def test_monitor_service_inspects_chrome_profile_disk_and_trust_score(tmp_path):
    data_dir = tmp_path / "camoufox_data"
    log_dir = tmp_path / "logs"
    pdir = data_dir / "chrome_live"
    (pdir / "Default" / "Network").mkdir(parents=True)
    (pdir / "Default" / "Cache").mkdir(parents=True)

    # 1. Write chrome_persona.json and tabs.json
    (pdir / "chrome_persona.json").write_text(
        json.dumps(
            {
                "seed": 4242,
                "platform": "windows",
                "platform_version": "19.0.0",
                "brand": "Chrome",
                "hardware_concurrency": 12,
            }
        ),
        encoding="utf-8",
    )
    (pdir / "tabs.json").write_text(
        json.dumps({"tabs": ["https://muse.ai/thread/8899aabb"]}), encoding="utf-8"
    )

    # 2. Write Chromium Default/Network/Cookies (datr created 15 days ago + hatch_gw + hatch_sess + ps_l)
    now = time.time()
    datr_created_chrome = cookies._unix_to_chrome_time(now - 15 * 86400)
    exp_chrome = cookies._unix_to_chrome_time(now + 25 * 86400)
    cookies_db = pdir / "Default" / "Network" / "Cookies"
    conn = sqlite3.connect(str(cookies_db))
    try:
        conn.executescript(cookies._CHROME_COOKIES_DDL)
        rows = [
            (".meta.com", "datr", "d1", datr_created_chrome, exp_chrome),
            (".facebook.com", "datr", "d1", datr_created_chrome, exp_chrome),
            (".instagram.com", "datr", "d1", datr_created_chrome, exp_chrome),
            (".facebook.com", "ps_l", "1", datr_created_chrome, exp_chrome),
            (".muse.ai", "hatch_gw", "gw_val", datr_created_chrome, exp_chrome),
            (".muse.ai", "hatch_sess", "sess_val", datr_created_chrome, exp_chrome),
        ]
        for host_k, cname, cval, ct, ex in rows:
            conn.execute(
                "INSERT INTO cookies (creation_utc, host_key, name, value, path, expires_utc, "
                "is_secure, is_httponly, last_access_utc) VALUES (?, ?, ?, ?, '/', ?, 1, 1, ?)",
                (ct, host_k, cname, cval, ex, ct),
            )
        conn.commit()
    finally:
        conn.close()

    # 3. Write Chromium Default/History (urls table)
    hist_db = pdir / "Default" / "History"
    conn_h = sqlite3.connect(str(hist_db))
    try:
        conn_h.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, visit_count INTEGER)")
        for idx, url in enumerate(
            [
                "https://muse.ai/thread/8899aabb",
                "https://www.nytimes.com/section/technology",
                "https://www.wikipedia.org/wiki/Browser",
                "https://www.amazon.com/dp/B08N5WRWNW",
            ],
            start=1,
        ):
            conn_h.execute("INSERT INTO urls VALUES (?, ?, 3)", (idx, url))
        conn_h.commit()
    finally:
        conn_h.close()

    mon = MonitorService(data_dir=str(data_dir), log_dir=str(log_dir))
    prof = Profile(
        name="chrome_live",
        proxy="http://user:pass@sp02-usa101.nguonproxy.com:10555",
        os_type="windows",
        timezone="America/New_York",
        locale="en-US",
        engine="chrome",
    )
    snap = mon.snapshot([prof], running_names=set())
    info = snap.profiles["chrome_live"]
    assert info.hatch_gw is True
    assert info.hatch_sess is True
    assert info.has_ps_l is True
    assert info.has_thread_url is True
    assert info.has_cache2 is True
    assert 14.5 <= info.datr_age_days <= 15.5
    assert info.datr_domains == 3
    assert info.history_pages == 4
    assert info.ext_domains == 3
    assert info.trust_score > 0
    disk = mon._inspect_profile_disk("chrome_live", is_running=False)
    assert disk["fp_hash"]
    assert "Win32|" in disk["fp_hw_sig"]
    assert "12 CPU" in disk["device_label"]


def test_prefetch_skips_an_installed_engine(monkeypatch):
    from src.services.browser import chrome_prefetch

    monkeypatch.setattr(chrome_prefetch, "is_installed", lambda: True)
    assert chrome_prefetch.start() is False


def test_prefetch_runs_once_at_a_time(monkeypatch):
    import threading

    from chrome_engine import fetch
    from src.services.browser import chrome_prefetch

    release = threading.Event()
    calls = []

    def fake_install():
        calls.append(1)
        release.wait(5)

    monkeypatch.setattr(chrome_prefetch, "is_installed", lambda: False)
    monkeypatch.setattr(fetch, "install", fake_install)
    logs = []
    assert chrome_prefetch.start(logs.append) is True
    assert chrome_prefetch.start(logs.append) is False
    release.set()
    chrome_prefetch._thread.join(5)
    assert calls == [1]
    assert len(logs) == 2  # downloading..., ready


def test_api_refuses_chrome_on_an_unsupported_host(client, monkeypatch):
    from src.services.browser import chrome_prefetch

    monkeypatch.setattr(chrome_prefetch, "unsupported_reason", lambda: "no build for darwin/x86_64")
    r = client.post("/api/v1/profiles", json={"name": "c3", "engine": "chrome"})
    assert r.status_code == 400
    assert "darwin/x86_64" in r.json()["detail"]
