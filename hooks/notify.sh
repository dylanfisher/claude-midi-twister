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
# Usage: notify.sh [url]   -- event JSON arrives on stdin.

curl --silent --show-error --max-time 2 \
    --request POST \
    --header 'Content-Type: application/json' \
    --data-binary @- \
    "${1:-http://127.0.0.1:7654/event}" >/dev/null 2>&1

# Unconditionally: a refused connection, a missing curl, a daemon mid-restart
# are all fine states for a visualizer to be in, and none of them are worth a
# line of the user's terminal.
exit 0
