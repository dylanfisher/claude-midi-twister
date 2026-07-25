"""The wire the hooks arrive on: one HTTP handler, and nothing else.

Split out of :mod:`mft.daemon` because it is transport and the rest of that
module is a display. Nothing here knows what a session is; it reads a payload,
hands it to something with a ``handle_event``, and answers 204.

That 204 is invariant 2 made concrete, and it is the reason this module is worth
keeping small and separate: Claude Code parses a hook's response body as hook
control JSON -- it is how a hook blocks a tool call or injects context. A
visualizer has no opinion about any of that, so the *only* code that can put
bytes on that socket lives in one file, where the rule is stated once and can be
checked at a glance.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

log = logging.getLogger("mft.httpd")

#: Header `hooks/notify.sh` carries the terminal identity in, as
#: ``name=value;name=value``. A header rather than a field spliced into the
#: event JSON: the script's whole job is to pipe stdin at `curl` untouched, and
#: rewriting JSON in `sh` to add one object is how that stops being reliable.
#:
#: The point of it is that *every* event names its tab, not just the two that
#: run the Python hook. An event that arrives anonymous can only be answered by
#: guessing which session it belongs to, and a wrong guess is a knob that lies.
TERMINAL_HEADER = "X-MFT-Terminal"

#: Bounds on what we will read out of that header: it is trusted input from our
#: own hook, but it arrives over a socket and nothing else here is unbounded.
TERMINAL_HEADER_MAX = 4096
TERMINAL_HEADER_FIELDS = 24


class EventSink(Protocol):
    """What this module needs of the thing behind the socket.

    Written down rather than importing :class:`~mft.daemon.Visualizer`, which
    would make the transport depend on the display it happens to be serving --
    and would be a cycle, since the daemon builds the server.
    """

    def handle_event(self, event: dict) -> dict: ...

    def status(self) -> dict: ...


def parse_terminal_header(raw: str) -> dict:
    """``name=value;name=value`` -> a terminal dict, or ``{}`` if it says nothing.

    Deliberately forgiving: this is a display, and an identity we cannot parse
    should cost the event its tab, not the event. Values are taken verbatim up to
    the first ``;`` -- terminal identifiers are short, printable and delimiter
    free (``w0t0p0:UUID``, ``%3``, ``/dev/ttys004``) -- and anything else is
    dropped rather than repaired.
    """
    if not raw or len(raw) > TERMINAL_HEADER_MAX:
        return {}
    terminal: dict[str, str] = {}
    for field in raw.split(";")[:TERMINAL_HEADER_FIELDS]:
        name, sep, value = field.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name or not value:
            continue
        terminal[name] = value
    return terminal


class HookHandler(BaseHTTPRequestHandler):
    server_version = "mft/1.0"

    @property
    def sink(self) -> EventSink:
        return self.server.sink  # type: ignore[attr-defined]

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self) -> None:
        """204, deliberately, for every hook without exception.

        Claude Code parses a hook's response body as hook control JSON -- it is
        how a hook blocks a tool call or injects context. A visualizer has no
        opinion about any of that, so it must never put a body on the wire.
        Debug output lives on GET /status instead.
        """
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_event(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            event = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            log.warning("bad json from hook: %s", exc)
            return None
        if not isinstance(event, dict):
            log.warning("hook payload is not an object: %r", type(event).__name__)
            return None
        # The body wins: `register_session.py` puts a richer terminal in it than
        # a header can carry, and it is the same field either way.
        if not isinstance(event.get("terminal"), dict) or not event["terminal"]:
            identity = parse_terminal_header(self.headers.get(TERMINAL_HEADER, ""))
            if identity:
                event["terminal"] = identity
        return event

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        event = self._read_event()
        if event is None:
            self._send_empty()
            return

        try:
            result = self.sink.handle_event(event)
        except Exception:
            log.exception("event handling failed")
        else:
            if not result.get("ok"):
                log.warning("event rejected: %s", result.get("error"))
        self._send_empty()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/status"):
            self._send(200, self.sink.status())
        else:
            self._send(200, {"ok": True, "hint": "POST hook JSON here"})

    def log_message(self, fmt: str, *args) -> None:
        log.debug("http: " + fmt, *args)


def serve(sink: EventSink, host: str, port: int) -> ThreadingHTTPServer:
    """Start the hook listener on a daemon thread and hand back the server.

    Returned rather than owned, because the caller is the one that knows when
    the board is finished with it -- see :func:`mft.cli.main`.
    """
    server = ThreadingHTTPServer((host, port), HookHandler)
    server.sink = sink  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, name="mft-http", daemon=True).start()
    log.info("listening on http://%s:%d", host, port)
    return server
