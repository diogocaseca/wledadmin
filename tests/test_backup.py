import json

import pytest

from wledadmin.services.app_settings import BACKUP_INTERVAL_HOURS, BACKUP_KEEP, AppSettingsService
from wledadmin.services.backup import (
    BackupError,
    BackupKind,
    BackupService,
    BackupTrigger,
    detect_kind,
    json_diff,
    validate_content,
)
from wledadmin.services.backup_scheduler import BackupScheduler
from wledadmin.services.jobs import ItemStatus, JobItem, JobManager, JobStatus
from wledadmin.wled import DeviceInfo

from .fakes import INFO_014, INFO_015, INFO_16, FakeDevice, fixture_bytes, load_fixture

IP_A, IP_B, IP_C = "10.0.0.1", "10.0.0.2", "10.0.0.3"


@pytest.fixture
async def app_settings(session_factory):
    svc = AppSettingsService(session_factory)
    await svc.load()
    return svc


@pytest.fixture
def backups(session_factory, client, registry, store, app_settings):
    return BackupService(
        session_factory, client, registry, store, app_settings,
        reboot_timeout=2.0, reboot_initial_delay=0.0, reboot_poll_interval=0.01,
    )


async def add(network, registry, store, fixture, ip, **kw):
    dev = network.add(ip, FakeDevice.from_fixture(fixture, **kw))
    info = DeviceInfo.model_validate(dev.info)
    device = await registry.upsert_from_info(info, ip, "manual")
    store.record_success(device.id, info, None)
    return dev, device


def item(device) -> JobItem:
    return JobItem(device.id, device.name)


# ---- conteúdo ----------------------------------------------------------------------


def test_detect_kind():
    assert detect_kind(load_fixture("cfg.json")) is BackupKind.CFG
    assert detect_kind(load_fixture("presets.json")) is BackupKind.PRESETS
    assert detect_kind({"foo": 1}) is None


def test_validate_content_rejects_wrong_kind_and_garbage():
    with pytest.raises(BackupError, match="presets"):
        validate_content(fixture_bytes("presets.json"), BackupKind.CFG)
    with pytest.raises(BackupError, match="JSON válido"):
        validate_content(b"{not json", BackupKind.PRESETS)
    with pytest.raises(BackupError, match="objeto"):
        validate_content(b"[1,2]", BackupKind.PRESETS)


def test_json_diff_ignores_formatting_and_key_order():
    a = b'{"b": 1, "a": {"x": 1}}'
    b = b'{"a":{"x":1},"b":1}'
    assert json_diff(a, b) == []
    diff = json_diff(a, b'{"a": {"x": 2}, "b": 1}')
    assert [(d.kind, d.text.strip()) for d in diff if d.kind in ("add", "del")] == [
        ("del", '"x": 1'), ("add", '"x": 2')]


# ---- backup ------------------------------------------------------------------------


async def test_backup_creates_then_dedupes(network, registry, store, backups):
    dev, device = await add(network, registry, store, INFO_014, IP_A)

    out = await backups.backup_device(device, BackupTrigger.MANUAL)
    assert [(o.kind, o.status) for o in out] == [(BackupKind.CFG, "created"),
                                                 (BackupKind.PRESETS, "created")]
    out = await backups.backup_device(device, BackupTrigger.SCHEDULED)
    assert [o.status for o in out] == ["unchanged", "unchanged"]

    presets = load_fixture("presets.json")
    presets["4"] = {"n": "Novo", "on": True}
    dev.files["presets.json"] = json.dumps(presets).encode()
    out = await backups.backup_device(device, BackupTrigger.SCHEDULED)
    assert [o.status for o in out] == ["unchanged", "created"]

    history = await backups.list_for_mac(device.mac)
    assert len(history) == 3
    assert (await registry.get(device.id)).last_backup_at is not None
    assert history[0].firmware_version == "0.14.4" and history[0].trigger == "scheduled"


async def test_backup_missing_presets_and_invalid(network, registry, store, backups):
    dev, device = await add(network, registry, store, INFO_014, IP_A)
    del dev.files["presets.json"]
    out = await backups.backup_device(device, BackupTrigger.MANUAL)
    assert [o.status for o in out] == ["created", "missing"]

    dev.files["cfg.json"] = b"<html>oops</html>"
    with pytest.raises(BackupError, match="JSON"):
        await backups.backup_device(device, BackupTrigger.MANUAL)


async def test_backup_offline_raises(network, registry, store, backups):
    dev, device = await add(network, registry, store, INFO_014, IP_A)
    dev.offline = True
    with pytest.raises(BackupError, match="não consegui baixar"):
        await backups.backup_device(device, BackupTrigger.MANUAL)


