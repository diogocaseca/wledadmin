"""Backup, histórico e restauração de cfg.json / presets.json.

Regras de segurança do restore (detalhes no README):
- cfg.json só volta para o mesmo dispositivo (mesmo MAC): ele contém pinos GPIO,
  IP estático e hostname, e num hardware diferente pode impedir o boot.
- Antes de enviar, o arquivo atual vira um backup "pre_restore" (o restore é reversível).
- O WLED grava o upload direto por cima do arquivo, sem arquivo temporário. Por isso
  validamos o JSON e o espaço livre antes, e verificamos o resultado depois.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import defer

from ..models import Backup, Device, utcnow
from ..wled import WLEDAuthRequired, WLEDClient, WLEDError, WLEDTimeout
from .app_settings import BACKUP_KEEP, AppSettingsService
from .jobs import ItemStatus, JobItem, StepFailed
from .registry import DeviceRegistry
from .status_store import StatusStore

log = logging.getLogger(__name__)

# Margem de segurança ao checar espaço livre no FS do dispositivo.
FS_MARGIN_BYTES = 4096
REBOOT_TIMEOUT = 90.0
MAX_IMPORT_BYTES = 512 * 1024


class BackupKind(StrEnum):
    CFG = "cfg"
    PRESETS = "presets"

    @property
    def file_name(self) -> str:
        return f"{self.value}.json"

    @property
    def label(self) -> str:
        return "Configuração" if self is BackupKind.CFG else "Presets"


class BackupTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    PRE_RESTORE = "pre_restore"
    IMPORT = "import"
    PRE_OTA = "pre_ota"

    @property
    def label(self) -> str:
        return {
            "manual": "manual",
            "scheduled": "agendado",
            "pre_restore": "antes de restaurar",
            "import": "importado",
            "pre_ota": "antes de atualizar firmware",
        }[self.value]


class BackupError(StepFailed):
    pass


# ---- conteúdo -----------------------------------------------------------------------

_CFG_KEYS = {"rev", "vid", "id", "nw", "ap", "wifi", "hw", "light", "def", "if", "ota"}


def parse_json_object(content: bytes) -> dict[str, Any]:
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BackupError(f"o arquivo não é um JSON válido ({e})") from e
    if not isinstance(data, dict):
        raise BackupError("o arquivo precisa conter um objeto JSON")
    return data


def detect_kind(data: dict[str, Any]) -> BackupKind | None:
    if data and all(k.isdigit() for k in data):
        return BackupKind.PRESETS
    if _CFG_KEYS & data.keys():
        return BackupKind.CFG
    return None


def validate_content(content: bytes, kind: BackupKind) -> dict[str, Any]:
    data = parse_json_object(content)
    detected = detect_kind(data)
    if detected is not None and detected is not kind:
        raise BackupError(f"o conteúdo parece ser de {detected.label.lower()}, não de "
                          f"{kind.label.lower()}")
    if detected is None and kind is BackupKind.CFG:
        raise BackupError("o conteúdo não parece um cfg.json do WLED")
    return data


def minify(data: dict[str, Any]) -> bytes:
    # Mantém a ordem das chaves; só remove espaços (upload mais curto, como a UI do WLED).
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class DiffLine:
    kind: str  # "add" | "del" | "ctx" | "hunk"
    text: str


def _pretty_lines(content: bytes | None) -> list[str]:
    if content is None:
        return []
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content.decode("utf-8", errors="replace").splitlines()
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).splitlines()


def json_diff(old: bytes | None, new: bytes | None, context: int = 3) -> list[DiffLine]:
    """Diff de JSON normalizado (chaves ordenadas): mostra o que mudou de fato."""
    lines = difflib.unified_diff(
        _pretty_lines(old), _pretty_lines(new), lineterm="", n=context
    )
    out: list[DiffLine] = []
    for line in lines:
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            out.append(DiffLine("hunk", line))
        elif line.startswith("+"):
            out.append(DiffLine("add", line[1:]))
        elif line.startswith("-"):
            out.append(DiffLine("del", line[1:]))
        else:
            out.append(DiffLine("ctx", line[1:]))
    return out


@dataclass(frozen=True)
class BackupOutcome:
    kind: BackupKind
    status: str  # created | unchanged | missing
    backup: Backup | None = None


# ---- serviço ------------------------------------------------------------------------


class BackupService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        client: WLEDClient,
        registry: DeviceRegistry,
        store: StatusStore,
        app_settings: AppSettingsService,
        *,
        on_device_changed: Callable[[], None] | None = None,
        reboot_timeout: float = REBOOT_TIMEOUT,
        reboot_initial_delay: float = 3.0,
        reboot_poll_interval: float = 2.0,
    ) -> None:
        self._sf = session_factory
        self._client = client
        self._registry = registry
        self._store = store
        self._settings = app_settings
        self._notify = on_device_changed
        self._reboot_timeout = reboot_timeout
        self._reboot_initial_delay = reboot_initial_delay
        self._reboot_poll_interval = reboot_poll_interval

    # ---- backup ---------------------------------------------------------------------

    async def backup_device(
        self, device: Device, trigger: BackupTrigger, item: JobItem | None = None
    ) -> list[BackupOutcome]:
        async with self._registry.lock(device.id):
            outcomes = []
            for kind in BackupKind:
                try:
                    content = await self._client.download_file(device.ip, kind.file_name)
                except WLEDError as e:
                    raise BackupError(f"não consegui baixar {kind.file_name}: {e}") from e
                if content is None:
                    outcomes.append(BackupOutcome(kind, "missing"))
                    continue
                validate_content(content, kind)
                outcomes.append(await self._store_version(device, kind, content, trigger))
            await self._touch_last_backup(device.id)

        if item is not None:
            for o in outcomes:
                text = {"created": "nova versão salva", "unchanged": "sem alterações",
                        "missing": "arquivo inexistente no dispositivo"}[o.status]
                item.step(f"{o.kind.label}: {text}")
            created = sum(o.status == "created" for o in outcomes)
            item.message = {0: "sem alterações", 1: "1 nova versão"}.get(
                created, f"{created} novas versões"
            )
        return outcomes

    async def _store_version(
        self,
        device: Device,
        kind: BackupKind,
        content: bytes,
        trigger: BackupTrigger,
        note: str | None = None,
    ) -> BackupOutcome:
        digest = sha256(content)
        info = self._store.get(device.id).info
        async with self._sf() as s:
            latest = await s.scalar(
                select(Backup)
                .options(defer(Backup.content))
                .where(Backup.device_mac == device.mac, Backup.kind == kind.value)
                .order_by(Backup.created_at.desc(), Backup.id.desc())
                .limit(1)
            )
            if latest is not None and latest.sha256 == digest:
                return BackupOutcome(kind, "unchanged", latest)
            backup = Backup(
                device_id=device.id,
                device_mac=device.mac,
                device_name=device.name,
                kind=kind.value,
                content=content,
                sha256=digest,
                size=len(content),
                firmware_version=(info.ver if info else None) or device.version,
                vid=(info.vid if info else None) or device.vid,
                release=(info.release if info else None) or device.release,
                arch=device.arch,
                trigger=trigger.value,
                pinned=False,
                note=note,
                created_at=utcnow(),
            )
            s.add(backup)
            await s.flush()
            await self._apply_retention(s, device.mac, kind.value)
            await s.commit()
            return BackupOutcome(kind, "created", backup)

    async def _apply_retention(self, s, mac: str, kind: str) -> None:
        keep = self._settings.get(BACKUP_KEEP)
        old_ids = (
            select(Backup.id)
            .where(Backup.device_mac == mac, Backup.kind == kind, Backup.pinned.is_(False))
            .order_by(Backup.created_at.desc(), Backup.id.desc())
            .offset(keep)
        )
        await s.execute(delete(Backup).where(Backup.id.in_(old_ids)))

    async def apply_retention_all(self) -> None:
        async with self._sf() as s:
            pairs = (await s.execute(select(Backup.device_mac, Backup.kind).distinct())).all()
            for mac, kind in pairs:
                await self._apply_retention(s, mac, kind)
            await s.commit()

    async def _touch_last_backup(self, device_id: int) -> None:
        async with self._sf() as s:
            await s.execute(
                update(Device).where(Device.id == device_id).values(last_backup_at=utcnow())
            )
            await s.commit()

    async def import_backup(
        self, device: Device, content: bytes, kind: BackupKind | None, note: str | None
    ) -> BackupOutcome:
        if len(content) > MAX_IMPORT_BYTES:
            raise BackupError(f"arquivo grande demais (máximo {MAX_IMPORT_BYTES // 1024} KiB)")
        data = parse_json_object(content)
        detected = detect_kind(data)
        kind = kind or detected
        if kind is None:
            raise BackupError("não reconheci o tipo do arquivo; escolha cfg ou presets")
        validate_content(content, kind)
        return await self._store_version(device, kind, content, BackupTrigger.IMPORT, note)

    # ---- consulta -------------------------------------------------------------------

    async def list_for_mac(self, mac: str) -> list[Backup]:
        async with self._sf() as s:
            rows = await s.scalars(
                select(Backup)
                .options(defer(Backup.content))
                .where(Backup.device_mac == mac)
                .order_by(Backup.created_at.desc(), Backup.id.desc())
            )
            return list(rows.all())

    async def get(self, backup_id: int) -> Backup | None:
        async with self._sf() as s:
            return await s.get(Backup, backup_id)

    async def previous_of(self, backup: Backup) -> Backup | None:
        async with self._sf() as s:
            return await s.scalar(
                select(Backup)
                .where(
                    Backup.device_mac == backup.device_mac,
                    Backup.kind == backup.kind,
                    (Backup.created_at < backup.created_at)
                    | ((Backup.created_at == backup.created_at) & (Backup.id < backup.id)),
                )
                .order_by(Backup.created_at.desc(), Backup.id.desc())
                .limit(1)
            )

    async def latest_all(self) -> list[Backup]:
        """Versão mais recente de cada (dispositivo, tipo), para exportação."""
        async with self._sf() as s:
            rows = (await s.scalars(
                select(Backup).order_by(Backup.created_at.desc(), Backup.id.desc())
            )).all()
        seen: dict[tuple[str, str], Backup] = {}
        for b in rows:
            seen.setdefault((b.device_mac, b.kind), b)
        return list(seen.values())

    async def set_pinned(self, backup_id: int, pinned: bool) -> None:
        async with self._sf() as s:
            await s.execute(update(Backup).where(Backup.id == backup_id).values(pinned=pinned))
            await s.commit()

    async def delete(self, backup_id: int) -> None:
        async with self._sf() as s:
            backup = await s.get(Backup, backup_id)
            if backup is None:
                return
            if backup.pinned:
                raise BackupError("desafixe a versão antes de excluir")
            await s.delete(backup)
            await s.commit()

    # ---- restauração ----------------------------------------------------------------

    def restore_problems(self, device: Device, backup: Backup) -> list[str]:
        """Bloqueios absolutos (não há como confirmar e seguir)."""
        problems = []
        if backup.kind == BackupKind.CFG and backup.device_mac != device.mac:
            problems.append(
                "Uma configuração (cfg.json) só pode voltar para o dispositivo de onde veio: "
                "ela contém pinos GPIO, IP estático e hostname."
            )
        if self._store.get(device.id).online is False:
            problems.append(f"{device.name} está offline.")
        return problems

    def restore_warnings(self, device: Device, backup: Backup) -> list[str]:
        warnings = []
        info = self._store.get(device.id).info
        current_ver = (info.ver if info else None) or device.version
        if backup.kind == BackupKind.CFG:
            warnings.append("O dispositivo vai reiniciar sozinho logo após o envio.")
            warnings.append(
                "Senhas (Wi-Fi, AP, MQTT, OTA, PIN) não fazem parte do cfg.json e continuam "
                "as que estão no dispositivo hoje."
            )
            if backup.trigger == BackupTrigger.IMPORT:
                warnings.append(
                    "Este backup foi importado de um arquivo: o wledadmin não tem como "
                    "garantir que ele veio deste dispositivo. Confira no diff a seção "
                    "\"hw\" (pinos) antes de continuar."
                )
        if backup.device_mac != device.mac:
            warnings.append(f"Os presets vêm de outro dispositivo ({backup.device_name}). "
                            "Os presets atuais deste dispositivo serão substituídos.")
        if backup.firmware_version and current_ver and backup.firmware_version != current_ver:
            warnings.append(
                f"O backup foi feito no firmware {backup.firmware_version} e o dispositivo "
                f"está no {current_ver}."
            )
        return warnings

    async def restore(
        self, device: Device, backup: Backup, pin: str | None, item: JobItem
    ) -> None:
        kind = BackupKind(backup.kind)
        problems = self.restore_problems(device, backup)
        if problems:
            raise BackupError(" ".join(problems))

        async with self._registry.lock(device.id):
            item.step("Validando o conteúdo do backup")
            data = validate_content(backup.content, kind)
            payload = minify(data)

            item.step(f"Baixando o {kind.file_name} atual")
            try:
                current = await self._client.download_file(device.ip, kind.file_name)
            except WLEDError as e:
                raise BackupError(f"não consegui ler o arquivo atual: {e}") from e

            if current is not None:
                try:
                    if json.loads(current) == data:
                        item.status = ItemStatus.SKIPPED
                        item.message = "o dispositivo já tem exatamente este conteúdo"
                        return
                except ValueError:
                    pass  # arquivo atual corrompido: seguimos com a restauração
                safety = await self._store_version(
                    device, kind, current, BackupTrigger.PRE_RESTORE
                )
                item.step(f"Backup de segurança: versão #{safety.backup.id}"
                          + (" (já existia)" if safety.status == "unchanged" else ""))

            self._check_free_space(device, len(payload), len(current or b""))

            if pin:
                item.step("Desbloqueando as configurações com o PIN")
                try:
                    await self._client.unlock_settings(device.ip, pin)
                except WLEDError as e:
                    raise BackupError(f"falha ao enviar o PIN: {e}") from e
            try:
                item.step(f"Enviando {kind.file_name} ({len(payload)} bytes)")
                try:
                    reply = await self._client.upload_file(device.ip, kind.file_name, payload)
                except WLEDAuthRequired as e:
                    raise BackupError(
                        "PIN incorreto." if pin else
                        "o dispositivo tem PIN de configurações; informe o PIN e tente de novo."
                    ) from e
                except WLEDError as e:
                    raise BackupError(f"falha no envio: {e}") from e
                item.step(f"Resposta do WLED: {reply or '(vazia)'}")
            finally:
                if pin and kind is BackupKind.PRESETS:
                    # cfg.json reinicia o dispositivo, o que já bloqueia de novo.
                    try:
                        await self._client.lock_settings(device.ip)
                        item.step("Configurações bloqueadas de novo")
                    except WLEDError:
                        item.step("Aviso: não consegui bloquear de novo (expira em 15 min)")

            if kind is BackupKind.PRESETS:
                await self._verify_presets(device, data, item)
            else:
                await self._verify_reboot(device, item)

        item.message = "restaurado e verificado"
        if self._notify:
            self._notify()

    def _check_free_space(self, device: Device, new_size: int, current_size: int) -> None:
        info = self._store.get(device.id).info
        if info is None or info.fs.t is None or info.fs.u is None:
            return  # sem dados de FS: o WLED vai recusar se não couber
        free = (info.fs.t - info.fs.u) * 1024 + current_size
        if new_size > free - FS_MARGIN_BYTES:
            raise BackupError(
                f"sem espaço no dispositivo: o arquivo tem {new_size} bytes e há cerca de "
                f"{max(free, 0)} bytes livres"
            )

    async def _verify_presets(self, device: Device, data: dict, item: JobItem) -> None:
        item.step("Conferindo o arquivo gravado")
        try:
            written = await self._client.download_file(device.ip, "presets.json")
        except WLEDError as e:
            raise BackupError(f"enviado, mas não consegui conferir: {e}") from e
        try:
            ok = written is not None and json.loads(written) == data
        except ValueError:
            ok = False
        if not ok:
            raise BackupError("o arquivo lido de volta é diferente do enviado")
        item.step("Conteúdo conferido")

    async def _verify_reboot(self, device: Device, item: JobItem) -> None:
        item.step("Aguardando o dispositivo reiniciar")
        try:
            info = await self._client.wait_for_reboot(
                device.ip,
                device.mac,
                timeout=self._reboot_timeout,
                initial_delay=self._reboot_initial_delay,
                interval=self._reboot_poll_interval,
            )
        except WLEDTimeout as e:
            raise BackupError(
                f"{e}. Se ele não aparecer, procure a rede Wi-Fi \"WLED-AP\" (modo de "
                "recuperação) ou restaure a versão de segurança."
            ) from e
        item.step(f"Dispositivo voltou (firmware {info.ver or '?'})")


def format_age(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "nunca"
    seconds = int(((now or utcnow()) - dt).total_seconds())
    if seconds < 90:
        return "agora"
    if seconds < 3600:
        return f"há {seconds // 60} min"
    if seconds < 86400 * 2:
        return f"há {seconds // 3600} h"
    return f"há {seconds // 86400} d"
