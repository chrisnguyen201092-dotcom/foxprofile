import contextlib
import json
import pathlib
import random
import re

from ..core.config import PROFILES_FILE

DEFAULT_WARMUP_TEXT = """Nhóm 1: Hệ sinh thái Meta & Muse Chính thức (Tín hiệu First-Party — Ưu tiên số 1)
(Camoufox cô lập cookie giữa các trang bằng Total Cookie Protection; chỉ các trang thuộc hệ Meta/Muse dưới đây mới đồng bộ trực tiếp cookie `datr`, `ps_l`, `layout_state` mà Meta Sigma kiểm tra. Khi đóng trình duyệt, hãy để tab tại hội thoại thật `/thread/<id>`, không để ở `/thread/new`)
https://muse.ai/?aymh_complete=1 — Đường dẫn truy cập trực tiếp Muse (bỏ qua vòng lặp chuyển hướng /aymh/redirect-cycle/).
https://www.facebook.com — Duyệt 1–2 phút để làm mới trực tiếp cookie gốc `.facebook.com` (`datr`, `fr`, `ps_l`, `wd`).
https://www.instagram.com — Duyệt giữ phiên Instagram liên kết trong Meta Accounts Center (`datr`, `ig_did`, `ps_l`).
https://accountscenter.meta.com — Kiểm tra đồng bộ tài khoản Meta Accounts Center (`.accountscenter.meta.com`).

Nhóm 2: Báo chí & Chuyên trang Mỹ Chuẩn IPv6 `AAAA` (Tùy chọn — Phản hồi nhanh <2.5s, không phát sinh Captcha)
(Tùy chọn — Dùng khi cần mở kèm 1–2 tab nhẹ để kiểm tra kết nối hoặc duy trì lưu lượng Proxy IPv6 tự nhiên)
https://www.cnet.com — Chuyên trang công nghệ Mỹ (IPv6 1.9s, đã kiểm chứng trên profile Kim Cương).
https://www.cntraveler.com — Tạp chí du lịch Mỹ (IPv6 2.5s, tải mượt và ổn định qua proxy).
https://www.self.com — Tạp chí đời sống Mỹ (IPv6 2.7s, phản hồi nhanh trên IPv6 thuần).
https://www.bonappetit.com — Chuyên trang ẩm thực Mỹ (IPv6 2.2s, dung lượng nhẹ).
https://www.wired.com — Tạp chí AI & Công nghệ Mỹ (IPv6 2.3s, tải sạch không phát sinh Captcha).
https://www.pcmag.com — Tạp chí phần cứng & PC Mỹ (IPv6 1.8s, phản hồi rất nhanh).
https://www.lifehacker.com — Chuyên trang phần mềm & giải pháp số Mỹ (IPv6 3.0s, tương thích tốt IPv6).
https://techcrunch.com — Tin tức Công nghệ & AI Mỹ (IPv6 2.7s, nhẹ và ổn định).
https://www.gq.com — Tạp chí phong cách Mỹ (IPv6 2.5s, phản hồi nhanh).
https://www.vanityfair.com — Tạp chí văn hóa Mỹ (IPv6 2.1s, không phát sinh Captcha).
https://www.espn.com — Tin tức thể thao Mỹ (IPv6 1.9s, cấp cookie vùng `edition` US).
https://www.npr.org — Đài phát thanh quốc gia Mỹ (IPv6 2.1s, giao diện tối giản).

Nhóm 3: Hệ sinh thái Google (Tùy chọn — Chuẩn IPv6 `AAAA`, kiểm tra kết nối & tìm kiếm)
(Đã loại trừ YouTube do hạn chế băng thông từ nhà mạng proxy và loại trừ hệ thống Microsoft/Bing)
https://www.google.com — Tìm kiếm 1–2 từ khóa tiếng Anh (IPv6 1.7s, cấp cookie `NID` / `AEC` / `UULE`).
https://news.google.com — Đọc lướt tiêu đề tin tức công nghệ Mỹ trên Google News (IPv6)."""

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
