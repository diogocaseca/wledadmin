"""HTTPS na porta do acesso direto, com recarga do certificado sem reiniciar.

O certificado do Let's Encrypt (ex.: App DuckDNS do Home Assistant) é renovado a cada ~60
dias. O uvicorn usa o mesmo `ssl.SSLContext` para todas as conexões do servidor; recarregar
a cadeia nesse objeto faz as conexões novas receberem o certificado novo.

Uma carga com erro no meio pode deixar o contexto com certificado e chave descasados, então
o par novo é validado antes num contexto descartável e só depois aplicado ao contexto vivo.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path

log = logging.getLogger(__name__)


class TLSError(Exception):
    """Certificado ou chave inutilizáveis (mensagem pronta para o usuário)."""


def _new_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def _load_into(ctx: ssl.SSLContext, certfile: Path, keyfile: Path) -> None:
    for label, path in (("certificado", certfile), ("chave", keyfile)):
        if not path.is_file():
            raise TLSError(f"arquivo do {label} não encontrado: {path}")
    try:
        ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    except ssl.SSLError as e:
        reason = (e.reason or str(e)).upper()
        if "KEY_VALUES_MISMATCH" in reason:
            raise TLSError(f"a chave {keyfile} não corresponde ao certificado {certfile}") from e
        raise TLSError(f"certificado ou chave inválidos ({certfile}, {keyfile}): {e}") from e
    except OSError as e:
        raise TLSError(f"não consegui ler {certfile} / {keyfile}: {e.strerror or e}") from e


class ReloadingTLS:
    def __init__(self, certfile: Path, keyfile: Path) -> None:
        self.certfile = Path(certfile)
        self.keyfile = Path(keyfile)
        self.context: ssl.SSLContext | None = None
        self._stamp: tuple[float, float] | None = None

    def _current_stamp(self) -> tuple[float, float] | None:
        try:
            return (self.certfile.stat().st_mtime, self.keyfile.stat().st_mtime)
        except OSError:
            return None

    def load(self) -> ssl.SSLContext:
        """Carga inicial. Levanta TLSError com mensagem legível."""
        ctx = _new_context()
        _load_into(ctx, self.certfile, self.keyfile)
        self.context = ctx
        self._stamp = self._current_stamp()
        return ctx

    def reload_if_changed(self) -> bool:
        """Recarrega se os arquivos mudaram. Devolve True se aplicou um certificado novo."""
        stamp = self._current_stamp()
        if self.context is None or stamp is None or stamp == self._stamp:
            return False
        try:
            _load_into(_new_context(), self.certfile, self.keyfile)  # valida antes
            _load_into(self.context, self.certfile, self.keyfile)
        except TLSError as e:
            log.warning("Certificado novo ignorado, mantendo o anterior: %s", e)
            self._stamp = stamp  # não insiste no mesmo arquivo ruim a cada ciclo
            return False
        self._stamp = stamp
        log.info("Certificado HTTPS recarregado (%s)", self.certfile)
        return True

    async def watch(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self.reload_if_changed()
