from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ...core.config import LAUNCH_WAIT_TIMEOUT
from ...core.logging import get_logger
from ...services.browser.launcher import ProfileBusyError
from ..dependencies import (
    get_browser_launcher,
    get_event_bus,
    get_monitor_service,
    get_profile_manager,
)
from ..helpers import require_profile
from ..schemas.browser import (
    BrowserStatusResponse,
    LaunchResponse,
    RunningBrowsersResponse,
)
from ..schemas.common import ErrorResponse, SuccessResponse

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager
    from ...services.monitor import MonitorService

logger = get_logger("api.browser")

router = APIRouter(prefix="/browser", tags=["browser"])


def _api_log(msg: str) -> None:
    logger.info("[browser] %s", msg)


@router.get("", response_model=RunningBrowsersResponse)
def list_running(
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> RunningBrowsersResponse:
    names = sorted(bl.running_profile_names())
    return RunningBrowsersResponse(running=names, count=len(names))


@router.post("/arrange")
def arrange_windows(
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> dict:
    """Tile all running browser windows on screen without changing viewport sizes (SWP_NOSIZE)."""
    ordered = [p.name for p in pm.list_profiles() if bl.is_running(p.name)]
    cnt = bl.arrange_windows(ordered) if hasattr(bl, "arrange_windows") else 0
    return {
        "success": True,
        "arranged": cnt,
        "message": f"Arranged {cnt} running browser window(s)",
    }


@router.get(
    "/{name}/status",
    response_model=BrowserStatusResponse,
    responses={404: {"model": ErrorResponse}},
)
def browser_status(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> BrowserStatusResponse:
    require_profile(name, pm)
    return BrowserStatusResponse(name=name, is_running=bl.is_running(name))


@router.get(
    "/{name}/prelaunch-check",
    responses={404: {"model": ErrorResponse}},
)
def prelaunch_check(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    mon: MonitorService = Depends(get_monitor_service),
) -> dict:
    """Run the 4-layer Pre-Launch Safety Guard on a profile without launching it."""
    profile = require_profile(name, pm)
    warnings = mon.check_prelaunch(
        profile,
        pm.list_profiles(),
        bl.running_profile_names(),
    )
    return {
        "name": name,
        "safe": len(warnings) == 0,
        "warnings": [
            {"code": w.code, "level": w.level, "message": w.message} for w in warnings
        ],
    }


@router.post(
    "/{name}/focus",
    responses={404: {"model": ErrorResponse}},
)
def focus_window(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> dict:
    """Bring a running profile's browser window to the foreground (OS PID level)."""
    require_profile(name, pm)
    if not bl.is_running(name):
        raise HTTPException(status_code=409, detail="Browser is not running")
    ok = bool(bl.focus_profile(name)) if hasattr(bl, "focus_profile") else False
    return {"success": ok, "name": name}


@router.post(
    "/{name}/launch",
    response_model=LaunchResponse,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        502: {"model": LaunchResponse},
    },
)
def launch_browser(
    name: str,
    response: Response,
    wait: bool = Query(
        True,
        description=(
            "If true (default), block until the browser is ready or has failed, "
            f"up to {LAUNCH_WAIT_TIMEOUT}s (FOXPROFILE_LAUNCH_TIMEOUT). "
            "Pass wait=false to return 202 immediately and poll GET /{name}/status."
        ),
    ),
    check_safety: bool | None = Query(
        None,
        description=(
            "If true, run the 4-layer Pre-Launch Safety Guard "
            "(no_proxy, port_busy, port_cooldown, proxy_changed_with_session, missing_geo)."
        ),
    ),
    force: bool = Query(
        False,
        description="If true, bypass Pre-Launch Safety Guard warnings and acknowledge proxy change.",
    ),
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    mon: MonitorService = Depends(get_monitor_service),
    bus: EventBus = Depends(get_event_bus),
) -> LaunchResponse:
    profile = require_profile(name, pm)
    if bl.is_running(name):
        raise HTTPException(status_code=409, detail="Browser already running")

    all_profiles = pm.list_profiles()
    should_check = check_safety if check_safety is not None else any(p.proxy for p in all_profiles)
    if should_check and not force:
        warnings = mon.check_prelaunch(
            profile,
            all_profiles,
            bl.running_profile_names(),
        )
        if warnings:
            details = " | ".join(f"[{w.code}] {w.message}" for w in warnings)
            raise HTTPException(
                status_code=409,
                detail=f"Pre-Launch Safety Guard blocked launch for '{name}': {details}",
            )
    elif force:
        mon.acknowledge_proxy_change(name, profile.proxy)

    ready_state = {"started": False}

    def _on_ready() -> None:
        ready_state["started"] = True
        mon.record_started(name, profile.proxy)
        bus.emit()

    def _on_stop() -> None:
        if ready_state["started"]:
            mon.record_stopped(name)
        else:
            mon.record_launch_failed(name)
        bus.emit()

    try:
        started = bl.start_thread(profile, _api_log, on_ready=_on_ready, on_stop=_on_stop)
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail="Profile is busy") from e
    if not started:
        raise HTTPException(status_code=409, detail="Browser already running")
    logger.info("API launched browser for: %s", name)
    bus.emit()

    if not wait:
        response.status_code = 202
        return LaunchResponse(success=True, message=f"Browser launching for '{name}'")

    ok, error = bl.wait_for_launch(name, LAUNCH_WAIT_TIMEOUT)
    if ok:
        return LaunchResponse(success=True, message=f"Browser started for '{name}'")
    if error == "timeout":
        response.status_code = 202
        return LaunchResponse(
            success=True,
            message=f"Browser for '{name}' is still starting",
        )
    response.status_code = 502
    return LaunchResponse(success=False, message=f"Launch failed: {error}")


@router.post(
    "/{name}/stop",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def stop_browser(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    mon: MonitorService = Depends(get_monitor_service),
    bus: EventBus = Depends(get_event_bus),
) -> SuccessResponse:
    require_profile(name, pm)
    if not bl.is_running(name):
        raise HTTPException(status_code=409, detail="Browser is not running")

    bl.stop_profile(name)
    mon.record_stopped(name)
    logger.info("API stopped browser for: %s", name)
    bus.emit()
    return SuccessResponse(message=f"Browser stopped for '{name}'")
