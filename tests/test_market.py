"""`app/market.py` 的契约测试 —— 全部离线，CI 里可跑。

不打真实网络：所有 HTTP 都走注入的 `http_get`，时钟与 sleep 也注入。真网络的那组在
`tests/live_network_checks.py`（文件名故意不匹配 `test*.py`，CI 的 `discover` 不会收集
它 —— CI 对 `skipped=` 是硬失败，真网络用例必须显式手动跑）。

被钉住的四条不变量（T3 验收项）：升序、去重、缺口报告、未收盘 bar 剔除。
另加限流/退避、快照口径、market_id 解析，都是「错了会静默算错指标」的地方。
"""

from __future__ import annotations

import json
import random
import unittest
import urllib.error
import urllib.parse
import warnings
from typing import Any, Dict, List, Mapping, Optional

# CPython 3.14: `urllib.response.addbase` inherits `tempfile._TemporaryFileWrapper`,
# so every `HTTPError` instance emits a ResourceWarning when garbage-collected
# without `close()`. These tests deliberately raise hundreds of them; the warning
# says nothing about the code under test, so silence just that message (and only
# that message) instead of leaving noise attached to unrelated test names.
# It has to be (re)installed in `setUpModule`: `unittest` calls
# `warnings.simplefilter("default")` after importing the module, which would
# otherwise override a module-level filter.
def setUpModule() -> None:
    warnings.filterwarnings("ignore", category=ResourceWarning,
                            message=r"Implicitly cleaning up <HTTPError")

from app.market import (
    BAR_MS, EARLIEST_MS, FALLBACK_MARKET_IDS, FRESH_MAX_AGE_MS, PAGE_LIMIT,
    BarsResult, FeedError, Gap, MarketClient, RateLimiter, StaleFeedError,
    bars_from_rows, check_freshness, detect_gaps, drop_unclosed, fmt_ms, split_unclosed,
)
from app.strategy import Bar

START = EARLIEST_MS                       # synthetic series starts at Lighter's first bar


# ------------------------------------------------------------------ fixtures


def row(open_ms: int, price: float = 100.0, *, close: Optional[float] = None,
        volume: float = 1.0) -> Dict[str, Any]:
    """A Lighter-shaped candle row: {t, o, h, l, c, v, V, i}, `t` in ms."""
    close = price + 1.0 if close is None else close
    return {"t": open_ms, "o": price, "h": max(price, close) + 2.0,
            "l": min(price, close) - 2.0, "c": close, "v": volume, "V": 0.0, "i": 0}


def series(count: int, *, start_ms: int = START, first_price: float = 100.0,
           step: float = 1.0) -> List[Dict[str, Any]]:
    return [row(start_ms + i * BAR_MS, first_price + i * step) for i in range(count)]


def details_row(symbol: str, market_id: int, **overrides: Any) -> Dict[str, Any]:
    base = {
        "symbol": symbol, "market_id": market_id, "market_type": "perp", "status": "active",
        "taker_fee": "0.0000", "maker_fee": "0.0000",
        "min_base_amount": "0.00007", "min_quote_amount": "10.000000",
        "supported_price_decimals": 1, "supported_size_decimals": 5,
        "price_decimals": 1, "size_decimals": 5,
        "min_initial_margin_fraction": 200, "maintenance_margin_fraction": 120,
        "mark_price": "100.5", "index_price": "100.4", "last_trade_price": 100.6,
        "is_frozen": False,
    }
    base.update(overrides)
    return base


def book_payload(bids, asks, *, total_bids=None, total_asks=None) -> Dict[str, Any]:
    def levels(pairs):
        return [{"price": str(p), "remaining_base_amount": str(s),
                 "initial_base_amount": str(s), "order_index": 1} for p, s in pairs]
    return {"code": 200,
            "bids": levels(bids), "asks": levels(asks),
            "total_bids": len(bids) if total_bids is None else total_bids,
            "total_asks": len(asks) if total_asks is None else total_asks}


