import struct

import pytest

from wledadmin.services.releases import ReleaseError, ReleaseService
from wledadmin.wled import Arch
from wledadmin.wled.firmware import (
    FirmwareError,
    asset_matches_arch,
    check_compatibility,
    normalize_release,
    parse_firmware,
    parse_version,
)

from .fakes import make_firmware, standard_github

# ---- parser -------------------------------------------------------------------------


def test_parse_esp32_family():
    for arch, expected in [("esp32", Arch.ESP32), ("esp32-c3", Arch.ESP32_C3),
                           ("esp32-s3", Arch.ESP32_S3)]:
        img = parse_firmware(make_firmware(arch, arch.upper()))
        assert img.family == "esp32" and img.chip is expected and img.flash_mb is None
        assert img.metadata.release_name == arch.upper()
        assert img.version == (16, 0, 1)


def test_parse_esp8266_flash_and_gzip():
    img = parse_firmware(make_firmware("esp8266", "ESP02", flash_mb=2, gz=True))
    assert img.family == "esp8266" and img.compressed and img.flash_mb == 2
    assert img.metadata.release_name == "ESP02"


def test_parse_without_metadata():
    img = parse_firmware(make_firmware(metadata=False))
    assert img.metadata is None and img.version is None


def test_metadata_with_bad_hash_is_ignored():
    data = bytearray(make_firmware())
    struct.pack_into("<I", data, 288 + 104, 12345)  # hash errado
    assert parse_firmware(bytes(data)).metadata is None


@pytest.mark.parametrize("data, msg", [
    (b"\x00" * 4096, "0xE9"),
    (b"\x1f\x8b garbage", ".gz inválido"),
    (b"\xe9" + b"\x00" * 10, "0xE9"),
])
def test_parse_rejects_garbage(data, msg):
    with pytest.raises(FirmwareError, match=msg):
        parse_firmware(data)


def test_parse_unknown_chip():
    data = bytearray(make_firmware())
    struct.pack_into("<H", data, 12, 0x77)
    with pytest.raises(FirmwareError, match="chip desconhecido"):
        parse_firmware(bytes(data))


def test_versions_and_release_names():
    assert parse_version("v16.0.1") == (16, 0, 1)
    assert parse_version("0.15.0-b3") == (0, 15, 0)
    assert parse_version("nightly") is None
    assert normalize_release("ESP32_V4") == "ESP32"
    assert asset_matches_arch("WLED_16.0.1_ESP32_Ethernet.bin", Arch.ESP32)
    assert not asset_matches_arch("WLED_16.0.1_ESP32-C3.bin", Arch.ESP32)
    assert asset_matches_arch("WLED_16.0.1_ESP02.bin.gz", Arch.ESP8266)
    assert not asset_matches_arch("WLED_16.0.1_ESP8266.bin", Arch.ESP32_C3)


# ---- compatibilidade ----------------------------------------------------------------


def compat(img, **kw):
    opts = dict(arch=Arch.ESP32, device_release="ESP32", device_version=(0, 15, 4),
                device_flash_mb=4, strict=True)
    return check_compatibility(parse_firmware(img), **{**opts, **kw})


def test_compat_ok():
    c = compat(make_firmware())
    assert c.blockers == [] and c.warnings == []


def test_compat_hardware_always_blocks():
    assert "ESP32-C3" in compat(make_firmware("esp32-c3", "ESP32-C3"), strict=False).blockers[0]
    assert "ESP8266" in compat(make_firmware("esp8266", "ESP8266"), strict=False).blockers[0]
    c = compat(make_firmware("esp8266", "ESP8266", flash_mb=4), arch=Arch.ESP8266,
               device_release="ESP02", device_flash_mb=2, strict=False)
    assert any("4 MB" in b for b in c.blockers)


def test_compat_release_and_safe_version_depend_on_strict():
    img = make_firmware(release="ESP32_Ethernet", safe=(0, 15, 3))
    strict = compat(img, device_version=(0, 15, 0))
    assert len(strict.blockers) == 2
    loose = compat(img, device_version=(0, 15, 0), strict=False)
    assert loose.blockers == [] and len(loose.warnings) == 2


def test_compat_v4_suffix_equivalent_and_downgrade_blocked():
    assert compat(make_firmware(), device_release="ESP32_V4").blockers == []
    c = compat(make_firmware(version="0.15.0"), device_version=(16, 0, 1), strict=False)
    assert any("downgrade" in b for b in c.blockers)


# ---- releases do GitHub -------------------------------------------------------------


@pytest.fixture
def github():
    return standard_github()


@pytest.fixture
async def releases(tmp_path, github):
    svc = ReleaseService(tmp_path, api_url="https://api.github.com", token=None,
                         transport=github.transport())
    yield svc
    await svc.close()


async def test_list_filters_and_caches(releases, github):
    stable = await releases.list_releases("wled/WLED", include_prereleases=False)
    assert [r.tag for r in stable] == ["v16.0.1", "v0.15.4", "v0.15.0", "v0.14.4"]
    with_beta = await releases.list_releases("wled/WLED", include_prereleases=True)
    assert [r.tag for r in with_beta][0] == "v17.0.0-beta1"
    assert "nightly" not in [r.tag for r in with_beta]
    assert len(github.requests) == 1, "segunda chamada vem do cache"


async def test_find_asset_rules(releases):
    r = await releases.get_release("wled/WLED", "v16.0.1")
    assert r.find_asset("ESP32").name == "WLED_16.0.1_ESP32.bin"
    assert r.find_asset("ESP32_V4").name == "WLED_16.0.1_ESP32.bin"
    assert r.find_asset("ESP02").name == "WLED_16.0.1_ESP02.bin.gz"
    assert r.find_asset("ESP32-S3_8MB_opi") is None


async def test_rate_limit_falls_back_to_disk(tmp_path, github, releases):
    await releases.list_releases("wled/WLED", include_prereleases=False)
    github.status_code = 403
    fresh = ReleaseService(tmp_path, api_url="https://api.github.com", token=None,
                           transport=github.transport())
    try:
        rs = await fresh.list_releases("wled/WLED", include_prereleases=False)
        assert rs[0].tag == "v16.0.1" and rs[0].assets
    finally:
        await fresh.close()
    empty = ReleaseService(tmp_path / "x", api_url="https://api.github.com", token=None,
                           transport=github.transport())
    with pytest.raises(ReleaseError, match="limite"):
        await empty.list_releases("wled/WLED", include_prereleases=False)
    await empty.close()


async def test_unknown_repo(releases):
    with pytest.raises(ReleaseError, match="não encontrado"):
        await releases.list_releases("nobody/nothing", include_prereleases=False)


async def test_fetch_asset_verifies_digest_and_caches(releases, github, tmp_path):
    r = await releases.get_release("wled/WLED", "v16.0.1")
    asset = r.find_asset("ESP32")
    data = await releases.fetch_asset(r, asset)
    assert parse_firmware(data).chip is Arch.ESP32
    n = len(github.requests)
    assert await releases.fetch_asset(r, asset) == data
    assert len(github.requests) == n, "segunda leitura vem do disco"

    other = r.find_asset("ESP32-C3")
    github.corrupt.add(other.url)
    with pytest.raises(ReleaseError, match="SHA-256"):
        await releases.fetch_asset(r, other)
    assert not (tmp_path / "firmware" / "v16.0.1" / other.name).exists()


def test_custom_store(releases):
    digest = releases.store_custom(b"abc")
    assert releases.load_custom(digest) == b"abc"
    with pytest.raises(ReleaseError):
        releases.load_custom("../../etc/passwd")
