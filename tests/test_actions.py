import pytest
from pydantic import ValidationError

from wledadmin.services.actions import ActionKind, ActionService, BulkAction
from wledadmin.wled import DeviceInfo

from .fakes import INFO_014, INFO_015, INFO_16, FakeDevice, load_fixture


@pytest.mark.parametrize(
    ("kind", "value", "payload"),
    [("on", None, {"on": True}), ("off", None, {"on": False}), ("toggle", None, {"on": "t"}),
     ("brightness", 0, {"bri": 0}), ("brightness", 255, {"bri": 255}),
     ("preset", 3, {"ps": 3})],
)
def test_payloads(kind, value, payload):
    assert BulkAction(kind=kind, value=value).to_payload() == payload


@pytest.mark.parametrize(
    ("kind", "value"),
    [("brightness", None), ("brightness", 256), ("brightness", -1), ("preset", 0),
     ("preset", 251), ("preset", None), ("explode", None)],
)
def test_invalid_actions(kind, value):
    with pytest.raises(ValidationError):
        BulkAction(kind=kind, value=value)


async def test_bulk_action_partial_failure(client, network, registry, store):
    devs = {}
    for fx, ip in [(INFO_014, "10.0.0.1"), (INFO_015, "10.0.0.2"), (INFO_16, "10.0.0.3")]:
        devs[ip] = network.add(ip, FakeDevice.from_fixture(fx))
        await registry.upsert_from_info(DeviceInfo.model_validate(load_fixture(fx)), ip, "manual")
    devs["10.0.0.3"].offline = True
    ids = [d.id for d in await registry.list()]

    svc = ActionService(client, registry, store, concurrency=4)
    results = await svc.execute([*ids, 999], BulkAction(kind=ActionKind.BRIGHTNESS, value=10))

    by_name = {r.name: r for r in results}
    assert by_name["Sala"].ok and by_name["Cozinha"].ok
    assert not by_name["Quarto"].ok
    assert not by_name["#999"].ok
    assert devs["10.0.0.1"].posts == [{"bri": 10, "v": True}]
    # O estado devolvido pelo "v": true já atualiza o cache, sem esperar o poll
    sala = next(d for d in await registry.list() if d.name == "Sala")
    assert store.get(sala.id).state.bri == 10
