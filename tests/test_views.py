"""`app/views.py` 的契约测试 —— **全离线**（临时目录 DB + 手工权益点/假 K 线）。

钉住的东西（T10a / T10c 验收项，逐条对应）：

1. **面板不 import `app.exchange`**（AST 扫描，不是 grep 字符串）—— 面板永远不碰
   私钥路径。同一个测试也扫 `app.executor`（它会把 exchange 拖进 import 图）。
2. **TWR 口径与 `app.dd` 是同一份**：面板曲线的末值必须**逐位等于**
   `app.dd.bootstrap()` 的结果（同一个函数、同一份流水），并且面板自己声明的
   `consistency.match` 为 True。
3. **累计盈亏剔除出入金**：入金 500 那一刻的 `cum_pnl` 不变（30 → 30），而权益
   从 2030 掉到 1950 时 `cum_pnl` 走 530 → 450。
4. **6 个区块结构完整**、数值写死。
5. **HTML 含 6 个区块标识且自包含**（无 `href`/`src`/`http`/`@import`）。
6. **事件流含「跳过」**（`kind='skip'` → 动作「跳过」），否则用户以为系统坏了。
7. **降级不崩**：DB 打不开 → 每块都是 0 值 + WARNING；单个 provider 崩只影响它
   自己那一块。
8. **三标的动态复用 `strategy.explain_entry`**（数值与直接调用一致）。
9. **kill 状态落库可读**（`set_kill` / `kill_state` 走 `events`）。
10. **懒建的 store 有 `close()` 且幂等**：`Panel.close()` 真的关掉
    `sqlite3.Connection`、关后不复活、不关别人的连接（否则 7×24 面板每重启一次就漏
    一个 fd）。这是 `ResourceWarning: unclosed database` 的根因。

期望值全部手算写死，每个断言旁给算式。
"""

from __future__ import annotations

import ast
import gc
import os
import pathlib
import re
import sqlite3
import tempfile
import unittest
import weakref
from typing import Any, Dict, List, Optional, Sequence

from app import strategy, views
from app.dd import TWR_BASE, bootstrap, net_flow, normalise_points
from app.risk import RiskLimits
from app.store import StateStore

ROOT = pathlib.Path(__file__).resolve().parent.parent

T0 = 1_760_000_000.0            # 2025-10-09T08:53:20Z
BAR_S = 4 * 3600.0
BAR_MS = 4 * 3600 * 1000

#: 四个权益点：1000 → 1030（交易 +3%）→ 1530（入金 500，无交易）→ 1450（交易 −80）
EQUITY_POINTS = [
    (T0, 1000.0, 1.0, 0.0),
    (T0 + BAR_S, 1030.0, 1.03, 0.0),
    (T0 + 2 * BAR_S, 1530.0, 1.03, 500.0),
    (T0 + 3 * BAR_S, 1450.0, 1.03 * 1450.0 / 1530.0, 500.0),
]
#: 末值 TWR = 1.03 × (1450/1530) = 0.9761437908496732（入金那一步 (1530−500)/1030 = 1.0）
TWR_FINAL = 0.9761437908496732
TWR_PEAK = 1.03
#: 累计盈亏（剔出入金）= 1450 − 1000 − 500 = −50
CUM_PNL = -50.0
#: 回撤 = (1.03 − 0.9761437908)/1.03 = 5.2287581699346455%
DRAWDOWN = 0.052287581699346455


def make_store(tmp: str) -> StateStore:
    return StateStore(os.path.join(tmp, "state.db"))


def seed(store: StateStore) -> None:
    """一个「入金后回撤 3.94%」的库：4 个权益点 + dd_state + 一个 BTC 多仓 + 事件。"""
    for ts, equity, twr, cum in EQUITY_POINTS:
        store.record_equity(equity, ts=ts, twr_index=twr, cum_flow=cum, source="dd")
    store.save_dd_state(twr_index=TWR_FINAL, twr_peak=TWR_PEAK, anchor_ts=T0,
                        anchor_equity=1000.0, anchor_cum_flow=500.0, ts=T0 + 3 * BAR_S)
    store.record_entry("BTC", 1, qty_int=100_000, size_decimals=5,
                       entry_price_int=650_000, price_decimals=2,
                       stop_price_int=630_000, bar_ms=int(T0 * 1000), market_id=1,
                       stop_order_id="stop-1", equity_at_entry=1530.0, ts=T0 + 2 * BAR_S)
    store.log_event("skip", "BTC skip: 未过密集门槛（带宽 0.42% > 门槛 0.31%）",
                    symbol="BTC", ts=T0 + 3 * BAR_S)
    store.log_event("entry", "BTC opened 1.0 @ 6500", symbol="BTC", ts=T0 + 3 * BAR_S + 1)
    store.log_event("heartbeat", "tick heartbeat from scheduler",
                    data={"source": "scheduler", "status": "ok"}, ts=T0 + 3 * BAR_S + 60)


