"""`scripts/backtest.py` 的离线测试（T11）。

这个引擎的产出会直接进 README / docs/BACKTEST.md 的数字表，所以测试的重点不是
"能跑"，而是**每个数字都能手算复现**：

* 手算样本：一段可以逐根写出 SMA20 的固定 bar 序列 → 精确的进场根、离场根、
  离场价、R 倍数、终值权益。断言里给出完整的算术推导（不是"跑出来是多少写多少"）。
* **同一根 bar 内同时触及止损与信号离场 → 按止损**（保守假设）。这条如果不成立，
  回测会系统性地高估收益，而且永远看不出来 —— 所以两份方向各测一次。
* 资金费的**符号与单位**：多头付钱 / 空头收钱、`rate` 是「每小时百分比」要 /100。
  符号错一次，1.77 年的结果会差出好几个百分点，且看起来仍然"合理"。
* 回撤**必须**是 `app.dd` 的口径：把同一串权益点喂给 `app.dd.bootstrap` /
  `app.dd.evaluate`，断言与引擎里的 `max_dd` / `twr_index` 逐位一致。
* 空信号序列 → 0 值、不崩。

全部离线：注入的手写信号 + 手写 bar，不打网络、不读文件、不依赖时钟。
"""

from __future__ import annotations

import pathlib
import unittest
from typing import List, Optional, Sequence
from unittest import mock

from app import dd
from app.strategy import Bar, Signal
from scripts import backtest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BAR_MS = 4 * 3600 * 1000
T0 = 1_700_000_000_000
RISK = 0.0133


# --------------------------------------------------------------------- 造数据


def _bars(rows: Sequence[Sequence[float]], start_ms: int = T0) -> List[Bar]:
    """`[(open, high, low, close), …]` → 4h 网格上的 bar 列表。"""
    return [
        Bar(open_ms=start_ms + i * BAR_MS, open=float(o), high=float(h),
            low=float(l), close=float(c))
        for i, (o, h, l, c) in enumerate(rows)
    ]


def _flat(n: int, price: float = 100.0, spread: float = 0.5) -> List[List[float]]:
    return [[price, price + spread, price - spread, price] for _ in range(n)]


def _signal(bar_index: int, side: int, entry: float, stop: float,
            symbol: str = "TST") -> Signal:
    dist = abs(entry - stop) / entry
    return Signal(symbol=symbol, bar_index=bar_index, bar_open_ms=T0 + bar_index * BAR_MS,
                  entry_price=entry, stop_price=stop, stop_dist=dist,
                  ma_max=entry, ma_min=stop, band=0.01, side=side)


def _run(rows: Sequence[Sequence[float]], signals: Sequence[Signal], *,
         risk_pct: float = RISK, funding=None, **kwargs):
    bars = _bars(rows)
    series = backtest.build_series("TST", bars, funding=funding, signals=list(signals))
    return backtest.simulate({"TST": series}, risk_pct=risk_pct, use_funding=funding is not None,
                             **kwargs)


# --------------------------------------------------------------------- 手算样本
#
# 序列（bar 0..19 全平 = close 100，于是 SMA20 = 100 一直到 bar 19）：
#
#   bar 20  开 100.0  收 98.8   low 98.5      ← 入场根（开 100，止损 96）
#   bar 21  收 101     bar 22 收 102  bar 23 收 103  bar 24 收 104
#   bar 25  收 99.0    low 98.6               ← 离场根（信号离场）
#
# 手算 SMA20：
#   bar 20： (19×100 + 98.8) / 20                = 99.94   → 收 98.8 < 99.94 ⇒ 信号成立，
#             但**这是入场当根，不判**（与 app.lifecycle.decide_exit 的 is_entry_bar 一致）
#   bar 21： (18×100 + 98.8 + 101) / 20          = 99.99   → 101 > 99.99 不舍离场
#   bar 22： (17×100 + 98.8 + 101 + 102) / 20    = 100.09  → 102 > ⇒ 不动
#   bar 23： (16×100 + 98.8 + 101 + 102 + 103)/20 = 100.24 → 103 > ⇒ 不动
#   bar 24： (15×100 + 98.8 + 101 + 102 + 103 + 104)/20 = 100.44 → 104 > ⇒ 不动
#   bar 25： (14×100 + 98.8 + 101 + 102 + 103 + 104 + 99)/20 = 100.39 → 99 < ⇒ **离场@99**
#
# R = (99 − 100) / (100 − 96) = −0.25
# 权益 = 10000 × (1 + 0.0133 × (−0.25)) = 9966.75
HAND_ROWS: List[List[float]] = (
    _flat(20)
    + [
        [100.0, 100.2, 98.5, 98.8],     # 20 入场根（收盘已在 SMA20 下方 → 但不判离场）
        [98.8, 101.2, 98.6, 101.0],     # 21
        [101.0, 102.2, 100.8, 102.0],   # 22
        [102.0, 103.2, 101.8, 103.0],   # 23
        [103.0, 104.2, 102.8, 104.0],   # 24
        [104.0, 104.2, 98.6, 99.0],     # 25 离场根（低 98.6 > 止损 96 → 只可能是信号离场）
    ]
)


