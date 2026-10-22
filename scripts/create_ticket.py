#!/usr/bin/env python3
"""Create a Ticket through the running API (``POST /api/tickets``).

An operator CLI that replaces curl. It only calls the API: it never touches
the database, never starts an executor, and changes no lifecycle state itself.

Exit codes: 0 on success, 1 on an HTTP error status, 2 if the API cannot be
reached. Argument errors exit 2 through argparse.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any
import urllib.error
import urllib.request

DEFAULT_API_BASE_URL = "http://127.0.0.1:8100"
API_BASE_URL_ENV = "AGENT_TASKFLOW_API_BASE_URL"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="registered repository name")
    parser.add_argument("--priority", default="normal", help="Ticket priority (default: normal)")
    parser.add_argument(
        "--api-base-url",
        default=None,
        help=f"API base URL (default: ${API_BASE_URL_ENV} or {DEFAULT_API_BASE_URL})",
    )
    parser.add_argument("--json", action="store_true", help="print the full JSON response")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("prompt", nargs="?", default=None, help="prompt text")
    source.add_argument("--prompt-file", type=Path, help="read the prompt from this file")
    return parser.parse_args(argv)


def _api_base_url(explicit: str | None) -> str:
    base = explicit if explicit is not None else os.environ.get(API_BASE_URL_ENV) or DEFAULT_API_BASE_URL
    return base.rstrip("/")


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file is not None:
        return args.prompt_file.read_text(encoding="utf-8")
    return args.prompt


def _build_request(base_url: str, payload: dict[str, str]) -> urllib.request.Request:
    return urllib.request.Request(
        f"{base_url}/api/tickets",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )


def _error_detail(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if isinstance(parsed, dict) and "detail" in parsed:
        detail = parsed["detail"]
        return detail if isinstance(detail, str) else json.dumps(detail)
    return text


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    base_url = _api_base_url(args.api_base_url)
    payload = {
        "repository": args.repository,
        "prompt": _read_prompt(args),
        "priority": args.priority,
    }
    request = _build_request(base_url, payload)
    try:
        with urllib.request.urlopen(request) as response:
            body: Any = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {_error_detail(exc.read() or b'')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"cannot reach the API at {base_url}: {exc.reason}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(body, indent=2, sort_keys=True))
    else:
        print(f"{body['task_key']} {body['display_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
