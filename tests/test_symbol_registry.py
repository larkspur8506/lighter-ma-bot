"""`app/symbol_registry.py` 的契约测试 —— 全部离线。

钉住的东西（T4 验收项）：
  * 白名单**只有** BTC / ETH / SOL，顺序与数值逐字段写死（不是自证：数值来自
    2026-10-27 `GET /orderBookDetails` 实测，抄在这里就是回归基线）；
  * 未注册标的**显性拦截**（`SymbolNotConfigured`），不是「跳过」；
  * `market_id` **运行时优先**（`orderBookDetails`），拿不到才回退注册表兜底值，
    且回退**必定**留一条 warning；
  * 兜底值与 `app.market.FALLBACK_MARKET_IDS` 一致（两处常量漂移会被这个测试抓住）。
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional

from app import symbol_registry as registry
from app.market import FALLBACK_MARKET_IDS, FeedError, MarketInfo


# ------------------------------------------------------------------ fakes


class FakeDetailsClient:
    """`MarketClient.market_details()` 的最小替身（只回一份 dict，不打网络）。"""

    def __init__(self, details: Optional[Dict[str, MarketInfo]] = None,
                 error: Optional[BaseException] = None) -> None:
        self.details = details or {}
        self.error = error
        self.calls = 0

    def market_details(self) -> Dict[str, MarketInfo]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.details)


def info(symbol: str, **overrides: Any) -> MarketInfo:
    """用注册表兜底值造一个 MarketInfo，再按需覆盖（模拟运行时返回）。"""
    return registry.spec_strict(symbol).as_market_info(**overrides)


# ------------------------------------------------------------------ tests


class RegistryContentTests(unittest.TestCase):
    """注册表的内容：只有三个标的，数值逐字段写死。"""

    def test_known_symbols_is_exactly_btc_eth_sol(self) -> None:
        self.assertEqual(registry.known_symbols(), ["BTC", "ETH", "SOL"])

    def test_btc_spec_fields(self) -> None:
        spec = registry.spec_strict("BTC")
        self.assertEqual(spec.symbol, "BTC")
        self.assertEqual(spec.fallback_market_id, 1)
        self.assertEqual(spec.price_decimals, 1)
        self.assertEqual(spec.size_decimals, 5)
        self.assertAlmostEqual(spec.min_base_amount, 0.00007, places=12)
        self.assertAlmostEqual(spec.min_quote_amount, 10.0, places=12)
        self.assertEqual(spec.min_initial_margin_fraction, 200)
        self.assertEqual(spec.exchange_max_leverage, 50.0)

    def test_eth_spec_fields(self) -> None:
        spec = registry.spec_strict("ETH")
        self.assertEqual(spec.fallback_market_id, 0)
        self.assertEqual(spec.price_decimals, 2)
        self.assertEqual(spec.size_decimals, 4)
        self.assertAlmostEqual(spec.min_base_amount, 0.002, places=12)
        self.assertAlmostEqual(spec.min_quote_amount, 10.0, places=12)
        self.assertEqual(spec.min_initial_margin_fraction, 200)
        self.assertEqual(spec.exchange_max_leverage, 50.0)

    def test_sol_spec_fields(self) -> None:
        spec = registry.spec_strict("SOL")
        self.assertEqual(spec.fallback_market_id, 2)
        self.assertEqual(spec.price_decimals, 3)
        self.assertEqual(spec.size_decimals, 3)
        self.assertAlmostEqual(spec.min_base_amount, 0.055, places=12)
        self.assertAlmostEqual(spec.min_quote_amount, 10.0, places=12)
        self.assertEqual(spec.min_initial_margin_fraction, 400)
        self.assertEqual(spec.exchange_max_leverage, 25.0)

    def test_max_leverage_helper(self) -> None:
        self.assertEqual(registry.max_leverage("BTC"), 50.0)
        self.assertEqual(registry.max_leverage("eth"), 50.0)
        self.assertEqual(registry.max_leverage("SOL"), 25.0)
        self.assertIsNone(registry.max_leverage("DOGE"))

    def test_missing_margin_fraction_yields_no_leverage_guess(self) -> None:
        spec = registry.SymbolSpec(
            symbol="BTC", fallback_market_id=1, price_decimals=1, size_decimals=5,
            min_base_amount=0.00007, min_quote_amount=10.0,
            min_initial_margin_fraction=0,
        )
        self.assertIsNone(spec.exchange_max_leverage)

    def test_fallback_ids_agree_with_market_module(self) -> None:
        for symbol in registry.known_symbols():
            self.assertEqual(registry.spec_strict(symbol).fallback_market_id,
                             FALLBACK_MARKET_IDS[symbol], symbol)
        self.assertEqual(sorted(FALLBACK_MARKET_IDS), ["BTC", "ETH", "SOL"])

    def test_as_market_info_defaults_and_override(self) -> None:
        built = registry.spec_strict("BTC").as_market_info()
        self.assertEqual(built.market_id, 1)
        self.assertEqual(built.status, "active")
        self.assertFalse(built.is_frozen)
        self.assertEqual(built.size_decimals, 5)
        # 运行时解析到的 id 覆盖兜底值
        self.assertEqual(registry.spec_strict("BTC").as_market_info(market_id=7).market_id, 7)


class ExplicitInterceptionTests(unittest.TestCase):
    """未注册标的 = 配置错误 = 显性拦截。"""

    def test_unregistered_symbol_raises(self) -> None:
        with self.assertRaises(registry.SymbolNotConfigured) as ctx:
            registry.spec_strict("DOGE")
        message = str(ctx.exception)
        self.assertIn("DOGE", message)
        self.assertIn("BTC, ETH, SOL", message)

    def test_unregistered_is_none_for_soft_api(self) -> None:
        self.assertIsNone(registry.spec("DOGE"))
        self.assertFalse(registry.is_registered("DOGE"))
        self.assertFalse(registry.is_registered(""))
        self.assertFalse(registry.is_registered(None))

    def test_registered_lookup_is_case_and_space_insensitive(self) -> None:
        self.assertTrue(registry.is_registered(" btc "))
        self.assertEqual(registry.spec_strict(" sol ").symbol, "SOL")

    def test_registry_holds_exactly_three_entries(self) -> None:
        self.assertEqual(len(registry.REGISTRY), 3)
        self.assertEqual(set(registry.REGISTRY), {"BTC", "ETH", "SOL"})

    def test_with_market_id_does_not_mutate_registry(self) -> None:
        patched = registry.with_market_id("BTC", 9)
        self.assertEqual(patched.fallback_market_id, 9)
        self.assertEqual(registry.spec_strict("BTC").fallback_market_id, 1)


class ResolveMarketInfoTests(unittest.TestCase):
    """`market_id` 运行时优先，兜底必留 warning。"""

    def test_no_client_uses_registry_without_warning(self) -> None:
        resolved = registry.resolve_market_info("BTC")
        self.assertEqual(resolved.source, "registry")
        self.assertEqual(resolved.info.market_id, 1)
        self.assertEqual(resolved.warnings, ())
        self.assertTrue(resolved.used_fallback)   # 非 orderBookDetails 一律算「用了兜底」

    def test_runtime_details_win(self) -> None:
        client = FakeDetailsClient({"BTC": info("BTC", market_id=7, size_decimals=5)})
        resolved = registry.resolve_market_info("BTC", client)
        self.assertEqual(resolved.source, "orderBookDetails")
        self.assertEqual(resolved.info.market_id, 7)
        self.assertEqual(resolved.warnings[0].count("market_id 7"), 1)
        self.assertIn("runtime wins", resolved.warnings[0])
        self.assertEqual(client.calls, 1)

    def test_runtime_without_drift_has_no_warning(self) -> None:
        client = FakeDetailsClient({"BTC": info("BTC")})
        resolved = registry.resolve_market_info("BTC", client)
        self.assertEqual(resolved.source, "orderBookDetails")
        self.assertEqual(resolved.warnings, ())

    def test_feed_error_falls_back_with_warning(self) -> None:
        client = FakeDetailsClient(error=FeedError("orderBookDetails 503"))
        resolved = registry.resolve_market_info("ETH", client)
        self.assertEqual(resolved.source, "registry_fallback")
        self.assertTrue(resolved.used_fallback)
        self.assertEqual(resolved.info.market_id, 0)
        self.assertEqual(len(resolved.warnings), 1)
        self.assertIn("unavailable", resolved.warnings[0])
        self.assertIn("market_id=0", resolved.warnings[0])

    def test_symbol_absent_from_details_falls_back_with_warning(self) -> None:
        client = FakeDetailsClient({"ETH": info("ETH")})
        resolved = registry.resolve_market_info("SOL", client)
        self.assertEqual(resolved.source, "registry_fallback")
        self.assertEqual(resolved.info.market_id, 2)
        self.assertEqual(len(resolved.warnings), 1)
        self.assertIn("not present in orderBookDetails", resolved.warnings[0])

    def test_unexpected_exception_still_falls_back(self) -> None:
        client = FakeDetailsClient(error=RuntimeError("boom"))
        resolved = registry.resolve_market_info("BTC", client)
        self.assertEqual(resolved.source, "registry_fallback")
        self.assertIn("RuntimeError", resolved.warnings[0])

    def test_unregistered_still_raises_with_client(self) -> None:
        client = FakeDetailsClient({"DOGE": MarketInfo(symbol="DOGE", market_id=42,
                                                       price_decimals=4, size_decimals=1)})
        with self.assertRaises(registry.SymbolNotConfigured):
            registry.resolve_market_info("DOGE", client)
        self.assertEqual(client.calls, 0)

    def test_drift_warnings_cover_decimals_and_margin(self) -> None:
        drift = registry.drift_warnings(
            registry.spec_strict("SOL"),
            info("SOL", price_decimals=4, size_decimals=2, min_initial_margin_fraction=500),
        )
        self.assertEqual(len(drift), 3)
        joined = " | ".join(drift)
        self.assertIn("price_decimals 4 != registry 3", joined)
        self.assertIn("size_decimals 2 != registry 3", joined)
        self.assertIn("min_initial_margin_fraction 500 != registry 400", joined)

    def test_as_dict_is_panel_ready(self) -> None:
        resolved = registry.resolve_market_info("SOL")
        payload = resolved.as_dict()
        self.assertEqual(sorted(payload), ["market_id", "source", "symbol", "warnings"])
        self.assertEqual(payload["symbol"], "SOL")
        self.assertEqual(payload["market_id"], 2)

    def test_describe_covers_all_three(self) -> None:
        rows = registry.describe()
        self.assertEqual([r["symbol"] for r in rows], ["BTC", "ETH", "SOL"])
        self.assertEqual([r["exchange_max_leverage"] for r in rows], [50.0, 50.0, 25.0])
        self.assertEqual(registry.describe("ETH")[0]["size_decimals"], 4)


class ImportPurityTests(unittest.TestCase):
    """注册表 import 时不得发起任何网络请求（源码级检查）。"""

    def test_module_does_not_open_sockets(self) -> None:
        source = (registry.__file__ or "")
        with open(source, "r", encoding="utf-8") as handle:
            text = handle.read()
        for forbidden in ("urlopen", "urllib.request", "socket.socket", "requests.get"):
            self.assertNotIn(forbidden, text, f"{forbidden} in symbol_registry source")


if __name__ == "__main__":
    unittest.main()