async def test_retention_keeps_pinned(network, registry, store, backups, app_settings):
    await app_settings.set(BACKUP_KEEP, 2)
    dev, device = await add(network, registry, store, INFO_014, IP_A)
    del dev.files["cfg.json"]
    ids = []
    for i in range(5):
        dev.files["presets.json"] = json.dumps({"0": {}, "1": {"n": f"v{i}"}}).encode()
        out = await backups.backup_device(device, BackupTrigger.MANUAL)
        ids.append(out[1].backup.id)
        if i == 0:
            await backups.set_pinned(ids[0], True)
    remaining = [b.id for b in await backups.list_for_mac(device.mac)]
    assert remaining == [ids[4], ids[3], ids[0]]
    with pytest.raises(BackupError, match="desafixe"):
        await backups.delete(ids[0])


async def test_import(network, registry, store, backups):
    _, device = await add(network, registry, store, INFO_014, IP_A)
    out = await backups.import_backup(device, fixture_bytes("cfg.json"), None, "da UI")
    assert out.status == "created" and out.kind is BackupKind.CFG
    assert out.backup.trigger == "import" and out.backup.note == "da UI"
    again = await backups.import_backup(device, fixture_bytes("cfg.json"), None, None)
    assert again.status == "unchanged"
    with pytest.raises(BackupError, match="parece ser de presets"):
        await backups.import_backup(device, fixture_bytes("presets.json"), BackupKind.CFG, None)
    with pytest.raises(BackupError, match="não reconheci"):
        await backups.import_backup(device, b'{"hello": 1}', None, None)


# ---- restore -----------------------------------------------------------------------


async def _first(backups, device, kind):
    for b in await backups.list_for_mac(device.mac):
        if b.kind == kind:
            return await backups.get(b.id)
    raise AssertionError(kind)


async def test_restore_presets_with_safety_backup(network, registry, store, backups):
    dev, device = await add(network, registry, store, INFO_014, IP_A)
    await backups.backup_device(device, BackupTrigger.MANUAL)
    original = await _first(backups, device, "presets")
    dev.files["presets.json"] = b'{"0": {}, "9": {"n": "estragado"}}'

    it = item(device)
    await backups.restore(device, original, None, it)
    assert it.message == "restaurado e verificado"
    assert json.loads(dev.files["presets.json"]) == load_fixture("presets.json")
    # minificado no envio
    assert b": " not in dev.uploads[-1][1]
    history = await backups.list_for_mac(device.mac)
    assert history[0].trigger == "pre_restore"
    safety = await backups.get(history[0].id)
    assert json.loads(safety.content) == {"0": {}, "9": {"n": "estragado"}}


async def test_restore_identical_is_skipped(network, registry, store, backups):
    dev, device = await add(network, registry, store, INFO_014, IP_A)
    await backups.backup_device(device, BackupTrigger.MANUAL)
    it = item(device)
    await backups.restore(device, await _first(backups, device, "presets"), None, it)
    assert it.status is ItemStatus.SKIPPED and dev.uploads == []


async def test_restore_cfg_waits_for_reboot(network, registry, store, backups):
    dev, device = await add(network, registry, store, INFO_014, IP_A)
    await backups.backup_device(device, BackupTrigger.MANUAL)
    cfg = await _first(backups, device, "cfg")
    dev.files["cfg.json"] = b'{"rev": [1, 0], "id": {"name": "mudou"}}'
    dev.info["uptime"] = 99999

    it = item(device)
    await backups.restore(device, cfg, None, it)
    assert dev.uploads[-1][0] == "cfg.json"
    assert any("Aguardando" in s for s in it.steps)
    assert any("voltou" in s for s in it.steps)


async def test_restore_cfg_never_returns(network, registry, store, backups):
    dev, device = await add(network, registry, store, INFO_014, IP_A,
                            never_returns_after_reboot=True)
    await backups.backup_device(device, BackupTrigger.MANUAL)
    cfg = await _first(backups, device, "cfg")
    dev.files["cfg.json"] = b'{"rev": [1, 0]}'
    with pytest.raises(BackupError, match="WLED-AP"):
        await backups.restore(device, cfg, None, item(device))


async def test_cfg_cannot_go_to_another_device(network, registry, store, backups):
    _, sala = await add(network, registry, store, INFO_014, IP_A)
    _, cozinha = await add(network, registry, store, INFO_015, IP_B)
    await backups.backup_device(sala, BackupTrigger.MANUAL)
    cfg = await _first(backups, sala, "cfg")
    assert backups.restore_problems(cozinha, cfg)
    with pytest.raises(BackupError, match="só pode voltar"):
        await backups.restore(cozinha, cfg, None, item(cozinha))
    assert network.devices[IP_B].uploads == []


async def test_restore_with_pin(network, registry, store, backups):
    dev, device = await add(network, registry, store, INFO_014, IP_A, pin="1234")
    await backups.backup_device(device, BackupTrigger.MANUAL)
    presets = await _first(backups, device, "presets")
    dev.files["presets.json"] = b'{"0": {}}'

    with pytest.raises(BackupError, match="informe o PIN"):
        await backups.restore(device, presets, None, item(device))
    with pytest.raises(BackupError, match="PIN incorreto"):
        await backups.restore(device, presets, "9999", item(device))
    it = item(device)
    await backups.restore(device, presets, "1234", it)
    assert it.message == "restaurado e verificado"
    assert dev.unlocked is False, "deve bloquear de novo após o restore"


