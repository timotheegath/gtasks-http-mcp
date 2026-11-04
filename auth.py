"""One-time OAuth bootstrap: run on your laptop to obtain a GOOGLE_REFRESH_TOKEN.

Runs a loopback listener on port 4466 (redirect URIs http://localhost:4466 and
http://127.0.0.1:4466 must be registered in the Google Cloud OAuth client),
prints the refresh token, and never stores it anywhere.
"""

from __future__ import annotations

import argparse
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

SCOPE = "https://www.googleapis.com/auth/tasks"
PORT = 4466
REDIRECT_URI = f"http://127.0.0.1:{PORT}"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

PAGE = b"""<html><body><h3>Authorization complete.</h3>
<p>Copy the printed GOOGLE_REFRESH_TOKEN into your server .env, then close this tab.</p>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        code = query.get("code", [None])[0]
        if code:
            type(self).code = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing ?code= parameter.")

    def log_message(self, format: str, *args: object) -> None:
        pass


def load_env_file(path: str) -> dict[str, str]:
    """Load KEY=VALUE lines from a .env file into os.environ (existing vars win)."""
    loaded: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key:
                loaded[key] = value
                os.environ.setdefault(key, value)
    return loaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Obtain a Google Tasks refresh token.")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="KEY=VALUE file to load credentials from (default: ./.env if it exists).",
    )
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--client-secret", default=None)
    args = parser.parse_args()
    if os.path.exists(args.env_file):
        load_env_file(args.env_file)
    client_id = args.client_id or os.getenv("GOOGLE_CLIENT_ID")
    client_secret = args.client_secret or os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        parser.error(
            "Provide --client-id/--client-secret, set GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET, "
            f"or point --env-file at a .env containing them (looked at: {args.env_file})."
        )

    auth_url = AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    server = HTTPServer(("127.0.0.1", PORT), _Handler)
    server.timeout = 300
    print(f"Opening browser for consent (scope: {SCOPE})...")
    print(f"If it does not open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)
    print(f"Waiting for callback on {REDIRECT_URI} ...")
    while _Handler.code is None:
        server.handle_request()
    server.server_close()

    with httpx.Client(timeout=30.0) as http:
        resp = http.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": _Handler.code,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
            },
        )
    resp.raise_for_status()
    refresh_token = resp.json()["refresh_token"]
    print("\nAdd this to your server .env (never commit it):\n")
    print(f"GOOGLE_REFRESH_TOKEN={refresh_token}\n")


if __name__ == "__main__":
    main()
