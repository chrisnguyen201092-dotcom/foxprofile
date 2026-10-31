"""Per-profile circular Avatar storage and Win32 GDI/GDI+ image processor.

Stores circular RGBA PNG avatars in `avatars/` (completely outside `camoufox_data/<Profile>/`
so browser profiles remain 100% isolated and untouched). Supports:
- Interactive screen region snip (`save_avatar_from_screen_rect`)
- Windows Clipboard image paste (`save_avatar_from_clipboard`)
- Image file import via Windows GDI+ (`save_avatar_from_file`)
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import pathlib
import struct
import sys
import zlib

AVATARS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "avatars")
)


def _profile_key(profile_name: str) -> str:
    return hashlib.sha1(profile_name.encode("utf-8")).hexdigest()[:16]


def get_avatar_path(profile_name: str, size: int = 22, *, avatars_dir: str | None = None) -> str | None:
    """Return path to the profile's circular avatar of `size` (22 or 64, `.gif` or `.png`), or None."""
    base = avatars_dir or AVATARS_DIR
    key = _profile_key(profile_name)
    if size > 24:
        gif_path = os.path.join(base, f"{key}_64.gif")
        if os.path.isfile(gif_path):
            return gif_path
    suffix = "_22.png" if size <= 24 else "_64.png"
    path = os.path.join(base, f"{key}{suffix}")
    if os.path.isfile(path):
        return path
    fallback = os.path.join(base, f"{key}_64.png")
    return fallback if os.path.isfile(fallback) else None


def avatar_version(profile_name: str, *, avatars_dir: str | None = None) -> float:
    """Return mtime of the profile's avatar (0.0 if none) for UI cache invalidation."""
    p = get_avatar_path(profile_name, 64, avatars_dir=avatars_dir) or get_avatar_path(
        profile_name, 22, avatars_dir=avatars_dir
    )
    if not p:
        return 0.0
    with contextlib.suppress(OSError):
        return os.path.getmtime(p)
    return 0.0


def remove_avatar(profile_name: str, *, avatars_dir: str | None = None) -> bool:
    """Delete any saved avatar files for `profile_name`."""
    base = avatars_dir or AVATARS_DIR
    key = _profile_key(profile_name)
    removed = False
    for suffix in ("_22.png", "_64.png", "_64.gif"):
        p = pathlib.Path(base) / f"{key}{suffix}"
        if p.is_file():
            with contextlib.suppress(OSError):
                p.unlink()
                removed = True
    return removed


