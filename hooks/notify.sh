#!/bin/sh
# Command hook: forward the event JSON on stdin to the daemon, and never fail.
#
# The obvious way to do this is a `type: "http"` hook, which costs no process
# spawn. The catch is that Claude Code reports *every* failed HTTP hook to the
# user as a non-blocking hook error, and there is no way to opt out -- so with
# the daemon stopped, every tool call prints two `connect ECONNREFUSED` lines.
# A visualizer being down is not something the user needs told twice per tool
# call, so we pay a `curl` (a few ms, and `async`, so nothing waits on it) to
# buy the right to fail silently.
#
# It also tells the daemon which terminal tab the event came from, in a header,
# because an event that doesn't is an event the daemon has to *guess* about.
# `/clear` retires a session id, and the hook that can read the environment
# (`register_session.py`) is a Python process and `async` -- so the new id's
# first tool call routinely arrives long before anything has said where it
# lives. A guess there is a second encoder for a tab that already had one: one
# knob holding the session, the other still pointing at the terminal, and both
# of them for as long as the TTL. The header closes that window for every event
# rather than for the two that run Python.
#
# Identity, not focus context: this reports the few variables that *name* a tab,
# where `register_session.py` collects everything the focus adapters could use.
# The daemon unions the two, so the cheap description here is enough to
# recognise the tab and never replaces the full one.
#
# Usage: notify.sh [url]   -- event JSON arrives on stdin.

# One `ps`, which doubles as the guard: a hook with no controlling tty belongs
# to the desktop app, and the app inherits the environment of whatever launched
# it. Reporting those variables would key the slot on someone else's tab and
# send a press to a window this session isn't in, so an identity with no tty
# behind it is not reported at all.
tty=$(ps -o tty= -p $PPID 2>/dev/null | tr -d '[:space:]')
case "$tty" in
    "" | "??" | "-") tty="" ;;
    /dev/*) ;;
    *) tty="/dev/$tty" ;;
esac

identity=""
if [ -n "$tty" ]; then
    identity="tty=$tty;"
    # The subset of `register_session.py`'s ENV_KEYS that identifies a tab
    # rather than describing it, in the order that file lists them.
    for name in TERM_PROGRAM TERM_SESSION_ID ITERM_SESSION_ID TMUX_PANE \
        WEZTERM_PANE KITTY_WINDOW_ID ALACRITTY_WINDOW_ID WINDOWID; do
        eval "value=\${$name-}"
        # A value carrying the delimiter or a newline would corrupt the header,
        # and no real terminal identifier has either, so it is dropped rather
        # than escaped.
        case "$value" in
            "" | *";"* | *"
"*) continue ;;
        esac
        identity="$identity$name=$value;"
    done
fi

curl --silent --show-error --max-time 2 \
    --request POST \
    --header 'Content-Type: application/json' \
    --header "X-MFT-Terminal: $identity" \
    --data-binary @- \
    "${1:-http://127.0.0.1:7654/event}" >/dev/null 2>&1

# Unconditionally: a refused connection, a missing curl, a daemon mid-restart
# are all fine states for a visualizer to be in, and none of them are worth a
# line of the user's terminal.
exit 0
