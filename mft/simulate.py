"""Fake Claude Code sessions, so you can watch the lights before wiring hooks.

    python -m mft.simulate --sessions 4

Posts the same JSON shapes the real hooks do, at plausible intervals: prompts,
tool churn, the occasional permission prompt or rate-limit error, then Stop.
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import config

PROJECTS = ["~/projects/api", "~/projects/web", "~/dotfiles", "~/projects/mft"]
TOOLS = ["Read", "Edit", "Bash", "Grep", "Write"]
MODEL = "claude-opus-5"


def post(url: str, event: dict) -> None:
    body = json.dumps(event).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"daemon unreachable: {exc}")


class FakeTranscript:
    """A real transcript file with fake numbers in it.

    The context ring reads token counts out of the session's transcript, so the
    simulator has to write one -- faking the *reading* instead would leave the
    part most likely to be wrong (the parser) unexercised.
    """

    def __init__(self, session_id: str, tokens: int) -> None:
        self.path = Path(tempfile.gettempdir()) / f"mft-sim-{session_id}.jsonl"
        self.tokens = tokens
        self.path.write_text("")

    def grow(self, by: int) -> None:
        self.tokens += by
        line = {
            "type": "assistant",
            "message": {
                "model": MODEL,
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": by,
                    "cache_read_input_tokens": max(0, self.tokens - by),
                    "output_tokens": 300,
                },
            },
        }
        with self.path.open("a") as handle:
            handle.write(json.dumps(line) + "\n")

    def compact(self) -> None:
        """What a compaction looks like from the outside: the gauge drops."""
        self.tokens = 0
        self.grow(30_000)


def run_session(url: str, rng: random.Random, index: int, stop: threading.Event) -> None:
    session_id = str(uuid.uuid4())
    cwd = rng.choice(PROJECTS)
    # Each fake session starts somewhere different in its context window, so the
    # board shows a spread of ring fills rather than sixteen identical gauges.
    transcript = FakeTranscript(session_id, rng.randint(20_000, 150_000))
    # One session in the batch runs unsupervised, so you can see what that
    # looks like without actually running one unsupervised.
    mode = "bypassPermissions" if index == 0 else "default"
    # ...and one opens a permission prompt and never answers it, because every
    # other prompt here is answered inside ten seconds and attention debt takes
    # five minutes to saturate. Without this, the escalating strobe -- the whole
    # visible consequence of neglect -- never gets far enough to look at, and the
    # thing you want to compare it against is a fresh gate on another encoder.
    abandoned = index == 1
    send = lambda **kw: post(
        url,
        {
            "session_id": session_id,
            "cwd": cwd,
            "permission_mode": mode,
            "transcript_path": str(transcript.path),
            "model": MODEL,
            **kw,
        },
    )

    # A distinct tty per fake session, since slots are keyed on the terminal
    # rather than the session id -- which is also what makes the `/clear` below
    # land back on the same knob.
    terminal = {"TERM_PROGRAM": "Apple_Terminal", "tty": f"/dev/ttys{index:03d}"}
    send(hook_event_name="SessionStart", terminal=terminal)
    if abandoned:
        stop.wait(rng.uniform(2, 6))
        send(
            hook_event_name="Notification",
            notification_type="permission_prompt",
            message="Claude needs your permission to use Bash",
        )
        # And that is the last thing it ever says. Press the encoder to forgive
        # it and watch the rate drop back to its base in one frame.
        stop.wait()
        return
    while not stop.is_set():
        stop.wait(rng.uniform(2, 8))
        if stop.is_set():
            return
        send(hook_event_name="UserPromptSubmit")
        stop.wait(rng.uniform(0.5, 2.5))

        if rng.random() < 0.15:
            # Plan approval: its own hue, slower than a permission gate.
            send(
                hook_event_name="Notification",
                notification_type="permission_prompt",
                message="Claude has written up a plan and is ready to execute. "
                "Would you like to proceed?",
            )
            stop.wait(rng.uniform(3, 9))
            send(hook_event_name="UserPromptSubmit")

        # Fan out a variable number, so the stack in the bottom-right corner
        # visibly grows and shrinks rather than always being two deep.
        fanout = rng.choice([0, 0, 0, 1, 2, 3, 5])
        for _ in range(fanout):
            send(hook_event_name="SubagentStart")

        for _ in range(rng.randint(1, 8)):
            if stop.is_set():
                return
            tool = rng.choice(TOOLS)
            send(hook_event_name="PreToolUse", tool_name=tool)
            stop.wait(rng.uniform(0.3, 1.5))
            transcript.grow(rng.randint(2_000, 12_000))
            send(hook_event_name="PostToolUse", tool_name=tool)

            if rng.random() < 0.12:
                send(
                    hook_event_name="Notification",
                    notification_type=rng.choice(
                        ["permission_prompt", "idle_prompt", "agent_needs_input"]
                    ),
                    message="Claude needs your permission to use Bash",
                )
                stop.wait(rng.uniform(3, 9))
                send(hook_event_name="UserPromptSubmit")

        for _ in range(fanout):
            stop.wait(rng.uniform(0.2, 1.0))
            send(hook_event_name="SubagentStop")

        if rng.random() < 0.15:
            send(hook_event_name="PreCompact", trigger="auto")
            stop.wait(rng.uniform(1, 3))
            transcript.compact()  # and the ring drops back down with it
            send(hook_event_name="PostCompact", trigger="auto")

        if rng.random() < 0.1:
            send(hook_event_name="StopFailure", error_type="rate_limit")
            stop.wait(rng.uniform(5, 12))
        send(hook_event_name="Stop")

        if rng.random() < 0.12:
            # A `/clear`: SessionEnd with reason `clear`, then a brand new
            # session id in the same tab. The encoder must not move, and the
            # gauge must empty. Half the time the pair arrives the other way
            # round, because in practice the order is not guaranteed.
            end = dict(hook_event_name="SessionEnd", reason="clear")
            old_id = session_id
            session_id = str(uuid.uuid4())
            transcript = FakeTranscript(session_id, 0)
            start = dict(hook_event_name="SessionStart", source="clear", terminal=terminal)
            if rng.random() < 0.5:
                post(url, {"session_id": old_id, "cwd": cwd, **end})
                send(**start)
            else:
                send(**start)
                post(url, {"session_id": old_id, "cwd": cwd, **end})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=4)
    parser.add_argument("--url", default=f"http://{config.HOST}:{config.PORT}/event")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    stop = threading.Event()
    threads = [
        threading.Thread(
            target=run_session,
            args=(args.url, random.Random(rng.random()), index, stop),
            daemon=True,
        )
        for index in range(args.sessions)
    ]
    for thread in threads:
        thread.start()
    print(f"simulating {args.sessions} sessions -> {args.url} (ctrl-c to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        print("\nstopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