async def test_restore_checks_free_space(network, registry, store, backups):
    dev, device = await add(network, registry, store, INFO_014, IP_A,
                            info={"fs": {"u": 1023, "t": 1024}})
    await backups.backup_device(device, BackupTrigger.MANUAL)
    big = {"0": {}} | {str(i): {"n": "x" * 200} for i in range(1, 60)}
    out = await backups.import_backup(device, json.dumps(big).encode(), None, None)
    with pytest.raises(BackupError, match="sem espaço"):
        await backups.restore(device, await backups.get(out.backup.id), None, item(device))
    assert dev.uploads == []


async def test_upload_failure_is_reported(network, registry, store, backups):
    dev, device = await add(network, registry, store, INFO_014, IP_A)
    await backups.backup_device(device, BackupTrigger.MANUAL)
    presets = await _first(backups, device, "presets")
    dev.files["presets.json"] = b'{"0": {}}'
    dev.upload_fails = True
    with pytest.raises(BackupError, match="falha no envio"):
        await backups.restore(device, presets, None, item(device))


# ---- jobs / cópia de presets -------------------------------------------------------


async def test_copy_presets_job(network, registry, store, backups):
    _, sala = await add(network, registry, store, INFO_014, IP_A)
    cozinha_dev, cozinha = await add(network, registry, store, INFO_015, IP_B)
    quarto_dev, quarto = await add(network, registry, store, INFO_16, IP_C)
    cozinha_dev.files["presets.json"] = b'{"0": {}}'
    quarto_dev.offline = True
    store.record_failure(quarto.id, "x")
    store.record_failure(quarto.id, "x")
    await backups.backup_device(sala, BackupTrigger.MANUAL)
    presets = await _first(backups, sala, "presets")

    jobs = JobManager()

    async def worker(device, it):
        await backups.restore(device, presets, None, it)

    job = jobs.start("copiar", [cozinha, quarto], worker, exclusive=True)
    await jobs.wait(job)
    by_name = {it.name: it for it in job.items}
    assert by_name["Cozinha"].status is ItemStatus.OK
    assert by_name["Quarto"].status is ItemStatus.FAILED and "offline" in by_name["Quarto"].message
    assert json.loads(cozinha_dev.files["presets.json"]) == load_fixture("presets.json")


async def test_exclusive_jobs_run_one_at_a_time():
    import asyncio

    jobs = JobManager()
    running, peak = 0, 0

    async def worker(_device, _item):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

    class D:
        def __init__(self, i):
            self.id, self.name = i, f"d{i}"

    j1 = jobs.start("a", [D(1), D(2)], worker, exclusive=True)
    j2 = jobs.start("b", [D(3)], worker, exclusive=True)
    await asyncio.sleep(0)
    assert j2.status is JobStatus.QUEUED
    await jobs.wait(j1)
    await jobs.wait(j2)
    assert peak == 1 and j2.status is JobStatus.DONE


# ---- agendador ---------------------------------------------------------------------


async def test_scheduler(network, registry, store, backups, app_settings):
    _, sala = await add(network, registry, store, INFO_014, IP_A)
    cozinha_dev, cozinha = await add(network, registry, store, INFO_015, IP_B)
    scheduler = BackupScheduler(backups, registry, store, app_settings)

    assert await scheduler.run_once() == 2
    assert await scheduler.run_once() == 0, "nada vencido logo depois"

    await app_settings.set(BACKUP_INTERVAL_HOURS, 0)
    async with backups._sf() as s:  # força "vencido"
        from sqlalchemy import update

        from wledadmin.models import Device
        await s.execute(update(Device).values(last_backup_at=None))
        await s.commit()
    assert await scheduler.run_once() == 0, "intervalo 0 desliga"

    await app_settings.set(BACKUP_INTERVAL_HOURS, 24)
    cozinha_dev.files["cfg.json"] = b"lixo"
    assert await scheduler.run_once() == 2
    assert await scheduler.run_once() == 0, "Sala em dia; Cozinha em espera após a falha"
    # passada a espera, a Cozinha (que nunca teve backup verificado) é tentada de novo
    from wledadmin.services.backup_scheduler import RETRY_AFTER_FAILURE
    scheduler._failures[cozinha.id] -= RETRY_AFTER_FAILURE
    assert await scheduler.run_once() == 1


async def test_app_settings_validation(app_settings, session_factory):
    from wledadmin.services.app_settings import SettingsError

    with pytest.raises(SettingsError):
        await app_settings.set(BACKUP_KEEP, 0)
    await app_settings.set(BACKUP_INTERVAL_HOURS, 6)
    fresh = AppSettingsService(session_factory)
    await fresh.load()
    assert fresh.get(BACKUP_INTERVAL_HOURS) == 6 and fresh.get(BACKUP_KEEP) == 30
