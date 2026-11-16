"""Stock: imports, duplicates, extraction, state rules (I1, I3) and moves."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import Actor
from app.core.events import OutboxEvent
from app.core.platform import Platform
from app.stock import service
from app.stock.models import ItemState, StockItem, StockItemEvent, StockItemSecret, StockPool
from app.stores import service as stores

ACTOR = Actor.system("tests")


async def _store(session: AsyncSession, platform: Platform, slug: str = "zephyr") -> int:
    store = await stores.create_store(session, platform, ACTOR, name=slug.title(), slug=slug)
    await session.commit()
    return store.id


async def _pool(
    session: AsyncSession, store_id: int, name: str = "RLCS x6", threshold: int = 2
) -> StockPool:
    pool = await service.create_pool(
        session, ACTOR, store_id, name=name, low_stock_threshold=threshold
    )
    await session.commit()
    return pool


async def _import(
    session: AsyncSession, platform: Platform, store_id: int, pool_id: int, text_: str
) -> dict[str, int]:
    record, _ = await service.commit_import(
        session,
        platform,
        ACTOR,
        store_id,
        pool_id,
        text_,
        expected_sha256=service.text_digest(text_),
    )
    await session.commit()
    return record.counts


def accounts(n: int, prefix: str = "user") -> str:
    return "\n".join(f"{prefix}{i}:pass{i}" for i in range(1, n + 1))


async def test_import_preview_then_commit(session: AsyncSession, platform: Platform) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id)
    preview = await service.preview_import(
        session, platform, store_id, pool.id, accounts(3) + "\nnot-an-account\n"
    )
    assert preview.counts["new"] == 3 and preview.counts["invalid"] == 1
    assert preview.detected_format == "colon"
    assert [line.login_hint for line in preview.lines] == ["us••••r1", "us••••r2", "us••••r3"]
    assert (await session.scalar(select(StockItem.id))) is None  # preview saves nothing

    counts = await _import(session, platform, store_id, pool.id, accounts(3))
    assert counts["added"] == 3
    items = (await session.scalars(select(StockItem).order_by(StockItem.position))).all()
    assert [i.state for i in items] == [ItemState.available] * 3
    assert [i.position for i in items] == [1, 2, 3]


async def test_commit_refuses_text_changed_after_preview(
    session: AsyncSession, platform: Platform
) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id)
    with pytest.raises(service.StockError, match="changed after the preview"):
        await service.commit_import(
            session,
            platform,
            ACTOR,
            store_id,
            pool.id,
            accounts(2),
            expected_sha256=service.text_digest(accounts(1)),
        )


async def test_secrets_are_never_stored_in_clear(session: AsyncSession, platform: Platform) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id)
    await _import(
        session, platform, store_id, pool.id, "secretlogin:SuperSecretPass:TwitchTokenValue123"
    )
    raw = await session.execute(text("SELECT * FROM stock_items"))
    dump = str(raw.all()) + str(
        (await session.execute(text("SELECT * FROM stock_item_secrets"))).all()
    )
    for secret in ("secretlogin", "SuperSecretPass", "TwitchTokenValue123"):
        assert secret not in dump
    item = await session.scalar(select(StockItem))
    assert item is not None and item.has_token
    [revealed] = await service.reveal(session, platform, [item])
    assert (revealed.login, revealed.password, revealed.token) == (
        "secretlogin",
        "SuperSecretPass",
        "TwitchTokenValue123",
    )


async def test_duplicates_are_classified(session: AsyncSession, platform: Platform) -> None:
    store_id = await _store(session, platform)
    pool_a = await _pool(session, store_id, "A")
    pool_b = await _pool(session, store_id, "B")
    await _import(session, platform, store_id, pool_a.id, "alpha:1\nbeta:2\ngamma:3")
    await service.extract(session, platform, ACTOR, store_id, pool_a.id, 1)  # alpha leaves stock
    await session.commit()

    preview = await service.preview_import(
        session,
        platform,
        store_id,
        pool_b.id,
        "ALPHA:other\nbeta:2\ndelta:4\ndelta:4\n  Gamma :3",
    )
    assert [line.status for line in preview.lines] == [
        "taken_out",  # extracted in this panel: can be put back on import
        "in_other_pool",
        "new",
        "repeated",
        "in_other_pool",
    ]
    counts = await _import(
        session, platform, store_id, pool_b.id, "ALPHA:other\nbeta:2\ndelta:4\ndelta:4"
    )
    assert counts["added"] == 1


async def test_same_login_in_two_stores_only_once_in_stock(
    session: AsyncSession, platform: Platform
) -> None:
    s1, s2 = await _store(session, platform, "one"), await _store(session, platform, "two")
    p1, p2 = await _pool(session, s1), await _pool(session, s2)
    p1_id, p2_id = p1.id, p2.id
    assert (await _import(session, platform, s1, p1_id, "shared:pw"))["added"] == 1
    # In stock in the first store: refused in the second (it would be sold twice).
    refused = await _import(session, platform, s2, p2_id, "shared:pw")
    assert refused["added"] == 0 and refused["in_other_store"] == 1
    # Taken out of the first store, it can go into the second one.
    await service.extract(session, platform, ACTOR, s1, p1_id, 1)
    await session.commit()
    assert (await _import(session, platform, s2, p2_id, "shared:pw"))["added"] == 1


async def test_reimport_attaches_missing_tokens(session: AsyncSession, platform: Platform) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id)
    await _import(session, platform, store_id, pool.id, "user1:pw1\nuser2:pw2")
    counts = await _import(session, platform, store_id, pool.id, "user1:pw1:tokA\nuser2:pw2")
    assert counts["added"] == 0 and counts["tokens_attached"] == 1
    assert len((await session.scalars(select(StockItemSecret))).all()) == 1


async def test_extraction_is_first_in_first_out_and_can_be_undone(
    session: AsyncSession, platform: Platform
) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id)
    await _import(session, platform, store_id, pool.id, accounts(5))
    extraction, items = await service.extract(
        session, platform, ACTOR, store_id, pool.id, 2, reason="manual sale"
    )
    await session.commit()
    assert [i.line() for i in items] == ["user1:pass1", "user2:pass2"]

    _, again = await service.view_extraction(session, platform, ACTOR, store_id, extraction.id)
    assert [i.login for i in again] == ["user1", "user2"]

    await service.undo_extraction(session, ACTOR, store_id, extraction.id)
    await session.commit()
    _, next_items = await service.extract(session, platform, ACTOR, store_id, pool.id, 1)
    assert next_items[0].login == "user1"  # undone items go back to their original place
    with pytest.raises(service.StockError, match="already undone"):
        await service.undo_extraction(session, ACTOR, store_id, extraction.id)


async def test_undo_refused_when_items_changed(session: AsyncSession, platform: Platform) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id)
    await _import(session, platform, store_id, pool.id, accounts(2))
    extraction, items = await service.extract(session, platform, ACTOR, store_id, pool.id, 2)
    await service.change_state(
        session, ACTOR, store_id, [items[0].id], ItemState.void, reason="banned"
    )
    await session.commit()
    with pytest.raises(service.StockError, match="cannot be undone"):
        await service.undo_extraction(session, ACTOR, store_id, extraction.id)


async def test_concurrent_extractions_never_share_an_item(
    session: AsyncSession, platform: Platform
) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id)
    await _import(session, platform, store_id, pool.id, accounts(30))

    async def take() -> list[str]:
        async with platform.db.sessionmaker() as s:
            try:
                _, items = await service.extract(s, platform, ACTOR, store_id, pool.id, 4)
            except service.StockError:
                return []
            await s.commit()
            return [i.login for i in items]

    results = await asyncio.gather(*(take() for _ in range(10)))
    taken = [login for batch in results for login in batch]
    assert len(taken) == 30 and len(set(taken)) == 30


async def test_database_forbids_deleting_items_and_reviving_final_states(
    session: AsyncSession, platform: Platform
) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id)
    await _import(session, platform, store_id, pool.id, accounts(2))
    item_id = await session.scalar(select(StockItem.id).order_by(StockItem.id))
    with pytest.raises(DBAPIError, match="cannot be delete"):
        await session.execute(text("DELETE FROM stock_items"))
    await session.rollback()

    await session.execute(
        update(StockItem).where(StockItem.id == item_id).values(state=ItemState.delivered)
    )
    await session.commit()
    with pytest.raises(DBAPIError, match="final state"):
        await session.execute(
            update(StockItem).where(StockItem.id == item_id).values(state=ItemState.available)
        )
    await session.rollback()
    with pytest.raises(DBAPIError, match="cannot be update"):
        await session.execute(update(StockItemEvent).values(event="rewritten"))
    await session.rollback()


async def test_state_rules(session: AsyncSession, platform: Platform) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id)
    await _import(session, platform, store_id, pool.id, accounts(3))
    ids = list(await session.scalars(select(StockItem.id).order_by(StockItem.id)))
    assert (
        await service.change_state(
            session, ACTOR, store_id, ids[:2], ItemState.quarantined, reason="check"
        )
        == 2
    )
    assert await service.change_state(session, ACTOR, store_id, ids[:1], ItemState.available) == 1
    await service.change_state(session, ACTOR, store_id, ids[2:], ItemState.void)
    await session.commit()
    with pytest.raises(service.StockError, match="cannot become"):
        await service.change_state(session, ACTOR, store_id, ids[2:], ItemState.available)
    with pytest.raises(service.StockError, match="by hand"):
        await service.change_state(session, ACTOR, store_id, ids[:1], ItemState.delivered)
    history = await service.item_history(session, ids[0])
    assert [e.event for e in history] == ["imported", "quarantined", "released"]


async def test_low_stock_and_depleted_events_fire_once(
    session: AsyncSession, platform: Platform
) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id, threshold=2)
    await _import(session, platform, store_id, pool.id, accounts(4))
    for _ in range(4):
        await service.extract(session, platform, ACTOR, store_id, pool.id, 1)
        await session.commit()
    types = list(
        await session.scalars(
            select(OutboxEvent.type)
            .where(OutboxEvent.type.like("stock.%"))
            .order_by(OutboxEvent.id)
        )
    )
    assert types == ["stock.imported", "stock.low", "stock.depleted"]


async def test_move_within_and_across_stores(session: AsyncSession, platform: Platform) -> None:
    zephyr, jewelry = (
        await _store(session, platform, "zephyr"),
        await _store(session, platform, "jewelry"),
    )
    source = await _pool(session, zephyr, "Source")
    other = await _pool(session, zephyr, "Other")
    target = await _pool(session, jewelry, "Affluenter 43 Drops")
    await _import(session, platform, zephyr, source.id, "mover1:p1:tok1\nmover2:p2\nmover3:p3")
    # Refused there: it is in stock in Zephyr (an account is in stock in one store at a time).
    assert (await _import(session, platform, jewelry, target.id, "mover3:p3"))[
        "in_other_store"
    ] == 1
    ids = list(
        await session.scalars(
            select(StockItem.id).where(StockItem.store_id == zephyr).order_by(StockItem.id)
        )
    )

    assert await service.move_items(session, platform, ACTOR, zephyr, ids[:1], other.id) == {
        "moved": 1,
        "skipped": 0,
        "sold_there": 0,
    }
    result = await service.move_items(
        session, platform, ACTOR, zephyr, ids, target.id, target_store_id=jewelry
    )
    await session.commit()
    assert result == {"moved": 3, "skipped": 0, "sold_there": 0}

    moved = await service.find_by_login(session, platform, jewelry, "mover1")
    assert moved is not None and moved.state is ItemState.available and moved.has_token
    [revealed] = await service.reveal(session, platform, [moved])
    assert revealed.token == "tok1"
    source_item = await service.find_by_login(session, platform, zephyr, "mover1")
    assert source_item is not None and source_item.state is ItemState.extracted
    gone = await service.find_by_login(session, platform, zephyr, "mover3")
    assert gone is not None and gone.state is ItemState.extracted  # moved to Jewelry too


# ---- API ---------------------------------------------------------------------------------


async def test_stock_api_flow(auth_client: httpx.AsyncClient) -> None:
    store = (
        await auth_client.post("/api/v1/stores", json={"name": "Zephyr", "slug": "zephyr"})
    ).json()
    base = f"/api/v1/stores/{store['id']}/stock"
    pool = (await auth_client.post(f"{base}/pools", json={"name": "RLCS x6"})).json()
    body = {"text": accounts(150)}
    preview = (
        await auth_client.post(f"{base}/pools/{pool['id']}/import/preview", json=body)
    ).json()
    assert preview["counts"]["new"] == 150
    assert "pass1" not in str(preview)

    created = await auth_client.post(
        f"{base}/pools/{pool['id']}/import",
        json={**body, "expected_sha256": preview["text_sha256"]},
    )
    assert created.status_code == 201 and created.json()["counts"]["added"] == 150

    page1 = (
        await auth_client.get(f"{base}/pools/{pool['id']}/items", params={"limit": 100})
    ).json()
    page2 = (
        await auth_client.get(
            f"{base}/pools/{pool['id']}/items", params={"limit": 100, "after": page1["next_after"]}
        )
    ).json()
    assert page1["total"] == 150 and len(page1["items"]) == 100 and len(page2["items"]) == 50
    assert page2["next_after"] is None

    extracted = (
        await auth_client.post(f"{base}/pools/{pool['id']}/extract", json={"count": 3})
    ).json()
    assert extracted["text"] == "user1:pass1\nuser2:pass2\nuser3:pass3"
    pools = (await auth_client.get(f"{base}/pools")).json()
    assert pools[0]["counts"]["available"] == 147 and pools[0]["counts"]["extracted"] == 3

    found = (await auth_client.get(f"{base}/search", params={"login": "USER5"})).json()
    assert found["state"] == "available"
    history = (await auth_client.get(f"{base}/items/{found['id']}/history")).json()
    assert history[0]["event"] == "imported"

    undo = await auth_client.post(f"{base}/extractions/{extracted['id']}/undo")
    assert undo.status_code == 200 and undo.json()["undone_at"]
    again = await auth_client.post(f"{base}/extractions/{extracted['id']}/undo")
    assert again.status_code == 409

    audit = (await auth_client.get("/api/v1/audit", params={"store_id": store["id"]})).json()
    assert {"stock.imported", "stock.extracted", "stock.extraction_undone"} <= {
        a["action"] for a in audit
    }
