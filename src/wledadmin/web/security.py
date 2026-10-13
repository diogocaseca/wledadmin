"""HTTP Basic obrigatório + proteção CSRF.

O navegador reenvia a credencial Basic automaticamente para qualquer requisição ao host,
inclusive as disparadas por outro site. Por isso métodos que alteram estado exigem que
Origin (ou Referer) seja o próprio painel. Clientes não-navegador (curl, scripts) não
mandam esses cabeçalhos e passam normalmente, ainda autenticados.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from ..services.container import Services
from .ingress import is_ingress

REALM = "wledadmin"
_basic = HTTPBasic(realm=REALM, auto_error=False)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]


INGRESS_USER = "Home Assistant"


async def require_admin(
    request: Request,
    services: ServicesDep,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> str:
    if is_ingress(request):
        return INGRESS_USER  # já autenticado pelo Home Assistant
    if credentials is not None and await services.auth.verify(
        credentials.username, credentials.password
    ):
        return credentials.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Autenticação necessária",
        headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
    )


AdminDep = Annotated[str, Depends(require_admin)]


class SameOriginMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Pelo Ingress, a origem é o endereço do HA, e o próprio HA já exige a sessão do
        # usuário para chegar até aqui.
        if request.method not in SAFE_METHODS and not is_ingress(request):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin is not None:
                allowed = {
                    request.headers.get("host", ""),
                    request.headers.get("x-forwarded-host", ""),  # atrás de reverse proxy
                } - {""}
                if origin == "null" or urlsplit(origin).netloc not in allowed:
                    return PlainTextResponse("Origem não permitida", status_code=403)
        return await call_next(request)