def fake_orders() -> List[Dict[str, Any]]:
    """交易所侧：BTC 止损单在、数量覆盖（1.0 = 仓位的 1.0）。"""
    return [{"symbol": "BTC", "price": 6300.0, "qty": 1.0, "order_id": "stop-1",
             "reduce_only": True}]


def flat_bars(start_ms: int, count: int, price: float) -> List[strategy.Bar]:
    return [strategy.Bar(open_ms=start_ms + i * BAR_MS, open=price, high=price * 1.001,
                         low=price * 0.999, close=price, volume=1.0)
            for i in range(count)]


class PanelFixture(unittest.TestCase):
    """公共夹具：临时库 + 固定时钟 + 假 provider。"""

    now = T0 + 3 * BAR_S + 600.0

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.store = make_store(self.tmp)
        self.addCleanup(self.store.close)

    def panel(self, *, sources: Optional[views.Sources] = None,
              limits: Optional[RiskLimits] = None) -> views.Panel:
        return views.Panel(lambda: self.store,
                           sources=sources if sources is not None else views.Sources(),
                           limits=limits if limits is not None else RiskLimits(risk_pct=0.0133),
                           now=lambda: self.now)

    def seeded_panel(self, **kwargs: Any) -> views.Panel:
        seed(self.store)
        sources = kwargs.pop("sources", views.Sources(
            prices=lambda: {"BTC": 6600.0}, orders=fake_orders))
        return self.panel(sources=sources, **kwargs)


# --------------------------------------------------------------------------- 1. import 边界


