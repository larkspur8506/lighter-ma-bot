"""`app/risk.py` 的契约测试 —— 全离线（纯函数，无网络、无 DB）。

钉住的东西（T4 验收项）：
  * 每个闸门**单独**可解释：拒单必须带稳定 `code`（不是解析文案）；
  * 全部 **fail-closed**：状态缺失 / 口径算不出 → 拒绝，不是放行；
  * DD Guard 阈值按 `risk_pct` 档位联动（1.33% → 25%/30%，2% → 30%/40%，3% → 40%/50%）；
  * kill / 熔断只挡**开新仓**（语义写在断言里，防止有人把它挪去挡平仓）。

期望值全部手算写死。
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, Optional

from app import risk
from app.risk import (
    GATE_DAILY_LOSS, GATE_DAILY_TRADES, GATE_DD, GATE_DUPLICATE_SYMBOL, GATE_KILL,
    GATE_MAX_POSITIONS, GATE_STATE, GATE_SYMBOL_NOTIONAL, GATE_TOTAL_NOTIONAL,
    GateResult, RiskConfigError, RiskDecision, RiskLimits, RiskState, check_entry,
)


# ------------------------------------------------------------------ fixtures


def state(**overrides: Any) -> RiskState:
    """一个「一切正常」的账户状态，按需覆盖。权益 10,000 / 无持仓 / DD 0%。"""
    base: Dict[str, Any] = dict(
        equity=10000.0,
        day_start_equity=10000.0,
        trades_today=0,
        open_positions=(),
        open_notional_total=0.0,
        open_notional_by_symbol={},
        dd_index=100.0,
        dd_peak_index=100.0,
    )
    base.update(overrides)
    return RiskState(**base)


def decision(symbol: str = "BTC", notional: float = 3324.8, *,
             st: Optional[RiskState] = None,
             limits: Optional[RiskLimits] = None) -> RiskDecision:
    return check_entry(symbol, notional, st if st is not None else state(), limits)


def codes(result: RiskDecision) -> set:
    return set(result.codes)


# ------------------------------------------------------------------ 阈值


class ThresholdTests(unittest.TestCase):
    """DD 阈值：照 PLAN T9 / 旧 `dd.py::TIERS`，风险档越高阈值越宽。"""

    def test_tiers_by_risk_pct(self) -> None:
        self.assertEqual(risk.dd_thresholds(0.0133)["warn"], 0.25)
        self.assertEqual(risk.dd_thresholds(0.0133)["kill"], 0.30)
        self.assertEqual(risk.dd_thresholds(0.01)["warn"], 0.25)     # ≤1.5% 档
        self.assertEqual(risk.dd_thresholds(0.015)["warn"], 0.25)    # 边界含在本档
        self.assertEqual(risk.dd_thresholds(0.02)["warn"], 0.30)     # ≤2.5% 档
        self.assertEqual(risk.dd_thresholds(0.02)["kill"], 0.40)
        self.assertEqual(risk.dd_thresholds(0.03)["warn"], 0.40)     # 更大档
        self.assertEqual(risk.dd_thresholds(0.03)["kill"], 0.50)
        self.assertEqual(risk.dd_thresholds(0.10)["kill"], 0.50)

    def test_unreadable_risk_pct_uses_strictest_tier(self) -> None:
        self.assertEqual(risk.dd_thresholds(None)["kill"], 0.30)
        self.assertEqual(risk.dd_thresholds("garbage")["kill"], 0.30)
        self.assertEqual(risk.dd_thresholds(float("nan"))["kill"], 0.30)

    def test_overrides(self) -> None:
        out = risk.dd_thresholds(0.0133, 0.10, 0.20)
        self.assertEqual((out["warn"], out["kill"]), (0.10, 0.20))
        self.assertFalse(out["auto"])
        self.assertFalse(out["rejected"])

    def test_contradictory_override_falls_back_to_tier(self) -> None:
        out = risk.dd_thresholds(0.0133, 0.40, 0.30)   # warn > kill
        self.assertEqual((out["warn"], out["kill"]), (0.25, 0.30))
        self.assertTrue(out["rejected"])
        out = risk.dd_thresholds(0.0133, 0.99, 0.0)    # 越界
        self.assertEqual((out["warn"], out["kill"]), (0.25, 0.30))
        self.assertTrue(out["rejected"])

    def test_dd_action(self) -> None:
        self.assertEqual(risk.dd_action(0.0, 0.0133), "ok")
        self.assertEqual(risk.dd_action(0.2499, 0.0133), "ok")
        self.assertEqual(risk.dd_action(0.25, 0.0133), "warn")
        self.assertEqual(risk.dd_action(0.2999, 0.0133), "warn")
        self.assertEqual(risk.dd_action(0.30, 0.0133), "kill")
        self.assertEqual(risk.dd_action(0.55, 0.0133), "kill")
        self.assertEqual(risk.dd_action(None, 0.0133), "kill")     # 口径不明 → 最坏情况

    def test_drawdown(self) -> None:
        self.assertAlmostEqual(risk.drawdown(80.0, 100.0), 0.2, places=12)
        self.assertAlmostEqual(risk.drawdown(0.0, 100.0), 1.0, places=12)
        self.assertEqual(risk.drawdown(120.0, 100.0), 0.0)          # 新高 → 无回撤
        self.assertIsNone(risk.drawdown(None, 100.0))
        self.assertIsNone(risk.drawdown(80.0, 0.0))
        self.assertIsNone(risk.drawdown(80.0, None))
        self.assertIsNone(risk.drawdown(-1.0, 100.0))
        self.assertIsNone(risk.drawdown(float("nan"), 100.0))


# ------------------------------------------------------------------ 放行


class HappyPathTests(unittest.TestCase):
    def test_all_gates_pass(self) -> None:
        result = decision()
        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.codes, ())
        self.assertEqual(result.reason, "")
        self.assertEqual(len(result.gates), 7)
        self.assertEqual([g.gate for g in result.gates],
                         [GATE_KILL, GATE_STATE, GATE_DAILY_LOSS, GATE_DAILY_TRADES,
                          GATE_MAX_POSITIONS, GATE_SYMBOL_NOTIONAL, GATE_DD])
        self.assertTrue(all(g.ok for g in result.gates))
        self.assertEqual(result.blocked, ())
        self.assertEqual(result.warnings, ())

    def test_limits_defaults_are_the_legacy_live_values(self) -> None:
        limits = RiskLimits()
        self.assertEqual(limits.risk_pct, 0.0133)
        self.assertEqual(limits.max_daily_loss_pct, 0.05)
        self.assertEqual(limits.max_trades_per_day, 4)
        self.assertEqual(limits.max_positions, 3)
        self.assertEqual(limits.max_symbol_notional_mult, 1.5)
        self.assertEqual(limits.max_total_notional_mult, 2.5)
        self.assertFalse(limits.kill)
        self.assertEqual((limits.dd_warn, limits.dd_kill), (0.25, 0.30))

    def test_two_open_positions_still_allow_the_third(self) -> None:
        st = state(open_positions=("ETH", "SOL"),
                   open_notional_total=2000.0,
                   open_notional_by_symbol={"ETH": 1200.0, "SOL": 800.0})
        result = decision("BTC", 3324.8, st=st)
        self.assertTrue(result.allowed, result.reason)

    def test_declared_totals_equal_to_cap_are_allowed(self) -> None:
        # 名义正好等于上限（1.5x / 2.5x）→ 放行（比较用 >，不是 >=）
        self.assertTrue(decision("BTC", 15000.0).allowed)
        st = state(open_notional_total=10000.0)
        self.assertTrue(decision("BTC", 15000.0, st=st).allowed)


# ------------------------------------------------------------------ 各闸门拒单


class KillSwitchTests(unittest.TestCase):
    def test_kill_blocks_new_entries(self) -> None:
        result = decision(limits=RiskLimits(kill=True))
        self.assertFalse(result.allowed)
        self.assertIn(GATE_KILL, codes(result))
        gate = result.gate(GATE_KILL)
        assert gate is not None
        self.assertTrue(gate.blocked)
        self.assertIn("exits", gate.message)      # kill 只挡开仓，不挡离场

    def test_kill_does_not_hide_other_gates(self) -> None:
        result = decision(st=state(equity=None), limits=RiskLimits(kill=True))
        self.assertTrue({GATE_KILL, GATE_STATE}.issubset(codes(result)))


class StateTests(unittest.TestCase):
    def test_missing_state_blocks_everything_fail_closed(self) -> None:
        result = check_entry("BTC", 1000.0, RiskState())
        self.assertFalse(result.allowed)
        self.assertIn(GATE_STATE, codes(result))
        self.assertIn(GATE_DD, codes(result))
        self.assertIn(GATE_DAILY_LOSS, codes(result))
        self.assertIn("unavailable", result.reason)

    def test_zero_or_negative_equity_blocks(self) -> None:
        for equity in (0.0, -1.0, None, float("nan")):
            result = decision(st=state(equity=equity))
            self.assertFalse(result.allowed, equity)
            self.assertIn(GATE_STATE, codes(result))

    def test_missing_day_start_equity_blocks(self) -> None:
        result = decision(st=state(day_start_equity=None))
        self.assertFalse(result.allowed)
        self.assertIn(GATE_DAILY_LOSS, codes(result))
        gate = result.gate(GATE_DAILY_LOSS)
        assert gate is not None
        self.assertTrue(gate.detail["state_unavailable"])
        self.assertIn("fail-closed", gate.message)

    def test_missing_trades_today_blocks(self) -> None:
        result = decision(st=state(trades_today=None))
        self.assertFalse(result.allowed)
        self.assertIn(GATE_DAILY_TRADES, codes(result))

    def test_missing_open_notional_total_with_positions_blocks(self) -> None:
        st = state(open_positions=("ETH",), open_notional_total=None)
        result = decision("BTC", 1000.0, st=st)
        self.assertFalse(result.allowed)
        self.assertIn(GATE_TOTAL_NOTIONAL, codes(result))

    def test_total_notional_falls_back_to_by_symbol_sum(self) -> None:
        st = state(open_positions=("ETH",), open_notional_total=None,
                   open_notional_by_symbol={"ETH": 500.0})
        self.assertTrue(decision("BTC", 1000.0, st=st).allowed)


class DailyLossTests(unittest.TestCase):
    def test_limit_is_minus_5pct(self) -> None:
        # 10,000 → 9,400 = -6% → 超过 5% → 拒
        result = decision(st=state(equity=9400.0))
        self.assertFalse(result.allowed)
        self.assertIn(GATE_DAILY_LOSS, codes(result))
        gate = result.gate(GATE_DAILY_LOSS)
        assert gate is not None
        self.assertAlmostEqual(gate.detail["loss_pct"], 0.06, places=12)
        self.assertEqual(gate.detail["max_daily_loss_pct"], 0.05)

    def test_exactly_at_limit_blocks(self) -> None:
        self.assertIn(GATE_DAILY_LOSS, codes(decision(st=state(equity=9500.0))))

    def test_below_limit_passes(self) -> None:
        self.assertTrue(decision(st=state(equity=9501.0)).allowed)

    def test_profit_day_is_not_a_loss(self) -> None:
        self.assertTrue(decision(st=state(equity=12000.0)).allowed)

    def test_custom_limit(self) -> None:
        limits = RiskLimits(max_daily_loss_pct=0.02)
        self.assertIn(GATE_DAILY_LOSS, codes(decision(st=state(equity=9700.0), limits=limits)))
        self.assertTrue(decision(st=state(equity=9810.0), limits=limits).allowed)


class DailyTradeCountTests(unittest.TestCase):
    def test_limit_is_four(self) -> None:
        self.assertTrue(decision(st=state(trades_today=3)).allowed)
        result = decision(st=state(trades_today=4))
        self.assertFalse(result.allowed)
        self.assertIn(GATE_DAILY_TRADES, codes(result))
        gate = result.gate(GATE_DAILY_TRADES)
        assert gate is not None
        self.assertEqual(gate.detail["trades_today"], 4)
        self.assertEqual(gate.detail["max_trades_per_day"], 4)

    def test_over_limit_blocks(self) -> None:
        self.assertIn(GATE_DAILY_TRADES, codes(decision(st=state(trades_today=9))))

    def test_custom_limit_and_zero_disables_all_entries(self) -> None:
        self.assertIn(GATE_DAILY_TRADES,
                      codes(decision(st=state(trades_today=2),
                                     limits=RiskLimits(max_trades_per_day=2))))
        self.assertIn(GATE_DAILY_TRADES,
                      codes(decision(st=state(trades_today=0),
                                     limits=RiskLimits(max_trades_per_day=0))))


class PositionCountTests(unittest.TestCase):
    def test_one_position_per_symbol(self) -> None:
        result = decision("BTC", 1000.0, st=state(open_positions=("BTC",),
                                                   open_notional_total=1000.0,
                                                   open_notional_by_symbol={"BTC": 1000.0}))
        self.assertFalse(result.allowed)
        self.assertIn(GATE_DUPLICATE_SYMBOL, codes(result))
        self.assertNotIn(GATE_MAX_POSITIONS, codes(result))

    def test_duplicate_check_is_case_insensitive(self) -> None:
        result = decision("BTC", 1000.0, st=state(open_positions=("btc",),
                                                   open_notional_total=1000.0,
                                                   open_notional_by_symbol={"btc": 1000.0}))
        self.assertIn(GATE_DUPLICATE_SYMBOL, codes(result))

    def test_three_positions_is_the_cap(self) -> None:
        st = state(open_positions=("ETH", "SOL", "XRP"))
        result = decision("BTC", 1000.0, st=st)
        self.assertFalse(result.allowed)
        self.assertIn(GATE_MAX_POSITIONS, codes(result))
        gate = result.gate(GATE_MAX_POSITIONS)
        assert gate is not None
        self.assertEqual(gate.detail["max_positions"], 3)
        self.assertEqual(gate.detail["open_positions"], ["ETH", "SOL", "XRP"])

    def test_custom_cap(self) -> None:
        result = decision("BTC", 1000.0, st=state(open_positions=("ETH",)),
                          limits=RiskLimits(max_positions=1))
        self.assertIn(GATE_MAX_POSITIONS, codes(result))


class NotionalCapTests(unittest.TestCase):
    def test_symbol_cap_is_1_5x_equity(self) -> None:
        # 已有 14,000 + 新 2,000 = 16,000 > 15,000
        st = state(open_positions=("BTC",), open_notional_total=14000.0,
                   open_notional_by_symbol={"BTC": 14000.0})
        result = decision("BTC", 2000.0, st=st)
        self.assertFalse(result.allowed)
        self.assertIn(GATE_SYMBOL_NOTIONAL, codes(result))
        gate = result.gate(GATE_SYMBOL_NOTIONAL)
        assert gate is not None
        self.assertEqual(gate.detail["symbol_cap"], 15000.0)
        self.assertEqual(gate.detail["symbol_after"], 16000.0)

    def test_total_cap_is_2_5x_equity(self) -> None:
        # 已有总名义 24,000 + 新 2,000 = 26,000 > 25,000（单标的不超 15,000）
        st = state(open_positions=("ETH", "SOL"), open_notional_total=24000.0,
                   open_notional_by_symbol={"ETH": 12000.0, "SOL": 12000.0})
        result = decision("BTC", 2000.0, st=st)
        self.assertFalse(result.allowed)
        self.assertIn(GATE_TOTAL_NOTIONAL, codes(result))
        gate = result.gate(GATE_TOTAL_NOTIONAL)
        assert gate is not None
        self.assertEqual(gate.detail["total_cap"], 25000.0)
        self.assertEqual(gate.detail["total_after"], 26000.0)

    def test_bad_notional_blocks(self) -> None:
        for notional in (0.0, -1.0, None, float("nan"), float("inf")):
            result = decision(notional=notional)          # type: ignore[arg-type]
            self.assertFalse(result.allowed, notional)
            self.assertIn(risk.GATE_BAD_NOTIONAL, codes(result))

    def test_custom_multipliers(self) -> None:
        limits = RiskLimits(max_symbol_notional_mult=0.2, max_total_notional_mult=0.3)
        self.assertIn(GATE_SYMBOL_NOTIONAL, codes(decision(notional=2500.0, limits=limits)))
        self.assertTrue(decision(notional=1500.0, limits=limits).allowed)


class DrawdownGuardTests(unittest.TestCase):
    def test_kill_threshold_at_1_33pct_risk(self) -> None:
        # DD = (100 - 69) / 100 = 31% >= 30% → 拒绝开新仓
        result = decision(st=state(dd_index=69.0))
        self.assertFalse(result.allowed)
        self.assertIn(GATE_DD, codes(result))
        gate = result.gate(GATE_DD)
        assert gate is not None
        self.assertAlmostEqual(gate.detail["drawdown"], 0.31, places=12)
        self.assertEqual(gate.detail["kill"], 0.30)
        self.assertIn("risk tier", gate.message)

    def test_exactly_at_kill_threshold_blocks(self) -> None:
        self.assertIn(GATE_DD, codes(decision(st=state(dd_index=70.0))))

    def test_warn_zone_allows_but_warns(self) -> None:
        # DD = 26% ≥ 25%（告警）且 < 30%（熔断）→ 放行，但必须留 warn
        result = decision(st=state(dd_index=74.0))
        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual(result.warnings[0].gate, GATE_DD)
        self.assertEqual(result.warnings[0].severity, risk.SEVERITY_WARN)
        self.assertIn("warn", result.warnings[0].message)

    def test_below_warn_is_clean(self) -> None:
        result = decision(st=state(dd_index=80.0))
        self.assertTrue(result.allowed)
        self.assertEqual(result.warnings, ())

    def test_threshold_follows_risk_tier(self) -> None:
        """同一 31% 回撤：1.33% 档熔断，3% 档（阈值 50%）不熔断。"""
        st = state(dd_index=69.0)
        self.assertIn(GATE_DD, codes(decision(st=st, limits=RiskLimits(risk_pct=0.0133))))
        self.assertNotIn(GATE_DD, codes(decision(st=st, limits=RiskLimits(risk_pct=0.03))))

    def test_missing_dd_state_blocks_fail_closed(self) -> None:
        result = decision(st=state(dd_index=None))
        self.assertFalse(result.allowed)
        self.assertIn(GATE_DD, codes(result))
        self.assertIn("fail-closed", result.reason)

        result = decision(st=state(dd_peak_index=None))
        self.assertIn(GATE_DD, codes(result))

    def test_peak_zero_blocks(self) -> None:
        self.assertIn(GATE_DD, codes(decision(st=state(dd_peak_index=0.0))))

    def test_negative_index_blocks(self) -> None:
        self.assertIn(GATE_DD, codes(decision(st=state(dd_index=-5.0))))

    def test_override_thresholds(self) -> None:
        limits = RiskLimits(dd_warn_pct=0.10, dd_kill_pct=0.15)
        self.assertIn(GATE_DD, codes(decision(st=state(dd_index=84.0), limits=limits)))


# ------------------------------------------------------------------ 结构化输出


class StructuredReasonTests(unittest.TestCase):
    def test_every_block_has_a_stable_code_and_message(self) -> None:
        result = decision(st=state(equity=0.0, trades_today=4))
        self.assertFalse(result.allowed)
        for gate in result.blocked:
            self.assertIn(gate.severity, (risk.SEVERITY_WARN, risk.SEVERITY_BLOCK))
            self.assertTrue(gate.code)
            self.assertTrue(gate.message)
            self.assertIn(gate.code, risk.GATE_KILL + GATE_STATE + GATE_DAILY_LOSS
                          + GATE_DAILY_TRADES + GATE_MAX_POSITIONS + GATE_DUPLICATE_SYMBOL
                          + GATE_SYMBOL_NOTIONAL + GATE_TOTAL_NOTIONAL + GATE_DD
                          + risk.GATE_BAD_NOTIONAL)

    def test_reason_mentions_gate_names(self) -> None:
        result = decision(st=state(equity=9400.0))
        self.assertIn("daily_loss", result.reason)

    def test_as_dict_is_panel_ready_and_serializable(self) -> None:
        payload = json.loads(json.dumps(decision().as_dict()))
        self.assertTrue(payload["allowed"])
        self.assertEqual(payload["blocked"], [])
        self.assertEqual(len(payload["gates"]), 7)
        self.assertEqual(payload["limits"]["max_positions"], 3)
        self.assertEqual(payload["limits"]["dd_kill_pct"], 0.30)

    def test_gate_result_is_self_describing(self) -> None:
        gate = GateResult(GATE_DD, risk.SEVERITY_BLOCK, "drawdown", "31% >= 30%",
                          {"drawdown": 0.31})
        self.assertFalse(gate.ok)
        self.assertTrue(gate.blocked)
        self.assertEqual(gate.as_dict()["code"], "drawdown")
        self.assertIn("drawdown", str(gate))


# ------------------------------------------------------------------ 配置校验


class LimitsValidationTests(unittest.TestCase):
    def test_invalid_values_fail_loud(self) -> None:
        bad = [
            {"risk_pct": 0.0}, {"risk_pct": 1.5}, {"risk_pct": float("nan")},
            {"max_daily_loss_pct": 0.0}, {"max_daily_loss_pct": 1.5},
            {"max_trades_per_day": -1}, {"max_trades_per_day": 2.5},
            {"max_positions": -1}, {"max_positions": 1.5},
            {"max_symbol_notional_mult": 0.0}, {"max_total_notional_mult": -1.0},
            {"max_symbol_notional_mult": 3.0, "max_total_notional_mult": 2.0},
            {"dd_warn_pct": 0.4, "dd_kill_pct": 0.3},
            {"dd_warn_pct": 0.99, "dd_kill_pct": 0.0},
        ]
        for kwargs in bad:
            with self.assertRaises(RiskConfigError, msg=kwargs):
                RiskLimits(**kwargs)

    def test_from_env_reads_documented_keys(self) -> None:
        limits = RiskLimits.from_env({
            "RISK_PCT": "0.02",
            "MAX_DAILY_LOSS_PCT": "0.03",
            "MAX_TRADES_PER_DAY": "2",
            "MAX_POSITIONS": "2",
            "MAX_SYMBOL_NOTIONAL_MULT": "1.2",
            "MAX_TOTAL_NOTIONAL_MULT": "2.0",
            "DD_WARN_PCT": "0.3",
            "DD_KILL_PCT": "0.4",
            "KILL_SWITCH": "true",
        })
        self.assertEqual(limits.risk_pct, 0.02)
        self.assertEqual(limits.max_daily_loss_pct, 0.03)
        self.assertEqual(limits.max_trades_per_day, 2)
        self.assertEqual(limits.max_positions, 2)
        self.assertEqual(limits.max_symbol_notional_mult, 1.2)
        self.assertEqual(limits.max_total_notional_mult, 2.0)
        self.assertTrue(limits.kill)
        self.assertEqual((limits.dd_warn, limits.dd_kill), (0.30, 0.40))

    def test_from_env_defaults_and_bad_values(self) -> None:
        limits = RiskLimits.from_env({})
        self.assertEqual(limits.max_positions, 3)
        self.assertEqual(limits.risk_pct, 0.0133)
        self.assertFalse(limits.kill)
        for env in ({"MAX_TRADES_PER_DAY": "abc"}, {"KILL_SWITCH": "maybe"},
                    {"RISK_PCT": "13.3"}, {"MAX_POSITIONS": "-2"}):
            with self.assertRaises(RiskConfigError, msg=env):
                RiskLimits.from_env(env)

    def test_from_config_uses_config_risk_pct(self) -> None:
        class FakeConfig:
            risk_pct = 0.0133

        limits = RiskLimits.from_config(FakeConfig())
        self.assertEqual(limits.risk_pct, 0.0133)
        limits = RiskLimits.from_config(FakeConfig(), max_positions=2)
        self.assertEqual(limits.max_positions, 2)

        class NoRisk:
            pass

        with self.assertRaises(RiskConfigError):
            RiskLimits.from_config(NoRisk())

    def test_thresholds_property_matches_helper(self) -> None:
        limits = RiskLimits(risk_pct=0.02)
        self.assertEqual((limits.dd_warn, limits.dd_kill), (0.30, 0.40))
        self.assertEqual(limits.as_dict()["dd_kill_pct"], 0.40)
        self.assertTrue(limits.as_dict()["dd_thresholds_auto"])


if __name__ == "__main__":
    unittest.main()
