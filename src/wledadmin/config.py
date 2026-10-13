"""Configuração via variáveis de ambiente (prefixo WLEDADMIN_) ou arquivo .env."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WLEDADMIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Servidor
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Persistência
    data_dir: Path = Path("./data")
    database_url: str | None = None  # padrão: sqlite em data_dir/wledadmin.db

    # Comunicação com os dispositivos
    http_connect_timeout: float = 2.0
    http_timeout: float = 4.0

    # Polling
    poll_interval: float = 10.0
    poll_concurrency: int = 16
    offline_after_failures: int = 2
    # Dispositivos offline são consultados só a cada N ciclos (backoff)
    offline_poll_every: int = 6

    # Descoberta
    mdns_enabled: bool = True
    scan_subnets: str = ""  # ex.: "192.168.1.0/24,192.168.10.0/24"
    scan_concurrency: int = 32
    scan_timeout: float = 1.5
    scan_max_hosts: int = 4096

    # Firmware (OTA). O token é opcional: sem ele a API do GitHub limita a 60 req/h,
    # o que basta porque a lista de releases fica em cache.
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    ota_upload_timeout: float = 240.0
    ota_reboot_timeout: float = 180.0

    @cached_property
    def db_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{self.data_dir / 'wledadmin.db'}"

    @property
    def scan_subnet_list(self) -> list[str]:
        return [s.strip() for s in self.scan_subnets.split(",") if s.strip()]
