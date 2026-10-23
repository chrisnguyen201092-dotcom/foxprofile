"""Where do GeoIP databases place a profile's exit IP, and does the browser agree?

Camoufox looks up the exit IP through the proxy on every launch and derives the
timezone and locale from its own GeoIP database. Sites use other databases, so
this check asks Cloudflare, ipinfo and ip-api about the same exit and reports
where they disagree with the timezone / locale the profile will present.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from ...core.strings import get_string
from ...utils.proxy_parser import parse_proxy

# (connect, read) per request, and the overall budget for one check: the exit
# IP lookup walks up to six services and must not hold a UI or API call for a minute.
_TIMEOUT = (5, 8)
_DEADLINE = 20
_CREDENTIALS = re.compile(r"//[^/@\s]+@")
_ROOT_CAUSE = re.compile(
    r"(\[Errno -?\d+\][^'\")]*|Tunnel connection failed: [^'\")]*|(?:Read|Connect) timed out|Name or service not known|nodename nor servname provided)"
)


@dataclass
class SourceResult:
    source: str
    ip: str | None = None
    country: str | None = None
    city: str | None = None
    timezone: str | None = None
    hosting: bool | None = None
    isp: str | None = None
    error: str | None = None


@dataclass
class GeoWarning:
    code: str
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def message(self) -> str:
        return get_string(f"geo_warn_{self.code}", **self.params)


@dataclass
class GeoCheckResult:
    exit_ip: str | None
    # What Camoufox derives from the exit IP (its own GeoIP database).
    country: str | None
    auto_timezone: str | None
    suggested_locale: str | None
    # What the browser will actually present: the pinned value, else the automatic one.
    timezone: str | None
    timezone_pinned: bool
    locale: str | None
    locale_pinned: bool
    sources: list[SourceResult]
    warnings: list[GeoWarning]
    error: str | None = None


# requests resolves hostnames locally for socks5:// and socks4://, so every
# lookup would reach the machine's own DNS resolver (its ISP) outside the proxy.
# The h/a variants make the proxy resolve them, as the browser itself does.
_REMOTE_DNS_SCHEMES = {"socks5": "socks5h", "socks4": "socks4a"}


def requests_proxy_url(proxy: str | None) -> str | None:
    """The profile's proxy as a requests URL whose DNS lookups go through the proxy."""
    cfg = parse_proxy(proxy or "")
    if not cfg:
        return None
    from camoufox.ip import Proxy

    url = Proxy(**cfg).as_string()
    scheme, sep, rest = url.partition("://")
    return f"{_REMOTE_DNS_SCHEMES.get(scheme.lower(), scheme)}{sep}{rest}"


def _requests_proxies(proxy: str | None) -> dict[str, str] | None:
    url = requests_proxy_url(proxy)
    return {"http": url, "https": url} if url else None


