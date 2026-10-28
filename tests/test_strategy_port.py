"""Port-consistency tests for `app/strategy.py` (the frozen signal engine).

`app/strategy.py` is a byte-for-byte copy of the signal core of the previous
system (`crypto-ma-alert`, live on Hyperliquid): the code that produced 700
backtested trades and every live trade so far. It must not be edited — a
changed constant or a reordered comparison silently redefines the strategy and
voids every backtest conclusion. So the tests here cannot "pin the code" by
importing it and comparing it to itself; they pin it from three sides:

1. **Byte identity.** The file's SHA-256 and size are asserted against the
   values recorded when it was copied. Any edit at all — a docstring, a
   reformat, an "optimisation" — fails here first, with the exact diff left to
   `git diff` rather than to a behaviour change nobody notices.

2. **Parity with an independent implementation.** `tests/reference_strategy.py`
   is a *second*, hand-written implementation of the same rules. Both are run
   over several synthetic series (designed plateaus, seeded random walks) and
   the resulting signals must match pairwise, bar index and price included.
   This is what catches a semantic drift that keeps the file looking plausible
   (e.g. "squeeze looks back 240 bars instead of 250").

3. **A frozen numerical snapshot.** Two concrete signals — bar index, open
   time, direction, entry and stop — are written out as literals. This catches
   the one failure the parity check cannot: someone editing *both*
   implementations in the same way. The numbers were taken from the original
   file at `/…/crypto-ma-alert/app/strategy.py` and cross-checked against the
   independent implementation before being frozen here.

Test data is generated in-process: no network, no fixture files, no temporary
directories. Signals are asserted against the strategy's own public API
(`scan`, `compute_indicators`, `squeeze_flags`, `evaluate_entry`), which is
what the rest of the bot will call.
"""

from __future__ import annotations

import ast
import hashlib
import math
import pathlib
import sys
import unittest
from typing import List, Sequence

import app.strategy as strategy
from tests import reference_strategy as reference

ROOT = pathlib.Path(__file__).resolve().parent.parent
STRATEGY_PATH = ROOT / "app" / "strategy.py"

# Recorded when the file was copied out of `crypto-ma-alert`:
#   sha256sum app/strategy.py  ->  8682667…
# Both are pinned: a 1-byte change that happens to keep the length (rare) is
# still caught by the hash; a hash collision is not a threat model here.
FROZEN_SHA256 = "868266752317212c24a8dcdd4937c775944d24035846ffba86805f34b22ece07"
FROZEN_BYTES = 25962

BAR_MS = 4 * 3600 * 1000
T0 = 1_600_000_000_000        # 2020-09-13T12:26:40Z, an arbitrary 4h-grid origin

# ---------------------------------------------------------------------------
# Synthetic series
# ---------------------------------------------------------------------------


def _bar(index: int, open_: float, close: float, spread: float = 0.0006) -> strategy.Bar:
    """Build a bar on the 4h grid whose high/low bracket open and close."""
    return strategy.Bar(
        open_ms=T0 + index * BAR_MS,
        open=round(open_, 4),
        high=round(max(open_, close) * (1 + spread), 4),
        low=round(min(open_, close) * (1 - spread), 4),
        close=round(close, 4),
    )


