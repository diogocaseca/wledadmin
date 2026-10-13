"""Credencial de administrador do painel (HTTP Basic obrigatório).

- No primeiro boot cria admin/admin, marcado como padrão (a UI mostra um aviso).
- A senha é guardada como hash Argon2id. Não é reversível, nem por quem tem o banco.
- Verificar Argon2 custa dezenas de ms e o HTTP Basic reenvia a credencial a cada
  requisição (inclusive no polling HTMX). Por isso guardamos um cache curto, em memória,
  de credenciais já verificadas: um HMAC com chave aleatória por processo, nunca a senha.
  O cache é limpo quando a credencial muda.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..models import AdminUser, utcnow

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
MIN_PASSWORD_LEN = 8
CACHE_TTL = 300.0


class CredentialError(ValueError):
    pass


class AuthService:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory
        self._ph = PasswordHasher()  # Argon2id com parâmetros recomendados
        self._dummy_hash = self._ph.hash(secrets.token_hex(16))
        self._cache_key = secrets.token_bytes(32)
        self._cache: dict[bytes, float] = {}

    def _fingerprint(self, username: str, password: str) -> bytes:
        msg = f"{username}\x00{password}".encode()
        return hmac.new(self._cache_key, msg, hashlib.sha256).digest()

    async def ensure_default_admin(self) -> None:
        async with self._sf() as s:
            if await s.scalar(select(func.count()).select_from(AdminUser)):
                return
            s.add(AdminUser(
                username=DEFAULT_USERNAME,
                password_hash=self._ph.hash(DEFAULT_PASSWORD),
                is_default=True,
                updated_at=utcnow(),
            ))
            await s.commit()

    async def reset_to_default(self) -> None:
        async with self._sf() as s:
            for user in (await s.scalars(select(AdminUser))).all():
                await s.delete(user)
            await s.commit()
        self._cache.clear()
        await self.ensure_default_admin()

    async def get_admin(self) -> AdminUser | None:
        async with self._sf() as s:
            return await s.scalar(select(AdminUser).order_by(AdminUser.id).limit(1))

    async def verify(self, username: str, password: str) -> bool:
        fp = self._fingerprint(username, password)
        exp = self._cache.get(fp)
        if exp is not None and exp > time.monotonic():
            return True

        user = await self.get_admin()
        # Usuário errado também passa por um verify (tempo constante, sem enumeração).
        stored = user.password_hash if user else self._dummy_hash
        try:
            self._ph.verify(stored, password)
            ok = user is not None and hmac.compare_digest(
                user.username.encode(), username.encode()
            )
        except (VerificationError, InvalidHashError):
            ok = False
        if ok:
            self._cache[fp] = time.monotonic() + CACHE_TTL
            if self._ph.check_needs_rehash(stored):
                await self._update(user.id, user.username, self._ph.hash(password), user.is_default)
        return ok

    async def change_credentials(
        self, current_password: str, new_username: str, new_password: str
    ) -> None:
        user = await self.get_admin()
        if user is None:
            raise CredentialError("nenhum administrador cadastrado")
        try:
            self._ph.verify(user.password_hash, current_password)
        except (VerificationError, InvalidHashError):
            raise CredentialError("senha atual incorreta") from None

        new_username = new_username.strip()
        if not new_username or len(new_username) > 64 or ":" in new_username:
            raise CredentialError("usuário inválido (1–64 caracteres, sem ':')")
        if len(new_password) < MIN_PASSWORD_LEN:
            raise CredentialError(
                f"a nova senha precisa de pelo menos {MIN_PASSWORD_LEN} caracteres"
            )
        if new_password == DEFAULT_PASSWORD:
            raise CredentialError("escolha uma senha diferente da padrão")

        await self._update(user.id, new_username, self._ph.hash(new_password), False)
        self._cache.clear()

    async def _update(self, user_id: int, username: str, pw_hash: str, is_default: bool) -> None:
        async with self._sf() as s:
            user = await s.get(AdminUser, user_id)
            user.username = username
            user.password_hash = pw_hash
            user.is_default = is_default
            user.updated_at = utcnow()
            await s.commit()
