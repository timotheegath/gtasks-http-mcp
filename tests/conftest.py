import asyncio
import json

import httpx
import pytest

import server
from google_client import GoogleTasksClient

TOKEN_URL = "https://oauth2.googleapis.com/token"


def run(coro):
    return asyncio.run(coro)


def req_json(request: httpx.Request):
    return json.loads(request.content)


class FakeGoogle:
    """MockTransport handler: serves the OAuth token endpoint and registered routes."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.routes: list[tuple[str, str, object]] = []
        self.invalid_grant = False
        self.unauthorized_once = False
        self.unauthorized_always = False
        self.raise_timeout = False

    def on(self, method: str, path_sub: str, responder):
        self.routes.append((method, path_sub, responder))
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(TOKEN_URL):
            if self.raise_timeout:
                raise httpx.ConnectTimeout("timed out", request=request)
            self.token_calls += 1
            if self.invalid_grant:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{self.token_calls}",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        if self.raise_timeout:
            raise httpx.ConnectTimeout("timed out", request=request)
        if self.unauthorized_always or self.unauthorized_once:
            self.unauthorized_once = False
            return httpx.Response(401, json={"error": {"message": "Request had invalid credentials."}})
        self.requests.append(request)
        for method, path_sub, responder in self.routes:
            if request.method == method and path_sub in request.url.path:
                return responder(request)
        return httpx.Response(
            404, json={"error": {"message": f"no route for {request.method} {request.url.path}"}}
        )


@pytest.fixture
def fake(monkeypatch):
    fg = FakeGoogle()
    client = GoogleTasksClient(
        client_id="cid",
        client_secret="sec",
        refresh_token="rt",
        base_url="https://tasks.googleapis.com",
        token_url=TOKEN_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fg.handler)),
    )
    monkeypatch.setattr(server, "_client", client)
    return fg


@pytest.fixture
def client(fake):
    return server.get_client()