class HandComputedTradeTest(unittest.TestCase):
    """一段固定 bar + 一个手写信号 → 逐项断言（数字全部手算，见上面的推导）。"""

    def setUp(self):
        self.result = _run(HAND_ROWS, [_signal(19, 1, entry=100.0, stop=96.0)])

    def test_one_trade_with_hand_computed_entry_and_exit_bar(self):
        self.assertEqual(len(self.result.trades), 1)
        trade = self.result.trades[0]
        self.assertEqual(trade.entry_bar, 20)          # 信号在 19，次根开盘入场
        self.assertEqual(trade.exit_bar, 25)           # SMA20 第一次被跌破
        self.assertEqual(trade.reason, backtest.EXIT_SIGNAL)
        self.assertEqual(trade.entry_price, 100.0)     # = bars[20].open
        self.assertEqual(trade.stop_price, 96.0)
        self.assertEqual(trade.exit_price, 99.0)       # 收盘价离场
        self.assertEqual(trade.bars_held, 5)

    def test_r_multiple_matches_hand_arithmetic(self):
        trade = self.result.trades[0]
        # (99 − 100) / (100 − 96) = −0.25
        self.assertAlmostEqual(trade.r_gross, -0.25, places=12)
        self.assertAlmostEqual(trade.r_net, -0.25, places=12)   # 无成本口径

    def test_entry_bar_never_exits_on_the_signal(self):
        """bar 20 收盘 98.8 < SMA20 99.94 —— 若在入场当根判离场，这里会变成 1 根 bar 的
        交易、R = −0.012。实际必须持有到 bar 25。"""
        trade = self.result.trades[0]
        self.assertGreater(trade.exit_bar, 20)
        self.assertAlmostEqual(trade.r_gross, -0.25, places=12)

    def test_equity_compounds_by_risk_times_r(self):
        # 10000 × (1 + 0.0133 × −0.25) = 9966.75
        self.assertAlmostEqual(self.result.final_equity, 9966.75, places=6)
        self.assertAlmostEqual(self.result.index, 0.996675, places=9)

    def test_twr_needs_no_external_flows(self):
        """回测没有入金：TWR 指数必须等于 终值/起值（否则就是剔出金算错了）。"""
        self.assertAlmostEqual(self.result.index,
                               self.result.final_equity / self.result.start_equity, places=12)

    def test_summary_of_that_single_trade(self):
        stats = backtest.summarise(self.result, years=1.0)
        self.assertEqual(stats["trades"], 1)
        self.assertEqual(stats["wins"], 0)
        self.assertEqual(stats["win_rate"], 0.0)
        self.assertAlmostEqual(stats["avg_r"], -0.25, places=12)
        self.assertAlmostEqual(stats["total_r"], -0.25, places=12)
        self.assertAlmostEqual(stats["multiple"], 0.996675, places=9)
        self.assertAlmostEqual(stats["cagr"], 0.996675 ** (1.0 / 1.0) - 1.0, places=12)
        self.assertEqual(stats["stop_exits"], 0)
        self.assertEqual(stats["signal_exits"], 1)


