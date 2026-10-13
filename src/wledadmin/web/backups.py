"""Páginas e fragmentos de backup, restauração, cópia de presets e jobs."""

from __future__ import annotations

import io
import re
import zipfile
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..models import Backup, Device
from ..services.backup import (
    MAX_IMPORT_BYTES,
    BackupError,
    BackupKind,
    BackupTrigger,
    json_diff,
)
from ..services.container import Services
from ..services.jobs import ItemStatus, Job, JobItem, JobStatus
from ..wled import WLEDError
from .ingress import redirect
from .pages import REFRESH, _base_ctx, _flash, templates
from .security import AdminDep, ServicesDep, require_admin

router = APIRouter(dependencies=[Depends(require_admin)], include_in_schema=False)


# ---- helpers compartilhados com a API -----------------------------------------------


def start_backup_job(services: Services, devices: list[Device]) -> Job:
    async def worker(device: Device, item: JobItem) -> None:
        if services.store.get(device.id).online is False:
            item.status = ItemStatus.SKIPPED
            item.message = "offline, pulado"
            return
        await services.backups.backup_device(device, BackupTrigger.MANUAL, item)

    return services.jobs.start("Backup manual", devices, worker, concurrency=4)


def start_restore_job(
    services: Services, backup: Backup, devices: list[Device], pin: str | None
) -> Job:
    async def worker(device: Device, item: JobItem) -> None:
        await services.backups.restore(device, backup, pin, item)

    kind = BackupKind(backup.kind)
    if len(devices) == 1 and devices[0].mac == backup.device_mac:
        title = f"Restaurar {kind.label.lower()} de {devices[0].name}"
    else:
        title = f"Copiar presets de {backup.device_name}"
    # Exclusivo: escreve nos dispositivos, um de cada vez e um job por vez.
    return services.jobs.start(title, devices, worker, exclusive=True)


async def get_backup_or_404(services: Services, backup_id: int) -> Backup:
    backup = await services.backups.get(backup_id)
    if backup is None:
        raise HTTPException(404, "backup não encontrado")
    return backup


async def get_device_or_404(services: Services, device_id: int) -> Device:
    device = await services.registry.get(device_id)
    if device is None:
        raise HTTPException(404, "dispositivo não encontrado")
    return device


def download_name(backup: Backup) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", backup.device_name).strip("_") or backup.device_mac
    return f"wled_{safe}_{backup.kind}_{backup.created_at:%Y%m%d-%H%M%S}.json"


def json_file_response(content: bytes, filename: str) -> Response:
    return Response(
        content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---- backup manual e jobs -----------------------------------------------------------


@router.post("/ui/backups", response_class=HTMLResponse)
async def ui_backup_now(
    request: Request,
    services: ServicesDep,
    device_ids: Annotated[list[int], Form()] = [],  # noqa: B006
):
    devices = await services.registry.get_many(device_ids)
    if not devices:
        return _flash(request, "Selecione pelo menos um dispositivo.", ok=False)
    job = start_backup_job(services, devices)
    return templates.TemplateResponse(request, "partials/job_status.html", {"job": job})


@router.get("/ui/jobs/{job_id}", response_class=HTMLResponse)
async def ui_job_status(request: Request, job_id: int, services: ServicesDep):
    job = services.jobs.get(job_id)
    if job is None:
        return _flash(request, "Job não encontrado (o wledadmin pode ter reiniciado).", ok=False)
    headers = REFRESH if job.status is JobStatus.DONE else None
    return templates.TemplateResponse(
        request, "partials/job_status.html", {"job": job}, headers=headers
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: int, services: ServicesDep, username: AdminDep):
    job = services.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job não encontrado (o wledadmin pode ter reiniciado)")
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(request, "job.html", {**ctx, "job": job})


# ---- página do dispositivo ----------------------------------------------------------


@router.get("/devices/{device_id}", response_class=HTMLResponse)
async def device_page(
    request: Request,
    device_id: int,
    services: ServicesDep,
    username: AdminDep,
    message: str | None = None,
    error: str | None = None,
):
    device = await get_device_or_404(services, device_id)
    backups = await services.backups.list_for_mac(device.mac)
    ota_history = await services.ota.history(device.mac)
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "device.html",
        {**ctx, "device": device, "status": services.store.get(device.id),
         "backups": backups, "ota_history": ota_history, "message": message, "error": error,
         "BackupKind": BackupKind, "BackupTrigger": BackupTrigger},
    )


@router.post("/devices/{device_id}/import")
async def import_backup(
    request: Request,
    device_id: int,
    services: ServicesDep,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()] = "auto",
    note: Annotated[str, Form()] = "",
):
    device = await get_device_or_404(services, device_id)
    content = await file.read(MAX_IMPORT_BYTES + 1)
    chosen = None if kind == "auto" else BackupKind(kind)
    try:
        outcome = await services.backups.import_backup(
            device, content, chosen, note.strip() or file.filename
        )
    except BackupError as e:
        return _redirect_device(request, device_id, error=f"Importação recusada: {e}")
    if outcome.status == "unchanged":
        msg = f"O arquivo é idêntico à versão #{outcome.backup.id}; nada foi criado."
    else:
        msg = f"Backup de {outcome.kind.label.lower()} importado como versão #{outcome.backup.id}."
    return _redirect_device(request, device_id, message=msg)


def _redirect_device(request: Request, device_id: int, **params: str) -> RedirectResponse:
    from urllib.parse import urlencode

    return redirect(request, f"/devices/{device_id}?{urlencode(params)}")


