"""HTTP layer for the Eldorado seller API: OAuth token, rate limit, retries, error mapping.

Rules (invariant I4):
- Reads (GET) are retried on timeouts, 429 and 5xx with backoff.
- Writes are never retried blindly: a write that fails is reported, and the caller re-reads the
  remote state before deciding anything (see ``EldoradoConnector``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.connectors.base import ConnectorError

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.eldorado.gg"
TALKJS_BASE_URL = "https://api.talkjs.com"
USER_AGENT = "ZephyrPlatform/0.1"
TOKEN_MARGIN_SECONDS = 60
READ_RETRIES = 3


class RateLimiter:
    """Spaces requests at least ``interval`` seconds apart (one limiter per connection)."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self._interval:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(now, self._next) + self._interval


@dataclass
class _Token:
    value: str
    expires_at: float


def jwt_claims(token: str) -> dict[str, Any]:
    """Claims of a JWT, without verifying it (only used to read TalkJS app id and user id)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "title", "detail"):
            if isinstance(body.get(key), str) and body[key]:
                return str(body[key])
    if isinstance(body, str) and body:
        return body[:300]
    return fallback


class EldoradoHttp:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        requests_per_second: float = 4.0,
        timeout: float = 20.0,
        retry_base_delay: float = 0.5,
    ) -> None:
        if not client_id or not client_secret:
            raise ConnectorError(
                "Eldorado client id and client secret are required.", retryable=False
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._limiter = RateLimiter(requests_per_second)
        self._retry_base_delay = retry_base_delay
        self._token: _Token | None = None
        self._token_lock = asyncio.Lock()
        self._chat_token: _Token | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- authentication ----------------------------------------------------------------

    async def token(self, *, force: bool = False) -> str:
        async with self._token_lock:
            if (
                not force
                and self._token
                and self._token.expires_at - time.time() > TOKEN_MARGIN_SECONDS
            ):
                return self._token.value
            await self._limiter.wait()
            try:
                response = await self._http.post(
                    f"{self._base_url}/api/authentication/seller/token",
                    json={"clientId": self._client_id, "clientSecret": self._client_secret},
                )
            except httpx.HTTPError as exc:
                raise ConnectorError(f"Eldorado unreachable: {exc}", retryable=True) from exc
            body = _json(response)
            value = (
                (body.get("accessToken") or body.get("AccessToken"))
                if isinstance(body, dict)
                else None
            )
            if response.status_code != 200 or not value:
                retryable = response.status_code == 429 or response.status_code >= 500
                raise ConnectorError(
                    "Eldorado rejected the credentials: "
                    + _message(body, f"HTTP {response.status_code}"),
                    retryable=retryable,
                    status=response.status_code,
                )
            expires_in = int(body.get("expiresIn") or body.get("ExpiresIn") or 900)
            self._token = _Token(str(value), time.time() + expires_in)
            return self._token.value

    # ---- requests ----------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        """Authenticated call to the seller API. Returns the decoded JSON body."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        is_read = method.upper() == "GET"
        attempts = READ_RETRIES if is_read else 1
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            token = await self.token()
            await self._limiter.wait()
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=clean_params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.TimeoutException as exc:
                if attempt < attempts:
                    await self._backoff(attempt)
                    continue
                # A write that timed out may or may not have happened: the caller re-reads.
                raise ConnectorError(
                    f"Eldorado timed out on {method} {path}", retryable=True
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    await self._backoff(attempt)
                    continue
                raise ConnectorError(f"Eldorado unreachable: {exc}", retryable=True) from exc

            if response.status_code == 401 and not refreshed:
                refreshed = True
                await self.token(force=True)
                attempt -= 1
                continue
            if response.status_code in ok:
                return _json(response)
            body = _json(response)
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < attempts:
                await self._backoff(attempt, response)
                continue
            raise ConnectorError(
                f"Eldorado {method} {path}: " + _message(body, f"HTTP {response.status_code}"),
                retryable=retryable,
                status=response.status_code,
            )

    async def _backoff(self, attempt: int, response: httpx.Response | None = None) -> None:
        delay = self._retry_base_delay * (2 ** (attempt - 1))
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                delay = max(delay, min(float(retry_after), 30.0))
        await asyncio.sleep(delay)

    # ---- TalkJS (buyer chat) -----------------------------------------------------------

    async def chat_session(self, *, force: bool = False) -> tuple[str, str, str]:
        """(token, app_id, seller_user_id) for the TalkJS REST API, cached ~15 minutes."""
        if (
            not force
            and self._chat_token
            and self._chat_token.expires_at - time.time() > TOKEN_MARGIN_SECONDS
        ):
            token = self._chat_token.value
        else:
            body = await self.request("GET", "api/conversations/me/authorize")
            raw = body.get("token") if isinstance(body, dict) else None
            if not raw:
                raise ConnectorError("Eldorado did not return a chat token.", retryable=True)
            token = str(raw)
            expires = float(jwt_claims(token).get("exp") or time.time() + 900)
            self._chat_token = _Token(token, expires)
        claims = jwt_claims(token)
        app_id, user_id = str(claims.get("iss") or ""), str(claims.get("sub") or "")
        if not app_id or not user_id:
            raise ConnectorError(
                "The chat token does not name the chat app or the seller.", retryable=False
            )
        return token, app_id, user_id

    async def talkjs(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None
    ) -> Any:
        for attempt in (1, 2):
            token, app_id, _ = await self.chat_session(force=attempt == 2)
            await self._limiter.wait()
            try:
                response = await self._http.request(
                    method,
                    f"{TALKJS_BASE_URL}/v1/{app_id}/{path.lstrip('/')}",
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise ConnectorError(f"Chat service unreachable: {exc}", retryable=True) from exc
            if response.status_code == 401 and attempt == 1:
                continue
            if response.status_code in (200, 201, 204):
                return _json(response)
            raise ConnectorError(
                f"Chat {method} {path}: HTTP {response.status_code}",
                retryable=response.status_code == 429 or response.status_code >= 500,
                status=response.status_code,
            )
        raise ConnectorError("Chat authorization failed.", retryable=True)


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return response.text
