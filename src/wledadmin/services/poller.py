"""Atualização periódica do status em segundo plano.

Uma rodada = um GET /json/si por dispositivo, em paralelo, limitado por semáforo.
Cada dispositivo tem tratamento de erro isolado: um offline só gera uma exceção
capturada para ele mesmo e nunca atrasa os demais além do próprio timeout.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable

from ..models import Device
from ..wled import WLEDClient, WLEDError
from .registry import DeviceRegistry
from .status_store import StatusStore

log = logging.getLogger(__name__)


class Poller:
    def __init__(
        self,
        client: WLEDClient,
        registry: DeviceRegistry,
        store: StatusStore,
        *,
        interval: float,
        concurrency: int,
        offline_poll_every: int,
        device_timeout: float,
    ) -> None:
        self._client = client
        self._registry = registry
        self._store = store
        self._interval = interval
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._offline_every = max(1, offline_poll_every)
        # Salvaguarda além dos timeouts do httpx (ex.: fallback de /json/si em 2 chamadas).
        self._device_timeout = device_timeout
        self._cycle = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    # ---- ciclo de vida ------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="wledadmin-poller")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    def wake(self) -> None:
        """Pede uma rodada imediata (ex.: após adicionar dispositivos)."""
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.poll_once()
            except Exception:  # nunca deixa o loop morrer
                log.exception("Erro inesperado na rodada de polling")
            remaining = max(0.0, self._interval - (time.monotonic() - started))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=remaining)
            except TimeoutError:
                pass

    # ---- polling ------------------------------------------------------------------

    async def poll_once(self) -> None:
        self._cycle += 1
        devices = await self._registry.list(only_enabled=True)
        due = [d for d in devices if self._is_due(d)]
        await self.poll_devices(due)

    def _is_due(self, device: Device) -> bool:
        # Backoff: dispositivos já confirmados offline são consultados com menos frequência.
        if self._store.get(device.id).online is False:
            return self._cycle % self._offline_every == 0
        return True

    async def poll_devices(self, devices: Iterable[Device]) -> None:
        await asyncio.gather(*(self._poll_device(d) for d in devices))

    async def _poll_device(self, device: Device) -> None:
        async with self._sem:
            try:
                async with asyncio.timeout(self._device_timeout):
                    state, info = await self._client.get_state_info(device.ip)
            except (WLEDError, TimeoutError) as e:
                self._store.record_failure(device.id, str(e) or "timeout")
                return

            mac = info.mac_normalized
            if mac is not None and mac != device.mac:
                # Outro dispositivo assumiu este IP; o "nosso" está em outro lugar.
                self._store.record_failure(
                    device.id, f"{device.ip} agora responde com outro MAC ({mac})"
                )
                return

            self._store.record_success(device.id, info, state)
            try:
                await self._registry.sync_from_poll(device, info)
            except Exception:
                log.exception("Falha ao persistir dados de %s", device.ip)
