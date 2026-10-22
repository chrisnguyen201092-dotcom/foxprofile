import asyncio
import re

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

from ..core.strings import get_string
from .proxy_parser import parse_proxy

_CREDENTIALS = re.compile(r"//[^/@\s]+@")


def _redact(text: str) -> str:
    return _CREDENTIALS.sub("//***@", text)


def _check_via_camoufox_ip(proxy_str: str) -> tuple[bool, str] | None:
    try:
        from camoufox.ip import public_ip

        from ..services.proxy.geo_check import requests_proxy_url

        lookup = getattr(public_ip, "__wrapped__", public_ip)
        ip = lookup(requests_proxy_url(proxy_str))
        if ip:
            return True, get_string("proxy_check_success", ip=ip)
    except Exception:
        pass
    return None


async def check_proxy(proxy_str: str, timeout: int = 10) -> tuple[bool, str]:
    if not AIOHTTP_AVAILABLE:
        return True, get_string("proxy_check_skipped")

    proxy_config = parse_proxy(proxy_str)
    if not proxy_config:
        return False, get_string("proxy_check_failed", reason="Invalid proxy format")

    proxy_url = proxy_config["server"]
    scheme = proxy_url.split("://", 1)[0].lower()
    if "username" in proxy_config:
        _, rest = proxy_url.split("://", 1)
        password = proxy_config.get("password", "")
        proxy_url = f"{scheme}://{proxy_config['username']}:{password}@{rest}"

    # aiohttp does not natively support socks4/socks5 without extra plugins;
    # use Camoufox's requests-based lookup for SOCKS proxies.
    if scheme.startswith("socks"):
        res = await asyncio.to_thread(_check_via_camoufox_ip, proxy_str)
        if res is not None:
            return res
        return False, get_string("proxy_check_failed", reason="SOCKS connection failed")

    timeout_obj = aiohttp.ClientTimeout(total=timeout)
    last_err = "Connection failed"
    try:
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            # 1) Cloudflare trace (supports both IPv4 & IPv6 dual-stack, fast)
            try:
                async with session.get(
                    "https://www.cloudflare.com/cdn-cgi/trace",
                    proxy=proxy_url,
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        kv = dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln)
                        if ip := kv.get("ip"):
                            loc = kv.get("loc")
                            label = f"{ip} ({loc})" if loc else ip
                            return True, get_string("proxy_check_success", ip=label)
                    else:
                        last_err = f"HTTP {resp.status}"
            except Exception as e:
                last_err = _redact(str(e) or type(e).__name__)

            # 2) api64.ipify.org (supports both IPv4 & IPv6)
            try:
                async with session.get(
                    "https://api64.ipify.org?format=json",
                    proxy=proxy_url,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if ip := data.get("ip"):
                            return True, get_string("proxy_check_success", ip=ip)
                    else:
                        last_err = f"HTTP {resp.status}"
            except Exception as e:
                last_err = _redact(str(e) or type(e).__name__)

            # 3) Fallback to Camoufox's multi-source IP resolver
            res = await asyncio.to_thread(_check_via_camoufox_ip, proxy_str)
            if res is not None:
                return res

            return False, get_string("proxy_check_failed", reason=last_err)
    except Exception as e:
        return False, get_string("proxy_check_failed", reason=_redact(str(e)))


def check_proxy_sync(proxy_str: str, timeout: int = 10) -> tuple[bool, str]:
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(check_proxy(proxy_str, timeout))
        finally:
            loop.close()
    except Exception as e:
        return False, get_string("proxy_check_failed", reason=_redact(str(e)))
