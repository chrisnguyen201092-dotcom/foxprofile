import importlib
import io
import os
import subprocess
import sys
import threading
import time

import pytest

from src.models.profile import Profile
from src.services.browser import launcher as launcher_mod
from src.services.browser.launcher import BrowserLauncher, ProfileBusyError
from src.ui.actions.browser import launch_or_stop
from src.ui.state import AppState


class FakeProc:
    def poll(self):
        return None


@pytest.fixture
def bl(monkeypatch):
    spawned = []

    def spawn(profile):
        spawned.append(profile.name)
        return FakeProc()

    monkeypatch.setattr(launcher_mod, "spawn_browser", spawn)
    monkeypatch.setattr(launcher_mod, "wait_for_exit", lambda *a: None)
    b = BrowserLauncher()
    monkeypatch.setattr(b, "_monitor_process", lambda *a: None)
    b.spawned = spawned
    yield b
    # Fake processes must not reach terminate() at interpreter exit.
    b._active_sessions.clear()


def test_second_launch_returns_false_without_callbacks(bl):
    calls = []
    assert bl.start_thread(Profile(name="p"), lambda m: None) is True
    started = bl.start_thread(
        Profile(name="p"),
        lambda m: None,
        on_start=lambda: calls.append("start"),
        on_stop=lambda: calls.append("stop"),
    )
    assert started is False
    assert calls == []
    assert bl.spawned == ["p"]


