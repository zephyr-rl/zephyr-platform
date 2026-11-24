"""Tools → Rocket Pass check (owner only). Not tied to a store."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import OwnerDep, PlatformDep, SessionDep
from app.tools.rocketpass import service
from app.tools.rocketpass.client import RocketPassError
from app.tools.rocketpass.models import CheckKind, EpicAccount, PassCheck

router = APIRouter(prefix="/tools/rocket-pass", tags=["tools"])


class AccountOut(BaseModel):
    id: int
    display_name: str
    account_id: str
    signed_in: bool
    xbox_gamertag: str | None
    xbox_linked: bool
    last_result: dict[str, Any] | None
    last_checked_at: datetime | None
    created_at: datetime


class CheckOut(BaseModel):
    id: int
    epic_account_id: int | None
    display_name: str
    kind: CheckKind
    state: str
    progress: str | None
    result: dict[str, Any] | None
    error: str | None
    actor: str
    xbox_user_code: str | None
    xbox_verification_uri: str | None
    xbox_expires_at: datetime | None
    created_at: datetime
    finished_at: datetime | None
    log: list[dict[str, Any]]


class SignInIn(BaseModel):
    code: str = Field(min_length=8, max_length=20000, description="The code, page text or URL")


class StartIn(BaseModel):
    kind: CheckKind = CheckKind.check


def _account(a: EpicAccount) -> AccountOut:
    return AccountOut(
        id=a.id,
        display_name=a.display_name,
        account_id=a.account_id,
        signed_in=bool(a.refresh_token),
        xbox_gamertag=a.xbox_gamertag,
        xbox_linked=bool(a.xbox_refresh_token),
        last_result=a.last_result,
        last_checked_at=a.last_checked_at,
        created_at=a.created_at,
    )


def _check(c: PassCheck) -> CheckOut:
    return CheckOut(
        id=c.id,
        epic_account_id=c.epic_account_id,
        display_name=c.display_name,
        kind=c.kind,
        state=c.state.value,
        progress=c.progress,
        result=c.result,
        error=c.error,
        actor=c.actor,
        xbox_user_code=c.xbox_user_code if c.state.value == "waiting" else None,
        xbox_verification_uri=c.xbox_verification_uri if c.state.value == "waiting" else None,
        xbox_expires_at=c.xbox_expires_at if c.state.value == "waiting" else None,
        created_at=c.created_at,
        finished_at=c.finished_at,
        log=c.log or [],
    )


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(_: OwnerDep, session: SessionDep) -> list[AccountOut]:
    rows = await session.scalars(select(EpicAccount).order_by(EpicAccount.display_name))
    return [_account(a) for a in rows]


@router.post("/accounts", response_model=CheckOut, status_code=status.HTTP_201_CREATED)
async def sign_in(
    body: SignInIn, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> CheckOut:
    """Save the account from the pasted code and start a first check."""
    async with httpx.AsyncClient(timeout=20) as http:
        client = service.client_factory(http)
        try:
            account = await service.sign_in(
                session, platform.secrets, auth.actor, client, body.code
            )
            check = await service.start(
                session, platform.secrets, auth.actor, client, account.id, CheckKind.check
            )
        except RocketPassError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not reach Epic Games.") from exc
    await session.commit()
    return _check(check)


@router.post("/accounts/{account_id}/checks", response_model=CheckOut, status_code=201)
async def start_check(
    account_id: int, body: StartIn, auth: OwnerDep, platform: PlatformDep, session: SessionDep
) -> CheckOut:
    async with httpx.AsyncClient(timeout=20) as http:
        try:
            check = await service.start(
                session,
                platform.secrets,
                auth.actor,
                service.client_factory(http),
                account_id,
                body.kind,
            )
        except service.NotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found.") from exc
        except RocketPassError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not reach Microsoft.") from exc
    await session.commit()
    return _check(check)


@router.delete("/accounts/{account_id}", status_code=204)
async def forget(
    account_id: int,
    auth: OwnerDep,
    session: SessionDep,
    xbox_only: bool = Query(False, description="Only unlink Xbox"),
) -> None:
    try:
        await service.forget(session, auth.actor, account_id, xbox_only=xbox_only)
    except service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found.") from exc
    await session.commit()


@router.get("/checks", response_model=list[CheckOut])
async def list_checks(
    _: OwnerDep, session: SessionDep, limit: int = Query(50, ge=1, le=200)
) -> list[CheckOut]:
    rows = await session.scalars(select(PassCheck).order_by(PassCheck.id.desc()).limit(limit))
    return [_check(c) for c in rows]


@router.get("/checks/{check_id}", response_model=CheckOut)
async def get_check(check_id: int, _: OwnerDep, session: SessionDep) -> CheckOut:
    check = await session.get(PassCheck, check_id)
    if check is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Check not found.")
    return _check(check)