class TwoTradeStatisticsTest(unittest.TestCase):
    """两笔连着的交易：T1 信号离场 R=−0.25，T2 止损 R=−1.00。

    第二段：信号在 bar 26（收 99），入场 bar 27 开 99、止损 95（距 4.04%），
    bar 27 的 low 94 ≤ 95 → 当根就止损，成交价 = 止损价 95。
    R = (95 − 99) / (99 − 95) = −1.00
    权益：9966.75 × (1 − 0.0133) = 9834.192225
    """

    def setUp(self):
        rows = HAND_ROWS + [
            [99.0, 99.5, 98.5, 99.0],      # 26
            [99.0, 99.4, 94.0, 94.5],      # 27 止损（low 94 ≤ 95）
        ]
        self.result = _run(rows, [
            _signal(19, 1, entry=100.0, stop=96.0),
            _signal(26, 1, entry=99.0, stop=95.0),
        ])

    def test_both_trades_and_their_r_multiples(self):
        self.assertEqual(len(self.result.trades), 2)
        self.assertAlmostEqual(self.result.trades[0].r_gross, -0.25, places=12)
        self.assertAlmostEqual(self.result.trades[1].r_gross, -1.00, places=12)
        self.assertEqual(self.result.trades[1].reason, backtest.EXIT_STOP)
        self.assertEqual(self.result.trades[1].exit_price, 95.0)

    def test_second_entry_waits_for_the_first_to_close(self):
        """T1 在 bar 25 平仓，T2 在 bar 27 入场 —— 没有重叠持仓。"""
        first, second = self.result.trades
        self.assertLess(first.exit_bar, second.entry_bar)

    def test_aggregate_r_statistics(self):
        stats = backtest.summarise(self.result, years=1.0)
        self.assertEqual(stats["trades"], 2)
        self.assertAlmostEqual(stats["avg_r"], -0.625, places=12)     # (−0.25 −1.00)/2
        self.assertAlmostEqual(stats["total_r"], -1.25, places=12)
        self.assertEqual(stats["win_rate"], 0.0)
        self.assertAlmostEqual(stats["worst_r"], -1.00, places=12)
        self.assertAlmostEqual(stats["best_r"], -0.25, places=12)
        self.assertAlmostEqual(stats["final_equity"], 9834.192225, places=6)
        self.assertEqual(stats["stop_exits"], 1)
        self.assertEqual(stats["signal_exits"], 1)


class StopWinsOnSameBarTest(unittest.TestCase):
    """同一根 bar 内止损与信号离场同时成立 → **必须**按止损（保守假设）。"""

    def test_long_stop_beats_signal_exit(self):
        rows = _flat(20) + [
            [100.0, 100.2, 99.0, 100.0],    # 20 入场根（开 100，止损 96）
            [100.0, 100.2, 95.0, 94.0],     # 21 low 95 ≤ 96（止损）且收 94 < SMA20（信号）
        ]
        result = _run(rows, [_signal(19, 1, entry=100.0, stop=96.0)])
        trade = result.trades[0]
        self.assertEqual(trade.reason, backtest.EXIT_STOP)
        self.assertEqual(trade.exit_price, 96.0)             # 止损价，**不是**收盘 94
        self.assertAlmostEqual(trade.r_gross, -1.0, places=12)

    def test_short_stop_beats_signal_exit(self):
        rows = _flat(20) + [
            [100.0, 101.0, 99.8, 100.0],    # 20 入场根（开 100，止损 104）
            [100.0, 105.0, 99.8, 106.0],    # 21 high 105 ≥ 104（止损）且收 106 > SMA20（信号）
        ]
        result = _run(rows, [_signal(19, -1, entry=100.0, stop=104.0)])
        trade = result.trades[0]
        self.assertEqual(trade.reason, backtest.EXIT_STOP)
        self.assertEqual(trade.exit_price, 104.0)             # 止损价，**不是**收盘 106
        self.assertAlmostEqual(trade.r_gross, -1.0, places=12)

    def test_gap_through_the_stop_fills_at_the_open(self):
        """跳空开在止损之外：成交价是开盘价（更差），不是止损价。"""
        rows = _flat(20) + [
            [100.0, 100.2, 99.0, 100.0],    # 20 入场根
            [95.0, 95.5, 94.0, 94.5],       # 21 开盘 95 < 止损 96 → 按 95 成交
        ]
        result = _run(rows, [_signal(19, 1, entry=100.0, stop=96.0)])
        trade = result.trades[0]
        self.assertEqual(trade.reason, backtest.EXIT_STOP)
        self.assertEqual(trade.exit_price, 95.0)
        self.assertAlmostEqual(trade.r_gross, -1.25, places=12)   # (95−100)/4


