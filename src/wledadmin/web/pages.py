"""Páginas HTML e fragmentos HTMX."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from ..services.actions import ActionKind, BulkAction
from ..services.app_settings import (
    BACKUP_INTERVAL_HOURS,
    BACKUP_KEEP,
    OTA_INCLUDE_PRERELEASES,
    OTA_REPO,
    OTA_STOP_ON_FAILURE,
    SettingsError,
)
from ..services.auth import CredentialError
from ..services.discovery import ScanAlreadyRunning
from . import view
from .operations import OperationError, add_manual, delete_device
from .security import AdminDep, ServicesDep, require_admin

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters.update(
    mac=view.fmt_mac, uptime=view.fmt_uptime, kib=view.fmt_kib, dt=view.fmt_dt
)

router = APIRouter(dependencies=[Depends(require_admin)], include_in_schema=False)

# Avisa a tabela para se atualizar (hx-trigger="... devices-changed from:body").
REFRESH = {"HX-Trigger": "devices-changed"}


async def _base_ctx(services, username: str) -> dict:
    admin = await services.auth.get_admin()
    return {
        "username": username,
        "default_password": bool(admin and admin.is_default),
        "settings": services.settings,
        "backup_interval_hours": services.app_settings.get(BACKUP_INTERVAL_HOURS),
        "backup_keep": services.app_settings.get(BACKUP_KEEP),
        "ota_repo": services.app_settings.get(OTA_REPO),
        "ota_prereleases": services.app_settings.get(OTA_INCLUDE_PRERELEASES),
        "ota_stop_on_failure": services.app_settings.get(OTA_STOP_ON_FAILURE),
        "github_token_set": bool(services.settings.github_token),
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, services: ServicesDep, username: AdminDep):
    rows = view.build_rows(
        await services.registry.list(), services.store,
        services.app_settings.get(BACKUP_INTERVAL_HOURS),
    )
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "devices.html",
        {**ctx, "rows": rows, "selected": set(), "scan": services.scanner.current,
         "mdns_active": services.mdns is not None},
    )


@router.get("/ui/devices/rows", response_class=HTMLResponse)
async def device_rows(
    request: Request,
    services: ServicesDep,
    device_ids: Annotated[list[int], Query()] = [],  # noqa: B006 - padrão do FastAPI
):
    rows = view.build_rows(
        await services.registry.list(), services.store,
        services.app_settings.get(BACKUP_INTERVAL_HOURS),
    )
    return templates.TemplateResponse(
        request, "partials/device_rows.html", {"rows": rows, "selected": set(device_ids)}
    )


@router.post("/ui/devices", response_class=HTMLResponse)
async def ui_add_device(request: Request, services: ServicesDep, host: Annotated[str, Form()]):
    try:
        device = await add_manual(services, host)
    except OperationError as e:
        return _flash(request, str(e), ok=False)
    return _flash(request, f"{device.name} ({device.ip}) adicionado.", ok=True, headers=REFRESH)


@router.delete("/ui/devices/{device_id}", response_class=HTMLResponse)
async def ui_delete_device(device_id: int, services: ServicesDep):
    await delete_device(services, device_id)
    return Response(status_code=200, headers=REFRESH)


@router.post("/ui/actions", response_class=HTMLResponse)
async def ui_actions(
    request: Request,
    services: ServicesDep,
    kind: Annotated[ActionKind, Form()],
    device_ids: Annotated[list[int], Form()] = [],  # noqa: B006
    bri_value: Annotated[int | None, Form()] = None,
    preset_value: Annotated[int | None, Form()] = None,
):
    if not device_ids:
        return _flash(request, "Selecione pelo menos um dispositivo.", ok=False)
    value = {ActionKind.BRIGHTNESS: bri_value, ActionKind.PRESET: preset_value}.get(kind)
    try:
        action = BulkAction(kind=kind, value=value)
    except ValidationError as e:
        return _flash(request, e.errors()[0]["msg"].removeprefix("Value error, "), ok=False)
    results = await services.actions.execute(device_ids, action)
    return templates.TemplateResponse(
        request, "partials/action_results.html", {"results": results}, headers=REFRESH
    )


@router.post("/ui/scan", response_class=HTMLResponse)
async def ui_scan_start(request: Request, services: ServicesDep, subnets: Annotated[str, Form()]):
    try:
        services.scanner.start(s for s in subnets.split(","))
    except (ScanAlreadyRunning, ValueError) as e:
        return _flash(request, str(e), ok=False)
    return _scan_status(request, services)


@router.get("/ui/scan", response_class=HTMLResponse)
async def ui_scan_status(request: Request, services: ServicesDep):
    return _scan_status(request, services)


def _scan_status(request: Request, services) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/scan_status.html", {"scan": services.scanner.current}
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, services: ServicesDep, username: AdminDep):
    return templates.TemplateResponse(
        request, "settings.html", await _base_ctx(services, username)
    )


@router.post("/settings/account", response_class=HTMLResponse)
async def settings_account(
    request: Request,
    services: ServicesDep,
    username: AdminDep,
    current_password: Annotated[str, Form()],
    new_username: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
):
    error = None
    if new_password != confirm_password:
        error = "a confirmação não confere com a nova senha"
    else:
        try:
            await services.auth.change_credentials(current_password, new_username, new_password)
        except CredentialError as e:
            error = str(e)
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {**ctx, "account_error": error, "account_saved": error is None},
        status_code=400 if error else 200,
    )


@router.post("/settings/backup", response_class=HTMLResponse)
async def settings_backup(
    request: Request,
    services: ServicesDep,
    username: AdminDep,
    backup_interval_hours: Annotated[int, Form()],
    backup_keep: Annotated[int, Form()],
):
    error = None
    try:
        keep_changed = backup_keep != services.app_settings.get(BACKUP_KEEP)
        await services.app_settings.set(BACKUP_INTERVAL_HOURS, backup_interval_hours)
        await services.app_settings.set(BACKUP_KEEP, backup_keep)
        if keep_changed:
            await services.backups.apply_retention_all()
    except SettingsError as e:
        error = str(e)
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {**ctx, "backup_error": error, "backup_saved": error is None},
        status_code=400 if error else 200,
    )


@router.post("/settings/firmware", response_class=HTMLResponse)
async def settings_firmware(
    request: Request,
    services: ServicesDep,
    username: AdminDep,
    ota_repo: Annotated[str, Form()],
    ota_prereleases: Annotated[bool, Form()] = False,
    ota_stop_on_failure: Annotated[bool, Form()] = False,
):
    error = None
    try:
        await services.app_settings.set(OTA_REPO, ota_repo)
        await services.app_settings.set(OTA_INCLUDE_PRERELEASES, ota_prereleases)
        await services.app_settings.set(OTA_STOP_ON_FAILURE, ota_stop_on_failure)
    except SettingsError as e:
        error = str(e)
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {**ctx, "firmware_error": error, "firmware_saved": error is None},
        status_code=400 if error else 200,
    )


def _flash(request: Request, message: str, *, ok: bool, headers=None) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/flash.html", {"message": message, "ok": ok}, headers=headers
    )
