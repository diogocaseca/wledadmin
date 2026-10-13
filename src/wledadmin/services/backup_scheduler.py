"""Backup automático em segundo plano.

Em vez de "rodar à meia-noite", o agendador verifica periodicamente quais dispositivos
estão online e com a última verificação de backup (devices.last_backup_at) mais velha
que o intervalo configurado. Assim ele sobrevive a reinícios do wledadmin e faz o
backup atrasado de um dispositivo assim que ele volta a ficar online.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from ..models import utcnow
from ..wled import WLEDError
from .app_settings import BACKUP_INTERVAL_HOURS, AppSettingsService
from .backup import BackupService, BackupTrigger
from .jobs import StepFailed
from .registry import DeviceRegistry
from .status_store import StatusStore

log = logging.getLogger(__name__)

# Depois de uma falha, espera isso antes de tentar o mesmo dispositivo de novo.
RETRY_AFTER_FAILURE = 3600.0


class BackupScheduler:
    def __init__(
        self,
        backups: BackupService,
        registry: DeviceRegistry,
        store: StatusStore,
        app_settings: AppSettingsService,
        *,
        check_every: float = 600.0,
        startup_delay: float = 60.0,
        concurrency: int = 4,
    ) -> None:
        self._backups = backups
        self._registry = registry
        self._store = store
        self._settings = app_settings
        self._check_every = check_every
        self._startup_delay = startup_delay
        self._concurrency = concurrency
        self._failures: dict[int, float] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="wledadmin-backup-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _sleep(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    async def _run(self) -> None:
        # Dá tempo ao poller de descobrir quem está online.
        if await self._sleep(self._startup_delay):
            return
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("Erro inesperado no backup agendado")
            if await self._sleep(self._check_every):
                return

    async def run_once(self) -> int:
        """Faz backup dos dispositivos vencidos. Retorna quantos foram processados."""
        hours = self._settings.get(BACKUP_INTERVAL_HOURS)
        if hours <= 0:
            return 0
        interval = timedelta(hours=hours)
        now = utcnow()
        mono = time.monotonic()
        due = [
            d for d in await self._registry.list(only_enabled=True)
            if self._store.get(d.id).online is True
            and (d.last_backup_at is None or now - d.last_backup_at >= interval)
            and mono - self._failures.get(d.id, -RETRY_AFTER_FAILURE) >= RETRY_AFTER_FAILURE
        ]
        sem = asyncio.Semaphore(self._concurrency)

        async def one(device) -> None:
            async with sem:
                try:
                    await self._backups.backup_device(device, BackupTrigger.SCHEDULED)
                    self._failures.pop(device.id, None)
                except (WLEDError, StepFailed) as e:
                    self._failures[device.id] = time.monotonic()
                    log.warning("Backup agendado de %s falhou: %s", device.name, e)

        await asyncio.gather(*(one(d) for d in due))
        if due:
            log.info("Backup agendado: %d dispositivo(s) verificados", len(due))
        return len(due)
