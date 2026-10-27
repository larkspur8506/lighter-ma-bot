"""`app/sizing.py` 的契约测试 —— **全离线**（不打网络，`MarketInfo` 直接构造）。

钉住的东西（T4 验收项，逐条对应 `docs/PLAN.md` T4）：
  1. 风险定仓口径：`名义 = 权益 × risk_pct ÷ 止损距离`；
  2. 精度**向下取整**（floor），且精度不足时宁可少买 —— 含「算出来不足一个 tick →
     截断到 0 → 拒单」这条边界；
  3. 低于 `min_base_amount` / `min_quote_amount` → 拒单（不产生非法单）；
  4. 权益不足（名义 ÷ 杠杆 > 权益）→ 拒单；
  5. 未注册标的 → 拒单（`symbol_not_registered`）；
  6. 杠杆上限 = `min(用户配置, 10000/mif)`；
  7. `sizing.py` 不 import 任何网络模块（AST 级检查）。

所有期望值都是**手算后写死**的数字（不是「拿实现算一遍再和实现比」）。
"""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import replace
from typing import Any, Dict

from app import sizing, symbol_registry
from app.market import MarketInfo


# ------------------------------------------------------------------ fixtures


def market(symbol: str, **overrides: Any) -> MarketInfo:
    """注册表兜底值 + 覆盖（模拟运行时 `orderBookDetails` 返回的市场规则）。"""
    info = symbol_registry.spec_strict(symbol).as_market_info()
    return replace(info, **overrides) if overrides else info


BTC = market("BTC")      # price_dec=1 size_dec=5 min_base=0.00007 min_quote=10 mif=200
ETH = market("ETH")      # price_dec=2 size_dec=4 min_base=0.0020  min_quote=10 mif=200
SOL = market("SOL")      # price_dec=3 size_dec=3 min_base=0.055   min_quote=10 mif=400


def req(symbol: str, side: int = 1, entry: float = 80000.0, stop: float = 76800.0,
        equity: float = 10000.0) -> sizing.SizingRequest:
    return sizing.SizingRequest(symbol=symbol, side=side, entry_price=entry,
                                stop_price=stop, equity=equity)


# ------------------------------------------------------------------ 公式


class TargetNotionalTests(unittest.TestCase):
    def test_legacy_formula(self) -> None:
        # 10,000 × 1.33% ÷ 4% = 133 ÷ 0.04 = 3,325
        self.assertEqual(sizing.target_notional(10000.0, 0.0133, 0.04), 3325.0)
        # 5,000 × 2% ÷ 5% = 100 ÷ 0.05 = 2,000
        self.assertEqual(sizing.target_notional(5000.0, 0.02, 0.05), 2000.0)
        # 1,000 × 1% ÷ 10% = 10 ÷ 0.10 = 100
        self.assertEqual(sizing.target_notional(1000.0, 0.01, 0.10), 100.0)

    def test_rejects_degenerate_inputs(self) -> None:
        for args in ((0.0, 0.01, 0.04), (1000.0, 0.0, 0.04), (1000.0, 0.01, 0.0),
                     (-1.0, 0.01, 0.04), (float("nan"), 0.01, 0.04)):
            with self.assertRaises(ValueError, msg=args):
                sizing.target_notional(*args)


