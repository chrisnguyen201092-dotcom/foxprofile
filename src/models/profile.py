import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

# Browser engines a profile can run on. A profile keeps its engine for life:
# the two keep incompatible browser data in the same directory.
ENGINES = ("camoufox", "chrome")

# Optional highlight colors for profile rows on the UI (None = default, no bar).
PROFILE_COLOR_PALETTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "palette_vibrant",
        (
            "#EF4444",  # Red
            "#F97316",  # Orange
            "#EAB308",  # Amber
            "#22C55E",  # Green
            "#06B6D4",  # Cyan
            "#3B82F6",  # Blue
            "#8B5CF6",  # Violet
            "#EC4899",  # Pink
        ),
    ),
    (
        "palette_deep",
        (
            "#B91C1C",  # Crimson
            "#C2410C",  # Rust
            "#A16207",  # Bronze
            "#15803D",  # Forest
            "#0E7490",  # Teal
            "#1D4ED8",  # Royal Blue
            "#6D28D9",  # Indigo
            "#BE185D",  # Magenta
        ),
    ),
    (
        "palette_pastel",
        (
            "#F87171",  # Coral
            "#FB923C",  # Peach
            "#FACC15",  # Lemon
            "#4ADE80",  # Mint
            "#22D3EE",  # Sky
            "#60A5FA",  # Periwinkle
            "#A78BFA",  # Lavender
            "#F472B6",  # Rose
        ),
    ),
    (
        "palette_earth",
        (
            "#78350F",  # Espresso
            "#92400E",  # Terracotta
            "#3F6212",  # Olive
            "#115E59",  # Pine
            "#1E3A8A",  # Navy
            "#4C1D95",  # Plum
            "#475569",  # Slate
            "#1E293B",  # Charcoal
        ),
    ),
)

PROFILE_COLORS: tuple[str, ...] = tuple(c for _, group in PROFILE_COLOR_PALETTES for c in group)

PROFILE_STATUS_PRESETS: tuple[tuple[str, str, str], ...] = (
    ("ready", "status_ready", "#16A34A"),
    ("working", "status_working", "#2563EB"),
    ("done", "status_done", "#7C3AED"),
    ("check", "status_check", "#D97706"),
    ("hold", "status_hold", "#DC2626"),
)

_STATUS_PRESET_MAP: dict[str, tuple[str, str]] = {
    code: (str_key, hex_col) for code, str_key, hex_col in PROFILE_STATUS_PRESETS
}


def default_status_tags() -> list[dict[str, str]]:
    return [
        {"code": code, "label": "", "color": hex_col} for code, _, hex_col in PROFILE_STATUS_PRESETS
    ]


_ACTIVE_STATUS_TAGS: list[dict[str, str]] = default_status_tags()


def get_active_status_tags() -> list[dict[str, str]]:
    return [dict(t) for t in _ACTIVE_STATUS_TAGS]


def set_active_status_tags(tags: list[dict[str, str]] | None) -> None:
    global _ACTIVE_STATUS_TAGS
    _ACTIVE_STATUS_TAGS = default_status_tags() if tags is None else [dict(t) for t in tags]


def resolve_tag_label(tag: dict[str, str], get_str_fn=None) -> str:
    lbl = (tag.get("label") or "").strip()
    if lbl:
        return lbl
    code = (tag.get("code") or "").strip().lower()
    preset = _STATUS_PRESET_MAP.get(code)
    if preset is not None and get_str_fn is not None:
        return str(get_str_fn(preset[0]))
    return tag.get("code") or ""


