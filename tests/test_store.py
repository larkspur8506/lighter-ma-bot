"""`app/store.py` 的契约测试 —— **全离线**（临时目录 + `:memory:`，无网络）。

钉住的东西（T5 验收项）：
  * 建库 + `PRAGMA user_version` 迁移：新库、老库升级、迁移失败回滚、库比代码新 → 拒绝；
  * 往返读写：开/平仓、成交、权益曲线、每日计数、回撤锚点、事件流；
  * **原子性**：写一半失败不留下「有 OPEN 仓位但没有开仓成交」的半状态；
  * **幂等**：同一 order id / fill id 重复写入，行数不变；部分成交**不**被误合并；
  * **同 bar 一笔开仓**：同 symbol+bar_ms 的第二笔 entry 成交绝不新增成交行（同价量 =
    重复同步，价量方向不同 = `EntryConflict` fail-loud），保证 `sum(entry qty) == position.qty`；
  * WAL + `busy_timeout`：第二个连接（面板进程）能读；写锁被占时报 busy 而不是损坏；
  * 崩溃恢复：不 checkpoint 直接关库，重开后状态正确；
  * 回撤锚点：30 天清理只删 `dd_anchors`、**永不碰** `dd_state`，峰值不能被静默调低。

期望值全部手算写死（不拿被测代码算完再断言它自己）。
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from app import store as store_module
from app.store import (
    ALLOWED_TRANSITIONS, MIGRATIONS, SCHEMA_VERSION, EntryConflict, InvalidTransition,
    MigrationError, SchemaError, StateStore, StoreError, day_bounds, trade_key, utc_day,
)

# 固定时间戳：1760000000 = 2025-10-09T08:53:20Z，落在 UTC 日 '2025-10-09'。
TS = 1760000000.0
DAY = "2025-10-09"
BAR_MS = 1760000000000        # 同一根 4h bar 的毫秒时间戳

# 一笔 BTC 多单：0.04 BTC @ 80000，止损 78000（整数 tick 表示）+ 对应浮点值。
ENTRY = dict(symbol="BTC", side=1, qty_int=4000, size_decimals=5,
             entry_price_int=800000, price_decimals=1, stop_price_int=780000,
             bar_ms=BAR_MS, market_id=1, entry_order_id="OID-ENTRY-1",
             stop_order_id="OID-STOP-1", equity_at_entry=10000.0, ts=TS)


def entry_plan(**overrides):
    plan = dict(ENTRY)
    plan.update(overrides)
    return plan


class StoreTestCase(unittest.TestCase):
    """公共夹具：一个临时目录里的文件库（WAL 需要真实文件，`:memory:` 没有 WAL）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "state.db")
        self.store = StateStore(self.path)
        self.addCleanup(self.store.close)

    def open_second_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn


# ------------------------------------------------------------------ 建库 / 迁移


