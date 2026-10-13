"""Ações em massa via POST /json/state.

O conjunto de ações é fechado e validado. Não aceitamos JSON arbitrário: é o mesmo
cuidado que o OTA vai exigir, e evita um formulário virar um "proxy" genérico pro WLED.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, model_validator

from ..wled import WLEDClient, WLEDError
from .registry import DeviceRegistry
from .status_store import StatusStore


class ActionKind(StrEnum):
    ON = "on"
    OFF = "off"
    TOGGLE = "toggle"
    BRIGHTNESS = "brightness"
    PRESET = "preset"


class BulkAction(BaseModel):
    kind: ActionKind
    value: int | None = None

    @model_validator(mode="after")
    def _check_value(self) -> Self:
        if self.kind is ActionKind.BRIGHTNESS:
            if self.value is None or not 0 <= self.value <= 255:
                raise ValueError("brilho deve estar entre 0 e 255")
        elif self.kind is ActionKind.PRESET:
            if self.value is None or not 1 <= self.value <= 250:
                raise ValueError("preset deve estar entre 1 e 250")
        return self

    def to_payload(self) -> dict[str, Any]:
        match self.kind:
            case ActionKind.ON:
                return {"on": True}
            case ActionKind.OFF:
                return {"on": False}
            case ActionKind.TOGGLE:
                return {"on": "t"}
            case ActionKind.BRIGHTNESS:
                return {"bri": self.value}
            case ActionKind.PRESET:
                return {"ps": self.value}


@dataclass(frozen=True)
class ActionResult:
    device_id: int
    name: str
    ok: bool
    error: str | None = None


class ActionService:
    def __init__(
        self,
        client: WLEDClient,
        registry: DeviceRegistry,
        store: StatusStore,
        *,
        concurrency: int,
    ) -> None:
        self._client = client
        self._registry = registry
        self._store = store
        self._concurrency = concurrency

    async def execute(self, device_ids: list[int], action: BulkAction) -> list[ActionResult]:
        devices = await self._registry.get_many(device_ids)
        payload = action.to_payload()
        sem = asyncio.Semaphore(max(1, self._concurrency))

        async def run(device) -> ActionResult:
            async with sem, self._registry.lock(device.id):
                try:
                    state = await self._client.set_state(device.ip, payload)
                except WLEDError as e:
                    self._store.record_failure(device.id, str(e))
                    return ActionResult(device.id, device.name, False, str(e))
                if state is not None:
                    self._store.record_state(device.id, state)
                return ActionResult(device.id, device.name, True)

        results = await asyncio.gather(*(run(d) for d in devices))
        found = {d.id for d in devices}
        missing = [
            ActionResult(i, f"#{i}", False, "dispositivo não encontrado")
            for i in device_ids
            if i not in found
        ]
        return sorted([*results, *missing], key=lambda r: r.name.lower())
