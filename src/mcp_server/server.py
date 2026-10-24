"""MCP server exposing FoxProfile to AI agents (Antigravity, Codex, Hermes, Claude, Cursor...).

It is a thin client of the FoxProfile REST API, so FoxProfile (desktop app or
`python -m src.server`) must be running. Configure with:
    FOXPROFILE_URL        default http://127.0.0.1:8000
    FOXPROFILE_API_TOKEN  required when the API has a token
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

BASE_URL = os.getenv("FOXPROFILE_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/v1"
TOKEN = os.getenv("FOXPROFILE_API_TOKEN", "")
TIMEOUT = 150

INSTRUCTIONS = """\
FoxProfile manages anti-detect Camoufox browser profiles. Each profile has its own
persistent device fingerprint, cookies, credentials (2FA / Hotmail OAuth2), and proxy.

Anti-Detect Safety Rules for AI Agents:
1. Before launching profiles, use `get_monitor_overview` or `check_prelaunch_safety` to see which
   profiles are ready, idle, and have no proxy port collisions or cooldown warnings.
2. `launch_profile` enforces the 4-layer Pre-Launch Safety Guard by default (blocks missing proxy,
   duplicate active proxy port, port shift cooldown, or proxy port change on an active session).
3. Use `get_profile_2fa` to generate 6-digit TOTP codes and `fetch_hotmail_otp` to read Hotmail
   verification OTPs via OAuth2 without opening a browser tab.
4. Use `list_proxy_pool`, `add_proxies_to_pool`, and `assign_proxy_from_pool` to manage spare proxies.
5. DOM automation (`browser_click`, `browser_type`, `browser_evaluate`) is automatically blocked on
   sensitive anti-detect domains (`muse.ai`, `hatch.io`, `meta.com`, `facebook.com`, reCAPTCHA) to
   protect account trust scores.
6. Cookie export/import and fingerprint reset require the profile to be stopped.
"""

mcp = FastMCP("foxprofile", instructions=INSTRUCTIONS)

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_PAGE_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_PAGE_ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


class FoxProfileError(Exception):
    pass


def _request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    raw: bool = False,
) -> Any:
    url = BASE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = response.read()
    except urllib.error.HTTPError as e:
        raise FoxProfileError(f"FoxProfile API {e.code}: {_error_detail(e.read())}") from e
    except urllib.error.URLError as e:
        raise FoxProfileError(
            f"Cannot reach FoxProfile at {BASE_URL}: {e.reason}. "
            "Start the desktop app or `python -m src.server` first.",
        ) from e
    if raw:
        return payload.decode("utf-8")
    return json.loads(payload) if payload else None


async def _arequest(*args: Any, **kwargs: Any) -> Any:
    # Tools must not block the event loop: when the MCP endpoint is served by
    # FoxProfile's own API process, a blocking call back into that same API
    # would deadlock.
    return await asyncio.to_thread(_request, *args, **kwargs)


def configure(base_url: str, token: str) -> None:
    """Point the tools at a FoxProfile API (used when served over HTTP in-process)."""
    global BASE_URL, TOKEN
    BASE_URL = base_url.rstrip("/") + "/api/v1"
    TOKEN = token


def _error_detail(payload: bytes) -> str:
    text = payload.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict) and "detail" in data:
        return str(data["detail"])
    return text


def _quote(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def _page(profile: str, suffix: str) -> str:
    return f"/browser/{_quote(profile)}/page/{suffix}"


# --- Profiles & Anti-Detect Operations --------------------------------------


@mcp.tool(annotations=_READ)
async def list_profiles() -> dict:
    """List all profiles with their proxy, OS, status, pages, and whether their browser is running."""
    return await _arequest("GET", "/profiles")


@mcp.tool(annotations=_READ)
async def get_monitor_overview(
    page: str | None = None,
    status: str | None = None,
    only_ready: bool = False,
) -> dict:
    """Get operational status of all profiles: running count, ready-to-launch profiles,
    proxy port collisions, shift cooldown timers, exit IPs, and today's runtime.
    Pass `only_ready=True` to list only profiles that can be launched safely right now."""
    return await _arequest(
        "GET",
        "/profiles/monitor/overview",
        query={
            "page": page,
            "status": status,
            "only_ready": str(only_ready).lower() if only_ready else None,
        },
    )


@mcp.tool(annotations=_READ)
async def get_profile(name: str) -> dict:
    """Get one profile's details."""
    return await _arequest("GET", f"/profiles/{_quote(name)}")


