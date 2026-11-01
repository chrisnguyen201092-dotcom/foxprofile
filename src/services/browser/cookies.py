"""Cookie export/import for stopped profiles.

Cookies travel between three shapes:
- Playwright: what cookie_tool.py reads from and writes to the browser.
- JSON in the EditThisCookie / Cookie-Editor layout, which GoLogin, GPM,
  Multilogin and the Cookie-Editor extension import directly.
- Netscape cookies.txt, used by yt-dlp, curl and wget.

For stopped profiles, both Camoufox (`cookies.sqlite`) and Chrome
(`Default/Network/Cookies` + `Local State` DPAPI/AES-256-GCM) support
direct `< 5ms` SQLite inspection and export/import without launching a browser.
"""

import base64
import contextlib
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import pathlib
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from typing import Any

from ...core.config import DATA_DIR
from ...core.logging import get_logger

logger = get_logger("browser.cookies")

_TOOL = os.path.join(pathlib.Path(__file__).parent, "cookie_tool.py")
_PROJECT_ROOT = str(pathlib.Path(__file__).resolve().parents[3])
_TIMEOUT = 120

_SAMESITE_TO_EXT = {"Strict": "strict", "Lax": "lax", "None": "no_restriction"}
_SAMESITE_FROM_EXT = {
    "strict": "Strict",
    "lax": "Lax",
    "no_restriction": "None",
    "none": "None",
}

FORMATS = ("json", "netscape")

# Firefox drops session cookies when the browser closes, so an imported
# session cookie would vanish before the profile is ever opened. Imported
# session cookies get this lifetime instead.
_SESSION_COOKIE_TTL = 365 * 24 * 3600

# Seconds between 1601-01-01 (Windows/WebKit epoch) and 1970-01-01 (Unix epoch)
WINDOWS_EPOCH_DIFF = 11_644_473_600

_MOZ_SAMESITE = {0: "None", 1: "Lax", 2: "Strict"}
_CHROME_SAMESITE_TO_PW = {-1: "Lax", 0: "None", 1: "Lax", 2: "Strict"}
_PW_SAMESITE_TO_CHROME = {"None": 0, "Lax": 1, "Strict": 2}


class CookieError(Exception):
    pass


