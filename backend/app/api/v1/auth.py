from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AuthDep, PlatformDep, SessionDep, client_ip
from app.core import audit
from app.identity import service as identity
from app.identity.models import User, UserSession

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=512)


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str
    role: str


class MeOut(BaseModel):
    user: UserOut
    csrf_token: str
    session_id: int


class SessionOut(BaseModel):
    id: int
    created_at: datetime
    last_seen_at: datetime
    ip: str | None
    user_agent: str | None
    current: bool


class RevokedOut(BaseModel):
    revoked: int


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


def _me(auth_user: UserOut, user_session: UserSession) -> MeOut:
    return MeOut(user=auth_user, csrf_token=user_session.csrf_token, session_id=user_session.id)


@router.post("/login", response_model=MeOut)
async def login(
    body: LoginIn, request: Request, response: Response, platform: PlatformDep, session: SessionDep
) -> MeOut:
    settings = platform.settings
    try:
        issued = await identity.login(
            session,
            settings,
            email=body.email,
            password=body.password,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except identity.RateLimited as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from None
    except identity.AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from None

    max_age = settings.session_max_age_days * 86400
    response.set_cookie(
        settings.session_cookie,
        issued.token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    user = await session.get(User, issued.session.user_id)
    assert user is not None
    return _me(
        UserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role),
        issued.session,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    auth: AuthDep, response: Response, platform: PlatformDep, session: SessionDep
) -> None:
    await identity.revoke_sessions(
        session, auth.actor, user_id=auth.user.id, session_ids=[auth.session.id]
    )
    await session.commit()
    response.delete_cookie(platform.settings.session_cookie, path="/")


@router.get("/me", response_model=MeOut)
async def me(auth: AuthDep) -> MeOut:
    u = auth.user
    return _me(
        UserOut(id=u.id, email=u.email, display_name=u.display_name, role=u.role), auth.session
    )


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(auth: AuthDep, session: SessionDep) -> list[SessionOut]:
    rows = await session.scalars(
        select(UserSession)
        .where(UserSession.user_id == auth.user.id, UserSession.revoked_at.is_(None))
        .order_by(UserSession.last_seen_at.desc())
    )
    return [
        SessionOut(
            id=s.id,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            ip=s.ip,
            user_agent=s.user_agent,
            current=s.id == auth.session.id,
        )
        for s in rows
    ]


@router.delete("/sessions/{session_id}", response_model=RevokedOut)
async def revoke_session(session_id: int, auth: AuthDep, session: SessionDep) -> RevokedOut:
    count = await identity.revoke_sessions(
        session, auth.actor, user_id=auth.user.id, session_ids=[session_id]
    )
    await session.commit()
    if not count:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")
    return RevokedOut(revoked=count)


@router.post("/sessions/revoke-others", response_model=RevokedOut)
async def revoke_other_sessions(auth: AuthDep, session: SessionDep) -> RevokedOut:
    count = await identity.revoke_sessions(
        session, auth.actor, user_id=auth.user.id, except_id=auth.session.id
    )
    await session.commit()
    return RevokedOut(revoked=count)


@router.post("/password", response_model=RevokedOut)
async def change_password(body: PasswordIn, auth: AuthDep, session: SessionDep) -> RevokedOut:
    """Change the password; every other session is signed out."""
    user = await session.get(User, auth.user.id)
    assert user is not None
    if not identity.verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The current password is wrong.")
    if body.new_password == body.current_password:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The new password must be different."
        )
    try:
        user.password_hash = identity.hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    audit.record(
        session, auth.actor, "user.password_changed", entity_type="user", entity_id=user.id
    )
    count = await identity.revoke_sessions(
        session, auth.actor, user_id=user.id, except_id=auth.session.id
    )
    await session.commit()
    return RevokedOut(revoked=count)
