"""对抗审查 P2/P3 回归（2026-10-30，第二波修复）。

- P2：离场部分成交的当场结账（记离场腿 + 仓位缩到交易所实际量 + entry 腿同步 +
  按剩余量重挂止损）；entry_not_filled 后迟到成交的自动认领；`_closing_fill`
  的 60s 前向容差收紧到 2s；离场锚 5% 宽偏离闸；时效闸超小数/nan 垃圾值钳制。
- P3：止损补挂未确认不喂对账（stop_guard_unverified）；migrate 版本读进事务；
  not_filled 后重试 coi 换号；离场成交后撤残留止损单。

每条对应子代理报告（deleg_5b90d0ab 视角 A/B）里的一个已复现缺陷。
"""
import asyncio
import unittest
from types import SimpleNamespace

from app.exchange import client_order_index as coi_of
from app.main import max_signal_age_minutes
from app.executor import _closing_fill, Executor
from tests.test_executor import (BAR_MS, BTC, Base, FakeExchange, NOW_S, PLAN_QTY_INT,
                                 T0, bars_result, quiet_bars, stop_hit_bars)


class _PartialExitExchange(FakeExchange):
    """离场 IOC 成交一部分：仓位缩到 `remain_qty_int` 但没平完。"""

    def __init__(self, remain_qty_int, **kw):
        super().__init__(**kw)
        self._remain = int(remain_qty_int)

    async def place_exit(self, *, symbol, market_id, side, qty_int, price_decimals,
                         exit_price, bar_ms=None, client_order_index=None,
                         limit_price_int=None):
        self.exits.append({"symbol": symbol, "side": int(side), "qty_int": int(qty_int),
                           "exit_price": float(exit_price), "bar_ms": int(bar_ms or 0),
                           "limit_price_int": limit_price_int})
        self.positions[symbol] = {"side": -int(side), "qty_int": self._remain,
                                  "market_id": int(market_id), "size_decimals": 5}
        return self._result("exit", symbol=symbol, market_id=int(market_id),
                            side=int(side), qty_int=int(qty_int), reduce_only=True)


class PartialExitTests(Base):
    def _seed_stop_hit(self, ex):
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        row = self.seed_position(entry_price_int=1120, stop_price_int=1050, bar_ms=T0,
                                 qty_int=415600)
        ex.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        ex.orders = []
        self.executor = self.make_executor(exchange=ex, fill_confirm_s=0)
        self.reader.exchange = ex
        return row

    def test_partial_exit_rebases_the_position_and_rehangs_the_stop(self):
        """P2：部分成交 → 记离场腿、仓位缩到交易所实际量、按剩余量重挂止损、回 OPEN。"""
        ex = _PartialExitExchange(remain_qty_int=200000)
        self._seed_stop_hit(ex)
        report = self.run_tick()
        outcome = report.outcomes[0]
        self.assertTrue(outcome.detail.get("partial"), outcome.reason)
        row = self.store.position(int(outcome.detail["position_id"]))
        self.assertEqual(row["state"], "OPEN")            # abort 回 OPEN，下一轮重判
        self.assertEqual(int(row["qty_int"]), 200000)     # 仓位按交易所实际量改账
        # entry 腿同步缩 —— audit 的 entry_qty_mismatch 不变式必须仍然干净
        self.assertEqual(self.store.audit(), [])
        legs = [t for t in self.store.events(limit=100) if t["kind"] == "partial_exit"]
        self.assertEqual(len(legs), 1)
        self.assertTrue(any(k == "partial_exit" for k, _ in self.alerts))
        # 按剩余量重挂的止损单在交易所
        self.assertEqual(ex.stops[-1]["qty_int"], 200000)

    def test_zero_fill_still_takes_the_not_filled_path(self):
        """回归守卫：数量没变（纯零成交）走既有 exit_not_filled 路径，不做部分结账。"""
        ex = FakeExchange()
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        self.seed_position(entry_price_int=1120, stop_price_int=1050, bar_ms=T0,
                           qty_int=415600)
        ex.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        ex.orders = []
        ex.no_fill = {"exit"}
        self.executor = self.make_executor(exchange=ex, fill_confirm_s=0)
        self.reader.exchange = ex
        self.run_tick()
        self.assertFalse(any(k == "partial_exit" for k, _ in self.alerts))
        self.assertTrue(any(k == "exit_not_filled" for k, _ in self.alerts))


