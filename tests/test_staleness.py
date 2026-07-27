"""Whether the daemon can tell that it is running code you have replaced.

The bug this exists for is not in any of these paths: it is that a module saved
half-finished imports cleanly and raises only when something calls it, and that
`mft.upkeep` catches exactly that. So what is pinned here is the comparison
itself -- a baseline, a file written after it, and a name in the answer.
"""

from __future__ import annotations

import os
import sys
import types
import unittest

from mft import staleness


class Sources(unittest.TestCase):
    def test_only_our_own_modules_are_watched(self):
        """site-packages is not edited mid-run and walking it would turn a
        cheap check into a few thousand stats."""
        found = staleness.sources()
        self.assertIn("mft.staleness", found)
        self.assertTrue(all(n == "mft" or n.startswith("mft.") for n in found))
        self.assertNotIn("json", found)
        self.assertNotIn("unittest", found)

    def test_a_module_with_no_file_is_skipped(self):
        """A namespace package or a C extension has nothing to stat, and
        `getattr(module, "__file__", "")` is how that arrives."""
        fake = types.ModuleType("mft.nowhere")
        sys.modules["mft.nowhere"] = fake
        try:
            self.assertNotIn("mft.nowhere", staleness.sources())
        finally:
            del sys.modules["mft.nowhere"]


class Changed(unittest.TestCase):
    def setUp(self):
        self.path = staleness.sources()["mft.staleness"]
        self.was = os.stat(self.path).st_mtime
        self.addCleanup(lambda: os.utime(self.path, (self.was, self.was)))

    def test_an_untouched_tree_is_not_stale(self):
        self.assertEqual(staleness.changed(staleness.snapshot()), [])

    def test_a_file_written_after_the_baseline_is_named(self):
        baseline = staleness.snapshot()
        os.utime(self.path, (self.was + 60, self.was + 60))
        self.assertEqual(staleness.changed(baseline), ["mft.staleness"])

    def test_a_file_touched_backwards_is_not_stale(self):
        """Strictly newer, not merely different: restoring an older copy is
        not the failure this warns about, and a clock that stepped back should
        not invent one."""
        baseline = staleness.snapshot()
        os.utime(self.path, (self.was - 60, self.was - 60))
        self.assertEqual(staleness.changed(baseline), [])

    def test_a_module_imported_since_the_baseline_is_not_news(self):
        """Whenever it was imported, it was imported from the file on disk
        now, so it cannot be stale against a baseline that predates it."""
        baseline = {k: v for k, v in staleness.snapshot().items() if k != "mft.staleness"}
        self.assertEqual(staleness.changed(baseline), [])

    def test_a_baseline_naming_a_vanished_module_is_ignored(self):
        baseline = dict(staleness.snapshot(), **{"mft.gone": 0.0})
        self.assertEqual(staleness.changed(baseline), [])


class Report(unittest.TestCase):
    def test_it_never_raises(self):
        """A diagnostic that can take the daemon down with it is worse than no
        diagnostic at all, so the wrapper swallows a bad baseline."""
        self.assertEqual(staleness.report({"mft.staleness": None}, "boot"), [])

    def test_a_clean_tree_says_nothing(self):
        with self.assertNoLogs("mft.staleness", "WARNING"):
            self.assertEqual(staleness.report(staleness.snapshot(), "boot"), [])

    def test_a_stale_tree_warns_once_and_names_the_file(self):
        path = staleness.sources()["mft.staleness"]
        was = os.stat(path).st_mtime
        baseline = staleness.snapshot()
        os.utime(path, (was + 60, was + 60))
        self.addCleanup(lambda: os.utime(path, (was, was)))
        with self.assertLogs("mft.staleness", "WARNING") as caught:
            self.assertEqual(staleness.report(baseline, "awake (lid)"), ["mft.staleness"])
        self.assertEqual(len(caught.records), 1)
        self.assertIn("mft.staleness", caught.output[0])
        self.assertIn("awake (lid)", caught.output[0])


if __name__ == "__main__":
    unittest.main()
