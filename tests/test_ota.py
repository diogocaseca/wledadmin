import pytest

from wledadmin.services.app_settings import AppSettingsService
from wledadmin.services.backup import BackupService, BackupTrigger
from wledadmin.services.jobs import ItemStatus, JobManager
from wledadmin.services.ota import OtaService, PlanStatus
from wledadmin.services.releases import ReleaseService
from wledadmin.wled import DeviceInfo

from .fakes import INFO_014, INFO_015, INFO_16, FakeDevice, make_firmware, standard_github


@pytest.fixture
def github():
    return standard_github()


@pytest.fixture
async def ota(session_factory, client, registry, store, settings, github):
    settings.ota_reboot_timeout = 2.0
    app_settings = AppSettingsService(session_factory)
    await app_settings.load()
    backups = BackupService(session_factory, client, registry, store, app_settings,
                            reboot_initial_delay=0, reboot_poll_interval=0.01)
    releases = ReleaseService(settings.data_dir, api_url="https://api.github.com", token=None,
                              transport=github.transport())
    svc = OtaService(session_factory, client, registry, store, backups, releases,
                     app_settings, JobManager(), settings,
                     reboot_initial_delay=0, reboot_poll_interval=0.01,
                     lock_poll_interval=0.01, lock_timeout=0.2, preset_settle=0)
    yield svc
    await releases.close()


async def add(network, registry, store, fixture, ip, **kw):
    dev = network.add(ip, FakeDevice.from_fixture(fixture, **kw))
    info = DeviceInfo.model_validate(dev.info)
    device = await registry.upsert_from_info(info, ip, "manual")
    store.record_success(device.id, info, None)
    return dev, device


async def release(ota, tag):
    return await ota._releases.get_release(ota.repo, tag)


async def run_and_wait(ota, plans, pin=None, stop=True):
    job = ota.run(plans, pin=pin, stop_on_failure=stop, title="teste")
    await ota._jobs.wait(job)
    return job


# ---- planejamento -------------------------------------------------------------------


async def test_auto_plan_and_run(ota, network, registry, store):
    # ESP32 já na 0.15.4: vai direto para a 16.0.1
    dev, device = await add(network, registry, store, INFO_015, "10.0.0.2",
                            info={"ver": "0.15.4"})
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    p = plans[0]
    assert p.status is PlanStatus.READY and not p.needs_device_confirm
    assert [s.file_name for s in p.steps] == ["WLED_16.0.1_ESP32.bin"]

    job = await run_and_wait(ota, plans)
    item = job.items[0]
    assert item.status is ItemStatus.OK, item.message
    assert item.message == "atualizado para 16.0.1"
    assert dev.info["ver"] == "16.0.1" and dev.firmware_uploads == ["WLED_16.0.1_ESP32.bin"]
    history = await ota.history(device.mac)
    assert [(h.from_version, h.to_version, h.status) for h in history] == [
        ("0.15.4", "16.0.1", "ok")]
    triggers = {b.trigger for b in await ota._backups.list_for_mac(device.mac)}
    assert triggers == {"pre_ota"}


async def test_two_step_for_old_esp32(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_015, "10.0.0.2")  # 0.15.0 ESP32
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    p = plans[0]
    assert p.status is PlanStatus.READY
    assert [s.file_name for s in p.steps] == ["WLED_0.15.4_ESP32.bin", "WLED_16.0.1_ESP32.bin"]
    assert p.path == "0.15.0 → 0.15.4 → 16.0.1"
    assert any("duas etapas" in w for w in p.warnings)

    job = await run_and_wait(ota, plans)
    assert job.items[0].status is ItemStatus.OK, job.items[0].message
    assert dev.firmware_uploads == ["WLED_0.15.4_ESP32.bin", "WLED_16.0.1_ESP32.bin"]
    assert any(s.startswith("[2/2]") for s in job.items[0].steps)
    assert len(await ota.history(device.mac)) == 2


