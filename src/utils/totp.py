"""Offline RFC 6238 TOTP (2FA Authenticator) generator.

Computes 6-digit 2FA codes 100% locally in Python using stdlib `hmac` + `hashlib.sha1`
without any network requests or browser DOM interaction.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import hmac
import html
import http.client
import http.cookiejar
import imaplib
import json
import random
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from email import message_from_bytes
from email.header import decode_header
from typing import Any

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
    r"(?:code|código|codigo|codul|kod|код|mã|otp|pin|verification|confirm|security|passcode|xác\s*nhận|xác\s*minh|bảo\s*mật|コード)"
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
    "security alert",
    "cảnh báo bảo mật",
    "2-step verification turned on",
    "đã bật xác minh 2 bước",
    "app password created",
    "did you just remove your email",
    "did you just add your phone",
    "did you just remove your phone",
    "có phải bạn vừa gỡ địa chỉ email",
    "có phải bạn vừa thêm số điện thoại",
    "có phải bạn vừa gỡ số điện thoại",
    "¿acabas de agregar tu número de teléfono",
    "¿acabas de eliminar tu número de teléfono",
    "tocmai ţi-ai adăugat numărul de telefon",
    "tocmai ți-ai adăugat numărul de telefon",
    "это вы только что добавили номер телефона",
    "це ви щойно додали номер телефону",
    "adakah anda baharu menambah nombor telefon",
    "kakadagdag mo lang ba ng phone number mo",
    "電話番号を追加しましたか",
    "電話番号を削除しましたか",
    "你刚刚是否添加了自己的电话号码",
    "你刚刚是否移除了自己的电话号码",
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


_GMAIL_DOMAINS = ("@gmail.com", "@googlemail.com")
_GMAIL_SPACED_APP_PW_RE = re.compile(r"^[a-zA-Z]{4}(?:\s+[a-zA-Z]{4}){3}$")


def is_gmail_address(email_addr: str | None) -> bool:
    """Return True if `email_addr` is a Gmail / Googlemail address."""
    em = (email_addr or "").strip().lower()
    return bool(em and em.endswith(_GMAIL_DOMAINS))


def normalize_gmail_app_password(raw: str | None) -> str:
    """Normalize a 16-letter Google App Password (`xxxx xxxx xxxx xxxx` -> `xxxxxxxxxxxxxxxx`), or `""`."""
    s = (raw or "").strip()
    if not s or "@" in s or "|" in s or s.lower().startswith("otpauth://"):
        return ""
    if _GMAIL_SPACED_APP_PW_RE.match(s):
        return re.sub(r"\s+", "", s)
    if " " not in s and len(s) == 16 and s.isalpha():
        return s
    return ""


def _looks_like_gmail_app_password(token: str, email_addr: str = "") -> bool:
    s = (token or "").strip()
    if not s:
        return False
    if _GMAIL_SPACED_APP_PW_RE.match(s):
        return True
    return bool(is_gmail_address(email_addr) and len(s) == 16 and s.isalpha() and s.islower())


def _looks_like_totp_token(token: str) -> bool:
    s = (token or "").strip()
    if not s:
        return False
    if s.lower().startswith("otpauth://"):
        return bool(normalize_totp_secret(s))
    if _UUID_RE.match(s) or _GMAIL_SPACED_APP_PW_RE.match(s):
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
            out["email"] = re.sub(r"^\d+\.\s*(?=[a-zA-Z])", "", line)
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

    if out["email"]:
        out["email"] = re.sub(r"^\d+\.\s*(?=[a-zA-Z])", "", out["email"].strip())

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
        elif _looks_like_gmail_app_password(tok, out["email"]) and not out["refresh_token"]:
            out["refresh_token"] = normalize_gmail_app_password(tok)
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


THUNDERBIRD_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:115.0) Gecko/20100101 Thunderbird/115.16.0"
)

_META_CHECKPOINT_PHRASES = (
    "action needed on your meta account",
    "cần hành động đối với tài khoản meta",
    "confirm you're human",
    "confirm you’re human",
    "xác nhận bạn là người thật",
    "xác nhận bạn là con người",
    "we suspended your account",
    "chúng tôi đã đình chỉ tài khoản",
    "your account has been disabled",
    "permanently disabled your account",
    "vô hiệu hóa vĩnh viễn tài khoản",
    "30 days to take action",
    "30 ngày để hành động",
    "180 days to take action",
    "180 ngày để hành động",
    "metaアカウントで対応が必要です",
    "アカウントを一時停止しました",
    "人間であることを確認",
    # Spanish
    "se requiere una acción en tu cuenta de meta",
    "se requiere una accion en tu cuenta de meta",
    "acción necesaria en tu cuenta de meta",
    "suspendimos tu cuenta",
    "hemos suspendido tu cuenta",
    "inhabilitamos tu cuenta",
    # Romanian
    "este necesară o acţiune privind contul tău de meta",
    "este necesară o acțiune privind contul tău de meta",
    "este necesara o actiune privind contul tau de meta",
    "ţi-am suspendat contul",
    "ți-am suspendat contul",
    # Russian
    "требуется выполнить действие в вашем аккаунте meta",
    "необходимо выполнить действие в вашем аккаунте meta",
    "мы приостановили действие вашего аккаунта",
    "ваш аккаунт заблокирован",
    # Ukrainian
    "потрібна дія щодо вашого облікового запису meta",
    "ми призупинили дію вашого облікового запису",
    # Malay / Indonesian
    "tindakan diperlukan pada akaun meta anda",
    "tindakan diperlukan di akun meta anda",
    "kami menggantung akaun anda",
    "kami menangguhkan akun anda",
    # Dutch
    "actie vereist voor je meta-account",
    "we hebben je account opgeschort",
    # Filipino / Tagalog
    "kailangan ng aksyon sa iyong meta account",
    "sinuspinde namin ang iyong account",
    # Portuguese
    "ação necessária na sua conta da meta",
    "acao necessaria na sua conta da meta",
    "ação necessária na tua conta meta",
    "suspendemos a sua conta",
    "suspendemos sua conta",
    # French
    "action requise sur votre compte meta",
    "nous avons suspendu votre compte",
    # German
    "handlungsbedarf für dein meta-konto",
    "aktion erforderlich für dein meta-konto",
    "wir haben dein konto gesperrt",
    # Italian
    "azione richiesta sul tuo account meta",
    "abbiamo sospeso il tuo account",
    # Turkish
    "meta hesabınızda işlem yapılması gerekiyor",
    "meta hesabında işlem yapılması gerekiyor",
    "hesabınızı askıya aldık",
    # Polish
    "wymagane działanie na koncie meta",
    "zawiesiliśmy twoje konto",
    # Thai / Korean / Chinese / Arabic
    "ต้องดำเนินการกับบัญชี meta ของคุณ",
    "meta 계정에 조치가 필요합니다",
    "你的 meta 帐户需要采取行动",
    "你的 meta 账户需要采取行动",
    "需要对你的 meta 账户采取措施",
    "我们暂停了你的账户",
    "您的 meta 帳號需要採取行動",
    "مطلوب اتخاذ إجراء بشأن حساب meta الخاص بك",
)

_META_ACTIVE_AGAIN_SUBJECTS = (
    "your account is now active",
    "tài khoản của bạn đang hoạt động",
)

_PERMANENT_TOKEN_ERRORS = (
    "invalid_grant",
    "aadsts70000",
    "aadsts50173",
    "interaction_required",
    "account has been locked",
    "chưa có refresh token",
    "chưa có mật khẩu ứng dụng",
    "application-specific password",
    "invalid credentials",
    "authenticationfailed",
)


def is_permanent_mail_token_error(error_msg: str | None) -> bool:
    """Return True if `error_msg` indicates an unrecoverable/revoked OAuth2 refresh token."""
    low = (error_msg or "").strip().lower()
    if not low:
        return False
    return any(tok in low for tok in _PERMANENT_TOKEN_ERRORS)


def detect_meta_checkpoint_mail(
    messages: list[dict[str, str]] | None,
) -> dict[str, str] | None:
    """Inspect recent inbox messages and return checkpoint details if a Meta human-check/suspension email is found."""
    if not messages:
        return None
    has_active_again = False
    active_again_msg: dict[str, str] | None = None
    checkpoint_msg: dict[str, str] | None = None

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        subj = str(msg.get("subject") or "").strip()
        prev = str(msg.get("preview") or "").strip()
        combined = f"{subj}\n{prev}".lower()
        subj_low = subj.lower()

        if any(k in subj_low for k in _META_ACTIVE_AGAIN_SUBJECTS):
            has_active_again = True
            if active_again_msg is None and (
                "suspended" in combined or "đình chỉ" in combined
            ):
                active_again_msg = msg
            continue

        if checkpoint_msg is None and any(p in combined for p in _META_CHECKPOINT_PHRASES):
            checkpoint_msg = msg

    target = checkpoint_msg or active_again_msg
    if target is None:
        return None
    return {
        "subject": str(target.get("subject") or "").strip(),
        "time": str(target.get("time") or "").strip(),
        "preview": str(target.get("preview") or "").strip()[:180],
        "sender": str(target.get("sender") or "").strip(),
        "resolved": "true" if has_active_again else "false",
    }


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
                "User-Agent": THUNDERBIRD_USER_AGENT,
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
        f"%24top={int(top)}&%24orderby=receivedDateTime%20desc"
        f"&%24select=subject,from,receivedDateTime,bodyPreview,body"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": THUNDERBIRD_USER_AGENT,
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


def _parse_rfc822_bytes(raw_bytes: bytes) -> dict[str, str]:
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
    return {
        "subject": subj or "(Không tiêu đề)",
        "sender": sender,
        "time": date_str,
        "preview": clean_prev,
        "otp": otp,
    }


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
        if getattr(imaplib.IMAP4_SSL, "__module__", "") == "imaplib":
            addrs = socket.getaddrinfo(
                "outlook.office365.com",
                993,
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
            if addrs:
                af, st, pr, _, sa = addrs[0]
                probe = socket.socket(af, st, pr)
                try:
                    probe.settimeout(min(3.0, max(1.5, float(timeout) / 4.0)))
                    probe.connect(sa)
                finally:
                    with contextlib.suppress(Exception):
                        probe.close()
        imap = imaplib.IMAP4_SSL("outlook.office365.com", 993, timeout=min(6, int(timeout)))
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
                    messages.append(_parse_rfc822_bytes(raw_bytes))
                    if len(messages) >= top:
                        break
        finally:
            with contextlib.suppress(Exception):
                imap.logout()
    except Exception:
        return None
    return messages


def _fetch_via_gmail_imap(
    email_addr: str,
    app_password: str,
    top: int = 8,
    timeout: int = 12,
) -> tuple[bool, str, list[dict[str, str]]]:
    """Read latest Gmail messages via `imap.gmail.com:993` using a 16-character Google App Password."""
    clean_em = (email_addr or "").strip()
    clean_pw = re.sub(r"\s+", "", (app_password or "").strip())
    if not clean_em or not clean_pw:
        return False, "Chưa có Mật khẩu ứng dụng (App Password 16 ký tự) cho Gmail", []
    messages: list[dict[str, str]] = []
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=timeout)
        try:
            imap.login(clean_em, clean_pw)
            for folder in ("INBOX", '"[Gmail]/Spam"'):
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
                    messages.append(_parse_rfc822_bytes(raw_bytes))
                    if len(messages) >= top:
                        break
                if len(messages) >= top:
                    break
        finally:
            with contextlib.suppress(Exception):
                imap.logout()
    except imaplib.IMAP4.error as e:
        err_str = str(e).strip()[:160]
        return False, f"Lỗi xác thực App Password Gmail: {err_str}", []
    except Exception as e:
        return False, f"Lỗi kết nối IMAP Gmail: {str(e)[:140]}", []

    first_otp = next((m["otp"] for m in messages if m.get("otp")), "")
    return True, first_otp, messages


_MICROSOFT_MAIL_DOMAINS = (
    "@hotmail.",
    "@outlook.",
    "@live.",
    "@msn.",
)

_MAILTEMP_VN_DOMAINS = (
    ".editzone.io.vn",
    ".cutflow.io.vn",
    ".capmanager.io.vn",
    ".capedit.io.vn",
    "@mailtemp.vn",
)

_LUUVAN_DOMAINS = (
    "@lhuniverse.io.vn",
    "@animee.tokyo",
    ".animee.tokyo",
    "@n0ai.cloud",
    "@vibecoder.io.vn",
    "@luuvan.site",
)

_TEMPMAIL_PLUS_DOMAINS = (
    "@merepost.com",
    "@mailto.plus",
    "@fexpost.com",
    "@fexbox.org",
    "@mailbox.in.ua",
    "@rover.info",
    "@chitthi.in",
    "@fextemp.com",
    "@any.pink",
    "@thankyou2010.com",
)

_TUAN89_DOMAINS = (
    "@tuan89.site",
    "@shoptoolgiare.com",
)

_GEN_EMAIL_ROW_RE = re.compile(
    r"loadInboxClientSide\('([^']+)'\)[^>]*>\s*"
    r"<div[^>]*>([^<]*)</div>\s*"
    r"<div[^>]*>([^<]*)</div>\s*"
    r"<div[^>]*>([^<]*)</div>",
    re.IGNORECASE | re.DOTALL,
)


def is_mailtemp_address(email_addr: str | None) -> bool:
    """Return True if `email_addr` is a non-Microsoft, non-Gmail custom domain mailbox (e.g. `mailtemp.vn` or `generator.email`)."""
    em = (email_addr or "").strip().lower()
    if not em or "@" not in em or is_gmail_address(em):
        return False
    return not any(d in em for d in _MICROSOFT_MAIL_DOMAINS)


def is_generator_email_address(
    email_addr: str | None,
    mail_token: str | None = None,
) -> bool:
    """Return True if `email_addr` should be read via `generator.email` rather than `mailtemp.vn`."""
    em = (email_addr or "").strip().lower()
    if not is_mailtemp_address(em):
        return False
    tok_low = (mail_token or "").strip().lower()
    if "generator.email" in tok_low:
        return True
    if (
        "mailtemp.vn" in tok_low
        or "tuan89.site" in tok_low
        or "tinyhost.shop" in tok_low
        or any(em.endswith(d) for d in _MAILTEMP_VN_DOMAINS)
        or any(em.endswith(d) for d in _TUAN89_DOMAINS)
    ):
        return False
    return not tok_low or tok_low == "webmail"


_SSL_DEFAULT_CTX = ssl.create_default_context()
_IPV4_POOL_LOCK = threading.Lock()
_IPV4_CONN_POOL: dict[str, list[_IPv4HTTPSConnection]] = {}
_GEN_EMAIL_AB_HP: list[str] = [""]
_AB_HP_RE = re.compile(r"__Host-ab_hp=([^;\s]+)")


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        addrs = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
        if not addrs:
            raise OSError(f"Cannot resolve IPv4 for {self.host}")
        af, socktype, proto, _, sa = addrs[0]
        raw_timeout = (
            float(self.timeout)
            if isinstance(self.timeout, (int, float)) and self.timeout > 0
            else 14.0
        )
        conn_timeout = min(4.5, max(2.5, raw_timeout / 3.0))
        read_timeout = max(12.0, raw_timeout)
        server_hostname = self._tunnel_host if self._tunnel_host else self.host

        last_err: Exception | None = None
        for _attempt in range(3):
            sock: socket.socket | None = None
            try:
                sock = socket.socket(af, socktype, proto)
                sock.settimeout(conn_timeout)
                sock.connect(sa)
                tls_sock = self._context.wrap_socket(sock, server_hostname=server_hostname)
                tls_sock.settimeout(read_timeout)
                self.sock = tls_sock
                return
            except Exception as e:
                last_err = e
                if sock is not None:
                    with contextlib.suppress(Exception):
                        sock.close()
        if last_err is not None:
            raise last_err
        raise OSError(f"Cannot connect IPv4 to {self.host}")


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_IPv4HTTPSConnection, req, context=self._context)


def _checkout_ipv4_conn(host: str, timeout: int) -> tuple[_IPv4HTTPSConnection, bool]:
    with _IPV4_POOL_LOCK:
        bucket = _IPV4_CONN_POOL.get(host)
        while bucket:
            conn = bucket.pop()
            if conn.sock is not None:
                return conn, True
    return (
        _IPv4HTTPSConnection(
            host,
            443,
            timeout=max(12, int(timeout)),
            context=_SSL_DEFAULT_CTX,
        ),
        False,
    )


def _checkin_ipv4_conn(host: str, conn: _IPv4HTTPSConnection) -> None:
    if conn.sock is None:
        return
    with _IPV4_POOL_LOCK:
        bucket = _IPV4_CONN_POOL.setdefault(host, [])
        if len(bucket) < 4:
            bucket.append(conn)
            return
    with contextlib.suppress(Exception):
        conn.close()


def _http_get_ipv4(
    host: str,
    path: str,
    headers: dict[str, str],
    timeout: int = 15,
) -> str:
    """Perform a thread-safe Keep-Alive HTTPS GET over IPv4 (`socket.AF_INET`) with fast SYN retry and same-socket redirect handling."""
    req_headers = {"Accept-Encoding": "gzip", **headers, "Connection": "keep-alive"}
    if host == "generator.email" and _GEN_EMAIL_AB_HP[0]:
        cur_ck = req_headers.get("Cookie") or ""
        if "__Host-ab_hp=" not in cur_ck:
            req_headers["Cookie"] = (
                f"{cur_ck}; __Host-ab_hp={_GEN_EMAIL_AB_HP[0]}"
                if cur_ck
                else f"__Host-ab_hp={_GEN_EMAIL_AB_HP[0]}"
            )
    last_err: Exception | None = None

    for attempt in range(2):
        conn, reused = _checkout_ipv4_conn(host, timeout)
        try:
            cur_path = path
            body_bytes = b""
            content_enc = ""
            for _redir in range(3):
                if conn.sock is not None and hasattr(conn.sock, "settimeout"):
                    conn.sock.settimeout(max(12.0, float(timeout)))
                conn.request("GET", cur_path, headers=req_headers)
                resp = conn.getresponse()
                body_bytes = resp.read()
                content_enc = (resp.getheader("Content-Encoding") or "").strip().lower()
                if host == "generator.email" and hasattr(resp, "getheaders"):
                    for hk, hv in resp.getheaders():
                        if hk.lower() == "set-cookie" and "__Host-ab_hp=" in hv:
                            m_hp = _AB_HP_RE.search(hv)
                            if m_hp:
                                _GEN_EMAIL_AB_HP[0] = m_hp.group(1)
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.getheader("Location") or ""
                    if loc:
                        parsed = urllib.parse.urlsplit(loc)
                        cur_path = (parsed.path or "/") + (
                            f"?{parsed.query}" if parsed.query else ""
                        )
                        continue
                break
            _checkin_ipv4_conn(host, conn)
            if content_enc == "gzip" and body_bytes[:2] == b"\x1f\x8b":
                with contextlib.suppress(Exception):
                    body_bytes = gzip.decompress(body_bytes)
            return body_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            with contextlib.suppress(Exception):
                conn.close()
            if not reused and attempt >= 1:
                break

    if last_err is not None:
        raise last_err
    raise TimeoutError("The read operation timed out")


def _fetch_via_generator_email(
    email_addr: str,
    *,
    top: int = 8,
    timeout: int = 15,
) -> tuple[bool, str, list[dict[str, str]]]:
    """Read latest messages from `generator.email` over IPv4 using `inbox_ctx` cookie."""
    clean_em = (email_addr or "").strip().lower()
    if "@" not in clean_em:
        return False, "Địa chỉ email không hợp lệ", []
    user, dom = clean_em.split("@", 1)
    ctx_base = f"{dom}/{user}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Cookie": f"inbox_ctx={ctx_base}; surl={ctx_base}",
    }
    try:
        # Requesting /inbox1/ directly with inbox_ctx + surl cookies returns 200 OK
        # immediately without the 302 redirect round-trip triggered by /{ctx_base}.
        page_html = _http_get_ipv4(
            "generator.email",
            "/inbox1/",
            headers=headers,
            timeout=max(15, int(timeout)),
        )
        page_low = page_html.lower()
        if (
            'id="email-table"' not in page_html
            and user not in page_low
            and "generator.email" not in page_low
        ):
            page_html = _http_get_ipv4(
                "generator.email",
                f"/{ctx_base}",
                headers=headers,
                timeout=max(15, int(timeout)),
            )
    except Exception as e:
        return False, f"Lỗi kết nối generator.email: {str(e)[:140]}", []

    idx = page_html.find('id="email-table"')
    table_chunk = page_html[idx : idx + 18000] if idx != -1 else page_html

    messages: list[dict[str, str]] = []
    rows = _GEN_EMAIL_ROW_RE.findall(table_chunk)
    for row_idx, (msg_ctx, raw_from, raw_subj, raw_time) in enumerate(rows[:top]):
        sender_str = html.unescape(raw_from).strip()
        subj_str = html.unescape(raw_subj).strip() or "(Không có tiêu đề)"
        time_str = html.unescape(raw_time).strip()[:16]
        preview_str = ""
        otp = extract_otp_from_text(subj_str, "")

        subj_low = subj_str.lower()
        needs_body_otp = (
            top >= 5
            and row_idx < 2
            and not otp
            and not any(h in subj_low for h in _NO_OTP_SUBJECT_HINTS)
            and "did you just add" not in subj_low
            and "did you just remove" not in subj_low
            and any(
                k in subj_low
                for k in (
                    "confirm",
                    "code",
                    "código",
                    "codul",
                    "kod",
                    "код",
                    "mã",
                    "verify",
                    "login",
                    "security",
                    "xác nhận",
                    "sahkan",
                    "bevestig",
                    "kumpirmahin",
                    "подтвердите",
                    "підтвердьте",
                    "認証",
                    "コード",
                )
            )
        )
        if needs_body_otp and msg_ctx:
            clean_ctx = msg_ctx.lstrip("/")
            with contextlib.suppress(Exception):
                msg_html = _http_get_ipv4(
                    "generator.email",
                    "/inbox1/",
                    headers={
                        "User-Agent": headers["User-Agent"],
                        "Accept": headers["Accept"],
                        "Cookie": f"inbox_ctx={clean_ctx}; surl={clean_ctx}",
                    },
                    timeout=max(10, int(timeout)),
                )
                m_idx = msg_html.find('id="email-table"')
                m_chunk = msg_html[m_idx : m_idx + 8000] if m_idx != -1 else msg_html
                del_idx = m_chunk.find("Message source")
                body_part = m_chunk[del_idx + 14 :] if del_idx != -1 else m_chunk
                preview_str = _strip_html_to_text(body_part)[:220]
                otp = extract_otp_from_text(subj_str, preview_str)

        messages.append(
            {
                "subject": subj_str,
                "sender": sender_str,
                "time": time_str,
                "preview": preview_str,
                "otp": otp,
            }
        )

    if not messages and idx != -1:
        # Single-message view when inbox has exactly 1 message rendered directly
        plain_table = _strip_html_to_text(table_chunk)
        m_from = re.search(r"From:\s*([^\s]+@[^\s]+)", plain_table, re.IGNORECASE)
        m_subj = re.search(
            r"Subject:\s*(.+?)\s+(?:Received:|Created:|Delete Message)",
            plain_table,
            re.IGNORECASE,
        )
        m_time = re.search(r"Received:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", plain_table)
        if m_from or m_subj:
            subj_s = (m_subj.group(1).strip() if m_subj else "(Không có tiêu đề)")
            sender_s = (m_from.group(1).strip() if m_from else "")
            time_s = (m_time.group(1).strip() if m_time else "")
            del_idx = plain_table.find("Message source")
            prev_s = (
                plain_table[del_idx + 14 :].strip()[:220]
                if del_idx != -1
                else plain_table[:220]
            )
            otp_s = extract_otp_from_text(subj_s, prev_s)
            messages.append(
                {
                    "subject": subj_s,
                    "sender": sender_s,
                    "time": time_s,
                    "preview": prev_s,
                    "otp": otp_s,
                }
            )

    first_otp = next((m["otp"] for m in messages if m.get("otp")), "")
    return True, first_otp, messages


def _fetch_via_mailtemp_api(
    email_addr: str,
    mail_token: str | None,
    *,
    password: str | None = None,
    top: int = 8,
    timeout: int = 12,
) -> tuple[bool, str, list[dict[str, str]]]:
    """Read latest messages from `mailtemp.vn` (`/api/site/unlock` + `/api/site/inbox`)."""
    clean_em = (email_addr or "").strip().lower()
    raw_tok = (mail_token or "").strip()
    raw_pw = (password or "").strip()

    token_part = ""
    pw_part = raw_pw
    if "|" in raw_tok:
        p1, p2 = raw_tok.split("|", 1)
        token_part = p1.strip()
        if not pw_part:
            pw_part = p2.strip()
    elif len(raw_tok) >= 20:
        token_part = raw_tok
    elif raw_tok and not pw_part:
        pw_part = raw_tok

    def _call_inbox(mbox_token: str) -> dict[str, Any]:
        inbox_url = f"https://mailtemp.vn/api/site/inbox?address={urllib.parse.quote(clean_em)}"
        headers = {"User-Agent": "Mozilla/5.0"}
        if mbox_token:
            headers["x-mailbox-token"] = mbox_token
        req = urllib.request.Request(inbox_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    inbox_data: dict[str, Any] | None = None
    try:
        if token_part:
            try:
                inbox_data = _call_inbox(token_part)
            except urllib.error.HTTPError as he:
                if he.code not in (401, 403) or not pw_part:
                    raise
        if inbox_data is None:
            if pw_part:
                unlock_req = urllib.request.Request(
                    "https://mailtemp.vn/api/site/unlock",
                    data=json.dumps({"address": clean_em, "password": pw_part}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                    method="POST",
                )
                with urllib.request.urlopen(unlock_req, timeout=timeout) as u_resp:
                    u_json = json.loads(u_resp.read().decode("utf-8", errors="replace"))
                    token_part = str(
                        u_json.get("token")
                        or (u_json.get("data") or {}).get("token")
                        or ""
                    ).strip()
            inbox_data = _call_inbox(token_part)
    except urllib.error.HTTPError as e:
        return False, f"Lỗi xác thực hộp thư mailtemp.vn (HTTP {e.code})", []
    except Exception as e:
        return False, f"Lỗi kết nối mailtemp.vn: {str(e)[:140]}", []

    raw_msgs = (inbox_data or {}).get("messages") or []
    messages: list[dict[str, str]] = []
    for item in raw_msgs[:top]:
        if not isinstance(item, dict):
            continue
        subj = str(item.get("subject") or "(Không có tiêu đề)").strip()
        from_addr = str(item.get("from") or "").strip()
        from_name = str(item.get("fromName") or "").strip()
        sender_str = f"{from_name} <{from_addr}>".strip() if from_name else from_addr
        raw_dt = str(item.get("receivedAt") or "").strip()
        date_str = raw_dt[:16].replace("T", " ") if len(raw_dt) >= 16 else raw_dt
        preview_str = str(item.get("preview") or "").strip()
        otp = str(item.get("code") or "").strip() or extract_otp_from_text(subj, preview_str)
        messages.append(
            {
                "subject": subj,
                "sender": sender_str,
                "time": date_str,
                "preview": preview_str[:220],
                "otp": otp,
            }
        )

    first_otp = next((m["otp"] for m in messages if m.get("otp")), "")
    return True, first_otp, messages


def _fetch_via_luuvan_api(
    email_addr: str,
    *,
    top: int = 8,
    timeout: int = 12,
) -> tuple[bool, str, list[dict[str, str]]]:
    """Read latest messages from `luuvan.site` (`/api/public/inbox` + `/api/public/messages/<id>`)."""
    clean_em = (email_addr or "").strip().lower()
    if "@" not in clean_em:
        return False, "Địa chỉ email không hợp lệ", []
    inbox_url = f"https://luuvan.site/api/public/inbox?address={urllib.parse.quote(clean_em)}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        req = urllib.request.Request(inbox_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return False, f"Lỗi kết nối luuvan.site: {str(e)[:140]}", []

    raw_msgs = (data or {}).get("messages") or []
    messages: list[dict[str, str]] = []
    for row_idx, item in enumerate(raw_msgs[:top]):
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        subj = str(item.get("subject") or "(Không có tiêu đề)").strip()
        sender_str = str(item.get("from") or "").strip()
        raw_dt = str(item.get("receivedAt") or "").strip()
        date_str = raw_dt[:16].replace("T", " ") if len(raw_dt) >= 16 else raw_dt
        preview_str = str(item.get("preview") or "").strip()
        otp = extract_otp_from_text(subj, preview_str)

        subj_low = subj.lower()
        needs_body_otp = (
            top >= 5
            and row_idx < 2
            and not otp
            and not any(h in subj_low for h in _NO_OTP_SUBJECT_HINTS)
            and "did you just add" not in subj_low
            and "did you just remove" not in subj_low
            and "電話番号を追加" not in subj
            and "電話番号を削除" not in subj
            and any(
                k in subj_low
                for k in (
                    "confirm",
                    "code",
                    "mã",
                    "verify",
                    "login",
                    "security",
                    "xác nhận",
                    "認証",
                    "コード",
                )
            )
        )
        if needs_body_otp and mid:
            with contextlib.suppress(Exception):
                msg_url = (
                    f"https://luuvan.site/api/public/messages/{urllib.parse.quote(mid)}"
                    f"?address={urllib.parse.quote(clean_em)}"
                )
                m_req = urllib.request.Request(msg_url, headers=headers)
                with urllib.request.urlopen(m_req, timeout=min(8, timeout)) as m_resp:
                    m_data = json.loads(m_resp.read().decode("utf-8", errors="replace"))
                    m_obj = (m_data or {}).get("message") or {}
                    raw_body = str(m_obj.get("text") or m_obj.get("html") or "")
                    preview_str = _strip_html_to_text(raw_body)[:220]
                    otp = extract_otp_from_text(subj, preview_str)

        messages.append(
            {
                "subject": subj,
                "sender": sender_str,
                "time": date_str,
                "preview": preview_str[:220],
                "otp": otp,
            }
        )

    first_otp = next((m["otp"] for m in messages if m.get("otp")), "")
    return True, first_otp, messages


def _fetch_via_tempmail_plus_api(
    email_addr: str,
    *,
    top: int = 8,
    timeout: int = 12,
) -> tuple[bool, str, list[dict[str, str]]]:
    """Read latest messages from `tempmail.plus` (`/api/mails`)."""
    clean_em = (email_addr or "").strip().lower()
    if "@" not in clean_em:
        return False, "Địa chỉ email không hợp lệ", []
    list_url = (
        f"https://tempmail.plus/api/mails?email={urllib.parse.quote(clean_em)}"
        f"&limit={max(4, int(top))}&epin="
    )
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        req = urllib.request.Request(list_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return False, f"Lỗi kết nối tempmail.plus: {str(e)[:140]}", []

    raw_msgs = (data or {}).get("mail_list") or []
    messages: list[dict[str, str]] = []
    for row_idx, item in enumerate(raw_msgs[:top]):
        if not isinstance(item, dict):
            continue
        mid = str(item.get("mail_id") or "").strip()
        subj = str(item.get("subject") or "(Không có tiêu đề)").strip()
        from_addr = str(item.get("from_mail") or "").strip()
        from_name = str(item.get("from_name") or "").strip()
        sender_str = f"{from_name} <{from_addr}>".strip() if from_name else from_addr
        date_str = str(item.get("time") or "").strip()[:16]
        preview_str = ""
        otp = extract_otp_from_text(subj, "")

        if top >= 5 and row_idx < 2 and not otp and mid:
            with contextlib.suppress(Exception):
                msg_url = (
                    f"https://tempmail.plus/api/mails/{urllib.parse.quote(mid)}"
                    f"?email={urllib.parse.quote(clean_em)}&epin="
                )
                m_req = urllib.request.Request(msg_url, headers=headers)
                with urllib.request.urlopen(m_req, timeout=min(8, timeout)) as m_resp:
                    m_data = json.loads(m_resp.read().decode("utf-8", errors="replace"))
                    raw_body = str((m_data or {}).get("text") or (m_data or {}).get("html") or "")
                    preview_str = _strip_html_to_text(raw_body)[:220]
                    otp = extract_otp_from_text(subj, preview_str)

        messages.append(
            {
                "subject": subj,
                "sender": sender_str,
                "time": date_str,
                "preview": preview_str[:220],
                "otp": otp,
            }
        )

    first_otp = next((m["otp"] for m in messages if m.get("otp")), "")
    return True, first_otp, messages


_TEMPMAIL_ID_VN_ROW_RE = re.compile(
    r'<tr[^>]*class="[^"]*fi-ta-row[^"]*"[^>]*>(.*?)</tr>',
    re.IGNORECASE | re.DOTALL,
)
_TEMPMAIL_ID_VN_SUBJ_RE = re.compile(
    r"fi-ta-cell-subject.*?<div[^>]*fi-ta-text[^>]*>(.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
_TEMPMAIL_ID_VN_FROM_RE = re.compile(
    r"fi-ta-cell-sender-name.*?<p[^>]*fi-ta-text-item[^>]*>(.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)
_TEMPMAIL_ID_VN_TIME_RE = re.compile(
    r"fi-ta-cell-created-at.*?<div[^>]*fi-ta-text[^>]*>(.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
_TEMPMAIL_ID_VN_HREF_RE = re.compile(
    r'href="(https://tempmail\.id\.vn/guest/message/[^"]+)"',
    re.IGNORECASE,
)


def _fetch_via_tempmail_id_vn(
    email_addr: str,
    mail_token: str | None,
    *,
    top: int = 8,
    timeout: int = 12,
) -> tuple[bool, str, list[dict[str, str]]]:
    """Read latest messages from `tempmail.id.vn` guest inbox URL."""
    raw_tok = (mail_token or "").strip()
    m_url = re.search(
        r"https?://tempmail\.id\.vn/guest/inbox/[0-9a-fA-F\-]+",
        raw_tok,
        re.IGNORECASE,
    )
    if m_url:
        inbox_url = m_url.group(0)
    elif _UUID_RE.match(raw_tok):
        inbox_url = f"https://tempmail.id.vn/guest/inbox/{raw_tok}"
    else:
        return False, "Thiếu link hộp thư tempmail.id.vn hợp lệ", []

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        req = urllib.request.Request(inbox_url, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            page_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"Lỗi kết nối tempmail.id.vn: {str(e)[:140]}", []

    rows = _TEMPMAIL_ID_VN_ROW_RE.findall(page_html)
    messages: list[dict[str, str]] = []
    for row_idx, rw in enumerate(rows[:top]):
        s_m = _TEMPMAIL_ID_VN_SUBJ_RE.search(rw)
        f_m = _TEMPMAIL_ID_VN_FROM_RE.search(rw)
        t_m = _TEMPMAIL_ID_VN_TIME_RE.search(rw)
        h_m = _TEMPMAIL_ID_VN_HREF_RE.search(rw)

        subj_str = (
            html.unescape(_HTML_TAG_RE.sub("", s_m.group(1))).strip()
            if s_m
            else "(Không có tiêu đề)"
        )
        sender_str = (
            html.unescape(_HTML_TAG_RE.sub("", f_m.group(1))).strip()
            if f_m
            else "Meta"
        )
        time_str = (
            html.unescape(_HTML_TAG_RE.sub("", t_m.group(1))).strip()[:16]
            if t_m
            else ""
        )
        preview_str = ""
        otp = extract_otp_from_text(subj_str, "")

        subj_low = subj_str.lower()
        needs_body_otp = (
            top >= 5
            and row_idx < 2
            and not otp
            and h_m is not None
            and not any(h in subj_low for h in _NO_OTP_SUBJECT_HINTS)
            and "did you just add" not in subj_low
            and "did you just remove" not in subj_low
            and "電話番号を追加" not in subj_str
            and "電話番号を削除" not in subj_str
            and any(
                k in subj_low
                for k in (
                    "confirm",
                    "code",
                    "mã",
                    "verify",
                    "login",
                    "security",
                    "xác nhận",
                    "認証",
                    "コード",
                )
            )
        )
        if needs_body_otp and h_m is not None:
            with contextlib.suppress(Exception):
                m_req = urllib.request.Request(
                    h_m.group(1),
                    headers={**headers, "Referer": inbox_url},
                )
                with opener.open(m_req, timeout=min(8, timeout)) as m_resp:
                    m_html = m_resp.read().decode("utf-8", errors="replace")
                    srcdoc_m = re.search(r'srcdoc="([^"]+)"', m_html, re.IGNORECASE)
                    raw_body = html.unescape(srcdoc_m.group(1)) if srcdoc_m else m_html
                    preview_str = _strip_html_to_text(raw_body)[:220]
                    otp = extract_otp_from_text(subj_str, preview_str)

        messages.append(
            {
                "subject": subj_str,
                "sender": sender_str,
                "time": time_str,
                "preview": preview_str[:220],
                "otp": otp,
            }
        )

    first_otp = next((m["otp"] for m in messages if m.get("otp")), "")
    return True, first_otp, messages


def _fetch_via_tuan89_api(
    email_addr: str,
    *,
    top: int = 8,
    timeout: int = 12,
) -> tuple[bool, str, list[dict[str, str]]]:
    """Read latest messages from `tuan89.site` (`/api/temp-mail/inbox` + `/api/temp-mail/message`)."""
    clean_em = (email_addr or "").strip().lower()
    if "@" not in clean_em:
        return False, "Địa chỉ email không hợp lệ", []
    inbox_url = (
        f"https://tuan89.site/api/temp-mail/inbox?email={urllib.parse.quote(clean_em)}"
    )
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        req = urllib.request.Request(inbox_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return False, f"Lỗi kết nối tuan89.site: {str(e)[:140]}", []

    if not isinstance(data, dict) or not data.get("success", True):
        err_m = str((data or {}).get("message") or "Hộp thư không tồn tại trên tuan89.site")
        return False, err_m, []

    raw_msgs = (data or {}).get("messages") or []
    messages: list[dict[str, str]] = []
    for row_idx, item in enumerate(raw_msgs[:top]):
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        subj = str(item.get("subject") or "(Không có tiêu đề)").strip()
        from_name = str(item.get("sender_name") or "").strip()
        from_addr = str(item.get("from_email") or "").strip()
        sender_str = f"{from_name} <{from_addr}>".strip() if from_name and from_addr else (from_name or from_addr)
        date_str = str(item.get("received_at") or "").strip()[:16]
        preview_str = str(item.get("snippet") or "").strip()
        otp = str(item.get("otp_code") or "").strip() or extract_otp_from_text(subj, preview_str)

        if top >= 5 and row_idx < 2 and not otp and mid:
            with contextlib.suppress(Exception):
                msg_url = (
                    f"https://tuan89.site/api/temp-mail/message?id={urllib.parse.quote(mid)}"
                    f"&email={urllib.parse.quote(clean_em)}"
                )
                m_req = urllib.request.Request(msg_url, headers=headers)
                with urllib.request.urlopen(m_req, timeout=min(8, timeout)) as m_resp:
                    m_data = json.loads(m_resp.read().decode("utf-8", errors="replace"))
                    m_obj = (m_data or {}).get("message") or {}
                    raw_body = str(m_obj.get("body_text") or m_obj.get("body_html") or "")
                    preview_str = _strip_html_to_text(raw_body)[:220]
                    otp = str(m_obj.get("otp_code") or "").strip() or extract_otp_from_text(subj, preview_str)

        messages.append(
            {
                "subject": subj,
                "sender": sender_str,
                "time": date_str,
                "preview": preview_str[:220],
                "otp": otp,
            }
        )

    first_otp = next((m["otp"] for m in messages if m.get("otp")), "")
    return True, first_otp, messages


def _fetch_via_tinyhost_api(
    email_addr: str,
    *,
    top: int = 8,
    timeout: int = 12,
) -> tuple[bool, str, list[dict[str, str]]]:
    """Read latest messages from `tinyhost.shop` (`/api/email/<domain>/<user>/`)."""
    clean_em = (email_addr or "").strip().lower()
    if "@" not in clean_em:
        return False, "Địa chỉ email không hợp lệ", []
    user, dom = clean_em.split("@", 1)
    inbox_url = (
        f"https://tinyhost.shop/api/email/{urllib.parse.quote(dom)}/{urllib.parse.quote(user)}/"
        f"?page=1&limit={max(4, int(top))}&include_total=false"
    )
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        req = urllib.request.Request(inbox_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return False, f"Lỗi kết nối tinyhost.shop: {str(e)[:140]}", []

    raw_msgs = (data or {}).get("emails") or []
    messages: list[dict[str, str]] = []
    for item in raw_msgs[:top]:
        if not isinstance(item, dict):
            continue
        subj = str(item.get("subject") or "(Không có tiêu đề)").strip()
        sender_str = str(item.get("sender") or "").strip()
        raw_dt = str(item.get("date") or "").strip()
        date_str = raw_dt[:16].replace("T", " ") if len(raw_dt) >= 16 else raw_dt
        raw_body = str(item.get("body") or item.get("html_body") or "")
        preview_str = _strip_html_to_text(raw_body)[:220]
        otp = extract_otp_from_text(subj, preview_str)
        messages.append(
            {
                "subject": subj,
                "sender": sender_str,
                "time": date_str,
                "preview": preview_str[:220],
                "otp": otp,
            }
        )

    first_otp = next((m["otp"] for m in messages if m.get("otp")), "")
    return True, first_otp, messages


def fetch_hotmail_messages(
    email_addr: str | None,
    mail_token: str | None,
    top: int = 8,
    timeout: int = 12,
    *,
    password: str | None = None,
) -> tuple[bool, str, list[dict[str, str]]]:
    """Read latest emails and extract OTP codes via Microsoft OAuth2 (Hotmail), Google App Password (Gmail), or mailtemp.vn."""
    clean_em = (email_addr or "").strip()
    if is_gmail_address(clean_em):
        app_pw = (mail_token or "").strip() or (password or "").strip()
        if "|" in app_pw:
            app_pw = app_pw.split("|", 1)[0].strip()
        return _fetch_via_gmail_imap(clean_em, app_pw, top=top, timeout=timeout)

    if is_mailtemp_address(clean_em):
        em_low = clean_em.lower()
        tok_low = (mail_token or "").strip().lower()
        if "tempmail.id.vn" in tok_low:
            return _fetch_via_tempmail_id_vn(
                clean_em,
                mail_token,
                top=top,
                timeout=timeout,
            )
        if "tinyhost.shop" in tok_low:
            return _fetch_via_tinyhost_api(clean_em, top=top, timeout=timeout)
        if "tuan89.site" in tok_low or any(em_low.endswith(d) for d in _TUAN89_DOMAINS):
            return _fetch_via_tuan89_api(clean_em, top=top, timeout=timeout)
        if "luuvan.site" in tok_low or any(em_low.endswith(d) for d in _LUUVAN_DOMAINS):
            return _fetch_via_luuvan_api(clean_em, top=top, timeout=timeout)
        if "tempmail.plus" in tok_low or any(em_low.endswith(d) for d in _TEMPMAIL_PLUS_DOMAINS):
            return _fetch_via_tempmail_plus_api(clean_em, top=top, timeout=timeout)
        if is_generator_email_address(clean_em, mail_token):
            return _fetch_via_generator_email(clean_em, top=top, timeout=timeout)
        ok_mt, otp_mt, msgs_mt = _fetch_via_mailtemp_api(
            clean_em,
            mail_token,
            password=password,
            top=top,
            timeout=timeout,
        )
        if not ok_mt and not any(em_low.endswith(d) for d in _MAILTEMP_VN_DOMAINS):
            return _fetch_via_generator_email(clean_em, top=top, timeout=timeout)
        return ok_mt, otp_mt, msgs_mt

    rt, cid = split_mail_token(mail_token)
    if not rt:
        return False, "Chưa có Refresh Token OAuth2", []

    ok, access_token, err = _exchange_microsoft_oauth_token(rt, cid, timeout=timeout)
    if not ok or not access_token:
        return False, f"Lỗi xác thực Token: {err}", []

    msgs = _fetch_via_graph_api(access_token, top=top, timeout=timeout)
    if msgs is None:
        msgs = _fetch_via_imap_xoauth2(
            clean_em,
            access_token,
            top=top,
            timeout=timeout,
        )
    if msgs is None:
        return False, "Không đọc được hòm thư từ Microsoft Graph / IMAP", []

    first_otp = next((m["otp"] for m in msgs if m.get("otp")), "")
    return True, first_otp, msgs


def _mail_provider_bucket(email_addr: str | None, mail_token: str | None = None) -> str:
    """Return the provider group name for per-provider circuit breaking."""
    clean_em = (email_addr or "").strip().lower()
    if is_gmail_address(clean_em):
        return "gmail"
    if not is_mailtemp_address(clean_em):
        return "hotmail"
    tok_low = (mail_token or "").strip().lower()
    if "tempmail.id.vn" in tok_low:
        return "tempmail.id.vn"
    if "tinyhost.shop" in tok_low:
        return "tinyhost.shop"
    if "tuan89.site" in tok_low or any(clean_em.endswith(d) for d in _TUAN89_DOMAINS):
        return "tuan89.site"
    if "luuvan.site" in tok_low or any(clean_em.endswith(d) for d in _LUUVAN_DOMAINS):
        return "luuvan.site"
    if "tempmail.plus" in tok_low or any(clean_em.endswith(d) for d in _TEMPMAIL_PLUS_DOMAINS):
        return "tempmail.plus"
    if is_generator_email_address(clean_em, mail_token):
        return "generator.email"
    return "mailtemp.vn"


def run_safe_mail_checkpoint_scan(
    targets: list[tuple[str, str, str]],
    *,
    delay_range: tuple[float, float] = (3.0, 5.0),
    top: int = 4,
    timeout: int = 12,
    max_consecutive_errors: int = 3,
    stop_event: threading.Event | None = None,
    on_progress: Callable[[int, int, str, str, str, dict[str, str] | None], None] | None = None,
    fetch_fn: Callable[..., tuple[bool, str, list[dict[str, str]]]] = fetch_hotmail_messages,
) -> dict[str, Any]:
    """Sequentially scan `targets` (`[(profile_name, email, mail_token), ...]`) with random jitter and a per-provider circuit breaker."""
    hits: dict[str, dict[str, str]] = {}
    dead_tokens: set[str] = set()
    scanned_names: set[str] = set()
    email_status: dict[str, tuple[str, str]] = {}
    provider_errors: dict[str, int] = {}
    tripped_providers: set[str] = set()
    total = len(targets)

    for idx, (prof_name, email_addr, mail_token) in enumerate(targets, 1):
        if stop_event is not None and stop_event.is_set():
            break

        em_clean = (email_addr or "").strip()
        em_key = em_clean.lower()
        provider = _mail_provider_bucket(em_clean, mail_token)
        if provider in tripped_providers:
            st_msg = f"⏭ Bỏ qua tạm thời ({provider} đang lỗi kết nối liên tiếp)"
            if em_key:
                email_status[em_key] = ("warn", st_msg)
            if on_progress is not None:
                on_progress(idx, total, prof_name, em_clean, st_msg, None)
            continue

        ok, otp_or_err, msgs = fetch_fn(
            em_clean,
            mail_token,
            top=top,
            timeout=timeout,
        )
        if not ok:
            if is_permanent_mail_token_error(otp_or_err):
                if em_key:
                    dead_tokens.add(em_key)
                provider_errors[provider] = 0
                st_msg = f"✗ Token hết hạn ({otp_or_err[:40]})"
                if em_key:
                    email_status[em_key] = ("error", st_msg)
                if on_progress is not None:
                    on_progress(idx, total, prof_name, em_clean, st_msg, None)
            else:
                cur_err = provider_errors.get(provider, 0) + 1
                provider_errors[provider] = cur_err
                st_msg = f"⚠ Lỗi kết nối ({otp_or_err[:40]})"
                if em_key:
                    email_status[em_key] = ("error", st_msg)
                if on_progress is not None:
                    on_progress(idx, total, prof_name, em_clean, st_msg, None)
                if cur_err >= max_consecutive_errors:
                    tripped_providers.add(provider)
        else:
            provider_errors[provider] = 0
            scanned_names.add(prof_name)
            cp = detect_meta_checkpoint_mail(msgs)
            if cp is not None:
                hit_data = {
                    "email": em_clean,
                    "subject": cp.get("subject", ""),
                    "time": cp.get("time", ""),
                    "preview": cp.get("preview", ""),
                    "resolved": cp.get("resolved", "false"),
                }
                hits[prof_name] = hit_data
                st_msg = f"🚨 DÍNH CHECKPOINT META: {cp.get('subject', '')[:42]}"
                if em_key:
                    email_status[em_key] = ("error", st_msg)
                if on_progress is not None:
                    on_progress(idx, total, prof_name, em_clean, st_msg, hit_data)
            else:
                otp_tag = f" · OTP: {otp_or_err}" if otp_or_err else ""
                if len(msgs) > 0:
                    st_msg = f"✓ An toàn — Không có thư Checkpoint ({len(msgs)} thư{otp_tag})"
                    if em_key:
                        email_status[em_key] = ("ok", st_msg)
                else:
                    st_msg = "⏳ Hòm thư trống (0 thư)"
                    if em_key:
                        email_status[em_key] = ("warn", st_msg)
                if on_progress is not None:
                    on_progress(idx, total, prof_name, em_clean, st_msg, None)

        if idx < total and provider not in tripped_providers:
            lo, hi = delay_range
            wait_s = random.uniform(min(lo, hi), max(lo, hi)) if hi > 0 else 0.0
            end_wait = time.monotonic() + wait_s
            while time.monotonic() < end_wait:
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(min(0.15, max(0.01, end_wait - time.monotonic())))

    aborted = (
        bool(tripped_providers)
        and not scanned_names
        and not hits
        and not dead_tokens
    )
    return {
        "hits": hits,
        "dead_tokens": dead_tokens,
        "scanned_names": scanned_names,
        "email_status": email_status,
        "tripped_providers": tripped_providers,
        "total": total,
        "aborted": aborted,
        "stopped": bool(stop_event is not None and stop_event.is_set()),
    }
