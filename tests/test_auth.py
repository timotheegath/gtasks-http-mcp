import os

import pytest

import auth


def test_load_env_file_parses_and_sets_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "GOOGLE_CLIENT_ID=cid123\n"
        'GOOGLE_CLIENT_SECRET="sec ret"\n'
        "\n"
        "export EXTRA=1\n"
    )
    loaded = auth.load_env_file(str(env_file))
    assert loaded["GOOGLE_CLIENT_ID"] == "cid123"
    assert loaded["GOOGLE_CLIENT_SECRET"] == "sec ret"
    assert loaded["EXTRA"] == "1"
    assert os.environ["GOOGLE_CLIENT_ID"] == "cid123"
    assert os.environ["GOOGLE_CLIENT_SECRET"] == "sec ret"


def test_load_env_file_does_not_override_existing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "existing")
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_CLIENT_ID=fromfile\n")
    auth.load_env_file(str(env_file))
    assert os.environ["GOOGLE_CLIENT_ID"] == "existing"


def test_server_loads_dotenv_by_default(tmp_path, monkeypatch):
    import server

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    (tmp_path / ".env").write_text("GOOGLE_CLIENT_ID=fromfile\n")
    loaded = server.load_server_env()
    assert loaded == {"GOOGLE_CLIENT_ID": "fromfile"}
    assert os.environ["GOOGLE_CLIENT_ID"] == "fromfile"


def test_server_load_server_env_missing_file(tmp_path, monkeypatch):
    import server

    monkeypatch.chdir(tmp_path)
    assert server.load_server_env() == {}


def test_parse_port_defaults_and_validates():
    import server

    assert server.parse_port(None) == 8080
    assert server.parse_port("  ") == 8080
    assert server.parse_port("9000") == 9000
    for bad in ("abc", "0", "65536", "-1"):
        with pytest.raises(ValueError):
            server.parse_port(bad)


def test_parse_list_env_and_loopback_helpers(monkeypatch):
    import server

    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "gtasks.example.com, tasks.other.dev ,")
    assert server.parse_list_env("MCP_ALLOWED_HOSTS") == ["gtasks.example.com", "tasks.other.dev"]
    monkeypatch.delenv("MCP_ALLOWED_HOSTS")
    assert server.parse_list_env("MCP_ALLOWED_HOSTS") == []
    assert server.is_loopback_host("127.0.0.1")
    assert server.is_loopback_host("::1")
    assert server.is_loopback_host("localhost")
    assert not server.is_loopback_host("0.0.0.0")
    assert not server.is_loopback_host("gtasks.example.com")


def _set_required_env(monkeypatch):
    import server

    for var in server.REQUIRED_ENV:
        monkeypatch.setenv(var, "x")


def test_main_refuses_public_bind_without_token(tmp_path, monkeypatch):
    import server

    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    with pytest.raises(SystemExit) as exc_info:
        server.main()
    assert exc_info.value.code == 1


def test_main_refuses_weak_token(tmp_path, monkeypatch):
    import server

    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_BEARER_TOKEN", "short")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    with pytest.raises(SystemExit) as exc_info:
        server.main()
    assert exc_info.value.code == 1


def test_main_rejects_invalid_port(tmp_path, monkeypatch):
    import server

    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MCP_BEARER_TOKEN", "a" * 32)
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "not-a-port")
    with pytest.raises(SystemExit) as exc_info:
        server.main()
    assert exc_info.value.code == 1