class FakeClock:
    """Monotonic fake clock: `sleep` advances time instead of blocking."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError(f"negative sleep {seconds}")
        self.sleeps.append(seconds)
        self.now += seconds


class FakeVenue:
    """Simulates Lighter's read endpoints closely enough to exercise paging.

    ``round_end_up`` reproduces the real off-by-one: `end_timestamp` is rounded up
    to the containing 4h bucket, so the previous page's first bar shows up again.
    """

    def __init__(self, *, candles: Optional[Mapping[int, List[dict]]] = None,
                 details: Optional[List[dict]] = None,
                 book: Optional[Dict[str, Any]] = None,
                 round_end_up: bool = False,
                 failures: Optional[List[Optional[BaseException]]] = None,
                 ignore_end: bool = False) -> None:
        self.candles = {k: list(v) for k, v in (candles or {}).items()}
        self.details = details
        self.book = book
        self.round_end_up = round_end_up
        self.failures = list(failures or [])
        self.ignore_end = ignore_end
        self.urls: List[str] = []
        self.queries: List[Dict[str, str]] = []

    # -- callable HTTP client -------------------------------------------------
    def __call__(self, url: str, timeout: float = 30.0) -> Any:
        self.urls.append(url)
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.queries.append(query)
        endpoint = parts.path.rsplit("/", 1)[-1]
        if endpoint == "candles":
            return self._candles(query)
        if endpoint == "orderBookDetails":
            return {"code": 200, "order_book_details": list(self.details or []),
                    "spot_order_book_details": []}
        if endpoint == "orderBookOrders":
            if self.book is None:
                raise AssertionError("test did not configure a book")
            return dict(self.book)
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def _candles(self, query: Mapping[str, str]) -> Dict[str, Any]:
        market = int(query["market_id"])
        rows = self.candles.get(market)
        if rows is None:
            raise AssertionError(f"test did not configure candles for market {market}")
        limit = int(query["count_back"])
        end = int(query["end_timestamp"])
        if self.round_end_up and end % BAR_MS:
            end += BAR_MS - (end % BAR_MS)
        visible = [r for r in rows if self.ignore_end or r["t"] <= end]
        return {"code": 200, "r": "4h", "c": visible[-limit:]}


def client(fake: FakeVenue, *, clock: Optional[FakeClock] = None,
           now_ms: Optional[int] = None, **kwargs: Any) -> MarketClient:
    clock = clock or FakeClock()
    params = dict(http_get=fake, clock=clock, sleep=clock.sleep,
                  now_ms=(lambda: now_ms) if now_ms is not None else None,
                  # 限流不参与这些断言：1/0.0006s 的间隔不会污染 sleep 记录
                  per_minute=100_000, jitter=0.0, rng=random.Random(0),
                  market_ids={"BTC": 1, "ETH": 0, "SOL": 2})
    params.update(kwargs)
    return MarketClient("mainnet", **params)


def http_error(url: str, code: int, headers: Optional[dict] = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, f"HTTP {code}", headers, None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ paging


class PagingTest(unittest.TestCase):
    def test_pages_backwards_until_since(self):
        rows = series(1200)
        fake = FakeVenue(candles={1: rows})
        now = rows[-1]["t"] + BAR_MS
        since = START + 700 * BAR_MS
        result = client(fake, now_ms=now).bars("BTC", since_ms=since, now_ms=now,
                                               page_limit=300)
        self.assertIsInstance(result, BarsResult)
        self.assertEqual(result.pages, 2, [q.get("end_timestamp") for q in fake.queries])
        self.assertEqual(len(result.bars), 500)                  # indices 700..1199
        self.assertEqual(result.bars[0].open_ms, since)
        self.assertEqual(result.bars[-1].open_ms, rows[-1]["t"])
        self.assertEqual(result.market_id, 1)
        self.assertEqual(result.rows_fetched, 600)               # 300 + 300

    def test_end_timestamp_walks_backwards(self):
        rows = series(900)
        fake = FakeVenue(candles={1: rows})
        now = rows[-1]["t"] + BAR_MS
        client(fake, now_ms=now).bars("BTC", since_ms=START + 100 * BAR_MS, now_ms=now,
                                      page_limit=300)
        ends = [int(q["end_timestamp"]) for q in fake.queries]
        self.assertEqual(ends[0], now)
        self.assertEqual(len(ends), 3)
        # 每次都严格回退到「上一页最早那根 - 1」
        self.assertEqual(ends[1], START + 600 * BAR_MS - 1)
        self.assertEqual(ends[2], START + 300 * BAR_MS - 1)

    def test_count_back_never_exceeds_server_cap(self):
        fake = FakeVenue(candles={1: series(10)})
        now = START + 10 * BAR_MS
        client(fake, now_ms=now).bars("BTC", now_ms=now)
        self.assertEqual(int(fake.queries[0]["count_back"]), PAGE_LIMIT)
        self.assertEqual(fake.queries[0]["resolution"], "4h")
        self.assertEqual(fake.queries[0]["start_timestamp"], "0")

    def test_boundary_duplicates_are_removed(self):
        """`end_timestamp` 向上取整会让上一页首根重复出现 —— 必须去重。"""
        rows = series(40)
        fake = FakeVenue(candles={1: rows}, round_end_up=True)
        now = rows[-1]["t"] + BAR_MS
        result = client(fake, now_ms=now).bars("BTC", now_ms=now, page_limit=10)
        stamps = [b.open_ms for b in result.bars]
        self.assertEqual(len(stamps), 40, f"pages={result.pages}")
        self.assertEqual(len(set(stamps)), 40)
        self.assertGreater(result.pages, 1)                      # 确实翻了页
        self.assertEqual(stamps, sorted(stamps))

    def test_rows_are_ascending_and_deduped(self):
        bars = bars_from_rows([row(3 * BAR_MS), row(BAR_MS), row(2 * BAR_MS),
                               row(BAR_MS, price=999.0)])
        self.assertEqual([b.open_ms for b in bars], [BAR_MS, 2 * BAR_MS, 3 * BAR_MS])
        self.assertEqual(bars[0].open, 999.0)                    # 后取到的那份覆盖前者

    def test_short_page_stops_paging(self):
        rows = series(37)
        fake = FakeVenue(candles={1: rows})
        now = rows[-1]["t"] + BAR_MS
        result = client(fake, now_ms=now).bars("BTC", since_ms=START, now_ms=now,
                                               page_limit=100)
        self.assertEqual(result.pages, 1)
        self.assertEqual(len(result.bars), 37)

    def test_empty_page_stops_paging(self):
        fake = FakeVenue(candles={1: []})
        now = START + 10 * BAR_MS
        with self.assertRaises(FeedError):
            client(fake, now_ms=now).bars("BTC", now_ms=now)

    def test_page_budget_exhausted_raises_instead_of_truncating(self):
        rows = series(100)
        fake = FakeVenue(candles={1: rows})
        now = rows[-1]["t"] + BAR_MS
        with self.assertRaises(FeedError) as caught:
            client(fake, now_ms=now).bars("BTC", since_ms=START, now_ms=now,
                                          page_limit=10, max_pages=3)
        self.assertIn("paged 3 times", str(caught.exception))


# ------------------------------------------------------------------ integrity


class BarsIntegrityTest(unittest.TestCase):
    def test_gap_is_reported_and_not_filled(self):
        rows = series(20)
        del rows[10:12]                                   # 挖掉两根（8h 的洞）
        fake = FakeVenue(candles={1: rows})
        now = rows[-1]["t"] + BAR_MS
        result = client(fake, now_ms=now).bars("BTC", now_ms=now)
        self.assertEqual(len(result.gaps), 1, result.gaps)
        gap = result.gaps[0]
        self.assertEqual(gap.missing, 2)
        self.assertEqual(gap.after_ms, rows[9]["t"])
        self.assertEqual(gap.before_ms, rows[10]["t"])    # 洞里剩下的那根
        self.assertEqual(gap.span_ms, 3 * BAR_MS)
        self.assertEqual(len(result.bars), 18)            # 没有补假 bar
        self.assertEqual(len(detect_gaps(result.bars)), 1)

    def test_gap_dataclass_reads_as_utc(self):
        gap = Gap(after_ms=START, before_ms=START + 3 * BAR_MS, missing=2)
        self.assertIn("缺 2 根", str(gap))
        self.assertIn(fmt_ms(START), str(gap))

    def test_unclosed_bar_is_dropped_and_named(self):
        rows = series(10)
        now = rows[-1]["t"] + 60_000                      # 最后一根刚开盘 1 分钟
        fake = FakeVenue(candles={1: rows})
        result = client(fake, now_ms=now).bars("BTC", now_ms=now)
        self.assertEqual(len(result.dropped_unclosed), 1)
        self.assertEqual(result.dropped_unclosed[0].open_ms, rows[-1]["t"])
        self.assertEqual(result.bars[-1].open_ms, rows[-2]["t"])
        self.assertNotIn(rows[-1]["t"], [b.open_ms for b in result.bars])
        self.assertEqual(result.summary()["dropped_unclosed"], [fmt_ms(rows[-1]["t"])])
        self.assertEqual(result.gaps, ())                 # 剔除不算缺口

    def test_bar_closing_exactly_now_counts_as_closed(self):
        rows = series(5)
        now = rows[-1]["t"] + BAR_MS
        result = client(FakeVenue(candles={1: rows}), now_ms=now).bars("BTC", now_ms=now)
        self.assertEqual(result.dropped_unclosed, ())
        self.assertEqual(len(result.bars), 5)

    def test_stale_feed_raises(self):
        rows = series(10)
        now = rows[-1]["t"] + BAR_MS + FRESH_MAX_AGE_MS + 1000      # 9h+
        with self.assertRaises(StaleFeedError) as caught:
            client(FakeVenue(candles={1: rows}), now_ms=now).bars("BTC", now_ms=now)
        self.assertIn("stale", str(caught.exception))

    def test_freshness_at_eight_hours_is_accepted(self):
        rows = series(10)
        now = rows[-1]["t"] + BAR_MS + FRESH_MAX_AGE_MS
        result = client(FakeVenue(candles={1: rows}), now_ms=now).bars("BTC", now_ms=now)
        self.assertEqual(result.last_close_age_ms, FRESH_MAX_AGE_MS)
        self.assertTrue(result.is_fresh)

    def test_strict_freshness_can_be_switched_off_for_backfills(self):
        rows = series(10)
        now = rows[-1]["t"] + BAR_MS + FRESH_MAX_AGE_MS + 1000
        result = client(FakeVenue(candles={1: rows}), now_ms=now).bars(
            "BTC", now_ms=now, strict_freshness=False)
        self.assertGreater(result.last_close_age_ms, FRESH_MAX_AGE_MS)
        self.assertFalse(result.is_fresh)

    def test_no_price_yet_raises(self):
        """最新一根还没收盘、且没有更早的已收盘 bar —— fail-closed，不返回空序列。"""
        single = series(1)
        now = single[0]["t"] + 1000
        with self.assertRaises(FeedError):
            client(FakeVenue(candles={1: single}), now_ms=now).bars("BTC", now_ms=now)

    def test_malformed_row_raises(self):
        bad = row(START)
        del bad["c"]
        with self.assertRaises(FeedError):
            bars_from_rows([bad])

    def test_high_below_low_raises(self):
        broken = row(START)
        broken["h"], broken["l"] = 1.0, 5.0
        with self.assertRaises(FeedError):
            bars_from_rows([broken])

    def test_bars_are_the_strategy_bar(self):
        rows = series(5)
        now = rows[-1]["t"] + BAR_MS
        result = client(FakeVenue(candles={1: rows}), now_ms=now).bars("BTC", now_ms=now)
        self.assertIs(type(result.bars[0]), Bar)
        self.assertEqual(result.bars[0].volume, 1.0)

    def test_bar_list_returns_plain_list(self):
        rows = series(5)
        now = rows[-1]["t"] + BAR_MS
        bars = client(FakeVenue(candles={1: rows}), now_ms=now).bar_list("BTC", now_ms=now)
        self.assertIsInstance(bars, list)
        self.assertEqual(len(bars), 5)

    def test_split_and_drop_helpers(self):
        bars = bars_from_rows(series(3))
        closed, unclosed = split_unclosed(bars, bars[-1].open_ms + 1)
        self.assertEqual(len(closed), 2)
        self.assertEqual(len(unclosed), 1)
        self.assertEqual(drop_unclosed(bars, bars[-1].open_ms + 1), closed)
        self.assertEqual(check_freshness(closed, closed[-1].open_ms + BAR_MS), 0)


# --------------------------------------------------------------- rate & retry


class RateLimiterTest(unittest.TestCase):
    def test_min_interval_is_enforced(self):
        clock = FakeClock()
        limiter = RateLimiter(60, clock=clock, sleep=clock.sleep)
        for _ in range(4):
            limiter.acquire()
        self.assertEqual(clock.sleeps, [1.0, 1.0, 1.0])       # 60/min -> 1s 间隔
        self.assertAlmostEqual(limiter.waited_s, 3.0)

    def test_sliding_window_caps_bursts(self):
        clock = FakeClock()
        limiter = RateLimiter(2, clock=clock, sleep=clock.sleep)
        for _ in range(4):
            limiter.acquire()
        # 2 个/60s：t=0, 30, 60, 90
        self.assertEqual([round(s, 6) for s in clock.sleeps], [30.0, 30.0, 30.0])
        self.assertAlmostEqual(clock.now - 1000.0, 90.0)

    def test_requests_through_a_client_are_spaced(self):
        rows = series(900)
        clock = FakeClock()
        now = rows[-1]["t"] + BAR_MS
        fake = FakeVenue(candles={1: rows})
        client(fake, clock=clock, now_ms=now, per_minute=60).bars(
            "BTC", since_ms=START + 100 * BAR_MS, now_ms=now, page_limit=300)
        self.assertEqual(len(fake.urls), 3)
        self.assertEqual([round(s, 6) for s in clock.sleeps], [1.0, 1.0])

    def test_bad_limits_are_rejected(self):
        with self.assertRaises(ValueError):
            RateLimiter(0)
        with self.assertRaises(ValueError):
            RateLimiter(60, window=0)


class RetryTest(unittest.TestCase):
    def _calls(self, client_, n: int = 1) -> None:
        for _ in range(n):
            client_._get_json("orderBookDetails")

    def test_429_is_retried_with_exponential_backoff(self):
        clock = FakeClock()
        fake = FakeVenue(details=[details_row("BTC", 1)],
                         failures=[http_error("u", 429), http_error("u", 429), None])
        client_ = client(fake, clock=clock, backoff_base=1.0, jitter=0.0)
        self._calls(client_)
        self.assertEqual(len(fake.urls), 3)
        self.assertEqual([round(s, 6) for s in clock.sleeps], [1.0, 2.0])
        self.assertEqual(client_.requests, 3)

    def test_jitter_only_ever_lengthens_the_wait(self):
        clock = FakeClock()
        base = 1.0
        fake = FakeVenue(details=[details_row("BTC", 1)],
                         failures=[http_error("u", 429), None])
        client_ = client(fake, clock=clock, backoff_base=base, jitter=0.5,
                         rng=random.Random(7))
        self._calls(client_)
        self.assertGreaterEqual(clock.sleeps[0], base)
        self.assertLessEqual(clock.sleeps[0], base * 1.5)

    def test_retry_after_header_is_honoured(self):
        clock = FakeClock()
        fake = FakeVenue(details=[details_row("BTC", 1)],
                         failures=[http_error("u", 429, {"Retry-After": "7"}), None])
        client_ = client(fake, clock=clock, backoff_base=1.0, jitter=0.0)
        self._calls(client_)
        self.assertEqual(clock.sleeps[0], 7.0)

    def test_retries_are_capped_then_feed_error(self):
        clock = FakeClock()
        fake = FakeVenue(failures=[http_error("u", 503)] * 10)
        client_ = client(fake, clock=clock, max_retries=2, jitter=0.0)
        with self.assertRaises(FeedError) as caught:
            self._calls(client_)
        self.assertIn("after 3 attempts", str(caught.exception))
        self.assertEqual(len(fake.urls), 3)

    def test_4xx_is_not_retried(self):
        clock = FakeClock()
        fake = FakeVenue(failures=[http_error("u", 404)])
        client_ = client(fake, clock=clock, jitter=0.0)
        with self.assertRaises(FeedError):
            self._calls(client_)
        self.assertEqual(len(fake.urls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_bad_json_is_retried_then_succeeds(self):
        clock = FakeClock()

        class Flaky:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, url, timeout=30.0):
                self.calls += 1
                if self.calls == 1:
                    raise json.JSONDecodeError("boom", "{", 1)
                return {"code": 200, "order_book_details": [details_row("BTC", 1)]}

        flaky = Flaky()
        client_ = client(flaky, clock=clock, jitter=0.0)  # type: ignore[arg-type]
        self._calls(client_)
        self.assertEqual(flaky.calls, 2)

    def test_api_level_error_code_is_fatal(self):
        class Bad:
            def __call__(self, url, timeout=30.0):
                return {"code": 400, "error": "bad market"}

        with self.assertRaises(FeedError) as caught:
            self._calls(client(Bad()))                    # type: ignore[arg-type]
        self.assertIn("API code 400", str(caught.exception))


# ------------------------------------------------------------------ metadata


class MarketInfoTest(unittest.TestCase):
    def test_decimals_and_limits_are_parsed(self):
        fake = FakeVenue(details=[details_row("BTC", 1)])
        info = client(fake).market_info("BTC")
        self.assertEqual(info.market_id, 1)
        self.assertEqual(info.price_decimals, 1)
        self.assertEqual(info.size_decimals, 5)
        self.assertAlmostEqual(info.min_base_amount, 0.00007)
        self.assertAlmostEqual(info.min_quote_amount, 10.0)
        self.assertEqual(info.min_initial_margin_fraction, 200)
        self.assertAlmostEqual(info.mark_price, 100.5)
        self.assertFalse(info.is_frozen)

    def test_only_active_perps_are_kept(self):
        fake = FakeVenue(details=[
            details_row("BTC", 1),
            details_row("DEAD", 9, status="inactive"),
            details_row("SPOT", 10, market_type="spot"),
        ])
        details = client(fake).market_details()
        self.assertEqual(sorted(details), ["BTC"])

    def test_details_are_cached(self):
        fake = FakeVenue(details=[details_row("BTC", 1)])
        client_ = client(fake)
        client_.market_details()
        client_.market_details()
        self.assertEqual(len(fake.urls), 1)
        client_.market_details(force=True)
        self.assertEqual(len(fake.urls), 2)

    def test_override_wins_over_details(self):
        fake = FakeVenue(details=[details_row("BTC", 1)])
        client_ = client(fake, market_ids={"BTC": 77})
        self.assertEqual(client_.market_id_of("BTC"), 77)
        self.assertEqual(fake.urls, [])                  # 连元信息都不用问

    def test_details_win_over_the_fallback_table(self):
        fake = FakeVenue(details=[details_row("BTC", 4242)])
        client_ = client(fake, market_ids={})
        self.assertEqual(client_.market_id_of("BTC"), 4242)
        self.assertNotEqual(client_.market_id_of("BTC"), FALLBACK_MARKET_IDS["BTC"])

    def test_fallback_table_is_used_only_when_details_are_unreachable(self):
        clock = FakeClock()
        fake = FakeVenue(details=[details_row("BTC", 1)],
                         failures=[http_error("u", 500)] * 10)
        client_ = client(fake, clock=clock, market_ids={}, max_retries=1)
        self.assertEqual(client_.market_id_of("BTC"), FALLBACK_MARKET_IDS["BTC"])
        self.assertTrue(client_.warnings)
        self.assertIn("orderBookDetails unavailable", client_.warnings[0])

    def test_unknown_symbol_raises(self):
        fake = FakeVenue(details=[details_row("BTC", 1)])
        with self.assertRaises(FeedError):
            client(fake, market_ids={}).market_id_of("DOGE")

    def test_market_info_is_strict_about_missing_details(self):
        fake = FakeVenue(details=[details_row("BTC", 1)])
        with self.assertRaises(FeedError):
            client(fake, market_ids={"SOL": 2}).market_info("SOL")


# ------------------------------------------------------------------ snapshot


class SnapshotTest(unittest.TestCase):
    def _venue(self, book, *, details=None):
        return FakeVenue(details=details if details is not None else [details_row("BTC", 1)],
                         book=book)

    def test_price_is_the_book_mid(self):
        fake = self._venue(book_payload(bids=[(100.0, 1.0)], asks=[(100.2, 2.0)]))
        snap = client(fake).snapshot("BTC")
        self.assertEqual(snap.price_source, "mid")
        self.assertAlmostEqual(snap.price, 100.1)
        self.assertAlmostEqual(snap.best_bid, 100.0)
        self.assertAlmostEqual(snap.best_ask, 100.2)
        self.assertAlmostEqual(snap.spread, 0.2)
        self.assertAlmostEqual(snap.spread_bps, 0.2 / 100.1 * 10_000)
        book_queries = [q for q in fake.queries if "limit" in q]
        self.assertEqual(len(book_queries), 1)
        self.assertEqual(book_queries[0]["limit"], "10")
        self.assertEqual(book_queries[0]["market_id"], "1")

    def test_levels_are_aggregated_by_price_and_ordered(self):
        book = book_payload(
            bids=[(100.0, 1.0), (100.0, 0.5), (99.9, 2.0)],
            asks=[(100.3, 1.0), (100.2, 0.25), (100.2, 0.75)],
        )
        snap = client(self._venue(book)).snapshot("BTC", depth=5)
        self.assertEqual([l.price for l in snap.bids], [100.0, 99.9])       # 降序
        self.assertEqual([l.price for l in snap.asks], [100.2, 100.3])      # 升序
        self.assertAlmostEqual(snap.bids[0].size, 1.5)
        self.assertAlmostEqual(snap.asks[0].size, 1.0)

    def test_zero_size_and_non_positive_price_are_ignored(self):
        book = book_payload(bids=[(100.0, 0.0), (0.0, 1.0), (99.0, 2.0)],
                            asks=[(101.0, 1.0)])
        snap = client(self._venue(book)).snapshot("BTC")
        self.assertEqual([l.price for l in snap.bids], [99.0])
        self.assertAlmostEqual(snap.mid, 100.0)

    def test_empty_book_falls_back_to_last_closed_close(self):
        rows = series(5)
        now = rows[-1]["t"] + BAR_MS / 2                 # 最后一根未收盘
        fake = FakeVenue(details=[details_row("BTC", 1)],
                         book=book_payload(bids=[], asks=[]),
                         candles={1: rows})
        snap = client(fake, now_ms=now).snapshot("BTC")
        self.assertEqual(snap.price_source, "last_close")
        self.assertAlmostEqual(snap.price, rows[-2]["c"])  # 已收盘那根的收盘价
        self.assertIsNone(snap.mid)

    def test_empty_book_and_no_bars_falls_back_to_last_trade(self):
        fake = FakeVenue(details=[details_row("BTC", 1, last_trade_price=123.0)],
                         book=book_payload(bids=[], asks=[]), candles={1: []})
        snap = client(fake).snapshot("BTC")
        self.assertEqual(snap.price_source, "last_trade")
        self.assertAlmostEqual(snap.price, 123.0)

    def test_no_price_at_all_raises(self):
        fake = FakeVenue(details=[details_row("BTC", 1, last_trade_price=None)],
                         book=book_payload(bids=[], asks=[]), candles={1: []})
        with self.assertRaises(FeedError):
            client(fake).snapshot("BTC")

    def test_snapshot_still_works_when_details_are_down(self):
        fake = FakeVenue(details=[], book=book_payload(bids=[(100.0, 1.0)],
                                                       asks=[(100.1, 1.0)]))
        client_ = client(fake, market_ids={"BTC": 1})
        snap = client_.snapshot("BTC")
        self.assertEqual(snap.price_source, "mid")
        self.assertIsNone(snap.mark_price)
        self.assertTrue(any("details unavailable" in w for w in client_.warnings))


if __name__ == "__main__":
    unittest.main()