def plateau_breakout_bars() -> List[strategy.Bar]:
    """Three plateaus: quiet drift -> gap up (long) -> gap down (short).

    Built to exercise the real mechanism rather than a lucky random path:

    * 420 bars around 100 with the oscillation *contracting*, so `band` keeps
      making new lows and the 250-bar 20th percentile is met for a long run
      (a squeeze cannot exist before bar 369 — 250 window + 120 warmup).
    * a 4.2% gap up at bar 420: close is above the previous bar's six-mean
      max while the means are still at the old level -> long, stop distance
      ~4% (inside [1.5%, 10%]).
    * 320 bars around 104.2 so the squeeze can re-form, then a 8% gap down at
      bar 740 with close below SMA200 -> short.

    Two signals, one per side, at known indices — enough to pin the entry and
    stop arithmetic without depending on a random path.
    """
    bars: List[strategy.Bar] = []
    for i in range(420):
        amp = 0.6 * math.exp(-i / 110.0)          # 0.6% -> 0.02% oscillation
        close = 100.0 * (1 + amp / 100.0 * math.sin(i / 5.0))
        prev_amp = 0.6 * math.exp(-(i - 1) / 110.0) if i else amp
        open_ = 100.0 * (1 + prev_amp / 100.0 * math.sin((i - 1) / 5.0)) if i else close
        bars.append(_bar(len(bars), open_, close))

    for leg_level, leg_len in ((104.2, 320), (95.8, 60)):
        for k in range(leg_len):
            close = leg_level * (1 + 0.0002 * math.sin(k / 4.0))
            open_ = bars[-1].close if k == 0 else leg_level * (1 + 0.0002 * math.sin((k - 1) / 4.0))
            bars.append(_bar(len(bars), open_, close))
    return bars


def walk_bars(seed: int, n: int = 700) -> List[strategy.Bar]:
    """Seeded random walk with a volatility regime break (long signals only).

    Uses `random.Random.random()` (Mersenne Twister, integer arithmetic) rather
    than `gauss()` so the series is bit-identical on every platform/Python
    version — a snapshot taken locally must hold in CI.
    """
    import random

    rnd = random.Random(seed)

    def noise() -> float:
        return (rnd.random() - 0.5) * 2.0

    bars: List[strategy.Bar] = []
    px = 100.0
    for i in range(n):
        if i < 300:
            vol, drift = 0.0012, 0.0
        elif i < 480:
            vol, drift = 0.0060, 0.0025
        elif i < 600:
            vol, drift = 0.0030, -0.0025
        else:
            vol, drift = 0.0030, 0.0008
        open_ = px * (1 + noise() * vol / 2)
        px = px * (1 + drift + noise() * vol)
        bars.append(_bar(i, open_, px, spread=abs(noise()) * vol / 2))
    return bars


def _signal_tuples(signals: Sequence[strategy.Signal]):
    """(bar_index, bar_open_ms, side, entry, stop, stop_dist) — the frozen shape."""
    return [(s.bar_index, s.bar_open_ms, s.side, s.entry_price, s.stop_price, s.stop_dist)
            for s in signals]


# ---------------------------------------------------------------------------
# 1. Byte identity
# ---------------------------------------------------------------------------


class FrozenSourceTest(unittest.TestCase):
    def test_sha256_matches_the_copied_file(self):
        digest = hashlib.sha256(STRATEGY_PATH.read_bytes()).hexdigest()
        self.assertEqual(
            digest, FROZEN_SHA256,
            "app/strategy.py changed. It is a byte-for-byte copy of the "
            "validated crypto-ma-alert signal engine; edit the port, never the engine.",
        )

    def test_size_matches_the_copied_file(self):
        self.assertEqual(STRATEGY_PATH.stat().st_size, FROZEN_BYTES)

    def test_module_imports_only_the_standard_library(self):
        # The engine must stay dependency-free: it is imported by the backtest,
        # the dry-run replay and the live tick, and a stray third-party import
        # would make one of those three see a different code path.
        tree = ast.parse(STRATEGY_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:        # relative import inside app/
                    continue
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= set(sys.stdlib_module_names),
                        f"non-stdlib imports: {sorted(imported - set(sys.stdlib_module_names))}")


# ---------------------------------------------------------------------------
# 2. Indicator edge cases (the parts a reimplementation gets subtly wrong)
# ---------------------------------------------------------------------------


