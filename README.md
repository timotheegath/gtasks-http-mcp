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
| `MCP_BEARER_TOKEN` | yes when public | Shared secret required as `Authorization: Bearer` on every path except `/healthz`. Mandatory unless `MCP_HOST` is loopback; must be >= 16 chars. Generate with `openssl rand -hex 32` |
| `MCP_HOST` | no | Bind address, default `0.0.0.0`. Use `127.0.0.1` for local-only access |
| `MCP_PORT` | no | Bind port, default `8080` |
| `MCP_ALLOWED_HOSTS` | no | Comma-separated extra `Host` headers accepted by the Host/Origin guard (`localhost`, `127.0.0.1`, `::1` are always allowed). Add your public tunnel hostname, e.g. `gtasks.example.com` |
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
cp .env.example .env             # fill in secrets (MCP_BEARER_TOKEN: openssl rand -hex 32)
uv sync
uv run python server.py          # auto-loads ./.env; serves HTTP on $MCP_HOST:$MCP_PORT/mcp (default 0.0.0.0:8080)
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
`cloudflared`) and exposes only the container port (`${MCP_PORT:-8080}`) — it is
never published to the host. Point the existing tunnel route at it, e.g.
`gtasks.<your-domain>` → `http://gtasks-mcp:${MCP_PORT:-8080}`, set
`MCP_ALLOWED_HOSTS=gtasks.<your-domain>` in `.env`, then add a custom MCP connector
in Vibe: URL `https://gtasks.<your-domain>/mcp` with header
`Authorization: Bearer <MCP_BEARER_TOKEN>`.

## Security checklist before going public

- `MCP_BEARER_TOKEN` is mandatory whenever `MCP_HOST` is not loopback: the server
  refuses to start without it (fail closed), rejects tokens shorter than 16 chars,
  compares them in constant time, and never logs the token — only 401 events.
- The built-in FastMCP Host/Origin guard (`host_origin_protection`) is always on:
  requests with a `Host` other than `localhost`/`127.0.0.1`/`::1` plus
  `MCP_ALLOWED_HOSTS` get a 421, which blocks DNS-rebinding and direct-IP probing
  with a wrong Host header.
- Keep the origin port firewalled: compose uses `expose`, not `ports`, so the port
  is reachable only from the tunnel network. Do not add a public `ports:` mapping.
- Put a Cloudflare Access (Zero Trust) policy in front of the hostname, restricted
  to your own email — the bearer token is a static shared secret, Access adds a
  second factor. Optionally add WAF rate-limiting rules on the route.
- Only ever send the bearer token over HTTPS (the tunnel provides it); treat the
  token like a password and rotate it (`openssl rand -hex 32`) if leaked.
- `/healthz` stays unauthenticated on purpose (Docker healthcheck); it only reports
  status/version, never secrets. Block it at the tunnel if you don't need it.
- The Google OAuth scope is `tasks` only (read/write), nothing else; the refresh
  token lives only in the server `.env`, which is never baked into the image.
- The container runs as a non-root user and `.env` is injected at runtime.

## Tests

```sh
uv sync
uv run pytest
```

Unit tests mock httpx (`httpx.MockTransport`): every tool happy path, date
normalization, pagination merging, 401 → refresh → retry, and every error mapping.