# ---- versões individuais ------------------------------------------------------------


@router.get("/backups/export.zip")
async def export_zip(services: ServicesDep):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for b in await services.backups.latest_all():
            folder = re.sub(r"[^A-Za-z0-9_-]+", "_", b.device_name).strip("_") or "wled"
            zf.writestr(f"{folder}_{b.device_mac}/{b.kind}.json", b.content)
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="wledadmin-backups.zip"'},
    )


@router.get("/backups/{backup_id}/download")
async def download_backup(backup_id: int, services: ServicesDep):
    backup = await get_backup_or_404(services, backup_id)
    return json_file_response(backup.content, download_name(backup))


@router.get("/backups/{backup_id}/diff", response_class=HTMLResponse)
async def backup_diff(request: Request, backup_id: int, services: ServicesDep,
                      username: AdminDep):
    backup = await get_backup_or_404(services, backup_id)
    previous = await services.backups.previous_of(backup)
    device = await services.registry.get_by_mac(backup.device_mac)
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "backup_diff.html",
        {**ctx, "backup": backup, "previous": previous, "device": device,
         "diff": json_diff(previous.content if previous else None, backup.content)},
    )


@router.post("/ui/backups/{backup_id}/pin")
async def ui_pin_backup(backup_id: int, services: ServicesDep,
                        pinned: Annotated[bool, Form()]):
    await services.backups.set_pinned(backup_id, pinned)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.delete("/ui/backups/{backup_id}", response_class=HTMLResponse)
async def ui_delete_backup(request: Request, backup_id: int, services: ServicesDep):
    try:
        await services.backups.delete(backup_id)
    except BackupError as e:
        return _flash(request, str(e), ok=False)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


# ---- restauração --------------------------------------------------------------------


@router.get("/backups/{backup_id}/restore", response_class=HTMLResponse)
async def restore_confirm(
    request: Request,
    backup_id: int,
    services: ServicesDep,
    username: AdminDep,
    device_id: Annotated[int | None, Query()] = None,
):
    backup = await get_backup_or_404(services, backup_id)
    if device_id is not None:
        device = await get_device_or_404(services, device_id)
    else:
        device = await services.registry.get_by_mac(backup.device_mac)
        if device is None:
            raise HTTPException(404, "o dispositivo deste backup não está mais cadastrado")

    problems = services.backups.restore_problems(device, backup)
    warnings = services.backups.restore_warnings(device, backup)
    diff, current_error = None, None
    if not problems:
        try:
            current = await services.client.download_file(
                device.ip, BackupKind(backup.kind).file_name
            )
            diff = json_diff(current, backup.content)
        except WLEDError as e:
            current_error = f"não consegui ler o arquivo atual do dispositivo: {e}"

    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "restore_confirm.html",
        {**ctx, "backup": backup, "device": device, "problems": problems,
         "warnings": warnings, "diff": diff, "current_error": current_error,
         "needs_import_confirm": backup.kind == BackupKind.CFG
         and backup.trigger == BackupTrigger.IMPORT},
    )


@router.post("/backups/{backup_id}/restore")
async def restore_submit(
    request: Request,
    backup_id: int,
    services: ServicesDep,
    device_id: Annotated[int, Form()],
    confirm: Annotated[bool, Form()] = False,
    confirm_import: Annotated[bool, Form()] = False,
    pin: Annotated[str, Form()] = "",
):
    backup = await get_backup_or_404(services, backup_id)
    device = await get_device_or_404(services, device_id)
    if not confirm:
        raise HTTPException(400, "confirmação obrigatória")
    if (backup.kind == BackupKind.CFG and backup.trigger == BackupTrigger.IMPORT
            and not confirm_import):
        raise HTTPException(400, "confirme que o backup importado é deste dispositivo")
    if problems := services.backups.restore_problems(device, backup):
        raise HTTPException(409, " ".join(problems))
    job = start_restore_job(services, backup, [device], pin or None)
    return redirect(request, f"/jobs/{job.id}")


# ---- cópia de presets ---------------------------------------------------------------


@router.get("/backups/{backup_id}/copy", response_class=HTMLResponse)
async def copy_presets_page(request: Request, backup_id: int, services: ServicesDep,
                            username: AdminDep):
    backup = await get_backup_or_404(services, backup_id)
    if backup.kind != BackupKind.PRESETS:
        raise HTTPException(400, "só presets podem ser copiados para outros dispositivos")
    devices = await services.registry.list()
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "copy_presets.html",
        {**ctx, "backup": backup, "devices": devices,
         "status": {d.id: services.store.get(d.id) for d in devices}},
    )


@router.post("/backups/{backup_id}/copy")
async def copy_presets_submit(
    request: Request,
    backup_id: int,
    services: ServicesDep,
    device_ids: Annotated[list[int], Form()] = [],  # noqa: B006
    confirm: Annotated[bool, Form()] = False,
    pin: Annotated[str, Form()] = "",
):
    backup = await get_backup_or_404(services, backup_id)
    if backup.kind != BackupKind.PRESETS:
        raise HTTPException(400, "só presets podem ser copiados para outros dispositivos")
    if not confirm:
        raise HTTPException(400, "confirmação obrigatória")
    devices = await services.registry.get_many(device_ids)
    if not devices:
        raise HTTPException(400, "selecione pelo menos um dispositivo")
    job = start_restore_job(services, backup, devices, pin or None)
    return redirect(request, f"/jobs/{job.id}")
