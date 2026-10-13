"""Ponto comum de entrada para mDNS, varredura e cadastro manual.

Os três caminhos terminam aqui: confirmar que o IP é um WLED via /json/info e fazer
upsert pelo MAC.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ...models import Device
from ...wled import WLEDClient
from ..registry import DeviceRegistry

log = logging.getLogger(__name__)


class NotWLED(Exception):
    pass


class DeviceProber:
    def __init__(
        self,
        client: WLEDClient,
        registry: DeviceRegistry,
        on_new_or_changed: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._notify = on_new_or_changed

    async def probe_and_register(
        self,
        ip: str,
        source: str,
        *,
        hostname: str | None = None,
        timeout: float | None = None,
    ) -> Device:
        """Levanta WLEDError se inacessível, NotWLED se responder mas não for WLED."""
        info = await self._client.get_info(ip, timeout=timeout)
        if not info.looks_like_wled:
            raise NotWLED(f"{ip} respondeu, mas não parece ser um WLED")
        if info.mac_normalized is None:
            raise NotWLED(f"{ip} não informou um MAC válido")
        device = await self._registry.upsert_from_info(info, ip, source, hostname)
        if self._notify:
            self._notify()
        return device
