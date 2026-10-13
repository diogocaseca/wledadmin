"""Linha de comando: `wledadmin serve` e `wledadmin reset-admin`."""

from __future__ import annotations

import argparse
import asyncio

from .config import Settings


def _serve(settings: Settings) -> None:
    import uvicorn

    # Um único worker: o status e os jobs vivem em memória no processo.
    uvicorn.run(
        "wledadmin.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
    )


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
