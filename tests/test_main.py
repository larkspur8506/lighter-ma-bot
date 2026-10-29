"""`app/main.py` 的契约测试（T10 应用层）—— **全离线**。

钉住的东西：

1. `/health` **永远** 200，且**不碰 DB / 网络 / 凭据**（T1 的契约不许被 T10 破坏）。
2. 7 个只读端点 + `/` 全部 200，结构正确；DB 打不开时仍是 200 + 0 值 + WARNING。
3. 4h 收盘后 tick 的定时口径：`:03` / `:33`（UTC），`next_tick_at` 写死数值断言。
4. **调度器不会因为一次 tick 失败就死掉**：连续失败计数递增、心跳照写、
   `tick_failed` 落库、之后成功一轮能恢复正常。
5. **skip 事件落库**（`record_tick_outcomes`）—— 面板才看得见「为什么没开单」。
6. `MarketBarSource` 的 TTL 缓存（一轮 3 个标的别把限流烧在刷页面上）。
7. **lifespan shutdown 收尾**：面板 / 调度器懒建的 `StateStore` 在应用关闭时被关闭
   （`Panel.close()` / `TickScheduler.close()`）—— 不关就是 `sqlite3.Connection` 泄漏
   （全量测试实测 5 条 `ResourceWarning: unclosed database`，修之后 0 条）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest import mock

from fastapi.testclient import TestClient

from app import config as app_config
from app import executor as executor_mod
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

    def test_homepage_blocks_and_the_records_page(self) -> None:
        """首页只回答「现在怎么样」；订单明细与事件流在 /records（2026-10-28 改版）。"""
        self.seed()
        client = self.client()
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        for name in views.HOMEPAGE_BLOCKS:
            with self.subTest(block=name):
                self.assertIn(f'id="block-{name}"', response.text)
        self.assertNotIn('id="block-events"', response.text)      # 已挪到记录页

        records = client.get("/records")
        self.assertEqual(records.status_code, 200)
        for name in views.RECORDS_BLOCKS:
            with self.subTest(block=name):
                self.assertIn(f'id="block-{name}"', records.text)

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
        client = TestClient(app)
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        for name in views.HOMEPAGE_BLOCKS:
            self.assertIn(f'id="block-{name}"', response.text)
        records = client.get("/records")                          # 记录页同样降级不崩
        self.assertEqual(records.status_code, 200)
        self.assertIn('id="block-records"', records.text)


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


# --------------------------------------------------------------------------- 9. 运行依赖
#
# 缺陷（PR #11 验收发现）：`default_store_factory()` 曾走 `Config.from_env()`，
# 于是「没配 Lighter 凭据」= 「没有数据库」：6 个区块全降级、admin 操作不落审计、
# 状态条还报「数据库不可用」。下面这些用例钉住修好之后的分工：
# **建库只看 DB_URL / DB_PATH；凭据只影响下单**。


@contextlib.contextmanager
def clean_env(**overrides: Any) -> Any:
    """完全受控的环境：先清掉所有凭据 / DB 变量，再按需覆盖（退出时还原）。"""
    base = {key: value for key, value in os.environ.items()
            if not key.startswith("LIGHTER_")}
    for key in ("ADMIN_TOKEN", "DB_URL", "DB_PATH"):
        base.pop(key, None)
    base.update({key: str(value) for key, value in overrides.items()})
    with mock.patch.dict(os.environ, base, clear=True):
        yield


class StorePathTests(unittest.TestCase):
    """store 的建立只依赖 DB 位置 —— 一个凭据都不需要。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def test_only_db_path_is_enough(self) -> None:
        db = os.path.join(self.tmp, "nested", "state.db")      # 目录还不存在
        with clean_env(DB_PATH=db):
            store = main.default_store_factory()
            self.addCleanup(store.close)
            self.assertEqual(os.path.abspath(store.path), os.path.abspath(db))
            self.assertTrue(os.path.exists(db))
            # 反证：同一份 env 下 `Config.from_env()` 一定会抛（缺凭据）——
            # 也就是说「旧实现建不出库」的原因是凭据，而不是数据库。
            with self.assertRaises(app_config.ConfigError):
                app_config.Config.from_env()

    def test_db_url_wins_over_db_path(self) -> None:
        url_db = os.path.join(self.tmp, "from_url.db")
        path_db = os.path.join(self.tmp, "from_path.db")
        with clean_env(DB_URL=f"sqlite:///{url_db}", DB_PATH=path_db):
            store = main.default_store_factory()
            self.addCleanup(store.close)
            self.assertEqual(os.path.abspath(store.path), os.path.abspath(url_db))
        self.assertFalse(os.path.exists(path_db), "DB_URL 优先时不许同时建 DB_PATH")

    def test_non_sqlite_db_url_fails_closed(self) -> None:
        path_db = os.path.join(self.tmp, "state.db")
        with clean_env(DB_URL="postgresql://bot@db:5432/state", DB_PATH=path_db):
            with self.assertRaises(main.StorePathError) as ctx:
                main.default_store_factory()
        self.assertIn("sqlite", str(ctx.exception))
        self.assertFalse(os.path.exists(path_db),
                         "非 sqlite 的 DB_URL 不许悄悄退回 DB_PATH")

    def test_a_missing_db_location_names_the_env_vars(self) -> None:
        with clean_env():
            with self.assertRaises(main.StorePathError) as ctx:
                main.default_store_factory()
        message = str(ctx.exception)
        self.assertIn("DB_URL", message)
        self.assertIn("DB_PATH", message)

    def test_empty_sqlite_url_is_rejected(self) -> None:
        with clean_env(DB_URL="sqlite:///"):
            with self.assertRaises(main.StorePathError):
                main.default_store_factory()

    def test_credential_key_list_mirrors_app_config(self) -> None:
        self.assertEqual(set(main.CREDENTIAL_ENV_KEYS), set(app_config.REQUIRED_KEYS))
        self.assertEqual(main.ADMIN_TOKEN_ENV, app_config.ADMIN_TOKEN_KEY)


