"""`app/dd.py` 的契约测试 —— **全离线**（临时目录里的库 + 手算的权益序列/流水）。

钉住的东西（T9 验收项，逐条对应）：

1. **TWR 剔出入金**：入金不会把亏损「稀释」（回撤不缩小），出金不会把提现算成亏损
   （回撤不放大）。两个方向都写死数值，并且**同时**断言「不剔流水会算成什么」——
   否则这个测试证明不了流水真的被用上了。
2. **峰值只升不降**：库里已有 1.25 的峰值时，一次只覆盖近期数据的重放**不能**把它降下来；
   `store.save_dd_state()` 在峰值下调时抛 `StoreError`（双保险）。
3. **档位联动**：`risk_pct` 1.33% / 2% / 3% 三档的告警线与熔断线，逐个值写死；
   判定**只走** `app.risk.dd_action()`（`app.dd` 里没有第二份阈值表）。
4. **脏值取最严档**：`None` / `NaN` / `±inf` / `peak <= 0` → 直接 `kill`；脏 `risk_pct`
   → 落最严档（25%/30%）。
5. **熔断只挡开仓，不挡离场**：同一个瞬间，`check_entry()` 被 `drawdown` 闸拦下，
   而 `lifecycle.decide_exit()` 照常产出止损/信号离场意图（它的签名里没有 `risk_state`）。
6. **锚点被剪 ≠ 回撤归零**：`prune_dd_anchors()` 之后判定不变；只有 `dd_state` **和**
   锚点都没了才 `available=False` —— 那时风控**拒单**（不是「回撤 0% 放行」）。
7. **熔断永不自解**：`update()` 从不调用 `reset_dd_peak()`；重置必须显式、带理由、落审计。
8. **指数基准**：库里按 `1.0` 存（`app/store.BASE_TWR_INDEX`），面板口径 ×100。

期望值全部手算写死，每个断言旁给算式。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any, List, Optional
from unittest import mock

import app.dd as dd_module
import app.risk as risk_module
import app.strategy as strategy
from app import lifecycle
from app.dd import (
    DdError, DdVerdict, DrawdownGuard, advance, bootstrap, cum_flow, evaluate, net_flow,
    normalise_points,
)
from app.risk import GATE_DD, RiskLimits, RiskState, check_entry
from app.state import DD_SOURCE_ANCHOR, DD_SOURCE_STATE, DD_SOURCE_UNAVAILABLE
from app.store import BASE_TWR_INDEX, StateStore, utc_day
from app.lifecycle import ACTION_EXIT, ACTION_STOP, PositionView

# 1760000000 = 2025-10-09T08:53:20Z
T0 = 1_760_000_000.0
TICK = 4 * 3600.0                 # 一根 4h bar
BAR_MS = 4 * 3600 * 1000

LIMITS_133 = RiskLimits(risk_pct=0.0133)      # 最严档：告警 25% / 熔断 30%


def points(*pairs):
    return [(T0 + float(offset), float(equity)) for offset, equity in pairs]


def deposit(ts: float, amount: float) -> dict:
    return {"ts": float(ts), "amount": float(amount)}


# --------------------------------------------------------------------------- 流水


class NetFlowTests(unittest.TestCase):
    """区间口径：**左开右闭** `(lo, hi]`。"""

    def setUp(self) -> None:
        self.flows = [deposit(T0, 100.0), deposit(T0 + 60, 200.0),
                      deposit(T0 + 120, -50.0), deposit(T0 + 180, 10.0)]

    def test_open_left_closed_right(self) -> None:
        # (T0, T0+120] → 200 + (−50) = 150。T0 那笔属于「上一步已经体现在权益里」，不重算。
        self.assertAlmostEqual(net_flow(self.flows, T0, T0 + 120), 150.0, places=12)
        # [T0+120, T0+180] 边界：120 那笔被排除，180 那笔算进来 → 10
        self.assertAlmostEqual(net_flow(self.flows, T0 + 120, T0 + 180), 10.0, places=12)

    def test_no_bounds_and_cumulative(self) -> None:
        self.assertAlmostEqual(net_flow(self.flows), 260.0, places=12)
        self.assertAlmostEqual(cum_flow(self.flows, T0 + 60), 300.0, places=12)
        self.assertAlmostEqual(cum_flow(self.flows, T0), 100.0, places=12)

    def test_dirty_flows_are_ignored_not_guessed(self) -> None:
        flows = [{"ts": None, "amount": 999.0}, {"ts": T0 + 10, "amount": float("nan")},
                 {"ts": "garbage", "amount": 5.0}, {"ts": T0 + 20, "value": 7.0}]
        self.assertAlmostEqual(net_flow(flows, T0, T0 + 60), 7.0, places=12)

    def test_normalise_points_drops_dirty_rows(self) -> None:
        rows = [{"ts": T0 + 60, "equity": 900.0}, {"ts": T0, "equity": 1000.0},
                {"ts": T0 + 120, "equity": float("nan")}, {"ts": None, "equity": 1.0},
                {"ts": T0 + 180, "equity": 0.0}]
        self.assertEqual(normalise_points(rows), [(T0, 1000.0), (T0 + 60, 900.0),
                                                  (T0 + 180, 0.0)])


# --------------------------------------------------------------------------- TWR


class BootstrapTests(unittest.TestCase):
    """逐点重放：`index_k = index_{k−1} · (equity_k − flow_k) / equity_{k−1}`。"""

    def test_deposit_does_not_shrink_the_drawdown(self) -> None:
        """10000 → 9000（亏 10%）→ 入金 1000 → 10000。

        剔入金：0.9 · (10000 − 1000)/9000 = 0.9 · 1.0 = **0.9** ⇒ DD = 10%
        不剔：  0.9 · 10000/9000          = **1.0** ⇒ DD = 0%（亏损被人为抹掉）
        """
        state = bootstrap(points((0, 10000.0), (TICK, 9000.0), (2 * TICK, 10000.0)),
                          [deposit(T0 + 2 * TICK, 1000.0)],
                          anchor_ts=T0, anchor_equity=10000.0)
        self.assertAlmostEqual(state.index, 0.9, places=12)
        self.assertAlmostEqual(state.peak, 1.0, places=12)
        self.assertAlmostEqual(state.drawdown, 0.1, places=12)
        self.assertEqual((state.steps, state.skipped), (2, 0))
        self.assertFalse(state.degraded)

        naive = bootstrap(points((0, 10000.0), (TICK, 9000.0), (2 * TICK, 10000.0)), [],
                          anchor_ts=T0, anchor_equity=10000.0)
        self.assertAlmostEqual(naive.index, 1.0, places=12)
        self.assertAlmostEqual(naive.drawdown, 0.0, places=12)

    def test_withdrawal_does_not_create_a_fake_drawdown(self) -> None:
        """10000 → 提现 1000（权益 8000）。

        剔出金：0.9 · (8000 + 1000)/10000 = 0.9 ⇒ DD = 10%（真实亏损）
        不剔：  0.8                        ⇒ DD = 20%（把提现算成了亏损）
        """
        state = bootstrap(points((0, 10000.0), (TICK, 8000.0)),
                          [deposit(T0 + TICK, -1000.0)],
                          anchor_ts=T0, anchor_equity=10000.0)
        self.assertAlmostEqual(state.index, 0.9, places=12)
        self.assertAlmostEqual(state.drawdown, 0.1, places=12)

        naive = bootstrap(points((0, 10000.0), (TICK, 8000.0)), [],
                          anchor_ts=T0, anchor_equity=10000.0)
        self.assertAlmostEqual(naive.drawdown, 0.2, places=12)

    def test_multi_step_chain_with_a_deposit_in_the_middle(self) -> None:
        """10000 → 11000 → 入金 1000 → 11000 → 9900。

        step1: 11000/10000            = 1.1   ⇒ index 1.1
        step2: (11000 − 1000)/11000   = 0.909090909…  ⇒ index 1.1 × 0.90909… = 1.0
        step3: 9900/11000             = 0.9   ⇒ index 0.9
        peak = 1.1 ⇒ DD = (1.1 − 0.9)/1.1 = 0.181818181…
        """
        state = bootstrap(
            points((0, 10000.0), (TICK, 11000.0), (2 * TICK, 11000.0),
                   (3 * TICK, 9900.0)),
            [deposit(T0 + 2 * TICK, 1000.0)],
            anchor_ts=T0, anchor_equity=10000.0)
        self.assertAlmostEqual(state.index, 0.9, places=12)
        self.assertAlmostEqual(state.peak, 1.1, places=12)
        self.assertAlmostEqual(state.drawdown, 0.18181818181818182, places=12)

    def test_negative_ratio_clamps_to_zero_and_flags_degraded(self) -> None:
        """(500 − 10000)/10000 = −0.95 ⇒ 指数归零（DD = 100%），不产出负指数。"""
        state = bootstrap(points((0, 10000.0), (TICK, 500.0)),
                          [deposit(T0 + TICK, 10000.0)],
                          anchor_ts=T0, anchor_equity=10000.0)
        self.assertEqual(state.index, 0.0)
        self.assertAlmostEqual(state.drawdown, 1.0, places=12)
        self.assertTrue(state.degraded)
        self.assertIn("negative index", state.reason)

    def test_non_positive_previous_equity_is_skipped_not_guessed(self) -> None:
        """上一格权益 0 ⇒ 收益率无定义：指数不动，`skipped + 1`。"""
        state = bootstrap(points((0, 10000.0), (TICK, 0.0), (2 * TICK, 1000.0)),
                          anchor_ts=T0, anchor_equity=10000.0)
        self.assertEqual(state.index, 0.0)      # step1: 0/10000 = 0
        self.assertEqual(state.skipped, 1)      # step2 被跳过
        self.assertEqual(state.steps, 2)

    def test_points_at_or_before_the_anchor_are_ignored(self) -> None:
        state = bootstrap([(T0 - 10 * TICK, 5000.0), (T0, 10000.0)],
                          anchor_ts=T0, anchor_equity=10000.0)
        self.assertEqual((state.index, state.peak), (1.0, 1.0))
        self.assertEqual(state.steps, 0)

    def test_invalid_anchor_raises(self) -> None:
        for bad_ts, bad_eq in ((0, 10000.0), (None, 10000.0), (T0, 0),
                               (T0, float("nan")), (T0, float("inf"))):
            with self.subTest(anchor=(bad_ts, bad_eq)):
                with self.assertRaises(DdError):
                    bootstrap([], anchor_ts=bad_ts, anchor_equity=bad_eq)


class AdvanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = bootstrap(points((0, 10000.0)), anchor_ts=T0, anchor_equity=10000.0)

    def test_peak_only_rises(self) -> None:
        up = advance(self.state, ts=T0 + TICK, equity=12000.0)
        self.assertAlmostEqual(up.index, 1.2, places=12)
        self.assertAlmostEqual(up.peak, 1.2, places=12)
        down = advance(up, ts=T0 + 2 * TICK, equity=9000.0)
        self.assertAlmostEqual(down.index, 0.9, places=12)     # 1.2 × 9000/12000
        self.assertAlmostEqual(down.peak, 1.2, places=12)      # 峰值不降
        self.assertAlmostEqual(down.drawdown, 0.25, places=12)  # (1.2 − 0.9)/1.2

    def test_same_timestamp_is_not_counted_twice(self) -> None:
        once = advance(self.state, ts=T0 + TICK, equity=11000.0)
        twice = advance(once, ts=T0 + TICK, equity=11000.0)
        self.assertIs(twice, once)                             # 原样返回，指数不动
        self.assertAlmostEqual(twice.index, 1.1, places=12)

    def test_dirty_input_returns_none(self) -> None:
        self.assertIsNone(advance(self.state, ts=None, equity=1000.0))
        self.assertIsNone(advance(self.state, ts=T0 + TICK, equity=float("nan")))


# --------------------------------------------------------------------------- 档位


class TierLinkageTests(unittest.TestCase):
    """阈值与判定**只能**来自 `app.risk`（同一个函数，不是同一组数字）。"""

    def test_dd_module_reuses_the_risk_functions(self) -> None:
        self.assertIs(dd_module.dd_action, risk_module.dd_action)
        self.assertIs(dd_module.dd_thresholds, risk_module.dd_thresholds)
        self.assertIs(dd_module.RISK_TIERS, risk_module.RISK_TIERS)
        self.assertIs(dd_module.risk_drawdown, risk_module.drawdown)

    def test_three_tiers_with_hardcoded_lines(self) -> None:
        # (index, peak) 取整到好算的数：DD = (100 − index)/100
        cases = (
            # risk_pct, dd, 期望 action
            (0.0133, 0.2499, "ok"), (0.0133, 0.25, "warn"), (0.0133, 0.2999, "warn"),
            (0.0133, 0.30, "kill"),
            (0.02, 0.2999, "ok"), (0.02, 0.30, "warn"), (0.02, 0.3999, "warn"),
            (0.02, 0.40, "kill"),
            (0.03, 0.3999, "ok"), (0.03, 0.40, "warn"), (0.03, 0.4999, "warn"),
            (0.03, 0.50, "kill"),
        )
        for risk_pct, drawdown, expected in cases:
            with self.subTest(risk_pct=risk_pct, drawdown=drawdown):
                index = 100.0 * (1.0 - drawdown)
                verdict = evaluate(index, 100.0, risk_pct=risk_pct)
                self.assertAlmostEqual(verdict.drawdown, drawdown, places=9)
                self.assertEqual(verdict.action, expected)

    def test_reported_thresholds_match_the_tier_table(self) -> None:
        self.assertEqual((evaluate(60.0, 100.0, risk_pct=0.0133).warn,
                          evaluate(60.0, 100.0, risk_pct=0.0133).kill), (0.25, 0.30))
        self.assertEqual((evaluate(60.0, 100.0, risk_pct=0.02).warn,
                          evaluate(60.0, 100.0, risk_pct=0.02).kill), (0.30, 0.40))
        self.assertEqual((evaluate(60.0, 100.0, risk_pct=0.03).warn,
                          evaluate(60.0, 100.0, risk_pct=0.03).kill), (0.40, 0.50))
        # 覆盖值仍然走 risk 的校验（越界 → 两者一起退回档位默认 + rejected）
        override = evaluate(60.0, 100.0, risk_pct=0.0133, warn_override=0.99)
        self.assertTrue(override.thresholds_rejected)
        self.assertEqual((override.warn, override.kill), (0.25, 0.30))

    def test_action_always_equals_risk_dd_action(self) -> None:
        for risk_pct in (0.0133, 0.02, 0.03, None, "garbage"):
            for index in (100.0, 80.0, 70.0, 60.0, 50.0, 40.0):
                with self.subTest(risk_pct=risk_pct, index=index):
                    verdict = evaluate(index, 100.0, risk_pct=risk_pct)
                    self.assertEqual(
                        verdict.action,
                        risk_module.dd_action(verdict.drawdown, risk_pct))


class DirtyValueTests(unittest.TestCase):
    """脏值 → **最严档**（不是放行）。"""

    def test_unreadable_drawdown_is_a_kill(self) -> None:
        for index, peak in ((None, 100.0), (90.0, None), (float("nan"), 100.0),
                            (90.0, float("nan")), (float("inf"), 100.0),
                            (90.0, float("-inf")), (90.0, 0.0), ("garbage", 100.0)):
            with self.subTest(index=index, peak=peak):
                verdict = evaluate(index, peak, risk_pct=0.0133)
                self.assertEqual(verdict.action, "kill")
                self.assertTrue(verdict.strictest)
                self.assertFalse(verdict.available)
                self.assertIsNone(verdict.drawdown)

    def test_unreadable_risk_pct_falls_to_the_strictest_tier(self) -> None:
        for risk_pct in (None, "garbage", float("nan"), float("inf")):
            with self.subTest(risk_pct=risk_pct):
                verdict = evaluate(90.0, 100.0, risk_pct=risk_pct)
                self.assertEqual((verdict.warn, verdict.kill), (0.25, 0.30))
                self.assertEqual(verdict.action, "ok")      # 10% 还没到最严档的 25%
                self.assertIsNone(verdict.risk_pct)

    def test_dirty_risk_pct_triggers_earlier_than_a_sane_high_tier(self) -> None:
        """同一个 28% 回撤：3% 档是 ok（40% 才告警），脏配置按 25% 告警。"""
        self.assertEqual(evaluate(72.0, 100.0, risk_pct=0.03).action, "ok")
        self.assertEqual(evaluate(72.0, 100.0, risk_pct=float("nan")).action, "warn")

    def test_index_above_peak_is_flagged(self) -> None:
        verdict = evaluate(120.0, 100.0, risk_pct=0.0133)
        self.assertAlmostEqual(verdict.drawdown, 0.0, places=12)
        self.assertTrue(verdict.degraded)
        self.assertIn("index > peak", verdict.reason)


# --------------------------------------------------------------------------- 落库


class GuardStoreTests(unittest.TestCase):
    """`DrawdownGuard` 与 `app/store` 的持久化契约。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(os.path.join(tmp.name, "state.db"))
        self.addCleanup(self.store.close)
        self.guard = DrawdownGuard(self.store, limits=LIMITS_133, clock=lambda: T0)

    # ------------------------------------------------------------ 基础

    def test_bootstrap_persists_index_peak_and_anchor(self) -> None:
        self.store.record_equity(10000.0, ts=T0)
        self.guard.bootstrap(equity=10000.0, ts=T0)
        row = self.store.dd_state()
        assert row is not None
        self.assertEqual(row["twr_index"], 1.0)
        self.assertEqual(row["twr_peak"], 1.0)
        self.assertEqual(row["anchor_ts"], T0)
        self.assertEqual(row["anchor_equity"], 10000.0)
        self.assertEqual(row["reset_count"], 0)
        kinds = [e["kind"] for e in self.store.events(limit=10)]
        self.assertIn("dd_bootstrap", kinds)

    def test_bootstrap_refuses_to_overwrite_an_existing_state(self) -> None:
        self.guard.bootstrap(equity=10000.0, ts=T0)
        with self.assertRaises(DdError):
            self.guard.bootstrap(equity=5000.0, ts=T0 + TICK)

    def test_update_after_loss_reads_exactly_ten_percent(self) -> None:
        self.guard.update(equity=10000.0, ts=T0)
        verdict = self.guard.update(equity=9000.0, ts=T0 + TICK)
        self.assertAlmostEqual(verdict.index, 0.9, places=12)
        self.assertAlmostEqual(verdict.peak, 1.0, places=12)
        self.assertAlmostEqual(verdict.drawdown, 0.1, places=12)
        self.assertEqual(verdict.action, "ok")
        self.assertEqual(verdict.source, DD_SOURCE_STATE)
        row = self.store.dd_state()
        assert row is not None
        self.assertAlmostEqual(row["twr_index"], 0.9, places=12)

    def test_deposit_through_the_guard_keeps_the_drawdown(self) -> None:
        """同一个 10% 亏损，中间充 1000 块 —— 回撤必须还是 10%（不被稀释）。"""
        self.guard.update(equity=10000.0, ts=T0)
        self.guard.update(equity=9000.0, ts=T0 + TICK)
        verdict = self.guard.update(equity=10000.0, ts=T0 + 2 * TICK,
                                    flows=[deposit(T0 + 2 * TICK, 1000.0)])
        self.assertAlmostEqual(verdict.index, 0.9, places=12)
        self.assertAlmostEqual(verdict.drawdown, 0.1, places=12)
        row = self.store.dd_state()
        assert row is not None
        self.assertAlmostEqual(row["twr_index"], 0.9, places=12)
        self.assertAlmostEqual(row["anchor_cum_flow"], 1000.0, places=12)

    # ------------------------------------------------------------ 峰值

    def test_peak_never_goes_down_even_when_the_replay_window_is_thin(self) -> None:
        """库里峰值 1.25，重放只看到 0.99 —— 峰值保持 1.25，DD 按 1.25 算。

        (1.25 − 0.99)/1.25 = 0.208
        """
        self.store.record_equity(10000.0, ts=T0)
        self.store.save_dd_state(twr_index=0.9, twr_peak=1.25, anchor_ts=T0,
                                 anchor_equity=10000.0, ts=T0)
        verdict = self.guard.update(equity=9900.0, ts=T0 + TICK)
        self.assertAlmostEqual(verdict.peak, 1.25, places=12)
        self.assertAlmostEqual(verdict.index, 0.99, places=12)
        self.assertAlmostEqual(verdict.drawdown, 0.208, places=12)
        row = self.store.dd_state()
        assert row is not None
        self.assertEqual(row["twr_peak"], 1.25)
        # 28% 会落在 1.33% 档的告警区，这里 20.8% 还不够 —— 顺便证明峰值没被夹到 1.0
        self.assertEqual(verdict.action, "ok")

    def test_store_refuses_a_lower_peak(self) -> None:
        self.store.save_dd_state(twr_index=0.9, twr_peak=1.25, anchor_ts=T0,
                                 anchor_equity=10000.0, ts=T0)
        with self.assertRaises(Exception) as caught:
            self.store.save_dd_state(twr_index=0.9, twr_peak=1.0, anchor_ts=T0,
                                     anchor_equity=10000.0, ts=T0 + TICK)
        self.assertIn("refusing to lower", str(caught.exception))

    def test_kill_transition_is_logged_once(self) -> None:
        """权益 10000 → 9000（DD 10%）→ 6900（DD 31%）→ 6900。

        10% ok；31% ≥ 30% → kill；之后再跑两轮仍然是 kill，**不再**落事件
        （否则 288 条/天的 dd_kill 会把事件流淹掉）。
        """
        self.guard.update(equity=10000.0, ts=T0)
        self.guard.update(equity=9000.0, ts=T0 + TICK)
        kill = self.guard.update(equity=6900.0, ts=T0 + 2 * TICK)
        self.assertAlmostEqual(kill.drawdown, 0.31, places=12)
        self.assertEqual(kill.action, "kill")
        self.guard.update(equity=6900.0, ts=T0 + 3 * TICK)
        self.guard.update(equity=6900.0, ts=T0 + 4 * TICK)
        kills = self.store.events(kind="dd_kill", limit=10)
        self.assertEqual(len(kills), 1, [e["message"] for e in kills])
        self.assertNotIn("dd_warn", [e["kind"] for e in self.store.events(limit=20)])

    def test_value_a_hair_below_the_line_warns_and_that_is_the_honest_answer(self) -> None:
        """权益 7000 = 名义上「正好 30%」，实测 0.29999999999999993 → **warn**。

        指数是逐步累乘的（1.0 × 0.9 × 7000/9000 = 0.7000000000000001），浮点尾数让
        这个点落在熔断线**下方**一个 ulp。这里把实测值写死，是为了钉住两件事：

        * 判定只认数：`dd_action()` 的比较里**没有** epsilon（加 epsilon 就是第二套口径，
          面板显示 30.00%、熔断按 29.99% 算，正是旧系统「三个口径混用」那类 bug）；
        * 面板/推送要报 `action` 和四位以上小数，**别**只报四舍五入的百分比 ——
          否则用户会看到「30.00% 却只告警」而以为熔断坏了。

        真正的熔断行为由 `test_kill_transition_is_logged_once`（31%）钉住。
        """
        self.guard.update(equity=10000.0, ts=T0)
        self.guard.update(equity=9000.0, ts=T0 + TICK)
        verdict = self.guard.update(equity=7000.0, ts=T0 + 2 * TICK)
        self.assertAlmostEqual(verdict.index, 0.7000000000000001, places=15)
        self.assertAlmostEqual(verdict.drawdown, 0.29999999999999993, places=15)
        self.assertLess(verdict.drawdown, 0.30)
        self.assertEqual(f"{verdict.drawdown:.2%}", "30.00%")     # 四舍五入后看起来是 30%
        self.assertEqual(verdict.action, "warn")

    # ------------------------------------------------------------ 只读

    def test_verdict_is_read_only(self) -> None:
        self.guard.update(equity=10000.0, ts=T0)
        self.guard.update(equity=6900.0, ts=T0 + TICK)
        before = dict(self.store.dd_state() or {})
        events_before = self.store.event_count()
        verdict = self.guard.verdict(T0 + TICK)
        self.assertEqual(verdict.action, "kill")
        self.assertEqual(dict(self.store.dd_state() or {}), before)
        self.assertEqual(self.store.event_count(), events_before)

    def test_risk_state_injection_carries_the_same_numbers(self) -> None:
        self.guard.update(equity=10000.0, ts=T0)
        self.guard.update(equity=6900.0, ts=T0 + TICK)
        base = RiskState(equity=6900.0, day_start_equity=6900.0, trades_today=0,
                         open_positions=(), open_notional_total=0.0,
                         open_notional_by_symbol={})
        injected = self.guard.risk_state(base, T0 + TICK)
        self.assertAlmostEqual(injected.dd_index, 0.69, places=12)
        self.assertAlmostEqual(injected.dd_peak_index, 1.0, places=12)


