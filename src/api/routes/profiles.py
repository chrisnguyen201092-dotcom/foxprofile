from __future__ import annotations

import os
import pathlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from ...core.config import DATA_DIR
from ...core.logging import get_logger
from ...models.profile import ENGINES
from ...services.browser import chrome_prefetch, cookies, fingerprint
from ...services.browser.launcher import ProfileBusyError
from ...services.proxy.geo_check import check_geo
from ...utils.totp import fetch_hotmail_messages, generate_totp
from ...utils.validation import (
    validate_color,
    validate_group,
    validate_locale,
    validate_notes,
    validate_profile_name,
    validate_proxy_format,
    validate_timezone,
)
from ...utils.warmup_sites import load_warmup_text, parse_warmup_groups
from ..dependencies import (
    get_browser_launcher,
    get_event_bus,
    get_monitor_service,
    get_profile_manager,
)
from ..helpers import build_profile_response, profile_response, require_profile
from ..schemas.common import ErrorResponse, SuccessResponse
from ..schemas.profiles import (
    BulkProfileOpsRequest,
    CookieImportRequest,
    CookieImportResponse,
    CustomPageMembersRequest,
    CustomPageOrderRequest,
    CustomPageRequest,
    DataDirResponse,
    ExportRequest,
    ExportResponse,
    FingerprintResponse,
    ImportRequest,
    ImportResponse,
    ProfileCreate,
    ProfileListResponse,
    ProfileMoveRequest,
    ProfileOrderRequest,
    ProfileResponse,
    ProfileUpdate,
)
from ..schemas.proxy import GeoCheckResponse

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager
    from ...services.monitor import MonitorService

logger = get_logger("api.profiles")

router = APIRouter(prefix="/profiles", tags=["profiles"])

_OS_TYPES = ("windows", "macos", "linux")


def _require_os(os_type: str) -> None:
    if os_type not in _OS_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"os_type must be one of: {', '.join(_OS_TYPES)}",
        )


def _require_engine(engine: str) -> None:
    if engine not in ENGINES:
        raise HTTPException(
            status_code=400,
            detail=f"engine must be one of: {', '.join(ENGINES)}",
        )
    if engine == "chrome" and (reason := chrome_prefetch.unsupported_reason()):
        raise HTTPException(status_code=400, detail=f"Chrome engine unavailable here: {reason}")


def _require_geo(timezone: str | None, locale: str | None) -> None:
    for validate, value in ((validate_timezone, timezone), (validate_locale, locale)):
        valid, msg = validate(value or "")
        if not valid:
            raise HTTPException(status_code=400, detail=msg)


def _require_color(color: str | None) -> None:
    valid, msg = validate_color(color or "")
    if not valid:
        raise HTTPException(status_code=400, detail=msg)


def _require_meta(group: str | None, notes: str | None) -> None:
    for validate, value in ((validate_group, group), (validate_notes, notes)):
        valid, msg = validate(value or "")
        if not valid:
            raise HTTPException(status_code=400, detail=msg)


