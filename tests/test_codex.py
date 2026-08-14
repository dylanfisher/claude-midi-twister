from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import install_agent_hooks
from mft import codex, config, discover, upkeep, usage
from mft.daemon import Visualizer
from mft.state import SessionTable
from mft.twister import NullTwister


class ProviderStateTest(unittest.TestCase):
    def test_same_id_different_providers_do_not_collide(self):
        table = SessionTable()
        claude = table.ensure("same", "/a", {"tty": "/dev/a"})
        codex_session = table.ensure(
            "same", "/b", {"tty": "/dev/b"}, provider="codex"
        )
        self.assertIsNot(claude, codex_session)
        self.assertIs(table.get("same"), claude)
        self.assertIs(table.get("same", "codex"), codex_session)

    def test_terminal_handoff_is_provider_aware(self):
        table = SessionTable()
        old = table.ensure("c", "/p", {"tty": "/dev/t"})
        old.terminal = {"tty": "/dev/t"}
        old.context_tokens = 900
        old.context_limit = 1000
        old.transcript_path = "/claude.jsonl"
        old.turn_count = 12
        old.failure_heat = 2.0
        old.permission_mode = "bypassPermissions"
        new = table.ensure("x", "/p", {"tty": "/dev/t"}, provider="codex")
        self.assertIs(old, new)
        self.assertEqual((new.provider, new.session_id), ("codex", "x"))
        self.assertEqual(new.slot, 0)
        self.assertEqual(new.terminal["tty"], "/dev/t")
        self.assertEqual(new.context_tokens, 0)
        self.assertEqual(new.context_limit, 0)
        self.assertEqual(new.transcript_path, "")
        self.assertEqual(new.turn_count, 0)
        self.assertEqual(new.failure_heat, 0.0)
        self.assertEqual(new.permission_mode, "")
        self.assertEqual(len(table.all()), 1)

    def test_codex_lifecycle_and_permission_are_observational(self):
        vis = Visualizer(NullTwister())
        base = {"provider": "codex", "session_id": "x", "cwd": "/p"}
        vis.handle_event({**base, "hook_event_name": "SessionStart", "approval_policy": "dontAsk"})
        session = vis.table.get("x", "codex")
        self.assertTrue(session.unsupervised)
        vis.handle_event({**base, "hook_event_name": "UserPromptSubmit"})
        self.assertEqual(session.state, "thinking")
        vis.handle_event({**base, "hook_event_name": "PreToolUse", "tool_name": "shell"})
        self.assertEqual(session.state, "working")
        result = vis.handle_event({**base, "hook_event_name": "PermissionRequest"})
        self.assertEqual(result["state"], "permission")
        self.assertNotIn("decision", result)
        self.assertNotIn("permissionDecision", result)

    def test_first_hook_hands_a_provisional_process_slot_to_the_real_session(self):
        vis = Visualizer(NullTwister())
        provisional = vis.table.ensure(
            "startup-pid-41",
            "/p",
            {"pid": "41", "tty": "/dev/ttys001"},
            provider="codex",
        )
        result = vis.handle_event({
            "provider": "codex",
            "session_id": "real-session",
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/p",
            "terminal": {"pid": "41", "tty": "/dev/ttys001"},
        })

        self.assertEqual(result["slot"], provisional.slot + 1)
        self.assertEqual(len(vis.table.all()), 1)
        self.assertIsNone(vis.table.get("startup-pid-41", "codex"))
        self.assertIs(vis.table.get("real-session", "codex"), provisional)


