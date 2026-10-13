"""Ingress do Home Assistant: prefixo de URL, login dispensado só pelo proxy do HA."""

import pytest
from fastapi.testclient import TestClient

from wledadmin.main import create_app

from .conftest import ADMIN
from .fakes import INFO_014, FakeDevice, standard_github

HA_IP = "172.30.32.2"
PREFIX = "/api/hassio_ingress/AbC123_-x"
INGRESS = {"X-Ingress-Path": PREFIX}


def make_client(settings, network, *, enabled=True, ip=HA_IP):
    settings.ingress = enabled
    app = create_app(settings, start_background=False, wled_transport=network.transport(),
                     github_transport=standard_github().transport())
    return TestClient(app, base_url="http://homeassistant.local:8123", client=(ip, 50000))


@pytest.fixture
def ha(settings, network):
    with make_client(settings, network) as tc:
        yield tc


def test_ingress_needs_no_login_and_prefixes_urls(ha):
    r = ha.get("/", headers=INGRESS)
    assert r.status_code == 200
    assert "Home Assistant" in r.text  # usuário exibido
    assert f'href="{PREFIX}/firmware"' in r.text
    assert f'hx-get="{PREFIX}/ui/devices/rows"' in r.text
    # relativas ao host do navegador: o host interno que o proxy usa não é acessível
    assert f'href="{PREFIX}/static/app.css"' in r.text
    assert f'src="{PREFIX}/static/htmx.min.js"' in r.text
    assert 'href="/firmware"' not in r.text
    assert ha.get("/static/app.css", headers=INGRESS).status_code == 200


def test_ingress_post_and_redirects(ha, network):
    network.add("192.168.1.50", FakeDevice.from_fixture(INFO_014))
    r = ha.post("/ui/devices", data={"host": "192.168.1.50"},
                headers={**INGRESS, "Origin": "http://homeassistant.local:8123"})
    assert r.status_code == 200 and "adicionado" in r.text
    r = ha.post("/devices/1/import", headers=INGRESS, follow_redirects=False,
                files={"file": ("x.json", b"lixo")})
    assert r.status_code == 303 and r.headers["location"].startswith(f"{PREFIX}/devices/1?")


def test_direct_access_on_same_server_still_requires_login(settings, network):
    with make_client(settings, network, ip="192.168.1.10") as tc:
        assert tc.get("/").status_code == 401
        # cabeçalho forjado por quem não é o proxy do HA: ignorado
        r = tc.get("/", headers=INGRESS)
        assert r.status_code == 401
        r = tc.get("/", headers={**INGRESS, **ADMIN})
        assert r.status_code == 200 and PREFIX not in r.text


def test_proxy_ip_without_header_requires_login(ha):
    assert ha.get("/").status_code == 401


def test_invalid_prefix_is_not_trusted(ha):
    assert ha.get("/", headers={"X-Ingress-Path": "/evil/../path"}).status_code == 401


def test_ingress_disabled(settings, network):
    with make_client(settings, network, enabled=False) as tc:
        r = tc.get("/", headers=INGRESS)
        assert r.status_code == 403 and "desativado" in r.text and "8000" in r.text
        # o acesso direto continua funcionando
        assert tc.get("/", headers=ADMIN).status_code == 200


def test_no_unprefixed_internal_urls_in_pages(ha, network):
    import re

    network.add("192.168.1.50", FakeDevice.from_fixture(INFO_014))
    ha.post("/api/devices", json={"host": "192.168.1.50"}, headers=INGRESS)
    ha.post("/api/backups", json={"device_ids": [1]}, headers=INGRESS)
    attr = re.compile(r'(?:href|action|hx-get|hx-post|hx-delete|formaction|src)="([^"#]*)"')
    for page in ["/", "/settings", "/devices/1", "/firmware", "/ui/devices/rows"]:
        r = ha.get(page, headers=INGRESS)
        assert r.status_code == 200, page
        external = ("https://", "javascript:", "mailto:")
        bad = [u for u in attr.findall(r.text)
               if u and not u.startswith(PREFIX) and not u.startswith(external)
               and not re.fullmatch(r"http://[\d.]+/", u)]  # link para a UI do WLED
        assert bad == [], f"{page}: {bad}"


def test_ingress_can_reset_direct_access_password(ha, settings, network):
    page = ha.get("/settings", headers=INGRESS).text
    assert 'name="current_password"' not in page
    r = ha.post("/settings/account", headers=INGRESS,
                data={"new_username": "diogo", "new_password": "s3nh4-forte",
                      "confirm_password": "s3nh4-forte"})
    assert r.status_code == 200 and "Credenciais alteradas" in r.text


def test_direct_access_still_requires_current_password(app_client):
    r = app_client.post("/settings/account", headers=ADMIN,
                        data={"new_username": "x", "new_password": "s3nh4-forte",
                              "confirm_password": "s3nh4-forte"})
    assert r.status_code == 400 and "senha atual incorreta" in r.text
