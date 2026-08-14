"""Provider boundary for hook payloads.

The renderer understands one lifecycle vocabulary.  Hooks from each runtime
are normalized here so provider-specific spellings never leak into it.
"""

from __future__ import annotations

from typing import Any

PROVIDERS = frozenset({"claude", "codex"})


def provider_of(event: dict[str, Any]) -> str:
    value = str(event.get("provider") or "claude").lower()
    return value if value in PROVIDERS else "claude"


def normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical event without mutating the hook's object.

    Codex hook releases have used thread/conversation terminology around the
    stable hook vocabulary, so the aliases are deliberately narrow and
    fail-closed. Unknown lifecycle names remain unknown to ``apply_event``.
    """
    event = dict(raw)
    provider = provider_of(event)
    event["provider"] = provider

    if not event.get("session_id"):
        for key in ("thread_id", "conversation_id", "sessionId"):
            if event.get(key):
                event["session_id"] = str(event[key])
                break
    if not event.get("hook_event_name"):
        event["hook_event_name"] = str(event.get("event_name") or event.get("event") or "")

    if provider == "codex":
        if event.get("hook_event_name") in {"Notification", "MessageDisplay", "StopFailure"}:
            event["hook_event_name"] = ""
        if not event.get("permission_mode"):
            event["permission_mode"] = str(
                event.get("approval_policy") or event.get("approvalPolicy") or ""
            )
        # Codex calls the human-entered prompt ``prompt`` in some hook payloads.
        if not event.get("message") and event.get("prompt"):
            event["message"] = str(event["prompt"])
    return event