class ModuleBoundaryTests(unittest.TestCase):
    """面板的 import 图里**不许**出现私钥路径。"""

    FORBIDDEN_MODULES = ("app.exchange", "app.executor", "lighter")

    def _imports(self, path: pathlib.Path) -> List[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.append(node.module or "")
        return found

    def test_views_never_imports_the_exchange_or_the_executor(self) -> None:
        modules = self._imports(ROOT / "app" / "views.py")
        offenders = [m for m in modules
                     if any(m == forbidden or m.startswith(forbidden + ".")
                            for forbidden in self.FORBIDDEN_MODULES)]
        self.assertEqual(offenders, [],
                         f"app/views.py 一旦 import exchange/executor，面板进程就能碰私钥: "
                         f"{offenders}")

    def test_views_has_no_transitive_hint_of_the_private_key_path(self) -> None:
        text = (ROOT / "app" / "views.py").read_text(encoding="utf-8")
        for needle in ("import app.exchange", "from app.exchange",
                       "signer_client", "sign_create_order", "send_tx",
                       "api_private_key", "LIGHTER_API_PRIVATE_KEY"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

    def test_views_module_is_importable_without_the_sdk(self) -> None:
        # 只断言 import 图，不真去卸包；`app.views` 的模块级 import 全是 stdlib + 本项目。
        modules = self._imports(ROOT / "app" / "views.py")
        self.assertNotIn("lighter", modules)
        self.assertTrue(all(m.split(".")[0] in ("app", "__future__", "html", "json",
                                                "logging", "math", "time", "dataclasses",
                                                "typing")
                            for m in modules), f"unexpected imports: {modules}")


# --------------------------------------------------------------------------- 2. TWR 口径


class TwrConsistencyTests(PanelFixture):
    """面板的 TWR 必须与 `app.dd` 逐位相同。"""

    def test_panel_curve_matches_dd_bootstrap_exactly(self) -> None:
        panel = self.seeded_panel()
        block = panel.pnl()

        points = normalise_points(self.store.equity_curve(limit=500))
        flows: List[Dict[str, float]] = [{"ts": T0 + 2 * BAR_S, "amount": 500.0}]
        expected = bootstrap(points, flows, anchor_ts=T0, anchor_equity=1000.0)

        self.assertEqual(block["twr_index"], expected.index)
        self.assertEqual(block["twr_peak"], expected.peak)
        self.assertAlmostEqual(block["twr_index"], TWR_FINAL, places=15)
        self.assertAlmostEqual(block["twr_peak"], TWR_PEAK, places=15)
        self.assertEqual(block["consistency"]["match"], True)
        self.assertEqual(block["consistency"]["index_delta"], 0.0)
        self.assertEqual(block["consistency"]["peak_delta"], 0.0)

    def test_dd_drawdown_matches_the_curve(self) -> None:
        """面板回撤 = `(peak − index)/peak`，与 `app/risk.drawdown` 同一个算式。"""
        block = self.seeded_panel().pnl()
        expected = (TWR_PEAK - TWR_FINAL) / TWR_PEAK
        self.assertAlmostEqual(block["drawdown_pct"], expected, places=15)
        self.assertAlmostEqual(block["drawdown_pct"], DRAWDOWN, places=15)

    def test_cum_pnl_excludes_the_deposit(self) -> None:
        block = self.seeded_panel().pnl()
        rows = {round(point["ts"]): point for point in block["points"]}
        # 入金那一刻：权益 1030→1530 全是入金，累计盈亏必须停在 30
        self.assertAlmostEqual(rows[round(T0 + 2 * BAR_S)]["cum_pnl"], 30.0, places=9)
        # 入金之后亏 80：1530−80=1450 ⇒ 累计盈亏 30−80 = −50
        self.assertAlmostEqual(rows[round(T0 + 3 * BAR_S)]["cum_pnl"], CUM_PNL, places=9)
        self.assertAlmostEqual(block["cum_pnl"], CUM_PNL, places=9)

    def test_flows_are_derived_from_the_cum_flow_column(self) -> None:
        """面板不另建流水表：流水来自 `equity_curve.cum_flow` 的差分。"""
        seed(self.store)
        points = self.store.equity_curve(limit=500)
        flows = views._flows_from_points(points)
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["ts"], T0 + 2 * BAR_S)
        self.assertEqual(flows[0]["amount"], 500.0)
        self.assertEqual(net_flow(flows, T0, T0 + 3 * BAR_S), 500.0)

    def test_peak_is_the_larger_of_replay_and_stored_state(self) -> None:
        """峰值只升不降：库里 dd_state 的峰值更高时，面板不许显示更小的回撤。"""
        seed(self.store)
        # 把库里峰值抬到 2.0（历史高点，重放窗口看不到）—— 回撤必须按 2.0 算
        self.store.save_dd_state(twr_index=TWR_FINAL, twr_peak=2.0, anchor_ts=T0,
                                 anchor_equity=1000.0, anchor_cum_flow=500.0,
                                 ts=T0 + 3 * BAR_S + 1)
        block = self.panel(sources=views.Sources(prices=lambda: {"BTC": 6600.0},
                                                orders=fake_orders)).pnl()
        self.assertEqual(block["peak"], 2.0)
        self.assertAlmostEqual(block["drawdown_pct"], (2.0 - TWR_FINAL) / 2.0, places=15)

    def test_no_points_degrades_to_zero_with_warning(self) -> None:
        block = views.pnl_curve([])
        self.assertEqual(block["points"], [])
        self.assertEqual(block["cum_pnl"], 0.0)
        self.assertEqual(block["twr_index"], TWR_BASE)
        self.assertEqual(block["twr_pct"], 100.0)
        self.assertTrue(block["warnings"])


# --------------------------------------------------------------------------- 3. 六区块


class SixBlockTests(PanelFixture):
    def test_snapshot_has_all_six_blocks(self) -> None:
        snapshot = self.seeded_panel().snapshot()
        self.assertEqual(snapshot["block_order"], list(views.BLOCKS))
        for name in views.BLOCKS:
            with self.subTest(block=name):
                self.assertIn(name, snapshot["blocks"])
                self.assertIn("warnings", snapshot["blocks"][name])

    def test_status_block_values(self) -> None:
        block = self.seeded_panel().status()
        self.assertEqual(block["equity"], 1450.0)          # 最后一个权益点
        self.assertEqual(block["equity_source"], "store")
        self.assertEqual(block["open_count"], 1)
        self.assertEqual(block["max_positions"], 3)        # RiskLimits 默认
        self.assertEqual(block["kill"], False)
        self.assertEqual(block["dd"]["action"], "ok")
        self.assertAlmostEqual(block["dd"]["drawdown"], DRAWDOWN, places=15)
        self.assertEqual(block["dd"]["warn"], 0.25)        # risk_pct 1.33% 档
        self.assertEqual(block["dd"]["kill"], 0.30)
        self.assertEqual(block["level"], "ok")
        self.assertEqual(block["last_tick_ts"], T0 + 3 * BAR_S + 60)

    def test_positions_block_values(self) -> None:
        block = self.seeded_panel().positions()
        self.assertEqual(block["open_count"], 1)
        btc = block["positions"][0]
        self.assertEqual(btc["symbol"], "BTC")
        self.assertEqual(btc["side_text"], "多")
        self.assertEqual(btc["entry_price"], 6500.0)
        self.assertEqual(btc["price"], 6600.0)             # provider
        self.assertAlmostEqual(btc["upnl"], 100.0, places=9)          # (6600−6500)×1×1
        self.assertAlmostEqual(btc["dist_to_stop_pct"], 300.0 / 6600.0, places=12)
        self.assertAlmostEqual(btc["notional"], 6600.0, places=9)       # 当前名义 = 6600×1
        self.assertAlmostEqual(btc["margin"], 6600.0 / 5.0, places=9)   # 逐仓 5x
        self.assertAlmostEqual(btc["liq_price"], 6500.0 * 0.8, places=9)
        self.assertEqual(btc["status"], "持仓中")
        self.assertEqual([row["symbol"] for row in block["positions"]], ["BTC", "ETH", "SOL"])

    def test_stops_block_covers_the_position(self) -> None:
        block = self.seeded_panel().stops()
        self.assertEqual(block["orders_known"], True)
        row = block["stops"][0]
        self.assertEqual(row["stop_price"], 6300.0)
        self.assertEqual(row["exchange_status"], "present")
        self.assertEqual(row["protected"], True)
        self.assertEqual(block["uncovered"], [])
        self.assertEqual(block["all_protected"], True)

    def test_stops_block_flags_a_missing_stop_order(self) -> None:
        """止损单不在 = 裸奔，必须红字（`protected=False` + warning）。"""
        seed(self.store)
        panel = self.panel(sources=views.Sources(prices=lambda: {"BTC": 6600.0},
                                                orders=lambda: []))
        block = panel.stops()
        self.assertEqual(block["stops"][0]["exchange_status"], "missing")
        self.assertEqual(block["stops"][0]["protected"], False)
        self.assertEqual(block["uncovered"], ["BTC"])
        self.assertTrue(block["warnings"])

    def test_stops_block_says_unknown_without_an_orders_source(self) -> None:
        """没有挂单数据源时只能说「未知」——**不能**说「在」，也不该说「不在」。"""
        seed(self.store)
        block = self.panel().stops()
        self.assertEqual(block["orders_known"], False)
        self.assertEqual(block["stops"][0]["exchange_status"], "unknown")
        self.assertEqual(block["stops"][0]["protected"], False)

    def test_events_block_includes_skips(self) -> None:
        block = self.seeded_panel().events()
        actions = [item["action"] for item in block["events"]]
        self.assertIn("跳过", actions)
        self.assertIn("开仓", actions)
        skip = [item for item in block["events"] if item["skipped"]]
        self.assertEqual(len(skip), 1)
        self.assertIn("未过密集门槛", skip[0]["message"])
        self.assertEqual(block["skipped_count"], 1)

    def test_snapshot_reports_level_and_warnings(self) -> None:
        # 没给 K 线数据源 ⇒ 只有⑤降级；其余五块正常出数（区块级隔离）
        snapshot = self.seeded_panel().snapshot()
        self.assertEqual(snapshot["level"], "ok")
        self.assertEqual(snapshot["blocks"]["symbols"]["degraded"], True)
        for name in ("status", "positions", "stops", "pnl", "events"):
            with self.subTest(block=name):
                self.assertEqual(snapshot["blocks"][name]["degraded"], False)
        self.assertEqual(snapshot["degraded"], True)

    def test_snapshot_is_clean_when_every_source_is_available(self) -> None:
        bars = flat_bars(1_740_000_000_000, 420, 100.0)
        sources = views.Sources(prices=lambda: {"BTC": 6600.0}, orders=fake_orders,
                                bars=lambda symbol: bars)
        snapshot = self.seeded_panel(sources=sources).snapshot()
        self.assertEqual(snapshot["degraded"], False)
        self.assertEqual(snapshot["warnings"], [])

    def test_event_action_mapping(self) -> None:
        cases = {
            "skip": views.ACTION_SKIP,
            "dd_skipped": views.ACTION_SKIP,
            "entry": views.ACTION_ENTRY,
            "exit": views.ACTION_EXIT,
            "stop": views.ACTION_STOP,
            "stop_fill_settled": views.ACTION_STOP,
            "dd_kill": views.ACTION_KILL,
            "circuit_open": views.ACTION_KILL,
            "admin_kill": views.ACTION_ADMIN,
            "dd_reset_audit": views.ACTION_ADMIN,
            "heartbeat": views.ACTION_HEARTBEAT,
            "reconcile_mismatch": views.ACTION_CHECK,
            "something_else": views.ACTION_OTHER,
        }
        for kind, expected in cases.items():
            with self.subTest(kind=kind):
                self.assertEqual(views.event_action(kind), expected)


# --------------------------------------------------------------------------- 4. HTML


class HtmlTests(PanelFixture):
    def _html(self) -> str:
        return self.seeded_panel().page()

    def test_html_contains_the_six_block_ids(self) -> None:
        html = self._html()
        for name in views.BLOCKS:
            with self.subTest(block=name):
                self.assertIn(f'id="block-{name}"', html)

    def test_html_is_self_contained(self) -> None:
        """自包含 = 没有外链（运行环境可能没外网；CDN 挂了就是空白页）。

        内联 `<script>`（⑦ 人工操作区的三个写按钮）是允许的：它同样不依赖任何
        外部资源 —— 所以禁的是外链形态（http(s)://、<link、src=、@import、cdn.），
        不是 script 本身。
        """
        html = self._html()
        for needle in ("http://", "https://", "<link", "src=", "@import", "cdn.",
                       "javascript:"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, html)
        self.assertIn("<script>", html)                 # 人工操作脚本是内联的
        self.assertIn("<svg", html)                     # 曲线是内联 SVG
        self.assertIn("<style>", html)                  # 样式内联

    def test_html_has_the_admin_block(self) -> None:
        """⑦ 人工操作区必须在页面上（kill / 强制清仓 / 重置回撤三个按钮）。"""
        html = self._html()
        for needle in ('id="block-admin"', 'id="btn-kill-on"', 'id="btn-kill-off"',
                       'id="btn-flatten"', 'id="btn-dd-reset"',
                       'localStorage.setItem'):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)

    def test_html_escapes_database_text(self) -> None:
        self.store.log_event("skip", "<script>alert('x')</script> 未过密集门槛")
        html = self._html()
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_html_shows_key_numbers(self) -> None:
        html = self._html()
        self.assertIn("1,450.00", html)                 # 权益
        self.assertIn("-50.00", html)                   # 累计盈亏
        self.assertIn("持仓中", html)
        self.assertIn("已覆盖", html)

    def test_html_renders_with_no_data(self) -> None:
        """空库（0 值）也得出页面 —— 降级路径必须被渲染过。"""
        html = self.panel().page()
        for name in views.BLOCKS:
            self.assertIn(f'id="block-{name}"', html)
        self.assertIn("数据不足", html)                  # 曲线占位文案