class IndicatorTest(unittest.TestCase):
    def test_sma_is_none_before_the_window_fills(self):
        out = strategy.sma_series([1.0, 2.0, 3.0, 4.0], 2)
        self.assertEqual(out, [None, 1.5, 2.5, 3.5])

    def test_sma_of_full_window_is_plain_mean(self):
        self.assertEqual(strategy.sma_series([2.0, 4.0, 6.0], 3), [None, None, 4.0])

    def test_ema_is_seeded_with_the_first_value(self):
        # ewm(span=3, adjust=False) with alpha = 0.5, seeded at the first close.
        self.assertEqual(strategy.ema_series([1.0, 2.0, 3.0, 4.0], 3),
                         [1.0, 1.5, 2.25, 3.125])

    def test_ema_of_empty_series(self):
        self.assertEqual(strategy.ema_series([], 20), [])

    def test_quantile_interpolates_like_pandas(self):
        values = [float(v) for v in range(10)]
        self.assertAlmostEqual(strategy.quantile(values, 0.2), 1.8, places=12)
        self.assertAlmostEqual(strategy.quantile(values, 0.5), 4.5, places=12)
        self.assertEqual(strategy.quantile(values, 0.0), 0.0)
        self.assertEqual(strategy.quantile(values, 1.0), 9.0)

    def test_quantile_degenerate_inputs(self):
        self.assertIsNone(strategy.quantile([], 0.2))
        self.assertEqual(strategy.quantile([7.5], 0.2), 7.5)

    def test_quantile_does_not_reorder_the_caller_list(self):
        values = [3.0, 1.0, 2.0]
        strategy.quantile(values, 0.2)
        self.assertEqual(values, [3.0, 1.0, 2.0])

    def test_band_is_none_until_sma120_exists(self):
        bars = plateau_breakout_bars()[:200]
        ind = strategy.compute_indicators(bars)
        self.assertIsNone(ind["band"][strategy.WARMUP_BARS - 2])    # 118: SMA120 missing
        self.assertIsNotNone(ind["band"][strategy.WARMUP_BARS - 1])  # 119 = the 120th bar
        self.assertIsNone(ind["ma_max"][0])

    def test_band_equals_spread_of_the_six_means(self):
        bars = plateau_breakout_bars()[:200]
        ind = strategy.compute_indicators(bars)
        i = 150
        means = [ind[key][i] for key in strategy.MA_KEYS]
        self.assertEqual(len(means), 6)
        self.assertAlmostEqual(ind["ma_max"][i], max(means), places=12)
        self.assertAlmostEqual(ind["ma_min"][i], min(means), places=12)
        self.assertAlmostEqual(ind["band"][i], (max(means) - min(means)) / bars[i].close, places=12)


class SqueezeFlagTest(unittest.TestCase):
    def test_first_true_bar_needs_window_plus_run(self):
        # 250-bar quantile window, 6 consecutive bars -> the 6th bar of the run
        # (index 249 + 5 = 254) is the first possible `True`.
        flags = strategy.squeeze_flags([0.001] * 260)
        self.assertEqual(flags.index(True), strategy.BAND_WINDOW - 1 + strategy.MIN_SQUEEZE_BARS - 1)
        self.assertEqual(sum(flags), 6)     # indices 254..259 only

    def test_run_is_broken_by_a_single_wide_bar(self):
        band = [0.001] * 260 + [0.5] * 3 + [0.001] * 10
        flags = strategy.squeeze_flags(band)
        # Three wide bars (260..262) reset the counter, so the run restarts at
        # 263 and only the 6th bar (268) onwards is a squeeze.
        self.assertEqual([int(f) for f in flags[260:]], [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1])

    def test_leading_none_band_delays_the_first_squeeze(self):
        # band is None for the first 119 bars in real use; those bars can never
        # contribute to a 250-value window, so the first True moves to 119+254.
        flags = strategy.squeeze_flags([None] * 119 + [0.001] * 260)
        self.assertEqual(flags.index(True), 119 + strategy.BAND_WINDOW - 1 + strategy.MIN_SQUEEZE_BARS - 1)

    def test_dense_run_counts_back_from_the_given_bar(self):
        band = [0.001] * 260 + [0.5] * 5 + [0.001] * 10
        raw = strategy.squeeze_flags(band, min_bars=1)
        self.assertEqual(strategy.dense_run(band, 268, raw=raw), 4)   # 265..268
        self.assertEqual(strategy.dense_run(band, 262, raw=raw), 0)   # inside the wide run


