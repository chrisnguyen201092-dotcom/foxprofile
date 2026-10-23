"""Offline RFC 6238 TOTP (2FA Authenticator) generator.

Computes 6-digit 2FA codes 100% locally in Python using stdlib `hmac` + `hashlib.sha1`
without any network requests or browser DOM interaction.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import html
import imaplib
import json
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from email import message_from_bytes
from email.header import decode_header

_BASE32_CLEAN_RE = re.compile(r"[\s\-]+")
_BASE32_VALID_RE = re.compile(r"^[A-Z2-7]+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
DEFAULT_OAUTH_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
_HTML_STYLE_SCRIPT_RE = re.compile(r"<(style|script|head)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_OTP_CONTEXT_AFTER_RE = re.compile(
    r"(?:code|mã|otp|pin|verification|confirm|security|passcode|xác\s*nhận|xác\s*minh|bảo\s*mật)"
    r"[^0-9a-zA-Z]{1,35}\b(\d{4,8})\b",
    re.IGNORECASE,
)
_OTP_CONTEXT_BEFORE_RE = re.compile(
    r"\b(\d{4,8})\b[^0-9a-zA-Z]{1,25}"
    r"(?:is\s+your|là\s+mã|verification\s+code|confirmation\s+code|security\s+code)",
    re.IGNORECASE,
)
_OTP_STANDALONE_6_8_RE = re.compile(r"\b(\d{5,8})\b")
_NO_OTP_SUBJECT_HINTS = (
    "new app(s) connected",
    "security info was added",
    "welcome to your new outlook.com",
)


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


def _looks_like_totp_token(token: str) -> bool:
    s = (token or "").strip()
    if not s:
        return False
    if s.lower().startswith("otpauth://"):
        return bool(normalize_totp_secret(s))
    if _UUID_RE.match(s):
        return False
    compact = _BASE32_CLEAN_RE.sub("", s).rstrip("=")
    if len(compact) < 12 or len(compact) > 64 or "@" in s:
        return False
    return bool(normalize_totp_secret(s))


def _looks_like_oauth_refresh_token(token: str) -> bool:
    s = (token or "").strip()
    if not s or "@" in s or " " in s or s.lower().startswith("otpauth://"):
        return False
    if s.startswith(("M.C", "M.R", "0.A", "1.A", "Ew", "eyJ")):
        return True
    return bool(len(s) >= 45 and not _looks_like_totp_token(s))


def parse_mail_credentials_line(raw: str | None) -> dict[str, str]:
    """Parse a delimited credential line (`mail|pass|token|client_id|recovery` or `mail|pass|2fa|recovery`).

    Supports `|`, Tab (`\\t`), `;`, or `:` delimiters (when an `@` email is present).
    Returns a dict with keys:
    `name`, `email`, `password`, `two_fa`, `recovery_email`, `mail_token`, `refresh_token`, `client_id`, `notes`.
    """
    out = {
        "name": "",
        "email": "",
        "password": "",
        "two_fa": "",
        "recovery_email": "",
        "mail_token": "",
        "refresh_token": "",
        "client_id": "",
        "notes": "",
    }
    if not raw or not isinstance(raw, str):
        return out
    line = raw.strip()
    if not line:
        return out

    if "|" in line:
        parts = [p.strip() for p in line.split("|")]
    elif "\t" in line:
        parts = [p.strip() for p in line.split("\t")]
    elif ";" in line:
        parts = [p.strip() for p in line.split(";")]
    elif ":" in line and "@" in line and not line.lower().startswith("otpauth://"):
        parts = [p.strip() for p in line.split(":")]
    else:
        if "@" in line:
            out["email"] = line
        elif _looks_like_oauth_refresh_token(line):
            out["refresh_token"] = line
            out["mail_token"] = line
        elif _looks_like_totp_token(line):
            out["two_fa"] = normalize_totp_secret(line)
        else:
            out["password"] = line
        return out

    if not parts:
        return out

    idx = 0
    if len(parts) >= 2 and "@" not in parts[0] and "@" in parts[1]:
        out["name"] = parts[0]
        out["email"] = parts[1]
        idx = 2
    elif "@" in parts[0]:
        out["email"] = parts[0]
        idx = 1
    else:
        email_pos = next((i for i, tok in enumerate(parts) if "@" in tok), -1)
        if email_pos == 1:
            out["name"] = parts[0]
            out["email"] = parts[1]
            idx = 2
        elif email_pos == 0:
            out["email"] = parts[0]
            idx = 1
        else:
            out["email"] = parts[0]
            idx = 1

    tail = [p for p in parts[idx:] if p != ""]
    if not tail:
        return out

    # First token after email is password (unless there's only 1 token and it's a 2FA URI or OAuth token)
    if len(tail) == 1 and tail[0].lower().startswith("otpauth://"):
        out["two_fa"] = normalize_totp_secret(tail[0])
        return out
    if len(tail) == 1 and _looks_like_oauth_refresh_token(tail[0]):
        out["refresh_token"] = tail[0]
        out["mail_token"] = tail[0]
        return out

    out["password"] = tail[0]
    extra_notes: list[str] = []
    fallback_recovery: list[str] = []
    for tok in tail[1:]:
        if "@" in tok and not out["recovery_email"]:
            out["recovery_email"] = tok
        elif _UUID_RE.match(tok) and not out["client_id"]:
            out["client_id"] = tok
        elif _looks_like_oauth_refresh_token(tok) and not out["refresh_token"]:
            out["refresh_token"] = tok
        elif _looks_like_totp_token(tok) and not out["two_fa"]:
            out["two_fa"] = normalize_totp_secret(tok)
        elif len(tok) <= 64 and " " not in tok:
            fallback_recovery.append(tok)
        else:
            extra_notes.append(tok)

    if not out["recovery_email"] and fallback_recovery:
        out["recovery_email"] = fallback_recovery.pop(0)
    extra_notes.extend(fallback_recovery)

    if out["refresh_token"] and out["client_id"]:
        out["mail_token"] = f"{out['refresh_token']}|{out['client_id']}"
    elif out["refresh_token"]:
        out["mail_token"] = out["refresh_token"]
    elif out["client_id"]:
        out["mail_token"] = out["client_id"]

    if extra_notes:
        out["notes"] = " | ".join(extra_notes)
    return out


def format_mail_credentials_line(
    email: str | None = None,
    password: str | None = None,
    two_fa: str | None = None,
    recovery_email: str | None = None,
    mail_token: str | None = None,
) -> str:
    """Format stored mail credentials as a standard pipe-delimited backup line."""
    em = (email or "").strip()
    pw = (password or "").strip()
    tfa = (two_fa or "").strip()
    tok = (mail_token or "").strip()
    rec = (recovery_email or "").strip()

    if tok and not tfa:
        parts = [em, pw, tok, rec]
    elif tok and tfa:
        parts = [em, pw, tfa, tok, rec]
    else:
        parts = [em, pw, tfa, rec]
    while parts and not parts[-1]:
        parts.pop()
    return "|".join(parts)


def format_oauth_reader_line(
    email: str | None = None,
    password: str | None = None,
    mail_token: str | None = None,
) -> str:
    """Format `email|password|refresh_token|client_id` for 1-click pasting into web mail readers."""
    parts = [
        (email or "").strip(),
        (password or "").strip(),
        (mail_token or "").strip(),
    ]
    while parts and not parts[-1]:
        parts.pop()
    return "|".join(parts)


def split_mail_token(mail_token: str | None) -> tuple[str, str]:
    """Return `(refresh_token, client_id)` from a stored `mail_token` string."""
    s = (mail_token or "").strip()
    if not s:
        return "", ""
    if "|" in s:
        parts = [p.strip() for p in s.split("|") if p.strip()]
        rt = ""
        cid = ""
        for p in parts:
            if _UUID_RE.match(p) and not cid:
                cid = p
            elif not rt:
                rt = p
        return rt, (cid or DEFAULT_OAUTH_CLIENT_ID)
    if _UUID_RE.match(s):
        return "", s
    return s, DEFAULT_OAUTH_CLIENT_ID


def _strip_html_to_text(raw: str) -> str:
    if not raw:
        return ""
    cleaned = _HTML_STYLE_SCRIPT_RE.sub(" ", raw)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_otp_from_text(subject: str, body: str) -> str:
    """Extract a 4-8 digit OTP verification code from an email's subject and body."""
    subj_clean = _strip_html_to_text(subject or "")
    subj_low = subj_clean.lower()
    if any(h in subj_low for h in _NO_OTP_SUBJECT_HINTS):
        return ""
    body_clean = _strip_html_to_text(body or "")

    for text in (subj_clean, body_clean):
        if not text:
            continue
        m_after = _OTP_CONTEXT_AFTER_RE.search(text)
        if m_after:
            return m_after.group(1)
        m_before = _OTP_CONTEXT_BEFORE_RE.search(text)
        if m_before:
            return m_before.group(1)

    for text in (subj_clean, body_clean):
        for m in _OTP_STANDALONE_6_8_RE.finditer(text):
            cand = m.group(1)
            if len(cand) in (5, 6, 7, 8):
                return cand
    return ""


