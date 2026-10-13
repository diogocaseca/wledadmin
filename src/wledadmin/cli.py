"""Linha de comando: `wledadmin serve` e `wledadmin reset-admin`."""

from __future__ import annotations

import argparse
import asyncio
import signal
import socket
import sys

from .config import Settings
from .tls import ReloadingTLS, TLSError

DIRECT = "acesso direto"
PANEL = "painel do Home Assistant"


def open_listeners(
    settings: Settings, *, direct: bool = True
) -> list[tuple[str, socket.socket]]:
    """Abre os sockets de escuta: acesso direto (WLEDADMIN_PORT, 0 = desligado) e, como
    App do Home Assistant, a porta do Ingress (WLEDADMIN_INGRESS_PORT).
    """
    wanted: list[tuple[str, int]] = []
    if direct and settings.port:
        wanted.append((DIRECT, settings.port))
    if settings.ingress_port and settings.ingress_port != settings.port:
        wanted.append((PANEL, settings.ingress_port))
    if not wanted:
        raise SystemExit("Nenhuma porta para escutar: defina WLEDADMIN_PORT ou "
                         "WLEDADMIN_INGRESS_PORT.")
    opened = []
    for name, port in wanted:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((settings.host, port))
        except OSError as e:
            sock.close()
            for _, s in opened:
                s.close()
            raise SystemExit(f"Não consegui abrir a porta {port} ({name}): {e.strerror}. "
                             "Ela pode estar em uso por outro serviço.") from e
        sock.set_inheritable(True)
        opened.append((name, sock))
    return opened


def prepare_tls(settings: Settings) -> ReloadingTLS | None:
    """Carrega o certificado da porta direta (levanta TLSError se não der)."""
    if not settings.port or not settings.ssl_certfile:
        return None
    tls = ReloadingTLS(settings.ssl_certfile, settings.ssl_keyfile or settings.ssl_certfile)
    tls.load()
    return tls


def _serve(settings: Settings) -> None:
    direct, tls = True, None
    try:
        tls = prepare_tls(settings)
    except TLSError as e:
        if not settings.ingress_port:
            raise SystemExit(f"HTTPS: {e}") from e
        # Como App do HA: melhor manter o painel no ar do que derrubar tudo.
        print(f"wledadmin: HTTPS: {e}. O acesso direto NÃO foi iniciado; o painel do Home "
              "Assistant continua funcionando.", file=sys.stderr, flush=True)
        direct = False
    listeners = open_listeners(settings, direct=direct)
    for name, sock in listeners:
        scheme = "https" if name == DIRECT and tls else "http"
        print(f"wledadmin: {name} na porta {sock.getsockname()[1]} ({scheme})", flush=True)
    # O uvicorn trata SIGTERM/SIGINT (desligamento limpo) e, ao terminar, devolve o sinal
    # ao tratador anterior. Com o tratador padrão do sistema, o processo morreria nesse
    # momento com código 143, e o Supervisor do HA reclama de app que "não trata SIGTERM".
    # Um tratador vazio aqui faz o processo terminar normalmente (código 0).
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: None)
    asyncio.run(serve_listeners(settings, listeners, tls))


async def serve_listeners(
    settings: Settings,
    listeners: list[tuple[str, socket.socket]],
    tls: ReloadingTLS | None,
) -> None:
    """Um servidor uvicorn por porta, todos com o MESMO app (status, jobs e banco
    compartilhados). O TLS do uvicorn vale para todos os sockets de um servidor, e a porta
    do painel precisa continuar em HTTP para o proxy do HA, daí um servidor por porta.

    O primeiro servidor roda a inicialização (lifespan); os outros só abrem depois que ela
    termina. No desligamento, o uvicorn repassa o sinal de um servidor para o anterior.
    """
    import uvicorn

    from .main import create_app

    app = create_app(settings)
    servers: list[tuple[uvicorn.Server, socket.socket]] = []
    for i, (name, sock) in enumerate(listeners):
        factory = None
        if name == DIRECT and tls is not None:
            def factory(_config, _default, _tls=tls):
                return _tls.context
        config = uvicorn.Config(
            app,
            log_level=settings.log_level.lower(),
            proxy_headers=True,
            lifespan="on" if i == 0 else "off",
            ssl_context_factory=factory,
        )
        servers.append((uvicorn.Server(config), sock))

    first, first_sock = servers[0]
    tasks = [asyncio.create_task(first.serve(sockets=[first_sock]))]
    while not first.started:
        if tasks[0].done():  # a inicialização falhou
            await tasks[0]
            return
        await asyncio.sleep(0.05)
    for server, sock in servers[1:]:
        tasks.append(asyncio.create_task(server.serve(sockets=[sock])))

    watcher = (asyncio.create_task(tls.watch(settings.ssl_reload_interval))
               if tls is not None else None)
    try:
        await asyncio.gather(*tasks)
    finally:
        if watcher is not None:
            watcher.cancel()


async def _reset_admin(settings: Settings) -> None:
    from .db import create_engine, create_session_factory, run_migrations
    from .services.auth import AuthService

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await run_migrations(settings.db_url)
    engine = create_engine(settings.db_url)
    try:
        await AuthService(create_session_factory(engine)).reset_to_default()
    finally:
        await engine.dispose()
    print("Credenciais redefinidas para admin/admin. Troque a senha no painel.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="wledadmin")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="inicia o servidor web (padrão)")
    sub.add_parser("reset-admin", help="redefine o login para admin/admin")
    args = parser.parse_args(argv)

    settings = Settings()
    if args.cmd == "reset-admin":
        asyncio.run(_reset_admin(settings))
    else:
        _serve(settings)


if __name__ == "__main__":
    main()
