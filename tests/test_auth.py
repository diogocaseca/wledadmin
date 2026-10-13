import pytest
from sqlalchemy import select

from wledadmin.models import AdminUser
from wledadmin.services.auth import AuthService, CredentialError


@pytest.fixture
async def auth(session_factory):
    svc = AuthService(session_factory)
    await svc.ensure_default_admin()
    return svc


async def test_default_admin_is_hashed(auth, session_factory):
    async with session_factory() as s:
        user = await s.scalar(select(AdminUser))
    assert user.username == "admin" and user.is_default
    assert user.password_hash.startswith("$argon2id$")
    assert "admin" not in user.password_hash.removeprefix("$argon2id$")


async def test_verify(auth):
    assert await auth.verify("admin", "admin")
    assert not await auth.verify("admin", "wrong")
    assert not await auth.verify("root", "admin")


async def test_ensure_default_is_idempotent(auth, session_factory):
    await auth.ensure_default_admin()
    async with session_factory() as s:
        assert len((await s.scalars(select(AdminUser))).all()) == 1


async def test_change_credentials_and_cache_invalidation(auth):
    assert await auth.verify("admin", "admin")  # popula o cache
    await auth.change_credentials("admin", "diogo", "s3nh4-forte")
    assert not await auth.verify("admin", "admin")
    assert await auth.verify("diogo", "s3nh4-forte")
    assert not (await auth.get_admin()).is_default


@pytest.mark.parametrize(
    ("current", "user", "new", "msg"),
    [("errada", "admin", "s3nh4-forte", "senha atual"),
     ("admin", "admin", "curta", "pelo menos"),
     ("admin", "", "s3nh4-forte", "usuário"),
     ("admin", "a:b", "s3nh4-forte", "usuário")],
)
async def test_change_credentials_validation(auth, current, user, new, msg):
    with pytest.raises(CredentialError, match=msg):
        await auth.change_credentials(current, user, new)


async def test_reset_to_default(auth):
    await auth.change_credentials("admin", "diogo", "s3nh4-forte")
    await auth.reset_to_default()
    assert await auth.verify("admin", "admin")
