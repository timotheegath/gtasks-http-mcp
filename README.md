# gtasks-mcp

Self-hosted, streamable-HTTP MCP server wrapping the full Google Tasks API v1.
One Python module of tools (`server.py`), an httpx wrapper with OAuth refresh-token
auth (`google_client.py`), and a one-time auth bootstrap (`auth.py`). No database,
no local state — all state lives in Google Tasks.

## Tools

Tasklists: `list_tasklists`, `get_tasklist`, `create_tasklist`, `update_tasklist`, `delete_tasklist`.

Tasks: `list_tasks`, `get_task`, `search_tasks`, `create_task`, `create_task_series`,
`update_task`, `complete_task`, `uncomplete_task`, `delete_task`, `move_task`,
`clear_completed_tasks`, plus the `get_today` / `get_due_soon` conveniences.

All tools accept an optional `tasklist_id` (defaults to `GTASKS_DEFAULT_LIST`, else
Google's `@default`). Errors are structured `{error, message, detail?}` with stable
codes: `not_found`, `invalid_request`, `rate_limited`, `auth_expired`,
`upstream_unavailable`, `internal_error`.

## Configuration

Environment variables (see `.env.example`):

| Variable | Required | Description |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID` | yes | OAuth client ID (Desktop-app type) |
| `GOOGLE_CLIENT_SECRET` | yes | OAuth client secret |
| `GOOGLE_REFRESH_TOKEN` | yes | Long-lived refresh token from `auth.py` |
| `MCP_BEARER_TOKEN` | recommended | Shared secret required as `Authorization: Bearer` on `/mcp` |
| `GTASKS_API_BASE` | no | Default `https://tasks.googleapis.com` (test override) |
| `GTASKS_DEFAULT_LIST` | no | Default list id when a tool omits `tasklist_id` |

Scope: `https://www.googleapis.com/auth/tasks` (read/write), nothing else.

## Auth bootstrap (run once, on your laptop)

```sh
uv run python auth.py            # loads ./.env automatically if present
uv run python auth.py --env-file ~/secrets/gtasks.env
# or explicitly: python auth.py --client-id ... --client-secret ...
# or via shell:  set -a; source .env; set +a; uv run python auth.py
```

Opens the consent screen with a loopback listener on port 4466 (register
`http://localhost:4466` and `http://127.0.0.1:4466` as redirect URIs in the Google
Cloud OAuth client) and prints a `GOOGLE_REFRESH_TOKEN`. The token is never stored
by this project — put it in `.env` on the server only.

## Run locally

```sh
cp .env.example .env             # fill in secrets
uv sync
uv run python server.py          # auto-loads ./.env; serves HTTP on 0.0.0.0:8080/mcp
curl http://127.0.0.1:8080/healthz
```

`server.py` loads `./.env` at startup when present (real environment variables
take precedence), same as `auth.py`. In Docker, compose injects `.env` via
`env_file` instead — the file itself is never baked into the image.

## Deploy (Docker Compose on the VPS, behind the existing Cloudflare tunnel)

```sh
cp .env.example .env             # fill in secrets
docker compose up -d --build
```

The service joins the tunnel's compose network (`TUNNEL_NETWORK`, default
`cloudflared`) and exposes only container port 8080. Point the existing tunnel
route at it, e.g. `gtasks.<your-domain>` → `http://gtasks-mcp:8080`, then add a
custom MCP connector in Vibe: URL `https://gtasks.<your-domain>/mcp` with header
`Authorization: Bearer <MCP_BEARER_TOKEN>`.

## Tests

```sh
uv sync
uv run pytest
```

Unit tests mock httpx (`httpx.MockTransport`): every tool happy path, date
normalization, pagination merging, 401 → refresh → retry, and every error mapping.