class _AdoptData:
    """entry_not_filled 事件里冻结的信号参数（与 _execute_entry 写入的同一形状）。"""

    @staticmethod
    def data(**over):
        base = {"qty_int": 415600, "side": 1, "entry_price_int": 1120,
                "stop_price_int": 900, "price_decimals": 1, "size_decimals": 5,
                "bar_ms": T0, "market_id": 1, "entry_coi": "123456",
                "equity_at_entry": 10000.0, "waited_s": 12.0}
        base.update(over)
        return base


class AdoptTests(Base):
    def _log_not_filled(self, **over):
        self.store.log_event("entry_not_filled", f"{BTC}: accepted but not filled",
                             symbol=BTC, data=_AdoptData.data(**over), ts=NOW_S)

    def test_late_filling_entry_is_adopted_and_backed_by_a_stop(self):
        """P2-3：确认窗外出现的真实仓位 → 按冻结参数补记账，守护本轮补挂止损。"""
        ex = FakeExchange()
        self.executor = self.make_executor(exchange=ex, fill_confirm_s=0)
        self.reader.exchange = ex
        self._log_not_filled()
        ex.positions[BTC] = {"side": 1, "qty_int": 300000, "market_id": 1,
                             "size_decimals": 5}     # 迟到的真实仓位（≤计划量）
        self.market.feeds[BTC] = bars_result(quiet_bars(400))   # 无信号：不干扰断言
        self.run_tick()
        rows = self.store.open_positions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["qty_int"]), 300000)  # 数量以交易所为准
        self.assertEqual(int(rows[0]["stop_price_int"]), 900)
        self.assertEqual(rows[0]["state"], "OPEN")
        self.assertTrue(any(k == "entry_adopted" for k, _ in self.alerts))
        self.assertTrue(ex.stops, "守护必须在本轮就给认领的仓补上止损")
        self.assertEqual(self.store.audit(), [])

    def test_exchange_position_without_a_not_filled_event_is_not_adopted(self):
        """人工仓（没有 entry_not_filled 记录）绝不认领 —— 只走既有对账告警。"""
        ex = FakeExchange()
        self.executor = self.make_executor(exchange=ex, fill_confirm_s=0)
        self.reader.exchange = ex
        ex.positions[BTC] = {"side": 1, "qty_int": 300000, "market_id": 1,
                             "size_decimals": 5}
        self.run_tick()
        self.assertEqual(self.store.open_positions(), [])
        self.assertFalse(any(k == "entry_adopted" for k, _ in self.alerts))

    def test_stale_not_filled_event_is_outside_the_adopt_window(self):
        self.executor = self.make_executor(fill_confirm_s=0)
        self.store.log_event("entry_not_filled", "old", symbol=BTC,
                             data=_AdoptData.data(), ts=NOW_S - Executor.ADOPT_WINDOW_S - 60)
        ex = FakeExchange()
        self.executor = self.make_executor(exchange=ex, fill_confirm_s=0)
        self.reader.exchange = ex
        ex.positions[BTC] = {"side": 1, "qty_int": 300000, "market_id": 1,
                             "size_decimals": 5}
        self.run_tick()
        self.assertEqual(self.store.open_positions(), [])
        self.assertFalse(any(k == "entry_adopted" for k, _ in self.alerts))

    def test_opposite_side_position_is_not_adopted(self):
        """认领守卫：与未成交信号**反向**的仓位不许认领（方向错了记账就是灾难）。"""
        ex = FakeExchange()
        self.executor = self.make_executor(exchange=ex, fill_confirm_s=0)
        self.reader.exchange = ex
        self._log_not_filled()
        ex.positions[BTC] = {"side": -1, "qty_int": 300000, "market_id": 1,
                             "size_decimals": 5}
        self.market.feeds[BTC] = bars_result(quiet_bars(400))
        self.run_tick()
        self.assertEqual(self.store.open_positions(), [])
        self.assertTrue(any(k == "entry_adopt_skipped" for k, _ in self.alerts))


