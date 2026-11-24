"""Talks to Epic Games, Psyonix (PsyNet) and Xbox Live to read and claim the Rocket Pass.

A port of the owner's RocketPassChecker (check_pass.js). The flow:

1. Epic: an authorization code (from the browser login) or a saved refresh token gives an Epic
   access token (Epic Games Launcher client).
2. Epic exchange code → Rocket League EOS token.
3. PsyNet ``Auth/AuthPlayer/v2`` (signed with an HMAC key) → a websocket session.
4. Over the websocket: ``RocketPass/GetPlayerInfo v2`` reads the pass; ``Microtransaction/
   ClaimEntitlements v2`` claims a pending pass (e.g. from Fortnite Crew).
5. Xbox (optional): a Microsoft device code sign-in, then Xbox Live user + XSTS tokens handed to
   Epic Online Services, so an Xbox Fortnite Crew reaches the Epic account without opening the
   game in Xbox Cloud.

These are the games' own private APIs: if Psyonix changes them, this stops working until it is
updated (the same as the original script). Tokens never leave this module except to be saved
encrypted by the caller; nothing here logs them.
"""

# ruff: noqa: S105  (public client ids and endpoint names of the official apps, not secrets)
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import websockets

# Public client ids used by the official apps (the same values as the original script).
EPIC_LAUNCHER_CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
EPIC_LAUNCHER_SECRET = "daafbccc737745039dffe53d94fc76cf"
RL_CLIENT_ID = "xyza7891p5D7s9R6Gm6moTHWGloerp7B"
RL_CLIENT_SECRET = "Knh18du4NVlFs+3uQ+ZPpDCVto0WYf4yXP8+OcwVt1o"
RL_DEPLOYMENT_ID = "da32ae9c12ae40e8a112c52e1f17f3ba"
MS_CLIENT_ID = "000000004c12ae6f"
MS_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"

EPIC_TOKEN_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
EPIC_EXCHANGE_URL = (
    "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/exchange"
)
EOS_TOKEN_URL = "https://api.epicgames.dev/epic/oauth/v2/token"
EOS_CONNECT_URL = "https://api.epicgames.dev/auth/v1/oauth/token"
PSY_ENV_URL = "https://psy.jaren.wtf/rlpp/env"
MS_DEVICE_URL = "https://login.live.com/oauth20_connect.srf"
MS_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
XBL_USER_URL = "https://user.auth.xboxlive.com/user/authenticate"
XBL_XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"

AUTH_URL = (
    "https://www.epicgames.com/id/login?redirectUrl="
    "https%3A%2F%2Fwww.epicgames.com%2Fid%2Fapi%2Fredirect%3FclientId%3D"
    f"{EPIC_LAUNCHER_CLIENT_ID}%26responseType%3Dcode"
)

DEFAULT_PSY_KEY = "c338bd36fb8c42b1a431d30add939fc7"
DEFAULT_BUILD_ID = "-1885667689"
DEFAULT_BUILD_SECRET = "76656a5b51ec57a13549eef732c8a8367638aa3a7989bade1f7e4e5f93d57625"
DEFAULT_FEATURE_SET = "PrimeUpdate60_1"
DEFAULT_PSY_HOST = "api.rlpp.psynet.gg"
DEFAULT_WS_URL = "wss://ws.rlpp.psynet.gg/ws/gc2"
PSYNET_USER_AGENT = "RL Win/261013.63066.530866 gzip (x86_64-pc-win32) curl-7.67.0 Schannel"


