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
import hashlib
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
from ..models.profile import (
    Profile,
    compute_days_since,
    has_ban_page,
    is_ban_page,
    is_ban_status,
)
from ..utils.totp import split_mail_token

PORT_COOLDOWN_MINUTES = 15
TOKEN_BURST_THRESHOLD = 12

_RE_PORT = re.compile(r":(\d{2,5})(?=:|$|\s|/)")
_RE_LEGACY_PORT = re.compile(r":(\d{2,5})(?:@|$|\s|/)")
_RE_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_RE_USA_CLUSTER = re.compile(r"\b(usa\d+)\b", re.IGNORECASE)
_RE_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(?:\||-)\s+(?P<lvl>\w+)\s+(?:\||-)\s+"
    r"(?P<mod>[\w\.]+)\s+(?:\||-)\s+(?P<msg>.*)$"
)
_RE_BRACKET_PROFILE = re.compile(r"^\[(?P<prof>[^\]]+)\]\s*(?P<rest>.*)$")
_RE_STARTING = re.compile(
    r"^(?:Starting browser for profile|Browser started for profile|\u0110ang kh\u1edfi ch\u1ea1y|Launching):\s*(?P<prof>.+)$"
)
_RE_STOPPING = re.compile(
    r"^(?:Stopping browser for (?P<p1>.+?)\.\.\.|Stopped browser for profile:\s*(?P<p2>.+))$"
)
_RE_TERMINATED = re.compile(
    r"^(?:"
    r"Browser process for (?P<p1>.+?) terminated\.|"
    r"Browser (?P<p2>.+?) terminated gracefully|"
    r"Browser close detected for profile:\s*(?P<p3>.+)"
    r")$"
)
_RE_SESSION_ENDED = re.compile(
    r"^(?:Session ended(?: for profile)?|Phi\u00ean k\u1ebft th\u00fac|\u0110\u00e3 \u0111\u00f3ng):\s*(?P<prof>.+)$"
)
_RE_LAUNCH_FAILED = re.compile(
    r"^(?:Launch failed for profile|Error starting browser for)\s+(?P<prof>[^:]+):"
)
_RE_RENAME_PROFILE = re.compile(r"^Updated profile:\s*(?P<old>.+?)\s*->\s*(?P<new>.+)$")
_RE_TRASH_PROFILE = re.compile(r"^(?:Moved profile to trash|Deleted profile):\s*(?P<prof>.+)$")

_MULTI_PART_TLDS = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.jp",
        "ne.jp",
        "or.jp",
        "com.vn",
        "net.vn",
        "org.vn",
        "edu.vn",
        "gov.vn",
        "com.br",
        "com.sg",
        "co.kr",
        "co.in",
        "co.nz",
        "com.tw",
        "com.hk",
    }
)

_NON_WARMUP_ROOT_DOMAINS = frozenset(
    {
        "muse.ai",
        "muse.vn",
        "hatch.io",
        "meta.com",
        "facebook.com",
        "instagram.com",
        "meta.ai",
        "fbcdn.net",
        "fbsbx.com",
        "whatsapp.com",
        "threads.net",
        "live.com",
        "microsoft.com",
        "outlook.com",
        "office.com",
        "microsoftonline.com",
        "msauth.net",
        "msftauth.net",
        "sfx.ms",
        "msn.com",
        "bing.com",
        "google.com",
        "doodles.google",
        "gstatic.com",
        "googleapis.com",
        "googleusercontent.com",
        "mozilla.org",
        "mozilla.com",
        "firefox.com",
        "cloudflare.com",
        "browserscan.net",
        "browserscan.com",
        "broserscan.net",
        "pixelscan.net",
        "iphey.com",
        "whoer.net",
        "amiunique.org",
    }
)


def _extract_root_domain(host: str) -> str:
    """Normalize a hostname to its registrable root domain (eTLD+1), e.g. 'www.amazon.com' -> 'amazon.com'."""
    h = (host or "").strip().lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h or "." not in h or h.replace(".", "").isdigit() or ":" in h:
        return ""
    parts = [p for p in h.split(".") if p]
    if len(parts) < 2:
        return ""
    last_two = f"{parts[-2]}.{parts[-1]}"
    if len(parts) >= 3 and last_two in _MULTI_PART_TLDS:
        return f"{parts[-3]}.{last_two}"
    return last_two


def extract_proxy_port(proxy_str: str | None) -> str:
    if not proxy_str:
        return ""
    s = _RE_SCHEME.sub("", proxy_str.strip())
    if "@" in s:
        s = s.rsplit("@", 1)[-1]
    m = _RE_PORT.search(s)
    return f":{m.group(1)}" if m else ""


def extract_proxy_endpoint(proxy_str: str | None) -> str:
    if not proxy_str:
        return ""
    s = _RE_SCHEME.sub("", proxy_str.strip())
    if "@" in s:
        s = s.rsplit("@", 1)[-1]
    s = s.split("/", 1)[0].strip()
    m = _RE_PORT.search(s)
    if not m:
        return ""
    host = s[: m.start()].strip().lower()
    if host:
        m_usa = _RE_USA_CLUSTER.search(host)
        if m_usa:
            host = m_usa.group(1).lower()
    return f"{host}:{m.group(1)}" if host else f":{m.group(1)}"


def extract_proxy_cluster(proxy_str: str | None) -> str:
    ep = extract_proxy_endpoint(proxy_str)
    if not ep or ":" not in ep:
        return ""
    host = ep.rsplit(":", 1)[0].strip().lower()
    if not host:
        return ""
    m_usa = _RE_USA_CLUSTER.search(host)
    return m_usa.group(1).lower() if m_usa else host


def is_gateway_ipv6_cluster(cluster: str) -> bool:
    return bool(cluster and cluster[0].isdigit())


def _ipv6_prefix_64(ip: str | None) -> str:
    clean = (ip or "").strip().lower()
    if ":" not in clean:
        return ""
    try:
        import ipaddress

        addr = ipaddress.IPv6Address(clean)
        exploded = addr.exploded.split(":")
        return ":".join(exploded[:4])
    except Exception:
        segs = [s for s in clean.split(":") if s]
        return ":".join(segs[:4]) if len(segs) >= 4 else ""


@dataclass
class PreLaunchWarning:
    code: str
    level: str  # "critical" | "warning"
    message: str


@dataclass
class AuditFinding:
    code: str
    level: str  # "critical" | "warning"
    title: str
    detail: str
    profiles: list[str] = field(default_factory=list)


@dataclass
class ProfileMonitorInfo:
    name: str
    port: str = ""
    shared_count: int = 1
    last_closed: dt.datetime | None = None
    cooldown_mins: int | None = None
    running_mins: int | None = None
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
    last_exit_ip: str = ""
    last_exit_country: str = ""
    last_exit_ts: str = ""
    last_exit_isp: str = ""
    last_exit_ip_type: str = ""
    prev_exit_ip: str = ""
    prev_exit_country: str = ""
    ip_country_changed: bool = False
    ip_fraud_score: int | None = None
    ip_scamalytics: int | None = None
    ip_proxycheck_risk: int | None = None
    ip_proxy_flag: bool = False
    ip_asn: str = ""
    has_grecaptcha: bool = False
    has_meta: bool = False
    has_microsoft: bool = False
    has_cache2: bool = False
    hatch_vml: bool = False
    has_ps_l: bool = False
    has_meta_checkpoint: bool = False
    datr_age_days: float = 0.0
    datr_domains: int = 0
    ext_domains: int = 0
    cookie_count: int = 0
    cookie_domains: int = 0
    history_pages: int = 0
    history_domains: int = 0
    age_days: int = 0
    session_count: int = 0
    active_days: int = 0
    total_minutes: int = 0
    is_banned: bool = False
    trust_score: int = 0
    trust_tier: str = "cold"
    audit_codes: list[str] = field(default_factory=list)
    cluster: str = ""
    cluster_collision_label: str = ""
    cluster_busy_by: list[str] = field(default_factory=list)
    can_open_parallel: bool = False
    has_muse_auth: bool = False
    has_proxy: bool = False
    has_geo_pin: bool = False
    has_recovery_key: bool = False
    has_recovery_armor: bool = False

    @property
    def has_warning(self) -> bool:
        return bool(
            self.busy_by
            or self.port_cooldown_by is not None
            or self.burst_10m >= TOKEN_BURST_THRESHOLD
            or self.proxy_port_changed
            or self.ip_country_changed
        )


def _fmt_mins_compact(mins: int) -> str:
    m = max(0, int(mins))
    if m < 60:
        return f"{m}p"
    h, rem = divmod(m, 60)
    return f"{h}h" if rem == 0 else f"{h}h {rem}p"


MAX_TRUST_SCORE = 150


def _compute_stage_cap_and_code(
    *,
    is_banned: bool,
    cookie_count: int,
    sess_cnt: int,
    history_pages: int,
    hatch_gw: bool,
    eff_datr_age: float,
    active_days: int,
    total_mins: int,
    avg_mins: float,
    has_meta: bool,
    datr_domains: int,
    has_ps_l: bool,
    has_thread_url: bool,
    clean_env: bool,
    ip_fraud_score: int | None,
    has_recovery_key: bool,
    has_recovery_armor: bool,
) -> tuple[int | None, str, str]:
    """Compute progressive stage ceiling (0-150), stage code, and human-readable stage label."""
    trusted_gates_failed = bool(
        eff_datr_age < 12.0
        or active_days < 7
        or sess_cnt < 12
        or total_mins < 480
        or (avg_mins < 15.0 and total_mins < 900)
        or (has_meta and (datr_domains < 3 or not has_ps_l))
        or (has_meta and not has_thread_url)
        or not clean_env
    )
    platinum_gates_failed = bool(
        eff_datr_age < 21.0
        or active_days < 14
        or sess_cnt < 25
        or total_mins < 1200
        or (avg_mins < 20.0 and total_mins < 1800)
        or (has_meta and not has_thread_url)
        or (has_meta and datr_domains < 4)
        or (ip_fraud_score is not None and ip_fraud_score >= 65)
        or not clean_env
    )
    elite_gates_failed = bool(
        platinum_gates_failed
        or eff_datr_age < 30.0
        or active_days < 20
        or sess_cnt < 35
        or total_mins < 1800
        or not has_recovery_key
        or (ip_fraud_score is not None and ip_fraud_score >= 60)
    )
    diamond_gates_failed = bool(
        elite_gates_failed
        or eff_datr_age < 45.0
        or active_days < 28
        or sess_cnt < 50
        or total_mins < 2700
        or (avg_mins < 25.0 and total_mins < 3300)
        or not has_recovery_armor
        or (ip_fraud_score is not None and ip_fraud_score > 55)
    )

    p0 = (
        min(1.0, eff_datr_age / 3.0)
        + min(1.0, active_days / 2.0)
        + min(1.0, total_mins / 30.0)
    ) / 3.0

    if is_banned:
        return 0, "banned", "Tạm ngưng / Khóa (Trần 0đ)"
    if cookie_count == 0 and sess_cnt == 0 and history_pages == 0:
        return 0, "empty", "Hồ sơ mới chưa khởi tạo (Trần 0đ)"
    if not hatch_gw:
        cap = 6 + int(round(12.0 * p0))
        return cap, "no_gw", f"Chưa có cổng hatch_gw (Trần tối đa {cap}/18đ — cần đăng nhập Muse)"
    if eff_datr_age < 3.0 or active_days < 2 or total_mins < 30:
        cap = 8 + int(round(12.0 * p0))
        pct = int(round(p0 * 100))
        return (
            cap,
            "stage_0a",
            f"Giai đoạn 0 — ○ Khởi tạo (Tiến độ lên ◇ Tích lũy: {pct}% → Trần {cap}/20đ)",
        )
    if trusted_gates_failed:
        sync_ratio = (
            (0.5 if (not has_meta or has_ps_l) else 0.0)
            + (0.5 if (not has_meta or (has_thread_url and datr_domains >= 3)) else 0.0)
        )
        p1 = (
            0.25 * min(1.0, eff_datr_age / 12.0)
            + 0.25 * min(1.0, active_days / 7.0)
            + 0.15 * min(1.0, sess_cnt / 12.0)
            + 0.25 * min(1.0, total_mins / 480.0)
            + 0.10 * sync_ratio
        )
        cap = 21 + int(round(34.0 * p1))
        pct = int(round(p1 * 100))
        return (
            cap,
            "stage_1",
            f"Giai đoạn 1 — ◇ Tích lũy (Tiến độ mở cổng ◈ Ổn định 12d+: {pct}% → Trần {cap}/55đ)",
        )
    if platinum_gates_failed:
        p2 = (
            min(1.0, eff_datr_age / 21.0)
            + min(1.0, active_days / 14.0)
            + min(1.0, sess_cnt / 25.0)
            + min(1.0, total_mins / 1200.0)
        ) / 4.0
        cap = 56 + int(round(24.0 * p2))
        pct = int(round(p2 * 100))
        return (
            cap,
            "stage_2",
            f"Giai đoạn 2 — ◈ Ổn định (Tiến độ mở cổng ❖ Bền vững 21d+: {pct}% → Trần {cap}/80đ)",
        )
    if elite_gates_failed:
        p3 = (
            min(1.0, eff_datr_age / 30.0)
            + min(1.0, active_days / 20.0)
            + min(1.0, sess_cnt / 35.0)
            + min(1.0, total_mins / 1800.0)
        ) / 4.0
        cap = 81 + int(round(24.0 * p3))
        pct = int(round(p3 * 100))
        return (
            cap,
            "stage_3",
            f"Giai đoạn 3 — ❖ Bền vững (Tiến độ mở cổng 🛡️ Tin cậy 30d+: {pct}% → Trần {cap}/105đ)",
        )
    if diamond_gates_failed:
        p4 = (
            min(1.0, eff_datr_age / 45.0)
            + min(1.0, active_days / 28.0)
            + min(1.0, sess_cnt / 50.0)
            + min(1.0, total_mins / 2700.0)
        ) / 4.0
        cap = 106 + int(round(24.0 * p4))
        pct = int(round(p4 * 100))
        return (
            cap,
            "stage_4",
            f"Giai đoạn 4 — 🛡️ Tin cậy (Tiến độ mở cổng ✦ Kim Cương 45d+: {pct}% → Trần {cap}/130đ)",
        )
    return (
        None,
        "stage_5",
        "Giai đoạn 5 — ✦ Kim Cương (131–150đ · Mở khóa hoàn toàn 150/150đ)",
    )


