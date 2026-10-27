"""Contract tests for `scripts/xvalidate.py`, run entirely offline.

The tool's whole value is its verdict, so three properties are pinned here:

* it actually validates when handed bars (`--bars-from-file`) — no network, no
  cache directory, no Lighter API;
* it says so when the two engines agree, on a series whose expected signal
  count is known independently (the same series `test_strategy_port` pins);
* it fails loudly when they disagree. A cross-validation tool that can only
  print "一致" would pass every test written against it, so the divergence path
  is exercised by feeding the live side a doctored result list.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from scripts import xvalidate
from tests.test_strategy_port import plateau_breakout_bars

# The synthetic series starts in 2020; `--since` defaults to Lighter's first 4h
# bar (2025-01-17), so an older series has to say so explicitly.
SINCE = "2020-01-01"


def _candle_rows(bars):
    """Raw Lighter-shaped rows: {t, o, h, l, c, v} with `t` in ms."""
    return [{"t": b.open_ms, "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
            for b in bars]


def _write_bars(payload):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(payload, handle)
    return handle.name


def _run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = xvalidate.main(argv)
    return code, buf.getvalue()


class OfflineValidationTest(unittest.TestCase):
    def setUp(self):
        self.bars = plateau_breakout_bars()
        self.path = _write_bars({"SYN": _candle_rows(self.bars)})
        self.addCleanup(os.unlink, self.path)

    def test_identical_engines_are_reported_as_identical(self):
        code, out = _run(["--symbols", "SYN", "--bars-from-file", self.path, "--since", SINCE])
        self.assertEqual(code, 0, out)
        self.assertIn("信号集合逐笔完全一致", out)
        self.assertIn("共同信号 2 笔", out)           # one long at 420, one short at 740
        self.assertIn("仅实盘有: 0  仅回测有: 0", out)
        self.assertIn("总体: ✅", out)

    def test_divergence_exits_nonzero_and_names_the_bar(self):
        # Live side loses every signal while the reference engine keeps its two.
        with mock.patch.object(xvalidate, "scan", return_value=[]):
            code, out = _run(["--symbols", "SYN", "--bars-from-file", self.path, "--since", SINCE])
        self.assertEqual(code, 1, out)
        self.assertIn("❌ 存在差异", out)
        self.assertIn("仅回测有: 2", out)
        self.assertIn("BT-ONLY", out)

    def test_too_little_history_is_reported_not_guessed(self):
        path = _write_bars({"SYN": _candle_rows(self.bars[:200])})
        self.addCleanup(os.unlink, path)
        code, out = _run(["--symbols", "SYN", "--bars-from-file", path, "--since", SINCE])
        self.assertEqual(code, 1, out)               # attempted, and failed: not a pass
        self.assertIn("不足", out)
        self.assertIn("❌", out)

    def test_unclosed_last_bar_is_dropped_by_default(self):
        rows = _candle_rows(self.bars)
        with mock.patch.object(xvalidate.time, "time", return_value=self.bars[-1].open_ms / 1000 + 60):
            kept = xvalidate.drop_unclosed(xvalidate._bars_from_lighter_rows(rows))
        self.assertEqual(kept[-1].open_ms, self.bars[-2].open_ms)

    def test_rows_are_deduped_and_sorted(self):
        rows = _candle_rows(self.bars)
        shuffled = list(reversed(rows)) + rows[:3]        # reverse order + duplicates
        bars = xvalidate._bars_from_lighter_rows(shuffled)
        self.assertEqual(len(bars), len(self.bars))
        self.assertEqual([b.open_ms for b in bars], sorted(b.open_ms for b in bars))


if __name__ == "__main__":
    unittest.main()
