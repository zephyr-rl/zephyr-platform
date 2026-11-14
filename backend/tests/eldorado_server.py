"""An in-process stand-in for the Eldorado and TalkJS APIs, served from the fixtures."""

from __future__ import annotations

import base64
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "eldorado"
CHAT_APP = "chatApp1"
CHAT_SELLER = "seller-talk-1"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def fake_jwt(claims: dict[str, Any]) -> str:
    def enc(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'HS256'})}.{enc(claims)}.signature"


class EldoradoServer:
    """Routes requests to fixture responses; tests can queue scripted responses per route."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.scripted: dict[tuple[str, str], list[httpx.Response]] = defaultdict(list)
        self.orders = fixture("seller_orders")["results"]
        self.vault = fixture("secret_details")
        self.tokens_issued = 0
        self.order_page_size = 2  # small pages, so tests follow the cursor like the real API

    def script(self, method: str, path: str, *responses: httpx.Response) -> None:
        self.scripted[(method, path)].extend(responses)

    def calls(self, method: str | None = None, path: str | None = None) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if (method is None or r.method == method) and (path is None or r.url.path == path)
        ]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if self.scripted.get(key):
            return self.scripted[key].pop(0)
        return self.route(request)

    def route(self, request: httpx.Request) -> httpx.Response:
        method, path, params = request.method, request.url.path, request.url.params
        if path == "/api/authentication/seller/token" and method == "POST":
            body = json.loads(request.content)
            if body != {"clientId": "client-1", "clientSecret": "secret-1"}:
                return httpx.Response(401, json={"message": "Invalid client credentials"})
            self.tokens_issued += 1
            return httpx.Response(
                200, json={"accessToken": f"access-{self.tokens_issued}", "expiresIn": 900}
            )
        if request.url.host == "www.eldorado.gg" and not request.headers.get(
            "authorization", ""
        ).startswith("Bearer access-"):
            return httpx.Response(401, json={"message": "Unauthorized"})
        if path == "/api/users/me":
            return httpx.Response(200, json=fixture("users_me"))
        if path == "/api/orders/me/seller/orders":
            rows = self.orders
            if params.get("orderState"):
                rows = [o for o in rows if o["state"]["state"] == params["orderState"]]
            if params.get("query"):
                rows = [o for o in rows if params["query"] in o["id"]] or rows[
                    :1
                ]  # fuzzy like the API
            return httpx.Response(200, json={"results": rows, "recordCount": len(rows)})
        if path.startswith("/api/orders/me/") and path.endswith("/deliver") and method == "PUT":
            order = self._order(path.split("/")[4])
            if order is None:
                return httpx.Response(404, json={"message": "Not found"})
            if order["state"]["state"] != "Paid":
                return httpx.Response(409, json={"message": "Order is not paid"})
            order["state"] = {"state": "Delivered"}
            return httpx.Response(204)
        if path.startswith("/api/orders/me/") and path.endswith("/cancel") and method == "POST":
            order = self._order(path.split("/")[4])
            if order is None:
                return httpx.Response(404)
            order["state"] = {"state": "Canceled"}
            return httpx.Response(200, json={})
        if path.startswith("/api/orders/me/"):
            order = self._order(path.split("/")[4])
            return (
                httpx.Response(200, json=order)
                if order
                else httpx.Response(404, json={"message": "Not found"})
            )
        if path == "/api/flexibleOffers/me/search":
            return httpx.Response(200, json=fixture("account_offers"))
        if path == "/api/v1/item-management/me/offers/me/search":
            return httpx.Response(200, json=fixture("item_offers"))
        if path == "/api/predefinedOffers/me":
            return httpx.Response(200, json=fixture("predefined_offers"))
        if path.endswith("/secretDetails") and method == "GET":
            return httpx.Response(200, json=self.vault)
        if path.endswith("/secretDetails") and method == "POST":
            entry = {"id": f"vault-{len(self.vault) + 1}", **json.loads(request.content)}
            self.vault.append(entry)
            return httpx.Response(201, json=entry)
        if path.startswith("/api/flexibleOffersUser/me/") or path.startswith(
            "/api/v1/item-management/me/offers/"
        ):
            return httpx.Response(200, json={})
        if path == "/api/conversations/me/authorize":
            return httpx.Response(
                200,
                json={
                    "token": fake_jwt(
                        {"iss": CHAT_APP, "sub": CHAT_SELLER, "exp": time.time() + 900}
                    )
                },
            )
        if path.startswith(f"/v1/{CHAT_APP}/conversations/") and path.endswith("/messages"):
            if method == "GET":
                return httpx.Response(200, json=fixture("talkjs_messages"))
            return httpx.Response(200, json={})
        if path.startswith("/api/orders/reviews/"):
            return httpx.Response(200, json=fixture("reviews"))
        return httpx.Response(404, json={"message": f"No route for {method} {path}"})

    def _order(self, order_id: str) -> dict[str, Any] | None:
        return next((o for o in self.orders if o["id"] == order_id), None)
