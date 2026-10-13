"""Telas e API de atualização de firmware (OTA).

O plano é sempre recalculado no servidor na hora de executar: nada do que vem do
formulário além das escolhas do usuário (versão, arquivo manual, reinstalar) é confiado.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.datastructures import ImmutableMultiDict

from ..services.app_settings import OTA_STOP_ON_FAILURE
from ..services.container import Services
from ..services.ota import DevicePlan, PlanStatus
from ..services.releases import ReleaseError
from ..wled.firmware import MAX_IMAGE_BYTES, FirmwareError, format_version, parse_firmware
from .pages import _base_ctx, templates
from .security import AdminDep, ServicesDep, require_admin

router = APIRouter(dependencies=[Depends(require_admin)], include_in_schema=False)
api_router = APIRouter(prefix="/api/firmware", dependencies=[Depends(require_admin)],
                       tags=["firmware"])

CONFIRM_WORD = "atualizar"


class PlanRequest(BaseModel):
    tag: str | None = None
    custom_id: str | None = None
    custom_name: str = "firmware.bin"
    device_ids: list[int] = Field(default_factory=list)
    manual: dict[int, str] = Field(default_factory=dict)
    reinstall: list[int] = Field(default_factory=list)


class RunRequest(PlanRequest):
    pin: str | None = None
    confirm: bool = False
    confirm_text: str = ""
    confirm_devices: list[int] = Field(default_factory=list)
    stop_on_failure: bool | None = None
    backup: bool = True  # backup de cfg.json/presets.json antes de cada dispositivo


def _plan_request_from(params: ImmutableMultiDict) -> PlanRequest:
    def ints(key: str) -> list[int]:
        out = []
        for v in params.getlist(key):
            try:
                out.append(int(v))
            except ValueError:
                pass
        return out

    manual = {}
    for key, value in params.multi_items():
        if key.startswith("manual_") and value:
            try:
                manual[int(key.removeprefix("manual_"))] = value
            except ValueError:
                pass
    return PlanRequest(
        tag=params.get("tag") or None,
        custom_id=params.get("custom") or None,
        custom_name=params.get("name") or "firmware.bin",
        device_ids=ints("device_ids"),
        manual=manual,
        reinstall=ints("reinstall"),
    )


async def build_plan(services: Services, req: PlanRequest):
    """Retorna (título, release ou None, planos)."""
    devices = await services.registry.get_many(req.device_ids)
    devices.sort(key=lambda d: d.name.lower())
    if not devices:
        raise HTTPException(400, "selecione pelo menos um dispositivo")
    if req.custom_id:
        plans = await services.ota.plan_custom(devices, req.custom_id, req.custom_name)
        return f"Instalar {req.custom_name}", None, plans
    if not req.tag:
        raise HTTPException(400, "escolha a versão")
    try:
        release = await services.releases.get_release(services.ota.repo, req.tag)
    except ReleaseError as e:
        raise HTTPException(502, str(e)) from e
    plans = await services.ota.plan_release(
        devices, release, manual=req.manual, reinstall=set(req.reinstall)
    )
    return f"Atualizar firmware para {release.version_str}", release, plans


def confirmation_errors(req: RunRequest, plans: list[DevicePlan]) -> list[str]:
    runnable = [p for p in plans if p.status is PlanStatus.READY and p.steps]
    errors = []
    if not runnable:
        errors.append("nenhum dispositivo pronto para atualizar")
    if not req.confirm:
        errors.append("marque a confirmação")
    if req.confirm_text.strip().lower() != CONFIRM_WORD:
        errors.append(f"digite \"{CONFIRM_WORD}\" para confirmar")
    for p in runnable:
        if p.needs_device_confirm and p.device.id not in req.confirm_devices:
            errors.append(f"confirme o arquivo escolhido para {p.device.name}")
    return errors


def plan_json(p: DevicePlan) -> dict:
    return {
        "device_id": p.device.id,
        "name": p.device.name,
        "status": p.status.value,
        "current_version": p.current_version,
        "device_release": p.device_release,
        "arch": p.arch.value,
        "path": p.path,
        "steps": [{"file": s.file_name, "version": format_version(s.version),
                   "source": s.source, "sha256": s.sha256} for s in p.steps],
        "blockers": p.blockers,
        "warnings": p.warnings,
        "candidates": [a.name for a in p.candidates],
        "needs_device_confirm": p.needs_device_confirm,
        "reinstall": p.reinstall,
    }


# ---- páginas ------------------------------------------------------------------------


@router.get("/firmware", response_class=HTMLResponse)
async def firmware_page(request: Request, services: ServicesDep, username: AdminDep,
                        refresh: bool = False, error: str | None = None):
    releases, releases_error = [], None
    try:
        releases = await services.ota.list_releases(force=refresh)
    except ReleaseError as e:
        releases_error = str(e)
    devices = await services.registry.list()
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "firmware.html",
        {**ctx, "releases": releases, "releases_error": releases_error, "devices": devices,
         "status": {d.id: services.store.get(d.id) for d in devices},
         "repo": services.ota.repo, "error": error},
    )


@router.post("/firmware/custom")
async def upload_custom(
    services: ServicesDep,
    file: Annotated[UploadFile, File()],
    device_ids: Annotated[list[int], Form()] = [],  # noqa: B006
):
    data = await file.read(MAX_IMAGE_BYTES + 1)
    try:
        parse_firmware(data)
    except FirmwareError as e:
        return RedirectResponse("/firmware?" + urlencode({"error": f"Arquivo recusado: {e}"}),
                                status_code=303)
    if not device_ids:
        return RedirectResponse("/firmware?" + urlencode(
            {"error": "Selecione os dispositivos antes de enviar o arquivo."}), status_code=303)
    custom_id = services.releases.store_custom(data)
    params = [("custom", custom_id), ("name", file.filename or "firmware.bin")]
    params += [("device_ids", str(i)) for i in device_ids]
    return RedirectResponse("/firmware/plan?" + urlencode(params), status_code=303)


async def _render_plan(request: Request, services: Services, username: str,
                       req: PlanRequest, *, errors: list[str] | None = None,
                       form: dict | None = None, status_code: int = 200):
    title, release, plans = await build_plan(services, req)
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "firmware_plan.html",
        {**ctx, "title": title, "release": release, "plans": plans, "req": req,
         "errors": errors or [], "form": form or {}, "PlanStatus": PlanStatus,
         "confirm_word": CONFIRM_WORD,
         "stop_on_failure": services.app_settings.get(OTA_STOP_ON_FAILURE),
         "runnable": sum(p.status is PlanStatus.READY and bool(p.steps) for p in plans)},
        status_code=status_code,
    )


@router.get("/firmware/plan", response_class=HTMLResponse)
async def plan_page(request: Request, services: ServicesDep, username: AdminDep):
    return await _render_plan(request, services, username,
                              _plan_request_from(request.query_params))


@router.post("/firmware/plan", response_class=HTMLResponse)
async def plan_page_post(request: Request, services: ServicesDep, username: AdminDep):
    """Recalcula o plano (escolha manual, reinstalar) sem colocar nada na URL: o
    formulário pode conter o PIN."""
    form = await request.form()
    return await _render_plan(
        request, services, username, _plan_request_from(form),
        form={"confirm_devices": {int(v) for v in form.getlist("confirm_device")
                                  if v.isdigit()},
              "stop_on_failure": form.get("stop_on_failure") == "on",
              "backup": form.get("backup") == "on"},
    )


@router.post("/firmware/run")
async def run_page(request: Request, services: ServicesDep, username: AdminDep):
    form = await request.form()
    base = _plan_request_from(form)
    req = RunRequest(
        **base.model_dump(),
        pin=(form.get("pin") or None),
        confirm=form.get("confirm") == "on",
        confirm_text=form.get("confirm_text") or "",
        confirm_devices=[int(v) for v in form.getlist("confirm_device") if v.isdigit()],
        stop_on_failure=form.get("stop_on_failure") == "on",
        backup=form.get("backup") == "on",
    )
    title, _, plans = await build_plan(services, req)
    if errors := confirmation_errors(req, plans):
        return await _render_plan(
            request, services, username, req, errors=errors, status_code=400,
            form={"confirm_devices": set(req.confirm_devices),
                  "stop_on_failure": req.stop_on_failure, "backup": req.backup},
        )
    job = services.ota.run(plans, pin=req.pin, stop_on_failure=bool(req.stop_on_failure),
                           title=title, backup=req.backup)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


# ---- recuperação após falha --------------------------------------------------------


@router.get("/firmware/recover/{job_id}/{device_id}", response_class=HTMLResponse)
async def recover_page(request: Request, job_id: int, device_id: int,
                       services: ServicesDep, username: AdminDep):
    device = await services.registry.get(device_id)
    if device is None:
        raise HTTPException(404, "dispositivo não encontrado")
    error, steps = None, []
    job = services.jobs.get(job_id)
    item = next((it for it in job.items if it.device_id == device_id), None) if job else None
    if item is not None:
        error, steps = item.message, item.steps
    elif last := await services.ota.last_failure(device.mac):
        error = last.message  # job saiu da memória (ex.: wledadmin reiniciou)
    report = await services.ota.diagnose(device)
    ctx = await _base_ctx(services, username)
    return templates.TemplateResponse(
        request,
        "firmware_recover.html",
        {**ctx, "device": device, "job_id": job_id, "error": error, "steps": steps,
         "report": report},
    )


# ---- API JSON -----------------------------------------------------------------------


@api_router.get("/releases")
async def api_releases(services: ServicesDep, refresh: bool = False):
    try:
        releases = await services.ota.list_releases(force=refresh)
    except ReleaseError as e:
        raise HTTPException(502, str(e)) from e
    return [{"tag": r.tag, "version": r.version_str, "prerelease": r.prerelease,
             "published_at": r.published_at, "url": r.html_url,
             "assets": [a.name for a in r.assets]} for r in releases]


@api_router.post("/plan")
async def api_plan(body: PlanRequest, services: ServicesDep):
    title, _, plans = await build_plan(services, body)
    return {"title": title, "plans": [plan_json(p) for p in plans]}


@api_router.post("/run", status_code=202)
async def api_run(body: RunRequest, services: ServicesDep):
    title, _, plans = await build_plan(services, body)
    if errors := confirmation_errors(body, plans):
        raise HTTPException(400, {"errors": errors, "plans": [plan_json(p) for p in plans]})
    stop = body.stop_on_failure
    if stop is None:
        stop = services.app_settings.get(OTA_STOP_ON_FAILURE)
    job = services.ota.run(plans, pin=body.pin, stop_on_failure=stop, title=title,
                           backup=body.backup)
    return {"job_id": job.id, "title": job.title}