class ForwarderTest(unittest.TestCase):
    def test_codex_stdout_is_json_and_daemon_failure_is_success(self):
        script = Path(__file__).parents[1] / "hooks" / "forward.py"
        done = subprocess.run(
            [sys.executable, str(script), "--provider", "codex", "--event", "SessionStart", "--url", "http://127.0.0.1:1/event"],
            input='{"session_id":"x"}', text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(json.loads(done.stdout), {})


class InstallerTest(unittest.TestCase):
    def test_codex_hooks_are_notify_only_and_preserve_unrelated_entries(self):
        hooks = install_agent_hooks.build_codex_hooks("http://x")
        self.assertEqual(set(hooks), set(install_agent_hooks.CODEX_EVENTS))
        permission = hooks["PermissionRequest"][0]
        self.assertNotIn("matcher", permission)
        self.assertNotIn("async", permission["hooks"][0])
        self.assertNotIn("async", hooks["SessionEnd"][0]["hooks"][0])
        self.assertEqual(hooks["SessionEnd"][0]["hooks"][0]["timeout"], 3)
        existing = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "mine"}]}]}}
        merged = install_agent_hooks.merge_hooks(existing, hooks)
        self.assertEqual(merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "mine")
        stripped = install_agent_hooks.strip_hooks(merged)
        self.assertEqual(stripped, existing)

    def test_combined_install_does_not_rewrite_existing_claude_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_path = root / "claude" / "settings.json"
            codex_path = root / "codex" / "hooks.json"
            claude_path.parent.mkdir()
            original = {
                "env": {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"},
                "hooks": {
                    "SessionStart": [{
                        "hooks": [{
                            "type": "command",
                            "command": f"python {install_agent_hooks.claude.REGISTER}",
                        }]
                    }],
                    "Stop": [{
                        "hooks": [{"type": "command", "command": "mine"}]
                    }],
                },
            }
            original_text = json.dumps(original, separators=(",", ":")) + "\n"
            claude_path.write_text(original_text)

            result = install_agent_hooks.main([
                "--claude-settings", str(claude_path),
                "--codex-hooks", str(codex_path),
                "--codex-config", str(root / "codex" / "config.toml"),
            ])

            self.assertEqual(result, 0)
            self.assertEqual(claude_path.read_text(), original_text)
            self.assertFalse(claude_path.with_suffix(".json.bak").exists())
            self.assertTrue(install_agent_hooks._installed(codex_path))

    def test_explicit_claude_install_can_replace_existing_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "hooks": {
                    "SessionStart": [{
                        "hooks": [{
                            "type": "command",
                            "command": f"python {install_agent_hooks.claude.REGISTER}",
                        }]
                    }]
                }
            }))

            result = install_agent_hooks.main([
                "--provider", "claude", "--claude-settings", str(path)
            ])

            self.assertEqual(result, 0)
            hooks = json.loads(path.read_text())["hooks"]
            register = hooks["SessionStart"][0]["hooks"][0]["command"]
            notify = hooks["PreToolUse"][0]["hooks"][0]["command"]
            self.assertIn("register_session.py", register)
            self.assertIn("notify.sh", notify)
            self.assertNotIn("forward.py", notify)
            self.assertTrue(path.with_suffix(".json.bak").exists())


class ContextAdapterTest(unittest.TestCase):
    def write(self, lines):
        temp = tempfile.NamedTemporaryFile(mode="w", delete=False)
        for line in lines:
            temp.write(json.dumps(line) + "\n")
        temp.close()
        self.addCleanup(Path(temp.name).unlink)
        return temp.name

    def test_recognized_rollout_shape(self):
        path = self.write([
            {"type": "session_meta", "payload": {"cli_version": "0.147.0"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"total_tokens": 1200}, "model_context_window": 4000
            }}},
        ])
        self.assertEqual(codex.read_context(path), (1200, 4000))

    def test_unknown_rollout_shape_fails_closed(self):
        path = self.write([{"type": "token_count", "tokens": 1200, "limit": 4000}])
        self.assertIsNone(codex.read_context(path))

    def test_metadata_at_the_head_survives_a_large_rollout(self):
        path = self.write([
            {"type": "session_meta", "payload": {"cli_version": "0.147.0"}},
            {"type": "padding", "payload": "x" * (config.CONTEXT_TAIL_BYTES + 100)},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"total_tokens": 137020},
                "model_context_window": 258400,
            }}},
        ])
        self.assertEqual(codex.read_context(path), (137020, 258400))


