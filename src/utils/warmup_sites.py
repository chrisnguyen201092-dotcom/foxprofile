import contextlib
import json
import pathlib
import random
import re

from ..core.config import PROFILES_FILE

DEFAULT_WARMUP_TEXT = """Web vào Meta không bị chặn
https://muse.ai/?aymh_complete=1 — Link bypass vòng lặp /aymh/redirect-cycle/ khi mở lại acc

Nhóm 1: Kích hoạt trực tiếp Meta Pixel (fbevents.js) & Cookie `ps_l` / `datr` — Quan trọng số 1 cho Meta Sigma
(Meta chỉ biết bạn là người thật khi vào các trang có nhúng mã `connect.facebook.net/en_US/fbevents.js` bắn sự kiện PageView kèm cookie `.facebook.com` / `.meta.com` về máy chủ Meta)
https://www.coursera.org — Nền tảng khóa học AI/Tech Mỹ (gắn Meta Pixel + Cloudflare + Google OAuth, cực khớp tệp người dùng Muse).
https://www.udemy.com — Khóa học lập trình/AI (bắn trực tiếp sự kiện Meta Pixel `fbevents.js` + cấp cookie Cloudflare sạch).
https://www.canva.com — Công cụ thiết kế (tích hợp sâu Meta Pixel, Facebook SDK và Cloudflare).
https://www.nytimes.com — Thời báo New York (gắn Meta Pixel + Fastly CDN hỗ trợ IPv6 tốc độ cao).
https://www.forbes.com — Tạp chí kinh doanh Mỹ (tích hợp Meta Pixel + truyền thông xã hội Mỹ).
https://www.eventbrite.com — Sự kiện tại Mỹ (nhúng trực tiếp Facebook Login SDK & Meta Pixel).
https://www.etsy.com — Sàn thương mại Mỹ (kích hoạt rất nhanh cookie `ps_l` / `ps_n` của Meta).
https://about.meta.com — Trang thông tin chính thức của Meta (làm tươi trực tiếp cookie `.meta.com` cùng miền với `auth.meta.com`).

Nhóm 2: Hệ sinh thái Google reCAPTCHA & Microsoft (Hỗ trợ chấm điểm `_GRECAPTCHA` trên `muse.ai` & Hotmail OAuth)
(Trang `muse.ai` nhúng trực tiếp Google reCAPTCHA v3 để chấm điểm bot ngầm; giữ ấm cookie Google & Microsoft giúp vượt Captcha và đăng nhập Hotmail mượt mà)
https://www.google.com — Tìm kiếm 1–2 từ khóa tiếng Anh (cấp cookie `NID` / `_GRECAPTCHA` cho reCAPTCHA v3 trên `muse.ai`).
https://www.youtube.com — Xem lướt 1 video ngắn (tăng độ tin cậy phiên Google trên trình duyệt).
https://news.google.com — Đọc lướt tiêu đề tin tức công nghệ Mỹ.
https://www.bing.com — Cổng tìm kiếm Microsoft (giữ ấm phiên đăng nhập Hotmail / Outlook)."""

_URL_LINE_RE = re.compile(
    r"^(?:[-*•]\s*)?(https?://[^\s—–\-|()]+)(?:\s*(?:—|–|-|\|)\s*(.*)|\s+\((.*)\))?$",
    re.IGNORECASE,
)


def _warmup_file_path(custom_path: pathlib.Path | None = None) -> pathlib.Path:
    if custom_path is not None:
        return custom_path
    return pathlib.Path(PROFILES_FILE).with_name("warmup_sites.json")


def load_warmup_text(custom_path: pathlib.Path | None = None) -> str:
    """Load saved warmup sites text from disk, or return DEFAULT_WARMUP_TEXT."""
    path = _warmup_file_path(custom_path)
    if path.exists():
        with contextlib.suppress(Exception):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("text"), str) and data["text"].strip():
                return data["text"].strip()
    return DEFAULT_WARMUP_TEXT


def save_warmup_text(text: str, custom_path: pathlib.Path | None = None) -> bool:
    """Persist warmup sites text to warmup_sites.json."""
    path = _warmup_file_path(custom_path)
    clean = (text or "").strip() or DEFAULT_WARMUP_TEXT
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"text": clean}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return False


def parse_warmup_groups(raw_text: str) -> list[dict[str, object]]:
    """Parse human-readable warmup text into groups of {'title', 'subtitle', 'items': [{'url', 'note'}]}."""
    groups: list[dict[str, object]] = []
    current_group: dict[str, object] | None = None

    for raw_line in (raw_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _URL_LINE_RE.match(line)
        if m:
            url = m.group(1).strip()
            note = (m.group(2) or m.group(3) or "").strip()
            if current_group is None:
                current_group = {
                    "title": "Web vào Meta không bị chặn",
                    "subtitle": "",
                    "items": [],
                }
                groups.append(current_group)
            items = current_group["items"]
            if isinstance(items, list):
                items.append({"url": url, "note": note})
        elif line.startswith("(") and line.endswith(")") and current_group is not None:
            current_group["subtitle"] = line
        else:
            current_group = {"title": line, "subtitle": "", "items": []}
            groups.append(current_group)

    return [g for g in groups if g.get("items")]


def pick_random_warmup_site(raw_text: str) -> dict[str, str] | None:
    """Pick one random warmup site dict {'url', 'note', 'group'} from parsed groups."""
    groups = parse_warmup_groups(raw_text)
    all_items: list[dict[str, str]] = []
    for g in groups:
        g_title = str(g.get("title") or "")
        for item in g.get("items", []):  # type: ignore[attr-defined]
            if isinstance(item, dict) and item.get("url"):
                all_items.append(
                    {
                        "url": str(item["url"]),
                        "note": str(item.get("note") or ""),
                        "group": g_title,
                    }
                )
    if not all_items:
        return None
    return random.choice(all_items)
