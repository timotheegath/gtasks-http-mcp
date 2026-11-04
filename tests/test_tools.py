import json

import httpx
from starlette.middleware import Middleware
from starlette.requests import Request

import server
from conftest import req_json, run


def task(task_id, title="t", status="needsAction", **extra):
    base = {
        "id": task_id,
        "title": title,
        "status": status,
        "updated": "2026-11-01T00:00:00.000Z",
        "etag": "e",
        "kind": "tasks#task",
    }
    base.update(extra)
    return base


def test_list_tasklists_paginates_and_normalizes(fake):
    def page1(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("pageToken") is None
        return httpx.Response(
            200,
            json={
                "items": [{"id": "l1", "title": "One", "updated": "u1", "selfLink": "x"}],
                "nextPageToken": "p2",
            },
        )

    def page2(req: httpx.Request) -> httpx.Response:
        assert req.url.params["pageToken"] == "p2"
        return httpx.Response(200, json={"items": [{"id": "l2", "title": "Two", "updated": "u2"}]})

    state = {"n": 0}

    def route(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return page1(req) if state["n"] == 1 else page2(req)

    fake.on("GET", "/users/@me/lists", route)
    result = run(server.list_tasklists())
    assert result == [
        {"id": "l1", "title": "One", "updated": "u1"},
        {"id": "l2", "title": "Two", "updated": "u2"},
    ]


def test_tasklist_crud(fake):
    fake.on("GET", "/lists/l1", lambda req: httpx.Response(200, json={"id": "l1", "title": "T", "updated": "u"}))
    fake.on("POST", "/users/@me/lists", lambda req: httpx.Response(200, json={"id": "l9", "title": req_json(req)["title"], "updated": "u"}))
    fake.on("PATCH", "/lists/l1", lambda req: httpx.Response(200, json={"id": "l1", "title": req_json(req)["title"], "updated": "u2"}))
    fake.on("DELETE", "/lists/l1", lambda req: httpx.Response(204))

    assert run(server.get_tasklist("l1")) == {"id": "l1", "title": "T", "updated": "u"}
    assert run(server.create_tasklist("New"))["id"] == "l9"
    assert run(server.update_tasklist("l1", "Renamed"))["title"] == "Renamed"
    assert run(server.delete_tasklist("l1")) == {"deleted": True, "tasklist_id": "l1"}
    assert fake.requests[-1].method == "DELETE"


def test_list_tasks_defaults_to_open_tasks_on_default_list(fake):
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        assert "/lists/@default/tasks" in req.url.path
        return httpx.Response(200, json={"items": [task("t1", title="Open task")]})

    fake.on("GET", "/tasks", responder)
    result = run(server.list_tasks())
    assert captured["showCompleted"] == "false"
    assert captured["showHidden"] == "false"
    assert captured["maxResults"] == "50"
    assert result["tasks"][0]["tasklist_id"] == "@default"
    assert result["tasks"][0]["id"] == "t1"
    assert "next_page_token" not in result


def test_list_tasks_completed_true_filters_completed_only(fake):
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        return httpx.Response(
            200,
            json={
                "items": [
                    task("t1", status="needsAction"),
                    task("t2", status="completed", completed="2026-11-01T10:00:00.000Z"),
                ]
            },
        )

    fake.on("GET", "/tasks", responder)
    result = run(server.list_tasks(tasklist_id="l1", completed=True))
    assert captured["showCompleted"] == "true"
    assert [t["id"] for t in result["tasks"]] == ["t2"]
    assert result["tasks"][0]["completed"] == "2026-11-01T10:00:00.000Z"


def test_list_tasks_pagination_merges_pages_and_surfaces_token(fake):
    pages = {
        None: {"items": [task("t1"), task("t2")], "nextPageToken": "p2"},
        "p2": {"items": [task("t3")]},
    }

    def responder(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[req.url.params.get("pageToken")])

    fake.on("GET", "/tasks", responder)
    result = run(server.list_tasks(tasklist_id="l1", max_results=2))
    assert [t["id"] for t in result["tasks"]] == ["t1", "t2"]
    assert result["next_page_token"] == "p2"

    result = run(server.list_tasks(tasklist_id="l1", max_results=100, page_token="p2"))
    assert [t["id"] for t in result["tasks"]] == ["t3"]
    assert "next_page_token" not in result


def test_list_tasks_due_filters_are_normalized(fake):
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        return httpx.Response(200, json={"items": []})

    fake.on("GET", "/tasks", responder)
    run(server.list_tasks(tasklist_id="l1", due_min="2026-11-01", due_max="2026-11-30T10:00+02:00"))
    assert captured["dueMin"] == "2026-11-01T00:00:00Z"
    assert captured["dueMax"] == "2026-11-30T08:00:00Z"


def test_list_tasks_max_results_capped_at_100(fake):
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        return httpx.Response(200, json={"items": []})

    fake.on("GET", "/tasks", responder)
    run(server.list_tasks(tasklist_id="l1", max_results=500))
    assert captured["maxResults"] == "100"


def test_default_list_from_env(fake, monkeypatch):
    monkeypatch.setenv("GTASKS_DEFAULT_LIST", "my-list")
    fake.on("GET", "/tasks", lambda req: httpx.Response(200, json={"items": []}))
    run(server.list_tasks())
    assert "/lists/my-list/tasks" in fake.requests[-1].url.path


def test_get_task_returns_normalized_task(fake):
    fake.on(
        "GET",
        "/lists/l1/tasks/t1",
        lambda req: httpx.Response(
            200,
            json=task(
                "t1",
                title="With links",
                notes="n",
                due="2026-11-20T00:00:00.000Z",
                parent="p1",
                links=[{"type": "email", "link": "https://mail.google.com/x"}],
                assignmentInfo={"assignee": "a@b.c"},
                webViewLink="https://tasks.google.com/x",
            ),
        ),
    )
    result = run(server.get_task("t1", tasklist_id="l1"))
    assert result["id"] == "t1"
    assert result["notes"] == "n"
    assert result["due"] == "2026-11-20T00:00:00.000Z"
    assert result["parent"] == "p1"
    assert result["links"][0]["type"] == "email"
    assert result["assignment_info"] == {"assignee": "a@b.c"}
    assert result["web_view_link"] == "https://tasks.google.com/x"
    assert result["deleted"] is False
    assert result["hidden"] is False
    assert result["tasklist_id"] == "l1"


def test_create_task_normalizes_due_and_parent(fake):
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured["body"] = req_json(req)
        return httpx.Response(200, json=task("t9", title=req_json(req)["title"], **{k: v for k, v in req_json(req).items() if k != "title"}))

    fake.on("POST", "/lists/@default/tasks", responder)
    result = run(
        server.create_task(title="Buy milk", due="2026-11-20", notes="2%", parent_task_id="p1")
    )
    assert captured["body"]["due"] == "2026-11-20T00:00:00Z"
    assert captured["body"]["parent"] == "p1"
    assert captured["body"]["notes"] == "2%"
    assert result["id"] == "t9"
    assert result["tasklist_id"] == "@default"


def test_create_task_invalid_due_returns_structured_error(fake):
    result = run(server.create_task(title="x", due="yesterday-ish"))
    assert result["error"] == "invalid_request"
    assert "due" in result["message"].lower()
    assert fake.requests == []


def test_create_task_series_creates_dated_tasks(fake):
    bodies = []

    def responder(req: httpx.Request) -> httpx.Response:
        bodies.append(req_json(req))
        return httpx.Response(200, json=task(f"t{len(bodies)}", **req_json(req)))

    fake.on("POST", "/tasks", responder)
    result = run(
        server.create_task_series(
            title="Gym", n=3, interval_days=7, start_date="2026-11-20", tasklist_id="l1"
        )
    )
    assert result["count"] == 3
    assert [b["due"] for b in bodies] == [
        "2026-11-20T00:00:00Z",
        "2026-11-27T00:00:00Z",
        "2026-12-04T00:00:00Z",
    ]
    assert [t["id"] for t in result["tasks"]] == ["t1", "t2", "t3"]


def test_create_task_series_validates_inputs(fake):
    assert run(server.create_task_series(title="x", n=0, interval_days=7, start_date="2026-11-20"))["error"] == "invalid_request"
    assert run(server.create_task_series(title="x", n=3, interval_days=0, start_date="2026-11-20"))["error"] == "invalid_request"


def test_update_task_patches_only_provided_fields(fake):
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured["body"] = req_json(req)
        assert req.method == "PATCH"
        return httpx.Response(200, json=task("t1", **req_json(req)))

    fake.on("PATCH", "/tasks/t1", responder)
    result = run(server.update_task("t1", tasklist_id="l1", title="New title"))
    assert captured["body"] == {"title": "New title"}
    assert result["title"] == "New title"


def test_complete_and_uncomplete_task(fake):
    bodies = []

    def responder(req: httpx.Request) -> httpx.Response:
        bodies.append(req_json(req))
        status = req_json(req)["status"]
        return httpx.Response(
            200,
            json=task("t1", status=status, **({"completed": "2026-11-02T00:00:00.000Z"} if status == "completed" else {})),
        )

    fake.on("PATCH", "/tasks/t1", responder)
    done = run(server.complete_task("t1", tasklist_id="l1"))
    assert bodies[0] == {"status": "completed"}
    assert done["status"] == "completed"
    assert done["completed"] == "2026-11-02T00:00:00.000Z"
    undone = run(server.uncomplete_task("t1", tasklist_id="l1"))
    assert bodies[1] == {"status": "needsAction"}
    assert undone["status"] == "needsAction"


def test_delete_task(fake):
    fake.on("DELETE", "/tasks/t1", lambda req: httpx.Response(204))
    result = run(server.delete_task("t1", tasklist_id="l1"))
    assert result == {"deleted": True, "task_id": "t1", "tasklist_id": "l1"}


def test_move_task(fake):
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        assert req.method == "POST" and req.url.path.endswith("/tasks/t1/move")
        return httpx.Response(200, json=task("t1"))

    fake.on("POST", "/move", responder)
    run(server.move_task("t1", tasklist_id="l1", parent_task_id="p1", previous_task_id="t0"))
    assert captured["parent"] == "p1"
    assert captured["previous"] == "t0"


def test_clear_completed_tasks(fake):
    fake.on("POST", "/lists/l1/clear", lambda req: httpx.Response(204))
    assert run(server.clear_completed_tasks(tasklist_id="l1")) == {
        "cleared": True,
        "tasklist_id": "l1",
    }


def test_search_tasks_across_lists_case_insensitive(fake):
    fake.on(
        "GET",
        "/users/@me/lists",
        lambda req: httpx.Response(
            200, json={"items": [{"id": "l1", "title": "A"}, {"id": "l2", "title": "B"}]}
        ),
    )

    def tasks_for(req: httpx.Request) -> httpx.Response:
        if "/lists/l1/" in req.url.path:
            items = [task("t1", title="Buy Milk"), task("t2", title="Call bob", notes="dentist")]
        else:
            items = [task("t3", title="MILK the report", notes="see Milk memo"),
                     task("t4", title="unrelated")]
        return httpx.Response(200, json={"items": items})

    fake.on("GET", "/tasks", tasks_for)
    result = run(server.search_tasks("milk"))
    assert {t["id"] for t in result["tasks"]} == {"t1", "t3"}
    assert result["query"] == "milk"
    assert all("tasklist_id" in t for t in result["tasks"])


def test_search_tasks_include_completed(fake):
    fake.on("GET", "/users/@me/lists", lambda req: httpx.Response(200, json={"items": [{"id": "l1"}]}))
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        items = [task("t1", title="Milk"), task("t2", title="milk done", status="completed")]
        if req.url.params.get("showCompleted") == "false":
            items = [t for t in items if t["status"] != "completed"]
        return httpx.Response(200, json={"items": items})

    fake.on("GET", "/tasks", responder)
    result = run(server.search_tasks("milk", include_completed=True))
    assert captured["showCompleted"] == "true"
    assert {t["id"] for t in result["tasks"]} == {"t1", "t2"}
    result = run(server.search_tasks("milk", include_completed=False))
    assert captured["showCompleted"] == "false"
    assert [t["id"] for t in result["tasks"]] == ["t1"]


def test_get_today_merges_lists_sorted_by_due(fake):
    fake.on(
        "GET",
        "/users/@me/lists",
        lambda req: httpx.Response(200, json={"items": [{"id": "l1"}, {"id": "l2"}]}),
    )
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured[req.url.path] = dict(req.url.params)
        if "/lists/l1/" in req.url.path:
            items = [task("t1", title="later", due="2026-12-31T00:00:00.000Z")]
        else:
            items = [task("t2", title="overdue", due="2020-01-01T00:00:00.000Z"),
                     task("t3", title="done", status="completed", due="2026-11-01T00:00:00.000Z")]
        return httpx.Response(200, json={"items": items})

    fake.on("GET", "/tasks", responder)
    result = run(server.get_today())
    expected_due_max = server.end_of_day_utc(0)
    assert captured["/tasks/v1/lists/l1/tasks"]["dueMax"] == expected_due_max
    assert captured["/tasks/v1/lists/l1/tasks"]["showCompleted"] == "false"
    assert [t["id"] for t in result["tasks"]] == ["t2", "t1"]

    result = run(server.get_today(include_completed=True))
    assert [t["id"] for t in result["tasks"]] == ["t2", "t3", "t1"]


def test_get_due_soon_uses_days_window(fake):
    fake.on("GET", "/users/@me/lists", lambda req: httpx.Response(200, json={"items": [{"id": "l1"}]}))
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        return httpx.Response(200, json={"items": []})

    fake.on("GET", "/tasks", responder)
    result = run(server.get_due_soon(days=3))
    assert captured["dueMax"] == server.end_of_day_utc(3)
    assert result["days"] == 3
    assert run(server.get_due_soon(days=-1))["error"] == "invalid_request"


def test_tool_returns_structured_not_found(fake):
    fake.on(
        "GET",
        "/tasks",
        lambda req: httpx.Response(404, json={"error": {"message": "Task not found: tX"}}),
    )
    result = run(server.get_task("tX", tasklist_id="l1"))
    assert result["error"] == "not_found"
    assert "tX" in result["message"]


def test_tool_returns_auth_expired_when_refresh_revoked(fake):
    fake.invalid_grant = True
    result = run(server.list_tasklists())
    assert result["error"] == "auth_expired"
    assert "consent_url" in result["detail"]


def test_healthz_ok(monkeypatch):
    for var in server.REQUIRED_ENV:
        monkeypatch.setenv(var, "x")
    scope = {"type": "http", "method": "GET", "path": "/healthz", "headers": []}
    resp = run(server.healthz(Request(scope)))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    assert body["version"] == server.__version__
    assert body["auth_configured"] is True


def test_healthz_misconfigured(monkeypatch):
    for var in server.REQUIRED_ENV:
        monkeypatch.delenv(var, raising=False)
    scope = {"type": "http", "method": "GET", "path": "/healthz", "headers": []}
    resp = run(server.healthz(Request(scope)))
    assert resp.status_code == 503
    assert json.loads(resp.body)["auth_configured"] is False


def test_bearer_middleware_rejects_and_accepts(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "sekret")
    called = {"inner": False}

    async def inner_app(scope, receive, send):
        called["inner"] = True

    async def send(message):
        pass

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = server.BearerAuthMiddleware(inner_app)
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
    responses = []

    async def send_collect(message):
        responses.append(message)

    run(mw(scope, receive, send_collect))
    assert responses[0]["status"] == 401
    assert called["inner"] is False

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer sekret")],
    }
    run(mw(scope, receive, send))
    assert called["inner"] is True

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/healthz",
        "headers": [],
    }
    run(mw(scope, receive, send))
    assert called["inner"] is True


def test_all_expected_tools_registered():
    tools = run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "list_tasklists",
        "get_tasklist",
        "create_tasklist",
        "update_tasklist",
        "delete_tasklist",
        "list_tasks",
        "get_task",
        "search_tasks",
        "create_task",
        "create_task_series",
        "update_task",
        "complete_task",
        "uncomplete_task",
        "delete_task",
        "move_task",
        "clear_completed_tasks",
        "get_today",
        "get_due_soon",
    }
    for t in tools:
        assert t.description, f"{t.name} has no description"