class SchemaTests(StoreTestCase):
    def test_fresh_database_is_at_current_version(self):
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 4)

    def test_v4_adds_stop_expiry_column(self):
        """v4：止损到期时刻（Lighter 的 orderExpiry 到期废单，守护据此临期换单）。"""
        cols = {row["name"] for row in self.store._fetchall("PRAGMA table_info(positions)")}
        self.assertIn("stop_expiry_ts", cols)

    def test_set_stop_order_updates_identity_and_expiry(self):
        pid = self.store.record_entry(
            "BTC", 1, qty_int=100_000, size_decimals=5, entry_price_int=650_000,
            price_decimals=2, stop_price_int=630_000, bar_ms=1_600_000_000_000,
            market_id=1, stop_order_id="coi:1", ts=1000.0).position_id
        out = self.store.set_stop_order(pid, stop_order_id="coi:2",
                                        stop_expiry_ts=1_700_000_000.0, ts=1001.0)
        self.assertEqual(out["stop_order_id"], "coi:2")
        self.assertEqual(out["stop_expiry_ts"], 1_700_000_000.0)
        self.assertEqual(out["state"], "OPEN")
        # 不知道到期时刻时不许写成 0（0 会被临期判定误读成「早就过期」）
        out = self.store.set_stop_order(pid, stop_order_id="coi:3", ts=1002.0)
        self.assertIsNone(out["stop_expiry_ts"])

    def test_all_tables_and_indexes_exist(self):
        names = {row["name"] for row in self.store._fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'")
            if not row["name"].startswith("sqlite_")}
        self.assertEqual(names, {"positions", "trades", "equity_curve", "daily_counters",
                                 "dd_anchors", "dd_state", "events", "account_flows"})
        indexes = {row["name"] for row in self.store._fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")}
        self.assertIn("idx_trades_symbol_ts", indexes)
        self.assertIn("idx_positions_symbol_state", indexes)
        self.assertIn("idx_account_flows_ts", indexes)
        self.assertIn("ux_account_flows_dedupe", indexes)

    def test_positions_columns_include_v2_additions(self):
        cols = {row["name"] for row in self.store._fetchall("PRAGMA table_info(positions)")}
        self.assertTrue({"symbol", "side", "qty_int", "entry_price_int", "stop_price_int",
                         "bar_ms", "state", "opened_at", "closed_at"} <= cols)
        # v2 迁移补的列
        self.assertTrue({"exit_price", "exit_price_int", "exit_order_id",
                         "client_order_index"} <= cols)

    def test_migrate_is_idempotent(self):
        self.assertEqual(self.store.migrate(), SCHEMA_VERSION)
        self.assertEqual(self.store.migrate(), SCHEMA_VERSION)
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)

    def test_old_v1_database_is_upgraded_in_place(self):
        """老库（user_version=1，没有 v2 的列）能被新代码直接打开并保留数据。"""
        path = os.path.join(self._tmp.name, "v1.db")
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        for statement in MIGRATIONS[1]:
            conn.execute(statement)
        conn.execute("PRAGMA user_version=1")
        conn.execute("COMMIT")
        conn.execute(
            """INSERT INTO positions(symbol, side, state, bar_ms, qty_int, qty, size_decimals,
                    entry_price_int, entry_price, price_decimals, created_at, updated_at)
               VALUES('ETH', -1, 'OPEN', 123456789, 20000, 2.0, 4, 250000, 2500.0, 2, 1.0, 1.0)""")
        v1_columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
        self.assertNotIn("exit_price", v1_columns)
        conn.close()

        upgraded = StateStore(path)
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.schema_version, SCHEMA_VERSION)
        row = upgraded.position(1)
        self.assertEqual(row["symbol"], "ETH")
        self.assertEqual(row["state"], "OPEN")
        self.assertEqual(row["exit_price"], None)          # 新列，老行留 NULL 而不是编一个值
        self.assertEqual(upgraded.open_symbols(), ("ETH",))
        # 升级后的库能正常走 v2 才有的代码路径（平仓写 exit_price/exit_price_int）
        result = upgraded.record_exit(1, exit_price_int=245000, exit_order_id="X", ts=TS)
        self.assertEqual(result["state"], "CLOSED")
        self.assertEqual(upgraded.position(1)["exit_price"], 2450.0)

    def test_database_newer_than_code_is_refused(self):
        self.store.conn.execute("PRAGMA user_version=99")
        with self.assertRaises(SchemaError):
            StateStore(self.path)

    def test_failed_migration_rolls_back_completely(self):
        """迁移中途语法错 → MigrationError，且**一个字母都没落库**（版本号仍是 0）。"""
        path = os.path.join(self._tmp.name, "broken.db")
        broken = {"1": ("CREATE TABLE t_ok (a INTEGER)",
                        "CREATE TABLE t_bad (")}
        with mock.patch.dict(store_module.MIGRATIONS, broken, clear=True):
            with self.assertRaises(MigrationError):
                StateStore(path)
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("t_ok", tables)

    def test_state_machine_transition_table_is_the_documented_one(self):
        self.assertEqual(ALLOWED_TRANSITIONS["IDLE"], ("OPEN",))
        self.assertEqual(ALLOWED_TRANSITIONS["OPEN"], ("CLOSING", "CLOSED"))
        self.assertEqual(ALLOWED_TRANSITIONS["CLOSING"], ("OPEN", "CLOSED"))
        self.assertEqual(ALLOWED_TRANSITIONS["CLOSED"], ())

    def test_illegal_transition_is_rejected(self):
        pid = self.store.record_entry(**entry_plan())["position_id"]
        self.store.record_exit(pid, exit_price_int=820000, ts=TS + 10)
        with self.assertRaises(InvalidTransition):
            self.store.set_position_state(pid, "OPEN")

    def test_second_position_on_same_symbol_is_rejected(self):
        self.store.record_entry(**entry_plan())
        with self.assertRaises(InvalidTransition):
            self.store.record_entry(**entry_plan(bar_ms=BAR_MS + 14400000))

    def test_open_positions_includes_closing(self):
        pid = self.store.record_entry(**entry_plan())["position_id"]
        self.store.begin_close(pid, ts=TS + 60)
        self.assertEqual(self.store.open_symbols(), ("BTC",))
        self.assertEqual(self.store.position(pid)["state"], "CLOSING")

    def test_from_config_reads_sqlite_url(self):
        class FakeConfig:
            db_path = "/tmp/ignored.db"
            db_url = "sqlite:///" + self.path
            sqlite_path = None

        store = StateStore.from_config(FakeConfig())
        self.addCleanup(store.close)
        self.assertEqual(store.path, self.path)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)

    def test_from_config_refuses_non_sqlite_url(self):
        class FakeConfig:
            db_path = "/tmp/x.db"
            db_url = "postgres://user:pw@host/db"
            sqlite_path = None

        with self.assertRaises(StoreError):
            StateStore.from_config(FakeConfig())

    def test_memory_database_works_and_reports_memory_journal(self):
        mem = StateStore(":memory:")
        self.addCleanup(mem.close)
        self.assertEqual(mem.journal_mode, "memory")
        self.assertEqual(mem.schema_version, SCHEMA_VERSION)
        mem.record_entry(**entry_plan())
        self.assertEqual(mem.position_count(), 1)


# ------------------------------------------------------------------ 往返读写