# --------------------------------------------------------------------------- 熔断语义


def flat_bars(start_ms: int, n: int, px: float, spread: float = 0.5):
    return [strategy.Bar(open_ms=start_ms + k * BAR_MS, open=px, high=px + spread,
                         low=px - spread, close=px) for k in range(n)]


def long_position() -> PositionView:
    return PositionView(position_id=1, symbol="BTC", side=1, state=lifecycle.OPEN,
                        qty_int=415600, size_decimals=5, entry_price=112.0,
                        entry_price_int=1120, stop_price=105.0, stop_price_int=1050,
                        price_decimals=1, bar_ms=T0 * 1000)


class CircuitBreakerSemanticsTests(unittest.TestCase):
    """**最关键的一条**：熔断挡开仓、**不挡离场**。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(os.path.join(tmp.name, "state.db"))
        self.addCleanup(self.store.close)
        self.guard = DrawdownGuard(self.store, limits=LIMITS_133, clock=lambda: T0)
        self.guard.update(equity=10000.0, ts=T0)
        self.kill_verdict = self.guard.update(equity=6000.0, ts=T0 + TICK)
        # 当日亏损不参与判定：日初权益设为当前权益，只剩 drawdown 闸会拦
        self.risk_state = RiskState(equity=6000.0, day_start_equity=6000.0, trades_today=0,
                                    open_positions=(), open_notional_total=0.0,
                                    open_notional_by_symbol={})

    def test_the_breaker_really_is_triggered(self) -> None:
        self.assertEqual(self.kill_verdict.action, "kill")
        self.assertAlmostEqual(self.kill_verdict.drawdown, 0.4, places=12)   # (1 − 0.6)/1
        self.assertTrue(self.kill_verdict.blocked)
        self.assertTrue(dd_module.entry_blocked(self.kill_verdict))

    def test_kill_blocks_new_entries_with_only_the_drawdown_gate(self) -> None:
        decision = check_entry("BTC", 1000.0,
                               self.guard.risk_state(self.risk_state, T0 + TICK),
                               self.guard.limits)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.codes, (GATE_DD,))
        self.assertIn("drawdown", decision.reason)

    def test_kill_does_not_block_a_stop_exit(self) -> None:
        """止损离场照常成立：low 104 < stop 105。"""
        bars = flat_bars(int(T0 * 1000), 45, 110.0)
        bars[-1] = strategy.Bar(open_ms=bars[-1].open_ms, open=110.0, high=110.2,
                                low=104.0, close=104.5)
        ind = strategy.compute_indicators(bars)
        decision = lifecycle.decide_exit(long_position(), bars, ind, 44)
        self.assertEqual(decision.action, ACTION_STOP)
        self.assertAlmostEqual(decision.exit_price, 105.0, places=12)

    def test_kill_does_not_block_a_signal_exit(self) -> None:
        """信号离场照常成立：close 108 < SMA20 109.9，且 low 107.5 没打到止损 105。"""
        bars = flat_bars(int(T0 * 1000), 45, 110.0)
        bars[-1] = strategy.Bar(open_ms=bars[-1].open_ms, open=110.0, high=110.2,
                                low=107.5, close=108.0)
        ind = strategy.compute_indicators(bars)
        decision = lifecycle.decide_exit(long_position(), bars, ind, 44)
        self.assertEqual(decision.action, ACTION_EXIT)

    def test_entry_blocked_and_exit_allowed_in_the_same_instant(self) -> None:
        """同一个回撤状态下，开仓被挡、离场不受影响 —— 这就是「单向出口」。"""
        bars = flat_bars(int(T0 * 1000), 45, 110.0)
        bars[-1] = strategy.Bar(open_ms=bars[-1].open_ms, open=110.0, high=110.2,
                                low=104.0, close=104.5)
        ind = strategy.compute_indicators(bars)
        entry = check_entry("BTC", 1000.0,
                            self.guard.risk_state(self.risk_state, T0 + TICK),
                            self.guard.limits)
        exit_decision = lifecycle.decide_exit(long_position(), bars, ind, 44)
        self.assertFalse(entry.allowed)
        self.assertEqual(entry.codes, (GATE_DD,))
        self.assertEqual(exit_decision.action, ACTION_STOP)

    def test_decide_exit_signature_has_no_risk_state(self) -> None:
        import inspect
        names = set(inspect.signature(lifecycle.decide_exit).parameters)
        self.assertNotIn("risk_state", names)
        self.assertNotIn("limits", names)
        self.assertFalse(hasattr(dd_module, "exit_blocked"))

    def test_kill_survives_a_restart(self) -> None:
        """重启 = 新建 guard/assembler 读同一个库：结论不许变。"""
        fresh = DrawdownGuard(StateStore(self.store.path), limits=LIMITS_133, clock=lambda: T0)
        self.addCleanup(fresh.store.close)
        verdict = fresh.verdict(T0 + TICK)
        self.assertEqual(verdict.action, "kill")
        self.assertAlmostEqual(verdict.drawdown, 0.4, places=12)


# --------------------------------------------------------------------------- 锚点生命周期


class AnchorLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(os.path.join(tmp.name, "state.db"))
        self.addCleanup(self.store.close)
        self.guard = DrawdownGuard(self.store, limits=LIMITS_133, clock=lambda: T0)

    def test_anchors_are_appended_and_pruned_but_never_carry_the_verdict(self) -> None:
        self.guard.update(equity=10000.0, ts=T0)
        self.guard.update(equity=6900.0, ts=T0 + TICK)
        self.assertGreaterEqual(self.store.dd_anchor_count(), 2)
        verdict_before = self.guard.verdict(T0 + TICK)
        removed = self.store.prune_dd_anchors(keep_days=30.0, ts=T0 + 40 * 86400.0)
        self.assertGreater(removed, 0)
        self.assertEqual(self.store.dd_anchor_count(), 1)      # 最新一行永不删
        verdict_after = self.guard.verdict(T0 + TICK)
        self.assertEqual(verdict_after.action, verdict_before.action)
        self.assertAlmostEqual(verdict_after.drawdown, verdict_before.drawdown, places=12)

    def test_pruning_all_anchors_does_not_reset_the_drawdown(self) -> None:
        """锚点被剪（甚至全删）≠ 回撤归零：`dd_state` 才是判据。"""
        self.guard.update(equity=10000.0, ts=T0)
        self.guard.update(equity=6900.0, ts=T0 + TICK)
        with self.store.write_transaction() as conn:
            conn.execute("DELETE FROM dd_anchors WHERE ts <= ?", (T0 + TICK,))
        self.assertEqual(self.store.dd_anchor_count(), 0)
        verdict = self.guard.verdict(T0 + TICK)
        self.assertTrue(verdict.available)
        self.assertEqual(verdict.source, DD_SOURCE_STATE)
        self.assertAlmostEqual(verdict.drawdown, 0.31, places=12)
        self.assertEqual(verdict.action, "kill")

    def test_anchor_fallback_is_degraded_but_usable(self) -> None:
        self.store.append_dd_anchor(T0, 10000.0, twr_index=0.8, twr_peak=1.0)
        verdict = self.guard.verdict(T0 + TICK)
        self.assertTrue(verdict.available)
        self.assertTrue(verdict.degraded)
        self.assertEqual(verdict.source, DD_SOURCE_ANCHOR)
        self.assertAlmostEqual(verdict.drawdown, 0.2, places=12)

    def test_losing_state_and_anchors_blocks_entries_instead_of_reading_zero(self) -> None:
        """**这条是重点**：口径不可用 → 拒单，而不是「回撤 0% 放行」。"""
        self.guard.update(equity=10000.0, ts=T0)
        with self.store.write_transaction() as conn:
            conn.execute("DELETE FROM dd_state")
            conn.execute("DELETE FROM dd_anchors")
        verdict = self.guard.verdict(T0 + TICK)
        self.assertEqual(verdict.action, "kill")
        self.assertFalse(verdict.available)
        self.assertTrue(verdict.strictest)
        self.assertIsNone(verdict.drawdown)
        self.assertEqual(verdict.source, DD_SOURCE_UNAVAILABLE)

        state = RiskState(equity=10000.0, day_start_equity=10000.0, trades_today=0,
                          open_positions=(), open_notional_total=0.0,
                          open_notional_by_symbol={})
        decision = check_entry("BTC", 1000.0, self.guard.risk_state(state, T0 + TICK),
                               self.guard.limits)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.codes, (GATE_DD,))
        self.assertIn("fail-closed", decision.reason)


# --------------------------------------------------------------------------- 重置


class ResetPeakTests(unittest.TestCase):
    """`reset_peak()` 是**唯一**能降峰值的路径：显式、带理由、落审计、永不自解。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(os.path.join(tmp.name, "state.db"))
        self.addCleanup(self.store.close)
        self.guard = DrawdownGuard(self.store, limits=LIMITS_133, clock=lambda: T0)

    def _kill(self) -> DdVerdict:
        self.guard.update(equity=10000.0, ts=T0)
        return self.guard.update(equity=6900.0, ts=T0 + TICK)

    def test_update_never_calls_reset(self) -> None:
        """自动路径**绝不**调 `reset_dd_peak()` —— 熔断永不自解。"""
        self._kill()
        with mock.patch.object(self.store, "reset_dd_peak",
                               side_effect=AssertionError("update() must never reset the peak")):
            for k in range(5):
                verdict = self.guard.update(equity=6900.0, ts=T0 + (2 + k) * TICK)
                self.assertEqual(verdict.action, "kill")
        row = self.store.dd_state()
        assert row is not None
        self.assertEqual(row["reset_count"], 0)
        self.assertEqual(row["twr_peak"], 1.0)

    def test_reset_requires_a_reason(self) -> None:
        self._kill()
        for bad in ("", "   ", None):
            with self.subTest(reason=bad):
                with self.assertRaises(DdError):
                    self.guard.reset_peak(reason=bad)

    def test_reset_is_audited_and_restores_entries(self) -> None:
        self._kill()
        verdict = self.guard.reset_peak(reason="human reviewed the 31% drawdown",
                                        equity=6900.0, ts=T0 + 2 * TICK)
        row = self.store.dd_state()
        assert row is not None
        self.assertEqual((row["twr_index"], row["twr_peak"]), (BASE_TWR_INDEX, BASE_TWR_INDEX))
        self.assertEqual(row["reset_count"], 1)
        self.assertEqual(row["anchor_ts"], T0 + 2 * TICK)
        self.assertEqual(row["anchor_equity"], 6900.0)
        self.assertIn("human reviewed", row["reset_reason"])
        self.assertEqual(verdict.action, "ok")
        self.assertAlmostEqual(verdict.drawdown, 0.0, places=12)

        # 两条事件各记一件事：store 的「峰值被改了」+ 本方法/调用方的「谁改的」
        resets = self.store.events(kind="dd_reset", limit=5)
        self.assertEqual(len(resets), 1)
        self.assertIn("human reviewed the 31% drawdown", resets[0]["message"])
        audits = self.store.events(kind="dd_reset_audit", limit=5)
        self.assertEqual(len(audits), 1)
        self.assertIn("by admin", audits[0]["message"])
        self.assertIn("previous index=0.69", audits[0]["message"])

        state = RiskState(equity=6900.0, day_start_equity=6900.0, trades_today=0,
                          open_positions=(), open_notional_total=0.0,
                          open_notional_by_symbol={})
        decision = check_entry("BTC", 1000.0, self.guard.risk_state(state, T0 + 2 * TICK),
                               self.guard.limits)
        self.assertTrue(decision.allowed, decision.reason)

    def test_reset_refuses_a_wiped_out_account(self) -> None:
        self.store.save_dd_state(twr_index=0.0, twr_peak=1.0, anchor_ts=T0,
                                 anchor_equity=10000.0, ts=T0)
        with self.assertRaises(DdError) as caught:
            self.guard.reset_peak(reason="try to hide the blow-up", equity=1.0)
        self.assertIn("refusing to reset", str(caught.exception))

    def test_reset_requires_an_existing_state(self) -> None:
        with self.assertRaises(DdError):
            self.guard.reset_peak(reason="nothing to reset")


