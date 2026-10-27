"""`app/executor.py` 的契约测试 —— **全离线**（临时目录里的真 SQLite + 手搓的
K 线 / 交易所 / 读取器，一个网络请求都不发）。

钉住的东西（T8 验收项，逐条对应）：

1. **正常一轮**：没信号 → 什么也不做；有信号 → 开仓 + 挂止损 + 落库 + 记权益，
   且**交易所收到的单子**每一个整数都对（`qty_int` / `trigger_price_int` / coi）。
2. **同 bar 重跑不重复下单**：第二次 tick 只产出 `same_bar_already_traded`，
   交易所的 `place_entry` / `place_stop` 调用计数**仍然是 1**。
3. **对账四类不一致全部能发现**：本地有仓交易所没有 / 交易所有仓本地没有 /
   数量不符 / 止损单丢失 —— 发现后**阻断新开仓**（另一个标的有信号也不发单）。
4. **对账不自动改写本地状态**：`reconcile()` 前后 `positions` 表逐字段相同、
   `events` 行数不变。
5. **异常不带着不确定状态继续**：下单抛异常 → 落 `events` + 本轮 `status='error'`
   + 后面的标的一个都不再处理；源码里没有「静默吞异常」（AST 静态检查）。
6. **连续失败熔断**：达到阈值后只挡开仓、不挡离场，且 `reset_circuit()` 是显式复位。
7. **止损成交不误报**：本地有仓 / 交易所已平 / 止损单也不在了 → 对账给 `grace`
   （不阻断），执行器用 `record_stop` 结案并落 `stop_fill_settled`。
8. **实盘 tick 用的信号是冻结引擎的**：`pending_signal()` 与 `strategy.scan()` 在
   重叠区间上逐字段相同。

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
from decimal import Decimal

import app.executor as executor
import app.strategy as strategy
from app.exchange import client_order_index
from app.executor import (BLOCK_CIRCUIT_OPEN, BLOCK_RECONCILE, ISSUE_ORDERS_UNAVAILABLE,
                          ISSUE_POSITION_MISSING, ISSUE_POSITION_UNKNOWN,
                          ISSUE_QTY_MISMATCH, ISSUE_SIDE_MISMATCH, ISSUE_STOP_ORDER_MISSING,
                          ExchangeAccount, ExchangeOrder, ExchangePosition,
                          LighterAccountReader, ReconcileIssue, ReaderError, reconcile)
from app.lifecycle import ACTION_ENTRY, ACTION_HOLD, ACTION_SKIP, ACTION_STOP
from app.market import BarsResult
from app.risk import RiskLimits
from app.sizing import SizingConfig
from app.store import StateStore
from app.symbol_registry import spec
from app.exchange import client_order_index as coi_of
from tests.test_strategy_port import plateau_breakout_bars

BAR_MS = 4 * 3600 * 1000
T0 = 1_600_000_000_000
NOW_S = 1_760_000_000.0

BTC = "BTC"
BTC_INFO = spec(BTC).as_market_info()          # pdec=1 / sdec=5
ETH_INFO = spec("ETH").as_market_info()

# 冻结引擎在 `plateau_breakout_bars()` 上的第一个信号：bar 420（做多）。
# 手算参考（equity=10000、risk_pct=1.33%、BTC）：
#   入场参考价 = bars[421].open = 104.2
#   止损       = ma_min@419 → 对齐到 1 位小数（多单 ceil）= 100.1 → stop_price_int = 1001
#   数量       = 10000 × 1.33% ÷ (104.2 − 100.1) = 32.43902… → floor 到 5 位 = 3243902
FULL = plateau_breakout_bars()
SIGNAL_BARS = FULL[:421]                       # 最后一根已收盘 bar = 420 = 信号 bar
NEXT_BAR = FULL[421]                           # 正在形成的 bar：open = 入场参考价
ENTRY_REF = 104.2
PLAN_QTY_INT = 3_243_902
ENTRY_PRICE_INT = 1042                         # to_int_price(104.2, 1)
STOP_PRICE_INT = 1001                          # to_int_price(100.1, 1)


# --------------------------------------------------------------------------- 夹具


def bars_result(bars, *, symbol=BTC, market_id=1, unclosed=()):
    """把一个 bar 列表包成 `BarsResult`（执行器只读 `.bars` / `.dropped_unclosed`）。"""
    last = bars[-1]
    return BarsResult(symbol=symbol, market_id=market_id, bars=tuple(bars),
                      dropped_unclosed=tuple(unclosed),
                      last_close_age_ms=60_000, now_ms=last.open_ms + BAR_MS + 60_000)


def quiet_bars(n=400):
    """n 根平盘 4h K 线（无信号；n ≥ 120 才不算 warmup）。"""
    return [strategy.Bar(open_ms=T0 + k * BAR_MS, open=100.0, high=100.5, low=99.5,
                         close=100.0) for k in range(n)]


def stop_hit_bars(n=380, level=110.0, low=104.0, close=106.0):
    """n-1 根 110 平盘 + 最后一根插到 104（止损 105 被打穿）。

    n 必须 ≥ `strategy.MIN_BARS_REQUIRED`（376），否则执行器会判 warmup 而不决策。

    止损优先于信号离场 → `decide_exit()` 返回 `action='stop'`、`exit_price=105.0`。
    """
    bars = [strategy.Bar(open_ms=T0 + k * BAR_MS, open=level, high=level + 0.2,
                         low=level - 0.2, close=level) for k in range(n - 1)]
    bars.append(strategy.Bar(open_ms=T0 + (n - 1) * BAR_MS, open=level, high=level + 0.2,
                             low=low, close=close))
    return bars


class FakeMarket:
    """`MarketClient` 的最小替身：`bars()` / `market_details()`。"""

    def __init__(self, feeds):
        self.feeds = dict(feeds)
        self.calls = []
        self.fail_on = set()

    def bars(self, symbol, *, now_ms=None, **kwargs):
        self.calls.append(symbol)
        if symbol in self.fail_on:
            raise RuntimeError(f"feed exploded for {symbol}")
        return self.feeds[symbol]

    def market_details(self, *, force=False):
        return {sym: (BTC_INFO if sym == BTC else ETH_INFO) for sym in self.feeds}


class FakeExchange:
    """`ExchangeClient` 的最小替身 —— 但**会记账**：下单会改自己的持仓/挂单表。

    这样"交易所侧"就是执行器行为的镜像，所以正常一轮之后对账是干净的；
    要制造不一致就在测试里直接改 `positions` / `orders`（等于强平 / 手动平仓 / 撤单）。
    """

    def __init__(self, *, dry_run=False, confirmed=True, reject=None):
        self.dry_run = bool(dry_run)
        self.confirmed = bool(confirmed)
        self.reject = dict(reject or {})       # {'entry'|'stop'|'exit': OrderResult}
        self.raise_on = set()                  # {'entry'} → place_entry 抛异常
        self.entries = []
        self.stops = []
        self.exits = []
        self.positions = {}                    # symbol -> {'side','qty_int','market_id'}
        self.orders = []                       # ExchangeOrder（只放会挂着的单）
        self.wait_calls = []

    # -- 下单 ---------------------------------------------------------------
    def _result(self, action, *, symbol="", **extra):
        from app.exchange import OrderResult
        return OrderResult(ok=True, action=action, code=200, symbol=symbol,
                           dry_run=self.dry_run, confirmed=self.confirmed,
                           idempotency_key=f"fake:{action}", **extra)

    async def place_entry(self, plan, *, bar_ms=None, client_order_index=None, **kw):
        if "entry" in self.raise_on:
            raise RuntimeError("boom in place_entry")
        if "entry" in self.reject:
            return self.reject["entry"]
        coi = coi_of(plan.symbol, int(bar_ms), "entry")
        self.entries.append({"symbol": plan.symbol, "side": plan.side,
                             "qty_int": plan.qty_int, "market_id": plan.market_id,
                             "bar_ms": int(bar_ms), "coi": coi})
        if not self.dry_run:            # dry-run 什么也没发 → 交易所侧不产生仓位
            self.positions[plan.symbol] = {"side": int(plan.side),
                                           "qty_int": int(plan.qty_int),
                                           "market_id": int(plan.market_id),
                                           "size_decimals": int(plan.size_decimals)}
        return self._result("entry", symbol=plan.symbol, market_id=int(plan.market_id),
                            side=int(plan.side), qty_int=int(plan.qty_int),
                            client_order_index=coi, tx_hash=f"0xentry{int(bar_ms)}")

    async def place_stop(self, plan, *, bar_ms=None, client_order_index=None, **kw):
        if "stop" in self.reject:
            return self.reject["stop"]
        coi = coi_of(plan.symbol, int(bar_ms), "stop")
        self.stops.append({"symbol": plan.symbol, "trigger_price_int": plan.stop_price_int,
                           "side": int(plan.side), "qty_int": plan.qty_int,
                           "reduce_only": True, "coi": coi, "bar_ms": int(bar_ms)})
        if not self.dry_run:
            self.orders.append(ExchangeOrder(
                order_id=f"stop-{coi}", client_order_index=coi,
                market_id=int(plan.market_id), symbol=plan.symbol, reduce_only=True,
                trigger_price_int=int(plan.stop_price_int), qty_int=int(plan.qty_int),
                status="open"))
        return self._result("stop", symbol=plan.symbol, market_id=int(plan.market_id),
                            trigger_price_int=int(plan.stop_price_int),
                            client_order_index=coi, reduce_only=True,
                            tx_hash=f"0xstop{int(bar_ms)}")

    async def place_exit(self, *, symbol, market_id, side, qty_int, price_decimals,
                         exit_price, bar_ms=None, client_order_index=None):
        if "exit" in self.reject:
            return self.reject["exit"]
        self.exits.append({"symbol": symbol, "side": int(side), "qty_int": int(qty_int),
                           "exit_price": float(exit_price), "bar_ms": int(bar_ms)})
        self.positions.pop(symbol, None)
        self.orders = [o for o in self.orders if o.symbol != symbol]
        return self._result("exit", symbol=symbol, market_id=int(market_id),
                            side=int(side), qty_int=int(qty_int), reduce_only=True,
                            tx_hash=f"0xexit{int(bar_ms)}")

    async def wait_for_tx(self, tx_hash, **kw):
        self.wait_calls.append(tx_hash)
        return {"ok": True, "code": 200, "error": "", "status": 2, "confirmed": True}

    # -- 交易所侧读数 -------------------------------------------------------
    def as_positions(self):
        out = []
        for symbol, pos in self.positions.items():
            sdec = int(pos.get("size_decimals", 5))
            qty = Decimal(int(pos["qty_int"])).scaleb(-sdec)
            out.append(ExchangePosition(symbol=symbol, side=int(pos["side"]), qty=qty,
                                        qty_str=str(qty), market_id=pos.get("market_id")))
        return tuple(out)


class FakeReader:
    """`AccountReader`：从 `FakeExchange` 读（所以正常一轮后两侧一致）。"""

    def __init__(self, exchange, *, equity=10_000.0, orders_error="", positions=None,
                 orders=None):
        self.exchange = exchange
        self.equity = equity
        self.orders_error = orders_error
        self._positions = positions
        self._orders = orders
        self.reads = 0

    async def read(self):
        self.reads += 1
        positions = (self._positions if self._positions is not None
                     else self.exchange.as_positions())
        orders = (self._orders if self._orders is not None else tuple(self.exchange.orders))
        return ExchangeAccount(equity=self.equity, positions=tuple(positions),
                               orders=tuple(orders) if not self.orders_error else (),
                               orders_error=self.orders_error, as_of=NOW_S)


class FakeClock:
    """单调递增的假时钟（每次 +1s），避免同一 ts 覆盖权益点。"""

    def __init__(self, start=NOW_S):
        self.now = float(start)

    def __call__(self):
        self.now += 1.0
        return self.now


class Base(unittest.TestCase):
    """每个用例一个临时库 + 一套替身。"""

    symbols = (BTC,)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "executor.db")
        self.store = StateStore(self.db)
        self.addCleanup(self.store.close)
        # 回撤状态（T9 `dd.py` 在实盘里每轮写；没有它 risk 的 DD 闸会 fail-closed 拒单）
        self.store.save_dd_state(twr_index=1.0, twr_peak=1.0,
                                 anchor_ts=NOW_S - 3600, anchor_equity=10_000.0)
        self.exchange = FakeExchange()
        self.reader = FakeReader(self.exchange)
        self.market = FakeMarket({BTC: bars_result(SIGNAL_BARS, unclosed=(NEXT_BAR,))})
        self.alerts = []
        self.clock = FakeClock()
        self.executor = self.make_executor()

    def make_executor(self, **kwargs):
        symbols = kwargs.pop("symbols", self.symbols)
        params = dict(store=self.store, market=self.market, exchange=self.exchange,
                      reader=self.reader, symbols=symbols,
                      limits=RiskLimits(), sizing_config=SizingConfig(),
                      alert=lambda kind, text: self.alerts.append((kind, text)),
                      clock=self.clock)
        params.update(kwargs)
        return executor.Executor(**params)

    # -- 便捷 ---------------------------------------------------------------
    def run_tick(self, ex=None, **kwargs):
        import asyncio
        return asyncio.run((ex or self.executor).run_once(now_ms=self.now_ms()))

    def now_ms(self):
        return SIGNAL_BARS[-1].open_ms + BAR_MS + 60_000

    def seed_position(self, *, symbol=BTC, side=1, qty_int=PLAN_QTY_INT,
                      entry_price_int=1120, stop_price_int=900, price_decimals=1,
                      size_decimals=5, bar_ms=None, market_id=1, stop_order_id=None,
                      state=None):
        """直接写一行活仓（越过下单），用于制造对账场景。"""
        bar = int(bar_ms if bar_ms is not None else SIGNAL_BARS[-1].open_ms - BAR_MS)
        result = self.store.record_entry(
            symbol, side, qty_int=qty_int, size_decimals=size_decimals,
            entry_price_int=entry_price_int, price_decimals=price_decimals,
            stop_price_int=stop_price_int, bar_ms=bar, market_id=market_id,
            entry_order_id=f"coi:{client_order_index(symbol, bar, 'entry')}",
            stop_order_id=(stop_order_id if stop_order_id is not None
                           else f"coi:{client_order_index(symbol, bar, 'stop')}"),
            equity_at_entry=10_000.0)
        if state is not None:
            self.store.set_position_state(int(result.position_id), state)
        return self.store.position(int(result.position_id))

    def events(self, kind=None):
        return self.store.events(limit=200, kind=kind)


# --------------------------------------------------------------------------- 信号口径


class SignalParityTests(unittest.TestCase):
    def test_pending_signal_matches_scan_on_the_same_bar(self):
        """执行器判信号用的是**冻结引擎的同一个函数**（`evaluate_entry`），不是重写。"""
        ind = strategy.compute_indicators(FULL)
        # scan() 只能在"有下一根"时产出信号，所以拿 FULL[:422] 扫（bar 420 有后继）
        scanned = [s for s in strategy.scan(FULL[:422], BTC) if s.bar_index == 420]
        self.assertEqual(len(scanned), 1)
        mine = executor.pending_signal(BTC, FULL[:422], ind, 420, NEXT_BAR.open)
        self.assertIsNotNone(mine)
        for name in ("bar_index", "bar_open_ms", "entry_price", "stop_price",
                     "stop_dist", "ma_max", "ma_min", "band", "side"):
            self.assertEqual(getattr(mine, name), getattr(scanned[0], name), name)
        self.assertEqual((mine.bar_index, mine.side), (420, 1))
        self.assertAlmostEqual(mine.entry_price, ENTRY_REF, places=10)

    def test_scanned_signal_is_not_produced_for_the_last_bar(self):
        """`scan()` 的循环上界是 len-1 —— 结构上不产出最后一根，故实盘必须用 evaluate_entry。"""
        self.assertNotIn(420, [s.bar_index for s in strategy.scan(FULL[:421], BTC)])
        self.assertEqual(len(SIGNAL_BARS), 421)                  # 最后一根 = bar 420


# --------------------------------------------------------------------------- 正常一轮


class NormalTickTests(Base):
    def test_no_signal_does_nothing(self):
        self.market.feeds[BTC] = bars_result(quiet_bars(400))
        report = self.run_tick()
        self.assertEqual(report.status, "ok")
        self.assertTrue(report.ok)
        self.assertEqual(report.outcomes[0].action, ACTION_SKIP)
        self.assertIn("no_signal", report.outcomes[0].decision["codes"])
        self.assertEqual(self.exchange.entries, [])
        self.assertEqual(self.exchange.stops, [])
        self.assertEqual(self.store.open_positions(), [])
        self.assertEqual(self.store.trade_count(), 0)

    def test_signal_opens_position_records_it_and_the_exchange_gets_the_order(self):
        report = self.run_tick()
        self.assertEqual(report.status, "ok", str(report))
        outcome = report.outcomes[0]
        self.assertEqual(outcome.action, ACTION_ENTRY)
        self.assertTrue(outcome.filled)
        self.assertTrue(outcome.ok)

        # ① 交易所收到的单子：每个整数都写死
        self.assertEqual(len(self.exchange.entries), 1)
        entry = self.exchange.entries[0]
        self.assertEqual((entry["symbol"], entry["side"]), (BTC, 1))
        self.assertEqual(entry["qty_int"], PLAN_QTY_INT)              # 32.43902 @ 5 位
        self.assertEqual(entry["market_id"], 1)
        self.assertEqual(entry["bar_ms"], SIGNAL_BARS[-1].open_ms)     # 信号 bar
        self.assertEqual(entry["coi"], client_order_index(BTC, SIGNAL_BARS[-1].open_ms,
                                                          "entry"))
        self.assertEqual(len(self.exchange.stops), 1)
        stop = self.exchange.stops[0]
        self.assertEqual(stop["trigger_price_int"], STOP_PRICE_INT)     # 100.1
        self.assertTrue(stop["reduce_only"])
        self.assertEqual(stop["qty_int"], PLAN_QTY_INT)

        # ② DB 有记录（仓位 + 事件）
        rows = self.store.open_positions()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], BTC)
        self.assertEqual(row["state"], "OPEN")
        self.assertEqual(row["side"], 1)
        self.assertEqual(row["qty_int"], PLAN_QTY_INT)
        self.assertEqual(row["size_decimals"], 5)
        self.assertEqual(row["entry_price_int"], ENTRY_PRICE_INT)       # 104.2 → 1042
        self.assertEqual(row["stop_price_int"], STOP_PRICE_INT)
        self.assertEqual(row["bar_ms"], SIGNAL_BARS[-1].open_ms)
        self.assertEqual(row["entry_order_id"],
                         f"coi:{client_order_index(BTC, row['bar_ms'], 'entry')}")
        self.assertEqual(row["stop_order_id"],
                         f"coi:{client_order_index(BTC, row['bar_ms'], 'stop')}")
        self.assertEqual(self.store.trade_count(), 1)                   # 开仓腿
        self.assertEqual(len(self.events("entry")), 1)
        self.assertEqual(len(self.events("transition")), 1)

        # ③ 权益落库（DD / 面板的输入）
        last = self.store.last_equity()
        self.assertIsNotNone(last)
        self.assertAlmostEqual(last["equity"], 10_000.0, places=6)
        self.assertEqual(last["source"], "exchange")

        # ④ 对账是干净的（交易所有仓、有止损单、数量一致）
        self.assertIsNotNone(report.reconcile)
        self.assertTrue(report.reconcile.ok, str(report.reconcile))
        self.assertFalse(report.reconcile.has_blockers)

    def test_same_bar_rerun_does_not_place_a_second_order(self):
        first = self.run_tick()
        self.assertEqual(first.outcomes[0].action, ACTION_ENTRY)
        second = self.run_tick()
        # 已有活仓 → 走离场分支（本根不动），关键断言是"没有第二张单"
        self.assertIn(second.outcomes[0].action, (ACTION_HOLD, ACTION_SKIP))
        self.assertEqual(len(self.exchange.entries), 1)
        self.assertEqual(len(self.exchange.stops), 1)
        self.assertEqual(len(self.store.open_positions()), 1)
        self.assertEqual(self.store.trade_count(), 1)
        # 对账仍然干净（第二次 tick 没有制造任何新状态）
        self.assertTrue(second.reconcile.ok, str(second.reconcile))

    def test_same_bar_after_the_position_closed_is_still_not_reopened(self):
        """仓在同一根 bar 内已平 → 再跑一遍必须被 `same_bar_already_traded` 挡住。"""
        self.run_tick()
        row = self.store.open_positions()[0]
        self.store.record_stop(int(row["id"]), exit_price_int=1001, price_decimals=1,
                               reason="stop")
        self.exchange.positions = {}
        self.exchange.orders = []
        report = self.run_tick()
        self.assertEqual(report.outcomes[0].action, ACTION_SKIP)
        self.assertIn("same_bar_already_traded", report.outcomes[0].decision["codes"])
        self.assertEqual(len(self.exchange.entries), 1)      # 没有第二次开仓
        self.assertEqual(len(self.store.open_positions()), 0)

    def test_hold_places_nothing(self):
        self.seed_position()
        report = self.run_tick()
        self.assertEqual(report.outcomes[0].action, ACTION_HOLD)
        self.assertEqual(self.exchange.entries, [])
        self.assertEqual(self.exchange.exits, [])

    def test_equity_is_recorded_on_every_tick(self):
        self.reader.equity = 12_345.67
        self.run_tick()
        self.run_tick()
        self.assertEqual(self.store.equity_point_count(), 2)
        self.assertAlmostEqual(self.store.last_equity()["equity"], 12_345.67, places=6)

    def test_dry_run_records_paper_positions_without_blocking_itself(self):
        """dry-run 下本地会记仓而交易所始终为空：照常报警，但不阻断（否则模拟盘被锁死）。"""
        self.exchange.dry_run = True
        first = self.run_tick()
        self.assertEqual(first.outcomes[0].action, ACTION_ENTRY)
        self.assertEqual(len(self.store.open_positions()), 1)
        # 第二轮：本地有仓、交易所没有 → 报告里必须有 grace/danger，但不阻断
        second = self.run_tick()
        self.assertIn(ISSUE_POSITION_MISSING, second.reconcile.codes)
        self.assertNotIn(BLOCK_RECONCILE, second.block_reasons)
        self.assertTrue(any(k == "reconcile_mismatch" for k, _ in self.alerts))


# --------------------------------------------------------------------------- 对账


class ReconcileDetectionTests(Base):
    """四类必须发现的不一致 + 阻断。"""

    def test_local_position_missing_on_exchange_is_detected_and_blocks_entries(self):
        # 本地有 BTC 活仓；交易所侧"强平"了仓，但止损单还挂着 → danger + blocker
        row = self.seed_position(bar_ms=SIGNAL_BARS[-1].open_ms - BAR_MS)
        self.exchange.orders = [ExchangeOrder(
            order_id="stop-x", client_order_index=int(row["stop_order_id"][4:]),
            market_id=1, symbol=BTC, reduce_only=True,
            trigger_price_int=int(row["stop_price_int"]))]
        report = self.run_tick()

        rec = report.reconcile
        self.assertIn(ISSUE_POSITION_MISSING, rec.codes)
        issue = rec.by_code(ISSUE_POSITION_MISSING)[0]
        self.assertTrue(issue.blocker)
        self.assertEqual(issue.severity, "danger")
        self.assertEqual(issue.detail["evidence"], "stop_order_still_active")
        # 报警 + 落库（只报警）
        self.assertTrue(any(k == "reconcile_mismatch" for k, _ in self.alerts))
        self.assertEqual(len(self.events("reconcile_mismatch")), 1)
        # 本地状态**没被改**：仓位还是 OPEN
        self.assertEqual(self.store.position(int(row["id"]))["state"], "OPEN")

    def test_exchange_position_unknown_locally_is_detected(self):
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 100_000, "market_id": 1}
        report = self.run_tick()
        self.assertIn(ISSUE_POSITION_UNKNOWN, report.reconcile.codes)
        issue = report.reconcile.by_code(ISSUE_POSITION_UNKNOWN)[0]
        self.assertTrue(issue.blocker)
        self.assertEqual(issue.detail["exchange_side"], 1)
        # 交易所的"凭空持仓"绝不会被本地补记
        self.assertEqual(self.store.open_positions(), [])
        self.assertEqual(len(self.events("reconcile_mismatch")), 1)

    def test_qty_mismatch_is_detected(self):
        row = self.seed_position()
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 2_000_000, "market_id": 1,
                                        "size_decimals": 5}
        report = self.run_tick()
        self.assertIn(ISSUE_QTY_MISMATCH, report.reconcile.codes)
        issue = report.reconcile.by_code(ISSUE_QTY_MISMATCH)[0]
        self.assertTrue(issue.blocker)
        self.assertEqual(issue.detail["local_qty_int"], PLAN_QTY_INT)
        self.assertEqual(issue.detail["exchange_qty"], "20.00000")
        # 数量没被"修正"成本地或交易所的口径
        self.assertEqual(self.store.position(int(row["id"]))["qty_int"], PLAN_QTY_INT)

    def test_side_mismatch_is_detected(self):
        self.seed_position(side=1)
        self.exchange.positions[BTC] = {"side": -1, "qty_int": PLAN_QTY_INT,
                                        "market_id": 1}
        report = self.run_tick()
        self.assertIn(ISSUE_SIDE_MISMATCH, report.reconcile.codes)
        self.assertTrue(report.reconcile.by_code(ISSUE_SIDE_MISMATCH)[0].blocker)

    def test_missing_stop_order_is_detected(self):
        self.seed_position()
        self.exchange.positions[BTC] = {"side": 1, "qty_int": PLAN_QTY_INT,
                                        "market_id": 1, "size_decimals": 5}
        self.exchange.orders = []                     # 止损单没了
        report = self.run_tick()
        self.assertIn(ISSUE_STOP_ORDER_MISSING, report.reconcile.codes)
        issue = report.reconcile.by_code(ISSUE_STOP_ORDER_MISSING)[0]
        self.assertTrue(issue.blocker)
        self.assertEqual(issue.detail["stop_price_int"], 900)
        # 不会自作主张补挂止损单
        self.assertEqual(self.exchange.stops, [])

    def test_orders_unavailable_blocks_entries(self):
        """挂单列表读不到 → 无法证明止损还在 → fail-closed。"""
        self.reader.orders_error = "ReaderError: 401 unauthorized"
        report = self.run_tick()
        self.assertIn(ISSUE_ORDERS_UNAVAILABLE, report.reconcile.codes)
        self.assertTrue(report.reconcile.by_code(ISSUE_ORDERS_UNAVAILABLE)[0].blocker)

    def test_mismatch_blocks_a_new_entry_on_a_different_symbol(self):
        """BTC 对账不一致 → 同一轮里 ETH 即使有信号也不下单。"""
        self.symbols = (BTC, "ETH")
        self.seed_position()                                    # 本地 BTC 活仓
        # 交易所侧数量不同 → 数量不一致（两侧都有行，名义算得出来，风控闸不会抢先拒）
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 2_000_000, "market_id": 1,
                                        "size_decimals": 5}
        self.market.feeds["ETH"] = bars_result(SIGNAL_BARS, symbol="ETH", market_id=2,
                                               unclosed=(NEXT_BAR,))
        ex = self.make_executor(symbols=(BTC, "ETH"))
        report = self.run_tick(ex)

        self.assertIn(ISSUE_QTY_MISMATCH, report.reconcile.codes)
        self.assertEqual(report.status, "blocked")
        self.assertEqual(report.blocked_entries, ("ETH",))
        self.assertIn(BLOCK_RECONCILE, report.block_reasons)
        eth = [o for o in report.outcomes if o.symbol == "ETH"][0]
        self.assertEqual(eth.action, ACTION_SKIP)
        self.assertIn(BLOCK_RECONCILE, eth.decision["codes"])
        self.assertEqual(self.exchange.entries, [])          # 一张单都没发
        # 本地也没记 ETH（只有测试自己种下的 BTC 那一行）
        self.assertEqual([r["symbol"] for r in self.store.open_positions()], [BTC])

    def test_reconcile_never_writes_to_the_store(self):
        """验收项 4 的机械保证：`reconcile()` 前后 positions/events 完全一致。"""
        self.seed_position()
        self.exchange.orders = []                       # 制造 3 类不一致
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 7, "market_id": 1}
        before_positions = [dict(r) for r in self.store.positions()]
        before_events = self.store.event_count()
        before_trades = self.store.trade_count()

        account = ExchangeAccount(equity=1.0, positions=self.exchange.as_positions(),
                                  orders=(), orders_error="", as_of=NOW_S)
        report = executor.reconcile(local_positions=self.store.open_positions(),
                                    account=account, now=NOW_S)
        self.assertGreaterEqual(len(report.issues), 2)
        self.assertTrue(report.has_blockers)
        self.assertEqual([dict(r) for r in self.store.positions()], before_positions)
        self.assertEqual(self.store.event_count(), before_events)
        self.assertEqual(self.store.trade_count(), before_trades)
        # 纯函数：连 store 都不该出现在签名里
        self.assertEqual(set(inspect.signature(reconcile).parameters),
                         {"local_positions", "account", "now", "stop_tolerance_ticks"})

    def test_stop_fill_is_grace_not_mismatch_and_gets_settled(self):
        """本地有仓 / 交易所已平 / 止损单也不在了 → grace（不误报），并由 record_stop 结案。"""
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        row = self.seed_position(entry_price_int=1120, stop_price_int=1050,
                                 bar_ms=T0, qty_int=415600)
        self.exchange.orders = []          # 止损单已成交 → 不在挂单里
        self.exchange.positions = {}       # 交易所已平
        report = self.run_tick()

        issue = report.reconcile.by_code(ISSUE_POSITION_MISSING)[0]
        self.assertEqual(issue.severity, "grace")
        self.assertFalse(issue.blocker)
        self.assertFalse(report.reconcile.has_blockers)
        # 决策是止损 → 用"交易所有证据"结案，而不是再发一张平仓单
        outcome = report.outcomes[0]
        self.assertEqual(outcome.action, ACTION_STOP)
        self.assertTrue(outcome.settled_stop_fill)
        self.assertEqual(self.exchange.exits, [])
        closed = self.store.position(int(row["id"]))
        self.assertEqual(closed["state"], "CLOSED")
        self.assertEqual(closed["exit_reason"], "stop_fill")
        self.assertEqual(closed["exit_price_int"], 1050)
        self.assertIn("stop_fill_settled", [e["kind"] for e in self.events()])
        # 止损腿成交已入账
        kinds = sorted(t["kind"] for t in self.store.trades(limit=10))
        self.assertEqual(kinds, ["entry", "stop"])

    def test_stop_with_no_exchange_evidence_falls_back_to_a_reduce_only_exit(self):
        """交易所有仓但止损单丢了 → 不能"结案"，只能发 reduce_only 平仓单。"""
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        row = self.seed_position(entry_price_int=1120, stop_price_int=1050,
                                 bar_ms=T0, qty_int=415600)
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        self.exchange.orders = []
        report = self.run_tick()
        self.assertEqual(report.outcomes[0].action, ACTION_STOP)
        self.assertFalse(report.outcomes[0].settled_stop_fill)
        self.assertEqual(len(self.exchange.exits), 1)
        self.assertEqual(self.exchange.exits[0]["qty_int"], 415600)
        self.assertEqual(self.store.position(int(row["id"]))["state"], "CLOSED")


class ReconcileUnitTests(unittest.TestCase):
    """`reconcile()` 的边界：容差、市场过滤、零持仓。"""

    def row(self, **over):
        base = dict(id=1, symbol=BTC, side=1, state="OPEN", qty_int=415600,
                    size_decimals=5, market_id=1, stop_price_int=1050,
                    stop_order_id=f"coi:{client_order_index(BTC, T0, 'stop')}",
                    price_decimals=1)
        base.update(over)
        return base

    def test_half_tick_tolerance(self):
        account = ExchangeAccount(positions=(
            ExchangePosition(symbol=BTC, side=1, qty=Decimal("4.156"), market_id=1),),
            orders=(ExchangeOrder(order_id="s", client_order_index=None, market_id=1,
                                  reduce_only=True, trigger_price_int=1050),))
        self.assertTrue(reconcile(local_positions=[self.row()], account=account).ok)
        # 差 2 个 tick（0.00002）→ 必须报
        off = ExchangeAccount(positions=(
            ExchangePosition(symbol=BTC, side=1, qty=Decimal("4.15602"), market_id=1),),
            orders=account.orders)
        self.assertIn(ISSUE_QTY_MISMATCH, reconcile(local_positions=[self.row()],
                                                    account=off).codes)

    def test_stop_matched_by_trigger_price_with_one_tick_tolerance(self):
        account = ExchangeAccount(positions=(
            ExchangePosition(symbol=BTC, side=1, qty=Decimal("4.156"), market_id=1),),
            orders=(ExchangeOrder(order_id="s", client_order_index=None, market_id=1,
                                  reduce_only=True, trigger_price_int=1049),))
        report = reconcile(local_positions=[self.row(stop_order_id=None)], account=account)
        self.assertTrue(report.ok, str(report))

    def test_non_reduce_only_order_is_not_a_stop(self):
        account = ExchangeAccount(positions=(
            ExchangePosition(symbol=BTC, side=1, qty=Decimal("4.156"), market_id=1),),
            orders=(ExchangeOrder(order_id="e", client_order_index=None, market_id=1,
                                  reduce_only=False, trigger_price_int=1050),))
        self.assertIn(ISSUE_STOP_ORDER_MISSING,
                      reconcile(local_positions=[self.row(stop_order_id=None)],
                                account=account).codes)

    def test_zero_exchange_position_rows_are_dropped(self):
        payload = {"code": 200, "accounts": [{"index": 7, "positions": [
            {"market_id": 1, "symbol": "BTC", "sign": 1, "position": "0",
             "avg_entry_price": "0"},
            {"market_id": 2, "symbol": "ETH", "sign": -1, "position": "1.5",
             "avg_entry_price": "2000.0"}]}]}
        positions = executor.positions_from_payload(payload, account_index=7)
        self.assertEqual([p.symbol for p in positions], ["ETH"])
        self.assertEqual((positions[0].side, positions[0].qty), (-1, Decimal("1.5")))


# --------------------------------------------------------------------------- 异常 / 熔断


class FailureHandlingTests(Base):
    def test_unexpected_exception_is_recorded_and_stops_the_round(self):
        """BTC 下单抛异常 → 落 events + status='error' + ETH 一个都不再处理。"""
        self.market.feeds["ETH"] = bars_result(SIGNAL_BARS, symbol="ETH", market_id=2,
                                               unclosed=(NEXT_BAR,))
        self.exchange.raise_on = {"entry"}
        ex = self.make_executor(symbols=(BTC, "ETH"))
        report = self.run_tick(ex)

        self.assertEqual(report.status, "error")
        self.assertIn("RuntimeError", report.error)
        self.assertIn("boom in place_entry", report.error)
        # 留痕：executor_error + tick_aborted 都在库里
        self.assertEqual(len(self.events("executor_error")), 1)
        self.assertEqual(len(self.events("tick_aborted")), 1)
        # 停止本轮：ETH 没有 outcome（根本没被处理），也没发单
        self.assertEqual([o.symbol for o in report.outcomes], [BTC])
        self.assertFalse(report.outcomes[0].ok)
        self.assertEqual(self.exchange.entries, [])
        self.assertEqual(self.store.open_positions(), [])
        # 失败被计数
        self.assertEqual(report.failures, 1)

    def test_feed_exception_aborts_before_any_decision(self):
        self.market.fail_on = {"ETH"}
        self.market.feeds["ETH"] = bars_result(SIGNAL_BARS, symbol="ETH", market_id=2)
        ex = self.make_executor(symbols=(BTC, "ETH"))
        report = self.run_tick(ex)
        self.assertEqual(report.status, "error")
        self.assertIn("feed exploded for ETH", report.error)
        self.assertEqual(report.outcomes, ())
        self.assertEqual(self.exchange.entries, [])

    def test_consecutive_failures_open_the_circuit_and_only_block_entries(self):
        from app.exchange import OrderResult
        reject = OrderResult(ok=False, action="entry", code=400, kind="rejected",
                             error="insufficient margin")
        self.exchange.reject["entry"] = reject
        ex = self.make_executor(max_consecutive_failures=2)
        first = self.run_tick(ex)
        self.assertEqual(first.status, "ok")          # 单次失败只算失败，不改变 tick 结论
        self.assertEqual(first.failures, 1)
        self.assertFalse(ex.circuit_open)
        second = self.run_tick(ex)
        self.assertEqual(second.failures, 2)
        self.assertTrue(ex.circuit_open)
        self.assertEqual(len(self.events("circuit_open")), 1)
        self.assertTrue(any(k == "circuit_open" for k, _ in self.alerts))

        # 熔断后：连 place_entry 都不再被调用（但本轮照样跑：读数据/记权益/管仓位）
        third = self.run_tick(ex)
        self.assertTrue(third.circuit_open)
        self.assertEqual(third.status, "blocked")
        self.assertIn(BLOCK_CIRCUIT_OPEN, third.block_reasons)
        self.assertEqual(third.blocked_entries, (BTC,))
        self.assertEqual(len(self.exchange.entries), 0)
        self.assertIn(BLOCK_CIRCUIT_OPEN, third.outcomes[0].decision["codes"])

        # 显式复位后恢复尝试
        ex.reset_circuit(reason="test")
        self.assertFalse(ex.circuit_open)
        self.assertEqual(len(self.events("circuit_reset")), 1)

    def test_circuit_does_not_block_an_exit(self):
        """熔断只挡开仓：有仓要平照样平（否则亏损仓被锁死）。"""
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        row = self.seed_position(entry_price_int=1120, stop_price_int=1050,
                                 bar_ms=T0, qty_int=415600)
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        self.exchange.orders = []            # 止损单丢了，必须发 reduce_only 平仓
        ex = self.make_executor(max_consecutive_failures=1)
        ex._circuit_open = True              # 手工置熔断
        report = self.run_tick(ex)
        self.assertTrue(report.circuit_open)
        self.assertEqual(report.outcomes[0].action, ACTION_STOP)
        self.assertEqual(len(self.exchange.exits), 1)
        self.assertEqual(self.store.position(int(row["id"]))["state"], "CLOSED")

    def test_rejected_stop_rolls_the_position_back_and_reports(self):
        from app.exchange import OrderResult
        self.exchange.reject["stop"] = OrderResult(
            ok=False, action="stop", code=400, kind="rejected", error="stop rejected")
        report = self.run_tick()
        outcome = report.outcomes[0]
        self.assertEqual(outcome.action, ACTION_ENTRY)
        self.assertFalse(outcome.ok)
        # 仓位**照样**记账（真实敞口必须留痕），但没有 stop_order_id
        rows = self.store.open_positions()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["stop_order_id"])
        self.assertIn("stop_order_failed", [e["kind"] for e in self.events()])
        self.assertEqual(report.failures, 1)

    def test_no_silent_exception_swallowing_in_source(self):
        """静态检查：没有裸 `except: pass` / `contextlib.suppress` / 空 handler。"""
        path = pathlib.Path(executor.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                body = [n for n in node.body if not isinstance(n, ast.Pass)]
                calls = [n for n in body if isinstance(n, ast.Expr)
                         and isinstance(n.value, ast.Call)]
                if node.type is None:
                    bad.append(f"bare except at line {node.lineno}")
                if not body:
                    bad.append(f"empty handler at line {node.lineno}")
                elif not calls and not any(isinstance(n, (ast.Raise, ast.Return,
                                                          ast.Assign, ast.AnnAssign))
                                           for n in body):
                    bad.append(f"handler that only continues/passes at line {node.lineno}")
        self.assertEqual(bad, [])
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("contextlib.suppress", source)
        self.assertNotIn("except: pass", source)
        self.assertNotIn("except Exception:\n            pass", source)

    def test_executor_does_not_touch_the_signer_directly(self):
        """只有 `app/exchange.py` 能碰 signer / 下单 API（T7 的边界，执行器不越界）。"""
        path = pathlib.Path(executor.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("lighter", imported)          # 不直接 import SDK
        code = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                          if not line.strip().startswith("#"))
        for banned in ("sign_create_order", "SignCreateOrder", "send_tx(",
                       "api_private_key"):
            self.assertNotIn(banned, code)

    def test_module_only_imports_stdlib_plus_the_repo(self):
        """不引入新第三方依赖（requirements.txt 不许改）。"""
        path = pathlib.Path(executor.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        allowed = {"__future__", "asyncio", "json", "logging", "time", "urllib",
                   "dataclasses", "decimal", "typing", "app", "tests"}
        self.assertEqual(roots - allowed, set(), f"unexpected imports: {sorted(roots - allowed)}")


# --------------------------------------------------------------------------- 读取器


class ReaderTests(unittest.TestCase):
    """`LighterAccountReader` 的解析（注入 http_json，全离线）。"""

    ACCOUNT = {
        "code": 200, "total": 1,
        "accounts": [{
            "index": 42, "collateral": "9000.5", "total_asset_value": "10000.25",
            "positions": [
                {"market_id": 1, "symbol": "BTC", "sign": 1, "position": "0.04156",
                 "avg_entry_price": "104.2", "position_value": "4.33"},
                {"market_id": 2, "symbol": "ETH", "sign": 0, "position": "0",
                 "avg_entry_price": "0"},
            ]}],
    }
    ORDERS = {"code": 200, "orders": [
        {"order_id": "abc", "client_order_index": 12345, "market_index": 1,
         "market_symbol": "BTC", "reduce_only": True, "trigger_price": "1001",
         "base_price": 9950, "base_size": 4156, "status": "open"},
    ]}

    def reader(self, *, auth=None, payloads=None):
        calls = []

        def http_json(url, headers=None, timeout=None):
            calls.append((url, dict(headers or {})))
            for needle, payload in (payloads or {}).items():
                if needle in url:
                    return payload
            raise AssertionError(f"unexpected url {url}")

        self.calls = calls
        return LighterAccountReader(base_url="https://api.example.test",
                                    account_index=42,
                                    auth_provider=(lambda: auth) if auth else None,
                                    http_json=http_json, clock=lambda: 123.0)

    def test_parses_positions_equity_and_orders(self):
        import asyncio
        reader = self.reader(auth="tok-1",
                             payloads={"/api/v1/accountActiveOrders": self.ORDERS,
                                       "/api/v1/account?": self.ACCOUNT})
        account = asyncio.run(reader.read())
        self.assertAlmostEqual(account.equity, 10000.25, places=6)   # 优先 total_asset_value
        self.assertEqual(len(account.positions), 1)                  # 零持仓行被丢弃
        pos = account.positions[0]
        self.assertEqual((pos.symbol, pos.side, str(pos.qty)), ("BTC", 1, "0.04156"))
        self.assertEqual(len(account.orders), 1)
        order = account.orders[0]
        self.assertEqual((order.order_id, order.client_order_index), ("abc", 12345))
        self.assertEqual(order.market_id, 1)                         # market_index → market_id
        self.assertTrue(order.reduce_only)
        self.assertEqual(order.trigger_price_int, 1001)
        self.assertEqual(account.orders_error, "")
        # 两个端点都带了 account index；只有挂单端点带 auth
        self.assertTrue(any("by=index" in url and "value=42" in url for url, _ in self.calls))
        self.assertTrue(any("accountActiveOrders?account_index=42" in url for url, _ in self.calls))
        headers = [h for url, h in self.calls if "accountActiveOrders" in url][0]
        self.assertEqual(headers.get("Authorization"), "tok-1")
        headers = [h for url, h in self.calls if "/api/v1/account?" in url][0]
        self.assertEqual(headers, {})

    def test_base_url_is_normalised_to_the_site_root(self):
        """base_url 含 `/api/v1` 时**不能**再拼一次，否则 403。

        `config.base_url` 是 `https://<host>/api/v1`（`app.market.BASE_URLS`），
        而这里的路径参数本身就带 `/api/v1/...`。直接拼会得到
        `/api/v1/api/v1/account` —— CloudFront 对重复路径回 403（实测：
        正确路径 200，重复路径 403），长得像「地域限制」，实际是路径错。
        """
        import asyncio
        seen = []

        def http_json(url, headers=None, timeout=None):
            seen.append(url)
            return self.ACCOUNT

        for base in ("https://host.test/api/v1", "https://host.test/api/v1/",
                     "https://host.test"):
            del seen[:]
            reader = LighterAccountReader(base_url=base, account_index=42,
                                          http_json=http_json)
            asyncio.run(reader.read())
            for url in seen:
                self.assertNotIn("/api/v1/api/v1", url)
                self.assertIn("/api/v1/account", url)

    def test_orders_failure_degrades_to_orders_error(self):
        import asyncio
        def http_json(url, headers=None, timeout=None):
            if "accountActiveOrders" in url:
                raise ReaderError("GET failed: HTTP 401")
            return self.ACCOUNT
        reader = LighterAccountReader(base_url="https://x.test", account_index=42,
                                      http_json=http_json)
        account = asyncio.run(reader.read())
        self.assertEqual(len(account.positions), 1)
        self.assertEqual(account.orders, ())
        self.assertIn("401", account.orders_error)

    def test_bad_payload_fails_loud(self):
        with self.assertRaises(ReaderError):
            executor.positions_from_payload({"code": 400, "message": "nope"})
        with self.assertRaises(ReaderError):
            executor.positions_from_payload({"code": 200, "accounts": []})
        with self.assertRaises(ReaderError):
            executor.orders_from_payload({"code": 200, "orders": "not-a-list"})

    def test_report_and_issue_are_json_serialisable(self):
        issue = ReconcileIssue(code=ISSUE_POSITION_UNKNOWN, symbol="BTC", message="m")
        blob = json.loads(json.dumps(issue.as_dict()))
        self.assertEqual(blob["code"], ISSUE_POSITION_UNKNOWN)
        self.assertTrue(blob["blocker"])
        report = executor.ReconcileReport(issues=(issue,))
        self.assertTrue(json.loads(json.dumps(report.as_dict()))["has_blockers"])


if __name__ == "__main__":                                        # pragma: no cover
    unittest.main()
