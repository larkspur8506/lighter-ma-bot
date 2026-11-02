"""`app/mr.py`（MR 腿状态机）的契约测试 —— 全离线（假交易所 + 临时 SQLite）。

钉住的东西：
  * 默认停用：没有 `mr_admin` 事件 → 任何信号都不下单；
  * 入场：信号 + 窗口 + 权益 → 恰好一笔市价单 + 台账 OPEN 行；
  * 幂等：同信号重复 tick 不重复下单（台账唯一约束兜底）；
  * 离场：收盘 > SMA5 → reduce_only 平仓 + 台账 CLOSED（funding、净盈亏入账）；
  * 对账 fail-closed：交易所持仓 ↔ 本地台账任一方向不一致 → 不动作 + recon_err；
  * kill（全局）挡入场不挡离场；MR 开关同理；
  * sizing：名义 = 权益×0.5，qty 按 4 位小数向下取整，权益不足 → SKIPPED 台账。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.executor import ExchangeAccount, ExchangePosition
from app.mr import MrConfig, MrLeg
from app.store import StateStore


def _hourly(date: str, close: float, *, hours=(14, 15, 16, 17, 18, 19)) -> list:
    base = int(datetime.strptime(date, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)
    rows = []
    for i, h in enumerate(hours):
        t = base + h * 3600_000
        o = close + 0.1 * (len(hours) - i)
        rows.append({"t": t, "o": o, "h": max(o, close), "l": min(o, close),
                     "c": close})
    return rows


CANDLES = []
for _date, _c in [("2026-04-06", 100.0), ("2026-04-07", 99.0), ("2026-04-08", 98.0),
                  ("2026-04-09", 97.0), ("2026-04-10", 96.0), ("2026-04-13", 95.0),
                  ("2026-04-14", 94.0), ("2026-04-15", 93.0), ("2026-04-16", 93.5),
                  ("2026-04-17", 95.5)]:
    CANDLES.extend(_hourly(_date, _c))

ENTRY_NOW_MS = int(datetime.fromisoformat("2026-04-16T13:40:00+00:00").timestamp() * 1000)
EXIT_NOW_MS = int(datetime.fromisoformat("2026-04-17T20:10:00+00:00").timestamp() * 1000)
ENTRY_DAY_CLOSE = 93.5     # 04-16 会话收盘 = 入场参考价


class FakeReader:
    def __init__(self, account: ExchangeAccount):
        self.account = account
        self.calls = 0
        self.pending: list = []          # 逐次覆盖（模拟「晚几拍才看到持仓」）
        self.fail_calls: set = set()     # 第 N 次读抛错（模拟 503/429 抖动）

    async def read(self) -> ExchangeAccount:
        self.calls += 1
        if self.calls in self.fail_calls:
            raise RuntimeError("503 simulated maintenance page")
        if self.pending:
            self.account = self.pending.pop(0)
        return self.account


class FakeExchange:
    """假交易所：**默认成交**（下单后改 reader 的账户），可切换成「受理但零成交」。"""

    def __init__(self):
        self.orders = []
        self.isolated = []
        self.reader: FakeReader | None = None
        self.fill = True                 # False = 受理但交易所没有持仓（IOC 零成交）
        self.fill_qty = None             # None = 按订单数量成交；给数 = 部分成交
        self.fill_price = None           # None = 用 ref_price

    def _positions_after(self, kw) -> tuple:
        if not kw["is_entry"]:
            return ()
        qty = self.fill_qty if self.fill_qty is not None else kw["qty_int"] / 10000
        px = self.fill_price if self.fill_price is not None else kw["ref_price"]
        return (ExchangePosition(
            symbol="QQQ", side=1, qty=Decimal(str(qty)), market_id=129,
            avg_entry_price=float(px), unrealized_pnl=3.0, position_value=250.0,
            allocated_margin=250.0, funding_paid=0.25),)

    async def ensure_isolated(self, *, market_id: int, leverage: float):
        self.isolated.append((market_id, leverage))

    async def place_market(self, **kwargs):
        self.orders.append(kwargs)
        if self.fill and self.reader is not None:
            acct = self.reader.account
            self.reader.account = ExchangeAccount(
                equity=acct.equity, positions=self._positions_after(kwargs),
                orders=(), orders_error="", as_of=0.0, transaction_time=0.0)
        return SimpleNamespace(order_id=f"OID-{len(self.orders)}", accepted=True,
                               ok=True, kind="ok")



class FakeMarket:
    now_ms = EXIT_NOW_MS                       # 模拟「API 只回 now 之前的 bar」

    def candles_1h(self, market_id: int, *, count_back: int = 500):
        return [r for r in CANDLES if r["t"] <= self.now_ms]


def _account(equity: float = 500.0, *, mr_qty: str = "0") -> ExchangeAccount:
    positions = ()
    if mr_qty != "0":
        positions = (ExchangePosition(
            symbol="QQQ", side=1, qty=Decimal(mr_qty), market_id=129,
            unrealized_pnl=3.0, position_value=250.0, allocated_margin=250.0,
            funding_paid=0.03),)
    return ExchangeAccount(equity=equity, positions=positions, orders=(),
                           orders_error="", as_of=0.0, transaction_time=0.0)


def _leg(tmpdir: str, *, equity=500.0, mr_qty="0", market_now=EXIT_NOW_MS):
    store = StateStore(f"{tmpdir}/mr_{abs(hash(tmpdir)) % 99999}.db")
    exchange = FakeExchange()
    market = FakeMarket()
    market.now_ms = market_now
    reader = FakeReader(_account(equity, mr_qty=mr_qty))
    exchange.reader = reader
    leg = MrLeg(store=store, exchange=exchange, reader=reader,
                market_client=market,
                config=MrConfig(fill_confirm_s=0.0),   # 测试不做真实轮询
                clock=lambda: EXIT_NOW_MS / 1000.0)
    return leg, store, exchange


def _enable(store: StateStore, ts: float = None):
    from app.views import set_mr_enabled
    set_mr_enabled(store, True, by="test", ts=ts)


class TestMrDisabled(unittest.TestCase):
    def test_no_event_means_disabled_no_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp)
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertFalse(status["enabled"])
            self.assertEqual(status["state"], "disabled")
            self.assertEqual(exchange.orders, [])


class TestMrEntry(unittest.TestCase):
    def test_entry_places_single_market_order_and_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["state"], "entered")
            self.assertEqual(len(exchange.orders), 1)
            order = exchange.orders[0]
            self.assertTrue(order["is_entry"])
            self.assertEqual(order["market_id"], 129)
            # 名义 = 500×0.5 = 250；ref = 04-15 会话收盘 93.0；qty = 250/93 = 2.6881
            self.assertAlmostEqual(order["qty_int"] / 10000.0, 2.6881, places=4)
            row = store.mr_open_trade()
            self.assertIsNotNone(row)
            self.assertEqual(row["signal_date"], "2026-04-15")
            self.assertEqual(row["entry_session"], "2026-04-16")
            # 数量 floor 到 4 位 ⇒ 名义略小于计划值；台账记的是交易所口径
            self.assertAlmostEqual(row["notional"], 250.0, delta=0.02)
            self.assertAlmostEqual(row["qty"], 2.6881, places=4)

    def test_second_tick_same_signal_does_not_reorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            _run(leg.on_tick(now_ms=ENTRY_NOW_MS + 60_000))
            self.assertEqual(len(exchange.orders), 1)

    def test_insufficient_equity_records_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, equity=16.0, market_now=ENTRY_NOW_MS)
            _enable(store)
            _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(exchange.orders, [])
            rows = store.mr_recent_trades(5)
            self.assertEqual(rows[0]["state"], "SKIPPED")

    def test_global_kill_blocks_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            store.log_event("admin_kill", "kill on", data={"kill": True, "by": "t"})
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(exchange.orders, [])
            self.assertTrue(status["kill"])


class TestMrExit(unittest.TestCase):
    def test_exit_closes_ledger_with_funding_and_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            _enable(store)
            trade_id = store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="OID-0",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            status = _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            self.assertEqual(len(exchange.orders), 1)
            order = exchange.orders[0]
            self.assertFalse(order["is_entry"])
            self.assertEqual(order["qty_int"], 26737)
            row = store.mr_recent_trades(1)[0]
            self.assertEqual(row["state"], "CLOSED")
            self.assertEqual(row["exit_signal_date"], "2026-04-17")
            # funding_pct = 0.03/250×100 = 0.012（贴近 0.0004%/h × 30.5h 基线）
            self.assertAlmostEqual(row["funding_pct"], 0.012, places=6)
            # net = (95.5 − 93.5)×2.6737 − 0.03
            self.assertAlmostEqual(row["net_pnl"], 5.3174, places=3)
            self.assertEqual(status["state"], "exited")
            self.assertEqual(trade_id, row["id"])


class TestMrRecon(unittest.TestCase):
    def test_exchange_position_without_ledger_is_recon_err(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="1.0")
            _enable(store)
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["recon"], "err")
            self.assertEqual(status["state"], "recon_err")
            self.assertEqual(exchange.orders, [])

    def test_ledger_open_without_exchange_position_recon_err(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp)
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-01", entry_session="2026-04-02", qty=1.0,
                qty_int=10000, entry_price=90.0, entry_order_id="x",
                entry_ts=0.0, notional=90.0, margin=90.0, equity_at_entry=500.0)
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["recon"], "err")
            self.assertEqual(exchange.orders, [])

    def test_fresh_entry_tolerated_before_position_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp)
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=1.0,
                qty_int=10000, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0 - 60, notional=93.5,
                margin=93.5, equity_at_entry=500.0)
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["recon"], "ok")


class TestMrKillButExitWorks(unittest.TestCase):
    def test_kill_blocks_entry_but_exit_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            store.log_event("admin_kill", "kill on", data={"kill": True, "by": "t"})
            status = _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            self.assertEqual(len(exchange.orders), 1)     # 离场不受 kill 影响
            self.assertEqual(status["state"], "exited")


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.run(coro)


class TestMrReviewP3(unittest.TestCase):
    """P3 对抗审核（2026-11-01 第二轮）新增契约：

    * 下单被拒 → 不写台账（否则假 OPEN 行会把下一轮逼进 recon_err）；
    * 停用 + 持仓 → 照常离场（停用只挡入场，不能困住仓位）；
    * funding 超基线 2 倍 → 自动停用 MR 腿（写 mr_admin 事件）；
    * executor sizing 基数 = 账户权益 − MR 占用（_MR_MARKET_ID 与 MrConfig 一致）。
    """

    def test_entry_rejected_no_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            async def reject(**kwargs):
                exchange.orders.append(kwargs)
                return SimpleNamespace(order_id="", accepted=False, ok=False,
                                       error="post only would cross", kind="rejected")
            exchange.place_market = reject
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["state"], "order_rejected")
            self.assertIsNone(store.mr_open_trade())

    def test_exit_rejected_keeps_holding(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            async def reject(**kwargs):
                exchange.orders.append(kwargs)
                return SimpleNamespace(order_id="", accepted=False, ok=False,
                                       error="boom", kind="rejected")
            exchange.place_market = reject
            status = _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            self.assertEqual(status["state"], "order_rejected")
            self.assertIsNotNone(store.mr_open_trade())   # 仍 OPEN，下轮重试

    def test_disabled_while_holding_still_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            # 不 _enable（停用态）但有持仓 → 必须照常评估离场，不许困仓
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            status = _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            self.assertEqual(len(exchange.orders), 1)
            self.assertEqual(status["state"], "exited")

    def test_disabled_and_flat_skips_candle_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp)
            calls = []
            leg.market_client = SimpleNamespace(
                candles_1h=lambda *a, **k: calls.append(1) or [])
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["state"], "disabled")
            self.assertEqual(calls, [])                    # 停用+空仓不拉 K 线

    def test_funding_over_twice_baseline_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            # 交易所侧本仓累计 funding = 5.0 USDC（基线 30.5h ≈ 0.012%）→ 触发
            rich = ExchangePosition(
                symbol="QQQ", side=1, qty=Decimal("2.6737"), market_id=129,
                unrealized_pnl=3.0, position_value=250.0, allocated_margin=250.0,
                funding_paid=5.0)
            leg.reader = FakeReader(ExchangeAccount(
                equity=500.0, positions=(rich,), orders=(), orders_error="",
                as_of=0.0, transaction_time=0.0))
            exchange.reader = leg.reader              # 假交易所要用同一份读数改仓位
            status = _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            self.assertEqual(status["state"], "exited")
            self.assertFalse(leg._mr_enabled())            # 自动停用已生效

    def test_exit_uses_exchange_qty_on_partial_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="1.5")   # 部分成交：实际 1.5
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            status = _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            self.assertEqual(status["state"], "exited")
            self.assertEqual(exchange.orders[0]["qty_int"], 15000)  # 按实际持仓平

    def test_funding_near_baseline_keeps_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            status = _run(leg.on_tick(now_ms=EXIT_NOW_MS))  # 默认 funding_paid=0.25
            self.assertEqual(status["state"], "exited")
            self.assertTrue(leg._mr_enabled())


class TestMrFillConfirmation(unittest.TestCase):
    """2026-11-02 复核 P1：「受理 ≠ 成交」——成交只认交易所持仓。

    Lighter 的 `ok=True` 只代表 code=200 进了 sequencer；拿它当成交会写出
    假 OPEN（零成交）或假 CLOSED（离场零成交 ⇒ 台账说空仓、交易所还有裸多仓）。
    """

    def test_entry_accepted_but_no_fill_writes_no_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            exchange.fill = False                       # 受理成功，但交易所没有仓
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["state"], "entry_not_filled")
            self.assertIsNone(store.mr_open_trade())    # 不写假台账
            # 下一轮（仍在窗口内）会重试，用新的 coi
            again = _run(leg.on_tick(now_ms=ENTRY_NOW_MS + 30 * 60_000))
            self.assertEqual(again["state"], "entry_not_filled")
            self.assertEqual(len(exchange.orders), 2)
            self.assertNotEqual(exchange.orders[0]["client_order_index"],
                                exchange.orders[1]["client_order_index"])

    def test_entry_partial_fill_records_exchange_qty(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            exchange.fill_qty = 1.5                     # 计划 2.6881，实际 1.5
            exchange.fill_price = 93.4                  # 交易所成交价（非参考价）
            _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            row = store.mr_open_trade()
            self.assertAlmostEqual(row["qty"], 1.5, places=6)
            self.assertAlmostEqual(row["entry_price"], 93.4, places=6)
            self.assertAlmostEqual(row["notional"], 1.5 * 93.4, places=6)
            self.assertIsNotNone(
                store.events(kind="mr_entry_partial_fill", limit=1))

    def test_entry_waits_for_position_to_appear(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            leg.config = MrConfig(fill_confirm_s=0.05, fill_poll_s=0.01)
            exchange.fill = False                     # 不自动成交，靠 reader 逐次给真相
            # 第 1 次读（tick 开头）空仓；第 2 次读（确认轮询）才看到仓
            leg.reader.pending = [_account(500.0), _account(500.0, mr_qty="2.6881")]
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["state"], "entered")
            self.assertIsNotNone(store.mr_open_trade())
            self.assertGreaterEqual(leg.reader.calls, 2)  # 真的多读了一次

    def test_exit_accepted_but_position_remains_keeps_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            exchange.fill = False                       # 离场受理但 IOC 零成交
            status = _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            self.assertEqual(status["state"], "exit_not_filled")
            self.assertIsNotNone(store.mr_open_trade())  # 仍是 OPEN（不许写假 CLOSED）
            self.assertEqual(store.mr_open_trade()["state"], "OPEN")

    def test_alert_is_throttled_on_repeated_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)

            async def reject(**kwargs):
                exchange.orders.append(kwargs)
                return SimpleNamespace(order_id="", accepted=False, ok=False,
                                       error="boom", kind="rejected")
            exchange.place_market = reject
            _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            first = len(store.events(kind="mr_admin_alert", limit=50))
            _run(leg.on_tick(now_ms=EXIT_NOW_MS + 300_000))
            second = len(store.events(kind="mr_admin_alert", limit=50))
            self.assertEqual(first, second)             # 30 分钟内不重复告警


class TestMrConfirmationResilience(unittest.TestCase):
    """2026-11-02 第四轮复核：确认窗口容错 + 丢失成交的认领。

    旧实现里确认轮询不捕读错误：一次 503 就把 tick 打崩，已成交的仓没有台账 →
    下一轮 recon_err 冻结（MR 无止损，入场方向就是裸多）。
    """

    def test_confirmation_survives_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            leg.config = MrConfig(fill_confirm_s=0.05, fill_poll_s=0.01)
            exchange.fill = False                     # 不自动成交，靠 reader 逐次给真相
            leg.reader.pending = [_account(500.0),
                                  _account(500.0, mr_qty="2.6881")]
            leg.reader.fail_calls = {2}               # 确认轮询第一次读就撞 503
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["state"], "entered")
            self.assertIsNotNone(store.mr_open_trade())

    def test_entry_adopted_after_confirmation_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            exchange.fill = False                     # 受理成功，但确认窗口内看不到仓
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["state"], "entry_not_filled")
            self.assertIsNone(store.mr_open_trade())
            # 下一轮：交易所其实有仓（成交回报/确认读失败）→ 凭意图认领，不再 recon_err
            leg.reader.account = _account(500.0, mr_qty="2.6881")
            again = _run(leg.on_tick(now_ms=ENTRY_NOW_MS + 300_000))
            self.assertEqual(again.get("adopted"), "entry")
            self.assertEqual(again["state"], "holding")   # 补记后本轮即按持仓评估离场
            row = store.mr_open_trade()
            self.assertIsNotNone(row)
            self.assertAlmostEqual(row["qty"], 2.6881, places=6)
            self.assertTrue(store.events(kind="mr_entry_adopted", limit=1))

    def test_exit_adopted_after_confirmation_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            exchange.fill = False                     # 卖单受理，但确认窗口内仍读到仓
            status = _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            self.assertEqual(status["state"], "exit_not_filled")
            self.assertEqual(store.mr_open_trade()["state"], "OPEN")
            # 下一轮：交易所已空仓（平仓成交了，只是确认丢了）→ 凭离场意图补记 CLOSED
            leg.reader.account = _account(500.0)
            again = _run(leg.on_tick(now_ms=EXIT_NOW_MS + 300_000))
            self.assertEqual(again.get("adopted"), "exit")
            self.assertIsNone(store.mr_open_trade())
            row = store.mr_recent_trades(1)[0]
            self.assertEqual(row["state"], "CLOSED")
            self.assertEqual(row["exit_reason"], "sma5_adopted")
            self.assertTrue(store.events(kind="mr_exit_adopted", limit=1))

    def test_blind_confirmation_never_closes_position(self):
        """确认窗口整轮读不到真相 ≠ 仓位已消失：绝不许写出假 CLOSED。"""
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            leg.reader.fail_calls = set(range(2, 40))   # 第 1 次（tick 开头）读到仓，之后全失败
            status = _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            self.assertEqual(status["state"], "exit_not_filled")
            self.assertEqual(store.mr_open_trade()["state"], "OPEN")

    def test_adoption_requires_matching_intent(self):
        """没有下单意图的未知仓仍然 fail-closed（不自动吃掉）。"""
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="1.0")
            _enable(store)
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertEqual(status["state"], "recon_err")
            self.assertIsNone(store.mr_open_trade())

    def test_same_day_missed_window_marks_skipped(self):
        after_window = int(datetime.fromisoformat(
            "2026-04-16T20:00:00+00:00").timestamp() * 1000)
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=after_window)
            # 启用事件必须早于该信号的截止时刻，否则「停用期间过期」去噪会跳过它
            _enable(store, ts=ENTRY_NOW_MS / 1000.0 - 86400.0)
            _run(leg.on_tick(now_ms=after_window))
            row = store.mr_trade_by_signal("2026-04-15")
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "SKIPPED")


class TestExecutorGuard(unittest.TestCase):
    """executor 的 sizing 基数剔除 MR 占用；_MR_MARKET_ID 与 MrConfig 钉死一致。"""

    def test_mr_market_id_pin(self):
        from app import executor as ex_mod
        self.assertEqual(ex_mod._MR_MARKET_ID, MrConfig().market_id)

    def test_crypto_equity_excludes_mr(self):
        from app import executor as ex_mod
        ex = ex_mod.Executor.__new__(ex_mod.Executor)
        ex._equity = 500.0
        ex._mr_contribution = 250.0
        self.assertEqual(ex._crypto_equity, 250.0)
        ex._equity = None
        self.assertIsNone(ex._crypto_equity)

    def test_mr_contribution_of(self):
        from app import executor as ex_mod
        ex = ex_mod.Executor.__new__(ex_mod.Executor)
        acct = _account(500.0, mr_qty="2.6737")       # position_value 250 / allocated 250
        # 2026-11-02 第五轮：占用改为**名义**（价格敞口），不再 allocated+uPnL
        self.assertAlmostEqual(ex._mr_contribution_of(acct), 250.0)
        self.assertAlmostEqual(ex._mr_contribution_of(_account()), 0.0)

    def test_mr_contribution_is_notional_not_margin(self):
        """5x 下保证金只占名义的 1/5 —— 扣保证金会把 crypto 腿放大 1.8 倍。"""
        from app import executor as ex_mod
        ex = ex_mod.Executor.__new__(ex_mod.Executor)
        pos = ExchangePosition(symbol="QQQ", side=1, qty=Decimal("2.6737"), market_id=129,
                               unrealized_pnl=0.0, position_value=250.0,
                               allocated_margin=50.0)      # 5x 逐仓口径
        acct = ExchangeAccount(equity=500.0, positions=(pos,), orders=(),
                               orders_error="", as_of=0.0, transaction_time=0.0)
        self.assertAlmostEqual(ex._mr_contribution_of(acct), 250.0)
        # 没有 position_value 时才回退「保证金 + uPnL」
        pos2 = ExchangePosition(symbol="QQQ", side=1, qty=Decimal("2.6737"), market_id=129,
                                unrealized_pnl=1.0, position_value=None,
                                allocated_margin=50.0)
        acct2 = ExchangeAccount(equity=500.0, positions=(pos2,), orders=(),
                                orders_error="", as_of=0.0, transaction_time=0.0)
        self.assertAlmostEqual(ex._mr_contribution_of(acct2), 51.0)

    def test_risk_state_equity_matches_sizing_equity(self):
        """MR 持仓时风控权益必须与定仓基数同源（否则每次开仓都撞 equity_mismatch）。"""
        from types import SimpleNamespace
        from app import executor as ex_mod
        from app import strategy
        from app.lifecycle import decide
        from app.risk import RiskState

        ex = ex_mod.Executor.__new__(ex_mod.Executor)
        ex._equity = 497.04
        ex._mr_contribution = 251.0
        snap = SimpleNamespace(to_risk_state=lambda: RiskState(
            equity=497.04, day_start_equity=497.04, trades_today=0,
            open_positions=(), open_notional_total=0.0,
            open_notional_by_symbol={}, dd_index=1.0, dd_peak_index=1.0))
        state = ex._risk_state_for_sizing(snap)
        self.assertAlmostEqual(state.equity, 246.04, places=6)
        # 2026-11-02 第五轮：日初权益必须同幅下移 —— 否则日亏闸把「基数缩小」当成亏损
        self.assertAlmostEqual(state.day_start_equity, 246.04, places=6)

        bars = [strategy.Bar(open_ms=i * 4 * 3600_000, open=100.0, high=101.0,
                             low=99.0, close=100.0) for i in range(3)]
        sig = strategy.Signal(symbol="BTC", bar_index=2, bar_open_ms=bars[2].open_ms,
                              entry_price=100.0, stop_price=98.0, stop_dist=0.02,
                              ma_max=101.0, ma_min=99.0, band=0.02, side=1)
        decision = decide("BTC", bars, i=2, signal=sig, equity=ex._crypto_equity,
                          market_info=None, risk_state=state)
        codes = [r.code for r in decision.rejections]
        self.assertNotIn("equity_mismatch", codes)
        self.assertNotIn("daily_loss", codes)


class TestFundGateAndResiduals(unittest.TestCase):
    """2026-11-02 第五轮：MR 必须吃基金级闸门 + 三条残余修复的回归。"""

    @staticmethod
    def _state(equity: float = 500.0, day_start: float = 500.0,
               dd_index: float = 1.0, dd_peak: float = 1.0):
        from app.risk import RiskState
        return RiskState(equity=equity, day_start_equity=day_start, trades_today=0,
                         open_positions=(), open_notional_total=0.0,
                         open_notional_by_symbol={},
                         dd_index=dd_index, dd_peak_index=dd_peak)

    def test_drawdown_kill_blocks_entry(self):
        """回撤熔断（DD kill）必须挡 MR 入场 —— 旧版只读 admin_kill，形同没有。"""
        from app.risk import RiskLimits
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            state = self._state(dd_index=0.65, dd_peak=1.0)   # −35% > 30% 熔断线
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS, risk_state=state,
                                      limits=RiskLimits()))
            self.assertEqual(status["state"], "blocked_drawdown")
            self.assertEqual(exchange.orders, [])                    # 一张单都没发
            self.assertIsNone(store.mr_trade_by_signal("2026-04-15"))  # 不写 SKIPPED
            self.assertEqual(status["fund_gate"], "risk_state")

    def test_daily_loss_blocks_entry(self):
        from app.risk import RiskLimits
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            state = self._state(equity=450.0, day_start=500.0)  # −10% > 5%
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS, risk_state=state,
                                      limits=RiskLimits()))
            self.assertEqual(status["state"], "blocked_daily_loss")
            self.assertEqual(exchange.orders, [])

    def test_fill_confirm_bounded_by_wall_clock(self):
        """确认轮询必须有墙钟上限：慢读 / 重试不能把 `_tick_lock` 顶住几分钟。"""
        import asyncio
        import time as _time

        class SlowFail:
            def __init__(self):
                self.n = 0

            async def read(self):
                self.n += 1
                await asyncio.sleep(0.15)
                raise RuntimeError("503")

        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp)
            slow = SlowFail()
            leg.reader = slow
            leg.config = MrConfig(fill_confirm_s=0.3, fill_poll_s=0.05)
            started = _time.monotonic()
            pos, ok = _run(leg._await_position(want=True))
            elapsed = _time.monotonic() - started
            self.assertIsNone(pos)
            self.assertFalse(ok)
            # 无墙钟闸时 = 7 次 × 0.15s ≈ 1.05s；有闸门应在预算附近收手
            self.assertLess(elapsed, 0.9)
            self.assertLessEqual(slow.n, 3)

    def test_partial_exit_pnl_includes_filled_leg(self):
        """部分成交离场：先平掉那一截的盈亏也要进台账。"""
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, mr_qty="2.6737")
            _enable(store)
            store.mr_record_entry(
                signal_date="2026-04-15", entry_session="2026-04-16", qty=2.6737,
                qty_int=26737, entry_price=ENTRY_DAY_CLOSE, entry_order_id="x",
                entry_ts=ENTRY_NOW_MS / 1000.0, notional=250.0, margin=250.0,
                equity_at_entry=500.0)
            original = exchange.place_market

            async def partial(**kwargs):
                exchange.orders.append(kwargs)
                exchange.reader.account = _account(500.0, mr_qty="1.6737")  # 只平掉 1.0
                return SimpleNamespace(order_id="O1", accepted=True, ok=True, kind="ok")

            exchange.place_market = partial
            first = _run(leg.on_tick(now_ms=EXIT_NOW_MS))
            self.assertEqual(first["state"], "exit_not_filled")
            row = store.mr_trade_by_signal("2026-04-15")
            parts = json.loads(row["raw"])["partial_exits"]
            self.assertAlmostEqual(parts[0]["qty"], 1.0, places=6)
            self.assertEqual(row["state"], "OPEN")

            exchange.place_market = original
            _run(leg.on_tick(now_ms=EXIT_NOW_MS + 300_000))
            row = store.mr_trade_by_signal("2026-04-15")
            self.assertEqual(row["state"], "CLOSED")
            # 两段之和 = (95.5 − 93.5) × 2.6737 − funding（fixture 的 0.03）
            self.assertAlmostEqual(row["net_pnl"], 2.0 * 2.6737 - 0.03, places=4)
            self.assertAlmostEqual(row["exit_price"], 95.5, places=6)

    def test_adoption_rejects_short_position(self):
        """交易所有空头仓时，绝不按 MR 入场认领（MR 只做多）。"""
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, exchange = _leg(tmp, market_now=ENTRY_NOW_MS)
            _enable(store)
            exchange.fill = False                     # 先留一条入场意图、零成交
            _run(leg.on_tick(now_ms=ENTRY_NOW_MS))
            self.assertTrue(store.events(kind="mr_entry_intent", limit=1))
            short = ExchangePosition(symbol="QQQ", side=-1, qty=Decimal("2.0"),
                                     market_id=129, avg_entry_price=93.0,
                                     position_value=186.0, allocated_margin=186.0)
            leg.reader.account = ExchangeAccount(
                equity=500.0, positions=(short,), orders=(), orders_error="",
                as_of=0.0, transaction_time=0.0)
            status = _run(leg.on_tick(now_ms=ENTRY_NOW_MS + 60_000))
            self.assertIsNone(store.mr_open_trade())
            self.assertEqual(status["recon"], "err")

    def test_mr_order_limit_direction(self):
        """买单的最差价必须在参考价**上方**（写反 = IOC 永远零成交）。"""
        from app.exchange import build_mr_market_order
        entry = build_mr_market_order(symbol="QQQ", market_id=129, qty_int=158,
                                      price_decimals=2, ref_price=100.0, is_entry=True,
                                      now_ms=1, slippage_bps=15, client_order_index=123)
        self.assertGreater(entry.price_int, 10000)
        self.assertFalse(entry.reduce_only)
        exit_ = build_mr_market_order(symbol="QQQ", market_id=129, qty_int=158,
                                      price_decimals=2, ref_price=100.0, is_entry=False,
                                      now_ms=1, slippage_bps=15, client_order_index=124)
        self.assertLess(exit_.price_int, 10000)
        self.assertTrue(exit_.reduce_only)

    def test_dropped_session_lands_one_event(self):
        """被末根闸丢弃的**已收盘**会话要留审计行；当天盘中半截会话不报、不重复报。"""
        with tempfile.TemporaryDirectory() as tmp:
            leg, store, _ = _leg(tmp, market_now=ENTRY_NOW_MS)
            dropped = [{"date": "2026-04-15", "bars": 4, "last_bar_et": "13:00",
                        "reason": "missing_close_bar"}]
            status = {}
            leg._note_dropped_sessions(dropped, ENTRY_NOW_MS, status)
            self.assertEqual(len(store.events(kind="mr_session_dropped", limit=5)), 1)
            self.assertEqual(status["dropped_sessions"], ["2026-04-15"])
            leg._note_dropped_sessions(dropped, ENTRY_NOW_MS + 60_000, {})
            self.assertEqual(len(store.events(kind="mr_session_dropped", limit=5)), 1)
            # 还没收盘的会话（当天 10:00 ET）不报
            mid_session = [{"date": "2026-04-16", "bars": 1, "last_bar_et": "10:00",
                            "reason": "too_few_bars"}]
            leg._note_dropped_sessions(mid_session,
                                       ENTRY_NOW_MS - 6 * 3600_000, {})
            self.assertEqual(len(store.events(kind="mr_session_dropped", limit=5)), 1)


if __name__ == "__main__":
    unittest.main()
