from .conftest import ADMIN, basic
from .fakes import INFO_014, INFO_015, FakeDevice

ORIGIN = {"Origin": "http://wledadmin.test"}


def test_healthz_is_public(app_client):
    assert app_client.get("/healthz").json() == {"status": "ok"}


def test_auth_required_everywhere(app_client):
    for path in ["/", "/api/devices", "/settings", "/ui/devices/rows"]:
        r = app_client.get(path)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"].startswith("Basic")
    assert app_client.get("/", headers=basic("admin", "x")).status_code == 401


def test_index_shows_default_password_banner(app_client):
    r = app_client.get("/", headers=ADMIN)
    assert r.status_code == 200
    assert "login padrão" in r.text


def test_add_device_via_api_and_list(app_client, network):
    network.add("192.168.1.50", FakeDevice.from_fixture(INFO_014))
    r = app_client.post("/api/devices", json={"host": "192.168.1.50"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    devices = app_client.get("/api/devices", headers=ADMIN).json()
    assert len(devices) == 1
    d = devices[0]
    assert d["name"] == "Sala" and d["online"] is True and d["arch"] == "esp8266"
    assert d["rssi"] == -61 and d["led_count"] == 60 and d["bri_percent"] == 50


def test_add_device_errors(app_client, network):
    r = app_client.post("/api/devices", json={"host": "192.168.1.99"}, headers=ADMIN)
    assert r.status_code == 422 and "não foi possível" in r.json()["detail"]
    r = app_client.post("/api/devices", json={"host": "not a host!"}, headers=ADMIN)
    assert r.status_code == 422


def test_ui_flow_add_rows_action_delete(app_client, network):
    dev = network.add("192.168.1.51", FakeDevice.from_fixture(INFO_015))
    r = app_client.post("/ui/devices", data={"host": "192.168.1.51"}, headers={**ADMIN, **ORIGIN})
    assert "adicionado" in r.text and r.headers["hx-trigger"] == "devices-changed"

    device_id = app_client.get("/api/devices", headers=ADMIN).json()[0]["id"]
    rows = app_client.get(f"/ui/devices/rows?device_ids={device_id}", headers=ADMIN).text
    assert "Cozinha" in rows and "24:0A:C4:AA:BB:02" in rows and "checked" in rows

    r = app_client.post(
        "/ui/actions",
        data={"kind": "brightness", "device_ids": [device_id], "bri_value": 255},
        headers={**ADMIN, **ORIGIN},
    )
    assert "1 de 1" in r.text
    assert dev.posts[-1] == {"bri": 255, "v": True}

    r = app_client.post("/ui/actions", data={"kind": "preset", "device_ids": [device_id],
                                             "preset_value": 999}, headers=ADMIN)
    assert "entre 1 e 250" in r.text

    r = app_client.delete(f"/ui/devices/{device_id}", headers=ADMIN)
    assert r.status_code == 200
    assert app_client.get("/api/devices", headers=ADMIN).json() == []


def test_api_action(app_client, network):
    dev = network.add("192.168.1.50", FakeDevice.from_fixture(INFO_014))
    app_client.post("/api/devices", json={"host": "192.168.1.50"}, headers=ADMIN)
    r = app_client.post("/api/actions", json={"device_ids": [1], "action": {"kind": "off"}},
                        headers=ADMIN)
    assert r.json() == [{"device_id": 1, "name": "Sala", "ok": True, "error": None}]
    assert dev.state["on"] is False


def test_cross_origin_post_is_blocked(app_client):
    r = app_client.post("/api/poll", headers={**ADMIN, "Origin": "http://evil.example"})
    assert r.status_code == 403
    r = app_client.post("/api/poll", headers={**ADMIN, **ORIGIN})
    assert r.status_code == 202
    r = app_client.post("/api/poll", headers=ADMIN)  # curl/script sem Origin
    assert r.status_code == 202


def test_scan_via_api(app_client, network):
    network.add("192.168.1.50", FakeDevice.from_fixture(INFO_014))
    r = app_client.post("/api/scan", headers=ADMIN)
    assert r.status_code == 202 and r.json()["total"] == 6
    for _ in range(100):
        status = app_client.get("/api/scan", headers=ADMIN).json()
        if status["status"] != "running":
            break
    assert status["found"] == ["192.168.1.50"]


def test_change_credentials_via_ui(app_client):
    form = {"current_password": "admin", "new_username": "diogo",
            "new_password": "s3nh4-forte", "confirm_password": "s3nh4-forte"}
    r = app_client.post("/settings/account", data=form, headers={**ADMIN, **ORIGIN})
    assert r.status_code == 200 and "Credenciais alteradas" in r.text
    assert app_client.get("/", headers=ADMIN).status_code == 401
    r = app_client.get("/", headers=basic("diogo", "s3nh4-forte"))
    assert r.status_code == 200 and "login padrão" not in r.text


def test_change_credentials_mismatch(app_client):
    form = {"current_password": "admin", "new_username": "admin",
            "new_password": "s3nh4-forte", "confirm_password": "outra-coisa"}
    r = app_client.post("/settings/account", data=form, headers=ADMIN)
    assert r.status_code == 400 and "não confere" in r.text
