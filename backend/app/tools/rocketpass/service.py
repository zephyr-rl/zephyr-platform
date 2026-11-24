"""Rocket Pass check: saved Epic sessions, checks run as background jobs, history.

Checks run in the worker (a claim waits up to about two minutes for the pass to arrive), and
the panel polls the check row. Each kind of check:

- ``check``: read the pass.
- ``claim``: claim pending entitlements (Fortnite Crew on PC/Epic) and wait for the pass.
- ``xbox``: hand the linked Xbox account to Epic first, then claim and wait. The first time,
  the owner approves a Microsoft code; the job waits for that and keeps the Xbox sign-in
  (encrypted) for the next time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import audit, jobs
from app.core.db import utcnow
from app.tools.rocketpass.client import (
    EpicToken,
    RocketPassClient,
    RocketPassError,
    SessionExpired,
)
from app.tools.rocketpass.models import CheckKind, CheckState, EpicAccount, PassCheck

log = logging.getLogger(__name__)

RUN_JOB = "rocketpass.run"
XBOX_WAIT_JOB = "rocketpass.xbox_wait"

ClientFactory = Callable[[httpx.AsyncClient], RocketPassClient]


def _epic_ctx(account_id: str) -> str:
    return f"rp-epic:{account_id}"


def _xbox_ctx(account_id: str) -> str:
    return f"rp-xbox:{account_id}"


class NotFound(Exception):
    pass


async def sign_in(
    session: AsyncSession,
    secrets: Any,
    actor: audit.Actor,
    client: RocketPassClient,
    pasted: str,
) -> EpicAccount:
    """Exchange the pasted code and save (or refresh) the account. Raises RocketPassError."""
    token = await client.login_with_code(pasted)
    account = await session.scalar(
        select(EpicAccount).where(EpicAccount.account_id == token.account_id)
    )
    if account is None:
        account = EpicAccount(account_id=token.account_id, display_name=token.display_name)
        session.add(account)
    _save_epic(account, secrets, token)
    await session.flush()
    audit.record(
        session,
        actor,
        "rocketpass.signed_in",
        entity_type="epic_account",
        entity_id=account.id,
        after={"display_name": account.display_name},
    )
    return account


def _save_epic(account: EpicAccount, secrets: Any, token: EpicToken) -> None:
    account.display_name = token.display_name or account.display_name
    account.refresh_token = (
        secrets.encrypt(token.refresh_token, context=_epic_ctx(account.account_id))
        if token.refresh_token
        else None
    )
    account.refresh_expires_at = token.refresh_expires_at


async def start(
    session: AsyncSession,
    secrets: Any,
    actor: audit.Actor,
    client: RocketPassClient,
    account_id: int,
    kind: CheckKind,
) -> PassCheck:
    account = await session.get(EpicAccount, account_id)
    if account is None:
        raise NotFound
    if not account.refresh_token:
        raise SessionExpired()
    check = PassCheck(
        epic_account_id=account.id,
        display_name=account.display_name,
        kind=kind,
        state=CheckState.queued,
        actor=actor.label,
    )
    session.add(check)
    if kind is CheckKind.xbox and not account.xbox_refresh_token:
        code = await client.xbox_device_code()
        check.state = CheckState.waiting
        check.xbox_user_code = str(code["user_code"])
        check.xbox_verification_uri = str(
            code.get("verification_uri") or "https://microsoft.com/link"
        )
        check.xbox_device_code = secrets.encrypt(
            str(code["device_code"]), context=_xbox_ctx(account.account_id)
        )
        check.xbox_expires_at = utcnow() + timedelta(seconds=int(code.get("expires_in") or 900))
        check.progress = "Waiting for you to approve the Microsoft sign-in"
        check.log = [
            {
                "at": utcnow().isoformat(),
                "level": "info",
                "message": f"Microsoft code {check.xbox_user_code} shown; waiting for approval",
            }
        ]
        await session.flush()
        await jobs.enqueue(
            session,
            XBOX_WAIT_JOB,
            {"check_id": check.id, "interval": int(code.get("interval") or 5)},
            run_at=utcnow() + timedelta(seconds=int(code.get("interval") or 5)),
            max_attempts=3,
        )
    else:
        await session.flush()
        await jobs.enqueue(session, RUN_JOB, {"check_id": check.id}, max_attempts=1)
    audit.record(
        session,
        actor,
        f"rocketpass.{kind.value}",
        entity_type="epic_account",
        entity_id=account.id,
        after={"check_id": check.id, "display_name": account.display_name},
    )
    return check


async def forget(
    session: AsyncSession, actor: audit.Actor, account_id: int, *, xbox_only: bool
) -> None:
    account = await session.get(EpicAccount, account_id)
    if account is None:
        raise NotFound
    if xbox_only:
        account.xbox_refresh_token = None
        account.xbox_gamertag = None
        action = "rocketpass.xbox_unlinked"
    else:
        await session.delete(account)
        action = "rocketpass.forgotten"
    audit.record(
        session,
        actor,
        action,
        entity_type="epic_account",
        entity_id=account_id,
        before={"display_name": account.display_name},
    )


# ---- jobs ---------------------------------------------------------------------------------

MAX_LOG = 400


class RunLog:
    """The check's step-by-step log, saved on the row as it grows (so a stuck run shows too)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], check_id: int) -> None:
        self._sessionmaker = sessionmaker
        self._check_id = check_id
        self.entries: list[dict[str, Any]] = []

    async def load(self) -> None:
        async with self._sessionmaker() as session:
            check = await session.get(PassCheck, self._check_id)
            self.entries = list(check.log or []) if check is not None else []

    async def __call__(self, level: str, message: str) -> None:
        self.entries.append({"at": utcnow().isoformat(), "level": level, "message": message[:500]})
        if len(self.entries) > MAX_LOG:
            self.entries = self.entries[:50] + self.entries[-(MAX_LOG - 50) :]
        async with self._sessionmaker() as session, session.begin():
            check = await session.get(PassCheck, self._check_id)
            if check is not None:
                check.log = list(self.entries)


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    check_id: int,
    *,
    state: CheckState,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    async with sessionmaker() as session, session.begin():
        check = await session.get(PassCheck, check_id)
        if check is None:
            return
        check.state, check.result, check.error = state, result, error
        check.progress = None
        check.finished_at = utcnow()
        check.xbox_device_code = None
        if result is not None and check.epic_account_id is not None:
            account = await session.get(EpicAccount, check.epic_account_id)
            if account is not None:
                account.last_result = result
                account.last_checked_at = utcnow()