class PrecisionTests(unittest.TestCase):
    """整数化 + floor：数量只往下走。"""

    def test_to_int_qty_btc(self) -> None:
        # 3325 / 80000 = 0.0415625 → ×1e5 = 4156.25 → floor 4156
        self.assertEqual(sizing.to_int_qty(3325.0, 80000.0, 5), 4156)
        self.assertEqual(sizing.to_int_base_qty(0.0415625, 5), 4156)
        self.assertEqual(sizing.from_int_qty(4156, 5), 0.04156)

    def test_to_int_base_qty_truncates_to_zero(self) -> None:
        # 0.000012 在 4 位精度下 = 0.12 个 tick → **0**（这是「必须截断到 0 且拒单」的边界）
        self.assertEqual(sizing.to_int_base_qty(0.000012, 4), 0)
        # 同样的数量在 5 位精度下是 1.2 个 tick → 1（继续走 min_base 检查）
        self.assertEqual(sizing.to_int_base_qty(0.000012, 5), 1)
        self.assertEqual(sizing.to_int_base_qty(0.0000199, 5), 1)
        self.assertEqual(sizing.to_int_base_qty(0.00001, 5), 1)
        self.assertEqual(sizing.to_int_base_qty(0.000009, 5), 0)

    def test_to_int_base_qty_is_not_off_by_one_for_round_numbers(self) -> None:
        # float 残差（0.00029 × 1e5 = 28.999999999999996）不能吃掉一个 tick
        self.assertEqual(sizing.to_int_base_qty(0.00029, 5), 29)
        self.assertEqual(sizing.to_int_base_qty(0.04156, 5), 4156)
        self.assertEqual(sizing.to_int_base_qty(3.325, 3), 3325)

    def test_to_int_price_floor_and_ceil(self) -> None:
        self.assertEqual(sizing.to_int_price(79830.04, 1), 798300)
        self.assertEqual(sizing.to_int_price(79830.09, 1), 798300)
        self.assertEqual(sizing.to_int_price(79830.01, 1, rounding="ceil"), 798301)
        self.assertEqual(sizing.to_int_price(2313.567, 2), 231356)
        self.assertEqual(sizing.to_int_price(2313.561, 2, rounding="ceil"), 231357)
        self.assertEqual(sizing.to_int_price(100.0, 3), 100000)

    def test_align_stop_rounds_toward_entry(self) -> None:
        # 多单止损在下：ceil → 76800.05 → 76800.1（止损更近 → 风险更小）
        price, price_int = sizing.align_stop_price(80000.0, 76800.05, 1, 1)
        self.assertAlmostEqual(price, 76800.1, places=9)
        self.assertEqual(price_int, 768001)
        # 空单止损在上：floor → 83200.04 → 83200.0（同样更近）
        price, price_int = sizing.align_stop_price(80000.0, 83200.04, -1, 1)
        self.assertAlmostEqual(price, 83200.0, places=9)
        self.assertEqual(price_int, 832000)

    def test_align_stop_rejects_bad_side_and_sub_tick(self) -> None:
        with self.assertRaises(ValueError):
            sizing.align_stop_price(100.0, 90.0, 0, 2)
        # 空单止损 floor 到 0 个 tick（0.001 @2dec = 0.1 tick）→ 无法表示
        with self.assertRaises(ValueError):
            sizing.align_stop_price(100.0, 0.001, -1, 2)

    def test_round_trip_check(self) -> None:
        # floor 掉了 0.0000025 BTC ≈ 0.2 USDC，容差 = max(0.8, 0.3325) → 通过
        self.assertTrue(sizing.round_trip_check(3325.0, 0.04156, 80000.0, 5))
        # 少 0.001 BTC = 80 USDC，远超容差 → 不通过
        self.assertFalse(sizing.round_trip_check(3325.0, 0.04056, 80000.0, 5))
        # 实际名义高于目标 → 一律不通过
        self.assertFalse(sizing.round_trip_check(1000.0, 0.02, 80000.0, 5))


# ------------------------------------------------------------------ 正常定仓


