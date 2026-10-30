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
    """Return path to the profile's circular PNG avatar of `size` (22 or 64), or None."""
    base = avatars_dir or AVATARS_DIR
    key = _profile_key(profile_name)
    suffix = "_22.png" if size <= 24 else "_64.png"
    path = os.path.join(base, f"{key}{suffix}")
    if os.path.isfile(path):
        return path
    fallback = os.path.join(base, f"{key}_64.png")
    return fallback if os.path.isfile(fallback) else None


def avatar_version(profile_name: str, *, avatars_dir: str | None = None) -> float:
    """Return mtime of the profile's avatar (0.0 if none) for UI cache invalidation."""
    p = get_avatar_path(profile_name, 22, avatars_dir=avatars_dir)
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
    for suffix in ("_22.png", "_64.png"):
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
            return _scale_hbitmap_to_circular_avatars_win32(
                int(hbmp.value),
                profile_name,
                avatars_dir=avatars_dir,
            )
        finally:
            if hbmp.value:
                gdi32.DeleteObject(wintypes.HANDLE(hbmp.value))
            if gp_bitmap.value:
                gdiplus.GdipDisposeImage(gp_bitmap)
            gdiplus.GdiplusShutdown(token)
    except Exception:
        return False