class DiscoveryTest(unittest.TestCase):
    def test_cheap_process_ids_exclude_codex_helpers(self):
        rows = [
            (1, "/dev/a", "/opt/bin/codex"),
            (2, "", "/opt/bin/codex app-server --stdio"),
            (3, "", "/opt/bin/codex exec hello"),
            (4, "/dev/b", "/bin/zsh -lc codex"),
        ]
        self.assertEqual(discover.codex_process_ids(rows), frozenset({1}))

    def test_ambiguous_cwd_assigns_no_terminal(self):
        procs = [discover.Proc(1, "/dev/a", "/p"), discover.Proc(2, "/dev/b", "/p")]
        threads = [codex.Thread("one", "one", "/p", updated_at=100)]
        self.assertEqual(discover.discover_codex(procs, threads, now=100), [])

    def test_unique_join_carries_title_and_rollout(self):
        procs = [discover.Proc(1, "/dev/a", "/p")]
        threads = [codex.Thread("one", "one", "/p", "/r.jsonl", "A title", 100)]
        found = discover.discover_codex(procs, threads, now=100)
        self.assertEqual(found[0].provider, "codex")
        self.assertEqual(found[0].title, "A title")
        self.assertEqual(found[0].terminal["tty"], "/dev/a")

    def test_launch_window_ignores_older_threads_in_the_same_directory(self):
        procs = [discover.Proc(1, "/dev/a", "/p")]
        threads = [
            codex.Thread("old", "old", "/p", updated_at=50),
            codex.Thread("new", "new", "/p", updated_at=100),
        ]
        found = discover.discover_codex(procs, threads, now=100, window=15)
        self.assertEqual([entry.session_id for entry in found], ["new"])

    def test_promptless_process_becomes_a_provisional_session(self):
        proc = discover.Proc(41, "/dev/a", "/p")
        found = discover.discover_codex_starts({41}, [proc])
        self.assertEqual(found[0].session_id, "startup-pid-41")
        self.assertEqual(found[0].provider, "codex")
        self.assertEqual(found[0].terminal["pid"], "41")


class StartupFallbackTest(unittest.TestCase):
    def entry(self) -> discover.Discovered:
        return discover.Discovered(
            session_id="codex-new",
            cwd="/p",
            transcript_path="/rollout.jsonl",
            terminal={"pid": "41", "tty": "/dev/ttys001"},
            provider="codex",
        )

    def test_only_a_new_interactive_pid_starts_discovery(self):
        with mock.patch("mft.upkeep.discover.codex_process_ids", return_value=frozenset({1})):
            keeper = upkeep.Upkeep(
                SessionTable(), released=lambda sessions: None, wake=lambda: None
            )
        with mock.patch.object(keeper, "sweep_codex") as sweep:
            with mock.patch(
                "mft.upkeep.discover.codex_process_ids", return_value=frozenset({1})
            ):
                keeper._codex_watch_stop = mock.Mock()
                keeper._codex_watch_stop.wait.side_effect = [False, True]
                keeper._watch_codex()
            sweep.assert_not_called()
            with mock.patch(
                "mft.upkeep.discover.codex_process_ids", return_value=frozenset({1, 2})
            ):
                keeper._codex_watch_stop = mock.Mock()
                keeper._codex_watch_stop.wait.side_effect = [False, True]
                keeper._watch_codex()
        sweep.assert_called_once_with(frozenset({2}))

    def test_worker_adopts_and_reports_a_new_codex_session(self):
        table = SessionTable()
        reports = []
        keeper = upkeep.Upkeep(
            table,
            released=lambda sessions: None,
            wake=lambda: None,
            adopted=reports.append,
        )
        with mock.patch(
            "mft.upkeep.discover.discover_codex_starts", return_value=[self.entry()]
        ):
            result = keeper._adopt_codex_once(frozenset({41}))

        session = table.get("codex-new", "codex")
        self.assertIsNotNone(session)
        self.assertEqual(session.terminal["tty"], "/dev/ttys001")
        self.assertEqual(result.new, [session])
        self.assertEqual(reports, [result])

    def test_worker_deduplicates_a_hook_discovered_session(self):
        table = SessionTable()
        existing = table.ensure(
            "codex-new",
            "/p",
            {"pid": "41", "tty": "/dev/ttys001"},
            provider="codex",
        )
        existing.terminal = {"pid": "41", "tty": "/dev/ttys001"}
        reports = []
        keeper = upkeep.Upkeep(
            table,
            released=lambda sessions: None,
            wake=lambda: None,
            adopted=reports.append,
        )
        with mock.patch("mft.upkeep.discover.discover_codex_starts") as starts:
            result = keeper._adopt_codex_once(frozenset({41}))

        self.assertEqual(table.all(), [existing])
        self.assertEqual(result.new, [])
        self.assertEqual(reports, [])
        starts.assert_not_called()