# ---------------------------------------------------------------------------
# 3. Parity with the independent implementation
# ---------------------------------------------------------------------------


class ReferenceParityTest(unittest.TestCase):
    """`app.strategy` vs `tests.reference_strategy` on the same bars."""

    def assert_same_signals(self, bars, sides, label=""):
        live = _signal_tuples(strategy.scan(bars, symbol="SYN", sides=sides))
        ref = reference.find_signals(bars, symbol="SYN", sides=sides)
        self.assertEqual(len(live), len(ref), f"{label}: signal count differs")
        for got, expected in zip(live, ref):
            self.assertEqual(got[0], expected[0], f"{label}: bar_index")
            self.assertEqual(got[1], expected[1], f"{label}: bar_open_ms")
            self.assertEqual(got[2], expected[2], f"{label}: side")
            self.assertAlmostEqual(got[3], expected[3], places=9, msg=f"{label}: entry")
            self.assertAlmostEqual(got[4], expected[4], places=9, msg=f"{label}: stop")
            self.assertAlmostEqual(got[5], expected[5], places=12, msg=f"{label}: stop_dist")
        return live

    def test_designed_series_two_sides(self):
        live = self.assert_same_signals(plateau_breakout_bars(), (1, -1), "plateau")
        self.assertEqual([s[2] for s in live], [1, -1])

    def test_seeded_walks(self):
        for seed in (7, 11, 23):
            with self.subTest(seed=seed):
                live = self.assert_same_signals(walk_bars(seed), (1, -1), f"walk{seed}")
                self.assertGreater(len(live), 0)

    def test_long_only_and_short_only_agree(self):
        bars = plateau_breakout_bars()
        self.assertEqual(len(self.assert_same_signals(bars, (1,), "long-only")), 1)
        self.assertEqual(len(self.assert_same_signals(bars, (-1,), "short-only")), 1)

    def test_short_regime_filter_switch_agrees(self):
        # short_regime_ma=0 disables the SMA200 filter in both implementations.
        bars = plateau_breakout_bars()
        live = [t for t in _signal_tuples(strategy.scan(bars, symbol="SYN", sides=(1, -1),
                                                       short_regime_ma=0))]
        ref = reference.find_signals(bars, sides=(1, -1), short_regime_ma=0)
        self.assertEqual(len(live), len(ref))
        self.assertGreaterEqual(len(live), 2)

    def test_short_is_blocked_while_price_is_above_sma200(self) -> None:
        """做空趋势过滤（用户 2026-10-28 明确要核对的那条）。

        判据在 `evaluate_entry`：`close < ma_min[i-1]` 破位后，还必须
        `close < SMA200[i]`；SMA200 缺失（预热不足）同样拒绝（fail-closed）。
        这里用最小可读的数据直接钉住三种情形：过滤成立 / 收盘在 MA200 上方被挡 /
        MA200 算不出来被挡。止损距离用 2.5% 确保不会因为 [1.5%, 10%] 而误判。
        """
        from app.strategy import Bar, evaluate_entry, REGIME_MA

        self.assertEqual(REGIME_MA, 200)
        bars = [Bar(open_ms=i * BAR_MS, open=100.0, high=101.0, low=99.0,
                    close=100.0, volume=1.0) for i in range(3)]
        bars[2] = Bar(open_ms=2 * BAR_MS, open=100.0, high=101.0, low=96.0,
                      close=97.0, volume=1.0)          # 收盘 97 < 下轨 99 → 破位
        base = {"ma_max": [None, 101.0, 102.5], "ma_min": [None, 99.0, 97.0],
                "band": [None, 0.02, 0.03], "sma200": [None, 90.0, 90.0]}

        def run(sma200: float = 90.0, ma_min_prev: float = 99.0):
            ind = {k: list(v) for k, v in base.items()}
            ind["sma200"][2] = sma200
            ind["ma_min"][1] = ma_min_prev
            return evaluate_entry("SYN", bars, ind, [True, True, True], 2,
                                  entry_price=97.0)

        allowed = run()                       # 收盘 97 < SMA200 90? 否 → 应被挡
        self.assertIsNone(allowed, "收盘 97 高于 SMA200 90，做空必须被挡")
        below = run(sma200=120.0)             # 收盘 97 < 120 → 过滤成立
        self.assertIsNotNone(below, "收盘 97 低于 SMA200 120，应给出做空信号")
        self.assertEqual((below.side, round(below.stop_dist, 4)), (-1, 0.0567))
        missing = run(sma200=None)            # 预热不足 → fail-closed
        self.assertIsNone(missing, "SMA200 未算出时不得做空")

    def test_bars_too_short_produce_no_signals(self) -> None:
        short = plateau_breakout_bars()[:strategy.MIN_BARS_REQUIRED - 1]
        self.assertEqual(strategy.scan(short, symbol="SYN", sides=(1, -1)), [])
        self.assertEqual(reference.find_signals(short, sides=(1, -1)), [])


