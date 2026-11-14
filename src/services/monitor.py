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
import functools
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import shutil
import sqlite3
import threading
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
from ..utils.totp import (
    is_gmail_address,
    is_mailtemp_address,
    normalize_gmail_app_password,
    split_mail_token,
)
from .proxy.geo_check import classify_isp_type, compute_meta_ip_fraud_score

PORT_COOLDOWN_MINUTES = 15
TOKEN_BURST_THRESHOLD = 12
VN_TZ = dt.timezone(dt.timedelta(hours=7))
MAIL_AUTO_SCAN_MIN_HOUR_VN = 9

_RE_PORT = re.compile(r":(\d{2,5})(?=:|$|\s|/)")
_RE_LEGACY_PORT = re.compile(r":(\d{2,5})(?:@|$|\s|/)")
_RE_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_RE_USA_CLUSTER = re.compile(r"\b(usa\d+)\b", re.IGNORECASE)
_RE_REMOVED_PHONE_COUNT = re.compile(
    r"(?:[1-9]\d*\s*(?:sđt\s*đã\s*gỡ|gỡ\s*sđt)|-[1-9]\d*\s*sđt)",
    re.IGNORECASE,
)
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
_LOG_MSG_PREFIXES = (
    "[",
    "Starting browser for profile:",
    "Browser started for profile:",
    "\u0110ang kh\u1edfi ch\u1ea1y:",
    "Launching:",
    "Stopping browser for ",
    "Stopped browser for profile:",
    "Browser process for ",
    "Browser ",
    "Session ended",
    "Phi\u00ean k\u1ebft th\u00fac:",
    "\u0110\u00e3 \u0111\u00f3ng:",
    "Launch failed for profile ",
    "Error starting browser for ",
    "Updated profile:",
    "Moved profile to trash:",
    "Deleted profile:",
)
_BRACKET_EVENT_TOKENS = (
    "Starting browser for ",
    "\u0110ang kh\u1edfi \u0111\u1ed9ng tr\u00ecnh duy\u1ec7t cho ",
    "BROWSER_STARTED",
    "BROWSER_CLOSED",
    "LAUNCH_CANCELLED",
    "LAUNCH_FAILED:",
)
_WATCHED_PROFILE_ENTRIES = frozenset(
    {
        "tabs.json",
        "cookies.sqlite",
        "places.sqlite",
        "prefs.js",
        "fingerprint.json",
        "chrome_persona.json",
        "cache2",
        "Default",
    }
)

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
        "metaaivm.com",
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