async def test_two_step_impossible_blocks(ota, network, registry, store, github):
    github.releases = [r for r in github.releases if r["tag_name"] != "v0.15.4"]
    _, device = await add(network, registry, store, INFO_015, "10.0.0.2")
    p = (await ota.plan_release([device], await release(ota, "v16.0.1")))[0]
    assert p.status is PlanStatus.BLOCKED and "intermediária" in p.blockers[0]


async def test_esp32_c3_single_step(ota, network, registry, store):
    _, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                          info={"ver": "0.15.4"})
    p = (await ota.plan_release([device], await release(ota, "v16.0.1")))[0]
    assert p.status is PlanStatus.READY and len(p.steps) == 1


async def test_014_device_needs_manual_choice(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_014, "10.0.0.1")  # sem release
    r = await release(ota, "v0.15.4")
    p = (await ota.plan_release([device], r))[0]
    assert p.status is PlanStatus.MANUAL
    assert {a.name for a in p.candidates} == {
        "WLED_0.15.4_ESP8266.bin", "WLED_0.15.4_ESP01.bin", "WLED_0.15.4_ESP02.bin",
        "WLED_0.15.4_ESP02.bin.gz"}

    wrong = (await ota.plan_release([device], r,
                                    manual={device.id: "WLED_0.15.4_ESP32.bin"}))[0]
    assert wrong.status is PlanStatus.BLOCKED

    ok = (await ota.plan_release([device], r,
                                 manual={device.id: "WLED_0.15.4_ESP8266.bin"}))[0]
    assert ok.status is PlanStatus.READY and ok.needs_device_confirm
    job = await run_and_wait(ota, [ok])
    assert job.items[0].status is ItemStatus.OK, job.items[0].message
    assert dev.info["ver"] == "0.15.4"


async def test_downgrade_current_and_reinstall(ota, network, registry, store):
    _, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                          info={"ver": "16.0.1"})
    old = (await ota.plan_release([device], await release(ota, "v0.15.4")))[0]
    assert old.status is PlanStatus.BLOCKED and "downgrade" in old.blockers[0]
    same = (await ota.plan_release([device], await release(ota, "v16.0.1")))[0]
    assert same.status is PlanStatus.CURRENT
    again = (await ota.plan_release([device], await release(ota, "v16.0.1"),
                                    reinstall={device.id}))[0]
    assert again.status is PlanStatus.READY and again.reinstall


async def test_blockers_offline_lock_repo(ota, network, registry, store):
    _, off = await add(network, registry, store, INFO_015, "10.0.0.2", info={"ver": "0.15.4"})
    store.record_failure(off.id, "x")
    store.record_failure(off.id, "x")
    _, locked = await add(network, registry, store, INFO_16, "10.0.0.3",
                          info={"ver": "0.15.4"}, ota_lock=True)
    _, fork = await add(network, registry, store, INFO_16, "10.0.0.4",
                        info={"ver": "0.15.4", "repo": "MoonModules/WLED-MM",
                              "mac": "34b4720c0d99"})
    plans = await ota.plan_release([off, locked, fork], await release(ota, "v16.0.1"))
    assert [p.status for p in plans] == [PlanStatus.BLOCKED, PlanStatus.READY,
                                         PlanStatus.BLOCKED]
    assert "offline" in plans[0].blockers[0]
    assert plans[1].ota_locked and any("trava de OTA" in w for w in plans[1].warnings)
    assert "outro repositório" in plans[2].blockers[0]
    job = await run_and_wait(ota, [plans[0], plans[2]])
    assert job.items == [], "nada bloqueado entra no job"


async def test_custom_bin_warns_instead_of_blocking(ota, network, registry, store):
    _, device = await add(network, registry, store, INFO_015, "10.0.0.2")  # 0.15.0 ESP32
    digest = ota._releases.store_custom(
        make_firmware("esp32", "Custom", "16.1.0", safe=(0, 15, 3)))
    p = (await ota.plan_custom([device], digest, "meu.bin"))[0]
    assert p.status is PlanStatus.READY and p.needs_device_confirm
    assert any("0.15.3" in w for w in p.warnings)
    assert any("Custom" in w for w in p.warnings)

    wrong = ota._releases.store_custom(make_firmware("esp32-c3", "ESP32-C3"))
    p = (await ota.plan_custom([device], wrong, "c3.bin"))[0]
    assert p.status is PlanStatus.BLOCKED


