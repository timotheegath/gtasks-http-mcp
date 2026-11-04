import httpx
import pytest

from google_client import AuthExpiredError, GoogleAPIError, normalize_due
from conftest import run


def test_access_token_is_cached_across_requests(fake, client):
    fake.on("GET", "/lists", lambda req: httpx.Response(200, json={"items": []}))
    run(client.list_tasklists())
    run(client.list_tasklists())
    assert fake.token_calls == 1
    assert fake.requests[0].headers["authorization"] == "Bearer tok-1"
    assert fake.requests[1].headers["authorization"] == "Bearer tok-1"


def test_401_triggers_refresh_and_retry(fake, client):
    fake.unauthorized_once = True
    fake.on("GET", "/lists", lambda req: httpx.Response(200, json={"items": [{"id": "l1"}]}))
    result = run(client.list_tasklists())
    assert result == {"items": [{"id": "l1"}]}
    assert fake.token_calls == 2
    assert fake.requests[0].headers["authorization"] == "Bearer tok-2"


def test_persistent_401_raises_auth_expired(fake, client):
    fake.unauthorized_always = True
    with pytest.raises(AuthExpiredError) as exc_info:
        run(client.list_tasklists())
    assert exc_info.value.error == "auth_expired"
    assert "cid" in exc_info.value.detail["consent_url"]
    assert fake.token_calls == 2


def test_invalid_grant_raises_auth_expired(fake, client):
    fake.invalid_grant = True
    with pytest.raises(AuthExpiredError) as exc_info:
        run(client.list_tasklists())
    assert exc_info.value.error == "auth_expired"
    assert exc_info.value.detail["consent_url"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth"
    )


def test_404_maps_to_not_found(fake, client):
    fake.on("GET", "/lists", lambda req: httpx.Response(404, json={"error": {"message": "Not found"}}))
    with pytest.raises(GoogleAPIError) as exc_info:
        run(client.list_tasklists())
    assert exc_info.value.error == "not_found"
    assert exc_info.value.detail["status"] == 404


def test_400_maps_to_invalid_request(fake, client):
    fake.on(
        "POST",
        "/tasks",
        lambda req: httpx.Response(400, json={"error": {"message": "Invalid task title"}}),
    )
    with pytest.raises(GoogleAPIError) as exc_info:
        run(client.insert_task("l1", {"title": ""}))
    assert exc_info.value.error == "invalid_request"
    assert "Invalid task title" in exc_info.value.message


def test_400_not_found_message_maps_to_not_found(fake, client):
    fake.on(
        "GET",
        "/tasks",
        lambda req: httpx.Response(400, json={"error": {"message": "Task not found: abc"}}),
    )
    with pytest.raises(GoogleAPIError) as exc_info:
        run(client.get_task("l1", "abc"))
    assert exc_info.value.error == "not_found"


def test_403_maps_to_rate_limited_with_retry_after(fake, client):
    fake.on(
        "GET",
        "/lists",
        lambda req: httpx.Response(
            403,
            json={"error": {"message": "Quota exceeded"}},
            headers={"Retry-After": "30"},
        ),
    )
    with pytest.raises(GoogleAPIError) as exc_info:
        run(client.list_tasklists())
    assert exc_info.value.error == "rate_limited"
    assert exc_info.value.detail["retry_after"] == "30"


def test_network_timeout_maps_to_upstream_unavailable(fake, client):
    fake.raise_timeout = True
    with pytest.raises(GoogleAPIError) as exc_info:
        run(client.list_tasklists())
    assert exc_info.value.error == "upstream_unavailable"


def test_list_tasks_param_mapping(fake, client):
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        return httpx.Response(200, json={"items": []})

    fake.on("GET", "/lists/l1/tasks", responder)
    run(
        client.list_tasks(
            "l1",
            show_completed=False,
            show_hidden=False,
            due_min="2026-11-01T00:00:00Z",
            due_max="2026-11-30T23:59:59Z",
            max_results=25,
            page_token="p9",
        )
    )
    assert captured["showCompleted"] == "false"
    assert captured["showHidden"] == "false"
    assert captured["dueMin"] == "2026-11-01T00:00:00Z"
    assert captured["dueMax"] == "2026-11-30T23:59:59Z"
    assert captured["maxResults"] == "25"
    assert captured["pageToken"] == "p9"


def test_move_task_sends_parent_and_previous(fake, client):
    captured = {}

    def responder(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        assert req.url.path.endswith("/tasks/t1/move")
        return httpx.Response(200, json={"id": "t1"})

    fake.on("POST", "/move", responder)
    run(client.move_task("l1", "t1", parent_task_id="p1", previous_task_id="prev1"))
    assert captured["parent"] == "p1"
    assert captured["previous"] == "prev1"


def test_normalize_due_date_only():
    assert normalize_due("2026-11-20") == "2026-11-20T00:00:00Z"


def test_normalize_due_datetime_naive_assumed_utc():
    assert normalize_due("2026-11-20T10:30") == "2026-11-20T10:30:00Z"


def test_normalize_due_with_offset_converted_to_utc():
    assert normalize_due("2026-11-20T10:30:00+02:00") == "2026-11-20T08:30:00Z"


def test_normalize_due_full_rfc3339():
    assert normalize_due("2026-11-20T10:30:15.500Z") == "2026-11-20T10:30:15.500000Z"


def test_normalize_due_invalid():
    with pytest.raises(GoogleAPIError) as exc_info:
        normalize_due("not-a-date")
    assert exc_info.value.error == "invalid_request"


def test_error_to_dict_shape(fake, client):
    fake.on("GET", "/lists", lambda req: httpx.Response(404, json={"error": {"message": "nope"}}))
    with pytest.raises(GoogleAPIError) as exc_info:
        run(client.list_tasklists())
    d = exc_info.value.to_dict()
    assert d["error"] == "not_found"
    assert d["message"]
    assert d["detail"] == {"status": 404}
