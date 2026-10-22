"""Read-only monitoring service integrated from monitor_dashboard.py.

Provides mtime-cached inspection of:
- Session history & cooldown time (`last_closed`, `cooldown_mins`, `today_minutes`)
- Proxy port sharing, active proxy collisions, and configurable IP cooldown warnings
- Cookie/session readiness (`hatch_gw`, `hatch_sess`, `sess_exp`) via immutable read-only SQLite
- Saved tab check (`/thread/` in `tabs.json`) and token refresh burst detection (`cache2/entries`)
- Pre-launch 4-layer safety guard (`check_prelaunch` / `check_bulk_prelaunch`)
- Disk free space (`free_gb`)
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from ..core.config import DATA_DIR, LOG_DIR
from ..core.strings import get_string
from ..models.profile import Profile

PORT_COOLDOWN_MINUTES = 15
TOKEN_BURST_THRESHOLD = 12

_RE_PORT = re.compile(r":(\d{2,5})(?:@|$|\s|/)")
_RE_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(?:\||-)\s+(?P<lvl>\w+)\s+(?:\||-)\s+"
    r"(?P<mod>[\w\.]+)\s+(?:\||-)\s+(?P<msg>.*)$"
)
_RE_BRACKET_PROFILE = re.compile(r"^\[(?P<prof>[^\]]+)\]\s*(?P<rest>.*)$")
_RE_STOPPING = re.compile(r"^Stopping browser for (?P<prof>.+?)\.\.\.$")
_RE_TERMINATED = re.compile(r"^Browser process for (?P<prof>.+?) terminated\.$")
_RE_SESSION_ENDED = re.compile(r"^(?:Session ended|Phi\u00ean k\u1ebft th\u00fac):\s*(?P<prof>.+)$")


def extract_proxy_port(proxy_str: str | None) -> str:
    if not proxy_str:
        return ""
    m = _RE_PORT.search(proxy_str.strip())
    return f":{m.group(1)}" if m else ""


@dataclass
class PreLaunchWarning:
    code: str
    level: str  # "critical" | "warning"
    message: str


@dataclass
class ProfileMonitorInfo:
    name: str
    port: str = ""
    shared_count: int = 1
    last_closed: dt.datetime | None = None
    cooldown_mins: int | None = None
    today_minutes: int = 0
    busy_by: list[str] = field(default_factory=list)
    port_cooldown_by: tuple[str, int] | None = None  # (other_profile_name, minutes_left)
    has_thread_url: bool = False
    tab_count: int = 0
    hatch_gw: bool = False
    hatch_sess: bool = False
    sess_exp: str = "-"
    burst_10m: int = 0
    last_used_port: str = ""
    proxy_port_changed: bool = False

    @property
    def has_warning(self) -> bool:
        return bool(
            self.busy_by
            or self.port_cooldown_by is not None
            or self.burst_10m >= TOKEN_BURST_THRESHOLD
            or self.proxy_port_changed
        )


@dataclass
class MonitorSnapshot:
    profiles: dict[str, ProfileMonitorInfo] = field(default_factory=dict)
    free_gb: float = 0.0
    gw_ready_count: int = 0
    thread_ok_count: int = 0
    active_ports: int = 0
    total_ports: int = 0
    proxy_collisions: list[tuple[str, list[str]]] = field(default_factory=list)
    token_bursts: list[tuple[str, int]] = field(default_factory=list)

    @property
    def disk_low(self) -> bool:
        return 0 < self.free_gb < 10.0


class MonitorService:
    """Fast mtime-cached read-only monitor for the main FoxProfile UI."""

    def __init__(self, data_dir: str = DATA_DIR, log_dir: str = LOG_DIR) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._log_dir = pathlib.Path(log_dir)
        self._log_cache_key: tuple[tuple[str, float, int], ...] = ()
        self._log_history: dict[str, dict[str, Any]] = {}
        self._live_started_at: dict[str, dt.datetime] = {}
        self._live_stopped_at: dict[str, dt.datetime] = {}
        self._live_sessions: dict[str, list[dict[str, Any]]] = {}
        self._disk_cache: dict[str, dict[str, Any]] = {}
        self._last_free_gb_ts: float = 0.0
        self._free_gb: float = 0.0
        self._cooldown_minutes: int = PORT_COOLDOWN_MINUTES
        self._last_used_ports: dict[str, str] = {}
        self._token_entry_memo: dict[str, tuple[float, bool]] = {}
        self._load_settings()

    def _settings_file(self) -> pathlib.Path:
        return self._data_dir.parent / "monitor_settings.json"

    def _load_settings(self) -> None:
        path = self._settings_file()
        if not path.exists():
            return
        with contextlib.suppress(Exception):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cd = raw.get("cooldown_minutes")
                if isinstance(cd, int) and 0 <= cd <= 1440:
                    self._cooldown_minutes = cd
                ports = raw.get("last_used_ports")
                if isinstance(ports, dict):
                    self._last_used_ports = {
                        str(k): str(v) for k, v in ports.items() if isinstance(k, str) and v
                    }

    def _save_settings(self) -> None:
        path = self._settings_file()
        with contextlib.suppress(Exception):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            payload = {
                "cooldown_minutes": self._cooldown_minutes,
                "last_used_ports": self._last_used_ports,
            }
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)

    @property
    def cooldown_minutes(self) -> int:
        return self._cooldown_minutes

    def set_cooldown_minutes(self, minutes: int) -> None:
        self._cooldown_minutes = max(0, min(1440, int(minutes)))
        self._save_settings()

    def acknowledge_proxy_change(self, profile_name: str, proxy: str | None) -> None:
        port = extract_proxy_port(proxy)
        if port:
            self._last_used_ports[profile_name] = port
        else:
            self._last_used_ports.pop(profile_name, None)
        self._save_settings()

    def record_started(self, profile_name: str, proxy: str | None = None) -> None:
        self._live_started_at[profile_name] = dt.datetime.now()
        if proxy is not None:
            port = extract_proxy_port(proxy)
            if port and self._last_used_ports.get(profile_name) != port:
                self._last_used_ports[profile_name] = port
                self._save_settings()

    def record_stopped(self, profile_name: str) -> None:
        now = dt.datetime.now()
        self._live_stopped_at[profile_name] = now
        start_dt = self._live_started_at.pop(profile_name, None)
        if start_dt is not None:
            dur = max(0.0, (now - start_dt).total_seconds())
            self._live_sessions.setdefault(profile_name, []).append(
                {
                    "start": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "end": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "duration_mins": max(1, round(dur / 60)) if dur >= 10 else 0,
                    "port": self._last_used_ports.get(profile_name, ""),
                    "is_live": False,
                }
            )

    def get_profile_sessions(self, profile_name: str, limit: int = 25) -> list[dict[str, Any]]:
        """Return recent browser sessions for `profile_name` (newest first)."""
        hist = self._refresh_log_history()
        h = hist.get(profile_name, {})
        port = self._last_used_ports.get(profile_name, "")
        merged: list[dict[str, Any]] = []
        seen_starts: set[str] = set()
        for s in list(h.get("sessions", [])) + list(self._live_sessions.get(profile_name, [])):
            st = str(s.get("start", ""))
            if st and st not in seen_starts:
                seen_starts.add(st)
                entry = dict(s)
                if not entry.get("port") and port:
                    entry["port"] = port
                merged.append(entry)
        merged.sort(key=lambda item: str(item.get("start", "")), reverse=True)

        live_start = self._live_started_at.get(profile_name) or h.get("_open_start")
        if live_start is not None and profile_name in self._live_started_at:
            now = dt.datetime.now()
            mins = max(0, int((now - live_start).total_seconds() // 60))
            merged.insert(
                0,
                {
                    "start": live_start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end": "",
                    "duration_mins": mins,
                    "port": port,
                    "is_live": True,
                },
            )
        return merged[:limit]

    def _refresh_log_history(self) -> dict[str, dict[str, Any]]:
        if not self._log_dir.exists():
            return self._log_history
        log_files = sorted(self._log_dir.glob("foxprofile_*.log"))[-3:]
        key_parts: list[tuple[str, float, int]] = []
        for lf in log_files:
            try:
                st = lf.stat()
                key_parts.append((lf.name, st.st_mtime, st.st_size))
            except OSError:
                continue
        cache_key = tuple(key_parts)
        if cache_key == self._log_cache_key and self._log_history:
            return self._log_history

        today_str = dt.datetime.now().strftime("%Y-%m-%d")
        hist: dict[str, dict[str, Any]] = {}

        def _ensure(pname: str) -> dict[str, Any]:
            if pname not in hist:
                hist[pname] = {
                    "last_closed": None,
                    "today_seconds": 0.0,
                    "_open_start": None,
                    "sessions": [],
                }
            return hist[pname]

        for lf in log_files:
            try:
                lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                m = _RE_LOG_LINE.match(line)
                if not m:
                    continue
                ts_str = m.group("ts")
                msg = m.group("msg").strip()
                try:
                    ts = dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue

                mb = _RE_BRACKET_PROFILE.match(msg)
                if mb:
                    pname = mb.group("prof").strip()
                    rest = mb.group("rest").strip()
                    h = _ensure(pname)
                    if rest.startswith("Starting browser for ") or rest == "BROWSER_STARTED":
                        if h["_open_start"] is None:
                            h["_open_start"] = ts
                    elif rest in ("BROWSER_CLOSED", "LAUNCH_CANCELLED") or rest.startswith(
                        "LAUNCH_FAILED:"
                    ):
                        start_ts = h["_open_start"]
                        if start_ts is not None:
                            dur = max(0.0, (ts - start_ts).total_seconds())
                            if ts_str.startswith(today_str):
                                h["today_seconds"] += dur
                            if rest == "BROWSER_CLOSED":
                                h["sessions"].append(
                                    {
                                        "start": start_ts.strftime("%Y-%m-%d %H:%M:%S"),
                                        "end": ts.strftime("%Y-%m-%d %H:%M:%S"),
                                        "duration_mins": max(1, round(dur / 60)) if dur >= 10 else 0,
                                        "port": self._last_used_ports.get(pname, ""),
                                        "is_live": False,
                                    }
                                )
                            h["_open_start"] = None
                        if rest == "BROWSER_CLOSED":
                            h["last_closed"] = ts
                    continue

                ms = (
                    _RE_STOPPING.match(msg)
                    or _RE_TERMINATED.match(msg)
                    or _RE_SESSION_ENDED.match(msg)
                )
                if ms:
                    pname = ms.group("prof").strip()
                    h = _ensure(pname)
                    start_ts = h["_open_start"]
                    if start_ts is not None:
                        dur = max(0.0, (ts - start_ts).total_seconds())
                        if ts_str.startswith(today_str):
                            h["today_seconds"] += dur
                        h["sessions"].append(
                            {
                                "start": start_ts.strftime("%Y-%m-%d %H:%M:%S"),
                                "end": ts.strftime("%Y-%m-%d %H:%M:%S"),
                                "duration_mins": max(1, round(dur / 60)) if dur >= 10 else 0,
                                "port": self._last_used_ports.get(pname, ""),
                                "is_live": False,
                            }
                        )
                        h["_open_start"] = None
                    h["last_closed"] = ts

        self._log_cache_key = cache_key
        self._log_history = hist
        return hist

    def _inspect_profile_disk(self, name: str, is_running: bool) -> dict[str, Any]:
        pdir = self._data_dir / name
        now_ts = time.time()
        cached = self._disk_cache.get(name)
        if not pdir.exists():
            return {
                "has_thread_url": False,
                "tab_count": 0,
                "hatch_gw": False,
                "hatch_sess": False,
                "sess_exp": "-",
                "burst_10m": 0,
                "last_active_ts": 0.0,
            }

        tabs_file = pdir / "tabs.json"
        cookies_db = pdir / "cookies.sqlite"
        prefs_file = pdir / "prefs.js"
        try:
            tabs_mtime = tabs_file.stat().st_mtime if tabs_file.exists() else 0.0
        except OSError:
            tabs_mtime = 0.0
        try:
            cookies_mtime = cookies_db.stat().st_mtime if cookies_db.exists() else 0.0
        except OSError:
            cookies_mtime = 0.0
        try:
            prefs_mtime = prefs_file.stat().st_mtime if prefs_file.exists() else 0.0
        except OSError:
            prefs_mtime = 0.0
        last_active_ts = max(tabs_mtime, cookies_mtime, prefs_mtime)

        if cached is not None:
            age = now_ts - cached.get("_ts", 0.0)
            if not is_running:
                if (
                    cached.get("_tabs_mtime") == tabs_mtime
                    and cached.get("_cookies_mtime") == cookies_mtime
                ):
                    cached["burst_10m"] = 0
                    cached["last_active_ts"] = last_active_ts
                    return cached
            elif (
                cached.get("_is_running") is True
                and cached.get("_tabs_mtime") == tabs_mtime
                and age < 15.0
            ):
                cached["last_active_ts"] = last_active_ts
                return cached

        result: dict[str, Any] = {
            "_ts": now_ts,
            "_is_running": is_running,
            "_tabs_mtime": tabs_mtime,
            "_cookies_mtime": -1.0 if is_running else cookies_mtime,
            "has_thread_url": False,
            "tab_count": 0,
            "hatch_gw": cached.get("hatch_gw", False) if cached else False,
            "hatch_sess": cached.get("hatch_sess", False) if cached else False,
            "sess_exp": cached.get("sess_exp", "-") if cached else "-",
            "burst_10m": 0,
            "last_active_ts": last_active_ts,
        }

        if tabs_mtime > 0:
            try:
                raw = json.loads(tabs_file.read_text(encoding="utf-8", errors="replace"))
                urls = (
                    raw
                    if isinstance(raw, list)
                    else (raw.get("tabs") or raw.get("urls") or [])
                )
                str_urls = [u for u in urls if isinstance(u, str)]
                result["tab_count"] = len(str_urls)
                result["has_thread_url"] = any("/thread/" in u for u in str_urls)
            except Exception:
                pass

        # Only read cookies.sqlite when the profile's browser is NOT running
        if not is_running and cookies_mtime > 0:
            try:
                db_posix = cookies_db.resolve().as_posix()
                uri = f"file:///{urllib.parse.quote(db_posix, safe=':/')}?mode=ro&immutable=1"
                conn = sqlite3.connect(uri, uri=True, timeout=0.3)
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT host, name, expiry FROM moz_cookies")
                    rows = cur.fetchall()
                finally:
                    conn.close()

                hatch_gw = False
                hatch_sess = False
                latest_exp = 0.0
                for _host, cname, exp in rows:
                    cname_s = str(cname or "")
                    if "cf_clearance" in cname_s or "__cf_bm" in cname_s or "hatch_gw" in cname_s:
                        hatch_gw = True
                    elif any(
                        k in cname_s.lower()
                        for k in ("session", "token", "auth", "user", "id", "clerk", "sb-", "hatch_sess")
                    ):
                        hatch_sess = True
                        if exp:
                            exp_val = float(exp)
                            while exp_val > 10_000_000_000:
                                exp_val /= 1000.0
                            latest_exp = max(latest_exp, exp_val)
                if rows and not hatch_sess and len(rows) > 1:
                    hatch_sess = True

                sess_exp = "-"
                if latest_exp > 0:
                    rem_h = (latest_exp - now_ts) / 3600.0
                    if rem_h < 0:
                        sess_exp = "Hết hạn"
                    elif rem_h < 24:
                        sess_exp = f"Còn {int(rem_h)}h"
                    else:
                        sess_exp = f"Còn {int(rem_h // 24)}d"
                elif hatch_sess:
                    sess_exp = "Session"

                result["hatch_gw"] = hatch_gw
                result["hatch_sess"] = hatch_sess
                result["sess_exp"] = sess_exp
            except Exception:
                pass

        # Only check live token bursts for currently running profiles (never for closed profiles)
        if is_running:
            entries_dir = pdir / "cache2" / "entries"
            if entries_dir.exists():
                burst_10m = 0
                try:
                    with os.scandir(entries_dir) as it:
                        for entry in it:
                            try:
                                st = entry.stat()
                                mtime = st.st_mtime
                                fsize = st.st_size
                                age_s = now_ts - mtime
                                if age_s < 2.0 or age_s > 600 or fsize < 20_000 or fsize > 35_000:
                                    continue
                                fp = entry.path
                                memo = self._token_entry_memo.get(fp)
                                if memo is not None and memo[0] == mtime:
                                    is_tok = memo[1]
                                else:
                                    is_tok = False
                                    with contextlib.suppress(OSError), open(fp, "rb") as cf:
                                        head = cf.read(4096)
                                        if b":https://muse.ai/api/hatch/token" in head:
                                            is_tok = True
                                        elif fsize > 4096:
                                            cf.seek(max(0, fsize - 2048))
                                            tail = cf.read(2048)
                                            if b":https://muse.ai/api/hatch/token" in tail:
                                                is_tok = True
                                    self._token_entry_memo[fp] = (mtime, is_tok)
                                if is_tok:
                                    burst_10m += 1
                            except OSError:
                                continue
                except OSError:
                    pass
                result["burst_10m"] = burst_10m

        self._disk_cache[name] = result
        return result

    def snapshot(
        self,
        profiles: list[Profile],
        running_names: set[str],
    ) -> MonitorSnapshot:
        now = dt.datetime.now()
        now_ts = time.time()
        if now_ts - self._last_free_gb_ts > 15.0:
            try:
                target = self._data_dir if self._data_dir.exists() else pathlib.Path.cwd()
                usage = shutil.disk_usage(target)
                self._free_gb = usage.free / (1024**3)
            except OSError:
                self._free_gb = 0.0
            self._last_free_gb_ts = now_ts

        hist = self._refresh_log_history()

        # Group profiles by proxy port
        port_to_profiles: dict[str, list[str]] = {}
        for p in profiles:
            port = extract_proxy_port(p.proxy)
            if port:
                port_to_profiles.setdefault(port, []).append(p.name)

        # Determine disk state, last_closed, and today_minutes per profile first
        disk_map: dict[str, dict[str, Any]] = {}
        last_closed_map: dict[str, dt.datetime | None] = {}
        today_mins_map: dict[str, int] = {}
        ports_seeded = False
        for p in profiles:
            is_run = p.name in running_names
            disk = self._inspect_profile_disk(p.name, is_run)
            disk_map[p.name] = disk

            port = extract_proxy_port(p.proxy)
            if (
                port
                and (disk.get("hatch_gw") or disk.get("hatch_sess"))
                and p.name not in self._last_used_ports
            ):
                self._last_used_ports[p.name] = port
                ports_seeded = True

            h = hist.get(p.name, {})
            lc = h.get("last_closed")
            live_stop = self._live_stopped_at.get(p.name)
            if live_stop and (lc is None or live_stop > lc):
                lc = live_stop
            disk_ts = float(disk.get("last_active_ts") or 0.0)
            if disk_ts > 0:
                disk_dt = dt.datetime.fromtimestamp(disk_ts)
                if lc is None or disk_dt > lc:
                    lc = disk_dt
            last_closed_map[p.name] = lc

            secs = float(h.get("today_seconds", 0.0))
            if p.name in running_names:
                start_dt = self._live_started_at.get(p.name) or h.get("_open_start")
                if start_dt and start_dt.date() == now.date():
                    secs += max(0.0, (now - start_dt).total_seconds())
            today_mins_map[p.name] = int(secs // 60)

        if ports_seeded:
            self._save_settings()

        snap = MonitorSnapshot(
            free_gb=self._free_gb,
            total_ports=len(port_to_profiles),
        )

        active_ports_set: set[str] = set()
        for port, members in port_to_profiles.items():
            running_on_port = [m for m in members if m in running_names]
            if running_on_port:
                active_ports_set.add(port)
            if len(running_on_port) > 1:
                snap.proxy_collisions.append((port, running_on_port))
        snap.active_ports = len(active_ports_set)

        cd_target = self._cooldown_minutes
        for p in profiles:
            is_run = p.name in running_names
            port = extract_proxy_port(p.proxy)
            siblings = port_to_profiles.get(port, [p.name]) if port else [p.name]
            busy_by = [m for m in siblings if m != p.name and m in running_names]

            lc = last_closed_map.get(p.name)
            cooldown_mins = (
                max(0, int((now - lc).total_seconds() // 60))
                if (lc is not None and not is_run)
                else None
            )

            # Check if a sibling on the same proxy port closed < cooldown_minutes ago
            port_cooldown_by: tuple[str, int] | None = None
            if cd_target > 0 and not is_run and not busy_by and len(siblings) > 1:
                for sib in siblings:
                    if sib == p.name:
                        continue
                    sib_lc = last_closed_map.get(sib)
                    if sib_lc is not None:
                        ago = int((now - sib_lc).total_seconds() // 60)
                        if 0 <= ago < cd_target:
                            left = cd_target - ago
                            if port_cooldown_by is None or left > port_cooldown_by[1]:
                                port_cooldown_by = (sib, left)

            disk = disk_map[p.name]
            if disk["hatch_gw"]:
                snap.gw_ready_count += 1
            if disk["has_thread_url"]:
                snap.thread_ok_count += 1
            if disk["burst_10m"] >= TOKEN_BURST_THRESHOLD:
                snap.token_bursts.append((p.name, disk["burst_10m"]))

            last_port = self._last_used_ports.get(p.name, "")
            has_sess = bool(disk["hatch_gw"] or disk["hatch_sess"])
            proxy_port_changed = bool(has_sess and last_port and port != last_port)

            snap.profiles[p.name] = ProfileMonitorInfo(
                name=p.name,
                port=port,
                shared_count=len(siblings),
                last_closed=lc,
                cooldown_mins=cooldown_mins,
                today_minutes=today_mins_map.get(p.name, 0),
                busy_by=busy_by,
                port_cooldown_by=port_cooldown_by,
                has_thread_url=bool(disk["has_thread_url"]),
                tab_count=int(disk["tab_count"]),
                hatch_gw=bool(disk["hatch_gw"]),
                hatch_sess=bool(disk["hatch_sess"]),
                sess_exp=str(disk["sess_exp"]),
                burst_10m=int(disk["burst_10m"]),
                last_used_port=last_port,
                proxy_port_changed=proxy_port_changed,
            )

        return snap

    def check_prelaunch(
        self,
        profile: Profile,
        all_profiles: list[Profile],
        running_names: set[str],
        loading_names: set[str] | None = None,
        *,
        snap: MonitorSnapshot | None = None,
    ) -> list[PreLaunchWarning]:
        """Evaluate the 4-layer pre-launch safety guard for a single profile."""
        active_names = set(running_names) | set(loading_names or ())
        if snap is None:
            snap = self.snapshot(all_profiles, active_names)
        info = snap.profiles.get(profile.name)
        warnings: list[PreLaunchWarning] = []

        proxy_str = (profile.proxy or "").strip()
        # Layer 1: No proxy attached -> risks leaking real local IP
        if not proxy_str:
            warnings.append(
                PreLaunchWarning(
                    code="no_proxy",
                    level="critical",
                    message=get_string("guard_no_proxy"),
                )
            )
        else:
            # Layer 2a: Proxy port is currently busy on another running/starting profile
            if info and info.busy_by:
                warnings.append(
                    PreLaunchWarning(
                        code="port_busy",
                        level="critical",
                        message=get_string(
                            "guard_port_busy",
                            port=info.port,
                            others=", ".join(info.busy_by),
                        ),
                    )
                )
            # Layer 2b: Proxy port is still within the shift cooldown window
            elif info and info.port_cooldown_by is not None and self._cooldown_minutes > 0:
                other_name, left_mins = info.port_cooldown_by
                warnings.append(
                    PreLaunchWarning(
                        code="port_cooldown",
                        level="warning",
                        message=get_string(
                            "guard_port_cooldown",
                            port=info.port,
                            other=other_name,
                            left=left_mins,
                            cooldown=self._cooldown_minutes,
                        ),
                    )
                )

            # Layer 4: Proxy is set, but Timezone or Locale is missing (unpinned)
            if not (profile.timezone or "").strip() or not (profile.locale or "").strip():
                warnings.append(
                    PreLaunchWarning(
                        code="missing_geo",
                        level="warning",
                        message=get_string("guard_missing_geo"),
                    )
                )

        # Layer 3: Profile has saved login cookies on a previous proxy port, and port changed
        if info and info.proxy_port_changed:
            warnings.append(
                PreLaunchWarning(
                    code="proxy_changed_with_session",
                    level="critical",
                    message=get_string(
                        "guard_proxy_changed",
                        old_port=info.last_used_port,
                        new_port=info.port or get_string("direct_short"),
                    ),
                )
            )

        return warnings

    def check_bulk_prelaunch(
        self,
        target_names: list[str],
        all_profiles: list[Profile],
        running_names: set[str],
        loading_names: set[str] | None = None,
    ) -> dict[str, list[PreLaunchWarning]]:
        """Evaluate pre-launch safety for a batch of profiles, including intra-batch port collisions."""
        active_names = set(running_names) | set(loading_names or ())
        snap = self.snapshot(all_profiles, active_names)
        by_name = {p.name: p for p in all_profiles}
        seen_ports: dict[str, str] = {}
        issues: dict[str, list[PreLaunchWarning]] = {}

        for name in target_names:
            if name in active_names:
                continue
            p = by_name.get(name)
            if p is None:
                continue
            wlist = self.check_prelaunch(
                p,
                all_profiles,
                running_names,
                loading_names,
                snap=snap,
            )
            port = extract_proxy_port(p.proxy)
            if port:
                if port in seen_ports:
                    wlist.insert(
                        0,
                        PreLaunchWarning(
                            code="batch_port_collision",
                            level="critical",
                            message=get_string(
                                "guard_batch_collision",
                                port=port,
                                other=seen_ports[port],
                            ),
                        ),
                    )
                else:
                    seen_ports[port] = name
            if wlist:
                issues[name] = wlist

        return issues