def resolve_status_display(status: str | None, get_str_fn=None) -> tuple[str, str] | None:
    """Return (display_label, hex_color) for a status preset code or custom status tag."""
    if not status:
        return None
    clean = status.strip()
    if not clean:
        return None
    low = clean.casefold()
    for tag in _ACTIVE_STATUS_TAGS:
        t_code = (tag.get("code") or "").strip()
        t_label = (tag.get("label") or "").strip()
        resolved_lbl = resolve_tag_label(tag, get_str_fn)
        if low in {t_code.casefold(), t_label.casefold(), resolved_lbl.casefold()} - {""}:
            return resolved_lbl, (tag.get("color") or "#0284C7")
    preset = _STATUS_PRESET_MAP.get(clean.lower())
    if preset is not None:
        str_key, hex_col = preset
        label = get_str_fn(str_key) if get_str_fn is not None else clean
        return label, hex_col
    return clean, "#0284C7"


_BAN_KEYWORDS = frozenset(
    {
        "hold",
        "ban",
        "banned",
        "bị ban",
        "bi ban",
        "tạm ngưng",
        "tam ngung",
        "die",
        "dead",
        "khóa",
        "khoa",
        "locked",
        "suspended",
        "suspend",
        "disconnect",
        "disconnected",
        "disabled",
    }
)
_DAYS_RE = re.compile(r"^(\d{1,4})\s*(?:d|days?|ngay|ngày)?$", re.IGNORECASE)
_YMD_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")


def today_ymd() -> str:
    return date.today().isoformat()


def parse_flexible_date(raw: object, today: date | None = None) -> str | None:
    """Parse YYYY-MM-DD, DD/MM/YYYY, or integer days ago ('12' -> today - 12d) into YYYY-MM-DD."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    ref = today or date.today()
    m_days = _DAYS_RE.match(s)
    if m_days:
        days = int(m_days.group(1))
        if 0 <= days <= 3650:
            return (ref - timedelta(days=days)).isoformat()
        return None
    m_ymd = _YMD_RE.match(s)
    if m_ymd:
        try:
            dt = date(int(m_ymd.group(1)), int(m_ymd.group(2)), int(m_ymd.group(3)))
            if 2000 <= dt.year <= 2100:
                return dt.isoformat()
        except ValueError:
            return None
    m_dmy = _DMY_RE.match(s)
    if m_dmy:
        try:
            dt = date(int(m_dmy.group(3)), int(m_dmy.group(2)), int(m_dmy.group(1)))
            if 2000 <= dt.year <= 2100:
                return dt.isoformat()
        except ValueError:
            return None
    return None


def compute_days_since(ymd: str | None, today: date | None = None) -> int | None:
    norm = parse_flexible_date(ymd, today=today)
    if not norm:
        return None
    try:
        dt = date.fromisoformat(norm)
    except ValueError:
        return None
    ref = today or date.today()
    return max(0, (ref - dt).days)


def format_date_dmy(ymd: str | None) -> str:
    norm = parse_flexible_date(ymd)
    if not norm:
        return ""
    try:
        dt = date.fromisoformat(norm)
        return dt.strftime("%d/%m/%Y")
    except ValueError:
        return norm


def is_ban_status(status: str | None, get_str_fn=None) -> bool:
    if not status or not status.strip():
        return False
    low = status.strip().casefold()
    if low in _BAN_KEYWORDS:
        return True
    disp = resolve_status_display(status, get_str_fn)
    return bool(disp is not None and disp[0].strip().casefold() in _BAN_KEYWORDS)


def is_ban_page(page_name: str | None) -> bool:
    if not page_name or not page_name.strip():
        return False
    return page_name.strip().casefold() in _BAN_KEYWORDS


def has_ban_page(pages: list[str] | None) -> bool:
    return any(is_ban_page(pg) for pg in (pages or []))


@dataclass
class Profile:
    name: str
    proxy: str | None = None
    os_type: str = "windows"
    # None = follow the proxy's exit IP (Camoufox geoip); set to pin a value.
    timezone: str | None = None
    locale: str | None = None
    engine: str = "camoufox"
    color: str | None = None
    group: str | None = None
    notes: str | None = None
    pages: list[str] = field(default_factory=list)
    status: str | None = None
    pinned: bool = False
    created_at: str | None = None
    banned_at: str | None = None
    email: str | None = None
    password: str | None = None
    two_fa: str | None = None
    recovery_email: str | None = None
    mail_token: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)
