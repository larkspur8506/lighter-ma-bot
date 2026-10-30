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
                          LighterAccountReader, ReconcileIssue, ReaderError, match_stop_order,
                          reconcile)
from app.lifecycle import ACTION_ENTRY, ACTION_EXIT, ACTION_HOLD, ACTION_SKIP, ACTION_STOP
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
        #: `snapshot()` 用：symbol -> 实时 mid（不给 = 没有 snapshot 方法，执行器退回计划价）
        self.mids = {}
        self.snapshot_error = None
        self.snapshot_calls = []

    def snapshot(self, symbol, **kwargs):
        from types import SimpleNamespace
        self.snapshot_calls.append(symbol)
        if self.snapshot_error is not None:
            raise self.snapshot_error
        mid = self.mids.get(str(symbol).upper())
        if mid is None:
            return SimpleNamespace(symbol=symbol, mid=None, price=None, best_bid=None,
                                   best_ask=None)
        return SimpleNamespace(symbol=symbol, mid=float(mid), price=float(mid),
                               best_bid=float(mid) - 0.05, best_ask=float(mid) + 0.05)

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
        self.cancels = []                      # 单张撤单记录（止损换单用）
        self.leverage_sets = []                # ensure_isolated 调用记录
        self.margin_allocs = []                # allocate_margin 调用记录
        self.reject_leverage = False           # True → ensure_isolated 返回拒绝
        self.reject_alloc = False              # True → allocate_margin 返回拒绝
        self.wait_calls = []
        #: 2026-10-30：模拟「tx executed 但零成交」（IOC 不可成交）。
        #: `{'entry'}` → 下单成功但交易所侧不产生仓位；`{'exit'}` → 平仓成功但仓位还在。
        self.no_fill = set()
        #: 部分成交：给了就按这个数量开仓（小于 plan.qty_int）
        self.partial_entry_qty_int = None

    # -- 下单 ---------------------------------------------------------------
    def _result(self, action, *, symbol="", **extra):
        from app.exchange import OrderResult
        return OrderResult(ok=True, action=action, code=200, symbol=symbol,
                           dry_run=self.dry_run, confirmed=self.confirmed,
                           idempotency_key=f"fake:{action}", **extra)

    async def ensure_isolated(self, *, market_id: int, leverage: float):
        from app.exchange import OrderResult
        self.leverage_sets.append({"market_id": int(market_id), "leverage": float(leverage)})
        if self.reject_leverage:
            return OrderResult(ok=False, action="update_leverage", code=20001,
                               error="leverage update rejected")
        return self._result("update_leverage", market_id=int(market_id),
                            tx_hash=f"0xlev{len(self.leverage_sets)}")

    async def allocate_margin(self, *, market_id: int, usdc_amount: float):
        from app.exchange import OrderResult
        self.margin_allocs.append({"market_id": int(market_id),
                                   "usdc_amount": float(usdc_amount)})
        if self.reject_alloc:
            return OrderResult(ok=False, action="update_margin_add", code=20001,
                               error="margin allocation rejected")
        return self._result("update_margin_add", market_id=int(market_id),
                            tx_hash=f"0xalloc{len(self.margin_allocs)}")

    async def place_entry(self, plan, *, bar_ms=None, client_order_index=None, **kw):
        if "entry" in self.raise_on:
            raise RuntimeError("boom in place_entry")
        if "entry" in self.reject:
            return self.reject["entry"]
        coi = (int(client_order_index) if client_order_index is not None
               else coi_of(plan.symbol, int(bar_ms), "entry"))
        self.entries.append({"symbol": plan.symbol, "side": plan.side,
                             "qty_int": plan.qty_int, "market_id": plan.market_id,
                             "bar_ms": int(bar_ms), "coi": coi,
                             "limit_price_int": kw.get("limit_price_int")})
        if not self.dry_run and "entry" not in self.no_fill:
            # dry-run 什么也没发 → 交易所侧不产生仓位；
            # `no_fill` = tx executed 但零成交（2026-10-30 线上事故的形态）
            qty = int(self.partial_entry_qty_int or plan.qty_int)
            self.positions[plan.symbol] = {"side": int(plan.side),
                                           "qty_int": qty,
                                           "market_id": int(plan.market_id),
                                           "size_decimals": int(plan.size_decimals)}
        return self._result("entry", symbol=plan.symbol, market_id=int(plan.market_id),
                            side=int(plan.side), qty_int=int(plan.qty_int),
                            client_order_index=coi, tx_hash=f"0xentry{int(bar_ms)}")

    async def place_stop(self, plan, *, bar_ms=None, client_order_index=None, **kw):
        if "stop" in self.raise_on:
            raise RuntimeError("boom in place_stop")
        if "stop" in self.reject:
            return self.reject["stop"]
        # 显式 coi 优先（止损守护的补挂/换单会传一个「本次尝试」的新键）
        coi = (int(client_order_index) if client_order_index is not None
               else coi_of(plan.symbol, int(bar_ms or 0) or 1, "stop"))
        self.stops.append({"symbol": plan.symbol, "trigger_price_int": plan.stop_price_int,
                           "side": int(plan.side), "qty_int": plan.qty_int,
                           "reduce_only": True, "coi": coi, "bar_ms": bar_ms})
        if not self.dry_run:
            self.orders.append(ExchangeOrder(
                order_id=str(100_000 + coi), client_order_index=coi,
                market_id=int(plan.market_id), symbol=plan.symbol, reduce_only=True,
                trigger_price_int=int(plan.stop_price_int), qty_int=int(plan.qty_int),
                status="open"))
        return self._result("stop", symbol=plan.symbol, market_id=int(plan.market_id),
                            trigger_price_int=int(plan.stop_price_int),
                            client_order_index=coi, reduce_only=True,
                            # 交易所返回的是**绝对** epoch 毫秒（now + 28 天）
                            order_expiry_ms=int(NOW_S * 1000) + 28 * 86400 * 1000,
                            tx_hash=f"0xstop{coi}")

    async def cancel_order(self, *, market_id, order_index):
        self.cancels.append({"market_id": int(market_id), "order_index": int(order_index)})
        if "cancel" in self.raise_on:
            raise RuntimeError("cancel boom")
        keep = [o for o in self.orders if str(o.order_id) != str(order_index)]
        removed = len(keep) != len(self.orders)
        self.orders = keep
        result = self._result("cancel", market_id=int(market_id))
        return result if removed else result

    async def place_exit(self, *, symbol, market_id, side, qty_int, price_decimals,
                         exit_price, bar_ms=None, client_order_index=None,
                         limit_price_int=None):
        if "exit" in self.raise_on:
            raise RuntimeError("boom in place_exit")
        if "exit" in self.reject:
            return self.reject["exit"]
        self.exits.append({"symbol": symbol, "side": int(side), "qty_int": int(qty_int),
                           "exit_price": float(exit_price), "bar_ms": int(bar_ms),
                           "limit_price_int": limit_price_int})
        if "exit" not in self.no_fill:          # no_fill：受理了但仓位还在
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
                 orders=None, trades=None, trades_error=""):
        self.exchange = exchange
        self.equity = equity
        self.orders_error = orders_error
        self._positions = positions
        self._orders = orders
        self._trades = trades                # None = 没有 trades 方法的数据 → 返回空
        self.trades_error = trades_error
        self.reads = 0

    async def trades(self, limit=20):
        if self.trades_error:
            raise ReaderError(f"trades unavailable: {self.trades_error}")
        return tuple(self._trades or ())

    async def read(self):
        self.reads += 1
        positions = (self._positions if self._positions is not None
                     else self.exchange.as_positions())
        orders = (self._orders if self._orders is not None else tuple(self.exchange.orders))
        return ExchangeAccount(equity=self.equity, positions=tuple(positions),
                               orders=tuple(orders) if not self.orders_error else (),
                               orders_error=self.orders_error, as_of=NOW_S)

    async def active_orders(self):
        """只读活挂单（止损守护换单前的可见性验证走这个）。"""
        if self.orders_error:
            raise ReaderError(f"activeOrders unavailable: {self.orders_error}")
        return (tuple(self._orders) if self._orders is not None
                else tuple(self.exchange.orders))


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
        """对账本身必须能发现「有仓没止损」。**关掉止损守护**再测：
        守护（2026-10-29 新增）会在对账之前把缺失的止损补上，那条路径由
        `StopGuardTests.test_missing_stop_is_replaced_with_the_frozen_stop_price` 钉住。"""
        self.seed_position()
        self.exchange.positions[BTC] = {"side": 1, "qty_int": PLAN_QTY_INT,
                                        "market_id": 1, "size_decimals": 5}
        self.exchange.orders = []                     # 止损单没了
        report = self.run_tick(self.make_executor(stop_guard=False))
        self.assertIn(ISSUE_STOP_ORDER_MISSING, report.reconcile.codes)
        issue = report.reconcile.by_code(ISSUE_STOP_ORDER_MISSING)[0]
        self.assertTrue(issue.blocker)
        self.assertEqual(issue.detail["stop_price_int"], 900)
        # 关掉守护时不会自作主张补挂
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


