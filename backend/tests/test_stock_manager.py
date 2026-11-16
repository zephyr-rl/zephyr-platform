"""Stock manager: accounts with credentials, search, export, remove / put back, moves between
stores without conflicts, pool deletion and the format converter."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import Actor, AuditLog
from app.core.platform import Platform
from app.stock import service
from app.stock.models import ItemState, StockItem
from tests.test_stock import _import, _pool, _store, accounts

ACTOR = Actor.system("tests")


async def test_accounts_page_search_and_export(
    auth_client, session: AsyncSession, platform: Platform
) -> None:  # type: ignore[no-untyped-def]
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id, "RLCS x6")
    pool_id = pool.id
    await _import(
        session,
        platform,
        store_id,
        pool_id,
        "alpha1:pw1:tokA\nbeta2:pw2\ngamma3@inbox.lv:pw3:tokC\n" + accounts(60, "bulk"),
    )
    base = f"/api/v1/stores/{store_id}/stock"

    page = (await auth_client.get(f"{base}/pools/{pool_id}/accounts", params={"limit": 50})).json()
    assert page["total"] == 63 and len(page["items"]) == 50
    first = page["items"][0]
    assert (first["login"], first["password"], first["token"]) == ("alpha1", "pw1", "tokA")
    assert first["last_event"] == "imported"
    page2 = (
        await auth_client.get(
            f"{base}/pools/{pool_id}/accounts", params={"limit": 50, "offset": 50}
        )
    ).json()
    assert len(page2["items"]) == 13

    found = (
        await auth_client.get(f"{base}/pools/{pool_id}/accounts", params={"q": "INBOX"})
    ).json()
    assert [a["login"] for a in found["items"]] == ["gamma3@inbox.lv"]
    by_token = (
        await auth_client.get(f"{base}/pools/{pool_id}/accounts", params={"q": "toka"})
    ).json()
    assert [a["login"] for a in by_token["items"]] == ["alpha1"]

    pools = (await auth_client.get(f"{base}/pools")).json()
    assert pools[0]["tokens"] == 2

    lpt = (
        await auth_client.get(
            f"{base}/pools/{pool_id}/export", params={"format": "login_password_token"}
        )
    ).json()
    assert lpt["count"] == 63 and lpt["text"].startswith("alpha1:pw1:tokA\nbeta2:pw2\n")
    tokens = (
        await auth_client.get(f"{base}/pools/{pool_id}/export", params={"format": "tokens"})
    ).json()
    assert tokens == {"text": "tokA\ntokC", "count": 2}
    # Exporting does not take accounts out of stock, and both reads are in the audit log.
    assert (await auth_client.get(f"{base}/pools")).json()[0]["counts"]["available"] == 63
    actions = set(
        await session.scalars(select(AuditLog.action).where(AuditLog.store_id == store_id))
    )
    assert {"stock.viewed", "stock.exported"} <= actions


async def test_remove_and_put_back(auth_client, session: AsyncSession, platform: Platform) -> None:  # type: ignore[no-untyped-def]
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id)
    pool_id = pool.id
    await _import(session, platform, store_id, pool_id, accounts(4))
    base = f"/api/v1/stores/{store_id}/stock"
    ids = [
        a["id"] for a in (await auth_client.get(f"{base}/pools/{pool_id}/accounts")).json()["items"]
    ]

    removed = await auth_client.post(
        f"{base}/items/remove", json={"item_ids": ids[:2], "reason": "banned"}
    )
    assert removed.json() == {"removed": 2}
    gone = (
        await auth_client.get(f"{base}/pools/{pool_id}/accounts", params={"state": "extracted"})
    ).json()
    assert {a["last_event"] for a in gone["items"]} == {"removed"}
    back = await auth_client.post(f"{base}/items/put-back", json={"item_ids": [ids[0]]})
    assert back.json() == {"put_back": 1}

    # Accounts taken out by an extraction come back with "undo", not "put back".
    await service.extract(session, platform, ACTOR, store_id, pool_id, 1)
    await session.commit()
    extracted = await session.scalar(
        select(StockItem.id).where(
            StockItem.pool_id == pool_id,
            StockItem.state == ItemState.extracted,
            StockItem.id != ids[1],
        )
    )
    refused = await auth_client.post(f"{base}/items/put-back", json={"item_ids": [extracted]})
    assert refused.status_code == 409 and "extraction history" in refused.json()["detail"]


async def test_moving_between_stores_back_and_forth(
    session: AsyncSession, platform: Platform
) -> None:
    zephyr = await _store(session, platform, "zephyr")
    kawai = await _store(session, platform, "kawai")
    zp = await _pool(session, zephyr, "Lasa x25")
    kp = await _pool(session, kawai, "Lasa x25")
    zp_id, kp_id = zp.id, kp.id
    await _import(session, platform, zephyr, zp_id, "move1:pw1:tok1\nmove2:pw2")
    ids = list(await session.scalars(select(StockItem.id).where(StockItem.pool_id == zp_id)))

    there = await service.move_items(
        session, platform, ACTOR, zephyr, ids, kp_id, target_store_id=kawai
    )
    await session.commit()
    assert there == {"moved": 2, "skipped": 0, "sold_there": 0}
    back_ids = list(await session.scalars(select(StockItem.id).where(StockItem.pool_id == kp_id)))
    # Back to the first store: the old rows come back (no "already known" conflict).
    back = await service.move_items(
        session, platform, ACTOR, kawai, back_ids, zp_id, target_store_id=zephyr
    )
    await session.commit()
    assert back == {"moved": 2, "skipped": 0, "sold_there": 0}
    rows = list(await session.scalars(select(StockItem).where(StockItem.store_id == zephyr)))
    assert {r.state for r in rows} == {ItemState.available} and len(rows) == 2
    revealed = {r.login: r for r in await service.reveal(session, platform, rows)}
    assert revealed["move1"].token == "tok1"

    # Pasting an account that is in stock in the other store is refused (it would be sold twice).
    kawai_preview = await service.preview_import(session, platform, kawai, kp_id, "move1:pw1")
    assert [(line.status, line.where) for line in kawai_preview.lines] == [
        ("in_other_store", "Zephyr")
    ]


async def test_import_puts_back_accounts_taken_out_here(
    session: AsyncSession, platform: Platform
) -> None:
    store_id = await _store(session, platform)
    pool = await _pool(session, store_id, "RLCS x6", threshold=0)
    other = await _pool(session, store_id, "RLCS x3", threshold=0)
    pool_id, other_id = pool.id, other.id
    await _import(session, platform, store_id, pool_id, accounts(3))
    await service.extract(session, platform, ACTOR, store_id, pool_id, 2)  # taken out by hand
    await session.commit()

    preview = await service.preview_import(session, platform, store_id, other_id, accounts(3))
    assert [line.status for line in preview.lines] == ["taken_out", "taken_out", "in_other_pool"]
    skipped = await _import(session, platform, store_id, other_id, accounts(2))
    assert skipped["added"] == 0 and skipped["put_back"] == 0
    text_ = accounts(2)
    record, _ = await service.commit_import(
        session,
        platform,
        ACTOR,
        store_id,
        other_id,
        text_,
        expected_sha256=service.text_digest(text_),
        put_back=True,
    )
    await session.commit()
    assert record.counts["put_back"] == 2
    states = {
        (i.pool_id, i.state)
        for i in await session.scalars(select(StockItem).where(StockItem.store_id == store_id))
    }
    assert states == {(other_id, ItemState.available), (pool_id, ItemState.available)}


async def test_delete_only_empty_pools_and_convert(
    auth_client, session: AsyncSession, platform: Platform
) -> None:  # type: ignore[no-untyped-def]
    store_id = await _store(session, platform)
    empty = await _pool(session, store_id, "Empty")
    used = await _pool(session, store_id, "Used")
    empty_id, used_id = empty.id, used.id
    await _import(session, platform, store_id, used_id, accounts(1))
    base = f"/api/v1/stores/{store_id}/stock"
    assert (await auth_client.delete(f"{base}/pools/{empty_id}")).status_code == 204
    refused = await auth_client.delete(f"{base}/pools/{used_id}")
    assert refused.status_code == 409 and "archive it instead" in refused.json()["detail"]

    text_ = (
        "name: userA password: passA token: tokA\n"
        "name: userB password: passB\ntoken: tokB\nnotanaccount"
    )
    out = (
        await auth_client.post(f"{base}/convert", json={"text": text_, "with_token": True})
    ).json()
    assert out["text"] == "userA:passA:tokA\nuserB:passB:tokB" and out["count"] == 2
    plain = (await auth_client.post(f"{base}/convert", json={"text": text_})).json()
    assert plain["text"] == "userA:passA\nuserB:passB"