class PlanOrderHappyPathTests(unittest.TestCase):
    def test_btc_worked_example(self) -> None:
        """完整算例（docs/SIZING_RISK.md 用的就是这一个）。

        权益 10,000 / risk_pct 1.33% / entry 80,000 / stop 76,800（4%）
          风险金额 = 10,000 × 0.0133            = 133.00 USDC
          数量     = 133 / 3,200                = 0.0415625 BTC → floor@5 → 0.04156
          名义     = 0.04156 × 80,000            = 3,324.80 USDC
          保证金   = 3,324.80 / 5x               = 664.96 USDC
          实际风险 = 0.04156 × 3,200             = 132.992 USDC（比目标少 0.008）
        """
        decision = sizing.plan_order(req("BTC"), BTC)
        self.assertTrue(decision.ok, decision.rejection)
        plan = decision.plan
        assert plan is not None

        self.assertEqual(plan.symbol, "BTC")
        self.assertEqual(plan.market_id, 1)
        self.assertEqual(plan.side, 1)
        self.assertEqual(plan.qty_int, 4156)
        self.assertAlmostEqual(plan.base_qty, 0.04156, places=12)
        self.assertAlmostEqual(plan.notional, 3324.8, places=9)
        self.assertAlmostEqual(plan.margin, 664.96, places=9)
        self.assertEqual(plan.leverage, 5.0)
        self.assertEqual(plan.exchange_max_leverage, 50.0)
        self.assertAlmostEqual(plan.risk_amount_target, 133.0, places=9)
        self.assertAlmostEqual(plan.risk_amount, 132.992, places=9)
        self.assertAlmostEqual(plan.stop_dist, 0.04, places=12)
        self.assertAlmostEqual(plan.risk_pct_actual, 0.0132992, places=12)
        self.assertEqual(plan.stop_price, 76800.0)
        self.assertEqual(plan.stop_price_int, 768000)
        self.assertEqual(plan.warnings, ())

    def test_eth_worked_example(self) -> None:
        """权益 5,000 / risk 2% / entry 2,500 / stop 2,400（4%）→ 1.0 ETH。"""
        decision = sizing.plan_order(
            req("ETH", entry=2500.0, stop=2400.0, equity=5000.0),
            ETH, sizing.SizingConfig(risk_pct=0.02, leverage=5.0),
        )
        self.assertTrue(decision.ok, decision.rejection)
        plan = decision.plan
        assert plan is not None
        self.assertEqual(plan.qty_int, 10000)
        self.assertAlmostEqual(plan.base_qty, 1.0, places=12)
        self.assertAlmostEqual(plan.notional, 2500.0, places=9)
        self.assertAlmostEqual(plan.margin, 500.0, places=9)
        self.assertAlmostEqual(plan.risk_amount, 100.0, places=9)

    def test_sol_worked_example(self) -> None:
        """权益 1,000 / risk 1.33% / entry 100 / stop 96 → 3.325 SOL（3 位精度）。"""
        decision = sizing.plan_order(req("SOL", entry=100.0, stop=96.0, equity=1000.0), SOL)
        self.assertTrue(decision.ok, decision.rejection)
        plan = decision.plan
        assert plan is not None
        self.assertEqual(plan.qty_int, 3325)
        self.assertAlmostEqual(plan.base_qty, 3.325, places=12)
        self.assertAlmostEqual(plan.notional, 332.5, places=9)
        self.assertAlmostEqual(plan.margin, 66.5, places=9)
        self.assertAlmostEqual(plan.risk_amount, 13.3, places=9)

    def test_short_side_is_symmetric(self) -> None:
        """空单同一套公式：entry 80,000 / stop 83,200（+4%）。"""
        decision = sizing.plan_order(req("BTC", side=-1, entry=80000.0, stop=83200.0), BTC)
        self.assertTrue(decision.ok, decision.rejection)
        plan = decision.plan
        assert plan is not None
        self.assertEqual(plan.side, -1)
        self.assertFalse(plan.is_long)
        self.assertEqual(plan.qty_int, 4156)
        self.assertAlmostEqual(plan.notional, 3324.8, places=9)
        self.assertAlmostEqual(plan.risk_amount, 132.992, places=9)
        self.assertEqual(plan.stop_price, 83200.0)

    def test_floor_never_exceeds_target_risk(self) -> None:
        cases = [
            ("BTC", 10000.0, 0.0133, 80000.0, 76800.0),
            ("BTC", 1234.5, 0.0133, 79250.0, 74000.0),
            ("ETH", 5000.0, 0.02, 2313.56, 2225.0),
            ("SOL", 777.0, 0.03, 99.956, 92.0),
        ]
        for symbol, equity, risk_pct, entry, stop in cases:
            info = market(symbol)
            decision = sizing.plan_order(
                req(symbol, entry=entry, stop=stop, equity=equity), info,
                sizing.SizingConfig(risk_pct=risk_pct, leverage=5.0),
            )
            self.assertTrue(decision.ok, (symbol, decision.rejection))
            plan = decision.plan
            assert plan is not None
            self.assertLessEqual(plan.notional, equity * risk_pct / (abs(entry - stop) / entry)
                                 * entry + 1e-9)
            self.assertLessEqual(plan.risk_amount, plan.risk_amount_target + 1e-9)
            self.assertEqual(plan.qty_int, sizing.to_int_base_qty(
                plan.risk_amount_target / abs(entry - stop), info.size_decimals))

    def test_deterministic(self) -> None:
        first = sizing.plan_order(req("BTC"), BTC).as_dict()
        second = sizing.plan_order(req("BTC"), BTC).as_dict()
        self.assertEqual(first, second)

    def test_decision_is_json_serializable_for_panel(self) -> None:
        payload = sizing.plan_order(req("BTC"), BTC).as_dict()
        round_tripped = json.loads(json.dumps(payload))
        self.assertTrue(round_tripped["ok"])
        self.assertEqual(round_tripped["plan"]["qty_int"], 4156)


