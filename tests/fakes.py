"""Rede WLED simulada, plugada no httpx como transporte (sem rede real)."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


INFO_014 = "info_0_14_esp8266.json"
INFO_015 = "info_0_15_esp32.json"
INFO_16 = "info_16_esp32c3.json"


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

    @classmethod
    def from_fixture(cls, name: str, **overrides: Any) -> FakeDevice:
        info = load_fixture(name)
        info.update(overrides.pop("info", {}))
        return cls(info=info, **overrides)


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
            return httpx.Response(404, text="Not Found")

        if request.method == "POST" and path == "/json/state":
            body = json.loads(request.content)
            dev.posts.append(body)
            for k, v in body.items():
                if k == "v":
                    continue
                if k == "on" and v == "t":
                    dev.state["on"] = not dev.state["on"]
                else:
                    dev.state[k] = v
            if dev.terse_post or not body.get("v"):
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json=copy.deepcopy(dev.state))
        return httpx.Response(405)
