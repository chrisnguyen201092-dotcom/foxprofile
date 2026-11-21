"""Bind-safety checks for the REST API without importing FastAPI at startup."""

import ipaddress
import socket

from ..core.config import API_TOKEN


def is_loopback(host: str) -> bool:
    """True if every address `host` resolves to is a loopback address."""
    if host == "":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_loopback for i in infos)


def check_bind_safety(host: str, token: str = API_TOKEN) -> None:
    """Refuse to expose the API beyond this machine without a token."""
    if not is_loopback(host) and not token:
        raise SystemExit(
            f"Refusing to listen on {host} without FOXPROFILE_API_TOKEN: anyone who can "
            "reach this port could control every profile and read every cookie. "
            "Set FOXPROFILE_API_TOKEN (at least 24 random characters) or use 127.0.0.1.",
        )
    if token and len(token) < 24:
        raise SystemExit("FOXPROFILE_API_TOKEN must be at least 24 characters long.")