class PanelWithoutCredentialsTests(unittest.TestCase):
    """只配 DB_PATH（+ admin token）、一个 Lighter 凭据都没有。

    验收项：store 建成 → admin 操作落 events（含失败/被拒）→ 只读端点吃真 DB 数据
    → 状态条**不**把「缺凭据」说成「数据库不可用」。
    """

    TOKEN = "testtoken123"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.db = os.path.join(self.tmp, "state.db")
        self.seed()

    # ---------------------------------------------------------------- 夹具

    def seed(self) -> None:
        store = StateStore(self.db)
        try:
            store.record_equity(1000.0, ts=T0, twr_index=1.0, cum_flow=0.0, source="dd")
            store.record_equity(1030.0, ts=T0 + BAR_S, twr_index=1.03, cum_flow=0.0,
                                source="dd")
            store.save_dd_state(twr_index=1.03, twr_peak=1.03, anchor_ts=T0,
                                anchor_equity=1000.0, anchor_cum_flow=0.0, ts=T0 + BAR_S)
            store.record_entry("BTC", 1, qty_int=100_000, size_decimals=5,
                               entry_price_int=650_000, price_decimals=2,
                               stop_price_int=630_000, bar_ms=int(T0 * 1000),
                               market_id=1, stop_order_id="stop-1")
            store.log_event("skip", "BTC skip: 未过密集门槛", symbol="BTC", ts=T0 + BAR_S)
            watchdog.record_heartbeat(store, ts=T0 + BAR_S, source="scheduler")
        finally:
            store.close()

    def events(self, kind: str) -> List[Dict[str, Any]]:
        store = StateStore(self.db)
        try:
            rows = store.events(kind=kind, limit=50)
        finally:
            store.close()
        return [{"message": row["message"], "data": json.loads(row["data"] or "{}")}
                for row in rows]

    def client(self, **kwargs: Any) -> TestClient:
        """**不注入 panel**：走 `default_store_factory` + 默认 flatten 的真实装配。

        用 `with self.client() as client:` 使用 —— 那样才会跑 FastAPI 的 lifespan
        shutdown，把面板懒建的 `StateStore` 关掉（`app.main` 的 lifespan 负责收尾）。
        裸 `TestClient(app)` 不进 lifespan，连接就没人关（这正是 `ResourceWarning:
        unclosed database` 的来源）。
        """
        app = main.create_app(limits=LIMITS, clock=lambda: T0 + BAR_S + 60, **kwargs)
        return TestClient(app)

    # ---------------------------------------------------------------- 只读

    def test_read_endpoints_serve_real_rows_from_the_database(self) -> None:
        with clean_env(DB_PATH=self.db, ADMIN_TOKEN=self.TOKEN):
            with self.client() as client:
                status = client.get("/status").json()
                positions = client.get("/positions").json()
                events = client.get("/events").json()

        self.assertEqual(status["equity"], 1030.0)          # 真 DB 里的权益，不是 0
        self.assertEqual(status["open_count"], 1)
        self.assertEqual(status["last_tick_ts"], T0 + BAR_S)  # 真心跳
        self.assertEqual(positions["open_count"], 1)
        self.assertEqual(events["degraded"], False)
        self.assertTrue(any("未过密集门槛" in str(row.get("message"))
                            for row in events["events"]))

    def test_status_blames_the_credentials_not_the_database(self) -> None:
        with clean_env(DB_PATH=self.db, ADMIN_TOKEN=self.TOKEN):
            with self.client() as client:
                status = client.get("/status").json()

        blob = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("数据库不可用", blob, "库明明可用，不许报数据库不可用")
        self.assertEqual(status["credentials"]["known"], True)
        self.assertIs(status["credentials"]["configured"], False)
        self.assertIn("LIGHTER_ACCOUNT_INDEX", status["credentials"]["missing"])
        self.assertTrue(any("凭据" in warning for warning in status["warnings"]))
        self.assertNotEqual(status["level"], "alert", "缺凭据不是「面板瞎了」级别的告警")

    # ---------------------------------------------------------------- 审计

    def test_a_successful_kill_is_audited(self) -> None:
        with clean_env(DB_PATH=self.db, ADMIN_TOKEN=self.TOKEN):
            with self.client() as client:
                response = client.post(f"/admin/kill?k={self.TOKEN}&on=1")
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["ok"], True)
        self.assertIsNotNone(response.json()["audit_event_id"])

        audits = self.events(views.ADMIN_EVENT_KIND)
        self.assertTrue(audits, "admin 写操作必须落 events（审计的全部意义）")
        kill_rows = [row for row in audits if row["data"].get("endpoint") == "kill"]
        self.assertEqual(len(kill_rows), 1)
        self.assertEqual(kill_rows[0]["data"]["result"], "ok")
        # token 绝不进库
        self.assertNotIn(self.TOKEN, json.dumps(audits, ensure_ascii=False))

    def test_a_rejected_kill_is_audited(self) -> None:
        with clean_env(DB_PATH=self.db, ADMIN_TOKEN=self.TOKEN):
            with self.client() as client:
                response = client.post("/admin/kill")     # 没带 token
        self.assertEqual(response.status_code, 401)

        audits = self.events(views.ADMIN_EVENT_KIND)
        rejected = [row for row in audits if row["data"].get("result") == "rejected"]
        self.assertTrue(rejected, "被拒绝的操作同样要留痕（谁试过什么）")
        self.assertEqual(rejected[0]["data"]["endpoint"], "kill")

    def test_a_flatten_that_cannot_reach_the_exchange_is_still_audited(self) -> None:
        """默认 flatten 在缺凭据时失败 —— 但它必须留下审计与原因，而不是静默。"""
        with clean_env(DB_PATH=self.db, ADMIN_TOKEN=self.TOKEN):
            with self.client() as client:
                response = client.post(
                    f"/admin/flatten?k={self.TOKEN}&confirm=yes")
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertIs(body["ok"], False)
        self.assertIn("凭据", body["error"])

        audits = self.events(views.ADMIN_EVENT_KIND)
        failed = [row for row in audits
                  if row["data"].get("endpoint") == "flatten"
                  and row["data"].get("result") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("凭据", failed[0]["data"].get("error", ""))


class NoDatabaseTests(unittest.TestCase):
    """真的没有 DB 位置时才说「数据库不可用」，并且说清要设哪个变量。"""

    def test_status_says_database_unavailable_only_without_a_db_path(self) -> None:
        with clean_env(ADMIN_TOKEN="tok"):
            status = TestClient(main.create_app(limits=LIMITS)).get("/status").json()
        self.assertEqual(status["level"], "alert")
        self.assertIn("数据库不可用", " ".join(status["warnings"]))
        self.assertIn("DB_PATH", " ".join(status["warnings"]))

    def test_write_operations_degrade_without_a_database(self) -> None:
        with clean_env(ADMIN_TOKEN="tok"):
            response = TestClient(main.create_app(limits=LIMITS)).post(
                "/admin/kill?k=tok&on=1")
        self.assertEqual(response.status_code, 200)           # 降级，不是 500
        body = response.json()
        self.assertIs(body["ok"], False)
        self.assertIs(body["degraded"], True)


# --------------------------------------------------------------------------- 10. store 生命周期


class StoreLifecycleTests(unittest.TestCase):
    """`create_app()` 的 lifespan shutdown 必须把懒建的 store 关掉。

    缺陷（PR #11 验收发现）：`Panel` 懒建的 `StateStore` 从来没有 close 路径。面板是
    7×24 常驻进程，`StateStore` 内部是一个长连接 `sqlite3.Connection` —— 于是每建一个
    app 就漏一个连接（fd 泄漏 + `-wal`/`-shm` 一直被持有）。实测：修之前全量测试有
    5 条 `ResourceWarning: unclosed database`，修之后 0 条。

    `TestClient` 必须用 `with` 才有 lifespan；裸 `TestClient(app)` 既不启动也不关闭 ——
    测试里如果没有 shutdown，连接自然没人关。下面两条既测 lifespan，也顺便钉住这一点。
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.made: List[StateStore] = []

    # ---------------------------------------------------------------- 夹具

    def factory(self) -> StateStore:
        store = StateStore(os.path.join(self.tmp, f"db{len(self.made)}.db"))
        self.made.append(store)
        # 兜底：用例失败时不把泄漏带进下一个用例。
        self.addCleanup(lambda: store.close() if not store._closed else None)
        return store

    def assert_closed(self, store: Any) -> None:
        with self.assertRaises(sqlite3.ProgrammingError):
            store.conn.execute("select 1")

    def assert_open(self, store: Any) -> None:
        store.conn.execute("select 1")           # 不抛 = 还活着

    # ---------------------------------------------------------------- 面板

    def test_lifespan_shutdown_closes_the_panel_store(self) -> None:
        app = main.create_app(store_factory=self.factory, admin_token="tok",
                              limits=LIMITS, clock=lambda: T0)
        with TestClient(app) as client:
            self.assertEqual(client.get("/status").status_code, 200)
            self.assertEqual(len(self.made), 1, "第一个请求应该懒建一个连接")
            store = self.made[0]
            self.assert_open(store)              # 服务期间是活的

        self.assert_closed(store)                # shutdown 之后已关
        self.assertTrue(app.state.panel.closed)

    def test_the_panel_does_not_reopen_after_shutdown(self) -> None:
        app = main.create_app(store_factory=self.factory, admin_token="tok", limits=LIMITS)
        with TestClient(app) as client:
            client.get("/status")
        panel = app.state.panel

        self.assertIsNone(panel.store(), "shutdown 之后不许再建连接")
        self.assertEqual(len(self.made), 1)
        self.assertEqual(panel.status()["level"], "alert")     # 降级，不是 500

    def test_shutdown_is_clean_when_the_store_was_never_built(self) -> None:
        app = main.create_app(store_factory=self.factory, limits=LIMITS)
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(self.made, [], "/health 不许碰 DB，退出时也就没有连接要关")

    def test_shutdown_survives_a_store_whose_close_raises(self) -> None:
        class BadClose:
            def close(self) -> None:
                raise RuntimeError("cannot close")

            def events(self, **kwargs: Any) -> List[Any]:
                return []

            def open_positions(self) -> List[Any]:
                return []

            def equity_curve(self, limit: int = 0) -> List[Any]:
                return []

            def dd_state(self) -> Dict[str, Any]:
                return {}

            def last_equity(self) -> None:
                return None

        app = main.create_app(store_factory=BadClose, limits=LIMITS, clock=lambda: T0)
        with TestClient(app) as client:
            self.assertEqual(client.get("/status").status_code, 200)
        # 退出不抛就够：关闭路径把异常降级成 WARNING

    # ---------------------------------------------------------------- 调度器

    def test_scheduler_close_closes_its_own_store(self) -> None:
        scheduler = main.TickScheduler(lambda: "ok", store_factory=self.factory)
        store = scheduler.store()
        self.assertIsNotNone(store)

        scheduler.close()
        self.assert_closed(store)
        scheduler.close()                          # 幂等
        self.assertIsNone(scheduler.store(), "close 之后不许再建连接")

    def test_scheduler_does_not_close_an_injected_store(self) -> None:
        store = self.factory()
        scheduler = main.TickScheduler(lambda: "ok", store=store)
        scheduler.close()
        self.assert_open(store)                    # 借用者不关调用方的库

    def test_stop_releases_the_scheduler_store(self) -> None:
        scheduler = main.TickScheduler(lambda: "ok", store_factory=self.factory)
        store = scheduler.store()

        asyncio.run(scheduler.stop())

        self.assert_closed(store)

    def test_scheduler_close_calls_the_runner_close_hook(self) -> None:
        calls: List[int] = []

        def runner() -> str:
            return "ok"

        def closer() -> None:
            calls.append(1)

        runner.close = closer                      # type: ignore[attr-defined]
        scheduler = main.TickScheduler(runner, store_factory=self.factory)
        scheduler.close()
        scheduler.close()
        self.assertEqual(calls, [1], "runner 的 store 也得收尾，且只收一次")

    def test_lifespan_closes_a_scheduler_built_from_the_factory(self) -> None:
        app = main.create_app(store_factory=self.factory, limits=LIMITS,
                              runner=lambda: "ok", enable_scheduler=True)
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            scheduler = app.state.scheduler_box["scheduler"]
            self.assertIsInstance(scheduler, main.TickScheduler)
            sched_store = scheduler.store()
            self.assertIsNotNone(sched_store)
            self.assertEqual(len(self.made), 1)

        self.assert_closed(sched_store)             # 调度器的连接也在 shutdown 里收尾
        self.assertTrue(app.state.panel.closed)


class ImportBoundaryTests(unittest.TestCase):
    """只读面板的 import 图里**没有**私钥模块（改动后也不许被破坏）。

    用子进程而不是 `sys.modules`：同一个 test run 里别的用例会 import
    `app.exchange`，进程内断言会被污染（假阴性/假阳性都可能）。
    """

    def test_importing_main_does_not_load_exchange_or_the_sdk(self) -> None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = (
            "import sys, app.views, app.main\n"
            "print(sorted(m for m in ('app.exchange', 'app.executor', 'lighter')\n"
            "             if m in sys.modules))\n")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": root,
               "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]",
                         f"面板的 import 图里出现了私钥路径: {result.stdout}")


class BarsCacheTests(unittest.TestCase):
    """面板复用 tick 的 K 线：同一份序列、同一个起点，且不再现场取数。

    实测（2026-10-28）：面板冷拉 3 标的 × 8 页 ≈27s；而 tick 明明已经拿到同一批 K 线。
    更隐蔽的问题是**起点不同会算出略微不同的 EMA**（EMA 用首根收盘播种）→ 面板显示的
    带宽/密集与 bot 的决策口径不完全一致。缓存共享一次解决两个问题。
    """

    def test_cache_returns_fresh_bars_and_drops_stale(self) -> None:
        cache = main.PanelCache()
        now = 1_000_000.0
        cache.set_bars("btc", [1, 2, 3], ts=now)
        self.assertEqual(cache.bars("BTC", clock=lambda: now + 10), [1, 2, 3])
        self.assertIsNone(cache.bars("BTC", clock=lambda: now + 4000))   # 过期
        self.assertIsNone(cache.bars("ETH"))                             # 没采过

    def test_panel_sources_prefer_the_ticks_bars(self) -> None:
        cache = main.PanelCache()
        cached = [object(), object()]
        cache.set_bars("BTC", cached)
        sources = main._default_panel_sources(cache, market_source=True)
        self.assertEqual(sources.bars("BTC"), cached)     # 命中缓存 → 不发任何网络请求

    def test_load_symbol_keeps_the_bars_for_the_panel(self) -> None:
        from types import SimpleNamespace

        from app.strategy import Bar, MIN_BARS_REQUIRED

        bars = [Bar(open_ms=i * 4 * 3600 * 1000, open=100.0, high=100.5, low=99.5,
                    close=100.0, volume=1.0) for i in range(MIN_BARS_REQUIRED + 20)]

        class Market:
            def bars(self, symbol, **kwargs):
                return SimpleNamespace(bars=tuple(bars), dropped_unclosed=())

        fake = SimpleNamespace(market=Market(), _prices={}, _bars={},
                               sides=(1, -1), short_regime_ma=200)
        asyncio.run(executor_mod.Executor._load_symbol(fake, "BTC", 0))
        self.assertEqual(len(fake._bars["BTC"]), len(bars))


class WarmPanelBarsTests(unittest.TestCase):
    """启动预热：把「首屏 25s」挪出用户请求路径。"""

    def test_warm_fills_the_cache(self) -> None:
        from app.strategy import Bar

        cache = main.PanelCache()
        bars = [Bar(open_ms=i * 4 * 3600 * 1000, open=100.0, high=101.0, low=99.0,
                    close=100.0, volume=1.0) for i in range(3)]
        asyncio.run(main._warm_panel_bars(cache, ["BTC", "ETH"], fetch=lambda s: bars))
        self.assertEqual(len(cache.bars("BTC")), 3)
        self.assertEqual(len(cache.bars("ETH")), 3)

    def test_a_failing_symbol_does_not_abort_the_others(self) -> None:
        cache = main.PanelCache()

        def fetch(symbol):
            if symbol == "BTC":
                raise RuntimeError("feed died")
            return [1, 2]

        asyncio.run(main._warm_panel_bars(cache, ["BTC", "ETH"], fetch=fetch))
        self.assertIsNone(cache.bars("BTC"))
        self.assertEqual(cache.bars("ETH"), [1, 2])


class WarmPanelStateTests(unittest.TestCase):
    """启动预热账户读数/现价：把「重启后 30 分钟两条冷启动提示」压到几秒。"""

    def _reader(self, *, equity=101.5, orders=(), error=""):
        class Order:
            def __init__(self, i):
                self.i = i

            def as_dict(self):
                return {"order_id": str(self.i), "reduce_only": True}

        class Reader:
            async def read(self_inner):
                from types import SimpleNamespace
                return SimpleNamespace(equity=equity, orders=tuple(Order(i) for i in orders),
                                       orders_error=error)

        return Reader()

    def test_warm_fills_equity_orders_and_prices(self) -> None:
        cache = main.PanelCache()
        asyncio.run(main._warm_panel_state(cache, ["BTC", "ETH"],
                                           reader=self._reader(orders=(1, 2)),
                                           prices=lambda: {"BTC": 81000.0}))
        self.assertEqual(cache.equity(), 101.5)
        self.assertEqual(len(cache.orders()), 2)             # provider 不再抛「尚未采集」
        self.assertEqual(cache.prices(), {"BTC": 81000.0})

    def test_a_failing_reader_does_not_raise_or_poison_the_cache(self) -> None:
        class Boom:
            async def read(self_inner):
                raise RuntimeError("proxy down")

        cache = main.PanelCache()
        asyncio.run(main._warm_panel_state(cache, ["BTC"], reader=Boom(),
                                           prices=lambda: {"BTC": 1.0}))
        # 账户失败但现价成功 → 现价仍然入缓存，挂单保持「未知」而不是空列表
        self.assertEqual(cache.prices(), {"BTC": 1.0})
        with self.assertRaises(RuntimeError):
            cache.orders()


class LiveGateStatusTests(unittest.TestCase):
    """状态条必须说清「在跑但永远不会下单」——2026-10-28 实测踩到的坑。

    `.env` 里 `LIGHTER_LEVERAGE=5` 与 `LIGHTER_LIVE_ACCOUNT_INDEX=764377` 粘成一行时，
    后者被吞进前者；`LiveGate` 的第二道门永远拒绝下单，而面板/心跳全绿。
    """

    def test_live_flag_without_account_index_is_surfaced(self) -> None:
        status = main.credential_status({"LIGHTER_LIVE_TRADING": "1",
                                         "LIGHTER_ACCOUNT_INDEX": "764377"})
        self.assertIs(status["live_gate"]["ok"], False)
        self.assertIn("LIGHTER_LIVE_ACCOUNT_INDEX", status["detail"])

    def test_live_gate_ok_when_the_account_is_declared(self) -> None:
        status = main.credential_status({"LIGHTER_LIVE_TRADING": "1",
                                         "LIGHTER_LIVE_ACCOUNT_INDEX": "764377"})
        self.assertIs(status["live_gate"]["ok"], True)
        self.assertEqual(status["live_gate"]["declared_account"], 764377)

    def test_dry_mode_never_warns_about_the_gate(self) -> None:
        status = main.credential_status({"LIGHTER_LIVE_TRADING": "0"})
        self.assertIs(status["live_gate"]["ok"], True)
        self.assertIs(status["live_gate"]["enabled"], False)


class MergeAcceptedStopsTests(unittest.TestCase):
    """开仓那一轮要把「交易所已受理」的止损单并进面板缓存。

    否则面板在下一轮 tick（最长 30 分钟）之前看不到这张单，把刚挂好的止损报成
    「没挂」——2026-10-29 线上就撞上过这个窗口（tick 开头读挂单，止损几秒后才下）。
    """

    def _report(self, **stop: Any) -> Any:
        base = {"ok": True, "dry_run": False, "symbol": "SOL", "market_id": 2,
                "client_order_index": 215_019_876_741_086, "trigger_price_int": 105_257,
                "price_int": 106_310, "qty_int": 1_191}
        base.update(stop)
        outcome = executor_mod.SymbolOutcome(symbol="SOL", action="entry", stop_order=base)
        return executor_mod.TickReport(outcomes=(outcome,))

    def test_accepted_stop_is_merged_and_marked_pending(self) -> None:
        cache = main.PanelCache()
        cache.update(orders=[], prices={"SOL": 99.0})
        self.assertEqual(main.merge_accepted_stops(cache, self._report()), 1)
        got = cache.orders()
        self.assertEqual(len(got), 1)
        self.assertIs(got[0]["pending_confirm"], True)
        self.assertEqual(got[0]["client_order_index"], 215_019_876_741_086)
        self.assertEqual(got[0]["trigger_price_int"], 105_257)
        # 幂等：同一张单再并一次不会出现第二行
        self.assertEqual(main.merge_accepted_stops(cache, self._report()), 0)
        self.assertEqual(len(cache.orders()), 1)

    def test_rejected_or_dry_run_stop_is_not_merged(self) -> None:
        cache = main.PanelCache()
        cache.update(orders=[])
        self.assertEqual(main.merge_accepted_stops(cache, self._report(ok=False)), 0)
        self.assertEqual(main.merge_accepted_stops(cache, self._report(dry_run=True)), 0)
        self.assertEqual(cache.orders(), [])

    def test_cold_cache_stays_unknown(self) -> None:
        """从未采集过挂单时不合并 —— 保持「未知」，绝不编一张单出来。"""
        cache = main.PanelCache()
        self.assertEqual(main.merge_accepted_stops(cache, self._report()), 0)
        self.assertIsNone(cache.orders_value)


if __name__ == "__main__":
    unittest.main()