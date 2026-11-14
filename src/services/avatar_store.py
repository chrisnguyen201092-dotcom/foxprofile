"""Per-profile circular Avatar storage and Win32 GDI/GDI+ image processor.

Stores circular RGBA PNG avatars in `avatars/` (completely outside `camoufox_data/<Profile>/`
so browser profiles remain 100% isolated and untouched). Supports:
- Interactive screen region snip (`save_avatar_from_screen_rect`)
- Windows Clipboard image paste (`save_avatar_from_clipboard`: `CF_HDROP`, `PNG`, `CF_DIB`, `CF_BITMAP`)
- Image file import via Windows GDI+ (`save_avatar_from_file`)
- Passive foreground Muse `⚡ Connected` card avatar detector (`auto_capture_muse_avatar_from_active_window`)
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import pathlib
import struct
import sys
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from typing import NamedTuple, TypeVar

_T = TypeVar("_T")

AVATARS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "avatars")
)

# Short-lived directory index cache so rendering 100+ profile cards in a single UI frame
# performs 1 `os.scandir(avatars)` instead of 100+ repeated directory scans on HDD.
# Maps `abs_base_dir -> (mono_ts, dir_mtime_ns, map_22, map_64_best, mtime_22, mtime_64)`.
_AVATAR_DIR_CACHE: dict[
    str,
    tuple[float, int, dict[str, str], dict[str, str], dict[str, float]],
] = {}


def _invalidate_avatar_cache(avatars_dir: str | None = None) -> None:
    if avatars_dir is None:
        _AVATAR_DIR_CACHE.clear()
    else:
        _AVATAR_DIR_CACHE.pop(os.path.abspath(avatars_dir), None)


def _get_avatar_index(
    base: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, float]] | None:
    abs_base = os.path.abspath(base)
    now_mono = time.monotonic()
    cached = _AVATAR_DIR_CACHE.get(abs_base)
    # Within the same UI render burst (< 1.0s) on the default AVATARS_DIR, reuse without statting
    if cached is not None and abs_base == AVATARS_DIR and (now_mono - cached[0]) < 1.0:
        return cached[2], cached[3], cached[4]
    try:
        dir_mtime_ns = os.stat(abs_base).st_mtime_ns
    except OSError:
        _AVATAR_DIR_CACHE.pop(abs_base, None)
        return None
    if (
        cached is not None
        and cached[1] == dir_mtime_ns
        and (now_mono - cached[0]) < 5.0
    ):
        return cached[2], cached[3], cached[4]

    map_22: dict[str, str] = {}
    map_64_plain: dict[str, str] = {}
    map_64_ver: dict[str, tuple[float, str]] = {}
    mtime_map: dict[str, float] = {}
    with contextlib.suppress(OSError), os.scandir(abs_base) as it:
        for entry in it:
            name = entry.name
            if not name.endswith(".png") or len(name) < 23 or not entry.is_file():
                continue
            key = name[:16]
            rest = name[16:-4]  # e.g. "_22", "_64", or "_64_<ms>"
            try:
                mt = entry.stat().st_mtime
            except OSError:
                continue
            if rest == "_22":
                map_22[key] = entry.path
                mtime_map[entry.path] = mt
            elif rest == "_64":
                map_64_plain[key] = entry.path
                mtime_map[entry.path] = mt
            elif rest.startswith("_64_"):
                prev = map_64_ver.get(key)
                if prev is None or mt > prev[0]:
                    map_64_ver[key] = (mt, entry.path)
                mtime_map[entry.path] = mt

    map_64_best: dict[str, str] = dict(map_64_plain)
    for k, (_mt, ver_path) in map_64_ver.items():
        map_64_best[k] = ver_path

    _AVATAR_DIR_CACHE[abs_base] = (now_mono, dir_mtime_ns, map_22, map_64_best, mtime_map)
    return map_22, map_64_best, mtime_map


def _profile_key(profile_name: str) -> str:
    return hashlib.sha1(profile_name.encode("utf-8")).hexdigest()[:16]


def get_avatar_path(profile_name: str, size: int = 22, *, avatars_dir: str | None = None) -> str | None:
    """Return path to the profile's circular PNG avatar of `size` (22 or 64), or None."""
    base = avatars_dir or AVATARS_DIR
    idx = _get_avatar_index(base)
    if idx is None:
        return None
    map_22, map_64_best, _ = idx
    key = _profile_key(profile_name)
    if size <= 24:
        return map_22.get(key) or map_64_best.get(key)
    return map_64_best.get(key) or map_22.get(key)


def avatar_version(profile_name: str, *, avatars_dir: str | None = None) -> float:
    """Return mtime of the profile's avatar (0.0 if none) for UI cache invalidation."""
    base = avatars_dir or AVATARS_DIR
    idx = _get_avatar_index(base)
    if idx is None:
        return 0.0
    map_22, map_64_best, mtime_map = idx
    key = _profile_key(profile_name)
    p = map_22.get(key) or map_64_best.get(key)
    if not p:
        return 0.0
    if p in mtime_map:
        return mtime_map[p]
    with contextlib.suppress(OSError):
        return os.path.getmtime(p)
    return 0.0


def remove_avatar(profile_name: str, *, avatars_dir: str | None = None) -> bool:
    """Delete any saved avatar files for `profile_name`."""
    base = pathlib.Path(avatars_dir or AVATARS_DIR)
    if not base.is_dir():
        return False
    key = _profile_key(profile_name)
    removed = False
    for p in list(base.glob(f"{key}_*")):
        if p.is_file():
            with contextlib.suppress(OSError):
                p.unlink()
                removed = True
    if removed:
        _invalidate_avatar_cache(avatars_dir)
    return removed


def rename_avatar(
    old_name: str,
    new_name: str,
    *,
    avatars_dir: str | None = None,
) -> bool:
    """Migrate saved circular avatar files when a profile is renamed."""
    if not old_name or not new_name or old_name == new_name:
        return False
    old_key = _profile_key(old_name)
    new_key = _profile_key(new_name)
    if old_key == new_key:
        return False
    base = pathlib.Path(avatars_dir or AVATARS_DIR)
    if not base.is_dir():
        return False
    old_files = [p for p in base.glob(f"{old_key}_*.png") if p.is_file()]
    if not old_files:
        return False

    for stale in list(base.glob(f"{new_key}_*.png")):
        if stale.is_file():
            with contextlib.suppress(OSError):
                stale.unlink()

    moved = False
    for src_p in old_files:
        suffix = src_p.name[len(old_key) :]
        dst_p = base / f"{new_key}{suffix}"
        with contextlib.suppress(OSError):
            os.replace(src_p, dst_p)
            with contextlib.suppress(OSError):
                os.utime(dst_p, None)
            moved = True

    if moved:
        _invalidate_avatar_cache(avatars_dir)
    return moved


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
    """Convert top-down BGRA square buffer to circular anti-aliased RGBA."""
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
                    ring_t = (
                        max(0.0, min(1.0, (dist - ring_inner) / max(0.5, radius - ring_inner)))
                        * 0.35
                    )
                    r = int(round(r * (1.0 - ring_t) + br * ring_t))
                    g = int(round(g * (1.0 - ring_t) + bg * ring_t))
                    b = int(round(b * (1.0 - ring_t) + bb * ring_t))

            out[idx] = r
            out[idx + 1] = g
            out[idx + 2] = b
            out[idx + 3] = alpha
    return bytes(out)