def _basic(client_id: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()


EPIC_BASIC = _basic(EPIC_LAUNCHER_CLIENT_ID, EPIC_LAUNCHER_SECRET)
RL_BASIC = _basic(RL_CLIENT_ID, RL_CLIENT_SECRET)


class RocketPassError(Exception):
    """A failure the owner can read and act on."""


class EulaRequired(RocketPassError):
    def __init__(self) -> None:
        super().__init__(
            "Epic requires this account to accept its updated terms first. Sign in at "
            "epicgames.com, accept them, then get a new code."
        )


class SessionExpired(RocketPassError):
    def __init__(self) -> None:
        super().__init__("The saved Epic session expired. Sign in to this account again.")


def extract_auth_code(text: str) -> str:
    """The 32-hex authorization code from what was pasted: the code, the page JSON or a URL."""
    raw = (text or "").strip()
    if "EULA_ACCEPTANCE" in raw or "corrective_action_required" in raw:
        raise EulaRequired()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("authorizationCode"):
            return str(parsed["authorizationCode"])
    except ValueError:
        pass
    for pattern in (
        r"[?&]code=([a-f0-9]{32})",
        r'"authorizationCode"\s*:\s*"([a-f0-9]{32})"',
        r"\b([a-f0-9]{32})\b",
    ):
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            return match.group(1)
    raise RocketPassError(
        "No authorization code found. Paste the code (32 letters and numbers) or the whole page."
    )


def sign(body: str, key: str) -> str:
    digest = hmac.new(key.encode(), f"-{body}".encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


@dataclass(frozen=True)
class PsyEnv:
    host: str = DEFAULT_PSY_HOST
    build_id: str = DEFAULT_BUILD_ID
    build_secret: str = DEFAULT_BUILD_SECRET
    feature_set: str = DEFAULT_FEATURE_SET
    hmac_key: str = DEFAULT_PSY_KEY


def _newer_feature_set(candidate: str, current: str) -> bool:
    def parts(value: str) -> list[Any]:
        return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", value.lower()) if p]

    try:
        return parts(candidate) > parts(current)
    except TypeError:
        return False


@dataclass(frozen=True)
class PassInfo:
    has_premium: bool
    tier: int
    xp_multiplier: float
    pips: int
    pips_per_level: int
    season_ends_at: datetime | None

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> PassInfo:
        info = result.get("RocketPassInfo") or {}
        end = result.get("EndTime")
        return cls(
            has_premium=bool(info.get("bOwnsPremium")),
            tier=int(info.get("TierLevel") or 0),
            xp_multiplier=float(info.get("XPMultiplier") or 0),
            pips=int(info.get("Pips") or 0),
            pips_per_level=int(info.get("PipsPerLevel") or 10),
            season_ends_at=datetime.fromtimestamp(int(end), UTC) if end else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_premium": self.has_premium,
            "tier": self.tier,
            "xp_boost_percent": round(self.xp_multiplier * 100),
            "pips": self.pips,
            "pips_per_level": self.pips_per_level,
            "season_ends_at": self.season_ends_at.isoformat() if self.season_ends_at else None,
        }


@dataclass
class CheckOutcome:
    info: PassInfo | None
    claimed_products: int = 0
    newly_activated: bool = False
    attempts: int = 0


@dataclass
class EpicToken:
    access_token: str
    refresh_token: str
    refresh_expires_at: datetime | None
    account_id: str
    display_name: str

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> EpicToken:
        expires = data.get("refresh_expires_at")
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or ""),
            refresh_expires_at=datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if isinstance(expires, str)
            else None,
            account_id=str(data.get("account_id") or ""),
            display_name=str(data.get("displayName") or data.get("account_id") or ""),
        )


class Socket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...


Connect = Callable[[str, dict[str, str]], Any]  # returns an async context manager of Socket
Progress = Callable[[str], Awaitable[None]]


def _ws_connect(url: str, headers: dict[str, str]) -> Any:
    return websockets.connect(
        url,
        additional_headers=headers,
        user_agent_header=PSYNET_USER_AGENT,
        open_timeout=15,
        ping_interval=None,
        max_size=8 * 1024 * 1024,
    )


def _frame(service: str, request_id: str, body: dict[str, Any], key: str) -> str:
    text = json.dumps(body, separators=(",", ":"))
    head = f"PsyService: {service}\r\nPsyRequestID: {request_id}\r\nPsySig: {sign(text, key)}"
    return f"{head}\r\n\r\n{text}"


def _parse(message: str | bytes) -> tuple[str, dict[str, Any]] | None:
    text = message.decode() if isinstance(message, bytes) else message
    if text.startswith("PsyPing:"):
        return None
    head, sep, body = text.partition("\r\n\r\n")
    if not sep:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return head, data if isinstance(data, dict) else {}


class RocketPassClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        connect: Connect = _ws_connect,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._connect = connect
        self._sleep = sleep

    # ---- Epic -----------------------------------------------------------------------------

    async def _epic_token(self, form: dict[str, str]) -> dict[str, Any]:
        r = await self._http.post(
            EPIC_TOKEN_URL,
            data=form,
            headers={"Authorization": EPIC_BASIC},
        )
        data = _json(r)
        if not data.get("access_token"):
            code = str(data.get("errorCode") or "")
            if "corrective_action_required" in code:
                raise EulaRequired()
            if form["grant_type"] == "refresh_token":
                raise SessionExpired()
            message = data.get("errorMessage") or data.get("error_description") or r.status_code
            raise RocketPassError(f"Epic refused the code: {message}")
        return data

    async def login_with_code(self, pasted: str) -> EpicToken:
        code = extract_auth_code(pasted)
        data = await self._epic_token({"grant_type": "authorization_code", "code": code})
        return EpicToken.from_response(data)

    async def refresh(self, refresh_token: str) -> EpicToken:
        data = await self._epic_token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        return EpicToken.from_response(data)

    async def psy_env(self) -> PsyEnv:
        """Current PsyNet keys from a community mirror; the built-in ones when it is down."""
        try:
            r = await self._http.get(PSY_ENV_URL, timeout=3)
            data = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            data = {}
        feature = str(data.get("featureSet") or "")
        if not feature or not _newer_feature_set(feature, DEFAULT_FEATURE_SET):
            return PsyEnv()
        return PsyEnv(
            host=str(data.get("host") or DEFAULT_PSY_HOST),
            build_id=str(data.get("buildId") or DEFAULT_BUILD_ID),
            build_secret=str(data.get("buildSecret") or DEFAULT_BUILD_SECRET),
            feature_set=feature,
            hmac_key=str((data.get("hmac") or {}).get("request") or DEFAULT_PSY_KEY),
        )

    async def _eos_token(self, token: EpicToken) -> str:
        r = await self._http.get(
            EPIC_EXCHANGE_URL, headers={"Authorization": f"bearer {token.access_token}"}
        )
        exchange = _json(r).get("code")
        if not exchange:
            raise RocketPassError("Epic did not give an exchange code. Try again in a minute.")
        r = await self._http.post(
            EOS_TOKEN_URL,
            data={
                "grant_type": "exchange_code",
                "exchange_code": str(exchange),
                "deployment_id": RL_DEPLOYMENT_ID,
                "scope": "basic_profile",
            },
            headers={"Authorization": RL_BASIC},
        )
        access = _json(r).get("access_token")
        if not access:
            raise RocketPassError("Rocket League sign-in failed (EOS token).")
        return str(access)

    async def _auth_player(self, token: EpicToken, eos: str, env: PsyEnv) -> dict[str, Any]:
        payload = {
            "Platform": "Epic",
            "PlayerName": token.display_name,
            "PlayerID": token.account_id,
            "Language": "INT",
            "AuthTicket": eos,
            "BuildRegion": "",
            "FeatureSet": env.feature_set,
            "Device": "PC",
            "LocalFirstPlayerID": f"Epic|{token.account_id}|0",
            "bSkipAuth": False,
            "bSetAsPrimaryAccount": True,
            "EpicAuthTicket": eos,
            "EpicAccountID": token.account_id,
        }
        body = json.dumps(payload, separators=(",", ":"))
        r = await self._http.post(
            f"https://{env.host}/rpc/Auth/AuthPlayer/v2",
            content=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "PsyBuildID": env.build_id,
                "PsyBuildSecret": env.build_secret,
                "PsyEnvironment": "Prod",
                "PsyFeatureSet": env.feature_set,
                "PsyRequestID": "PsyNetMessage_RP_Auth",
                "PsySig": sign(body, env.hmac_key),
                "User-Agent": PSYNET_USER_AGENT,
            },
        )
        data = _json(r)
        if r.status_code != 200 or data.get("Error"):
            kind = (
                (data.get("Error") or {}).get("Type")
                if isinstance(data.get("Error"), dict)
                else None
            )
            raise RocketPassError(
                f"Rocket League servers refused the sign-in ({kind or r.status_code})."
            )
        result = data.get("Result") or data
        return result if isinstance(result, dict) else {}

    # ---- The pass ----------------------------------------------------------------------------

    async def check(
        self,
        token: EpicToken,
        *,
        claim: bool = False,
        max_attempts: int = 60,
        interval: float = 2.0,
        progress: Progress | None = None,
    ) -> CheckOutcome:
        """Read the pass; with ``claim``, claim pending entitlements and keep trying until the
        pass shows as owned or ``max_attempts`` run out (about two minutes by default)."""
        eos = await self._eos_token(token)
        env = await self.psy_env()
        auth = await self._auth_player(token, eos, env)
        url = str(auth.get("PerConURLv2") or DEFAULT_WS_URL)
        headers = {
            "PsyBuildID": env.build_id,
            "PsyBuildSecret": env.build_secret,
            "PsyEnvironment": "Prod",
            "PsyToken": str(auth.get("PsyToken") or ""),
            "PsySessionID": str(auth.get("SessionID") or ""),
        }
        player = f"Epic|{token.account_id}|0"
        outcome = CheckOutcome(info=None)

        async with self._connect(url, headers) as ws:

            async def request(service: str, request_id: str, body: dict[str, Any]) -> None:
                await ws.send(_frame(service, request_id, body, env.hmac_key))

            async def read_until(request_id: str, seconds: float) -> dict[str, Any] | None:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + seconds
                while (left := deadline - loop.time()) > 0:
                    try:
                        message = await asyncio.wait_for(ws.recv(), left)
                    except TimeoutError:
                        return None
                    parsed = _parse(message)
                    if parsed is None:
                        continue
                    head, data = parsed
                    result = data.get("Result") or {}
                    if "Claim" in head:
                        outcome.claimed_products += len(result.get("Products") or [])
                    if result.get("RocketPassInfo") is not None:
                        outcome.info = PassInfo.from_result(result)
                    if request_id in head:
                        return data
                return None

            async def claim_entitlements(request_id: str) -> None:
                await request(
                    "Microtransaction/ClaimEntitlements v2",
                    request_id,
                    {"PlayerID": player, "AuthCode": eos},
                )

            if claim:
                await claim_entitlements("RP_Sync_Claim")
                await read_until("RP_Sync_Claim", 5)
            await request("RocketPass/GetPlayerInfo v2", "RP_Initial_Check", {"PlayerID": player})
            await read_until("RP_Initial_Check", 15)
            if outcome.info is None:
                raise RocketPassError("Rocket League did not answer with the pass details.")
            if outcome.info.has_premium or not claim:
                return outcome

            for attempt in range(1, max_attempts + 1):
                outcome.attempts = attempt
                if progress is not None:
                    await progress(
                        f"Waiting for the pass to arrive · attempt {attempt} of {max_attempts}"
                    )
                await claim_entitlements(f"RP_Claim_{attempt}")
                await self._sleep(0.3)
                await request(
                    "RocketPass/GetPlayerInfo v2", f"RP_Check_{attempt}", {"PlayerID": player}
                )
                await read_until(f"RP_Check_{attempt}", max(interval, 1.0) + 3)
                if outcome.info is not None and outcome.info.has_premium:
                    outcome.newly_activated = True
                    return outcome
                await self._sleep(interval)
        return outcome

    # ---- Xbox --------------------------------------------------------------------------------

    async def xbox_device_code(self) -> dict[str, Any]:
        r = await self._http.post(
            MS_DEVICE_URL,
            data={"client_id": MS_CLIENT_ID, "scope": MS_SCOPE, "response_type": "device_code"},
        )
        data = _json(r)
        if not data.get("user_code"):
            raise RocketPassError("Microsoft did not give a sign-in code. Try again.")
        return data

    async def xbox_poll(self, device_code: str) -> dict[str, Any] | None:
        """The Microsoft tokens once the owner approved; None while still waiting."""
        r = await self._http.post(
            MS_TOKEN_URL,
            data={
                "client_id": MS_CLIENT_ID,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
        )
        data = _json(r)
        if data.get("access_token"):
            return data
        if data.get("error") in ("authorization_pending", "slow_down"):
            return None
        if data.get("error") == "expired_token":
            raise RocketPassError("The Microsoft code expired before it was used. Start again.")
        raise RocketPassError(
            f"Microsoft sign-in failed: {data.get('error_description') or data.get('error')}"
        )

    async def xbox_refresh(self, refresh_token: str) -> dict[str, Any]:
        r = await self._http.post(
            MS_TOKEN_URL,
            data={
                "client_id": MS_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": MS_SCOPE,
            },
        )
        data = _json(r)
        if not data.get("access_token"):
            raise RocketPassError("The saved Xbox sign-in expired. Link Xbox again.")
        return data

    async def xbox_sync(self, ms_access_token: str) -> str:
        """Hand the Xbox Live tokens to Epic Online Services. Returns the gamertag."""
        ticket = ms_access_token if ms_access_token.startswith("t=") else f"t={ms_access_token}"
        r = await self._http.post(
            XBL_USER_URL,
            json={
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": ticket,
                },
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
            },
            headers={"Accept": "application/json"},
        )
        user = _json(r)
        if not user.get("Token"):
            raise RocketPassError("Xbox Live sign-in failed.")
        claims = ((user.get("DisplayClaims") or {}).get("xui") or [{}])[0]
        user_hash, gamertag = claims.get("uhs"), claims.get("gtg") or "Xbox player"
        xsts = None
        for party in ("http://xboxlive.com", "http://mp.microsoft.com/"):
            r = await self._http.post(
                XBL_XSTS_URL,
                json={
                    "Properties": {"SandboxId": "RETAIL", "UserTokens": [user["Token"]]},
                    "RelyingParty": party,
                    "TokenType": "JWT",
                },
                headers={"Accept": "application/json"},
            )
            xsts = _json(r).get("Token")
            if xsts:
                break
        if not xsts:
            raise RocketPassError("Xbox Live did not give a security token (XSTS).")
        await self._http.post(
            EOS_CONNECT_URL,
            data={
                "grant_type": "external_auth",
                "external_auth_type": "xbl_xsts_token",
                "external_auth_token": f"XBL3.0 x={user_hash};{xsts}",
                "deployment_id": RL_DEPLOYMENT_ID,
                "nonce": secrets.token_hex(16),
            },
            headers={"Authorization": RL_BASIC},
        )
        return str(gamertag)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
