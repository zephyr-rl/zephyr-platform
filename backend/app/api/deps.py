"""Request dependencies: platform, database session, authenticated user, CSRF."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import Actor
from app.core.platform import Platform
from app.identity import service as identity
from app.identity.models import Role, User, UserSession

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_platform(request: Request) -> Platform:
    platform: Platform = request.app.state.platform
    return platform


PlatformDep = Annotated[Platform, Depends(get_platform)]


async def get_session(platform: PlatformDep) -> AsyncIterator[AsyncSession]:
    async with platform.db.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def client_ip(request: Request) -> str | None:
    # The direct peer only. Proxy headers are not trusted (invariant I6); when the panel runs
    # behind Caddy, uvicorn's --forwarded-allow-ips must name the proxy explicitly.
    return request.client.host if request.client else None


@dataclass(frozen=True)
class Auth:
    user: User
    session: UserSession
    actor: Actor


async def current_auth(request: Request, platform: PlatformDep, session: SessionDep) -> Auth:
    token = request.cookies.get(platform.settings.session_cookie)
    user_session = (
        await identity.resolve_session(session, platform.settings, token) if token else None
    )
    if user_session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    if request.method in UNSAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(sent, user_session.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token.")
    user = await session.get(User, user_session.user_id)
    assert user is not None
    return Auth(user, user_session, Actor(label=user.email, user_id=user.id, ip=client_ip(request)))


AuthDep = Annotated[Auth, Depends(current_auth)]


async def require_owner(auth: AuthDep) -> Auth:
    if auth.user.role is not Role.owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can do this.")
    return auth


OwnerDep = Annotated[Auth, Depends(require_owner)]