def _decode_mime_header(raw_val: str | None) -> str:
    if not raw_val:
        return ""
    out: list[str] = []
    try:
        for part, enc in decode_header(raw_val):
            if isinstance(part, bytes):
                out.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(str(part))
        return "".join(out).strip()
    except Exception:
        return str(raw_val).strip()


def _exchange_microsoft_oauth_token(
    refresh_token: str,
    client_id: str,
    timeout: int = 12,
) -> tuple[bool, str, str]:
    """Exchange `(refresh_token, client_id)` for `(ok, access_token, error_msg)`."""
    endpoints = (
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "https://login.live.com/oauth20_token.srf",
    )
    last_err = "OAuth2 token exchange failed"
    for url in endpoints:
        data = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                at = str(payload.get("access_token") or "").strip()
                if at:
                    return True, at, ""
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8", errors="replace"))
                desc = err_body.get("error_description") or err_body.get("error") or str(e)
                last_err = str(desc).splitlines()[0][:160]
            except Exception:
                last_err = f"HTTP {e.code}"
        except Exception as e:
            last_err = str(e)[:160]
    return False, "", last_err


def _fetch_via_graph_api(
    access_token: str,
    top: int = 8,
    timeout: int = 12,
) -> list[dict[str, str]] | None:
    url = (
        f"https://graph.microsoft.com/v1.0/me/messages?"
        f"%24top={int(top)}&%24select=subject,from,receivedDateTime,bodyPreview,body"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

    items = data.get("value")
    if not isinstance(items, list):
        return None

    messages: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        subj = str(item.get("subject") or "").strip()
        from_obj = (item.get("from") or {}).get("emailAddress") or {}
        sender_name = str(from_obj.get("name") or "").strip()
        sender_addr = str(from_obj.get("address") or "").strip()
        sender = f"{sender_name} <{sender_addr}>".strip(" <>") if sender_addr else sender_name
        raw_time = str(item.get("receivedDateTime") or "").strip()
        fmt_time = raw_time.replace("T", " ").replace("Z", "")[:16]
        preview = str(item.get("bodyPreview") or "").strip()
        body_obj = item.get("body") or {}
        full_body = str(body_obj.get("content") or preview)
        clean_preview = _strip_html_to_text(preview or full_body)[:220]
        otp = extract_otp_from_text(subj, f"{preview}\n{full_body}")
        messages.append(
            {
                "subject": subj or "(Không tiêu đề)",
                "sender": sender,
                "time": fmt_time,
                "preview": clean_preview,
                "otp": otp,
            }
        )
    return messages


def _fetch_via_imap_xoauth2(
    email_addr: str,
    access_token: str,
    top: int = 8,
    timeout: int = 12,
) -> list[dict[str, str]] | None:
    if not email_addr:
        return None
    auth_str = f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01".encode()
    messages: list[dict[str, str]] = []
    try:
        imap = imaplib.IMAP4_SSL("outlook.office365.com", 993, timeout=timeout)
        try:
            imap.authenticate("XOAUTH2", lambda _: auth_str)
            for folder in ("INBOX", "Junk"):
                typ, _ = imap.select(folder, readonly=True)
                if typ != "OK":
                    continue
                typ, data = imap.search(None, "ALL")
                if typ != "OK" or not data or not data[0]:
                    continue
                msg_ids = data[0].split()[-top:]
                for mid in reversed(msg_ids):
                    t_fetch, msg_data = imap.fetch(mid, "(RFC822)")
                    if t_fetch != "OK" or not msg_data:
                        continue
                    raw_bytes = next(
                        (
                            part[1]
                            for part in msg_data
                            if isinstance(part, tuple) and isinstance(part[1], bytes)
                        ),
                        b"",
                    )
                    if not raw_bytes:
                        continue
                    msg = message_from_bytes(raw_bytes)
                    subj = _decode_mime_header(msg.get("Subject"))
                    sender = _decode_mime_header(msg.get("From"))
                    date_str = _decode_mime_header(msg.get("Date"))[:22]
                    body_parts: list[str] = []
                    if msg.is_multipart():
                        for part in msg.walk():
                            ctype = part.get_content_type()
                            if ctype in ("text/plain", "text/html"):
                                payload_b = part.get_payload(decode=True)
                                if isinstance(payload_b, bytes):
                                    body_parts.append(
                                        payload_b.decode(
                                            part.get_content_charset() or "utf-8",
                                            errors="replace",
                                        )
                                    )
                    else:
                        payload_b = msg.get_payload(decode=True)
                        if isinstance(payload_b, bytes):
                            body_parts.append(
                                payload_b.decode(
                                    msg.get_content_charset() or "utf-8",
                                    errors="replace",
                                )
                            )
                    joined_body = "\n".join(body_parts)
                    clean_prev = _strip_html_to_text(joined_body)[:220]
                    otp = extract_otp_from_text(subj, joined_body)
                    messages.append(
                        {
                            "subject": subj or "(Không tiêu đề)",
                            "sender": sender,
                            "time": date_str,
                            "preview": clean_prev,
                            "otp": otp,
                        }
                    )
                    if len(messages) >= top:
                        break
        finally:
            with contextlib.suppress(Exception):
                imap.logout()
    except Exception:
        return None
    return messages


def fetch_hotmail_messages(
    email_addr: str | None,
    mail_token: str | None,
    top: int = 8,
    timeout: int = 12,
) -> tuple[bool, str, list[dict[str, str]]]:
    """Read latest emails and extract OTP codes directly via Microsoft OAuth2 (Graph API + IMAP)."""
    rt, cid = split_mail_token(mail_token)
    if not rt:
        return False, "Chưa có Refresh Token OAuth2", []

    ok, access_token, err = _exchange_microsoft_oauth_token(rt, cid, timeout=timeout)
    if not ok or not access_token:
        return False, f"Lỗi xác thực Token: {err}", []

    msgs = _fetch_via_graph_api(access_token, top=top, timeout=timeout)
    if msgs is None:
        msgs = _fetch_via_imap_xoauth2(
            (email_addr or "").strip(),
            access_token,
            top=top,
            timeout=timeout,
        )
    if msgs is None:
        return False, "Không đọc được hòm thư từ Microsoft Graph / IMAP", []

    first_otp = next((m["otp"] for m in msgs if m.get("otp")), "")
    return True, first_otp, msgs
