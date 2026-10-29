"""出入金流水推断的契约测试 —— **全离线**。

背景（2026-10-28 实测）：`/transfer/history` 对 UDA 桥接入金**永远返回空**
（SDK 的 `TransferHistoryItem` 类型枚举里没有 deposit 类），所以首次 bootstrap 之后
再入金，TWR/盈亏曲线会把权益跳变全额当成交易盈利（真实事故：399 USDC 入金 →
`cum_pnl=399`、TWR 显示 +395%）。修法 = 无持仓、无成交时由权益跳变推断流水：
`app/executor.infer_transfer_flow`（纯函数）→ `app/main._inferred_flow_for_tick`
（落库 + 落事件）→ `app/store.account_flows`（持久化，重放每轮喂回）。

钉住的东西：

1. **推断的硬前提缺一不可**：有持仓 / 有成交 / 没有上一格权益 → 一律不推断
   （宁可少剔一笔，不能把盈亏猜成流水、更不能猜反方向）。
2. **时间戳落对窗口**：优先 `transaction_time`（入金真实到账时刻），不在
   `(prev_ts, as_of]` 内退回 `as_of` —— 重放窗口是左开右闭，落错窗 = 白剔。
3. **幂等**：同一笔流水重复推断只落一行（`source, ts, amount` 唯一索引）；
   `_inferred_flow_for_tick` 第二次调用返回 `None` 而不是重复 append。
4. **重放自愈**：把事故场景原样重放（锚点 101.0089 → 权益 500.0089），带流水时
   指数回到 1.0、`cum_pnl=0.0`；不带流水就是事故里的 4.9506 / 399.0089 ——
   两个方向都写死数值，证明流水真的被用上了。
5. **迁移**：v2 老库升级到 v3 后 `account_flows` 可用（线上库就是这条路径）。

期望值全部手算写死。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from app import main as main_module
from app.executor import (
    TRANSFER_FLOW_EPS, ExchangeAccount, account_transaction_time_from_payload,
    infer_transfer_flow,
)
from app.store import SCHEMA_VERSION, StateStore
from app.views import pnl_curve

# 事故时间线（2026-10-28，真实数值）：14:33:27 tick 写下 101.0089 → 14:39:07 入金
# 399 USDC 到账 → 15:03:38 下一轮 tick 读到 500.0089。
PREV_TS = 1793198007.0
DEPOSIT_TS = 1793198347.122463          # /account 的 transaction_time（µs → s）
NOW_TS = 1793199818.0
PREV_EQUITY = 101.0089
NEW_EQUITY = 500.0089
DEPOSIT = NEW_EQUITY - PREV_EQUITY      # 399.0


def account(**overrides) -> ExchangeAccount:
    base = dict(equity=NEW_EQUITY, positions=(), orders=(), as_of=NOW_TS,
                transaction_time=DEPOSIT_TS)
    base.update(overrides)
    return ExchangeAccount(**base)


class InferTransferFlowTests(unittest.TestCase):
    """`infer_transfer_flow` 的前提与输出（手算值）。"""

    def test_flat_no_fills_equity_jump_is_a_deposit_flow(self) -> None:
        flow = infer_transfer_flow(prev_ts=PREV_TS, prev_equity=PREV_EQUITY,
                                   account=account(), fills_since_prev=False)
        self.assertEqual(flow, {"ts": DEPOSIT_TS, "amount": DEPOSIT})

    def test_negative_jump_is_a_withdrawal_flow(self) -> None:
        flow = infer_transfer_flow(prev_ts=PREV_TS, prev_equity=NEW_EQUITY,
                                   account=account(equity=PREV_EQUITY),
                                   fills_since_prev=False)
        self.assertEqual(flow, {"ts": DEPOSIT_TS, "amount": -DEPOSIT})

    def test_no_change_means_no_flow(self) -> None:
        self.assertIsNone(infer_transfer_flow(
            prev_ts=PREV_TS, prev_equity=NEW_EQUITY, account=account(),
            fills_since_prev=False))

    def test_tiny_jitter_below_eps_is_not_a_flow(self) -> None:
        self.assertIsNone(infer_transfer_flow(
            prev_ts=PREV_TS, prev_equity=NEW_EQUITY,
            account=account(equity=NEW_EQUITY + TRANSFER_FLOW_EPS / 2.0),
            fills_since_prev=False))

    def test_open_positions_block_inference(self) -> None:
        from decimal import Decimal
        from app.executor import ExchangePosition
        pos = ExchangePosition(symbol="SOL", side=1, qty=Decimal("0.100"), market_id=2)
        self.assertIsNone(infer_transfer_flow(
            prev_ts=PREV_TS, prev_equity=PREV_EQUITY,
            account=account(positions=(pos,)), fills_since_prev=False))

    def test_fills_block_inference(self) -> None:
        self.assertIsNone(infer_transfer_flow(
            prev_ts=PREV_TS, prev_equity=PREV_EQUITY, account=account(),
            fills_since_prev=True))

    def test_no_previous_point_blocks_inference(self) -> None:
        for prev_ts, prev_equity in ((0.0, PREV_EQUITY), (None, PREV_EQUITY),
                                     (PREV_TS, None), (PREV_TS, 0.0)):
            self.assertIsNone(infer_transfer_flow(
                prev_ts=prev_ts, prev_equity=prev_equity, account=account(),
                fills_since_prev=False), f"prev=({prev_ts!r}, {prev_equity!r})")

    def test_unknown_equity_blocks_inference(self) -> None:
        self.assertIsNone(infer_transfer_flow(
            prev_ts=PREV_TS, prev_equity=PREV_EQUITY, account=account(equity=None),
            fills_since_prev=False))

    def test_transaction_time_outside_window_falls_back_to_as_of(self) -> None:
        # transaction_time 早于上一格（不可能的读数）→ 不猜，用 as_of。
        flow = infer_transfer_flow(
            prev_ts=PREV_TS, prev_equity=PREV_EQUITY,
            account=account(transaction_time=PREV_TS - 10.0), fills_since_prev=False)
        self.assertEqual(flow, {"ts": NOW_TS, "amount": DEPOSIT})
        # transaction_time 缺失（0.0）→ 同样退回 as_of。
        flow = infer_transfer_flow(
            prev_ts=PREV_TS, prev_equity=PREV_EQUITY,
            account=account(transaction_time=0.0), fills_since_prev=False)
        self.assertEqual(flow, {"ts": NOW_TS, "amount": DEPOSIT})


class TransactionTimePayloadTests(unittest.TestCase):
    """`account_transaction_time_from_payload`：µs → 秒、按 account_index 过滤。"""

    PAYLOAD = {"code": 200, "accounts": [
        {"index": 1, "transaction_time": 1111111111111111.0},
        {"index": 764377, "transaction_time": "1793198347122463"},
    ]}

    def test_microseconds_become_seconds(self) -> None:
        out = account_transaction_time_from_payload(self.PAYLOAD, account_index=764377)
        self.assertEqual(out, 1793198347.122463)

    def test_wrong_account_index_is_skipped(self) -> None:
        out = account_transaction_time_from_payload(self.PAYLOAD, account_index=1)
        self.assertEqual(out, 1111111111.111111)

    def test_garbage_payload_returns_zero(self) -> None:
        for payload in ({}, {"accounts": []}, {"accounts": [{}]}, None, 5):
            self.assertEqual(account_transaction_time_from_payload(payload), 0.0)


class AccountFlowsStoreTests(unittest.TestCase):
    """`account_flows` 表：幂等、过滤、脏输入拒收。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = StateStore(os.path.join(self._tmp.name, "state.db"))
        self.addCleanup(self.store.close)

    def test_record_flow_round_trip_and_ordering(self) -> None:
        self.assertTrue(self.store.record_flow(399.0, ts=DEPOSIT_TS, source="inferred"))
        self.assertTrue(self.store.record_flow(-50.0, ts=DEPOSIT_TS + 10.0,
                                               source="manual"))
        rows = self.store.flows()
        self.assertEqual([(r["ts"], r["amount"]) for r in rows],
                         [(DEPOSIT_TS, 399.0), (DEPOSIT_TS + 10.0, -50.0)])
        self.assertEqual([r["amount"] for r in self.store.flows(since=DEPOSIT_TS + 5.0)],
                         [-50.0])

    def test_record_flow_is_idempotent_on_source_ts_amount(self) -> None:
        self.assertTrue(self.store.record_flow(399.0, ts=DEPOSIT_TS, source="inferred"))
        # 同键重复 → False（幂等命中），行数不变。
        self.assertFalse(self.store.record_flow(399.0, ts=DEPOSIT_TS, source="inferred"))
        self.assertEqual(len(self.store.flows()), 1)
        # 金额或来源不同 → 是另一条流水。
        self.assertTrue(self.store.record_flow(399.0, ts=DEPOSIT_TS, source="manual"))
        self.assertTrue(self.store.record_flow(-399.0, ts=DEPOSIT_TS, source="inferred"))
        self.assertEqual(len(self.store.flows()), 3)

    def test_record_flow_rejects_dirty_input(self) -> None:
        for ts in (None, 0.0, -5.0, float("nan")):
            with self.assertRaises(Exception):
                self.store.record_flow(1.0, ts=ts)
        for amount in (None, 0.0, float("inf")):
            with self.assertRaises(Exception):
                self.store.record_flow(amount, ts=DEPOSIT_TS)

    def test_v2_database_migrates_to_current_and_flows_work(self) -> None:
        # 线上库就是这条路径：老库（user_version=2）升级后 account_flows 可用。
        # v3 加 account_flows、v4 加 positions.stop_expiry_ts（2026-10-29），一路升到 SCHEMA_VERSION。
        path = os.path.join(self._tmp.name, "legacy.db")
        legacy = sqlite3.connect(path)
        legacy.execute("PRAGMA user_version=2")
        legacy.commit()
        legacy.close()
        self.assertGreaterEqual(SCHEMA_VERSION, 3)
        store = StateStore(path)
        self.addCleanup(store.close)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        self.assertTrue(store.record_flow(399.0, ts=DEPOSIT_TS, source="manual"))
        self.assertEqual([r["amount"] for r in store.flows()], [399.0])


