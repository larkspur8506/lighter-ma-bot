"""三个写操作的契约测试（T10b）—— **全离线**（临时库 + 假 flatten，不发任何网络请求）。

钉住的东西（验收项逐条对应）：

1. **无 token → 401**；错 token → 401；`ADMIN_TOKEN` 为空 → 401（fail-closed）。
2. **`flatten` 没确认 → 拒绝**（400 + `ok=false`），**且不调用** flatten 实现
   —— 只"返回一个错误"但照样清仓，是最坏的一种实现。
3. **每次操作落 `events`**：成功、被拒、降级都写 `admin_action`；kill 本身还写
   `admin_kill`；dd/reset 由 `app.dd` 写 `dd_reset` + `dd_reset_audit`。
4. **`kill` 不挡离场**：kill 打开后 `risk.check_entry()` 被 `kill` 闸拦下，而
   `lifecycle.decide_exit()` 照样产出止损/离场意图（它的签名里没有风控入参）。
5. **`dd/reset` 只能显式调用**（必须有 reason），重置后熔断解除。
6. **token 不进 events / 不进响应**（`?k=` 会进访问日志，能控制的只有"绝不进我们的库"）。
7. **DB 不可用时降级不崩**：写操作返回 `ok=false + warnings`，**不是 500**。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from app import lifecycle, views
from app.risk import GATE_KILL, RiskLimits, RiskState, check_entry
from app.store import StateStore

T0 = 1_760_000_000.0
BAR_S = 4 * 3600.0
BAR_MS = 4 * 3600 * 1000
TOKEN = "s3cret-admin-token"

LIMITS = RiskLimits(risk_pct=0.0133)


def make_store(tmp: str) -> StateStore:
    return StateStore(os.path.join(tmp, "state.db"))


def seed_dd(store: StateStore, *, index: float = 0.6, peak: float = 1.0,
            equity: float = 6000.0) -> None:
    """一个回撤已经过熔断线的库（index 0.6 / peak 1.0 ⇒ DD 40% > 30%）。"""
    store.record_equity(10000.0, ts=T0, twr_index=1.0, cum_flow=0.0, source="dd")
    store.record_equity(equity, ts=T0 + BAR_S, twr_index=index, cum_flow=0.0, source="dd")
    store.save_dd_state(twr_index=index, twr_peak=peak, anchor_ts=T0,
                        anchor_equity=10000.0, anchor_cum_flow=0.0, ts=T0 + BAR_S)


class FlatBars(list):
    """很简单的平盘 K 线（只为了让 `decide_exit` 有东西可判）。"""


def flat_bars(start_ms: int, count: int, price: float) -> List[Any]:
    from app.strategy import Bar

    return [Bar(open_ms=start_ms + i * BAR_MS, open=price, high=price * 1.0005,
                low=price * 0.9995, close=price, volume=1.0) for i in range(count)]


class AdminFixture(unittest.TestCase):
    """公共夹具：临时库 + 假 flatten + 固定时钟。"""

    now = T0 + 2 * BAR_S

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.store = make_store(self.tmp)
        self.addCleanup(self.store.close)
        self.flatten_calls: List[Dict[str, Any]] = []
        self.flatten_result: Dict[str, Any] = {"ok": True, "closed": ["BTC"],
                                               "cancelled": True, "steps": []}
        self.app = None
        self.client = None

    def build(self, *, admin_token: str = TOKEN, store: Optional[Any] = None,
              panel: Optional[views.Panel] = None) -> TestClient:
        from app.main import create_app

        store = store if store is not None else self.store
        panel = panel if panel is not None else views.Panel(
            lambda: store, limits=LIMITS, now=lambda: self.now)
        self.app = create_app(panel=panel, admin_token=admin_token, limits=LIMITS,
                              clock=lambda: self.now, flatten=self.fake_flatten)
        self.client = TestClient(self.app)
        self.store = store
        return self.client

    def fake_flatten(self, store: Any, *, reason: str = "", by: str = "admin",
                     **_: Any) -> Dict[str, Any]:
        self.flatten_calls.append({"reason": reason, "by": by,
                                   "positions": len(store.open_positions())})
        return dict(self.flatten_result)

    # ---------------------------------------------------------------- 断言辅助

    def admin_events(self) -> List[Dict[str, Any]]:
        rows = self.store.events(kind=views.ADMIN_EVENT_KIND, limit=50)
        return [{"message": row["message"], "data": json.loads(row["data"] or "{}")}
                for row in rows]


# --------------------------------------------------------------------------- 1. 鉴权


class AuthTests(AdminFixture):
    def test_missing_token_is_rejected(self) -> None:
        client = self.build()
        for endpoint in ("/admin/kill", "/admin/flatten", "/admin/dd/reset"):
            with self.subTest(endpoint=endpoint):
                response = client.post(endpoint)
                self.assertEqual(response.status_code, 401)
                self.assertIs(response.json()["ok"], False)

    def test_wrong_token_is_rejected(self) -> None:
        client = self.build()
        for endpoint in ("/admin/kill", "/admin/flatten", "/admin/dd/reset"):
            with self.subTest(endpoint=endpoint):
                response = client.post(f"{endpoint}?k=nope&confirm=yes&reason=x")
                self.assertEqual(response.status_code, 401)

    def test_empty_configured_token_rejects_everything(self) -> None:
        """`ADMIN_TOKEN` 没配 = 关闭写面板，而不是"没密码所以随便进"。"""
        client = self.build(admin_token="")
        response = client.post("/admin/kill?k=&reason=x")
        self.assertEqual(response.status_code, 401)
        self.assertIn("ADMIN_TOKEN", response.json()["error"])

    def test_header_token_is_accepted(self) -> None:
        client = self.build()
        response = client.post("/admin/kill", headers={"X-Admin-Token": TOKEN},
                               params={"reason": "header auth"})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["ok"], True)

    def test_query_token_is_accepted(self) -> None:
        client = self.build()
        response = client.post("/admin/kill", params={"k": TOKEN, "reason": "query auth"})
        self.assertEqual(response.status_code, 200)

    def test_token_never_lands_in_events_or_responses(self) -> None:
        """`?k=` 会进访问日志；我们至少要保证它**不进自己的库**。"""
        client = self.build()
        client.post("/admin/kill", params={"k": TOKEN, "reason": "audit me"})
        body = json.dumps(client.post("/admin/flatten",
                                      params={"k": TOKEN, "confirm": "yes"}).json())
        self.assertNotIn(TOKEN, body)
        dump = json.dumps([event for event in self.admin_events()], ensure_ascii=False)
        self.assertNotIn(TOKEN, dump)
        all_rows = self.store.events(limit=100)
        self.assertNotIn(TOKEN, json.dumps([dict(row) for row in all_rows], default=str))

    def test_rejected_attempts_are_audited(self) -> None:
        client = self.build()
        client.post("/admin/kill?k=wrong")
        events = self.admin_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["endpoint"], "kill")
        self.assertEqual(events[0]["data"]["result"], "rejected")


# --------------------------------------------------------------------------- 2. kill


class KillTests(AdminFixture):
    def test_kill_returns_200_and_persists(self) -> None:
        client = self.build()
        response = client.post("/admin/kill", params={"k": TOKEN, "reason": "手动停手"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["ok"], True)
        self.assertIs(body["kill"], True)
        self.assertIsNotNone(body["audit_event_id"])
        # 落库：admin_kill（状态）+ admin_action（端点审计）
        self.assertIs(views.kill_state(self.store)["kill"], True)
        self.assertEqual(len(self.store.events(kind="admin_kill", limit=10)), 1)
        self.assertEqual(len(self.admin_events()), 1)

    def test_kill_can_be_turned_off(self) -> None:
        client = self.build()
        client.post("/admin/kill", params={"k": TOKEN, "reason": "on"})
        response = client.post("/admin/kill", params={"k": TOKEN, "on": 0,
                                                      "reason": "off again"})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["kill"], False)
        self.assertIs(views.kill_state(self.store)["kill"], False)
        self.assertEqual(len(self.store.events(kind="admin_kill", limit=10)), 2)

    def test_kill_is_visible_on_the_panel(self) -> None:
        client = self.build()
        client.post("/admin/kill", params={"k": TOKEN, "reason": "今天不交易"})
        status = client.get("/status").json()
        self.assertIs(status["kill"], True)
        self.assertEqual(status["kill_reason"], "今天不交易")
        positions = client.get("/positions").json()
        self.assertIs(positions["entry_blocked"], True)

    def test_kill_does_not_block_exits(self) -> None:
        """**最关键的一条**：kill 只挡开新仓，止损/离场照常。

        两侧都断言：
        * `check_entry()` 被 `kill` 闸拦下（唯一原因是它）；
        * `lifecycle.decide_exit()` 仍然给出 `stop` 意图 —— 它的签名里根本没有风控入参。
        """
        client = self.build()
        client.post("/admin/kill", params={"k": TOKEN, "reason": "kill 只挡开仓"})
        kill = views.kill_state(self.store)["kill"]
        self.assertIs(kill, True)

        limits = RiskLimits(risk_pct=0.0133, kill=kill)
        state = RiskState(equity=10000.0, day_start_equity=10000.0, trades_today=0,
                          open_positions=("BTC",), open_notional_total=500.0,
                          open_notional_by_symbol={"BTC": 500.0},
                          dd_index=1.0, dd_peak_index=1.0)
        decision = check_entry("ETH", 100.0, state, limits)
        self.assertIs(decision.allowed, False)
        self.assertIn(GATE_KILL, decision.codes)

        self.store.record_entry("BTC", 1, qty_int=100_000, size_decimals=5,
                                entry_price_int=1100, price_decimals=1,
                                stop_price_int=1050, bar_ms=int(T0 * 1000), market_id=1)
        bars = flat_bars(int(T0 * 1000), 45, 110.0)
        bars[-1] = bars[-1].__class__(open_ms=bars[-1].open_ms, open=110.0, high=110.2,
                                      low=104.0, close=104.5)
        ind = __import__("app.strategy", fromlist=["compute_indicators"]).compute_indicators(bars)
        exit_decision = lifecycle.decide_exit(self.store.open_positions()[0], bars, ind, 44)
        self.assertEqual(exit_decision.action, lifecycle.ACTION_STOP)
        self.assertAlmostEqual(float(exit_decision.exit_price), 105.0, places=9)

    def test_kill_degrades_when_the_db_is_gone(self) -> None:
        def boom() -> Any:
            raise RuntimeError("database is locked")

        panel = views.Panel(boom, limits=LIMITS, now=lambda: self.now)
        client = self.build(panel=panel, store=None)
        response = client.post("/admin/kill", params={"k": TOKEN, "reason": "no db"})
        self.assertEqual(response.status_code, 200)          # 不是 500
        body = response.json()
        self.assertIs(body["ok"], False)
        self.assertIs(body["degraded"], True)
        self.assertTrue(body["warnings"])


# --------------------------------------------------------------------------- 3. flatten


class FlattenTests(AdminFixture):
    def test_flatten_without_confirmation_is_refused(self) -> None:
        client = self.build()
        response = client.post("/admin/flatten", params={"k": TOKEN, "reason": "abort"})
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIs(body["ok"], False)
        self.assertIn("confirm", body["error"])
        self.assertEqual(self.flatten_calls, [])              # 关键：什么都没执行
        audited = self.admin_events()
        self.assertEqual(len(audited), 1)
        self.assertEqual(audited[0]["data"]["result"], "rejected")

    def test_flatten_with_confirmation_executes_and_audits(self) -> None:
        client = self.build()
        response = client.post("/admin/flatten",
                               params={"k": TOKEN, "confirm": "yes",
                                       "reason": "系统判断有 bug，现在就要出来"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["ok"], True)
        self.assertEqual(body["closed"], ["BTC"])
        self.assertIs(body["cancelled"], True)
        self.assertEqual(len(self.flatten_calls), 1)
        self.assertEqual(self.flatten_calls[0]["reason"], "系统判断有 bug，现在就要出来")
        audited = self.admin_events()
        self.assertEqual(audited[0]["data"]["endpoint"], "flatten")
        self.assertEqual(audited[0]["data"]["result"], "ok")
        self.assertEqual(audited[0]["data"]["closed"], ["BTC"])

    def test_every_confirmation_spelling_is_accepted(self) -> None:
        client = self.build()
        for value in ("yes", "YES", "1", "true", "confirm", "flatten"):
            with self.subTest(confirm=value):
                response = client.post("/admin/flatten",
                                       params={"k": TOKEN, "confirm": value})
                self.assertEqual(response.status_code, 200)

    def test_a_failing_flatten_is_reported_not_hidden(self) -> None:
        self.flatten_result = {"ok": False, "closed": [], "cancelled": False,
                               "error": "LiveTradingDisabled: live trading is off",
                               "steps": [{"symbol": "BTC", "ok": False}]}
        client = self.build()
        response = client.post("/admin/flatten", params={"k": TOKEN, "confirm": "yes"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["ok"], False)
        self.assertTrue(body["warnings"])
        self.assertEqual(self.admin_events()[0]["data"]["result"], "failed")

    def test_default_flatten_reports_the_live_gate_instead_of_crashing(self) -> None:
        """**默认实现**（不是注入的假实现）在真实 HTTP handler 里也必须能跑完。

        这条测试是为了钉住一个实测踩到的 bug：默认 flatten 曾经写成 sync + `asyncio.run()`，
        在 FastAPI 的 handler（已经在跑 event loop）里直接抛
        `RuntimeError: Cannot run the event loop while another loop is running`，
        「清仓」按钮变成必然失败。现在它是 async，且**没有** live gate（下面这套假凭据
        没开 `LIGHTER_LIVE_TRADING`），所以结论必须是 `ok=false` + 真实原因，
        而不是一个循环错误。
        """
        from unittest import mock

        env = {
            "LIGHTER_ACCOUNT_INDEX": "12345",
            "LIGHTER_API_KEY_INDEX": "4",
            "LIGHTER_API_PRIVATE_KEY": "0x" + "ab" * 40,
            "LIGHTER_L1_ADDRESS": "0x" + "cd" * 20,
            "LIGHTER_ENVIRONMENT": "testnet",
            "ADMIN_TOKEN": TOKEN,
        }
        self.store.record_entry("BTC", 1, qty_int=100_000, size_decimals=5,
                                entry_price_int=650_000, price_decimals=2,
                                stop_price_int=630_000, bar_ms=int(T0 * 1000),
                                market_id=1)
        panel = views.Panel(lambda: self.store, limits=LIMITS, now=lambda: self.now)
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("LIGHTER_LIVE_TRADING", None)
            os.environ.pop("LIGHTER_LIVE_ACCOUNT_INDEX", None)
            from app.main import create_app

            client = TestClient(create_app(panel=panel, admin_token=TOKEN,
                                          limits=LIMITS, clock=lambda: self.now))
            response = client.post("/admin/flatten",
                                   params={"k": TOKEN, "confirm": "yes",
                                           "reason": "smoke"})
        self.assertEqual(response.status_code, 200)           # 不是 500
        body = response.json()
        self.assertIs(body["ok"], False)
        self.assertNotIn("another loop is running", json.dumps(body))
        self.assertTrue(body["steps"])
        self.assertIn("live", json.dumps(body, ensure_ascii=False).lower())

    def test_flatten_exception_degrades_to_a_warning(self) -> None:
        def explode(*_: Any, **__: Any) -> Any:
            raise RuntimeError("boom")

        self.fake_flatten = explode                       # type: ignore[assignment]
        client = self.build()
        response = client.post("/admin/flatten", params={"k": TOKEN, "confirm": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["ok"], False)
        self.assertIn("boom", response.json()["error"])


# --------------------------------------------------------------------------- 4. dd reset


class DdResetTests(AdminFixture):
    def test_reset_requires_a_reason(self) -> None:
        seed_dd(self.store)
        client = self.build()
        response = client.post("/admin/dd/reset", params={"k": TOKEN})
        self.assertEqual(response.status_code, 400)
        self.assertIs(response.json()["ok"], False)
        self.assertEqual(self.admin_events()[0]["data"]["result"], "rejected")
        # 峰值没被动过
        self.assertEqual(self.store.dd_state()["twr_peak"], 1.0)

    def test_reset_clears_the_breaker_and_is_audited(self) -> None:
        seed_dd(self.store)                                    # DD 40% ⇒ 熔断
        client = self.build()
        self.assertEqual(client.get("/status").json()["dd"]["action"], "kill")

        response = client.post("/admin/dd/reset",
                               params={"k": TOKEN, "reason": "人工核对后重开"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIs(body["ok"], True)
        self.assertEqual(body["verdict"]["action"], "ok")       # 熔断解除
        self.assertEqual(body["reason"], "人工核对后重开")

        state = self.store.dd_state()
        self.assertEqual(state["twr_index"], 1.0)               # 指数重置到基准
        self.assertEqual(state["twr_peak"], 1.0)
        self.assertEqual(state["reset_reason"], "人工核对后重开")

        kinds = [row["kind"] for row in self.store.events(limit=50)]
        self.assertIn("dd_reset", kinds)                       # 状态变更（app.store 写）
        self.assertIn("dd_reset_audit", kinds)                 # 谁调的（app.dd 写）
        self.assertIn(views.ADMIN_EVENT_KIND, kinds)           # 端点级审计
        audit = self.store.events(kind="dd_reset_audit", limit=1)[0]
        self.assertIn("人工核对后重开", audit["message"])

    def test_reset_refuses_when_the_index_is_zero(self) -> None:
        """指数 0（账户打光/状态坏）→ 拒绝重置：抹平峰值只会把爆仓藏起来。"""
        seed_dd(self.store, index=0.0, equity=0.0)
        client = self.build()
        response = client.post("/admin/dd/reset", params={"k": TOKEN, "reason": "try"})
        self.assertEqual(response.status_code, 200)            # 业务拒绝 ≠ 系统故障
        body = response.json()
        self.assertIs(body["ok"], False)
        self.assertIs(body["degraded"], True)
        self.assertIn("refusing", body["error"])
        self.assertEqual(self.admin_events()[0]["data"]["result"], "rejected")

    def test_reset_degrades_when_the_db_is_gone(self) -> None:
        def boom() -> Any:
            raise RuntimeError("database is locked")

        panel = views.Panel(boom, limits=LIMITS, now=lambda: self.now)
        client = self.build(panel=panel, store=None)
        response = client.post("/admin/dd/reset", params={"k": TOKEN, "reason": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["ok"], False)
        self.assertIs(response.json()["degraded"], True)


# --------------------------------------------------------------------------- 5. 只读 admin 视图


class AdminStateTests(AdminFixture):
    def test_admin_state_requires_the_token(self) -> None:
        client = self.build()
        self.assertEqual(client.get("/admin/state").status_code, 401)
        self.assertEqual(client.get(f"/admin/state?k={TOKEN}").status_code, 200)

    def test_admin_state_reports_kill_and_scheduler(self) -> None:
        client = self.build()
        client.post("/admin/kill", params={"k": TOKEN, "reason": "state check"})
        body = client.get(f"/admin/state?k={TOKEN}").json()
        self.assertIs(body["kill"]["kill"], True)
        self.assertIs(body["scheduler"]["configured"], False)   # 注入的是 panel，没有调度器
        self.assertIs(body["cache"]["orders_known"], False)


if __name__ == "__main__":
    unittest.main()
