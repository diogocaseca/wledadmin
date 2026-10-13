import asyncio
import time

import pytest

from wledadmin.services.poller import Poller
from wledadmin.wled import DeviceInfo

from .fakes import INFO_014, INFO_015, INFO_16, FakeDevice, load_fixture


def make_poller(client, registry, store, **kw):
    opts = {"interval": 3600, "concurrency": 8, "offline_poll_every": 3, "device_timeout": 5}
    return Poller(client, registry, store, **{**opts, **kw})


async def register(registry, fixture, ip):
    return await registry.upsert_from_info(DeviceInfo.model_validate(load_fixture(fixture)), ip,
                                           "manual")


async def test_poll_mixed_online_offline(client, network, registry, store):
    network.add("10.0.0.1", FakeDevice.from_fixture(INFO_014))
    network.add("10.0.0.2", FakeDevice.from_fixture(INFO_015))
    off = network.add("10.0.0.3", FakeDevice.from_fixture(INFO_16, offline=True))
    a = await register(registry, INFO_014, "10.0.0.1")
    b = await register(registry, INFO_015, "10.0.0.2")
    c = await register(registry, INFO_16, "10.0.0.3")
    poller = make_poller(client, registry, store)

    await poller.poll_once()
    assert store.get(a.id).online is True
    assert store.get(b.id).info.name == "Cozinha"
    # Primeira falha: ainda não marca offline (anti-flapping)
    assert store.get(c.id).online is None
    assert store.get(c.id).consecutive_failures == 1

    await poller.poll_once()
    assert store.get(c.id).online is False

    off.offline = False
    # Backoff: offline só é consultado a cada 3 ciclos
    await poller.poll_once()  # ciclo 3 -> consulta
    assert store.get(c.id).online is True


async def test_offline_backoff_skips_requests(client, network, registry, store):
    network.add("10.0.0.3", FakeDevice.from_fixture(INFO_16, offline=True))
    c = await register(registry, INFO_16, "10.0.0.3")
    poller = make_poller(client, registry, store, offline_poll_every=10)
    await poller.poll_once()
    await poller.poll_once()  # marca offline
    assert store.get(c.id).online is False
    before = len(network.requests)
    for _ in range(5):
        await poller.poll_once()
    assert len(network.requests) == before


async def test_slow_device_does_not_block_others(client, network, registry, store, monkeypatch):
    network.add("10.0.0.1", FakeDevice.from_fixture(INFO_014))
    network.add("10.0.0.2", FakeDevice.from_fixture(INFO_015))
    a = await register(registry, INFO_014, "10.0.0.1")
    b = await register(registry, INFO_015, "10.0.0.2")

    original = client.get_state_info

    async def slow(host):
        if host == "10.0.0.2":
            await asyncio.sleep(10)
        return await original(host)

    monkeypatch.setattr(client, "get_state_info", slow)
    poller = make_poller(client, registry, store, device_timeout=0.2)
    t0 = time.monotonic()
    await poller.poll_once()
    assert time.monotonic() - t0 < 2
    assert store.get(a.id).online is True
    assert store.get(b.id).consecutive_failures == 1


async def test_mac_mismatch_marks_failure(client, network, registry, store):
    a = await register(registry, INFO_014, "10.0.0.1")
    # Outro WLED assumiu o IP
    network.add("10.0.0.1", FakeDevice.from_fixture(INFO_015))
    await make_poller(client, registry, store).poll_once()
    st = store.get(a.id)
    assert st.online is not True and "outro MAC" in st.last_error


async def test_poll_persists_firmware_change(client, network, registry, store):
    a = await register(registry, INFO_014, "10.0.0.1")
    network.add("10.0.0.1", FakeDevice.from_fixture(INFO_014, info={"ver": "0.15.0",
                                                                    "release": "ESP8266"}))
    await make_poller(client, registry, store).poll_once()
    dev = await registry.get(a.id)
    assert dev.version == "0.15.0" and dev.release == "ESP8266"


@pytest.mark.parametrize("n", [40])
async def test_many_devices_concurrent(client, network, registry, store, n):
    for i in range(n):
        ip = f"10.0.1.{i + 1}"
        mac = f"0000000001{i:02x}"
        network.add(ip, FakeDevice.from_fixture(INFO_014, info={"mac": mac}))
        await registry.upsert_from_info(
            DeviceInfo.model_validate({**load_fixture(INFO_014), "mac": mac}), ip, "scan"
        )
    await make_poller(client, registry, store).poll_once()
    assert sum(1 for s in store.all().values() if s.online) == n


async def test_run_loop_start_stop_and_wake(client, network, registry, store):
    network.add("10.0.0.1", FakeDevice.from_fixture(INFO_014))
    a = await register(registry, INFO_014, "10.0.0.1")
    poller = make_poller(client, registry, store)
    poller.start()
    for _ in range(50):
        if store.get(a.id).online:
            break
        await asyncio.sleep(0.02)
    assert store.get(a.id).online is True
    n = len(network.requests)
    poller.wake()
    for _ in range(50):
        if len(network.requests) > n:
            break
        await asyncio.sleep(0.02)
    assert len(network.requests) > n
    await poller.stop()