# --------------------------------------------------------------------------- 其它


class LedgerTests(unittest.TestCase):
    """`equity_curve.twr_index` 与判定用的是同一个指数（面板/T10c 的硬约束）。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(os.path.join(tmp.name, "state.db"))
        self.addCleanup(self.store.close)
        self.guard = DrawdownGuard(self.store, limits=LIMITS_133, clock=lambda: T0)

    def test_equity_curve_carries_the_same_index_as_the_verdict(self) -> None:
        self.guard.update(equity=10000.0, ts=T0)
        verdict = self.guard.update(equity=8700.0, ts=T0 + TICK)
        point = self.store.equity_point(T0 + TICK)
        assert point is not None
        self.assertAlmostEqual(point["twr_index"], verdict.index, places=12)
        self.assertAlmostEqual(point["twr_index"], 0.87, places=12)
        self.assertEqual(point["source"], "dd")

    def test_display_index_is_the_base_100_convention(self) -> None:
        self.guard.update(equity=10000.0, ts=T0)
        verdict = self.guard.update(equity=8700.0, ts=T0 + TICK)
        self.assertAlmostEqual(verdict.display_index, 87.0, places=12)
        self.assertAlmostEqual(verdict.display_peak, 100.0, places=12)
        twr = bootstrap(points((0, 10000.0), (TICK, 8700.0)),
                        anchor_ts=T0, anchor_equity=10000.0)
        self.assertAlmostEqual(twr.display_index, 87.0, places=12)

    def test_unreadable_equity_is_skipped_without_writing(self) -> None:
        self.guard.update(equity=10000.0, ts=T0)
        before = dict(self.store.dd_state() or {})
        verdict = self.guard.update(equity=float("nan"), ts=T0 + TICK)
        self.assertEqual(dict(self.store.dd_state() or {}), before)
        self.assertEqual(verdict.action, "ok")            # 沿用上一轮的状态
        self.assertIn("dd_skipped", [e["kind"] for e in self.store.events(limit=10)])

    def test_utc_day_helper_is_untouched_by_dd(self) -> None:
        """回撤口径不参与「当日」切割（那是 daily_loss 闸的事）—— 只做一次 sanity 检查。"""
        self.assertEqual(utc_day(T0), "2025-10-09")


if __name__ == "__main__":                      # pragma: no cover
    unittest.main()
