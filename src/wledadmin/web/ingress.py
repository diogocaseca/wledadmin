"""Suporte ao Ingress do Home Assistant (painel na barra lateral do HA).

Pelo Ingress, o Supervisor do HA faz proxy das requisições a partir do IP 172.30.32.2,
remove o prefixo `/api/hassio_ingress/<token>` do caminho e o informa no cabeçalho
`X-Ingress-Path`. O usuário já foi autenticado pelo HA (a documentação do HA pede que o
app não peça login de novo).

Uma requisição só é tratada como Ingress se as três condições valerem: o Ingress está
habilitado (WLEDADMIN_INGRESS), a conexão vem do IP do proxy e o cabeçalho está presente.
Nesse caso o prefixo vira o `root_path` do ASGI, e todas as URLs geradas o incluem.
"""

from __future__ import annotations

import logging
import re

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

_PREFIX_RE = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_\-]+$")

DISABLED_PAGE = """<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<title>wledadmin</title>
<body style="font:15px system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px">
<h1 style="font-size:20px">Acesso pelo painel do Home Assistant desativado</h1>
<p>O Ingress está desligado nas opções do app WLED Admin. Use o acesso direto
(<code>http://&lt;endereço-do-home-assistant&gt;:&lt;porta&gt;</code>, com a porta da seção
<strong>Rede</strong> do app, padrão 8000) com o usuário e a senha do WLED Admin, ou ligue
a opção <strong>ingress</strong> na aba Configuração do app.</p></body></html>"""


ONLY_PANEL_PAGE = """<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<title>wledadmin</title>
<body style="font:15px system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px">
<h1 style="font-size:20px">Porta reservada ao painel do Home Assistant</h1>
<p>Esta porta só atende o painel do WLED Admin na barra lateral do Home Assistant. Para
acesso direto, use a porta configurada na seção <strong>Rede</strong> do app.</p>
</body></html>"""


class IngressMiddleware:
    """Decide, por requisição, se ela veio pelo Ingress do HA.

    Com `ingress_port` (App do HA): a porta do Ingress só aceita o proxy do HA; na porta
    de acesso direto o cabeçalho é sempre ignorado. Sem `ingress_port` (uma porta só):
    vale a regra de origem + cabeçalho.
    """

    def __init__(self, app: ASGIApp, *, enabled: bool, proxy_ip: str,
                 ingress_port: int | None = None) -> None:
        self.app = app
        self.enabled = enabled
        self.proxy_ip = proxy_ip
        self.ingress_port = ingress_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        raw = next((v for k, v in scope["headers"] if k == b"x-ingress-path"), None)
        client = scope.get("client")
        from_proxy = bool(client) and client[0] == self.proxy_ip
        server = scope.get("server")
        on_ingress_port = (self.ingress_port is not None and server is not None
                           and server[1] == self.ingress_port)

        if self.ingress_port is not None and not on_ingress_port:
            # Porta de acesso direto: nunca é Ingress, venha o cabeçalho de onde vier.
            await self.app(scope, receive, send)
            return

        if on_ingress_port and not (from_proxy and raw is not None):
            log.warning("Porta do Ingress acessada fora do proxy do HA (origem %s)", client)
            await self._reply(scope, receive, send, ONLY_PANEL_PAGE)
            return

        if raw is not None and from_proxy:
            if not self.enabled:
                await self._reply(scope, receive, send, DISABLED_PAGE)
                return
            prefix = raw.decode("latin-1").rstrip("/")
            if not _PREFIX_RE.match(prefix):
                log.warning("X-Ingress-Path inesperado recusado: %r", prefix)
                if on_ingress_port:
                    await self._reply(scope, receive, send, ONLY_PANEL_PAGE)
                    return
            else:
                # O Starlette espera que `path` já inclua o `root_path` (como o uvicorn
                # faz com --root-path); o Supervisor entrega o caminho sem o prefixo.
                scope["root_path"] = prefix
                scope["path"] = prefix + scope["path"]
                if scope.get("raw_path") is not None:
                    scope["raw_path"] = prefix.encode() + scope["raw_path"]
                scope.setdefault("state", {})["ingress"] = True
        elif raw is not None:
            # Cabeçalho vindo de fora do proxy do HA: não é confiável, ignoramos.
            log.debug("X-Ingress-Path ignorado (origem %s)", client)
        await self.app(scope, receive, send)

    @staticmethod
    async def _reply(scope: Scope, receive: Receive, send: Send, page: str) -> None:
        if scope["type"] == "http":
            await HTMLResponse(page, status_code=403)(scope, receive, send)


def is_ingress(scope_or_request: Scope | Request) -> bool:
    scope = scope_or_request.scope if isinstance(scope_or_request, Request) else scope_or_request
    return bool(scope.get("state", {}).get("ingress"))


def root(request: Request) -> str:
    """Prefixo para URLs internas ("" fora do Ingress)."""
    return request.scope.get("root_path", "")


def redirect(request: Request, path: str) -> RedirectResponse:
    return RedirectResponse(root(request) + path, status_code=303)
