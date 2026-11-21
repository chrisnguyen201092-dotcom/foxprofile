"""Bearer-token authentication for the REST API.

When FOXPROFILE_API_TOKEN is set every /api/v1 route except /health requires
`Authorization: Bearer <token>` (or `X-API-Key: <token>`). Without a token the
API only accepts connections from the local machine; see `check_bind_safety`.
"""

import secrets

from fastapi import HTTPException, Request

from ..core.config import API_TOKEN
from .bind_safety import check_bind_safety, is_loopback

__all__ = ["check_bind_safety", "is_loopback", "require_token"]


def _presented_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def require_token(request: Request) -> None:
    """FastAPI dependency: reject the request unless it carries the API token."""
    if not API_TOKEN:
        return
    if not secrets.compare_digest(_presented_token(request), API_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