def _get(url: str, proxies: dict[str, str] | None) -> Any:
    import requests

    resp = requests.get(url, proxies=proxies, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp


def _exit_ip(proxy: str | None) -> str:
    """The exit IP exactly as the runner resolves it at launch (IPv4 preferred)."""
    from camoufox.ip import public_ip

    # public_ip is lru_cached for the whole process; a check must be fresh.
    lookup = getattr(public_ip, "__wrapped__", public_ip)
    return lookup(requests_proxy_url(proxy))


def _camoufox_geo(ip: str) -> tuple[str, str, str, float]:
    """(country, timezone, most likely locale, its share) from Camoufox's database."""
    from camoufox.geolocation import get_geolocation
    from camoufox.locales import SELECTOR, normalize_locale

    geo = get_geolocation(ip)
    country = geo.locale.region or ""
    try:
        # Private API: the language shares Camoufox draws the automatic locale from.
        languages, probabilities = SELECTOR._load_territory_data(country)
    except Exception:
        return country, geo.timezone, geo.locale.as_string, 1.0
    top = int(probabilities.argmax())
    language = str(languages[top]).replace("_", "-")
    locale = normalize_locale(f"{language}-{country}").as_string
    return country, geo.timezone, locale, float(probabilities[top])


_DETAIL_IP_RE = re.compile(r"IP:\s*([0-9a-fA-F:\.]+)")
_DETAIL_COUNTRY_RE = re.compile(r"\(([A-Za-z]{2})(?:\b|[^A-Za-z])")
_DC_KEYWORDS = (
    "choopa",
    "vultr",
    "constant company",
    "digitalocean",
    "amazon",
    "aws",
    "google cloud",
    "google llc",
    "microsoft",
    "azure",
    "hetzner",
    "ovh",
    "contabo",
    "linode",
    "akamai",
    "leaseweb",
    "datacamp",
    "cdn77",
    "tzulo",
    "psychz",
    "velia",
    "hostinger",
    "m247",
    "cogent",
    "zenlayer",
    "frantech",
    "buyvm",
    "multacom",
    "colocrossing",
    "sharktech",
    "ionos",
    "kamatera",
    "upcloud",
    "scaleway",
    "oracle",
    "alibaba",
    "tencent",
    "hosting",
    "datacenter",
    "data center",
    "server",
    "vps",
    "cloud",
    "dedipath",
    "nocix",
    "wholesale internet",
    "quadranet",
    "performive",
    "hivelocity",
)
_ISP_MEMO: dict[str, tuple[str, str]] = {}


def classify_isp_type(isp_or_org: str | None, hosting_flag: bool | None = None) -> str:
    """Classify an IP as `'DC'` (Datacenter/Hosting) or `'ISP'` (Residential/ISP), or `''`."""
    clean = (isp_or_org or "").strip()
    low = clean.lower()
    if hosting_flag is True:
        return "DC"
    if low and any(k in low for k in _DC_KEYWORDS):
        return "DC"
    if hosting_flag is False and clean:
        return "ISP"
    if clean:
        return "ISP"
    return ""


def lookup_ip_isp(ip: str | None, timeout: float = 3.5) -> tuple[str, str]:
    """Return `(isp_name, ip_type)` for `ip` (`ip_type` is `'DC'`, `'ISP'`, or `''`)."""
    clean_ip = (ip or "").strip()
    if not clean_ip:
        return "", ""
    cached = _ISP_MEMO.get(clean_ip)
    if cached is not None:
        return cached
    try:
        import requests

        resp = requests.get(
            f"http://ip-api.com/json/{clean_ip}?fields=status,isp,org,hosting",
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("status") == "success":
                raw_isp = str(data.get("isp") or data.get("org") or "").strip()
                hosting = data.get("hosting")
                ip_type = classify_isp_type(
                    raw_isp,
                    hosting if isinstance(hosting, bool) else None,
                )
                res = (raw_isp, ip_type)
                _ISP_MEMO[clean_ip] = res
                return res
    except Exception:
        pass
    return "", ""


def extract_ip_from_check_detail(detail: str) -> str | None:
    """Extract the raw IPv4/IPv6 exit IP from a proxy check result string."""
    if not detail:
        return None
    m = _DETAIL_IP_RE.search(detail)
    if m:
        return m.group(1).strip()
    return None


def extract_country_from_check_detail(detail: str) -> str:
    """Extract a 2-letter country code (e.g. 'US') from a proxy check result string."""
    if not detail:
        return ""
    m = _DETAIL_COUNTRY_RE.search(detail)
    if m:
        code = m.group(1).strip().upper()
        if code not in ("XX", "T1"):
            return code
    return ""


def derive_ip_metadata(
    ip: str | None,
    detail: str = "",
) -> tuple[str, str | None, str | None]:
    """Derive (country_code, timezone, locale) from an exit IP and check detail string."""
    country = extract_country_from_check_detail(detail)
    if not ip:
        return country, None, None
    try:
        from ...utils.validation import validate_locale, validate_timezone

        cam_country, tz, loc, _share = _camoufox_geo(ip.strip())
        if not country and cam_country and cam_country.upper() not in ("XX", "T1"):
            country = cam_country.strip().upper()
        clean_tz = tz if tz and validate_timezone(tz)[0] else None
        clean_loc = loc if loc and validate_locale(loc)[0] else None
        return country, clean_tz, clean_loc
    except Exception:
        return country, None, None


def derive_geo_from_ip(ip: str | None) -> tuple[str | None, str | None]:
    """Derive validated (timezone, locale) from an exit IP using Camoufox's offline GeoIP DB."""
    _country, tz, loc = derive_ip_metadata(ip)
    return tz, loc


def _cloudflare(proxies: dict[str, str] | None) -> SourceResult:
    text = _get("https://www.cloudflare.com/cdn-cgi/trace", proxies).text
    data = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return SourceResult("cloudflare", ip=data.get("ip"), country=data.get("loc"))


def _ipinfo(proxies: dict[str, str] | None) -> SourceResult:
    data = _get("https://ipinfo.io/json", proxies).json()
    org = str(data.get("org") or "").strip()
    # Strip leading ASN prefix like "AS20473 " if present
    isp_clean = re.sub(r"^AS\d+\s+", "", org).strip() if org else None
    return SourceResult(
        "ipinfo",
        ip=data.get("ip"),
        country=data.get("country"),
        city=data.get("city"),
        timezone=data.get("timezone"),
        isp=isp_clean or None,
    )


def _ipapi(proxies: dict[str, str] | None) -> SourceResult:
    # The free ip-api endpoint is HTTP only.
    fields = "status,message,query,countryCode,city,timezone,isp,org,hosting"
    data = _get(f"http://ip-api.com/json?fields={fields}", proxies).json()
    if data.get("status") != "success":
        return SourceResult("ip-api", error=str(data.get("message") or data))
    isp_val = str(data.get("isp") or data.get("org") or "").strip() or None
    return SourceResult(
        "ip-api",
        ip=data.get("query"),
        country=data.get("countryCode"),
        city=data.get("city"),
        timezone=data.get("timezone"),
        hosting=bool(data.get("hosting")),
        isp=isp_val,
    )


_SOURCES = {"cloudflare": _cloudflare, "ipinfo": _ipinfo, "ip-api": _ipapi}


def _short_error(exc: BaseException) -> str:
    """The innermost cause (refused, timed out, 407...), without proxy credentials."""
    while True:
        inner = exc.__cause__ or exc.__context__
        reason = getattr(exc.args[0], "reason", None) if exc.args else None
        if isinstance(reason, BaseException):
            inner = inner or reason
        if inner is None or inner is exc:
            break
        exc = inner
    text = " ".join(f"{type(exc).__name__}: {exc}".split()) if str(exc) else type(exc).__name__
    # requests flattens the cause into a long message; keep only the useful tail.
    found = _ROOT_CAUSE.findall(text)
    if found:
        text = f"{type(exc).__name__}: {found[-1].strip()}"
    text = _CREDENTIALS.sub("//***@", text)
    return text if len(text) <= 200 else text[:200] + "..."


def _query_source(name: str, proxies: dict[str, str] | None) -> SourceResult:
    try:
        return _SOURCES[name](proxies)
    except Exception as e:
        return SourceResult(name, error=_short_error(e))


def _is_v6(ip: str) -> bool:
    return ":" in ip


def compare(
    exit_ip: str | None,
    country: str | None,
    timezone: str | None,
    locale: str | None,
    locale_pinned: bool,
    sources: list[SourceResult],
) -> list[GeoWarning]:
    """Everything a site could see that does not line up with the profile."""
    warnings: list[GeoWarning] = []
    for s in sources:
        if s.error:
            continue
        # Cloudflare reports XX (unknown) and T1 (Tor) instead of a country.
        if country and s.country and s.country.upper() not in (country, "XX", "T1"):
            warnings.append(
                GeoWarning(
                    "country_mismatch", {"source": s.source, "got": s.country, "expected": country}
                )
            )
        if timezone and s.timezone and s.timezone != timezone:
            warnings.append(
                GeoWarning(
                    "timezone_mismatch",
                    {"source": s.source, "got": s.timezone, "expected": timezone},
                )
            )
        if s.hosting:
            warnings.append(GeoWarning("hosting", {"source": s.source}))
        # Dual-stack proxy: Camoufox derives the timezone and WebRTC address from
        # the IPv4 exit, but the browser reaches sites over IPv6 (or vice versa).
        if s.source == "cloudflare" and exit_ip and s.ip and _is_v6(s.ip) != _is_v6(exit_ip):
            warnings.append(GeoWarning("ip_differs", {"exit_ip": exit_ip, "seen_ip": s.ip}))
    region = (locale or "").rsplit("-", 1)[-1]
    # Numeric regions such as es-419 (Latin America) span several countries.
    if locale_pinned and country and region and not region.isdigit() and region != country:
        warnings.append(GeoWarning("locale_country", {"locale": locale, "country": country}))
    return warnings


def check_geo(
    proxy: str | None,
    timezone: str | None = None,
    locale: str | None = None,
) -> GeoCheckResult:
    """Look up the exit IP through `proxy` and compare GeoIP sources (blocking, <= ~20s)."""
    proxies = _requests_proxies(proxy)
    pool = ThreadPoolExecutor(max_workers=len(_SOURCES) + 1)
    deadline = time.monotonic() + _DEADLINE

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    try:
        exit_future = pool.submit(_exit_ip, proxy)
        source_futures = {name: pool.submit(_query_source, name, proxies) for name in _SOURCES}
        sources = []
        for name, future in source_futures.items():
            try:
                sources.append(future.result(timeout=remaining()))
            except FutureTimeout:
                sources.append(SourceResult(name, error="timed out"))
        try:
            exit_ip: str | None = exit_future.result(timeout=remaining())
            error = None
        except FutureTimeout:
            exit_ip, error = None, f"no answer within {_DEADLINE}s"
        except Exception as e:
            exit_ip, error = None, _short_error(e)
    finally:
        # Do not wait for a lookup stuck on a dead proxy; it ends on its own timeout.
        pool.shutdown(wait=False, cancel_futures=True)

    country = auto_timezone = suggested_locale = None
    share = 0.0
    if exit_ip:
        try:
            country, auto_timezone, suggested_locale, share = _camoufox_geo(exit_ip)
        except Exception as e:
            error = _short_error(e)

    effective_tz = timezone or auto_timezone
    warnings = compare(exit_ip, country, effective_tz, locale, bool(locale), sources)
    if not locale and suggested_locale and share < 0.99:
        # Automatic locale is redrawn at random on every launch, weighted by
        # how many people in the country speak each language.
        warnings.append(
            GeoWarning(
                "locale_random", {"suggested": suggested_locale, "share": round(share * 100)}
            )
        )
    return GeoCheckResult(
        exit_ip=exit_ip,
        country=country,
        auto_timezone=auto_timezone,
        suggested_locale=suggested_locale,
        timezone=effective_tz,
        timezone_pinned=bool(timezone),
        locale=locale or None,
        locale_pinned=bool(locale),
        sources=sources,
        warnings=warnings,
        error=error,
    )
