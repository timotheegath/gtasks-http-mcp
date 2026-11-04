"""Async httpx client for the Google Tasks API v1 with OAuth 2.0 refresh-token auth."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

TASKS_API_BASE = "https://tasks.googleapis.com"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
DEFAULT_LIST_ID = "@default"
TOKEN_EXPIRY_MARGIN_S = 60


class GoogleAPIError(Exception):
    """Structured API error; `error` is a stable machine-readable code."""

    def __init__(self, error: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.error = error
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.error, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


class AuthExpiredError(GoogleAPIError):
    def __init__(self, consent_url: str):
        super().__init__(
            "auth_expired",
            "Google authorization expired or was revoked. Re-run auth.py to obtain a new GOOGLE_REFRESH_TOKEN.",
            {"consent_url": consent_url},
        )


def consent_url(client_id: str) -> str:
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:4466",
            "response_type": "code",
            "scope": TASKS_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
    )


def parse_due_date(value: str) -> date:
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise GoogleAPIError(
            "invalid_request",
            f"Invalid date {value!r}: expected YYYY-MM-DD, YYYY-MM-DDTHH:MM, or full RFC3339.",
        ) from None
    return dt.date()


def normalize_due(value: str) -> str:
    """Normalize YYYY-MM-DD / partial / full RFC3339 to RFC3339 UTC (naive input = UTC)."""
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise GoogleAPIError(
            "invalid_request",
            f"Invalid due date {value!r}: expected YYYY-MM-DD, YYYY-MM-DDTHH:MM, or full RFC3339.",
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class GoogleTasksClient:
    """Thin async wrapper over the Google Tasks API v1 REST endpoints."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        base_url: str = TASKS_API_BASE,
        token_url: str = OAUTH_TOKEN_URL,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._base_url = base_url.rstrip("/")
        self._token_url = token_url
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._refresh_lock = asyncio.Lock()

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "GoogleTasksClient":
        env = os.environ if environ is None else environ
        return cls(
            client_id=env["GOOGLE_CLIENT_ID"],
            client_secret=env["GOOGLE_CLIENT_SECRET"],
            refresh_token=env["GOOGLE_REFRESH_TOKEN"],
            base_url=env.get("GTASKS_API_BASE", TASKS_API_BASE),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _refresh_access_token(self, force: bool = False) -> str:
        async with self._refresh_lock:
            if not force and self._access_token and time.time() < self._token_expires_at:
                return self._access_token
            try:
                resp = await self._http.post(
                    self._token_url,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": self._refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
            except httpx.HTTPError as exc:
                raise GoogleAPIError(
                    "upstream_unavailable",
                    f"OAuth token endpoint unreachable ({exc.__class__.__name__}).",
                ) from exc
            if resp.status_code in (400, 401, 403):
                raise AuthExpiredError(consent_url(self._client_id))
            if resp.status_code != 200:
                raise GoogleAPIError(
                    "upstream_unavailable",
                    f"OAuth token endpoint returned {resp.status_code}.",
                    {"status": resp.status_code},
                )
            data = resp.json()
            self._access_token = data["access_token"]
            expires_in = int(data.get("expires_in", 3600))
            self._token_expires_at = time.time() + max(expires_in - TOKEN_EXPIRY_MARGIN_S, 0)
            return self._access_token

    async def _access_token_now(self) -> str:
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token
        return await self._refresh_access_token()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> Any:
        token = await self._access_token_now()
        try:
            resp = await self._http.request(
                method,
                self._base_url + path,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise GoogleAPIError(
                "upstream_unavailable",
                f"Google Tasks API unreachable ({exc.__class__.__name__}).",
            ) from exc
        if resp.status_code == 401 and not _retried:
            await self._refresh_access_token(force=True)
            return await self.request(method, path, params=params, json_body=json_body, _retried=True)
        return self._check(resp, context=f"{method} {path}")

    def _check(self, resp: httpx.Response, context: str) -> Any:
        if resp.status_code < 400:
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        try:
            message = resp.json().get("error", {}).get("message", "")
        except ValueError:
            message = resp.text[:200]
        status = resp.status_code
        if status == 404 or (status == 400 and "not found" in message.lower()):
            raise GoogleAPIError(
                "not_found", f"Not found ({context}): {message}".strip(), {"status": status}
            )
        if status == 400:
            raise GoogleAPIError("invalid_request", message or "Invalid request.", {"status": status})
        if status == 401:
            raise AuthExpiredError(consent_url(self._client_id))
        if status in (403, 429):
            detail: dict[str, Any] = {"status": status}
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                detail["retry_after"] = retry_after
            raise GoogleAPIError(
                "rate_limited", message or "Rate limited or quota exceeded.", detail
            )
        raise GoogleAPIError(
            "api_error", message or f"Google API error {status}.", {"status": status}
        )

    async def list_tasklists(
        self, max_results: int = 100, page_token: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return await self.request("GET", "/tasks/v1/users/@me/lists", params=params)

    async def get_tasklist(self, tasklist_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/tasks/v1/lists/{tasklist_id}")

    async def insert_tasklist(self, title: str) -> dict[str, Any]:
        return await self.request("POST", "/tasks/v1/users/@me/lists", json_body={"title": title})

    async def patch_tasklist(self, tasklist_id: str, title: str) -> dict[str, Any]:
        return await self.request(
            "PATCH", f"/tasks/v1/lists/{tasklist_id}", json_body={"title": title}
        )

    async def delete_tasklist(self, tasklist_id: str) -> None:
        await self.request("DELETE", f"/tasks/v1/lists/{tasklist_id}")

    async def list_tasks(
        self,
        tasklist_id: str,
        *,
        show_completed: bool | None = None,
        show_hidden: bool = False,
        due_min: str | None = None,
        due_max: str | None = None,
        updated_min: str | None = None,
        max_results: int = 100,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": max_results, "showHidden": str(show_hidden).lower()}
        if show_completed is not None:
            params["showCompleted"] = str(show_completed).lower()
        if due_min:
            params["dueMin"] = due_min
        if due_max:
            params["dueMax"] = due_max
        if updated_min:
            params["updatedMin"] = updated_min
        if page_token:
            params["pageToken"] = page_token
        return await self.request("GET", f"/tasks/v1/lists/{tasklist_id}/tasks", params=params)

    async def get_task(self, tasklist_id: str, task_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/tasks/v1/lists/{tasklist_id}/tasks/{task_id}")

    async def insert_task(self, tasklist_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request(
            "POST", f"/tasks/v1/lists/{tasklist_id}/tasks", json_body=body
        )

    async def patch_task(
        self, tasklist_id: str, task_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.request(
            "PATCH", f"/tasks/v1/lists/{tasklist_id}/tasks/{task_id}", json_body=body
        )

    async def delete_task(self, tasklist_id: str, task_id: str) -> None:
        await self.request("DELETE", f"/tasks/v1/lists/{tasklist_id}/tasks/{task_id}")

    async def move_task(
        self,
        tasklist_id: str,
        task_id: str,
        parent_task_id: str | None = None,
        previous_task_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if parent_task_id:
            params["parent"] = parent_task_id
        if previous_task_id:
            params["previous"] = previous_task_id
        return await self.request(
            "POST", f"/tasks/v1/lists/{tasklist_id}/tasks/{task_id}/move", params=params
        )

    async def clear_completed(self, tasklist_id: str) -> None:
        await self.request("POST", f"/tasks/v1/lists/{tasklist_id}/clear")
