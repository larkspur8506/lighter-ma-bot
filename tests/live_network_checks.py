"""真网络测试（**不在 CI 里跑**，手动执行）。

    LIGHTER_LIVE_TEST=1 python3 -m unittest tests.live_network_checks -v

为什么文件名不是 `test*.py`：CI 的 `unittest discover -s tests -t .` 用默认模式
`test*.py` 收集，而 CI 对 `skipped=[1-9]` 是**硬失败**（怕 skips 掩盖缺失依赖）。
真网络用例必须靠 `@skipUnless` 挡住，那样在 CI 里就一定会被 skip 掉、把构建搞红。
所以这组用例单独放一个不匹配默认模式的模块，由人显式点名运行 —— 既不会污染 CI，
也不会被误当成「已经自动覆盖」。

覆盖的是离线假数据覆盖不到的两件事：

1. 真实 `/candles` 分页真的能翻到历史（本地 1251 笔信号的验证前提）；
2. 真实 `/orderBookOrders` 的盘口结构与 `orderBookDetails` 的交易规则字段确实是我们
   假设的那个形状 —— 如果 Lighter 改了字段名，离线测试永远发现不了。
"""

from __future__ import annotations

import os
import time
import unittest

from app.market import (
    BAR_MS, EARLIEST_MS, FALLBACK_MARKET_IDS, FRESH_MAX_AGE_MS, MarketClient,
    detect_gaps,
)

LIVE = os.environ.get("LIGHTER_LIVE_TEST") == "1"
SYMBOLS = ("BTC", "ETH", "SOL")


def live_client(**kwargs) -> MarketClient:
    # 60 req/min 是真的限流；不要为了跑得快把它调高。
    return MarketClient(os.environ.get("LIGHTER_LIVE_ENV", "mainnet"), **kwargs)


@unittest.skipUnless(LIVE, "set LIGHTER_LIVE_TEST=1 to run the real-network checks")
class LiveCandlesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = live_client(per_minute=60)
        cls.now = int(time.time() * 1000)
        cls.results = {}

    def test_candles_are_ordered_unique_and_closed(self):
        for symbol in SYMBOLS:
            with self.subTest(symbol=symbol):
                result = self.client.bars(symbol, since_ms=self.now - 600 * BAR_MS,
                                          now_ms=self.now)
                self.results[symbol] = result
                stamps = [b.open_ms for b in result.bars]
                self.assertEqual(stamps, sorted(stamps), "bars must ascend")
                self.assertEqual(len(stamps), len(set(stamps)), "bars must be unique")
                self.assertGreaterEqual(len(stamps), 376,
                                        "strategy needs >= 376 bars for one signal window")
                self.assertLessEqual(result.last_close_age_ms, FRESH_MAX_AGE_MS)
                for bar in result.bars:
                    self.assertLessEqual(bar.open_ms + BAR_MS, result.now_ms)
                    self.assertLessEqual(bar.low, bar.high)
                    self.assertLessEqual(bar.low, bar.close)
                    self.assertLessEqual(bar.low, bar.open)
                    self.assertGreaterEqual(bar.high, bar.close)
                    self.assertGreaterEqual(bar.high, bar.open)
                self.assertEqual(len(result.dropped_unclosed), 1,
                                 "exactly the forming bar should be dropped")
                print(f"\n{symbol}: {len(result.bars)} closed bars, "
                      f"{result.pages} page(s), gaps={len(result.gaps)}, "
                      f"dropped={[b.open_ms for b in result.dropped_unclosed]}, "
                      f"age={result.last_close_age_ms / 3_600_000:.2f}h")

    def test_no_gaps_in_the_last_200_bars(self):
        for symbol in SYMBOLS:
            with self.subTest(symbol=symbol):
                result = self.results.get(symbol) or self.client.bars(
                    symbol, since_ms=self.now - 600 * BAR_MS, now_ms=self.now)
                tail = result.bars[-200:]
                self.assertEqual(detect_gaps(tail), [],
                                 f"{symbol}: unexpected 4h gap in the last 200 bars")

    def test_market_ids_match_the_documented_fallback(self):
        details = self.client.market_details()
        for symbol in SYMBOLS:
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, details)
                if os.environ.get("LIGHTER_LIVE_ENV", "mainnet") == "mainnet":
                    self.assertEqual(details[symbol].market_id, FALLBACK_MARKET_IDS[symbol],
                                     "fallback table drifted from the live venue")
                print(f"\n{symbol}: market_id={details[symbol].market_id} "
                      f"price_dec={details[symbol].price_decimals} "
                      f"size_dec={details[symbol].size_decimals} "
                      f"min_base={details[symbol].min_base_amount} "
                      f"min_quote={details[symbol].min_quote_amount} "
                      f"mark={details[symbol].mark_price}")

    def test_full_history_paging_reaches_lighter_start(self):
        """翻到 2025-01-17 需要多页 —— 这条是「1251 笔信号」成立的前提。"""
        result = self.client.bars("BTC", since_ms=EARLIEST_MS, now_ms=self.now)
        # 2025-01-17T08:00Z 到 2026-10-27 约 648 天 = 3888 根 4h bar（含未收盘的那根）。
        self.assertGreater(result.pages, 1)
        self.assertGreaterEqual(len(result.bars), 3880)
        self.assertEqual(result.bars[0].open_ms, EARLIEST_MS,
                         "paging must reach Lighter's very first 4h bar")
        print(f"\nBTC full history: {len(result.bars)} bars from "
              f"{result.bars[0].open_ms} in {result.pages} pages, "
              f"gaps={len(result.gaps)}")

    def test_last_closed_bar_matches_lighter_own_1h_candles(self):
        """最新一根 4h bar 必须等于「同一时间段 4 根 1h bar 的聚合」。

        这是「最新 bar 与官方 UI 一致」能自动化的最强证据：同一交易所、不同分辨率、
        独立请求 —— 如果我们的桶边界或 OHLCV 抄错了，这里立刻对不上。
        """
        result = self.client.bars("BTC", since_ms=self.now - 6 * BAR_MS, now_ms=self.now)
        bar = result.bars[-1]
        # `end_timestamp` 实测是**排他**的：要拿到 07:00 那根 1h bar，end 必须 >= 08:00-1ms。
        end = bar.open_ms + BAR_MS - 1
        # 故意走原始端点：比较两个分辨率是「交易所自身形状」的核对，
        # 不是数据层要提供的能力（数据层只认 4h）。
        # 注意 `/candles` 实测忽略 `count_back`（总是 500 根），所以这里按时间窗口过滤，
        # 不能指望它只返回 4 行。
        payload = self.client._get_json(                      # noqa: SLF001
            f"candles?market_id={result.market_id}&resolution=1h"
            f"&start_timestamp=0&end_timestamp={end}&count_back=10")
        hours = sorted((h for h in payload["c"]
                        if bar.open_ms <= int(h["t"]) < bar.open_ms + BAR_MS),
                       key=lambda h: int(h["t"]))
        self.assertEqual([int(h["t"]) for h in hours],
                         [bar.open_ms + i * 3_600_000 for i in range(4)])
        self.assertAlmostEqual(float(hours[0]["o"]), bar.open, places=6)
        self.assertAlmostEqual(max(float(h["h"]) for h in hours), bar.high, places=6)
        self.assertAlmostEqual(min(float(h["l"]) for h in hours), bar.low, places=6)
        self.assertAlmostEqual(float(hours[-1]["c"]), bar.close, places=6)
        self.assertAlmostEqual(sum(float(h["v"]) for h in hours), bar.volume, places=4)
        print(f"\n4h bar {bar.open_ms}: aggregate of 4x1h matches "
              f"o/h/l/c/v {bar.open}/{bar.high}/{bar.low}/{bar.close}/{bar.volume}")


