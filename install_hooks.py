#!/usr/bin/env python3
"""Install (or remove) the visualizer's hooks in ~/.claude/settings.json.

    python3 install_hooks.py --print       # show the block, change nothing
    python3 install_hooks.py               # merge it in, with a .bak backup
    python3 install_hooks.py --uninstall   # take it back out

Every event runs a command hook that posts the event JSON to the daemon and
exits 0 whatever happens. SessionStart and UserPromptSubmit run
``register_session.py``, because only a command hook can see the environment --
and the environment is the only thing that says which terminal tab the session
lives in, which is what press-to-focus needs. The rest run ``notify.sh``: a
`curl`, plus one `ps` to name the tab in a header, because an event that arrives
without one has to be answered by guessing which session it belongs to and
`/clear` is where that guess costs you an encoder. All but SessionEnd are
``async``, so nothing you
are waiting on is ever behind one; SessionEnd is synchronous because the
process is about to go away and the board needs to hear about it.

``type: "http"`` hooks would be cheaper -- they post with no process spawn at
all -- but Claude Code reports every failed HTTP hook to the user, and offers
no way to opt out, so a stopped daemon means two `connect ECONNREFUSED` lines
per tool call. Pass ``--http-hooks`` to take that trade if the daemon is always
up on your machine.

One environment variable goes in alongside them, and it is the only thing this
script installs that changes how a session behaves:
``CLAUDE_CODE_DISABLE_TERMINAL_TITLE`` hands the terminal title to the daemon,
which paints the session's state there as a glyph. See ``ENV`` below for the
whole argument, and ``mft/tab.py`` for what it buys.

Nothing installed here can influence a session: every hook is notify-only and
the daemon answers all of them with a bodiless 204. Permissions in particular
are shown and never answered -- the device is a display, not a control surface.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

import hook_settings

REPO = Path(__file__).resolve().parent
REGISTER = REPO / "hooks" / "register_session.py"
NOTIFY = REPO / "hooks" / "notify.sh"
FORWARD = REPO / "hooks" / "forward.py"

#: Marks our entries so --uninstall can find them again.
TAG = "mft-twister"

#: Environment the daemon needs sessions to start with, and why.
#:
#: Claude Code writes its own terminal title, with an animated spinner in front
#: of it, so while a turn runs it rewrites that title several times a second.
#: The daemon puts a state glyph there instead (see :mod:`mft.tab`) and cannot
#: win a race it loses every frame -- so it takes the title over rather than
#: sharing it, and pays that back by reading Claude Code's *own* generated title
#: out of the transcript and prefixing that. What you lose is the spinner, and
#: the title while the daemon is down; `--uninstall` puts both back.
#:
#: Set here rather than in the app bundle because it has to be in the
#: environment of every `claude`, however it was launched.
ENV = {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"}

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
    # Deliberately unmatched. `SessionEnd` matches on the *reason* a session
    # ended, and `"matcher": "clear"` would install a hook that fires on `/clear`
    # and nothing else. We want every ending, and the reason is in the payload:
    # one hook, and the branch lives in `state.apply_event` where the difference
    # between "the tab is still there" and "the session is gone" is meaningful.
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
    """Recognize current tagged hooks and legacy command-only installs."""
    for hook in entry.get("hooks", []):
        if not isinstance(hook, dict):
            continue
        if hook.get("_source") == TAG:
            return True
        command = hook.get("command", "")
        if str(REGISTER) in command or str(NOTIFY) in command:
            return True
        if str(FORWARD) in command and "--provider claude" in command:
            return True
    return False


def missing_events(settings_path: str | os.PathLike = "") -> list[str]:
    """Events this code expects that aren't installed, in install order.

    Hooks are read from the settings file, not from here, so the two drift the
    moment an event is added and `install_hooks.py` isn't re-run -- and the only
    symptom is a part of the board that quietly never lights up. Cheap to check,
    so the daemon checks it at startup and says so.
    """
    expected = list(REGISTER_EVENTS) + [event for event, _ in NOTIFY_EVENTS]
    path = settings_path or os.path.expanduser("~/.claude/settings.json")
    return hook_settings.missing_events(path, expected, _is_ours)


def missing_env(settings_path: str | os.PathLike = "") -> list[str]:
    """Variables in ENV that the settings file doesn't set to our value.

    Same argument as :func:`missing_events`, and a worse failure: the hooks
    still work without it, the board is still right, and the only symptom is a
    tab strip whose glyph flickers in and out under Claude Code's own title.
    Reported only once something of ours is installed, so an untouched settings
    file isn't diagnosed as drift.
    """
    path = Path(settings_path or os.path.expanduser("~/.claude/settings.json"))
    try:
        settings = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return []
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}
    if not isinstance(hooks, dict) or not any(
        isinstance(entry, dict) and _is_ours(entry)
        for entries in hooks.values()
        if isinstance(entries, list)
        for entry in entries
    ):
        return []
    env = settings.get("env") or {}
    return [name for name, value in ENV.items() if env.get(name) != value]


def merge(settings: dict, new_hooks: dict) -> dict:
    # Strip first, so an event we used to install and no longer do
    # (PermissionRequest) doesn't survive an upgrade as an orphan pointing at an
    # endpoint the daemon has stopped serving.
    hook_settings.merge(settings, new_hooks, _is_ours)
    settings.setdefault("env", {}).update(ENV)
    return settings


def strip(settings: dict) -> dict:
    hook_settings.strip(settings, _is_ours)

    # Only the variables we set, and only where they still hold the value we
    # set them to. `env` is a flat map with nowhere to record who wrote an
    # entry, so a user who has since set one of these deliberately keeps it --
    # the cost of guessing wrong here is silently changing how their sessions
    # run, long after they have forgotten this script touched the file.
    env = settings.get("env", {})
    for name, value in ENV.items():
        if env.get(name) == value:
            del env[name]
    if not env:
        settings.pop("env", None)
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
        print(json.dumps({"hooks": hooks, "env": ENV}, indent=2))
        return 0

    if args.check:
        missing = missing_events(args.settings)
        env = missing_env(args.settings)
        if not missing and not env:
            print(f"{args.settings}: up to date")
            return 0
        if missing:
            print(f"{args.settings} is missing {len(missing)} event(s):")
            for event in missing:
                print(f"  {event}")
        if env:
            print(f"{args.settings} is missing {len(env)} env var(s):")
            for name in env:
                print(f"  {name}={ENV[name]}")
        print("re-run install_hooks.py to add them.")
        return 1

    path = Path(args.settings)
    settings = hook_settings.read(path)
    if settings is None:
        return 1
    settings = strip(settings) if args.uninstall else merge(settings, hooks)
    hook_settings.write(path, settings)
    print(f"{'removed from' if args.uninstall else 'installed into'} {path}")
    if not args.uninstall:
        print("start the daemon, then open a new Claude Code session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
