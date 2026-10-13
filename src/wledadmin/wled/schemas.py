"""Modelos tolerantes das respostas JSON do WLED.

Os campos mudam entre versões (0.14, 0.15, 16.x), conforme wled00/json.cpp:
- `release`, `repo`, `deviceId`, `cn`, `psrSz` não existem na 0.14.
- `arch` vem como "esp8266"/"esp32" literal ou como ESP.getChipModel() ("ESP32-C3", ...).

Por isso todo campo é opcional, chaves desconhecidas são ignoradas e um valor com tipo
inesperado vira None em vez de invalidar a resposta inteira.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _lenient_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _lenient_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (str, int, float)):
        return str(v)
    return None


def _lenient_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    return None


def _lenient_obj(v: Any) -> Any:
    return v if isinstance(v, dict) else {}


OptInt = Annotated[int | None, BeforeValidator(_lenient_int)]
OptStr = Annotated[str | None, BeforeValidator(_lenient_str)]
OptBool = Annotated[bool | None, BeforeValidator(_lenient_bool)]


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


class Arch(StrEnum):
    ESP8266 = "esp8266"
    ESP32 = "esp32"
    ESP32_S2 = "esp32-s2"
    ESP32_S3 = "esp32-s3"
    ESP32_C3 = "esp32-c3"
    ESP32_C6 = "esp32-c6"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return "?" if self is Arch.UNKNOWN else self.value.upper()


def normalize_arch(raw: str | None) -> Arch:
    if not raw:
        return Arch.UNKNOWN
    key = raw.strip().lower().replace("_", "-")
    try:
        return Arch(key)
    except ValueError:
        return Arch.UNKNOWN


_MAC_RE = re.compile(r"^[0-9a-f]{12}$")


def normalize_mac(raw: str | None) -> str | None:
    """Normaliza para 12 hex minúsculos sem separador (formato nativo do WLED)."""
    if not raw:
        return None
    mac = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    if not _MAC_RE.match(mac) or mac == "000000000000":
        return None
    return mac


class LedsInfo(_Lenient):
    count: OptInt = None
    fps: OptInt = None
    pwr: OptInt = None
    maxpwr: OptInt = None
    maxseg: OptInt = None


class WifiInfo(_Lenient):
    bssid: OptStr = None
    rssi: OptInt = None
    signal: OptInt = None
    channel: OptInt = None


class FsInfo(_Lenient):
    u: OptInt = None
    t: OptInt = None
    pmt: OptInt = None


class DeviceInfo(_Lenient):
    ver: OptStr = None
    vid: OptInt = None
    name: OptStr = None
    mac: OptStr = None
    ip: OptStr = None
    arch_raw: OptStr = Field(default=None, alias="arch")
    core: OptStr = None
    release: OptStr = None  # 0.15+
    repo: OptStr = None  # 16.0+
    brand: OptStr = None
    product: OptStr = None
    uptime: OptInt = None
    freeheap: OptInt = None
    psram: OptInt = None
    fxcount: OptInt = None
    palcount: OptInt = None
    ws: OptInt = None
    leds: Annotated[LedsInfo, BeforeValidator(_lenient_obj)] = LedsInfo()
    wifi: Annotated[WifiInfo, BeforeValidator(_lenient_obj)] = WifiInfo()
    fs: Annotated[FsInfo, BeforeValidator(_lenient_obj)] = FsInfo()

    @property
    def arch(self) -> Arch:
        return normalize_arch(self.arch_raw)

    @property
    def mac_normalized(self) -> str | None:
        return normalize_mac(self.mac)

    @property
    def looks_like_wled(self) -> bool:
        # `brand` é sempre "WLED" no firmware oficial; forks antigos podem omitir.
        if self.brand is not None:
            return self.brand.upper() == "WLED"
        return self.ver is not None and self.leds.count is not None


class DeviceState(_Lenient):
    on: OptBool = None
    bri: OptInt = None
    ps: OptInt = None
    pl: OptInt = None
    transition: OptInt = None

    @property
    def bri_percent(self) -> int | None:
        return None if self.bri is None else round(self.bri * 100 / 255)
