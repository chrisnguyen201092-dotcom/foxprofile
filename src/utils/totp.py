"""Offline RFC 6238 TOTP (2FA Authenticator) generator.

Computes 6-digit 2FA codes 100% locally in Python using stdlib `hmac` + `hashlib.sha1`
without any network requests or browser DOM interaction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
import urllib.parse

_BASE32_CLEAN_RE = re.compile(r"[\s\-]+")
_BASE32_VALID_RE = re.compile(r"^[A-Z2-7]+$")


def extract_totp_params(raw: str | None) -> tuple[str, int, int]:
    """Return `(normalized_base32_secret, digits, period)` from a secret or `otpauth://` URI.

    Returns `("", 6, 30)` if the input is empty or not a valid Base32 TOTP secret.
    """
    if not raw or not isinstance(raw, str):
        return "", 6, 30
    s = raw.strip()
    if not s:
        return "", 6, 30

    digits = 6
    period = 30
    if s.lower().startswith("otpauth://"):
        try:
            parsed = urllib.parse.urlsplit(s)
            qs = urllib.parse.parse_qs(parsed.query)
            secret_list = qs.get("secret") or qs.get("SECRET") or []
            s = secret_list[0] if secret_list else ""
            if "digits" in qs and qs["digits"]:
                d_val = int(qs["digits"][0])
                if d_val in (6, 7, 8):
                    digits = d_val
            if "period" in qs and qs["period"]:
                p_val = int(qs["period"][0])
                if 10 <= p_val <= 120:
                    period = p_val
        except Exception:
            return "", 6, 30

    cleaned = _BASE32_CLEAN_RE.sub("", s).rstrip("=").upper()
    if not cleaned or not _BASE32_VALID_RE.match(cleaned):
        return "", 6, 30

    padded = cleaned + "=" * ((8 - len(cleaned) % 8) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
        if not key:
            return "", 6, 30
    except Exception:
        return "", 6, 30

    return cleaned, digits, period


def normalize_totp_secret(raw: str | None) -> str:
    """Return the normalized uppercase Base32 secret, or `""` if invalid."""
    secret, _, _ = extract_totp_params(raw)
    return secret


def generate_totp(
    secret_or_uri: str | None,
    timestamp: float | None = None,
) -> tuple[str, int] | None:
    """Generate `(otp_code, seconds_remaining)` for a Base32 secret or `otpauth://` URI."""
    secret, digits, period = extract_totp_params(secret_or_uri)
    if not secret:
        return None
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
    except Exception:
        return None

    now = time.time() if timestamp is None else float(timestamp)
    counter = int(now // period)
    rem = int(period - (int(now) % period))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    code_int = binary % (10**digits)
    return f"{code_int:0{digits}d}", rem