# ------------------------------------------------------------------ 拒单


class RejectionTests(unittest.TestCase):
    def assertRejected(self, decision: sizing.SizingDecision, code: str) -> sizing.Rejection:
        self.assertFalse(decision.ok, f"expected rejection {code}, got {decision}")
        self.assertIsNone(decision.plan)
        rejection = decision.rejection
        assert rejection is not None
        self.assertEqual(rejection.code, code, rejection.message)
        self.assertTrue(rejection.message)
        return rejection

    def test_unregistered_symbol_is_rejected(self) -> None:
        """未注册标的 → 显性拦截（即使调用方手上有一份看起来正常的 MarketInfo）。"""
        doge = market("BTC").__class__(symbol="DOGE", market_id=42, price_decimals=4,
                                       size_decimals=1, min_base_amount=1.0,
                                       min_quote_amount=10.0, min_initial_margin_fraction=200)
        decision = sizing.plan_order(req("DOGE", entry=0.5, stop=0.48), doge)
        rejection = self.assertRejected(decision, sizing.REJECT_SYMBOL_NOT_REGISTERED)
        self.assertIn("DOGE", rejection.message)
        self.assertEqual(rejection.detail["known"], ["BTC", "ETH", "SOL"])

    def test_qty_truncates_to_zero_is_rejected(self) -> None:
        """0.0000266 ETH 在 4 位精度下 → 0 个 tick → 拒单（绝不下 0 数量的单）。"""
        decision = sizing.plan_order(req("ETH", entry=2500.0, stop=2000.0, equity=1.0), ETH)
        rejection = self.assertRejected(decision, sizing.REJECT_QTY_BELOW_TICK)
        self.assertAlmostEqual(rejection.detail["base_qty_raw"], 0.0000266, places=12)
        self.assertEqual(rejection.detail["size_decimals"], 4)

    def test_one_tick_but_below_min_base_is_rejected(self) -> None:
        """BTC 5 位精度：3.125 tick → floor 3 tick = 0.00003 < min_base 0.00007 → 拒单。"""
        decision = sizing.plan_order(
            req("BTC", entry=80000.0, stop=76800.0, equity=10.0), BTC,
            sizing.SizingConfig(risk_pct=0.01, leverage=5.0),
        )
        rejection = self.assertRejected(decision, sizing.REJECT_BELOW_MIN_BASE)
        self.assertEqual(rejection.detail["qty_int"], 3)
        self.assertAlmostEqual(rejection.detail["base_qty"], 0.00003, places=12)

    def test_below_min_quote_is_rejected(self) -> None:
        """0.00009 BTC × 80,000 = 7.20 USDC < min_quote 10（但 base 已过 0.00007）。"""
        decision = sizing.plan_order(
            req("BTC", entry=80000.0, stop=76800.0, equity=15.0), BTC,
            sizing.SizingConfig(risk_pct=0.02, leverage=5.0),
        )
        rejection = self.assertRejected(decision, sizing.REJECT_BELOW_MIN_QUOTE)
        self.assertAlmostEqual(rejection.detail["notional"], 7.2, places=9)
        self.assertAlmostEqual(rejection.detail["min_quote_amount"], 10.0, places=9)

    def test_insufficient_margin_is_rejected(self) -> None:
        """0.25% 止损 → 名义 5,320，5x 下保证金 1,064 > 权益 1,000 → 拒单。"""
        decision = sizing.plan_order(
            req("BTC", entry=80000.0, stop=79800.0, equity=1000.0), BTC)
        rejection = self.assertRejected(decision, sizing.REJECT_INSUFFICIENT_MARGIN)
        self.assertAlmostEqual(rejection.detail["notional"], 5320.0, places=9)
        self.assertAlmostEqual(rejection.detail["required_margin"], 1064.0, places=9)
        self.assertEqual(rejection.detail["equity"], 1000.0)
        self.assertEqual(rejection.detail["leverage"], 5.0)

    def test_stop_on_wrong_side_is_rejected(self) -> None:
        self.assertRejected(sizing.plan_order(req("BTC", side=1, entry=80000.0, stop=81000.0), BTC),
                            sizing.REJECT_STOP_ON_WRONG_SIDE)
        self.assertRejected(sizing.plan_order(req("BTC", side=-1, entry=80000.0, stop=79000.0), BTC),
                            sizing.REJECT_STOP_ON_WRONG_SIDE)

    def test_bad_input_is_rejected(self) -> None:
        for request in (
            req("BTC", equity=0.0),
            req("BTC", equity=-5.0),
            req("BTC", entry=0.0),
            req("BTC", stop=-1.0),
            req("BTC", side=0),
            req("BTC", equity=float("nan")),
        ):
            decision = sizing.plan_order(request, BTC)
            code = decision.rejection.code if decision.rejection else None
            self.assertEqual(code, sizing.REJECT_BAD_INPUT, (request, code))

    def test_frozen_or_inactive_market_is_rejected(self) -> None:
        frozen = sizing.plan_order(req("BTC"), replace(BTC, is_frozen=True))
        self.assertEqual(frozen.rejection.code, sizing.REJECT_MARKET_NOT_TRADEABLE)
        self.assertIn("frozen", frozen.rejection.message)

        paused = sizing.plan_order(req("BTC"), replace(BTC, status="paused"))
        self.assertEqual(paused.rejection.code, sizing.REJECT_MARKET_NOT_TRADEABLE)
        self.assertIn("paused", paused.rejection.message)

    def test_missing_market_rules_is_rejected_fail_closed(self) -> None:
        no_mif = sizing.plan_order(req("BTC"), replace(BTC, min_initial_margin_fraction=0))
        self.assertEqual(no_mif.rejection.code, sizing.REJECT_MISSING_MARKET_RULES)

        no_decimals = sizing.plan_order(req("BTC"), replace(BTC, size_decimals=None))
        self.assertEqual(no_decimals.rejection.code, sizing.REJECT_MISSING_MARKET_RULES)

        absurd_mif = sizing.plan_order(req("BTC"), replace(BTC, min_initial_margin_fraction=20_000))
        self.assertEqual(absurd_mif.rejection.code, sizing.REJECT_MISSING_MARKET_RULES)

        no_info = sizing.plan_order(req("BTC"), None)
        self.assertEqual(no_info.rejection.code, sizing.REJECT_MISSING_MARKET_RULES)

    def test_stop_that_collapses_onto_entry_is_rejected(self) -> None:
        """止损离入场不足一个 tick：多单 ceil 后贴到入场价 → 拒（不挂错方向的止损）。"""
        decision = sizing.plan_order(
            req("BTC", entry=80000.0, stop=79999.99, equity=100000.0), BTC)
        assert decision.rejection is not None
        self.assertEqual(decision.rejection.code, sizing.REJECT_BAD_INPUT)
        self.assertIn("collapses onto entry", decision.rejection.message)

    def test_stop_that_rounds_past_entry_is_rejected(self) -> None:
        """入场价不在 tick 网格上时，ceil 可能把止损推过入场价 → 拒（先于保证金检查）。"""
        decision = sizing.plan_order(
            req("BTC", entry=80000.04, stop=80000.01, equity=10000.0), BTC)
        assert decision.rejection is not None
        self.assertEqual(decision.rejection.code, sizing.REJECT_BAD_INPUT)
        self.assertIn("wrong side", decision.rejection.message)
        self.assertAlmostEqual(decision.rejection.detail["stop_aligned"], 80000.1, places=9)

    def test_rejection_is_str_and_dict_friendly(self) -> None:
        decision = sizing.plan_order(req("DOGE"), BTC)
        self.assertIn("symbol_not_registered", str(decision))
        payload = json.loads(json.dumps(decision.as_dict()))
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["plan"])
        self.assertEqual(payload["rejection"]["code"], "symbol_not_registered")


