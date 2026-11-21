from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query

from ...services.monitor import (
    extract_proxy_cluster,
    extract_proxy_endpoint,
    extract_proxy_port,
    is_high_risk_embryo_profile,
)
from ...services.proxy.geo_check import check_geo
from ...utils.proxy_parser import mask_proxy
from ...utils.validation import validate_locale, validate_proxy_format, validate_timezone
from ..dependencies import (
    get_browser_launcher,
    get_event_bus,
    get_monitor_service,
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
    ProxyQuarantineSwapRequest,
)

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager, IProxyService
    from ...services.monitor import MonitorService

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
    mon: MonitorService = Depends(get_monitor_service),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    """Assign a proxy to a stopped profile or replace a dead proxy/port across stopped profiles."""
    valid, msg = validate_proxy_format(body.new_proxy)
    if not valid or not body.new_proxy.strip():
        raise HTTPException(status_code=400, detail=msg or "Invalid proxy")

    running = bl.running_profile_names()
    snap = mon.snapshot(pm.list_profiles(), running, fast_init=True) if not body.force else None
    ep_new = extract_proxy_endpoint(body.new_proxy)
    if not body.force and snap is not None and ep_new and ep_new in snap.frozen_endpoints:
        raise HTTPException(
            status_code=409,
            detail=f"Endpoint '{ep_new}' is in 72h Dead-Port Freeze; pass force=true to override",
        )

    if body.profile:
        if body.profile not in pm.profiles:
            raise HTTPException(status_code=404, detail=f"Profile '{body.profile}' not found")
        if body.profile in running:
            raise HTTPException(
                status_code=409,
                detail=f"Stop '{body.profile}' before changing its proxy",
            )
        if not body.force and snap is not None:
            prof_obj = pm.profiles[body.profile]
            cl_new = extract_proxy_cluster(body.new_proxy)
            if is_high_risk_embryo_profile(prof_obj) and cl_new.startswith("usa") and cl_new[3:].isdigit():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"ASN Firewall blocked high-risk profile '{body.profile}' "
                        f"on clean '{cl_new}' subnet; pass force=true to override"
                    ),
                )
            info = snap.profiles.get(body.profile)
            if info and (info.hatch_gw or info.hatch_sess):
                old_ep = extract_proxy_endpoint(prof_obj.proxy)
                if old_ep and ep_new != old_ep:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Profile '{body.profile}' has an active saved session on "
                            f"'{old_ep}'; pass force=true to override"
                        ),
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


@router.get("/pool/quarantine")
def list_quarantined_pool(
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    mon: MonitorService = Depends(get_monitor_service),
) -> dict:
    """List quarantined endpoints and ranked clean spare proxies."""
    quarantined = pm.list_quarantined_proxies()
    unassigned = pm.list_unassigned_proxies()
    clean_spares = mon.rank_clean_spare_proxies(
        unassigned,
        pm.list_profiles(),
        bl.running_profile_names(),
        quarantined_endpoints=set(quarantined.keys()),
    )
    return {
        "quarantined": quarantined,
        "quarantined_count": len(quarantined),
        "clean_spares": [mask_proxy(px) for px in clean_spares],
        "clean_spare_count": len(clean_spares),
    }


@router.post("/pool/quarantine-swap")
def quarantine_and_swap(
    body: ProxyQuarantineSwapRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    mon: MonitorService = Depends(get_monitor_service),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    """Quarantine a bad proxy/endpoint and cleanly swap stopped active profiles without tainting archived bans."""
    old_target = (body.old_target or "").strip()
    if not old_target:
        raise HTTPException(status_code=400, detail="old_target is required")
    running = bl.running_profile_names()
    quarantined = pm.list_quarantined_proxies()
    profiles_list = pm.list_profiles()
    snap = mon.snapshot(profiles_list, running, fast_init=True)

    old_ep = "" if old_target.startswith(":") else extract_proxy_endpoint(old_target)
    old_port = old_target if old_target.startswith(":") else ""
    matching_active = []
    for p in profiles_list:
        cur = (p.proxy or "").strip()
        if not cur:
            continue
        is_ban = bool(info := snap.profiles.get(p.name)) and info.is_banned
        if is_ban:
            continue
        hit = (
            (extract_proxy_port(cur) == old_port)
            if old_port
            else (cur == old_target or (old_ep and extract_proxy_endpoint(cur) == old_ep))
        )
        if hit:
            matching_active.append(p)

    running_hits = [p.name for p in matching_active if p.name in running]
    if running_hits:
        raise HTTPException(
            status_code=409,
            detail=f"Stop running profile(s) first: {', '.join(running_hits)}",
        )

    for_high_risk = any(is_high_risk_embryo_profile(p) for p in matching_active)
    chosen_px = (body.new_proxy or "").strip()
    if chosen_px:
        valid, msg = validate_proxy_format(chosen_px)
        if not valid:
            raise HTTPException(status_code=400, detail=msg or "Invalid replacement proxy")
        ep_new = extract_proxy_endpoint(chosen_px)
        if not body.force and ep_new and ep_new in snap.frozen_endpoints:
            raise HTTPException(
                status_code=409,
                detail=f"Replacement endpoint '{ep_new}' is in 72h Dead-Port Freeze",
            )
    else:
        spares = mon.rank_clean_spare_proxies(
            pm.list_unassigned_proxies(),
            profiles_list,
            running,
            quarantined_endpoints=set(quarantined.keys()),
            for_high_risk=for_high_risk,
            snap=snap,
        )
        if not spares:
            raise HTTPException(
                status_code=409,
                detail="No clean, unfrozen spare proxy available in pool",
            )
        chosen_px = spares[0]

    updated = pm.replace_proxy_across_profiles(
        old_target,
        chosen_px,
        skip_names=running,
        preserve_archived=True,
        quarantine_old=True,
        quarantine_reason=body.reason,
    )
    bus.emit()
    return {
        "success": True,
        "quarantined": old_ep or old_target,
        "new_proxy": mask_proxy(chosen_px),
        "updated_profiles": updated,
    }


@router.delete("/pool/quarantine")
def remove_quarantined_proxy(
    endpoint: str = Query(..., description="Quarantined host:port endpoint or proxy string"),
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    removed = pm.unquarantine_proxy(endpoint)
    if removed:
        bus.emit()
    return {"success": removed, "endpoint": endpoint}


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