class StoredFlowsForReplayTests(unittest.TestCase):
    """`_stored_flows_for_replay`：锚点之后的流水、左开、坏行跳过。"""

    def setUp(self) -> None:
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)

    def test_anchor_row_is_excluded_left_open(self) -> None:
        self.store.record_flow(10.0, ts=100.0, source="inferred")
        self.store.record_flow(399.0, ts=200.0, source="inferred")
        self.store.save_dd_state(twr_index=1.0, twr_peak=1.0, anchor_ts=100.0,
                                 anchor_equity=101.0, anchor_cum_flow=0.0, ts=100.0)
        self.assertEqual(main_module._stored_flows_for_replay(self.store),
                         [{"ts": 200.0, "amount": 399.0}])

    def test_no_dd_state_returns_everything(self) -> None:
        self.store.record_flow(399.0, ts=DEPOSIT_TS, source="manual")
        self.assertEqual(main_module._stored_flows_for_replay(self.store),
                         [{"ts": DEPOSIT_TS, "amount": 399.0}])

    def test_zero_amount_rows_are_skipped(self) -> None:
        self.store.record_flow(1.0, ts=200.0, source="manual")
        with self.store.write_transaction() as conn:
            conn.execute("INSERT INTO account_flows(ts, amount, source, note, created_at) "
                         "VALUES(200.5, 0.0, 'manual', '', 0.0)")
        self.assertEqual(main_module._stored_flows_for_replay(self.store),
                         [{"ts": 200.0, "amount": 1.0}])


