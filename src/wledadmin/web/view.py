"""View-model da tabela: junta cadastro (SQLite) com status ao vivo (StatusStore)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from ..models import Device, utcnow
from ..services.backup import format_age
from ..services.status_store import StatusStore
from ..wled import Arch, normalize_arch


@dataclass(frozen=True)
class DeviceRow:
    id: int
    name: str
    ip: str
    mac: str
    hostname: str | None
    source: str
    online: bool | None
    version: str | None
    arch: str
    arch_label: str
    rssi: int | None
    signal: int | None
    uptime: int | None
    led_count: int | None
    freeheap: int | None
    on: bool | None
    bri: int | None
    bri_percent: int | None
    preset: int | None
    last_error: str | None
    last_seen_at: datetime | None
    last_backup_at: datetime | None
    backup_age: str
    backup_stale: bool

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("last_seen_at", "last_backup_at"):
            d[key] = d[key].isoformat() if d[key] else None
        return d


def build_rows(
    devices: list[Device], store: StatusStore, backup_interval_hours: int = 0
) -> list[DeviceRow]:
    now = utcnow()
    # Atrasado = passou do dobro do intervalo sem verificação de backup.
    stale_after = timedelta(hours=backup_interval_hours * 2) if backup_interval_hours else None
    rows = []
    for d in devices:
        st = store.get(d.id)
        info, state = st.info, st.state
        arch = info.arch if info else normalize_arch(d.arch)
        rows.append(DeviceRow(
            id=d.id,
            name=d.name,
            ip=d.ip,
            mac=d.mac,
            hostname=d.hostname,
            source=d.source,
            online=st.online,
            version=(info.ver if info else None) or d.version,
            arch=arch.value,
            arch_label=arch.label if arch is not Arch.UNKNOWN else (d.arch or "?"),
            rssi=info.wifi.rssi if info else None,
            signal=info.wifi.signal if info else None,
            uptime=info.uptime if info else None,
            led_count=(info.leds.count if info else None) or d.led_count,
            freeheap=info.freeheap if info else None,
            on=state.on if state else None,
            bri=state.bri if state else None,
            bri_percent=state.bri_percent if state else None,
            preset=state.ps if state and state.ps not in (None, -1) else None,
            last_error=st.last_error,
            last_seen_at=st.last_ok_at or d.last_seen_at,
            last_backup_at=d.last_backup_at,
            backup_age=format_age(d.last_backup_at, now),
            backup_stale=stale_after is not None
            and (d.last_backup_at is None or now - d.last_backup_at > stale_after),
        ))
    return rows


# ---- filtros Jinja ------------------------------------------------------------------

def fmt_mac(mac: str | None) -> str:
    if not mac or len(mac) != 12:
        return mac or "—"
    return ":".join(mac[i : i + 2] for i in range(0, 12, 2)).upper()


def fmt_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}min"
    return f"{m}min"


def fmt_kib(n: int | None) -> str:
    return "—" if n is None else f"{n / 1024:.0f} KiB"


def fmt_dt(dt: datetime | None) -> str:
    return "—" if dt is None else dt.astimezone().strftime("%d/%m %H:%M:%S")
