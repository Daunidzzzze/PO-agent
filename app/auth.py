"""Вход без паролей по кодам (§10) + логин панели (§12).

Кука подписана HMAC — отдельная библиотека не нужна.
"""
import hmac
import time
from collections import defaultdict, deque
from hashlib import sha256

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config
from .db import get_session
from .models import Team, User, utcnow

STUDENT_COOKIE = "po_session"
ADMIN_COOKIE = "po_admin"


def sign(value: str) -> str:
    mac = hmac.new(config.SECRET_KEY.encode(), value.encode(), sha256).hexdigest()[:32]
    return f"{value}.{mac}"


def unsign(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    value, _, mac = token.rpartition(".")
    if not hmac.compare_digest(sign(value), token):
        return None
    return value


def make_student_cookie(user_id: int, epoch: int = 0) -> str:
    exp = int(time.time()) + config.AUTH_COOKIE_DAYS * 86400
    return sign(f"{user_id}:{exp}:{epoch}")


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    raw = unsign(request.cookies.get(STUDENT_COOKIE))
    if not raw:
        raise HTTPException(401, "not authenticated")
    parts = raw.split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        raise HTTPException(401, "bad session")
    uid, exp, epoch = int(parts[0]), int(parts[1]), int(parts[2])
    if exp < time.time():
        raise HTTPException(401, "session expired")
    user = await session.get(User, uid)
    if not user or not user.is_active:
        raise HTTPException(401, "user disabled")
    if user.session_epoch != epoch:
        raise HTTPException(401, "session revoked")
    user.last_seen_at = utcnow()
    await session.commit()
    return user


async def optional_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User | None:
    try:
        return await current_user(request, session)
    except HTTPException:
        return None


def require_admin(request: Request) -> str:
    login = unsign(request.cookies.get(ADMIN_COOKIE))
    if login != config.ADMIN_LOGIN:
        raise HTTPException(401, "admin auth required")
    return login


_attempts: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request, limit: int = 10, window: int = 60) -> None:
    """§16: перебор кодов входа. ponytail: счётчик в памяти процесса —
    для одной реплики достаточно; на нескольких нужен Redis."""
    ip = (request.headers.get("x-real-ip")
          or (request.client.host if request.client else "?"))
    now = time.time()
    hits = _attempts[ip]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        raise HTTPException(429, "Слишком много попыток входа. Подождите минуту.")
    hits.append(now)


async def user_by_codes(session: AsyncSession, team_code: str, user_code: str) -> User | None:
    """Персональный код действителен только вместе со своим кодом команды —
    иначе участник команды A вошёл бы, назвав код команды B."""
    q = (
        select(User)
        .join(Team, Team.id == User.team_id)
        .where(
            User.user_code == user_code.strip(),
            func.lower(Team.code) == team_code.strip().lower(),
            User.is_active.is_(True),
            Team.is_active.is_(True),
        )
    )
    return (await session.execute(q)).scalars().first()