class RoundTripTests(StoreTestCase):
    def test_entry_writes_position_trade_event_and_counter(self):
        result = self.store.record_entry(**entry_plan())
        self.assertTrue(result.inserted_position)
        self.assertTrue(result.inserted_trade)
        self.assertEqual(result.state, "OPEN")

        row = self.store.position(result.position_id)
        self.assertEqual(row["symbol"], "BTC")
        self.assertEqual(row["side"], 1)
        self.assertEqual(row["state"], "OPEN")
        self.assertEqual(row["qty_int"], 4000)
        self.assertEqual(row["qty"], 0.04)
        self.assertEqual(row["entry_price"], 80000.0)
        self.assertEqual(row["stop_price"], 78000.0)
        self.assertEqual(row["notional"], 3200.0)
        self.assertEqual(row["risk_amount"], 80.0)          # (80000-78000) × 0.04
        self.assertEqual(row["bar_ms"], BAR_MS)
        self.assertEqual(row["opened_at"], TS)
        self.assertIsNone(row["closed_at"])

        trade = self.store.trade(result.trade_id)
        self.assertEqual(trade["kind"], "entry")
        self.assertEqual(trade["price"], 80000.0)
        self.assertEqual(trade["qty"], 0.04)
        self.assertEqual(trade["order_id"], "OID-ENTRY-1")
        self.assertEqual(trade["position_id"], result.position_id)
        self.assertEqual(trade["idempotency_key"], "order:OID-ENTRY-1:1:800000:4000")

        counter = self.store.daily_counter(DAY)
        self.assertEqual(counter["trades_today"], 1)
        self.assertEqual(counter["day_start_equity"], 10000.0)

        events = self.store.events(limit=5)
        self.assertEqual(events[0]["kind"], "open")
        self.assertEqual(events[0]["symbol"], "BTC")

    def test_exit_closes_position_and_books_pnl(self):
        pid = self.store.record_entry(**entry_plan())["position_id"]
        result = self.store.record_exit(pid, exit_price_int=820000, exit_order_id="OID-EXIT-1",
                                        fee=3.2, ts=TS + 3600)
        self.assertEqual(result.state, "CLOSED")
        row = self.store.position(pid)
        self.assertEqual(row["state"], "CLOSED")
        self.assertEqual(row["closed_at"], TS + 3600)
        self.assertEqual(row["exit_price"], 82000.0)
        self.assertAlmostEqual(row["net_pnl"], 76.8, places=9)   # 80.0 - 3.2
        self.assertEqual(row["exit_reason"], "exit")
        self.assertEqual(self.store.open_symbols(), ())
        self.assertAlmostEqual(self.store.realized_pnl(), 76.8, places=9)
        self.assertAlmostEqual(self.store.daily_counter(DAY)["realized_pnl"], 76.8, places=9)
        # 平仓成交的 side 与仓位相反（reduce-only 腿）
        exit_trade = [t for t in self.store.trades(limit=10) if t["kind"] == "exit"][0]
        self.assertEqual(exit_trade["side"], -1)
        self.assertEqual(exit_trade["realized_pnl"], 76.8)

    def test_short_side_pnl_sign(self):
        pid = self.store.record_entry(**entry_plan(side=-1, stop_price_int=820000))["position_id"]
        self.store.record_exit(pid, exit_price_int=780000, ts=TS + 60)
        self.assertAlmostEqual(self.store.position(pid)["net_pnl"], 80.0, places=9)

    def test_stop_kind_is_recorded(self):
        pid = self.store.record_entry(**entry_plan())["position_id"]
        self.store.record_stop(pid, exit_price_int=780000, exit_order_id="OID-STOP-1",
                               reason="stop", ts=TS + 120)
        row = self.store.position(pid)
        self.assertEqual(row["state"], "CLOSED")
        self.assertEqual(row["exit_reason"], "stop")
        self.assertAlmostEqual(row["net_pnl"], -80.0, places=9)
        kinds = [t["kind"] for t in self.store.trades(limit=10)]
        self.assertEqual(sorted(kinds), ["entry", "stop"])

    def test_equity_curve_round_trip_and_ordering(self):
        self.store.record_equity(10000.0, ts=TS, twr_index=1.0)
        self.store.record_equity(10100.0, ts=TS + 60, twr_index=1.01, cum_flow=0.0)
        self.store.record_equity(9900.0, ts=TS + 120, twr_index=0.99)
        curve = self.store.equity_curve(limit=10)
        self.assertEqual([p["equity"] for p in curve], [10000.0, 10100.0, 9900.0])
        self.assertEqual(self.store.last_equity()["equity"], 9900.0)
        self.assertEqual(self.store.first_equity(since=TS)["ts"], TS)
        self.assertEqual(self.store.previous_equity(TS + 120)["equity"], 10100.0)

        # 同一个 ts 重复写 = 覆盖（面板/对账重复采样不该制造重复点）
        self.store.record_equity(9950.0, ts=TS + 120)
        self.assertEqual(self.store.equity_point_count(), 3)
        self.assertEqual(self.store.last_equity()["equity"], 9950.0)
        self.assertEqual(self.store.last_equity()["twr_index"], 0.99)   # 未被 NULL 覆盖掉

    def test_trades_query_filters(self):
        pid = self.store.record_entry(**entry_plan())["position_id"]
        self.store.record_trade(symbol="ETH", side=1, price=2500.0, qty=2.0, kind="entry",
                                order_id="OID-ETH-1", fill_id="FILL-ETH-1", ts=TS + 5)
        self.store.record_exit(pid, exit_price_int=820000, ts=TS + 60)
        self.assertEqual(len(self.store.trades(limit=10)), 3)
        self.assertEqual(len(self.store.trades(limit=10, symbol="ETH")), 1)
        self.assertEqual(len(self.store.trades(limit=10, kind="entry")), 2)
        self.assertEqual(len(self.store.trades(limit=10, since=TS + 30)), 1)
        self.assertEqual(len(self.store.trades(limit=10, position_id=pid)), 2)

    def test_reopen_after_abrupt_close_restores_state(self):
        """崩溃恢复：不 `checkpoint()`、不 `close()`，直接放手 → 重开状态正确。"""
        pid = self.store.record_entry(**entry_plan())["position_id"]
        self.store.record_equity(10000.0, ts=TS)
        self.store.save_dd_state(twr_index=1.0, twr_peak=1.25, anchor_ts=TS,
                                 anchor_equity=10000.0)
        self.assertTrue(os.path.exists(self.path + "-wal"))    # 写还在 WAL 里
        self.store.conn.close()                    # 模拟进程消失（没走 close()/checkpoint）
        self.store._closed = True                  # addCleanup 的 close() 变成 no-op

        reopened = StateStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version, SCHEMA_VERSION)
        self.assertEqual(reopened.open_symbols(), ("BTC",))
        self.assertEqual(reopened.position(pid)["entry_price"], 80000.0)
        self.assertEqual(reopened.trade_count(), 1)
        self.assertEqual(reopened.last_equity()["equity"], 10000.0)
        self.assertEqual(reopened.dd_state()["twr_peak"], 1.25)
        self.assertEqual(reopened.audit(), [])


# ------------------------------------------------------------------ 幂等