# ------------------------------------------------------------------ 杠杆


class LeverageTests(unittest.TestCase):
    def test_configured_leverage_is_used_when_below_exchange_cap(self) -> None:
        plan = sizing.plan_order(req("BTC"), BTC, sizing.SizingConfig(leverage=5.0)).plan
        assert plan is not None
        self.assertEqual(plan.leverage, 5.0)
        self.assertAlmostEqual(plan.margin, 3324.8 / 5.0, places=9)

    def test_configured_leverage_above_exchange_cap_is_clamped_with_warning(self) -> None:
        decision = sizing.plan_order(req("BTC"), BTC, sizing.SizingConfig(leverage=60.0))
        self.assertTrue(decision.ok, decision.rejection)
        plan = decision.plan
        assert plan is not None
        self.assertEqual(plan.leverage, 50.0)                 # 10000/200
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn("clamped", plan.warnings[0])
        self.assertAlmostEqual(plan.margin, 3324.8 / 50.0, places=9)

    def test_sol_cap_is_25x(self) -> None:
        plan = sizing.plan_order(req("SOL", entry=100.0, stop=96.0, equity=1000.0), SOL,
                                 sizing.SizingConfig(leverage=40.0)).plan
        assert plan is not None
        self.assertEqual(plan.exchange_max_leverage, 25.0)
        self.assertEqual(plan.leverage, 25.0)
        self.assertIn("25x", plan.warnings[0])

    def test_lower_leverage_can_make_a_plan_unaffordable(self) -> None:
        """同一笔计算，杠杆从 5x 降到 2x → 保证金超权益 → 拒单（不是静默缩小仓位）。"""
        tight = req("BTC", entry=80000.0, stop=79800.0, equity=1000.0)
        self.assertEqual(sizing.plan_order(tight, BTC).rejection.code,
                         sizing.REJECT_INSUFFICIENT_MARGIN)
        # 20x 才能容下 5,320 的名义（266 保证金 ≤ 1,000）
        plan = sizing.plan_order(tight, BTC, sizing.SizingConfig(leverage=20.0)).plan
        assert plan is not None
        self.assertEqual(plan.leverage, 20.0)
        self.assertAlmostEqual(plan.margin, 266.0, places=9)