def compute_trust_breakdown(
    info: ProfileMonitorInfo,
    profile: Profile | None = None,
) -> dict[str, Any]:
    """Return a full item-by-item breakdown of how a profile's Trust Score (0-150) is calculated."""
    eff_datr_age = float(info.datr_age_days or 0.0)
    active_days = int(info.active_days or 0)
    sess_cnt = int(info.session_count or 0)
    total_mins = int(info.total_minutes or 0)
    avg_mins = (total_mins / max(1, sess_cnt)) if sess_cnt > 0 else 0.0
    if profile is not None:
        has_proxy = bool(profile.proxy)
        has_geo_pin = bool(profile.proxy and profile.timezone and profile.locale)
        has_tfa = bool((profile.two_fa or "").strip())
        has_mtok = bool((profile.mail_token or "").strip())
        has_pwd_or_rec = bool(
            (profile.password or "").strip() or (profile.recovery_email or "").strip()
        )
        has_recovery_key = bool(has_tfa or has_mtok or info.has_recovery_key)
        has_recovery_armor = bool(
            has_tfa or (has_mtok and has_pwd_or_rec) or info.has_recovery_armor
        )
    else:
        has_proxy = bool(info.has_proxy or info.port or info.last_exit_ip or info.has_geo_pin)
        has_geo_pin = bool(info.has_geo_pin or (has_proxy and info.trust_score > 0))
        has_recovery_key = bool(info.has_recovery_key)
        has_recovery_armor = bool(info.has_recovery_armor)
    has_muse_auth = bool(info.has_muse_auth)

    # 1. Cookie `datr` Age (max 40)
    datr_tiers = [
        (45.0, 40),
        (30.0, 32),
        (21.0, 24),
        (14.0, 18),
        (10.0, 13),
        (7.0, 9),
        (3.0, 4),
    ]
    p1_pts = 0
    p1_next = "Mốc kế: Đăng nhập tạo cookie datr (+2đ)"
    if eff_datr_age >= 45.0:
        p1_pts = 40
        p1_next = "✓ Đã đạt điểm tối đa (≥45 ngày)"
    elif eff_datr_age > 0.0:
        p1_pts = 2
        next_d, next_p = 3.0, 4
        for idx_t, (td, tp) in enumerate(datr_tiers):
            if eff_datr_age >= td:
                p1_pts = tp
                if idx_t > 0:
                    next_d, next_p = datr_tiers[idx_t - 1]
                break
        rem_d = max(0.1, next_d - eff_datr_age)
        p1_next = f"Mốc kế (≥{int(next_d)}d → +{next_p}đ): đợi thêm {rem_d:.1f} ngày"

    # 2. Active Days & Session Consistency (max 30)
    rhythm_tiers = [
        (28, 50, 30),
        (20, 35, 26),
        (14, 25, 22),
        (10, 18, 17),
        (7, 12, 13),
        (5, 7, 8),
        (3, 4, 5),
        (2, 2, 2),
    ]
    p2_pts = 0
    next_r = (2, 2, 2)
    for idx_r, (rd, rs, rp) in enumerate(rhythm_tiers):
        if active_days >= rd and sess_cnt >= rs:
            p2_pts = rp
            next_r = rhythm_tiers[idx_r - 1] if idx_r > 0 else (rd, rs, rp)
            break
    if p2_pts >= 30:
        p2_next = "✓ Đã đạt điểm tối đa (≥28 ngày & ≥50 phiên)"
    else:
        nd, ns, np_pts = next_r
        need_parts: list[str] = []
        if active_days < nd:
            need_parts.append(f"+{nd - active_days} ngày mở")
        if sess_cnt < ns:
            need_parts.append(f"+{ns - sess_cnt} phiên")
        need_str = ", ".join(need_parts) if need_parts else "đủ điều kiện"
        p2_next = f"Mốc kế (≥{nd}d & ≥{ns} phiên → +{np_pts}đ): cần {need_str}"

    # 3. Cumulative Runtime Hours (max 25)
    runtime_tiers = [
        (2700, "45h", 25),
        (1800, "30h", 22),
        (1200, "20h", 18),
        (720, "12h", 14),
        (480, "8h", 11),
        (240, "4h", 7),
        (120, "2h", 4),
        (45, "45p", 2),
    ]
    p3_pts = 0
    next_rt = (45, "45p", 2)
    for idx_m, (rm, rlbl, rp) in enumerate(runtime_tiers):
        if total_mins >= rm:
            p3_pts = rp
            next_rt = runtime_tiers[idx_m - 1] if idx_m > 0 else (rm, rlbl, rp)
            break
    if p3_pts >= 25:
        p3_next = "✓ Đã đạt điểm tối đa (≥45h / 2700p)"
    else:
        nm, nlbl, np_rt = next_rt
        rem_m = max(1, nm - total_mins)
        p3_next = f"Mốc kế (≥{nlbl} / {nm}p → +{np_rt}đ): treo thêm {_fmt_mins_compact(rem_m)}"

    # 4. Hatch VM Gateway (`hatch_gw`), VM Locator (`hatch_vml`), OIDC Session & `/thread/` (max 20)
    gw_ok = bool(info.hatch_gw)
    gw_pts = 9 if gw_ok else 0
    vml_ok = bool(info.hatch_vml or (info.hatch_gw and not info.has_meta))
    vml_pts = 4 if vml_ok else 0
    sess_ok = bool(info.hatch_sess)
    sess_pts = 3 if sess_ok else 0
    thr_ok = bool(info.has_thread_url)
    thr_pts = 4 if thr_ok else 0
    p4_pts = gw_pts + vml_pts + sess_pts + thr_pts

    # 5. First-Party Storage (`ps_l`/`ps_n`), Cookie/Cache2 Sync & Recovery Armor (max 15)
    ps_ok = bool(info.has_ps_l or (not info.has_meta and info.has_grecaptcha))
    ps_pts = 3 if ps_ok else 0
    if (info.datr_domains >= 4 and info.cookie_count >= 18) or info.cookie_count >= 45:
        ck_pts = 4
    elif (info.datr_domains >= 3 and info.cookie_count >= 14) or info.cookie_count >= 30:
        ck_pts = 2
    elif info.datr_domains >= 2 or info.cookie_count >= 10:
        ck_pts = 1
    else:
        ck_pts = 0
    cache_ok = bool(info.has_cache2 and info.cookie_count > 0)
    cache_pts = 3 if cache_ok else 0
    rec_pts = 5 if has_recovery_armor else (3 if has_recovery_key else 0)
    p5_pts = ps_pts + ck_pts + cache_pts + rec_pts

    # 6. Network Cleanliness, IP Fraud Score & Geo-Pinning (max 20)
    geo_pts = 5 if (has_proxy and has_geo_pin) else (2 if has_proxy else 0)
    ip_pts = 0
    clean_env = not (
        info.has_meta_checkpoint or info.proxy_port_changed or info.ip_country_changed
    )
    if has_proxy:
        if info.ip_fraud_score is not None:
            if info.ip_fraud_score <= 15 and not info.ip_proxy_flag:
                ip_pts = 15
            elif info.ip_fraud_score <= 30 and not info.ip_proxy_flag:
                ip_pts = 12
            elif info.ip_fraud_score <= 55:
                ip_pts = 9
            elif info.ip_fraud_score < 65:
                ip_pts = 5
            flag_str = "Cờ Proxy" if info.ip_proxy_flag else "Không cờ Proxy"
            longevity_bonus = 0
            if info.ip_fraud_score <= 55 and clean_env:
                if eff_datr_age >= 45.0 and active_days >= 28 and has_recovery_armor:
                    longevity_bonus = max(0, 15 - ip_pts)
                    ip_pts = 15
                elif eff_datr_age >= 30.0 and active_days >= 20 and has_recovery_key:
                    longevity_bonus = max(0, 12 - ip_pts)
                    ip_pts = max(ip_pts, 12)
            bonus_note = f" · +{longevity_bonus}đ Độ bền IP" if longevity_bonus > 0 else ""
            ip_detail = f"Fraud {info.ip_fraud_score}/100 ({flag_str}{bonus_note})"
        elif info.last_exit_ip:
            ip_pts = 5
            ip_detail = "Đã có IP (chưa chấm điểm → tạm tính +5đ)"
        else:
            ip_detail = "Chưa kiểm tra IP ra"
    else:
        ip_detail = "Chưa gắn Proxy"
    p6_pts = geo_pts + ip_pts

    pillars: list[dict[str, Any]] = [
        {
            "key": "datr_age",
            "title": "1. Tuổi Cookie Định Danh (datr)",
            "earned": p1_pts,
            "max_pts": 40,
            "current_str": f"{eff_datr_age:.1f} ngày · {info.datr_domains} miền Meta/Muse",
            "next_hint": p1_next,
            "tiers_str": "Mốc: >0d (+2) · ≥3d (+4) · ≥7d (+9) · ≥10d (+13) · ≥14d (+18) · ≥21d (+24) · ≥30d (+32) · ≥45d (+40)",
            "sub_items": [],
        },
        {
            "key": "rhythm",
            "title": "2. Nhịp Độ Ngày Mở & Số Phiên",
            "earned": p2_pts,
            "max_pts": 30,
            "current_str": f"{active_days} ngày mở · {sess_cnt} phiên",
            "next_hint": p2_next,
            "tiers_str": "Mốc: 2d/2p (+2) · 3d/4p (+5) · 5d/7p (+8) · 7d/12p (+13) · 10d/18p (+17) · 14d/25p (+22) · 20d/35p (+26) · 28d/50p (+30)",
            "sub_items": [],
        },
        {
            "key": "runtime",
            "title": "3. Tổng Thời Gian Treo Tích Lũy",
            "earned": p3_pts,
            "max_pts": 25,
            "current_str": f"{_fmt_mins_compact(total_mins)} ({total_mins}p · TB {avg_mins:.0f}p/phiên)",
            "next_hint": p3_next,
            "tiers_str": "Mốc: ≥45p (+2) · ≥2h (+4) · ≥4h (+7) · ≥8h (+11) · ≥12h (+14) · ≥20h (+18) · ≥30h (+22) · ≥45h (+25)",
            "sub_items": [],
        },
        {
            "key": "hatch_vm",
            "title": "4. Phiên Muse & Máy Ảo Hatch",
            "earned": p4_pts,
            "max_pts": 20,
            "current_str": "",
            "next_hint": "",
            "tiers_str": "",
            "sub_items": [
                {
                    "label": "Cookie cổng máy ảo (hatch_gw)",
                    "detail": "Đã có" if gw_ok else "Chưa có",
                    "earned": gw_pts,
                    "max_pts": 9,
                    "ok": gw_ok,
                },
                {
                    "label": "Định vị máy ảo (hatch_vml)",
                    "detail": "Đã có" if vml_ok else "Chưa có",
                    "earned": vml_pts,
                    "max_pts": 4,
                    "ok": vml_ok,
                },
                {
                    "label": "Phiên OIDC (hatch_sess)",
                    "detail": info.sess_exp if sess_ok else "Chưa có",
                    "earned": sess_pts,
                    "max_pts": 3,
                    "ok": sess_ok,
                },
                {
                    "label": "Tab hội thoại (/thread/)",
                    "detail": f"Đã lưu ({info.tab_count} tab)" if thr_ok else "Chưa mở /thread/",
                    "earned": thr_pts,
                    "max_pts": 4,
                    "ok": thr_ok,
                },
            ],
        },
        {
            "key": "ecosystem",
            "title": "5. Đồng Bộ Cookie, Cache2 & Khóa Khôi Phục",
            "earned": p5_pts,
            "max_pts": 15,
            "current_str": "",
            "next_hint": "",
            "tiers_str": "",
            "sub_items": [
                {
                    "label": "First-Party Storage (ps_l / _GRECAPTCHA)",
                    "detail": "Đã có" if ps_ok else "Chưa có",
                    "earned": ps_pts,
                    "max_pts": 3,
                    "ok": ps_ok,
                },
                {
                    "label": "Độ phủ Cookie & Miền (≥4 miền/18ck hoặc ≥45ck)",
                    "detail": f"{info.datr_domains} miền · {info.cookie_count} ck · {info.history_pages} trang",
                    "earned": ck_pts,
                    "max_pts": 4,
                    "ok": ck_pts == 4,
                },
                {
                    "label": "Bộ nhớ đệm HTTP Cache2 (ETag)",
                    "detail": "Đã đồng bộ" if cache_ok else "Chưa có",
                    "earned": cache_pts,
                    "max_pts": 3,
                    "ok": cache_ok,
                },
                {
                    "label": "Bảo hiểm Khôi phục (2FA / Mail Token + Pass)",
                    "detail": (
                        "Đầy đủ (+5đ)"
                        if has_recovery_armor
                        else ("Có Token/2FA (+3đ)" if has_recovery_key else "Chưa lưu")
                    ),
                    "earned": rec_pts,
                    "max_pts": 5,
                    "ok": has_recovery_armor,
                },
            ],
        },
        {
            "key": "network",
            "title": "6. Hạ Tầng Mạng, Geo-Pinning & Độ Bền IP",
            "earned": p6_pts,
            "max_pts": 20,
            "current_str": "",
            "next_hint": "",
            "tiers_str": "",
            "sub_items": [
                {
                    "label": "Ghim Proxy + Timezone + Locale",
                    "detail": (
                        "Đã ghim đủ"
                        if geo_pts == 5
                        else ("Thiếu Timezone/Locale" if geo_pts == 2 else "Chưa gắn Proxy")
                    ),
                    "earned": geo_pts,
                    "max_pts": 5,
                    "ok": geo_pts == 5,
                },
                {
                    "label": "Độ sạch & Độ bền IP (≤15: +15đ · ≤30/Bền 30d: +12đ · ≤55: +9đ)",
                    "detail": ip_detail,
                    "earned": ip_pts,
                    "max_pts": 15,
                    "ok": ip_pts >= 12,
                },
            ],
        },
    ]

    # Risk Penalties
    penalties: list[dict[str, Any]] = []
    if info.has_meta_checkpoint:
        penalties.append(
            {
                "code": "checkpoint",
                "label": "Dính dấu vết URL / Cookie Checkpoint Meta",
                "points": -40,
            }
        )
    if info.hatch_sess and not info.hatch_gw and not has_muse_auth:
        penalties.append(
            {
                "code": "missing_gw",
                "label": "Có phiên hatch_sess nhưng mất cổng máy ảo hatch_gw",
                "points": -20,
            }
        )
    if info.proxy_port_changed or info.ip_country_changed:
        penalties.append(
            {
                "code": "ip_port_changed",
                "label": "Đổi cổng Proxy hoặc nhảy quốc gia IP khi đang có phiên",
                "points": -35,
            }
        )
    if info.burst_10m >= TOKEN_BURST_THRESHOLD:
        penalties.append(
            {
                "code": "token_burst",
                "label": f"Lặp token /api/hatch/token bất thường ({info.burst_10m} lần/10p)",
                "points": -20,
            }
        )
    if sess_cnt >= 3 and avg_mins < 5.0:
        penalties.append(
            {
                "code": "short_sessions_5m",
                "label": f"Mở-tắt quá nhanh kiểu bot ping-check (TB {avg_mins:.1f}p/phiên < 5p)",
                "points": -20,
            }
        )
    elif sess_cnt >= 6 and avg_mins < 8.0 and total_mins < 480:
        penalties.append(
            {
                "code": "short_sessions_8m",
                "label": f"Thời lượng phiên trung bình ngắn (TB {avg_mins:.1f}p/phiên < 8p)",
                "points": -8,
            }
        )
    if info.has_meta and info.datr_domains < 2 and info.cookie_count < 10:
        penalties.append(
            {
                "code": "incomplete_meta",
                "label": "Có Meta nhưng thiếu đồng bộ Cookie (<2 miền & <10 cookie)",
                "points": -12,
            }
        )

    positive_total = sum(int(p["earned"]) for p in pillars)
    penalty_total = sum(int(pen["points"]) for pen in penalties)
    raw_score = max(0, min(MAX_TRUST_SCORE, positive_total + penalty_total))

    stage_cap, stage_code, stage_label = _compute_stage_cap_and_code(
        is_banned=info.is_banned,
        cookie_count=info.cookie_count,
        sess_cnt=sess_cnt,
        history_pages=info.history_pages,
        hatch_gw=bool(info.hatch_gw),
        eff_datr_age=eff_datr_age,
        active_days=active_days,
        total_mins=total_mins,
        avg_mins=avg_mins,
        has_meta=bool(info.has_meta),
        datr_domains=info.datr_domains,
        has_ps_l=bool(info.has_ps_l),
        has_thread_url=bool(info.has_thread_url),
        clean_env=clean_env,
        ip_fraud_score=info.ip_fraud_score,
        has_recovery_key=has_recovery_key,
        has_recovery_armor=has_recovery_armor,
    )

    if stage_cap == 0:
        computed_final = 0
    elif stage_cap is not None:
        computed_final = max(1, min(stage_cap, raw_score))
    else:
        computed_final = max(1, raw_score)

    final_score = info.trust_score if info.trust_score > 0 or stage_cap == 0 else computed_final
    capped_by = max(0, raw_score - final_score) if stage_cap is not None else 0

    # Build checklist of gates for the current / next tier
    if stage_code in ("stage_4", "stage_5"):
        next_tier_title = "Điều kiện mở trần 130đ → Lên hạng ✦ Kim Cương (131–150đ · Chuẩn 45 ngày an toàn tuyệt đối)"
        ip_imm_ok = info.ip_fraud_score is None or info.ip_fraud_score <= 55
        gates: list[dict[str, Any]] = [
            {
                "label": "Tuổi Cookie datr ≥ 45 ngày (1.5 tháng)",
                "current": f"{eff_datr_age:.1f}/45 ngày",
                "passed": eff_datr_age >= 45.0,
            },
            {
                "label": "Số ngày mở ≥ 28 ngày",
                "current": f"{active_days}/28 ngày",
                "passed": active_days >= 28,
            },
            {
                "label": "Tổng số phiên ≥ 50 phiên",
                "current": f"{sess_cnt}/50 phiên",
                "passed": sess_cnt >= 50,
            },
            {
                "label": "Tổng giờ treo ≥ 45h (2700p)",
                "current": f"{_fmt_mins_compact(total_mins)} / 45h",
                "passed": total_mins >= 2700,
            },
            {
                "label": "Thời lượng TB ≥ 25p/phiên (hoặc ≥55h)",
                "current": f"TB {avg_mins:.0f}p/phiên",
                "passed": avg_mins >= 25.0 or total_mins >= 3300,
            },
            {
                "label": "Bảo hiểm khôi phục đầy đủ (2FA hoặc Token + Pass/Mail phụ)",
                "current": "Đã khóa bảo hiểm" if has_recovery_armor else "Thiếu Pass/Mail phụ hoặc 2FA",
                "passed": has_recovery_armor,
            },
            {
                "label": "Mạng ổn định & IP sạch (Fraud ≤ 55, không đổi Port)",
                "current": (
                    f"Fraud {info.ip_fraud_score}/100"
                    if info.ip_fraud_score is not None
                    else "An toàn"
                ),
                "passed": ip_imm_ok and clean_env,
            },
        ]
    elif stage_code == "stage_3":
        next_tier_title = "Điều kiện mở trần 105đ → Lên hạng 🛡️ Tin cậy (106–130đ · Chuẩn 30d bật 2FA/Pass)"
        ip_mst_ok = info.ip_fraud_score is None or info.ip_fraud_score < 60
        gates = [
            {
                "label": "Tuổi Cookie datr ≥ 30 ngày (Đủ tuổi cài 2FA/Pass)",
                "current": f"{eff_datr_age:.1f}/30 ngày",
                "passed": eff_datr_age >= 30.0,
            },
            {
                "label": "Số ngày mở ≥ 20 ngày",
                "current": f"{active_days}/20 ngày",
                "passed": active_days >= 20,
            },
            {
                "label": "Tổng số phiên ≥ 35 phiên",
                "current": f"{sess_cnt}/35 phiên",
                "passed": sess_cnt >= 35,
            },
            {
                "label": "Tổng giờ treo ≥ 30h (1800p)",
                "current": f"{_fmt_mins_compact(total_mins)} / 30h",
                "passed": total_mins >= 1800,
            },
            {
                "label": "Đã lưu khóa khôi phục (mail_token hoặc 2FA)",
                "current": "Đã có" if has_recovery_key else "Chưa lưu Token/2FA",
                "passed": has_recovery_key,
            },
            {
                "label": "Mạng sạch (Fraud < 60, không đổi Port/Checkpoint)",
                "current": (
                    f"Fraud {info.ip_fraud_score}/100"
                    if info.ip_fraud_score is not None
                    else "An toàn"
                ),
                "passed": ip_mst_ok and clean_env,
            },
        ]
    elif stage_code == "stage_2":
        next_tier_title = "Điều kiện mở trần 80đ → Lên hạng ❖ Bền vững (81–105đ · Chuẩn 21 ngày)"
        meta_dom_ok = (not info.has_meta) or (info.datr_domains >= 4 and info.has_thread_url)
        ip_gate_ok = info.ip_fraud_score is None or info.ip_fraud_score < 65
        gates = [
            {
                "label": "Tuổi Cookie datr ≥ 21 ngày",
                "current": f"{eff_datr_age:.1f} ngày",
                "passed": eff_datr_age >= 21.0,
            },
            {
                "label": "Số ngày mở ≥ 14 ngày",
                "current": f"{active_days}/14 ngày",
                "passed": active_days >= 14,
            },
            {
                "label": "Tổng số phiên ≥ 25 phiên",
                "current": f"{sess_cnt}/25 phiên",
                "passed": sess_cnt >= 25,
            },
            {
                "label": "Tổng giờ treo ≥ 20h (1200p)",
                "current": f"{_fmt_mins_compact(total_mins)} / 20h",
                "passed": total_mins >= 1200,
            },
            {
                "label": "Thời lượng TB ≥ 20p/phiên (hoặc ≥30h)",
                "current": f"TB {avg_mins:.0f}p/phiên",
                "passed": avg_mins >= 20.0 or total_mins >= 1800,
            },
            {
                "label": "Đồng bộ ≥4 miền Meta/Muse & /thread/",
                "current": f"{info.datr_domains} miền · /thread/: {'Có' if info.has_thread_url else 'Chưa'}",
                "passed": meta_dom_ok,
            },
            {
                "label": "Độ sạch IP (Fraud < 65, tối ưu ≤ 30)",
                "current": (
                    f"Fraud {info.ip_fraud_score}/100"
                    if info.ip_fraud_score is not None
                    else "Chưa chấm"
                ),
                "passed": ip_gate_ok,
            },
        ]
    else:
        next_tier_title = "Điều kiện vượt trần 20đ/55đ → Lên hạng ◈ Ổn định (56–80đ · Chuẩn 12 ngày)"
        meta_sync_ok = (not info.has_meta) or (
            info.datr_domains >= 3 and info.has_ps_l and info.has_thread_url
        )
        gates = [
            {
                "label": "Cookie cổng máy ảo (hatch_gw)",
                "current": "Đã có" if info.hatch_gw else "Chưa đăng nhập Muse",
                "passed": bool(info.hatch_gw),
            },
            {
                "label": "Tuổi Cookie datr ≥ 12d (3d→35đ · 7d→55đ)",
                "current": f"{eff_datr_age:.1f}/12 ngày",
                "passed": eff_datr_age >= 12.0,
            },
            {
                "label": "Số ngày mở ≥ 7d (2d→35đ · 4d→55đ)",
                "current": f"{active_days}/7 ngày",
                "passed": active_days >= 7,
            },
            {
                "label": "Tổng số phiên ≥ 12 phiên",
                "current": f"{sess_cnt}/12 phiên",
                "passed": sess_cnt >= 12,
            },
            {
                "label": "Tổng giờ treo ≥ 8h/480p (30p→35đ · 2h→55đ)",
                "current": f"{_fmt_mins_compact(total_mins)} / 8h",
                "passed": total_mins >= 480,
            },
            {
                "label": "Thời lượng TB ≥ 15p/phiên (hoặc ≥15h)",
                "current": f"TB {avg_mins:.0f}p/phiên",
                "passed": avg_mins >= 15.0 or total_mins >= 900,
            },
            {
                "label": "Đồng bộ ≥3 miền + ps_l + /thread/",
                "current": (
                    f"{info.datr_domains} miền · ps_l: {'Có' if info.has_ps_l else 'Chưa'} · "
                    f"/thread/: {'Có' if info.has_thread_url else 'Chưa'}"
                ),
                "passed": meta_sync_ok,
            },
            {
                "label": "Không dính Checkpoint / Không đổi Port IP",
                "current": "An toàn" if clean_env else "Đang có cảnh báo",
                "passed": clean_env,
            },
        ]

    return {
        "name": info.name,
        "final_score": final_score,
        "max_score": MAX_TRUST_SCORE,
        "trust_tier": info.trust_tier,
        "raw_score": raw_score,
        "positive_total": positive_total,
        "penalty_total": penalty_total,
        "stage_cap": stage_cap,
        "stage_code": stage_code,
        "stage_label": stage_label,
        "capped_by": capped_by,
        "pillars": pillars,
        "penalties": penalties,
        "gates": gates,
        "next_tier_title": next_tier_title,
    }


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
    audit_findings: list[AuditFinding] = field(default_factory=list)
    diamond_count: int = 0
    elite_count: int = 0
    platinum_count: int = 0
    trusted_count: int = 0
    clean_ip_count: int = 0
    scored_ip_count: int = 0

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
        self._disk_cache_dirty: bool = False
        self._last_free_gb_ts: float = 0.0
        self._free_gb: float = 0.0
        self._cooldown_minutes: int = PORT_COOLDOWN_MINUTES
        self._last_used_ports: dict[str, str] = {}
        self._ip_history: dict[str, list[dict[str, Any]]] = {}
        self._ip_scores: dict[str, dict[str, Any]] = {}
        self._token_entry_memo: dict[str, tuple[float, bool]] = {}
        self._last_pairings: list[list[str]] = []
        self._last_shifts: dict[str, str] = {}
        self._schedule_cycle_days: int = 5
        self._fixed_work_squads: dict[str, list[str]] = {}
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
                raw_scores = raw.get("ip_scores")
                if isinstance(raw_scores, dict):
                    clean_scores: dict[str, dict[str, Any]] = {}
                    for ip_k, s_val in raw_scores.items():
                        if isinstance(ip_k, str) and isinstance(s_val, dict):
                            clean_scores[ip_k.strip()] = dict(s_val)
                    self._ip_scores = clean_scores
                raw_ips = raw.get("ip_history")
                if isinstance(raw_ips, dict):
                    clean_hist: dict[str, list[dict[str, Any]]] = {}
                    for pname, entries in raw_ips.items():
                        if not isinstance(pname, str) or not isinstance(entries, list):
                            continue
                        items: list[dict[str, Any]] = []
                        for item in entries[:20]:
                            if isinstance(item, dict) and item.get("ip"):
                                ip_str = str(item.get("ip", "")).strip()
                                entry_d: dict[str, Any] = {
                                    "ip": ip_str,
                                    "country": str(item.get("country", "")).strip().upper(),
                                    "tz": str(item.get("tz", "")).strip(),
                                    "port": str(item.get("port", "")).strip(),
                                    "ts": str(item.get("ts", "")).strip(),
                                    "isp": str(item.get("isp", "")).strip(),
                                    "ip_type": str(item.get("ip_type", "")).strip().upper(),
                                }
                                if item.get("fraud_score") is not None:
                                    with contextlib.suppress(TypeError, ValueError):
                                        entry_d["fraud_score"] = int(item["fraud_score"])
                                if item.get("scamalytics") is not None:
                                    with contextlib.suppress(TypeError, ValueError):
                                        entry_d["scamalytics"] = int(item["scamalytics"])
                                if item.get("proxycheck_risk") is not None:
                                    with contextlib.suppress(TypeError, ValueError):
                                        entry_d["proxycheck_risk"] = int(item["proxycheck_risk"])
                                if item.get("proxy_flag"):
                                    entry_d["proxy_flag"] = bool(item["proxy_flag"])
                                if item.get("asn"):
                                    entry_d["asn"] = str(item["asn"]).strip()
                                items.append(entry_d)
                        if items:
                            clean_hist[pname] = items
                    self._ip_history = clean_hist
                raw_sess = raw.get("session_history")
                if isinstance(raw_sess, dict):
                    clean_sess: dict[str, list[dict[str, Any]]] = {}
                    for pname, entries in raw_sess.items():
                        if not isinstance(pname, str) or not isinstance(entries, list):
                            continue
                        s_items: list[dict[str, Any]] = []
                        for item in entries[-50:]:
                            if (
                                isinstance(item, dict)
                                and item.get("start")
                                and item.get("end")
                            ):
                                s_items.append(
                                    {
                                        "start": str(item.get("start", "")).strip(),
                                        "end": str(item.get("end", "")).strip(),
                                        "duration_mins": int(item.get("duration_mins") or 0),
                                        "port": str(item.get("port", "")).strip(),
                                        "is_live": False,
                                    }
                                )
                        if s_items:
                            clean_sess[pname] = s_items
                    self._live_sessions = clean_sess
                raw_pairs = raw.get("last_pairings")
                if isinstance(raw_pairs, list):
                    clean_pairs: list[list[str]] = []
                    for pr in raw_pairs:
                        if isinstance(pr, list) and len(pr) == 2 and pr[0] and pr[1]:
                            clean_pairs.append([str(pr[0]), str(pr[1])])
                    self._last_pairings = clean_pairs
                raw_shifts = raw.get("last_shifts")
                if isinstance(raw_shifts, dict):
                    self._last_shifts = {
                        str(k): str(v)
                        for k, v in raw_shifts.items()
                        if isinstance(k, str) and v in ("morning", "evening")
                    }
                raw_cyc = raw.get("schedule_cycle_days")
                if isinstance(raw_cyc, int) and 2 <= raw_cyc <= 7:
                    self._schedule_cycle_days = raw_cyc
                raw_squads = raw.get("fixed_work_squads")
                if isinstance(raw_squads, dict):
                    clean_squads: dict[str, list[str]] = {}
                    for sk, members in raw_squads.items():
                        if isinstance(sk, str) and isinstance(members, list):
                            m_list = [str(m).strip() for m in members if str(m).strip()]
                            if m_list:
                                clean_squads[sk] = m_list
                    self._fixed_work_squads = clean_squads
                raw_disk = raw.get("disk_cache")
                if isinstance(raw_disk, dict):
                    clean_disk: dict[str, dict[str, Any]] = {}
                    for pname, dval in raw_disk.items():
                        if (
                            isinstance(pname, str)
                            and isinstance(dval, dict)
                            and dval.get("_cache_ver") == 2
                        ):
                            clean_disk[pname] = dict(dval)
                    self._disk_cache = clean_disk

    def _save_settings(self) -> None:
        path = self._settings_file()
        with contextlib.suppress(Exception):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            payload = {
                "cooldown_minutes": self._cooldown_minutes,
                "last_used_ports": self._last_used_ports,
                "ip_history": self._ip_history,
                "ip_scores": self._ip_scores,
                "session_history": {
                    k: v[-50:] for k, v in self._live_sessions.items() if v
                },
                "last_pairings": self._last_pairings,
                "last_shifts": self._last_shifts,
                "schedule_cycle_days": self._schedule_cycle_days,
                "fixed_work_squads": self._fixed_work_squads,
                "disk_cache": {
                    k: v
                    for k, v in self._disk_cache.items()
                    if isinstance(v, dict) and not v.get("_is_running")
                },
            }
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)

    @property
    def cooldown_minutes(self) -> int:
        return self._cooldown_minutes

    def set_cooldown_minutes(self, minutes: int) -> None:
        self._cooldown_minutes = max(0, min(1440, int(minutes)))
        self._save_settings()

    @property
    def last_pairings(self) -> list[list[str]]:
        return list(self._last_pairings)

    @property
    def last_shifts(self) -> dict[str, str]:
        return dict(self._last_shifts)

    @property
    def schedule_cycle_days(self) -> int:
        return self._schedule_cycle_days

    @property
    def fixed_work_squads(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._fixed_work_squads.items()}

    def set_fixed_work_squad(self, key: str, names: list[str] | None) -> None:
        """Pin or unpin a fixed parallel work squad (key format: '<size>:<squad_idx>')."""
        clean_key = (key or "").strip()
        if not clean_key:
            return
        if not names:
            if self._fixed_work_squads.pop(clean_key, None) is not None:
                self._save_settings()
            return
        clean_names = [str(n).strip() for n in names if str(n).strip()]
        if clean_names:
            self._fixed_work_squads[clean_key] = clean_names
            self._save_settings()

    def record_schedule_applied(
        self,
        pairings: list[list[str]],
        shifts: dict[str, str],
        cycle_days: int = 5,
    ) -> None:
        """Persist the applied schedule's pairings and shifts for next-cycle anti-correlation."""
        self._last_pairings = [
            [str(pr[0]), str(pr[1])]
            for pr in (pairings or [])
            if isinstance(pr, (list, tuple)) and len(pr) == 2 and pr[0] and pr[1]
        ]
        self._last_shifts = {
            str(k): str(v)
            for k, v in (shifts or {}).items()
            if k and v in ("morning", "evening")
        }
        self._schedule_cycle_days = max(2, min(7, int(cycle_days)))
        self._save_settings()

    def acknowledge_proxy_change(self, profile_name: str, proxy: str | None) -> None:
        port = extract_proxy_port(proxy)
        if port:
            self._last_used_ports[profile_name] = port
        else:
            self._last_used_ports.pop(profile_name, None)
        if profile_name in self._ip_history and len(self._ip_history[profile_name]) > 1:
            self._ip_history[profile_name] = self._ip_history[profile_name][:1]
        self._save_settings()

    def record_ip_risk(self, ip: str, risk_data: dict[str, Any]) -> None:
        """Persist IP risk/fraud score outside camoufox_data/<Profile>/ and enrich matching history."""
        clean_ip = (ip or "").strip()
        if not clean_ip or not isinstance(risk_data, dict):
            return
        stored = dict(self._ip_scores.get(clean_ip, {}))
        for k in (
            "fraud_score",
            "scamalytics",
            "proxycheck_risk",
            "proxy_flag",
            "ip_type",
            "detail_type",
            "isp",
            "asn",
        ):
            val = risk_data.get(k)
            if val is not None and val != "":
                stored[k] = val
        stored["ts"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._ip_scores[clean_ip] = stored
        for entries in self._ip_history.values():
            for item in entries:
                if item.get("ip") == clean_ip:
                    if stored.get("fraud_score") is not None:
                        item["fraud_score"] = stored["fraud_score"]
                    if stored.get("scamalytics") is not None:
                        item["scamalytics"] = stored["scamalytics"]
                    if stored.get("proxycheck_risk") is not None:
                        item["proxycheck_risk"] = stored["proxycheck_risk"]
                    if "proxy_flag" in stored:
                        item["proxy_flag"] = bool(stored["proxy_flag"])
                    if stored.get("isp") and not item.get("isp"):
                        item["isp"] = str(stored["isp"])
                    if stored.get("ip_type") and not item.get("ip_type"):
                        item["ip_type"] = str(stored["ip_type"]).upper()
                    if stored.get("asn"):
                        item["asn"] = str(stored["asn"])
        self._save_settings()

    def get_ip_risk(self, ip: str | None) -> dict[str, Any]:
        clean_ip = (ip or "").strip()
        if not clean_ip:
            return {}
        return dict(self._ip_scores.get(clean_ip, {}))

    def record_exit_ip(
        self,
        profile_name: str,
        ip: str,
        *,
        country: str = "",
        tz: str = "",
        proxy: str | None = None,
        isp: str = "",
        ip_type: str = "",
        fraud_score: int | None = None,
        scamalytics: int | None = None,
        proxycheck_risk: int | None = None,
        proxy_flag: bool = False,
        asn: str = "",
    ) -> dict[str, Any] | None:
        """Record an exit IP check for `profile_name` (stored outside camoufox_data/<Profile>/)."""
        clean_ip = (ip or "").strip()
        if not clean_ip or not profile_name:
            return None
        clean_country = (country or "").strip().upper()
        if clean_country in ("XX", "T1"):
            clean_country = ""
        clean_tz = (tz or "").strip()
        clean_isp = (isp or "").strip()
        clean_type = (ip_type or "").strip().upper()
        clean_asn = (asn or "").strip()
        if fraud_score is not None or scamalytics is not None or proxycheck_risk is not None:
            self._ip_scores[clean_ip] = {
                "fraud_score": fraud_score,
                "scamalytics": scamalytics,
                "proxycheck_risk": proxycheck_risk,
                "proxy_flag": bool(proxy_flag),
                "ip_type": clean_type,
                "isp": clean_isp,
                "asn": clean_asn,
                "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        cached_risk = self._ip_scores.get(clean_ip, {})
        eff_fraud = fraud_score if fraud_score is not None else cached_risk.get("fraud_score")
        eff_scam = scamalytics if scamalytics is not None else cached_risk.get("scamalytics")
        eff_pc = (
            proxycheck_risk if proxycheck_risk is not None else cached_risk.get("proxycheck_risk")
        )
        eff_flag = bool(proxy_flag or cached_risk.get("proxy_flag"))
        eff_asn = clean_asn or str(cached_risk.get("asn") or "")
        if not clean_isp and cached_risk.get("isp"):
            clean_isp = str(cached_risk["isp"])
        if not clean_type and cached_risk.get("ip_type"):
            clean_type = str(cached_risk["ip_type"]).upper()

        port = extract_proxy_port(proxy)
        now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entries = self._ip_history.setdefault(profile_name, [])
        if (
            entries
            and entries[0].get("ip") == clean_ip
            and (not port or entries[0].get("port", "") == port)
        ):
            entries[0]["ts"] = now_str
            if clean_country:
                entries[0]["country"] = clean_country
            if clean_tz:
                entries[0]["tz"] = clean_tz
            if port:
                entries[0]["port"] = port
            if clean_isp:
                entries[0]["isp"] = clean_isp
            if clean_type:
                entries[0]["ip_type"] = clean_type
            if eff_fraud is not None:
                entries[0]["fraud_score"] = eff_fraud
            if eff_scam is not None:
                entries[0]["scamalytics"] = eff_scam
            if eff_pc is not None:
                entries[0]["proxycheck_risk"] = eff_pc
            if eff_flag:
                entries[0]["proxy_flag"] = True
            if eff_asn:
                entries[0]["asn"] = eff_asn
            out = dict(entries[0])
        else:
            entry: dict[str, Any] = {
                "ip": clean_ip,
                "country": clean_country,
                "tz": clean_tz,
                "port": port,
                "ts": now_str,
                "isp": clean_isp,
                "ip_type": clean_type,
            }
            if eff_fraud is not None:
                entry["fraud_score"] = eff_fraud
            if eff_scam is not None:
                entry["scamalytics"] = eff_scam
            if eff_pc is not None:
                entry["proxycheck_risk"] = eff_pc
            if eff_flag:
                entry["proxy_flag"] = True
            if eff_asn:
                entry["asn"] = eff_asn
            entries.insert(0, entry)
            del entries[20:]
            out = dict(entry)
        self._save_settings()
        return out

    def get_ip_history(self, profile_name: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return recorded exit IP entries for `profile_name` (newest first)."""
        out: list[dict[str, Any]] = []
        for item in self._ip_history.get(profile_name, [])[:limit]:
            d = dict(item)
            ip_k = str(d.get("ip") or "").strip()
            if ip_k and ip_k in self._ip_scores:
                sc = self._ip_scores[ip_k]
                if d.get("fraud_score") is None and sc.get("fraud_score") is not None:
                    d["fraud_score"] = sc["fraud_score"]
                if d.get("scamalytics") is None and sc.get("scamalytics") is not None:
                    d["scamalytics"] = sc["scamalytics"]
                if d.get("proxycheck_risk") is None and sc.get("proxycheck_risk") is not None:
                    d["proxycheck_risk"] = sc["proxycheck_risk"]
                if sc.get("proxy_flag"):
                    d["proxy_flag"] = True
                if not d.get("asn") and sc.get("asn"):
                    d["asn"] = sc["asn"]
            out.append(d)
        return out

    def clear_ip_history(self, profile_name: str) -> None:
        """Clear recorded exit IP history for `profile_name`."""
        if self._ip_history.pop(profile_name, None) is not None:
            self._save_settings()

    def rename_profile(self, old_name: str, new_name: str) -> None:
        """Migrate monitor metadata when a profile is renamed."""
        if not old_name or not new_name or old_name == new_name:
            return
        changed = False
        merged_before = self._merged_completed_sessions(old_name)
        if old_name in self._last_used_ports:
            self._last_used_ports[new_name] = self._last_used_ports.pop(old_name)
            changed = True
        if old_name in self._ip_history:
            self._ip_history[new_name] = self._ip_history.pop(old_name)
            changed = True
        if old_name in self._live_started_at:
            self._live_started_at[new_name] = self._live_started_at.pop(old_name)
        if old_name in self._live_stopped_at:
            self._live_stopped_at[new_name] = self._live_stopped_at.pop(old_name)
        if old_name in self._live_sessions:
            prev_sess = self._live_sessions.pop(old_name)
            self._live_sessions.setdefault(new_name, []).extend(prev_sess)
            changed = True
        if merged_before and len(merged_before) > len(self._live_sessions.get(new_name, [])):
            self._live_sessions[new_name] = list(reversed(merged_before[:50]))
            changed = True
        self._log_cache_key = ()
        self._log_history.clear()
        if old_name in self._disk_cache:
            self._disk_cache[new_name] = self._disk_cache.pop(old_name)
        if old_name in self._last_shifts:
            self._last_shifts[new_name] = self._last_shifts.pop(old_name)
            changed = True
        for pr in self._last_pairings:
            if len(pr) == 2:
                if pr[0] == old_name:
                    pr[0] = new_name
                    changed = True
                if pr[1] == old_name:
                    pr[1] = new_name
                    changed = True
        for sq_list in self._fixed_work_squads.values():
            for idx, m_name in enumerate(sq_list):
                if m_name == old_name:
                    sq_list[idx] = new_name
                    changed = True
        if changed:
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
            sessions = self._live_sessions.setdefault(profile_name, [])
            sessions.append(
                {
                    "start": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "end": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "duration_mins": max(1, round(dur / 60)) if dur >= 10 else 0,
                    "port": self._last_used_ports.get(profile_name, ""),
                    "is_live": False,
                }
            )
            del sessions[:-50]
            self._save_settings()

    def _merged_completed_sessions(
        self,
        profile_name: str,
        hist: dict[str, dict[str, Any]] | None = None,
        fallback_port: str = "",
    ) -> list[dict[str, Any]]:
        if hist is None:
            hist = self._refresh_log_history()
        h = hist.get(profile_name, {})
        port = self._last_used_ports.get(profile_name, "") or fallback_port
        merged: list[dict[str, Any]] = []
        accepted_intervals: list[tuple[dt.datetime | None, dt.datetime | None, str]] = []

        def _parse_ts(val: str) -> dt.datetime | None:
            if not val:
                return None
            with contextlib.suppress(ValueError):
                return dt.datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
            return None

        candidates = list(h.get("sessions", [])) + list(
            self._live_sessions.get(profile_name, [])
        )
        for s in candidates:
            st = str(s.get("start", "")).strip()
            en = str(s.get("end", "")).strip()
            if not st:
                continue
            st_dt = _parse_ts(st)
            en_dt = _parse_ts(en)
            is_dup = False
            for idx, (ast_dt, aen_dt, ast_str) in enumerate(accepted_intervals):
                if (
                    st == ast_str
                    or (
                        st_dt is not None
                        and ast_dt is not None
                        and abs((st_dt - ast_dt).total_seconds()) <= 45.0
                    )
                    or (
                        en_dt is not None
                        and aen_dt is not None
                        and abs((en_dt - aen_dt).total_seconds()) <= 45.0
                    )
                ):
                    is_dup = True
                    if not merged[idx].get("port") and (s.get("port") or port):
                        merged[idx]["port"] = str(s.get("port") or port)
                    break
            if not is_dup:
                entry = dict(s)
                if not entry.get("port") and port:
                    entry["port"] = port
                entry["is_live"] = False
                merged.append(entry)
                accepted_intervals.append((st_dt, en_dt, st))

        merged.sort(key=lambda item: str(item.get("start", "")), reverse=True)
        return merged

    def get_profile_sessions(
        self,
        profile_name: str,
        limit: int = 30,
        *,
        is_running: bool | None = None,
        current_proxy: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent browser sessions for `profile_name` (newest first)."""
        hist = self._refresh_log_history()
        h = hist.get(profile_name, {})
        fallback_port = extract_proxy_port(current_proxy)
        port = self._last_used_ports.get(profile_name, "") or fallback_port
        merged = self._merged_completed_sessions(
            profile_name,
            hist=hist,
            fallback_port=fallback_port,
        )

        show_live = (
            is_running
            if is_running is not None
            else (profile_name in self._live_started_at)
        )
        live_start = self._live_started_at.get(profile_name) or h.get("_open_start")
        if show_live and live_start is not None:
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
        log_files = sorted(self._log_dir.glob("foxprofile_*.log"))[-30:]
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

        def _close_session(h: dict[str, Any], pname: str, ts: dt.datetime, ts_str: str) -> None:
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
                if not msg:
                    continue

                mr = _RE_RENAME_PROFILE.match(msg)
                if mr:
                    old_p = mr.group("old").strip()
                    new_p = mr.group("new").strip()
                    if old_p != new_p:
                        old_prefix = f"{old_p} ("
                        match_keys = [
                            k
                            for k in list(hist.keys())
                            if k == old_p or (k.startswith(old_prefix) and k.endswith(")"))
                        ]
                        if match_keys:
                            nh = _ensure(new_p)
                            new_port = self._last_used_ports.get(new_p, "")
                            existing_starts = {s["start"] for s in nh["sessions"]}
                            for mk in match_keys:
                                oh = hist.pop(mk)
                                for s_item in oh["sessions"]:
                                    if s_item["start"] not in existing_starts:
                                        copied = dict(s_item)
                                        if new_port:
                                            copied["port"] = new_port
                                        nh["sessions"].append(copied)
                                        existing_starts.add(s_item["start"])
                                nh["today_seconds"] += oh["today_seconds"]
                                if oh["last_closed"] and (
                                    not nh["last_closed"] or oh["last_closed"] > nh["last_closed"]
                                ):
                                    nh["last_closed"] = oh["last_closed"]
                                if oh["_open_start"] and not nh["_open_start"]:
                                    nh["_open_start"] = oh["_open_start"]
                            nh["sessions"].sort(key=lambda item: str(item.get("start", "")))
                    continue

                mt = _RE_TRASH_PROFILE.match(msg)
                if mt:
                    hist.pop(mt.group("prof").strip(), None)
                    continue

                mb = _RE_BRACKET_PROFILE.match(msg)
                if mb:
                    pname = mb.group("prof").strip()
                    rest = mb.group("rest").strip()
                    if (
                        rest.startswith("Starting browser for ")
                        or rest.startswith("\u0110ang kh\u1edfi \u0111\u1ed9ng tr\u00ecnh duy\u1ec7t cho ")
                        or rest == "BROWSER_STARTED"
                    ):
                        try:
                            ts = dt.datetime.fromisoformat(ts_str)
                        except ValueError:
                            continue
                        h = _ensure(pname)
                        if h["_open_start"] is None or (
                            rest != "BROWSER_STARTED"
                            and (ts - h["_open_start"]).total_seconds() > 120.0
                        ):
                            h["_open_start"] = ts
                    elif rest in ("BROWSER_CLOSED", "LAUNCH_CANCELLED") or rest.startswith(
                        "LAUNCH_FAILED:"
                    ):
                        try:
                            ts = dt.datetime.fromisoformat(ts_str)
                        except ValueError:
                            continue
                        h = _ensure(pname)
                        if rest == "BROWSER_CLOSED":
                            _close_session(h, pname, ts, ts_str)
                        else:
                            h["_open_start"] = None
                    continue

                mst = _RE_STARTING.match(msg)
                if mst:
                    try:
                        ts = dt.datetime.fromisoformat(ts_str)
                    except ValueError:
                        continue
                    pname = mst.group("prof").strip()
                    h = _ensure(pname)
                    is_ready_line = msg.startswith("Browser started for profile:")
                    if h["_open_start"] is None or (
                        not is_ready_line
                        and (ts - h["_open_start"]).total_seconds() > 120.0
                    ):
                        h["_open_start"] = ts
                    continue

                mlf = _RE_LAUNCH_FAILED.match(msg)
                if mlf:
                    pname = mlf.group("prof").strip()
                    h = _ensure(pname)
                    h["_open_start"] = None
                    continue

                ms = (
                    _RE_STOPPING.match(msg)
                    or _RE_TERMINATED.match(msg)
                    or _RE_SESSION_ENDED.match(msg)
                )
                if ms:
                    try:
                        ts = dt.datetime.fromisoformat(ts_str)
                    except ValueError:
                        continue
                    pname = next(g for g in ms.groups() if g is not None).strip()
                    h = _ensure(pname)
                    _close_session(h, pname, ts, ts_str)

        # Merge legacy "<BaseName> (<email_or_note>)" log entries into "<BaseName>"
        for k in list(hist.keys()):
            if " (" in k and k.endswith(")"):
                base = k.split(" (", 1)[0].strip()
                if base and base != k:
                    nh = _ensure(base)
                    oh = hist[k]
                    base_port = self._last_used_ports.get(base, "")
                    existing_starts = {s["start"] for s in nh["sessions"]}
                    for s_item in oh["sessions"]:
                        if s_item["start"] not in existing_starts:
                            copied = dict(s_item)
                            if not copied.get("port") and base_port:
                                copied["port"] = base_port
                            nh["sessions"].append(copied)
                            existing_starts.add(s_item["start"])
                    nh["sessions"].sort(key=lambda item: str(item.get("start", "")))
                    nh["today_seconds"] += oh["today_seconds"]
                    if oh["last_closed"] and (
                        not nh["last_closed"] or oh["last_closed"] > nh["last_closed"]
                    ):
                        nh["last_closed"] = oh["last_closed"]

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
                "hatch_vml": False,
                "hatch_sess": False,
                "sess_exp": "-",
                "burst_10m": 0,
                "last_active_ts": 0.0,
                "has_grecaptcha": False,
                "has_meta": False,
                "has_microsoft": False,
                "has_cache2": False,
                "has_ps_l": False,
                "has_meta_checkpoint": False,
                "datr_age_days": 0.0,
                "datr_domains": 0,
                "ext_domains": 0,
                "cookie_count": 0,
                "cookie_domains": 0,
                "history_pages": 0,
                "history_domains": 0,
                "fp_hash": "",
                "fp_hw_sig": "",
            }

        tabs_file = pdir / "tabs.json"
        cookies_db = pdir / "cookies.sqlite"
        places_db = pdir / "places.sqlite"
        prefs_file = pdir / "prefs.js"
        fp_file = pdir / "fingerprint.json"
        has_cache2 = (pdir / "cache2").is_dir()
        try:
            tabs_mtime = tabs_file.stat().st_mtime if tabs_file.exists() else 0.0
        except OSError:
            tabs_mtime = 0.0
        try:
            cookies_mtime = cookies_db.stat().st_mtime if cookies_db.exists() else 0.0
        except OSError:
            cookies_mtime = 0.0
        try:
            places_mtime = places_db.stat().st_mtime if places_db.exists() else 0.0
        except OSError:
            places_mtime = 0.0
        try:
            prefs_mtime = prefs_file.stat().st_mtime if prefs_file.exists() else 0.0
        except OSError:
            prefs_mtime = 0.0
        try:
            fp_mtime = fp_file.stat().st_mtime if fp_file.exists() else 0.0
        except OSError:
            fp_mtime = 0.0
        last_active_ts = max(tabs_mtime, cookies_mtime, prefs_mtime)

        if cached is not None:
            age = now_ts - cached.get("_ts", 0.0)
            ct_sec = float(cached.get("_datr_ctime_sec") or 0.0)
            if 0 < ct_sec <= now_ts:
                cached["datr_age_days"] = round((now_ts - ct_sec) / 86400.0, 2)
            latest_exp_sec = float(cached.get("_latest_exp_sec") or 0.0)
            if latest_exp_sec > 0:
                rem_h = (latest_exp_sec - now_ts) / 3600.0
                if rem_h < 0:
                    cached["sess_exp"] = "Hết hạn"
                elif rem_h < 24:
                    cached["sess_exp"] = f"Còn {int(rem_h)}h"
                else:
                    cached["sess_exp"] = f"Còn {int(rem_h // 24)}d"
            if not is_running:
                if (
                    cached.get("_cache_ver") == 2
                    and cached.get("_tabs_mtime") == tabs_mtime
                    and cached.get("_cookies_mtime") == cookies_mtime
                    and cached.get("_places_mtime") == places_mtime
                    and cached.get("_fp_mtime") == fp_mtime
                ):
                    cached["burst_10m"] = 0
                    cached["last_active_ts"] = last_active_ts
                    cached["has_cache2"] = has_cache2
                    return cached
            elif (
                cached.get("_is_running") is True
                and cached.get("_tabs_mtime") == tabs_mtime
                and age < 15.0
            ):
                cached["last_active_ts"] = last_active_ts
                cached["has_cache2"] = has_cache2
                return cached

        result: dict[str, Any] = {
            "_cache_ver": 2,
            "_ts": now_ts,
            "_is_running": is_running,
            "_tabs_mtime": tabs_mtime,
            "_cookies_mtime": -1.0 if is_running else cookies_mtime,
            "_places_mtime": -1.0 if is_running else places_mtime,
            "_fp_mtime": fp_mtime,
            "_datr_ctime_sec": cached.get("_datr_ctime_sec", 0.0) if cached else 0.0,
            "_latest_exp_sec": cached.get("_latest_exp_sec", 0.0) if cached else 0.0,
            "has_thread_url": False,
            "tab_count": 0,
            "hatch_gw": cached.get("hatch_gw", False) if cached else False,
            "hatch_sess": cached.get("hatch_sess", False) if cached else False,
            "has_muse_auth": cached.get("has_muse_auth", False) if cached else False,
            "sess_exp": cached.get("sess_exp", "-") if cached else "-",
            "burst_10m": 0,
            "last_active_ts": last_active_ts,
            "has_grecaptcha": cached.get("has_grecaptcha", False) if cached else False,
            "has_meta": cached.get("has_meta", False) if cached else False,
            "has_microsoft": cached.get("has_microsoft", False) if cached else False,
            "has_cache2": has_cache2,
            "hatch_vml": cached.get("hatch_vml", False) if cached else False,
            "has_ps_l": cached.get("has_ps_l", False) if cached else False,
            "has_meta_checkpoint": (
                cached.get("has_meta_checkpoint", False) if cached else False
            ),
            "datr_age_days": cached.get("datr_age_days", 0.0) if cached else 0.0,
            "datr_domains": cached.get("datr_domains", 0) if cached else 0,
            "ext_domains": cached.get("ext_domains", 0) if cached else 0,
            "cookie_count": cached.get("cookie_count", 0) if cached else 0,
            "cookie_domains": cached.get("cookie_domains", 0) if cached else 0,
            "history_pages": cached.get("history_pages", 0) if cached else 0,
            "history_domains": cached.get("history_domains", 0) if cached else 0,
            "fp_hash": (cached.get("fp_hash", "") if cached else "") if fp_mtime > 0 else "",
            "fp_hw_sig": (cached.get("fp_hw_sig", "") if cached else "") if fp_mtime > 0 else "",
            "device_label": (cached.get("device_label", "") if cached else "") if fp_mtime > 0 else "",
        }

        if fp_mtime > 0 and (
            not cached
            or cached.get("_fp_mtime") != fp_mtime
            or not cached.get("device_label")
        ):
            try:
                raw_fp = fp_file.read_text(encoding="utf-8", errors="replace")
                fp_obj = json.loads(raw_fp)
                fp_inner = fp_obj.get("fingerprint", {}) if isinstance(fp_obj, dict) else {}
                if isinstance(fp_inner, dict) and fp_inner:
                    norm_fp = json.dumps(fp_inner, sort_keys=True, separators=(",", ":"))
                    result["fp_hash"] = hashlib.sha1(norm_fp.encode("utf-8")).hexdigest()[:16]
                    nav = fp_inner.get("navigator") or {}
                    scr = fp_inner.get("screen") or {}
                    vc = fp_inner.get("videoCard") or fp_inner.get("gpu") or fp_inner.get("webgl") or {}
                    plat = str(nav.get("platform") or fp_obj.get("os") or "")
                    w = scr.get("width") or ""
                    h_px = scr.get("height") or ""
                    cores = nav.get("hardwareConcurrency") or ""
                    rend = str(
                        (vc.get("renderer") if isinstance(vc, dict) else "")
                        or fp_inner.get("webgl:renderer")
                        or ""
                    )[:48]
                    if plat and w and h_px:
                        result["fp_hw_sig"] = f"{plat}|{w}x{h_px}|{cores}c|{rend}"
                    scr_lbl = f"{w}x{h_px}" if w else ""
                    cpu_lbl = f"{cores} CPU" if cores is not None and cores != "" else ""
                    result["device_label"] = (
                        f"{scr_lbl} · {cpu_lbl}" if (scr_lbl and cpu_lbl) else cpu_lbl
                    )
            except Exception:
                pass

        if tabs_mtime > 0:
            try:
                raw = json.loads(tabs_file.read_text(encoding="utf-8", errors="replace"))
                urls = raw if isinstance(raw, list) else (raw.get("tabs") or raw.get("urls") or [])
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
                    try:
                        cur.execute(
                            "SELECT host, name, creationTime, expiry FROM moz_cookies"
                        )
                        rows = cur.fetchall()
                    except sqlite3.OperationalError:
                        cur.execute("SELECT host, name, 0, expiry FROM moz_cookies")
                        rows = cur.fetchall()
                finally:
                    conn.close()

                hatch_gw = False
                hatch_vml = False
                hatch_sess = False
                has_muse_onetap = False
                has_muse_native_dev = False
                has_muse_layout = False
                has_grecaptcha = False
                has_meta = False
                has_microsoft = False
                has_ps_l = False
                has_chk_cookie = False
                datr_age_days = 0.0
                earliest_datr_ct = 0.0
                datr_hosts: set[str] = set()
                cookie_hosts: set[str] = set()
                latest_exp = 0.0
                for _host, cname, ctime, exp in rows:
                    host_l = str(_host or "").lower().lstrip(".")
                    if host_l:
                        cookie_hosts.add(_extract_root_domain(host_l) or host_l)
                    is_hatch_host = "muse" in host_l or "hatch" in host_l
                    cname_s = str(cname or "")
                    cname_l = cname_s.lower()
                    if cname_s == "_GRECAPTCHA" or "recaptcha" in cname_l:
                        has_grecaptcha = True
                    if any(d in host_l for d in ("meta.com", "facebook.com", "instagram.com")):
                        has_meta = True
                    if any(
                        d in host_l
                        for d in ("live.com", "microsoft.com", "office.com", "outlook.com")
                    ):
                        has_microsoft = True
                    if cname_s in ("ps_l", "ps_n"):
                        has_ps_l = True
                    if "checkpoint" in cname_l:
                        has_chk_cookie = True
                    if is_hatch_host and cname_s == "__Host-hatch_one_tap":
                        has_muse_onetap = True
                    if is_hatch_host and cname_s == "hatch_native_auth_device":
                        has_muse_native_dev = True
                    if is_hatch_host and cname_s == "layout_state":
                        has_muse_layout = True
                    if cname_s == "datr":
                        if host_l:
                            datr_hosts.add(host_l)
                        if ctime:
                            ct_val = float(ctime)
                            while ct_val > 10_000_000_000:
                                ct_val /= 1000.0
                            if 0 < ct_val <= now_ts:
                                datr_age_days = max(
                                    datr_age_days,
                                    (now_ts - ct_val) / 86400.0,
                                )
                                if earliest_datr_ct == 0.0 or ct_val < earliest_datr_ct:
                                    earliest_datr_ct = ct_val
                    if cname_s == "hatch_vml":
                        hatch_vml = True
                    elif cname_s == "hatch_gw" or (
                        is_hatch_host and cname_s in ("cf_clearance", "__cf_bm")
                    ):
                        hatch_gw = True
                    elif cname_s == "hatch_sess" or (
                        "checkpoint" not in cname_l
                        and any(
                            k in cname_l
                            for k in (
                                "session",
                                "token",
                                "auth",
                                "user",
                                "clerk",
                                "sb-",
                            )
                        )
                    ):
                        hatch_sess = True
                        if exp:
                            exp_val = float(exp)
                            while exp_val > 10_000_000_000:
                                exp_val /= 1000.0
                            latest_exp = max(latest_exp, exp_val)
                if (
                    not hatch_gw
                    and has_muse_onetap
                    and has_muse_layout
                    and (hatch_vml or result["has_thread_url"])
                ):
                    hatch_gw = True
                    hatch_vml = True
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
                result["hatch_vml"] = hatch_vml
                result["hatch_sess"] = hatch_sess
                result["has_muse_auth"] = bool(has_muse_onetap or has_muse_native_dev)
                result["sess_exp"] = sess_exp
                result["has_grecaptcha"] = has_grecaptcha
                result["has_meta"] = has_meta
                result["has_microsoft"] = has_microsoft
                result["has_ps_l"] = has_ps_l
                result["has_meta_checkpoint"] = has_chk_cookie
                result["_datr_ctime_sec"] = earliest_datr_ct
                result["_latest_exp_sec"] = latest_exp
                result["datr_age_days"] = round(datr_age_days, 2)
                result["datr_domains"] = len(datr_hosts)
                result["cookie_count"] = len(rows)
                result["cookie_domains"] = len(cookie_hosts)
            except Exception:
                pass

        # Only read places.sqlite when the profile's browser is NOT running
        if not is_running and places_mtime > 0:
            try:
                pl_posix = places_db.resolve().as_posix()
                pl_uri = f"file:///{urllib.parse.quote(pl_posix, safe=':/')}?mode=ro&immutable=1"
                conn_pl = sqlite3.connect(pl_uri, uri=True, timeout=0.3)
                try:
                    cur_pl = conn_pl.cursor()
                    cur_pl.execute(
                        "SELECT url, rev_host FROM moz_places WHERE visit_count > 0"
                    )
                    pl_rows = cur_pl.fetchall()
                finally:
                    conn_pl.close()

                result["history_pages"] = len(pl_rows)
                all_rev_hosts: set[str] = set()
                ext_rev_hosts: set[str] = set()
                has_chk_url = False
                for u, rh in pl_rows:
                    u_l = str(u or "").lower()
                    rh_s = str(rh or "")
                    if (
                        "auth.meta.com/checkpoint" in u_l
                        or "oidc/start?checkpoint=1" in u_l
                        or "auth.meta.com/recover" in u_l
                    ):
                        has_chk_url = True
                    host_cand = (
                        rh_s.rstrip(".")[::-1].lower()
                        if rh_s
                        else (urllib.parse.urlsplit(u_l).hostname or "")
                    )
                    rd = _extract_root_domain(host_cand)
                    if rd:
                        all_rev_hosts.add(rd)
                        if rd not in _NON_WARMUP_ROOT_DOMAINS:
                            ext_rev_hosts.add(rd)
                result["history_domains"] = len(all_rev_hosts)
                result["ext_domains"] = len(ext_rev_hosts)
                if has_chk_url:
                    result["has_meta_checkpoint"] = True
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
        if not is_running:
            self._disk_cache_dirty = True
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

        # Group profiles by proxy endpoint (host:port) and by /64 cluster so same-branch cooldowns are tracked
        banned_names: set[str] = {
            p.name
            for p in profiles
            if (
                p.banned_at
                or is_ban_status(p.status)
                or has_ban_page(p.pages)
                or is_ban_page(p.group)
            )
        }
        port_to_profiles: dict[str, list[str]] = {}
        cluster_to_profiles: dict[str, list[str]] = {}
        v6_to_profiles: dict[str, list[str]] = {}
        ip_to_profiles: dict[str, list[str]] = {}
        for p in profiles:
            ep = extract_proxy_endpoint(p.proxy)
            if ep:
                port_to_profiles.setdefault(ep, []).append(p.name)
            cl = extract_proxy_cluster(p.proxy)
            if cl and not is_gateway_ipv6_cluster(cl):
                cluster_to_profiles.setdefault(cl, []).append(p.name)
            p_entries = self._ip_history.get(p.name, [])
            p_ip_raw = (p_entries[0].get("ip", "") if p_entries else "").strip().lower()
            if p_ip_raw:
                ip_to_profiles.setdefault(p_ip_raw, []).append(p.name)
            p_v6 = _ipv6_prefix_64(p_ip_raw)
            if p_v6:
                v6_to_profiles.setdefault(p_v6, []).append(p.name)

        # Determine disk state, last_closed, and today_minutes per profile first
        today_str = now.strftime("%Y-%m-%d")
        disk_map: dict[str, dict[str, Any]] = {}
        last_closed_map: dict[str, dt.datetime | None] = {}
        today_mins_map: dict[str, int] = {}
        sess_stats_map: dict[str, tuple[int, int, int]] = {}
        ports_seeded = False
        for p in profiles:
            is_run = p.name in running_names
            disk = self._inspect_profile_disk(p.name, is_run)
            disk_map[p.name] = disk

            port = extract_proxy_port(p.proxy)
            if port and p.name in self._last_used_ports and self._last_used_ports[p.name] != port:
                m_leg = _RE_LEGACY_PORT.search((p.proxy or "").strip())
                leg_port = f":{m_leg.group(1)}" if m_leg else ""
                if leg_port and self._last_used_ports[p.name] == leg_port:
                    self._last_used_ports[p.name] = port
                    for s_live in self._live_sessions.get(p.name, []):
                        if s_live.get("port") == leg_port:
                            s_live["port"] = port
                    ports_seeded = True
            if (
                port
                and (disk.get("hatch_gw") or disk.get("hatch_sess"))
                and p.name not in self._last_used_ports
            ):
                self._last_used_ports[p.name] = port
                ports_seeded = True

            h = hist.get(p.name, {})
            completed = self._merged_completed_sessions(
                p.name,
                hist=hist,
                fallback_port=port,
            )
            lc = h.get("last_closed")
            if completed:
                with contextlib.suppress(ValueError):
                    end_dt = dt.datetime.fromisoformat(str(completed[0].get("end", "")))
                    if lc is None or end_dt > lc:
                        lc = end_dt
            live_stop = self._live_stopped_at.get(p.name)
            if live_stop and (lc is None or live_stop > lc):
                lc = live_stop
            disk_ts = float(disk.get("last_active_ts") or 0.0)
            if disk_ts > 0:
                disk_dt = dt.datetime.fromtimestamp(disk_ts)
                if lc is None or disk_dt > lc:
                    lc = disk_dt
            last_closed_map[p.name] = lc

            secs = 0.0
            total_all_mins = 0
            active_dates_set: set[str] = set()
            midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            for s_item in completed:
                en_str = str(s_item.get("end", ""))
                st_str = str(s_item.get("start", ""))
                if len(st_str) >= 10:
                    active_dates_set.add(st_str[:10])
                total_all_mins += max(1, int(s_item.get("duration_mins") or 0))
                if en_str.startswith(today_str) or st_str.startswith(today_str):
                    try:
                        st_dt = dt.datetime.fromisoformat(st_str)
                        en_dt = dt.datetime.fromisoformat(en_str)
                        eff_st = max(st_dt, midnight_today)
                        secs += max(0.0, (en_dt - eff_st).total_seconds())
                    except ValueError:
                        secs += float(s_item.get("duration_mins") or 0) * 60.0
            sess_cnt = len(completed)
            if p.name in running_names:
                sess_cnt += 1
                start_dt = self._live_started_at.get(p.name) or h.get("_open_start")
                if start_dt is not None:
                    active_dates_set.add(start_dt.strftime("%Y-%m-%d"))
                    run_s = max(0.0, (now - start_dt).total_seconds())
                    total_all_mins += int(run_s // 60)
                    today_st = max(start_dt, midnight_today)
                    secs += max(0.0, (now - today_st).total_seconds())
                else:
                    active_dates_set.add(today_str)
            today_mins_map[p.name] = int(secs // 60)
            sess_stats_map[p.name] = (
                sess_cnt,
                min(len(active_dates_set), sess_cnt),
                total_all_mins,
            )

        if ports_seeded or self._disk_cache_dirty:
            self._disk_cache_dirty = False
            self._save_settings()

        snap = MonitorSnapshot(
            free_gb=self._free_gb,
            total_ports=len(port_to_profiles),
        )

        active_ports_set: set[str] = set()
        for ep, members in port_to_profiles.items():
            running_on_port = [m for m in members if m in running_names]
            if running_on_port:
                active_ports_set.add(ep)
            if len(running_on_port) > 1:
                port_label = f":{ep.rsplit(':', 1)[-1]}" if ":" in ep else ep
                snap.proxy_collisions.append((port_label, running_on_port))
        snap.active_ports = len(active_ports_set)

        running_by_cluster: dict[str, list[str]] = {}
        running_by_v6_64: dict[str, list[str]] = {}
        running_by_exact_ip: dict[str, list[str]] = {}
        for p_run in profiles:
            if p_run.name not in running_names:
                continue
            r_cl = extract_proxy_cluster(p_run.proxy)
            if r_cl:
                running_by_cluster.setdefault(r_cl, []).append(p_run.name)
            r_entries = self._ip_history.get(p_run.name, [])
            r_ip = (r_entries[0].get("ip", "") if r_entries else "").strip().lower()
            if r_ip:
                running_by_exact_ip.setdefault(r_ip, []).append(p_run.name)
            r_v6 = _ipv6_prefix_64(r_ip)
            if r_v6:
                running_by_v6_64.setdefault(r_v6, []).append(p_run.name)

        cd_target = self._cooldown_minutes
        for p in profiles:
            is_run = p.name in running_names
            port = extract_proxy_port(p.proxy)
            ep = extract_proxy_endpoint(p.proxy)
            cluster = extract_proxy_cluster(p.proxy)
            siblings = port_to_profiles.get(ep, [p.name]) if ep else [p.name]
            busy_by = [m for m in siblings if m != p.name and m in running_names]

            lc = last_closed_map.get(p.name)
            cooldown_mins = (
                max(0, int((now - lc).total_seconds() // 60))
                if (lc is not None and not is_run)
                else None
            )
            running_mins: int | None = None
            if is_run:
                start_dt = self._live_started_at.get(p.name) or hist.get(p.name, {}).get(
                    "_open_start"
                )
                running_mins = (
                    max(0, int((now - start_dt).total_seconds() // 60))
                    if start_dt is not None
                    else 0
                )

            # Check if a sibling on the same proxy port, exact exit IP, or /64 branch closed < cooldown_minutes ago
            port_cooldown_by: tuple[str, int] | None = None
            if cd_target > 0 and not is_run and not busy_by:
                ip_ent_cd = self._ip_history.get(p.name, [])
                my_ip_cd = (ip_ent_cd[0].get("ip", "") if ip_ent_cd else "").strip().lower()
                my_v6_cd = _ipv6_prefix_64(my_ip_cd)
                cd_candidates: list[str] = list(siblings)
                if cluster and not is_gateway_ipv6_cluster(cluster):
                    for cm in cluster_to_profiles.get(cluster, []):
                        if cm not in cd_candidates:
                            cd_candidates.append(cm)
                if my_ip_cd:
                    for im in ip_to_profiles.get(my_ip_cd, []):
                        if im not in cd_candidates:
                            cd_candidates.append(im)
                if my_v6_cd:
                    for vm in v6_to_profiles.get(my_v6_cd, []):
                        if vm not in cd_candidates:
                            cd_candidates.append(vm)
                for sib in cd_candidates:
                    if (
                        sib == p.name
                        or sib in running_names
                        or (p.name not in banned_names and sib in banned_names)
                    ):
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

            ip_entries = self._ip_history.get(p.name, [])
            last_ip = ip_entries[0].get("ip", "") if ip_entries else ""
            last_country = ip_entries[0].get("country", "") if ip_entries else ""
            last_ts = ip_entries[0].get("ts", "") if ip_entries else ""
            last_isp = ip_entries[0].get("isp", "") if ip_entries else ""
            last_ip_type = ip_entries[0].get("ip_type", "") if ip_entries else ""
            prev_ip = ip_entries[1].get("ip", "") if len(ip_entries) > 1 else ""
            prev_country = ""
            for prev_entry in ip_entries[1:]:
                c = prev_entry.get("country", "")
                if c:
                    prev_country = c
                    if not prev_ip:
                        prev_ip = prev_entry.get("ip", "")
                    break
            ip_country_changed = bool(
                p.proxy and last_country and prev_country and last_country != prev_country
            )

            ip_risk_obj = self._ip_scores.get(last_ip, {}) if last_ip else {}
            entry0 = ip_entries[0] if ip_entries else {}
            raw_fraud = (
                ip_risk_obj.get("fraud_score")
                if ip_risk_obj.get("fraud_score") is not None
                else entry0.get("fraud_score")
            )
            ip_fraud_score = int(raw_fraud) if raw_fraud is not None else None
            raw_scam = (
                ip_risk_obj.get("scamalytics")
                if ip_risk_obj.get("scamalytics") is not None
                else entry0.get("scamalytics")
            )
            ip_scamalytics = int(raw_scam) if raw_scam is not None else None
            raw_pc = (
                ip_risk_obj.get("proxycheck_risk")
                if ip_risk_obj.get("proxycheck_risk") is not None
                else entry0.get("proxycheck_risk")
            )
            ip_proxycheck_risk = int(raw_pc) if raw_pc is not None else None
            ip_proxy_flag = bool(ip_risk_obj.get("proxy_flag") or entry0.get("proxy_flag"))
            ip_asn = str(ip_risk_obj.get("asn") or entry0.get("asn") or "")
            if not last_isp and ip_risk_obj.get("isp"):
                last_isp = str(ip_risk_obj["isp"])
            if not last_ip_type and ip_risk_obj.get("ip_type"):
                last_ip_type = str(ip_risk_obj["ip_type"]).upper()

            if ip_fraud_score is not None:
                snap.scored_ip_count += 1
                if ip_fraud_score <= 25 and not ip_proxy_flag:
                    snap.clean_ip_count += 1

            has_grecaptcha = bool(disk.get("has_grecaptcha"))
            has_meta = bool(disk.get("has_meta"))
            has_microsoft = bool(disk.get("has_microsoft"))
            has_cache2 = bool(disk.get("has_cache2"))
            hatch_vml = bool(disk.get("hatch_vml"))
            has_ps_l = bool(disk.get("has_ps_l"))
            has_meta_checkpoint = bool(disk.get("has_meta_checkpoint"))
            datr_age_days = float(disk.get("datr_age_days") or 0.0)
            datr_domains = int(disk.get("datr_domains") or 0)
            ext_domains = int(disk.get("ext_domains") or 0)
            cookie_count = int(disk.get("cookie_count") or 0)
            cookie_domains = int(disk.get("cookie_domains") or 0)
            history_pages = int(disk.get("history_pages") or 0)
            history_domains = int(disk.get("history_domains") or 0)
            cal_age_days = compute_days_since(p.created_at) or 0
            sess_cnt, active_days, total_mins = sess_stats_map.get(p.name, (0, 0, 0))
            if datr_age_days > 0.0:
                # Use full 24-hour days since login (`int(datr_age_days)`) unless the user
                # manually set an older historical `created_at` (>3 days older than cookie).
                if cal_age_days - datr_age_days > 3.0:
                    age_days = cal_age_days
                else:
                    age_days = int(datr_age_days)
            elif cookie_count == 0 and sess_cnt == 0 and history_pages == 0 and cal_age_days <= 3:
                age_days = 0
            else:
                age_days = cal_age_days
            eff_datr_age = (
                max(0.1, round(datr_age_days, 1))
                if datr_age_days > 0
                else round(float(age_days) if (not has_meta and cookie_count > 0) else 0.0, 1)
            )
            raw_sess_cnt = sess_cnt
            if sess_cnt == 0 and (cookie_count > 0 or history_pages > 0):
                sess_cnt = 1
                active_days = 1
            if (
                raw_sess_cnt == 0
                and eff_datr_age >= 21.0
                and cookie_count >= 25
                and (datr_domains >= 3 or ext_domains >= 4)
            ):
                # Veteran profile whose local session logs were rotated
                active_days = 16
                sess_cnt = max(sess_cnt, 28)
                total_mins = max(total_mins, 1500)
            is_banned = bool(
                p.banned_at
                or is_ban_status(p.status)
                or has_ban_page(p.pages)
                or is_ban_page(p.group)
            )

            # Calculate Strict Meta Sigma (L14/L28/L45) & Hatch OIDC Trust Score (0 - 150)
            score = 0
            # 1. Meta `datr` Device-Identity Age, Multi-Day Rhythm & Cumulative Runtime (max 95)
            # 1a. Cookie `datr` Age (max 40)
            if eff_datr_age >= 45.0:
                score += 40
            elif eff_datr_age >= 30.0:
                score += 32
            elif eff_datr_age >= 21.0:
                score += 24
            elif eff_datr_age >= 14.0:
                score += 18
            elif eff_datr_age >= 10.0:
                score += 13
            elif eff_datr_age >= 7.0:
                score += 9
            elif eff_datr_age >= 3.0:
                score += 4
            elif eff_datr_age > 0.0:
                score += 2

            # 1b. Active Days & Session Consistency (max 30)
            if active_days >= 28 and sess_cnt >= 50:
                score += 30
            elif active_days >= 20 and sess_cnt >= 35:
                score += 26
            elif active_days >= 14 and sess_cnt >= 25:
                score += 22
            elif active_days >= 10 and sess_cnt >= 18:
                score += 17
            elif active_days >= 7 and sess_cnt >= 12:
                score += 13
            elif active_days >= 5 and sess_cnt >= 7:
                score += 8
            elif active_days >= 3 and sess_cnt >= 4:
                score += 5
            elif active_days >= 2 and sess_cnt >= 2:
                score += 2

            # 1c. Cumulative Runtime Hours (max 25)
            if total_mins >= 2700:
                score += 25
            elif total_mins >= 1800:
                score += 22
            elif total_mins >= 1200:
                score += 18
            elif total_mins >= 720:
                score += 14
            elif total_mins >= 480:
                score += 11
            elif total_mins >= 240:
                score += 7
            elif total_mins >= 120:
                score += 4
            elif total_mins >= 45:
                score += 2

            # 2. Hatch VM Gateway (`hatch_gw`), VM Locator (`hatch_vml`), OIDC Session & `/thread/` (max 20)
            if disk["hatch_gw"]:
                score += 9
            if hatch_vml or (disk["hatch_gw"] and not has_meta):
                score += 4
            if disk["hatch_sess"]:
                score += 3
            if disk["has_thread_url"]:
                score += 4

            # 3. First-Party Storage (`ps_l`/`ps_n`), Meta/Muse Cross-Domain Sync, Cache2 ETag & Recovery Armor (max 15)
            if has_ps_l or (not has_meta and has_grecaptcha):
                score += 3
            if (datr_domains >= 4 and cookie_count >= 18) or cookie_count >= 45:
                score += 4
            elif (datr_domains >= 3 and cookie_count >= 14) or cookie_count >= 30:
                score += 2
            elif datr_domains >= 2 or cookie_count >= 10:
                score += 1
            if has_cache2 and cookie_count > 0:
                score += 3

            has_tfa = bool((p.two_fa or "").strip())
            has_mtok = bool((p.mail_token or "").strip())
            has_pwd_or_rec = bool(
                (p.password or "").strip() or (p.recovery_email or "").strip()
            )
            has_recovery_key = bool(has_tfa or has_mtok)
            has_recovery_armor = bool(has_tfa or (has_mtok and has_pwd_or_rec))
            if has_recovery_armor:
                score += 5
            elif has_recovery_key:
                score += 3
            elif has_pwd_or_rec:
                score += 1
            clean_env = not (
                has_meta_checkpoint or proxy_port_changed or ip_country_changed
            )

            # 4. Network Cleanliness, IP Fraud Score & Geo-Pinning (max 20)
            if p.proxy:
                if p.timezone and p.locale:
                    score += 5
                else:
                    score += 1
                if ip_fraud_score is not None:
                    ip_pts = 0
                    if ip_fraud_score <= 15 and not ip_proxy_flag:
                        ip_pts = 15
                    elif ip_fraud_score <= 30 and not ip_proxy_flag:
                        ip_pts = 12
                    elif ip_fraud_score <= 55:
                        ip_pts = 9
                    elif ip_fraud_score < 65:
                        ip_pts = 5
                    if ip_fraud_score <= 55 and clean_env:
                        if eff_datr_age >= 45.0 and active_days >= 28 and has_recovery_armor:
                            ip_pts = 15
                        elif eff_datr_age >= 30.0 and active_days >= 20 and has_recovery_key:
                            ip_pts = max(ip_pts, 12)
                    score += ip_pts
                elif last_ip:
                    score += 5

            # 5. Strict Meta Sigma Risk Penalties & Progressive Hard Gates (Safety Buffer)
            if has_meta_checkpoint:
                score -= 40
            if disk["hatch_sess"] and not disk["hatch_gw"] and not disk.get("has_muse_auth"):
                score -= 20
            if proxy_port_changed or ip_country_changed:
                score -= 35
            if disk["burst_10m"] >= TOKEN_BURST_THRESHOLD:
                score -= 20
            if sess_cnt >= 3 and (total_mins / max(1, sess_cnt)) < 5.0:
                # Ping-check bot pattern (opening & closing in < 5m per session)
                score -= 20
            elif sess_cnt >= 6 and (total_mins / max(1, sess_cnt)) < 8.0 and total_mins < 480:
                score -= 8
            if has_meta and datr_domains < 2 and cookie_count < 10:
                # Incomplete Meta/Muse cookie ecosystem state
                score -= 12

            score = max(0, min(MAX_TRUST_SCORE, score))

            stage_cap, _, _ = _compute_stage_cap_and_code(
                is_banned=is_banned,
                cookie_count=cookie_count,
                sess_cnt=sess_cnt,
                history_pages=history_pages,
                hatch_gw=bool(disk["hatch_gw"]),
                eff_datr_age=eff_datr_age,
                active_days=active_days,
                total_mins=total_mins,
                avg_mins=(total_mins / max(1, sess_cnt)) if sess_cnt > 0 else 0.0,
                has_meta=has_meta,
                datr_domains=datr_domains,
                has_ps_l=has_ps_l,
                has_thread_url=bool(disk["has_thread_url"]),
                clean_env=clean_env,
                ip_fraud_score=ip_fraud_score,
                has_recovery_key=has_recovery_key,
                has_recovery_armor=has_recovery_armor,
            )
            if stage_cap == 0:
                trust_score = 0
            elif stage_cap is not None:
                trust_score = max(1, min(stage_cap, score))
            else:
                trust_score = max(1, score)

            if is_banned:
                trust_tier = "banned"
            elif trust_score >= 131:
                trust_tier = "diamond"
                snap.diamond_count += 1
                snap.elite_count += 1
                snap.platinum_count += 1
                snap.trusted_count += 1
            elif trust_score >= 106:
                trust_tier = "elite"
                snap.elite_count += 1
                snap.platinum_count += 1
                snap.trusted_count += 1
            elif trust_score >= 81:
                trust_tier = "platinum"
                snap.platinum_count += 1
                snap.trusted_count += 1
            elif trust_score >= 56:
                trust_tier = "trusted"
                snap.trusted_count += 1
            elif trust_score >= 21:
                trust_tier = "warming"
            else:
                trust_tier = "cold"

            cluster_busy_by: list[str] = []
            cluster_collision_label = (
                cluster if (cluster and not is_gateway_ipv6_cluster(cluster)) else ""
            )
            my_ip_low = (last_ip or "").strip().lower()
            my_v6 = _ipv6_prefix_64(my_ip_low)
            if not is_run and not busy_by:
                if cluster and not is_gateway_ipv6_cluster(cluster):
                    cluster_busy_by = [
                        m for m in running_by_cluster.get(cluster, []) if m != p.name
                    ]
                if my_ip_low and my_ip_low in running_by_exact_ip:
                    for m in running_by_exact_ip[my_ip_low]:
                        if m != p.name and m not in cluster_busy_by:
                            cluster_busy_by.append(m)
                            if not cluster_collision_label:
                                cluster_collision_label = last_ip
                if my_v6 and my_v6 in running_by_v6_64:
                    for m in running_by_v6_64[my_v6]:
                        if m != p.name and m not in cluster_busy_by:
                            cluster_busy_by.append(m)
                            if not cluster_collision_label:
                                cluster_collision_label = f"{my_v6}::/64"
            if not cluster_collision_label:
                cluster_collision_label = f"{my_v6}::/64" if my_v6 else (cluster or "/64")

            can_open_parallel = bool(
                running_names
                and not is_run
                and not is_banned
                and p.proxy
                and not busy_by
                and port_cooldown_by is None
                and not cluster_busy_by
                and not proxy_port_changed
                and not ip_country_changed
                and disk["burst_10m"] < TOKEN_BURST_THRESHOLD
            )

            eff_siblings = (
                [m for m in siblings if m not in banned_names or m in running_names]
                if p.name not in banned_names
                else siblings
            )
            snap.profiles[p.name] = ProfileMonitorInfo(
                name=p.name,
                port=port,
                shared_count=max(1, len(eff_siblings)),
                last_closed=lc,
                cooldown_mins=cooldown_mins,
                running_mins=running_mins,
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
                last_exit_ip=last_ip,
                last_exit_country=last_country,
                last_exit_ts=last_ts,
                last_exit_isp=last_isp,
                last_exit_ip_type=last_ip_type,
                prev_exit_ip=prev_ip,
                prev_exit_country=prev_country,
                ip_country_changed=ip_country_changed,
                ip_fraud_score=ip_fraud_score,
                ip_scamalytics=ip_scamalytics,
                ip_proxycheck_risk=ip_proxycheck_risk,
                ip_proxy_flag=ip_proxy_flag,
                ip_asn=ip_asn,
                has_grecaptcha=has_grecaptcha,
                has_meta=has_meta,
                has_microsoft=has_microsoft,
                has_cache2=has_cache2,
                hatch_vml=hatch_vml,
                has_ps_l=has_ps_l,
                has_meta_checkpoint=has_meta_checkpoint,
                datr_age_days=round(eff_datr_age, 2),
                datr_domains=datr_domains,
                ext_domains=ext_domains,
                cookie_count=cookie_count,
                cookie_domains=cookie_domains,
                history_pages=history_pages,
                history_domains=history_domains,
                age_days=age_days,
                session_count=sess_cnt,
                active_days=active_days,
                total_minutes=total_mins,
                is_banned=is_banned,
                trust_score=trust_score,
                trust_tier=trust_tier,
                cluster=cluster,
                cluster_collision_label=cluster_collision_label,
                cluster_busy_by=cluster_busy_by,
                can_open_parallel=can_open_parallel,
                has_muse_auth=bool(disk.get("has_muse_auth")),
                has_proxy=bool(p.proxy),
                has_geo_pin=bool(p.proxy and p.timezone and p.locale),
                has_recovery_key=has_recovery_key,
                has_recovery_armor=has_recovery_armor,
            )

        self._populate_audit_findings(profiles, disk_map, snap)
        return snap

    def _populate_audit_findings(
        self,
        profiles: list[Profile],
        disk_map: dict[str, dict[str, Any]],
        snap: MonitorSnapshot,
    ) -> None:
        """Run read-only Cross-Profile Correlation & Leak Audit across all profiles."""
        findings: list[AuditFinding] = []

        def _attach(code: str, level: str, title: str, detail: str, names: list[str]) -> None:
            findings.append(
                AuditFinding(
                    code=code,
                    level=level,
                    title=title,
                    detail=detail,
                    profiles=list(names),
                )
            )
            for n in names:
                info = snap.profiles.get(n)
                if info is not None and code not in info.audit_codes:
                    info.audit_codes.append(code)

        # 1. Burned Proxy / IP / IPv6 /64 Subnet shared with explicitly Banned profiles
        banned_by_ep: dict[str, list[str]] = {}
        banned_by_ip: dict[str, list[str]] = {}
        banned_by_v6: dict[str, list[str]] = {}
        hard_banned_names: set[str] = set()
        for p in profiles:
            info = snap.profiles.get(p.name)
            if not info or not info.is_banned:
                continue
            if is_ban_status(p.status):
                hard_banned_names.add(p.name)
            ep = extract_proxy_endpoint(p.proxy)
            if ep:
                banned_by_ep.setdefault(ep, []).append(p.name)
            if info.last_exit_ip:
                banned_by_ip.setdefault(info.last_exit_ip, []).append(p.name)
                v6_pref = _ipv6_prefix_64(info.last_exit_ip)
                if v6_pref and is_ban_status(p.status):
                    banned_by_v6.setdefault(v6_pref, []).append(p.name)

        subnet_affected: dict[str, list[str]] = {}
        for p in profiles:
            info = snap.profiles.get(p.name)
            if not info or info.is_banned:
                continue
            ep = extract_proxy_endpoint(p.proxy)
            if ep and ep in banned_by_ep:
                b_list = banned_by_ep[ep]
                # Do not flag active accounts that already established a working Hatch gateway if the old account was only Disconnected
                is_working_replacement = info.hatch_gw and not info.has_meta_checkpoint
                if not is_working_replacement or any(bn in hard_banned_names for bn in b_list):
                    b_names = ", ".join(b_list)
                    _attach(
                        "burned_proxy",
                        "critical",
                        get_string("audit_burned_proxy_title", name=p.name, port=info.port or ep),
                        get_string("audit_burned_proxy_detail", name=p.name, banned=b_names, port=info.port or ep),
                        [p.name, *b_list],
                    )
            elif info.last_exit_ip and info.last_exit_ip in banned_by_ip:
                b_names = ", ".join(banned_by_ip[info.last_exit_ip])
                _attach(
                    "burned_ip",
                    "critical",
                    get_string("audit_burned_ip_title", name=p.name, ip=info.last_exit_ip),
                    get_string("audit_burned_ip_detail", name=p.name, banned=b_names, ip=info.last_exit_ip),
                    [p.name, *banned_by_ip[info.last_exit_ip]],
                )
            elif info.last_exit_ip:
                v6_pref = _ipv6_prefix_64(info.last_exit_ip)
                if v6_pref and v6_pref in banned_by_v6:
                    subnet_affected.setdefault(v6_pref, []).append(p.name)

        for v6_pref, aff_names in subnet_affected.items():
            b_list = banned_by_v6.get(v6_pref, [])
            b_names = ", ".join(b_list)
            shown_names = ", ".join(aff_names[:5]) + (f" (+{len(aff_names) - 5})" if len(aff_names) > 5 else "")
            _attach(
                "burned_subnet",
                "warning",
                get_string("audit_burned_subnet_title", name=shown_names, subnet=f"{v6_pref}::/64"),
                get_string(
                    "audit_burned_subnet_detail",
                    name=shown_names,
                    banned=b_names,
                    subnet=f"{v6_pref}::/64",
                ),
                [*aff_names, *b_list],
            )

        # 2. Credential Leak / Duplicate Audit (Email, 2FA Secret, OAuth2 Refresh Token)
        email_map: dict[str, list[str]] = {}
        tfa_map: dict[str, list[str]] = {}
        rt_map: dict[str, list[str]] = {}
        for p in profiles:
            em = (p.email or "").strip().lower()
            if em:
                email_map.setdefault(em, []).append(p.name)
            tfa = (p.two_fa or "").strip().upper()
            if len(tfa) >= 12:
                tfa_map.setdefault(tfa, []).append(p.name)
            rt, _ = split_mail_token(p.mail_token)
            if len(rt) >= 24:
                rt_map.setdefault(rt, []).append(p.name)

        for em, names in email_map.items():
            if len(names) > 1:
                _attach(
                    "duplicate_email",
                    "critical",
                    get_string("audit_dup_email_title", email=em),
                    get_string("audit_dup_email_detail", names=", ".join(names)),
                    names,
                )
        for _tfa, names in tfa_map.items():
            if len(names) > 1:
                _attach(
                    "duplicate_2fa",
                    "critical",
                    get_string("audit_dup_2fa_title"),
                    get_string("audit_dup_2fa_detail", names=", ".join(names)),
                    names,
                )
        for _rt, names in rt_map.items():
            if len(names) > 1:
                _attach(
                    "duplicate_token",
                    "critical",
                    get_string("audit_dup_token_title"),
                    get_string("audit_dup_token_detail", names=", ".join(names)),
                    names,
                )

        # 3. Fingerprint Clone / Same-Port Hardware Collision Audit
        fp_hash_map: dict[str, list[str]] = {}
        ep_hw_map: dict[tuple[str, str], list[str]] = {}
        for p in profiles:
            d = disk_map.get(p.name) or {}
            fph = str(d.get("fp_hash") or "")
            if fph:
                fp_hash_map.setdefault(fph, []).append(p.name)
            ep = extract_proxy_endpoint(p.proxy)
            hw_sig = str(d.get("fp_hw_sig") or "")
            if ep and hw_sig:
                ep_hw_map.setdefault((ep, hw_sig), []).append(p.name)

        for _fph, names in fp_hash_map.items():
            if len(names) > 1:
                _attach(
                    "duplicate_fingerprint",
                    "critical",
                    get_string("audit_dup_fp_title"),
                    get_string("audit_dup_fp_detail", names=", ".join(names)),
                    names,
                )
        for (ep, hw_sig), names in ep_hw_map.items():
            if len(names) > 1:
                port_lbl = f":{ep.rsplit(':', 1)[-1]}" if ":" in ep else ep
                _attach(
                    "same_port_hw",
                    "warning",
                    get_string("audit_same_port_hw_title", port=port_lbl),
                    get_string(
                        "audit_same_port_hw_detail",
                        names=", ".join(names),
                        hw=hw_sig.replace("|", " · "),
                    ),
                    names,
                )

        # 4. High IP Fraud Score & Active Cross-Endpoint IP / Gateway /64 Collision Audit
        active_ip_eps: dict[str, dict[str, list[str]]] = {}
        active_gw_v6_eps: dict[str, dict[str, list[str]]] = {}
        for p in profiles:
            info = snap.profiles.get(p.name)
            if not info or info.is_banned:
                continue
            ep_p = extract_proxy_endpoint(p.proxy) or p.name
            ip_low = (info.last_exit_ip or "").strip().lower()
            if ip_low:
                active_ip_eps.setdefault(ip_low, {}).setdefault(ep_p, []).append(p.name)
                v6_p = _ipv6_prefix_64(ip_low)
                if v6_p and is_gateway_ipv6_cluster(info.cluster):
                    active_gw_v6_eps.setdefault(v6_p, {}).setdefault(ep_p, []).append(p.name)
            if info.ip_fraud_score is not None and info.ip_fraud_score >= 70:
                _attach(
                    "high_fraud_ip",
                    "critical" if info.ip_fraud_score >= 75 else "warning",
                    get_string(
                        "audit_high_fraud_ip_title",
                        name=p.name,
                        score=info.ip_fraud_score,
                    ),
                    get_string(
                        "audit_high_fraud_ip_detail",
                        name=p.name,
                        ip=info.last_exit_ip or "-",
                        score=info.ip_fraud_score,
                    ),
                    [p.name],
                )

        for ip_low, ep_dict in active_ip_eps.items():
            if len(ep_dict) > 1:
                names = [n for grp in ep_dict.values() for n in grp]
                _attach(
                    "duplicate_exit_ip",
                    "critical",
                    f"Trùng IP đầu ra ({ip_low}) giữa nhiều cổng Proxy khác nhau",
                    f"Các hồ sơ {', '.join(names)} đang gắn các cổng Proxy khác nhau nhưng trả về cùng một địa chỉ IP đầu ra ({ip_low}).",
                    names,
                )

        for v6_p, ep_dict in active_gw_v6_eps.items():
            if len(ep_dict) > 1:
                names = [n for grp in ep_dict.values() for n in grp]
                _attach(
                    "duplicate_gateway_subnet",
                    "warning",
                    f"Trùng dải mạng /64 ({v6_p}::/64) giữa các cổng Gateway",
                    f"Các hồ sơ {', '.join(names)} trên cổng Gateway đang dùng chung một dải mạng IPv6 /64 ({v6_p}::/64).",
                    names,
                )

        snap.audit_findings = findings

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
            # Layer 2b: Shared IPv6 /64 or gateway cluster is currently active on another running profile
            elif info and info.cluster_busy_by:
                warnings.append(
                    PreLaunchWarning(
                        code="cluster_busy",
                        level="warning",
                        message=get_string(
                            "guard_cluster_busy",
                            cluster=info.cluster_collision_label or info.cluster or "/64",
                            others=", ".join(info.cluster_busy_by),
                        ),
                    )
                )
            # Layer 2c: Proxy port or /64 branch is still within the shift cooldown window
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

            # Layer 2d: Reopening a Disconnected/Banned profile whose port was reassigned to a live profile
            ep_check = extract_proxy_endpoint(proxy_str)
            if info and info.is_banned and ep_check:
                live_owners = [
                    p.name
                    for p in all_profiles
                    if p.name != profile.name
                    and extract_proxy_endpoint(p.proxy) == ep_check
                    and not (
                        p.banned_at
                        or is_ban_status(p.status)
                        or has_ban_page(p.pages)
                        or is_ban_page(p.group)
                    )
                ]
                if live_owners:
                    warnings.append(
                        PreLaunchWarning(
                            code="port_reassigned_live",
                            level="critical",
                            message=get_string(
                                "guard_port_reassigned_live",
                                port=info.port or ep_check,
                                others=", ".join(live_owners),
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

        # Exit IP country jump detection
        if info and info.ip_country_changed:
            warnings.append(
                PreLaunchWarning(
                    code="ip_country_changed",
                    level="warning",
                    message=get_string(
                        "guard_ip_country_changed",
                        old_country=info.prev_exit_country,
                        old_ip=info.prev_exit_ip or "-",
                        new_country=info.last_exit_country,
                        new_ip=info.last_exit_ip or "-",
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
        seen_clusters: dict[str, str] = {}
        seen_v6_64: dict[str, str] = {}
        seen_exact_ip: dict[str, str] = {}
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
            ep = extract_proxy_endpoint(p.proxy)
            cluster = extract_proxy_cluster(p.proxy)
            info = snap.profiles.get(name)
            my_ip = (info.last_exit_ip if info else "").strip()
            my_ip_low = my_ip.lower()
            my_v6 = _ipv6_prefix_64(my_ip_low)
            port_collided = False
            if ep:
                if ep in seen_ports:
                    port_collided = True
                    wlist.insert(
                        0,
                        PreLaunchWarning(
                            code="batch_port_collision",
                            level="critical",
                            message=get_string(
                                "guard_batch_collision",
                                port=port,
                                other=seen_ports[ep],
                            ),
                        ),
                    )
                else:
                    seen_ports[ep] = name
            if not port_collided and my_ip_low:
                if my_ip_low in seen_exact_ip:
                    wlist.insert(
                        0,
                        PreLaunchWarning(
                            code="batch_cluster_collision",
                            level="critical",
                            message=get_string(
                                "guard_batch_cluster_collision",
                                cluster=my_ip,
                                other=seen_exact_ip[my_ip_low],
                            ),
                        ),
                    )
                else:
                    seen_exact_ip[my_ip_low] = name
            if (
                not port_collided
                and cluster
                and not is_gateway_ipv6_cluster(cluster)
                and not any(w.code == "batch_cluster_collision" for w in wlist)
            ):
                if cluster in seen_clusters:
                    wlist.insert(
                        0,
                        PreLaunchWarning(
                            code="batch_cluster_collision",
                            level="warning",
                            message=get_string(
                                "guard_batch_cluster_collision",
                                cluster=cluster,
                                other=seen_clusters[cluster],
                            ),
                        ),
                    )
                else:
                    seen_clusters[cluster] = name
            if (
                not port_collided
                and my_v6
                and not any(w.code == "batch_cluster_collision" for w in wlist)
            ):
                if my_v6 in seen_v6_64:
                    wlist.insert(
                        0,
                        PreLaunchWarning(
                            code="batch_cluster_collision",
                            level="warning",
                            message=get_string(
                                "guard_batch_cluster_collision",
                                cluster=f"{my_v6}::/64",
                                other=seen_v6_64[my_v6],
                            ),
                        ),
                    )
                else:
                    seen_v6_64[my_v6] = name
            if wlist:
                issues[name] = wlist

        return issues
