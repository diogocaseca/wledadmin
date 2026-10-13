import io
import json
import zipfile

from .conftest import ADMIN
from .fakes import INFO_014, INFO_015, FakeDevice, fixture_bytes, load_fixture

ORIGIN = {"Origin": "http://wledadmin.test"}
H = {**ADMIN, **ORIGIN}


def setup_two(app_client, network):
    sala = network.add("192.168.1.50", FakeDevice.from_fixture(INFO_014))
    cozinha = network.add("192.168.1.51", FakeDevice.from_fixture(INFO_015))
    for ip in ("192.168.1.50", "192.168.1.51"):
        assert app_client.post("/api/devices", json={"host": ip}, headers=ADMIN).status_code == 201
    ids = {d["name"]: d["id"] for d in app_client.get("/api/devices", headers=ADMIN).json()}
    return sala, cozinha, ids


def run_backup(app_client, ids):
    job = app_client.post("/api/backups", json={"device_ids": ids}, headers=ADMIN).json()
    for _ in range(200):
        job = app_client.get(f"/api/jobs/{job['id']}", headers=ADMIN).json()
        if job["status"] == "done":
            return job
    raise AssertionError("job não terminou")


def test_backup_via_api_and_device_page(app_client, network):
    _, _, ids = setup_two(app_client, network)
    job = run_backup(app_client, list(ids.values()))
    assert {it["status"] for it in job["items"]} == {"ok"}

    backups = app_client.get(f"/api/devices/{ids['Sala']}/backups", headers=ADMIN).json()
    assert sorted(b["kind"] for b in backups) == ["cfg", "presets"]

    page = app_client.get(f"/devices/{ids['Sala']}", headers=ADMIN).text
    assert "Histórico de backups" in page and "Configuração" in page and "copiar para" in page

    rows = app_client.get("/ui/devices/rows", headers=ADMIN).text
    assert "agora" in rows

    cfg = next(b for b in backups if b["kind"] == "cfg")
    r = app_client.get(f"/backups/{cfg['id']}/download", headers=ADMIN)
    assert r.headers["content-disposition"].startswith('attachment; filename="wled_Sala_cfg_')
    assert r.content == fixture_bytes("cfg.json")


def test_ui_backup_button_returns_job_fragment(app_client, network):
    _, _, ids = setup_two(app_client, network)
    r = app_client.post("/ui/backups", data={"device_ids": [ids["Sala"]]}, headers=H)
    assert "Backup manual" in r.text and "/ui/jobs/" in r.text
    r = app_client.post("/ui/backups", data={}, headers=H)
    assert "Selecione" in r.text


def test_import_and_diff_pages(app_client, network):
    _, _, ids = setup_two(app_client, network)
    run_backup(app_client, [ids["Sala"]])
    changed = load_fixture("presets.json")
    changed["1"]["bri"] = 10
    r = app_client.post(
        f"/devices/{ids['Sala']}/import",
        files={"file": ("presets.json", json.dumps(changed).encode(), "application/json")},
        data={"kind": "auto", "note": ""},
        headers=H,
        follow_redirects=False,
    )
    assert r.status_code == 303 and "importado" in r.headers["location"]

    backups = app_client.get(f"/api/devices/{ids['Sala']}/backups", headers=ADMIN).json()
    imported = next(b for b in backups if b["trigger"] == "import")
    assert imported["note"] == "presets.json"
    diff = app_client.get(f"/backups/{imported['id']}/diff", headers=ADMIN).text
    assert "d-del" in diff and "d-add" in diff and "&#34;bri&#34;: 10" in diff

    r = app_client.post(
        f"/devices/{ids['Sala']}/import",
        files={"file": ("x.json", b"not json", "application/json")},
        headers=H, follow_redirects=False,
    )
    assert "recusada" in r.headers["location"]


