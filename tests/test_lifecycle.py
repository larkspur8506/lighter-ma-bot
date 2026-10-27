"""`app/lifecycle.py` 的契约测试 —— **全离线**（临时目录里的库 + 手搓的 K 线）。

钉住的东西（T6 验收项，逐条对应）：

1. **状态机**：`IDLE → OPEN → CLOSING → CLOSED` 的每一步都落 `events`；
   非法转换（`CLOSED → OPEN` / `IDLE → CLOSED` / `OPEN → IDLE`）**抛错**，绝不静默。
2. **离场判据是冻结口径**：信号离场走 `app.strategy.exit_triggered()`（用 spy 证明它真的
   被调用、且参数就是这根 bar），本模块不重写判据。
3. **减仓不受熔断阻挡**：DD 熔断（`risk_pct=1.33%` 档 → 30%）与每日亏损超限（5%）时，
   开仓被拒但**平仓/止损照常成立**；`decide_exit()` 的签名里根本没有风控入参。
4. **sizing / risk 任一拒绝 → 不产生开仓意图**，且带结构化 `Rejection(stage, code,
   message, detail)`；决策顺序 = **先 sizing 后 risk**（risk 拿到的是真实名义）。
5. **同 bar 只开一仓**（与 T5 的 `UNIQUE(symbol, bar_ms)` 呼应）。
6. **决策是纯数据**：frozen dataclass + `as_dict()` 可 JSON 序列化（T10 面板直接渲染）。
7. **不发网络请求**：源码里没有 `urllib` / `requests` / `httpx` / `socket`，
   `app.market` 只在 `TYPE_CHECKING` 下作类型引用（运行时 `vars()` 里没有它）。

期望值全部手算写死（每个断言旁给出算式），不做「自证式断言」。
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import pathlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError, is_dataclass
from unittest import mock

import app.lifecycle as lifecycle
import app.strategy as strategy
from app.risk import (GATE_DAILY_LOSS, GATE_DD, GATE_KILL, RiskLimits, RiskState)
from app.sizing import REJECT_BELOW_MIN_QUOTE, SizingConfig
from app.store import ALLOWED_TRANSITIONS, POSITION_STATES, StateStore
from app.symbol_registry import spec
from tests.test_strategy_port import plateau_breakout_bars

BAR_MS = 4 * 3600 * 1000
T0 = 1_600_000_000_000            # 任意 4h 栅格原点（与真实数据的 t 同量级）
BTC_INFO = spec("BTC").as_market_info()      # pdec=1 / sdec=5 / mif=200 / min_quote=10

# 手算参考（equity=10000、BTC）
#   止损距离 = 104.2 − 100.0338 = 4.1662
#   数量     = 10000 × 1.33% ÷ 4.1662 = 31.928 … → 精度 floor 后见 plan 断言
# 下面这些数字来自 `strategy.scan(plateau_breakout_bars())` 的真实信号（见
# `EntryDecisionTests.test_real_signal_produces_an_entry_intent_with_exact_numbers`）。
PLAN_QTY_INT = 3243902            # 32.43902 @ size_decimals=5
PLAN_NOTIONAL = 3380.145884       # 32.43902 × 104.2
# 风险额 = 32.43902 × (104.2 − 100.1) = 132.999982（floor 之后略小于目标 133）


# --------------------------------------------------------------------------- 夹具


def flat_bars(start_ms: int, n: int, px: float, spread: float = 0.5):
    """n 根完全平盘的 4h K 线（high/low 夹住 close，止损默认打不到）。"""
    return [strategy.Bar(open_ms=start_ms + k * BAR_MS, open=px, high=px + spread,
                         low=px - spread, close=px) for k in range(n)]


def long_exit_bars(n: int = 45, last_close: float = 108.0, last_low: float = 107.5):
    """45 根 110 的平盘 + 最后一根跌到 108（low 107.5 > 止损 105 → 只触发信号离场）。

    SMA20@44 = (19×110 + 108) / 20 = **109.9** > 108 → `exit_triggered(…, side=1)` 为真。
    """
    bars = flat_bars(T0, n, 110.0)
    bars[-1] = strategy.Bar(open_ms=T0 + (n - 1) * BAR_MS, open=110.0, high=110.2,
                            low=last_low, close=last_close)
    return bars


def short_exit_bars(n: int = 45, last_close: float = 92.0, last_high: float = 92.5):
    """45 根 90 的平盘 + 最后一根涨到 92（high 92.5 < 止损 96 → 只触发信号离场）。

    SMA20@44 = (19×90 + 92) / 20 = **90.1** < 92 → `exit_triggered(…, side=-1)` 为真。
    """
    bars = flat_bars(T0, n, 90.0)
    bars[-1] = strategy.Bar(open_ms=T0 + (n - 1) * BAR_MS, open=90.0, high=last_high,
                            low=89.8, close=last_close)
    return bars


def long_position(**overrides) -> lifecycle.PositionView:
    """多单：entry 112 / stop 105 / qty 4.156，信号 bar = bars[0]（入场根 = bars[1]）。"""
    fields = dict(position_id=1, symbol="BTC", side=1, state=lifecycle.OPEN,
                  qty_int=415600, size_decimals=5, entry_price=112.0, entry_price_int=1120,
                  stop_price=105.0, stop_price_int=1050, price_decimals=1, bar_ms=T0)
    fields.update(overrides)
    return lifecycle.PositionView(**fields)


def short_position(**overrides) -> lifecycle.PositionView:
    """空单：entry 88 / stop 96；其余同上。"""
    fields = dict(position_id=2, symbol="BTC", side=-1, state=lifecycle.OPEN,
                  qty_int=415600, size_decimals=5, entry_price=88.0, entry_price_int=880,
                  stop_price=96.0, stop_price_int=960, price_decimals=1, bar_ms=T0)
    fields.update(overrides)
    return lifecycle.PositionView(**fields)


def risk_state(**overrides) -> RiskState:
    """一切正常的账户快照（回撤 0、当日无亏损、无持仓）。"""
    fields = dict(equity=10000.0, day_start_equity=10000.0, trades_today=0,
                  open_positions=(), open_notional_total=0.0,
                  open_notional_by_symbol={}, dd_index=1.0, dd_peak_index=1.0)
    fields.update(overrides)
    return RiskState(**fields)


_REAL: dict = {}


def real_signal():
    """冻结引擎在 `plateau_breakout_bars()` 上给出的第 1 个信号（长仓 @ bar 420）。"""
    if not _REAL:
        bars = plateau_breakout_bars()
        sigs = strategy.scan(bars, "BTC")
        _REAL["bars"] = bars
        _REAL["signal"] = sigs[0]
        _REAL["count"] = len(sigs)
    return _REAL["bars"], _REAL["signal"]


class StoreCase(unittest.TestCase):
    """每个用例一个临时库（`StateStore` 用真 SQLite，不 mock 存储层）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "lifecycle.db")
        self.store = StateStore(self.path)
        self.addCleanup(self.store.close)
        self.lc = lifecycle.Lifecycle(self.store, limits=RiskLimits(),
                                      sizing_config=SizingConfig())

    def enter(self, ts: float = 1760000000.0):
        """用真实信号开一仓（走 apply_entry，落库 + 落事件），返回 (decision, transition, row)。"""
        bars, sig = real_signal()
        d = self.lc.decide("BTC", bars, i=sig.bar_index, signal=sig, equity=10000.0,
                           market_info=BTC_INFO, risk_state=risk_state())
        self.assertEqual(d.action, lifecycle.ACTION_ENTRY, d.reason_text)
        tr = self.lc.apply_entry(d, ts=ts)
        row = self.store.open_positions()[0]
        return d, tr, row

    def transition_events(self):
        return [e for e in self.store.events(limit=50) if e["kind"] == "transition"]