class PositionPayloadPnlTests(unittest.TestCase):
    """账户响应里的浮盈/仓位价值/强平价必须被解析出来（面板的盈亏就靠这三个）。"""

    def payload(self, **pos):
        row = {"market_id": 2, "symbol": "SOL", "sign": -1, "position": "1.191",
               "avg_entry_price": "99.577", "position_value": "119.845566",
               "unrealized_pnl": "-1.249359", "liquidation_price": "507.22748904544505"}
        row.update(pos)
        return {"code": 200, "accounts": [{"index": 764377, "positions": [row]}]}

    def test_fields_are_parsed_from_strings(self):
        pos = executor.positions_from_payload(self.payload(), account_index=764377)[0]
        self.assertAlmostEqual(pos.unrealized_pnl, -1.249359, places=6)
        self.assertAlmostEqual(pos.position_value, 119.845566, places=6)
        self.assertAlmostEqual(pos.liquidation_price, 507.2275, places=3)
        self.assertEqual(pos.as_dict()["unrealized_pnl"], -1.249359)

    def test_missing_fields_degrade_to_none_not_zero(self):
        payload = self.payload()
        payload["accounts"][0]["positions"][0].pop("unrealized_pnl")
        payload["accounts"][0]["positions"][0].pop("position_value")
        pos = executor.positions_from_payload(payload, account_index=764377)[0]
        self.assertIsNone(pos.unrealized_pnl)
        self.assertIsNone(pos.position_value)


class DecimalTriggerPriceTests(unittest.TestCase):
    """交易所的止损触发价是**小数串**（`Order.trigger_price: str`）。

    旧解析用 `str.isdigit()` 判空 ⇒ 所有小数触发价都被丢成 None（2026-10-28 实盘看到
    `trigger_price_int: null`），于是「按 ±1 tick 匹配外部/老止损单」这条兜底永久失效。
    现在：原样保留小数串，匹配时按标的的 price_decimals 换算成整数 tick 比较。
    """

    def order(self, raw, *, market_id=2, symbol="SOL"):
        return ExchangeOrder(order_id="s", client_order_index=None, market_id=market_id,
                             symbol=symbol, reduce_only=True, trigger_price_raw=raw)

    def test_decimal_string_is_scaled_by_the_symbols_precision(self):
        row = dict(symbol="SOL", market_id=2, stop_price_int=99057)
        matched, by = match_stop_order(row, [self.order("99.057")])
        self.assertIsNotNone(matched)
        self.assertEqual(by, "trigger_price")

    def test_one_tick_off_matches_two_ticks_does_not(self):
        row = dict(symbol="SOL", market_id=2, stop_price_int=99057)
        self.assertIsNotNone(match_stop_order(row, [self.order("99.058")])[0])
        self.assertIsNone(match_stop_order(row, [self.order("99.059")])[0])

    def test_unknown_symbol_never_matches_by_price(self):
        """未知标的拿不到 price_decimals ⇒ 不匹配（宁可不认，也不把别人的单当自己的）。"""
        row = dict(symbol="DOGE", market_id=2, stop_price_int=99057)
        self.assertIsNone(match_stop_order(row, [self.order("99.057", symbol="DOGE")])[0])

    def test_integer_trigger_ticks_still_work(self):
        row = dict(symbol="BTC", market_id=1, stop_price_int=1050)
        order = ExchangeOrder(order_id="s", client_order_index=None, market_id=1,
                              symbol="BTC", reduce_only=True, trigger_price_int=1050)
        self.assertEqual(match_stop_order(row, [order])[1], "trigger_price")

    def test_reader_converts_the_decimal_string_at_parse_time(self) -> None:
        """实测原始行：`"trigger_price": "98.890"`（小数串）→ 必须变成 98890。

        否则面板与对账看到的永远是 `trigger_price_int: null`（旧行为），止损覆盖只能靠
        client_order_index 一条腿站着。
        """
        payload = {"code": 200, "orders": [{
            "order_id": "844426248695643", "client_order_index": 256212589665254,
            "market_index": 2, "market_symbol": "SOL", "reduce_only": True,
            "trigger_price": "98.890", "base_price": 97901, "base_size": 117,
            "status": "pending"}]}
        orders = executor.orders_from_payload(payload)
        self.assertEqual(orders[0].trigger_price_int, 98890)
        self.assertEqual(orders[0].trigger_price_raw, "98.890")

    def test_reader_leaves_unknown_symbols_unconverted(self) -> None:
        payload = {"code": 200, "orders": [{
            "order_id": "x", "client_order_index": 1, "market_index": 9,
            "market_symbol": "DOGE", "reduce_only": True, "trigger_price": "0.123",
            "base_price": 12, "base_size": 1000, "status": "pending"}]}
        order = executor.orders_from_payload(payload)[0]
        self.assertIsNone(order.trigger_price_int)          # 不猜精度
        self.assertEqual(order.trigger_price_raw, "0.123")

    def test_zero_or_garbage_trigger_price_is_ignored(self):
        row = dict(symbol="SOL", market_id=2, stop_price_int=99057)
        for raw in ("0", "0.000", "abc", ""):
            with self.subTest(raw=raw):
                self.assertIsNone(match_stop_order(row, [self.order(raw)])[0])