class IdempotencyTests(StoreTestCase):
    def test_trade_key_shapes(self):
        self.assertEqual(trade_key(fill_id="F1", order_id="O1", side=1, price_int=800000,
                                   qty_int=4000), "fill:F1")
        self.assertEqual(trade_key(order_id="O1", side=-1, price_int=800000, qty_int=4000),
                         "order:O1:-1:800000:4000")
        self.assertEqual(trade_key(order_id="O1", side=1, price=80000.0, qty=0.04),
                         "order:O1:1:80000:0.04")
        self.assertEqual(trade_key(symbol="btc", kind="entry", side=1, price_int=800000,
                                   qty_int=4000, ts_ms=1760000000000),
                         "local:BTC:entry:1760000000000:1:800000:4000")

    def test_same_order_id_twice_creates_one_trade(self):
        for _ in range(3):
            result = self.store.record_trade(symbol="BTC", side=1, price=80000.0, qty=0.04,
                                             kind="entry", order_id="OID-ENTRY-1",
                                             price_int=800000, qty_int=4000, ts=TS)
        self.assertEqual(self.store.trade_count(), 1)
        self.assertFalse(result.inserted_trade)
        self.assertTrue(result.idempotent)

    def test_same_fill_id_twice_creates_one_trade(self):
        for _ in range(3):
            result = self.store.record_trade(symbol="BTC", side=1, price=80000.0, qty=0.04,
                                             kind="entry", fill_id="FILL-42", order_id="OID-9",
                                             ts=TS)
        self.assertEqual(self.store.trade_count(), 1)
        self.assertFalse(result.inserted_trade)
        self.assertEqual(self.store.trade(result.trade_id)["fill_id"], "FILL-42")

    def test_partial_fills_of_one_order_are_not_merged(self):
        """同一订单号的两笔部分成交（价/量不同）必须是**两行**。"""
        self.store.record_trade(symbol="BTC", side=1, price=80000.0, qty=0.02, kind="entry",
                                order_id="OID-P", price_int=800000, qty_int=2000, ts=TS)
        self.store.record_trade(symbol="BTC", side=1, price=80100.0, qty=0.02, kind="entry",
                                order_id="OID-P", price_int=801000, qty_int=2000, ts=TS + 1)
        self.assertEqual(self.store.trade_count(), 2)

    def test_repeated_entry_for_same_bar_is_idempotent(self):
        first = self.store.record_entry(**entry_plan())
        again = self.store.record_entry(**entry_plan())
        third = self.store.record_entry(**entry_plan(entry_order_id="OID-ENTRY-1"))
        self.assertEqual(first.position_id, again.position_id)
        self.assertEqual(first.position_id, third.position_id)
        self.assertEqual(self.store.position_count(), 1)
        self.assertEqual(self.store.trade_count(), 1)     # 幂等键相同 → 不新增成交
        self.assertEqual(self.store.daily_counter(DAY)["trades_today"], 1)

    def test_repeated_exit_is_idempotent(self):
        pid = self.store.record_entry(**entry_plan())["position_id"]
        first = self.store.record_exit(pid, exit_price_int=820000, ts=TS + 60)
        again = self.store.record_exit(pid, exit_price_int=820000, ts=TS + 90)
        self.assertIsNotNone(first.trade_id)
        self.assertIsNone(again.trade_id)          # 已 CLOSED → 不再记成交
        self.assertTrue(again.idempotent)
        self.assertEqual(self.store.trade_count(), 2)      # entry + exit，无第三行
        self.assertEqual(self.store.position(pid)["closed_at"], TS + 60)   # 不被改写
        self.assertAlmostEqual(self.store.position(pid)["net_pnl"], 80.0, places=9)

    def test_trade_without_order_id_uses_local_key(self):
        for _ in range(2):
            self.store.record_trade(symbol="BTC", side=-1, price=78000.0, qty=0.04,
                                    kind="stop", price_int=780000, qty_int=4000, ts=TS)
        self.assertEqual(self.store.trade_count(), 1)


# ------------------------------------------------------------------ 原子性