# ---- execução: falhas ---------------------------------------------------------------


async def test_pin_flow(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                            info={"ver": "0.15.4"}, pin="1234")
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    job = await run_and_wait(ota, plans)
    assert job.items[0].status is ItemStatus.FAILED and "PIN" in job.items[0].message
    job = await run_and_wait(ota, plans, pin="1234")
    assert job.items[0].status is ItemStatus.OK, job.items[0].message


async def test_stop_on_failure_cancels_rest(ota, network, registry, store):
    bad, d1 = await add(network, registry, store, INFO_015, "10.0.0.2",
                        info={"ver": "0.15.4", "name": "A"}, ota_fails=True)
    good, d2 = await add(network, registry, store, INFO_16, "10.0.0.3",
                         info={"ver": "0.15.4", "name": "B"})
    plans = await ota.plan_release([d1, d2], await release(ota, "v16.0.1"))
    job = await run_and_wait(ota, plans, stop=True)
    assert [i.status for i in job.items] == [ItemStatus.FAILED, ItemStatus.SKIPPED]
    assert "recusou" in job.items[0].message and good.firmware_uploads == []
    assert (await ota.history(d1.mac))[0].status == "failed"

    job = await run_and_wait(ota, plans, stop=False)
    assert [i.status for i in job.items] == [ItemStatus.FAILED, ItemStatus.OK]


async def test_version_not_changed_is_failure(ota, network, registry, store):
    _, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                          info={"ver": "0.15.4"}, ota_keeps_version=True)
    job = await run_and_wait(ota, await ota.plan_release([device],
                                                         await release(ota, "v16.0.1")))
    assert job.items[0].status is ItemStatus.FAILED
    assert "continua na 0.15.4" in job.items[0].message


async def test_never_returns(ota, network, registry, store):
    _, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                          info={"ver": "0.15.4"}, reboot_requests=10_000)
    job = await run_and_wait(ota, await ota.plan_release([device],
                                                         await release(ota, "v16.0.1")))
    assert job.items[0].status is ItemStatus.FAILED and "WLED-AP" in job.items[0].message


async def test_backup_failure_prevents_update(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                            info={"ver": "0.15.4"})
    dev.files["cfg.json"] = b"not json"
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    dev.files["cfg.json"] = b"still not json"
    job = await run_and_wait(ota, plans)
    assert job.items[0].status is ItemStatus.FAILED and "backup falhou" in job.items[0].message
    assert dev.firmware_uploads == []


async def test_device_side_release_validation(ota, network, registry, store):
    """Escolha manual de outro release num 16.x: o próprio dispositivo recusa."""
    dev, device = await add(network, registry, store, INFO_015, "10.0.0.2",
                            info={"ver": "16.0.1"}, validates_release=True)
    r = await release(ota, "v16.0.1")
    p = (await ota.plan_release([device], r, reinstall={device.id},
                                manual={device.id: "WLED_16.0.1_ESP32_Ethernet.bin"}))[0]
    assert p.status is PlanStatus.READY and any("Ethernet" in w for w in p.warnings)
    job = await run_and_wait(ota, [p])
    assert job.items[0].status is ItemStatus.FAILED and "mismatch" in job.items[0].message


# ---- backup opcional e recuperação ---------------------------------------------------


async def test_backup_can_be_skipped(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                            info={"ver": "0.15.4"})
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    job = ota.run(plans, pin=None, stop_on_failure=True, title="t", backup=False)
    await ota._jobs.wait(job)
    assert job.kind == "ota"
    assert job.items[0].status is ItemStatus.OK
    assert job.items[0].steps[0] == "Backup pulado (desativado na confirmação)"
    assert await ota._backups.list_for_mac(device.mac) == []


