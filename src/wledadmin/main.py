"""App factory do FastAPI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings
from .services.container import Services
from .web import api, backups, firmware, pages
from .web.ingress import IngressMiddleware
from .web.security import SameOriginMiddleware


def create_app(
    settings: Settings | None = None,
    *,
    start_background: bool = True,
    wled_transport: httpx.AsyncBaseTransport | None = None,
    github_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """`start_background=False` e os transportes existem para os testes."""
    settings = settings or Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # Uma linha por requisição a cada ciclo de polling é ruído.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services = await Services.create(
            settings, transport=wled_transport, github_transport=github_transport
        )
        app.state.services = services
        if start_background:
            await services.start_background()
        try:
            yield
        finally:
            await services.close()

    app = FastAPI(title="wledadmin", version=__version__, lifespan=lifespan)
    app.add_middleware(SameOriginMiddleware)
    # Por último = mais externo: define root_path/estado antes da checagem de origem.
    app.add_middleware(
        IngressMiddleware,
        enabled=settings.ingress,
        proxy_ip=settings.ingress_proxy_ip,
        ingress_port=settings.ingress_port,
    )
    # Arquivos estáticos (htmx, css) são públicos: não contêm nada sensível.
    app.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static"
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    app.include_router(pages.router)
    app.include_router(backups.router)
    app.include_router(firmware.router)
    app.include_router(firmware.api_router)
    app.include_router(api.router)
    return app