class OffLoopNetworkTests(unittest.TestCase):
    """tick 里的取数必须 off-loop。

    `market.bars()` / `market.market_details()` 是同步 urllib；直接在事件循环里调会把
    uvicorn 的只读端点一起卡住（2026-10-28 实测：tick 期间 `/status` 偶发空响应）。
    这里用一个「睡 0.3 秒」的假 market 验证：取数期间事件循环仍在转。
    """

    def test_market_bars_runs_off_the_event_loop(self) -> None:
        import asyncio
        import time
        from types import SimpleNamespace

        class SlowMarket:
            def bars(self, symbol, **kwargs):
                time.sleep(0.3)
                return SimpleNamespace(bars=(), dropped_unclosed=())

        fake_executor = SimpleNamespace(market=SlowMarket(), _prices={})

        async def scenario() -> int:
            ticks = {"n": 0}

            async def ticker() -> None:
                while True:
                    ticks["n"] += 1
                    await asyncio.sleep(0.005)

            task = asyncio.create_task(ticker())
            try:
                with self.assertRaises(Exception):
                    await executor.Executor._load_symbol(fake_executor, "BTC", 0)
            finally:
                task.cancel()
            return ticks["n"]

        spins = asyncio.run(scenario())
        self.assertGreater(spins, 20, f"事件循环被阻塞了：0.3s 内只转了 {spins} 次")


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
        allowed = {"__future__", "asyncio", "json", "logging", "math", "time", "urllib",
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


class TransferFlowTests(unittest.TestCase):
    """`transfers_to_flows` + `LighterAccountReader.transfers`（TWR 的出入金流水输入）。

    `app.dd.net_flow` 吃的格式是 `{"ts": epoch_s, "amount": 带符号 USDC}`
    （正 = 入金，负 = 出金）——方向猜反比少一笔更危险，所以认不出的行整条丢弃。
    """

    def test_parses_direction_signed_amounts(self) -> None:
        payload = {"code": 200, "transfers": [
            {"ts": 1793120000000, "amount": "50.5", "direction": "in"},      # ms → s
            {"timestamp": 1793120000, "amount": 20, "type": "withdrawal"},   # 负
            {"created_at": 1793120000, "usdc": 5, "side": "deposit"},        # 正
        ]}
        flows = executor.transfers_to_flows(payload)
        self.assertEqual(flows[0], {"ts": 1793120000.0, "amount": 50.5})
        self.assertEqual(flows[1], {"ts": 1793120000.0, "amount": -20.0})
        self.assertEqual(flows[2], {"ts": 1793120000.0, "amount": 5.0})

    def test_drops_unrecognisable_rows(self) -> None:
        payload = {"transfers": [
            {"amount": "10", "direction": "sideways"},       # 方向不认识
            {"amount": "10"},                                # 没有方向字段
            {"ts": 1793120000, "direction": "in"},           # 没有金额
            {"ts": 0, "amount": "1", "direction": "in"},     # 时间戳非法
            "junk",                                          # 行不是 Mapping
        ]}
        self.assertEqual(executor.transfers_to_flows(payload), ())
        self.assertEqual(executor.transfers_to_flows({"transfers": []}), ())
        self.assertEqual(executor.transfers_to_flows(None), ())
        self.assertEqual(executor.transfers_to_flows("nope"), ())
        self.assertEqual(executor.transfers_to_flows(12345), ())

    def test_reader_transfers_uses_auth_and_site_root(self) -> None:
        seen: List[Tuple[str, Dict[str, str]]] = []

        def http_json(url, headers=None, timeout=None):
            seen.append((url, dict(headers or {})))
            return {"code": 200, "transfers": [], "cursor": ""}

        reader = executor.LighterAccountReader(
            base_url="https://host.test/api/v1", account_index=42,
            auth_provider=lambda: "tok-9", http_json=http_json)
        self.assertEqual(reader.transfers(), ())
        url, headers = seen[0]
        self.assertNotIn("/api/v1/api/v1", url)              # 站点根归一化（403 坑）
        self.assertIn("/api/v1/transfer/history?account_index=42", url)
        self.assertEqual(headers.get("Authorization"), "tok-9")


class StopGuardTests(Base):
    """止损守护：缺止损→补挂；临期→先挂新再撤旧；读不到挂单 / dry-run / 开关关 → 什么都不做。

    线上动机（2026-10-29）：Lighter 挂单有 orderExpiry（28 天），交易所到期自动废单，
    而仓库原来只有开仓那一刻挂止损 ⇒ 到期后裸奔且无人知道。
    """

    def setUp(self):
        super().setUp()
        self.qmarket = FakeMarket({BTC: bars_result(quiet_bars(400))})

    def _open_position(self, *, stop_order_id="coi:111", expiry_ts=None,
                       stop_price_int=STOP_PRICE_INT, qty_int=PLAN_QTY_INT):
        self.seed_position(stop_order_id=stop_order_id, stop_price_int=stop_price_int,
                           qty_int=qty_int)
        self.exchange.positions[BTC] = {"side": 1, "qty_int": qty_int,
                                        "market_id": 1, "size_decimals": 5}
        if expiry_ts is not None:
            pid = self.store.open_positions()[0]["id"]
            self.store.set_stop_order(pid, stop_order_id=stop_order_id,
                                      stop_expiry_ts=expiry_ts)
        return self.store.open_positions()[0]

    def _executor(self, **kw):
        kw.setdefault("market", self.qmarket)
        return self.make_executor(**kw)

    def _live_stop(self, *, coi=111, trigger_int=STOP_PRICE_INT, qty_int=PLAN_QTY_INT,
                   expiry_ms=None):
        order = ExchangeOrder(order_id=str(100_000 + coi), client_order_index=coi,
                              market_id=1, symbol=BTC, reduce_only=True,
                              trigger_price_int=trigger_int, qty_int=qty_int,
                              order_expiry_ms=expiry_ms, status="open")
        self.exchange.orders.append(order)
        return order

    def test_expiry_is_backfilled_from_the_exchange_order(self):
        """v4 之前建的仓没有 stop_expiry_ts：用交易所的 order_expiry 回填（一次性自愈）。"""
        self._live_stop(coi=111, expiry_ms=int((NOW_S + 20 * 86400) * 1000))
        self._open_position(stop_order_id="coi:111")
        self.run_tick(self._executor())
        after = self.store.open_positions()[0]
        self.assertAlmostEqual(after["stop_expiry_ts"], NOW_S + 20 * 86400, delta=2)
        self.assertEqual(self.exchange.stops, [], "20 天后才到期，不该换单")

    def test_backfilled_expiry_can_trigger_a_renewal(self):
        self._live_stop(coi=111, expiry_ms=int((NOW_S + 600) * 1000))
        self._open_position(stop_order_id="coi:111")
        self.run_tick(self._executor())
        self.assertEqual(len(self.exchange.stops), 1, "回填后判定临期 → 换单")
        self.assertEqual(len(self.exchange.cancels), 1)

    def test_missing_stop_is_replaced_with_the_frozen_stop_price(self):
        row = self._open_position()                     # 本地有仓、交易所没有止损单
        ex = self._executor()
        report = self.run_tick(ex)
        self.assertEqual(len(self.exchange.stops), 1, "缺止损必须补挂一张")
        placed = self.exchange.stops[0]
        self.assertEqual(placed["trigger_price_int"], row["stop_price_int"])
        self.assertEqual(placed["qty_int"], row["qty_int"])
        # 记账改指新单，并且记下到期时刻（供下一轮临期换单）
        after = self.store.open_positions()[0]
        self.assertEqual(after["stop_order_id"], f"coi:{placed['coi']}")
        self.assertIsNotNone(after["stop_expiry_ts"])
        self.assertEqual(len(report.stop_guards), 1)
        self.assertTrue(report.stop_guards[0]["pending_confirm"])
        self.assertTrue(self.store.events(limit=5, kind="stop_guard"))
        # 补上之后对账不该再报「缺止损」阻断开仓
        self.assertNotIn(BLOCK_RECONCILE, report.block_reasons)

    def test_orders_unreadable_means_no_action(self):
        self._open_position()
        self.reader.orders_error = "auth expired"
        ex = self._executor(reader=self.reader)
        report = self.run_tick(ex)
        self.assertEqual(self.exchange.stops, [], "读不到挂单时不能断定缺止损，更不许下单")
        self.assertIn(BLOCK_RECONCILE, report.block_reasons)

    def test_dry_run_and_disabled_guard_do_nothing(self):
        self._open_position()
        self.exchange.dry_run = True
        self.run_tick(self._executor())
        self.assertEqual(self.exchange.stops, [], "dry-run 不许发单")
        self.exchange.dry_run = False
        self.run_tick(self._executor(stop_guard=False))
        self.assertEqual(self.exchange.stops, [], "关掉守护就退回旧行为")

    def test_expiring_stop_is_renewed_before_it_lapses(self):
        old = self._live_stop(coi=111)
        pid = self._open_position(stop_order_id="coi:111",
                                  expiry_ts=NOW_S + 3600.0)["id"]
        ex = self._executor()
        report = self.run_tick(ex)
        self.assertEqual(len(self.exchange.stops), 1, "临期要换一张新单")
        new_coi = self.exchange.stops[0]["coi"]
        self.assertNotEqual(new_coi, 111)
        # 旧单被单张撤掉（不是 cancel_all），DB 指向新单
        self.assertEqual(self.exchange.cancels,
                         [{"market_id": 1, "order_index": int(old.order_id)}])
        after = self.store.open_positions()[0]
        self.assertEqual(after["stop_order_id"], f"coi:{new_coi}")
        self.assertGreater(after["stop_expiry_ts"], NOW_S + 27 * 86400)
        self.assertEqual(len(report.stop_guards), 1)
        # 面板缓存合并时要知道「这轮换掉了哪张旧单」，好把旧 coi 从"还在挂着"里拿掉
        self.assertEqual(report.stop_guards[0]["replaced_coi"], 111)

    def test_far_expiry_is_left_alone(self):
        self._live_stop(coi=111)
        self._open_position(stop_order_id="coi:111", expiry_ts=NOW_S + 20 * 86400)
        self.run_tick(self._executor())
        self.assertEqual(self.exchange.stops, [])
        self.assertEqual(self.exchange.cancels, [])

    def test_renew_is_aborted_when_the_new_order_is_not_visible(self):
        """换单红线：新单没在活挂单里看到，就绝不撤旧单（否则可能两头落空）。"""
        self._live_stop(coi=111)
        self._open_position(stop_order_id="coi:111", expiry_ts=NOW_S + 3600.0)

        class BlindReader(FakeReader):
            async def active_orders(self):
                return ()                      # 永远看不到 → 验证失败

        ex = self._executor(reader=BlindReader(self.exchange))
        ex.STOP_VERIFY_ATTEMPTS = 1
        ex.STOP_VERIFY_INTERVAL_S = 0.0
        self.run_tick(ex)
        self.assertEqual(self.exchange.cancels, [], "看不到新单时不许撤旧单")
        self.assertEqual(self.store.open_positions()[0]["stop_order_id"], "coi:111")
        self.assertTrue(self.store.events(limit=3, kind="stop_guard_unverified"))

    def test_cancel_failure_is_recorded_but_new_stop_stays(self):
        self._live_stop(coi=111)
        self._open_position(stop_order_id="coi:111", expiry_ts=NOW_S + 3600.0)
        self.exchange.raise_on.add("cancel")
        self.run_tick(self._executor())
        self.assertTrue(self.store.events(limit=3, kind="stop_cancel_failed"))
        self.assertNotEqual(self.store.open_positions()[0]["stop_order_id"], "coi:111")

    def test_entry_sets_isolated_mode_before_placing_the_order(self):
        ex = self._executor(market=self.market)
        self.run_tick(ex)
        self.assertEqual(len(self.exchange.leverage_sets), 1)
        self.assertEqual(self.exchange.leverage_sets[0],
                         {"market_id": 1, "leverage": 5.0})
        # 抵押由交易所开仓时自动划入（2026-10-29 实测）→ 开仓路径**不盲发** ADD；
        # 偏低由下一轮 tick 的抵押守护补足（见 MarginGuardTests）。
        self.assertEqual(self.exchange.margin_allocs, [])

    def test_leverage_rejection_blocks_the_entry(self):
        ex = self._executor(market=self.market)
        self.exchange.reject_leverage = True
        report = self.run_tick(ex)
        outcome = report.outcomes[0]
        self.assertFalse(outcome.ok)
        self.assertEqual(self.exchange.entries, [], "模式设置失败就不许开仓")
        self.assertEqual(self.store.open_positions(), [])
        self.assertTrue(any("margin mode rejected" in e["message"]
                            for e in self.events("order_rejected")))

    def test_flag_off_skips_margin_ops(self):
        ex = self._executor(market=self.market, margin_isolated=False)
        self.run_tick(ex)
        self.assertEqual(self.exchange.leverage_sets, [])
        self.assertEqual(self.exchange.margin_allocs, [])
        self.assertEqual(len(self.exchange.entries), 1)

    def test_entry_records_the_stop_expiry_for_later_renewal(self):
        ex = self._executor(market=self.market)          # 有信号的市场 → 会开仓
        self.run_tick(ex)
        row = self.store.open_positions()[0]
        self.assertIsNotNone(row["stop_expiry_ts"], "开仓后要记住止损到期时刻")

    def test_closing_position_is_not_touched(self):
        self._open_position()
        pid = self.store.open_positions()[0]["id"]
        self.store.set_position_state(pid, "CLOSING")
        self.run_tick(self._executor())
        self.assertEqual(self.exchange.stops, [], "正在离场的仓位别去动它的保护单")

    def test_qty_mismatch_is_left_to_reconcile(self):
        """交易所数量与本地不符时不补挂：先让对账把它当 blocker 报出来。"""
        self._open_position()
        self.exchange.positions[BTC]["qty_int"] = PLAN_QTY_INT - 1
        report = self.run_tick(self._executor())
        self.assertEqual(self.exchange.stops, [])
        self.assertIn(BLOCK_RECONCILE, report.block_reasons)


# ----------------------------------------------------------------- 逐仓抵押守护


class MarginGuardTests(Base):
    """allocated 低于 名义/杠杆 → 补差额；读不到 → 跳过不猜；被拒 → 事件+报警。"""

    def _quiet_executor(self, **kw):
        kw.setdefault("market", FakeMarket({BTC: bars_result(quiet_bars(400))}))
        return self.make_executor(**kw)

    def _account_with(self, *, allocated, position_value=10.0):
        from app.executor import ExchangePosition
        pos = ExchangePosition(symbol=BTC, side=1,
                               qty=__import__("decimal").Decimal("1.0"),
                               market_id=1, position_value=position_value,
                               allocated_margin=allocated)
        return FakeReader(self.exchange, positions=(pos,))

    def test_shortfall_is_topped_up(self):
        self.seed_position()
        ex = self._quiet_executor(reader=self._account_with(allocated=1.0))  # 目标 10/5=2
        self.run_tick(ex)
        self.assertEqual(self.exchange.margin_allocs,
                         [{"market_id": 1, "usdc_amount": 1.0}])
        self.assertTrue(self.store.events(limit=5, kind="margin_topup"))

    def test_healthy_allocation_is_left_alone(self):
        self.seed_position()
        ex = self._quiet_executor(reader=self._account_with(allocated=2.0))
        self.run_tick(ex)
        self.assertEqual(self.exchange.margin_allocs, [])

    def test_unknown_allocation_is_skipped_not_guessed(self):
        self.seed_position()
        ex = self._quiet_executor(reader=self._account_with(allocated=None))
        self.run_tick(ex)
        self.assertEqual(self.exchange.margin_allocs, [])

    def test_topup_failure_is_recorded_and_alerted(self):
        self.seed_position()
        ex = self._quiet_executor(reader=self._account_with(allocated=1.0))
        self.exchange.reject_alloc = True
        self.run_tick(ex)
        self.assertTrue(self.store.events(limit=8, kind="margin_topup_failed"))
        self.assertTrue(any(k == "margin_topup_failed" for k, _ in self.alerts))
        self.assertEqual(len(self.store.open_positions()), 1, "守护只补抵押，不动仓位")

    def test_flag_off_disables_the_guard(self):
        self.seed_position()
        ex = self._quiet_executor(reader=self._account_with(allocated=1.0),
                                  margin_isolated=False)
        self.run_tick(ex)
        self.assertEqual(self.exchange.margin_allocs, [])


# --------------------------------------------------------------------------- 成交确认
#
# 2026-10-30 线上事故回归：Lighter 的 `sendTx` 返回 code=200 只表示「进了 sequencer」。
# 20:03 那笔 SOL 空单 tx executed，但**零成交**（bar 收盘 99.835，3.8 分钟后市价已跌到
# 99.1，卖出最差价 99.535 已在盘口之上 ⇒ IOC 不可成交），而本地照样记了仓 + 报「止损
# 未覆盖」，幽灵仓一直留到人工清理。下面这几条钉住「受理 ≠ 成交」的三个方向。


class EntryFillVerificationTests(Base):
    """开仓必须等到交易所真的出现仓位才记账。"""

    def test_entry_without_exchange_fill_is_not_booked(self):
        self.exchange.no_fill = {"entry"}
        ex = self.make_executor(fill_confirm_s=0)
        report = self.run_tick(ex)

        self.assertEqual(self.store.open_positions(), [], "零成交绝不能记仓")
        self.assertEqual(self.store.trade_count(), 0, "零成交绝不能记成交行")
        self.assertEqual(self.exchange.stops, [], "没有仓位就不该挂止损")
        kinds = [e["kind"] for e in self.events()]
        self.assertIn("entry_not_filled", kinds)
        self.assertIn("entry_not_filled", [k for k, _ in self.alerts])
        self.assertFalse(report.outcomes[0].filled)
        self.assertFalse(report.outcomes[0].ok)
        self.assertEqual(self.store.audit(), [], "账务不变式仍然成立")

    def test_partial_fill_books_the_exchange_qty_not_the_plan(self):
        self.exchange.partial_entry_qty_int = PLAN_QTY_INT // 2
        ex = self.make_executor(fill_confirm_s=0)
        self.run_tick(ex)

        rows = self.store.open_positions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["qty_int"]), PLAN_QTY_INT // 2)
        self.assertEqual(self.exchange.stops[0]["qty_int"], PLAN_QTY_INT // 2,
                         "止损数量必须跟着真实成交量")
        self.assertIn("entry_partial_fill", [e["kind"] for e in self.events()])
        self.assertEqual(self.store.audit(), [])

    def test_fill_qty_is_taken_after_it_settles(self):
        """部分成交会「长」：连续两次读数一致才算落定，记账用最大值。"""
        from app.executor import ExchangeAccount, ExchangePosition

        def account(qty_int):
            qty = Decimal(qty_int).scaleb(-5)
            return ExchangeAccount(
                equity=10_000.0, as_of=NOW_S,
                positions=(ExchangePosition(symbol=BTC, side=1, qty=qty, qty_str=str(qty)),),
                orders=())

        class ScriptedReader:
            def __init__(self, seq):
                self.seq = list(seq)
                self.reads = 0

            async def read(self):
                value = self.seq[min(self.reads, len(self.seq) - 1)]
                self.reads += 1
                return value

        class ZeroSleeper:
            async def __call__(self, _seconds):
                return None

        ex = self.make_executor(fill_confirm_s=3.0, fill_poll_interval_s=1.0,
                                sleeper=ZeroSleeper())
        ex.reader = ScriptedReader([account(100_000), account(100_000),
                                    account(250_000), account(PLAN_QTY_INT)])
        import asyncio as _asyncio
        got = _asyncio.run(ex._confirm_entry_fill(
            symbol=BTC, side=1, size_decimals=5, qty_int=PLAN_QTY_INT, timeout_s=3.0))
        self.assertEqual(got, 100_000, "连续两次一致就该落定，不必等满窗口")

        ex.reader = ScriptedReader([account(100_000), account(250_000)])
        got = _asyncio.run(ex._confirm_entry_fill(
            symbol=BTC, side=1, size_decimals=5, qty_int=PLAN_QTY_INT, timeout_s=1.0))
        self.assertEqual(got, 250_000, "窗口内数量在长 → 记最大值")

        ex.reader = ScriptedReader([ExchangeAccount(equity=10_000.0, as_of=NOW_S)])
        got = _asyncio.run(ex._confirm_entry_fill(
            symbol=BTC, side=1, size_decimals=5, qty_int=PLAN_QTY_INT, timeout_s=0))
        self.assertIsNone(got, "没有仓位 = 没成交")

    def test_dry_run_does_not_wait_for_a_fill(self):
        """dry-run 本来就不会在交易所产生仓位 —— 不能因此把模拟盘锁死。"""
        self.exchange.dry_run = True
        ex = self.make_executor(fill_confirm_s=0)
        self.run_tick(ex)
        self.assertEqual(len(self.store.open_positions()), 1)
        self.assertNotIn("entry_not_filled", [e["kind"] for e in self.events()])