class AtomicityTests(StoreTestCase):
    def test_failure_while_writing_the_trade_leaves_no_position(self):
        """成交写入抛错 → 仓位/事件/计数全部回滚（不存在「有仓无成交」的半状态）。"""
        original = self.store._insert_trade_row

        def boom(conn, row):
            raise RuntimeError("disk on fire")

        with mock.patch.object(self.store, "_insert_trade_row", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self.store.record_entry(**entry_plan())
        self.assertEqual(self.store._insert_trade_row, original)

        self.assertEqual(self.store.position_count(), 0)
        self.assertEqual(self.store.trade_count(), 0)
        self.assertEqual(self.store.event_count(), 0)
        self.assertIsNone(self.store.daily_counter(DAY))
        self.assertEqual(self.store.audit(), [])

        # 同一个库重试必须能成功（不是「写坏了就永久烂掉」）
        result = self.store.record_entry(**entry_plan())
        self.assertTrue(result.inserted_position)
        self.assertEqual(self.store.position_count(), 1)
        self.assertEqual(self.store.trade_count(), 1)

    def test_failure_while_closing_leaves_position_open(self):
        pid = self.store.record_entry(**entry_plan())["position_id"]

        def boom(conn, row):
            raise RuntimeError("disk on fire")

        with mock.patch.object(self.store, "_insert_trade_row", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self.store.record_exit(pid, exit_price_int=820000, ts=TS + 60)

        row = self.store.position(pid)
        self.assertEqual(row["state"], "OPEN")         # 状态没被单独改掉
        self.assertIsNone(row["closed_at"])
        self.assertIsNone(row["exit_price"])
        self.assertEqual(self.store.trade_count(), 1)  # 只有开仓那笔
        self.assertAlmostEqual(self.store.realized_pnl(), 0.0)   # 当日盈亏没被记上

    def test_write_lock_contention_fails_closed_without_half_state(self):
        """第二个进程拿着写锁 → 本进程报 busy（不是损坏、也不是半状态）。"""
        store = StateStore(self.path, busy_timeout_ms=50)
        self.addCleanup(store.close)
        other = self.open_second_connection()
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO events(ts, kind, message, created_at) VALUES(0,'x','x',0)")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.record_entry(**entry_plan())
        finally:
            other.execute("ROLLBACK")
        self.assertEqual(store.position_count(), 0)
        self.assertEqual(store.trade_count(), 0)
        # 锁释放后重试成功
        store.record_entry(**entry_plan())
        self.assertEqual(store.position_count(), 1)

    def test_nested_transaction_rolls_back_as_one_unit(self):
        with self.assertRaises(RuntimeError):
            with self.store.write_transaction() as conn:
                conn.execute("INSERT INTO events(ts, kind, message, created_at) "
                             "VALUES(1,'outer','outer',1)")
                with self.store.write_transaction() as inner:
                    inner.execute("INSERT INTO events(ts, kind, message, created_at) "
                                  "VALUES(2,'inner','inner',2)")
                raise RuntimeError("abort the whole unit")
        self.assertEqual(self.store.event_count(), 0)

    def test_audit_detects_hand_written_half_state(self):
        """有人绕过 store 直接写库 → `audit()` 必须报出来（不变量守护）。"""
        self.assertEqual(self.store.audit(), [])
        self.store.conn.execute(
            """INSERT INTO positions(symbol, side, state, bar_ms, qty_int, qty, size_decimals,
                    entry_price_int, entry_price, price_decimals, created_at, updated_at)
               VALUES('SOL', 1, 'OPEN', 1, 1000, 1.0, 3, 15000, 150.0, 2, 1.0, 1.0)""")
        findings = self.store.audit()
        # 一个字都没记的仓位同时违反两条不变量：没有开仓成交、成交合计（0）≠ 仓位量。
        self.assertEqual([f.code for f in findings],
                         ["open_without_entry_trade", "entry_qty_mismatch"])
        self.assertEqual(findings[0]["detail"]["symbol"], "SOL")
        self.assertEqual(findings[1]["detail"]["position_qty"], 1.0)
        self.assertEqual(findings[1]["detail"]["entry_qty"], 0.0)


# ------------------------------------------------------------------ 同 bar 重复 entry


class SameBarEntryTests(StoreTestCase):
    """一根 bar 只记一笔开仓成交 —— 重复同步幂等、价量冲突 fail-loud。

    守的不变式：`sum(该仓位 kind='entry' 的 qty) == positions.qty`（容差 1e-9）。
    """

    def entry_legs(self, position_id: int):
        return self.store._fetchall(
            "SELECT * FROM trades WHERE position_id=? AND kind='entry' ORDER BY id ASC",
            (int(position_id),))

    def assert_leg_sum_invariant(self) -> None:
        rows = self.store._fetchall("SELECT id, qty, qty_int FROM positions")
        self.assertTrue(rows)
        for row in rows:
            legs = self.entry_legs(int(row["id"]))
            self.assertAlmostEqual(sum(float(t["qty"]) for t in legs), float(row["qty"]),
                                   places=9)
            if row["qty_int"] is not None:
                self.assertEqual(sum(int(t["qty_int"] or 0) for t in legs),
                                 int(row["qty_int"]))

    def assert_nothing_extra(self) -> None:
        self.assertEqual(self.store.position_count(), 1)
        self.assertEqual(self.store.trade_count(), 1)
        self.assertEqual(self.store.event_count(), 1)
        self.assertEqual(self.store.daily_counter(DAY)["trades_today"], 1)
        self.assertEqual(self.store.trades_today(DAY), 1)
        self.assertEqual(self.store.audit(), [])

    def test_same_fill_id_is_fully_idempotent(self):
        first = self.store.record_entry(**entry_plan(fill_id="FILL-1"))
        again = self.store.record_entry(**entry_plan(fill_id="FILL-1"))
        self.assertEqual(again.position_id, first.position_id)
        self.assertEqual(again.trade_id, first.trade_id)
        self.assertFalse(again.inserted_position)
        self.assertFalse(again.inserted_trade)
        self.assertTrue(again.idempotent)
        self.assertTrue(again.duplicate_fill)
        self.assert_nothing_extra()
        self.assertEqual(self.entry_legs(first.position_id)[0]["fill_id"], "FILL-1")

    def test_different_fill_id_same_price_and_qty_is_a_duplicate_sync(self):
        """同一根 bar 换个 fill_id 再来一次 = 重复同步，**不新增成交行**（缺陷回归点）。"""
        first = self.store.record_entry(**entry_plan(fill_id="f1", entry_order_id="o1"))
        again = self.store.record_entry(**entry_plan(fill_id="f2", entry_order_id="o2"))
        self.assertEqual(again.position_id, first.position_id)
        self.assertEqual(again.trade_id, first.trade_id)     # 指回**原来那笔**
        self.assertFalse(again.inserted_trade)
        self.assertTrue(again.duplicate_fill)
        self.assert_nothing_extra()
        legs = self.entry_legs(first.position_id)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["fill_id"], "f1")           # 已有值不被覆盖
        self.assertEqual(legs[0]["order_id"], "o1")            # 已有值不被覆盖
        self.assertEqual(self.store.position(first.position_id)["qty"], 0.04)

    def test_duplicate_sync_backfills_a_missing_fill_id(self):
        """先只记了订单号、后到 fill_id → 补全那一行，仍然只有一笔成交。"""
        first = self.store.record_entry(**entry_plan())                 # 无 fill_id
        again = self.store.record_entry(**entry_plan(fill_id="f1"))
        self.assertFalse(again.inserted_trade)
        self.assertEqual(self.store.trade_count(), 1)
        leg = self.entry_legs(first.position_id)[0]
        self.assertEqual(leg["fill_id"], "f1")
        self.assertEqual(leg["idempotency_key"], "order:OID-ENTRY-1:1:800000:4000")
        self.assert_nothing_extra()

    def test_different_price_for_the_same_bar_fails_loud(self):
        self.store.record_entry(**entry_plan(fill_id="f1"))
        with self.assertRaises(EntryConflict):
            self.store.record_entry(**entry_plan(fill_id="f2", entry_price_int=801000))
        self.assert_nothing_extra()
        # 冲突之后库仍然可用（不是「写坏了」）
        pid = self.store.position_by_bar("BTC", BAR_MS)["id"]
        self.store.record_exit(pid, exit_price_int=820000, ts=TS + 60)
        self.assertEqual(self.store.position(pid)["state"], "CLOSED")

    def test_different_qty_for_the_same_bar_fails_loud(self):
        self.store.record_entry(**entry_plan(fill_id="f1"))
        with self.assertRaises(StoreError):        # 是 StoreError 的子类
            self.store.record_entry(**entry_plan(fill_id="f2", qty_int=2000))
        self.assert_nothing_extra()

    def test_opposite_side_for_the_same_bar_fails_loud(self):
        self.store.record_entry(**entry_plan(fill_id="f1"))
        with self.assertRaises(EntryConflict):
            # 空单止损要在入场价上方，先过 record_entry 自己的参数校验
            self.store.record_entry(**entry_plan(fill_id="f2", side=-1, stop_price_int=820000))
        self.assert_nothing_extra()

    def test_closed_position_duplicate_sync_adds_nothing(self):
        pid = self.store.record_entry(**entry_plan(fill_id="f1"))["position_id"]
        self.store.record_exit(pid, exit_price_int=820000, ts=TS + 60)
        again = self.store.record_entry(**entry_plan(fill_id="f2"))
        self.assertFalse(again.inserted_trade)
        self.assertEqual(self.store.trade_count(), 2)          # entry + exit，无第三行
        self.assertEqual(self.entry_legs(pid).__len__(), 1)
        self.assertEqual(self.store.audit(), [])

    def test_missing_entry_leg_is_backfilled(self):
        """历史半状态（有仓无成交）：补记**唯一**那一笔，仓位与计数口径都对齐。"""
        self.store.conn.execute(
            """INSERT INTO positions(symbol, side, state, bar_ms, qty_int, qty, size_decimals,
                    entry_price_int, entry_price, price_decimals, created_at, updated_at)
               VALUES('BTC', 1, 'OPEN', ?, 4000, 0.04, 5, 800000, 80000.0, 1, 1.0, 1.0)""",
            (BAR_MS,))
        pid = int(self.store._scalar("SELECT id FROM positions WHERE symbol='BTC'"))
        self.assertEqual([f.code for f in self.store.audit()],
                         ["open_without_entry_trade", "entry_qty_mismatch"])

        result = self.store.record_entry(**entry_plan())
        self.assertEqual(result.position_id, pid)
        self.assertTrue(result.inserted_trade)
        self.assertEqual(self.store.position_count(), 1)
        self.assertEqual(self.store.trade_count(), 1)
        self.assertEqual(self.store.audit(), [])
        self.assert_leg_sum_invariant()
        self.assertEqual(self.store.trades_today(DAY), 1)

    def test_audit_flags_a_hand_written_second_entry_leg(self):
        """人为制造「成交合计 ≠ 仓位量」→ `audit()` 必须报 `entry_qty_mismatch`。"""
        pid = self.store.record_entry(**entry_plan(fill_id="f1"))["position_id"]
        self.assertEqual(self.store.audit(), [])
        self.store.conn.execute(
            """INSERT INTO trades(ts, symbol, side, price, price_int, qty, qty_int, fee, kind,
                    order_id, fill_id, position_id, bar_ms, idempotency_key, created_at)
               VALUES(?, 'BTC', 1, 80000.0, 800000, 0.04, 4000, 0.0, 'entry',
                      'o2', 'f2', ?, ?, 'fill:f2', 1.0)""", (TS, pid, BAR_MS))
        findings = self.store.audit()
        self.assertEqual([f.code for f in findings],
                         ["entry_qty_mismatch", "daily_counter_mismatch"])
        detail = findings[0]["detail"]
        self.assertEqual(detail["position_id"], pid)
        self.assertEqual(detail["entry_legs"], 2)
        self.assertAlmostEqual(detail["position_qty"], 0.04, places=12)
        self.assertAlmostEqual(detail["entry_qty"], 0.08, places=12)
        # 手写的第二笔既破坏了 qty 不变式，也让两个当日口径分叉（2 行 vs 计数 1）
        self.assertEqual(self.store.trades_today(DAY), 2)
        self.assertEqual(self.store.daily_counter(DAY)["trades_today"], 1)
        self.assertEqual(findings[1]["detail"],
                         {"day": DAY, "counter_trades_today": 1, "entry_trade_rows": 2})

    def test_audit_flags_daily_counter_drift(self):
        self.store.record_entry(**entry_plan())
        self.assertEqual(self.store.audit(), [])
        self.store.conn.execute("UPDATE daily_counters SET trades_today=5 WHERE day=?", (DAY,))
        findings = self.store.audit()
        self.assertEqual([f.code for f in findings], ["daily_counter_mismatch"])
        self.assertEqual(findings[0]["detail"],
                         {"day": DAY, "counter_trades_today": 5, "entry_trade_rows": 1})
        self.store.conn.execute("UPDATE daily_counters SET trades_today=1 WHERE day=?", (DAY,))
        self.assertEqual(self.store.audit(), [])

    def test_audit_flags_entry_trade_without_a_counter_row(self):
        pid = self.store.record_entry(**entry_plan())["position_id"]
        self.store.conn.execute("DELETE FROM daily_counters")
        codes = [f.code for f in self.store.audit()]
        self.assertEqual(codes, ["daily_counter_mismatch"])
        self.assertEqual(self.entry_legs(pid)[0]["kind"], "entry")

    def test_entry_exit_rounds_keep_the_leg_sum_invariant(self):
        """不变式测试：多轮 entry（含重复同步）+ exit 之后，逐仓仍成立。"""
        symbols = ["BTC", "ETH", "SOL", "DOGE", "ARB", "OP"]
        for index, symbol in enumerate(symbols):
            bar = BAR_MS + index * 14400000
            pid = self.store.record_entry(
                **entry_plan(symbol=symbol, bar_ms=bar, fill_id=f"f{index}"))["position_id"]
            self.assert_leg_sum_invariant()
            # 每仓都来一次重复同步（不同 fill_id、同价量）
            self.store.record_entry(**entry_plan(symbol=symbol, bar_ms=bar,
                                                fill_id=f"dup{index}"))
            self.assert_leg_sum_invariant()
            self.store.record_exit(pid, exit_price_int=820000, ts=TS + 60 + index)
            self.assert_leg_sum_invariant()
        self.assertEqual(self.store.position_count(), len(symbols))
        self.assertEqual(self.store.trade_count(), 2 * len(symbols))   # entry + exit
        self.assertEqual(self.store.daily_counter(DAY)["trades_today"], len(symbols))
        self.assertEqual(self.store.trades_today(DAY), len(symbols))
        self.assertEqual(self.store.audit(), [])


# ------------------------------------------------------------------ 并发 / WAL


class WalTests(StoreTestCase):
    def test_journal_mode_is_wal_for_files(self):
        self.assertEqual(self.store.journal_mode, "wal")
        self.assertEqual(
            self.store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_busy_timeout_is_applied(self):
        self.assertEqual(
            self.store.conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_second_connection_reads_while_first_is_writing(self):
        """面板进程（只读连接）能看到已提交的数据，且不被写事务挡住。"""
        self.store.record_entry(**entry_plan())
        reader = self.open_second_connection()
        reader.execute("BEGIN")                        # 读事务
        rows = reader.execute("SELECT symbol, qty FROM positions").fetchall()
        self.assertEqual([(r["symbol"], r["qty"]) for r in rows], [("BTC", 0.04)])

        self.store.record_equity(10100.0, ts=TS + 60)  # 写事务照常提交
        reader.execute("COMMIT")
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM equity_curve").fetchone()[0], 1)

    def test_checkpoint_folds_wal_into_main_file(self):
        self.store.record_entry(**entry_plan())
        self.store.checkpoint()
        self.assertEqual(self.store.integrity_check(), "ok")
        self.assertEqual(self.store.position_count(), 1)


# ------------------------------------------------------------------ 每日计数


class DailyCounterTests(StoreTestCase):
    def test_ensure_day_writes_day_start_once(self):
        first = self.store.ensure_day(DAY, day_start_equity=10000.0, ts=TS)
        self.assertEqual(first["day_start_equity"], 10000.0)
        second = self.store.ensure_day(DAY, day_start_equity=12000.0, ts=TS + 60)
        self.assertEqual(second["day_start_equity"], 10000.0)   # 同一天不覆盖

    def test_day_start_is_filled_from_first_equity_after_null_row(self):
        self.store.ensure_day(DAY, ts=TS)                       # 先建行、没有权益
        self.assertIsNone(self.store.daily_counter(DAY)["day_start_equity"])
        self.store.record_entry(**entry_plan())                 # 开仓时带上权益
        self.assertEqual(self.store.daily_counter(DAY)["day_start_equity"], 10000.0)

    def test_trades_today_takes_the_stricter_of_counter_and_rows(self):
        self.assertEqual(self.store.trades_today(DAY), 0)
        self.store.record_entry(**entry_plan())
        self.assertEqual(self.store.trades_today(DAY), 1)
        # 计数行被外力抬高（迁移/运维）→ 取大者（更严的一侧）
        self.store.conn.execute("UPDATE daily_counters SET trades_today=3 WHERE day=?", (DAY,))
        self.assertEqual(self.store.trades_today(DAY), 3)

    def test_exits_do_not_count_as_trades_today(self):
        pid = self.store.record_entry(**entry_plan())["position_id"]
        self.store.record_exit(pid, exit_price_int=820000, ts=TS + 60)
        self.assertEqual(self.store.trades_today(DAY), 1)

    def test_day_bounds_are_utc_half_open(self):
        start, end = day_bounds(DAY)
        self.assertEqual(start, 1759968000.0)          # 2025-10-09T00:00:00Z
        self.assertEqual(end - start, 86400.0)
        self.assertEqual(utc_day(TS), DAY)
        self.assertEqual(utc_day(start), DAY)
        self.assertEqual(utc_day(end), "2025-10-10")


# ------------------------------------------------------------------ 回撤锚点


class DrawdownAnchorTests(StoreTestCase):
    def test_save_and_read_dd_state(self):
        state = self.store.save_dd_state(twr_index=0.8, twr_peak=1.0, anchor_ts=TS,
                                         anchor_equity=10000.0, ts=TS)
        self.assertEqual(state["twr_index"], 0.8)
        self.assertEqual(state["twr_peak"], 1.0)
        self.assertEqual(state["reset_count"], 0)
        self.assertEqual(self.store.dd_state()["anchor_equity"], 10000.0)

    def test_save_dd_state_refuses_to_lower_the_peak(self):
        self.store.save_dd_state(twr_index=0.8, twr_peak=1.25, anchor_ts=TS,
                                 anchor_equity=10000.0, ts=TS)
        with self.assertRaises(StoreError):
            self.store.save_dd_state(twr_index=0.9, twr_peak=1.0, anchor_ts=TS,
                                     anchor_equity=9000.0, ts=TS + 60)
        self.assertEqual(self.store.dd_state()["twr_peak"], 1.25)   # 没被改

    def test_reset_dd_peak_is_the_only_way_down_and_is_audited(self):
        self.store.save_dd_state(twr_index=0.7, twr_peak=1.25, anchor_ts=TS,
                                 anchor_equity=10000.0, ts=TS)
        state = self.store.reset_dd_peak(anchor_ts=TS + 60, anchor_equity=7000.0,
                                         reason="manual reset after review", ts=TS + 60)
        self.assertEqual(state["twr_peak"], 1.0)
        self.assertEqual(state["twr_index"], 1.0)
        self.assertEqual(state["reset_count"], 1)
        self.assertEqual(state["reset_reason"], "manual reset after review")
        self.assertEqual(state["last_reset_at"], TS + 60)
        events = self.store.events(kind="dd_reset")
        self.assertEqual(len(events), 1)
        # 再存一次更高的峰值仍然可以（重置后从 1.0 重新往上涨）
        self.store.save_dd_state(twr_index=1.1, twr_peak=1.1, anchor_ts=TS + 120,
                                 anchor_equity=7700.0, ts=TS + 120)
        self.assertEqual(self.store.dd_state()["twr_peak"], 1.1)
        self.assertEqual(self.store.dd_state()["reset_count"], 1)  # 审计计数不被覆盖

    def test_reset_requires_a_reason(self):
        with self.assertRaises(StoreError):
            self.store.reset_dd_peak(anchor_ts=TS, anchor_equity=10000.0, reason="   ")

    def test_prune_removes_old_anchors_but_never_the_newest_or_dd_state(self):
        old = TS - 40 * 86400.0
        self.store.append_dd_anchor(old, 9000.0, twr_index=0.9, twr_peak=1.0)
        self.store.append_dd_anchor(TS - 35 * 86400.0, 9200.0, twr_index=0.92, twr_peak=1.0)
        self.store.append_dd_anchor(TS - 20 * 86400.0, 9500.0, twr_index=0.95, twr_peak=1.0)
        self.store.append_dd_anchor(TS, 10000.0, twr_index=1.0, twr_peak=1.0)
        self.store.save_dd_state(twr_index=0.8, twr_peak=1.25, anchor_ts=old,
                                 anchor_equity=9000.0, ts=TS)
        self.assertEqual(self.store.dd_anchor_count(), 4)

        removed = self.store.prune_dd_anchors(keep_days=30, ts=TS)
        self.assertEqual(removed, 2)                      # 40 天前 + 35 天前（都早于 30 天窗口）
        self.assertEqual(self.store.dd_anchor_count(), 2)
        self.assertEqual(self.store.latest_dd_anchor()["ts"], TS)
        # **关键**：dd_state 一行都没少，峰值也没变
        self.assertEqual(self.store.dd_state()["twr_peak"], 1.25)

    def test_prune_keeps_the_newest_anchor_even_if_it_is_older_than_the_window(self):
        """表不能被清空 —— 空表是「回撤无从重建」的最坏输入。"""
        old = TS - 400 * 86400.0
        self.store.append_dd_anchor(old, 9000.0, twr_index=0.9, twr_peak=1.0)
        self.assertEqual(self.store.prune_dd_anchors(keep_days=30, ts=TS), 0)
        self.assertEqual(self.store.dd_anchor_count(), 1)

    def test_prune_does_not_touch_dd_state_when_anchors_all_expire(self):
        self.store.append_dd_anchor(TS - 90 * 86400.0, 9000.0, twr_index=0.9, twr_peak=1.0)
        self.store.append_dd_anchor(TS - 60 * 86400.0, 9500.0, twr_index=0.95, twr_peak=1.0)
        self.store.save_dd_state(twr_index=0.76, twr_peak=1.25, anchor_ts=TS - 90 * 86400.0,
                                 anchor_equity=9000.0, ts=TS)
        self.store.prune_dd_anchors(keep_days=30, ts=TS)
        self.assertEqual(self.store.dd_anchor_count(), 1)   # 最新那条保留
        state = self.store.dd_state()
        self.assertEqual(state["twr_peak"], 1.25)
        self.assertEqual(state["twr_index"], 0.76)

    def test_anchor_upsert_is_idempotent_on_same_ts(self):
        self.store.append_dd_anchor(TS, 9000.0, twr_index=0.9, twr_peak=1.0)
        self.store.append_dd_anchor(TS, 9100.0, twr_index=0.91, twr_peak=1.0)
        self.assertEqual(self.store.dd_anchor_count(), 1)
        self.assertEqual(self.store.latest_dd_anchor()["equity"], 9100.0)


# ------------------------------------------------------------------ 面板 / 边界


class PanelSnapshotTests(StoreTestCase):
    def test_snapshot_carries_every_block(self):
        pid = self.store.record_entry(**entry_plan())["position_id"]
        self.store.record_equity(10000.0, ts=TS)
        self.store.save_dd_state(twr_index=0.9, twr_peak=1.0, anchor_ts=TS,
                                 anchor_equity=10000.0, ts=TS)
        snap = self.store.panel_snapshot(ts=TS + 60, trade_limit=5)
        self.assertEqual(snap["day"], DAY)
        self.assertEqual(snap["open_symbols"], ["BTC"])
        self.assertEqual(snap["open_count"], 1)
        self.assertEqual(snap["positions"][0]["id"], pid)
        self.assertEqual(snap["open_notional_total"], 3200.0)
        self.assertEqual(snap["trades"][0]["kind"], "entry")
        self.assertEqual(len(snap["equity_curve"]), 1)
        self.assertEqual(snap["last_equity"]["equity"], 10000.0)
        self.assertEqual(snap["trades_today"], 1)
        self.assertEqual(snap["dd_state"]["twr_peak"], 1.0)
        self.assertEqual([e["kind"] for e in snap["events"]], ["open"])

    def test_integrity_check_ok(self):
        self.assertEqual(self.store.integrity_check(), "ok")

    def test_invalid_inputs_are_rejected_loudly(self):
        with self.assertRaises(StoreError):
            self.store.record_entry(**entry_plan(side=0))
        with self.assertRaises(StoreError):
            self.store.record_entry(**entry_plan(qty_int=0))
        with self.assertRaises(StoreError):
            self.store.record_entry(**entry_plan(stop_price_int=820000))   # 多单止损在入场之上
        with self.assertRaises(StoreError):
            self.store.record_entry(**entry_plan(symbol=""))
        with self.assertRaises(StoreError):
            self.store.record_equity(float("nan"))
        with self.assertRaises(StoreError):
            self.store.record_trade(symbol="BTC", side=1, price=0.0, qty=1.0, kind="entry")
        with self.assertRaises(StoreError):
            self.store.record_trade(symbol="BTC", side=1, price=1.0, qty=1.0, kind="nonsense")
        with self.assertRaises(StoreError):
            self.store.daily_counter("not-a-day")

    def test_store_does_not_import_network_modules(self):
        """存储层不得有任何网络 I/O 依赖（T5 验收项，自动化守护）。

        字符串故意拼开：本文件自身也不出现那些模块名，免得仓库级 grep 误伤。
        """
        banned = ("url" + "lib", "req" + "uests", "h" + "ttpx", "so" + "cket")
        source = pathlib.Path(store_module.__file__).read_text(encoding="utf-8")
        for token in banned:
            self.assertNotIn(token, source, f"app/store.py must not reference {token}")


if __name__ == "__main__":                                        # pragma: no cover
    unittest.main()