class InferredFlowForTickTests(unittest.TestCase):
    """`_inferred_flow_for_tick`：落库 + 落事件 + 幂等（真实 StateStore，全离线）。"""

    def setUp(self) -> None:
        self.store = StateStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.record_equity(PREV_EQUITY, ts=PREV_TS, source="exchange")

    def test_jump_is_recorded_with_event(self) -> None:
        executor = mock.Mock()
        executor.last_account = account()
        flow = main_module._inferred_flow_for_tick(self.store, executor)
        self.assertEqual(flow, {"ts": DEPOSIT_TS, "amount": DEPOSIT})
        rows = self.store.flows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["amount"], rows[0]["source"]), (DEPOSIT, "inferred"))
        events = self.store.events(limit=10)
        self.assertTrue(any(e["kind"] == "flow_inferred" for e in events))

    def test_second_call_is_idempotent(self) -> None:
        executor = mock.Mock()
        executor.last_account = account()
        self.assertIsNotNone(main_module._inferred_flow_for_tick(self.store, executor))
        # 上一格权益点还没被下一轮 tick 覆盖，推断结果相同，但库里已有 → 不再新增。
        self.assertIsNone(main_module._inferred_flow_for_tick(self.store, executor))
        self.assertEqual(len(self.store.flows()), 1)

    def test_fills_block_inference(self) -> None:
        executor = mock.Mock()
        executor.last_account = account()
        with mock.patch.object(self.store, "trades",
                               return_value=[{"ts": PREV_TS + 1.0}]):
            self.assertIsNone(main_module._inferred_flow_for_tick(self.store, executor))
        self.assertEqual(self.store.flows(), [])

    def test_trades_at_or_before_prev_point_do_not_block(self) -> None:
        # 上一格权益点**之后**写的成交才算「区间内」；ts == prev_ts 的成交已体现在
        # 上一格里（ts > prev_ts 才算）。
        executor = mock.Mock()
        executor.last_account = account()
        with mock.patch.object(self.store, "trades",
                               return_value=[{"ts": PREV_TS}]):
            self.assertIsNotNone(main_module._inferred_flow_for_tick(self.store, executor))

    def test_no_previous_equity_point_means_no_inference(self) -> None:
        store = StateStore(":memory:")
        self.addCleanup(store.close)
        executor = mock.Mock()
        executor.last_account = account()
        self.assertIsNone(main_module._inferred_flow_for_tick(store, executor))
        self.assertEqual(store.flows(), [])


