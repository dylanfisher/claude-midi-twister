#!/usr/bin/env python3
"""Install (or remove) the visualizer's hooks in ~/.claude/settings.json.

    python3 install_hooks.py --print       # show the block, change nothing
    python3 install_hooks.py               # merge it in, with a .bak backup
    python3 install_hooks.py --uninstall   # take it back out

Every event runs a command hook that posts the event JSON to the daemon and
exits 0 whatever happens. SessionStart and UserPromptSubmit run
``register_session.py``, because only a command hook can see the environment --
and the environment is the only thing that says which terminal tab the session
lives in, which is what press-to-focus needs. The rest run ``notify.sh``, which
is a `curl` and nothing else. All but SessionEnd are ``async``, so nothing you
are waiting on is ever behind one; SessionEnd is synchronous because the
process is about to go away and the board needs to hear about it.

``type: "http"`` hooks would be cheaper -- they post with no process spawn at
all -- but Claude Code reports every failed HTTP hook to the user, and offers
no way to opt out, so a stopped daemon means two `connect ECONNREFUSED` lines
per tool call. Pass ``--http-hooks`` to take that trade if the daemon is always
up on your machine.

Nothing installed here can influence a session: every hook is notify-only and
the daemon answers all of them with a bodiless 204. Permissions in particular
are shown and never answered -- the device is a display, not a control surface.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
REGISTER = REPO / "hooks" / "register_session.py"
NOTIFY = REPO / "hooks" / "notify.sh"

#: Marks our entries so --uninstall can find them again.
TAG = "mft-twister"

#: `PermissionRequest` is deliberately *not* installed. An HTTP hook on it sits
#: in the path between Claude Code and its own permission prompt, and a
#: visualizer has no business being able to delay -- let alone answer -- a
#: permission. `Notification` already reports permission prompts, after the fact
#: and with nothing waiting on the reply, which is all a light needs.

#: (event name, matcher or None). Matchers only apply to the tool and
#: subagent events; the rest ignore them. `Notification` carries the thing we
#: care about -- which of permission / idle / needs-input / completed this is --
#: in its payload as `notification_type`, so it needs no matcher.
NOTIFY_EVENTS = [
    ("PreToolUse", "*"),
    ("PostToolUse", "*"),
    ("PostToolUseFailure", "*"),
    ("Notification", None),
    ("PreCompact", None),
    ("PostCompact", None),
    ("SubagentStart", "*"),
    ("SubagentStop", "*"),
    ("Stop", None),
    ("StopFailure", None),
    ("SessionEnd", None),
]

#: The one event that isn't `async`. Everything else can be delivered late
#: without anyone noticing; this one has to be delivered at all, and an async
#: hook on the way out races the process it was spawned from. It's a `curl` to
#: localhost, so "synchronous" is a millisecond or two.
SYNCHRONOUS = {"SessionEnd"}

#: Runs in the message render path -- opt in with --with-message-display once
#: everything else works.
STREAMING_EVENT = ("MessageDisplay", None)


#: Events that carry the session's environment to the daemon. SessionStart is
#: the announcement; UserPromptSubmit repeats it, so that a session started
#: while the daemon was down -- or one still running across a daemon restart --
#: is focusable again after a single turn rather than never. Hooks only push,
#: so without the repeat there is no way to ask a session where it lives.
REGISTER_EVENTS = ("SessionStart", "UserPromptSubmit")


def build_hooks(url: str, with_message_display: bool, http: bool = False) -> dict:
    def command(argv: str, is_async: bool = True) -> dict:
        hook = {"type": "command", "command": argv, "timeout": 5, "_source": TAG}
        if is_async:
            hook["async"] = True
        return {"hooks": [hook]}

    quote = shlex.quote
    hooks: dict[str, list] = {
        event: [command(f"{quote(sys.executable)} {quote(str(REGISTER))} --url {quote(url)}")]
        for event in REGISTER_EVENTS
    }
    events = list(NOTIFY_EVENTS)
    if with_message_display:
        events.append(STREAMING_EVENT)

    for event, matcher in events:
        if http:
            entry: dict = {"hooks": [{"type": "http", "url": url, "_source": TAG}]}
        else:
            entry = command(
                f"{quote(str(NOTIFY))} {quote(url)}",
                is_async=event not in SYNCHRONOUS,
            )
        if matcher:
            entry["matcher"] = matcher
        hooks[event] = [entry]
    return hooks


def _is_ours(entry: dict) -> bool:
    return any(h.get("_source") == TAG for h in entry.get("hooks", []))


def missing_events(settings_path: str | os.PathLike = "") -> list[str]:
    """Events this code expects that aren't installed, in install order.

    Hooks are read from the settings file, not from here, so the two drift the
    moment an event is added and `install_hooks.py` isn't re-run -- and the only
    symptom is a part of the board that quietly never lights up. Cheap to check,
    so the daemon checks it at startup and says so.
    """
    path = Path(settings_path or os.path.expanduser("~/.claude/settings.json"))
    try:
        settings = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        # No settings, or settings we can't read: not our business to diagnose.
        return []
    installed = {
        event
        for event, entries in settings.get("hooks", {}).items()
        if any(_is_ours(e) for e in entries)
    }
    if not installed:
        # Nothing of ours is installed at all. That's "not set up", not "drift",
        # and reporting every event as missing would only bury the real message.
        return []
    expected = list(REGISTER_EVENTS) + [event for event, _ in NOTIFY_EVENTS]
    return [event for event in expected if event not in installed]


def merge(settings: dict, new_hooks: dict) -> dict:
    # Strip first, so an event we used to install and no longer do
    # (PermissionRequest) doesn't survive an upgrade as an orphan pointing at an
    # endpoint the daemon has stopped serving.
    existing = strip(settings).setdefault("hooks", {})
    for event, entries in new_hooks.items():
        current = [e for e in existing.get(event, []) if not _is_ours(e)]
        existing[event] = current + entries
    return settings


def strip(settings: dict) -> dict:
    existing = settings.get("hooks", {})
    for event in list(existing):
        kept = [e for e in existing[event] if not _is_ours(e)]
        if kept:
            existing[event] = kept
        else:
            del existing[event]
    if not existing:
        settings.pop("hooks", None)
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--settings",
        default=os.path.expanduser("~/.claude/settings.json"),
        help="settings file to edit (default: user settings, so every project gets it)",
    )
    parser.add_argument("--url", default="http://127.0.0.1:7654/event")
    parser.add_argument("--with-message-display", action="store_true")
    parser.add_argument(
        "--http-hooks",
        action="store_true",
        help="use type:http hooks (no process spawn, but noisy when the daemon is down)",
    )
    parser.add_argument("--print", dest="dry", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report events this code expects that aren't installed, and exit non-zero",
    )
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()

    hooks = build_hooks(args.url, args.with_message_display, http=args.http_hooks)
    if args.dry:
        print(json.dumps({"hooks": hooks}, indent=2))
        return 0

    if args.check:
        missing = missing_events(args.settings)
        if not missing:
            print(f"{args.settings}: up to date")
            return 0
        print(f"{args.settings} is missing {len(missing)} event(s):")
        for event in missing:
            print(f"  {event}")
        print("re-run install_hooks.py to add them.")
        return 1

    path = Path(args.settings)
    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            print(f"{path} is not valid JSON ({exc}); refusing to touch it")
            return 1
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        print(f"backed up {path} -> {backup}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)

    settings = strip(settings) if args.uninstall else merge(settings, hooks)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"{'removed from' if args.uninstall else 'installed into'} {path}")
    if not args.uninstall:
        print("start the daemon, then open a new Claude Code session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
