import contextlib
import ctypes
import hashlib
import os
import pathlib
import struct
import sys

import flet as ft

from ...core.strings import get_string
from .colors import COLORS, FONT, FONT_BOLD, FONT_DATA, FONT_MEDIUM, FONT_SEMIBOLD


def build_page_theme() -> ft.Theme:
    return ft.Theme(
        font_family=FONT,
        color_scheme_seed=COLORS["sidebar"],
        color_scheme=ft.ColorScheme(
            primary=COLORS["sidebar"],
            on_primary="#FFFFFF",
            secondary=COLORS["accent"],
            surface=COLORS["card_bg"],
            on_surface=COLORS["text_main"],
            on_surface_variant=COLORS["text_sub"],
            surface_container=COLORS["bg"],
            surface_container_high=COLORS["card_bg"],
            outline=COLORS["rule_strong"],
            outline_variant=COLORS["card_border"],
            error=COLORS["error"],
        ),
        scrollbar_theme=ft.ScrollbarTheme(
            thumb_color=COLORS["scrollbar_grab"],
            thickness=8,
            radius=8,
        ),
        tooltip_theme=ft.TooltipTheme(
            decoration=ft.BoxDecoration(bgcolor=COLORS["text_main"], border_radius=6),
            text_style=ft.TextStyle(color="#F2F4F1", size=12),
        ),
        checkbox_theme=ft.CheckboxTheme(
            fill_color={ft.ControlState.SELECTED: COLORS["sidebar"]},
            border_side=ft.BorderSide(1.5, COLORS["text_sub"]),
            shape=ft.RoundedRectangleBorder(radius=4),
        ),
    )


_FONT_FILES = {
    FONT: "fonts/BeVietnamPro-Regular.ttf",
    FONT_MEDIUM: "fonts/BeVietnamPro-Medium.ttf",
    FONT_SEMIBOLD: "fonts/BeVietnamPro-SemiBold.ttf",
    FONT_BOLD: "fonts/BeVietnamPro-Bold.ttf",
    FONT_DATA: "fonts/JetBrainsMono.ttf",
}

ASSETS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets"))


def _stamp_pe_icon(exe_path: pathlib.Path, ico_path: pathlib.Path) -> bool:
    """Replace RT_GROUP_ICON (101) in a Windows PE executable with ico_path."""
    if os.name != "nt" or not exe_path.is_file() or not ico_path.is_file():
        return False
    try:
        ico_bytes = ico_path.read_bytes()
        if len(ico_bytes) < 6:
            return False
        reserved, ico_type, count = struct.unpack_from("<HHH", ico_bytes, 0)
        if reserved != 0 or ico_type != 1 or count == 0:
            return False

        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
        kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
        kernel32.UpdateResourceW.restype = wintypes.BOOL
        kernel32.UpdateResourceW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.WORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.EndUpdateResourceW.restype = wintypes.BOOL
        kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]

        h_update = kernel32.BeginUpdateResourceW(str(exe_path), False)
        if not h_update:
            return False

        rt_icon = 3
        rt_group_icon = 14
        lang_en_us = 1033
        grp_header = bytearray(struct.pack("<HHH", 0, 1, count))

        for i in range(count):
            entry_off = 6 + i * 16
            entry_head = ico_bytes[entry_off : entry_off + 12]
            img_size, img_offset = struct.unpack_from("<II", ico_bytes, entry_off + 12)
            icon_id = i + 1
            grp_header.extend(entry_head)
            grp_header.extend(struct.pack("<H", icon_id))
            img_data = ico_bytes[img_offset : img_offset + img_size]
            buf = ctypes.create_string_buffer(img_data, len(img_data))
            kernel32.UpdateResourceW(
                h_update,
                rt_icon,
                icon_id,
                lang_en_us,
                buf,
                len(img_data),
            )

        grp_buf = ctypes.create_string_buffer(bytes(grp_header), len(grp_header))
        kernel32.UpdateResourceW(
            h_update,
            rt_group_icon,
            101,
            lang_en_us,
            grp_buf,
            len(grp_header),
        )
        ok = bool(kernel32.EndUpdateResourceW(h_update, False))
        if ok:
            with contextlib.suppress(Exception):
                ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
        return ok
    except Exception:
        return False


def setup_windows_taskbar_icon() -> None:
    """Configure Windows Taskbar AppUserModel properties and stamp the FoxProfile icon."""
    if os.name != "nt":
        return
    icon_path = os.path.abspath(os.path.join(ASSETS_DIR, "icon.ico"))
    if not os.path.exists(icon_path):
        return

    os.environ.setdefault("FLET_APP_USER_MODEL_ID", "FoxProfile.DesktopApp")
    os.environ.setdefault("FLET_APP_RELAUNCH_DISPLAY_NAME", "FoxProfile")
    os.environ.setdefault("FLET_APP_RELAUNCH_ICON", icon_path)
    os.environ.setdefault(
        "FLET_APP_RELAUNCH_COMMAND",
        f'"{sys.executable}" -m src.main',
    )
    with contextlib.suppress(Exception):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FoxProfile.DesktopApp")

    with contextlib.suppress(Exception):
        ico_file = pathlib.Path(icon_path)
        ico_hash = hashlib.sha256(ico_file.read_bytes()).hexdigest()[:16]
        client_root = pathlib.Path.home() / ".flet" / "client"
        if client_root.is_dir():
            for exe_file in client_root.glob("flet-desktop-*/flet/flet.exe"):
                marker = exe_file.parent / ".foxprofile-icon.sha256"
                if marker.exists() and marker.read_text(encoding="utf-8").strip() == ico_hash:
                    continue
                if _stamp_pe_icon(exe_file, ico_file):
                    marker.write_text(ico_hash, encoding="utf-8")


def build_os_dropdown(value: str = "windows") -> ft.Dropdown:
    return ft.Dropdown(
        label=get_string("operating_system"),
        value=value,
        bgcolor=COLORS["input_bg"],
        color=COLORS["text_main"],
        border_color=COLORS["rule_strong"],
        focused_border_color=COLORS["sidebar"],
        label_style=ft.TextStyle(color=COLORS["text_sub"]),
        border_radius=8,
        options=[
            ft.dropdown.Option("windows", "Windows"),
            ft.dropdown.Option("macos", "macOS"),
            ft.dropdown.Option("linux", "Linux"),
        ],
    )


def build_engine_dropdown(value: str = "camoufox", disabled: bool = False) -> ft.Dropdown:
    return ft.Dropdown(
        label=get_string("browser_engine"),
        value=value,
        disabled=disabled,
        bgcolor=COLORS["input_bg"],
        color=COLORS["text_main"],
        border_color=COLORS["rule_strong"],
        focused_border_color=COLORS["sidebar"],
        label_style=ft.TextStyle(color=COLORS["text_sub"]),
        border_radius=8,
        options=[
            ft.dropdown.Option("camoufox", get_string("engine_camoufox")),
            ft.dropdown.Option("chrome", get_string("engine_chrome")),
        ],
    )


def configure_page(page: ft.Page) -> None:
    page.title = get_string("window_title")
    page.window.width, page.window.height = 1400, 860
    page.window.min_width, page.window.min_height = 1180, 680

    icon_name = "icon.ico" if os.name == "nt" else "icon.png"
    icon_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "assets", icon_name)
    )
    if os.path.exists(icon_path):
        page.window.icon = icon_path

    page.padding = page.spacing = 0
    page.fonts = dict(_FONT_FILES)
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = COLORS["bg"]
    page.theme = build_page_theme()
