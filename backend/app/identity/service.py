"""Authentication: password hashing, login with rate limit, revocable sessions (invariant I6)."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, events
from app.core.config import Settings
from app.core.db import utcnow
from app.identity.models import LoginAttempt, Role, User, UserSession

_hasher = PasswordHasher()
# Verified against when the e-mail is unknown, so response time does not reveal valid e-mails.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

MIN_PASSWORD_LENGTH = 12


class AuthError(Exception):
    pass


class RateLimited(AuthError):
    pass


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must have at least {MIN_PASSWORD_LENGTH} characters.")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalise_email(email: str) -> str:
    return email.strip().lower()


@dataclass(frozen=True)
class IssuedSession:
    session: UserSession
    token: str


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    role: Role = Role.owner,
    actor: audit.Actor,
) -> User:
    user = User(
        email=normalise_email(email),
        display_name=display_name,
        password_hash=hash_password(password),
        role=role,
    )
    session.add(user)
    await session.flush()
    audit.record(
        session,
        actor,
        "user.created",
        entity_type="user",
        entity_id=user.id,
        after={"email": user.email, "role": role.value},
    )
    return user


async def _recent_failures(session: AsyncSession, settings: Settings, **where: str | None) -> int:
    since = utcnow() - timedelta(minutes=settings.login_window_minutes)
    stmt = select(func.count()).where(LoginAttempt.at >= since, LoginAttempt.success.is_(False))
    for column, value in where.items():
        stmt = stmt.where(getattr(LoginAttempt, column) == value)
    return int(await session.scalar(stmt) or 0)


async def login(
    session: AsyncSession,
    settings: Settings,
    *,
    email: str,
    password: str,
    ip: str | None,
    user_agent: str | None,
) -> IssuedSession:
    """Verify credentials and open a session. Commits the attempt record either way."""
    email = normalise_email(email)
    if await _recent_failures(
        session, settings, email=email
    ) >= settings.login_max_failures_per_email or (
        ip
        and await _recent_failures(session, settings, ip=ip) >= settings.login_max_failures_per_ip
    ):
        raise RateLimited("Too many failed attempts. Try again in a few minutes.")

    user = await session.scalar(select(User).where(User.email == email))
    valid = verify_password(user.password_hash if user else _DUMMY_HASH, password)
    ok = bool(user and user.is_active and valid)
    session.add(LoginAttempt(email=email, ip=ip, success=ok))

    if not ok or user is None:
        events.publish(session, "auth.login_failed", {"email": email, "ip": ip})
        audit.record(
            session,
            audit.Actor(label=f"anonymous:{email}", ip=ip),
            "auth.login_failed",
            entity_type="user",
            entity_id=user.id if user else None,
        )
        await session.commit()
        raise AuthError("Invalid e-mail or password.")

    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)

    token = secrets.token_urlsafe(32)
    now = utcnow()
    user_session = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=settings.session_max_age_days),
        ip=ip,
        user_agent=(user_agent or "")[:400] or None,
    )
    session.add(user_session)
    await session.flush()
    audit.record(
        session,
        audit.Actor(label=user.email, user_id=user.id, ip=ip),
        "auth.login",
        entity_type="session",
        entity_id=user_session.id,
    )
    await session.commit()
    return IssuedSession(user_session, token)


async def resolve_session(
    session: AsyncSession, settings: Settings, token: str
) -> UserSession | None:
    """Return the live session for a cookie value, refreshing its last-seen time."""
    now = utcnow()
    user_session = await session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(token))
    )
    if (
        user_session is None
        or user_session.revoked_at is not None
        or user_session.expires_at <= now
        or user_session.last_seen_at <= now - timedelta(days=settings.session_idle_days)
    ):
        return None
    user = await session.get(User, user_session.user_id)
    if user is None or not user.is_active:
        return None
    if now - user_session.last_seen_at > timedelta(minutes=1):
        user_session.last_seen_at = now
        await session.commit()
    return user_session


async def revoke_sessions(
    session: AsyncSession,
    actor: audit.Actor,
    *,
    user_id: int,
    session_ids: list[int] | None = None,
    except_id: int | None = None,
) -> int:
    stmt = update(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if session_ids is not None:
        stmt = stmt.where(UserSession.id.in_(session_ids))
    if except_id is not None:
        stmt = stmt.where(UserSession.id != except_id)
    result = await session.execute(stmt.values(revoked_at=utcnow()).returning(UserSession.id))
    revoked = list(result.scalars())
    if revoked:
        audit.record(
            session,
            actor,
            "auth.sessions_revoked",
            entity_type="user",
            entity_id=user_id,
            after={"session_ids": revoked},
        )
    return len(revoked)
