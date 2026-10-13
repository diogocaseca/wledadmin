"""Descoberta contínua via mDNS (_wled._tcp).

Exige que o processo enxergue o multicast da LAN. No Docker, isso significa
network_mode: host (veja o README).
"""

from __future__ import annotations

import asyncio
import logging
import time

from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from ...wled import WLEDError
from .probe import DeviceProber, NotWLED

log = logging.getLogger(__name__)

SERVICE_TYPE = "_wled._tcp.local."
# Anúncios "Updated" chegam com frequência; não sondamos o mesmo nome mais que isso.
REPROBE_AFTER = 60.0


class MDNSDiscovery:
    def __init__(self, prober: DeviceProber) -> None:
        self._prober = prober
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._tasks: set[asyncio.Task] = set()
        self._last_probe: dict[str, float] = {}

    async def start(self) -> None:
        self._azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        self._browser = AsyncServiceBrowser(
            self._azc.zeroconf, [SERVICE_TYPE], handlers=[self._on_change]
        )
        log.info("Descoberta mDNS iniciada (%s)", SERVICE_TYPE)

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.async_cancel()
        for t in list(self._tasks):
            t.cancel()
        if self._azc is not None:
            await self._azc.async_close()

    def _on_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change not in (ServiceStateChange.Added, ServiceStateChange.Updated):
            return
        now = time.monotonic()
        if now - self._last_probe.get(name, -REPROBE_AFTER) < REPROBE_AFTER:
            return
        self._last_probe[name] = now
        task = asyncio.ensure_future(self._resolve(zeroconf, service_type, name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zeroconf, 3000):
            log.debug("mDNS: não resolveu %s", name)
            return
        addrs = info.parsed_addresses(IPVersion.V4Only)
        if not addrs:
            return
        hostname = (info.server or "").rstrip(".") or None
        await self.handle_found(addrs[0], hostname)

    async def handle_found(self, ip: str, hostname: str | None) -> None:
        try:
            await self._prober.probe_and_register(ip, "mdns", hostname=hostname)
        except (WLEDError, NotWLED, ValueError) as e:
            log.info("mDNS: %s ignorado: %s", ip, e)
        except Exception:
            log.exception("mDNS: erro ao registrar %s", ip)
