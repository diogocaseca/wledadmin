"""Planejamento e execução de atualizações de firmware (OTA).

Planejar não altera nada: decide, para cada dispositivo, qual arquivo instalar e se
há algo que impeça. A execução é sempre um job exclusivo e sequencial (um dispositivo
por vez, um job de escrita por vez), com backup antes e verificação de versão depois.

Regras (combinadas com o usuário):
- Downgrade é bloqueado; reinstalar a mesma versão é permitido quando pedido.
- Arquivo escolhido automaticamente (release oficial): divergência de release ou de
  versão mínima bloqueiam. Se a versão mínima não for atendida, planejamos duas etapas
  passando por uma versão intermediária.
- Escolha manual ou .bin próprio: essas duas situações viram avisos; problemas de
  hardware (chip, família, flash) continuam bloqueando.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Settings
from ..models import Backup, Device, OtaUpdate, utcnow
from ..wled import (
    Arch,
    DeviceInfo,
    WLEDAuthRequired,
    WLEDClient,
    WLEDError,
    WLEDHTTPError,
    WLEDOtaLocked,
    WLEDTimeout,
    normalize_arch,
)
from ..wled.firmware import (
    FirmwareError,
    FirmwareImage,
    asset_matches_arch,
    check_compatibility,
    format_version,
    parse_firmware,
    parse_version,
)
from .app_settings import OTA_INCLUDE_PRERELEASES, OTA_REPO, AppSettingsService
from .backup import BackupKind, BackupService, BackupTrigger
from .jobs import ItemStatus, Job, JobItem, JobManager, StepFailed
from .registry import DeviceRegistry
from .releases import Release, ReleaseAsset, ReleaseError, ReleaseService
from .status_store import StatusStore

Version = tuple[int, int, int]


class PlanStatus(StrEnum):
    READY = "ready"  # pode atualizar
    MANUAL = "manual"  # precisa que o usuário escolha o arquivo
    CURRENT = "current"  # já está na versão alvo (pode reinstalar)
    BLOCKED = "blocked"


@dataclass(frozen=True)
class FirmwareChoice:
    source: str  # github | custom
    file_name: str
    sha256: str
    image: FirmwareImage
    release: Release | None = None
    asset: ReleaseAsset | None = None
    custom_id: str | None = None

    @property
    def version(self) -> Version | None:
        if self.image.version:
            return self.image.version
        return self.release.version if self.release else None


@dataclass
class DevicePlan:
    device: Device
    current_version: str | None
    device_release: str | None
    arch: Arch
    status: PlanStatus = PlanStatus.READY
    steps: list[FirmwareChoice] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    candidates: list[ReleaseAsset] = field(default_factory=list)
    # Escolha manual ou .bin próprio: exige confirmação por dispositivo.
    needs_device_confirm: bool = False
    reinstall: bool = False

    def block(self, reason: str) -> DevicePlan:
        self.status = PlanStatus.BLOCKED
        self.blockers.append(reason)
        return self

    @property
    def path(self) -> str:
        versions = [self.current_version or "?"] + [format_version(s.version) for s in self.steps]
        return " → ".join(versions)


@dataclass(frozen=True)
class FileCheck:
    kind: BackupKind
    backup: Backup | None  # versão mais nova desse tipo no histórico
    # same | different | missing (não existe no dispositivo) | no_backup | unreadable
    status: str
    detail: str | None = None

    @property
    def should_restore(self) -> bool:
        return self.backup is not None and self.status in ("different", "missing")


@dataclass(frozen=True)
class RecoveryReport:
    online: bool
    error: str | None = None
    version: str | None = None
    files: list[FileCheck] = field(default_factory=list)

    @property
    def needs_restore(self) -> bool:
        return any(f.should_restore for f in self.files)


def _same_json(a: bytes, b: bytes) -> bool:
    try:
        return json.loads(a) == json.loads(b)
    except ValueError:
        return a == b


class OtaService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        client: WLEDClient,
        registry: DeviceRegistry,
        store: StatusStore,
        backups: BackupService,
        releases: ReleaseService,
        app_settings: AppSettingsService,
        jobs: JobManager,
        settings: Settings,
        *,
        on_device_changed: Callable[[], None] | None = None,
        reboot_initial_delay: float = 5.0,
        reboot_poll_interval: float = 3.0,
    ) -> None:
        self._sf = session_factory
        self._client = client
        self._registry = registry
        self._store = store
        self._backups = backups
        self._releases = releases
        self._settings = app_settings
        self._jobs = jobs
        self._cfg = settings
        self._notify = on_device_changed
        self._reboot_initial_delay = reboot_initial_delay
        self._reboot_poll_interval = reboot_poll_interval

    @property
    def repo(self) -> str:
        return self._settings.get(OTA_REPO)

    async def list_releases(self, *, force: bool = False) -> list[Release]:
        return await self._releases.list_releases(
            self.repo,
            include_prereleases=self._settings.get(OTA_INCLUDE_PRERELEASES),
            force=force,
        )

    # ---- planejamento ------------------------------------------------------------------

    async def plan_release(
        self,
        devices: list[Device],
        release: Release,
        *,
        manual: dict[int, str] | None = None,
        reinstall: set[int] | None = None,
    ) -> list[DevicePlan]:
        manual = manual or {}
        reinstall = reinstall or set()
        lock_states = await self._ota_lock_states(devices)
        plans = []
        for d in devices:  # sequencial: downloads em cache são compartilhados
            plans.append(await self._plan_one_release(
                d, release, manual.get(d.id), d.id in reinstall, lock_states.get(d.id)
            ))
        return plans

    async def plan_custom(self, devices: list[Device], custom_id: str,
                          file_name: str) -> list[DevicePlan]:
        try:
            data = self._releases.load_custom(custom_id)
            image = parse_firmware(data)
        except (ReleaseError, FirmwareError) as e:
            return [self._base_plan(d).block(str(e)) for d in devices]
        choice = FirmwareChoice("custom", file_name, image.sha256, image, custom_id=custom_id)
        lock_states = await self._ota_lock_states(devices)
        plans = []
        for d in devices:
            plan = self._base_plan(d)
            plans.append(plan)
            if not self._check_reachable(plan, lock_states.get(d.id)):
                continue
            plan.needs_device_confirm = True
            self._apply_compat(plan, choice, strict=False)
            if plan.status is not PlanStatus.BLOCKED:
                plan.steps = [choice]
                if image.version and image.version == parse_version(plan.current_version):
                    plan.reinstall = True
        return plans

    def _base_plan(self, device: Device) -> DevicePlan:
        info = self._store.get(device.id).info
        return DevicePlan(
            device=device,
            current_version=(info.ver if info else None) or device.version,
            device_release=(info.release if info else None) or device.release,
            arch=info.arch if info else normalize_arch(device.arch),
        )

    async def _ota_lock_states(self, devices: list[Device]) -> dict[int, bool | None]:
        """Lê ota.lock do cfg.json de cada dispositivo online (None = não deu para ler)."""
        sem = asyncio.Semaphore(4)

        async def one(d: Device) -> tuple[int, bool | None]:
            if self._store.get(d.id).online is not True:
                return d.id, None
            async with sem:
                try:
                    cfg = await self._client.get_json_file(d.ip, "cfg.json")
                except WLEDError:
                    return d.id, None
            ota = cfg.get("ota") if isinstance(cfg, dict) else None
            return d.id, bool(ota.get("lock")) if isinstance(ota, dict) else None

        return dict(await asyncio.gather(*(one(d) for d in devices)))

    def _check_reachable(self, plan: DevicePlan, ota_locked: bool | None) -> bool:
        st = self._store.get(plan.device.id)
        if st.online is not True or st.info is None:
            plan.block("dispositivo offline (ou ainda não consultado)")
            return False
        if ota_locked:
            plan.block("OTA bloqueado no dispositivo: desative \"OTA lock\" em Config → "
                       "Security & Updates (exige a senha de OTA)")
            return False
        if ota_locked is None:
            plan.warnings.append("não consegui ler o cfg.json para conferir o OTA lock")
        return True

    async def _plan_one_release(
        self,
        device: Device,
        release: Release,
        manual_asset: str | None,
        reinstall: bool,
        ota_locked: bool | None,
    ) -> DevicePlan:
        plan = self._base_plan(device)
        if not self._check_reachable(plan, ota_locked):
            return plan
        info: DeviceInfo = self._store.get(device.id).info
        if info.repo and info.repo.lower() != self.repo.lower():
            return plan.block(f"o dispositivo roda firmware de outro repositório ({info.repo})")

        current = parse_version(plan.current_version)
        target = release.version
        if current and target and current > target:
            return plan.block(f"downgrade ({plan.current_version} → {release.version_str}) "
                              "não é permitido")
        if current and current == target:
            if not reinstall:
                plan.status = PlanStatus.CURRENT
                return plan
            plan.reinstall = True

        if manual_asset:
            return await self._plan_manual(plan, release, manual_asset)

        rel = plan.device_release
        asset = release.find_asset(rel) if rel and rel != "Custom" else None
        if asset is None:
            plan.status = PlanStatus.MANUAL
            plan.candidates = [a for a in release.assets if asset_matches_arch(a.name, plan.arch)]
            if not rel:
                plan.warnings.append("o dispositivo não informa o release (firmware 0.14): "
                                     "escolha o arquivo certo para o hardware dele")
            elif rel == "Custom":
                plan.warnings.append("o dispositivo roda um build customizado: escolha o arquivo")
            else:
                plan.warnings.append(f"não há arquivo \"{rel}\" na versão {release.version_str}: "
                                     "escolha o arquivo manualmente")
            if not plan.candidates:
                plan.block(f"nenhum arquivo para {plan.arch.label} na versão "
                           f"{release.version_str}")
            return plan

        choice = await self._github_choice(plan, release, asset)
        if choice is None:
            return plan
        self._apply_compat(plan, choice, strict=True, check_safe_version=False)
        if plan.status is PlanStatus.BLOCKED:
            return plan

        safe = choice.image.metadata.safe_update_version if choice.image.metadata else None
        if safe and any(safe) and current and current < safe:
            step1 = await self._intermediate(plan, release, current, safe)
            if step1 is None:
                return plan.block(
                    f"é preciso estar na {format_version(safe)} ou mais nova antes da "
                    f"{release.version_str}, e não achei versão intermediária com o arquivo "
                    f"\"{rel}\""
                )
            plan.steps = [step1, choice]
            plan.warnings.append(
                f"atualização em duas etapas: {plan.current_version} → "
                f"{format_version(step1.version)} → {release.version_str} (o WLED recomenda "
                f"estar na {format_version(safe)}+ antes da {release.version_str})"
            )
        else:
            plan.steps = [choice]
        return plan

    async def _plan_manual(self, plan: DevicePlan, release: Release, name: str) -> DevicePlan:
        plan.candidates = [a for a in release.assets if asset_matches_arch(a.name, plan.arch)]
        asset = release.asset(name)
        if asset is None or not asset_matches_arch(name, plan.arch):
            return plan.block(f"o arquivo {name} não serve para {plan.arch.label}")
        choice = await self._github_choice(plan, release, asset)
        if choice is None:
            return plan
        plan.needs_device_confirm = True
        self._apply_compat(plan, choice, strict=False)
        if plan.status is not PlanStatus.BLOCKED:
            plan.status = PlanStatus.READY
            plan.steps = [choice]
        return plan

    async def _github_choice(self, plan: DevicePlan, release: Release,
                             asset: ReleaseAsset) -> FirmwareChoice | None:
        try:
            data = await self._releases.fetch_asset(release, asset)
            image = parse_firmware(data)
        except (ReleaseError, FirmwareError) as e:
            plan.block(f"{asset.name}: {e}")
            return None
        return FirmwareChoice("github", asset.name, image.sha256, image, release, asset)

    async def _intermediate(self, plan: DevicePlan, target: Release, current: Version,
                            safe: Version) -> FirmwareChoice | None:
        """Versão estável mais nova entre `safe` e o alvo que aceite a versão atual."""
        try:
            releases = await self._releases.list_releases(self.repo, include_prereleases=False)
        except ReleaseError:
            return None
        for r in releases:
            if not (r.version and safe <= r.version < target.version):
                continue
            asset = r.find_asset(plan.device_release)
            if asset is None:
                continue
            probe = DevicePlan(plan.device, plan.current_version, plan.device_release, plan.arch)
            choice = await self._github_choice(probe, r, asset)
            if choice is None:
                continue
            self._apply_compat(probe, choice, strict=True)
            if probe.status is not PlanStatus.BLOCKED:
                return choice
        return None

    def _apply_compat(self, plan: DevicePlan, choice: FirmwareChoice, *, strict: bool,
                      check_safe_version: bool = True) -> None:
        info = self._store.get(plan.device.id).info
        compat = check_compatibility(
            choice.image,
            arch=plan.arch,
            device_release=plan.device_release,
            device_version=parse_version(plan.current_version),
            device_flash_mb=info.flash if info else None,
            strict=strict,
            check_safe_version=check_safe_version,
        )
        plan.warnings.extend(compat.warnings)
        for b in compat.blockers:
            plan.block(b)

    # ---- execução ----------------------------------------------------------------------

    def run(self, plans: list[DevicePlan], *, pin: str | None, stop_on_failure: bool,
            title: str, backup: bool = True) -> Job:
        runnable = [p for p in plans if p.status is PlanStatus.READY and p.steps]
        by_id = {p.device.id: p for p in runnable}
        stopped = {"value": False}

        async def worker(device: Device, item: JobItem) -> None:
            if stopped["value"]:
                item.status = ItemStatus.SKIPPED
                item.message = "cancelado: um dispositivo anterior falhou"
                return
            try:
                await self._update_device(by_id[device.id], pin, item, backup)
            except Exception:
                if stop_on_failure:
                    stopped["value"] = True
                raise

        return self._jobs.start(title, [p.device for p in runnable], worker, exclusive=True,
                                kind="ota")

    async def _update_device(self, plan: DevicePlan, pin: str | None, item: JobItem,
                             backup: bool) -> None:
        device = plan.device
        if backup:
            item.step("Backup de cfg.json e presets.json antes de atualizar")
            try:
                await self._backups.backup_device(device, BackupTrigger.PRE_OTA)
            except (StepFailed, WLEDError) as e:
                raise StepFailed(
                    f"o backup falhou, então a atualização não foi iniciada: {e}"
                ) from e
        else:
            item.step("Backup pulado (desativado na confirmação)")

        async with self._registry.lock(device.id):
            total = len(plan.steps)
            for n, choice in enumerate(plan.steps, 1):
                prefix = f"[{n}/{total}] " if total > 1 else ""
                await self._install(device, choice, pin, item, prefix)
        final = format_version(plan.steps[-1].version)
        item.message = f"atualizado para {final}" if final != "?" else "firmware instalado"
        if self._notify:
            self._notify()

    async def _install(self, device: Device, choice: FirmwareChoice, pin: str | None,
                       item: JobItem, prefix: str) -> None:
        started = utcnow()
        log_start = len(item.steps)
        from_version: str | None = None
        try:
            item.step(f"{prefix}Preparando {choice.file_name}")
            data = await self._load_bytes(choice)
            info = await self._client.get_info(device.ip)
            if info.mac_normalized != device.mac:
                raise StepFailed(f"{device.ip} agora responde com outro MAC; nada foi enviado")
            from_version = info.ver
            if pin:
                item.step(f"{prefix}Desbloqueando com o PIN")
                await self._client.unlock_settings(device.ip, pin)
            item.step(f"{prefix}Enviando firmware ({len(data) / 1024:.0f} KiB); pode levar "
                      "alguns minutos")
            reply = await self._upload(device, choice, data, bool(pin))
            item.step(f"{prefix}Resposta do WLED: {reply[:120] or '(vazia)'}")
            item.step(f"{prefix}Aguardando o dispositivo reiniciar")
            try:
                new = await self._client.wait_for_reboot(
                    device.ip, device.mac,
                    timeout=self._cfg.ota_reboot_timeout,
                    initial_delay=self._reboot_initial_delay,
                    interval=self._reboot_poll_interval,
                )
            except WLEDTimeout as e:
                raise StepFailed(
                    f"{e}. Se ele não voltar, procure a rede Wi-Fi \"WLED-AP\" ou regrave por "
                    "USB; o backup de antes da atualização está no histórico."
                ) from e
            expected = choice.version
            if expected and parse_version(new.ver) != expected:
                raise StepFailed(f"o dispositivo reiniciou, mas continua na {new.ver} "
                                 f"(esperado {format_version(expected)})")
            item.step(f"{prefix}Agora na versão {new.ver}")
            await self._record(device, choice, from_version, "ok", None,
                               item.steps[log_start:], started)
        except StepFailed as e:
            await self._record(device, choice, from_version, "failed", str(e),
                               item.steps[log_start:], started)
            raise
        except WLEDError as e:
            await self._record(device, choice, from_version, "failed", str(e),
                               item.steps[log_start:], started)
            raise StepFailed(str(e)) from e

    async def _upload(self, device: Device, choice: FirmwareChoice, data: bytes,
                      used_pin: bool) -> str:
        try:
            return await self._client.upload_firmware(
                device.ip, choice.file_name, data, timeout=self._cfg.ota_upload_timeout
            )
        except WLEDOtaLocked as e:
            raise StepFailed("OTA bloqueado no dispositivo (Config → Security & Updates)") from e
        except WLEDAuthRequired as e:
            if "sub-rede" in str(e):
                raise StepFailed(str(e)) from e
            raise StepFailed("PIN incorreto." if used_pin else
                             "o dispositivo tem PIN de configurações; informe o PIN.") from e
        except WLEDHTTPError as e:
            raise StepFailed(f"o dispositivo recusou o firmware: {e}") from e
        except WLEDError as e:
            raise StepFailed(
                f"a conexão caiu durante o envio ({e}). O ESP só troca de firmware quando "
                "recebe o arquivo inteiro, então ele deve continuar na versão anterior."
            ) from e

    async def _load_bytes(self, choice: FirmwareChoice) -> bytes:
        try:
            if choice.source == "custom":
                data = self._releases.load_custom(choice.custom_id)
            else:
                data = await self._releases.fetch_asset(choice.release, choice.asset)
        except ReleaseError as e:
            raise StepFailed(str(e)) from e
        if parse_firmware(data).sha256 != choice.sha256:
            raise StepFailed("o arquivo mudou desde o planejamento; planeje de novo")
        return data

    async def _record(self, device: Device, choice: FirmwareChoice, from_version: str | None,
                      status: str, message: str | None, steps: list[str], started) -> None:
        meta = choice.image.metadata
        async with self._sf() as s:
            s.add(OtaUpdate(
                device_id=device.id,
                device_mac=device.mac,
                device_name=device.name,
                from_version=from_version,
                to_version=format_version(choice.version) if choice.version else None,
                release_name=meta.release_name if meta else None,
                file_name=choice.file_name,
                source=choice.source,
                sha256=choice.sha256,
                status=status,
                message=message,
                steps=json.dumps(steps, ensure_ascii=False),
                started_at=started,
                finished_at=utcnow(),
            ))
            await s.commit()

    # ---- recuperação após falha ---------------------------------------------------------

    async def diagnose(self, device: Device) -> RecoveryReport:
        """Estado atual do dispositivo comparado com os backups mais novos.

        Feito na hora (não usa o cache do poller): depois de um OTA com falha o
        dispositivo pode ter voltado com a configuração de fábrica, ou nem ter voltado.
        """
        try:
            state, info = await self._client.get_state_info(device.ip)
        except WLEDError as e:
            return RecoveryReport(online=False, error=str(e))
        if info.mac_normalized and info.mac_normalized != device.mac:
            return RecoveryReport(online=False,
                                  error=f"{device.ip} agora responde com outro MAC")
        self._store.record_success(device.id, info, state)
        if self._notify:
            self._notify()

        history = await self._backups.list_for_mac(device.mac)
        files = []
        for kind in BackupKind:
            latest = next((b for b in history if b.kind == kind.value), None)
            if latest is None:
                files.append(FileCheck(kind, None, "no_backup"))
                continue
            try:
                current = await self._client.download_file(device.ip, kind.file_name)
            except WLEDError as e:
                files.append(FileCheck(kind, latest, "unreadable", str(e)))
                continue
            if current is None:
                files.append(FileCheck(kind, latest, "missing"))
                continue
            full = await self._backups.get(latest.id)
            same = full is not None and _same_json(current, full.content)
            files.append(FileCheck(kind, latest, "same" if same else "different"))
        return RecoveryReport(online=True, version=info.ver, files=files)

    async def last_failure(self, mac: str) -> OtaUpdate | None:
        async with self._sf() as s:
            return await s.scalar(
                select(OtaUpdate)
                .where(OtaUpdate.device_mac == mac, OtaUpdate.status == "failed")
                .order_by(OtaUpdate.started_at.desc(), OtaUpdate.id.desc())
                .limit(1)
            )

    async def history(self, mac: str) -> list[OtaUpdate]:
        async with self._sf() as s:
            rows = await s.scalars(
                select(OtaUpdate).where(OtaUpdate.device_mac == mac)
                .order_by(OtaUpdate.started_at.desc(), OtaUpdate.id.desc())
            )
            return list(rows.all())