@unittest.skipUnless(LIVE, "set LIGHTER_LIVE_TEST=1 to run the real-network checks")
class LiveSnapshotTest(unittest.TestCase):
    def test_snapshot_mid_sits_between_bid_and_ask(self):
        client = live_client(per_minute=60)
        for symbol in SYMBOLS:
            with self.subTest(symbol=symbol):
                snap = client.snapshot(symbol, depth=10)
                self.assertIn(snap.price_source, ("mid", "last_close", "last_trade"))
                if snap.best_bid is not None and snap.best_ask is not None:
                    self.assertLessEqual(snap.best_bid, snap.best_ask)
                    self.assertGreaterEqual(snap.price, snap.best_bid)
                    self.assertLessEqual(snap.price, snap.best_ask)
                    spread = snap.spread or 0.0
                    spread_bps = snap.spread_bps or 0.0
                    self.assertGreater(spread, 0)
                    self.assertLess(spread_bps, 100, "spread over 1% is suspicious")
                if snap.mark_price:
                    # 中间价与标记价不该差太远（现货指数与盘口偶有基差）
                    self.assertLess(abs(snap.price - snap.mark_price) / snap.mark_price, 0.05)
                self.assertIsNotNone(snap.info, "market details should be reachable")
                print(f"\n{symbol}: price={snap.price} ({snap.price_source}) "
                      f"bid={snap.best_bid} ask={snap.best_ask} "
                      f"spread_bps={(snap.spread_bps or 0.0):.3f} mark={snap.mark_price} "
                      f"levels={len(snap.bids)}/{len(snap.asks)}")

    def test_order_book_levels_are_sorted_and_aggregated(self):
        client = live_client(per_minute=60)
        book = client.order_book("BTC", depth=10)
        self.assertTrue(book.bids)
        self.assertTrue(book.asks)
        self.assertEqual([l.price for l in book.bids],
                         sorted((l.price for l in book.bids), reverse=True))
        self.assertEqual([l.price for l in book.asks],
                         sorted(l.price for l in book.asks))
        self.assertEqual(len({l.price for l in book.bids}), len(book.bids))
        self.assertLessEqual(len(book.bids), 10)
        for level in list(book.bids) + list(book.asks):
            self.assertGreater(level.price, 0)
            self.assertGreater(level.size, 0)
        print(f"\nbest bid {book.best_bid} / ask {book.best_ask} "
              f"mid {book.mid} spread {book.spread} "
              f"totals {book.total_bids}/{book.total_asks}")


if __name__ == "__main__":
    unittest.main()
