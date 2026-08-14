"""Isolated Codex CLI adapter: App Server metadata, quota, and rollouts."""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

from .context import tail_lines

if TYPE_CHECKING:
    from .state import Session

log = logging.getLogger("mft.codex")


@dataclass(frozen=True)
class Thread:
    thread_id: str
    session_id: str
    cwd: str
    path: str = ""
    title: str = ""
    updated_at: int = 0


def app_server_call(method: str, params: Optional[dict] = None, timeout: float = 3.0) -> Optional[dict]:
    """Call one stable App Server method over stdio, failing closed."""
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            ["codex", "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        assert process.stdin is not None and process.stdout is not None
        messages = (
            {"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "agent-midi-twister", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False},
            }},
            {"method": "initialized", "params": {}},
            {"id": 2, "method": method, "params": params or {}},
        )
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") == 2:
                result = response.get("result")
                return result if isinstance(result, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("Codex App Server %s unavailable: %s", method, exc)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
    return None


def list_threads(limit: int = 100) -> list[Thread]:
    result = app_server_call(
        "thread/list",
        {"limit": limit, "sortKey": "updated_at", "sortDirection": "desc", "sourceKinds": ["cli"]},
    )
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    found: list[Thread] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not row.get("cwd"):
            continue
        found.append(Thread(
            thread_id=str(row["id"]),
            session_id=str(row.get("sessionId") or row["id"]),
            cwd=str(row["cwd"]),
            path=str(row.get("path") or ""),
            title=str(row.get("name") or row.get("preview") or ""),
            updated_at=int(row.get("updatedAt") or 0),
        ))
    return found


def rate_limit() -> Optional[tuple[float, str]]:
    """Five-hour Codex quota as ``(used percent, reset epoch)``."""
    result = app_server_call("account/rateLimits/read")
    snapshot = result.get("rateLimits") if isinstance(result, dict) else None
    primary = snapshot.get("primary") if isinstance(snapshot, dict) else None
    if not isinstance(primary, dict) or not isinstance(primary.get("usedPercent"), (int, float)):
        return None
    return float(primary["usedPercent"]), str(primary.get("resetsAt") or "")


def read_context(path: str) -> Optional[tuple[int, int]]:
    """Recognized rollout-v1 context usage; unknown shapes return ``None``.

    The structural signature is the version boundary: a session_meta record
    with a CLI version followed by an event_msg/token_count record containing
    ``last_token_usage.total_tokens`` and ``model_context_window``.
    """
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            first = handle.readline().decode("utf-8", errors="replace")
        lines = tail_lines(path)
    except OSError:
        return None
    try:
        meta = json.loads(first)
    except json.JSONDecodeError:
        return None
    payload = meta.get("payload")
    versioned = (
        meta.get("type") == "session_meta"
        and isinstance(payload, dict)
        and isinstance(payload.get("cli_version"), str)
    )
    if not versioned:
        return None
    for line in reversed(lines):
        if '"token_count"' not in line:
            continue
        try:
            entry = json.loads(line)
            payload = entry["payload"]
            info = payload["info"]
            usage = info["last_token_usage"]
            tokens = usage["total_tokens"]
            limit = info["model_context_window"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if (
            entry.get("type") == "event_msg"
            and payload.get("type") == "token_count"
            and isinstance(tokens, int) and tokens > 0
            and isinstance(limit, int) and limit > 0
        ):
            return tokens, limit
    return None


def refresh_context(session: "Session", now: float) -> None:
    if not session.transcript_path or now - session.context_checked_at < 5.0:
        return
    session.context_checked_at = now
    reading = read_context(session.transcript_path)
    if reading is not None:
        session.context_tokens, session.context_limit = reading
