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

    async def read(self) -> ExchangeAccount:
        self.calls += 1
        return self.account


class FakeExchange:
    def __init__(self):
        self.orders = []
        self.isolated = []

    async def ensure_isolated(self, *, market_id: int, leverage: float):
        self.isolated.append((market_id, leverage))

    async def place_market(self, **kwargs):
        self.orders.append(kwargs)
        return SimpleNamespace(order_id=f"OID-{len(self.orders)}", accepted=True)


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
            funding_paid=0.25),)
    return ExchangeAccount(equity=equity, positions=positions, orders=(),
                           orders_error="", as_of=0.0, transaction_time=0.0)


def _leg(tmpdir: str, *, equity=500.0, mr_qty="0", market_now=EXIT_NOW_MS):
    store = StateStore(f"{tmpdir}/mr_{abs(hash(tmpdir)) % 99999}.db")
    exchange = FakeExchange()
    market = FakeMarket()
    market.now_ms = market_now
    leg = MrLeg(store=store, exchange=exchange, reader=FakeReader(_account(equity, mr_qty=mr_qty)),
                market_client=market, config=MrConfig(),
                clock=lambda: EXIT_NOW_MS / 1000.0)
    return leg, store, exchange


def _enable(store: StateStore):
    from app.views import set_mr_enabled
    set_mr_enabled(store, True, by="test")


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
            self.assertAlmostEqual(row["notional"], 250.0, places=6)

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
            # funding_pct = 0.25/250×100 = 0.1
            self.assertAlmostEqual(row["funding_pct"], 0.1, places=6)
            # net = (95.5 − 93.5)×2.6737 − 0.25
            self.assertAlmostEqual(row["net_pnl"], 5.0974, places=3)
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


if __name__ == "__main__":
    unittest.main()