def test_concurrent_launches_spawn_one_browser(bl, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_spawn(profile):
        entered.set()
        release.wait(5)
        bl.spawned.append(profile.name)
        return FakeProc()

    monkeypatch.setattr(launcher_mod, "spawn_browser", slow_spawn)
    first = threading.Thread(target=bl.start_thread, args=(Profile(name="p"), lambda m: None))
    first.start()
    assert entered.wait(5)
    try:
        with pytest.raises(ProfileBusyError):
            bl.start_thread(Profile(name="p"), lambda m: None)
    finally:
        release.set()
        first.join(5)
    assert bl.spawned == ["p"]
    assert bl.is_running("p")


def test_failed_spawn_releases_the_profile(bl, monkeypatch):
    def broken(profile):
        raise OSError("no runner")

    monkeypatch.setattr(launcher_mod, "spawn_browser", broken)
    stopped = []
    assert bl.start_thread(Profile(name="p"), lambda m: None, on_stop=lambda: stopped.append(1))
    assert stopped == [1]
    with bl.exclusive("p"):
        pass


class _Profiles:
    def __init__(self, *names):
        self.profiles = {n: Profile(name=n) for n in names}


def test_ui_click_does_not_stay_loading_when_launched_elsewhere(bl, monkeypatch):
    state = AppState()
    # The API launched the profile between the UI's is_running check and its
    # start_thread call.
    monkeypatch.setattr(bl, "is_running", lambda name: False)
    bl.start_thread(Profile(name="p"), lambda m: None)

    launch_or_stop("p", _Profiles("p"), bl, state, lambda m: None)

    assert not state.is_loading("p")
    assert "p" not in state.failed
    assert bl.spawned == ["p"]


def test_a_raising_callback_does_not_leave_the_profile_busy(bl):
    def boom():
        raise RuntimeError("UI gone")

    with pytest.raises(RuntimeError):
        bl.start_thread(Profile(name="p"), lambda m: None, on_start=boom)
    with bl.exclusive("p"):
        pass
    assert bl.spawned == []


def test_an_old_session_ending_keeps_the_newer_launch(bl, monkeypatch):
    exits = []
    monkeypatch.setattr(
        launcher_mod, "wait_for_exit", lambda proc, name, notify: exits.append(notify)
    )
    bl.start_thread(Profile(name="p"), lambda m: None)
    # The first browser is gone but its exit callback has not run yet.
    bl._active_sessions.clear()
    bl._stop_notifiers.clear()
    bl.start_thread(Profile(name="p"), lambda m: None)

    exits[0]()

    assert bl.is_running("p")
    assert bl.spawned == ["p", "p"]


def test_api_launch_answers_409_when_the_browser_appeared_meanwhile(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core.container import Container

    monkeypatch.chdir(tmp_path)
    container = Container()
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    assert client.post("/api/v1/profiles", json={"name": "p"}).status_code in (200, 201)
    monkeypatch.setattr(container.browser_launcher, "is_running", lambda name: False)
    monkeypatch.setattr(container.browser_launcher, "start_thread", lambda *a, **k: False)

    r = client.post("/api/v1/browser/p/launch")

    assert r.status_code == 409
    assert r.json()["detail"] == "Browser already running"


class _FakeStdoutProc:
    def __init__(self, pid: int, lines: list[str]) -> None:
        self.pid = pid
        self.stdout = io.StringIO("".join(f"{line}\n" for line in lines))
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> int:
        self.terminated = True
        return 0


def test_config_sets_single_thread_openblas_env(monkeypatch):
    import src.core.config as config_mod
    from src.services.browser import process

    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    importlib.reload(config_mod)

    assert os.environ.get("OPENBLAS_NUM_THREADS") == "1"
    assert os.environ.get("OMP_NUM_THREADS") == "1"

    captured = {}
    monkeypatch.setattr(process.subprocess, "Popen", lambda args, **kw: captured.update(kw))
    process.spawn_browser(Profile(name="p"))
    assert captured["env"].get("OPENBLAS_NUM_THREADS") == "1"
    assert captured["env"].get("OMP_NUM_THREADS") == "1"


def test_browser_started_triggers_delayed_wrapper_trim(monkeypatch):
    proc = _FakeStdoutProc(12345, ["CONTROL:9999:tok", "BROWSER_STARTED"])
    trimmed: list[int] = []
    called = threading.Event()

    monkeypatch.setattr(launcher_mod, "spawn_browser", lambda _profile: proc)
    monkeypatch.setattr(launcher_mod, "wait_for_exit", lambda *a: None)
    monkeypatch.setattr(launcher_mod, "_WRAPPER_TRIM_DELAY_SECONDS", 0.01)

    def fake_trim(pid: int) -> int:
        trimmed.append(pid)
        called.set()
        return 1

    monkeypatch.setattr(launcher_mod, "trim_runner_wrapper_memory", fake_trim)
    b = BrowserLauncher()
    try:
        assert b.start_thread(Profile(name="p"), lambda _m: None) is True
        ok, err = b.wait_for_launch("p", timeout=2.0)
        assert ok is True
        assert err is None
        assert called.wait(timeout=2.0)
        assert trimmed == [12345]
    finally:
        b._active_sessions.clear()


def test_wrapper_trim_skipped_when_stopped_before_delay(monkeypatch):
    # Case 1: stopped cleanly before the trim delay expires
    proc_stop = _FakeStdoutProc(22345, ["CONTROL:9999:tok", "BROWSER_STARTED"])
    trimmed: list[int] = []

    monkeypatch.setattr(launcher_mod, "spawn_browser", lambda _profile: proc_stop)
    monkeypatch.setattr(launcher_mod, "wait_for_exit", lambda *a: None)
    monkeypatch.setattr(launcher_mod, "terminate", lambda p, *_a: p.terminate())
    monkeypatch.setattr(launcher_mod, "_WRAPPER_TRIM_DELAY_SECONDS", 2.0)
    monkeypatch.setattr(
        launcher_mod,
        "trim_runner_wrapper_memory",
        lambda pid: (trimmed.append(pid), 1)[1],
    )
    b = BrowserLauncher()
    try:
        assert b.start_thread(Profile(name="p1"), lambda _m: None) is True
        ok, _ = b.wait_for_launch("p1", timeout=2.0)
        assert ok is True
        assert b.stop_profile("p1") is True
        time.sleep(0.05)
        assert trimmed == []

        # Case 2: launch fails before BROWSER_STARTED
        proc_fail = _FakeStdoutProc(22346, ["LAUNCH_FAILED: bad proxy"])
        monkeypatch.setattr(launcher_mod, "spawn_browser", lambda _profile: proc_fail)
        monkeypatch.setattr(launcher_mod, "_WRAPPER_TRIM_DELAY_SECONDS", 0.01)
        assert b.start_thread(Profile(name="p2"), lambda _m: None) is True
        ok_fail, err_fail = b.wait_for_launch("p2", timeout=2.0)
        assert ok_fail is False
        assert "bad proxy" in (err_fail or "")
        time.sleep(0.05)
        assert trimmed == []
    finally:
        b._active_sessions.clear()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 working set trimming")
def test_trim_runner_wrapper_memory_win32_safety():
    from src.services.browser.window_title import trim_runner_wrapper_memory

    assert trim_runner_wrapper_memory(0) == 0
    assert trim_runner_wrapper_memory(-1) == 0
    assert trim_runner_wrapper_memory(True) == 0  # type: ignore[arg-type]
    assert trim_runner_wrapper_memory(os.getpid()) == 0
    assert trim_runner_wrapper_memory(999_999_999) == 0

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; buf = bytearray(20 * 1024 * 1024); print('READY', flush=True); sys.stdin.read()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        assert child.poll() is None
        trimmed = trim_runner_wrapper_memory(child.pid)
        assert 1 <= trimmed <= 2
    finally:
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()
        child.wait(timeout=5)
