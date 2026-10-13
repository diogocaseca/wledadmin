"""Tabelas ORM (SQLAlchemy 2.0).

Só dados que mudam devagar ficam aqui. Telemetria volátil (RSSI, uptime, heap, on/bri)
vive em memória no StatusStore, para não gravar no SQLite a cada ciclo de polling.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, TypeDecorator
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


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    # Hash Argon2id (argon2-cffi). Nunca a senha em texto.
    password_hash: Mapped[str] = mapped_column(String(255))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