async def run_check(
    sessionmaker: async_sessionmaker[AsyncSession],
    secrets: Any,
    client: RocketPassClient,
    check_id: int,
) -> None:
    async with sessionmaker() as session, session.begin():
        check = await session.get(PassCheck, check_id)
        if check is None or check.state in (CheckState.done, CheckState.failed):
            return
        account = (
            await session.get(EpicAccount, check.epic_account_id) if check.epic_account_id else None
        )
        if account is None or not account.refresh_token:
            check.state, check.error, check.finished_at = (
                CheckState.failed,
                "The Epic account was forgotten or its session expired.",
                utcnow(),
            )
            return
        check.state, check.progress = CheckState.running, "Signing in to Epic"
        kind, epic_id = check.kind, account.account_id
        refresh = secrets.decrypt(account.refresh_token, context=_epic_ctx(epic_id))
        xbox_refresh = (
            secrets.decrypt(account.xbox_refresh_token, context=_xbox_ctx(epic_id))
            if account.xbox_refresh_token
            else None
        )

    log = RunLog(sessionmaker, check_id)
    await log.load()
    client.log = log
    client.watch_http()
    await log("info", f"Started: {kind.value}")

    async def progress(text: str) -> None:
        await log("info", text)
        async with sessionmaker() as s, s.begin():
            row = await s.get(PassCheck, check_id)
            if row is not None:
                row.progress = text[:200]

    try:
        return await _run(
            sessionmaker,
            secrets,
            client,
            check_id,
            kind,
            epic_id,
            refresh,
            xbox_refresh,
            progress,
            log,
        )
    except Exception as exc:  # anything unexpected must end the check, with the reason
        reason = f"Unexpected error: {type(exc).__name__}: {exc}"[:500]
        await log("error", reason)
        await _finish(sessionmaker, check_id, state=CheckState.failed, error=reason)


async def _run(
    sessionmaker: async_sessionmaker[AsyncSession],
    secrets: Any,
    client: RocketPassClient,
    check_id: int,
    kind: CheckKind,
    epic_id: str,
    refresh: str,
    xbox_refresh: str | None,
    progress: Callable[[str], Any],
    log: RunLog,
) -> None:
    try:
        token = await client.refresh(refresh)
    except SessionExpired as exc:
        async with sessionmaker() as session, session.begin():
            account = await session.scalar(
                select(EpicAccount).where(EpicAccount.account_id == epic_id)
            )
            if account is not None:
                account.refresh_token = None
        await log("error", f"Epic session: {exc}")
        await _finish(sessionmaker, check_id, state=CheckState.failed, error=str(exc))
        return
    except (RocketPassError, httpx.HTTPError) as exc:
        await log("error", _detail(exc))
        await _finish(sessionmaker, check_id, state=CheckState.failed, error=_message(exc))
        return
    await log("info", f"Signed in to Epic as {token.display_name}")
    async with (
        sessionmaker() as session,
        session.begin(),
    ):  # refresh tokens rotate: keep the new one
        account = await session.scalar(select(EpicAccount).where(EpicAccount.account_id == epic_id))
        if account is not None:
            _save_epic(account, secrets, token)

    result: dict[str, Any] = {}
    try:
        if kind is CheckKind.xbox:
            if xbox_refresh is None:
                raise RocketPassError("Xbox is not linked to this account. Link it again.")
            await progress("Syncing Xbox Live with Epic")
            ms = await client.xbox_refresh(xbox_refresh)
            gamertag = await client.xbox_sync(str(ms["access_token"]))
            async with sessionmaker() as session, session.begin():
                account = await session.scalar(
                    select(EpicAccount).where(EpicAccount.account_id == epic_id)
                )
                if account is not None and ms.get("refresh_token"):
                    account.xbox_refresh_token = secrets.encrypt(
                        str(ms["refresh_token"]), context=_xbox_ctx(epic_id)
                    )
                    account.xbox_gamertag = gamertag
            result["xbox_gamertag"] = gamertag
        await progress(
            "Reading the Rocket Pass" if kind is CheckKind.check else "Claiming the pass"
        )
        outcome = await client.check(token, claim=kind is not CheckKind.check, progress=progress)
    except (RocketPassError, httpx.HTTPError, OSError, TimeoutError) as exc:
        await log("error", _detail(exc))
        await _finish(sessionmaker, check_id, state=CheckState.failed, error=_message(exc))
        return
    assert outcome.info is not None
    await log(
        "info",
        f"Done: premium={outcome.info.has_premium}, tier {outcome.info.tier}, "
        f"{outcome.claimed_products} products claimed, {outcome.attempts} attempts",
    )
    result.update(outcome.info.to_dict())
    result["claimed_products"] = outcome.claimed_products
    result["newly_activated"] = outcome.newly_activated
    result["attempts"] = outcome.attempts
    await _finish(sessionmaker, check_id, state=CheckState.done, result=result)