def _is_muse_thread_url(url: str) -> bool:
    """Return True if `url` points to a real saved conversation thread (`/thread/<id>`) or workspace artifact (`/library/artifacts/<slug>`), excluding empty `/thread/new`."""
    if "/library/artifacts/" in url:
        tail_art = (
            url.split("/library/artifacts/", 1)[1]
            .split("?", 1)[0]
            .split("#", 1)[0]
            .strip("/")
        )
        if tail_art:
            return True
    if "/thread/" not in url:
        return False
    tail = url.split("/thread/", 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
    return bool(tail and tail.lower() != "new")


@functools.lru_cache(maxsize=2048)
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


@functools.lru_cache(maxsize=2048)
def extract_proxy_port(proxy_str: str | None) -> str:
    if not proxy_str:
        return ""
    s = _RE_SCHEME.sub("", proxy_str.strip())
    if "@" in s:
        s = s.rsplit("@", 1)[-1]
    m = _RE_PORT.search(s)
    return f":{m.group(1)}" if m else ""


@functools.lru_cache(maxsize=2048)
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


@functools.lru_cache(maxsize=2048)
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


@functools.lru_cache(maxsize=2048)
def _ipv6_prefix_64(ip: str | None) -> str:
    clean = (ip or "").strip().lower()
    if ":" not in clean:
        return ""
    try:
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
    sess_exp_days: int | None = None
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
    has_accountscenter: bool = False
    shared_port_count: int = 1

    def __post_init__(self) -> None:
        if self.sess_exp_days is None and self.sess_exp and self.sess_exp != "-":
            s = self.sess_exp.strip()
            if "Hết hạn" in s or "Expired" in s:
                self.sess_exp_days = -1
            elif s.endswith("d"):
                num_str = "".join(ch for ch in s if ch.isdigit())
                if num_str:
                    self.sess_exp_days = int(num_str)
            elif s.endswith("h"):
                self.sess_exp_days = 0

    @property
    def has_warning(self) -> bool:
        return bool(
            self.busy_by
            or self.cluster_busy_by
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


MAX_TRUST_SCORE = 100


def _parse_profile_created_date(raw: str | None) -> dt.date | None:
    """Parse YYYY-MM-DD or DD/MM/YYYY from a profile created_at string."""
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        with contextlib.suppress(ValueError):
            return dt.datetime.strptime(s[:10], fmt).date()
    return None


def _is_watch_embryo(profile: Profile | None) -> bool:
    """Return True when the profile belongs to the Theo dõi incubation workspace."""
    if profile is None:
        return False
    group_low = (profile.group or "").strip().lower()
    if (
        "theo dõi" in group_low
        or "theo doi" in group_low
        or group_low[:2] in {"1.", "2.", "3.", "4.", "5."}
    ):
        return True
    return any(
        "theo dõi" in str(pg).lower() or "theo doi" in str(pg).lower()
        for pg in (profile.pages or [])
    )


def _classify_profile_lineage(
    profile: Profile | None,
    info: ProfileMonitorInfo,
    eff_acc_age: float,
) -> tuple[str, int, str, str]:
    """Classify account lineage cleanliness & Zero-Mutation adherence (0-18 pts)."""
    p_name_clean = ((profile.name if profile is not None else info.name) or "").strip().lower()
    email_low = (profile.email or "").strip().lower() if profile is not None else ""
    rec_low = (profile.recovery_email or "").strip().lower() if profile is not None else ""
    notes_low = (profile.notes or "").strip().lower() if profile is not None else ""
    group_low = (profile.group or "").strip().lower() if profile is not None else ""
    created_date = _parse_profile_created_date(profile.created_at if profile is not None else None)

    if (
        email_low.endswith("@tuan89.site")
        or group_low.startswith("5.")
        or "nguy hiểm nhất" in group_low
    ):
        return (
            "dead_tuan89",
            0,
            "Nhóm 5 (@tuan89.site · Miền blacklist trực tiếp)",
            "Phôi phế phẩm (@tuan89.site) — Tuyệt đối không gắn proxy chính",
        )

    if (
        group_low.startswith("4.")
        or "gốc @tuan89" in group_low
        or "+19376123124" in notes_low
        or "@tuan89.site" in notes_low
        or p_name_clean in {"62", "63", "tlazglb@hotmail.com"}
        or email_low == "tlazglb@hotmail.com"
        or "burned_batch" in info.audit_codes
    ):
        return (
            "red_taint",
            2,
            "Báo động Đỏ (Dính +19376123124 / Gốc @tuan89.site)",
            "Giữ trần cứng 44đ · Chỉ dùng cách ly, tuyệt đối không bấm Referral",
        )

    if (
        group_low.startswith("3.")
        or "spam sđt" in group_low
        or bool(_RE_REMOVED_PHONE_COUNT.search(notes_low))
        or p_name_clean == "ntnusxi@hotmail.coms"
        or (
            rec_low.endswith("@smvmail.com")
            and created_date is not None
            and created_date >= dt.date(2026, 10, 18)
        )
    ):
        return (
            "orange_mutation",
            8,
            "Di sản Đổi mail 2 tầng / Spam SĐT (18/10–22/10)",
            "Giữ trần 75đ (❖ Bền vững) cho đến khi sống > 3 tháng (90d+)",
        )

    if (
        (created_date is not None and created_date <= dt.date(2026, 10, 17))
        or eff_acc_age >= 90.0
        or group_low.startswith("1.")
        or (email_low.endswith(".io.vn") and not rec_low)
        or "0 gỡ sđt" in notes_low
    ):
        ac_note = "đã mở accountscenter" if info.has_accountscenter else "0 đụng accountscenter"
        return (
            "pure_origin",
            18,
            f"Phôi Nguyên Bản / Trước 18/10 ({ac_note})",
            "✓ Đạt chuẩn Zero-Mutation tối đa (+18/18đ)",
        )

    return (
        "clean_standard",
        15,
        "Phôi Sạch Chuẩn (Không dính lô @tuan89 / +19376123124)",
        "✓ Phôi sạch — Giữ nguyên mail gốc & 0 mở accountscenter",
    )


def _compute_stage_cap_and_code(
    *,
    is_banned: bool,
    has_meta_checkpoint: bool = False,
    cookie_count: int,
    sess_cnt: int,
    history_pages: int,
    hatch_gw: bool,
    has_muse_auth: bool = False,
    eff_datr_age: float,
    eff_acc_age: float = 0.0,
    is_watch_embryo: bool = False,
    lineage_tier: str = "clean_standard",
    clean_env: bool,
    ip_fraud_score: int | None,
    sess_exp_days: int | None = None,
) -> tuple[int | None, str, str]:
    """Compute empirical Age-First stage ceiling (0-100), stage code, and label."""
    if is_banned or has_meta_checkpoint:
        return 0, "banned", "Đã khóa / Dính Meta Checkpoint (0đ — Ban 100%)"

    if (
        cookie_count == 0
        and sess_cnt == 0
        and history_pages == 0
        and eff_datr_age <= 0.0
        and not is_watch_embryo
    ):
        return 0, "stage_0_empty", "Hồ sơ trống chưa khởi tạo phiên trình duyệt (0đ)"

    if lineage_tier == "dead_tuan89":
        return 12, "stage_0_tuan89", "Nhóm 5 (@tuan89.site) — Phôi phế phẩm (Trần 12đ)"

    if eff_acc_age < 3.0:
        return 20, "stage_0_under_3d", "Mới tạo < 3 ngày — Vùng quét hậu kiểm T+48h (Trần 20đ)"

    if (not is_watch_embryo and eff_datr_age <= 0.0) or (
        eff_datr_age > 0.0 and not hatch_gw and not has_muse_auth
    ):
        return (
            20,
            "stage_0_no_auth",
            "Chưa hoàn tất xác thực cổng Muse / Meta OIDC (Trần 20đ)",
        )

    if lineage_tier == "red_taint":
        return (
            44,
            "stage_1_red_taint",
            "Báo động Đỏ — Dính +19376123124 / Gốc @tuan89 (Trần cứng 44đ)",
        )

    if eff_datr_age <= 0.0:
        return (
            44,
            "stage_1_incubating",
            "Đang ủ phôi (CP=0) — Chưa mở trình duyệt (Trần 44đ)",
        )

    if eff_acc_age < 7.0 or eff_datr_age < 7.0:
        return (
            44,
            "stage_1_under_7d",
            "Giai đoạn 1 — ◇ Ổn định sau login (< 7 ngày · Trần 44đ)",
        )

    if (
        not clean_env
        or (ip_fraud_score is not None and ip_fraud_score >= 65)
        or (sess_exp_days is not None and sess_exp_days < 0)
    ):
        return (
            61,
            "stage_2_env_hold",
            "Cảnh báo môi trường mạng hoặc hết hạn phiên hatch_sess (Trần 61đ)",
        )

    if eff_acc_age < 21.0:
        return (
            61,
            "stage_2_7d_to_21d",
            "Giai đoạn 2 — ◈ Ổn định 7–20 ngày tuổi (Qua cửa sổ 14–18d lên 21d+ · Trần 61đ)",
        )

    if eff_acc_age < 30.0:
        return (
            75,
            "stage_3_21d_to_30d",
            "Giai đoạn 3 — ❖ Bền vững 21–29 ngày tuổi (Hướng tới mốc > 1 tháng · Trần 75đ)",
        )

    if lineage_tier == "orange_mutation" and eff_acc_age < 90.0:
        return (
            75,
            "stage_3_orange_cap",
            "Di sản đổi mail 2 tầng — Giữ trần 75đ đến khi sống > 3 tháng (90d+)",
        )

    if eff_acc_age < 90.0:
        return (
            89,
            "stage_4_over_1m",
            "Giai đoạn 4 — ◆ Tin cậy (> 1 tháng · 30–89d tuổi) → Mở Kim Cương ở 90d+ (Trần 89đ)",
        )

    return (
        None,
        "stage_5_over_3m",
        "Giai đoạn 5 — ✦ Kim Cương (> 3 tháng / 90d+ tuổi · Mở khóa hoàn toàn 100/100đ)",
    )


def _score_age_ladder(days: float, tiers: list[tuple[float, int]], label_unit: str) -> tuple[int, str]:
    """Score a continuous survival age (days) against sorted descending tiers."""
    for threshold, pts in tiers:
        if days >= threshold:
            return pts, f"≥{int(threshold)}{label_unit}"
    return (1 if days > 0.0 else 0), "<3d"


def compute_trust_breakdown(
    info: ProfileMonitorInfo,
    profile: Profile | None = None,
) -> dict[str, Any]:
    """Return an empirical 0-100 Trust Score 2.0 breakdown centered on Survival Age (50%)."""
    eff_datr_age = float(info.datr_age_days or 0.0)
    sess_cnt = int(info.session_count or 0)
    cal_age_days = compute_days_since(profile.created_at) if profile is not None else None
    eff_acc_age = max(
        float(cal_age_days) if cal_age_days is not None else float(info.age_days or 0),
        eff_datr_age,
    )
    is_watch_embryo = _is_watch_embryo(profile)

    email_low = (profile.email or "").strip().lower() if profile is not None else ""
    rec_low = (profile.recovery_email or "").strip().lower() if profile is not None else ""
    is_incubating_embryo = is_watch_embryo or (eff_acc_age >= 3.0 and bool(email_low))
    if profile is not None:
        has_proxy = bool(profile.proxy)
        has_geo_pin = bool(profile.proxy and profile.timezone and profile.locale)
        has_tfa = bool((profile.two_fa or "").strip())
        has_mtok = bool((profile.mail_token or "").strip())
    else:
        has_proxy = bool(info.has_proxy or info.port or info.last_exit_ip or info.has_geo_pin)
        has_geo_pin = bool(info.has_geo_pin)
        has_tfa = False
        has_mtok = bool(info.has_recovery_key)
    has_muse_auth = bool(info.has_muse_auth)

    # Pillar 1: Real Account Survival Age & OIDC Device Stabilization (max 50 pts)
    acc_age_tiers: list[tuple[float, int]] = [
        (90.0, 36),
        (60.0, 32),
        (30.0, 28),
        (21.0, 22),
        (14.0, 16),
        (10.0, 12),
        (7.0, 8),
        (3.0, 4),
    ]
    acc_age_pts, _ = _score_age_ladder(eff_acc_age, acc_age_tiers, "d")

    datr_age_tiers: list[tuple[float, int]] = [
        (14.0, 14),
        (7.0, 11),
        (3.0, 6),
    ]
    if eff_datr_age > 0.0:
        datr_age_pts = 3
        for threshold, pts in datr_age_tiers:
            if eff_datr_age >= threshold:
                datr_age_pts = pts
                break
    else:
        datr_age_pts = 0
    p1_pts = acc_age_pts + datr_age_pts

    # Pillar 2: Lineage Cleanliness & Zero-Mutation (max 18 pts)
    lineage_tier, p2_pts, lineage_cur, lineage_hint = _classify_profile_lineage(
        profile,
        info,
        eff_acc_age,
    )

    # Pillar 3: muse.ai 30d Sliding Session Health (max 17 pts)
    sess_exp_days = info.sess_exp_days
    if info.hatch_sess:
        if sess_exp_days is None:
            h_sess_pts = 8
            h_sess_detail = "Session"
        elif sess_exp_days >= 15:
            h_sess_pts = 10
            h_sess_detail = f"Còn {sess_exp_days}d (Tốt)"
        elif sess_exp_days >= 8:
            h_sess_pts = 8
            h_sess_detail = f"Còn {sess_exp_days}d (Ổn)"
        elif sess_exp_days >= 4:
            h_sess_pts = 5
            h_sess_detail = f"Còn {sess_exp_days}d (Sắp trượt)"
        elif sess_exp_days >= 0:
            h_sess_pts = 2
            h_sess_detail = f"Còn {sess_exp_days}d (Cần mở ngay)"
        else:
            h_sess_pts = 0
            h_sess_detail = "Hết hạn"
    else:
        h_sess_pts = 0
        h_sess_detail = "Chưa có phiên"

    gw_full_ok = bool(info.hatch_gw and (info.hatch_vml or has_muse_auth or not info.has_meta))
    if gw_full_ok:
        gw_pts = 4
        gw_detail = "GW + VML/OIDC"
    elif info.hatch_gw or has_muse_auth:
        gw_pts = 3
        gw_detail = "Có GW/OIDC"
    else:
        gw_pts = 0
        gw_detail = "Chưa đăng nhập"

    cache_pts = 2 if (info.has_cache2 and info.cookie_count > 0) else 0
    cookie_sync_ok = bool(
        info.has_ps_l or info.datr_domains >= 2 or (not info.has_meta and info.has_grecaptcha)
    )
    sync_pts = cache_pts + (1 if cookie_sync_ok else 0)
    sync_detail = (
        f"Cache2: {'Có' if cache_pts else 'Chưa'} · {info.datr_domains} miền Meta"
    )
    p3_pts = h_sess_pts + gw_pts + sync_pts

    # Pillar 4: Network Isolation, ASN & External Inbox Check (max 15 pts)
    geo_pts = 4 if (has_proxy and has_geo_pin) else (1 if has_proxy else 0)
    clean_env = not (
        info.has_meta_checkpoint or info.proxy_port_changed or info.ip_country_changed
    )
    is_primary_hostodo = bool(
        info.cluster.startswith("usa")
        or (info.last_exit_ip or "").strip().lower().startswith("2606:65c0:40:")
        or (profile is not None and "sp02-usa" in (profile.proxy or "").lower())
    )
    ip_pts = 0
    if has_proxy:
        if info.ip_fraud_score is not None:
            fs = info.ip_fraud_score
            if (fs <= 35 and not info.ip_proxy_flag) or (fs <= 55 and is_primary_hostodo):
                ip_pts = 7
            elif fs <= 55:
                ip_pts = 7 if (eff_acc_age >= 30.0 and eff_datr_age >= 7.0 and clean_env) else 5
            elif fs < 65:
                ip_pts = 3
            ip_detail = (
                f"Fraud {fs}/100 ({'Hostodo /64' if is_primary_hostodo else 'Cần lưu ý ASN'})"
            )
        elif is_primary_hostodo or info.last_exit_ip:
            ip_pts = 4
            ip_detail = (
                "Cụm Hostodo (chưa chấm điểm → +4đ)"
                if is_primary_hostodo
                else "Đã có IP (chưa chấm điểm → +4đ)"
            )
        else:
            ip_detail = "Chưa kiểm tra Exit IP"
    else:
        ip_detail = "Chưa gắn Proxy"

    shared_ports = max(1, int(info.shared_port_count or info.shared_count or 1))
    has_ip_taint = any(c in info.audit_codes for c in ("burned_proxy", "burned_ip"))
    if has_proxy and shared_ports <= 1 and not has_ip_taint:
        port_pts = 2
        port_detail = "Độc lập 1-1"
    elif has_proxy and shared_ports <= 1 and has_ip_taint and eff_acc_age >= 21.0:
        port_pts = 1
        port_detail = "Độc lập mới (Vết cũ >21d)"
    else:
        port_pts = 0
        port_detail = f"Dùng chung ({shared_ports})" if shared_ports > 1 else "Thiếu/Trùng IP"

    std_domains = ("@hotmail.com", "@outlook.com", "@live.com", "@gmail.com", "@yahoo.com")
    has_ext_mail = bool(
        has_mtok
        or has_tfa
        or (email_low and "@" in email_low and not email_low.endswith(std_domains))
    )
    if has_ext_mail:
        inbox_pts = 2
        inbox_detail = "Graph Token / Webmail riêng"
    elif email_low or rec_low or eff_acc_age >= 21.0:
        inbox_pts = 1
        inbox_detail = "Có địa chỉ Mail lưu"
    else:
        inbox_pts = 0
        inbox_detail = "Chưa lưu"
    p4_pts = geo_pts + ip_pts + port_pts + inbox_pts

    pillars: list[dict[str, Any]] = [
        {
            "key": "survival_age",
            "title": "1. Tuổi Sống Sót & Thiết Bị OIDC (50%)",
            "earned": p1_pts,
            "max_pts": 50,
            "current_str": "",
            "next_hint": "",
            "tiers_str": "",
            "sub_items": [
                {
                    "label": "Tuổi sống sót tài khoản (>1m: 28đ · >3m: 36đ)",
                    "detail": f"{eff_acc_age:.0f} ngày",
                    "earned": acc_age_pts,
                    "max_pts": 36,
                    "ok": eff_acc_age >= 21.0,
                },
                {
                    "label": "Định danh thiết bị OIDC datr (≥7d: 11đ · ≥14d: 14đ)",
                    "detail": f"{eff_datr_age:.1f} ngày · {info.datr_domains} miền",
                    "earned": datr_age_pts,
                    "max_pts": 14,
                    "ok": eff_datr_age >= 7.0,
                },
            ],
        },
        {
            "key": "lineage",
            "title": "2. Độ Sạch Phôi Gốc & Zero-Mutation",
            "earned": p2_pts,
            "max_pts": 18,
            "current_str": lineage_cur,
            "next_hint": lineage_hint,
            "tiers_str": "Mốc: Trước 18/10 & io.vn (+18) · Phôi sạch (+15) · Đổi 2 tầng (+8) · +19376123124 (+2)",
            "sub_items": [],
        },
        {
            "key": "muse_session",
            "title": "3. Sức Khỏe Phiên Trượt muse.ai (30d)",
            "earned": p3_pts,
            "max_pts": 17,
            "current_str": "",
            "next_hint": "",
            "tiers_str": "",
            "sub_items": [
                {
                    "label": "Hạn phiên trượt hatch_sess (≥15d: +10đ)",
                    "detail": h_sess_detail,
                    "earned": h_sess_pts,
                    "max_pts": 10,
                    "ok": h_sess_pts >= 8,
                },
                {
                    "label": "Cổng xác thực hatch_gw / OIDC",
                    "detail": gw_detail,
                    "earned": gw_pts,
                    "max_pts": 4,
                    "ok": gw_pts >= 3,
                },
                {
                    "label": "Đồng bộ Cache2 ETag & Cookie thiết bị",
                    "detail": sync_detail,
                    "earned": sync_pts,
                    "max_pts": 3,
                    "ok": sync_pts >= 2,
                },
            ],
        },
        {
            "key": "network",
            "title": "4. Cách Ly Mạng, ASN & Hộp Thư Ngoài",
            "earned": p4_pts,
            "max_pts": 15,
            "current_str": "",
            "next_hint": "",
            "tiers_str": "",
            "sub_items": [
                {
                    "label": "Ghim Proxy + Timezone + Locale",
                    "detail": (
                        "Đã ghim đủ"
                        if geo_pts == 4
                        else ("Thiếu TZ/Locale" if geo_pts == 1 else "Chưa gắn Proxy")
                    ),
                    "earned": geo_pts,
                    "max_pts": 4,
                    "ok": geo_pts == 4,
                },
                {
                    "label": "Độ sạch IP & Cụm Hostodo /64",
                    "detail": ip_detail,
                    "earned": ip_pts,
                    "max_pts": 7,
                    "ok": ip_pts >= 7,
                },
                {
                    "label": "Độc lập Cổng Proxy & IPv6",
                    "detail": port_detail,
                    "earned": port_pts,
                    "max_pts": 2,
                    "ok": port_pts == 2,
                },
                {
                    "label": "Kiểm tra Hộp thư ngoài (Pre-Flight Check)",
                    "detail": inbox_detail,
                    "earned": inbox_pts,
                    "max_pts": 2,
                    "ok": inbox_pts == 2,
                },
            ],
        },
    ]

    # Risk Penalties (grounded 100% in empirical ban mechanisms)
    penalties: list[dict[str, Any]] = []
    if info.has_meta_checkpoint:
        penalties.append(
            {
                "code": "checkpoint",
                "label": "Dính Meta Checkpoint (Quy luật bất di bất dịch = Ban 100%)",
                "points": -100,
            }
        )
    if info.proxy_port_changed or info.ip_country_changed:
        penalties.append(
            {
                "code": "ip_port_changed",
                "label": "Đổi cổng Proxy / Nhảy quốc gia IP khi đang có phiên",
                "points": -25,
            }
        )
    if info.has_accountscenter:
        ac_pen = -12 if eff_acc_age < 21.0 else -5
        penalties.append(
            {
                "code": "accountscenter_visited",
                "label": "Lịch sử mở accountscenter.meta.com (Vi phạm Zero-Mutation)",
                "points": ac_pen,
            }
        )
    if has_ip_taint:
        taint_pen = -4 if eff_acc_age >= 21.0 else -15
        penalties.append(
            {
                "code": "burned_ip_proxy",
                "label": (
                    "Vết lịch sử Port/IPv6 cũ (Giảm nhẹ nhờ đã sống >21d)"
                    if eff_acc_age >= 21.0
                    else "Liên đới lịch sử Cổng Proxy / IPv6 với tài khoản đã khóa"
                ),
                "points": taint_pen,
            }
        )
    if "duplicate_fingerprint" in info.audit_codes:
        penalties.append(
            {
                "code": "duplicate_fingerprint",
                "label": "Trùng lặp Fingerprint thiết bị giữa nhiều hồ sơ",
                "points": -20,
            }
        )
    if info.ip_fraud_score is not None and info.ip_fraud_score >= 65:
        penalties.append(
            {
                "code": "high_fraud_ip",
                "label": f"Exit IP rủi ro rất cao ({info.ip_fraud_score}/100)",
                "points": -12,
            }
        )
    if info.burst_10m >= TOKEN_BURST_THRESHOLD:
        penalties.append(
            {
                "code": "token_burst",
                "label": f"Tần suất gọi /api/hatch/token cao ({info.burst_10m} lần/10p)",
                "points": -12,
            }
        )
    if info.hatch_sess and not info.hatch_gw and not has_muse_auth:
        penalties.append(
            {
                "code": "missing_gw",
                "label": "Cookie hatch_sess đơn lẻ thiếu cổng xác thực hatch_gw / OIDC",
                "points": -15,
            }
        )

    positive_total = sum(int(p["earned"]) for p in pillars)
    penalty_total = sum(int(pen["points"]) for pen in penalties)
    raw_score = max(0, min(MAX_TRUST_SCORE, positive_total + penalty_total))

    stage_cap, stage_code, stage_label = _compute_stage_cap_and_code(
        is_banned=info.is_banned,
        has_meta_checkpoint=info.has_meta_checkpoint,
        cookie_count=info.cookie_count,
        sess_cnt=sess_cnt,
        history_pages=info.history_pages,
        hatch_gw=bool(info.hatch_gw),
        has_muse_auth=has_muse_auth,
        eff_datr_age=eff_datr_age,
        eff_acc_age=eff_acc_age,
        is_watch_embryo=is_incubating_embryo,
        lineage_tier=lineage_tier,
        clean_env=clean_env,
        ip_fraud_score=info.ip_fraud_score,
        sess_exp_days=sess_exp_days,
    )

    if stage_cap == 0:
        final_score = 0
    elif stage_cap is not None:
        final_score = max(1, min(stage_cap, raw_score))
    else:
        final_score = max(1, raw_score)

    if info.is_banned or info.has_meta_checkpoint:
        computed_tier = "banned"
    elif final_score == 0:
        computed_tier = "cold"
    elif final_score >= 90:
        computed_tier = "diamond"
    elif final_score >= 76:
        computed_tier = "elite"
    elif final_score >= 62:
        computed_tier = "platinum"
    elif final_score >= 45:
        computed_tier = "trusted"
    elif final_score >= 21:
        computed_tier = "warming"
    else:
        computed_tier = "cold"

    capped_by = max(0, raw_score - final_score) if stage_cap is not None else 0

    # Build empirical next-milestone checklist (Zero-Mutation compliant — no 2FA prompts)
    if stage_code in ("stage_4_over_1m", "stage_5_over_3m"):
        next_tier_title = (
            "Điều kiện mở trần 89đ → Hạng cao nhất ✦ Kim Cương (90–100đ · Sống > 3 tháng / 90d+)"
        )
        gates: list[dict[str, Any]] = [
            {
                "label": "Tuổi sống sót tài khoản ≥ 90 ngày (> 3 tháng)",
                "current": f"{eff_acc_age:.0f}/90 ngày",
                "passed": eff_acc_age >= 90.0,
            },
            {
                "label": "Định danh thiết bị OIDC ổn định (datr ≥ 14 ngày)",
                "current": f"{eff_datr_age:.1f}/14 ngày",
                "passed": eff_datr_age >= 14.0,
            },
            {
                "label": "Duy trì Zero-Mutation (0 đụng accountscenter.meta.com)",
                "current": "Vi phạm" if info.has_accountscenter else "An toàn",
                "passed": not info.has_accountscenter,
            },
            {
                "label": "Phiên trượt hatch_sess còn hạn & Mạng sạch",
                "current": info.sess_exp,
                "passed": clean_env and (sess_exp_days is None or sess_exp_days >= 0),
            },
        ]
    elif stage_code in ("stage_3_21d_to_30d", "stage_3_orange_cap"):
        next_tier_title = (
            "Điều kiện mở trần 75đ → Hạng ◆ Tin cậy (76–89đ · Sống > 1 tháng / 30d+)"
        )
        gates = [
            {
                "label": "Tuổi sống sót tài khoản ≥ 30 ngày (> 1 tháng)",
                "current": f"{eff_acc_age:.0f}/30 ngày",
                "passed": eff_acc_age >= 30.0,
            },
            {
                "label": "Không dính vết Đổi mail 2 tầng (Orange cần ≥ 90d)",
                "current": "Vết 18–22/10" if lineage_tier == "orange_mutation" else "Phôi sạch",
                "passed": lineage_tier != "orange_mutation" or eff_acc_age >= 90.0,
            },
            {
                "label": "Giữ nguyên Proxy/Quốc gia & Phiên hatch_sess còn hạn",
                "current": info.sess_exp,
                "passed": clean_env and (sess_exp_days is None or sess_exp_days >= 0),
            },
        ]
    elif stage_code in ("stage_2_7d_to_21d", "stage_2_env_hold"):
        next_tier_title = (
            "Điều kiện mở trần 61đ → Hạng ❖ Bền vững (62–75đ · Vượt hậu kiểm 21 ngày+)"
        )
        gates = [
            {
                "label": "Tuổi sống sót tài khoản ≥ 21 ngày (Vượt cửa sổ quét 14–18d)",
                "current": f"{eff_acc_age:.0f}/21 ngày",
                "passed": eff_acc_age >= 21.0,
            },
            {
                "label": "Ổn định thiết bị OIDC sau login (datr ≥ 7 ngày)",
                "current": f"{eff_datr_age:.1f}/7 ngày",
                "passed": eff_datr_age >= 7.0,
            },
            {
                "label": "Phiên trượt hatch_sess còn hạn & Mạng sạch",
                "current": (
                    f"Fraud {info.ip_fraud_score}/100 · {info.sess_exp}"
                    if info.ip_fraud_score is not None
                    else info.sess_exp
                ),
                "passed": (
                    clean_env
                    and (sess_exp_days is None or sess_exp_days >= 0)
                    and (info.ip_fraud_score is None or info.ip_fraud_score < 65)
                ),
            },
        ]
    else:
        next_tier_title = (
            "Điều kiện vượt trần 20đ/44đ → Hạng ◈ Ổn định (45–61đ · Đã login & Qua mốc 7 ngày)"
        )
        gates = [
            {
                "label": "Tuổi phôi ≥ 3 ngày (Vượt cửa sổ hậu kiểm T+48h)",
                "current": f"{eff_acc_age:.0f}/3 ngày",
                "passed": eff_acc_age >= 3.0,
            },
            {
                "label": "Đã kích hoạt phiên trình duyệt Muse (hatch_gw / OIDC)",
                "current": "Đã có" if (info.hatch_gw or has_muse_auth) else "Đang ủ / Chưa mở",
                "passed": bool(info.hatch_gw or has_muse_auth),
            },
            {
                "label": "Ổn định thiết bị OIDC sau login (datr ≥ 7 ngày)",
                "current": f"{eff_datr_age:.1f}/7 ngày",
                "passed": eff_datr_age >= 7.0,
            },
            {
                "label": "Không dính Báo động Đỏ (+19376123124 / @tuan89.site)",
                "current": "Cần cách ly" if lineage_tier in ("red_taint", "dead_tuan89") else "Sạch",
                "passed": lineage_tier not in ("red_taint", "dead_tuan89"),
            },
        ]

    return {
        "name": info.name,
        "final_score": final_score,
        "max_score": MAX_TRUST_SCORE,
        "trust_tier": computed_tier,
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
    mail_checkpoint_hits: list[tuple[str, dict[str, str]]] = field(default_factory=list)
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
        self._log_file_events_cache: dict[str, tuple[float, int, list[tuple[str, str]]]] = {}
        self._log_events_sig: tuple[tuple[str, int, tuple[str, str] | None], ...] = ()
        self._log_events_disk_loaded: bool = False
        self._log_history: dict[str, dict[str, Any]] = {}
        self._live_started_at: dict[str, dt.datetime] = {}
        self._live_stopped_at: dict[str, dt.datetime] = {}
        self._live_sessions: dict[str, list[dict[str, Any]]] = {}
        self._disk_cache: dict[str, dict[str, Any]] = {}
        self._verified_closed_ts: dict[str, float] = {}
        self._disk_cache_dirty: bool = False
        self._save_lock = threading.Lock()
        self._last_free_gb_ts: float = 0.0
        self._free_gb: float = 0.0
        self._cooldown_minutes: int = PORT_COOLDOWN_MINUTES
        self._last_used_ports: dict[str, str] = {}
        self._last_used_endpoints: dict[str, str] = {}
        self._ip_history: dict[str, list[dict[str, Any]]] = {}
        self._ip_scores: dict[str, dict[str, Any]] = {}
        self._token_entry_memo: dict[str, tuple[float, bool]] = {}
        self._last_pairings: list[list[str]] = []
        self._last_shifts: dict[str, str] = {}
        self._schedule_cycle_days: int = 5
        self._fixed_work_squads: dict[str, list[str]] = {}
        self._sort_by: str = "custom"
        self._last_mail_scan_ts: float = 0.0
        self._last_aborted_mail_scan_ts: float = 0.0
        self._mail_checkpoint_hits: dict[str, dict[str, str]] = {}
        self._mail_dead_tokens: set[str] = set()
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
                eps = raw.get("last_used_endpoints")
                if isinstance(eps, dict):
                    self._last_used_endpoints = {
                        str(k): str(v) for k, v in eps.items() if isinstance(k, str) and v
                    }
                raw_scores = raw.get("ip_scores")
                if isinstance(raw_scores, dict):
                    clean_scores: dict[str, dict[str, Any]] = {}
                    for ip_k, s_val in raw_scores.items():
                        if isinstance(ip_k, str) and isinstance(s_val, dict):
                            sd = dict(s_val)
                            s_isp = str(sd.get("isp") or "").strip()
                            s_type = str(sd.get("ip_type") or "").strip().upper()
                            if not s_type and s_isp:
                                s_type = classify_isp_type(s_isp)
                                if s_type:
                                    sd["ip_type"] = s_type
                            if sd.get("scamalytics") is not None:
                                meta_sc = compute_meta_ip_fraud_score(
                                    int(sd["scamalytics"]),
                                    int(sd["proxycheck_risk"])
                                    if sd.get("proxycheck_risk") is not None
                                    else None,
                                    ip_type=s_type,
                                    proxy_flag=bool(sd.get("proxy_flag")),
                                )
                                if meta_sc is not None:
                                    sd["fraud_score"] = meta_sc
                            clean_scores[ip_k.strip()] = sd
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
                                i_isp = str(item.get("isp", "")).strip()
                                i_type = str(item.get("ip_type", "")).strip().upper()
                                if not i_type and i_isp:
                                    i_type = classify_isp_type(i_isp)
                                entry_d: dict[str, Any] = {
                                    "ip": ip_str,
                                    "country": str(item.get("country", "")).strip().upper(),
                                    "tz": str(item.get("tz", "")).strip(),
                                    "port": str(item.get("port", "")).strip(),
                                    "ts": str(item.get("ts", "")).strip(),
                                    "isp": i_isp,
                                    "ip_type": i_type,
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
                                sc_ref = self._ip_scores.get(ip_str, {})
                                eff_scam = entry_d.get("scamalytics", sc_ref.get("scamalytics"))
                                eff_pc = entry_d.get(
                                    "proxycheck_risk", sc_ref.get("proxycheck_risk")
                                )
                                eff_type = i_type or str(sc_ref.get("ip_type") or "").upper()
                                if eff_scam is not None:
                                    meta_f = compute_meta_ip_fraud_score(
                                        int(eff_scam),
                                        int(eff_pc) if eff_pc is not None else None,
                                        ip_type=eff_type,
                                        proxy_flag=bool(
                                            entry_d.get("proxy_flag") or sc_ref.get("proxy_flag")
                                        ),
                                    )
                                    if meta_f is not None:
                                        entry_d["fraud_score"] = meta_f
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
                raw_sort = raw.get("sort_by")
                if isinstance(raw_sort, str) and raw_sort.strip():
                    self._sort_by = raw_sort.strip()
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
                            and dval.get("_cache_ver") == 6
                        ):
                            clean_disk[pname] = dict(dval)
                    self._disk_cache = clean_disk
                raw_scan_ts = raw.get("last_mail_scan_ts")
                if isinstance(raw_scan_ts, (int, float)) and raw_scan_ts >= 0:
                    self._last_mail_scan_ts = float(raw_scan_ts)
                raw_cp_hits = raw.get("mail_checkpoint_hits")
                if isinstance(raw_cp_hits, dict):
                    clean_hits: dict[str, dict[str, str]] = {}
                    for pname, hval in raw_cp_hits.items():
                        if isinstance(pname, str) and isinstance(hval, dict):
                            clean_hits[pname] = {
                                str(k): str(v) for k, v in hval.items() if v is not None
                            }
                    self._mail_checkpoint_hits = clean_hits
                raw_dead = raw.get("mail_dead_tokens")
                if isinstance(raw_dead, list):
                    self._mail_dead_tokens = {
                        str(em).strip().lower() for em in raw_dead if str(em).strip()
                    }

    def _save_settings(self, *, async_write: bool = False) -> None:
        path = self._settings_file()
        with contextlib.suppress(Exception):
            payload = {
                "cooldown_minutes": self._cooldown_minutes,
                "last_used_ports": dict(self._last_used_ports),
                "last_used_endpoints": dict(self._last_used_endpoints),
                "ip_history": dict(self._ip_history),
                "ip_scores": dict(self._ip_scores),
                "session_history": {
                    k: v[-50:] for k, v in self._live_sessions.items() if v
                },
                "last_pairings": list(self._last_pairings),
                "last_shifts": dict(self._last_shifts),
                "schedule_cycle_days": self._schedule_cycle_days,
                "sort_by": self._sort_by,
                "fixed_work_squads": dict(self._fixed_work_squads),
                "last_mail_scan_ts": self._last_mail_scan_ts,
                "mail_checkpoint_hits": dict(self._mail_checkpoint_hits),
                "mail_dead_tokens": sorted(self._mail_dead_tokens),
                "disk_cache": {
                    k: v
                    for k, v in self._disk_cache.items()
                    if isinstance(v, dict) and not v.get("_is_running")
                },
            }

            def _write_to_disk() -> None:
                with self._save_lock, contextlib.suppress(Exception):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if payload["last_mail_scan_ts"] <= 0.0 and path.exists():
                        with contextlib.suppress(Exception):
                            disk_raw = json.loads(path.read_text(encoding="utf-8"))
                            if isinstance(disk_raw, dict):
                                disk_ts = disk_raw.get("last_mail_scan_ts")
                                if (
                                    isinstance(disk_ts, (int, float))
                                    and disk_ts > payload["last_mail_scan_ts"]
                                ):
                                    self._last_mail_scan_ts = float(disk_ts)
                                    payload["last_mail_scan_ts"] = float(disk_ts)
                    tmp = path.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    os.replace(tmp, path)

            if async_write and "PYTEST_CURRENT_TEST" not in os.environ:
                threading.Thread(target=_write_to_disk, daemon=True).start()
            else:
                _write_to_disk()

    @property
    def last_mail_scan_ts(self) -> float:
        return self._last_mail_scan_ts

    @property
    def mail_checkpoint_hits(self) -> dict[str, dict[str, str]]:
        return {k: dict(v) for k, v in self._mail_checkpoint_hits.items()}

    @property
    def mail_dead_tokens(self) -> set[str]:
        return set(self._mail_dead_tokens)

    def should_auto_scan_mail(
        self,
        now_ts: float | None = None,
        cooldown_hours: float = 20.0,
        *,
        min_hour_vn: int = MAIL_AUTO_SCAN_MIN_HOUR_VN,
    ) -> bool:
        """Return True if it is at/after `min_hour_vn` (09:00 AM Vietnam time, UTC+7)
        and no mail scan has been recorded since today's `min_hour_vn` VN cutoff.
        """
        cur = time.time() if now_ts is None else float(now_ts)
        if (
            self._last_aborted_mail_scan_ts > 0.0
            and 0.0 <= (cur - self._last_aborted_mail_scan_ts) < 1800.0
        ):
            return False
        now_vn = dt.datetime.fromtimestamp(cur, tz=VN_TZ)
        if 0 <= min_hour_vn <= 23:
            if now_vn.hour < min_hour_vn:
                return False
            today_cutoff_vn = now_vn.replace(
                hour=min_hour_vn,
                minute=0,
                second=0,
                microsecond=0,
            )
            if self._last_mail_scan_ts <= 0.0:
                return True
            last_vn = dt.datetime.fromtimestamp(self._last_mail_scan_ts, tz=VN_TZ)
            return last_vn < today_cutoff_vn
        return (cur - self._last_mail_scan_ts) >= (cooldown_hours * 3600.0)

    def get_live_mail_scan_targets(
        self,
        profiles: list[Profile],
        pool_token_map: dict[str, str] | None = None,
    ) -> list[tuple[str, str, str]]:
        """Return `[(profile_name, email, mail_token), ...]` for active `Live` / `Theo dõi` profiles eligible for daily mail check."""
        has_live_page = any(
            any(pg.casefold() in ("live", "theo dõi", "theo doi") for pg in (p.pages or []))
            for p in profiles
        )
        tok_lookup = pool_token_map or {}
        targets: list[tuple[str, str, str]] = []
        for p in profiles:
            if (
                p.banned_at
                or is_ban_status(p.status)
                or has_ban_page(p.pages)
                or is_ban_page(p.group)
            ):
                continue
            if has_live_page and not any(
                pg.casefold() in ("live", "theo dõi", "theo doi") for pg in (p.pages or [])
            ):
                continue
            em = (p.email or "").strip()
            if not em or em.lower() in self._mail_dead_tokens:
                continue
            tok = (getattr(p, "mail_token", None) or "").strip() or tok_lookup.get(
                em.lower(), ""
            )
            if not tok and is_gmail_address(em):
                tok = normalize_gmail_app_password(getattr(p, "password", None))
            if is_mailtemp_address(em):
                if not tok:
                    tok = (getattr(p, "password", None) or "").strip() or "webmail"
                targets.append((p.name, em, tok))
                continue
            rt, _ = split_mail_token(tok)
            if not rt:
                continue
            targets.append((p.name, em, tok))
        return targets

    def record_mail_checkpoint_hit(
        self,
        profile_name: str,
        hit: dict[str, str] | None,
    ) -> None:
        clean_name = (profile_name or "").strip()
        if not clean_name:
            return
        if hit is None:
            if self._mail_checkpoint_hits.pop(clean_name, None) is not None:
                self._save_settings()
            return
        self._mail_checkpoint_hits[clean_name] = {
            str(k): str(v) for k, v in hit.items() if v is not None
        }
        self._save_settings()

    def record_mail_scan(
        self,
        hits: dict[str, dict[str, str]],
        *,
        dead_tokens: set[str] | list[str] | None = None,
        scanned_names: set[str] | list[str] | None = None,
        update_timestamp: bool = True,
        scan_ts: float | None = None,
    ) -> None:
        if update_timestamp:
            self._last_mail_scan_ts = time.time() if scan_ts is None else float(scan_ts)
            self._last_aborted_mail_scan_ts = 0.0
        else:
            self._last_aborted_mail_scan_ts = (
                time.time() if scan_ts is None else float(scan_ts)
            )
        if scanned_names:
            for pname in scanned_names:
                if pname not in hits:
                    self._mail_checkpoint_hits.pop(pname, None)
        for pname, hval in (hits or {}).items():
            if pname and isinstance(hval, dict):
                self._mail_checkpoint_hits[pname] = {
                    str(k): str(v) for k, v in hval.items() if v is not None
                }
        if dead_tokens:
            for em in dead_tokens:
                clean_em = str(em).strip().lower()
                if clean_em:
                    self._mail_dead_tokens.add(clean_em)
        self._save_settings()

    def clear_mail_checkpoint_hits(
        self,
        names: list[str] | set[str] | None = None,
    ) -> None:
        if names is None:
            if self._mail_checkpoint_hits:
                self._mail_checkpoint_hits.clear()
                self._save_settings()
            return
        changed = False
        for n in names:
            if self._mail_checkpoint_hits.pop(n, None) is not None:
                changed = True
        if changed:
            self._save_settings()

    @property
    def cooldown_minutes(self) -> int:
        return self._cooldown_minutes

    def set_cooldown_minutes(self, minutes: int) -> None:
        self._cooldown_minutes = max(0, min(1440, int(minutes)))
        self._save_settings()

    @property
    def sort_by(self) -> str:
        return self._sort_by

    def set_sort_by(self, sort_by: str) -> None:
        clean = (sort_by or "custom").strip() or "custom"
        if clean != self._sort_by:
            self._sort_by = clean
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
        ep = extract_proxy_endpoint(proxy)
        if port:
            self._last_used_ports[profile_name] = port
        else:
            self._last_used_ports.pop(profile_name, None)
        if ep:
            self._last_used_endpoints[profile_name] = ep
        else:
            self._last_used_endpoints.pop(profile_name, None)
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
        s_isp = str(stored.get("isp") or "").strip()
        s_type = str(stored.get("ip_type") or "").strip().upper()
        if not s_type and s_isp:
            s_type = classify_isp_type(s_isp)
            if s_type:
                stored["ip_type"] = s_type
        if stored.get("scamalytics") is not None:
            meta_f = compute_meta_ip_fraud_score(
                int(stored["scamalytics"]),
                int(stored["proxycheck_risk"])
                if stored.get("proxycheck_risk") is not None
                else None,
                ip_type=s_type,
                proxy_flag=bool(stored.get("proxy_flag")),
            )
            if meta_f is not None:
                stored["fraud_score"] = meta_f
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
        if not clean_type and clean_isp:
            clean_type = classify_isp_type(clean_isp)
        if eff_scam is not None:
            meta_f = compute_meta_ip_fraud_score(
                int(eff_scam),
                int(eff_pc) if eff_pc is not None else None,
                ip_type=clean_type,
                proxy_flag=eff_flag,
            )
            if meta_f is not None:
                eff_fraud = meta_f
                if clean_ip in self._ip_scores:
                    self._ip_scores[clean_ip]["fraud_score"] = meta_f
                    if clean_type:
                        self._ip_scores[clean_ip]["ip_type"] = clean_type

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
        if old_name in self._last_used_endpoints:
            self._last_used_endpoints[new_name] = self._last_used_endpoints.pop(old_name)
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
        self._log_events_sig = ()
        self._log_history.clear()
        if old_name in self._disk_cache:
            self._disk_cache[new_name] = self._disk_cache.pop(old_name)
        self._verified_closed_ts.pop(old_name, None)
        self._verified_closed_ts.pop(new_name, None)
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
        self._verified_closed_ts.pop(profile_name, None)
        if proxy is not None:
            port = extract_proxy_port(proxy)
            ep = extract_proxy_endpoint(proxy)
            changed = False
            if port and self._last_used_ports.get(profile_name) != port:
                self._last_used_ports[profile_name] = port
                changed = True
            if ep and self._last_used_endpoints.get(profile_name) != ep:
                self._last_used_endpoints[profile_name] = ep
                changed = True
            if changed:
                self._save_settings()

    def record_launch_failed(self, profile_name: str) -> None:
        """Discard any pending live start timestamp when a browser launch fails before opening."""
        self._live_started_at.pop(profile_name, None)

    def record_stopped(self, profile_name: str) -> None:
        now = dt.datetime.now()
        self._live_stopped_at[profile_name] = now
        self._verified_closed_ts.pop(profile_name, None)
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
        h_sessions = h.get("sessions")
        live_sessions = self._live_sessions.get(profile_name)
        if not h_sessions and not live_sessions:
            return []
        failed_ends: list[dt.datetime] = list(h.get("failed_ends", []))
        port = self._last_used_ports.get(profile_name, "") or fallback_port
        merged: list[dict[str, Any]] = []
        accepted_intervals: list[tuple[dt.datetime | None, dt.datetime | None, str]] = []

        def _parse_ts(val: str) -> dt.datetime | None:
            if not val:
                return None
            with contextlib.suppress(ValueError):
                return dt.datetime.fromisoformat(val)
            return None

        candidates = list(h_sessions or []) + list(live_sessions or [])
        for s in candidates:
            st = str(s.get("start", "")).strip()
            en = str(s.get("end", "")).strip()
            if not st:
                continue
            st_dt = _parse_ts(st)
            en_dt = _parse_ts(en)
            if en_dt is not None and any(
                abs((en_dt - f_dt).total_seconds()) <= 10.0 for f_dt in failed_ends
            ):
                continue
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

    def _log_events_cache_file(self) -> pathlib.Path:
        return self._log_dir / ".log_events_cache.json"

    def _load_log_events_cache(self) -> None:
        if self._log_events_disk_loaded:
            return
        self._log_events_disk_loaded = True
        if "PYTEST_CURRENT_TEST" in os.environ:
            return
        path = self._log_events_cache_file()
        if not path.exists():
            return
        with contextlib.suppress(Exception):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for fname, item in raw.items():
                    if (
                        isinstance(fname, str)
                        and isinstance(item, list)
                        and len(item) == 3
                        and isinstance(item[0], (int, float))
                        and isinstance(item[1], int)
                        and isinstance(item[2], list)
                    ):
                        lines_list: list[tuple[str, str]] = [
                            (str(pair[0]), str(pair[1]))
                            for pair in item[2]
                            if isinstance(pair, (list, tuple)) and len(pair) == 2
                        ]
                        self._log_file_events_cache[fname] = (
                            float(item[0]),
                            int(item[1]),
                            lines_list,
                        )

    def _save_log_events_cache(self, active_names: set[str]) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ:
            return
        path = self._log_events_cache_file()
        payload = {
            fname: [mtime, fsize, lines]
            for fname, (mtime, fsize, lines) in self._log_file_events_cache.items()
            if fname in active_names
        }

        def _bg_write() -> None:
            with contextlib.suppress(Exception):
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
                os.replace(tmp, path)

        threading.Thread(target=_bg_write, daemon=True).start()

    def _refresh_log_history(self) -> dict[str, dict[str, Any]]:
        if not self._log_dir.exists():
            return self._log_history
        self._load_log_events_cache()
        log_files = sorted(self._log_dir.glob("foxprofile_*.log"))[-30:]
        valid_logs: list[tuple[pathlib.Path, float, int]] = []
        key_parts: list[tuple[str, float, int]] = []
        for lf in log_files:
            try:
                st = lf.stat()
                valid_logs.append((lf, st.st_mtime, st.st_size))
                key_parts.append((lf.name, st.st_mtime, st.st_size))
            except OSError:
                continue
        cache_key = tuple(key_parts)
        if cache_key == self._log_cache_key and self._log_history:
            return self._log_history

        log_cache_updated = False
        per_file_lines: list[tuple[str, list[tuple[str, str]]]] = []
        for lf, lf_mtime, lf_size in valid_logs:
            cached_f = self._log_file_events_cache.get(lf.name)
            if (
                cached_f is not None
                and abs(cached_f[0] - lf_mtime) < 1e-3
                and cached_f[1] == lf_size
            ):
                relevant_lines = cached_f[2]
            else:
                try:
                    raw_lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                relevant_lines = []
                for line in raw_lines:
                    if len(line) < 26 or not line[0].isdigit():
                        continue
                    sep_parts = line.split(" - ", 3)
                    if len(sep_parts) == 4 and len(sep_parts[0]) == 19:
                        ts_part = sep_parts[0]
                        msg = sep_parts[3].strip()
                    else:
                        m = _RE_LOG_LINE.match(line)
                        if not m:
                            continue
                        ts_part = m.group("ts")
                        msg = m.group("msg").strip()
                    if not msg or not msg.startswith(_LOG_MSG_PREFIXES):
                        continue
                    if msg[0] == "[" and not any(
                        tok in msg for tok in _BRACKET_EVENT_TOKENS
                    ):
                        continue
                    if msg.startswith("Updated profile:"):
                        mr_chk = _RE_RENAME_PROFILE.match(msg)
                        if not mr_chk or mr_chk.group("old").strip() == mr_chk.group("new").strip():
                            continue
                    relevant_lines.append((ts_part, msg))
                self._log_file_events_cache[lf.name] = (lf_mtime, lf_size, relevant_lines)
                log_cache_updated = True
            per_file_lines.append((lf.name, relevant_lines))

        if log_cache_updated:
            self._save_log_events_cache({lf.name for lf, _, _ in valid_logs})

        events_sig = tuple(
            (fname, len(lines), lines[-1] if lines else None)
            for fname, lines in per_file_lines
        )
        if events_sig == self._log_events_sig and self._log_history:
            self._log_cache_key = cache_key
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
                    "failed_ends": [],
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

        def _abort_failed_open(h: dict[str, Any], ts: dt.datetime | None) -> None:
            h["_open_start"] = None
            if ts is not None:
                h["failed_ends"].append(ts)
                if h["sessions"]:
                    last_s = h["sessions"][-1]
                    with contextlib.suppress(ValueError):
                        last_end = dt.datetime.strptime(
                            str(last_s.get("end", "")), "%Y-%m-%d %H:%M:%S"
                        )
                        last_start = dt.datetime.strptime(
                            str(last_s.get("start", "")), "%Y-%m-%d %H:%M:%S"
                        )
                        if abs((last_end - ts).total_seconds()) <= 10.0:
                            h["sessions"].pop()
                            dur_rem = max(0.0, (last_end - last_start).total_seconds())
                            if str(last_s.get("end", "")).startswith(today_str):
                                h["today_seconds"] = max(0.0, h["today_seconds"] - dur_rem)
                            if h["sessions"]:
                                prev_end_str = str(h["sessions"][-1].get("end", ""))
                                h["last_closed"] = dt.datetime.strptime(
                                    prev_end_str, "%Y-%m-%d %H:%M:%S"
                                )
                            else:
                                h["last_closed"] = None

        for _fname, relevant_lines in per_file_lines:
            for ts_str, msg in relevant_lines:
                if msg[0] == "[":
                    end_br = msg.find("]")
                    if end_br <= 1:
                        continue
                    pname = msg[1:end_br].strip()
                    rest = msg[end_br + 1 :].strip()
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
                            _abort_failed_open(h, ts)
                    continue

                if msg.startswith("Updated profile:"):
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
                                    nh["failed_ends"].extend(oh.get("failed_ends", []))
                                    if oh["last_closed"] and (
                                        not nh["last_closed"] or oh["last_closed"] > nh["last_closed"]
                                    ):
                                        nh["last_closed"] = oh["last_closed"]
                                    if oh["_open_start"] and not nh["_open_start"]:
                                        nh["_open_start"] = oh["_open_start"]
                                nh["sessions"].sort(key=lambda item: str(item.get("start", "")))
                    continue

                if msg.startswith(("Moved profile to trash:", "Deleted profile:")):
                    mt = _RE_TRASH_PROFILE.match(msg)
                    if mt:
                        hist.pop(mt.group("prof").strip(), None)
                    continue

                if msg.startswith(
                    (
                        "Starting browser for profile:",
                        "Browser started for profile:",
                        "\u0110ang kh\u1edfi ch\u1ea1y:",
                        "Launching:",
                    )
                ):
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

                if msg.startswith(("Launch failed for profile ", "Error starting browser for ")):
                    mlf = _RE_LAUNCH_FAILED.match(msg)
                    if mlf:
                        try:
                            ts_fail = dt.datetime.fromisoformat(ts_str)
                        except ValueError:
                            ts_fail = None
                        pname = mlf.group("prof").strip()
                        h = _ensure(pname)
                        _abort_failed_open(h, ts_fail)
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
                    nh["failed_ends"].extend(oh.get("failed_ends", []))
                    if oh["last_closed"] and (
                        not nh["last_closed"] or oh["last_closed"] > nh["last_closed"]
                    ):
                        nh["last_closed"] = oh["last_closed"]

        # Prune any persisted _live_sessions entries that correspond to LAUNCH_FAILED timestamps
        pruned_live = False
        for pname, h_entry in hist.items():
            f_ends: list[dt.datetime] = h_entry.get("failed_ends", [])
            if not f_ends or pname not in self._live_sessions:
                continue
            orig_list = self._live_sessions[pname]
            kept_list = []
            for s_item in orig_list:
                en_str = str(s_item.get("end", "")).strip()
                is_failed = False
                if en_str:
                    with contextlib.suppress(ValueError):
                        en_dt = dt.datetime.strptime(en_str, "%Y-%m-%d %H:%M:%S")
                        if any(abs((en_dt - f_dt).total_seconds()) <= 10.0 for f_dt in f_ends):
                            is_failed = True
                if is_failed:
                    pruned_live = True
                else:
                    kept_list.append(s_item)
            if len(kept_list) != len(orig_list):
                if kept_list:
                    self._live_sessions[pname] = kept_list
                else:
                    self._live_sessions.pop(pname, None)
        if pruned_live:
            self._save_settings(async_write=True)

        self._log_cache_key = cache_key
        self._log_events_sig = events_sig
        self._log_history = hist
        return hist

    def _inspect_profile_disk(
        self,
        name: str,
        is_running: bool,
        *,
        fast_init: bool = False,
    ) -> dict[str, Any]:
        pdir = self._data_dir / name
        now_ts = time.time()
        cached = self._disk_cache.get(name)

        if (
            not is_running
            and cached is not None
            and cached.get("_cache_ver") == 6
            and not cached.get("_is_running")
            and (
                fast_init
                or (
                    "PYTEST_CURRENT_TEST" not in os.environ
                    and name not in self._live_stopped_at
                    and (now_ts - self._verified_closed_ts.get(name, 0.0)) < 25.0
                )
            )
        ):
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
            cached["burst_10m"] = 0
            return cached

        tabs_file = pdir / "tabs.json"
        tabs_mtime = 0.0
        cookies_db = pdir / "cookies.sqlite"
        cookies_mtime = 0.0
        is_chrome_cookies = False
        places_db = pdir / "places.sqlite"
        places_mtime = 0.0
        is_chrome_history = False
        prefs_mtime = 0.0
        fp_file = pdir / "fingerprint.json"
        fp_mtime = 0.0
        ch_fp_mtime = 0.0
        is_chrome_persona = False
        has_cache2 = False
        has_default_dir = False

        try:
            with os.scandir(pdir) as it:
                for entry in it:
                    ename = entry.name
                    if ename not in _WATCHED_PROFILE_ENTRIES:
                        continue
                    if ename == "tabs.json":
                        with contextlib.suppress(OSError):
                            tabs_mtime = entry.stat(follow_symlinks=False).st_mtime
                    elif ename == "cookies.sqlite":
                        with contextlib.suppress(OSError):
                            cookies_mtime = entry.stat(follow_symlinks=False).st_mtime
                    elif ename == "places.sqlite":
                        with contextlib.suppress(OSError):
                            places_mtime = entry.stat(follow_symlinks=False).st_mtime
                    elif ename == "prefs.js":
                        with contextlib.suppress(OSError):
                            prefs_mtime = entry.stat(follow_symlinks=False).st_mtime
                    elif ename == "fingerprint.json":
                        with contextlib.suppress(OSError):
                            fp_mtime = entry.stat(follow_symlinks=False).st_mtime
                    elif ename == "chrome_persona.json":
                        with contextlib.suppress(OSError):
                            ch_fp_mtime = entry.stat(follow_symlinks=False).st_mtime
                    elif ename == "cache2":
                        with contextlib.suppress(OSError):
                            has_cache2 = entry.is_dir(follow_symlinks=False)
                    elif ename == "Default":
                        with contextlib.suppress(OSError):
                            has_default_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
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
                "has_accountscenter": False,
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

        if fp_mtime == 0.0 and ch_fp_mtime > 0.0:
            fp_file = pdir / "chrome_persona.json"
            fp_mtime = ch_fp_mtime
            is_chrome_persona = True

        # Only probe Chromium `Default/` paths when Camoufox files are absent
        if has_default_dir and (
            cookies_mtime == 0.0
            or places_mtime == 0.0
            or prefs_mtime == 0.0
            or not has_cache2
        ):
            def _stat_mtime(p: pathlib.Path) -> float:
                try:
                    return p.stat().st_mtime
                except OSError:
                    return 0.0

            default_dir = pdir / "Default"
            if cookies_mtime == 0.0:
                for _cand in (
                    default_dir / "Network" / "Cookies",
                    default_dir / "Cookies",
                ):
                    _cm = _stat_mtime(_cand)
                    if _cm > 0.0:
                        cookies_db = _cand
                        cookies_mtime = _cm
                        is_chrome_cookies = True
                        break
            if places_mtime == 0.0:
                _ch_hist = default_dir / "History"
                _pm = _stat_mtime(_ch_hist)
                if _pm > 0.0:
                    places_db = _ch_hist
                    places_mtime = _pm
                    is_chrome_history = True
            if prefs_mtime == 0.0:
                _ch_prefs = default_dir / "Preferences"
                _prm = _stat_mtime(_ch_prefs)
                if _prm > 0.0:
                    prefs_mtime = _prm
            if not has_cache2:
                has_cache2 = (
                    (default_dir / "Cache").is_dir()
                    or (default_dir / "Network" / "Cache").is_dir()
                )

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
                    cached.get("_cache_ver") == 6
                    and cached.get("_tabs_mtime") == tabs_mtime
                    and cached.get("_cookies_mtime") == cookies_mtime
                    and cached.get("_places_mtime") == places_mtime
                    and cached.get("_fp_mtime") == fp_mtime
                ):
                    cached["burst_10m"] = 0
                    cached["last_active_ts"] = last_active_ts
                    cached["has_cache2"] = has_cache2
                    self._verified_closed_ts[name] = now_ts
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
            "_cache_ver": 6,
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
            "has_accountscenter": (
                cached.get("has_accountscenter", False) if cached else False
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
                if is_chrome_persona and isinstance(fp_obj, dict):
                    norm_fp = json.dumps(fp_obj, sort_keys=True, separators=(",", ":"))
                    result["fp_hash"] = hashlib.sha1(norm_fp.encode("utf-8")).hexdigest()[:16]
                    plat_raw = str(fp_obj.get("platform") or "windows").lower()
                    plat = {"windows": "Win32", "macos": "MacIntel", "linux": "Linux x86_64"}.get(
                        plat_raw, plat_raw
                    )
                    cores = fp_obj.get("hardware_concurrency") or 8
                    w = fp_obj.get("screen_width")
                    h_px = fp_obj.get("screen_height")
                    rend_full = str(fp_obj.get("gpu_renderer") or "")
                    if not (w and h_px) or not rend_full:
                        with contextlib.suppress(Exception):
                            from chrome_engine.persona import Persona

                            p_obj = Persona.from_json(fp_obj)
                            if not (w and h_px):
                                w, h_px = p_obj.effective_screen()
                            if not rend_full:
                                _, rend_full = p_obj.effective_gpu()
                    rend = str(rend_full or "")[:48]
                    if plat and w and h_px:
                        result["fp_hw_sig"] = f"{plat}|{w}x{h_px}|{cores}c|{rend}"
                    scr_lbl = f"{w}x{h_px}" if (w and h_px) else ""
                    cpu_lbl = f"{cores} CPU" if cores is not None and cores != "" else ""
                    result["device_label"] = (
                        f"{scr_lbl} · {cpu_lbl}" if (scr_lbl and cpu_lbl) else cpu_lbl
                    )
                else:
                    fp_inner = fp_obj.get("fingerprint", {}) if isinstance(fp_obj, dict) else {}
                    if isinstance(fp_inner, dict) and fp_inner:
                        norm_fp = json.dumps(fp_inner, sort_keys=True, separators=(",", ":"))
                        result["fp_hash"] = hashlib.sha1(norm_fp.encode("utf-8")).hexdigest()[:16]
                        nav = fp_inner.get("navigator") or {}
                        scr = fp_inner.get("screen") or {}
                        vc = (
                            fp_inner.get("videoCard")
                            or fp_inner.get("gpu")
                            or fp_inner.get("webgl")
                            or {}
                        )
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
                result["has_thread_url"] = any(_is_muse_thread_url(u) for u in str_urls)
            except Exception:
                pass

        # Only read cookies.sqlite / Default/Network/Cookies when the profile's browser is NOT running
        if not is_running and cookies_mtime > 0:
            try:
                db_posix = cookies_db.resolve().as_posix()
                uri = f"file:///{urllib.parse.quote(db_posix, safe=':/')}?mode=ro&immutable=1"
                conn = sqlite3.connect(uri, uri=True, timeout=0.3)
                try:
                    cur = conn.cursor()
                    if is_chrome_cookies:
                        cur.execute(
                            "SELECT host_key, name, creation_utc, expires_utc FROM cookies"
                        )
                        raw_rows = cur.fetchall()
                        rows = [
                            (
                                h,
                                n,
                                max(0.0, (float(ct) / 1_000_000.0) - 11_644_473_600.0)
                                if ct and float(ct) > 0
                                else 0,
                                max(0.0, (float(ex) / 1_000_000.0) - 11_644_473_600.0)
                                if ex and float(ex) > 0
                                else 0,
                            )
                            for h, n, ct, ex in raw_rows
                        ]
                    else:
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
                has_meta_fs = False
                has_chk_cookie = False
                datr_age_days = 0.0
                earliest_datr_ct = 0.0
                datr_hosts: set[str] = set()
                cookie_hosts: set[str] = set()
                exact_hatch_sess_exp = 0.0
                muse_auth_exp = 0.0
                fallback_sess_exp = 0.0
                for _host, cname, ctime, exp in rows:
                    host_l = str(_host or "").lower().lstrip(".")
                    if host_l:
                        cookie_hosts.add(_extract_root_domain(host_l) or host_l)
                    is_hatch_host = (
                        "muse" in host_l or "hatch" in host_l or "metaaivm" in host_l
                    )
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
                    elif cname_s == "fs" and "meta.com" in host_l:
                        has_meta_fs = True
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
                            if cname_s == "hatch_sess":
                                exact_hatch_sess_exp = max(exact_hatch_sess_exp, exp_val)
                            elif is_hatch_host:
                                muse_auth_exp = max(muse_auth_exp, exp_val)
                            else:
                                fallback_sess_exp = max(fallback_sess_exp, exp_val)
                latest_exp = exact_hatch_sess_exp or muse_auth_exp or fallback_sess_exp
                # As of 2026-10, muse.ai no longer sets legacy `hatch_gw` / `hatch_vml` cookies on every
                # logged-in session; primary auth relies on `hatch_sess` + `hatch_native_auth_device` / `__Host-hatch_one_tap`
                # and dynamically negotiates the gateway token over HTTPS (`/api/auth/check` -> `/api/hatch/token`).
                if (
                    not hatch_gw
                    and not has_chk_cookie
                    and (has_muse_onetap or has_muse_native_dev)
                    and (
                        (has_muse_layout and (hatch_vml or result["has_thread_url"] or hatch_sess))
                        or (hatch_sess and (has_muse_native_dev or result["has_thread_url"]))
                    )
                ):
                    hatch_gw = True
                    hatch_vml = True
                elif (
                    hatch_gw
                    and not hatch_vml
                    and not has_chk_cookie
                    and (has_muse_onetap or has_muse_native_dev or result["has_thread_url"])
                ):
                    hatch_vml = True
                if (
                    not has_ps_l
                    and not has_chk_cookie
                    and has_meta_fs
                    and len(datr_hosts) >= 3
                    and (has_muse_onetap or has_muse_native_dev)
                ):
                    has_ps_l = True
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

        # Only read places.sqlite / Default/History when the profile's browser is NOT running
        if not is_running and places_mtime > 0:
            try:
                pl_posix = places_db.resolve().as_posix()
                pl_uri = f"file:///{urllib.parse.quote(pl_posix, safe=':/')}?mode=ro&immutable=1"
                conn_pl = sqlite3.connect(pl_uri, uri=True, timeout=0.3)
                try:
                    cur_pl = conn_pl.cursor()
                    if is_chrome_history:
                        cur_pl.execute("SELECT url, '' FROM urls WHERE visit_count > 0")
                    else:
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
                has_ac_mut = False
                for u, rh in pl_rows:
                    u_l = str(u or "").lower()
                    rh_s = str(rh or "")
                    if (
                        "auth.meta.com/checkpoint" in u_l
                        or "oidc/start?checkpoint=1" in u_l
                        or "auth.meta.com/recover" in u_l
                    ):
                        has_chk_url = True
                    if (
                        "accountscenter.meta.com/personal_info" in u_l
                        or "accountscenter.meta.com/password_and_security" in u_l
                    ):
                        has_ac_mut = True
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
                result["has_accountscenter"] = has_ac_mut
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
            self._verified_closed_ts[name] = now_ts
            self._disk_cache_dirty = True
        return result

    def snapshot(
        self,
        profiles: list[Profile],
        running_names: set[str],
        *,
        fast_init: bool = False,
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
            disk = self._inspect_profile_disk(p.name, is_run, fast_init=fast_init)
            disk_map[p.name] = disk

            port = extract_proxy_port(p.proxy)
            ep_seed = extract_proxy_endpoint(p.proxy)
            if port and p.name in self._last_used_ports and self._last_used_ports[p.name] != port:
                m_leg = _RE_LEGACY_PORT.search((p.proxy or "").strip())
                leg_port = f":{m_leg.group(1)}" if m_leg else ""
                if leg_port and self._last_used_ports[p.name] == leg_port:
                    self._last_used_ports[p.name] = port
                    if ep_seed:
                        self._last_used_endpoints[p.name] = ep_seed
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
                if ep_seed:
                    self._last_used_endpoints[p.name] = ep_seed
                ports_seeded = True
            elif (
                ep_seed
                and (disk.get("hatch_gw") or disk.get("hatch_sess"))
                and p.name not in self._last_used_endpoints
                and self._last_used_ports.get(p.name) == port
            ):
                self._last_used_endpoints[p.name] = ep_seed
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
                disk_dt = min(now, dt.datetime.fromtimestamp(disk_ts))
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
            self._save_settings(async_write=True)

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
                        elapsed_s = (now - sib_lc).total_seconds()
                        if -5.0 <= elapsed_s < cd_target * 60:
                            ago = max(0, int(elapsed_s // 60))
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
            last_ep = self._last_used_endpoints.get(p.name, "")
            has_sess = bool(disk["hatch_gw"] or disk["hatch_sess"])
            port_diff = bool(last_port and port != last_port)
            ep_diff = bool(last_ep and ep and ep != last_ep)
            proxy_port_changed = bool(has_sess and (port_diff or ep_diff))
            if ep_diff and not port_diff:
                last_port = last_ep

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
            has_meta_checkpoint = bool(
                disk.get("has_meta_checkpoint") or p.name in self._mail_checkpoint_hits
            )
            datr_age_days = float(disk.get("datr_age_days") or 0.0)
            datr_domains = int(disk.get("datr_domains") or 0)
            ext_domains = int(disk.get("ext_domains") or 0)
            cookie_count = int(disk.get("cookie_count") or 0)
            cookie_domains = int(disk.get("cookie_domains") or 0)
            history_pages = int(disk.get("history_pages") or 0)
            history_domains = int(disk.get("history_domains") or 0)
            cal_age_days = compute_days_since(p.created_at)
            sess_cnt, active_days, total_mins = sess_stats_map.get(p.name, (0, 0, 0))
            age_days = cal_age_days if cal_age_days is not None else int(datr_age_days)
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
            if not is_banned and p.name in self._mail_checkpoint_hits:
                snap.mail_checkpoint_hits.append(
                    (p.name, dict(self._mail_checkpoint_hits[p.name]))
                )

            has_tfa = bool((p.two_fa or "").strip())
            has_mtok = bool((p.mail_token or "").strip())
            has_pwd_or_rec = bool(
                (p.password or "").strip() or (p.recovery_email or "").strip()
            )
            has_recovery_key = bool(has_tfa or has_mtok)
            has_recovery_armor = bool(has_tfa or (has_mtok and has_pwd_or_rec))

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
                trust_score=0,
                trust_tier="banned" if (is_banned or has_meta_checkpoint) else "cold",
                cluster=cluster,
                cluster_collision_label=cluster_collision_label,
                cluster_busy_by=cluster_busy_by,
                can_open_parallel=can_open_parallel,
                has_muse_auth=bool(disk.get("has_muse_auth")),
                has_proxy=bool(p.proxy),
                has_geo_pin=bool(p.proxy and p.timezone and p.locale),
                has_recovery_key=has_recovery_key,
                has_recovery_armor=has_recovery_armor,
                has_accountscenter=bool(disk.get("has_accountscenter")),
                shared_port_count=max(1, len(eff_siblings)),
            )

        self._populate_audit_findings(profiles, disk_map, snap)

        # Single source of truth: compute final Trust Score 2.0 (0-100) after audit_codes are attached
        for p in profiles:
            info = snap.profiles[p.name]
            bd = compute_trust_breakdown(info, p)
            info.trust_score = int(bd["final_score"])
            info.trust_tier = str(bd["trust_tier"])
            if info.trust_tier == "diamond":
                snap.diamond_count += 1
                snap.elite_count += 1
                snap.platinum_count += 1
                snap.trusted_count += 1
            elif info.trust_tier == "elite":
                snap.elite_count += 1
                snap.platinum_count += 1
                snap.trusted_count += 1
            elif info.trust_tier == "platinum":
                snap.platinum_count += 1
                snap.trusted_count += 1
            elif info.trust_tier == "trusted":
                snap.trusted_count += 1

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

        # 1. Burned Proxy / Historical 128-bit IP / IPv6 /64 Subnet / Seller Batch shared with Banned profiles
        banned_by_ep: dict[str, list[str]] = {}
        banned_by_gw_port: dict[str, list[str]] = {}
        banned_by_ip: dict[str, list[str]] = {}
        banned_by_v6: dict[str, list[str]] = {}
        banned_fvia_names: list[str] = []
        for p in profiles:
            info = snap.profiles.get(p.name)
            if not info or not info.is_banned:
                continue
            ep = extract_proxy_endpoint(p.proxy)
            if ep:
                banned_by_ep.setdefault(ep, []).append(p.name)
            for s_item in self._live_sessions.get(p.name, []):
                pt_s = str(s_item.get("port") or "").strip()
                if pt_s.startswith(":5"):
                    lst_gw = banned_by_gw_port.setdefault(pt_s, [])
                    if p.name not in lst_gw:
                        lst_gw.append(p.name)
            p_ips: list[str] = []
            if info.last_exit_ip:
                p_ips.append(info.last_exit_ip.strip().lower())
            for ent in self._ip_history.get(p.name, []):
                ip_h = str(ent.get("ip") or "").strip().lower()
                if ip_h and ip_h not in p_ips:
                    p_ips.append(ip_h)
                pt_h = str(ent.get("port") or "").strip()
                if pt_h.startswith(":5"):
                    lst_gw = banned_by_gw_port.setdefault(pt_h, [])
                    if p.name not in lst_gw:
                        lst_gw.append(p.name)
            for ip_val in p_ips:
                lst_ip = banned_by_ip.setdefault(ip_val, [])
                if p.name not in lst_ip:
                    lst_ip.append(p.name)
            if info.last_exit_ip:
                v6_pref = _ipv6_prefix_64(info.last_exit_ip)
                if v6_pref and is_ban_status(p.status):
                    banned_by_v6.setdefault(v6_pref, []).append(p.name)
            if (p.recovery_email or "").strip().lower().endswith("@fviainboxes.com"):
                banned_fvia_names.append(p.name)

        subnet_affected: dict[str, list[str]] = {}
        for p in profiles:
            info = snap.profiles.get(p.name)
            if not info or info.is_banned:
                continue
            ep = extract_proxy_endpoint(p.proxy)
            b_ep_list: list[str] = list(banned_by_ep.get(ep, [])) if ep else []
            if (
                is_gateway_ipv6_cluster(info.cluster)
                and info.port
                and info.port in banned_by_gw_port
            ):
                for bn in banned_by_gw_port[info.port]:
                    if bn not in b_ep_list:
                        b_ep_list.append(bn)

            my_hist_ips: list[str] = []
            if info.last_exit_ip:
                my_hist_ips.append(info.last_exit_ip.strip().lower())
            for ent in self._ip_history.get(p.name, []):
                ip_h = str(ent.get("ip") or "").strip().lower()
                if ip_h and ip_h not in my_hist_ips:
                    my_hist_ips.append(ip_h)
            matched_hist_ip = next((ip_v for ip_v in my_hist_ips if ip_v in banned_by_ip), "")

            if b_ep_list:
                b_names = ", ".join(b_ep_list)
                _attach(
                    "burned_proxy",
                    "critical",
                    get_string("audit_burned_proxy_title", name=p.name, port=info.port or ep),
                    get_string(
                        "audit_burned_proxy_detail",
                        name=p.name,
                        banned=b_names,
                        port=info.port or ep,
                    ),
                    [p.name, *b_ep_list],
                )
            elif matched_hist_ip:
                b_ip_list = banned_by_ip[matched_hist_ip]
                b_names = ", ".join(b_ip_list)
                _attach(
                    "burned_ip",
                    "critical",
                    get_string("audit_burned_ip_title", name=p.name, ip=matched_hist_ip),
                    get_string(
                        "audit_burned_ip_detail",
                        name=p.name,
                        banned=b_names,
                        ip=matched_hist_ip,
                    ),
                    [p.name, *b_ip_list],
                )
            elif info.last_exit_ip:
                v6_pref = _ipv6_prefix_64(info.last_exit_ip)
                if v6_pref and v6_pref in banned_by_v6:
                    subnet_affected.setdefault(v6_pref, []).append(p.name)

            p_name_clean = p.name.strip().lower()
            email_low = (p.email or "").strip().lower()
            rec_low = (p.recovery_email or "").strip().lower()
            notes_low = (p.notes or "").strip().lower()
            group_low = (p.group or "").strip().lower()
            cr_date = _parse_profile_created_date(p.created_at)
            is_exempt_fvia = bool(
                (cr_date is not None and cr_date <= dt.date(2026, 10, 17))
                or email_low == "ashleymilleruh3nptuw@hotmail.com"
                or "gốc gmail" in notes_low
                or "@ngoctugroup.com" in notes_low
            )
            is_tainted_batch = bool(
                "@tuan89.site" in notes_low
                or email_low.endswith("@tuan89.site")
                or "+19376123124" in notes_low
                or group_low.startswith("4.")
                or group_low.startswith("5.")
                or p_name_clean in {"62", "63", "tlazglb@hotmail.com"}
                or email_low == "tlazglb@hotmail.com"
                or (
                    len(banned_fvia_names) >= 3
                    and rec_low.endswith("@fviainboxes.com")
                    and not is_exempt_fvia
                )
            )
            if is_tainted_batch:
                _attach(
                    "burned_batch",
                    "warning",
                    f"Hồ sơ [{p.name}] dính lô phôi rủi ro (@tuan89.site / +19376123124)",
                    (
                        f"Hồ sơ [{p.name}] thuộc nhóm phôi rủi ro cao (gốc @tuan89.site, "
                        f"cụm SĐT +19376123124 hoặc cùng lô {len(banned_fvia_names)} tài khoản "
                        f"@fviainboxes.com đã quét ban). Tuyệt đối cách ly, không bấm Referral hay mở chung đội."
                    ),
                    [p.name],
                )

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
