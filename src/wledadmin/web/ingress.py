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
<p>O Ingress está desligado nas opções do app wledadmin. Use o acesso direto pela porta do
wledadmin (por padrão <code>http://&lt;endereço-do-home-assistant&gt;:8000</code>), com o
usuário e a senha do wledadmin, ou ligue a opção <strong>ingress</strong> na aba
Configuração do app.</p></body></html>"""


class IngressMiddleware:
    def __init__(self, app: ASGIApp, *, enabled: bool, proxy_ip: str) -> None:
        self.app = app
        self.enabled = enabled
        self.proxy_ip = proxy_ip

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            raw = next((v for k, v in scope["headers"] if k == b"x-ingress-path"), None)
            client = scope.get("client")
            if raw is not None and client and client[0] == self.proxy_ip:
                if not self.enabled:
                    if scope["type"] == "http":
                        await HTMLResponse(DISABLED_PAGE, status_code=403)(scope, receive, send)
                    return
                prefix = raw.decode("latin-1").rstrip("/")
                if not _PREFIX_RE.match(prefix):
                    log.warning("X-Ingress-Path inesperado ignorado: %r", prefix)
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


def is_ingress(scope_or_request: Scope | Request) -> bool:
    scope = scope_or_request.scope if isinstance(scope_or_request, Request) else scope_or_request
    return bool(scope.get("state", {}).get("ingress"))


def root(request: Request) -> str:
    """Prefixo para URLs internas ("" fora do Ingress)."""
    return request.scope.get("root_path", "")


def redirect(request: Request, path: str) -> RedirectResponse:
    return RedirectResponse(root(request) + path, status_code=303)
