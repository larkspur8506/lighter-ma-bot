"""`app/watchdog.py` 的契约测试 —— **全离线**（快照 dict + 临时目录里的库，无网络）。

钉住的东西（T9 验收项，逐条对应）：

1. **`check_health()` 是纯函数**：输入一份快照，输出逐项结论；六项检查**各自单独**能触发
   （容器死 / health 不通 / 心跳超期 / bar 停摆 / 推送连败 / DB 不可写），且互不掩盖。
2. **阈值来自常量、不现编**：`bar_state` 用的就是 `app.market` 的新鲜度口径（8h = 2×4h），
   心跳是 6h（1.5 个调度周期），推送是 6h 内 ≥3 条。
3. **缺项不算 ok**：探针没接上（`None`）→ `skipped`，而 `skipped` 会让整体状态变成 `warn`
   —— 「以为在查、其实没查」是监控最典型的失效。
4. **探针抛异常 → 那一项 error/probe_error**，不能退化成 `skipped` 后被当成 ok。
5. **watchdog 自身崩溃不带崩主循环**：`run_once()` 永不抛异常，异常落 `events`（`watchdog_error`）
   + 日志，并且返回的报告 `status='error'`（**不是**静默 ok）。
6. **落 events 有去重**：结论没变就不写第二条 `watchdog_report`（否则 288 条/天）。
7. 默认探针的真实行为：`/proc/1/stat` 解析、DB 真写一次（临时表）、心跳取「事件 > 权益点」。

期望值全部写死。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any, Dict, Optional
from unittest import mock

import app.watchdog as wd
from app.store import StateStore
from app.watchdog import (
    CHECK_BAR_STATE, CHECK_CONTAINER, CHECK_DB_WRITE, CHECK_HEALTH, CHECK_HEARTBEAT,
    CHECK_PUSH, STATUS_ERROR, STATUS_OK, STATUS_SKIPPED, STATUS_WARN, BarStateProbe,
    CheckResult, HealthReport, HealthSnapshot, Watchdog, WatchdogConfig,
    WatchdogConfigError, check_health, default_db_probe, default_heartbeat_probe,
    record_heartbeat, run_once,
)

NOW = 1_760_000_000.0            # 2025-10-09T08:53:20Z
H = 3600.0


# --------------------------------------------------------------------------- 夹具


def snapshot(**overrides: Any) -> HealthSnapshot:
    """一份「六项全绿」的快照，按需覆盖。"""
    base: Dict[str, Any] = dict(
        now=NOW,
        container={"alive": True, "pid1_state": "S"},
        health={"ok": True, "status": 200, "latency_ms": 4.0},
        heartbeat_ts=NOW - 60.0,
        heartbeat_source="heartbeat",
        bar_state={"BTC": {"last_close_ms": (NOW - 3600.0) * 1000.0}},
        push_failures=0,
        db_writable=True,
    )
    base.update(overrides)
    return HealthSnapshot(**base)


def report(**overrides: Any) -> HealthReport:
    return check_health(snapshot(**overrides))


def codes(rep: HealthReport) -> Dict[str, str]:
    return {item.check: (item.code or item.check) for item in rep.checks}


# --------------------------------------------------------------------------- 全绿


class HappyPathTests(unittest.TestCase):
    def test_all_six_checks_pass(self) -> None:
        rep = report()
        self.assertEqual(rep.status, STATUS_OK)
        self.assertTrue(rep.ok)
        self.assertEqual(rep.codes, ())
        self.assertEqual([c.check for c in rep.checks],
                         [CHECK_CONTAINER, CHECK_HEALTH, CHECK_HEARTBEAT, CHECK_BAR_STATE,
                          CHECK_PUSH, CHECK_DB_WRITE])
        self.assertTrue(all(c.status == STATUS_OK for c in rep.checks))
        self.assertFalse(rep.needs_alert)
        self.assertIn("ok", rep.summary())
        self.assertTrue(rep.as_dict()["ok"])

    def test_report_is_json_friendly(self) -> None:
        import json
        payload = report().as_dict()
        json.dumps(payload)                      # 不抛 = T10 面板能直接序列化
        self.assertEqual(payload["status"], STATUS_OK)
        self.assertEqual(len(payload["checks"]), 6)


# --------------------------------------------------------------------------- 各检查项


class ContainerCheckTests(unittest.TestCase):
    def test_dead_container_is_an_error(self) -> None:
        rep = report(container={"alive": False, "pid1_state": "Z"})
        self.assertEqual(rep.status, STATUS_ERROR)
        self.assertEqual(codes(rep)[CHECK_CONTAINER], "container_dead")
        self.assertTrue(rep.check(CHECK_CONTAINER).failed)

    def test_unknown_liveness_is_a_warning(self) -> None:
        rep = report(container={"alive": None, "error": "FileNotFoundError: no /proc"})
        self.assertEqual(rep.status, STATUS_WARN)
        self.assertEqual(codes(rep)[CHECK_CONTAINER], "container_unknown")

    def test_unwired_container_probe_is_skipped_and_never_ok(self) -> None:
        rep = report(container=None)
        self.assertEqual(rep.check(CHECK_CONTAINER).status, STATUS_SKIPPED)
        self.assertEqual(rep.status, STATUS_WARN)       # skipped 不等于 ok
        self.assertEqual(codes(rep)[CHECK_CONTAINER], "container_unwired")

    def test_default_probe_parses_proc_stat(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "stat")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1 (uvicorn) S 0 1 1 0 -1 4194560\n")
        self.assertEqual(wd.default_container_probe(path)["alive"], True)      # noqa: E712
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1 (uvicorn) Z 0 1 1 0 -1 4194560\n")
        self.assertEqual(wd.default_container_probe(path)["alive"], False)     # noqa: E712
        missing = wd.default_container_probe(os.path.join(tmp.name, "nope"))
        self.assertIsNone(missing["alive"])
        self.assertIn("FileNotFoundError", missing["error"])


class HealthEndpointTests(unittest.TestCase):
    def test_unreachable_health_is_an_error(self) -> None:
        rep = report(health={"ok": False, "status": None, "error": "URLError: refused"})
        self.assertEqual(rep.status, STATUS_ERROR)
        self.assertEqual(codes(rep)[CHECK_HEALTH], "health_unreachable")

    def test_non_200_with_ok_body_still_fails(self) -> None:
        rep = report(health={"ok": False, "status": 503})
        self.assertEqual(codes(rep)[CHECK_HEALTH], "health_unreachable")
        self.assertIn("503", rep.check(CHECK_HEALTH).message)

    def test_unwired_health_probe_is_skipped(self) -> None:
        rep = report(health=None)
        self.assertEqual(codes(rep)[CHECK_HEALTH], "health_unwired")


class HeartbeatTests(unittest.TestCase):
    """心跳：4h 调度周期，容忍 6h（1.5 个周期）。"""

    def test_stale_heartbeat_is_an_error(self) -> None:
        rep = report(heartbeat_ts=NOW - 7 * H)
        self.assertEqual(rep.status, STATUS_ERROR)
        self.assertEqual(codes(rep)[CHECK_HEARTBEAT], "heartbeat_stale")
        self.assertIn("7.00h", rep.check(CHECK_HEARTBEAT).message)

    def test_boundary_is_inclusive(self) -> None:
        # 正好 6h → ok（超过才算超期）；6h + 1s → error
        self.assertEqual(report(heartbeat_ts=NOW - 6 * H).check(CHECK_HEARTBEAT).status,
                         STATUS_OK)
        self.assertEqual(report(heartbeat_ts=NOW - 6 * H - 1).check(CHECK_HEARTBEAT).code,
                         "heartbeat_stale")

    def test_missing_heartbeat_is_a_warning(self) -> None:
        rep = report(heartbeat_ts=None, heartbeat_source="missing")
        self.assertEqual(rep.status, STATUS_WARN)
        self.assertEqual(codes(rep)[CHECK_HEARTBEAT], "heartbeat_missing")

    def test_heartbeat_from_the_future_is_flagged(self) -> None:
        rep = report(heartbeat_ts=NOW + 2 * H)
        self.assertEqual(codes(rep)[CHECK_HEARTBEAT], "heartbeat_in_future")

    def test_custom_limit_is_honoured(self) -> None:
        rep = check_health(snapshot(heartbeat_ts=NOW - 3 * H), heartbeat_max_age_s=2 * H)
        self.assertEqual(codes(rep)[CHECK_HEARTBEAT], "heartbeat_stale")


class BarStateTests(unittest.TestCase):
    """bar_state：最后一根**已收盘** bar 的收盘时刻超过 8h（= 2 根 4h）就是停摆。"""

    def test_stalled_feed_is_an_error(self) -> None:
        rep = report(bar_state={"BTC": {"last_close_ms": (NOW - 20 * H) * 1000.0},
                                "ETH": {"last_close_ms": (NOW - 1 * H) * 1000.0},
                                "SOL": {"last_close_ms": (NOW - 2 * H) * 1000.0}})
        self.assertEqual(rep.status, STATUS_ERROR)
        self.assertEqual(codes(rep)[CHECK_BAR_STATE], "bar_stalled")
        self.assertIn("BTC", rep.check(CHECK_BAR_STATE).message)
        self.assertEqual(rep.check(CHECK_BAR_STATE).detail["stalled"], ["BTC"])

    def test_boundary_is_inclusive(self) -> None:
        at_limit = report(bar_state={"BTC": {"last_close_ms": (NOW - 8 * H) * 1000.0}})
        self.assertEqual(at_limit.check(CHECK_BAR_STATE).status, STATUS_OK)
        over = report(bar_state={"BTC": {"last_close_ms": (NOW - 8 * H - 1) * 1000.0}})
        self.assertEqual(over.check(CHECK_BAR_STATE).code, "bar_stalled")

    def test_unreadable_symbol_is_an_error_not_a_skip(self) -> None:
        rep = report(bar_state={"BTC": {"last_close_ms": (NOW - H) * 1000.0},
                                "ETH": {"error": "FeedError: 503"}})
        self.assertEqual(codes(rep)[CHECK_BAR_STATE], "bar_state_unreadable")
        self.assertIn("ETH", rep.check(CHECK_BAR_STATE).message)

    def test_unwired_bar_state_is_skipped(self) -> None:
        rep = report(bar_state=None)
        self.assertEqual(codes(rep)[CHECK_BAR_STATE], "bar_state_unwired")

    def test_probe_computes_the_age_from_a_market_like_object(self) -> None:
        class FakeResult:
            def __init__(self, last_close_ms: int, n: int) -> None:
                self.last_close_ms = last_close_ms
                self.bars = [object()] * n

        class FakeMarket:
            def bars(self, symbol: str, **kwargs: Any) -> Any:
                if symbol == "SOL":
                    raise RuntimeError("boom")
                return FakeResult(int((NOW - 2 * H) * 1000.0), 376)

        probe = BarStateProbe(FakeMarket(), ("BTC", "SOL"))
        out = probe()
        self.assertEqual(out["BTC"]["last_close_ms"], (NOW - 2 * H) * 1000)
        self.assertEqual(out["BTC"]["bars"], 376)
        self.assertIn("boom", out["SOL"]["error"])
        rep = check_health(HealthSnapshot(now=NOW, container={"alive": True},
                                          health={"ok": True}, heartbeat_ts=NOW - 60,
                                          bar_state=out, push_failures=0, db_writable=True))
        self.assertEqual(codes(rep)[CHECK_BAR_STATE], "bar_state_unreadable")


class PushCheckTests(unittest.TestCase):
    """推送：近 6h 失败 1–2 条 warn，≥3 条 error。"""

    def test_single_failure_is_a_warning(self) -> None:
        for failures in (1, 2):
            with self.subTest(failures=failures):
                rep = report(push_failures=failures)
                self.assertEqual(rep.status, STATUS_WARN)
                self.assertEqual(codes(rep)[CHECK_PUSH], "push_failures")

    def test_three_failures_is_an_error(self) -> None:
        rep = report(push_failures=3)
        self.assertEqual(rep.status, STATUS_ERROR)
        self.assertEqual(codes(rep)[CHECK_PUSH], "push_failing")

    def test_unwired_push_probe_is_skipped(self) -> None:
        rep = report(push_failures=None)
        self.assertEqual(codes(rep)[CHECK_PUSH], "push_unwired")

    def test_default_probe_counts_only_the_window(self) -> None:
        store = _store(self)
        store.log_event("alert_failed", "old failure", ts=NOW - 10 * H)
        store.log_event("alert_failed", "fresh failure 1", ts=NOW - 1 * H)
        store.log_event("alert_failed", "fresh failure 2", ts=NOW - 2 * H)
        store.log_event("executor_error", "not a push failure", ts=NOW - 1 * H)
        self.assertEqual(wd.default_push_probe(store, window_s=6 * H, now_ts=NOW), 2)


class DbWriteCheckTests(unittest.TestCase):
    def test_read_only_database_is_an_error(self) -> None:
        rep = report(db_writable=False, db_detail={"error": "OperationalError: readonly"})
        self.assertEqual(rep.status, STATUS_ERROR)
        self.assertEqual(codes(rep)[CHECK_DB_WRITE], "db_not_writable")
        self.assertIn("readonly", rep.check(CHECK_DB_WRITE).message)

    def test_unwired_db_probe_is_skipped(self) -> None:
        rep = report(db_writable=None)
        self.assertEqual(codes(rep)[CHECK_DB_WRITE], "db_unwired")

    def test_default_probe_writes_and_cleans_up(self) -> None:
        store = _store(self)
        out = default_db_probe(store)
        self.assertTrue(out["writable"], out)
        # 临时表已经删掉：再查一次不该存在
        with self.assertRaises(Exception):
            with store.write_transaction() as conn:
                conn.execute("SELECT n FROM _watchdog_probe").fetchone()

    def test_default_probe_reports_a_closed_database(self) -> None:
        store = StateStore(os.path.join(tempfile.mkdtemp(), "closed.db"))
        store.close()
        out = default_db_probe(store)
        self.assertFalse(out["writable"])
        self.assertIn("Error", out["error"])


# --------------------------------------------------------------------------- 汇总语义


class ReportSemanticsTests(unittest.TestCase):
    def test_multiple_failures_are_all_reported(self) -> None:
        rep = report(container={"alive": False}, health={"ok": False, "status": None},
                     heartbeat_ts=None, bar_state={"BTC": {"error": "boom"}},
                     push_failures=5, db_writable=False)
        self.assertEqual(rep.status, STATUS_ERROR)
        self.assertEqual(len(rep.errors), 5)          # heartbeat 缺失只是 warn
        self.assertEqual(set(rep.codes),
                         {"container_dead", "health_unreachable", "bar_state_unreadable",
                          "push_failing", "db_not_writable", "heartbeat_missing"})
        self.assertTrue(rep.needs_alert)

    def test_empty_report_is_never_ok(self) -> None:
        rep = HealthReport(generated_at=NOW, checks=())
        self.assertEqual(rep.status, STATUS_WARN)

    def test_probe_error_overrides_skipped(self) -> None:
        snap = HealthSnapshot(now=NOW, container={"alive": True}, health=None,
                              heartbeat_ts=NOW - 60, bar_state={"BTC": {"last_close_ms": NOW * 1000}},
                              push_failures=0, db_writable=True,
                              probe_errors=(("health", "RuntimeError: boom"),))
        rep = check_health(snap)
        item = rep.check(CHECK_HEALTH)
        self.assertEqual(item.status, STATUS_ERROR)
        self.assertEqual(item.code, "probe_error")
        self.assertIn("boom", item.message)
        self.assertEqual(rep.status, STATUS_ERROR)


# --------------------------------------------------------------------------- 执行器


def _store(case: unittest.TestCase) -> StateStore:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    store = StateStore(os.path.join(tmp.name, "state.db"))
    case.addCleanup(store.close)
    return store


def green_probes(**overrides: Any) -> Dict[str, Any]:
    """一套「六项全绿」的探针（可覆盖单个）。"""
    probes: Dict[str, Any] = dict(
        container_probe=lambda: {"alive": True, "pid1_state": "S"},
        health_probe=lambda: {"ok": True, "status": 200},
        heartbeat_probe=lambda: (NOW - 60.0, "test"),
        bar_state_probe=lambda: {"BTC": {"last_close_ms": (NOW - H) * 1000.0}},
        push_probe=lambda: 0,
        db_probe=lambda: {"writable": True},
    )
    probes.update(overrides)
    return probes


class WatchdogRunTests(unittest.TestCase):
    def test_clean_run_is_ok_and_logs_one_report(self) -> None:
        store = _store(self)
        dog = Watchdog(store, **green_probes(), clock=lambda: NOW)
        rep = dog.run_once()
        self.assertEqual(rep.status, STATUS_OK)
        self.assertEqual([e["kind"] for e in store.events(limit=5)], ["watchdog_report"])

    def test_identical_conclusions_are_not_logged_twice(self) -> None:
        store = _store(self)
        dog = Watchdog(store, **green_probes(), clock=lambda: NOW)
        for _ in range(3):
            dog.run_once()
        reports = store.events(kind="watchdog_report", limit=20)
        self.assertEqual(len(reports), 1)

        # 结论变化（心跳停了）→ 再来一条
        dog = Watchdog(store, **green_probes(heartbeat_probe=lambda: (None, "missing")),
                       clock=lambda: NOW)
        dog.run_once()
        reports = store.events(kind="watchdog_report", limit=20)
        self.assertEqual(len(reports), 2)
        self.assertIn("heartbeat_missing", reports[0]["data"])

    def test_probe_exception_is_collected_and_does_not_stop_the_run(self) -> None:
        store = _store(self)
        dog = Watchdog(store, **green_probes(
            bar_state_probe=lambda: (_ for _ in ()).throw(RuntimeError("feed died"))),
            clock=lambda: NOW)
        rep = dog.run_once()
        self.assertEqual(rep.status, STATUS_ERROR)                 # 不许报绿
        self.assertEqual(rep.check(CHECK_BAR_STATE).code, "probe_error")
        self.assertIn("feed died", dog.last_error)
        kinds = [e["kind"] for e in store.events(limit=10)]
        self.assertIn("watchdog_error", kinds)
        self.assertIn("watchdog_report", kinds)
        errors = store.events(kind="watchdog_error", limit=5)
        self.assertIn("feed died", errors[0]["message"])

    def test_judging_crash_is_recorded_and_returns_an_error_report(self) -> None:
        """判定层自己崩了（这里让 `now` 变成非有限值）→ 落 events + 报告 error。"""
        store = _store(self)
        dog = Watchdog(store, **green_probes(), clock=lambda: NOW)
        with mock.patch.object(wd, "check_health", side_effect=RuntimeError("judge crashed")):
            rep = dog.run_once()
        self.assertEqual(rep.status, STATUS_ERROR)
        self.assertIn("judge crashed", rep.error)
        self.assertEqual(store.events(kind="watchdog_error", limit=1)[0]["kind"],
                         "watchdog_error")
        self.assertIn("judge crashed", store.events(kind="watchdog_error", limit=1)[0]["message"])

    def test_broken_clock_does_not_raise(self) -> None:
        store = _store(self)

        def boom() -> float:
            raise RuntimeError("clock died")

        dog = Watchdog(store, **green_probes(), clock=boom)
        rep = dog.run_once()                        # 不抛
        self.assertEqual(rep.status, STATUS_ERROR)
        self.assertIn("clock died", rep.error)
        self.assertIn("watchdog_error", [e["kind"] for e in store.events(limit=5)])

    def test_broken_database_does_not_raise(self) -> None:
        """`events` 写不进去时只剩日志，但 `run_once()` 仍然返回报告。"""
        store = _store(self)
        dog = Watchdog(store, **green_probes(), clock=lambda: NOW)
        with mock.patch.object(store, "log_event", side_effect=RuntimeError("disk is gone")):
            rep = dog.run_once()                    # 不抛
        self.assertEqual(rep.status, STATUS_OK)     # 判定本身是好的
        self.assertTrue(dog.last_report is not None)

    def test_alert_hook_failure_is_recorded(self) -> None:
        store = _store(self)

        def bad_alert(kind: str, text: str) -> None:
            raise RuntimeError("webhook 500")

        dog = Watchdog(store, **green_probes(
            heartbeat_probe=lambda: (NOW - 9 * H, "test")), alert=bad_alert, clock=lambda: NOW)
        rep = dog.run_once()
        self.assertEqual(rep.status, STATUS_ERROR)
        failures = store.events(kind="alert_failed", limit=5)
        self.assertEqual(len(failures), 1)
        self.assertIn("webhook 500", failures[0]["message"])

    def test_alert_is_called_only_when_something_is_wrong(self) -> None:
        store = _store(self)
        calls: list = []
        dog = Watchdog(store, **green_probes(),
                       alert=lambda kind, text: calls.append((kind, text)),
                       clock=lambda: NOW)
        dog.run_once()
        self.assertEqual(calls, [])
        dog = Watchdog(store, **green_probes(push_probe=lambda: 4),
                       alert=lambda kind, text: calls.append((kind, text)),
                       clock=lambda: NOW)
        dog.run_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "watchdog")
        self.assertIn("push_failing", calls[0][1])

    def test_missing_bar_state_probe_without_market_is_skipped(self) -> None:
        store = _store(self)
        probes = green_probes()
        probes.pop("bar_state_probe")
        dog = Watchdog(store, **probes, clock=lambda: NOW)
        rep = dog.run_once()
        self.assertEqual(rep.check(CHECK_BAR_STATE).code, "bar_state_unwired")
        self.assertEqual(rep.status, STATUS_WARN)

    def test_market_wires_the_bar_state_probe(self) -> None:
        store = _store(self)

        class FakeMarket:
            def bars(self, symbol: str, **kwargs: Any) -> Any:
                class R:
                    last_close_ms = int((NOW - H) * 1000.0)
                    bars = [object()]
                return R()

        probes = green_probes()
        probes.pop("bar_state_probe")
        dog = Watchdog(store, market=FakeMarket(), **probes, clock=lambda: NOW)
        rep = dog.run_once()
        self.assertEqual(rep.check(CHECK_BAR_STATE).status, STATUS_OK)
        self.assertEqual(rep.status, STATUS_OK)

    def test_module_level_run_once_helper(self) -> None:
        store = _store(self)
        rep = run_once(store, now_ts=NOW, **green_probes())
        self.assertEqual(rep.status, STATUS_OK)
        self.assertEqual(len(store.events(kind="watchdog_report", limit=5)), 1)


# --------------------------------------------------------------------------- 心跳来源


class HeartbeatProbeTests(unittest.TestCase):
    def test_heartbeat_event_wins_over_the_equity_curve(self) -> None:
        store = _store(self)
        store.record_equity(10000.0, ts=NOW - 5 * H)
        record_heartbeat(store, ts=NOW - 60.0, source="scheduler")
        ts, source = default_heartbeat_probe(store)
        self.assertEqual(ts, NOW - 60.0)
        self.assertEqual(source, "heartbeat")

    def test_equity_curve_is_the_fallback_heartbeat(self) -> None:
        store = _store(self)
        store.record_equity(10000.0, ts=NOW - 5 * H)
        ts, source = default_heartbeat_probe(store)
        self.assertEqual(ts, NOW - 5 * H)
        self.assertEqual(source, "equity_curve")

    def test_nothing_recorded_means_missing(self) -> None:
        store = _store(self)
        self.assertEqual(default_heartbeat_probe(store), (None, "missing"))

    def test_record_heartbeat_is_readable_and_ordered(self) -> None:
        store = _store(self)
        record_heartbeat(store, ts=NOW - 100.0)
        record_heartbeat(store, ts=NOW - 50.0)
        rows = store.events(kind="heartbeat", limit=5)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ts"], NOW - 50.0)        # 最新在前
        self.assertEqual(default_heartbeat_probe(store)[0], NOW - 50.0)


# --------------------------------------------------------------------------- 配置


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        config = WatchdogConfig()
        self.assertEqual(config.interval_s, 300.0)
        self.assertEqual(config.heartbeat_max_age_s, 6 * H)
        self.assertEqual(config.bar_stall_age_s, 8 * H)
        self.assertEqual(config.push_window_s, 6 * H)
        self.assertEqual(config.push_fail_alarm, 3)
        self.assertEqual(config.symbols, ("BTC", "ETH", "SOL"))

    def test_invalid_values_are_rejected_loudly(self) -> None:
        for kwargs in ({"interval_s": 0}, {"heartbeat_max_age_s": -1},
                       {"push_fail_alarm": 0}, {"push_fail_alarm": 2.5},
                       {"health_timeout_s": float("nan")}, {"health_url": "  "}):
            with self.subTest(**kwargs):
                with self.assertRaises(WatchdogConfigError):
                    WatchdogConfig(**kwargs)

    def test_from_env(self) -> None:
        config = WatchdogConfig.from_env({
            "WATCHDOG_INTERVAL_S": "60",
            "WATCHDOG_HEARTBEAT_MAX_AGE_S": "7200",
            "WATCHDOG_PUSH_FAIL_ALARM": "5",
            "WATCHDOG_HEALTH_URL": "http://127.0.0.1:9999/health",
            "WATCHDOG_SYMBOLS": "btc, eth",
        })
        self.assertEqual(config.interval_s, 60.0)
        self.assertEqual(config.heartbeat_max_age_s, 7200.0)
        self.assertEqual(config.push_fail_alarm, 5)
        self.assertEqual(config.health_url, "http://127.0.0.1:9999/health")
        self.assertEqual(config.symbols, ("BTC", "ETH"))

    def test_from_env_rejects_garbage(self) -> None:
        with self.assertRaises(WatchdogConfigError):
            WatchdogConfig.from_env({"WATCHDOG_INTERVAL_S": "soon"})
        with self.assertRaises(WatchdogConfigError):
            WatchdogConfig.from_env({"WATCHDOG_PUSH_FAIL_ALARM": "3.5"})


class StructureTests(unittest.TestCase):
    """结构性约束：判定与 IO 分开，且没有导入任何网络以外的重依赖。"""

    def test_check_health_is_pure(self) -> None:
        import inspect
        source = inspect.getsource(wd.check_health)
        for forbidden in ("time.time", "logging", "store.", "requests"):
            self.assertNotIn(forbidden, source)

    def test_bar_stall_limit_reuses_the_market_freshness_constant(self) -> None:
        """8h 不是 watchdog 自己编的：它就是 `app.market.FRESH_MAX_AGE_MS`。"""
        from app.market import BAR_MS, FRESH_MAX_AGE_MS
        self.assertEqual(wd.DEFAULT_BAR_STALL_AGE_S, FRESH_MAX_AGE_MS / 1000.0)
        self.assertEqual(wd.DEFAULT_BAR_STALL_AGE_S, 2 * BAR_MS / 1000.0)

    def test_snapshot_is_json_friendly(self) -> None:
        import json
        payload = snapshot().as_dict()
        json.dumps(payload)
        self.assertEqual(payload["now"], NOW)


if __name__ == "__main__":                      # pragma: no cover
    unittest.main()