async def wait_for_xbox(
    sessionmaker: async_sessionmaker[AsyncSession],
    secrets: Any,
    client: RocketPassClient,
    check_id: int,
    interval: int,
) -> None:
    """One poll of the Microsoft sign-in. Raises RetryLater while the owner has not approved."""
    async with sessionmaker() as session:
        check = await session.get(PassCheck, check_id)
        if check is None or check.state is not CheckState.waiting or not check.xbox_device_code:
            return
        account = (
            await session.get(EpicAccount, check.epic_account_id) if check.epic_account_id else None
        )
        if account is None:
            await _finish(
                sessionmaker,
                check_id,
                state=CheckState.failed,
                error="The Epic account was forgotten.",
            )
            return
        epic_id = account.account_id
        device = secrets.decrypt(check.xbox_device_code, context=_xbox_ctx(epic_id))
        expired = check.xbox_expires_at is not None and check.xbox_expires_at < utcnow()
    if expired:
        await _finish(
            sessionmaker,
            check_id,
            state=CheckState.failed,
            error="The Microsoft code expired before it was approved. Start again.",
        )
        return
    log = RunLog(sessionmaker, check_id)
    await log.load()
    client.log = log
    try:
        tokens = await client.xbox_poll(device)
    except (RocketPassError, httpx.HTTPError) as exc:
        await log("error", f"Microsoft sign-in: {_detail(exc)}")
        await _finish(sessionmaker, check_id, state=CheckState.failed, error=_message(exc))
        return
    if tokens is None:
        raise jobs.RetryLater(timedelta(seconds=max(2, interval)), "waiting for Microsoft sign-in")
    async with sessionmaker() as session, session.begin():
        account = await session.scalar(select(EpicAccount).where(EpicAccount.account_id == epic_id))
        check = await session.get(PassCheck, check_id)
        if account is None or check is None:
            return
        account.xbox_refresh_token = secrets.encrypt(
            str(tokens.get("refresh_token") or ""), context=_xbox_ctx(epic_id)
        )
        check.state, check.progress = CheckState.queued, "Microsoft sign-in approved"
        check.log = [
            *log.entries,
            {"at": utcnow().isoformat(), "level": "info", "message": "Microsoft sign-in approved"},
        ]
        check.xbox_device_code = None
        await jobs.enqueue(session, RUN_JOB, {"check_id": check_id}, max_attempts=1)


def _detail(exc: BaseException) -> str:
    """For the log: the exception type and its message (no tokens are ever in these)."""
    return f"{type(exc).__name__}: {exc}"[:500]


def _message(exc: BaseException) -> str:
    if isinstance(exc, RocketPassError):
        return str(exc)
    if isinstance(exc, httpx.HTTPError | OSError | TimeoutError):
        return "Could not reach Epic, Rocket League or Xbox servers. Try again in a minute."
    return "Unexpected error."


def _default_client(http: httpx.AsyncClient) -> RocketPassClient:
    return RocketPassClient(http)


# Looked up on every use, so tests can swap in a fake client.
client_factory: ClientFactory = _default_client


def register(platform: Any) -> None:
    async def run(ctx: jobs.JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as http:
            await run_check(
                ctx.sessionmaker, platform.secrets, client_factory(http), int(payload["check_id"])
            )
        return {}

    async def xbox_wait(ctx: jobs.JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as http:
            await wait_for_xbox(
                ctx.sessionmaker,
                platform.secrets,
                client_factory(http),
                int(payload["check_id"]),
                int(payload.get("interval") or 5),
            )
        return {}

    platform.jobs.register(RUN_JOB, run)
    platform.jobs.register(XBOX_WAIT_JOB, xbox_wait)