# --------------------------------------------------------------------------- 冻结口径


class FrozenSurfaceTests(StoreCase):
    def test_state_names_are_the_store_ones(self):
        # 状态名与 T5 **同一份**，不是复制一份字面量
        self.assertEqual(lifecycle.STATES, POSITION_STATES)
        self.assertEqual(lifecycle.STATES, ("IDLE", "OPEN", "CLOSING", "CLOSED"))
        self.assertEqual((lifecycle.IDLE, lifecycle.OPEN, lifecycle.CLOSING, lifecycle.CLOSED),
                         ("IDLE", "OPEN", "CLOSING", "CLOSED"))

    def test_transition_table_matches_the_store(self):
        self.assertEqual(lifecycle.LEGAL_TRANSITIONS, dict(ALLOWED_TRANSITIONS))
        self.assertEqual(lifecycle.LEGAL_TRANSITIONS["CLOSED"], ())       # 终态
        self.assertEqual(lifecycle.LEGAL_TRANSITIONS["CLOSING"], ("OPEN", "CLOSED"))

    def test_module_does_not_import_a_network_client(self):
        path = pathlib.Path(lifecycle.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        banned = {"urllib", "requests", "httpx", "socket", "http", "aiohttp"}
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.append(node.module)
        roots = {name.split(".")[0] for name in found}
        self.assertEqual(roots & banned, set(), f"network imports found: {sorted(roots & banned)}")
        # `app.market` 只在 TYPE_CHECKING 下出现 → 运行时拿不到它
        self.assertNotIn("MarketInfo", vars(lifecycle))
        self.assertNotIn("market", vars(lifecycle))

    def test_decision_is_a_frozen_plain_data_object(self):
        d = lifecycle.decide_exit(long_position(), long_exit_bars(),
                                  strategy.compute_indicators(long_exit_bars()), 44)
        self.assertTrue(is_dataclass(d))
        with self.assertRaises(FrozenInstanceError):
            d.action = "entry"                                       # type: ignore[misc]
        blob = json.loads(json.dumps(d.as_dict()))                   # T10 面板直接吃
        self.assertEqual(blob["action"], lifecycle.ACTION_EXIT)
        self.assertEqual(set(blob) >= {"action", "from_state", "to_state", "rejections",
                                       "plan", "codes", "detail"}, True)

    def test_exit_decision_takes_no_risk_inputs(self):
        """减仓不受熔断阻挡的**结构性**保证：签名里根本没有风控入参。"""
        params = set(inspect.signature(lifecycle.decide_exit).parameters)
        self.assertEqual(params & {"risk_state", "limits", "risk", "risk_limits"}, set())
        params = set(inspect.signature(lifecycle.decide).parameters)
        self.assertIn("risk_state", params)      # 开仓分支要它
        self.assertIn("limits", params)


# --------------------------------------------------------------------------- 开仓


class EntryDecisionTests(StoreCase):
    def test_real_signal_produces_an_entry_intent_with_exact_numbers(self):
        bars, sig = real_signal()
        self.assertEqual((sig.bar_index, sig.side), (420, 1))
        self.assertAlmostEqual(sig.entry_price, 104.2, places=10)
        self.assertAlmostEqual(sig.stop_price, 100.03379333333334, places=10)

        d = lifecycle.decide_entry(sig, equity=10000.0, market_info=BTC_INFO,
                                   risk_state=risk_state(), bars=bars, i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_ENTRY)
        self.assertEqual((d.from_state, d.to_state), ("IDLE", "OPEN"))
        self.assertTrue(d.is_intent)
        self.assertEqual(d.rejections, ())
        self.assertEqual(d.bar_ms, sig.bar_open_ms)
        self.assertEqual(d.side, 1)
        # 定仓：10000 × 1.33% ÷ 4.1662 → 数量 floor 到 5 位小数 → × 104.2
        self.assertEqual(d.qty_int, PLAN_QTY_INT)
        self.assertAlmostEqual(d.plan.base_qty, 32.43902, places=8)
        self.assertAlmostEqual(d.notional, PLAN_NOTIONAL, places=6)
        self.assertAlmostEqual(d.plan.risk_amount, 132.999982, places=6)
        self.assertEqual(d.plan.stop_price, 100.1)                   # 多单止损 ceil 到 1 位小数
        self.assertEqual(d.plan.stop_price_int, 1001)
        self.assertEqual(d.plan.leverage, 5.0)
        self.assertAlmostEqual(d.plan.margin, 676.0291768, places=6)
        self.assertEqual(d.detail["stage"], "approved")

    def test_sizing_runs_before_risk_and_risk_sees_the_sized_notional(self):
        """顺序 = 先 sizing 后 risk，且 risk 拿到的是**算出来的真实名义**。"""
        bars, sig = real_signal()
        calls = []
        real_plan = lifecycle.plan_order
        real_check = lifecycle.check_entry

        def spy_plan(request, info, config=None):
            calls.append(("sizing", request.symbol))
            return real_plan(request, info, config)

        def spy_check(symbol, notional, state, limits=None):
            calls.append(("risk", round(notional, 6)))
            return real_check(symbol, notional, state, limits)

        with mock.patch.object(lifecycle, "plan_order", spy_plan), \
                mock.patch.object(lifecycle, "check_entry", spy_check):
            d = lifecycle.decide_entry(sig, equity=10000.0, market_info=BTC_INFO,
                                       risk_state=risk_state(), bars=bars, i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_ENTRY)
        self.assertEqual(calls, [("sizing", "BTC"), ("risk", 3380.145884)])

    def test_sizing_rejection_skips_risk_entirely(self):
        """sizing 先跑：它拒了就没有名义可问，risk 根本不该被调用（更不该放行）。"""
        bars, sig = real_signal()
        with mock.patch.object(lifecycle, "check_entry",
                               side_effect=AssertionError("risk must not run when sizing fails")):
            d = lifecycle.decide_entry(sig, equity=10.0, market_info=BTC_INFO,
                                       risk_state=risk_state(equity=10.0, day_start_equity=10.0),
                                       bars=bars, i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertIsNone(d.plan)
        self.assertEqual(len(d.rejections), 1)
        rej = d.rejections[0]
        self.assertEqual(rej.stage, lifecycle.STAGE_SIZING)
        self.assertEqual(rej.code, REJECT_BELOW_MIN_QUOTE)     # 3.38 名义 < min_quote 10
        self.assertIn("min_quote_amount", rej.detail)
        self.assertAlmostEqual(rej.detail["notional"], 3.379206, places=6)

    def test_daily_loss_limit_blocks_new_entries(self):
        """当日亏 10%（> 5% 上限）→ 拒开仓，理由结构化。"""
        bars, sig = real_signal()
        d = lifecycle.decide_entry(sig, equity=9000.0, market_info=BTC_INFO,
                                   risk_state=risk_state(equity=9000.0), bars=bars,
                                   i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertIn(GATE_DAILY_LOSS, d.codes)
        self.assertEqual(d.rejections[0].stage, lifecycle.STAGE_RISK)
        self.assertAlmostEqual(d.rejections[0].detail["loss_pct"], 0.1, places=9)
        self.assertEqual(d.rejections[0].detail["equity"], 9000.0)
        # 定仓本身是成功的（plan 保留给面板看），只是被风控挡下
        self.assertIsNotNone(d.plan)
        self.assertEqual(d.qty_int, 2919512)          # 9000 × 1.33% ÷ 4.1662 → floor(…,5)
        self.assertAlmostEqual(float(d.notional or 0.0), 3042.131504, places=6)

    def test_drawdown_kill_blocks_new_entries(self):
        """TWR 回撤 30% = 1.33% 档的熔断线 → 拒开仓。"""
        bars, sig = real_signal()
        d = lifecycle.decide_entry(sig, equity=10000.0, market_info=BTC_INFO,
                                   risk_state=risk_state(dd_index=0.70, dd_peak_index=1.0),
                                   bars=bars, i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertEqual(d.codes, (GATE_DD,))
        self.assertAlmostEqual(d.rejections[0].detail["drawdown"], 0.30, places=9)
        self.assertAlmostEqual(d.rejections[0].detail["kill"], 0.30, places=9)

    def test_every_blocked_gate_is_reported(self):
        """kill 与当日亏损同时成立 → 两条结构化理由都在（不静默只报第一条）。"""
        bars, sig = real_signal()
        limits = RiskLimits(kill=True, max_trades_per_day=1)
        d = lifecycle.decide_entry(sig, equity=9000.0, market_info=BTC_INFO,
                                   risk_state=risk_state(equity=9000.0, trades_today=1,
                                                         dd_index=0.70),
                                   limits=limits, bars=bars, i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertEqual(set(d.codes), {GATE_KILL, GATE_DAILY_LOSS, "daily_trade_limit", GATE_DD})
        self.assertTrue(all(r.stage == lifecycle.STAGE_RISK for r in d.rejections))
        self.assertTrue(all(r.message for r in d.rejections))

    def test_existing_position_blocks_a_second_entry(self):
        bars, sig = real_signal()
        d = lifecycle.decide_entry(sig, equity=10000.0, market_info=BTC_INFO,
                                   risk_state=risk_state(), position=long_position(),
                                   bars=bars, i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertEqual(d.codes, (lifecycle.REJECT_SYMBOL_BUSY,))
        self.assertEqual(d.rejections[0].stage, lifecycle.STAGE_LIFECYCLE)

    def test_same_bar_never_trades_twice(self):
        """`UNIQUE(symbol, bar_ms)` 的决策侧对偶：同一根 bar 第二次不给意图。"""
        bars, sig = real_signal()
        d = lifecycle.decide_entry(sig, equity=10000.0, market_info=BTC_INFO,
                                   risk_state=risk_state(), bar_taken=True,
                                   bars=bars, i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertEqual(d.codes, (lifecycle.REJECT_SAME_BAR,))

    def test_no_signal_is_a_structured_skip(self):
        d = lifecycle.decide_entry(None, equity=10000.0, market_info=BTC_INFO,
                                   risk_state=risk_state(), bars=flat_bars(T0, 5, 100.0), i=4)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertEqual(d.codes, (lifecycle.REJECT_NO_SIGNAL,))

    def test_signal_from_another_bar_is_refused(self):
        bars, sig = real_signal()
        d = lifecycle.decide_entry(sig, equity=10000.0, market_info=BTC_INFO,
                                   risk_state=risk_state(), bars=bars, i=sig.bar_index + 1)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertEqual(d.codes, (lifecycle.REJECT_SIGNAL_BAR,))
        self.assertEqual(d.rejections[0].detail["signal_bar_ms"], sig.bar_open_ms)

    def test_equity_mismatch_between_sizing_and_risk_is_refused(self):
        """按 A 权益定仓、按 B 权益放行 = 事故；两个数不一致就拒。"""
        bars, sig = real_signal()
        d = lifecycle.decide_entry(sig, equity=10000.0, market_info=BTC_INFO,
                                   risk_state=risk_state(equity=12000.0),
                                   bars=bars, i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertEqual(d.codes, (lifecycle.REJECT_EQUITY_MISMATCH,))
        self.assertEqual(d.rejections[0].detail, {"equity": 10000.0, "risk_state_equity": 12000.0})

    def test_frozen_market_never_produces_an_intent(self):
        frozen = spec("BTC").as_market_info(is_frozen=True)
        bars, sig = real_signal()
        d = lifecycle.decide_entry(sig, equity=10000.0, market_info=frozen,
                                   risk_state=risk_state(), bars=bars, i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertEqual(d.rejections[0].stage, lifecycle.STAGE_SIZING)
        self.assertEqual(d.rejections[0].code, "market_not_tradeable")


# --------------------------------------------------------------------------- 离场


class ExitDecisionTests(StoreCase):
    def test_long_signal_exit_fires_on_close_below_sma20(self):
        bars = long_exit_bars()
        ind = strategy.compute_indicators(bars)
        self.assertAlmostEqual(ind["sma20"][44], 109.9, places=9)     # (19×110+108)/20
        d = lifecycle.decide_exit(long_position(), bars, ind, 44)
        self.assertEqual(d.action, lifecycle.ACTION_EXIT)
        self.assertEqual((d.from_state, d.to_state), ("OPEN", "CLOSING"))
        self.assertAlmostEqual(d.exit_price, 108.0, places=9)
        self.assertEqual(d.detail["kind"], "exit")
        self.assertEqual(d.detail["trigger"], "sma20")
        self.assertEqual(d.rejections, ())
        self.assertEqual(d.bar_ms, bars[44].open_ms)

    def test_short_signal_exit_fires_on_close_above_sma20(self):
        bars = short_exit_bars()
        ind = strategy.compute_indicators(bars)
        self.assertAlmostEqual(ind["sma20"][44], 90.1, places=9)      # (19×90+92)/20
        d = lifecycle.decide_exit(short_position(), bars, ind, 44)
        self.assertEqual(d.action, lifecycle.ACTION_EXIT)
        self.assertAlmostEqual(d.exit_price, 92.0, places=9)

    def test_exit_uses_the_frozen_exit_triggered_function(self):
        """离场判据必须是 `app.strategy.exit_triggered` —— 用 spy 证明调用点与参数。"""
        bars = long_exit_bars()
        ind = strategy.compute_indicators(bars)
        seen = []
        real = strategy.exit_triggered

        def spy(bars_arg, ind_arg, i_arg, side_arg=1):
            seen.append((len(bars_arg), i_arg, side_arg, ind_arg is ind))
            return real(bars_arg, ind_arg, i_arg, side_arg)

        with mock.patch.object(strategy, "exit_triggered", spy):
            d = lifecycle.decide_exit(long_position(), bars, ind, 44)
        self.assertEqual(seen, [(45, 44, 1, True)])
        self.assertEqual(d.action, lifecycle.ACTION_EXIT)

    def test_close_exactly_on_sma20_does_not_exit(self):
        """冻结口径是**严格**小于：SMA20 上的收盘不触发离场（不是「≤」）。"""
        bars = flat_bars(T0, 45, 110.0)          # 收盘 == SMA20 == 110
        ind = strategy.compute_indicators(bars)
        self.assertAlmostEqual(ind["sma20"][44], 110.0, places=9)
        d = lifecycle.decide_exit(long_position(), bars, ind, 44)
        self.assertEqual(d.action, lifecycle.ACTION_HOLD)
        self.assertFalse(strategy.exit_triggered(bars, ind, 44, 1))

    def test_stop_hit_long_wins_and_records_the_stop_price(self):
        bars = long_exit_bars(last_close=104.0, last_low=104.5)      # low 104.5 ≤ 105
        ind = strategy.compute_indicators(bars)
        self.assertTrue(strategy.exit_triggered(bars, ind, 44, 1))   # 信号也触发了
        d = lifecycle.decide_exit(long_position(), bars, ind, 44)
        self.assertEqual(d.action, lifecycle.ACTION_STOP)            # 但止损优先
        self.assertAlmostEqual(d.exit_price, 105.0, places=9)        # 成交价 = 止损价
        self.assertEqual(d.detail["kind"], "stop")
        self.assertEqual(d.detail["trigger"], "stop_price")

    def test_stop_hit_short_wins_and_records_the_stop_price(self):
        bars = short_exit_bars(last_close=98.0, last_high=97.5)      # high 97.5 ≥ 96
        ind = strategy.compute_indicators(bars)
        d = lifecycle.decide_exit(short_position(), bars, ind, 44)
        self.assertEqual(d.action, lifecycle.ACTION_STOP)
        self.assertAlmostEqual(d.exit_price, 96.0, places=9)

    def test_entry_bar_only_allows_the_stop(self):
        """入场当根不判 SMA20（与旧系统 walk() / 回测的 `u > ei` 守卫一致）。"""
        # 22 根平盘 110，最后一根跌到 108：SMA20@21 = (19×110+108)/20 = 109.9 > 108
        bars = flat_bars(T0, 22, 110.0)
        bars[21] = strategy.Bar(open_ms=T0 + 21 * BAR_MS, open=110.0, high=110.2,
                                low=107.5, close=108.0)
        ind = strategy.compute_indicators(bars)
        self.assertAlmostEqual(ind["sma20"][21], 109.9, places=9)
        self.assertTrue(strategy.exit_triggered(bars, ind, 21, 1))    # 信号本身是成立的
        entry_bar_pv = long_position(bar_ms=T0 + 20 * BAR_MS)         # 信号 bar = 20 → 入场根 = 21
        d = lifecycle.decide_exit(entry_bar_pv, bars, ind, 21)
        self.assertEqual(d.action, lifecycle.ACTION_HOLD)             # 但入场根不判 MA20
        self.assertTrue(d.detail["is_entry_bar"])

        crash = flat_bars(T0, 22, 110.0)
        crash[21] = strategy.Bar(open_ms=T0 + 21 * BAR_MS, open=110.0, high=110.2,
                                 low=104.0, close=104.5)              # low 104 ≤ 止损 105
        ind2 = strategy.compute_indicators(crash)
        d2 = lifecycle.decide_exit(entry_bar_pv, crash, ind2, 21)
        self.assertEqual(d2.action, lifecycle.ACTION_STOP)           # 止损照常生效
        stop_px = d2.exit_price
        assert stop_px is not None
        self.assertAlmostEqual(stop_px, 105.0, places=9)

    def test_bar_before_the_entry_bar_is_a_hold(self):
        bars = long_exit_bars(n=3)
        ind = strategy.compute_indicators(bars)
        pv = long_position(bar_ms=T0 + 10 * BAR_MS)                  # 入场根在更后面
        d = lifecycle.decide_exit(pv, bars, ind, 2)
        self.assertEqual(d.action, lifecycle.ACTION_HOLD)
        self.assertIn("precedes the entry bar", d.reason)

    def test_closing_position_is_not_exited_twice(self):
        bars = long_exit_bars()
        ind = strategy.compute_indicators(bars)
        d = lifecycle.decide_exit(long_position(state=lifecycle.CLOSING), bars, ind, 44)
        self.assertEqual(d.action, lifecycle.ACTION_HOLD)
        self.assertIn("already CLOSING", d.reason)

    def test_no_position_is_a_structured_skip(self):
        d = lifecycle.decide_exit(long_position(state=lifecycle.CLOSED),
                                  long_exit_bars(),
                                  strategy.compute_indicators(long_exit_bars()), 44)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertEqual(d.codes, (lifecycle.REJECT_NO_POSITION,))

    def test_missing_stop_price_is_reported_not_swallowed(self):
        bars = long_exit_bars()
        ind = strategy.compute_indicators(bars)
        d = lifecycle.decide_exit(long_position(stop_price=None, stop_price_int=None), bars, ind, 44)
        self.assertEqual(d.action, lifecycle.ACTION_EXIT)            # 信号离场照常
        self.assertEqual(d.codes, (lifecycle.REJECT_BAD_POSITION,))  # 但止损不可判要说出来
        self.assertIn("no stop price", d.rejections[0].message)

    # ------------------------------------------------------------ 熔断不挡减仓

    def test_drawdown_circuit_breaker_still_allows_the_exit(self):
        """**重点**：DD 熔断时，开新仓被拒，但平仓/止损照常成立。"""
        bars = long_exit_bars()
        killed = risk_state(dd_index=0.60, dd_peak_index=1.0)         # 40% 回撤 ≫ 30% 熔断
        limits = RiskLimits(kill=True)                                # 顺便把 kill 也打开
        d = self.lc.decide("BTC", bars, signal=None, equity=10000.0, market_info=BTC_INFO,
                           risk_state=killed)
        # 该标的库里没仓 → 这是开仓分支（无信号 → skip，与熔断无关）
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)

        # 同一份被熔断的账户状态，换成「有活仓」→ 离场照常产出意图
        pv = long_position()
        with mock.patch.object(self.lc, "position_view", return_value=pv):
            d = self.lc.decide("BTC", bars, equity=10000.0, market_info=BTC_INFO,
                               risk_state=killed)
        self.assertEqual(d.action, lifecycle.ACTION_EXIT)
        self.assertEqual(d.rejections, ())
        # 而且 `limits` 里的 kill 也传进来了 —— 依然不影响减仓
        self.assertTrue(self.lc.limits.kill is False)
        killed_limits = RiskLimits(kill=True)
        d2 = lifecycle.decide("BTC", bars, position=pv, equity=10000.0,
                              market_info=BTC_INFO, risk_state=killed, limits=killed_limits)
        self.assertEqual(d2.action, lifecycle.ACTION_EXIT)
        self.assertEqual(d2.rejections, ())

    def test_daily_loss_limit_still_allows_the_exit(self):
        """同一条规则的另一半：当日亏损 20% 时也**必须**能平仓。"""
        bars = long_exit_bars()
        losing = risk_state(equity=8000.0)                            # 20% > 5% 上限
        pv = long_position()
        d = lifecycle.decide("BTC", bars, position=pv, equity=8000.0, market_info=BTC_INFO,
                             risk_state=losing)
        self.assertEqual(d.action, lifecycle.ACTION_EXIT)
        self.assertEqual(d.rejections, ())
        # 同样的账户状态下去开新仓 → 被 daily_loss 拒（证明这份状态确实「熔断了」）
        bars2, sig = real_signal()
        d2 = lifecycle.decide_entry(sig, equity=8000.0, market_info=BTC_INFO,
                                    risk_state=losing, bars=bars2, i=sig.bar_index)
        self.assertEqual(d2.action, lifecycle.ACTION_SKIP)
        self.assertIn(GATE_DAILY_LOSS, d2.codes)

    def test_drawdown_kill_blocks_entries_in_the_same_account_state(self):
        """把上一条的反面写死：**同一份**熔断状态确实挡住了开仓。"""
        bars, sig = real_signal()
        killed = risk_state(dd_index=0.60, dd_peak_index=1.0)
        d = lifecycle.decide_entry(sig, equity=10000.0, market_info=BTC_INFO,
                                   risk_state=killed, bars=bars, i=sig.bar_index)
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertIn(GATE_DD, d.codes)


# --------------------------------------------------------------------------- 状态机


class StateMachineTests(StoreCase):
    def test_illegal_transitions_raise(self):
        for source, target in (("CLOSED", "OPEN"), ("CLOSED", "IDLE"), ("IDLE", "CLOSED"),
                               ("IDLE", "CLOSING"), ("OPEN", "IDLE"), ("CLOSING", "CLOSING")):
            with self.subTest(source=source, target=target):
                with self.assertRaises(lifecycle.IllegalTransition) as ctx:
                    self.lc.transition("BTC", target, from_state=source, reason="test")
                self.assertIn(f"{source} -> {target}", str(ctx.exception))

    def test_unknown_state_raises(self):
        with self.assertRaises(lifecycle.IllegalTransition):
            self.lc.transition("BTC", "PARTIAL", from_state="OPEN")
        with self.assertRaises(lifecycle.IllegalTransition):
            self.lc.transition("BTC", "OPEN", from_state="BOGUS")

    def test_closed_position_row_cannot_be_reopened(self):
        """CLOSED 是行的终态：重开要新开一行，改老行 = 抛错（库里也拦）。"""
        _d, _tr, row = self.enter()
        pid = int(row["id"])
        self.lc.begin_close(pid, reason="exit")
        self.lc.complete_close(pid, exit_price_int=1020, kind="exit")
        self.assertEqual(self.store.position(pid)["state"], "CLOSED")
        with self.assertRaises(lifecycle.IllegalTransition):
            self.lc.transition("BTC", "OPEN", position_id=pid)
        with self.assertRaises(lifecycle.IllegalTransition):
            self.lc.abort_close(pid)
        self.assertEqual(self.store.position(pid)["state"], "CLOSED")

    def test_every_transition_step_lands_in_events(self):
        _d, tr, row = self.enter()
        pid = int(row["id"])
        self.assertEqual((tr.from_state, tr.to_state, tr.reason), ("IDLE", "OPEN", "entry"))
        first = self.lc.begin_close(pid, reason="stop", bar_ms=T0 + 9 * BAR_MS)
        self.assertEqual((first.from_state, first.to_state), ("OPEN", "CLOSING"))
        second = self.lc.complete_close(pid, exit_price_int=1001, kind="stop", reason="stop")
        self.assertEqual((second.from_state, second.to_state), ("CLOSING", "CLOSED"))

        events = self.transition_events()
        self.assertEqual(len(events), 3)
        messages = sorted(e["message"] for e in events)
        self.assertEqual(messages, ["BTC CLOSING→CLOSED (stop)",
                                    "BTC IDLE→OPEN (entry)",
                                    "BTC OPEN→CLOSING (stop)"])
        payloads = sorted(json.loads(e["data"])["to"] for e in events)
        self.assertEqual(payloads, ["CLOSED", "CLOSING", "OPEN"])
        self.assertTrue(all(json.loads(e["data"])["position_id"] == pid for e in events))
        self.assertEqual({t.event_id for t in (tr, first, second)},
                         {int(e["id"]) for e in events})
        # 库里也照常记了它自己的语义事件（open / closing / stop）
        kinds = {e["kind"] for e in self.store.events(limit=50)}
        self.assertTrue({"open", "closing", "stop"} <= kinds)

    def test_abort_close_is_an_explicit_rollback(self):
        _d, _tr, row = self.enter()
        pid = int(row["id"])
        self.lc.begin_close(pid, reason="exit")
        self.assertEqual(self.store.position(pid)["state"], "CLOSING")
        back = self.lc.abort_close(pid, reason="close_rejected")
        self.assertEqual((back.from_state, back.to_state), ("CLOSING", "OPEN"))
        self.assertEqual(self.store.position(pid)["state"], "OPEN")
        self.assertIn("BTC CLOSING→OPEN (close_rejected)",
                      [e["message"] for e in self.transition_events()])

    def test_apply_entry_requires_an_entry_intent(self):
        d = lifecycle.decide_entry(None, equity=10000.0, market_info=BTC_INFO,
                                   risk_state=risk_state())
        with self.assertRaises(lifecycle.LifecycleError):
            self.lc.apply_entry(d)

    def test_apply_exit_requires_an_exit_intent(self):
        d = lifecycle.decide_exit(long_position(), long_exit_bars(),
                                  strategy.compute_indicators(long_exit_bars()), 44)
        hold = lifecycle.decide_exit(long_position(), flat_bars(T0, 45, 110.0),
                                     strategy.compute_indicators(flat_bars(T0, 45, 110.0)), 44)
        self.assertEqual(hold.action, lifecycle.ACTION_HOLD)
        with self.assertRaises(lifecycle.LifecycleError):
            self.lc.apply_exit(hold)
        self.assertEqual(d.action, lifecycle.ACTION_EXIT)

    def test_apply_returns_nothing_for_hold_and_skip(self):
        hold = lifecycle.decide_exit(long_position(), flat_bars(T0, 45, 110.0),
                                     strategy.compute_indicators(flat_bars(T0, 45, 110.0)), 44)
        self.assertEqual(self.lc.apply(hold), ())

    def test_store_state_is_read_back_through_the_state_machine(self):
        self.assertEqual(self.lc.state("BTC"), lifecycle.IDLE)
        _d, _tr, row = self.enter()
        self.assertEqual(self.lc.state("BTC"), lifecycle.OPEN)
        self.assertEqual(self.lc.position_view("BTC").position_id, int(row["id"]))
        self.lc.begin_close(int(row["id"]), reason="exit")
        self.assertEqual(self.lc.state("BTC"), lifecycle.CLOSING)
        self.lc.complete_close(int(row["id"]), exit_price_int=1020)
        self.assertEqual(self.lc.state("BTC"), lifecycle.IDLE)       # 槽位释放（行仍是 CLOSED）
        self.assertEqual(self.store.position(int(row["id"]))["state"], "CLOSED")

    def test_same_bar_cannot_be_traded_again_through_the_store(self):
        _d, _tr, row = self.enter()
        pid = int(row["id"])
        self.lc.begin_close(pid, reason="exit")
        self.lc.complete_close(pid, exit_price_int=1020)
        self.assertTrue(self.lc.bar_taken("BTC", int(row["bar_ms"])))
        # 同一根 bar 再开 → 决策侧拦（不给意图）
        bars, sig = real_signal()
        d = self.lc.decide("BTC", bars, i=sig.bar_index, signal=sig, equity=10000.0,
                           market_info=BTC_INFO, risk_state=risk_state())
        self.assertEqual(d.action, lifecycle.ACTION_SKIP)
        self.assertIn(d.codes[0], (lifecycle.REJECT_SAME_BAR, lifecycle.REJECT_SIGNAL_BAR))
        # 换一根 bar（新行）→ 允许，且是新的一笔
        self.assertFalse(self.lc.bar_taken("BTC", int(row["bar_ms"]) + 5 * BAR_MS))


# --------------------------------------------------------------------------- 完整回合


class RoundTripTests(StoreCase):
    def test_entry_then_signal_exit_end_to_end(self):
        """真实信号开仓 → 平盘 → 跌破 SMA20 离场：库、成交、事件、对账全链路。"""
        d, tr, row = self.enter(ts=1760000000.0)
        pid = int(row["id"])
        self.assertEqual(row["state"], "OPEN")
        self.assertEqual(row["qty_int"], PLAN_QTY_INT)
        self.assertAlmostEqual(row["stop_price"], 100.1, places=9)
        self.assertEqual(row["bar_ms"], d.bar_ms)

        sig_bar = int(row["bar_ms"])
        bars = flat_bars(sig_bar, 45, 104.2)                  # 与入场同价的平盘
        bars[44] = strategy.Bar(open_ms=sig_bar + 44 * BAR_MS, open=104.2, high=104.7,
                                low=101.9, close=102.0)       # 102 > 止损 100.1，只破 SMA20
        ind = strategy.compute_indicators(bars)
        self.assertAlmostEqual(ind["sma20"][44], 104.09, places=9)   # (19×104.2+102)/20

        exit_decision = self.lc.decide("BTC", bars, ind=ind, equity=10000.0,
                                       market_info=BTC_INFO, risk_state=risk_state())
        self.assertEqual(exit_decision.action, lifecycle.ACTION_EXIT)
        self.assertAlmostEqual(exit_decision.exit_price, 102.0, places=9)
        transitions = self.lc.apply_exit(exit_decision, ts=1760000100.0)
        self.assertEqual([(t.from_state, t.to_state) for t in transitions],
                         [("OPEN", "CLOSING"), ("CLOSING", "CLOSED")])

        closed = self.store.position(pid)
        self.assertEqual(closed["state"], "CLOSED")
        self.assertEqual(closed["exit_price_int"], 1020)              # 1 位小数 → 102.0
        # pnl = (102 − 104.2) × 32.43902 = −71.365844
        self.assertAlmostEqual(closed["net_pnl"], -71.365844, places=6)
        self.assertEqual(closed["exit_reason"], "exit")

        legs = [(t["kind"], t["price"]) for t in reversed(self.store.trades())]
        self.assertEqual(legs, [("entry", 104.2), ("exit", 102.0)])
        self.assertEqual(self.lc.state("BTC"), lifecycle.IDLE)
        self.assertEqual(self.store.audit(), [])                      # T5 的不变式全过
        self.assertEqual(len(self.transition_events()), 3)

    def test_stop_exit_end_to_end_uses_the_stop_price(self):
        _d, _tr, row = self.enter(ts=1760000000.0)
        pid = int(row["id"])
        sig_bar = int(row["bar_ms"])
        bars = flat_bars(sig_bar, 45, 104.2)
        bars[44] = strategy.Bar(open_ms=sig_bar + 44 * BAR_MS, open=104.2, high=104.7,
                                low=99.5, close=100.0)        # low 99.5 ≤ 止损 100.1
        ind = strategy.compute_indicators(bars)
        d = self.lc.decide("BTC", bars, ind=ind, equity=10000.0, market_info=BTC_INFO,
                           risk_state=risk_state())
        self.assertEqual(d.action, lifecycle.ACTION_STOP)
        self.lc.apply_exit(d, ts=1760000100.0)
        closed = self.store.position(pid)
        self.assertEqual(closed["exit_price_int"], 1001)             # 止损价 100.1
        self.assertEqual(closed["exit_reason"], "stop")
        # pnl = (100.1 − 104.2) × 32.43902 = −132.999982
        self.assertAlmostEqual(closed["net_pnl"], -132.999982, places=6)
        self.assertEqual([t["kind"] for t in reversed(self.store.trades())], ["entry", "stop"])
        self.assertEqual(self.store.audit(), [])


if __name__ == "__main__":
    unittest.main()
