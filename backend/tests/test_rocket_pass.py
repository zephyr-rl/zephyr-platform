"""Tools → Rocket Pass check: the PsyNet protocol (with fakes), sessions, jobs and the API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import jobs
from app.core.audit import Actor
from app.core.platform import Platform
from app.tools.rocketpass import client as rp
from app.tools.rocketpass import service
from app.tools.rocketpass.models import CheckKind, CheckState

ACTOR = Actor.system("tests")
CODE = "0123456789abcdef0123456789abcdef"
END = 1_800_000_000


# ---- fakes -----------------------------------------------------------------------------------


class FakeEpic:
    """Answers the HTTP side: Epic, EOS, PsyNet auth, Microsoft and Xbox Live."""

    def __init__(self) -> None:
        self.refreshes = 0
        self.refresh_fails = False
        self.xbox_pending = 1  # polls answering "pending" before approval
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        body = request.content.decode()
        if url == rp.EPIC_TOKEN_URL:
            if "grant_type=refresh_token" in body:
                if self.refresh_fails:
                    return httpx.Response(
                        400,
                        json={
                            "errorCode": "errors.com.epicgames.account.auth_token.invalid_refresh_token"  # noqa: E501
                        },
                    )
                self.refreshes += 1
            elif CODE not in body:
                return httpx.Response(400, json={"errorMessage": "bad code"})
            return httpx.Response(
                200,
                json={
                    "access_token": "epic-access",
                    "refresh_token": f"epic-refresh-{self.refreshes}",
                    "refresh_expires_at": "2026-12-24T00:00:00.000Z",
                    "account_id": "acc-1",
                    "displayName": "PassOwner",
                },
            )
        if url == rp.EPIC_EXCHANGE_URL:
            return httpx.Response(200, json={"code": "exchange-1"})
        if url == rp.EOS_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "eos-access"})
        if url == rp.PSY_ENV_URL:
            return httpx.Response(503)
        if url.endswith("/rpc/Auth/AuthPlayer/v2"):
            assert request.headers["PsySig"] == rp.sign(body, rp.DEFAULT_PSY_KEY)
            return httpx.Response(
                200,
                json={
                    "Result": {
                        "PsyToken": "psy-token",
                        "SessionID": "sess",
                        "PerConURLv2": "wss://ws.test/gc2",
                    }
                },
            )
        if url == rp.MS_DEVICE_URL:
            return httpx.Response(
                200,
                json={
                    "user_code": "ABCD-EFGH",
                    "device_code": "dev-123",
                    "verification_uri": "https://microsoft.com/link",
                    "interval": 1,
                    "expires_in": 900,
                },
            )
        if url == rp.MS_TOKEN_URL:
            if "device_code" in body and self.xbox_pending > 0:
                self.xbox_pending -= 1
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(
                200, json={"access_token": "ms-access", "refresh_token": "ms-refresh"}
            )
        if url == rp.XBL_USER_URL:
            return httpx.Response(
                200,
                json={
                    "Token": "xau",
                    "DisplayClaims": {"xui": [{"uhs": "hash", "gtg": "XboxGuy"}]},
                },
            )
        if url == rp.XBL_XSTS_URL:
            return httpx.Response(200, json={"Token": "xsts"})
        if url == rp.EOS_CONNECT_URL:
            return httpx.Response(200, json={"access_token": "eos-xbox"})
        raise AssertionError(f"unexpected request {url}")


class FakePsyNet:
    """The websocket: the pass becomes premium after ``premium_after`` claims."""

    def __init__(self, premium_after: int | None = None) -> None:
        self.premium_after = premium_after
        self.claims = 0
        self.sent: list[str] = []
        self.headers: dict[str, str] = {}
        self._out: list[str] = []

    @asynccontextmanager
    async def connect(self, url: str, headers: dict[str, str]) -> AsyncIterator[FakePsyNet]:
        assert url == "wss://ws.test/gc2"
        self.headers = headers
        yield self

    async def send(self, message: str) -> None:
        self.sent.append(message)
        head, _, body = message.partition("\r\n\r\n")
        request_id = head.split("PsyRequestID: ")[1].split("\r\n")[0]
        assert f"PsySig: {rp.sign(body, rp.DEFAULT_PSY_KEY)}" in head
        if "ClaimEntitlements" in head:
            self.claims += 1
            products = [{"ProductID": 1}] if self.premium_after == self.claims else []
            self._out.append(
                f"PsyResponseID: {request_id}\r\nClaim\r\n\r\n"
                + json.dumps({"Result": {"Products": products}})
            )
        else:
            premium = self.premium_after is not None and self.claims >= self.premium_after
            info = {
                "bOwnsPremium": premium,
                "TierLevel": 42,
                "XPMultiplier": 0.2,
                "Pips": 3,
                "PipsPerLevel": 10,
            }
            self._out.append(
                f"PsyResponseID: {request_id}\r\n\r\n"
                + json.dumps({"Result": {"RocketPassInfo": info, "EndTime": END}})
            )

    async def recv(self) -> str:
        if not self._out:
            raise TimeoutError
        return self._out.pop(0)


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def epic() -> FakeEpic:
    return FakeEpic()


@pytest.fixture
def psynet() -> FakePsyNet:
    return FakePsyNet(premium_after=3)


@pytest.fixture
def fakes(
    epic: FakeEpic, psynet: FakePsyNet, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeEpic, FakePsyNet]:
    def factory(_: httpx.AsyncClient) -> rp.RocketPassClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(epic.handler))
        return rp.RocketPassClient(http, psynet.connect, sleep=_no_sleep)

    monkeypatch.setattr(service, "client_factory", factory)
    return epic, psynet


async def _run_jobs(platform: Platform) -> None:
    while await jobs.run_one(platform.db.sessionmaker, platform.jobs, "t"):
        pass


# ---- protocol ----------------------------------------------------------------------------------


def test_extract_auth_code() -> None:
    assert rp.extract_auth_code(CODE) == CODE
    assert rp.extract_auth_code(json.dumps({"authorizationCode": CODE, "sid": None})) == CODE
    assert rp.extract_auth_code(f"https://localhost/?code={CODE}&x=1") == CODE
    with pytest.raises(rp.EulaRequired):
        rp.extract_auth_code(
            '{"errorCode":"errors.com.epicgames.oauth.corrective_action_required"}'
        )
    with pytest.raises(rp.RocketPassError, match="No authorization code"):
        rp.extract_auth_code("hello")


def test_pass_info_and_feature_set_order() -> None:
    info = rp.PassInfo.from_result(
        {
            "RocketPassInfo": {"bOwnsPremium": True, "TierLevel": 70, "XPMultiplier": 0.25},
            "EndTime": END,
        }
    )
    assert info.to_dict() == {
        "has_premium": True,
        "tier": 70,
        "xp_boost_percent": 25,
        "pips": 0,
        "pips_per_level": 10,
        "season_ends_at": datetime.fromtimestamp(END, UTC).isoformat(),
    }
    assert rp._newer_feature_set("PrimeUpdate61_1", "PrimeUpdate60_1")
    assert rp._newer_feature_set("PrimeUpdate100_0", "PrimeUpdate60_1")
    assert not rp._newer_feature_set("PrimeUpdate59_9", "PrimeUpdate60_1")


async def test_claim_keeps_trying_until_the_pass_arrives(
    epic: FakeEpic, psynet: FakePsyNet
) -> None:
    client = rp.RocketPassClient(
        httpx.AsyncClient(transport=httpx.MockTransport(epic.handler)),
        psynet.connect,
        sleep=_no_sleep,
    )
    token = await client.login_with_code(CODE)
    seen: list[str] = []

    async def progress(text: str) -> None:
        seen.append(text)

    outcome = await client.check(token, claim=True, progress=progress)
    assert outcome.info is not None and outcome.info.has_premium and outcome.newly_activated
    assert psynet.claims == 3 and outcome.attempts == 2 and outcome.claimed_products == 1
    assert seen[-1] == "Waiting for the pass to arrive · attempt 2 of 60"
    assert psynet.headers["PsyToken"] == "psy-token"


async def test_plain_check_never_claims(epic: FakeEpic, psynet: FakePsyNet) -> None:
    client = rp.RocketPassClient(
        httpx.AsyncClient(transport=httpx.MockTransport(epic.handler)),
        psynet.connect,
        sleep=_no_sleep,
    )
    outcome = await client.check(await client.login_with_code(CODE))
    assert psynet.claims == 0 and outcome.info is not None and not outcome.info.has_premium
    assert outcome.info.tier == 42


async def test_claim_gives_up_after_the_attempts(epic: FakeEpic) -> None:
    never = FakePsyNet(premium_after=None)
    client = rp.RocketPassClient(
        httpx.AsyncClient(transport=httpx.MockTransport(epic.handler)),
        never.connect,
        sleep=_no_sleep,
    )
    outcome = await client.check(await client.login_with_code(CODE), claim=True, max_attempts=4)
    assert outcome.info is not None and not outcome.info.has_premium
    assert outcome.attempts == 4 and not outcome.newly_activated and never.claims == 5


# ---- sessions and jobs ---------------------------------------------------------------------


async def test_sign_in_check_and_claim(
    session: AsyncSession, platform: Platform, fakes: tuple[FakeEpic, FakePsyNet]
) -> None:
    _epic, _psynet = fakes
    client = service.client_factory(httpx.AsyncClient())
    account = await service.sign_in(session, platform.secrets, ACTOR, client, f"?code={CODE}")
    assert account.refresh_token and "epic-refresh" not in account.refresh_token  # encrypted
    first = await service.start(
        session, platform.secrets, ACTOR, client, account.id, CheckKind.check
    )
    await session.commit()
    await _run_jobs(platform)

    await session.refresh(first)
    assert first.state is CheckState.done and first.result is not None
    assert first.result["has_premium"] is False and first.result["tier"] == 42
    await session.refresh(account)
    assert account.last_result == first.result
    # The refresh token rotated and was saved again (encrypted).
    assert (
        platform.secrets.decrypt(account.refresh_token, context="rp-epic:acc-1") == "epic-refresh-1"
    )

    claim = await service.start(
        session, platform.secrets, ACTOR, client, account.id, CheckKind.claim
    )
    await session.commit()
    await _run_jobs(platform)
    await session.refresh(claim)
    assert claim.state is CheckState.done and claim.result is not None
    assert claim.result["has_premium"] and claim.result["newly_activated"]


async def test_expired_session_asks_to_sign_in_again(
    session: AsyncSession, platform: Platform, fakes: tuple[FakeEpic, FakePsyNet]
) -> None:
    epic, _ = fakes
    client = service.client_factory(httpx.AsyncClient())
    account = await service.sign_in(session, platform.secrets, ACTOR, client, CODE)
    check = await service.start(
        session, platform.secrets, ACTOR, client, account.id, CheckKind.check
    )
    await session.commit()
    epic.refresh_fails = True
    await _run_jobs(platform)
    await session.refresh(check)
    await session.refresh(account)
    assert check.state is CheckState.failed and "Sign in to this account again" in (
        check.error or ""
    )
    assert account.refresh_token is None
    with pytest.raises(rp.SessionExpired):
        await service.start(session, platform.secrets, ACTOR, client, account.id, CheckKind.check)


async def test_xbox_sync_waits_for_the_microsoft_code(
    session: AsyncSession, platform: Platform, fakes: tuple[FakeEpic, FakePsyNet]
) -> None:
    epic, _psynet = fakes
    client = service.client_factory(httpx.AsyncClient())
    account = await service.sign_in(session, platform.secrets, ACTOR, client, CODE)
    check = await service.start(
        session, platform.secrets, ACTOR, client, account.id, CheckKind.xbox
    )
    await session.commit()
    assert check.state is CheckState.waiting and check.xbox_user_code == "ABCD-EFGH"
    assert check.xbox_device_code and "dev-123" not in check.xbox_device_code

    # First poll: still pending (the job is re-queued without counting as a failure).
    sm = platform.db.sessionmaker
    with pytest.raises(jobs.RetryLater):
        await service.wait_for_xbox(sm, platform.secrets, client, check.id, 1)
    await service.wait_for_xbox(sm, platform.secrets, client, check.id, 1)  # approved
    await _run_jobs(platform)  # the queued run (and the leftover wait job, now a no-op)

    await session.refresh(check)
    await session.refresh(account)
    assert check.state is CheckState.done and check.result is not None
    assert check.result["xbox_gamertag"] == "XboxGuy" and check.result["has_premium"]
    assert account.xbox_gamertag == "XboxGuy" and account.xbox_refresh_token
    assert rp.EOS_CONNECT_URL in epic.requests

    # Next time no Microsoft code is needed.
    again = await service.start(
        session, platform.secrets, ACTOR, client, account.id, CheckKind.xbox
    )
    assert again.state is CheckState.queued and again.xbox_user_code is None


# ---- API -----------------------------------------------------------------------------------


async def test_api_flow(
    auth_client: httpx.AsyncClient, platform: Platform, fakes: tuple[FakeEpic, FakePsyNet]
) -> None:
    bad = await auth_client.post(
        "/api/v1/tools/rocket-pass/accounts", json={"code": "not a code at all"}
    )
    assert bad.status_code == 400 and "No authorization code" in bad.json()["detail"]

    r = await auth_client.post("/api/v1/tools/rocket-pass/accounts", json={"code": CODE})
    assert r.status_code == 201, r.text
    check = r.json()
    assert check["state"] == "queued" and check["display_name"] == "PassOwner"
    await _run_jobs(platform)
    done = (await auth_client.get(f"/api/v1/tools/rocket-pass/checks/{check['id']}")).json()
    assert done["state"] == "done" and done["result"]["tier"] == 42

    accounts = (await auth_client.get("/api/v1/tools/rocket-pass/accounts")).json()
    assert (
        len(accounts) == 1 and accounts[0]["signed_in"] and accounts[0]["last_result"]["tier"] == 42
    )
    assert "refresh" not in json.dumps(accounts)

    claim = await auth_client.post(
        f"/api/v1/tools/rocket-pass/accounts/{accounts[0]['id']}/checks", json={"kind": "claim"}
    )
    assert claim.status_code == 201
    await _run_jobs(platform)
    history = (await auth_client.get("/api/v1/tools/rocket-pass/checks")).json()
    ours = [h for h in history if h["id"] in (check["id"], claim.json()["id"])]
    assert [h["kind"] for h in ours] == ["claim", "check"]
    assert ours[0]["result"]["has_premium"] is True

    gone = await auth_client.delete(f"/api/v1/tools/rocket-pass/accounts/{accounts[0]['id']}")
    assert gone.status_code == 204
    assert (await auth_client.get("/api/v1/tools/rocket-pass/accounts")).json() == []
    # The history stays, without the account link.
    after = (await auth_client.get("/api/v1/tools/rocket-pass/checks")).json()
    assert [h["epic_account_id"] for h in after if h["id"] == check["id"]] == [None]


async def test_failed_check_keeps_a_log(
    session: AsyncSession, platform: Platform, fakes: tuple[FakeEpic, FakePsyNet]
) -> None:
    epic, _ = fakes
    client = service.client_factory(httpx.AsyncClient())
    account = await service.sign_in(session, platform.secrets, ACTOR, client, CODE)
    ok = await service.start(session, platform.secrets, ACTOR, client, account.id, CheckKind.check)
    await session.commit()
    await _run_jobs(platform)
    await session.refresh(ok)
    lines = [e["message"] for e in ok.log]
    assert lines[0] == "Started: check"
    assert any(
        "account-public-service-prod.ol.epicgames.com" in m and "HTTP 200" in m for m in lines
    )
    assert any(m.startswith("→ RocketPass/GetPlayerInfo v2") for m in lines)
    assert any("pass premium=False tier=42" in m for m in lines)
    assert lines[-1].startswith("Done: premium=False")
    assert not any("epic-access" in m or "epic-refresh" in m or "eos-access" in m for m in lines)

    # PsyNet refuses the sign-in: the log has the HTTP status and the error fields.
    original = epic.handler

    def refusing(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/rpc/Auth/AuthPlayer/v2"):
            return httpx.Response(
                401, json={"Error": {"Type": "InvalidAuthTicket", "Message": "bad"}}
            )
        return original(request)

    epic.handler = refusing  # type: ignore[method-assign]
    bad = await service.start(session, platform.secrets, ACTOR, client, account.id, CheckKind.check)
    await session.commit()
    await _run_jobs(platform)
    await session.refresh(bad)
    assert bad.state is CheckState.failed and "InvalidAuthTicket" in (bad.error or "")
    errors = [e for e in bad.log if e["level"] == "error"]
    assert any(
        "HTTP 401" in e["message"] and "Error.Type=InvalidAuthTicket" in e["message"]
        for e in errors
    )
    assert errors[-1]["message"].startswith("RocketPassError:")


async def test_unexpected_error_ends_the_check(
    session: AsyncSession,
    platform: Platform,
    fakes: tuple[FakeEpic, FakePsyNet],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = service.client_factory(httpx.AsyncClient())
    account = await service.sign_in(session, platform.secrets, ACTOR, client, CODE)
    check = await service.start(
        session, platform.secrets, ACTOR, client, account.id, CheckKind.check
    )
    await session.commit()

    async def broken(*args: object, **kwargs: object) -> None:
        raise KeyError("PerConURLv2")

    monkeypatch.setattr(rp.RocketPassClient, "check", broken)
    await _run_jobs(platform)
    await session.refresh(check)
    assert check.state is CheckState.failed
    assert check.error == "Unexpected error: KeyError: 'PerConURLv2'"
    assert check.log[-1] == {**check.log[-1], "level": "error", "message": check.error}