class ReplaySelfHealTests(unittest.TestCase):
    """把事故场景原样重放：带流水指数回到 1.0，不带就是事故数值（证明流水被用上）。"""

    POINTS_NO_FLOW = [
        {"ts": PREV_TS, "equity": PREV_EQUITY, "cum_flow": 0.0},
        {"ts": NOW_TS, "equity": NEW_EQUITY, "cum_flow": 0.0},
    ]
    POINTS_WITH_FLOW = [
        {"ts": PREV_TS, "equity": PREV_EQUITY, "cum_flow": 0.0},
        {"ts": NOW_TS, "equity": NEW_EQUITY, "cum_flow": DEPOSIT},
    ]

    def test_twr_replay_with_inferred_flow_stays_flat(self) -> None:
        # app.dd.bootstrap 的窗口口径：(PREV_TS, NOW_TS] 覆盖 14:39:07 的入金。
        from app.dd import bootstrap
        healed = bootstrap(self.POINTS_WITH_FLOW, [{"ts": DEPOSIT_TS, "amount": DEPOSIT}],
                           anchor_ts=PREV_TS, anchor_equity=PREV_EQUITY)
        # (500.0089 − 399.0) / 101.0089 = 101.0089 / 101.0089 = 1.0
        self.assertAlmostEqual(healed.index, 1.0, places=9)
        # 不带流水（= 事故现场）：500.0089 / 101.0089 = 4.9501470…
        broken = bootstrap(self.POINTS_NO_FLOW, [], anchor_ts=PREV_TS,
                           anchor_equity=PREV_EQUITY)
        self.assertAlmostEqual(broken.index, 4.9501470, places=6)

    def test_panel_cum_pnl_with_flow_column_excludes_deposit(self) -> None:
        healed = pnl_curve(self.POINTS_WITH_FLOW, anchor_ts=PREV_TS,
                           anchor_equity=PREV_EQUITY)
        # cum_pnl = 500.0089 − 101.0089 − 399.0 = 0.0（区间内没有真实盈亏）
        self.assertAlmostEqual(healed["cum_pnl"], 0.0, places=9)
        broken = pnl_curve(self.POINTS_NO_FLOW, anchor_ts=PREV_TS,
                           anchor_equity=PREV_EQUITY)
        self.assertAlmostEqual(broken["cum_pnl"], 399.0, places=9)
        self.assertAlmostEqual(broken["twr_index"], 4.9501470, places=6)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
