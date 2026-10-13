"""Cache em memória do último status conhecido de cada dispositivo.

A UI lê só daqui. Ela nunca espera por um dispositivo, então renderizar o painel é
instantâneo mesmo com vários dispositivos offline.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from ..models import utcnow
from ..wled import DeviceInfo, DeviceState


@dataclass(frozen=True)
class DeviceStatus:
    device_id: int
    online: bool | None = None  # None = ainda não consultado
    info: DeviceInfo | None = None
    state: DeviceState | None = None
    last_ok_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0


class StatusStore:
    def __init__(self, offline_after_failures: int = 2) -> None:
        self._offline_after = max(1, offline_after_failures)
        self._data: dict[int, DeviceStatus] = {}

    def get(self, device_id: int) -> DeviceStatus:
        return self._data.get(device_id) or DeviceStatus(device_id=device_id)

    def all(self) -> dict[int, DeviceStatus]:
        return dict(self._data)

    def remove(self, device_id: int) -> None:
        self._data.pop(device_id, None)

    def record_success(self, device_id: int, info: DeviceInfo, state: DeviceState) -> None:
        now = utcnow()
        self._data[device_id] = replace(
            self.get(device_id),
            online=True,
            info=info,
            state=state,
            last_ok_at=now,
            last_attempt_at=now,
            last_error=None,
            consecutive_failures=0,
        )

    def record_state(self, device_id: int, state: DeviceState) -> None:
        """Atualiza só o estado (resposta de uma ação com "v": true)."""
        now = utcnow()
        self._data[device_id] = replace(
            self.get(device_id), online=True, state=state, last_ok_at=now, last_error=None,
            consecutive_failures=0,
        )

    def record_failure(self, device_id: int, error: str) -> None:
        cur = self.get(device_id)
        failures = cur.consecutive_failures + 1
        # Anti-flapping: só marca offline depois de N falhas seguidas.
        online = cur.online if failures < self._offline_after else False
        self._data[device_id] = replace(
            cur,
            online=online,
            last_attempt_at=utcnow(),
            last_error=error,
            consecutive_failures=failures,
        )
