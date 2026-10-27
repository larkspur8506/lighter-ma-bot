"""`app/main.py` 的契约测试（T10 应用层）—— **全离线**。

钉住的东西：

1. `/health` **永远** 200，且**不碰 DB / 网络 / 凭据**（T1 的契约不许被 T10 破坏）。
2. 7 个只读端点 + `/` 全部 200，结构正确；DB 打不开时仍是 200 + 0 值 + WARNING。
3. 4h 收盘后 tick 的定时口径：`:03` / `:33`（UTC），`next_tick_at` 写死数值断言。
4. **调度器不会因为一次 tick 失败就死掉**：连续失败计数递增、心跳照写、
   `tick_failed` 落库、之后成功一轮能恢复正常。
5. **skip 事件落库**（`record_tick_outcomes`）—— 面板才看得见「为什么没开单」。
6. `MarketBarSource` 的 TTL 缓存（一轮 3 个标的别把限流烧在刷页面上）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from app import main, views, watchdog
from app.risk import RiskLimits
from app.store import StateStore

T0 = 1_760_000_000.0
BAR_S = 4 * 3600.0
BAR_MS = 4 * 3600 * 1000
LIMITS = RiskLimits(risk_pct=0.0133)


def utc(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


class FakeClock:
    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class StoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.store = StateStore(os.path.join(self.tmp, "state.db"))
        self.addCleanup(self.store.close)

    def seed(self) -> None:
        self.store.record_equity(1000.0, ts=T0, twr_index=1.0, cum_flow=0.0, source="dd")
        self.store.record_equity(1030.0, ts=T0 + BAR_S, twr_index=1.03, cum_flow=0.0,
                                 source="dd")
        self.store.save_dd_state(twr_index=1.03, twr_peak=1.03, anchor_ts=T0,
                                 anchor_equity=1000.0, anchor_cum_flow=0.0, ts=T0 + BAR_S)
        self.store.record_entry("BTC", 1, qty_int=100_000, size_decimals=5,
                                entry_price_int=650_000, price_decimals=2,
                                stop_price_int=630_000, bar_ms=int(T0 * 1000),
                                market_id=1, stop_order_id="stop-1")
        self.store.log_event("skip", "BTC skip: 未过密集门槛", symbol="BTC", ts=T0 + BAR_S)

    def client(self, **kwargs: Any) -> TestClient:
        panel = views.Panel(lambda: self.store, limits=LIMITS,
                            now=lambda: T0 + BAR_S + 60,
                            sources=views.Sources(prices=lambda: {"BTC": 6600.0},
                                                  orders=lambda: [{"symbol": "BTC",
                                                                   "price": 6300.0,
                                                                   "qty": 1.0,
                                                                   "order_id": "stop-1",
                                                                   "reduce_only": True}]))
        app = main.create_app(panel=panel, admin_token="tok", limits=LIMITS,
                              clock=lambda: T0 + BAR_S + 60, **kwargs)
        return TestClient(app)


# --------------------------------------------------------------------------- 1. health


class HealthTests(unittest.TestCase):
    def test_health_is_200_without_any_configuration(self) -> None:
        """模块级 `app`（`uvicorn app.main:app` 用的那个）在**没有**任何 env 时也要活。"""
        from app.main import app as fastapi_app

        response = TestClient(fastapi_app).get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["ok"], True)
        self.assertEqual(body["service"], "lighter-ma-bot")
        self.assertEqual(body["version"], main.VERSION)

    def test_health_does_not_touch_the_database(self) -> None:
        calls = {"n": 0}

        def boom() -> Any:
            calls["n"] += 1
            raise RuntimeError("database is locked")

        app = main.create_app(store_factory=boom, admin_token="tok", limits=LIMITS)
        response = TestClient(app).get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls["n"], 0, "/health 不许碰 DB（T1 契约）")


# --------------------------------------------------------------------------- 2. 只读端点


class ReadEndpointTests(StoreFixture):
    READ_PATHS = ("/health", "/status", "/positions", "/stops", "/pnl", "/symbols",
                  "/events")

    def test_all_read_endpoints_return_200(self) -> None:
        self.seed()
        client = self.client()
        for path in self.READ_PATHS:
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)

    def test_status_payload(self) -> None:
        self.seed()
        body = self.client().get("/status").json()
        self.assertEqual(body["equity"], 1030.0)
        self.assertEqual(body["open_count"], 1)
        self.assertEqual(body["kill"], False)
        self.assertEqual(body["dd"]["action"], "ok")
        self.assertIn("data_source", body)
        self.assertIn("last_tick_text", body)

    def test_positions_payload(self) -> None:
        self.seed()
        body = self.client().get("/positions").json()
        self.assertEqual([row["symbol"] for row in body["positions"]],
                         ["BTC", "ETH", "SOL"])
        self.assertEqual(body["positions"][0]["price"], 6600.0)
        self.assertEqual(body["open_count"], 1)

    def test_stops_payload(self) -> None:
        self.seed()
        body = self.client().get("/stops").json()
        self.assertIs(body["orders_known"], True)
        self.assertIs(body["all_protected"], True)

    def test_pnl_payload(self) -> None:
        self.seed()
        body = self.client().get("/pnl").json()
        self.assertEqual(body["cum_pnl"], 30.0)
        self.assertAlmostEqual(body["twr_pct"], 103.0, places=9)
        self.assertIs(body["consistency"]["match"], True)
        self.assertEqual(body["units"], {"cum_pnl": "U", "twr_pct": "%"})

    def test_symbols_payload(self) -> None:
        self.seed()
        body = self.client().get("/symbols").json()
        self.assertEqual([row["symbol"] for row in body["symbols"]],
                         ["BTC", "ETH", "SOL"])
        self.assertTrue(body["degraded"])                 # 测试里没有 K 线源

    def test_events_payload_includes_skips(self) -> None:
        self.seed()
        body = self.client().get("/events").json()
        self.assertGreaterEqual(body["count"], 1)
        self.assertIn("跳过", [item["action"] for item in body["events"]])

    def test_html_page_has_the_six_blocks(self) -> None:
        self.seed()
        response = self.client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        for name in views.BLOCKS:
            with self.subTest(block=name):
                self.assertIn(f'id="block-{name}"', response.text)

    def test_declared_routes_are_exactly_the_intended_surface(self) -> None:
        app = main.create_app(admin_token="tok", limits=LIMITS)
        paths = {route.path for route in app.routes}
        expected = {"/health", "/status", "/positions", "/stops", "/pnl", "/symbols",
                    "/events", "/", "/admin/kill", "/admin/flatten", "/admin/dd/reset",
                    "/admin/state"}
        self.assertTrue(expected.issubset(paths), f"missing routes: {expected - paths}")
        # 没有默认的 /docs /redoc /openapi.json：面板是唯一对外表面
        for extra in ("/docs", "/redoc", "/openapi.json"):
            self.assertNotIn(extra, paths)


class ReadEndpointDegradedTests(unittest.TestCase):
    def test_broken_store_still_returns_200_with_zeros(self) -> None:
        def boom() -> Any:
            raise RuntimeError("database is locked")

        app = main.create_app(store_factory=boom, admin_token="tok", limits=LIMITS)
        client = TestClient(app)
        for path in ("/status", "/positions", "/stops", "/pnl", "/symbols", "/events"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, f"{path} must not 500")
                body = response.json()
                self.assertTrue(body["warnings"], f"{path} must carry a WARNING")
                self.assertIs(body["degraded"], True)

        status = client.get("/status").json()
        self.assertEqual(status["equity"], 0.0)
        self.assertEqual(status["level"], "alert")
        pnl = client.get("/pnl").json()
        self.assertEqual(pnl["cum_pnl"], 0.0)
        self.assertEqual(pnl["twr_pct"], 100.0)

    def test_degraded_page_still_renders(self) -> None:
        def boom() -> Any:
            raise RuntimeError("database is locked")

        app = main.create_app(store_factory=boom, limits=LIMITS)
        response = TestClient(app).get("/")
        self.assertEqual(response.status_code, 200)
        for name in views.BLOCKS:
            self.assertIn(f'id="block-{name}"', response.text)


# --------------------------------------------------------------------------- 3. 定时口径


class NextTickTests(unittest.TestCase):
    def test_fires_at_03_and_33(self) -> None:
        cases = [
            (utc(2026, 1, 1, 0, 0, 0), utc(2026, 1, 1, 0, 3, 0)),
            (utc(2026, 1, 1, 0, 2, 59), utc(2026, 1, 1, 0, 3, 0)),
            (utc(2026, 1, 1, 0, 3, 0), utc(2026, 1, 1, 0, 33, 0)),
            (utc(2026, 1, 1, 0, 5, 0), utc(2026, 1, 1, 0, 33, 0)),
            (utc(2026, 1, 1, 0, 33, 0), utc(2026, 1, 1, 1, 3, 0)),
            (utc(2026, 1, 1, 0, 59, 59), utc(2026, 1, 1, 1, 3, 0)),
            (utc(2026, 1, 1, 23, 40, 0), utc(2026, 1, 2, 0, 3, 0)),
        ]
        for now, expected in cases:
            with self.subTest(now=datetime.fromtimestamp(now, timezone.utc).isoformat()):
                self.assertEqual(main.next_tick_at(now), expected)

    def test_always_strictly_in_the_future(self) -> None:
        now = utc(2026, 1, 1, 12, 0, 0)
        first = main.next_tick_at(now)
        self.assertGreater(first, now)
        self.assertGreater(main.next_tick_at(first), first)

    def test_tick_minutes_are_the_documented_ones(self) -> None:
        self.assertEqual(main.TICK_MINUTES, (3, 33))


# --------------------------------------------------------------------------- 4. 调度器


class SchedulerTests(StoreFixture):
    def test_a_failing_tick_is_recorded_and_does_not_kill_the_scheduler(self) -> None:
        clock = FakeClock(utc(2026, 1, 1, 0, 3, 0))
        calls = {"n": 0}

        def runner() -> str:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("exchange returned 502")
            return "ok"

        scheduler = main.TickScheduler(runner, store=self.store, clock=clock)

        async def run() -> List[Dict[str, Any]]:
            return [await scheduler.tick() for _ in range(3)]

        reports = asyncio.run(run())          # 不许抛
        self.assertEqual([report["status"] for report in reports],
                         ["error", "error", "ok"])
        self.assertEqual([report["failures"] for report in reports], [1, 2, 2])
        self.assertEqual(scheduler.runs, 3)
        self.assertEqual(scheduler.failures, 2)
        self.assertEqual(scheduler.last_status, "ok")

        # 每次失败都落 tick_failed；每轮开始/结束都写心跳
        self.assertEqual(len(self.store.events(kind="tick_failed", limit=10)), 2)
        beats = self.store.events(kind=watchdog.HEARTBEAT_KIND, limit=20)
        self.assertEqual(len(beats), 6)
        self.assertEqual(scheduler.last_error, "")

    def test_heartbeat_carries_the_status(self) -> None:
        clock = FakeClock(utc(2026, 1, 1, 0, 3, 0))
        scheduler = main.TickScheduler(lambda: "done", store=self.store, clock=clock)
        asyncio.run(scheduler.tick())
        beats = self.store.events(kind=watchdog.HEARTBEAT_KIND, limit=10)
        self.assertEqual(len(beats), 2)
        import json as _json

        payloads = [_json.loads(row["data"]) for row in beats]
        self.assertEqual([p["phase"] for p in payloads], ["end", "start"])   # DESC
        self.assertEqual(payloads[0]["status"], "ok")
        stamp, source = watchdog.default_heartbeat_probe(self.store)
        self.assertEqual(stamp, clock.t)
        self.assertEqual(source, watchdog.HEARTBEAT_KIND)

    def test_scheduler_survives_a_store_that_cannot_be_opened(self) -> None:
        def boom() -> Any:
            raise RuntimeError("no db")

        scheduler = main.TickScheduler(lambda: "ok", store_factory=boom)
        report = asyncio.run(scheduler.tick())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(scheduler.runs, 1)

    def test_run_forever_ticks_then_stops_cleanly(self) -> None:
        clock = FakeClock(utc(2026, 1, 1, 0, 0, 0))
        calls = {"runs": 0}

        def runner() -> str:
            calls["runs"] += 1
            return "ok"

        state: Dict[str, Any] = {"sleeps": 0}
        scheduler = main.TickScheduler(runner, store=self.store, clock=clock)

        async def fake_sleep(seconds: float) -> None:
            state["sleeps"] += 1
            if state["sleeps"] == 1:
                clock.advance(200)                 # 越过 00:03 → 应当跑一轮
            else:
                scheduler._stopping = True

        scheduler._sleep = fake_sleep

        async def run() -> None:
            await scheduler.run_forever()

        asyncio.run(run())
        self.assertEqual(calls["runs"], 1)
        self.assertEqual(scheduler.runs, 1)
        self.assertFalse(scheduler.started)

    def test_stop_is_idempotent(self) -> None:
        scheduler = main.TickScheduler(lambda: "ok", store=self.store)

        async def run() -> None:
            await scheduler.stop()
            await scheduler.stop()

        asyncio.run(run())
        self.assertFalse(scheduler.started)


# --------------------------------------------------------------------------- 5. skip 事件


class TickOutcomeTests(StoreFixture):
    def test_record_tick_outcomes_writes_skips_to_events(self) -> None:
        from app.executor import SymbolOutcome, TickReport

        report = TickReport(
            started_at=T0, finished_at=T0 + 1, status="ok", equity=1000.0,
            outcomes=(
                SymbolOutcome(symbol="BTC", action="skip",
                              reason="均线不密集：带宽 0.42% 未压到门槛 0.31%（现 2 根）"),
                SymbolOutcome(symbol="ETH", action="skip",
                              reason="均线不密集：未过密集门槛"),
                SymbolOutcome(symbol="SOL", action="entry", reason="做多信号成立"),
            ))
        ids = main.record_tick_outcomes(self.store, report, ts=T0 + 2)
        self.assertEqual(len(ids), 3)

        rows = self.store.events(limit=10)
        kinds = [row["kind"] for row in rows]
        self.assertEqual(kinds.count("skip"), 2)
        self.assertEqual(kinds.count("entry"), 1)

        panel = views.Panel(lambda: self.store, now=lambda: T0 + 3)
        block = panel.events()
        self.assertEqual(block["skipped_count"], 2)
        self.assertTrue(all("不密集" in item["message"]
                            for item in block["events"] if item["skipped"]))

    def test_record_tick_outcomes_tolerates_a_missing_store(self) -> None:
        from app.executor import TickReport

        self.assertEqual(main.record_tick_outcomes(None, TickReport()), [])

    def test_an_event_write_failure_does_not_raise(self) -> None:
        class Broken:
            def log_event(self, *args: Any, **kwargs: Any) -> int:
                raise RuntimeError("readonly database")

        from app.executor import SymbolOutcome, TickReport

        report = TickReport(outcomes=(SymbolOutcome(symbol="BTC", action="skip"),))
        self.assertEqual(main.record_tick_outcomes(Broken(), report), [])


# --------------------------------------------------------------------------- 6. 行情缓存


class MarketBarSourceTests(unittest.TestCase):
    def test_calls_are_cached_for_the_ttl(self) -> None:
        class FakeMarket:
            def __init__(self) -> None:
                self.calls: List[str] = []

            def bar_list(self, symbol: str) -> List[str]:
                self.calls.append(symbol)
                return [f"{symbol}-bar"]

        market = FakeMarket()
        clock = FakeClock(1000.0)
        source = main.MarketBarSource(market, ttl_s=120.0, clock=clock)
        self.assertEqual(source("btc"), ["BTC-bar"])
        self.assertEqual(source("BTC"), ["BTC-bar"])
        self.assertEqual(market.calls, ["BTC"])              # 第二次命中缓存
        clock.advance(121.0)
        source("BTC")
        self.assertEqual(market.calls, ["BTC", "BTC"])       # TTL 过期后重取

    def test_errors_are_not_cached(self) -> None:
        class FlakyMarket:
            def __init__(self) -> None:
                self.calls = 0

            def bar_list(self, symbol: str) -> List[str]:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("502")
                return ["ok"]

        market = FlakyMarket()
        source = main.MarketBarSource(market, clock=FakeClock(0.0))
        with self.assertRaises(RuntimeError):
            source("BTC")
        self.assertEqual(source("BTC"), ["ok"])              # 失败不写缓存，下一次能好


if __name__ == "__main__":
    unittest.main()
