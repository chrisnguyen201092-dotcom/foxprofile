"""Cookie export/import for stopped profiles.

Cookies travel between three shapes:
- Playwright: what cookie_tool.py reads from and writes to the browser.
- JSON in the EditThisCookie / Cookie-Editor layout, which GoLogin, GPM,
  Multilogin and the Cookie-Editor extension import directly.
- Netscape cookies.txt, used by yt-dlp, curl and wget.
"""

import json
import os
import pathlib
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
        domain = item["domain"]
        if item.get("hostOnly") is False and not domain.startswith("."):
            domain = "." + domain
        # Extension layout uses expirationDate; Playwright layout uses expires.
        expires = item.get("expirationDate", item.get("expires"))
        same_site = item.get("sameSite")
        if same_site not in ("Strict", "Lax", "None"):
            same_site = _SAMESITE_FROM_EXT.get(str(same_site).lower(), "Lax")
        cookie = {
            "name": item["name"],
            "value": str(item.get("value", "")),
            "domain": domain,
            "path": item.get("path") or "/",
            "httpOnly": bool(item.get("httpOnly")),
            "secure": bool(item.get("secure")) or same_site == "None",
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
        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "httpOnly": http_only,
            "secure": secure.upper() == "TRUE",
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
        cookies.append(
            {
                "name": name,
                "value": val,
                "domain": default_domain,
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


_MOZ_SAMESITE = {0: "None", 1: "Lax", 2: "Strict"}


def read_cookies_sqlite_readonly(profile_name: str) -> list[dict[str, Any]] | None:
    """Fast read-only SQLite read of Camoufox/Firefox cookies.sqlite (<5ms).

    Returns None if cookies.sqlite does not exist or cannot be queried.
    """
    db_path = pathlib.Path(DATA_DIR) / profile_name / "cookies.sqlite"
    if not db_path.exists():
        return None
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
        if "cf_clearance" in cname or "__cf_bm" in cname:
            has_gw = True
        if any(k in cname.lower() for k in ("session", "token", "auth", "sid", "user")):
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
    fast = read_cookies_sqlite_readonly(profile_name) if engine == "camoufox" else None
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
    result = _call_tool("import", profile_name, os_type, json.dumps(cookies), engine)
    logger.info("Imported %d cookies into %s", result["imported"], profile_name)
    return result["imported"]