# ---------------------------------------------------------------------------
# 4. Frozen snapshot taken from the original file
# ---------------------------------------------------------------------------


class FrozenSnapshotTest(unittest.TestCase):
    # (bar_index, bar_open_ms, side, entry_price, stop_price) — verified against
    # crypto-ma-alert/app/strategy.py AND tests/reference_strategy.py before
    # being written down. Do not regenerate these from the code under test.
    EXPECTED_PLATEAU = (
        (420, T0 + 420 * BAR_MS, 1, 104.2, 100.03379333333334),
        (740, T0 + 740 * BAR_MS, -1, 95.8, 104.13085333333333),
    )
    EXPECTED_PLATEAU_DIGEST = (
        "d524b786dc8d02c940861e4e93f793ad0c92d4b3db92316555916848473517b4"
    )

    # Seeded walk (seed=11): 33 consecutive long signals, bars 666..698. The
    # digest covers every signal's (index, open_ms, side, entry, stop) exactly,
    # so a change in any one of them fails without listing all 33 rows.
    EXPECTED_WALK_11_COUNT = 33
    EXPECTED_WALK_11_FIRST = (666, T0 + 666 * BAR_MS, 1, 121.3621, 117.89595166666666)
    EXPECTED_WALK_11_LAST = (698, T0 + 698 * BAR_MS, 1, 123.6346, 118.75354999999999)
    EXPECTED_WALK_11_DIGEST = (
        "0635d963aa64cc24ec7ba6c968afb9c8933347287e82da5643c472026b6055e9"
    )

    @staticmethod
    def _digest(signals) -> str:
        payload = "\n".join(
            f"{s.bar_index}|{s.bar_open_ms}|{s.side}|{s.entry_price!r}|{s.stop_price!r}"
            for s in signals
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def test_plateau_series_snapshot(self):
        signals = strategy.scan(plateau_breakout_bars(), symbol="SYN", sides=(1, -1))
        live = _signal_tuples(signals)
        self.assertEqual(len(live), len(self.EXPECTED_PLATEAU))
        for got, expected in zip(live, self.EXPECTED_PLATEAU):
            self.assertEqual(got[:3], expected[:3])
            self.assertAlmostEqual(got[3], expected[3], places=12)   # entry
            self.assertAlmostEqual(got[4], expected[4], places=10)   # stop
        self.assertEqual(self._digest(signals), self.EXPECTED_PLATEAU_DIGEST)

    def test_walk_11_snapshot(self):
        signals = strategy.scan(walk_bars(11), symbol="SYN", sides=(1, -1))
        self.assertEqual(len(signals), self.EXPECTED_WALK_11_COUNT)
        self.assertEqual([s.bar_index for s in signals],
                         list(range(666, 666 + self.EXPECTED_WALK_11_COUNT)))
        self.assertEqual(_signal_tuples(signals)[0][:5], self.EXPECTED_WALK_11_FIRST)
        self.assertEqual(_signal_tuples(signals)[-1][:5], self.EXPECTED_WALK_11_LAST)
        self.assertEqual(self._digest(signals), self.EXPECTED_WALK_11_DIGEST)

    def test_stop_and_entry_arithmetic_is_where_the_rules_say(self):
        # Independent of the table above: for the long at bar 420 the stop must
        # be the *current* bar's six-mean minimum and the entry the next bar's
        # open — the two facts that decide every later R multiple.
        bars = plateau_breakout_bars()
        ind = strategy.compute_indicators(bars)
        sig = [s for s in strategy.scan(bars, symbol="SYN", sides=(1, -1)) if s.side == 1][0]
        self.assertEqual(sig.entry_price, bars[sig.bar_index + 1].open)
        self.assertAlmostEqual(sig.stop_price, ind["ma_min"][sig.bar_index], places=12)
        self.assertAlmostEqual(sig.ma_max, ind["ma_max"][sig.bar_index - 1], places=12)
        self.assertAlmostEqual(sig.band, ind["band"][sig.bar_index - 1], places=12)
        self.assertGreater(bars[sig.bar_index].close, sig.ma_max)
        self.assertLess(sig.stop_price, sig.entry_price)
        self.assertAlmostEqual(sig.stop_dist, (sig.entry_price - sig.stop_price) / sig.entry_price, places=12)

    def test_short_requires_close_below_sma200(self):
        bars = plateau_breakout_bars()
        ind = strategy.compute_indicators(bars)
        shorts = [s for s in strategy.scan(bars, symbol="SYN", sides=(1, -1)) if s.side == -1]
        self.assertTrue(shorts)
        for sig in shorts:
            self.assertLess(bars[sig.bar_index].close, ind["sma200"][sig.bar_index])
            self.assertLess(bars[sig.bar_index].close, ind["ma_min"][sig.bar_index - 1])
            self.assertGreater(sig.stop_price, sig.entry_price)


class ScanContractTest(unittest.TestCase):
    def test_no_signal_on_the_last_bar(self):
        # The entry price is the *next* bar's open, so the last bar can never
        # produce a signal; `scan` must stop one bar short.
        bars = plateau_breakout_bars()
        for sig in strategy.scan(bars, symbol="SYN", sides=(1, -1)):
            self.assertLess(sig.bar_index, len(bars) - 1)

    def test_start_argument_skips_earlier_bars(self):
        bars = plateau_breakout_bars()
        # Only the short at bar 740 survives a start of 500; starting past it
        # leaves nothing to find.
        self.assertEqual([(s.bar_index, s.side)
                          for s in strategy.scan(bars, symbol="SYN", sides=(1, -1), start=500)],
                         [(740, -1)])
        self.assertEqual(strategy.scan(bars, symbol="SYN", sides=(1, -1), start=741), [])

    def test_exit_trigger_is_close_versus_sma20(self):
        bars = plateau_breakout_bars()
        ind = strategy.compute_indicators(bars)
        for i in range(150, len(bars)):
            self.assertEqual(strategy.exit_triggered(bars, ind, i, side=1),
                             bars[i].close < ind["sma20"][i])
            self.assertEqual(strategy.exit_triggered(bars, ind, i, side=-1),
                             bars[i].close > ind["sma20"][i])

    def test_explain_entry_never_contradicts_evaluate_entry(self):
        # `/why` is diagnostic only: its verdict is whatever evaluate_entry says,
        # and a divergence must surface as drift instead of a second answer.
        bars = plateau_breakout_bars()
        ind = strategy.compute_indicators(bars)
        sq = strategy.squeeze_flags(ind["band"])
        for i in range(369, 470):
            why = strategy.explain_entry("SYN", bars, ind, sq, i,
                                         entry_price=bars[i + 1].open,
                                         allow_long=True, allow_short=True)
            sig = strategy.evaluate_entry("SYN", bars, ind, sq, i,
                                          entry_price=bars[i + 1].open,
                                          allow_long=True, allow_short=True)
            self.assertFalse(why["drift"], f"drift at bar {i}: {why['reason']}")
            self.assertEqual(why["fired"], sig is not None)
            self.assertEqual(why["side"], int(sig.side) if sig is not None else 0)


if __name__ == "__main__":
    unittest.main()
