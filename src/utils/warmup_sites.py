import contextlib
import json
import pathlib
import random
import re

from ..core.config import PROFILES_FILE

DEFAULT_WARMUP_TEXT = """Web vào Meta không bị chặn
https://muse.ai/?aymh_complete=1

Nhóm 1: Nuôi trực tiếp điểm Google reCAPTCHA (_GRECAPTCHA) — Quan trọng nhất
(Vì cổng bảo vệ của muse.ai và auth.meta.com dùng Google reCAPTCHA để chấm điểm Bot hay Người thật)
https://www.google.com — Tìm kiếm 1–2 từ khóa tiếng Anh bất kỳ (ví dụ thời tiết, tin tức công nghệ, du lịch Mỹ).
https://news.google.com — Đọc lướt tiêu đề tin tức Mỹ, cuộn trang tự nhiên.
https://www.youtube.com — Bật 1 video ngắn (2–3 phút) chạy ở tab bên cạnh trong lúc chờ muse.ai trả lời.
https://www.bing.com — Cổng tìm kiếm của Microsoft (rất tốt cho dàn acc đăng nhập bằng Hotmail / Outlook).

Nhóm 2: Báo chí & Công nghệ Mỹ (Tạo lịch sử duyệt web places.sqlite + Cookie CDN sạch)
(Các trang này chạy hạ tầng Fastly / Cloudflare / Akamai hỗ trợ IPv6 toàn phần, tải chữ cực nhẹ, rất hợp để chuyển tab qua lại tạo sự kiện focus/blur khi làm việc với Muse)
https://en.wikipedia.org — Siêu nhẹ, tải tức thì trên IPv6, bấm đọc 1–2 bài viết tiếng Anh.
https://www.cnn.com — Trang tin tức số 1 tại Mỹ, hỗ trợ IPv6 hoàn hảo.
https://www.npr.org — Đài phát thanh quốc gia Mỹ, giao diện cực nhẹ, không quảng cáo nặng máy.
https://techcrunch.com — Trang tin công nghệ & AI (rất khớp với hành vi của người dùng muse.ai).
https://www.wired.com — Tạp chí công nghệ Mỹ, tải mượt 100% qua IPv6.
https://www.theguardian.com — Báo tiếng Anh lớn, hỗ trợ IPv6 toàn bộ ảnh và bài viết.

Nhóm 3: Gu Tài chính & Kinh tế Mỹ (Tải nhanh, nhiều chữ — đã test 200 OK 100% IPv6)
https://www.cnbc.com — Đài tài chính số 1 Mỹ – IPv6 rất mượt.
https://finance.yahoo.com — Xem bảng giá chứng khoán Mỹ.
https://www.investopedia.com — Tra cứu thuật ngữ tài chính / đầu tư.

Nhóm 4: Gu Kỹ thuật & Tài liệu (Rất hợp khi dùng Muse để hỏi về code / data)
https://developer.mozilla.org — MDN Web Docs – siêu nhẹ, hỗ trợ IPv6 100%.
https://docs.python.org — Tài liệu Python chính thức.
https://www.cloudflare.com — Giúp lấy trực tiếp cookie tin cậy của hệ sinh thái Cloudflare.

Nhóm 5: Gu Thể thao & Đời sống / Khoa học Mỹ (Độ uy tín tên miền tuyệt đối)
https://www.espn.com — Trang thể thao lớn nhất nước Mỹ.
https://www.nasa.gov — Trang khoa học / vũ trụ chính phủ Mỹ, hỗ trợ IPv6 chuẩn 100%.
https://www.nih.gov — Viện Y tế Quốc gia Mỹ (.gov), hỗ trợ IPv6 chuẩn 100%, độ uy tín tuyệt đối."""

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
