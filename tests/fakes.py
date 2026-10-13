"""Rede WLED simulada, plugada no httpx como transporte (sem rede real).

Reproduz o comportamento conferido no código do WLED (wled_server.cpp / file.cpp):
- GET /cfg.json e /presets.json servidos do "FS" sem PIN; caminhos com "sec" nunca.
- POST /upload exige correctPIN (global no dispositivo); upload de cfg.json reinicia.
- {"pin": "..."} num POST JSON desbloqueia; GET /settings/lock bloqueia de novo.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


INFO_014 = "info_0_14_esp8266.json"
INFO_015 = "info_0_15_esp32.json"
INFO_16 = "info_16_esp32c3.json"


def _default_files() -> dict[str, bytes]:
    return {"cfg.json": fixture_bytes("cfg.json"), "presets.json": fixture_bytes("presets.json")}


@dataclass
class FakeDevice:
    info: dict[str, Any]
    state: dict[str, Any] = field(default_factory=lambda: load_fixture("state.json"))
    offline: bool = False
    timeout: bool = False
    has_si: bool = True
    # True: devolve {"success": true} mesmo com "v": true (firmwares muito antigos)
    terse_post: bool = False
    raw_body: bytes | None = None  # força uma resposta não-JSON
    status_code: int = 200
    posts: list[dict[str, Any]] = field(default_factory=list)

    # Sistema de arquivos e restauração
    files: dict[str, bytes] = field(default_factory=_default_files)
    pin: str | None = None
    unlocked: bool | None = None  # correctPIN; None = derivado de `pin` na criação
    uploads: list[tuple[str, bytes]] = field(default_factory=list)
    # Requisições que falham enquanto o dispositivo reinicia (após upload de cfg.json)
    reboot_requests: int = 2
    never_returns_after_reboot: bool = False
    upload_fails: bool = False
    rebooting: int = 0

    def __post_init__(self) -> None:
        if self.unlocked is None:
            self.unlocked = self.pin is None

    @classmethod
    def from_fixture(cls, name: str, **overrides: Any) -> FakeDevice:
        info = load_fixture(name)
        info.update(overrides.pop("info", {}))
        return cls(info=info, **overrides)

    def reboot(self) -> None:
        self.rebooting = self.reboot_requests
        self.unlocked = self.pin is None
        self.info["uptime"] = 2


class FakeWLEDNetwork:
    """Mapeia host -> FakeDevice. Hosts não cadastrados recusam conexão."""

    def __init__(self) -> None:
        self.devices: dict[str, FakeDevice] = {}
        self.requests: list[httpx.Request] = []

    def add(self, host: str, device: FakeDevice) -> FakeDevice:
        self.devices[host] = device
        return device

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        dev = self.devices.get(host)
        if dev is None or dev.offline:
            raise httpx.ConnectError("connection refused", request=request)
        if dev.rebooting > 0 or (dev.never_returns_after_reboot and dev.uploads):
            dev.rebooting = max(0, dev.rebooting - 1)
            raise httpx.ConnectError("rebooting", request=request)
        if dev.timeout:
            raise httpx.ConnectTimeout("timed out", request=request)
        if dev.raw_body is not None:
            return httpx.Response(dev.status_code, content=dev.raw_body)
        if dev.status_code != 200:
            return httpx.Response(dev.status_code, text="error")

        path = request.url.path
        if request.method == "GET":
            if path == "/json/info":
                return httpx.Response(200, json=dev.info)
            if path == "/json/state":
                return httpx.Response(200, json=dev.state)
            if path == "/json/si" and dev.has_si:
                return httpx.Response(200, json={"state": dev.state, "info": dev.info})
            if path == "/settings/lock":
                dev.unlocked = dev.pin is None
                return httpx.Response(200, text="<html>Settings locked</html>")
            name = path.lstrip("/")
            if "sec" not in name and name in dev.files:
                return httpx.Response(200, content=dev.files[name],
                                      headers={"content-type": "application/json"})
            return httpx.Response(404, text="<html>Not Found</html>")

        if request.method == "POST" and path == "/upload":
            return self._upload(dev, request)

        if request.method == "POST" and path == "/json/state":
            body = json.loads(request.content)
            dev.posts.append(body)
            if "pin" in body:
                if dev.pin is not None and body["pin"] == dev.pin:
                    dev.unlocked = True
            for k, v in body.items():
                if k in ("v", "pin"):
                    continue
                if k == "on" and v == "t":
                    dev.state["on"] = not dev.state["on"]
                else:
                    dev.state[k] = v
            if dev.terse_post or not body.get("v"):
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json=copy.deepcopy(dev.state))
        return httpx.Response(405)

    def _upload(self, dev: FakeDevice, request: httpx.Request) -> httpx.Response:
        if not dev.unlocked:
            return httpx.Response(401, text="Please unlock settings using PIN code!")
        if dev.upload_fails:
            raise httpx.ReadError("connection reset during upload", request=request)
        ctype = request.headers["content-type"].encode()
        msg = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + ctype + b"\r\n\r\n" + request.read()
        )
        part = next(msg.iter_parts())
        filename = part.get_filename() or ""
        content = part.get_payload(decode=True)
        name = filename.lstrip("/")
        dev.files[name] = content
        dev.uploads.append((name, content))
        if "cfg.json" in filename:
            dev.reboot()
            return httpx.Response(200, text="Config restore ok.\nRebooting...")
        return httpx.Response(200, text="File Uploaded!")
