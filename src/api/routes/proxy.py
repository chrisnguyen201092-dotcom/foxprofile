from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException

from ...services.monitor import extract_proxy_port
from ...services.proxy.geo_check import check_geo
from ...utils.validation import validate_locale, validate_proxy_format, validate_timezone
from ..dependencies import (
    get_browser_launcher,
    get_event_bus,
    get_profile_manager,
    get_proxy_service,
)
from ..schemas.proxy import (
    GeoCheckRequest,
    GeoCheckResponse,
    ProxyCheckRequest,
    ProxyCheckResponse,
    ProxyPoolAddRequest,
    ProxyPoolAssignRequest,
)

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager, IProxyService

router = APIRouter(prefix="/proxy", tags=["proxy"])


@router.post("/check", response_model=ProxyCheckResponse)
def check_proxy(
    body: ProxyCheckRequest,
    ps: IProxyService = Depends(get_proxy_service),
) -> ProxyCheckResponse:
    ok, message = ps.check_proxy_sync(body.proxy, body.timeout or 10)
    return ProxyCheckResponse(success=ok, message=message)


@router.get("/pool")
def get_proxy_pool(
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> dict:
    """List unassigned spare proxies in the pool and current port usage across profiles."""
    unassigned = pm.list_unassigned_proxies() if hasattr(pm, "list_unassigned_proxies") else []
    running = bl.running_profile_names()
    ports: dict[str, list[dict]] = {}
    for p in pm.list_profiles():
        port = extract_proxy_port(p.proxy)
        if port:
            ports.setdefault(port, []).append(
                {
                    "profile": p.name,
                    "proxy": p.proxy,
                    "is_running": p.name in running,
                }
            )
    return {
        "unassigned_proxies": unassigned,
        "unassigned_count": len(unassigned),
        "active_ports": ports,
    }


@router.post("/pool")
def add_to_proxy_pool(
    body: ProxyPoolAddRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    """Add spare proxies to the central proxy pool."""
    added, skipped = pm.add_proxies_to_pool(body.proxies)
    if added > 0:
        bus.emit()
    return {
        "added": added,
        "skipped": skipped,
        "unassigned_count": len(pm.list_unassigned_proxies()),
    }


@router.post("/pool/assign")
def assign_from_proxy_pool(
    body: ProxyPoolAssignRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    """Assign a proxy to a stopped profile or replace a dead proxy/port across stopped profiles."""
    valid, msg = validate_proxy_format(body.new_proxy)
    if not valid or not body.new_proxy.strip():
        raise HTTPException(status_code=400, detail=msg or "Invalid proxy")

    running = bl.running_profile_names()
    if body.profile:
        if body.profile not in pm.profiles:
            raise HTTPException(status_code=404, detail=f"Profile '{body.profile}' not found")
        if body.profile in running:
            raise HTTPException(
                status_code=409,
                detail=f"Stop '{body.profile}' before changing its proxy",
            )
        cnt = pm.bulk_update_proxies({body.profile: body.new_proxy.strip()})
        bus.emit()
        return {"success": cnt > 0, "updated_profiles": [body.profile] if cnt > 0 else []}

    if body.old_target:
        updated = pm.replace_proxy_across_profiles(
            body.old_target,
            body.new_proxy.strip(),
            skip_names=running,
        )
        if updated:
            bus.emit()
        return {"success": len(updated) > 0, "updated_profiles": updated}

    raise HTTPException(
        status_code=400,
        detail="Provide either 'profile' or 'old_target' to assign the proxy",
    )


@router.post("/geo-check", response_model=GeoCheckResponse)
def geo_check(body: GeoCheckRequest) -> GeoCheckResponse:
    """Check an unsaved proxy / timezone / locale combination (see GET /profiles/{name}/ip-check)."""
    for validate, value in (
        (validate_proxy_format, body.proxy),
        (validate_timezone, body.timezone),
        (validate_locale, body.locale),
    ):
        valid, msg = validate(value or "")
        if not valid:
            raise HTTPException(status_code=400, detail=msg)
    return GeoCheckResponse.from_result(
        check_geo(body.proxy or None, body.timezone or None, body.locale or None)
    )
