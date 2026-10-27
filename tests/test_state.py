"""`app/state.py` 的契约测试 —— **全离线**（临时目录里的库 + 注入的假读数）。

钉住的东西（T5 验收项）：
  * `RiskState` 装配正确，且 `open_positions` 是**标的元组**（与 T4 `risk.py` 同型）；
  * 每个字段都带来源标记（`equity_source` / `day_start_source` / `dd.source` …）；
  * **回撤锚点丢失不得静默归零**：锚点被 30 天清理 → 用 `dd_state`；`dd_state` 也没了
    → `index=peak=None` → `drawdown` 闸拒单（绝不返回 0%）；
  * 权益/价格/持仓三个注入点的降级路径（provider 抛错、返回 NaN、返回垃圾）；
  * `snapshot()` 只读（调用前后行数不变），唯一的写操作是显式的 `ensure_day_start()`。

期望值全部手算写死（见每个断言旁的算式）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from app.risk import GATE_DAILY_LOSS, GATE_DD, GATE_STATE, GATE_TOTAL_NOTIONAL, RiskLimits
from app.state import (
    DD_SOURCE_ANCHOR, DD_SOURCE_STATE, DD_SOURCE_UNAVAILABLE,
    EQUITY_SOURCE_PROVIDER, EQUITY_SOURCE_STALE, EQUITY_SOURCE_STORE,
    EQUITY_SOURCE_UNAVAILABLE, DDReading, StateAssembler,
    assemble_risk_state,
)
from app.store import StateStore, day_bounds, utc_day
from app.risk import check_entry

TS = 1760000000.0               # 2025-10-09T08:53:20Z → UTC 日 '2025-10-09'
DAY = "2025-10-09"
BAR_MS = 1760000000000

BTC_ENTRY = dict(symbol="BTC", side=1, qty_int=4000, size_decimals=5,
                 entry_price_int=800000, price_decimals=1, stop_price_int=780000,
                 bar_ms=BAR_MS, entry_order_id="OID-BTC", equity_at_entry=10000.0)
ETH_ENTRY = dict(symbol="ETH", side=-1, qty_int=20000, size_decimals=4,
                 entry_price_int=250000, price_decimals=2, stop_price_int=260000,
                 bar_ms=BAR_MS, entry_order_id="OID-ETH", equity_at_entry=10000.0)


class StateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "state.db")
        self.store = StateStore(self.path)
        self.addCleanup(self.store.close)

    # ---------------------------------------------------------------- 夹具

    def seed(self, *, equity: float = 10000.0, day_start: float = 10000.0,
             dd_index: float = 0.875, dd_peak: float = 1.25) -> None:
        """两个持仓（BTC 多 0.04@80000 / ETH 空 2.0@2500）+ 权益点 + 回撤状态。

        名义：BTC 0.04×80000 = 3200，ETH 2.0×2500 = 5000，合计 8200。
        回撤：(1.25 − 0.875) / 1.25 = 0.30（= 1.33% 风险档的熔断线，正好可测闸门）。
        """
        self.store.record_entry(**BTC_ENTRY, ts=TS)
        self.store.record_entry(**ETH_ENTRY, ts=TS + 1)
        self.store.record_equity(equity, ts=TS, twr_index=dd_index)
        self.store.save_dd_state(twr_index=dd_index, twr_peak=dd_peak, anchor_ts=TS,
                                 anchor_equity=day_start, ts=TS)

    def assembler(self, **kwargs) -> StateAssembler:
        kwargs.setdefault("clock", lambda: TS + 60)
        return StateAssembler(self.store, **kwargs)


# ------------------------------------------------------------------ 装配正确性


class AssemblyTests(StateTestCase):
    def test_snapshot_values_are_exact(self):
        self.seed()
        snap = self.assembler().snapshot()
        self.assertEqual(snap.as_of, TS + 60)
        self.assertEqual(snap.day, DAY)
        self.assertEqual(snap.equity, 10000.0)
        self.assertEqual(snap.equity_source, EQUITY_SOURCE_STORE)
        self.assertEqual(snap.day_start_equity, 10000.0)
        self.assertEqual(snap.day_start_source, "counter")
        self.assertEqual(snap.trades_today, 2)          # BTC + ETH 两笔开仓
        self.assertEqual(snap.trades_today_source, "trades")
        self.assertEqual(snap.open_positions, ("BTC", "ETH"))
        self.assertEqual(snap.positions_source, "store")
        self.assertEqual(snap.open_notional_by_symbol, {"BTC": 3200.0, "ETH": 5000.0})
        self.assertEqual(snap.open_notional_total, 8200.0)
        self.assertEqual(snap.dd.source, DD_SOURCE_STATE)
        self.assertEqual(snap.dd.index, 0.875)
        self.assertEqual(snap.dd.peak_index, 1.25)
        self.assertAlmostEqual(snap.dd.drawdown_pct, 0.30, places=12)
        self.assertTrue(snap.dd.available)
        self.assertFalse(snap.dd.degraded)
        self.assertTrue(snap.ok)
        self.assertEqual(snap.warnings, ())

    def test_risk_state_shape_matches_risk_module(self):
        """`open_positions` 必须是**标的元组**，不是计数 —— T4 用它判重仓/总数。"""
        self.seed()
        risk_state = self.assembler().risk_state()
        self.assertIsInstance(risk_state.open_positions, tuple)
        self.assertEqual(risk_state.open_positions, ("BTC", "ETH"))
        self.assertEqual(risk_state.equity, 10000.0)
        self.assertEqual(risk_state.day_start_equity, 10000.0)
        self.assertEqual(risk_state.trades_today, 2)
        self.assertEqual(risk_state.open_notional_total, 8200.0)
        self.assertEqual(risk_state.open_notional_by_symbol, {"BTC": 3200.0, "ETH": 5000.0})
        self.assertEqual(risk_state.dd_index, 0.875)
        self.assertEqual(risk_state.dd_peak_index, 1.25)
        self.assertEqual(risk_state.symbols(), ("BTC", "ETH"))     # T4 的读法
        self.assertEqual(risk_state.notional_for("btc"), 3200.0)   # 大小写不敏感

    def test_assembled_state_drives_the_real_risk_gates(self):
        """插到真闸门上验一遍：30% 回撤 → `drawdown` 熔断（1.33% 档阈值 30%）。"""
        self.seed()
        risk_state = self.assembler().risk_state()
        decision = check_entry("SOL", 1000.0, risk_state, RiskLimits())
        self.assertEqual(decision.codes, (GATE_DD,))          # 只有 DD 挡，其余全过
        self.assertIn("30.00%", decision.reason)

        # 峰值抬高到 2.0 → 回撤 56% 仍然熔断；把峰值放回 1.25 并且指数回到峰值 → 放行
        self.store.save_dd_state(twr_index=1.25, twr_peak=1.25, anchor_ts=TS,
                                 anchor_equity=10000.0, ts=TS + 30)
        self.assertTrue(check_entry("SOL", 1000.0, self.assembler().risk_state(),
                                    RiskLimits()).allowed)

    def test_closing_positions_are_still_reported_as_open(self):
        self.seed()
        pid = [p for p in self.store.open_positions() if p["symbol"] == "ETH"][0]["id"]
        self.store.begin_close(pid, ts=TS + 30)
        snap = self.assembler().snapshot()
        self.assertEqual(snap.open_positions, ("BTC", "ETH"))   # CLOSING 也算有仓

    def test_snapshot_is_read_only(self):
        self.seed()
        before = (self.store.position_count(), self.store.trade_count(),
                  self.store.event_count(), self.store.equity_point_count())
        for _ in range(3):
            self.assembler().snapshot()
        after = (self.store.position_count(), self.store.trade_count(),
                 self.store.event_count(), self.store.equity_point_count())
        self.assertEqual(before, after)

    def test_describe_and_as_dict_are_renderable(self):
        self.seed()
        snap = self.assembler().snapshot()
        text = snap.describe()
        self.assertIn("equity=10,000.00 (store)", text)
        self.assertIn("dd=30.00% (dd_state)", text)
        payload = snap.as_dict()
        self.assertEqual(payload["open_positions"], ["BTC", "ETH"])
        self.assertEqual(payload["dd"]["drawdown"], 0.30)
        self.assertTrue(payload["ok"])

    def test_assemble_risk_state_helper(self):
        self.seed()
        risk_state = assemble_risk_state(self.store, now_ts=TS + 60)
        self.assertEqual(risk_state.open_positions, ("BTC", "ETH"))
        self.assertEqual(risk_state.dd_peak_index, 1.25)


# ------------------------------------------------------------------ 回撤锚点生命周期


class DrawdownLifecycleTests(StateTestCase):
    def test_anchor_expiry_does_not_reset_drawdown_to_zero(self):
        """锚点被 30 天清理 → 回撤**仍然是 30%**（靠 `dd_state`），不是 0%。"""
        self.seed()
        self.store.append_dd_anchor(TS - 90 * 86400.0, 9000.0, twr_index=0.9, twr_peak=1.0)
        self.store.append_dd_anchor(TS - 45 * 86400.0, 9200.0, twr_index=0.92, twr_peak=1.0)
        self.assertEqual(self.store.prune_dd_anchors(keep_days=30, ts=TS + 60), 1)
        self.assertEqual(self.store.dd_anchor_count(), 1)      # 只剩最新那条

        dd = self.assembler().dd_reading()
        self.assertEqual(dd.source, DD_SOURCE_STATE)
        self.assertTrue(dd.available)
        self.assertFalse(dd.degraded)                          # dd_state 才 1 分钟大
        self.assertAlmostEqual(dd.drawdown_pct, 0.30, places=12)
        self.assertNotEqual(dd.drawdown_pct, 0.0)

    def test_all_anchors_gone_but_dd_state_survives(self):
        self.seed()
        for offset in (90, 60, 45):
            self.store.append_dd_anchor(TS - offset * 86400.0, 9000.0, twr_index=0.9,
                                        twr_peak=1.0)
        # 连「最新一行永不删」的保护也要能兜住：手工把锚点全清掉
        self.store.conn.execute("DELETE FROM dd_anchors")
        self.assertEqual(self.store.dd_anchor_count(), 0)

        dd = self.assembler().dd_reading()
        self.assertEqual(dd.source, DD_SOURCE_STATE)
        self.assertAlmostEqual(dd.drawdown_pct, 0.30, places=12)
        self.assertFalse(dd.history_available)                 # 面板画不出历史，但判定不受影响

    def test_missing_dd_state_falls_back_to_the_newest_anchor_as_degraded(self):
        self.seed()
        self.store.append_dd_anchor(TS, 8750.0, twr_index=0.875, twr_peak=1.25)
        self.store.conn.execute("DELETE FROM dd_state")
        dd = self.assembler().dd_reading()
        self.assertEqual(dd.source, DD_SOURCE_ANCHOR)
        self.assertTrue(dd.available)
        self.assertTrue(dd.degraded)                           # 峰值可能曾更高
        self.assertAlmostEqual(dd.drawdown_pct, 0.30, places=12)
        self.assertIn("lower bound", dd.reason)

    def test_no_evidence_at_all_is_unavailable_not_zero(self):
        """`dd_state` 和 `dd_anchors` 都没有 → `None`（拒单），**绝不** 0%。"""
        self.seed()
        self.store.conn.execute("DELETE FROM dd_state")
        dd = self.assembler().dd_reading()
        self.assertEqual(dd.source, DD_SOURCE_UNAVAILABLE)
        self.assertFalse(dd.available)
        self.assertIsNone(dd.index)
        self.assertIsNone(dd.peak_index)
        self.assertIsNone(dd.drawdown_pct)
        self.assertNotEqual(dd.drawdown_pct, 0.0)
        self.assertIn("refusing to assume 0%", dd.reason)

        snap = self.assembler().snapshot()
        self.assertFalse(snap.ok)
        self.assertTrue(any("dd unavailable" in w for w in snap.warnings))
        risk_state = snap.to_risk_state()
        self.assertIsNone(risk_state.dd_index)
        self.assertIsNone(risk_state.dd_peak_index)
        decision = check_entry("SOL", 1000.0, risk_state, RiskLimits())
        self.assertIn(GATE_DD, decision.codes)
        self.assertFalse(decision.allowed)

    def test_equity_curve_alone_never_bootstraps_a_peak(self):
        """有 30 天权益曲线但没锚点 → 仍然不可用（不许「用窗口重算一个峰值」）。"""
        for i in range(10):
            self.store.record_equity(10000.0 + i * 100.0, ts=TS - (10 - i) * 86400.0)
        self.store.record_equity(9200.0, ts=TS)
        self.store.conn.execute("DELETE FROM dd_state")
        dd = self.assembler().dd_reading()
        self.assertFalse(dd.available)
        self.assertIsNone(dd.drawdown_pct)

    def test_corrupt_peak_below_index_is_clamped_and_flagged(self):
        self.seed()
        self.store.conn.execute("UPDATE dd_state SET twr_index=1.5, twr_peak=1.0")
        dd = self.assembler().dd_reading()
        self.assertTrue(dd.available)
        self.assertTrue(dd.degraded)
        self.assertEqual(dd.peak_index, 1.5)                   # 夹到 index
        self.assertEqual(dd.drawdown_pct, 0.0)
        self.assertIn("clamped", dd.reason)
        self.assertEqual([f.code for f in self.store.audit()], ["dd_peak_below_index"])

    def test_stale_dd_state_is_flagged_not_silently_trusted(self):
        self.seed()
        dd = self.assembler(dd_max_age_s=3600.0).dd_reading(now_ts=TS + 2 * 86400.0)
        self.assertTrue(dd.available)
        self.assertTrue(dd.degraded)
        self.assertIn("stale", dd.reason)
        self.assertAlmostEqual(dd.drawdown_pct, 0.30, places=12)   # 数值仍可用，只是黄灯


# ------------------------------------------------------------------ 权益注入


class EquityTests(StateTestCase):
    def test_provider_wins_over_the_stored_point(self):
        self.seed()
        snap = self.assembler(equity_provider=lambda: 12345.67).snapshot()
        self.assertEqual(snap.equity, 12345.67)
        self.assertEqual(snap.equity_source, EQUITY_SOURCE_PROVIDER)

    def test_provider_failure_is_fail_closed(self):
        self.seed()

        def boom():
            raise ConnectionError("account api down")

        snap = self.assembler(equity_provider=boom).snapshot()
        self.assertIsNone(snap.equity)                         # 不用库里的旧值充数
        self.assertEqual(snap.equity_source, EQUITY_SOURCE_UNAVAILABLE)
        self.assertTrue(any("equity_provider raised" in w for w in snap.warnings))
        self.assertIn(GATE_STATE, check_entry("SOL", 1000.0, snap.to_risk_state(),
                                              RiskLimits()).codes)

    def test_provider_nan_is_treated_as_unavailable(self):
        self.seed()
        snap = self.assembler(equity_provider=lambda: float("nan")).snapshot()
        self.assertIsNone(snap.equity)
        self.assertEqual(snap.equity_source, EQUITY_SOURCE_UNAVAILABLE)

    def test_provider_negative_equity_is_rejected(self):
        self.seed()
        snap = self.assembler(equity_provider=lambda: -1.0).snapshot()
        self.assertIsNone(snap.equity)

    def test_stale_equity_fallback_only_when_explicitly_allowed(self):
        self.seed()
        snap = self.assembler(equity_provider=lambda: None,
                              allow_stale_equity=True).snapshot()
        self.assertEqual(snap.equity, 10000.0)
        self.assertEqual(snap.equity_source, EQUITY_SOURCE_STALE)
        self.assertTrue(any("out of date" in w for w in snap.warnings))

    def test_no_equity_at_all(self):
        snap = self.assembler().snapshot()                     # 空库
        self.assertIsNone(snap.equity)
        self.assertEqual(snap.equity_source, EQUITY_SOURCE_UNAVAILABLE)
        self.assertNotEqual(snap.ok, True)
        self.assertEqual(snap.open_positions, ())


# ------------------------------------------------------------------ 日初权益 / 计数


class DayStartTests(StateTestCase):
    def test_counter_row_wins(self):
        self.store.ensure_day(DAY, day_start_equity=9800.0, ts=TS)
        self.store.record_equity(10000.0, ts=TS - 60)
        snap = self.assembler().snapshot()
        self.assertEqual(snap.day_start_equity, 9800.0)
        self.assertEqual(snap.day_start_source, "counter")

    def test_previous_point_before_the_day_boundary_is_used(self):
        start, _end = day_bounds(DAY)
        self.store.record_equity(9700.0, ts=start - 3600.0)     # 昨天最后一根
        self.store.record_equity(9600.0, ts=start + 60.0)       # 今天第一根（已被亏损污染）
        self.store.record_equity(10000.0, ts=TS)
        snap = self.assembler().snapshot()
        self.assertEqual(snap.day_start_equity, 9700.0)
        self.assertEqual(snap.day_start_source, "curve_prev")

    def test_first_point_of_the_day_is_the_last_resort(self):
        start, _end = day_bounds(DAY)
        self.store.record_equity(9500.0, ts=start + 60.0)
        self.store.record_equity(10000.0, ts=TS)
        snap = self.assembler().snapshot()
        self.assertEqual(snap.day_start_equity, 9500.0)
        self.assertEqual(snap.day_start_source, "curve_first")
        self.assertTrue(any("first equity point" in w for w in snap.warnings))

    def test_missing_day_start_blocks_the_daily_loss_gate(self):
        self.store.record_entry(**BTC_ENTRY, ts=TS)
        self.store.record_equity(10000.0, ts=TS)
        self.store.save_dd_state(twr_index=1.0, twr_peak=1.0, anchor_ts=TS,
                                 anchor_equity=10000.0, ts=TS)
        # 把两条兜底来源都抹掉：计数行没有日初权益、库里也没有权益点
        self.store.conn.execute("DELETE FROM equity_curve")
        self.store.conn.execute("UPDATE daily_counters SET day_start_equity=NULL")
        snap = self.assembler().snapshot()
        self.assertIsNone(snap.day_start_equity)
        self.assertEqual(snap.day_start_source, "missing")
        self.assertIn(GATE_DAILY_LOSS, check_entry("SOL", 1000.0, snap.to_risk_state(),
                                                   RiskLimits()).codes)

    def test_trades_today_prefers_the_stricter_counter(self):
        self.seed()
        self.assertEqual(self.assembler().snapshot().trades_today, 2)
        self.store.conn.execute("UPDATE daily_counters SET trades_today=5 WHERE day=?", (DAY,))
        snap = self.assembler().snapshot()
        self.assertEqual(snap.trades_today, 5)
        self.assertEqual(snap.trades_today_source, "counter")

    def test_ensure_day_start_writes_once_and_is_the_only_write(self):
        self.store.record_equity(10000.0, ts=TS)
        assembler = self.assembler(equity_provider=lambda: 10500.0)
        first = assembler.ensure_day_start()
        self.assertEqual(first["day_start_equity"], 10500.0)
        self.assertEqual(first["trades_today"], 0)
        second = assembler.ensure_day_start(now_ts=TS + 60)      # 同一天再调不会覆盖
        self.assertEqual(second["day_start_equity"], 10500.0)
        self.assertEqual(self.store.daily_counter(DAY)["day_start_equity"], 10500.0)


# ------------------------------------------------------------------ 名义 / 持仓注入


class NotionalTests(StateTestCase):
    def test_price_provider_overrides_the_recorded_entry_price(self):
        self.seed()
        marks = {"BTC": 90000.0, "ETH": 2400.0}
        snap = self.assembler(price_provider=lambda sym: marks.get(sym)).snapshot()
        # BTC 0.04×90000 = 3600；ETH 2.0×2400 = 4800 → 8400
        self.assertEqual(snap.open_notional_by_symbol, {"BTC": 3600.0, "ETH": 4800.0})
        self.assertEqual(snap.open_notional_total, 8400.0)

    def test_bad_price_provider_falls_back_to_the_entry_price(self):
        self.seed()

        def flaky(symbol):
            raise RuntimeError("mark price down")

        snap = self.assembler(price_provider=flaky).snapshot()
        self.assertEqual(snap.open_notional_total, 8200.0)      # 库里的成交价，不是编的
        self.assertTrue(any("falling back to the recorded entry price" in w
                            for w in snap.warnings))

    def test_uncomputable_notional_fails_closed(self):
        self.seed()
        self.store.conn.execute("UPDATE positions SET qty=0 WHERE symbol='BTC'")
        snap = self.assembler().snapshot()
        self.assertEqual(snap.open_notional_by_symbol, {})      # 不做「只报算得出的那几个」
        self.assertIsNone(snap.open_notional_total)
        decision = check_entry("SOL", 1000.0, snap.to_risk_state(), RiskLimits())
        self.assertIn(GATE_TOTAL_NOTIONAL, decision.codes)

    def test_exchange_positions_are_unioned_with_the_store(self):
        self.seed()
        snap = self.assembler(
            positions_provider=lambda: [{"symbol": "SOL", "qty": 10.0}]).snapshot()
        self.assertEqual(snap.open_positions, ("BTC", "ETH", "SOL"))
        self.assertEqual(snap.positions_source, "store+exchange")
        self.assertTrue(any("not in the store" in w for w in snap.warnings))
        # 交易所侧有仓、库里没有行 → 名义不明 → 总名义不可用（fail-closed）
        self.assertIsNone(snap.open_notional_total)

    def test_positions_provider_failure_falls_back_to_the_store(self):
        self.seed()

        def boom():
            raise RuntimeError("positions api down")

        snap = self.assembler(positions_provider=boom).snapshot()
        self.assertEqual(snap.open_positions, ("BTC", "ETH"))   # 漏报持仓比多报危险
        self.assertEqual(snap.positions_source, "store_fallback")
        self.assertTrue(any("positions_provider raised" in w for w in snap.warnings))

    def test_store_only_positions_warn_when_the_exchange_disagrees(self):
        self.seed()
        snap = self.assembler(positions_provider=lambda: [{"symbol": "BTC"}]).snapshot()
        self.assertEqual(snap.open_positions, ("BTC", "ETH"))   # 并集：ETH 仍在
        self.assertTrue(any("store has positions the exchange does not report" in w
                            for w in snap.warnings))

    def test_no_positions_means_zero_notional(self):
        self.store.record_equity(10000.0, ts=TS)
        snap = self.assembler().snapshot()
        self.assertEqual(snap.open_positions, ())
        self.assertEqual(snap.open_notional_by_symbol, {})
        self.assertEqual(snap.open_notional_total, 0.0)

    def test_assembler_requires_a_store(self):
        with self.assertRaises(ValueError):
            StateAssembler(None)                             # type: ignore[arg-type]

    def test_assembler_clock_fallback_and_bad_now_ts(self):
        assembler = StateAssembler(self.store, clock=lambda: float("nan"))
        snap = assembler.snapshot()                           # clock 坏了 → 退回 time.time()
        self.assertGreater(snap.as_of, 1_600_000_000.0)
        with self.assertRaises(ValueError):
            assembler.snapshot(now_ts=float("inf"))


class DDPrimitiveTests(unittest.TestCase):
    """`DDReading` 自身的口径（不碰库）。"""

    def test_drawdown_math(self):
        reading = DDReading(index=0.75, peak_index=1.0, source=DD_SOURCE_STATE, available=True)
        self.assertAlmostEqual(reading.drawdown_pct, 0.25, places=12)

    def test_unavailable_reading_has_no_drawdown(self):
        reading = DDReading()
        self.assertIsNone(reading.drawdown_pct)
        self.assertFalse(reading.available)
        self.assertEqual(reading.source, DD_SOURCE_UNAVAILABLE)
        payload = reading.as_dict()
        self.assertIsNone(payload["drawdown"])
        self.assertFalse(payload["available"])

    def test_drawdown_never_negative(self):
        reading = DDReading(index=1.2, peak_index=1.0, source=DD_SOURCE_STATE, available=True)
        self.assertEqual(reading.drawdown_pct, 0.0)

    def test_utc_day_matches_store(self):
        self.assertEqual(utc_day(TS), DAY)


if __name__ == "__main__":                                    # pragma: no cover
    unittest.main()