def _call_tool(
    action: str, profile_name: str, os_type: str, stdin: str = "", engine: str = "camoufox"
) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = _PROJECT_ROOT + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    try:
        proc = subprocess.run(
            [sys.executable, _TOOL, action, profile_name, os_type, engine],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
            cwd=pathlib.Path.cwd(),
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise CookieError(f"Cookie {action} timed out after {_TIMEOUT}s") from e

    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    if not lines:
        tail = (proc.stderr or proc.stdout).strip()[-300:]
        raise CookieError(f"Cookie {action} failed: {tail or 'no output'}")
    result = json.loads(lines[-1].removeprefix("RESULT:"))
    if "error" in result:
        raise CookieError(result["error"])
    return result


# --- Format conversion -------------------------------------------------------


def _to_extension_json(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, c in enumerate(cookies, start=1):
        expires = c.get("expires", -1)
        session = expires is None or expires < 0
        item = {
            "domain": c["domain"],
            "hostOnly": not c["domain"].startswith("."),
            "httpOnly": bool(c.get("httpOnly")),
            "name": c["name"],
            "path": c.get("path", "/"),
            "sameSite": _SAMESITE_TO_EXT.get(c.get("sameSite", ""), "unspecified"),
            "secure": bool(c.get("secure")),
            "session": session,
            "storeId": "0",
            "value": c["value"],
            "id": i,
        }
        if not session:
            item["expirationDate"] = expires
        out.append(item)
    return out


def _to_netscape(cookies: list[dict[str, Any]]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# Exported by FoxProfile", ""]
    for c in cookies:
        domain = c["domain"]
        if c.get("httpOnly"):
            domain = "#HttpOnly_" + domain
        expires = c.get("expires", -1)
        lines.append(
            "\t".join(
                [
                    domain,
                    "TRUE" if c["domain"].startswith(".") else "FALSE",
                    c.get("path", "/"),
                    "TRUE" if c.get("secure") else "FALSE",
                    str(int(expires)) if expires and expires > 0 else "0",
                    c["name"],
                    c["value"],
                ],
            ),
        )
    return "\n".join(lines) + "\n"


def _as_timestamp(value: Any) -> float:
    """Expiry as epoch seconds, or -1 when missing or not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def _from_json(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cookies = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name") or not item.get("domain"):
            continue
        name = str(item["name"])
        domain = str(item["domain"])
        is_host_prefix = name.startswith("__Host-")
        if is_host_prefix:
            domain = domain.lstrip(".")
        elif item.get("hostOnly") is False and not domain.startswith("."):
            domain = "." + domain
        # Extension layout uses expirationDate; Playwright layout uses expires.
        expires = item.get("expirationDate", item.get("expires"))
        same_site = item.get("sameSite")
        if same_site not in ("Strict", "Lax", "None"):
            same_site = _SAMESITE_FROM_EXT.get(str(same_site).lower(), "Lax")
        cookie = {
            "name": name,
            "value": str(item.get("value", "")),
            "domain": domain,
            "path": "/" if is_host_prefix else (item.get("path") or "/"),
            "httpOnly": bool(item.get("httpOnly")),
            "secure": True if is_host_prefix else (bool(item.get("secure")) or same_site == "None"),
            "sameSite": same_site,
        }
        expiry = _as_timestamp(expires)
        if expiry > 0 and not item.get("session"):
            cookie["expires"] = expiry
        else:
            cookie["expires"] = time.time() + _SESSION_COOKIE_TTL
        cookies.append(cookie)
    return cookies


def _from_netscape(text: str) -> list[dict[str, Any]]:
    cookies = []
    for raw in text.splitlines():
        line = raw.strip()
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line.removeprefix("#HttpOnly_")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _subdomains, path, secure, expires, name, value = parts
        is_host_prefix = name.startswith("__Host-")
        cookie = {
            "name": name,
            "value": value,
            "domain": domain.lstrip(".") if is_host_prefix else domain,
            "path": "/" if is_host_prefix else (path or "/"),
            "httpOnly": http_only,
            "secure": True if is_host_prefix else (secure.upper() == "TRUE"),
            "sameSite": "Lax",
        }
        if expires.isdigit() and int(expires) > 0:
            cookie["expires"] = float(expires)
        else:
            cookie["expires"] = time.time() + _SESSION_COOKIE_TTL
        cookies.append(cookie)
    return cookies


def _from_header_string(text: str, default_domain: str = ".hatch.io") -> list[dict[str, Any]]:
    """Parse 'Cookie: k1=v1; k2=v2' or 'k1=v1; k2=v2' header string."""
    raw = text.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    eff_domain = default_domain
    if default_domain == ".hatch.io" and any(
        mk in raw
        for mk in (
            "hatch_sess=",
            "hatch_native_auth_device=",
            "__Host-hatch_one_tap=",
            "layout_state=",
        )
    ):
        eff_domain = ".muse.ai"
    cookies = []
    for part in raw.split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        k, v = piece.split("=", 1)
        name = k.strip()
        val = v.strip()
        if not name or "\n" in name or "\t" in name or " " in name:
            continue
        is_host_prefix = name.startswith("__Host-")
        cookies.append(
            {
                "name": name,
                "value": val,
                "domain": eff_domain.lstrip(".") if is_host_prefix else eff_domain,
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
                "expires": time.time() + _SESSION_COOKIE_TTL,
            }
        )
    return cookies


def parse_cookies(text: str) -> list[dict[str, Any]]:
    """Parse JSON (extension or Playwright layout), Netscape cookies.txt, or Cookie header string."""
    stripped = text.strip()
    if stripped.startswith(("[", "{")):
        try:
            data = json.loads(stripped)
        except ValueError as e:
            raise CookieError(f"Invalid cookie JSON: {e}") from e
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        if not isinstance(data, list):
            raise CookieError("Cookie JSON must be a list of cookies")
        return _from_json(data)
    netscape = _from_netscape(text)
    if netscape:
        return netscape
    if "\t" not in stripped and "=" in stripped:
        return _from_header_string(stripped)
    return []


def format_cookies(cookies: list[dict[str, Any]], fmt: str = "json") -> tuple[str, int]:
    """Format a list of Playwright-shaped cookie dicts into `fmt` ('json' or 'netscape')."""
    if fmt not in FORMATS:
        raise CookieError(f"Unknown format '{fmt}'. Use one of: {', '.join(FORMATS)}")
    if fmt == "netscape":
        return _to_netscape(cookies), len(cookies)
    return json.dumps(_to_extension_json(cookies), indent=2, ensure_ascii=False), len(cookies)


# --- Chromium OS_Crypt & SQLite Direct Engine (<5ms) -------------------------


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_unprotect(encrypted_bytes: bytes) -> bytes | None:
    """Decrypt a Windows DPAPI blob using CryptUnprotectData."""
    if sys.platform != "win32" or not encrypted_bytes:
        return None
    try:
        buf = ctypes.create_string_buffer(encrypted_bytes, len(encrypted_bytes))
        blob_in = _DataBlob(len(encrypted_bytes), buf)
        blob_out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def _dpapi_protect(plain_bytes: bytes) -> bytes | None:
    """Encrypt bytes into a Windows DPAPI blob using CryptProtectData."""
    if sys.platform != "win32" or not plain_bytes:
        return None
    try:
        buf = ctypes.create_string_buffer(plain_bytes, len(plain_bytes))
        blob_in = _DataBlob(len(plain_bytes), buf)
        blob_out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def _load_chrome_aes_gcm_key(profile_dir: pathlib.Path) -> bytes | None:
    """Read and unwrap `os_crypt.encrypted_key` from Chromium's `Local State` file."""
    local_state = profile_dir / "Local State"
    if not local_state.is_file():
        return None
    try:
        data = json.loads(local_state.read_text(encoding="utf-8", errors="replace"))
        enc_key_b64 = (data.get("os_crypt") or {}).get("encrypted_key")
        if not enc_key_b64 or not isinstance(enc_key_b64, str):
            return None
        raw = base64.b64decode(enc_key_b64)
        if raw.startswith(b"DPAPI"):
            raw = raw[5:]
            key = _dpapi_unprotect(raw)
            if key and len(key) == 32:
                return key
        elif len(raw) == 32:
            return raw
    except Exception:
        return None
    return None


def _strip_host_key_sha256_prefix(plaintext: bytes, host_key: str) -> str:
    """Strip the 32-byte SHA-256(host_key) domain binding prefix added in Chromium M100+ (DB v24+)."""
    if len(plaintext) >= 32:
        if host_key:
            expected = hashlib.sha256(host_key.encode("utf-8")).digest()
            if plaintext[:32] == expected:
                return plaintext[32:].decode("utf-8", errors="replace")
        # Fallback heuristic from DonutBrowser: if first 32 bytes are non-UTF8 or contain control bytes
        # while the remainder is clean printable ASCII/UTF-8, strip the 32-byte digest prefix.
        tail = plaintext[32:]
        if tail and all(0x20 <= b < 0x7F for b in tail):
            try:
                head_str = plaintext[:32].decode("utf-8")
                if any(ord(ch) < 0x20 for ch in head_str):
                    return tail.decode("utf-8")
            except UnicodeDecodeError:
                return tail.decode("utf-8")
    return plaintext.decode("utf-8", errors="replace")


def _decrypt_chrome_cookie_value(
    encrypted_value: bytes,
    host_key: str,
    aes_gcm_key: bytes | None,
) -> str | None:
    """Decrypt a Chromium `v10`/`v11` cookie value (AES-256-GCM or Linux PBKDF2 AES-128-CBC)."""
    if not encrypted_value:
        return ""
    raw = bytes(encrypted_value)
    prefix = raw[:3]
    if prefix in (b"v10", b"v11"):
        # 1. Try AES-256-GCM (Windows DPAPI Local State key or 32-byte key)
        if aes_gcm_key and len(aes_gcm_key) == 32 and len(raw) >= 3 + 12 + 16:
            with contextlib.suppress(Exception):
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM

                nonce = raw[3:15]
                ct_and_tag = raw[15:]
                pt = AESGCM(aes_gcm_key).decrypt(nonce, ct_and_tag, None)
                return _strip_host_key_sha256_prefix(pt, host_key)
        # 2. Fallback: Linux Chromium default `peanuts` PBKDF2-HMAC-SHA1 AES-128-CBC
        body = raw[3:]
        if len(body) >= 16 and len(body) % 16 == 0:
            with contextlib.suppress(Exception):
                from cryptography.hazmat.primitives import hashes, padding
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

                kdf = PBKDF2HMAC(
                    algorithm=hashes.SHA1(),
                    length=16,
                    salt=b"saltysalt",
                    iterations=1,
                )
                cbc_key = kdf.derive(b"peanuts")
                cipher = Cipher(algorithms.AES(cbc_key), modes.CBC(b" " * 16))
                dec = cipher.decryptor()
                padded = dec.update(body) + dec.finalize()
                unpadder = padding.PKCS7(128).unpadder()
                pt = unpadder.update(padded) + unpadder.finalize()
                return _strip_host_key_sha256_prefix(pt, host_key)
        return None
    # Unprefixed raw DPAPI on very old Windows Chromium builds
    if sys.platform == "win32":
        unprotected = _dpapi_unprotect(raw)
        if unprotected is not None:
            return unprotected.decode("utf-8", errors="replace")
    with contextlib.suppress(UnicodeDecodeError):
        return raw.decode("utf-8")
    return None


def _encrypt_chrome_cookie_value(
    plaintext: str,
    host_key: str,
    aes_gcm_key: bytes | None,
) -> tuple[str, bytes]:
    """Return `(value_col, encrypted_value_col)` for inserting into Chromium's `cookies` table.

    When a 32-byte `aes_gcm_key` is available in `Local State`, encrypts using Chromium M100+
    `v10` AES-256-GCM with the 32-byte `SHA256(host_key)` domain prefix. Otherwise stores
    plaintext `value` with empty `encrypted_value = b""` (which Chromium reads natively).
    """
    if aes_gcm_key and len(aes_gcm_key) == 32:
        with contextlib.suppress(Exception):
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            nonce = secrets.token_bytes(12)
            domain_hash = hashlib.sha256(host_key.encode("utf-8")).digest()
            payload = domain_hash + plaintext.encode("utf-8")
            ct = AESGCM(aes_gcm_key).encrypt(nonce, payload, None)
            return "", b"v10" + nonce + ct
    return plaintext, b""


def _chrome_time_to_unix(chrome_utc: int | float | None) -> float:
    if not chrome_utc:
        return -1.0
    val = float(chrome_utc)
    if val <= 0:
        return -1.0
    unix_sec = (val / 1_000_000.0) - WINDOWS_EPOCH_DIFF
    return unix_sec if unix_sec > 0 else -1.0


def _unix_to_chrome_time(unix_sec: float | int | None) -> int:
    if unix_sec is None or unix_sec <= 0:
        return 0
    return int((float(unix_sec) + WINDOWS_EPOCH_DIFF) * 1_000_000)


def find_chrome_cookies_db(profile_dir: pathlib.Path) -> pathlib.Path | None:
    """Return the existing Chromium Cookies SQLite file for `profile_dir`, or None."""
    for rel in (
        pathlib.Path("Default") / "Network" / "Cookies",
        pathlib.Path("Default") / "Cookies",
    ):
        cand = profile_dir / rel
        if cand.is_file():
            return cand
    return None


def read_chrome_cookies_sqlite_readonly(profile_name: str) -> list[dict[str, Any]] | None:
    """Fast read-only SQLite read of Chromium `Default/Network/Cookies` (<5ms).

    Strictly uses `mode=ro&immutable=1` so no WAL/SHM/lock is created and mtime is untouched.
    Returns None if the database does not exist or if an encrypted cookie cannot be decrypted.
    """
    pdir = pathlib.Path(DATA_DIR) / profile_name
    db_path = find_chrome_cookies_db(pdir)
    if db_path is None:
        return None
    try:
        db_posix = db_path.resolve().as_posix()
        uri = f"file:///{urllib.parse.quote(db_posix, safe=':/')}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT host_key, name, value, encrypted_value, path, expires_utc, "
                "is_secure, is_httponly, samesite FROM cookies"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        return None

    aes_key: bytes | None = None
    key_loaded = False
    out: list[dict[str, Any]] = []
    for (
        host_key,
        name,
        value,
        encrypted_value,
        path,
        expires_utc,
        is_secure,
        is_httponly,
        samesite,
    ) in rows:
        host_s = str(host_key or "")
        val_s = str(value or "")
        if not val_s and encrypted_value:
            if not key_loaded:
                aes_key = _load_chrome_aes_gcm_key(pdir)
                key_loaded = True
            decrypted = _decrypt_chrome_cookie_value(bytes(encrypted_value), host_s, aes_key)
            if decrypted is None:
                # Cannot decrypt this cookie without launching the browser
                return None
            val_s = decrypted
        exp_val = _chrome_time_to_unix(expires_utc)
        out.append(
            {
                "name": str(name or ""),
                "value": val_s,
                "domain": host_s,
                "path": str(path or "/"),
                "expires": exp_val if exp_val > 0 else -1,
                "secure": bool(is_secure),
                "httpOnly": bool(is_httponly),
                "sameSite": _CHROME_SAMESITE_TO_PW.get(
                    int(samesite if samesite is not None else -1), "Lax"
                ),
            }
        )
    return out


_CHROME_COOKIES_DDL = """
CREATE TABLE IF NOT EXISTS meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
INSERT OR IGNORE INTO meta VALUES('mmap_status', '-1');
INSERT OR IGNORE INTO meta VALUES('version', '23');
INSERT OR IGNORE INTO meta VALUES('last_compatible_version', '23');
CREATE TABLE IF NOT EXISTS cookies(
    creation_utc INTEGER NOT NULL,
    host_key TEXT NOT NULL,
    top_frame_site_key TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    encrypted_value BLOB NOT NULL DEFAULT X'',
    path TEXT NOT NULL,
    expires_utc INTEGER NOT NULL,
    is_secure INTEGER NOT NULL,
    is_httponly INTEGER NOT NULL,
    last_access_utc INTEGER NOT NULL,
    has_expires INTEGER NOT NULL DEFAULT 1,
    is_persistent INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 1,
    samesite INTEGER NOT NULL DEFAULT -1,
    source_scheme INTEGER NOT NULL DEFAULT 2,
    source_port INTEGER NOT NULL DEFAULT 443,
    is_same_party INTEGER NOT NULL DEFAULT 0,
    last_update_utc INTEGER NOT NULL DEFAULT 0,
    source_type INTEGER NOT NULL DEFAULT 0,
    has_cross_site_ancestor INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS cookies_unique_index
    ON cookies(host_key, top_frame_site_key, name, path, source_scheme, source_port, has_cross_site_ancestor);
"""


def write_chrome_cookies_sqlite(profile_name: str, cookies: list[dict[str, Any]]) -> int:
    """Directly import cookies into a stopped Chrome profile's SQLite database (<5ms)."""
    pdir = pathlib.Path(DATA_DIR) / profile_name
    db_path = find_chrome_cookies_db(pdir)
    if db_path is None:
        db_path = pdir / "Default" / "Network" / "Cookies"
        db_path.parent.mkdir(parents=True, exist_ok=True)

    aes_key = _load_chrome_aes_gcm_key(pdir)
    now_chrome = _unix_to_chrome_time(time.time())
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        conn.executescript(_CHROME_COOKIES_DDL)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(cookies)")
        existing_cols = {row[1] for row in cur.fetchall()}
        has_modern_cols = "top_frame_site_key" in existing_cols and "source_port" in existing_cols

        count = 0
        for c in cookies:
            name = str(c.get("name") or "")
            host_key = str(c.get("domain") or "")
            if not name or not host_key:
                continue
            path = str(c.get("path") or "/")
            raw_val = str(c.get("value") or "")
            val_col, enc_col = _encrypt_chrome_cookie_value(raw_val, host_key, aes_key)
            exp_sec = float(c.get("expires") or -1)
            has_expires = 1 if exp_sec > 0 else 0
            expires_utc = _unix_to_chrome_time(exp_sec) if has_expires else 0
            is_secure = 1 if c.get("secure") else 0
            is_httponly = 1 if c.get("httpOnly") else 0
            samesite = _PW_SAMESITE_TO_CHROME.get(str(c.get("sameSite") or "Lax"), 1)
            source_scheme = 2 if is_secure else 1
            source_port = 443 if is_secure else 80

            cur.execute(
                "DELETE FROM cookies WHERE host_key = ? AND name = ? AND path = ?",
                (host_key, name, path),
            )
            if has_modern_cols:
                cur.execute(
                    "INSERT INTO cookies ("
                    "creation_utc, host_key, top_frame_site_key, name, value, encrypted_value, "
                    "path, expires_utc, is_secure, is_httponly, last_access_utc, has_expires, "
                    "is_persistent, priority, samesite, source_scheme, source_port, last_update_utc"
                    ") VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        now_chrome,
                        host_key,
                        name,
                        val_col,
                        sqlite3.Binary(enc_col),
                        path,
                        expires_utc,
                        is_secure,
                        is_httponly,
                        now_chrome,
                        has_expires,
                        has_expires,
                        samesite,
                        source_scheme,
                        source_port,
                        now_chrome,
                    ),
                )
            else:
                cur.execute(
                    "INSERT INTO cookies ("
                    "creation_utc, host_key, name, value, encrypted_value, path, expires_utc, "
                    "is_secure, is_httponly, last_access_utc, has_expires, is_persistent, "
                    "priority, samesite"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (
                        now_chrome,
                        host_key,
                        name,
                        val_col,
                        sqlite3.Binary(enc_col),
                        path,
                        expires_utc,
                        is_secure,
                        is_httponly,
                        now_chrome,
                        has_expires,
                        has_expires,
                        samesite,
                    ),
                )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def read_cookies_sqlite_readonly(profile_name: str) -> list[dict[str, Any]] | None:
    """Fast read-only SQLite read of Camoufox (`cookies.sqlite`) or Chrome (`Cookies`) (<5ms).

    Returns None if neither database exists or cannot be queried.
    """
    db_path = pathlib.Path(DATA_DIR) / profile_name / "cookies.sqlite"
    if not db_path.exists():
        return read_chrome_cookies_sqlite_readonly(profile_name)
    try:
        db_posix = db_path.resolve().as_posix()
        uri = f"file:///{urllib.parse.quote(db_posix, safe=':/')}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite "
                "FROM moz_cookies"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        return None

    out: list[dict[str, Any]] = []
    for name, value, host, path, expiry, is_secure, is_http_only, same_site in rows:
        exp_val = float(expiry or -1)
        while exp_val > 10_000_000_000:
            exp_val = exp_val / 1000.0
        out.append(
            {
                "name": str(name or ""),
                "value": str(value or ""),
                "domain": str(host or ""),
                "path": str(path or "/"),
                "expires": exp_val if exp_val > 0 else -1,
                "secure": bool(is_secure),
                "httpOnly": bool(is_http_only),
                "sameSite": _MOZ_SAMESITE.get(int(same_site or 0), "Lax"),
            }
        )
    return out


def inspect_cookie_summary(
    profile_name: str,
    live_cookies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a safe metadata summary of cookies (counts & domains, no secret values)."""
    raw = (
        live_cookies
        if live_cookies is not None
        else (read_cookies_sqlite_readonly(profile_name) or [])
    )
    domain_counts: dict[str, int] = {}
    has_gw = False
    has_session = False
    for c in raw:
        dom = str(c.get("domain") or "").lstrip(".") or "unknown"
        domain_counts[dom] = domain_counts.get(dom, 0) + 1
        cname = str(c.get("name") or "")
        if (
            "cf_clearance" in cname
            or "__cf_bm" in cname
            or cname in ("hatch_gw", "__Host-hatch_one_tap", "hatch_native_auth_device")
        ):
            has_gw = True
        if any(
            k in cname.lower()
            for k in ("session", "token", "auth", "sid", "user", "hatch_sess")
        ):
            has_session = True
    top_domains = sorted(domain_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    return {
        "total": len(raw),
        "domains": top_domains,
        "has_gw": has_gw,
        "has_session": has_session,
    }


# --- Public API --------------------------------------------------------------


def export_cookies(
    profile_name: str, os_type: str, fmt: str = "json", engine: str = "camoufox"
) -> tuple[str, int]:
    """Return (text, count) of a stopped profile's cookies in `fmt`."""
    if fmt not in FORMATS:
        raise CookieError(f"Unknown format '{fmt}'. Use one of: {', '.join(FORMATS)}")
    fast = (
        read_chrome_cookies_sqlite_readonly(profile_name)
        if engine == "chrome"
        else read_cookies_sqlite_readonly(profile_name)
    )
    if fast is not None:
        cookies = fast
    else:
        cookies = _call_tool("export", profile_name, os_type, engine=engine)["cookies"]
    logger.info("Exported %d cookies from %s (%s)", len(cookies), profile_name, fmt)
    return format_cookies(cookies, fmt)


def import_cookies(profile_name: str, os_type: str, text: str, engine: str = "camoufox") -> int:
    """Add cookies from JSON or Netscape text to a stopped profile."""
    cookies = parse_cookies(text)
    if not cookies:
        raise CookieError("No valid cookies found in the input")
    if (
        engine == "chrome"
        and os.getenv("FOXPROFILE_FORCE_COOKIE_TOOL") != "1"
        and getattr(_call_tool, "__name__", "") == "_call_tool"
    ):
        try:
            imported = write_chrome_cookies_sqlite(profile_name, cookies)
            logger.info("Imported %d cookies into %s via direct SQLite", imported, profile_name)
            return imported
        except Exception as e:
            logger.warning("Direct Chrome SQLite cookie import failed (%s); falling back", e)
    result = _call_tool("import", profile_name, os_type, json.dumps(cookies), engine)
    logger.info("Imported %d cookies into %s", result["imported"], profile_name)
    return result["imported"]