# ------------------------------------------------------------------ 配置


class SizingConfigTests(unittest.TestCase):
    def test_defaults_match_legacy_live_parameters(self) -> None:
        cfg = sizing.SizingConfig()
        self.assertEqual(cfg.risk_pct, 0.0133)               # 旧系统实盘 1.33%
        self.assertEqual(cfg.leverage, 5.0)                  # 旧系统逐仓 5x
        self.assertEqual(sizing.DEFAULT_RISK_PCT, 0.0133)
        self.assertEqual(sizing.DEFAULT_LEVERAGE, 5.0)

    def test_invalid_config_fails_loud(self) -> None:
        for kwargs in ({"risk_pct": 0.0}, {"risk_pct": 1.5}, {"risk_pct": 13.3},
                       {"risk_pct": float("nan")}, {"leverage": 0.5}, {"leverage": 101.0},
                       {"leverage": float("inf")}):
            with self.assertRaises(ValueError, msg=kwargs):
                sizing.SizingConfig(**kwargs)

    def test_risk_pct_is_a_fraction_not_a_percent(self) -> None:
        # 13.3 是「13.3% 写成了百分数」的典型错法 → 必须报错，不能当成 13.3%
        with self.assertRaises(ValueError):
            sizing.SizingConfig(risk_pct=13.3)

    def test_from_env(self) -> None:
        cfg = sizing.SizingConfig.from_env({"RISK_PCT": "0.0133", "LIGHTER_LEVERAGE": "3"})
        self.assertEqual(cfg.risk_pct, 0.0133)
        self.assertEqual(cfg.leverage, 3.0)
        self.assertEqual(sizing.SizingConfig.from_env({}),
                         sizing.SizingConfig(risk_pct=0.0133, leverage=5.0))
        with self.assertRaises(ValueError):
            sizing.SizingConfig.from_env({"RISK_PCT": "abc"})
        with self.assertRaises(ValueError):
            sizing.SizingConfig.from_env({"LIGHTER_LEVERAGE": "0"})

    def test_from_config_uses_config_risk_pct(self) -> None:
        class FakeConfig:
            risk_pct = 0.02

        cfg = sizing.SizingConfig.from_config(FakeConfig())
        self.assertEqual(cfg.risk_pct, 0.02)
        self.assertEqual(cfg.leverage, 5.0)

        class NoRisk:
            pass

        with self.assertRaises(ValueError):
            sizing.SizingConfig.from_config(NoRisk())


