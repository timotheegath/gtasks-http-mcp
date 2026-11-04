import httpx
import pytest

import find_list_id
from conftest import run


def test_fetch_all_tasklists_merges_pages(fake, client):
    def responder(req: httpx.Request) -> httpx.Response:
        if "pageToken" in req.url.params:
            return httpx.Response(200, json={"items": [{"id": "l2", "title": "Two"}]})
        return httpx.Response(
            200, json={"items": [{"id": "l1", "title": "One"}], "nextPageToken": "tok"}
        )

    fake.on("GET", "/tasks/v1/users/@me/lists", responder)
    lists = run(find_list_id.fetch_all_tasklists(client))
    assert [tl["id"] for tl in lists] == ["l1", "l2"]


def test_format_lists_marks_default_list():
    out = find_list_id.format_lists(
        [
            {"id": "b2", "title": "Work"},
            {"id": "@default", "title": "My Tasks"},
        ]
    )
    assert "b2" in out and "Work" in out
    default_line = next(line for line in out.splitlines() if "@default" in line)
    assert "@default list" in default_line
    work_line = next(line for line in out.splitlines() if "b2" in line)
    assert "@default list" not in work_line


def test_main_requires_credentials(monkeypatch, tmp_path):
    for key in find_list_id.REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit) as exc_info:
        find_list_id.main(["--env-file", str(tmp_path / "missing.env")])
    assert exc_info.value.code == 2


def test_main_prints_consent_hint_on_auth_expired(fake, client, monkeypatch, capsys):
    fake.invalid_grant = True
    monkeypatch.setattr(
        find_list_id.GoogleTasksClient, "from_env", classmethod(lambda cls: client)
    )
    for key in find_list_id.REQUIRED_ENV:
        monkeypatch.setenv(key, "x")
    with pytest.raises(SystemExit) as exc_info:
        find_list_id.main(["--env-file", "/nonexistent/.env"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "auth_expired" in err
    assert "auth.py" in err


def test_main_prints_ids_and_env_hint(fake, client, monkeypatch, capsys):
    fake.on(
        "GET",
        "/tasks/v1/users/@me/lists",
        lambda req: httpx.Response(
            200,
            json={"items": [{"id": "abc123", "title": "Work"}, {"id": "@default", "title": "My Tasks"}]},
        ),
    )
    monkeypatch.setattr(
        find_list_id.GoogleTasksClient, "from_env", classmethod(lambda cls: client)
    )
    for key in find_list_id.REQUIRED_ENV:
        monkeypatch.setenv(key, "x")
    with pytest.raises(SystemExit) as exc_info:
        find_list_id.main(["--env-file", "/nonexistent/.env"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "abc123" in out
    assert "Work" in out
    assert "GTASKS_DEFAULT_LIST=<id" in out
