"""Monta e conecta todos os serviços. Um único lugar para o ciclo de vida."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ..config import Settings
from ..db import create_engine, create_session_factory, run_migrations
from ..wled import WLEDClient, build_http_client
from .actions import ActionService
from .auth import AuthService
from .discovery import DeviceProber, MDNSDiscovery, Scanner
from .poller import Poller
from .registry import DeviceRegistry
from .status_store import StatusStore

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker
    http: httpx.AsyncClient
    client: WLEDClient
    registry: DeviceRegistry
    store: StatusStore
    poller: Poller
    prober: DeviceProber
    scanner: Scanner
    actions: ActionService
    auth: AuthService
    mdns: MDNSDiscovery | None = None

    @classmethod
    async def create(
        cls, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> Services:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        await run_migrations(settings.db_url)
        engine = create_engine(settings.db_url)
        sf = create_session_factory(engine)

        http = build_http_client(settings.http_connect_timeout, settings.http_timeout, transport)
        client = WLEDClient(http)
        registry = DeviceRegistry(sf)
        store = StatusStore(settings.offline_after_failures)
        poller = Poller(
            client, registry, store,
            interval=settings.poll_interval,
            concurrency=settings.poll_concurrency,
            offline_poll_every=settings.offline_poll_every,
            device_timeout=settings.http_timeout * 2 + 1,
        )
        prober = DeviceProber(client, registry, on_new_or_changed=poller.wake)
        scanner = Scanner(
            prober,
            concurrency=settings.scan_concurrency,
            timeout=settings.scan_timeout,
            max_hosts=settings.scan_max_hosts,
        )
        actions = ActionService(client, registry, store, concurrency=settings.poll_concurrency)
        auth = AuthService(sf)
        await auth.ensure_default_admin()
        return cls(settings, engine, sf, http, client, registry, store, poller, prober,
                   scanner, actions, auth)

    async def start_background(self) -> None:
        self.poller.start()
        if self.settings.mdns_enabled:
            self.mdns = MDNSDiscovery(self.prober)
            try:
                await self.mdns.start()
            except OSError as e:
                # Sem multicast (ex.: Docker em bridge). Varredura/manual continuam valendo.
                log.warning("mDNS indisponível (%s); use a varredura ou o cadastro manual", e)
                self.mdns = None

    async def close(self) -> None:
        if self.mdns is not None:
            await self.mdns.stop()
        await self.scanner.stop()
        await self.poller.stop()
        await self.http.aclose()
        await self.engine.dispose()
