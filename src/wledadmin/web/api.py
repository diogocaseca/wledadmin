"""API JSON (mesma autenticação do painel). Útil para scripts e automações."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..services.actions import BulkAction
from ..services.app_settings import BACKUP_INTERVAL_HOURS
from ..services.backup import BackupKind, BackupTrigger
from ..services.discovery import ScanAlreadyRunning
from ..services.jobs import Job
from . import view
from .backups import (
    download_name,
    get_backup_or_404,
    get_device_or_404,
    json_file_response,
    start_backup_job,
    start_restore_job,
)
from .operations import OperationError, add_manual, delete_device
from .security import ServicesDep, require_admin

router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)], tags=["api"])


class AddDeviceIn(BaseModel):
    host: str


class ActionIn(BaseModel):
    device_ids: list[int] = Field(min_length=1)
    action: BulkAction


class ScanIn(BaseModel):
    subnets: list[str] | None = None  # padrão: WLEDADMIN_SCAN_SUBNETS


@router.get("/devices")
async def list_devices(services: ServicesDep):
    rows = view.build_rows(
        await services.registry.list(), services.store,
        services.app_settings.get(BACKUP_INTERVAL_HOURS),
    )
    return [r.to_json() for r in rows]


@router.post("/devices", status_code=201)
async def create_device(body: AddDeviceIn, services: ServicesDep):
    try:
        device = await add_manual(services, body.host)
    except OperationError as e:
        raise HTTPException(422, str(e)) from e
    return {"id": device.id, "mac": device.mac, "ip": device.ip, "name": device.name}


@router.delete("/devices/{device_id}", status_code=204)
async def remove_device(device_id: int, services: ServicesDep):
    if not await delete_device(services, device_id):
        raise HTTPException(404, "dispositivo não encontrado")


@router.post("/actions")
async def run_action(body: ActionIn, services: ServicesDep):
    results = await services.actions.execute(body.device_ids, body.action)
    return [asdict(r) for r in results]


@router.post("/scan", status_code=202)
async def start_scan(services: ServicesDep, body: ScanIn | None = None):
    subnets = (body.subnets if body else None) or services.settings.scan_subnet_list
    try:
        job = services.scanner.start(subnets)
    except ScanAlreadyRunning as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _scan_json(job)


@router.get("/scan")
async def scan_status(services: ServicesDep):
    job = services.scanner.current
    if job is None:
        raise HTTPException(404, "nenhuma varredura executada")
    return _scan_json(job)


@router.post("/poll", status_code=202)
async def trigger_poll(services: ServicesDep):
    services.poller.wake()
    return {"status": "agendado"}


class BackupRunIn(BaseModel):
    device_ids: list[int] = Field(min_length=1)


class RestoreIn(BaseModel):
    device_id: int | None = None  # padrão: o dispositivo de origem do backup
    pin: str | None = None
    confirm: bool = False
    confirm_import: bool = False


class CopyPresetsIn(BaseModel):
    device_ids: list[int] = Field(min_length=1)
    pin: str | None = None
    confirm: bool = False


@router.get("/devices/{device_id}/backups")
async def list_backups(device_id: int, services: ServicesDep):
    device = await get_device_or_404(services, device_id)
    return [_backup_json(b) for b in await services.backups.list_for_mac(device.mac)]


@router.post("/backups", status_code=202)
async def run_backup(body: BackupRunIn, services: ServicesDep):
    devices = await services.registry.get_many(body.device_ids)
    if not devices:
        raise HTTPException(404, "nenhum dispositivo encontrado")
    return _job_json(start_backup_job(services, devices))


@router.get("/backups/{backup_id}/content")
async def backup_content(backup_id: int, services: ServicesDep):
    backup = await get_backup_or_404(services, backup_id)
    return json_file_response(backup.content, download_name(backup))


@router.post("/backups/{backup_id}/restore", status_code=202)
async def restore_backup(backup_id: int, body: RestoreIn, services: ServicesDep):
    backup = await get_backup_or_404(services, backup_id)
    if body.device_id is not None:
        device = await get_device_or_404(services, body.device_id)
    else:
        device = await services.registry.get_by_mac(backup.device_mac)
        if device is None:
            raise HTTPException(404, "o dispositivo deste backup não está cadastrado")
    if not body.confirm:
        raise HTTPException(400, 'envie "confirm": true para restaurar')
    if (backup.kind == BackupKind.CFG and backup.trigger == BackupTrigger.IMPORT
            and not body.confirm_import):
        raise HTTPException(400, 'backup importado: envie também "confirm_import": true')
    if problems := services.backups.restore_problems(device, backup):
        raise HTTPException(409, " ".join(problems))
    return _job_json(start_restore_job(services, backup, [device], body.pin))


@router.post("/backups/{backup_id}/copy", status_code=202)
async def copy_presets(backup_id: int, body: CopyPresetsIn, services: ServicesDep):
    backup = await get_backup_or_404(services, backup_id)
    if backup.kind != BackupKind.PRESETS:
        raise HTTPException(400, "só presets podem ser copiados para outros dispositivos")
    if not body.confirm:
        raise HTTPException(400, 'envie "confirm": true para copiar')
    devices = await services.registry.get_many(body.device_ids)
    if not devices:
        raise HTTPException(404, "nenhum dispositivo encontrado")
    return _job_json(start_restore_job(services, backup, devices, body.pin))


@router.get("/jobs/{job_id}")
async def job_status(job_id: int, services: ServicesDep):
    job = services.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job não encontrado")
    return _job_json(job)


def _backup_json(b) -> dict:
    return {
        "id": b.id, "kind": b.kind, "device_mac": b.device_mac, "device_name": b.device_name,
        "sha256": b.sha256, "size": b.size, "firmware_version": b.firmware_version,
        "release": b.release, "trigger": b.trigger, "pinned": b.pinned, "note": b.note,
        "created_at": b.created_at.isoformat(),
    }


def _job_json(job: Job) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "status": job.status.value,
        "items": [
            {"device_id": it.device_id, "name": it.name, "status": it.status.value,
             "message": it.message, "steps": it.steps}
            for it in job.items
        ],
    }


def _scan_json(job) -> dict:
    return {
        "status": job.status.value,
        "total": job.total,
        "done": job.done,
        "found": job.found,
        "error": job.error,
        "started_at": job.started_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