class UsageParityTest(unittest.TestCase):
    def test_worst_provider_and_independent_watermarks(self):
        watcher = usage.UsageWatcher("/missing")
        with mock.patch("mft.usage.read", return_value=usage.Reading(40, "c1")), mock.patch("mft.codex.rate_limit", return_value=(70, "x1")):
            self.assertIsNone(watcher.poll(100))
            watcher._codex_thread.join(timeout=1)
            self.assertIsNone(watcher.poll(100))
        with mock.patch("mft.usage.read", return_value=usage.Reading(51, "c1")), mock.patch("mft.codex.rate_limit", return_value=(76, "x1")):
            self.assertIsNone(watcher.poll(100 + 10_000))
            watcher._codex_thread.join(timeout=1)
            announcement = watcher.poll(100 + 10_000)
        self.assertEqual(announcement, usage.Announcement("codex", 75, 76))
        payload = watcher.payload()
        self.assertEqual(payload["provider"], "codex")
        self.assertEqual(set(payload["providers"]), {"claude", "codex"})

    def test_codex_poll_never_waits_on_the_render_thread(self):
        release = threading.Event()

        def blocked():
            release.wait(timeout=1)
            return (20, "x")

        watcher = usage.UsageWatcher("/missing")
        with mock.patch("mft.usage.read", return_value=None), mock.patch(
            "mft.codex.rate_limit", side_effect=blocked
        ):
            started = time.monotonic()
            self.assertIsNone(watcher.poll(100))
            self.assertLess(time.monotonic() - started, 0.05)
            release.set()
            watcher._codex_thread.join(timeout=1)

    def test_claude_only_poll_starts_no_codex_process(self):
        watcher = usage.UsageWatcher("/missing")
        watcher.observe(usage.Reading(40, "c1"))
        with mock.patch(
            "mft.usage.read", return_value=usage.Reading(51, "c1")
        ), mock.patch("mft.codex.rate_limit") as rate_limit:
            announcement = watcher.poll(100, include_codex=False)

        self.assertEqual(announcement, usage.Announcement("claude", 50, 51))
        rate_limit.assert_not_called()
        self.assertIsNone(watcher._codex_thread)

    def test_current_reading_uses_cached_codex_without_an_app_server_call(self):
        watcher = usage.UsageWatcher("/missing")
        watcher._codex.observe(usage.Reading(64, "x"))
        with mock.patch("mft.usage.read", return_value=None), mock.patch(
            "mft.codex.rate_limit"
        ) as rate_limit:
            self.assertEqual(watcher.current_reading(), ("codex", 64, False))
        rate_limit.assert_not_called()

    def test_daemon_announces_the_crossing_providers_exact_reading(self):
        vis = Visualizer(NullTwister())
        vis.usage.poll = mock.Mock(
            return_value=usage.Announcement("codex", 75, 89)
        )
        vis.usage.provider_readings = mock.Mock(
            return_value={"claude": {"percent": 40}, "codex": {"percent": 89}}
        )

        vis._check_usage(100)

        overlay = vis._overlays[-1]
        self.assertEqual(overlay.percent, 89)
        self.assertEqual(overlay.word.text, "CDX")


if __name__ == "__main__":
    unittest.main()
