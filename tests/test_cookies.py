import base64
import hashlib
import json
import secrets
import sys
import time

import pytest

from src.services.browser import cookies


def _ext_cookie(**overrides):
    base = {
        "domain": ".example.com",
        "hostOnly": False,
        "httpOnly": True,
        "name": "sid",
        "path": "/",
        "sameSite": "lax",
        "secure": True,
        "session": False,
        "expirationDate": 2_000_000_000,
        "value": "abc",
    }
    base.update(overrides)
    return base


def test_parse_extension_json():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie()]))
    assert c == {
        "name": "sid",
        "value": "abc",
        "domain": ".example.com",
        "path": "/",
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
        "expires": 2_000_000_000.0,
    }


def test_parse_host_only_keeps_bare_domain():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie(domain="app.example.com", hostOnly=True)]))
    assert c["domain"] == "app.example.com"


def test_parse_non_host_only_adds_leading_dot():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie(domain="example.com", hostOnly=False)]))
    assert c["domain"] == ".example.com"


def test_same_site_none_forces_secure():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie(sameSite="no_restriction", secure=False)]))
    assert c["sameSite"] == "None"
    assert c["secure"] is True


def test_session_cookie_gets_long_lifetime():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie(session=True, expirationDate=None)]))
    assert c["expires"] > time.time() + 300 * 24 * 3600


def test_parse_playwright_layout():
    pw = {
        "name": "a",
        "value": "1",
        "domain": ".x.com",
        "path": "/",
        "expires": 2_000_000_000,
        "httpOnly": False,
        "secure": False,
        "sameSite": "Strict",
    }
    [c] = cookies.parse_cookies(json.dumps({"cookies": [pw]}))
    assert c["sameSite"] == "Strict"
    assert c["expires"] == 2_000_000_000.0


def test_parse_skips_entries_without_name_or_domain():
    parsed = cookies.parse_cookies(json.dumps([{"value": "x"}, _ext_cookie()]))
    assert [c["name"] for c in parsed] == ["sid"]


def test_parse_invalid_json_raises():
    with pytest.raises(cookies.CookieError):
        cookies.parse_cookies("[not json")


def test_parse_netscape_with_http_only_and_session():
    text = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.shop.vn\tTRUE\t/\tTRUE\t2000000000\ttoken\tt0k\n"
        ".shop.vn\tTRUE\t/\tFALSE\t0\tvisitor\txyz\n"
        "# a comment\n"
        "malformed line\n"
    )
    token, visitor = cookies.parse_cookies(text)
    assert token["httpOnly"] is True and token["secure"] is True
    assert token["expires"] == 2_000_000_000.0
    assert visitor["httpOnly"] is False
    assert visitor["expires"] > time.time()


def test_netscape_round_trip():
    source = [
        {
            "name": "a",
            "value": "1",
            "domain": ".x.com",
            "path": "/",
            "expires": 2_000_000_000,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "b",
            "value": "2",
            "domain": "y.com",
            "path": "/p",
            "expires": 2_000_000_000,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
    ]
    parsed = cookies.parse_cookies(cookies._to_netscape(source))
    assert [(c["name"], c["domain"], c["path"], c["httpOnly"]) for c in parsed] == [
        ("a", ".x.com", "/", True),
        ("b", "y.com", "/p", False),
    ]


def test_extension_json_round_trip():
    source = [
        {
            "name": "a",
            "value": "1",
            "domain": "y.com",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        }
    ]
    [ext] = cookies._to_extension_json(source)
    assert ext["hostOnly"] is True and ext["session"] is True
    assert "expirationDate" not in ext
    assert ext["sameSite"] == "no_restriction"
    [back] = cookies.parse_cookies(json.dumps([ext]))
    assert back["domain"] == "y.com" and back["sameSite"] == "None"


def test_export_rejects_unknown_format():
    with pytest.raises(cookies.CookieError):
        cookies.export_cookies("p", "windows", "xml")


def test_non_numeric_expiry_is_treated_as_session():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie(expirationDate="session")]))
    assert c["expires"] > time.time() + 300 * 24 * 3600