async def test_diagnose_offline(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_16, "10.0.0.3")
    dev.offline = True
    report = await ota.diagnose(device)
    assert not report.online and "sem conexão" in report.error and report.files == []


async def test_diagnose_detects_factory_reset_config(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                            info={"ver": "0.15.4"})
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    await run_and_wait(ota, plans)  # sucesso, gera o backup pre_ota
    # Simula um dispositivo que voltou com cfg de fábrica e sem presets
    dev.files["cfg.json"] = b'{"rev": [1, 0], "id": {"name": "WLED"}}'
    del dev.files["presets.json"]
    store.record_failure(device.id, "x")
    store.record_failure(device.id, "x")

    report = await ota.diagnose(device)
    assert report.online and report.version == "16.0.1" and report.needs_restore
    by_kind = {f.kind.value: f for f in report.files}
    assert by_kind["cfg"].status == "different" and by_kind["cfg"].backup.trigger == "pre_ota"
    assert by_kind["presets"].status == "missing"
    assert store.get(device.id).online is True, "diagnóstico atualiza o status"


async def test_diagnose_intact_and_without_backup(ota, network, registry, store):
    _, device = await add(network, registry, store, INFO_16, "10.0.0.3")
    report = await ota.diagnose(device)
    assert [f.status for f in report.files] == ["no_backup", "no_backup"]
    assert not report.needs_restore
    await ota._backups.backup_device(device, BackupTrigger.MANUAL)
    report = await ota.diagnose(device)
    assert [f.status for f in report.files] == ["same", "same"] and not report.needs_restore


# ---- trava de OTA --------------------------------------------------------------------


async def _locked(network, registry, store, **kw):
    return await add(network, registry, store, INFO_16, "10.0.0.3",
                     info={"ver": "0.15.4"}, ota_lock=True, **kw)


async def run_locked(ota, plans, pin=None, ota_password=None):
    job = ota.run(plans, pin=pin, stop_on_failure=True, title="t", ota_password=ota_password)
    await ota._jobs.wait(job)
    return job.items[0]


async def test_ota_lock_unlocked_and_relocked(ota, network, registry, store):
    dev, device = await _locked(network, registry, store, pin="1234")
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    item = await run_locked(ota, plans, pin="1234", ota_password="wledota")
    assert item.status is ItemStatus.OK, item.message
    assert dev.info["ver"] == "16.0.1" and dev.ota_lock is True
    assert "Trava de OTA desligada" in item.steps and "Trava de OTA religada" in item.steps
    relock = [p for p in dev.posts if p.get("ota", {}).get("lock") is True][-1]
    assert "psk" not in relock["ota"], "religar não reenvia a senha"


async def test_ota_lock_without_or_wrong_password(ota, network, registry, store):
    dev, device = await _locked(network, registry, store)
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    item = await run_locked(ota, plans)
    assert item.status is ItemStatus.FAILED and "informe a senha de OTA" in item.message
    item = await run_locked(ota, plans, ota_password="errada")
    assert item.status is ItemStatus.FAILED and "senha de OTA incorreta" in item.message
    assert dev.firmware_uploads == [] and dev.ota_lock is True


async def test_ota_lock_needs_pin_for_cfg(ota, network, registry, store):
    dev, device = await _locked(network, registry, store, pin="1234")
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    item = await run_locked(ota, plans, ota_password="wledota")
    assert item.status is ItemStatus.FAILED and "PIN" in item.message
    assert dev.firmware_uploads == []


async def test_ota_lock_two_steps_unlock_once(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_015, "10.0.0.2", ota_lock=True)
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    assert len(plans[0].steps) == 2
    item = await run_locked(ota, plans, ota_password="wledota")
    assert item.status is ItemStatus.OK, item.message
    assert item.steps.count("Trava de OTA desligada") == 1
    assert item.steps.count("Trava de OTA religada") == 1
    assert dev.ota_lock is True and dev.info["ver"] == "16.0.1"