class ExitFillVerificationTests(Base):
    """平仓同样要确认：没成交却记 CLOSED = 本地以为平了、交易所还开着。"""

    def _stop_hit_executor(self, **kwargs):
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        row = self.seed_position(entry_price_int=1120, stop_price_int=1050,
                                 bar_ms=T0, qty_int=415600)
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        self.exchange.orders = []
        return row, self.make_executor(fill_confirm_s=0, **kwargs)

    def test_exit_without_fill_keeps_the_position_open(self):
        row, ex = self._stop_hit_executor()
        self.exchange.no_fill = {"exit"}
        report = self.run_tick(ex)

        self.assertEqual(self.store.position(int(row["id"]))["state"], "OPEN",
                         "没成交就不能记 CLOSED（否则就是裸仓）")
        self.assertIn("exit_not_filled", [e["kind"] for e in self.events()])
        self.assertIn("exit_not_filled", [k for k, _ in self.alerts])
        self.assertFalse(report.outcomes[0].filled)
        self.assertIsNotNone(self.store.position(int(row["id"]))["closed_at"] is None)

    def test_exit_that_fills_closes_the_position(self):
        row, ex = self._stop_hit_executor()
        self.run_tick(ex)
        self.assertEqual(self.store.position(int(row["id"]))["state"], "CLOSED")