def test_chrome_direct_sqlite_import_export_plaintext_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(cookies, "DATA_DIR", str(tmp_path))
    payload = json.dumps(
        [
            _ext_cookie(domain=".muse.ai", name="hatch_gw", value="gw-token-123"),
            _ext_cookie(domain=".muse.ai", name="hatch_sess", value="sess-xyz-999"),
        ]
    )
    count = cookies.import_cookies("chrome_prof", "windows", payload, engine="chrome")
    assert count == 2
    db_file = tmp_path / "chrome_prof" / "Default" / "Network" / "Cookies"
    assert db_file.is_file()

    exported_text, exported_count = cookies.export_cookies(
        "chrome_prof", "windows", fmt="json", engine="chrome"
    )
    assert exported_count == 2
    items = json.loads(exported_text)
    by_name = {it["name"]: it["value"] for it in items}
    assert by_name == {"hatch_gw": "gw-token-123", "hatch_sess": "sess-xyz-999"}

    summary = cookies.inspect_cookie_summary("chrome_prof")
    assert summary["total"] == 2
    assert summary["has_gw"] is True
    assert summary["has_session"] is True


def test_chrome_os_crypt_v10_aes_gcm_with_sha256_domain_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(cookies, "DATA_DIR", str(tmp_path))
    prof_dir = tmp_path / "chrome_enc"
    prof_dir.mkdir(parents=True)

    raw_aes_key = secrets.token_bytes(32)
    if sys.platform == "win32":
        dpapi_blob = cookies._dpapi_protect(raw_aes_key)
        assert dpapi_blob is not None
        enc_key_bytes = b"DPAPI" + dpapi_blob
    else:
        enc_key_bytes = raw_aes_key

    (prof_dir / "Local State").write_text(
        json.dumps(
            {"os_crypt": {"encrypted_key": base64.b64encode(enc_key_bytes).decode("ascii")}}
        ),
        encoding="utf-8",
    )

    payload = json.dumps(
        [
            _ext_cookie(domain=".meta.com", name="datr", value="meta_datr_secret_value"),
            _ext_cookie(domain=".muse.ai", name="hatch_sess", value="encrypted_muse_sess"),
        ]
    )
    imported = cookies.import_cookies("chrome_enc", "windows", payload, engine="chrome")
    assert imported == 2

    # Verify the value on disk is actually encrypted (value == "" and encrypted_value starts with b"v10")
    import sqlite3

    conn = sqlite3.connect(str(prof_dir / "Default" / "Network" / "Cookies"))
    try:
        rows = conn.execute("SELECT name, value, encrypted_value FROM cookies").fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    for _name, plain_val, enc_val in rows:
        assert plain_val == ""
        assert bytes(enc_val).startswith(b"v10")

    # Verify read-only SQLite export decrypts v10 AES-GCM + strips the 32-byte SHA-256(host_key) prefix
    read_back = cookies.read_chrome_cookies_sqlite_readonly("chrome_enc")
    assert read_back is not None
    assert {c["name"]: c["value"] for c in read_back} == {
        "datr": "meta_datr_secret_value",
        "hatch_sess": "encrypted_muse_sess",
    }


def test_chrome_os_crypt_linux_pbkdf2_cbc_decryption():
    from cryptography.hazmat.primitives import hashes, padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    host_key = ".facebook.com"
    secret = "sb_cookie_val_42"
    domain_digest = hashlib.sha256(host_key.encode("utf-8")).digest()
    plaintext = domain_digest + secret.encode("utf-8")

    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1)
    cbc_key = kdf.derive(b"peanuts")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(cbc_key), modes.CBC(b" " * 16))
    enc = cipher.encryptor()
    encrypted_value = b"v10" + enc.update(padded) + enc.finalize()

    decrypted = cookies._decrypt_chrome_cookie_value(encrypted_value, host_key, None)
    assert decrypted == secret
