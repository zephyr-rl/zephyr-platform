from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, text

from app.api.deps import AuthDep, OwnerDep, PlatformDep, SessionDep
from app.core.audit import AuditLog
from app.core.jobs import Job, JobState

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: SessionDep) -> dict[str, str]:
    try:
        await session.execute(text("select 1"))
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable") from None
    return {"status": "ready"}


class ConnectorOut(BaseModel):
    key: str
    name: str
    capabilities: list[str]
    credential_fields: list[str]


@router.get("/connectors", response_model=list[ConnectorOut])
async def connectors(auth: AuthDep, platform: PlatformDep) -> list[ConnectorOut]:
    return [
        ConnectorOut(
            key=c.key,
            name=c.name,
            capabilities=sorted(c.capabilities),
            credential_fields=list(c.credential_fields),
        )
        for c in platform.connectors.all()
    ]


class AuditOut(BaseModel):
    id: int
    at: datetime
    actor: str
    action: str
    entity_type: str | None
    entity_id: str | None
    store_id: int | None
    before: Any
    after: Any
    reason: str | None


@router.get("/audit", response_model=list[AuditOut])
async def audit_log(
    auth: OwnerDep,
    session: SessionDep,
    store_id: int | None = None,
    before_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[AuditOut]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if store_id is not None:
        stmt = stmt.where(AuditLog.store_id == store_id)
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    return [
        AuditOut(
            id=a.id,
            at=a.at,
            actor=a.actor_label,
            action=a.action,
            entity_type=a.entity_type,
            entity_id=a.entity_id,
            store_id=a.store_id,
            before=a.before,
            after=a.after,
            reason=a.reason,
        )
        for a in await session.scalars(stmt)
    ]


class JobsSummary(BaseModel):
    counts: dict[str, int]
    dead: list[dict[str, Any]]


@router.get("/jobs/summary", response_model=JobsSummary)
async def jobs_summary(auth: AuthDep, session: SessionDep) -> JobsSummary:
    rows = await session.execute(select(Job.state, func.count()).group_by(Job.state))
    counts = {state.value: 0 for state in JobState}
    counts.update({state.value: n for state, n in rows})
    dead = await session.scalars(
        select(Job).where(Job.state == JobState.dead).order_by(Job.id.desc()).limit(20)
    )
    return JobsSummary(
        counts=counts,
        dead=[
            {
                "id": j.id,
                "type": j.type,
                "store_id": j.store_id,
                "error": j.last_error,
                "finished_at": j.finished_at,
            }
            for j in dead
        ],
    )
