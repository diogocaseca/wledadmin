"""Cliente assíncrono para a API HTTP/JSON do WLED.

É o único ponto do projeto que fala com os dispositivos. Polling, ações em massa e,
nas próximas fases, backup (/cfg.json, /presets.json) e OTA (/update) passam por aqui.

Referência: https://kno.wled.ge/interfaces/json-api/ e wled00/json.cpp.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .errors import (
    WLEDAuthRequired,
    WLEDBadResponse,
    WLEDError,
    WLEDHTTPError,
    WLEDTimeout,
    WLEDUnreachable,
)
from .schemas import DeviceInfo, DeviceState


def build_http_client(
    connect_timeout: float,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Cria o httpx.AsyncClient compartilhado.

    Keep-alive desligado de propósito: o ESPAsyncWebServer tem pouquíssimos sockets
    (especialmente no ESP8266) e uma conexão ociosa nossa pode tirar a vaga do Home
    Assistant ou da UI do próprio WLED. Abrir uma conexão a cada 10 s custa quase nada.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=connect_timeout),
        limits=httpx.Limits(max_connections=128, max_keepalive_connections=0),
        headers={"Connection": "close", "User-Agent": "wledadmin"},
        follow_redirects=False,
        transport=transport,
    )


class WLEDClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    # ---- baixo nível -------------------------------------------------------------

    async def _request(
        self,
        method: str,
        host: str,
        path: str,
        *,
        json: Any = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"http://{host}{path}"
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=min(timeout, 2.0))
        try:
            resp = await self._http.request(method, url, **kwargs)
        except httpx.TimeoutException as e:
            raise WLEDTimeout(f"{host}: timeout") from e
        except httpx.TransportError as e:
            raise WLEDUnreachable(f"{host}: {type(e).__name__}") from e
        except httpx.HTTPError as e:  # pragma: no cover - salvaguarda
            raise WLEDError(f"{host}: {e}") from e

        if resp.status_code == 401:
            raise WLEDAuthRequired(f"{host}: autenticação exigida")
        if resp.status_code >= 400:
            raise WLEDHTTPError(resp.status_code, f"{host}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise WLEDBadResponse(f"{host}: resposta não é JSON") from e

    async def _get_obj(self, host: str, path: str, timeout: float | None = None) -> dict:
        data = await self._request("GET", host, path, timeout=timeout)
        if not isinstance(data, dict):
            raise WLEDBadResponse(f"{host}{path}: JSON inesperado")
        return data

    # ---- API pública -------------------------------------------------------------

    async def get_info(self, host: str, *, timeout: float | None = None) -> DeviceInfo:
        return DeviceInfo.model_validate(await self._get_obj(host, "/json/info", timeout))

    async def get_state(self, host: str) -> DeviceState:
        return DeviceState.model_validate(await self._get_obj(host, "/json/state"))

    async def get_state_info(self, host: str) -> tuple[DeviceState, DeviceInfo]:
        """Estado + info numa única requisição via /json/si.

        Se o firmware não tiver /json/si (404), cai para duas requisições.
        """
        try:
            data = await self._get_obj(host, "/json/si")
        except WLEDHTTPError as e:
            if e.status_code != 404:
                raise
            state, info = await asyncio.gather(self.get_state(host), self.get_info(host))
            return state, info
        if not isinstance(data.get("info"), dict):
            raise WLEDBadResponse(f"{host}/json/si: sem objeto 'info'")
        return (
            DeviceState.model_validate(data.get("state") or {}),
            DeviceInfo.model_validate(data["info"]),
        )

    async def set_state(self, host: str, payload: dict[str, Any]) -> DeviceState | None:
        """POST /json/state com "v": true para receber o estado completo de volta.

        Retorna None se o firmware responder só {"success": true}.
        """
        data = await self._request("POST", host, "/json/state", json={**payload, "v": True})
        if not isinstance(data, dict):
            raise WLEDBadResponse(f"{host}/json/state: JSON inesperado")
        if isinstance(data.get("state"), dict):
            data = data["state"]
        if "on" in data or "bri" in data:
            return DeviceState.model_validate(data)
        return None

    async def get_json_file(self, host: str, name: str) -> Any:
        """Lê um arquivo JSON do sistema de arquivos do WLED (ex.: cfg.json, presets.json).

        Base para o backup da fase 2.
        """
        if name not in {"cfg.json", "presets.json"}:
            raise ValueError(f"arquivo não suportado: {name}")
        return await self._request("GET", host, f"/{name}")