class FundingTest(unittest.TestCase):
    """资金费：单位（百分比→分数）、符号（多头付/空头收、direction 反转）、累计。"""

    @staticmethod
    def _rates(n_hours: int, rate: str, direction: str = "long", start_s: int = 0):
        return backtest.FundingRates.from_rows(
            "TST", [{"timestamp": start_s + 3600 * (i + 1), "rate": rate,
                     "direction": direction} for i in range(n_hours)])

    def test_rate_is_percent_per_hour(self):
        """rate=1.0 表示 1%/小时 → 小数 0.01；4 小时 = 4%。"""
        rates = self._rates(4, "1.0")
        self.assertAlmostEqual(rates.paid(1, 0, 4 * 3600), 0.04, places=12)

    def test_long_pays_when_direction_is_long(self):
        rates = self._rates(4, "0.01")            # 0.01%/h → 1e-4
        self.assertAlmostEqual(rates.paid(1, 0, 4 * 3600), 4e-4, places=12)   # 多头付
        self.assertAlmostEqual(rates.paid(-1, 0, 4 * 3600), -4e-4, places=12)  # 空头收

    def test_short_direction_flips_the_sign(self):
        rates = self._rates(4, "0.01", direction="short")
        self.assertAlmostEqual(rates.paid(-1, 0, 4 * 3600), 4e-4, places=12)   # 空头付
        self.assertAlmostEqual(rates.paid(1, 0, 4 * 3600), -4e-4, places=12)   # 多头收

    def test_window_is_half_open_on_the_left(self):
        """区间是 (start, end]：起点那一小时已经计入上一格。"""
        rates = self._rates(4, "0.01")
        self.assertAlmostEqual(rates.paid(1, 0, 2 * 3600), 2e-4, places=12)
        self.assertAlmostEqual(rates.paid(1, 3 * 3600, 4 * 3600), 1e-4, places=12)
        self.assertAlmostEqual(rates.paid(1, 4 * 3600, 4 * 3600), 0.0, places=12)

    def test_engine_accumulates_over_every_held_bar(self):
        """两笔手算交易场景 + 合成费率：多头每根的支出 = qty × close × 4h 费率。"""
        rows = HAND_ROWS + [
            [99.0, 99.5, 98.5, 99.0],
            [99.0, 99.4, 94.0, 94.5],
        ]
        rates = backtest.FundingRates.from_rows(
            "TST", [{"timestamp": (T0 // 1000) + 3600 * h, "rate": "0.01", "direction": "long"}
                    for h in range(1, 200)])
        result = _run(rows, [_signal(19, 1, entry=100.0, stop=96.0),
                             _signal(26, 1, entry=99.0, stop=95.0)], funding=rates)
        self.assertEqual(len(result.trades), 2)
        for trade in result.trades:
            expected = 0.0
            for index in range(trade.entry_bar, trade.exit_bar + 1):
                expected += trade.qty * rows[index][3] * 4 * (0.01 / 100.0)
            self.assertAlmostEqual(trade.funding, expected, places=9)
            self.assertGreater(trade.funding, 0.0)        # 多头在 long 方向付钱 = 支出
        self.assertAlmostEqual(result.funding_total,
                               sum(t.funding for t in result.trades), places=9)
        # 扣资金费后 R 一定更差，且差额 = 资金费 / 初始风险
        self.assertLess(result.trades[0].r_net, result.trades[0].r_gross)
        self.assertAlmostEqual(result.trades[0].r_gross - result.trades[0].r_net,
                               result.trades[0].funding / result.trades[0].risk_usd, places=12)

    def test_short_receives_in_a_long_paying_regime(self):
        rows = _flat(20) + [
            [100.0, 100.5, 99.5, 100.0],
            [100.0, 100.4, 99.6, 100.0],
            [100.0, 100.4, 99.6, 100.0],
            [100.0, 100.4, 99.6, 100.0],
        ]
        rates = backtest.FundingRates.from_rows(
            "TST", [{"timestamp": (T0 // 1000) + 3600 * h, "rate": "0.01", "direction": "long"}
                    for h in range(1, 200)])
        # 空单在 bar 20 入场、直到数据末尾（eod 平仓），全程收取资金费
        result = _run(rows, [_signal(19, -1, entry=100.0, stop=104.0)], funding=rates)
        trade = result.trades[0]
        self.assertEqual(trade.reason, backtest.EXIT_EOD)
        self.assertLess(trade.funding, 0.0)          # 负 = 收入
        self.assertGreater(trade.r_net, trade.r_gross)


class DrawdownUsesAppDdTest(unittest.TestCase):
    """回撤口径必须是 `app.dd`：与熔断/面板同一个函数，逐位一致。"""

    def _run_path(self):
        # 一笔先赚后亏再赚的路径（手写信号，止损/离场可控）
        rows = _flat(20) + [
            [100.0, 100.2, 99.0, 100.0],    # 20 入场根（开 100，止损 96）
            [100.0, 106.0, 99.5, 105.0],    # 21 上涨
            [105.0, 106.0, 99.0, 99.5],     # 22 跌破 SMA20 → 信号离场 @99.5
            [99.5, 100.0, 99.0, 99.8],
        ]
        return _run(rows, [_signal(19, 1, entry=100.0, stop=96.0)], risk_pct=0.02)

    def test_index_and_peak_match_dd_bootstrap(self):
        result = self._run_path()
        self.assertTrue(result.points)
        points = [{"ts": ts, "equity": equity} for ts, equity in result.points]
        state = dd.bootstrap(points[1:], anchor_ts=points[0]["ts"],
                            anchor_equity=points[0]["equity"])
        self.assertAlmostEqual(result.index, state.index, places=12)
        self.assertAlmostEqual(result.peak, state.peak, places=12)

    def test_max_drawdown_matches_dd_evaluate_over_the_path(self):
        result = self._run_path()
        state = dd.bootstrap([], anchor_ts=result.points[0][0] - 1,
                            anchor_equity=result.start_equity)
        worst = 0.0
        worst_point: Optional[tuple] = None
        for ts, equity in result.points:
            state = dd.advance(state, ts=ts, equity=equity) or state
            verdict = dd.evaluate(state.index, state.peak, risk_pct=0.02)
            if verdict.drawdown is not None and verdict.drawdown >= worst:
                worst = float(verdict.drawdown)
                worst_point = (state.index, state.peak)
        self.assertAlmostEqual(result.max_dd, worst, places=12)
        if worst_point is None:                        # 没有权益点 = 测试数据坏了
            self.fail("expected at least one drawdown reading")
        # `app.risk.drawdown`（dd.evaluate 内部同一个函数）对最深那一格给出同一个数
        index, peak = worst_point
        value = dd.risk_drawdown(index, peak)
        if value is None:
            self.fail("dd.risk_drawdown returned None for a live index/peak pair")
        self.assertAlmostEqual(value, worst, places=12)

    def test_drawdown_verdict_uses_the_risk_tier_thresholds(self):
        result = self._run_path()
        verdict = dd.evaluate(result.index, result.peak, risk_pct=0.02)
        self.assertAlmostEqual(result.dd_warn, verdict.warn, places=12)
        self.assertAlmostEqual(result.dd_kill, verdict.kill, places=12)
        self.assertEqual(result.dd_warn, 0.30)      # 2% 档：warn 30% / kill 40%
        self.assertEqual(result.dd_kill, 0.40)

    def test_max_drawdown_is_never_negative(self):
        result = self._run_path()
        self.assertGreaterEqual(result.max_dd, 0.0)
        self.assertLessEqual(result.max_dd, 1.0)


class EmptyAndDegenerateTest(unittest.TestCase):
    def test_no_signals_yields_zeroes_and_does_not_crash(self):
        result = _run(_flat(40), [])
        self.assertEqual(result.trades, [])
        self.assertEqual(result.final_equity, result.start_equity)
        stats = backtest.summarise(result, years=1.0)
        self.assertEqual(stats["trades"], 0)
        self.assertEqual(stats["win_rate"], 0.0)
        self.assertEqual(stats["avg_r"], 0.0)
        self.assertEqual(stats["total_r"], 0.0)
        self.assertEqual(stats["cagr"], 0.0)
        self.assertEqual(stats["multiple"], 1.0)
        self.assertEqual(stats["max_dd"], 0.0)
        self.assertEqual(stats["by_symbol"], {})

    def test_empty_bar_list_yields_zeroes(self):
        series = backtest.Series(symbol="TST", bars=[], ind={}, signals=[])
        result = backtest.simulate({"TST": series}, risk_pct=RISK)
        self.assertEqual(result.trades, [])
        self.assertEqual(result.final_equity, result.start_equity)
        self.assertEqual(result.max_dd, 0.0)

    def test_signal_on_the_last_bar_has_no_entry(self):
        """末根没有"下一根开盘"，信号必须被丢弃而不是用收盘价凑一笔。"""
        result = _run(_flat(25), [_signal(24, 1, entry=100.0, stop=96.0)])
        self.assertEqual(result.trades, [])

    def test_open_position_at_the_end_is_closed_at_the_last_close(self):
        result = _run(_flat(25), [_signal(19, 1, entry=100.0, stop=96.0)])
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.reason, backtest.EXIT_EOD)
        self.assertEqual(trade.exit_bar, 24)
        self.assertAlmostEqual(trade.r_gross, 0.0, places=12)   # 平进平出

    def test_second_signal_while_in_position_is_skipped(self):
        """每标的同时至多一仓：持仓中的第二个信号被跳过并计数。"""
        rows = _flat(25)
        result = _run(rows, [_signal(19, 1, entry=100.0, stop=96.0),
                             _signal(21, 1, entry=100.0, stop=96.0)])
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.skipped_busy, 1)

    def test_bad_risk_pct_is_rejected(self):
        with self.assertRaises(ValueError):
            _run(_flat(5), [], risk_pct=0.0)


class SingleSourceOfTruthTest(unittest.TestCase):
    """口径唯一性：信号只能来自 `app.strategy`，回撤只能来自 `app.dd`。

    验收会 grep 这两条 import；这里再加一层行为断言（`signals_for` 真的调 `scan`）
    与一层源码断言（不复制判定逻辑）。
    """

    @property
    def source(self) -> str:
        return (ROOT / "scripts" / "backtest.py").read_text(encoding="utf-8")

    def test_imports_the_frozen_engine(self):
        self.assertIn("from app.strategy import", self.source)
        self.assertIn("from app.dd import", self.source)

    def test_does_not_reimplement_the_signal_rule(self):
        for forbidden in ("def squeeze", "def band(", "def scan(", "def exit_triggered(",
                          "def evaluate_entry("):
            self.assertNotIn(forbidden, self.source,
                             f"{forbidden!r} 出现在 backtest.py：信号口径必须只有一份")

    def test_signals_for_delegates_to_strategy_scan(self):
        bars = _bars(_flat(5))
        sentinel = [_signal(3, 1, entry=100.0, stop=96.0)]
        with mock.patch.object(backtest, "scan", return_value=sentinel) as patched:
            out = backtest.signals_for("TST", bars)
        patched.assert_called_once()
        self.assertIs(out, sentinel)

    def test_drawdown_chain_is_app_dd(self):
        """不改 app.dd 就没法换口径：把 dd.advance 换成探针，引擎必须调它。"""
        rows = _flat(22) + [[100.0, 100.4, 99.6, 100.0]]
        series = backtest.build_series("TST", _bars(rows),
                                       signals=[_signal(19, 1, entry=100.0, stop=96.0)])
        calls = []
        real = backtest.dd_advance

        def probe(state, **kwargs):
            calls.append(kwargs.get("ts"))
            return real(state, **kwargs)

        with mock.patch.object(backtest, "dd_advance", side_effect=probe):
            backtest.simulate({"TST": series}, risk_pct=RISK)
        self.assertGreater(len(calls), 0)


class SummaryShapeTest(unittest.TestCase):
    """`summarise` 的字段是报告/JSON 的契约，缺一个键就等于报告少一列。"""

    REQUIRED = ("trades", "wins", "losses", "win_rate", "avg_r", "avg_r_net", "median_r",
                "best_r", "worst_r", "avg_win_r", "avg_loss_r", "total_r", "gross_pnl",
                "net_pnl", "funding_usd", "fees_usd", "start_equity", "final_equity",
                "multiple", "years", "cagr", "twr_index", "twr_peak", "max_dd", "dd_warn",
                "dd_kill", "stop_exits", "signal_exits", "eod_exits", "avg_bars_held",
                "long_trades", "short_trades", "skipped_busy", "by_symbol")

    def test_all_contract_keys_present_and_json_serialisable(self):
        import json
        stats = backtest.run_scenario("TST", {"TST": backtest.build_series(
            "TST", _bars(HAND_ROWS), signals=[_signal(19, 1, entry=100.0, stop=96.0)])},
            risk_pct=RISK, use_funding=False, include_trades=True)
        for key in self.REQUIRED:
            self.assertIn(key, stats)
        self.assertIn("trades_detail", stats)
        json.dumps(stats)          # 不抛 = 可序列化

    def test_run_scenario_matches_simulate_plus_summarise(self):
        series = backtest.build_series("TST", _bars(HAND_ROWS),
                                       signals=[_signal(19, 1, entry=100.0, stop=96.0)])
        stats = backtest.run_scenario("TST", {"TST": series}, risk_pct=RISK, use_funding=False)
        result = backtest.simulate({"TST": series}, risk_pct=RISK, use_funding=False)
        self.assertEqual(stats["trades"], len(result.trades))
        self.assertAlmostEqual(stats["final_equity"], result.final_equity, places=9)


class CliOfflineTest(unittest.TestCase):
    """`--bars-from-file` + `--no-funding`：完全离线跑通并给出统计。"""

    def test_offline_cli_run(self):
        import contextlib
        import io
        import json
        import os
        import tempfile

        rows = {"TST": [{"t": bar.open_ms, "o": bar.open, "h": bar.high,
                         "l": bar.low, "c": bar.close, "v": 0.0}
                        for bar in _bars(_flat(400))]}
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(rows, handle)
        self.addCleanup(os.unlink, handle.name)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = backtest.main(["--symbols", "TST", "--bars-from-file", handle.name,
                                  "--no-funding", "--risk", "0.0133", "--since", "2020-01-01"])
        out = buf.getvalue()
        self.assertEqual(code, 0, out)
        self.assertIn("成交假设", out)
        self.assertIn("TST", out)

    def test_json_mode_is_machine_readable(self):
        import contextlib
        import io
        import json
        import os
        import tempfile

        rows = {"TST": [{"t": bar.open_ms, "o": bar.open, "h": bar.high,
                         "l": bar.low, "c": bar.close, "v": 0.0}
                        for bar in _bars(_flat(400))]}
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(rows, handle)
        self.addCleanup(os.unlink, handle.name)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = backtest.main(["--symbols", "TST", "--bars-from-file", handle.name,
                                  "--no-funding", "--json", "--since", "2020-01-01"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertIn("assumptions", payload)
        self.assertIn("runs", payload)
        self.assertIn("1.33%", payload["runs"])
        self.assertIn("TST", payload["runs"]["1.33%"])
        net = payload["runs"]["1.33%"]["TST"]["net"]
        self.assertEqual(net["trades"], 0)              # 400 根平盘：算不出密集突破
        self.assertAlmostEqual(net["multiple"], 1.0, places=9)


if __name__ == "__main__":
    unittest.main()
