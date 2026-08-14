"""Shared JSON hook-file ownership for the Claude and Codex installers."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

OwnsHook = Callable[[dict], bool]


def read(path: Path) -> dict | None:
    """Read one settings object, refusing malformed or non-object JSON."""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError as exc:
        print(f"{path} is not valid JSON ({exc}); refusing to touch it")
        return None
    if not isinstance(value, dict):
        print(f"{path} does not contain a JSON object; refusing to touch it")
        return None
    return value


def write(path: Path, data: dict) -> None:
    """Back up an existing file, then write one formatted settings object."""
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        print(f"backed up {path} -> {backup}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def strip(settings: dict, owns: OwnsHook) -> dict:
    """Remove exactly the hook entries recognized by ``owns``."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        if not isinstance(hooks[event], list):
            continue
        entries = hooks[event]
        kept = [entry for entry in entries if not (isinstance(entry, dict) and owns(entry))]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return settings


def merge(settings: dict, additions: dict, owns: OwnsHook) -> dict:
    """Replace our hook entries while preserving every unrelated entry."""
    hooks = strip(settings, owns).setdefault("hooks", {})
    for event, entries in additions.items():
        current = hooks.get(event, []) if isinstance(hooks.get(event, []), list) else []
        hooks[event] = current + entries
    return settings


def installed(path: str | Path, owns: OwnsHook) -> bool:
    """Whether a readable hook file contains at least one owned entry."""
    try:
        data = json.loads(Path(path).read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return False
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    if not isinstance(hooks, dict):
        return False
    return any(
        isinstance(entry, dict) and owns(entry)
        for entries in hooks.values()
        if isinstance(entries, list)
        for entry in entries
    )


def missing_events(
    path: str | Path,
    expected: Iterable[str],
    owns: OwnsHook,
    *,
    empty_if_uninstalled: bool = True,
) -> list[str]:
    """Expected event names with no owned entry, in expected order."""
    try:
        data = json.loads(Path(path).read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return []
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    if not isinstance(hooks, dict):
        return [] if empty_if_uninstalled else list(expected)
    installed_events = {
        event
        for event, entries in hooks.items()
        if isinstance(entries, list)
        and any(isinstance(entry, dict) and owns(entry) for entry in entries)
    }
    if empty_if_uninstalled and not installed_events:
        return []
    return [event for event in expected if event not in installed_events]