def test_restore_flow_presets(app_client, network):
    sala, _, ids = setup_two(app_client, network)
    run_backup(app_client, [ids["Sala"]])
    presets = next(b for b in app_client.get(f"/api/devices/{ids['Sala']}/backups",
                                             headers=ADMIN).json() if b["kind"] == "presets")
    sala.files["presets.json"] = b'{"0": {}, "7": {"n": "outro"}}'

    page = app_client.get(f"/backups/{presets['id']}/restore", headers=ADMIN).text
    assert "O que vai mudar" in page and "outro" in page

    r = app_client.post(f"/backups/{presets['id']}/restore",
                        data={"device_id": ids["Sala"]}, headers=H)
    assert r.status_code == 400  # sem confirmação

    r = app_client.post(f"/backups/{presets['id']}/restore",
                        data={"device_id": ids["Sala"], "confirm": "on"}, headers=H,
                        follow_redirects=False)
    assert r.status_code == 303
    job_url = r.headers["location"]
    for _ in range(200):
        page = app_client.get(job_url, headers=ADMIN).text
        if "concluído" in page:
            break
    assert "restaurado e verificado" in page and "Backup de segurança" in page
    assert json.loads(sala.files["presets.json"]) == load_fixture("presets.json")


def test_restore_cfg_to_other_device_blocked(app_client, network):
    _, cozinha, ids = setup_two(app_client, network)
    run_backup(app_client, [ids["Sala"]])
    cfg = next(b for b in app_client.get(f"/api/devices/{ids['Sala']}/backups",
                                         headers=ADMIN).json() if b["kind"] == "cfg")
    page = app_client.get(f"/backups/{cfg['id']}/restore?device_id={ids['Cozinha']}",
                          headers=ADMIN).text
    assert "Não é possível restaurar" in page
    r = app_client.post(f"/api/backups/{cfg['id']}/restore",
                        json={"device_id": ids["Cozinha"], "confirm": True}, headers=ADMIN)
    assert r.status_code == 409
    assert cozinha.uploads == []


def test_copy_presets_via_api(app_client, network):
    _, cozinha, ids = setup_two(app_client, network)
    cozinha.files["presets.json"] = b'{"0": {}}'
    run_backup(app_client, [ids["Sala"]])
    presets = next(b for b in app_client.get(f"/api/devices/{ids['Sala']}/backups",
                                             headers=ADMIN).json() if b["kind"] == "presets")
    assert app_client.get(f"/backups/{presets['id']}/copy", headers=ADMIN).status_code == 200
    r = app_client.post(f"/api/backups/{presets['id']}/copy",
                        json={"device_ids": [ids["Cozinha"]]}, headers=ADMIN)
    assert r.status_code == 400
    job = app_client.post(f"/api/backups/{presets['id']}/copy",
                          json={"device_ids": [ids["Cozinha"]], "confirm": True},
                          headers=ADMIN).json()
    assert job["title"] == "Copiar presets de Sala"
    for _ in range(200):
        job = app_client.get(f"/api/jobs/{job['id']}", headers=ADMIN).json()
        if job["status"] == "done":
            break
    assert job["items"][0]["status"] == "ok"
    assert json.loads(cozinha.files["presets.json"]) == load_fixture("presets.json")


def test_export_zip(app_client, network):
    _, _, ids = setup_two(app_client, network)
    run_backup(app_client, list(ids.values()))
    r = app_client.get("/backups/export.zip", headers=ADMIN)
    names = sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert names == ["Cozinha_240ac4aabb02/cfg.json", "Cozinha_240ac4aabb02/presets.json",
                     "Sala_a4cf12aabb01/cfg.json", "Sala_a4cf12aabb01/presets.json"]


def test_settings_backup_form(app_client):
    r = app_client.post("/settings/backup",
                        data={"backup_interval_hours": 6, "backup_keep": 10}, headers=H)
    assert r.status_code == 200 and "salvas" in r.text
    page = app_client.get("/settings", headers=ADMIN).text
    assert 'value="6"' in page and 'value="10"' in page
    r = app_client.post("/settings/backup",
                        data={"backup_interval_hours": 6, "backup_keep": 0}, headers=H)
    assert r.status_code == 400 and "entre 1 e 1000" in r.text
