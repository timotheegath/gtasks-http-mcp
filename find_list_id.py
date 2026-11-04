"""Print your Google Tasks list titles with their ids, to set GTASKS_DEFAULT_LIST.

List ids are not shown anywhere in the Google Tasks UI. Run this with the same
credentials as the server and pick the id of the list you want as default:

    uv run python find_list_id.py                 # loads ./.env automatically if present
    uv run python find_list_id.py --env-file ~/secrets/gtasks.env
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from google_client import (
    DEFAULT_LIST_ID,
    AuthExpiredError,
    GoogleAPIError,
    GoogleTasksClient,
    load_env_file,
)

REQUIRED_ENV = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
MAX_PAGE = 100


async def fetch_all_tasklists(client: GoogleTasksClient) -> list[dict[str, Any]]:
    lists: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        resp = await client.list_tasklists(max_results=MAX_PAGE, page_token=token)
        lists.extend(resp.get("items", []))
        token = resp.get("nextPageToken")
        if not token:
            return lists


def format_lists(lists: list[dict[str, Any]]) -> str:
    width = max((len(tl.get("title") or "(untitled)") for tl in lists), default=0)
    lines = []
    for tl in lists:
        title = tl.get("title") or "(untitled)"
        marker = "   <- Google's @default list" if tl["id"] == DEFAULT_LIST_ID else ""
        lines.append(f"  {title:<{width}}  {tl['id']}{marker}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Print Google Tasks list titles with their ids (for GTASKS_DEFAULT_LIST)."
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="KEY=VALUE file to load credentials from (default: ./.env if it exists).",
    )
    args = parser.parse_args(argv)
    if os.path.exists(args.env_file):
        load_env_file(args.env_file)
    missing = [key for key in REQUIRED_ENV if not os.getenv(key)]
    if missing:
        parser.error(
            f"Missing {', '.join(missing)} — set them in the environment or in {args.env_file} "
            "(same credentials as the MCP server; run auth.py first if you don't have a "
            "GOOGLE_REFRESH_TOKEN yet)."
        )

    async def run() -> int:
        client = GoogleTasksClient.from_env()
        try:
            lists = await fetch_all_tasklists(client)
        except AuthExpiredError as exc:
            print(f"{exc.error}: {exc.message}", file=sys.stderr)
            print(f"Re-run auth.py, or re-consent here: {exc.detail['consent_url']}", file=sys.stderr)
            return 1
        except GoogleAPIError as exc:
            print(f"{exc.error}: {exc.message}", file=sys.stderr)
            return 1
        finally:
            await client.close()
        if not lists:
            print("No task lists found in this Google account.")
            return 0
        lists.sort(key=lambda tl: (tl.get("title") or "").lower())
        print(f"Task lists in this account ({len(lists)}) — set GTASKS_DEFAULT_LIST to an id:\n")
        print(format_lists(lists))
        print("\nThen add to your server .env:  GTASKS_DEFAULT_LIST=<id from above>")
        return 0

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
