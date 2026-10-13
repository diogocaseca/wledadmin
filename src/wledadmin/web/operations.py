"""Operações compartilhadas entre as rotas HTMX (HTML) e a API JSON."""

from __future__ import annotations

import ipaddress
import re

from ..models import Device
from ..services.container import Services
from ..services.discovery import NotWLED
from ..wled import WLEDError

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9-]{1,63})*$")


class OperationError(Exception):
    pass


def validate_host(raw: str) -> str:
    host = raw.strip()
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if _HOSTNAME_RE.match(host):
            return host
        raise OperationError("informe um IPv4 ou hostname válido") from None
    if addr.version != 4:
        raise OperationError("só IPv4 é suportado")
    return str(addr)


async def add_manual(services: Services, raw_host: str) -> Device:
    host = validate_host(raw_host)
    try:
        device = await services.prober.probe_and_register(host, "manual")
    except WLEDError as e:
        raise OperationError(f"não foi possível falar com {host}: {e}") from e
    except (NotWLED, ValueError) as e:
        raise OperationError(str(e)) from e
    # Já consulta o status para a linha aparecer completa.
    await services.poller.poll_devices([device])
    return device


async def delete_device(services: Services, device_id: int) -> bool:
    deleted = await services.registry.delete(device_id)
    services.store.remove(device_id)
    return deleted