class GraceSettlementTests(Base):
    """`grace` 不能是终点：要么按真实成交结案，要么作废幽灵仓。"""

    def _phantom(self, **kw):
        """本地有仓 + 交易所空仓 + 止损单没有（= grace 的两个条件）。"""
        row = self.seed_position(stop_order_id="coi:999", **kw)
        self.exchange.positions.pop(BTC, None)
        self.exchange.orders = []
        self.market.feeds[BTC] = bars_result(quiet_bars(), unclosed=())
        return row

    def test_grace_voids_a_position_that_never_traded(self):
        row = self._phantom()
        ex = self.make_executor(fill_confirm_s=0, grace_ticks=2)

        first = self.run_tick(ex)
        self.assertEqual(self.store.position(int(row["id"]))["state"], "OPEN",
                         "第一轮只累计，不动手")
        self.assertIn("grace", json.dumps(first.reconcile.as_dict()))

        second = self.run_tick(ex)
        after = self.store.position(int(row["id"]))
        self.assertEqual(after["state"], "CLOSED")
        self.assertEqual(after["exit_reason"], "no_fill")
        self.assertEqual(int(after["qty_int"]), 0)
        self.assertEqual(self.store.trade_count(), 0, "未成交的开仓行必须删掉")
        self.assertIn("position_voided", [e["kind"] for e in self.events()])
        self.assertIn("position_voided", [k for k, _ in self.alerts])
        self.assertEqual(self.store.audit(), [])
        self.assertEqual(len(second.grace_settlements), 1)

    def test_grace_void_survives_executor_rebuild(self):
        """生产形状回归：`_tick()` 每轮**新建** Executor，计数必须跨实例。

        旧实现把 `_grace_seen` 挂在实例上 —— 单测拿同一个实例连跑两轮所以全绿，
        线上每轮新实例 `seen` 恒为 1，自动作废在生产里一次都没触发过（2026-10-30
        复审发现）。现在计数落在 events 表：第二个实例必须能接着第一轮的计数作废。
        """
        row = self._phantom()
        first = self.run_tick(self.make_executor(fill_confirm_s=0, grace_ticks=2))
        self.assertEqual(self.store.position(int(row["id"]))["state"], "OPEN",
                         "第一轮只累计，不动手")
        self.assertEqual(len(self.events("grace_unsettled")), 1,
                         "第一轮没结案必须落一条 grace_unsettled（持久计数 + 审计 trail）")

        second = self.run_tick(self.make_executor(fill_confirm_s=0, grace_ticks=2))
        after = self.store.position(int(row["id"]))
        self.assertEqual(after["state"], "CLOSED")
        self.assertEqual(after["exit_reason"], "no_fill")
        self.assertIn("position_voided", [e["kind"] for e in self.events()])
        self.assertEqual(len(second.grace_settlements), 1)

    def test_grace_settles_a_real_stop_fill_from_trades(self):
        row = self._phantom()
        trade = executor.ExchangeTrade(
            trade_id="37135831346", symbol=BTC, side=-1, qty=float(row["qty"]),
            qty_str=str(row["qty"]), price=float(row["stop_price"]),
            ts=float(row["opened_at"]) + 5.0, pnl=-12.5)
        self.reader._trades = (trade,)
        ex = self.make_executor(fill_confirm_s=0, grace_ticks=2)
        self.run_tick(ex)

        after = self.store.position(int(row["id"]))
        self.assertEqual(after["state"], "CLOSED")
        self.assertEqual(after["exit_reason"], "settled_from_trades")
        self.assertAlmostEqual(float(after["net_pnl"]), -12.5, places=6)
        self.assertIn("stop_fill_settled", [e["kind"] for e in self.events()])

    def test_grace_waits_when_the_trades_window_does_not_cover_it(self):
        """成交窗口盖不住仓位生命周期 ⇒ 「查不到」不等于「没有」⇒ 不许作废。"""
        row = self._phantom()
        # 窗口里最老的一笔在开仓**之后** ⇒ 覆盖不住，不能证明「没有成交」
        self.reader._trades = (executor.ExchangeTrade(
            trade_id="1", symbol="ETH", side=1, qty=1.0, qty_str="1.0", price=100.0,
            ts=float(row["opened_at"]) + 3600.0),)
        ex = self.make_executor(fill_confirm_s=0, grace_ticks=1)
        for _ in range(3):
            self.run_tick(ex)
        self.assertEqual(self.store.position(int(row["id"]))["state"], "OPEN")
        self.assertNotIn("position_voided", [e["kind"] for e in self.events()])

    def test_unreadable_trades_never_void_a_position(self):
        row = self._phantom()
        self.reader.trades_error = "boom"
        ex = self.make_executor(fill_confirm_s=0, grace_ticks=1)
        for _ in range(3):
            self.run_tick(ex)
        self.assertEqual(self.store.position(int(row["id"]))["state"], "OPEN")
        self.assertIn("trades_unavailable", [e["kind"] for e in self.events()])




