"""API JSON (mesma autenticação do painel). Útil para scripts e automações."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..services.actions import BulkAction
from ..services.discovery import ScanAlreadyRunning
from . import view
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
    rows = view.build_rows(await services.registry.list(), services.store)
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