def encode_rgba_png(width: int, height: int, rgba_bytes: bytes) -> bytes:
    """Encode raw RGBA bytes (row-major, top-to-bottom) as a standard PNG file."""
    stride = width * 4
    raw_rows = bytearray()
    for y in range(height):
        raw_rows.append(0)  # Filter type 0 (None)
        row_start = y * stride
        raw_rows.extend(rgba_bytes[row_start : row_start + stride])

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    compressed = zlib.compress(bytes(raw_rows), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


def apply_circular_mask_bgra_to_rgba(
    bgra: bytes,
    size: int,
    *,
    border_rgb: tuple[int, int, int] = (255, 255, 255),
) -> bytes:
    """Convert bottom-up or top-down BGRA square buffer to circular anti-aliased RGBA."""
    out = bytearray(size * size * 4)
    cx = (size - 1) / 2.0
    cy = (size - 1) / 2.0
    radius = (size / 2.0) - 0.6
    ring_inner = radius - (1.2 if size <= 28 else 1.8)
    br, bg, bb = border_rgb

    for y in range(size):
        for x in range(size):
            idx = (y * size + x) * 4
            b = bgra[idx]
            g = bgra[idx + 1]
            r = bgra[idx + 2]

            dist = math.hypot(x - cx, y - cy)
            if dist >= radius + 0.7:
                alpha = 0
                r = g = b = 0
            else:
                # Smooth anti-aliased outer edge
                edge_cov = max(0.0, min(1.0, (radius + 0.5) - dist))
                alpha = int(round(edge_cov * 255.0))
                # Subtle crisp ring near the outer boundary
                if dist >= ring_inner:
                    ring_t = max(0.0, min(1.0, (dist - ring_inner) / max(0.5, radius - ring_inner))) * 0.35
                    r = int(round(r * (1.0 - ring_t) + br * ring_t))
                    g = int(round(g * (1.0 - ring_t) + bg * ring_t))
                    b = int(round(b * (1.0 - ring_t) + bb * ring_t))

            out[idx] = r
            out[idx + 1] = g
            out[idx + 2] = b
            out[idx + 3] = alpha
    return bytes(out)


def _save_square_rgba_buffers(
    profile_name: str,
    rgba_22: bytes,
    rgba_64: bytes,
    *,
    avatars_dir: str | None = None,
) -> bool:
    base = pathlib.Path(avatars_dir or AVATARS_DIR)
    base.mkdir(parents=True, exist_ok=True)
    key = _profile_key(profile_name)
    old_gif = base / f"{key}_64.gif"
    if old_gif.is_file():
        with contextlib.suppress(OSError):
            old_gif.unlink()
    (base / f"{key}_22.png").write_bytes(encode_rgba_png(22, 22, rgba_22))
    (base / f"{key}_64.png").write_bytes(encode_rgba_png(64, 64, rgba_64))
    return True


def _scale_hbitmap_to_circular_avatars_win32(
    hbitmap: int,
    profile_name: str,
    *,
    avatars_dir: str | None = None,
) -> bool:
    """Center-crop a Win32 HBITMAP to a square, scale to 22x22 & 64x64, and save circular PNGs."""
    if sys.platform != "win32" or not hbitmap:
        return False
    import ctypes
    from ctypes import wintypes

    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    class BITMAP(ctypes.Structure):
        _fields_ = [
            ("bmType", wintypes.LONG),
            ("bmWidth", wintypes.LONG),
            ("bmHeight", wintypes.LONG),
            ("bmWidthBytes", wintypes.LONG),
            ("bmPlanes", wintypes.WORD),
            ("bmBitsPixel", wintypes.WORD),
            ("bmBits", ctypes.c_void_p),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    bmp = BITMAP()
    if gdi32.GetObjectW(wintypes.HANDLE(hbitmap), ctypes.sizeof(BITMAP), ctypes.byref(bmp)) == 0:
        return False

    src_w = int(bmp.bmWidth)
    src_h = abs(int(bmp.bmHeight))
    if src_w < 4 or src_h < 4:
        return False

    # Center-crop to square
    side = min(src_w, src_h)
    src_x = (src_w - side) // 2
    src_y = (src_h - side) // 2

    screen_dc = user32.GetDC(0)
    src_dc = gdi32.CreateCompatibleDC(screen_dc)
    old_src = gdi32.SelectObject(src_dc, wintypes.HANDLE(hbitmap))

    def _render_size(target_size: int) -> bytes | None:
        dst_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = target_size
        bmi.biHeight = -target_size  # top-down DIB
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB

        bits_ptr = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(
            dst_dc,
            ctypes.byref(bmi),
            0,
            ctypes.byref(bits_ptr),
            None,
            0,
        )
        if not dib or not bits_ptr.value:
            gdi32.DeleteDC(dst_dc)
            return None
        old_dst = gdi32.SelectObject(dst_dc, dib)
        try:
            halftone = 4
            srccopy = 0x00CC0020
            gdi32.SetStretchBltMode(dst_dc, halftone)
            gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
            gdi32.StretchBlt(
                dst_dc,
                0,
                0,
                target_size,
                target_size,
                src_dc,
                src_x,
                src_y,
                side,
                side,
                srccopy,
            )
            raw_bgra = ctypes.string_at(bits_ptr.value, target_size * target_size * 4)
            return apply_circular_mask_bgra_to_rgba(raw_bgra, target_size)
        finally:
            gdi32.SelectObject(dst_dc, old_dst)
            gdi32.DeleteObject(dib)
            gdi32.DeleteDC(dst_dc)

    try:
        rgba_22 = _render_size(22)
        rgba_64 = _render_size(64)
        if not rgba_22 or not rgba_64:
            return False
        return _save_square_rgba_buffers(profile_name, rgba_22, rgba_64, avatars_dir=avatars_dir)
    finally:
        gdi32.SelectObject(src_dc, old_src)
        gdi32.DeleteDC(src_dc)
        user32.ReleaseDC(0, screen_dc)


def save_avatar_from_screen_rect(
    profile_name: str,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    avatars_dir: str | None = None,
) -> bool:
    """Capture screen region `(x1, y1, x2, y2)` via Win32 GDI and save as circular avatar."""
    if sys.platform != "win32":
        return False
    left, right = min(x1, x2), max(x1, x2)
    top, bottom = min(y1, y2), max(y1, y2)
    w = right - left
    h = bottom - top
    if w < 8 or h < 8:
        return False
    try:
        import ctypes

        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        srccopy = 0x00CC0020
        captureblt = 0x40000000

        screen_dc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        hbmp = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
        old = gdi32.SelectObject(mem_dc, hbmp)
        try:
            gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, left, top, srccopy | captureblt)
        finally:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, screen_dc)

        try:
            return _scale_hbitmap_to_circular_avatars_win32(
                hbmp,
                profile_name,
                avatars_dir=avatars_dir,
            )
        finally:
            gdi32.DeleteObject(hbmp)
    except Exception:
        return False


