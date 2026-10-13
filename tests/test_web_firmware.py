import pytest
from fastapi.testclient import TestClient

from wledadmin.main import create_app

from .conftest import ADMIN
from .fakes import INFO_014, INFO_16, FakeDevice, make_firmware, standard_github

H = {**ADMIN, "Origin": "http://wledadmin.test"}


@pytest.fixture
def github():
    return standard_github()


@pytest.fixture
def fw_client(settings, network, github):
    settings.ota_reboot_timeout = 2.0
    app = create_app(settings, start_background=False, wled_transport=network.transport(),
                     github_transport=github.transport())
    with TestClient(app, base_url="http://wledadmin.test") as tc:
        app.state.services.ota._reboot_initial_delay = 0
        app.state.services.ota._reboot_poll_interval = 0.01
        yield tc


def add(tc, network, fixture, ip, **kw):
    dev = network.add(ip, FakeDevice.from_fixture(fixture, **kw))
    r = tc.post("/api/devices", json={"host": ip}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return dev, r.json()["id"]


def wait_job(tc, job_id):
    for _ in range(300):
        job = tc.get(f"/api/jobs/{job_id}", headers=ADMIN).json()
        if job["status"] == "done":
            return job
    raise AssertionError("job não terminou")


def test_firmware_page_lists_stable_releases(fw_client):
    page = fw_client.get("/firmware", headers=ADMIN).text
    assert "16.0.1" in page and "0.15.4" in page
    assert "17.0.0" not in page and "nightly" not in page


def test_plan_page_and_confirmation(fw_client, network):
    dev, dev_id = add(fw_client, network, INFO_16, "192.168.1.52", info={"ver": "0.15.4"})
    page = fw_client.get(f"/firmware/plan?tag=v16.0.1&device_ids={dev_id}", headers=ADMIN)
    assert page.status_code == 200
    assert "WLED_16.0.1_ESP32-C3.bin" in page.text and "pronto" in page.text

    form = {"tag": "v16.0.1", "device_ids": str(dev_id), "confirm": "on",
            "confirm_text": "errado"}
    r = fw_client.post("/firmware/run", data=form, headers=H)
    assert r.status_code == 400 and "digite" in r.text
    assert dev.firmware_uploads == []

    form["confirm_text"] = "ATUALIZAR"
    r = fw_client.post("/firmware/run", data=form, headers=H, follow_redirects=False)
    assert r.status_code == 303
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    job = wait_job(fw_client, job_id)
    assert job["items"][0]["status"] == "ok", job
    assert dev.info["ver"] == "16.0.1"
    device_page = fw_client.get(f"/devices/{dev_id}", headers=ADMIN).text
    assert "Atualizações de firmware" in device_page and "WLED_16.0.1_ESP32-C3.bin" in device_page


def test_manual_choice_requires_device_confirm(fw_client, network):
    dev, dev_id = add(fw_client, network, INFO_014, "192.168.1.50")
    page = fw_client.get(f"/firmware/plan?tag=v0.15.4&device_ids={dev_id}", headers=ADMIN).text
    assert "escolher arquivo" in page and "WLED_0.15.4_ESP01.bin" in page

    base = {"tag": "v0.15.4", "device_ids": str(dev_id),
            f"manual_{dev_id}": "WLED_0.15.4_ESP8266.bin",
            "confirm": "on", "confirm_text": "atualizar"}
    replan = fw_client.post("/firmware/plan", data={**base, "pin": "1234"}, headers=H)
    assert replan.status_code == 200 and "Conferi que este arquivo" in replan.text
    assert "1234" not in replan.text, "o PIN nunca volta para a página"

    r = fw_client.post("/firmware/run", data=base, headers=H)
    assert r.status_code == 400 and "confirme o arquivo escolhido" in r.text
    r = fw_client.post("/firmware/run", data={**base, "confirm_device": str(dev_id)},
                       headers=H, follow_redirects=False)
    assert r.status_code == 303
    wait_job(fw_client, int(r.headers["location"].rsplit("/", 1)[1]))
    assert dev.firmware_uploads == ["WLED_0.15.4_ESP8266.bin"]


def test_custom_upload_flow(fw_client, network):
    dev, dev_id = add(fw_client, network, INFO_16, "192.168.1.52", info={"ver": "0.15.4"})
    data = make_firmware("esp32-c3", "ESP32-C3", "16.1.0")
    r = fw_client.post("/firmware/custom", files={"file": ("meu.bin", data)},
                       data={"device_ids": str(dev_id)}, headers=H, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/firmware/plan?custom=")
    page = fw_client.get(r.headers["location"], headers=ADMIN).text
    assert "Instalar meu.bin" in page and "Conferi que este arquivo" in page

    r = fw_client.post("/firmware/custom", files={"file": ("x.bin", b"lixo")},
                       data={"device_ids": str(dev_id)}, headers=H, follow_redirects=False)
    assert "recusado" in r.headers["location"]


def test_api_plan_and_run(fw_client, network):
    dev, dev_id = add(fw_client, network, INFO_16, "192.168.1.52", info={"ver": "0.15.4"})
    plan = fw_client.post("/api/firmware/plan", json={"tag": "v16.0.1", "device_ids": [dev_id]},
                          headers=ADMIN).json()
    assert plan["plans"][0]["status"] == "ready"
    assert plan["plans"][0]["path"] == "0.15.4 → 16.0.1"

    r = fw_client.post("/api/firmware/run", json={"tag": "v16.0.1", "device_ids": [dev_id]},
                       headers=ADMIN)
    assert r.status_code == 400
    r = fw_client.post("/api/firmware/run", headers=ADMIN, json={
        "tag": "v16.0.1", "device_ids": [dev_id], "confirm": True,
        "confirm_text": "atualizar"})
    assert r.status_code == 202
    assert wait_job(fw_client, r.json()["job_id"])["items"][0]["status"] == "ok"


def test_firmware_settings(fw_client):
    r = fw_client.post("/settings/firmware", headers=H,
                       data={"ota_repo": "wled/WLED", "ota_prereleases": "true"})
    assert r.status_code == 200 and "salvas" in r.text
    assert "17.0.0" in fw_client.get("/firmware", headers=ADMIN).text
    r = fw_client.post("/settings/firmware", headers=H, data={"ota_repo": "sem barra"})
    assert r.status_code == 400 and "dono/repositório" in r.text