# --------------------------------------------------------------------------- 5. 降级


class DegradedTests(unittest.TestCase):
    def _broken(self) -> views.Panel:
        def boom() -> Any:
            raise RuntimeError("database is locked")
        return views.Panel(boom, now=lambda: T0)

    def test_every_block_degrades_to_zeros_with_warnings(self) -> None:
        panel = self._broken()
        status = panel.status()
        self.assertEqual(status["equity"], 0.0)
        self.assertEqual(status["level"], "alert")
        self.assertFalse(status["degraded"] is False)
        self.assertTrue(status["warnings"])

        positions = panel.positions()
        self.assertEqual(positions["open_count"], 0)
        self.assertEqual([row["symbol"] for row in positions["positions"]],
                         ["BTC", "ETH", "SOL"])
        self.assertTrue(all(row["entry_price"] == 0.0 for row in positions["positions"]))
        self.assertTrue(positions["warnings"])

        stops = panel.stops()
        self.assertEqual(stops["stops"], [])
        self.assertFalse(stops["all_protected"])
        self.assertTrue(stops["warnings"])

        pnl = panel.pnl()
        self.assertEqual(pnl["points"], [])
        self.assertEqual(pnl["cum_pnl"], 0.0)
        self.assertEqual(pnl["twr_pct"], 100.0)
        self.assertTrue(pnl["warnings"])

        events = panel.events()
        self.assertEqual(events["events"], [])
        self.assertTrue(events["warnings"])

        symbols = panel.symbols_block()
        self.assertEqual(len(symbols["symbols"]), 3)
        self.assertTrue(all(row["price"] == 0.0 for row in symbols["symbols"]))

    def test_degraded_page_still_renders(self) -> None:
        html = self._broken().page()
        for name in views.BLOCKS:
            self.assertIn(f'id="block-{name}"', html)
        self.assertIn("⚠", html)                        # WARNING 有地方显示

    def test_a_broken_provider_only_degrades_its_own_block(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = make_store(tmp.name)
        self.addCleanup(store.close)
        seed(store)

        def boom() -> Any:
            raise RuntimeError("market api 502")

        panel = views.Panel(lambda: store,
                            sources=views.Sources(prices=boom, orders=fake_orders),
                            now=lambda: T0 + 3 * BAR_S + 600.0)
        positions = panel.positions()
        self.assertTrue(positions["warnings"])
        self.assertIn("现价 provider 失败", " ".join(positions["warnings"]))
        # 其它区块不受影响
        self.assertEqual(panel.events()["count"], 4)
        self.assertEqual(panel.stops()["all_protected"], True)
        self.assertEqual(panel.pnl()["cum_pnl"], CUM_PNL)

    def test_store_recovers_on_the_next_request(self) -> None:
        """DB 只是短暂锁住：下一次请求要能自己好起来（而不是永久 500）。"""
        state = {"fail": True}
        healthy = StateStore(":memory:")
        self.addCleanup(healthy.close)

        def factory() -> Any:
            if state["fail"]:
                raise RuntimeError("locked")
            return healthy

        panel = views.Panel(factory, now=lambda: T0)
        self.assertEqual(panel.status()["level"], "alert")
        state["fail"] = False
        self.assertEqual(panel.status()["equity"], 0.0)   # 空库，但不再是降级路径
        self.assertEqual(panel.status()["kill_known"], True)


# --------------------------------------------------------------------------- 6. 三标的动态


class SymbolsBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = make_store(tmp.name)
        self.addCleanup(self.store.close)

    def test_symbols_reuse_strategy_explain_entry(self) -> None:
        bars = flat_bars(1_740_000_000_000, 420, 100.0)
        panel = views.Panel(lambda: self.store,
                            sources=views.Sources(bars=lambda symbol: bars,
                                                  prices=lambda: {"BTC": 100.0}),
                            now=lambda: (1_740_000_000_000 + 420 * BAR_MS) / 1000.0)
        block = panel.symbols_block()
        ind = strategy.compute_indicators(bars)
        squeeze = strategy.squeeze_flags(ind["band"])
        expected = strategy.explain_entry("BTC", bars, ind, squeeze, len(bars) - 1,
                                          entry_price=100.0)
        btc = block["symbols"][0]
        self.assertEqual(btc["blocked_by"], expected["blocked_by"])
        self.assertEqual(btc["reason"], expected["reason"])
        self.assertEqual(btc["band_pct"], expected["values"]["band_prev"])
        self.assertEqual(btc["threshold_pct"], expected["values"]["band_thr"])
        self.assertEqual(btc["dense_bars"], expected["values"]["dense_cnt"])
        self.assertEqual(btc["bars"], 420)
        self.assertIsNotNone(btc["sma20"])
        self.assertIsNotNone(btc["sma200"])
        self.assertEqual(btc["next_bar_close_ms"], bars[-1].open_ms + BAR_MS)

    def test_symbols_block_degrades_without_a_bar_source(self) -> None:
        panel = views.Panel(lambda: self.store, now=lambda: T0)
        block = panel.symbols_block()
        self.assertTrue(block["degraded"])
        self.assertEqual(len(block["symbols"]), 3)
        self.assertTrue(all(row["dense_bars"] == 0 for row in block["symbols"]))
        self.assertIn("没有 K 线数据源", " ".join(block["warnings"]))

    def test_one_broken_symbol_does_not_kill_the_table(self) -> None:
        def bars(symbol: str) -> Any:
            if symbol == "ETH":
                raise RuntimeError("eth feed down")
            return flat_bars(1_740_000_000_000, 420, 100.0)

        panel = views.Panel(lambda: self.store, sources=views.Sources(bars=bars),
                            now=lambda: T0)
        block = panel.symbols_block()
        self.assertEqual(len(block["symbols"]), 3)
        eth = [row for row in block["symbols"] if row["symbol"] == "ETH"][0]
        self.assertEqual(eth["blocked_by"], "no_data")
        self.assertTrue(any("ETH" in w for w in block["warnings"]))


# --------------------------------------------------------------------------- 7. kill 开关


class KillSwitchTests(PanelFixture):
    def test_kill_state_is_persisted_in_events(self) -> None:
        self.assertFalse(views.kill_state(self.store)["kill"])
        views.set_kill(self.store, True, by="admin", reason="manual stop", ts=T0)
        state = views.kill_state(self.store)
        self.assertEqual(state["kill"], True)
        self.assertEqual(state["reason"], "manual stop")
        self.assertEqual(state["by"], "admin")
        rows = self.store.events(kind=views.KILL_EVENT_KIND, limit=5)
        self.assertEqual(len(rows), 1)

    def test_kill_then_off_is_a_second_event(self) -> None:
        views.set_kill(self.store, True, ts=T0)
        views.set_kill(self.store, False, ts=T0 + 1)
        self.assertEqual(views.kill_state(self.store)["kill"], False)
        self.assertEqual(self.store.event_count(), 2)
        # 最新那条必须在最前面（事件流按 ts DESC 读）
        self.assertEqual(len(self.store.events(kind=views.KILL_EVENT_KIND, limit=1)), 1)

    def test_status_reflects_the_kill_switch(self) -> None:
        seed(self.store)
        views.set_kill(self.store, True, reason="我今天不想交易", ts=T0 + 3 * BAR_S)
        block = self.panel(limits=RiskLimits(risk_pct=0.0133)).status()
        self.assertEqual(block["kill"], True)
        self.assertEqual(block["kill_reason"], "我今天不想交易")
        self.assertIn(block["level"], ("warn", "alert"))
        self.assertTrue(any("kill" in reason for reason in block["reasons"]))


# --------------------------------------------------------------------------- 8. store 生命周期
#
# 缺陷（PR #11 验收发现）：`Panel` 懒建的 store **没有任何 close() 路径**。面板是
# 7×24 常驻进程，`StateStore` 内部是一个长连接 `sqlite3.Connection` —— 建了不关就是
# fd 泄漏 + SQLite 的 `-wal`/`-shm` 一直被持有。实测证据：修之前全量测试有 5 条
# `ResourceWarning: unclosed database`，修之后 0 条。


class PanelStoreLifecycleTests(unittest.TestCase):
    """`Panel.close()`：真的关、幂等、关后不复活、不关别人的连接。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.made: List[StateStore] = []

    # ---------------------------------------------------------------- 夹具

    def factory(self) -> StateStore:
        store = StateStore(os.path.join(self.tmp, f"db{len(self.made)}.db"))
        self.made.append(store)
        return store

    def assert_closed(self, store: Any) -> None:
        """`sqlite3.Connection` 已关的判据：再执行一句就抛 `ProgrammingError`。"""
        with self.assertRaises(sqlite3.ProgrammingError):
            store.conn.execute("select 1")

    # ---------------------------------------------------------------- 正常路径

    def test_close_closes_the_lazily_created_store(self) -> None:
        panel = views.Panel(self.factory)
        store = panel.store()
        self.assertIsNotNone(store)
        self.assertEqual(panel.store_error, "")

        panel.close()

        self.assertTrue(panel.closed)
        self.assert_closed(store)
        self.assertEqual(len(self.made), 1, "close 不许顺手再建一个连接")

    def test_close_drops_the_only_strong_reference(self) -> None:
        # 刻意不走 `self.factory`：它会把 store 留在 `self.made` 里，那也是一条强引用
        # （这里要验的恰恰是「Panel 不持有」）。
        store = StateStore(os.path.join(self.tmp, "ref.db"))
        panel = views.Panel(lambda: store)
        panel.store()
        ref = weakref.ref(store)

        del store
        panel.close()
        gc.collect()

        self.assertIsNone(ref(), "close() 之后 Panel 不许再持有 store 的强引用")

    def test_context_manager_closes_too(self) -> None:
        with views.Panel(self.factory) as panel:
            store = panel.store()
        self.assertTrue(panel.closed)
        self.assert_closed(store)

    def test_close_runs_a_duck_typed_close_exactly_once(self) -> None:
        calls: List[int] = []

        class Duck:
            def close(self) -> None:
                calls.append(1)

        panel = views.Panel(Duck)
        panel.store()
        panel.close()
        panel.close()
        panel.close()
        self.assertEqual(calls, [1], "幂等：底层的 close() 只许被调一次")

    # ---------------------------------------------------------------- 幂等 / 边界

    def test_close_is_idempotent(self) -> None:
        panel = views.Panel(self.factory)
        panel.store()
        panel.close()
        panel.close()                       # 不许抛
        panel.close()
        self.assertTrue(panel.closed)

    def test_close_without_a_store_is_a_noop(self) -> None:
        panel = views.Panel(self.factory)
        panel.close()                       # 从没请求过 store
        self.assertTrue(panel.closed)
        self.assertEqual(self.made, [], "从没建过的 store 不该被「关」出一个来")

    def test_store_after_close_returns_none_and_builds_nothing(self) -> None:
        panel = views.Panel(self.factory)
        panel.store()
        panel.close()

        self.assertIsNone(panel.store(), "关闭之后不许复活一个没人会再关的连接")
        self.assertEqual(len(self.made), 1)
        self.assertIn("closed", panel.store_error)
        self.assertIn("closed", repr(panel))

    def test_blocks_degrade_instead_of_raising_after_close(self) -> None:
        panel = views.Panel(self.factory, now=lambda: T0)
        panel.store()
        panel.close()

        status = panel.status()
        self.assertEqual(status["level"], "alert")
        self.assertTrue(any("closed" in str(w) for w in status["warnings"]))
        self.assertEqual(panel.positions()["open_count"], 0)      # 0 值，不是 500
        self.assertTrue(panel.snapshot()["degraded"])

    def test_a_store_without_close_is_fine(self) -> None:
        panel = views.Panel(lambda: object())
        panel.store()
        panel.close()                       # 没有 close() 也不许抛
        self.assertTrue(panel.closed)

    def test_a_broken_close_does_not_raise(self) -> None:
        class Boom:
            def close(self) -> None:
                raise RuntimeError("cannot close")

        panel = views.Panel(Boom)
        panel.store()
        panel.close()                       # 关闭路径只记 WARNING
        self.assertTrue(panel.closed)

    def test_a_factory_that_raises_still_closes_cleanly(self) -> None:
        def boom() -> Any:
            raise RuntimeError("database is locked")

        panel = views.Panel(boom)
        self.assertIsNone(panel.store())
        panel.close()
        self.assertTrue(panel.closed)

    # ---------------------------------------------------------------- 所有权

    def test_owns_store_false_leaves_someone_elses_store_open(self) -> None:
        store = self.factory()
        self.addCleanup(store.close)
        panel = views.Panel(lambda: store, owns_store=False)
        panel.store()

        panel.close()

        store.conn.execute("select 1")      # 借用者的 close() 不许关别人的库
        self.assertFalse(store._closed)


if __name__ == "__main__":
    unittest.main()
