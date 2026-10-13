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
            raise WLEDUnreachable(f"{host}: {describe_transport_error(e)}") from e
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
        """Lê e decodifica um arquivo JSON do sistema de arquivos do WLED."""
        _check_file_name(name)
        return await self._request("GET", host, f"/{name}")

    # ---- arquivos (backup/restore) ---------------------------------------------------
    #
    # Conferido em wled00/file.cpp e wled_server.cpp (0.14.4, 0.15.0, main):
    # - GET /<arquivo> é servido direto do FS, sem PIN; caminhos contendo "sec"
    #   (wsec.json, com as senhas) nunca são servidos.
    # - POST /upload (multipart, campo "data", filename = "/cfg.json") exige correctPIN.
    #   Upload de cfg.json faz o dispositivo reiniciar sozinho.
    # - Enviar {"pin": "..."} num POST JSON chama checkSettingsPIN(); o desbloqueio é
    #   global no dispositivo e dura 15 min. GET /settings/lock bloqueia de novo.

    async def download_file(
        self, host: str, name: str, *, max_bytes: int = 1024 * 1024, timeout: float = 15.0
    ) -> bytes | None:
        """Bytes crus do arquivo (para hash/backup). None se o arquivo não existir."""
        _check_file_name(name)
        url = f"http://{host}/{name}"
        try:
            async with self._http.stream(
                "GET", url, timeout=httpx.Timeout(timeout, connect=min(timeout, 3.0))
            ) as resp:
                if resp.status_code == 404:
                    return None
                if resp.status_code == 401:
                    raise WLEDAuthRequired(f"{host}: autenticação exigida")
                if resp.status_code >= 400:
                    raise WLEDHTTPError(resp.status_code, f"{host}/{name}: HTTP {resp.status_code}")
                chunks, total = [], 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise WLEDBadResponse(f"{host}/{name}: maior que {max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.TimeoutException as e:
            raise WLEDTimeout(f"{host}: timeout") from e
        except httpx.TransportError as e:
            raise WLEDUnreachable(f"{host}: {describe_transport_error(e)}") from e

    async def upload_file(
        self, host: str, name: str, content: bytes, *, timeout: float = 20.0
    ) -> str:
        """Envia um arquivo via POST /upload. Retorna o texto de resposta do WLED."""
        _check_file_name(name)
        files = {"data": (f"/{name}", content, "application/json")}
        try:
            resp = await self._http.post(
                f"http://{host}/upload",
                files=files,
                timeout=httpx.Timeout(timeout, connect=min(timeout, 3.0)),
            )
        except httpx.TimeoutException as e:
            raise WLEDTimeout(f"{host}: timeout no upload") from e
        except httpx.TransportError as e:
            raise WLEDUnreachable(f"{host}: {describe_transport_error(e)}") from e
        if resp.status_code == 401:
            raise WLEDAuthRequired(f"{host}: PIN de configurações necessário ou incorreto")
        if resp.status_code >= 400:
            raise WLEDHTTPError(resp.status_code, f"{host}: upload HTTP {resp.status_code}")
        return resp.text.strip()

    async def unlock_settings(self, host: str, pin: str) -> None:
        """Desbloqueia as configurações com o PIN. O WLED não diz se o PIN estava certo;
        um PIN errado aparece como 401 na operação seguinte."""
        await self._request("POST", host, "/json/state", json={"pin": pin})

    async def lock_settings(self, host: str) -> None:
        try:
            await self._http.get(f"http://{host}/settings/lock")
        except httpx.HTTPError as e:
            raise WLEDUnreachable(f"{host}: {describe_transport_error(e)}") from e

    async def wait_for_reboot(
        self,
        host: str,
        mac: str,
        *,
        timeout: float = 90.0,
        initial_delay: float = 3.0,
        interval: float = 2.0,
    ) -> DeviceInfo:
        """Espera o dispositivo voltar depois de um reboot.

        Considera "voltou" quando responde com o mesmo MAC e uptime menor que o tempo
        decorrido (prova de que reiniciou, e não que respondeu antes de reiniciar).
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.sleep(initial_delay)
        last_error: Exception | None = None
        while loop.time() - start < timeout:
            try:
                info = await self.get_info(host, timeout=3.0)
            except WLEDError as e:
                last_error = e
            else:
                elapsed = loop.time() - start
                if info.mac_normalized == mac and (
                    info.uptime is None or info.uptime <= elapsed + 5
                ):
                    return info
            await asyncio.sleep(interval)
        raise WLEDTimeout(
            f"{host}: não voltou em {timeout:.0f} s"
            + (f" (último erro: {last_error})" if last_error else "")
        )


def describe_transport_error(e: Exception) -> str:
    if isinstance(e, httpx.ConnectError):
        return "sem conexão (desligado ou fora da rede)"
    if isinstance(e, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return "conexão interrompida"
    return type(e).__name__


FILE_NAMES = frozenset({"cfg.json", "presets.json"})


def _check_file_name(name: str) -> None:
    if name not in FILE_NAMES:
        raise ValueError(f"arquivo não suportado: {name}")
