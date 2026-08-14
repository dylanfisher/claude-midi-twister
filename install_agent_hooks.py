#!/usr/bin/env python3
"""Install Agent Midi Twister hooks for Claude Code, Codex CLI, or both.

The default combined install adopts an existing Claude Twister hook install
without rewriting it.  The legacy hooks already speak the protocol the daemon
expects, so adding Codex support does not require changing a working Claude
setup.  Use ``--provider claude`` when replacing or upgrading Claude hooks is
the intended operation.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

import hook_settings
import install_hooks as claude

REPO = Path(__file__).resolve().parent
FORWARD = REPO / "hooks" / "forward.py"
TAG = claude.TAG
CODEX_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PermissionRequest", "PreCompact", "PostCompact", "SubagentStart",
    "SubagentStop", "Stop", "SessionEnd",
)


def _command(provider: str, event: str, url: str) -> dict:
    command = " ".join(
        shlex.quote(part)
        for part in (
            sys.executable, str(FORWARD), "--provider", provider,
            "--event", event, "--url", url,
        )
    )
    hook: dict = {
        "type": "command",
        "command": command,
        "timeout": 3 if event == "SessionEnd" else 5,
        "_source": TAG,
    }
    # No matcher means all tools. In particular, do not copy Claude's "*".
    return {"hooks": [hook]}


def build_codex_hooks(url: str) -> dict:
    return {event: [_command("codex", event, url)] for event in CODEX_EVENTS}


def _is_ours(entry: dict) -> bool:
    return any(
        isinstance(hook, dict) and hook.get("_source") == TAG
        for hook in entry.get("hooks", [])
    )


def strip_hooks(settings: dict) -> dict:
    return hook_settings.strip(settings, _is_ours)


def merge_hooks(settings: dict, additions: dict) -> dict:
    return hook_settings.merge(settings, additions, _is_ours)


def missing_codex_events(path: str | os.PathLike) -> list[str]:
    return hook_settings.missing_events(path, CODEX_EVENTS, _is_ours)


def _installed(path: str | os.PathLike) -> bool:
    return hook_settings.installed(path, _is_ours)


def _claude_installed(path: str | os.PathLike) -> bool:
    """Recognize both tagged and pre-tag Claude Twister installations.

    Some existing settings files contain the original hook commands without
    the private ``_source`` marker.  Those hooks are still valid and, more
    importantly, are exactly the installations the combined upgrade must
    adopt rather than replace.
    """
    return hook_settings.installed(path, claude._is_ours)


def _inline_warning(config_path: Path) -> None:
    try:
        text = config_path.read_text()
    except OSError:
        return
    if "hooks" in text:
        print(
            f"warning: {config_path} also appears to define hooks; Codex merges "
            "config.toml and hooks.json, so review both with /hooks."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("claude", "codex", "all"), default="all")
    parser.add_argument("--claude-settings", default=os.path.expanduser("~/.claude/settings.json"))
    parser.add_argument("--codex-hooks", default=os.path.expanduser("~/.codex/hooks.json"))
    parser.add_argument("--codex-config", default=os.path.expanduser("~/.codex/config.toml"))
    parser.add_argument("--url", default="http://127.0.0.1:7654/event")
    parser.add_argument("--print", dest="dry", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--with-message-display", action="store_true")
    args = parser.parse_args(argv)
    providers = ("claude", "codex") if args.provider == "all" else (args.provider,)

    claude_hooks = claude.build_hooks(args.url, args.with_message_display)
    codex_hooks = build_codex_hooks(args.url)
    if args.dry:
        result = {}
        if "claude" in providers:
            result["claude"] = {"hooks": claude_hooks, "env": claude.ENV}
        if "codex" in providers:
            result["codex"] = {"hooks": codex_hooks}
        print(json.dumps(result, indent=2))
        return 0

    if args.check:
        missing: list[str] = []
        if "claude" in providers:
            if not _claude_installed(args.claude_settings):
                missing += [f"claude:{e}" for e in (*claude.REGISTER_EVENTS, *(e for e, _ in claude.NOTIFY_EVENTS))]
            else:
                missing += [f"claude:{e}" for e in claude.missing_events(args.claude_settings)]
                missing += [f"claude:env:{e}" for e in claude.missing_env(args.claude_settings)]
        if "codex" in providers:
            missing += [f"codex:{e}" for e in (
                missing_codex_events(args.codex_hooks)
                if _installed(args.codex_hooks) else CODEX_EVENTS
            )]
        if missing:
            print("out of date: " + ", ".join(missing))
            return 1
        print("Agent Midi Twister hooks are up to date")
        return 0

    preserve_claude = (
        args.provider == "all"
        and not args.uninstall
        and _claude_installed(args.claude_settings)
    )

    if "claude" in providers and preserve_claude:
        print(
            f"left existing Claude hooks unchanged in {args.claude_settings}; "
            "use --provider claude to replace them"
        )
    elif "claude" in providers:
        path = Path(args.claude_settings)
        data = hook_settings.read(path)
        if data is None:
            return 1
        data = claude.strip(data) if args.uninstall else claude.merge(data, claude_hooks)
        hook_settings.write(path, data)
        print(f"{'removed from' if args.uninstall else 'installed into'} {path}")

    if "codex" in providers:
        path = Path(args.codex_hooks)
        data = hook_settings.read(path)
        if data is None:
            return 1
        data = strip_hooks(data) if args.uninstall else merge_hooks(data, codex_hooks)
        hook_settings.write(path, data)
        print(f"{'removed from' if args.uninstall else 'installed into'} {path}")
        _inline_warning(Path(args.codex_config))

    if not args.uninstall and "codex" in providers:
        print("In Codex, run /hooks and review and trust the installed definitions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
