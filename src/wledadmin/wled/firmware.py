"""Análise de binários de firmware do WLED, antes de qualquer envio por OTA.

O WLED 0.14/0.15 grava qualquer arquivo recebido em /update. Só o 16.x confere o nome
do release embutido. Por isso a validação aqui é a principal proteção contra gravar o
binário errado.

Formato conferido em arquivos reais dos releases (0.15.3 e 16.0.1):
- Imagem ESP (cabeçalho esp_image_header_t): byte 0 = 0xE9.
  * ESP32/S2/S3/C3/C6: byte 8 = 0xEE (wp_pin), chip_id (uint16 LE) no offset 12,
    bytes 19..22 reservados (zero), byte 23 = hash_appended (0/1).
  * ESP8266: nibble alto do byte 3 = tamanho da flash (2 = 1 MB, 3 = 2 MB, 4 = 4 MB...).
- Metadados do WLED (wled_metadata.cpp): struct "packed" com magic 0x57535453,
  desc_version, versão (48 bytes), release (48 bytes), hash DJB2 do release e
  safe_update_version (3 bytes). Presentes nos builds a partir da 0.15.3.
- Builds .bin.gz (ESP8266 de 2 MB) são gzip de uma imagem igual às de cima.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import struct
import zlib
from dataclasses import dataclass

from .schemas import Arch

ESP_IMAGE_MAGIC = 0xE9
GZIP_MAGIC = b"\x1f\x8b"
MAX_IMAGE_BYTES = 8 * 1024 * 1024

WLED_DESC_MAGIC = struct.pack("<I", 0x57535453)
_DESC_FORMAT = "<II48s48sI3B"
_DESC_SIZE = struct.calcsize(_DESC_FORMAT)

# esp_chip_id_t do ESP-IDF
CHIP_IDS: dict[int, Arch] = {
    0x0000: Arch.ESP32,
    0x0002: Arch.ESP32_S2,
    0x0005: Arch.ESP32_C3,
    0x0009: Arch.ESP32_S3,
    0x000D: Arch.ESP32_C6,
}

# Nibble alto do byte 3 no cabeçalho do ESP8266 -> MB
ESP8266_FLASH_MB: dict[int, float] = {0: 0.5, 1: 0.25, 2: 1, 3: 2, 4: 4, 8: 8, 9: 16}


class FirmwareError(ValueError):
    pass


@dataclass(frozen=True)
class FirmwareMetadata:
    wled_version: str
    release_name: str
    desc_version: int
    safe_update_version: tuple[int, int, int]


@dataclass(frozen=True)
class FirmwareImage:
    size: int
    sha256: str
    compressed: bool
    family: str  # "esp32" (qualquer ESP32) | "esp8266"
    chip: Arch | None  # só para family == "esp32"
    flash_mb: float | None  # só para family == "esp8266"
    metadata: FirmwareMetadata | None

    @property
    def version(self) -> tuple[int, int, int] | None:
        return parse_version(self.metadata.wled_version) if self.metadata else None


def _djb2(data: bytes) -> int:
    h = 5381
    for c in data:
        h = ((h << 5) + h + c) & 0xFFFFFFFF
    return h


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", errors="replace")


def find_metadata(image: bytes) -> FirmwareMetadata | None:
    pos = image.find(WLED_DESC_MAGIC)
    while pos != -1 and pos + _DESC_SIZE <= len(image):
        _, desc_ver, ver, rel, h, s0, s1, s2 = struct.unpack_from(_DESC_FORMAT, image, pos)
        release = rel.split(b"\0", 1)[0]
        # Mesmo critério do firmware: o hash do release descarta falsos positivos.
        if release and h == _djb2(release):
            return FirmwareMetadata(_cstr(ver), _cstr(rel), desc_ver, (s0, s1, s2))
        pos = image.find(WLED_DESC_MAGIC, pos + 1)
    return None


def _decompress(data: bytes) -> bytes:
    try:
        d = zlib.decompressobj(wbits=31)
        out = d.decompress(data, MAX_IMAGE_BYTES)
    except (zlib.error, gzip.BadGzipFile) as e:
        raise FirmwareError(f"arquivo .gz inválido ({e})") from e
    if d.unconsumed_tail:
        raise FirmwareError("arquivo descompactado grande demais")
    return out


def parse_firmware(data: bytes) -> FirmwareImage:
    if len(data) > MAX_IMAGE_BYTES:
        raise FirmwareError("arquivo grande demais para ser um firmware do WLED")
    compressed = data[:2] == GZIP_MAGIC
    image = _decompress(data) if compressed else data
    if len(image) < 1024 or image[0] != ESP_IMAGE_MAGIC:
        raise FirmwareError("não é uma imagem de firmware ESP (byte inicial diferente de 0xE9)")

    looks_esp32 = image[8] == 0xEE and image[19:23] == b"\0\0\0\0" and image[23] in (0, 1)
    if looks_esp32:
        chip_id = struct.unpack_from("<H", image, 12)[0]
        chip = CHIP_IDS.get(chip_id)
        if chip is None:
            raise FirmwareError(f"chip desconhecido no cabeçalho (chip_id={chip_id})")
        family, flash_mb = "esp32", None
    else:
        family, chip = "esp8266", None
        flash_mb = ESP8266_FLASH_MB.get(image[3] >> 4)

    return FirmwareImage(
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        compressed=compressed,
        family=family,
        chip=chip,
        flash_mb=flash_mb,
        metadata=find_metadata(image),
    )


# ---- versões e nomes de release ------------------------------------------------------

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(raw: str | None) -> tuple[int, int, int] | None:
    if not raw:
        return None
    m = _VERSION_RE.search(raw)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def format_version(v: tuple[int, int, int] | None) -> str:
    return "?" if v is None else ".".join(map(str, v))


def normalize_release(name: str | None) -> str | None:
    """Igual ao normalizeReleaseName() do WLED: "ESP32_V4" equivale a "ESP32"."""
    if not name:
        return None
    return name[:-3] if name.endswith("_V4") else name


def arch_family(arch: Arch) -> str | None:
    if arch is Arch.ESP8266:
        return "esp8266"
    if arch is Arch.UNKNOWN:
        return None
    return "esp32"


def asset_matches_arch(asset_name: str, arch: Arch) -> bool:
    """O arquivo do release é da mesma família de chip? (usado para listar opções)"""
    m = re.match(r"WLED_[^_]+_(.+?)\.bin(\.gz)?$", asset_name)
    if not m:
        return False
    rel = m[1].upper()
    if arch is Arch.ESP8266:
        return rel.startswith(("ESP8266", "ESP01", "ESP02"))
    if arch is Arch.ESP32:
        return rel.startswith("ESP32") and not rel.startswith("ESP32-")
    if arch is Arch.UNKNOWN:
        return False
    return rel.startswith(arch.value.upper())


@dataclass(frozen=True)
class Compatibility:
    blockers: list[str]
    warnings: list[str]


def check_compatibility(
    image: FirmwareImage,
    *,
    arch: Arch,
    device_release: str | None,
    device_version: tuple[int, int, int] | None,
    device_flash_mb: float | None,
    strict: bool,
    check_safe_version: bool = True,
) -> Compatibility:
    """Compara um firmware com o dispositivo.

    strict=True (arquivo escolhido automaticamente do release oficial): divergência de
    nome de release e de versão mínima segura bloqueiam.
    strict=False (.bin próprio ou escolha manual): esses dois viram avisos; continuam
    bloqueando só os problemas de hardware e o downgrade.
    check_safe_version=False: o chamador trata a versão mínima (ex.: planejando duas etapas).
    """
    blockers: list[str] = []
    warnings: list[str] = []

    family = arch_family(arch)
    if family is None:
        blockers.append("a arquitetura do dispositivo é desconhecida")
    elif image.family != family:
        blockers.append(f"o arquivo é para {image.family.upper()}, e o dispositivo é "
                        f"{arch.label}")
    elif image.family == "esp32" and image.chip is not arch:
        blockers.append(f"o arquivo é para {image.chip.label}, e o dispositivo é {arch.label}")

    if image.family == "esp8266" and image.flash_mb and device_flash_mb:
        if image.flash_mb > device_flash_mb:
            blockers.append(f"o arquivo espera {image.flash_mb:g} MB de flash e o dispositivo "
                            f"tem {device_flash_mb:g} MB")
        elif image.flash_mb < device_flash_mb:
            warnings.append(f"o arquivo é para {image.flash_mb:g} MB de flash e o dispositivo "
                            f"tem {device_flash_mb:g} MB (o sistema de arquivos pode ser perdido)")

    meta = image.metadata
    if meta is None:
        warnings.append("o arquivo não tem metadados do WLED (builds anteriores à 0.15.3): "
                        "não dá para conferir o nome do release nem a versão")
    else:
        img_rel = normalize_release(meta.release_name)
        dev_rel = normalize_release(device_release)
        if dev_rel and img_rel != dev_rel:
            msg = (f"o arquivo é do release \"{meta.release_name}\" e o dispositivo usa "
                   f"\"{device_release}\"")
            if strict:
                blockers.append(msg)
            else:
                warnings.append(msg + "; dispositivos 16.x recusam essa troca sozinhos")
        target = image.version
        if target and device_version:
            if target < device_version:
                blockers.append(f"downgrade ({format_version(device_version)} → "
                                f"{format_version(target)}) não é permitido")
            safe = meta.safe_update_version
            if check_safe_version and any(safe) and device_version < safe:
                msg = (f"o WLED recomenda estar na {format_version(safe)} ou mais nova antes de "
                       f"instalar a {format_version(target)}")
                (blockers if strict else warnings).append(msg)
    return Compatibility(blockers, warnings)
