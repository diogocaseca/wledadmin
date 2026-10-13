"""Configurações editáveis pela tela, guardadas na tabela app_settings.

Os padrões ficam aqui no código; o banco só guarda o que o usuário alterou.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..models import AppSetting


@dataclass(frozen=True)
class IntSetting:
    key: str
    default: int
    min: int
    max: int
    label: str


BACKUP_INTERVAL_HOURS = IntSetting(
    "backup_interval_hours", 24, 0, 24 * 30, "Intervalo do backup automático (horas, 0 desliga)"
)
BACKUP_KEEP = IntSetting("backup_keep", 30, 1, 1000, "Versões guardadas por dispositivo e tipo")

ALL_SETTINGS = (BACKUP_INTERVAL_HOURS, BACKUP_KEEP)


class SettingsError(ValueError):
    pass


class AppSettingsService:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory
        self._cache: dict[str, int] = {}

    async def load(self) -> None:
        async with self._sf() as s:
            for setting in ALL_SETTINGS:
                row = await s.get(AppSetting, setting.key)
                self._cache[setting.key] = _parse(setting, row.value if row else None)

    def get(self, setting: IntSetting) -> int:
        return self._cache.get(setting.key, setting.default)

    async def set(self, setting: IntSetting, value: int) -> None:
        if not setting.min <= value <= setting.max:
            raise SettingsError(
                f"{setting.label}: use um valor entre {setting.min} e {setting.max}"
            )
        async with self._sf() as s:
            row = await s.get(AppSetting, setting.key)
            if row is None:
                s.add(AppSetting(key=setting.key, value=str(value)))
            else:
                row.value = str(value)
            await s.commit()
        self._cache[setting.key] = value


def _parse(setting: IntSetting, raw: str | None) -> int:
    try:
        value = int(raw) if raw is not None else setting.default
    except ValueError:
        return setting.default
    return value if setting.min <= value <= setting.max else setting.default
