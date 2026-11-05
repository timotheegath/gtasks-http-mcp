"""gtasks-mcp: streamable-HTTP MCP server wrapping the full Google Tasks API v1."""

from __future__ import annotations

import asyncio
import functools
import hmac
import ipaddress
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from google_client import (
    DEFAULT_LIST_ID,
    GoogleAPIError,
    GoogleTasksClient,
    load_env_file,
    normalize_due,
    parse_due_date,
)

__version__ = "0.1.0"

REQUIRED_ENV = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
MAX_PAGE = 100
DEFAULT_MAX_RESULTS = 50
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
MIN_TOKEN_LENGTH = 16
OPEN_PATHS = ("/healthz",)

mcp = FastMCP("gtasks-mcp", version=__version__)

_client: GoogleTasksClient | None = None


def get_client() -> GoogleTasksClient:
    global _client
    if _client is None:
        _client = GoogleTasksClient.from_env()
    return _client


def log_event(**fields: Any) -> None:
    fields["ts"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(fields, separators=(",", ":")), flush=True)


def resolve_list(tasklist_id: str | None) -> str:
    return tasklist_id or os.getenv("GTASKS_DEFAULT_LIST") or DEFAULT_LIST_ID


def structured_errors(fn):
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except GoogleAPIError as exc:
            return exc.to_dict()
        except Exception as exc:
            log_event(event="tool_exception", tool=fn.__name__, exc_type=type(exc).__name__)
            return {
                "error": "internal_error",
                "message": "Unexpected server error.",
                "detail": {"type": type(exc).__name__},
            }

    return wrapper


def logged(fn):
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        status = "ok"
        try:
            result = await fn(*args, **kwargs)
            if isinstance(result, dict) and result.get("error"):
                status = str(result["error"])
            return result
        except Exception:
            status = "internal_error"
            raise
        finally:
            log_event(
                tool=fn.__name__,
                tasklist_id=kwargs.get("tasklist_id"),
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                status=status,
            )

    return wrapper


def normalize_tasklist(raw: dict[str, Any]) -> dict[str, Any]:
    return {"id": raw.get("id"), "title": raw.get("title", ""), "updated": raw.get("updated")}


_CAMEL_MAP = {"assignmentInfo": "assignment_info", "webViewLink": "web_view_link"}
_OPTIONAL_TASK_FIELDS = (
    "notes",
    "due",
    "completed",
    "parent",
    "links",
    "assignmentInfo",
    "webViewLink",
)


def normalize_task(raw: dict[str, Any], tasklist_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": raw.get("id"),
        "title": raw.get("title", ""),
        "status": raw.get("status", "needsAction"),
        "tasklist_id": tasklist_id,
        "updated": raw.get("updated"),
        "deleted": bool(raw.get("deleted", False)),
        "hidden": bool(raw.get("hidden", False)),
    }
    for field in _OPTIONAL_TASK_FIELDS:
        if field in raw:
            out[_CAMEL_MAP.get(field, field)] = raw[field]
    return out


def clamp_max_results(max_results: int) -> int:
    return max(1, min(MAX_PAGE, max_results))