@mcp.tool()
async def create_profile(
    name: str,
    os_type: Literal["windows", "macos", "linux"] = "windows",
    proxy: str | None = None,
    timezone: str | None = None,
    locale: str | None = None,
    engine: Literal["camoufox", "chrome"] = "camoufox",
    group: str | None = None,
    status: str | None = None,
    notes: str | None = None,
) -> dict:
    """Create a profile. proxy format: [scheme://][user:pass@]host:port or host:port:user:pass
    (http, https, socks4, socks5). timezone (IANA, e.g. America/Chicago) and locale (e.g. en-US)
    default to following the proxy's IP. engine: camoufox (Firefox, default) or chrome."""
    body: dict[str, Any] = {
        "name": name,
        "os_type": os_type,
        "proxy": proxy,
        "timezone": timezone,
        "locale": locale,
        "engine": engine,
    }
    if group is not None:
        body["group"] = group
    if status is not None:
        body["status"] = status
    if notes is not None:
        body["notes"] = notes
    return await _arequest("POST", "/profiles", body)


@mcp.tool()
async def update_profile(
    name: str,
    new_name: str | None = None,
    proxy: str | None = None,
    os_type: Literal["windows", "macos", "linux"] | None = None,
    timezone: str | None = None,
    locale: str | None = None,
    status: str | None = None,
    group: str | None = None,
    notes: str | None = None,
    pinned: bool | None = None,
) -> dict:
    """Rename a profile or change its proxy / OS / timezone / locale / status / group / notes / pinned.
    Changing the OS generates a new fingerprint. Pass timezone="" or locale="" to follow the IP again."""
    body = {
        k: v
        for k, v in {
            "name": new_name,
            "proxy": proxy,
            "os_type": os_type,
            "timezone": timezone,
            "locale": locale,
            "status": status,
            "group": group,
            "notes": notes,
            "pinned": pinned,
        }.items()
        if v is not None
    }
    return await _arequest("PATCH", f"/profiles/{_quote(name)}", body)


@mcp.tool()
async def bulk_update_profiles(
    names: list[str],
    status: str | None = None,
    group: str | None = None,
    pinned: bool | None = None,
    add_to_page: str | None = None,
    remove_from_page: str | None = None,
) -> dict:
    """Bulk update status tag (e.g. 'ready', 'working', 'done', 'check', 'hold', or '' to clear),
    group, pinned state, or custom page membership (e.g. 'Live', 'Disconnected') for multiple profiles."""
    body: dict[str, Any] = {"names": names}
    for k, v in {
        "status": status,
        "group": group,
        "pinned": pinned,
        "add_to_page": add_to_page,
        "remove_from_page": remove_from_page,
    }.items():
        if v is not None:
            body[k] = v
    return await _arequest("POST", "/profiles/bulk-ops", body)


@mcp.tool(annotations=_READ)
async def check_prelaunch_safety(name: str) -> dict:
    """Run the 4-layer Pre-Launch Safety Guard on a profile without launching it.
    Reports whether launching is safe or lists any proxy/cooldown/session/geo warnings."""
    return await _arequest("GET", f"/browser/{_quote(name)}/prelaunch-check")


@mcp.tool()
async def launch_profile(
    name: str,
    check_safety: bool = True,
    force: bool = False,
) -> dict:
    """Open the profile's browser and wait until it is ready.
    By default (`check_safety=True`), enforces the 4-layer Pre-Launch Safety Guard and refuses
    to launch if the profile has no proxy, shares a busy/cooldown proxy port, or changed proxy port
    with an active session. Pass `force=True` only when the user explicitly confirms overriding warnings."""
    return await _arequest(
        "POST",
        f"/browser/{_quote(name)}/launch",
        query={
            "check_safety": str(check_safety).lower(),
            "force": str(force).lower(),
        },
    )


@mcp.tool()
async def stop_profile(name: str) -> dict:
    """Close the profile's browser."""
    return await _arequest("POST", f"/browser/{_quote(name)}/stop")


@mcp.tool(annotations=_READ)
async def running_profiles() -> dict:
    """Names of profiles whose browser is currently open."""
    return await _arequest("GET", "/browser")


@mcp.tool()
async def arrange_browser_windows() -> dict:
    """Tile all running browser windows neatly on the screen using SWP_NOSIZE
    (guaranteed never to alter browser viewport dimensions)."""
    return await _arequest("POST", "/browser/arrange")


@mcp.tool()
async def focus_browser_window(name: str) -> dict:
    """Bring a running profile's browser window to the foreground."""
    return await _arequest("POST", f"/browser/{_quote(name)}/focus")


