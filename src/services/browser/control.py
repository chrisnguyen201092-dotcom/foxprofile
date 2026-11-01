"""Remote control of a running profile's pages.

runner.py starts `serve()` next to the browser. It listens on 127.0.0.1 on a
random port, requires a random per-launch token, and announces itself on
stdout as "CONTROL:<port>:<token>" so the launcher can forward API calls.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import math
import os
import random
import re
import secrets
from typing import Any
from urllib.parse import unquote, urlparse

from aiohttp import web

ALLOWED_SCHEMES = ("http", "https", "about")
MAX_FULL_PAGE_HEIGHT = 16_384
MAX_TEXT = 40_000
TOKEN_HEADER = "X-Control-Token"

PROTECTED_ANTI_DETECT_DOMAINS = (
    "muse.ai",
    "metaaivm.com",
    "hatch.io",
    "meta.com",
    "meta.ai",
    "facebook.com",
    "instagram.com",
    "challenges.cloudflare.com",
    "accounts.google.com",
    "login.live.com",
    "account.live.com",
)

_FAST_BIGRAMS = frozenset(
    {
        "th",
        "he",
        "in",
        "er",
        "an",
        "re",
        "on",
        "at",
        "en",
        "nd",
        "ti",
        "es",
        "or",
        "te",
        "of",
        "ed",
        "is",
        "it",
        "al",
        "ar",
        "st",
        "to",
        "nt",
        "ng",
    }
)


class ControlError(Exception):
    pass


_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(.*)$", re.S)


def _bezier_trajectory(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int = 8,
    rng: random.Random | None = None,
) -> list[tuple[float, float]]:
    """Generate a smooth cubic Bezier mouse path with ease-in-out timing."""
    r = rng or random
    x0, y0 = float(start[0]), float(start[1])
    x3, y3 = float(end[0]), float(end[1])
    dx, dy = x3 - x0, y3 - y0
    dist = math.hypot(dx, dy)
    if steps <= 1 or dist < 2.0:
        return [(x3, y3)]

    spread = min(80.0, max(6.0, dist * 0.22))
    # Two control points at ~30% and ~70% along the segment with lateral arc offset
    x1 = x0 + dx * 0.30 + r.uniform(-spread, spread)
    y1 = y0 + dy * 0.30 + r.uniform(-spread, spread)
    x2 = x0 + dx * 0.70 + r.uniform(-spread * 0.6, spread * 0.6)
    y2 = y0 + dy * 0.70 + r.uniform(-spread * 0.6, spread * 0.6)

    pts: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        u = i / steps
        # Smoothstep ease-in-out
        t = u * u * (3.0 - 2.0 * u)
        mt = 1.0 - t
        px = (mt**3) * x0 + 3.0 * (mt**2) * t * x1 + 3.0 * mt * (t**2) * x2 + (t**3) * x3
        py = (mt**3) * y0 + 3.0 * (mt**2) * t * y1 + 3.0 * mt * (t**2) * y2 + (t**3) * y3
        pts.append((round(px, 2), round(py, 2)))
    pts[-1] = (x3, y3)
    return pts


def _human_keystroke_delays(
    text: str,
    base_ms: float = 38.0,
    rng: random.Random | None = None,
) -> list[float]:
    """Compute realistic per-character inter-keystroke delays (ms) with bigram & punctuation cadence."""
    r = rng or random
    delays: list[float] = []
    prev = ""
    for ch in text:
        mult = r.uniform(0.78, 1.28)
        pair = (prev + ch).lower()
        if pair in _FAST_BIGRAMS:
            mult *= r.uniform(0.62, 0.82)
        elif ch in " .,!?;:\n":
            mult *= r.uniform(1.35, 1.95)
        elif ch.isupper() or ch.isdigit():
            mult *= r.uniform(1.12, 1.40)
        delays.append(round(max(12.0, base_ms * mult), 2))
        prev = ch
    return delays


def is_protected_anti_detect_url(url: str) -> tuple[bool, str]:
    """Return (is_protected, matched_domain_or_reason) for sensitive anti-detect URLs."""
    if not url:
        return False, ""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().strip(".")
        path = (parsed.path or "").lower()
    except ValueError:
        return False, ""
    if not host:
        return False, ""
    for dom in PROTECTED_ANTI_DETECT_DOMAINS:
        if host == dom or host.endswith("." + dom):
            return True, dom
    if "/recaptcha" in path or "turnstile" in path:
        return True, f"{host}{path}"
    return False, ""


def _check_url(url: str) -> str:
    url = url.strip()
    # "%66ile:" or "java%73cript:" would miss _SCHEME and get https:// prepended;
    # decode a percent-encoded scheme and check it like any other.
    colon = url.find(":")
    slash = url.find("/")
    if colon != -1 and "%" in url[:colon] and (slash == -1 or colon < slash):
        decoded_scheme = unquote(url[:colon]).lower()
        if decoded_scheme not in ALLOWED_SCHEMES:
            raise ControlError(f"URL scheme '{decoded_scheme}' is not allowed")

    match = _SCHEME.match(url)
    # "localhost:8080/x" looks like a scheme but is host:port; bare hosts get https.
    if not match or re.match(r"\d+(/|$)", match.group(2)):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError as e:  # e.g. an unclosed IPv6 bracket: "[::1"
        raise ControlError(f"Malformed URL: {e}") from e
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https") and not host:
        # Older Python versions parse "https://[]" without raising.
        raise ControlError("Malformed URL: no host")
    if scheme not in ALLOWED_SCHEMES:
        # file:, view-source:, chrome: etc. would let a remote caller read
        # local files or browser internals through the snapshot.
        raise ControlError(f"URL scheme '{scheme}' is not allowed")
    if scheme == "about" and url.lower() != "about:blank":
        # about:config, about:logins etc. expose or change browser internals.
        raise ControlError("Only about:blank is allowed among about: pages")
    return url


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class PageController:
    """Executes control actions against a Playwright BrowserContext."""

    DOM_MUTATION_ACTIONS = frozenset(
        {"click", "type", "press", "click_at", "keyboard_type", "evaluate"}
    )

    def __init__(self, context: Any) -> None:
        self._context = context
        self._current: Any = None
        self._mouse_pos: tuple[float, float] = (120.0, 120.0)

    # --- page selection ---------------------------------------------------

    def _open_pages(self) -> list[Any]:
        return [p for p in self._context.pages if not p.is_closed()]

    async def _page(self) -> Any:
        pages = self._open_pages()
        if self._current is not None and not self._current.is_closed():
            return self._current
        if pages:
            self._current = pages[-1]
            return self._current
        self._current = await self._context.new_page()
        return self._current

    async def _guard_dom_action(self, action: str) -> Any:
        page = await self._page()
        if os.getenv("FOXPROFILE_ALLOW_PROTECTED_DOM") == "1":
            return page
        blocked, reason = is_protected_anti_detect_url(getattr(page, "url", "") or "")
        if blocked:
            raise ControlError(
                f"Anti-Detect Safety Guard: DOM automation '{action}' is blocked on "
                f"protected domain '{reason}' to prevent bot detection and protect "
                "_GRECAPTCHA / Cloudflare trust scores. Please interact manually in the browser."
            )
        return page

    async def _describe(self, page: Any) -> dict[str, Any]:
        return {"url": page.url, "title": await page.title()}

    # --- actions ----------------------------------------------------------

    async def navigate(self, url: str, wait_until: str = "load") -> dict[str, Any]:
        page = await self._page()
        await page.goto(_check_url(url), wait_until=wait_until)
        return await self._describe(page)

    async def back(self) -> dict[str, Any]:
        page = await self._page()
        await page.go_back()
        return await self._describe(page)

    async def snapshot(self, max_chars: int = MAX_TEXT) -> dict[str, Any]:
        """Accessibility tree of the page: roles, names and text, compact."""
        page = await self._page()
        aria = await page.locator("body").aria_snapshot()
        aria, truncated = _truncate(aria, max_chars)
        return {**await self._describe(page), "snapshot": aria, "truncated": truncated}

    async def text(self, selector: str = "body", max_chars: int = MAX_TEXT) -> dict[str, Any]:
        page = await self._page()
        content = await page.locator(selector).first.inner_text()
        content, truncated = _truncate(content, max_chars)
        return {"text": content, "truncated": truncated}

    async def click(self, selector: str, timeout: int = 10_000) -> dict[str, Any]:
        page = await self._guard_dom_action("click")
        await page.locator(selector).first.click(timeout=timeout)
        return await self._describe(page)

    async def type(
        self,
        selector: str,
        text: str,
        submit: bool = False,
        clear: bool = True,
        timeout: int = 10_000,
    ) -> dict[str, Any]:
        page = await self._guard_dom_action("type")
        field = page.locator(selector).first
        if clear:
            await field.fill(text, timeout=timeout)
        else:
            await field.press_sequentially(text, timeout=timeout)
        if submit:
            await field.press("Enter")
        return await self._describe(page)

    async def press(self, key: str, selector: str | None = None) -> dict[str, Any]:
        page = await self._guard_dom_action("press")
        if selector:
            await page.locator(selector).first.press(key)
        else:
            await page.keyboard.press(key)
        return await self._describe(page)

    async def click_at(self, x: float, y: float, human: bool = True) -> dict[str, Any]:
        """Click viewport coordinates, moving along a human-like Bezier trajectory first."""
        page = await self._guard_dom_action("click_at")
        mouse = getattr(page, "mouse", None)
        if human and mouse is not None and hasattr(mouse, "move"):
            for px, py in _bezier_trajectory(self._mouse_pos, (x, y), steps=8):
                await mouse.move(px, py)
                await asyncio.sleep(0.002)
        self._mouse_pos = (float(x), float(y))
        if mouse is not None:
            sig = inspect.signature(mouse.click)
            if human and "delay" in sig.parameters:
                await mouse.click(x, y, delay=random.randint(25, 65))
            else:
                await mouse.click(x, y)
        return await self._describe(page)

    async def keyboard_type(self, text: str, human: bool = True) -> dict[str, Any]:
        """Type into whatever element has focus with human-like inter-keystroke cadence."""
        page = await self._guard_dom_action("keyboard_type")
        if human and 0 < len(text) <= 200:
            for ch, delay_ms in zip(text, _human_keystroke_delays(text), strict=True):
                await page.keyboard.type(ch)
                await asyncio.sleep(min(delay_ms, 15.0) / 1000.0)
        else:
            await page.keyboard.type(text)
        return await self._describe(page)

    async def wait_for(self, selector: str, timeout: int = 15_000) -> dict[str, Any]:
        page = await self._page()
        await page.locator(selector).first.wait_for(timeout=timeout)
        return await self._describe(page)

    async def screenshot(self, full_page: bool = False) -> dict[str, Any]:
        page = await self._page()
        if full_page:
            height = await page.evaluate("() => document.documentElement.scrollHeight")
            if height > MAX_FULL_PAGE_HEIGHT:
                raise ControlError(
                    f"Page is {height}px tall; full-page screenshots are limited to "
                    f"{MAX_FULL_PAGE_HEIGHT}px. Scroll and take viewport screenshots instead.",
                )
        png = await page.screenshot(full_page=full_page, type="png")
        return {**await self._describe(page), "png_base64": base64.b64encode(png).decode()}

    async def evaluate(self, script: str) -> dict[str, Any]:
        page = await self._guard_dom_action("evaluate")
        return {"result": await page.evaluate(script)}

    async def tabs(self) -> dict[str, Any]:
        current = await self._page()
        pages = self._open_pages()
        return {
            "tabs": [
                {"index": i, "url": p.url, "title": await p.title(), "active": p is current}
                for i, p in enumerate(pages)
            ],
        }

    async def tab_new(self, url: str | None = None) -> dict[str, Any]:
        self._current = await self._context.new_page()
        if url:
            await self._current.goto(_check_url(url))
        return await self.tabs()

    async def tab_select(self, index: int) -> dict[str, Any]:
        pages = self._open_pages()
        if not 0 <= index < len(pages):
            raise ControlError(f"No tab at index {index}")
        self._current = pages[index]
        await self._current.bring_to_front()
        return await self.tabs()

    async def tab_close(self, index: int) -> dict[str, Any]:
        pages = self._open_pages()
        if not 0 <= index < len(pages):
            raise ControlError(f"No tab at index {index}")
        if len(pages) == 1:
            # Closing the last tab closes the profile's browser.
            raise ControlError("Cannot close the last tab; stop the profile instead")
        await pages[index].close()
        self._current = None
        return await self.tabs()

    async def cookies_export(self) -> dict[str, Any]:
        raw = await self._context.cookies()
        return {"cookies": raw}

    async def cookies_import(self, cookies: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(cookies, list):
            raise ControlError("cookies must be a list")
        await self._context.add_cookies(cookies)
        return {"imported": len(cookies)}

    ACTIONS = (
        "navigate",
        "back",
        "snapshot",
        "text",
        "click",
        "type",
        "press",
        "click_at",
        "keyboard_type",
        "wait_for",
        "screenshot",
        "evaluate",
        "tabs",
        "tab_new",
        "tab_select",
        "tab_close",
        "cookies_export",
        "cookies_import",
    )

    async def dispatch(self, action: str, params: dict[str, Any]) -> Any:
        if action not in self.ACTIONS:
            raise ControlError(f"Unknown action '{action}'")
        try:
            return await getattr(self, action)(**params)
        except TypeError as e:
            raise ControlError(f"Invalid parameters for '{action}': {e}") from e


def build_app(controller: PageController, token: str) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            result = await controller.dispatch(body.get("action", ""), body.get("params") or {})
        except ControlError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # Playwright timeouts, navigation errors, JS errors
            message = " ".join(str(e).split())
            if "Call log:" in message:
                message = message.split("Call log:", 1)[0].strip()
            return web.json_response(
                {"ok": False, "error": f"{type(e).__name__}: {message[:500]}"},
                status=422,
            )
        return web.json_response({"ok": True, "result": result})

    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.router.add_post("/action", handle)
    return app


async def serve(context: Any) -> tuple[web.AppRunner, int, str]:
    """Start the control server for a context. Returns (runner, port, token)."""
    token = secrets.token_urlsafe(24)
    runner = web.AppRunner(build_app(PageController(context), token), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port, token
