"""Jobs em segundo plano com progresso por dispositivo.

Usado por backup em lote, restauração e cópia de presets, e pensado para o OTA da
fase 3: cada item registra etapas legíveis, e jobs "exclusivos" (que escrevem no
dispositivo) rodam um de cada vez, na ordem em que foram pedidos.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ..models import Device, utcnow

log = logging.getLogger(__name__)

MAX_JOBS_KEPT = 50


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"


class ItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"


class StepFailed(Exception):
    """Falha esperada de uma etapa, com mensagem para o usuário."""


@dataclass
class JobItem:
    device_id: int
    name: str
    status: ItemStatus = ItemStatus.PENDING
    message: str | None = None
    steps: list[str] = field(default_factory=list)

    def step(self, text: str) -> None:
        self.steps.append(text)


@dataclass
class Job:
    id: int
    title: str
    exclusive: bool
    items: list[JobItem]
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in ItemStatus}
        for it in self.items:
            out[it.status.value] += 1
        return out

    @property
    def done_count(self) -> int:
        return sum(1 for it in self.items if it.status not in (ItemStatus.PENDING,
                                                               ItemStatus.RUNNING))


Worker = Callable[[Device, JobItem], Awaitable[None]]


class JobManager:
    def __init__(self) -> None:
        self._jobs: OrderedDict[int, Job] = OrderedDict()
        self._ids = itertools.count(1)
        self._exclusive = asyncio.Lock()
        self._tasks: dict[int, asyncio.Task] = {}

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self) -> list[Job]:
        return list(reversed(self._jobs.values()))

    def start(
        self,
        title: str,
        devices: Sequence[Device],
        worker: Worker,
        *,
        concurrency: int = 1,
        exclusive: bool = False,
    ) -> Job:
        job = Job(
            id=next(self._ids),
            title=title,
            exclusive=exclusive,
            items=[JobItem(d.id, d.name) for d in devices],
        )
        self._jobs[job.id] = job
        while len(self._jobs) > MAX_JOBS_KEPT:
            self._jobs.popitem(last=False)
        task = asyncio.create_task(self._run(job, list(devices), worker, concurrency))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _t, jid=job.id: self._tasks.pop(jid, None))
        return job

    async def wait(self, job: Job) -> None:
        task = self._tasks.get(job.id)
        if task is not None:
            await asyncio.shield(task)

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job: Job, devices: list[Device], worker: Worker, concurrency: int):
        if job.exclusive:
            async with self._exclusive:
                await self._run_items(job, devices, worker, 1)
        else:
            await self._run_items(job, devices, worker, concurrency)

    async def _run_items(self, job, devices, worker, concurrency) -> None:
        job.status = JobStatus.RUNNING
        sem = asyncio.Semaphore(max(1, concurrency))

        async def run_one(device: Device, item: JobItem) -> None:
            async with sem:
                item.status = ItemStatus.RUNNING
                try:
                    await worker(device, item)
                    if item.status is ItemStatus.RUNNING:
                        item.status = ItemStatus.OK
                except StepFailed as e:
                    item.status = ItemStatus.FAILED
                    item.message = str(e)
                except Exception as e:
                    log.exception("Job %s: erro em %s", job.title, device.name)
                    item.status = ItemStatus.FAILED
                    item.message = f"erro inesperado: {e}"

        try:
            await asyncio.gather(*(run_one(d, it) for d, it in zip(devices, job.items,
                                                                  strict=True)))
        finally:
            job.status = JobStatus.DONE
            job.finished_at = utcnow()