async def fetch_task_pages(
    client: GoogleTasksClient,
    tasklist_id: str,
    *,
    max_results: int,
    page_token: str | None,
    params: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch pages until max_results items are collected; return raw items + raw next token."""
    items: list[dict[str, Any]] = []
    next_token: str | None = None
    token = page_token
    while len(items) < max_results:
        resp = await client.list_tasks(
            tasklist_id,
            max_results=min(MAX_PAGE, max_results - len(items)),
            page_token=token,
            **params,
        )
        items.extend(resp.get("items", []))
        next_token = resp.get("nextPageToken")
        if not next_token:
            return items, None
        token = next_token
    return items, next_token


async def list_all_tasklists(client: GoogleTasksClient) -> list[dict[str, Any]]:
    lists: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        resp = await client.list_tasklists(max_results=MAX_PAGE, page_token=token)
        lists.extend(resp.get("items", []))
        token = resp.get("nextPageToken")
        if not token:
            return lists


async def fan_out_due(
    client: GoogleTasksClient,
    *,
    due_max: str,
    include_completed: bool,
    tasklist_id: str | None,
) -> list[dict[str, Any]]:
    if tasklist_id:
        list_ids = [tasklist_id]
    else:
        list_ids = [tl["id"] for tl in await list_all_tasklists(client)]
    params = {"show_completed": include_completed, "show_hidden": False, "due_max": due_max}

    async def one(list_id: str) -> list[dict[str, Any]]:
        raw, _ = await fetch_task_pages(
            client, list_id, max_results=MAX_PAGE, page_token=None, params=params
        )
        tasks = [normalize_task(t, list_id) for t in raw]
        if not include_completed:
            tasks = [t for t in tasks if t["status"] == "needsAction"]
        return tasks

    results = await asyncio.gather(*[one(lid) for lid in list_ids])
    merged = [t for chunk in results for t in chunk]
    merged.sort(key=lambda t: (t.get("due") is None, t.get("due") or "", t.get("updated") or ""))
    return merged


def end_of_day_utc(days_ahead: int = 0) -> str:
    day = datetime.now(timezone.utc).date() + timedelta(days=days_ahead)
    dt = datetime(day.year, day.month, day.day, 23, 59, 59, 999000, tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


@mcp.tool
@logged
@structured_errors
async def list_tasklists() -> list[dict[str, Any]]:
    """List all Google Tasks task lists. Returns an array of {id, title, updated}."""
    client = get_client()
    raw_lists = await list_all_tasklists(client)
    return [normalize_tasklist(tl) for tl in raw_lists]


@mcp.tool
@logged
@structured_errors
async def get_tasklist(tasklist_id: str) -> dict[str, Any]:
    """Get one task list. Returns {id, title, updated}."""
    client = get_client()
    raw = await client.get_tasklist(tasklist_id)
    return normalize_tasklist(raw)


@mcp.tool
@logged
@structured_errors
async def create_tasklist(title: str) -> dict[str, Any]:
    """Create a new task list with the given title. Returns the created list {id, title, updated}."""
    client = get_client()
    raw = await client.insert_tasklist(title)
    return normalize_tasklist(raw)


@mcp.tool
@logged
@structured_errors
async def update_tasklist(tasklist_id: str, title: str) -> dict[str, Any]:
    """Rename a task list. Returns the updated list {id, title, updated}."""
    client = get_client()
    raw = await client.patch_tasklist(tasklist_id, title)
    return normalize_tasklist(raw)


@mcp.tool
@logged
@structured_errors
async def delete_tasklist(tasklist_id: str) -> dict[str, Any]:
    """Delete a task list. WARNING: this permanently deletes the list AND all tasks in it."""
    client = get_client()
    await client.delete_tasklist(tasklist_id)
    return {"deleted": True, "tasklist_id": tasklist_id}


@mcp.tool
@logged
@structured_errors
async def list_tasks(
    tasklist_id: str | None = None,
    completed: bool = False,
    due_min: str | None = None,
    due_max: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    page_token: str | None = None,
) -> dict[str, Any]:
    """List tasks in a task list, ordered as in the Google Tasks app.

    completed=false (default) returns open tasks only, like the app; completed=true
    returns completed tasks only. due_min/due_max are RFC3339 or YYYY-MM-DD filters.
    max_results (default 50, cap 100) is auto-fetched across pages; a raw
    next_page_token is returned when more results remain.
     tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    client = get_client()
    list_id = resolve_list(tasklist_id)
    params: dict[str, Any] = {"show_completed": completed, "show_hidden": False}
    if due_min:
        params["due_min"] = normalize_due(due_min)
    if due_max:
        params["due_max"] = normalize_due(due_max)
    raw, next_token = await fetch_task_pages(
        client, list_id, max_results=clamp_max_results(max_results), page_token=page_token, params=params
    )
    tasks = [normalize_task(t, list_id) for t in raw]
    if completed:
        tasks = [t for t in tasks if t["status"] == "completed"]
    out: dict[str, Any] = {"tasks": tasks}
    if next_token:
        out["next_page_token"] = next_token
    return out


@mcp.tool
@logged
@structured_errors
async def get_task(task_id: str, tasklist_id: str | None = None) -> dict[str, Any]:
    """Get a single task by id. Returns the full normalized task.  tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    client = get_client()
    list_id = resolve_list(tasklist_id)
    raw = await client.get_task(list_id, task_id)
    return normalize_task(raw, list_id)


@mcp.tool
@logged
@structured_errors
async def search_tasks(
    query: str,
    tasklist_id: str | None = None,
    include_completed: bool = False,
) -> dict[str, Any]:
    """Case-insensitive search over task titles and notes across all lists (or one list).

    Helper, not a Google API feature.  tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    client = get_client()
    q = query.lower()
    if tasklist_id:
        list_ids = [tasklist_id]
    else:
        list_ids = [tl["id"] for tl in await list_all_tasklists(client)]
    params = {"show_completed": include_completed, "show_hidden": False}

    async def one(list_id: str) -> list[dict[str, Any]]:
        raw, _ = await fetch_task_pages(
            client, list_id, max_results=MAX_PAGE, page_token=None, params=params
        )
        return [
            normalize_task(t, list_id)
            for t in raw
            if q in (t.get("title") or "").lower() or q in (t.get("notes") or "").lower()
        ]

    results = await asyncio.gather(*[one(lid) for lid in list_ids])
    merged = [t for chunk in results for t in chunk]
    merged.sort(key=lambda t: (t.get("due") is None, t.get("due") or "", t.get("updated") or ""))
    return {"tasks": merged, "query": query}


@mcp.tool
@logged
@structured_errors
async def create_task(
    title: str,
    tasklist_id: str | None = None,
    notes: str | None = None,
    due: str | None = None,
    parent_task_id: str | None = None,
) -> dict[str, Any]:
    """Create a task. due accepts YYYY-MM-DD, YYYY-MM-DDTHH:MM, or full RFC3339
    (converted to RFC3339 UTC). The Google Tasks API discards the time-of-day —
    only the date is stored, so put time-of-day guidance in notes.
    parent_task_id creates a subtask.
    Returns the created task.  tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    client = get_client()
    list_id = resolve_list(tasklist_id)
    body: dict[str, Any] = {"title": title}
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = normalize_due(due)
    if parent_task_id is not None:
        body["parent"] = parent_task_id
    raw = await client.insert_task(list_id, body)
    return normalize_task(raw, list_id)


@mcp.tool
@logged
@structured_errors
async def create_task_series(
    title: str,
    n: int,
    interval_days: int,
    start_date: str,
    tasklist_id: str | None = None,
) -> dict[str, Any]:
    """Create n tasks with the same title, due every interval_days starting at
    start_date (YYYY-MM-DD or RFC3339). Helper for recurring tasks, since the API
    cannot set repeat rules. Returns the created tasks.  tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    if n < 1 or n > 100:
        raise GoogleAPIError("invalid_request", "n must be between 1 and 100.")
    if interval_days < 1:
        raise GoogleAPIError("invalid_request", "interval_days must be >= 1.")
    client = get_client()
    list_id = resolve_list(tasklist_id)
    start = parse_due_date(start_date)
    bodies = [
        {"title": title, "due": f"{(start + timedelta(days=i * interval_days)).isoformat()}T00:00:00Z"}
        for i in range(n)
    ]
    created = await asyncio.gather(*[client.insert_task(list_id, b) for b in bodies])
    return {"tasks": [normalize_task(t, list_id) for t in created], "count": n}


@mcp.tool
@logged
@structured_errors
async def update_task(
    task_id: str,
    tasklist_id: str | None = None,
    title: str | None = None,
    notes: str | None = None,
    due: str | None = None,
    parent_task_id: str | None = None,
) -> dict[str, Any]:
    """Patch a task: only the provided fields change (patch semantics).
    due accepts YYYY-MM-DD, YYYY-MM-DDTHH:MM, or full RFC3339 (converted to
    RFC3339 UTC); the Google Tasks API discards the time-of-day — only the date
    is stored. parent_task_id re-parents the task.
    Returns the updated task.  tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    client = get_client()
    list_id = resolve_list(tasklist_id)
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = normalize_due(due)
    if parent_task_id is not None:
        body["parent"] = parent_task_id
    raw = await client.patch_task(list_id, task_id, body)
    return normalize_task(raw, list_id)


@mcp.tool
@logged
@structured_errors
async def complete_task(task_id: str, tasklist_id: str | None = None) -> dict[str, Any]:
    """Mark a task completed (sets status=completed and the completed timestamp).
    Returns the updated task.  tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    client = get_client()
    list_id = resolve_list(tasklist_id)
    raw = await client.patch_task(list_id, task_id, {"status": "completed"})
    return normalize_task(raw, list_id)


@mcp.tool
@logged
@structured_errors
async def uncomplete_task(task_id: str, tasklist_id: str | None = None) -> dict[str, Any]:
    """Mark a task as not completed (status=needsAction, clears completed).
    Returns the updated task.  tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    client = get_client()
    list_id = resolve_list(tasklist_id)
    raw = await client.patch_task(list_id, task_id, {"status": "needsAction"})
    return normalize_task(raw, list_id)


@mcp.tool
@logged
@structured_errors
async def delete_task(task_id: str, tasklist_id: str | None = None) -> dict[str, Any]:
    """Permanently delete a task. WARNING: the API has no trash/recycle bin.
     tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    client = get_client()
    list_id = resolve_list(tasklist_id)
    await client.delete_task(list_id, task_id)
    return {"deleted": True, "task_id": task_id, "tasklist_id": list_id}


@mcp.tool
@logged
@structured_errors
async def move_task(
    task_id: str,
    tasklist_id: str | None = None,
    parent_task_id: str | None = None,
    previous_task_id: str | None = None,
) -> dict[str, Any]:
    """Reposition a task within its list: re-parent via parent_task_id and/or place
    after previous_task_id (omit both to move to the top). Returns the moved task.
     tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    client = get_client()
    list_id = resolve_list(tasklist_id)
    raw = await client.move_task(list_id, task_id, parent_task_id, previous_task_id)
    return normalize_task(raw, list_id)


@mcp.tool
@logged
@structured_errors
async def clear_completed_tasks(tasklist_id: str | None = None) -> dict[str, Any]:
    """Delete all completed tasks in a list.  tasklist_id defaults to the GTASKS_DEFAULT_LIST env var, or Google's @default list when unset."""
    client = get_client()
    list_id = resolve_list(tasklist_id)
    await client.clear_completed(list_id)
    return {"cleared": True, "tasklist_id": list_id}


@mcp.tool
@logged
@structured_errors
async def get_today(include_completed: bool = False) -> dict[str, Any]:
    """Open tasks due today or overdue, merged across all task lists, sorted by due date.
    Set include_completed=true to also include completed tasks. Convenience helper
    over tasks.list with a due_max filter."""
    client = get_client()
    tasks = await fan_out_due(
        client, due_max=end_of_day_utc(0), include_completed=include_completed, tasklist_id=None
    )
    return {"tasks": tasks, "due_max": end_of_day_utc(0)}


@mcp.tool
@logged
@structured_errors
async def get_due_soon(days: int = 7, include_completed: bool = False) -> dict[str, Any]:
    """Open tasks due within `days` days (including overdue), merged across all task
    lists, sorted by due date. Set include_completed=true to also include completed
    tasks. Convenience helper over tasks.list with a due_max filter."""
    if days < 0:
        raise GoogleAPIError("invalid_request", "days must be >= 0.")
    client = get_client()
    due_max = end_of_day_utc(days)
    tasks = await fan_out_due(
        client, due_max=due_max, include_completed=include_completed, tasklist_id=None
    )
    return {"tasks": tasks, "days": days, "due_max": due_max}


class BearerAuthMiddleware:
    """Pure ASGI middleware: require `Authorization: Bearer <MCP_BEARER_TOKEN>` on
    every path except OPEN_PATHS (health checks stay reachable without a token)."""

    def __init__(self, app: Any):
        self.app = app
        self.token = os.getenv("MCP_BEARER_TOKEN")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            self.token
            and scope["type"] == "http"
            and scope.get("path", "") not in OPEN_PATHS
        ):
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            expected = f"Bearer {self.token}".encode("utf-8")
            if not hmac.compare_digest(headers.get(b"authorization", b""), expected):
                log_event(
                    event="auth_rejected",
                    method=scope.get("method"),
                    path=scope.get("path"),
                )
                response = JSONResponse(
                    {"error": "unauthorized", "message": "Invalid or missing bearer token."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(request: Request) -> Response:
    configured = all(os.getenv(v) for v in REQUIRED_ENV)
    return JSONResponse(
        {
            "status": "ok" if configured else "misconfigured",
            "version": __version__,
            "auth_configured": configured,
        },
        status_code=200 if configured else 503,
    )


def load_server_env(path: str = ".env") -> dict[str, Any]:
    """Load KEY=VALUE pairs from a .env file if it exists (existing env wins)."""
    if os.path.exists(path):
        return load_env_file(path)
    return {}


def server_host() -> str:
    return os.getenv("MCP_HOST", "").strip() or DEFAULT_HOST


def parse_port(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"MCP_PORT must be an integer, got {raw!r}.") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"MCP_PORT must be between 1 and 65535, got {port}.")
    return port


def parse_list_env(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    load_server_env()
    missing = [v for v in REQUIRED_ENV if not os.getenv(v)]
    if missing:
        log_event(
            event="startup_failed",
            error="missing_env",
            missing=missing,
            message="Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN (see auth.py).",
        )
        sys.exit(1)
    host = server_host()
    try:
        port = parse_port(os.getenv("MCP_PORT"))
    except ValueError as exc:
        log_event(event="startup_failed", error="invalid_config", message=str(exc))
        sys.exit(1)
    token = os.getenv("MCP_BEARER_TOKEN")
    if not token:
        if not is_loopback_host(host):
            log_event(
                event="startup_failed",
                error="missing_bearer_token",
                message=(
                    "MCP_BEARER_TOKEN is required when MCP_HOST is not loopback. "
                    "Generate one with: openssl rand -hex 32"
                ),
            )
            sys.exit(1)
        log_event(
            event="startup_warning",
            warning="MCP_BEARER_TOKEN unset; bearer auth disabled (loopback bind only).",
            host=host,
        )
    elif len(token) < MIN_TOKEN_LENGTH:
        log_event(
            event="startup_failed",
            error="weak_bearer_token",
            message=(
                f"MCP_BEARER_TOKEN must be at least {MIN_TOKEN_LENGTH} characters. "
                "Generate one with: openssl rand -hex 32"
            ),
        )
        sys.exit(1)
    allowed_hosts = parse_list_env("MCP_ALLOWED_HOSTS")
    middleware = [Middleware(BearerAuthMiddleware)] if token else []
    log_event(
        event="startup",
        version=__version__,
        host=host,
        port=port,
        bearer_auth=bool(token),
        allowed_hosts=allowed_hosts,
    )
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path="/mcp",
        show_banner=False,
        middleware=middleware,
        host_origin_protection=True,
        allowed_hosts=allowed_hosts,
    )


if __name__ == "__main__":
    main()