async def test_ota_lock_relocked_after_failed_upload(ota, network, registry, store):
    dev, device = await _locked(network, registry, store, ota_fails=True)
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    item = await run_locked(ota, plans, ota_password="wledota")
    assert item.status is ItemStatus.FAILED and "recusou" in item.message
    assert dev.ota_lock is True and "Trava de OTA religada" in item.steps


async def test_ota_lock_left_open_when_device_disappears(ota, network, registry, store):
    dev, device = await _locked(network, registry, store, reboot_requests=10_000)
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    item = await run_locked(ota, plans, ota_password="wledota")
    assert item.status is ItemStatus.FAILED and "WLED-AP" in item.message
    assert item.data.get("ota_relock_pending") is True and dev.ota_lock is False
    # o dispositivo volta: religar pela recuperação
    dev.rebooting = 0
    dev.reboot_requests = 0
    assert await ota.relock_ota(device, None, item) is True
    assert dev.ota_lock is True and "ota_relock_pending" not in item.data


# ---- luzes apagadas durante a atualização ---------------------------------------------


async def test_lights_off_fixes_lit_bulb_and_restores_state(ota, network, registry, store):
    """O caso da lâmpada: com a luz acesa o firmware novo era descartado."""
    dev, device = await add(network, registry, store, INFO_014, "10.0.0.1",
                            info={"ver": "0.15.4", "release": "ESP8266"},
                            ota_fails_when_lit=True,
                            boot_state={"on": True, "bri": 255, "ps": -1})
    dev.state.update({"on": True, "bri": 90, "ps": 3})
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))

    lit = ota.run(plans, pin=None, stop_on_failure=True, title="t", lights_off=False)
    await ota._jobs.wait(lit)
    assert lit.items[0].status is ItemStatus.FAILED
    assert "continua na 0.15.4" in lit.items[0].message

    # o reboot da tentativa anterior aplicou o estado de boot; o usuário volta ao preset 3
    dev.state.update({"on": True, "bri": 90, "ps": 3})
    job = ota.run(plans, pin=None, stop_on_failure=True, title="t")  # padrão: apaga
    await ota._jobs.wait(job)
    item = job.items[0]
    assert item.status is ItemStatus.OK, item.message
    assert dev.info["ver"] == "16.0.1"
    assert "Apagando as luzes durante a atualização" in item.steps
    assert "Luzes acesas de novo (preset 3)" in item.steps
    # depois do boot (255, sem preset) volta ao que era: preset 3 e brilho 90
    assert dev.state["on"] is True and dev.state["bri"] == 90 and dev.state["ps"] == 3
    off = [p for p in dev.posts if p.get("on") is False]
    assert off and off[0].get("tt") == 0


async def test_lights_stay_off_if_they_were_off(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                            info={"ver": "0.15.4"}, boot_state={"on": True, "bri": 128})
    dev.state["on"] = False
    job = await run_and_wait(ota, await ota.plan_release([device],
                                                         await release(ota, "v16.0.1")))
    item = job.items[0]
    assert item.status is ItemStatus.OK
    assert "As luzes já estavam apagadas" in item.steps
    assert dev.state["on"] is False, "o boot acendeu, mas devolvemos apagado"


async def test_lights_restored_after_failure(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                            info={"ver": "0.15.4"}, ota_fails=True)
    dev.state.update({"on": True, "bri": 77, "ps": -1})
    job = await run_and_wait(ota, await ota.plan_release([device],
                                                         await release(ota, "v16.0.1")))
    item = job.items[0]
    assert item.status is ItemStatus.FAILED and "recusou" in item.message
    assert dev.state["on"] is True and dev.state["bri"] == 77
    assert "Luzes acesas de novo" in item.steps


async def test_lights_off_failure_aborts_before_upload(ota, network, registry, store):
    dev, device = await add(network, registry, store, INFO_16, "10.0.0.3",
                            info={"ver": "0.15.4"})
    plans = await ota.plan_release([device], await release(ota, "v16.0.1"))
    dev.offline = True
    job = await run_and_wait(ota, plans)
    assert job.items[0].status is ItemStatus.FAILED
    assert dev.firmware_uploads == []