@mcp.tool(annotations=_READ)
async def get_profile_2fa(name: str) -> dict:
    """Generate the current 6-digit RFC 6238 TOTP 2FA code for a profile's saved 2FA secret."""
    return await _arequest("GET", f"/profiles/{_quote(name)}/totp")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def fetch_hotmail_otp(name: str, top: int = 8) -> dict:
    """Read the profile's Hotmail/Outlook inbox via Microsoft OAuth2 (Graph API / IMAP XOAUTH2)
    and return the latest emails and extracted 6-digit verification OTP code."""
    return await _arequest("POST", f"/profiles/{_quote(name)}/mail-otp", query={"top": top})


@mcp.tool(annotations=_READ)
async def get_profile_history(name: str) -> dict:
    """Get a profile's past browser session logs and exit IP history (including ISP/DC type)."""
    return await _arequest("GET", f"/profiles/{_quote(name)}/history")


@mcp.tool(annotations=_READ)
async def list_warmup_sites() -> dict:
    """List categorized IPv6 & Google reCAPTCHA (_GRECAPTCHA) warmup websites."""
    return await _arequest("GET", "/profiles/warmup-sites")


@mcp.tool(annotations=_READ)
async def export_cookies(name: str, format: Literal["json", "netscape"] = "json") -> str:
    """Export a stopped profile's cookies as Cookie-Editor JSON or Netscape cookies.txt."""
    return await _arequest(
        "GET", f"/profiles/{_quote(name)}/cookies", query={"format": format}, raw=True
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True))
async def import_cookies(name: str, content: str) -> dict:
    """Add cookies (Cookie-Editor JSON or Netscape cookies.txt text) to a stopped profile."""
    return await _arequest("POST", f"/profiles/{_quote(name)}/cookies", {"content": content})


@mcp.tool(annotations=_READ)
async def get_fingerprint(name: str) -> dict:
    """Show the device a profile presents: platform, screen size, CPU cores."""
    return await _arequest("GET", f"/profiles/{_quote(name)}/fingerprint")


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def reset_fingerprint(name: str) -> dict:
    """Discard a stopped profile's fingerprint; its next launch presents a new device.
    Sites that remember the old device may treat the account as logging in from a new machine."""
    return await _arequest("DELETE", f"/profiles/{_quote(name)}/fingerprint")


# --- Proxy & Pool Management ------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def check_proxy(proxy: str) -> dict:
    """Test a proxy and report the exit IP."""
    return await _arequest("POST", "/proxy/check", {"proxy": proxy})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def check_profile_ip(name: str) -> dict:
    """Check where GeoIP services (Cloudflare, ipinfo, ip-api) place the profile's exit IP and
    warn when they disagree with the timezone/locale the browser presents, or flag a datacenter IP."""
    return await _arequest("GET", f"/profiles/{_quote(name)}/ip-check")


@mcp.tool(annotations=_READ)
async def list_proxy_pool() -> dict:
    """List spare (unassigned) proxies in the central pool and active port usage across profiles."""
    return await _arequest("GET", "/proxy/pool")


@mcp.tool()
async def add_proxies_to_pool(proxies: list[str]) -> dict:
    """Add spare proxies to the central proxy pool (skips duplicates already in use)."""
    return await _arequest("POST", "/proxy/pool", {"proxies": proxies})


@mcp.tool()
async def assign_proxy_from_pool(
    new_proxy: str,
    profile: str | None = None,
    old_target: str | None = None,
) -> dict:
    """Assign `new_proxy` to a stopped `profile`, or replace `old_target` (proxy string or `:port`)
    across all stopped profiles."""
    return await _arequest(
        "POST",
        "/proxy/pool/assign",
        {"new_proxy": new_proxy, "profile": profile, "old_target": old_target},
    )


# --- Page control -----------------------------------------------------------


@mcp.tool(annotations=_PAGE_ACT)
async def browser_navigate(profile: str, url: str) -> dict:
    """Open a URL (http/https) in the profile's active tab."""
    return await _arequest("POST", _page(profile, "navigate"), {"url": url})


@mcp.tool(annotations=_PAGE_ACT)
async def browser_back(profile: str) -> dict:
    """Go back in the active tab's history."""
    return await _arequest("POST", _page(profile, "back"))