def _is_valid_avatar_rgba(
    rgba_64: bytes,
    *,
    strict_variance: bool = False,
    reject_uniform: bool = False,
) -> bool:
    """Verify a 64x64 circular RGBA buffer is not a failed all-black or blank uniform capture."""
    if len(rgba_64) != 64 * 64 * 4:
        return False

    cx = 31.5
    cy = 31.5
    inner_r = 25.0  # Exclude the outer white border ring

    total_inner = 0
    non_black = 0
    non_white = 0
    r_min, g_min, b_min = 255, 255, 255
    r_max, g_max, b_max = 0, 0, 0
    r_sum, g_sum, b_sum = 0, 0, 0
    samples: list[tuple[int, int, int]] = []

    for y in range(6, 58, 2):
        for x in range(6, 58, 2):
            if math.hypot(x - cx, y - cy) <= inner_r:
                idx = (y * 64 + x) * 4
                r = rgba_64[idx]
                g = rgba_64[idx + 1]
                b = rgba_64[idx + 2]
                a = rgba_64[idx + 3]
                if a < 200:
                    continue
                total_inner += 1
                samples.append((r, g, b))
                r_sum += r
                g_sum += g
                b_sum += b
                if r > 8 or g > 8 or b > 8:
                    non_black += 1
                if r < 244 or g < 244 or b < 244:
                    non_white += 1
                if r < r_min:
                    r_min = r
                if g < g_min:
                    g_min = g
                if b < b_min:
                    b_min = b
                if r > r_max:
                    r_max = r
                if g > g_max:
                    g_max = g
                if b > b_max:
                    b_max = b

    if total_inner < 50:
        return False
    # Reject completely black buffers (e.g. failed GDI BitBlt / uninitialized DIB)
    if non_black == 0:
        return False

    if strict_variance:
        # For passive auto-capture, reject blank white/gray/black placeholders
        if non_white < max(12, total_inner // 10):
            return False
        if non_black < max(12, total_inner // 10):
            return False
        dyn_range = max(r_max - r_min, g_max - g_min, b_max - b_min)
        if dyn_range < 32:
            return False
        mean_r = r_sum / total_inner
        mean_g = g_sum / total_inner
        mean_b = b_sum / total_inner
        varied = sum(
            1
            for (r, g, b) in samples
            if (abs(r - mean_r) + abs(g - mean_g) + abs(b - mean_b)) >= 28.0
        )
        if varied < max(10, total_inner // 10):
            return False
    elif reject_uniform:
        # For manual screen snips, reject flat blank-background or single-color overlay crops
        dyn_range = max(r_max - r_min, g_max - g_min, b_max - b_min)
        if dyn_range < 16:
            return False
        mean_r = r_sum / total_inner
        mean_g = g_sum / total_inner
        mean_b = b_sum / total_inner
        varied = sum(
            1
            for (r, g, b) in samples
            if (abs(r - mean_r) + abs(g - mean_g) + abs(b - mean_b)) >= 18.0
        )
        if varied < 6:
            return False

    return True


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
    png_22 = encode_rgba_png(22, 22, rgba_22)
    png_64 = encode_rgba_png(64, 64, rgba_64)

    for old_ver in list(base.glob(f"{key}_64_*.png")):
        with contextlib.suppress(OSError):
            old_ver.unlink()

    (base / f"{key}_22.png").write_bytes(png_22)
    (base / f"{key}_64.png").write_bytes(png_64)
    ver_ms = int(time.time() * 1000)
    (base / f"{key}_64_{ver_ms}.png").write_bytes(png_64)
    _invalidate_avatar_cache(avatars_dir)
    return True


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _BITMAP(ctypes.Structure):
        _fields_ = [
            ("bmType", wintypes.LONG),
            ("bmWidth", wintypes.LONG),
            ("bmHeight", wintypes.LONG),
            ("bmWidthBytes", wintypes.LONG),
            ("bmPlanes", wintypes.WORD),
            ("bmBitsPixel", wintypes.WORD),
            ("bmBits", ctypes.c_void_p),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
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

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class _GdiplusStartupInput(ctypes.Structure):
        _fields_ = [
            ("GdiplusVersion", wintypes.UINT),
            ("DebugEventCallback", ctypes.c_void_p),
            ("SuppressBackgroundThread", wintypes.BOOL),
            ("SuppressExternalCodecs", wintypes.BOOL),
        ]


def _win32_apis():
    """Return `(user32, gdi32, kernel32, shell32, gdiplus)` with 64-bit-safe prototypes."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32
    shell32 = ctypes.windll.shell32
    gdiplus = ctypes.windll.gdiplus

    if not getattr(_win32_apis, "_configured", False):
        # user32
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.GetDC.restype = ctypes.c_void_p
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClipboardFormatW.restype = wintypes.UINT
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        if hasattr(user32, "SetThreadDpiAwarenessContext"):
            user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_ssize_t]
            user32.SetThreadDpiAwarenessContext.restype = ctypes.c_ssize_t

        # gdi32 (64-bit HANDLE / HGDIOBJ / HDC safety)
        gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gdi32.GetDeviceCaps.restype = ctypes.c_int
        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
        gdi32.CreateDIBSection.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        gdi32.CreateDIBSection.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.BitBlt.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.StretchBlt.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.StretchBlt.restype = wintypes.BOOL
        gdi32.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gdi32.SetStretchBltMode.restype = ctypes.c_int
        gdi32.SetBrushOrgEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        gdi32.SetBrushOrgEx.restype = wintypes.BOOL
        gdi32.StretchDIBits.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
            wintypes.DWORD,
        ]
        gdi32.StretchDIBits.restype = ctypes.c_int

        # kernel32 (Clipboard HGLOBAL)
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t

        # shell32 (CF_HDROP file copy)
        shell32.DragQueryFileW.argtypes = [
            ctypes.c_void_p,
            wintypes.UINT,
            wintypes.LPWSTR,
            wintypes.UINT,
        ]
        shell32.DragQueryFileW.restype = wintypes.UINT

        # gdiplus
        gdiplus.GdiplusStartup.argtypes = [
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        gdiplus.GdiplusStartup.restype = ctypes.c_int
        gdiplus.GdiplusShutdown.argtypes = [ctypes.c_ulonglong]
        gdiplus.GdiplusShutdown.restype = None
        gdiplus.GdipCreateBitmapFromFile.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        gdiplus.GdipCreateBitmapFromFile.restype = ctypes.c_int
        gdiplus.GdipCreateBitmapFromHBITMAP.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        gdiplus.GdipCreateBitmapFromHBITMAP.restype = ctypes.c_int
        gdiplus.GdipCreateHBITMAPFromBitmap.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        ]
        gdiplus.GdipCreateHBITMAPFromBitmap.restype = ctypes.c_int
        gdiplus.GdipGetImageWidth.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.UINT),
        ]
        gdiplus.GdipGetImageWidth.restype = ctypes.c_int
        gdiplus.GdipGetImageHeight.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.UINT),
        ]
        gdiplus.GdipGetImageHeight.restype = ctypes.c_int
        gdiplus.GdipCreateFromHDC.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        gdiplus.GdipCreateFromHDC.restype = ctypes.c_int
        gdiplus.GdipGraphicsClear.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        gdiplus.GdipGraphicsClear.restype = ctypes.c_int
        gdiplus.GdipSetInterpolationMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gdiplus.GdipSetInterpolationMode.restype = ctypes.c_int
        gdiplus.GdipSetSmoothingMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gdiplus.GdipSetSmoothingMode.restype = ctypes.c_int
        gdiplus.GdipSetPixelOffsetMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gdiplus.GdipSetPixelOffsetMode.restype = ctypes.c_int
        gdiplus.GdipDrawImageRectRectI.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        gdiplus.GdipDrawImageRectRectI.restype = ctypes.c_int
        gdiplus.GdipFlush.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gdiplus.GdipFlush.restype = ctypes.c_int
        gdiplus.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
        gdiplus.GdipDeleteGraphics.restype = ctypes.c_int
        gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        gdiplus.GdipDisposeImage.restype = ctypes.c_int

        _win32_apis._configured = True  # type: ignore[attr-defined]
    return user32, gdi32, kernel32, shell32, gdiplus


@contextlib.contextmanager
def _per_monitor_dpi_context() -> Iterator[bool]:
    """Temporarily switch ONLY the calling thread to Per-Monitor DPI Aware v2."""
    if sys.platform != "win32":
        yield False
        return
    user32, _gdi32, _k32, _sh32, _gp = _win32_apis()
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    if setter is None:
        yield False
        return
    prev = 0
    for ctx in (-4, -3, -2):  # PER_MONITOR_AWARE_V2, PER_MONITOR_AWARE, SYSTEM_AWARE
        with contextlib.suppress(Exception):
            res = int(setter(ctx))
            if res:
                prev = res
                break
    try:
        yield bool(prev)
    finally:
        if prev:
            with contextlib.suppress(Exception):
                setter(prev)


_DPI_WORKER_STATE = threading.local()


def _run_in_dpi_worker(fn: Callable[[], _T]) -> _T:
    """Execute DPI-switching Win32 work on an isolated worker thread so GUI threads keep their native DPI context."""
    if sys.platform != "win32" or getattr(_DPI_WORKER_STATE, "active", False):
        return fn()

    box: list[object] = [None, None]

    def _worker() -> None:
        _DPI_WORKER_STATE.active = True
        try:
            box[0] = fn()
        except BaseException as exc:
            box[1] = exc
        finally:
            _DPI_WORKER_STATE.active = False

    worker = threading.Thread(target=_worker, name="FoxAvatarDpiWorker", daemon=True)
    worker.start()
    worker.join()
    if box[1] is not None:
        raise box[1]  # type: ignore[misc]
    return box[0]  # type: ignore[return-value]


def _read_virtual_screen_metrics_raw(user32: object) -> tuple[int, int, int, int]:
    vx = int(user32.GetSystemMetrics(76))  # type: ignore[attr-defined]
    vy = int(user32.GetSystemMetrics(77))  # type: ignore[attr-defined]
    vw = int(user32.GetSystemMetrics(78))  # type: ignore[attr-defined]
    vh = int(user32.GetSystemMetrics(79))  # type: ignore[attr-defined]
    if vw <= 0 or vh <= 0:
        vx, vy = 0, 0
        vw = max(1, int(user32.GetSystemMetrics(0)))  # type: ignore[attr-defined]
        vh = max(1, int(user32.GetSystemMetrics(1)))  # type: ignore[attr-defined]
    return (vx, vy, vw, vh)


def get_virtual_screen_metrics(*, physical: bool = False) -> tuple[int, int, int, int]:
    """Return `(x, y, width, height)` of the Windows virtual desktop in logical or physical px."""
    if sys.platform != "win32":
        return (0, 0, 1920, 1080)
    user32, gdi32, _k32, _sh32, _gp = _win32_apis()
    if not physical:
        return _read_virtual_screen_metrics_raw(user32)

    def _read_physical() -> tuple[int, int, int, int]:
        with _per_monitor_dpi_context() as switched:
            vx, vy, vw, vh = _read_virtual_screen_metrics_raw(user32)
            if not switched:
                hdc = user32.GetDC(None)
                if hdc:
                    try:
                        log_w = max(1, int(gdi32.GetDeviceCaps(hdc, 8)))  # HORZRES
                        log_h = max(1, int(gdi32.GetDeviceCaps(hdc, 10)))  # VERTRES
                        phys_w = int(gdi32.GetDeviceCaps(hdc, 118))  # DESKTOPHORZRES
                        phys_h = int(gdi32.GetDeviceCaps(hdc, 117))  # DESKTOPVERTRES
                        if phys_w > log_w or phys_h > log_h:
                            sx = phys_w / float(log_w)
                            sy = phys_h / float(log_h)
                            vx = int(round(vx * sx))
                            vy = int(round(vy * sy))
                            vw = max(1, int(round(vw * sx)))
                            vh = max(1, int(round(vh * sy)))
                    finally:
                        user32.ReleaseDC(None, hdc)
            return (vx, vy, vw, vh)

    return _run_in_dpi_worker(_read_physical)


def logical_rect_to_physical_rect(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    logical_bounds: tuple[int, int, int, int] | None = None,
    physical_bounds: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    """Convert virtual-desktop logical coordinates `(x1, y1, x2, y2)` to physical device pixels."""
    lvx, lvy, lvw, lvh = (
        logical_bounds
        if logical_bounds is not None
        else get_virtual_screen_metrics(physical=False)
    )
    pvx, pvy, pvw, pvh = (
        physical_bounds
        if physical_bounds is not None
        else get_virtual_screen_metrics(physical=True)
    )
    if lvw <= 0 or lvh <= 0 or pvw <= 0 or pvh <= 0 or (lvx, lvy, lvw, lvh) == (pvx, pvy, pvw, pvh):
        return (x1, y1, x2, y2)
    sx = pvw / float(lvw)
    sy = pvh / float(lvh)
    return (
        pvx + int(round((x1 - lvx) * sx)),
        pvy + int(round((y1 - lvy) * sy)),
        pvx + int(round((x2 - lvx) * sx)),
        pvy + int(round((y2 - lvy) * sy)),
    )


def _make_topdown_bgra_bmi(width: int, height: int) -> _BITMAPINFOHEADER:
    import ctypes

    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth = width
    bmi.biHeight = -height  # top-down 32-bit DIB
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    return bmi


def _render_gp_bitmap_to_square_rgba(
    gp_bitmap: int,
    target_size: int,
) -> bytes | None:
    """Downscale a GDI+ `GpBitmap*` (center-cropped square) to `target_size` circular RGBA."""
    if sys.platform != "win32" or not gp_bitmap:
        return None
    import ctypes
    from ctypes import wintypes

    user32, gdi32, _k32, _sh32, gdiplus = _win32_apis()
    gp_ptr = ctypes.c_void_p(gp_bitmap)
    w_u = wintypes.UINT(0)
    h_u = wintypes.UINT(0)
    if (
        gdiplus.GdipGetImageWidth(gp_ptr, ctypes.byref(w_u)) != 0
        or gdiplus.GdipGetImageHeight(gp_ptr, ctypes.byref(h_u)) != 0
    ):
        return None
    src_w = int(w_u.value)
    src_h = int(h_u.value)
    if src_w < 4 or src_h < 4:
        return None

    side = min(src_w, src_h)
    src_x = (src_w - side) // 2
    src_y = (src_h - side) // 2

    screen_dc = user32.GetDC(None)
    dst_dc = gdi32.CreateCompatibleDC(screen_dc)
    if not dst_dc:
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)
        return None

    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth = target_size
    bmi.biHeight = -target_size  # top-down DIB
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0

    bits_ptr = ctypes.c_void_p()
    dib = gdi32.CreateDIBSection(
        dst_dc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0
    )
    if not dib or not bits_ptr.value:
        gdi32.DeleteDC(dst_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)
        return None

    old_dst = gdi32.SelectObject(dst_dc, dib)
    graphics = ctypes.c_void_p(0)
    try:
        if gdiplus.GdipCreateFromHDC(dst_dc, ctypes.byref(graphics)) != 0 or not graphics.value:
            return None
        # Dark neutral backdrop for any transparent PNG pixels before circular mask
        gdiplus.GdipGraphicsClear(graphics, wintypes.DWORD(0xFF17332E))
        gdiplus.GdipSetInterpolationMode(graphics, 7)  # HighQualityBicubic
        gdiplus.GdipSetSmoothingMode(graphics, 4)  # AntiAlias
        gdiplus.GdipSetPixelOffsetMode(graphics, 4)  # Half
        status = gdiplus.GdipDrawImageRectRectI(
            graphics,
            gp_ptr,
            0,
            0,
            target_size,
            target_size,
            src_x,
            src_y,
            side,
            side,
            2,  # UnitPixel
            None,
            None,
            None,
        )
        if status != 0:
            return None
        gdiplus.GdipFlush(graphics, 1)
        raw_bgra = ctypes.string_at(bits_ptr.value, target_size * target_size * 4)
        return apply_circular_mask_bgra_to_rgba(raw_bgra, target_size)
    finally:
        if graphics.value:
            gdiplus.GdipDeleteGraphics(graphics)
        if old_dst:
            gdi32.SelectObject(dst_dc, old_dst)
        gdi32.DeleteObject(dib)
        gdi32.DeleteDC(dst_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)


def _scale_hbitmap_to_circular_avatars_win32(
    hbitmap: int,
    profile_name: str,
    *,
    avatars_dir: str | None = None,
    strict_variance: bool = False,
    reject_uniform: bool = False,
) -> bool:
    """Center-crop a Win32 HBITMAP to a square, scale to 22x22 & 64x64, and save circular PNGs."""
    if sys.platform != "win32" or not hbitmap:
        return False
    import ctypes

    user32, gdi32, _k32, _sh32, gdiplus = _win32_apis()
    hbmp_ptr = ctypes.c_void_p(hbitmap)

    # 1. Preferred path: GDI+ HighQualityBicubic from HBITMAP (works even if HBITMAP is selected/clipboard-owned)
    token = ctypes.c_ulonglong(0)
    startup_in = _GdiplusStartupInput(1, None, False, False)
    if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup_in), None) == 0:
        gp_bitmap = ctypes.c_void_p(0)
        try:
            if (
                gdiplus.GdipCreateBitmapFromHBITMAP(hbmp_ptr, None, ctypes.byref(gp_bitmap)) == 0
                and gp_bitmap.value
            ):
                rgba_22 = _render_gp_bitmap_to_square_rgba(gp_bitmap.value, 22)
                rgba_64 = _render_gp_bitmap_to_square_rgba(gp_bitmap.value, 64)
                if (
                    rgba_22
                    and rgba_64
                    and _is_valid_avatar_rgba(
                        rgba_64,
                        strict_variance=strict_variance,
                        reject_uniform=reject_uniform,
                    )
                ):
                    return _save_square_rgba_buffers(
                        profile_name, rgba_22, rgba_64, avatars_dir=avatars_dir
                    )
        finally:
            if gp_bitmap.value:
                gdiplus.GdipDisposeImage(gp_bitmap)
            gdiplus.GdiplusShutdown(token)

    # 2. Fallback path: 64-bit GDI StretchBlt (HALFTONE)
    bmp = _BITMAP()
    if gdi32.GetObjectW(hbmp_ptr, ctypes.sizeof(_BITMAP), ctypes.byref(bmp)) == 0:
        return False

    src_w = int(bmp.bmWidth)
    src_h = abs(int(bmp.bmHeight))
    if src_w < 4 or src_h < 4:
        return False

    side = min(src_w, src_h)
    src_x = (src_w - side) // 2
    src_y = (src_h - side) // 2

    screen_dc = user32.GetDC(None)
    src_dc = gdi32.CreateCompatibleDC(screen_dc)
    if not src_dc:
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)
        return False
    old_src = gdi32.SelectObject(src_dc, hbmp_ptr)
    if not old_src:
        gdi32.DeleteDC(src_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)
        return False

    def _render_size(target_size: int) -> bytes | None:
        dst_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not dst_dc:
            return None
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth = target_size
        bmi.biHeight = -target_size
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

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
            if not gdi32.StretchBlt(
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
            ):
                return None
            raw_bgra = ctypes.string_at(bits_ptr.value, target_size * target_size * 4)
            return apply_circular_mask_bgra_to_rgba(raw_bgra, target_size)
        finally:
            if old_dst:
                gdi32.SelectObject(dst_dc, old_dst)
            gdi32.DeleteObject(dib)
            gdi32.DeleteDC(dst_dc)

    try:
        rgba_22 = _render_size(22)
        rgba_64 = _render_size(64)
        if (
            not rgba_22
            or not rgba_64
            or not _is_valid_avatar_rgba(
                rgba_64,
                strict_variance=strict_variance,
                reject_uniform=reject_uniform,
            )
        ):
            return False
        return _save_square_rgba_buffers(profile_name, rgba_22, rgba_64, avatars_dir=avatars_dir)
    finally:
        gdi32.SelectObject(src_dc, old_src)
        gdi32.DeleteDC(src_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)


class DesktopFreezeFrame(NamedTuple):
    """Frozen desktop snapshot holding physical BGRA pixels and logical dimmed PPM preview."""

    raw_bgra: bytes
    phys_x: int
    phys_y: int
    phys_w: int
    phys_h: int
    log_x: int
    log_y: int
    log_w: int
    log_h: int
    dimmed_ppm: bytes


def bgra_to_ppm_p6(
    bgra: bytes,
    width: int,
    height: int,
    *,
    brightness: float = 0.62,
) -> bytes:
    """Encode top-down BGRA buffer to binary PPM (P6) with fast C-level LUT brightness scaling."""
    if width <= 0 or height <= 0:
        return b""
    expected = width * height * 4
    if len(bgra) < expected:
        return b""
    src = bgra if len(bgra) == expected else bgra[:expected]
    lut = bytes(max(0, min(255, int(round(i * brightness)))) for i in range(256))
    rgb = bytearray(width * height * 3)
    rgb[0::3] = src[2::4].translate(lut)
    rgb[1::3] = src[1::4].translate(lut)
    rgb[2::3] = src[0::4].translate(lut)
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return header + bytes(rgb)


def capture_desktop_freeze_frame() -> DesktopFreezeFrame | None:
    """Capture the full Windows virtual desktop at physical DPI before showing any snip overlay."""
    if sys.platform != "win32":
        return None
    log_x, log_y, log_w, log_h = get_virtual_screen_metrics(physical=False)

    def _capture() -> DesktopFreezeFrame | None:
        try:
            import ctypes

            user32, gdi32, _k32, _sh32, _gp = _win32_apis()
            srccopy = 0x00CC0020

            with _per_monitor_dpi_context():
                phys_x, phys_y, phys_w, phys_h = get_virtual_screen_metrics(physical=True)
                if phys_w < 16 or phys_h < 16 or log_w < 16 or log_h < 16:
                    return None
                screen_dc = user32.GetDC(None)
                if not screen_dc:
                    return None

                mem_dc = None
                hbmp = None
                old_mem = None
                prev_dc = None
                prev_dib = None
                old_prev = None
                try:
                    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
                    if not mem_dc:
                        return None
                    bmi = _make_topdown_bgra_bmi(phys_w, phys_h)
                    bits_ptr = ctypes.c_void_p()
                    hbmp = gdi32.CreateDIBSection(
                        mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0
                    )
                    if not hbmp or not bits_ptr.value:
                        return None
                    old_mem = gdi32.SelectObject(mem_dc, hbmp)
                    ok_blt = bool(
                        gdi32.BitBlt(mem_dc, 0, 0, phys_w, phys_h, screen_dc, phys_x, phys_y, srccopy)
                    )
                    if not ok_blt:
                        return None
                    raw_bgra = ctypes.string_at(bits_ptr.value, phys_w * phys_h * 4)
                    step = max(4, ((len(raw_bgra) // 2048) // 4) * 4)
                    if not any(
                        raw_bgra[i] or raw_bgra[i + 1] or raw_bgra[i + 2]
                        for i in range(0, len(raw_bgra) - 3, step)
                    ):
                        return None

                    if (log_w, log_h) == (phys_w, phys_h):
                        preview_bgra = raw_bgra
                    else:
                        prev_dc = gdi32.CreateCompatibleDC(screen_dc)
                        if not prev_dc:
                            return None
                        prev_bmi = _make_topdown_bgra_bmi(log_w, log_h)
                        prev_bits_ptr = ctypes.c_void_p()
                        prev_dib = gdi32.CreateDIBSection(
                            prev_dc, ctypes.byref(prev_bmi), 0, ctypes.byref(prev_bits_ptr), None, 0
                        )
                        if not prev_dib or not prev_bits_ptr.value:
                            return None
                        old_prev = gdi32.SelectObject(prev_dc, prev_dib)
                        gdi32.SetStretchBltMode(prev_dc, 3)  # COLORONCOLOR
                        if not gdi32.StretchBlt(
                            prev_dc, 0, 0, log_w, log_h, mem_dc, 0, 0, phys_w, phys_h, srccopy
                        ):
                            return None
                        preview_bgra = ctypes.string_at(prev_bits_ptr.value, log_w * log_h * 4)

                    dimmed_ppm = bgra_to_ppm_p6(preview_bgra, log_w, log_h, brightness=0.62)
                    return DesktopFreezeFrame(
                        raw_bgra=raw_bgra,
                        phys_x=phys_x,
                        phys_y=phys_y,
                        phys_w=phys_w,
                        phys_h=phys_h,
                        log_x=log_x,
                        log_y=log_y,
                        log_w=log_w,
                        log_h=log_h,
                        dimmed_ppm=dimmed_ppm,
                    )
                finally:
                    if prev_dc and old_prev:
                        gdi32.SelectObject(prev_dc, old_prev)
                    if prev_dib:
                        gdi32.DeleteObject(prev_dib)
                    if prev_dc:
                        gdi32.DeleteDC(prev_dc)
                    if mem_dc and old_mem:
                        gdi32.SelectObject(mem_dc, old_mem)
                    if hbmp:
                        gdi32.DeleteObject(hbmp)
                    if mem_dc:
                        gdi32.DeleteDC(mem_dc)
                    user32.ReleaseDC(None, screen_dc)
        except Exception:
            return None

    return _run_in_dpi_worker(_capture)


def save_avatar_from_bgra_crop(
    profile_name: str,
    raw_bgra: bytes,
    frame_w: int,
    frame_h: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    avatars_dir: str | None = None,
    strict_variance: bool = False,
    reject_uniform: bool = True,
) -> bool:
    """Crop `(x1, y1, x2, y2)` directly from an in-memory top-down BGRA frame and save avatar."""
    if sys.platform != "win32" or frame_w <= 0 or frame_h <= 0:
        return False
    if len(raw_bgra) < frame_w * frame_h * 4:
        return False
    left = max(0, min(x1, x2))
    right = min(frame_w, max(x1, x2))
    top = max(0, min(y1, y2))
    bottom = min(frame_h, max(y1, y2))
    cw = right - left
    ch = bottom - top
    if cw < 8 or ch < 8:
        return False
    try:
        import ctypes

        user32, gdi32, _k32, _sh32, _gp = _win32_apis()
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            return False
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        user32.ReleaseDC(None, screen_dc)
        if not mem_dc:
            return False

        bmi = _make_topdown_bgra_bmi(cw, ch)
        bits_ptr = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0
        )
        gdi32.DeleteDC(mem_dc)
        if not hbmp or not bits_ptr.value:
            if hbmp:
                gdi32.DeleteObject(hbmp)
            return False

        try:
            row_bytes = cw * 4
            src_stride = frame_w * 4
            crop_buf = bytearray(ch * row_bytes)
            src_view = memoryview(raw_bgra)
            for row in range(ch):
                src_off = (top + row) * src_stride + (left * 4)
                dst_off = row * row_bytes
                crop_buf[dst_off : dst_off + row_bytes] = src_view[src_off : src_off + row_bytes]
            crop_bytes = bytes(crop_buf)
            ctypes.memmove(bits_ptr.value, crop_bytes, len(crop_bytes))
            return _scale_hbitmap_to_circular_avatars_win32(
                int(hbmp),
                profile_name,
                avatars_dir=avatars_dir,
                strict_variance=strict_variance,
                reject_uniform=reject_uniform,
            )
        finally:
            gdi32.DeleteObject(hbmp)
    except Exception:
        return False


def save_avatar_from_screen_rect(
    profile_name: str,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    avatars_dir: str | None = None,
    strict_variance: bool = False,
    reject_uniform: bool = True,
    coords_are_physical: bool = False,
) -> bool:
    """Capture screen region `(x1, y1, x2, y2)` via Win32 GDI and save as circular avatar."""
    if sys.platform != "win32":
        return False
    log_bounds = None if coords_are_physical else get_virtual_screen_metrics(physical=False)

    def _capture_rect() -> bool:
        rx1, ry1, rx2, ry2 = x1, y1, x2, y2
        if not coords_are_physical:
            rx1, ry1, rx2, ry2 = logical_rect_to_physical_rect(
                rx1, ry1, rx2, ry2, logical_bounds=log_bounds
            )
        left, right = min(rx1, rx2), max(rx1, rx2)
        top, bottom = min(ry1, ry2), max(ry1, ry2)
        w = right - left
        h = bottom - top
        if w < 8 or h < 8:
            return False
        try:
            import ctypes

            user32, gdi32, _k32, _sh32, _gp = _win32_apis()
            srccopy = 0x00CC0020

            with _per_monitor_dpi_context():
                screen_dc = user32.GetDC(None)
                if not screen_dc:
                    return False
                mem_dc = gdi32.CreateCompatibleDC(screen_dc)
                if not mem_dc:
                    user32.ReleaseDC(None, screen_dc)
                    return False

                bmi = _make_topdown_bgra_bmi(w, h)
                bits_ptr = ctypes.c_void_p()
                hbmp = gdi32.CreateDIBSection(
                    mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0
                )
                if not hbmp or not bits_ptr.value:
                    gdi32.DeleteDC(mem_dc)
                    user32.ReleaseDC(None, screen_dc)
                    return False

                old = gdi32.SelectObject(mem_dc, hbmp)
                ok_blt = False
                try:
                    ok_blt = bool(gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, left, top, srccopy))
                finally:
                    if old:
                        gdi32.SelectObject(mem_dc, old)
                    gdi32.DeleteDC(mem_dc)
                    user32.ReleaseDC(None, screen_dc)

            try:
                if not ok_blt:
                    return False
                return _scale_hbitmap_to_circular_avatars_win32(
                    int(hbmp),
                    profile_name,
                    avatars_dir=avatars_dir,
                    strict_variance=strict_variance,
                    reject_uniform=reject_uniform,
                )
            finally:
                gdi32.DeleteObject(hbmp)
        except Exception:
            return False

    return _run_in_dpi_worker(_capture_rect)


def _save_avatar_from_dib_memory(
    dib_ptr: int,
    dib_size: int,
    profile_name: str,
    *,
    avatars_dir: str | None = None,
) -> bool:
    """Render a packed `CF_DIB` memory block via `StretchDIBits` into a 32-bit HBITMAP and save."""
    if sys.platform != "win32" or not dib_ptr or dib_size < 40:
        return False
    import ctypes

    user32, gdi32, _k32, _sh32, _gp = _win32_apis()
    hdr = _BITMAPINFOHEADER.from_address(dib_ptr)
    bi_size = int(hdr.biSize)
    w = int(hdr.biWidth)
    h = abs(int(hdr.biHeight))
    if bi_size < 40 or w < 4 or h < 4 or dib_size <= bi_size:
        return False

    # Calculate offset to pixel bits inside packed CF_DIB
    clr_used = int(hdr.biClrUsed)
    bit_count = int(hdr.biBitCount)
    compression = int(hdr.biCompression)
    if clr_used == 0 and bit_count <= 8:
        clr_used = 1 << bit_count
    mask_bytes = 12 if (compression == 3 and bi_size == 40) else 0  # BI_BITFIELDS = 3
    bits_offset = bi_size + (clr_used * 4) + mask_bytes
    if bits_offset >= dib_size:
        return False

    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    if not mem_dc:
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)
        return False

    dst_bmi = _BITMAPINFOHEADER()
    dst_bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    dst_bmi.biWidth = w
    dst_bmi.biHeight = -h
    dst_bmi.biPlanes = 1
    dst_bmi.biBitCount = 32
    dst_bmi.biCompression = 0

    bits_ptr = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(
        mem_dc, ctypes.byref(dst_bmi), 0, ctypes.byref(bits_ptr), None, 0
    )
    if not hbmp or not bits_ptr.value:
        gdi32.DeleteDC(mem_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)
        return False

    old = gdi32.SelectObject(mem_dc, hbmp)
    copied = 0
    try:
        srccopy = 0x00CC0020
        copied = gdi32.StretchDIBits(
            mem_dc,
            0,
            0,
            w,
            h,
            0,
            0,
            w,
            h,
            ctypes.c_void_p(dib_ptr + bits_offset),
            ctypes.c_void_p(dib_ptr),
            0,  # DIB_RGB_COLORS
            srccopy,
        )
    finally:
        if old:
            gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteDC(mem_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)

    try:
        if copied <= 0:
            return False
        return _scale_hbitmap_to_circular_avatars_win32(
            int(hbmp),
            profile_name,
            avatars_dir=avatars_dir,
        )
    finally:
        gdi32.DeleteObject(hbmp)


def save_avatar_from_clipboard(
    profile_name: str,
    *,
    avatars_dir: str | None = None,
) -> bool:
    """Read an image from the Windows Clipboard (`CF_HDROP`, `PNG`, `CF_DIB`, `CF_BITMAP`) and save."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32, _gdi32, kernel32, shell32, _gp = _win32_apis()
        cf_bitmap = 2
        cf_dib = 8
        cf_hdrop = 15

        # Retry OpenClipboard briefly in case another process is releasing it
        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.02)
        if not opened:
            return False

        copied_file_path: str | None = None
        png_bytes: bytes | None = None
        try:
            # 1. Check CF_HDROP (user copied an image file in Explorer)
            if user32.IsClipboardFormatAvailable(cf_hdrop):
                hdrop = user32.GetClipboardData(cf_hdrop)
                if hdrop:
                    count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                    if count > 0:
                        buf = ctypes.create_unicode_buffer(1024)
                        if shell32.DragQueryFileW(hdrop, 0, buf, 1024) > 0:
                            candidate = buf.value
                            if candidate and os.path.isfile(candidate):
                                copied_file_path = candidate

            # 2. Check registered PNG clipboard format (browsers / modern screenshot tools)
            if not copied_file_path:
                for fmt_name in ("PNG", "image/png"):
                    cf_png = user32.RegisterClipboardFormatW(fmt_name)
                    if cf_png and user32.IsClipboardFormatAvailable(cf_png):
                        hmem = user32.GetClipboardData(cf_png)
                        if hmem:
                            sz = int(kernel32.GlobalSize(hmem))
                            ptr = kernel32.GlobalLock(hmem)
                            if ptr and sz > 24:
                                try:
                                    png_bytes = ctypes.string_at(ptr, sz)
                                finally:
                                    kernel32.GlobalUnlock(hmem)
                            if png_bytes:
                                break

            # 3. Check CF_DIB (device-independent bitmap — works reliably across all Win32 apps)
            if not copied_file_path and not png_bytes and user32.IsClipboardFormatAvailable(cf_dib):
                hdib = user32.GetClipboardData(cf_dib)
                if hdib:
                    sz = int(kernel32.GlobalSize(hdib))
                    ptr = kernel32.GlobalLock(hdib)
                    if ptr and sz > 40:
                        try:
                            if _save_avatar_from_dib_memory(
                                int(ptr), sz, profile_name, avatars_dir=avatars_dir
                            ):
                                return True
                        finally:
                            kernel32.GlobalUnlock(hdib)

            # 4. Check CF_BITMAP
            if not copied_file_path and not png_bytes and user32.IsClipboardFormatAvailable(cf_bitmap):
                hbmp = user32.GetClipboardData(cf_bitmap)
                if hbmp and _scale_hbitmap_to_circular_avatars_win32(
                    int(hbmp),
                    profile_name,
                    avatars_dir=avatars_dir,
                ):
                    return True
        finally:
            user32.CloseClipboard()

        if copied_file_path:
            return save_avatar_from_file(profile_name, copied_file_path, avatars_dir=avatars_dir)

        if png_bytes:
            fd, tmp_png = tempfile.mkstemp(suffix=".png", prefix="foxav_")
            os.close(fd)
            try:
                pathlib.Path(tmp_png).write_bytes(png_bytes)
                return save_avatar_from_file(profile_name, tmp_png, avatars_dir=avatars_dir)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_png)

        return False
    except Exception:
        return False


def save_avatar_from_file(
    profile_name: str,
    file_path: str,
    *,
    avatars_dir: str | None = None,
) -> bool:
    """Load any image file (`.png`, `.jpg`, `.jpeg`, `.bmp`, `.gif`) via Win32 GDI+ and save circular avatar."""
    if sys.platform != "win32" or not os.path.isfile(file_path):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        _u32, gdi32, _k32, _sh32, gdiplus = _win32_apis()

        token = ctypes.c_ulonglong(0)
        startup_in = _GdiplusStartupInput(1, None, False, False)
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

            # Direct GDI+ HighQualityBicubic render
            rgba_22 = _render_gp_bitmap_to_square_rgba(gp_bitmap.value, 22)
            rgba_64 = _render_gp_bitmap_to_square_rgba(gp_bitmap.value, 64)
            if rgba_22 and rgba_64 and _is_valid_avatar_rgba(rgba_64):
                return _save_square_rgba_buffers(
                    profile_name, rgba_22, rgba_64, avatars_dir=avatars_dir
                )

            # Fallback via HBITMAP
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
                gdi32.DeleteObject(hbmp)
            if gp_bitmap.value:
                gdiplus.GdipDisposeImage(gp_bitmap)
            gdiplus.GdiplusShutdown(token)
    except Exception:
        return False


def _box_has_avatar_content_bgra(
    raw_bgra: bytes,
    width: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
) -> bool:
    """Verify the candidate box `(x1, y1, x2, y2)` contains a real loaded avatar, not a blank white/black area."""
    bw = x2 - x1
    bh = y2 - y1
    if bw < 32 or bh < 32:
        return False

    samples: list[tuple[int, int, int]] = []
    non_white = 0
    non_black = 0
    r_min, g_min, b_min = 255, 255, 255
    r_max, g_max, b_max = 0, 0, 0

    # Sample a 7x7 interior grid inside the inscribed circle
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    max_r = min(bw, bh) * 0.38
    for gy in range(7):
        sy = int(round(y1 + bh * (0.18 + 0.64 * (gy / 6.0))))
        for gx in range(7):
            sx = int(round(x1 + bw * (0.18 + 0.64 * (gx / 6.0))))
            if math.hypot(sx - cx, sy - cy) > max_r:
                continue
            idx = (sy * width + sx) * 4
            b = raw_bgra[idx]
            g = raw_bgra[idx + 1]
            r = raw_bgra[idx + 2]
            samples.append((r, g, b))
            if r < 242 or g < 242 or b < 242:
                non_white += 1
            if r > 14 or g > 14 or b > 14:
                non_black += 1
            if r < r_min:
                r_min = r
            if g < g_min:
                g_min = g
            if b < b_min:
                b_min = b
            if r > r_max:
                r_max = r
            if g > g_max:
                g_max = g
            if b > b_max:
                b_max = b

    n = len(samples)
    if n < 16:
        return False
    # Reject mostly-white or mostly-black regions
    if non_white < max(5, n // 5) or non_black < max(5, n // 5):
        return False
    # Require genuine color/luminance spread across the avatar circle
    return max(r_max - r_min, g_max - g_min, b_max - b_min) >= 28


def detect_foreground_running_profile(
    running_names: tuple[str, ...] | list[str],
) -> str | None:
    """Return the running profile name matching the active Win32 foreground window title."""
    if sys.platform != "win32" or not running_names:
        return None
    try:
        import ctypes

        user32, _gdi32, _k32, _sh32, _gp = _win32_apis()
        fg = user32.GetForegroundWindow()
        if not fg or user32.IsIconic(fg):
            return None
        length = user32.GetWindowTextLengthW(fg)
        if length <= 0:
            return None
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(fg, buf, length + 1)
        title = buf.value or ""
        for pname in sorted(running_names, key=len, reverse=True):
            if title.startswith(f"[{pname}] "):
                return pname
        if len(running_names) == 1 and not title.startswith("[") and ("Muse" in title or " — " in title):
            return running_names[0]
        return None
    except Exception:
        return None


def find_muse_connected_avatar_box_bgra(
    raw_bgra: bytes,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    """Detect the `⚡ Connected` green badge in a BGRA frame and return the circular avatar bbox `(x1, y1, x2, y2)`."""
    if width < 160 or height < 180 or len(raw_bgra) < width * height * 4:
        return None

    def _is_green(r: int, g: int, b: int) -> bool:
        return g >= 115 and r < 85 and b < 140 and (g - r) >= 60 and (g - b) >= 35

    # Scan with stride=2 to locate the green `⚡ Connected` badge across 100%-200% DPI scales
    visited: set[tuple[int, int]] = set()
    for sy in range(80, height - 10, 2):
        row_off = sy * width * 4
        for sx in range(14, width - 24, 2):
            idx = row_off + sx * 4
            b = raw_bgra[idx]
            g = raw_bgra[idx + 1]
            r = raw_bgra[idx + 2]
            if _is_green(r, g, b) and (sx, sy) not in visited:
                gx_min = sx
                gx_max = sx
                gy_min = sy
                gy_max = sy
                count = 0
                for yy in range(max(0, sy - 20), min(height, sy + 40)):
                    r_off = yy * width * 4
                    for xx in range(max(0, sx - 24), min(width, sx + 40)):
                        ii = r_off + xx * 4
                        bb, gg, rr = raw_bgra[ii], raw_bgra[ii + 1], raw_bgra[ii + 2]
                        if _is_green(rr, gg, bb):
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
                if (
                    8 <= gw <= 38
                    and 8 <= gh <= 38
                    and 40 <= count <= 950
                    and gw <= gh * 1.7
                    and gh <= gw * 1.7
                ):
                    scale = max(0.80, min(2.5, (gw + gh) / 28.0))
                    gx_c = (gx_min + gx_max) / 2.0
                    gy_c = (gy_min + gy_max) / 2.0
                    x1 = int(round(gx_c - 13.0 * scale))
                    x2 = int(round(gx_c + 109.0 * scale))
                    side = x2 - x1
                    if side < 44 or x1 < 0 or x2 > width:
                        continue
                    for v_mult in (1.0, 0.88, 1.12):
                        y1 = int(round(gy_c - 174.0 * scale * v_mult))
                        y2 = y1 + side
                        if (
                            y1 >= 0
                            and y2 <= height
                            and _box_has_avatar_content_bgra(raw_bgra, width, x1, y1, x2, y2)
                        ):
                            return (x1, y1, x2, y2)
    return None


def auto_capture_muse_avatar_from_active_window(
    profile_name: str,
    *,
    avatars_dir: str | None = None,
    allow_unprefixed_single: bool = True,
) -> bool:
    """Passively inspect the foreground Camoufox window for the Muse `⚡ Connected` card and auto-save its avatar."""
    if sys.platform != "win32" or not profile_name:
        return False
    try:
        import ctypes

        user32, gdi32, _k32, _sh32, _gp = _win32_apis()

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
        if not (
            title.startswith(prefix)
            or (
                allow_unprefixed_single
                and not title.startswith("[")
                and ("Muse" in title or " — " in title)
            )
        ):
            return False

        srccopy = 0x00CC0020

        def _capture_fg_and_save() -> bool:
            raw_bgra = b""
            scan_w = 0
            scan_h = 0
            with _per_monitor_dpi_context():
                rect = _RECT()
                if not user32.GetWindowRect(fg, ctypes.byref(rect)):
                    return False
                win_w = int(rect.right - rect.left)
                win_h = int(rect.bottom - rect.top)
                if win_w < 300 or win_h < 300:
                    return False

                # Scan the top band of the browser window in physical DPI coordinates
                scan_x = int(rect.left) + 8
                scan_y = int(rect.top) + 48
                scan_w = min(win_w - 16, 2560)
                scan_h = min(win_h - 56, 760)
                if scan_w < 200 or scan_h < 200:
                    return False

                screen_dc = user32.GetDC(None)
                if not screen_dc:
                    return False
                mem_dc = gdi32.CreateCompatibleDC(screen_dc)
                if not mem_dc:
                    user32.ReleaseDC(None, screen_dc)
                    return False

                bmi = _make_topdown_bgra_bmi(scan_w, scan_h)
                bits_ptr = ctypes.c_void_p()
                dib = gdi32.CreateDIBSection(
                    mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0
                )
                if not dib or not bits_ptr.value:
                    gdi32.DeleteDC(mem_dc)
                    user32.ReleaseDC(None, screen_dc)
                    return False

                old = gdi32.SelectObject(mem_dc, dib)
                try:
                    if gdi32.BitBlt(mem_dc, 0, 0, scan_w, scan_h, screen_dc, scan_x, scan_y, srccopy):
                        raw_bgra = ctypes.string_at(bits_ptr.value, scan_w * scan_h * 4)
                finally:
                    if old:
                        gdi32.SelectObject(mem_dc, old)
                    gdi32.DeleteObject(dib)
                    gdi32.DeleteDC(mem_dc)
                    user32.ReleaseDC(None, screen_dc)

            if not raw_bgra:
                return False
            box = find_muse_connected_avatar_box_bgra(raw_bgra, scan_w, scan_h)
            if box is None:
                return False
            bx1, by1, bx2, by2 = box
            return save_avatar_from_bgra_crop(
                profile_name,
                raw_bgra,
                scan_w,
                scan_h,
                bx1,
                by1,
                bx2,
                by2,
                avatars_dir=avatars_dir,
                strict_variance=True,
                reject_uniform=True,
            )

        return _run_in_dpi_worker(_capture_fg_and_save)
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

        user32, gdi32, _k32, _sh32, gdiplus = _win32_apis()

        token = ctypes.c_ulonglong(0)
        startup_in = _GdiplusStartupInput(1, None, False, False)
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

            screen_dc = user32.GetDC(None)
            dst_dc = gdi32.CreateCompatibleDC(screen_dc)
            if not dst_dc:
                if screen_dc:
                    user32.ReleaseDC(None, screen_dc)
                return None

            bmi = _BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
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
                if screen_dc:
                    user32.ReleaseDC(None, screen_dc)
                return None

            old_dst = gdi32.SelectObject(dst_dc, dib)
            graphics = ctypes.c_void_p(0)
            try:
                if gdiplus.GdipCreateFromHDC(dst_dc, ctypes.byref(graphics)) != 0:
                    return None
                argb_bg = (0xFF << 24) | (r_bg << 16) | (g_bg << 8) | b_bg
                gdiplus.GdipGraphicsClear(graphics, wintypes.DWORD(argb_bg))
                gdiplus.GdipSetInterpolationMode(graphics, 7)
                gdiplus.GdipSetSmoothingMode(graphics, 4)
                gdiplus.GdipSetPixelOffsetMode(graphics, 4)
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
                    2,
                    None,
                    None,
                    None,
                )
                gdiplus.GdipFlush(graphics, 1)
                raw_bgra = ctypes.string_at(bits_ptr.value, size * size * 4)
            finally:
                if graphics.value:
                    gdiplus.GdipDeleteGraphics(graphics)
                if old_dst:
                    gdi32.SelectObject(dst_dc, old_dst)
                gdi32.DeleteObject(dib)
                gdi32.DeleteDC(dst_dc)
                if screen_dc:
                    user32.ReleaseDC(None, screen_dc)

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
