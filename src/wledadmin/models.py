"""Tabelas ORM (SQLAlchemy 2.0).

Só dados que mudam devagar ficam aqui. Telemetria volátil (RSSI, uptime, heap, on/bri)
vive em memória no StatusStore, para não gravar no SQLite a cada ciclo de polling.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """O SQLite não guarda fuso; normalizamos tudo para UTC na ida e na volta."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Identidade estável: o IP muda com o DHCP, o MAC não.
    mac: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    ip: Mapped[str] = mapped_column(String(64))
    hostname: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16))  # mdns | scan | manual
    arch: Mapped[str] = mapped_column(String(16), default="unknown")
    version: Mapped[str | None] = mapped_column(String(64))
    vid: Mapped[int | None] = mapped_column(Integer)
    release: Mapped[str | None] = mapped_column(String(64))  # 0.15+; usado pelo OTA futuro
    led_count: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Última verificação de backup bem-sucedida, mesmo sem mudança de conteúdo.
    # É o que o agendador usa (backups iguais não geram linha nova em `backups`).
    last_backup_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    # Hash Argon2id (argon2-cffi). Nunca a senha em texto.
    password_hash: Mapped[str] = mapped_column(String(255))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Backup(Base):
    """Uma versão de cfg.json ou presets.json de um dispositivo.

    Só é criada quando o conteúdo muda (SHA-256 diferente da versão anterior).
    `device_mac` é guardado à parte: se o dispositivo for removido do painel, o histórico
    continua e volta a se associar quando ele for redescoberto.
    """

    __tablename__ = "backups"
    __table_args__ = (Index("ix_backups_mac_kind_created", "device_mac", "kind", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    device_mac: Mapped[str] = mapped_column(String(12))
    device_name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # cfg | presets
    content: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    firmware_version: Mapped[str | None] = mapped_column(String(64))
    vid: Mapped[int | None] = mapped_column(Integer)
    release: Mapped[str | None] = mapped_column(String(64))
    arch: Mapped[str | None] = mapped_column(String(16))
    # manual | scheduled | pre_restore | import (e pre_ota na fase 3)
    trigger: Mapped[str] = mapped_column(String(16))
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppSetting(Base):
    """Configurações editáveis pela tela (chave/valor). Padrões ficam no código."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class OtaUpdate(Base):
    """Histórico permanente de cada etapa de atualização de firmware."""

    __tablename__ = "ota_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    device_mac: Mapped[str] = mapped_column(String(12), index=True)
    device_name: Mapped[str] = mapped_column(String(255))
    from_version: Mapped[str | None] = mapped_column(String(64))
    to_version: Mapped[str | None] = mapped_column(String(64))
    release_name: Mapped[str | None] = mapped_column(String(64))
    file_name: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16))  # github | custom
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # ok | failed
    message: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[str] = mapped_column(Text)  # JSON: lista de etapas
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime)
