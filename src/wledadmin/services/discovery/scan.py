"""Varredura de faixa de IP sob demanda (fallback para quando o mDNS não alcança)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ...models import utcnow
from ...wled import WLEDError
from .probe import DeviceProber, NotWLED

log = logging.getLogger(__name__)


def expand_targets(specs: Iterable[str], max_hosts: int) -> list[str]:
    """Aceita CIDRs ("192.168.1.0/24") e IPs avulsos. Remove duplicados, mantém ordem."""
    seen: dict[str, None] = {}
    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        try:
            net = ipaddress.ip_network(spec, strict=False)
        except ValueError as e:
            raise ValueError(f"faixa inválida: {spec!r}") from e
        if net.version != 4:
            raise ValueError(f"só IPv4 é suportado: {spec!r}")
        hosts = [net.network_address] if net.num_addresses == 1 else net.hosts()
        for h in hosts:
            seen[str(h)] = None
            if len(seen) > max_hosts:
                raise ValueError(f"faixa grande demais (máximo {max_hosts} hosts)")
    return list(seen)


class ScanStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class ScanJob:
    targets: list[str]
    status: ScanStatus = ScanStatus.RUNNING
    done: int = 0
    found: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def total(self) -> int:
        return len(self.targets)


class ScanAlreadyRunning(Exception):
    pass


class Scanner:
    def __init__(
        self, prober: DeviceProber, *, concurrency: int, timeout: float, max_hosts: int
    ) -> None:
        self._prober = prober
        self._concurrency = concurrency
        self._timeout = timeout
        self._max_hosts = max_hosts
        self.current: ScanJob | None = None
        self._task: asyncio.Task | None = None

    def start(self, specs: Iterable[str]) -> ScanJob:
        if self.current and self.current.status is ScanStatus.RUNNING:
            raise ScanAlreadyRunning("já existe uma varredura em andamento")
        targets = expand_targets(specs, self._max_hosts)
        if not targets:
            raise ValueError("nenhuma faixa de IP informada")
        job = ScanJob(targets=targets)
        self.current = job
        self._task = asyncio.create_task(self._run(job), name="wledadmin-scan")
        return job

    async def wait(self) -> None:
        if self._task:
            await self._task

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self, job: ScanJob) -> None:
        sem = asyncio.Semaphore(max(1, self._concurrency))

        async def probe(ip: str) -> None:
            async with sem:
                try:
                    await self._prober.probe_and_register(ip, "scan", timeout=self._timeout)
                    job.found.append(ip)
                except (WLEDError, NotWLED, ValueError):
                    pass  # host vazio ou não-WLED: esperado na maioria dos IPs
                finally:
                    job.done += 1

        try:
            await asyncio.gather(*(probe(ip) for ip in job.targets))
            job.status = ScanStatus.DONE
        except Exception as e:
            log.exception("Varredura falhou")
            job.status = ScanStatus.FAILED
            job.error = str(e)
        finally:
            job.finished_at = utcnow()
            log.info("Varredura: %d/%d hosts, %d WLED", job.done, job.total, len(job.found))
