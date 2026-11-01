import asyncio

import pytest

from src.services.browser.control import (
    ControlError,
    PageController,
    _bezier_trajectory,
    _check_url,
    _human_keystroke_delays,
    is_protected_anti_detect_url,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("example.com", "https://example.com"),
        ("http://a.vn/x", "http://a.vn/x"),
        ("about:blank", "about:blank"),
        ("localhost:8080/x", "https://localhost:8080/x"),
    ],
)
def test_allowed_urls(url, expected):
    assert _check_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "view-source:https://a.com",
        "javascript:alert(1)",
        "chrome://x",
        "about:config",
        "about:logins",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_blocked_urls(url):
    with pytest.raises(ControlError):
        _check_url(url)


class FakeMouse:
    def __init__(self):
        self.moves: list[tuple[float, float]] = []
        self.clicks: list[tuple[float, float, int | None]] = []

    async def move(self, x: float, y: float):
        self.moves.append((x, y))

    async def click(self, x: float, y: float, delay: int | None = None):
        self.clicks.append((x, y, delay))


class FakeKeyboard:
    def __init__(self):
        self.typed: list[str] = []
        self.pressed: list[str] = []

    async def type(self, text: str):
        self.typed.append(text)

    async def press(self, key: str):
        self.pressed.append(key)


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()

    def is_closed(self):
        return self.closed

    async def title(self):
        return "T"

    async def goto(self, url, wait_until="load"):
        self.url = url

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        pass

    async def evaluate(self, script):
        return f"eval:{script}"


class FakeContext:
    def __init__(self, n=1):
        self.pages = [FakePage(f"https://p{i}.test/") for i in range(n)]

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


def run(coro):
    return asyncio.run(coro)


def test_dispatch_rejects_unknown_action_and_bad_params():
    c = PageController(FakeContext())
    with pytest.raises(ControlError, match="Unknown action"):
        run(c.dispatch("rm_rf", {}))
    with pytest.raises(ControlError, match="Invalid parameters"):
        run(c.dispatch("navigate", {"nope": 1}))


def test_navigate_uses_active_tab():
    ctx = FakeContext(2)
    c = PageController(ctx)
    result = run(c.dispatch("navigate", {"url": "example.com"}))
    assert result == {"url": "https://example.com", "title": "T"}
    assert ctx.pages[-1].url == "https://example.com"


def test_tabs_select_and_close():
    ctx = FakeContext(2)
    c = PageController(ctx)
    tabs = run(c.dispatch("tab_select", {"index": 0}))["tabs"]
    assert [t["active"] for t in tabs] == [True, False]
    tabs = run(c.dispatch("tab_close", {"index": 0}))["tabs"]
    assert [t["url"] for t in tabs] == ["https://p1.test/"]
    with pytest.raises(ControlError, match="last tab"):
        run(c.dispatch("tab_close", {"index": 0}))
    with pytest.raises(ControlError, match="No tab"):
        run(c.dispatch("tab_select", {"index": 5}))


def test_protected_anti_detect_dom_guard():
    assert is_protected_anti_detect_url("https://muse.ai/chat")[0] is True
    assert is_protected_anti_detect_url("https://auth.meta.com/login")[0] is True
    assert is_protected_anti_detect_url("https://www.google.com/recaptcha/api2/anchor")[0] is True
    assert is_protected_anti_detect_url("https://www.cnn.com/us")[0] is False

    ctx = FakeContext(1)
    c = PageController(ctx)
    # Normal domain -> evaluate succeeds
    res = run(c.dispatch("evaluate", {"script": "1+1"}))
    assert res == {"result": "eval:1+1"}

    # Protected domain (muse.ai) -> DOM actions (click, type, evaluate, press) are blocked!
    ctx.pages[0].url = "https://muse.ai/thread/123"
    for act, params in (
        ("click", {"selector": "button"}),
        ("type", {"selector": "input", "text": "hi"}),
        ("press", {"key": "Enter"}),
        ("click_at", {"x": 10, "y": 20}),
        ("keyboard_type", {"text": "abc"}),
        ("evaluate", {"script": "document.title"}),
    ):
        with pytest.raises(ControlError, match="Anti-Detect Safety Guard"):
            run(c.dispatch(act, params))


def test_bezier_trajectory_and_human_click_at():
    pts = _bezier_trajectory((100.0, 100.0), (500.0, 350.0), steps=12)
    assert len(pts) == 12
    assert pts[-1] == (500.0, 350.0)
    # Ensure trajectory is curved (intermediate points deviate from exact straight line)
    deviations = [
        abs((x - 100.0) * (350.0 - 100.0) - (y - 100.0) * (500.0 - 100.0)) for x, y in pts[:-1]
    ]
    assert max(deviations) > 0.0

    ctx = FakeContext(1)
    c = PageController(ctx)
    res = run(c.dispatch("click_at", {"x": 420, "y": 280}))
    assert res == {"url": "https://p0.test/", "title": "T"}
    assert len(ctx.pages[0].mouse.moves) == 8
    assert ctx.pages[0].mouse.moves[-1] == (420.0, 280.0)
    assert len(ctx.pages[0].mouse.clicks) == 1
    cx, cy, click_delay = ctx.pages[0].mouse.clicks[0]
    assert (cx, cy) == (420, 280)
    assert 25 <= click_delay <= 65


def test_human_keystroke_delays_and_keyboard_type():
    delays = _human_keystroke_delays("Hello, world! 123")
    assert len(delays) == len("Hello, world! 123")
    assert all(12.0 <= d <= 150.0 for d in delays)
    # Variation must be non-zero (not robotic uniform cadence)
    assert len({round(d, 2) for d in delays}) > 3

    ctx = FakeContext(1)
    c = PageController(ctx)
    res = run(c.dispatch("keyboard_type", {"text": "Fox!\nGo"}))
    assert res == {"url": "https://p0.test/", "title": "T"}
    assert ctx.pages[0].keyboard.typed == list("Fox!\nGo")


# ---------------------------------------------------------------------------
# Percent-encoded scheme bypass (fix 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "%66ile:///C:/Windows/win.ini",  # decodes to file:///...
        "java%73cript:alert(1)",  # decodes to javascript:...
        "%76iew-source:https://a.com",  # decodes to view-source:...
    ],
)
def test_percent_encoded_blocked_schemes(url):
    """Percent-encoded disallowed schemes must be caught before scheme detection."""
    with pytest.raises(ControlError):
        _check_url(url)


@pytest.mark.parametrize("url", ["[", "[]", "[::1", "http://[::1"])
def test_malformed_url_is_a_control_error(url):
    with pytest.raises(ControlError):
        _check_url(url)