class LiveReferencePriceTests(Base):
    """最差价的锚 = 下单那一刻的实时 mid（对齐 HL 的 slippage_limit 口径）。

    2026-10-30 的对照：同一根信号，HL 用「实时 mid ± 0.3%」⇒ 以 99.446 成交（比收盘价
    差 39bp）；Lighter 用「4h 收盘 ± 0.3%」= 99.535，而市价已 99.1 ⇒ 零成交。
    """

    def test_entry_bound_uses_the_live_mid_not_the_bar_close(self):
        # 信号价 104.2（bar close），发单时 mid 已经跑到 106.0
        self.market.mids[BTC] = 106.0
        ex = self.make_executor(fill_confirm_s=0)
        self.run_tick(ex)

        sent = self.exchange.entries[0]
        self.assertEqual(sent["limit_price_int"], 1060, "锚必须是实时 mid 的整数 tick")
        self.assertNotEqual(sent["limit_price_int"], ENTRY_PRICE_INT,
                            "不能再用 bar 收盘（104.2）当锚")
        # 真正发出去的最差价 = mid × (1+30bp)，买侧向上取整
        from app.exchange import DEFAULT_ENTRY_SLIPPAGE_BPS, worst_price_int
        self.assertEqual(worst_price_int(1060, DEFAULT_ENTRY_SLIPPAGE_BPS, is_ask=False), 1064)

    def test_entry_falls_back_to_the_bar_close_when_the_quote_fails(self):
        self.market.snapshot_error = RuntimeError("book exploded")
        ex = self.make_executor(fill_confirm_s=0)
        self.run_tick(ex)
        self.assertIsNone(self.exchange.entries[0]["limit_price_int"])
        self.assertIn("live_quote_failed", [e["kind"] for e in self.events()])
        self.assertEqual(len(self.store.open_positions()), 1, "报价失败不许阻断开仓")

    def test_wild_quote_is_rejected(self):
        """实时价离信号价 >3% ⇒ 当脏数据，退回信号价（不许拿离谱价格下真单）。"""
        self.market.mids[BTC] = 120.0          # 信号价 104.2 → +15%
        ex = self.make_executor(fill_confirm_s=0)
        self.run_tick(ex)
        self.assertIsNone(self.exchange.entries[0]["limit_price_int"])
        self.assertIn("live_quote_rejected", [e["kind"] for e in self.events()])

    def test_exit_bound_uses_the_live_mid_within_the_wide_gate(self):
        """离场锚带 5% 宽偏离闸（对抗审查 P2-4）：带内照样追实时 mid。"""
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        self.seed_position(entry_price_int=1120, stop_price_int=1050, bar_ms=T0,
                           qty_int=415600)
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        self.exchange.orders = []
        self.market.mids[BTC] = 103.0          # 比止损价 105 低 1.9%（带内）
        ex = self.make_executor(fill_confirm_s=0)
        self.run_tick(ex)
        self.assertEqual(self.exchange.exits[0]["limit_price_int"], 1030)
        self.assertNotIn("live_quote_rejected", [e["kind"] for e in self.events()])

    def test_exit_falls_back_when_the_mid_is_wildly_off(self):
        """mid 相对信号参考价偏出 5% 带宽（脏数据/串价）→ 退回计划价并留痕。"""
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        self.seed_position(entry_price_int=1120, stop_price_int=1050, bar_ms=T0,
                           qty_int=415600)
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        self.exchange.orders = []
        self.market.mids[BTC] = 96.0           # 比止损价 105 低 8.6%（带外）
        ex = self.make_executor(fill_confirm_s=0)
        self.run_tick(ex)
        self.assertIsNone(self.exchange.exits[0]["limit_price_int"])
        self.assertIn("live_quote_rejected", [e["kind"] for e in self.events()])


