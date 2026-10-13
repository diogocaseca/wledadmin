"""Cadastro de dispositivos (SQLite) com upsert por MAC e lock por dispositivo."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..models import Device, utcnow
from ..wled import DeviceInfo

log = logging.getLogger(__name__)

# Persiste last_seen_at no máximo uma vez nesse intervalo (evita escrita a cada poll).
LAST_SEEN_PERSIST_EVERY = timedelta(minutes=5)


class DeviceRegistry:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory
        self._locks: dict[int, asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(self, device_id: int):
        """Serializa operações que alteram um dispositivo (ações, e no futuro backup/OTA)."""
        lock = self._locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            yield

    async def list(self, *, only_enabled: bool = False) -> list[Device]:
        async with self._sf() as s:
            q = select(Device).order_by(Device.name.collate("NOCASE"), Device.id)
            if only_enabled:
                q = q.where(Device.enabled.is_(True))
            return list((await s.scalars(q)).all())

    async def get(self, device_id: int) -> Device | None:
        async with self._sf() as s:
            return await s.get(Device, device_id)

    async def get_by_mac(self, mac: str) -> Device | None:
        async with self._sf() as s:
            return await s.scalar(select(Device).where(Device.mac == mac))

    async def get_many(self, ids: Sequence[int]) -> list[Device]:
        if not ids:
            return []
        async with self._sf() as s:
            return list((await s.scalars(select(Device).where(Device.id.in_(ids)))).all())

    async def delete(self, device_id: int) -> bool:
        async with self._sf() as s:
            dev = await s.get(Device, device_id)
            if dev is None:
                return False
            await s.delete(dev)
            await s.commit()
        self._locks.pop(device_id, None)
        return True

    async def upsert_from_info(
        self, info: DeviceInfo, ip: str, source: str, hostname: str | None = None
    ) -> Device:
        """Cria ou atualiza pelo MAC. Se o IP mudou (DHCP), atualiza o IP."""
        mac = info.mac_normalized
        if mac is None:
            raise ValueError(f"{ip}: /json/info sem MAC válido")
        async with self._sf() as s:
            dev = await s.scalar(select(Device).where(Device.mac == mac))
            now = utcnow()
            if dev is None:
                dev = Device(
                    mac=mac, ip=ip, hostname=hostname, source=source,
                    name=info.name or ip, created_at=now, enabled=True,
                )
                s.add(dev)
                log.info("Novo dispositivo %s (%s) via %s", dev.name, ip, source)
            else:
                if dev.ip != ip:
                    log.info("Dispositivo %s mudou de IP: %s -> %s", dev.name, dev.ip, ip)
                    dev.ip = ip
                if hostname:
                    dev.hostname = hostname
            _apply_info(dev, info)
            dev.last_seen_at = now
            await s.commit()
            return dev

    async def sync_from_poll(self, device: Device, info: DeviceInfo) -> None:
        """Grava só se algum campo de mudança lenta mudou, ou se last_seen está velho."""
        now = utcnow()
        snapshot = (device.name, device.version, device.vid, device.release, device.arch,
                    device.led_count)
        probe = Device(name=device.name, arch=device.arch)
        _apply_info(probe, info)
        changed = snapshot != (probe.name, probe.version, probe.vid, probe.release, probe.arch,
                               probe.led_count)
        stale = device.last_seen_at is None or now - device.last_seen_at > LAST_SEEN_PERSIST_EVERY
        if not (changed or stale):
            return
        async with self._sf() as s:
            dev = await s.get(Device, device.id)
            if dev is None:
                return
            _apply_info(dev, info)
            dev.last_seen_at = now
            await s.commit()
        device.last_seen_at = now


def _apply_info(dev: Device, info: DeviceInfo) -> None:
    if info.name:
        dev.name = info.name
    dev.version = info.ver
    dev.vid = info.vid
    dev.release = info.release
    dev.arch = info.arch.value
    dev.led_count = info.leds.count