@router.get("", response_model=ProfileListResponse)
def list_profiles(
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> ProfileListResponse:
    profiles = [profile_response(p, bl) for p in pm.list_profiles()]
    return ProfileListResponse(
        profiles=profiles,
        total=len(profiles),
        custom_pages=pm.list_custom_pages(),
    )


@router.get("/monitor/overview")
def monitor_overview(
    page: str | None = Query(None, description="Filter by custom page name"),
    status: str | None = Query(None, description="Filter by status tag"),
    only_ready: bool = Query(
        False,
        description="If true, only return profiles that are ready to launch safely",
    ),
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    mon: MonitorService = Depends(get_monitor_service),
) -> dict:
    """Operational overview of all profiles (cooldowns, port collisions, exit IPs, readiness)."""
    all_profiles = pm.list_profiles()
    running = bl.running_profile_names()
    snap = mon.snapshot(all_profiles, running)
    cd_target = mon.cooldown_minutes

    items: list[dict] = []
    ready_count = 0
    cooldown_count = 0
    warning_count = 0

    for p in all_profiles:
        if page and not any(pg.casefold() == page.strip().casefold() for pg in p.pages):
            continue
        if status and (p.status or "").strip().casefold() != status.strip().casefold():
            continue
        info = snap.profiles.get(p.name)
        is_run = p.name in running
        in_cooldown = bool(
            info
            and not is_run
            and (
                info.busy_by
                or info.port_cooldown_by is not None
                or (
                    cd_target > 0
                    and info.cooldown_mins is not None
                    and info.cooldown_mins < cd_target
                )
            )
        )
        has_warn = bool(info and info.has_warning)
        is_ready = bool(p.proxy and not is_run and not in_cooldown and not has_warn)

        if is_ready:
            ready_count += 1
        if in_cooldown:
            cooldown_count += 1
        if has_warn:
            warning_count += 1

        if only_ready and not is_ready:
            continue

        items.append(
            {
                "name": p.name,
                "status": p.status,
                "group": p.group,
                "pages": list(p.pages),
                "pinned": bool(getattr(p, "pinned", False)),
                "proxy_port": info.port if info else "",
                "is_running": is_run,
                "ready_to_launch": is_ready,
                "in_cooldown": in_cooldown,
                "cooldown_mins": info.cooldown_mins if info else None,
                "busy_by": list(info.busy_by) if info else [],
                "port_cooldown_by": (
                    {"profile": info.port_cooldown_by[0], "minutes_left": info.port_cooldown_by[1]}
                    if info and info.port_cooldown_by
                    else None
                ),
                "has_warning": has_warn,
                "last_exit_ip": info.last_exit_ip if info else "",
                "last_exit_country": info.last_exit_country if info else "",
                "last_exit_ip_type": info.last_exit_ip_type if info else "",
                "ip_fraud_score": info.ip_fraud_score if info else None,
                "ip_scamalytics": info.ip_scamalytics if info else None,
                "ip_proxycheck_risk": info.ip_proxycheck_risk if info else None,
                "ip_proxy_flag": bool(info.ip_proxy_flag) if info else False,
                "trust_score": info.trust_score if info else 0,
                "trust_tier": info.trust_tier if info else "cold",
                "audit_codes": list(info.audit_codes) if info else [],
                "today_minutes": info.today_minutes if info else 0,
            }
        )

    return {
        "total_matched": len(items),
        "running_count": len(running),
        "ready_count": ready_count,
        "cooldown_count": cooldown_count,
        "warning_count": warning_count,
        "diamond_count": snap.diamond_count,
        "elite_count": snap.elite_count,
        "platinum_count": snap.platinum_count,
        "trusted_count": snap.trusted_count,
        "clean_ip_count": snap.clean_ip_count,
        "scored_ip_count": snap.scored_ip_count,
        "cooldown_target_minutes": cd_target,
        "proxy_collisions": [
            {"port": port, "profiles": names} for port, names in snap.proxy_collisions
        ],
        "audit_findings": [
            {
                "code": f.code,
                "level": f.level,
                "title": f.title,
                "detail": f.detail,
                "profiles": list(f.profiles),
            }
            for f in snap.audit_findings
        ],
        "profiles": items,
    }


@router.get("/warmup-sites")
def get_warmup_sites() -> dict:
    """List categorized IPv6 & _GRECAPTCHA warmup websites."""
    groups = parse_warmup_groups(load_warmup_text())
    return {"groups": groups, "total_groups": len(groups)}


@router.post("/bulk-ops")
def bulk_profile_ops(
    body: BulkProfileOpsRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    """Update status, group, pinned state, or page membership for one or more profiles."""
    supplied = body.model_dump(exclude_unset=True)
    names = [n for n in body.names if n in pm.profiles]
    if not names:
        raise HTTPException(status_code=404, detail="No matching profiles found")

    updated: dict[str, int] = {}
    if "status" in supplied:
        st = (body.status or "").strip() or None
        updated["status"] = pm.bulk_update_status(names, st)
    if "group" in supplied:
        grp = (body.group or "").strip() or None
        updated["group"] = pm.bulk_update_group(names, grp)
    if "pinned" in supplied and body.pinned is not None:
        updated["pinned"] = pm.bulk_update_pinned(names, bool(body.pinned))
    if body.add_to_page:
        ok = pm.add_profiles_to_page(body.add_to_page.strip(), names)
        updated["added_to_page"] = len(names) if ok else 0
    if body.remove_from_page:
        ok = pm.remove_profiles_from_page(body.remove_from_page.strip(), names)
        updated["removed_from_page"] = len(names) if ok else 0

    bus.emit()
    return {"success": True, "profiles": names, "updated": updated}


@router.post("/pages", response_model=SuccessResponse)
def save_custom_page(
    body: CustomPageRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> SuccessResponse:
    valid, msg = validate_group(body.name.strip())
    if not body.name.strip() or not valid:
        raise HTTPException(status_code=400, detail=msg or "Page name cannot be empty")
    if not pm.save_custom_page(body.name, body.profile_names, body.original_name):
        raise HTTPException(status_code=409, detail="Page name already exists or invalid")
    bus.emit()
    return SuccessResponse(message=f"Saved page '{body.name.strip()}'")


@router.put("/pages/reorder", response_model=SuccessResponse)
def reorder_custom_pages(
    body: CustomPageOrderRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> SuccessResponse:
    pm.reorder_custom_pages(body.ordered_pages)
    bus.emit()
    return SuccessResponse(message="Reordered custom pages")


@router.delete("/pages/{page_name}", response_model=SuccessResponse)
def delete_custom_page(
    page_name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> SuccessResponse:
    if not pm.delete_custom_page(page_name):
        raise HTTPException(status_code=404, detail=f"Page '{page_name}' not found")
    bus.emit()
    return SuccessResponse(message=f"Deleted page '{page_name}'")


@router.post("/pages/{page_name}/members", response_model=SuccessResponse)
def add_profiles_to_page(
    page_name: str,
    body: CustomPageMembersRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> SuccessResponse:
    if not pm.add_profiles_to_page(page_name, body.profile_names):
        raise HTTPException(status_code=400, detail="Invalid page name")
    bus.emit()
    return SuccessResponse(message=f"Added profiles to '{page_name}'")


@router.post("/pages/{page_name}/remove-members", response_model=SuccessResponse)
def remove_profiles_from_page(
    page_name: str,
    body: CustomPageMembersRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> SuccessResponse:
    if not pm.remove_profiles_from_page(page_name, body.profile_names):
        raise HTTPException(status_code=404, detail=f"Page '{page_name}' not found")
    bus.emit()
    return SuccessResponse(message=f"Removed profiles from '{page_name}'")


@router.put("/order", response_model=SuccessResponse)
def reorder_profiles(
    body: ProfileOrderRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> SuccessResponse:
    pm.reorder_profiles(body.names)
    bus.emit()
    return SuccessResponse(message="Profile order updated")


@router.post(
    "",
    response_model=ProfileResponse,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def create_profile(
    body: ProfileCreate,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> ProfileResponse:
    valid, msg = validate_profile_name(body.name)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)
    _require_os(body.os_type)
    _require_engine(body.engine)
    _require_geo(body.timezone, body.locale)
    _require_color(body.color)
    _require_meta(body.group, body.notes)

    if body.proxy:
        valid, msg = validate_proxy_format(body.proxy)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)

    if not pm.add_profile(
        body.name,
        body.proxy or "",
        body.os_type,
        body.timezone or None,
        body.locale or None,
        body.engine,
        body.color or None,
        (body.group or "").strip() or None,
        (body.notes or "").strip() or None,
        body.pages,
        status=(body.status or "").strip() or None,
        pinned=bool(body.pinned),
    ):
        raise HTTPException(status_code=409, detail="Profile already exists")

    logger.info("API created profile: %s", body.name)
    if body.engine == "chrome":
        chrome_prefetch.start()
    bus.emit()
    return build_profile_response(body.name, pm, bl)


@router.post(
    "/{name}/move",
    response_model=SuccessResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def move_profile(
    name: str,
    body: ProfileMoveRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> SuccessResponse:
    require_profile(name, pm)
    if body.direction == 0:
        raise HTTPException(status_code=400, detail="direction must be non-zero")
    pm.move_profile(name, body.direction)
    bus.emit()
    return SuccessResponse(message=f"Moved profile '{name}'")


@router.get(
    "/{name}/totp",
    responses={404: {"model": ErrorResponse}},
)
def get_profile_totp(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
) -> dict:
    """Generate the current 6-digit RFC 6238 2FA code for a profile."""
    profile = require_profile(name, pm)
    pair = generate_totp(getattr(profile, "two_fa", None))
    if pair is None:
        return {
            "name": name,
            "has_2fa": False,
            "code": None,
            "remaining_seconds": 0,
        }
    code, rem = pair
    return {
        "name": name,
        "has_2fa": True,
        "code": code,
        "remaining_seconds": rem,
    }


@router.post(
    "/{name}/mail-otp",
    responses={404: {"model": ErrorResponse}},
)
def fetch_profile_mail_otp(
    name: str,
    top: int = Query(8, ge=1, le=20),
    pm: IProfileManager = Depends(get_profile_manager),
) -> dict:
    """Read the profile's Hotmail/Outlook inbox via Microsoft OAuth2 and extract the latest OTP."""
    profile = require_profile(name, pm)
    ok, otp_or_err, messages = fetch_hotmail_messages(
        getattr(profile, "email", None),
        getattr(profile, "mail_token", None),
        top=top,
    )
    if not ok:
        return {
            "name": name,
            "email": getattr(profile, "email", None),
            "success": False,
            "error": otp_or_err,
            "latest_otp": "",
            "messages": [],
        }
    return {
        "name": name,
        "email": getattr(profile, "email", None),
        "success": True,
        "error": "",
        "latest_otp": otp_or_err,
        "messages": messages,
    }


@router.get(
    "/{name}/history",
    responses={404: {"model": ErrorResponse}},
)
def get_profile_history(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    mon: MonitorService = Depends(get_monitor_service),
) -> dict:
    """Return session history and exit IP history for a profile."""
    profile = require_profile(name, pm)
    sessions = mon.get_profile_sessions(
        name,
        is_running=bl.is_running(name),
        current_proxy=profile.proxy,
    )
    ip_history = mon.get_ip_history(name)
    return {
        "name": name,
        "sessions": sessions,
        "ip_history": ip_history,
    }


@router.get(
    "/{name}",
    response_model=ProfileResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_profile(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> ProfileResponse:
    require_profile(name, pm)
    return build_profile_response(name, pm, bl)


@router.patch(
    "/{name}",
    response_model=ProfileResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def update_profile(
    name: str,
    body: ProfileUpdate,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> ProfileResponse:
    profile = require_profile(name, pm)
    supplied = body.model_dump(exclude_unset=True)

    new_name = supplied.get("name", name)
    new_proxy = supplied.get("proxy", profile.proxy)
    new_os = supplied.get("os_type", profile.os_type)
    new_timezone = supplied.get("timezone", profile.timezone)
    new_locale = supplied.get("locale", profile.locale)
    new_color = supplied.get("color", profile.color)
    new_group = supplied.get("group", profile.group)
    new_notes = supplied.get("notes", profile.notes)

    if "name" in supplied:
        valid, msg = validate_profile_name(new_name)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)
        if new_name != name and bl.is_running(name):
            raise HTTPException(
                status_code=409,
                detail="Stop the browser before renaming",
            )

    if supplied.get("engine") not in (None, profile.engine):
        raise HTTPException(
            status_code=400,
            detail="A profile's engine cannot be changed; create a new profile instead",
        )
    if "os_type" in supplied:
        _require_os(new_os)
    _require_geo(supplied.get("timezone"), supplied.get("locale"))
    _require_color(supplied.get("color"))
    _require_meta(supplied.get("group"), supplied.get("notes"))

    if "proxy" in supplied and new_proxy:
        valid, msg = validate_proxy_format(new_proxy)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)

    if not pm.update_profile(
        name,
        new_name,
        new_proxy or "",
        new_os,
        new_timezone or None,
        new_locale or None,
        new_color or None,
        (new_group or "").strip() or None,
        (new_notes or "").strip() or None,
        supplied.get("pages"),
        **(
            {"new_status": (supplied.get("status") or "").strip() or None}
            if "status" in supplied
            else {}
        ),
        **(
            {"new_pinned": bool(supplied["pinned"])}
            if "pinned" in supplied and supplied.get("pinned") is not None
            else {}
        ),
    ):
        raise HTTPException(status_code=409, detail="Update failed (name conflict?)")

    logger.info("API updated profile: %s -> %s", name, new_name)
    bus.emit()
    return build_profile_response(new_name, pm, bl)


@router.delete(
    "/{name}",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def delete_profile(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> SuccessResponse:
    require_profile(name, pm)
    if bl.is_running(name):
        raise HTTPException(
            status_code=409,
            detail="Stop the browser before deleting",
        )
    pm.delete_profile(name)
    logger.info("API deleted profile: %s", name)
    bus.emit()
    return SuccessResponse(message=f"Profile '{name}' deleted")


@router.get(
    "/{name}/data-dir",
    response_model=DataDirResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_data_dir(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
) -> DataDirResponse:
    require_profile(name, pm)
    data_dir = os.path.join(os.getcwd(), DATA_DIR, name)
    return DataDirResponse(
        name=name,
        data_dir=data_dir,
        exists=pathlib.Path(data_dir).exists(),
    )


@router.post(
    "/{name}/export",
    response_model=ExportResponse,
    responses={404: {"model": ErrorResponse}},
)
def export_profile(
    name: str,
    body: ExportRequest,
    pm: IProfileManager = Depends(get_profile_manager),
) -> ExportResponse:
    require_profile(name, pm)
    if not pathlib.Path(body.export_dir).is_dir():
        raise HTTPException(status_code=400, detail="export_dir is not a directory")

    success, result = pm.export_profile(name, body.export_dir, body.include_data)
    if success:
        logger.info("API exported profile: %s -> %s", name, result)
        return ExportResponse(success=True, zip_path=result)
    return ExportResponse(success=False, error=result)


@router.post(
    "/import",
    response_model=ImportResponse,
    responses={400: {"model": ErrorResponse}},
)
def import_profile(
    body: ImportRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> ImportResponse:
    if not pathlib.Path(body.zip_path).is_file():
        raise HTTPException(status_code=400, detail="zip_path is not a file")

    success, result = pm.import_profile(body.zip_path, body.overwrite)
    if success:
        logger.info("API imported profile: %s", result)
        bus.emit()
        return ImportResponse(success=True, profile_name=result)
    return ImportResponse(success=False, error=result)


def _require_stopped(name: str, bl: IBrowserLauncher, action: str) -> None:
    if bl.is_running(name):
        raise HTTPException(status_code=409, detail=f"Stop the browser before {action}")


_BUSY_DETAIL = "Profile is running or busy with another operation"


@router.get(
    "/{name}/cookies",
    response_class=PlainTextResponse,
    responses={
        200: {"content": {"text/plain": {}, "application/json": {}}},
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
def export_cookies(
    name: str,
    format: str = Query("json", pattern="^(json|netscape)$"),
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> PlainTextResponse:
    """Export cookies as Cookie-Editor JSON or Netscape cookies.txt."""
    profile = require_profile(name, pm)
    _require_stopped(name, bl, "exporting cookies")
    try:
        with bl.exclusive(name):
            text, count = cookies.export_cookies(name, profile.os_type, format, profile.engine)
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail=_BUSY_DETAIL) from e
    except cookies.CookieError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    media = "application/json" if format == "json" else "text/plain"
    return PlainTextResponse(text, media_type=media, headers={"X-Cookie-Count": str(count)})


@router.post(
    "/{name}/cookies",
    response_model=CookieImportResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def import_cookies(
    name: str,
    body: CookieImportRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> CookieImportResponse:
    """Import cookies from JSON or Netscape text into a stopped profile."""
    profile = require_profile(name, pm)
    _require_stopped(name, bl, "importing cookies")
    try:
        with bl.exclusive(name):
            count = cookies.import_cookies(name, profile.os_type, body.content, profile.engine)
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail=_BUSY_DETAIL) from e
    except cookies.CookieError as e:
        return CookieImportResponse(success=False, error=str(e))
    logger.info("API imported %d cookies into %s", count, name)
    return CookieImportResponse(success=True, imported=count)


@router.get(
    "/{name}/fingerprint",
    response_model=FingerprintResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_fingerprint(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
) -> FingerprintResponse:
    """Show the fingerprint the profile presents on every launch."""
    require_profile(name, pm)
    info = fingerprint.summary(os.path.join(DATA_DIR, name))
    if info is None:
        return FingerprintResponse(name=name, exists=False)
    return FingerprintResponse(name=name, exists=True, **info)


@router.delete(
    "/{name}/fingerprint",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def reset_fingerprint(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> SuccessResponse:
    """Discard the saved fingerprint; the next launch generates a new device."""
    require_profile(name, pm)
    _require_stopped(name, bl, "resetting the fingerprint")
    removed = fingerprint.reset(os.path.join(DATA_DIR, name))
    logger.info("API reset fingerprint for %s (existed=%s)", name, removed)
    return SuccessResponse(message=f"Fingerprint reset for '{name}'")


@router.get(
    "/{name}/ip-check",
    response_model=GeoCheckResponse,
    responses={404: {"model": ErrorResponse}},
)
def check_profile_ip(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
) -> GeoCheckResponse:
    """Where GeoIP sources place the profile's exit IP vs. the timezone/locale it presents.

    Goes out through the profile's proxy to Cloudflare, ipinfo and ip-api (~10s).
    """
    profile = require_profile(name, pm)
    return GeoCheckResponse.from_result(check_geo(profile.proxy, profile.timezone, profile.locale))