# --------------------------------------------------------------------------- 信号时效闸


class SignalAgeGateTests(Base):
    """信号时效闸（2026-10-30）：只允许「信号 bar 收盘后 N 分钟内」建仓。

    夹具的日历：最后一根已收盘 bar = `SIGNAL_BARS[-1]`，`Base.now_ms()` 落在它收盘后
    **1 分钟**，所以在默认 30 分钟窗口内。要造超龄必须显式把 now 往后挪（`tick_at`）。
    """

    def tick_at(self, minutes_after_close, *, ex=None):
        """在「信号 bar 收盘后 N 分钟」跑一轮。"""
        import asyncio
        now_ms = (SIGNAL_BARS[-1].open_ms + BAR_MS
                  + int(round(float(minutes_after_close) * 60_000)))
        return asyncio.run((ex or self.executor).run_once(now_ms=now_ms))

    def test_stale_entry_is_downgraded_to_skip_and_places_no_order(self):
        ex = self.make_executor(fill_confirm_s=0)            # 默认 30 分钟窗口
        report = self.tick_at(45, ex=ex)

        # ① 决策被降级成 skip，带结构化原因
        outcome = report.outcomes[0]
        self.assertEqual(outcome.action, ACTION_SKIP)
        self.assertIn("stale_signal", outcome.decision["codes"])
        # ② 一张单都没发、一本账都没记
        self.assertEqual(self.exchange.entries, [])
        self.assertEqual(self.exchange.stops, [])
        self.assertEqual(self.store.open_positions(), [])
        self.assertEqual(self.store.trade_count(), 0)
        # ③ 不算失败、不熔断、本轮仍然 ok
        self.assertEqual(report.status, "ok")
        self.assertTrue(report.ok)
        self.assertEqual(ex.failures, 0)
        self.assertFalse(ex.circuit_open)

        # ④ 事件落库（面板/排查的入口）
        events = self.events("entry_skipped_stale_signal")
        self.assertEqual(len(events), 1)
        data = json.loads(events[0]["data"])
        self.assertEqual(data["symbol"], BTC)
        self.assertEqual(data["bar_ms"], SIGNAL_BARS[-1].open_ms)
        self.assertAlmostEqual(data["age_min"], 45.0, places=3)
        self.assertEqual(data["limit_min"], 30.0)

    def test_fresh_entry_still_executes_inside_the_default_window(self):
        ex = self.make_executor(max_signal_age_min=30, fill_confirm_s=0)
        report = self.tick_at(29, ex=ex)

        self.assertEqual(report.outcomes[0].action, ACTION_ENTRY)
        self.assertEqual(len(self.exchange.entries), 1)
        self.assertEqual(len(self.store.open_positions()), 1)
        self.assertEqual(self.events("entry_skipped_stale_signal"), [])

    def test_the_window_edge_is_inclusive(self):
        """年龄 = 30 分钟（等于窗口）放行；31 分钟拦下。"""
        ex = self.make_executor(max_signal_age_min=30)
        bar_ms = SIGNAL_BARS[-1].open_ms
        close_ms = bar_ms + BAR_MS
        self.assertEqual(ex._signal_age_exceeded(bar_ms, close_ms + 30 * 60_000),
                         (False, 30.0))
        exceeded, age_min = ex._signal_age_exceeded(bar_ms, close_ms + 31 * 60_000)
        self.assertTrue(exceeded)
        self.assertAlmostEqual(age_min, 31.0, places=6)

    def test_zero_disables_the_gate(self):
        """`0` = 关闭：3 小时前的 bar 照常建仓（线上事故那种形态不再被拦）。"""
        ex = self.make_executor(max_signal_age_min=0, fill_confirm_s=0)
        report = self.tick_at(180, ex=ex)

        self.assertEqual(report.outcomes[0].action, ACTION_ENTRY)
        self.assertEqual(len(self.exchange.entries), 1)
        self.assertEqual(self.store.trade_count(), 1)
        self.assertEqual(self.events("entry_skipped_stale_signal"), [])

    def test_a_negative_window_is_stored_as_off(self):
        self.assertEqual(self.make_executor(max_signal_age_min=-5).max_signal_age_min, 0.0)

    def test_stop_is_never_blocked_by_a_stale_bar(self):
        """离场永远放行：5 小时前的 bar 照样把止损打出去。"""
        import asyncio
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        self.seed_position(entry_price_int=1120, stop_price_int=1050, bar_ms=T0,
                           qty_int=415600)
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        self.exchange.orders = []
        ex = self.make_executor(fill_confirm_s=0)             # 默认 30 分钟
        now_ms = bars[-1].open_ms + BAR_MS + 5 * 3600 * 1000
        report = asyncio.run(ex.run_once(now_ms=now_ms))

        self.assertEqual(report.outcomes[0].action, ACTION_STOP)
        self.assertEqual(len(self.exchange.exits), 1)
        self.assertEqual(self.store.open_positions(), [])
        self.assertEqual(self.events("entry_skipped_stale_signal"), [])

    def test_signal_exit_is_never_blocked_by_a_stale_bar(self):
        import asyncio
        bars = stop_hit_bars()
        self.market.feeds[BTC] = bars_result(bars, unclosed=())
        # 止损放到 90（够不着）⇒ 最后一根走「收盘跌破 SMA20」的信号离场
        self.seed_position(entry_price_int=1120, stop_price_int=900, bar_ms=T0,
                           qty_int=415600)
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        self.exchange.orders = []
        ex = self.make_executor(fill_confirm_s=0)
        now_ms = bars[-1].open_ms + BAR_MS + 5 * 3600 * 1000
        report = asyncio.run(ex.run_once(now_ms=now_ms))

        self.assertEqual(report.outcomes[0].action, ACTION_EXIT)
        self.assertEqual(len(self.exchange.exits), 1)
        self.assertEqual(self.events("entry_skipped_stale_signal"), [])

    def test_max_signal_age_minutes_reads_the_env(self):
        """默认 30 / `0` 关闭 / 负数与脏值退回 30（脏配置不许静默关掉闸门）。"""
        from app import main
        self.assertEqual(main.max_signal_age_minutes({}), 30.0)
        self.assertEqual(main.max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": ""}), 30.0)
        self.assertEqual(main.max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": "0"}), 0.0)
        self.assertEqual(main.max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": "45"}), 45.0)
        self.assertEqual(main.max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": " 60 "}),
                         60.0)
        self.assertEqual(main.max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": "-5"}), 30.0)
        self.assertEqual(main.max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": "abc"}),
                         30.0)
        # "inf"/"1e999"：float() 能解析但不是有限数 —— 静默放行等于把闸门关掉，
        # 必须按脏值退回 30（2026-10-30 复审补的边角）。
        self.assertEqual(main.max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": "inf"}),
                         30.0)
        self.assertEqual(main.max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": "1e999"}),
                         30.0)
        self.assertEqual(main.max_signal_age_minutes({"LIGHTER_MAX_SIGNAL_AGE_MIN": "-inf"}),
                         30.0)


if __name__ == "__main__":                                        # pragma: no cover
    unittest.main()
