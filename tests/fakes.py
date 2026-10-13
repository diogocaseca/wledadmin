"""Rede WLED simulada, plugada no httpx como transporte (sem rede real).

Reproduz o comportamento conferido no código do WLED (wled_server.cpp / file.cpp):
- GET /cfg.json e /presets.json servidos do "FS" sem PIN; caminhos com "sec" nunca.
- POST /upload exige correctPIN (global no dispositivo); upload de cfg.json reinicia.
- {"pin": "..."} num POST JSON desbloqueia; GET /settings/lock bloqueia de novo.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import struct
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

    # OTA (/update)
    ota_lock: bool = False
    ota_password: str = "wledota"
    ota_fails: bool = False  # responde 500 "Update failed!"
    ota_keeps_version: bool = False  # reinicia, mas continua na versão antiga
    validates_release: bool = False  # comportamento do 16.x
    firmware_uploads: list[str] = field(default_factory=list)

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
            if name == "cfg.json" and dev.ota_lock:
                cfg = json.loads(dev.files[name])
                cfg.setdefault("ota", {})["lock"] = True
                return httpx.Response(200, json=cfg)
            if "sec" not in name and name in dev.files:
                return httpx.Response(200, content=dev.files[name],
                                      headers={"content-type": "application/json"})
            return httpx.Response(404, text="<html>Not Found</html>")

        if request.method == "POST" and path == "/upload":
            return self._upload(dev, request)

        if request.method == "POST" and path == "/update":
            return self._ota(dev, request)

        if request.method == "POST" and path == "/json/cfg":
            return self._cfg(dev, request)

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

    def _cfg(self, dev: FakeDevice, request: httpx.Request) -> httpx.Response:
        """POST parcial em /json/cfg, com as regras de cfg.cpp para `ota`."""
        body = json.loads(request.content)
        dev.posts.append(body)
        if "pin" in body and dev.pin is not None and body["pin"] == dev.pin:
            dev.unlocked = True
        if not dev.unlocked:
            return httpx.Response(401, json={"error": 1})
        ota = body.get("ota")
        if isinstance(ota, dict):
            pwd = ota.get("psk")
            pwd_correct = not dev.ota_lock or (pwd is not None and pwd == dev.ota_password)
            if pwd_correct and "lock" in ota:
                dev.ota_lock = bool(ota["lock"])
        return httpx.Response(200, json={"success": True})

    def _ota(self, dev: FakeDevice, request: httpx.Request) -> httpx.Response:
        from wledadmin.wled.firmware import normalize_release, parse_firmware

        if dev.ota_lock:
            return httpx.Response(401, text="<html><h2>Access Denied</h2>"
                                            "Please unlock OTA in security settings!</html>")
        if not dev.unlocked:
            return httpx.Response(401, text="<html>Please unlock settings using PIN code!</html>")
        ctype = request.headers["content-type"].encode()
        msg = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + ctype + b"\r\n\r\n" + request.read()
        )
        part = next(msg.iter_parts())
        assert part.get_param("name", header="content-disposition") == "update"
        content = part.get_payload(decode=True)
        if dev.ota_fails:
            return httpx.Response(500, text="<html><h2>Update failed!</h2>Please check your "
                                            "file and retry!</html>")
        meta = parse_firmware(content).metadata
        if (dev.validates_release and meta and normalize_release(meta.release_name)
                != normalize_release(dev.info.get("release"))):
            return httpx.Response(500, text="<html>Update failed! Firmware release name "
                                            "mismatch</html>")
        dev.firmware_uploads.append(part.get_filename())
        if meta and not dev.ota_keeps_version:
            dev.info["ver"] = meta.wled_version
            dev.info["release"] = meta.release_name
        dev.reboot()
        return httpx.Response(200, text="<html><h2>Update successful!</h2>Rebooting...</html>")


# ---- firmware sintético ----------------------------------------------------------------

_CHIP_IDS = {"esp32": 0, "esp32-s2": 2, "esp32-c3": 5, "esp32-s3": 9}
_FLASH_NIBBLE = {1: 0x20, 2: 0x30, 4: 0x40}


def _djb2(data: bytes) -> int:
    h = 5381
    for c in data:
        h = ((h << 5) + h + c) & 0xFFFFFFFF
    return h


def make_firmware(
    arch: str = "esp32",
    release: str = "ESP32",
    version: str = "16.0.1",
    *,
    safe: tuple[int, int, int] = (0, 15, 2),
    flash_mb: int = 4,
    metadata: bool = True,
    gz: bool = False,
) -> bytes:
    """Imagem mínima com o mesmo layout dos binários reais (cabeçalho + metadados)."""
    body = bytearray(8192)
    if arch == "esp8266":
        body[:24] = bytes([0xE9, 2, 3, _FLASH_NIBBLE[flash_mb], 0x80, 0xF4, 0x10, 0x40,
                           0, 0xF0, 0x10, 0x40, 0x60, 0x0D, 0, 0, 0, 0x80, 0xFE, 0x3F,
                           0, 0x90, 0xFE, 0x3F])
        offset = 4120
    else:
        body[:12] = bytes([0xE9, 5, 2, 0x20, 0x68, 0x3E, 0x08, 0x40, 0xEE, 0, 0, 0])
        struct.pack_into("<H", body, 12, _CHIP_IDS[arch])
        body[14:24] = bytes([0, 0, 0, 0xFF, 0xFF, 0, 0, 0, 0, 1])
        offset = 288
    if metadata:
        rel = release.encode()
        struct.pack_into("<II48s48sI3B", body, offset, 0x57535453, 1, version.encode(), rel,
                         _djb2(rel), *safe)
    data = bytes(body)
    return gzip.compress(data) if gz else data


class FakeGitHub:
    """API de releases + downloads de assets."""

    def __init__(self, repo: str = "wled/WLED") -> None:
        self.repo = repo
        self.releases: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.status_code = 200
        self.corrupt: set[str] = set()

    def add_release(self, tag: str, assets: dict[str, bytes], *, prerelease: bool = False,
                    draft: bool = False) -> None:
        raw_assets = []
        for name, data in assets.items():
            url = f"https://github.com/{self.repo}/releases/download/{tag}/{name}"
            self.files[url] = data
            raw_assets.append({"name": name, "size": len(data), "browser_download_url": url,
                               "digest": "sha256:" + hashlib.sha256(data).hexdigest()})
        self.releases.append({
            "tag_name": tag, "name": tag, "prerelease": prerelease, "draft": draft,
            "published_at": "2026-07-07T18:19:48Z",
            "html_url": f"https://github.com/{self.repo}/releases/tag/{tag}",
            "assets": raw_assets,
        })

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == f"/repos/{self.repo}/releases":
            if self.status_code != 200:
                return httpx.Response(self.status_code, json={"message": "rate limited"})
            return httpx.Response(200, json=self.releases)
        if request.url.path.startswith("/repos/"):
            return httpx.Response(404, json={"message": "Not Found"})
        url = str(request.url)
        if url in self.files:
            data = self.files[url]
            if url in self.corrupt:
                data = data[:-1] + b"X"
            return httpx.Response(200, content=data)
        return httpx.Response(404)


def standard_github() -> FakeGitHub:
    """Releases parecidos com os reais: 0.14.4, 0.15.0, 0.15.4, 16.0.1, beta e nightly."""
    gh = FakeGitHub()

    def assets(ver: str, *, meta: bool, safe32=(0, 15, 3), safe=(0, 15, 2)) -> dict:
        def fw(arch, rel, **kw):
            return make_firmware(arch, rel, ver, metadata=meta, **kw)
        return {
            f"WLED_{ver}_ESP8266.bin": fw("esp8266", "ESP8266", flash_mb=4, safe=safe),
            f"WLED_{ver}_ESP01.bin": fw("esp8266", "ESP01", flash_mb=1, safe=safe),
            f"WLED_{ver}_ESP02.bin": fw("esp8266", "ESP02", flash_mb=4, safe=safe),
            f"WLED_{ver}_ESP02.bin.gz": fw("esp8266", "ESP02", flash_mb=2, gz=True, safe=safe),
            f"WLED_{ver}_ESP32.bin": fw("esp32", "ESP32", safe=safe32),
            f"WLED_{ver}_ESP32_Ethernet.bin": fw("esp32", "ESP32_Ethernet", safe=safe32),
            f"WLED_{ver}_ESP32-C3.bin": fw("esp32-c3", "ESP32-C3", safe=safe),
        }

    gh.add_release("v0.14.4", assets("0.14.4", meta=False))
    gh.add_release("v0.15.0", assets("0.15.0", meta=False))
    gh.add_release("v0.15.4", assets("0.15.4", meta=True, safe32=(0, 0, 0), safe=(0, 0, 0)))
    gh.add_release("v16.0.1", assets("16.0.1", meta=True))
    gh.add_release("v17.0.0-beta1", assets("17.0.0", meta=True), prerelease=True)
    gh.add_release("nightly", assets("17.0.0", meta=True), prerelease=True)
    gh.add_release("v18.0.0", {}, draft=True)
    return gh
