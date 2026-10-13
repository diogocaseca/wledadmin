import pytest

from wledadmin.wled import (
    WLEDAuthRequired,
    WLEDBadResponse,
    WLEDHTTPError,
    WLEDTimeout,
    WLEDUnreachable,
)

from .fakes import INFO_014, INFO_015, FakeDevice

IP = "192.168.1.50"


async def test_get_state_info_uses_single_si_request(client, network):
    network.add(IP, FakeDevice.from_fixture(INFO_015))
    state, info = await client.get_state_info(IP)
    assert info.name == "Cozinha" and state.bri == 128
    assert [r.url.path for r in network.requests] == ["/json/si"]


async def test_get_state_info_falls_back_without_si(client, network):
    network.add(IP, FakeDevice.from_fixture(INFO_014, has_si=False))
    state, info = await client.get_state_info(IP)
    assert info.ver == "0.14.4" and state.on is True
    assert sorted(r.url.path for r in network.requests) == ["/json/info", "/json/si", "/json/state"]


async def test_offline_raises_unreachable(client):
    with pytest.raises(WLEDUnreachable):
        await client.get_info("192.168.1.99")


async def test_timeout_raises_timeout(client, network):
    network.add(IP, FakeDevice.from_fixture(INFO_014, timeout=True))
    with pytest.raises(WLEDTimeout):
        await client.get_info(IP)


async def test_non_json_response(client, network):
    network.add(IP, FakeDevice.from_fixture(INFO_014, raw_body=b"<html>router</html>"))
    with pytest.raises(WLEDBadResponse):
        await client.get_info(IP)


async def test_json_array_is_bad_response(client, network):
    network.add(IP, FakeDevice.from_fixture(INFO_014, raw_body=b"[1,2]"))
    with pytest.raises(WLEDBadResponse):
        await client.get_info(IP)


async def test_http_errors(client, network):
    dev = network.add(IP, FakeDevice.from_fixture(INFO_014, status_code=401))
    with pytest.raises(WLEDAuthRequired):
        await client.get_info(IP)
    dev.status_code = 500
    with pytest.raises(WLEDHTTPError) as exc:
        await client.get_info(IP)
    assert exc.value.status_code == 500


async def test_set_state_sends_v_true_and_returns_state(client, network):
    dev = network.add(IP, FakeDevice.from_fixture(INFO_014))
    state = await client.set_state(IP, {"bri": 200})
    assert dev.posts == [{"bri": 200, "v": True}]
    assert state is not None and state.bri == 200


async def test_set_state_terse_response_returns_none(client, network):
    network.add(IP, FakeDevice.from_fixture(INFO_014, terse_post=True))
    assert await client.set_state(IP, {"on": False}) is None


async def test_get_json_file_restricted(client, network):
    with pytest.raises(ValueError):
        await client.get_json_file(IP, "../etc/passwd")