class CoiRetryTests(Base):
    def test_retry_after_entry_not_filled_uses_a_fresh_coi(self):
        """P3：not_filled 后同 bar 重试必须换 coi —— 否则交易所以 duplicate 拒单。"""
        ex = FakeExchange()
        ex.no_fill = {"entry"}
        self.executor = self.make_executor(exchange=ex, fill_confirm_s=0)
        self.reader.exchange = ex
        self.run_tick()
        self.assertEqual(len(ex.entries), 1)
        ex.no_fill = set()
        self.run_tick()
        self.assertEqual(len(ex.entries), 2, "not_filled 后下一轮应重试同一信号")
        self.assertNotEqual(ex.entries[0]["coi"], ex.entries[1]["coi"],
                            "重试的 coi 必须与首次尝试不同")
        self.assertEqual(self.store.audit(), [])


class SignalAgeClampTests(unittest.TestCase):
    def test_tiny_decimal_env_value_falls_back_to_default(self):
        """P3：0~1 之间的超小数（如 1e-9）只会把所有开仓永久 skip → 退回默认 30。"""
        for dirty in ("1e-9", "0.0000001", "0.5"):
            self.assertEqual(max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": dirty}),
                             30.0, dirty)
        self.assertEqual(max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": "0"}), 0.0)
        self.assertEqual(max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": "45"}), 45.0)

    def test_nan_in_constructor_falls_back_to_the_default_not_off(self):
        """P3：`max(0.0, nan) == 0.0` 会静默关闸 —— nan/inf 必须退回默认 30。"""
        from tests.test_executor import FakeMarket, FakeReader, RiskLimits, SizingConfig
        b = Base()
        b.setUp()
        ex = FakeExchange()
        b.executor = b.make_executor(exchange=ex, fill_confirm_s=0,
                                     max_signal_age_min=float("nan"))
        self.assertEqual(b.executor.max_signal_age_min, 30.0)
        b.executor = b.make_executor(exchange=ex, fill_confirm_s=0,
                                     max_signal_age_min=float("inf"))
        self.assertEqual(b.executor.max_signal_age_min, 30.0)
        b.executor = b.make_executor(exchange=ex, fill_confirm_s=0,
                                     max_signal_age_min=-5)
        self.assertEqual(b.executor.max_signal_age_min, 0.0)   # 既有语义：负数=显式关闭


class ClosingFillWindowTests(unittest.TestCase):
    """P2：平仓成交判据必须发生在 opened_at **之后**（只留 2s 漂移容差）。"""

    ROW = {"symbol": "SOL", "side": 1, "qty": 1.2, "qty_int": 1200,
           "opened_at": 1_000_000.0, "price_decimals": 3, "stop_price": 98.0}

    @staticmethod
    def _trade(ts, qty=0.6, price=100.0):
        return SimpleNamespace(symbol="SOL", side=-1, qty=qty, price=price, ts=ts,
                               pnl=-3.21, trade_id=f"t{ts}", raw={})

    def test_fills_from_the_previous_position_40s_before_open_are_ignored(self):
        """审计 B 的复现形态：上一代仓在 opened_at 前 39~40s 的平仓腿不再算新仓的证据。"""
        trades = [self._trade(1_000_000.0 - 40), self._trade(1_000_000.0 - 39)]
        self.assertIsNone(_closing_fill(self.ROW, trades))

    def test_fill_one_second_before_open_is_within_the_drift_tolerance(self):
        trades = [self._trade(1_000_000.0 - 1, qty=1.2)]
        fill = _closing_fill(self.ROW, trades)
        self.assertIsNotNone(fill)

    def test_fill_after_open_still_settles(self):
        trades = [self._trade(1_000_000.0 + 5, qty=1.2)]
        fill = _closing_fill(self.ROW, trades)
        self.assertIsNotNone(fill)
        self.assertEqual(fill["trade_ids"], ["t1000005.0"])


class StopBackfillVisibleTests(Base):
    def test_unverified_backfill_is_not_fed_to_reconcile(self):
        """P3：补挂的止损没在活挂单里确认到 → 不许把它喂给对账掩盖 missing。"""
        ex = FakeExchange()
        self.executor = self.make_executor(exchange=ex, fill_confirm_s=0)
        self.reader.exchange = ex
        self.seed_position(entry_price_int=1120, stop_price_int=1050, bar_ms=T0)
        ex.positions[BTC] = {"side": 1, "qty_int": PLAN_QTY_INT, "market_id": 1}
        ex.orders = []                       # 交易所没有止损单 → missing

        class _BlindReader:
            async def active_orders(self):
                return ()                    # 活挂单永远读不到新单（可见性检查失败）

        saved = self.reader.active_orders
        self.reader.active_orders = _BlindReader().active_orders
        try:
            account = asyncio.run(self.reader.read())
            account2, guards = asyncio.run(self.executor._guard_stops(account))
        finally:
            self.reader.active_orders = saved
        self.assertEqual(guards, [])
        self.assertEqual(len(account2.orders), len(account.orders),
                         "未确认的新单不许 append 进对账读数")
        self.assertTrue(ex.stops, "补挂动作本身仍要发生（挂在交易所，下一轮自愈）")
        self.assertTrue(any(e["kind"] == "stop_guard_unverified" for e in self.events()))


class MigrateVersionInTxnTests(unittest.TestCase):
    def test_migrate_still_idempotent_and_returns_the_original_version(self):
        """P3：版本读进事务后，幂等性（已是最新原地返回原版本）不得变。"""
        import tempfile, os
        from app.store import StateStore, SCHEMA_VERSION
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.db")
            s1 = StateStore(path)
            self.assertEqual(s1.migrate(), SCHEMA_VERSION)
            self.assertEqual(s1.migrate(), SCHEMA_VERSION)   # 第二次原地返回
            s1.close()
            s2 = StateStore(path)
            self.assertEqual(s2.migrate(), SCHEMA_VERSION)   # 老库重开=已是最新
            s2.close()


class LeftoverStopCancelTests(Base):
    def test_online_exit_cancels_the_resting_stop(self):
        """P3：在线离场成交后，仍挂在交易所的止损单必须被撤掉（不咬下一笔开仓）。"""
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        row = self.seed_position(entry_price_int=1120, stop_price_int=1050, bar_ms=T0,
                                 qty_int=415600)
        coi = coi_of(BTC, T0, "stop")
        stop = SimpleNamespace(order_id="999", client_order_index=coi, market_id=1,
                               symbol=BTC, reduce_only=True, trigger_price_int=1050,
                               qty_int=415600, status="resting", order_expiry_ms=0)
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        self.exchange.orders = [stop]
        self.market.mids[BTC] = 103.0          # 带内（1.9%），离场正常走 mid 锚
        self.executor = self.make_executor(fill_confirm_s=0)
        self.run_tick()
        row2 = self.store.position(int(row["id"]))
        self.assertEqual(row2["state"], "CLOSED")
        self.assertIn({"market_id": 1, "order_index": 999}, self.exchange.cancels,
                      "残留止损必须被撤")


if __name__ == "__main__":                                     # pragma: no cover
    unittest.main()
