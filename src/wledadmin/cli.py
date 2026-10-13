"""Linha de comando: `wledadmin serve` e `wledadmin reset-admin`."""

from __future__ import annotations

import argparse
import asyncio
import socket

from .config import Settings


def open_listeners(settings: Settings) -> list[tuple[str, socket.socket]]:
    """Abre os sockets de escuta: acesso direto (WLEDADMIN_PORT, 0 = desligado) e, como
    App do Home Assistant, a porta do Ingress (WLEDADMIN_INGRESS_PORT).

    Os dois ficam no mesmo processo e no mesmo servidor: status, jobs e banco são
    compartilhados, e a inicialização (lifespan) roda uma vez só.
    """
    wanted: list[tuple[str, int]] = []
    if settings.port:
        wanted.append(("acesso direto", settings.port))
    if settings.ingress_port and settings.ingress_port != settings.port:
        wanted.append(("painel do Home Assistant", settings.ingress_port))
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


def _serve(settings: Settings) -> None:
    import uvicorn

    listeners = open_listeners(settings)
    for name, sock in listeners:
        print(f"wledadmin: {name} na porta {sock.getsockname()[1]}", flush=True)
    # Um único worker: o status e os jobs vivem em memória no processo.
    config = uvicorn.Config(
        "wledadmin.main:create_app",
        factory=True,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
    )
    uvicorn.Server(config).run(sockets=[sock for _, sock in listeners])


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
