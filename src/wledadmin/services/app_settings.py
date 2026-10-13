"""Configurações editáveis pela tela, guardadas na tabela app_settings.

Os padrões ficam aqui no código; o banco só guarda o que o usuário alterou.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..models import AppSetting


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class IntSetting:
    key: str
    default: int
    min: int
    max: int
    label: str

    def parse(self, raw: str) -> int:
        return self.validate(int(raw))

    def validate(self, value: int) -> int:
        if not self.min <= value <= self.max:
            raise SettingsError(f"{self.label}: use um valor entre {self.min} e {self.max}")
        return value


@dataclass(frozen=True)
class BoolSetting:
    key: str
    default: bool
    label: str

    def parse(self, raw: str) -> bool:
        return raw == "1"

    def validate(self, value: bool) -> bool:
        return bool(value)


@dataclass(frozen=True)
class StrSetting:
    key: str
    default: str
    pattern: str
    label: str
    hint: str

    def parse(self, raw: str) -> str:
        return self.validate(raw)

    def validate(self, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(self.pattern, value):
            raise SettingsError(f"{self.label}: {self.hint}")
        return value


Setting = IntSetting | BoolSetting | StrSetting

BACKUP_INTERVAL_HOURS = IntSetting(
    "backup_interval_hours", 24, 0, 24 * 30, "Intervalo do backup automático (horas, 0 desliga)"
)
BACKUP_KEEP = IntSetting("backup_keep", 30, 1, 1000, "Versões guardadas por dispositivo e tipo")
OTA_REPO = StrSetting(
    "ota_repo", "wled/WLED", r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
    "Repositório de firmware", "use o formato dono/repositório (ex.: wled/WLED)",
)
OTA_INCLUDE_PRERELEASES = BoolSetting("ota_prereleases", False, "Mostrar versões beta")
OTA_STOP_ON_FAILURE = BoolSetting("ota_stop_on_failure", True, "Parar no primeiro erro")

ALL_SETTINGS: tuple[Setting, ...] = (
    BACKUP_INTERVAL_HOURS, BACKUP_KEEP, OTA_REPO, OTA_INCLUDE_PRERELEASES, OTA_STOP_ON_FAILURE,
)


class AppSettingsService:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory
        self._cache: dict[str, Any] = {}

    async def load(self) -> None:
        async with self._sf() as s:
            for setting in ALL_SETTINGS:
                row = await s.get(AppSetting, setting.key)
                self._cache[setting.key] = _parse(setting, row.value if row else None)

    def get(self, setting: Setting) -> Any:
        return self._cache.get(setting.key, setting.default)

    async def set(self, setting: Setting, value: Any) -> None:
        value = setting.validate(value)
        raw = ("1" if value else "0") if isinstance(setting, BoolSetting) else str(value)
        async with self._sf() as s:
            row = await s.get(AppSetting, setting.key)
            if row is None:
                s.add(AppSetting(key=setting.key, value=raw))
            else:
                row.value = raw
            await s.commit()
        self._cache[setting.key] = value


def _parse(setting: Setting, raw: str | None) -> Any:
    if raw is None:
        return setting.default
    try:
        return setting.parse(raw)
    except (ValueError, SettingsError):
        return setting.default