# ------------------------------------------------------------------ 无网络


class NoNetworkImportTests(unittest.TestCase):
    """验收项：`sizing.py` 不 import 网络模块（AST 级，不靠文本 grep 误伤注释）。"""

    BANNED = {"urllib", "urllib.request", "socket", "http", "http.client", "requests",
              "httpx", "httpx2", "aiohttp", "ssl"}

    def setUp(self) -> None:
        self.source = (sizing.__file__ or "")
        with open(self.source, "r", encoding="utf-8") as handle:
            self.tree = ast.parse(handle.read(), filename=self.source)

    def _module_names(self) -> set:
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    def test_no_network_modules_imported(self) -> None:
        imported = self._module_names()
        self.assertEqual(imported & self.BANNED, set(), f"network imports: {imported}")

    def test_app_market_is_type_checking_only(self) -> None:
        """`app.market` 只能出现在 `if TYPE_CHECKING:` 里（类型引用，不拉网络代码路径）。"""
        runtime_imports: set = set()
        for node in self.tree.body:                      # 只看模块顶层（不含 if 块内部）
            if isinstance(node, ast.ImportFrom) and node.module:
                runtime_imports.add(node.module)
            elif isinstance(node, ast.Import):
                runtime_imports.update(alias.name for alias in node.names)
        self.assertNotIn("app.market", runtime_imports)
        self.assertIn("app.market", self._module_names())  # 但确实作为类型引用了

    def test_market_client_is_never_imported(self) -> None:
        with open(self.source, "r", encoding="utf-8") as handle:
            code = handle.read()
        self.assertNotIn("import MarketClient", code)
        self.assertNotIn("MarketClient(", code)


if __name__ == "__main__":
    unittest.main()
