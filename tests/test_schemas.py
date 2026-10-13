import pytest

from wledadmin.wled import Arch, DeviceInfo, DeviceState, normalize_arch, normalize_mac

from .fakes import INFO_014, INFO_015, INFO_16, load_fixture


@pytest.mark.parametrize(
    ("fixture", "arch", "release", "mac"),
    [
        (INFO_014, Arch.ESP8266, None, "a4cf12aabb01"),
        (INFO_015, Arch.ESP32, "ESP32", "240ac4aabb02"),
        (INFO_16, Arch.ESP32_C3, "ESP32-C3", "34b4720c0d03"),
    ],
)
def test_info_across_versions(fixture, arch, release, mac):
    info = DeviceInfo.model_validate(load_fixture(fixture))
    assert info.arch is arch
    assert info.release == release
    assert info.mac_normalized == mac
    assert info.looks_like_wled
    assert info.leds.count and info.wifi.rssi and info.uptime is not None


def test_info_tolerates_missing_and_wrong_types():
    info = DeviceInfo.model_validate(
        {"ver": "0.14.0", "brand": "WLED", "uptime": "abc", "wifi": "nope", "leds": None,
         "freeheap": True, "unknown_future_key": {"x": 1}}
    )
    assert info.uptime is None
    assert info.wifi.rssi is None
    assert info.leds.count is None
    assert info.freeheap is None
    assert info.arch is Arch.UNKNOWN
    assert info.mac_normalized is None


def test_empty_info_does_not_raise():
    info = DeviceInfo.model_validate({})
    assert not info.looks_like_wled


def test_non_wled_brand():
    assert not DeviceInfo.model_validate({"brand": "Tasmota", "ver": "1"}).looks_like_wled


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("esp8266", Arch.ESP8266), ("ESP32-S3", Arch.ESP32_S3), ("esp32_s2", Arch.ESP32_S2),
     ("ESP32-C6", Arch.ESP32_C6), ("riscv-x", Arch.UNKNOWN), (None, Arch.UNKNOWN)],
)
def test_normalize_arch(raw, expected):
    assert normalize_arch(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("A4:CF:12:AA:BB:01", "a4cf12aabb01"), ("a4cf12aabb01", "a4cf12aabb01"),
     ("000000000000", None), ("xyz", None), ("", None)],
)
def test_normalize_mac(raw, expected):
    assert normalize_mac(raw) == expected


def test_state_brightness_percent():
    st = DeviceState.model_validate(load_fixture("state.json"))
    assert st.on is True and st.bri == 128 and st.ps == 3
    assert st.bri_percent == 50
