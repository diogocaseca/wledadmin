"""Exceções da comunicação com dispositivos WLED.

Todas herdam de WLEDError, para que quem chama possa tratar "dispositivo com problema"
de forma única sem deixar escapar exceções do httpx.
"""


class WLEDError(Exception):
    """Erro genérico ao falar com um dispositivo WLED."""


class WLEDUnreachable(WLEDError):
    """Falha de conexão (host desligado, recusou conexão, sem rota)."""


class WLEDTimeout(WLEDUnreachable):
    """O dispositivo não respondeu dentro do timeout."""


class WLEDAuthRequired(WLEDError):
    """O dispositivo exigiu autenticação (ex.: settings/OTA protegidos por PIN/senha)."""


class WLEDOtaLocked(WLEDError):
    """O dispositivo está com "OTA lock" ligado (Config → Security & Updates)."""


class WLEDHTTPError(WLEDError):
    """Resposta HTTP de erro (4xx/5xx)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class WLEDBadResponse(WLEDError):
    """Resposta que não é o JSON esperado (provavelmente não é um WLED)."""
