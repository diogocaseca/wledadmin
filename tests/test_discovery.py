import pytest

from wledadmin.services.discovery import (
    DeviceProber,
    MDNSDiscovery,
    NotWLED,
    ScanAlreadyRunning,
    Scanner,
    ScanStatus,
    expand_targets,
)

from .fakes import INFO_014, INFO_015, FakeDevice


def test_expand_targets():
    assert expand_targets(["192.168.1.0/30"], 100) == ["192.168.1.1", "192.168.1.2"]
    assert expand_targets(["10.0.0.5", "10.0.0.5/32"], 100) == ["10.0.0.5"]
    with pytest.raises(ValueError, match="grande demais"):
        expand_targets(["10.0.0.0/16"], 1000)
    with pytest.raises(ValueError, match="inválida"):
        expand_targets(["banana"], 10)
    with pytest.raises(ValueError, match="IPv4"):
        expand_targets(["fe80::/120"], 10)


async def test_probe_upserts_by_mac_and_follows_ip_change(client, network, registry):
    prober = DeviceProber(client, registry)
    network.add("10.0.0.1", FakeDevice.from_fixture(INFO_014))
    d1 = await prober.probe_and_register("10.0.0.1", "mdns", hostname="wled-sala.local")
    # Mesmo dispositivo (mesmo MAC) aparece em outro IP via DHCP
    network.devices["10.0.0.9"] = network.devices.pop("10.0.0.1")
    d2 = await prober.probe_and_register("10.0.0.9", "scan")
    assert d1.id == d2.id
    devices = await registry.list()
    assert len(devices) == 1 and devices[0].ip == "10.0.0.9"
    assert devices[0].source == "mdns" and devices[0].hostname == "wled-sala.local"


async def test_probe_rejects_non_wled(client, network, registry):
    prober = DeviceProber(client, registry)
    network.add("10.0.0.2", FakeDevice(info={"brand": "Tasmota", "ver": "13"}))
    with pytest.raises(NotWLED):
        await prober.probe_and_register("10.0.0.2", "scan")
    network.add("10.0.0.3", FakeDevice.from_fixture(INFO_014, info={"mac": ""}))
    with pytest.raises(NotWLED):
        await prober.probe_and_register("10.0.0.3", "scan")
    assert await registry.list() == []


async def test_scan_finds_only_wled(client, network, registry):
    network.add("192.168.1.2", FakeDevice.from_fixture(INFO_014))
    network.add("192.168.1.5", FakeDevice.from_fixture(INFO_015))
    network.add("192.168.1.3", FakeDevice(info={"hello": "router"}))
    network.add("192.168.1.4", FakeDevice.from_fixture(INFO_014, timeout=True))
    scanner = Scanner(DeviceProber(client, registry), concurrency=4, timeout=0.5, max_hosts=256)
    job = scanner.start(["192.168.1.0/29"])
    with pytest.raises(ScanAlreadyRunning):
        scanner.start(["192.168.1.0/29"])
    await scanner.wait()
    assert job.status is ScanStatus.DONE
    assert job.done == job.total == 6
    assert sorted(job.found) == ["192.168.1.2", "192.168.1.5"]
    assert {d.ip for d in await registry.list()} == {"192.168.1.2", "192.168.1.5"}


async def test_mdns_handler_registers_and_swallows_errors(client, network, registry):
    mdns = MDNSDiscovery(DeviceProber(client, registry))
    network.add("10.0.0.7", FakeDevice.from_fixture(INFO_015))
    await mdns.handle_found("10.0.0.7", "wled-cozinha.local")
    await mdns.handle_found("10.0.0.8", None)  # offline: não pode levantar
    devices = await registry.list()
    assert [(d.name, d.source) for d in devices] == [("Cozinha", "mdns")]
