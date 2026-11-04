import os

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