@mcp.tool(annotations=_PAGE_READ)
async def browser_snapshot(profile: str, max_chars: int = 40_000) -> dict:
    """Accessibility snapshot of the active tab (roles, names, text). Use it to pick selectors."""
    return await _arequest("GET", _page(profile, "snapshot"), query={"max_chars": max_chars})


@mcp.tool(annotations=_PAGE_READ)
async def browser_get_text(profile: str, selector: str = "body", max_chars: int = 40_000) -> dict:
    """Visible text of an element (default: whole page)."""
    return await _arequest(
        "GET",
        _page(profile, "text"),
        query={"selector": selector, "max_chars": max_chars},
    )


@mcp.tool(annotations=_PAGE_ACT)
async def browser_click(profile: str, selector: str, timeout_ms: int = 10_000) -> dict:
    """Click the first element matching a Playwright selector (CSS, text=..., role=...[name=...]).
    Blocked on protected anti-detect domains (muse.ai, meta.com, reCAPTCHA)."""
    return await _arequest(
        "POST",
        _page(profile, "click"),
        {"selector": selector, "timeout": timeout_ms},
    )


@mcp.tool(annotations=_PAGE_ACT)
async def browser_type(
    profile: str,
    selector: str,
    text: str,
    submit: bool = False,
    clear: bool = True,
) -> dict:
    """Type into a field. submit=True presses Enter afterwards; clear=False appends.
    Blocked on protected anti-detect domains (muse.ai, meta.com, reCAPTCHA)."""
    return await _arequest(
        "POST",
        _page(profile, "type"),
        {"selector": selector, "text": text, "submit": submit, "clear": clear},
    )


@mcp.tool(annotations=_PAGE_ACT)
async def browser_press(profile: str, key: str, selector: str | None = None) -> dict:
    """Press a key (Enter, Tab, Escape, ArrowDown, Control+A...), optionally on an element.
    Blocked on protected anti-detect domains (muse.ai, meta.com, reCAPTCHA)."""
    return await _arequest("POST", _page(profile, "press"), {"key": key, "selector": selector})


@mcp.tool(annotations=_PAGE_ACT)
async def browser_click_at(profile: str, x: float, y: float) -> dict:
    """Click viewport coordinates read from a (non-full-page) browser_screenshot.
    Blocked on protected anti-detect domains (muse.ai, meta.com, reCAPTCHA)."""
    return await _arequest("POST", _page(profile, "click-at"), {"x": x, "y": y})


@mcp.tool(annotations=_PAGE_READ)
async def browser_wait_for(profile: str, selector: str, timeout_ms: int = 15_000) -> dict:
    """Wait until an element matching the selector appears."""
    return await _arequest(
        "POST",
        _page(profile, "wait"),
        {"selector": selector, "timeout": timeout_ms},
    )


@mcp.tool(annotations=_PAGE_READ)
async def browser_screenshot(profile: str, full_page: bool = False) -> Image:
    """Screenshot of the active tab as a PNG image."""
    result = await _arequest(
        "GET",
        _page(profile, "screenshot"),
        query={"format": "json", "full_page": str(full_page).lower()},
    )
    return Image(data=base64.b64decode(result["png_base64"]), format="png")


@mcp.tool(annotations=_PAGE_ACT)
async def browser_evaluate(profile: str, script: str) -> dict:
    """Run JavaScript in the active tab and return the JSON result, e.g. 'document.title'.
    Blocked on protected anti-detect domains (muse.ai, meta.com, reCAPTCHA)."""
    return await _arequest("POST", _page(profile, "evaluate"), {"script": script})


@mcp.tool(annotations=_PAGE_READ)
async def browser_tabs(profile: str) -> dict:
    """List the profile's open tabs and which one is active."""
    return await _arequest("GET", _page(profile, "tabs"))


@mcp.tool(annotations=_PAGE_ACT)
async def browser_tab_new(profile: str, url: str | None = None) -> dict:
    """Open a new tab (optionally at a URL) and make it active."""
    return await _arequest("POST", _page(profile, "tabs"), {"url": url})


@mcp.tool(annotations=_PAGE_ACT)
async def browser_tab_select(profile: str, index: int) -> dict:
    """Make the tab at `index` (from browser_tabs) the active one."""
    return await _arequest("POST", _page(profile, f"tabs/{index}/select"))


@mcp.tool(annotations=_PAGE_ACT)
async def browser_tab_close(profile: str, index: int) -> dict:
    """Close the tab at `index`. The last tab cannot be closed; use stop_profile."""
    return await _arequest("DELETE", _page(profile, f"tabs/{index}"))


def main() -> None:
    mcp.run(transport="stdio")
