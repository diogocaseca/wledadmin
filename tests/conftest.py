from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from wledadmin.config import Settings
from wledadmin.db import create_engine, create_session_factory, run_migrations
from wledadmin.main import create_app
from wledadmin.services.registry import DeviceRegistry
from wledadmin.services.status_store import StatusStore
from wledadmin.wled import WLEDClient, build_http_client

from .fakes import FakeWLEDNetwork


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        mdns_enabled=False,
        poll_interval=3600,
        scan_subnets="192.168.1.48/29",
    )


@pytest.fixture
def network() -> FakeWLEDNetwork:
    return FakeWLEDNetwork()


@pytest.fixture
async def client(network):
    http = build_http_client(1.0, 2.0, transport=network.transport())
    yield WLEDClient(http)
    await http.aclose()


@pytest.fixture
async def session_factory(settings):
    await run_migrations(settings.db_url)
    engine = create_engine(settings.db_url)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def registry(session_factory) -> DeviceRegistry:
    return DeviceRegistry(session_factory)


@pytest.fixture
def store() -> StatusStore:
    return StatusStore(offline_after_failures=2)


def basic(user: str, pw: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


ADMIN = basic("admin", "admin")


@pytest.fixture
def app_client(settings, network):
    app = create_app(settings, start_background=False, wled_transport=network.transport())
    with TestClient(app, base_url="http://wledadmin.test") as tc:
        yield tc