def save_avatar_from_clipboard(
    profile_name: str,
    *,
    avatars_dir: str | None = None,
) -> bool:
    """Read an image from the Windows Clipboard (`CF_BITMAP`) and save as circular avatar."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        cf_bitmap = 2
        if not user32.IsClipboardFormatAvailable(cf_bitmap):
            return False
        if not user32.OpenClipboard(0):
            return False
        try:
            user32.GetClipboardData.restype = ctypes.c_void_p
            hbmp = user32.GetClipboardData(cf_bitmap)
            if not hbmp:
                return False
            return _scale_hbitmap_to_circular_avatars_win32(
                int(hbmp),
                profile_name,
                avatars_dir=avatars_dir,
            )
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


def save_avatar_from_file(
    profile_name: str,
    file_path: str,
    *,
    avatars_dir: str | None = None,
) -> bool:
    """Load any image file (`.png`, `.jpg`, `.bmp`, `.gif`) via Win32 GDI+ and save circular avatar."""
    if sys.platform != "win32" or not os.path.isfile(file_path):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        gdiplus = ctypes.windll.gdiplus
        gdi32 = ctypes.windll.gdi32

        class GdiplusStartupInput(ctypes.Structure):
            _fields_ = [
                ("GdiplusVersion", wintypes.UINT),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL),
                ("SuppressExternalCodecs", wintypes.BOOL),
            ]

        token = ctypes.c_ulonglong(0)
        startup_in = GdiplusStartupInput(1, None, False, False)
        if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup_in), None) != 0:
            return False

        gp_bitmap = ctypes.c_void_p(0)
        hbmp = ctypes.c_void_p(0)
        try:
            status = gdiplus.GdipCreateBitmapFromFile(
                ctypes.c_wchar_p(os.path.abspath(file_path)),
                ctypes.byref(gp_bitmap),
            )
            if status != 0 or not gp_bitmap.value:
                return False
            status = gdiplus.GdipCreateHBITMAPFromBitmap(
                gp_bitmap,
                ctypes.byref(hbmp),
                wintypes.DWORD(0xFF000000),
            )
            if status != 0 or not hbmp.value:
                return False
            ok = _scale_hbitmap_to_circular_avatars_win32(
                int(hbmp.value),
                profile_name,
                avatars_dir=avatars_dir,
            )
            if ok and file_path.lower().endswith(".gif"):
                base = pathlib.Path(avatars_dir or AVATARS_DIR)
                base.mkdir(parents=True, exist_ok=True)
                key = _profile_key(profile_name)
                with contextlib.suppress(OSError):
                    (base / f"{key}_64.gif").write_bytes(pathlib.Path(file_path).read_bytes())
            return ok
        finally:
            if hbmp.value:
                gdi32.DeleteObject(wintypes.HANDLE(hbmp.value))
            if gp_bitmap.value:
                gdiplus.GdipDisposeImage(gp_bitmap)
            gdiplus.GdiplusShutdown(token)
    except Exception:
        return False


def find_muse_connected_avatar_box_bgra(
    raw_bgra: bytes,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    """Detect the `⚡ Connected` green badge in a BGRA frame and return the circular avatar bbox `(x1, y1, x2, y2)`."""
    if width < 160 or height < 180 or len(raw_bgra) < width * height * 4:
        return None

    # Scan with stride=2 to locate the ~14x14 green `⚡ Connected` circle quickly
    visited: set[tuple[int, int]] = set()
    for sy in range(160, height - 10, 2):
        row_off = sy * width * 4
        for sx in range(20, width - 60, 2):
            idx = row_off + sx * 4
            b = raw_bgra[idx]
            g = raw_bgra[idx + 1]
            r = raw_bgra[idx + 2]
            if g > 120 and r < 60 and b < 60 and (sx, sy) not in visited:
                # Flood/window gather around (sx, sy) within +/- 18px
                gx_min = sx
                gx_max = sx
                gy_min = sy
                gy_max = sy
                count = 0
                for yy in range(max(0, sy - 16), min(height, sy + 18)):
                    r_off = yy * width * 4
                    for xx in range(max(0, sx - 16), min(width, sx + 18)):
                        ii = r_off + xx * 4
                        bb, gg, rr = raw_bgra[ii], raw_bgra[ii + 1], raw_bgra[ii + 2]
                        if gg > 120 and rr < 60 and bb < 60:
                            visited.add((xx, yy))
                            count += 1
                            if xx < gx_min:
                                gx_min = xx
                            if xx > gx_max:
                                gx_max = xx
                            if yy < gy_min:
                                gy_min = yy
                            if yy > gy_max:
                                gy_max = yy
                gw = gx_max - gx_min + 1
                gh = gy_max - gy_min + 1
                if 9 <= gw <= 26 and 9 <= gh <= 26 and 55 <= count <= 420:
                    scale = max(0.85, min(2.0, (gw + gh) / 28.0))
                    gx_c = (gx_min + gx_max) / 2.0
                    gy_c = (gy_min + gy_max) / 2.0
                    x1 = int(round(gx_c - 13.0 * scale))
                    y1 = int(round(gy_c - 174.0 * scale))
                    x2 = int(round(gx_c + 109.0 * scale))
                    y2 = int(round(gy_c - 52.0 * scale))
                    if x1 >= 0 and y1 >= 0 and x2 <= width and y2 <= height and (x2 - x1) >= 48:
                        # Verify avatar center has non-uniform content
                        cx = (x1 + x2) // 2
                        cy = (y1 + y2) // 2
                        c_idx = (cy * width + cx) * 4
                        cb, cg, cr = raw_bgra[c_idx], raw_bgra[c_idx + 1], raw_bgra[c_idx + 2]
                        if not (cr > 250 and cg > 250 and cb > 250):
                            return (x1, y1, x2, y2)
    return None


def auto_capture_muse_avatar_from_active_window(
    profile_name: str,
    *,
    avatars_dir: str | None = None,
) -> bool:
    """Passively inspect the foreground Camoufox window for the Muse `⚡ Connected` card and auto-save its avatar."""
    if sys.platform != "win32" or not profile_name:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        fg = user32.GetForegroundWindow()
        if not fg or user32.IsIconic(fg):
            return False

        length = user32.GetWindowTextLengthW(fg)
        if length <= 0:
            return False
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(fg, buf, length + 1)
        title = buf.value or ""
        prefix = f"[{profile_name}] "
        if not (title.startswith(prefix) or "Muse" in title or " — " in title):
            return False

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        rect = RECT()
        if not user32.GetWindowRect(fg, ctypes.byref(rect)):
            return False
        win_w = int(rect.right - rect.left)
        win_h = int(rect.bottom - rect.top)
        if win_w < 300 or win_h < 300:
            return False

        # Only scan the top 480px band of the browser window where the Muse card opens
        scan_x = int(rect.left) + 8
        scan_y = int(rect.top) + 70
        scan_w = min(win_w - 16, 1600)
        scan_h = min(win_h - 80, 440)
        if scan_w < 200 or scan_h < 200:
            return False

        srccopy = 0x00CC0020
        screen_dc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = scan_w
        bmi.biHeight = -scan_h
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        bits_ptr = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0)
        if not dib or not bits_ptr.value:
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, screen_dc)
            return False

        old = gdi32.SelectObject(mem_dc, dib)
        try:
            gdi32.BitBlt(mem_dc, 0, 0, scan_w, scan_h, screen_dc, scan_x, scan_y, srccopy)
            raw_bgra = ctypes.string_at(bits_ptr.value, scan_w * scan_h * 4)
        finally:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(dib)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, screen_dc)

        box = find_muse_connected_avatar_box_bgra(raw_bgra, scan_w, scan_h)
        if box is None:
            return False
        bx1, by1, bx2, by2 = box
        return save_avatar_from_screen_rect(
            profile_name,
            scan_x + bx1,
            scan_y + by1,
            scan_x + bx2,
            scan_y + by2,
            avatars_dir=avatars_dir,
        )
    except Exception:
        return False


def get_smooth_fox_logo_path(
    logo_src_path: str,
    size: int = 24,
    bg_rgb: tuple[int, int, int] = (23, 51, 46),
    *,
    avatars_dir: str | None = None,
) -> str | None:
    """Render `logo.png` downscaled with GDI+ HighQualityBicubic onto `bg_rgb` for crisp Tk rendering."""
    if sys.platform != "win32" or not os.path.isfile(logo_src_path):
        return None
    base = pathlib.Path(avatars_dir or AVATARS_DIR)
    base.mkdir(parents=True, exist_ok=True)
    r_bg, g_bg, b_bg = bg_rgb
    out_path = base / f"_fox_logo_hq_{size}_{r_bg:02x}{g_bg:02x}{b_bg:02x}.png"
    if out_path.is_file():
        return str(out_path)
    try:
        import ctypes
        from ctypes import wintypes

        gdiplus = ctypes.windll.gdiplus
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32

        class GdiplusStartupInput(ctypes.Structure):
            _fields_ = [
                ("GdiplusVersion", wintypes.UINT),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL),
                ("SuppressExternalCodecs", wintypes.BOOL),
            ]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        token = ctypes.c_ulonglong(0)
        startup_in = GdiplusStartupInput(1, None, False, False)
        if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup_in), None) != 0:
            return None

        gp_bitmap = ctypes.c_void_p(0)
        try:
            if (
                gdiplus.GdipCreateBitmapFromFile(
                    ctypes.c_wchar_p(os.path.abspath(logo_src_path)),
                    ctypes.byref(gp_bitmap),
                )
                != 0
                or not gp_bitmap.value
            ):
                return None

            w_u = wintypes.UINT(0)
            h_u = wintypes.UINT(0)
            gdiplus.GdipGetImageWidth(gp_bitmap, ctypes.byref(w_u))
            gdiplus.GdipGetImageHeight(gp_bitmap, ctypes.byref(h_u))
            src_w = max(1, int(w_u.value))
            src_h = max(1, int(h_u.value))

            screen_dc = user32.GetDC(0)
            dst_dc = gdi32.CreateCompatibleDC(screen_dc)

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = size
            bmi.biHeight = -size
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0

            bits_ptr = ctypes.c_void_p()
            dib = gdi32.CreateDIBSection(
                dst_dc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0
            )
            if not dib or not bits_ptr.value:
                gdi32.DeleteDC(dst_dc)
                user32.ReleaseDC(0, screen_dc)
                return None

            old_dst = gdi32.SelectObject(dst_dc, dib)
            graphics = ctypes.c_void_p(0)
            try:
                if gdiplus.GdipCreateFromHDC(dst_dc, ctypes.byref(graphics)) != 0:
                    return None
                argb_bg = (0xFF << 24) | (r_bg << 16) | (g_bg << 8) | b_bg
                gdiplus.GdipGraphicsClear(graphics, wintypes.DWORD(argb_bg))
                # InterpolationModeHighQualityBicubic = 7, SmoothingModeAntiAlias = 4, PixelOffsetModeHalf = 4
                gdiplus.GdipSetInterpolationMode(graphics, 7)
                gdiplus.GdipSetSmoothingMode(graphics, 4)
                gdiplus.GdipSetPixelOffsetMode(graphics, 4)
                # Crop 10% outer transparent margin of logo.png so the fox head fills the icon nicely
                crop_x = int(src_w * 0.10)
                crop_y = int(src_h * 0.08)
                crop_w = int(src_w * 0.80)
                crop_h = int(src_h * 0.84)
                gdiplus.GdipDrawImageRectRectI(
                    graphics,
                    gp_bitmap,
                    0,
                    0,
                    size,
                    size,
                    crop_x,
                    crop_y,
                    crop_w,
                    crop_h,
                    2,  # UnitPixel = 2
                    None,
                    None,
                    None,
                )
                gdiplus.GdipFlush(graphics, 1)
                raw_bgra = ctypes.string_at(bits_ptr.value, size * size * 4)
            finally:
                if graphics.value:
                    gdiplus.GdipDeleteGraphics(graphics)
                gdi32.SelectObject(dst_dc, old_dst)
                gdi32.DeleteObject(dib)
                gdi32.DeleteDC(dst_dc)
                user32.ReleaseDC(0, screen_dc)

            rgba = bytearray(size * size * 4)
            for idx in range(0, size * size * 4, 4):
                rgba[idx] = raw_bgra[idx + 2]
                rgba[idx + 1] = raw_bgra[idx + 1]
                rgba[idx + 2] = raw_bgra[idx]
                rgba[idx + 3] = 255
            out_path.write_bytes(encode_rgba_png(size, size, bytes(rgba)))
            return str(out_path)
        finally:
            if gp_bitmap.value:
                gdiplus.GdipDisposeImage(gp_bitmap)
            gdiplus.GdiplusShutdown(token)
    except Exception:
        return None
