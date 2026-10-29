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
import html
import gc
import os
import pathlib
import re
import sqlite3
import tempfile
import unittest
import unittest.mock
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
    """交易所侧：BTC 止损单在、数量覆盖（1.0 = 仓位的 1.0）。

    ⚠️ 这是**旧形态**（price/qty）。生产上 `views.Sources.orders` 喂的是
    `ExchangeOrder.as_dict()`（只有 int 形态 + 触发价，没有 price/qty）——
    只用这一份夹具测，会把「面板永远报止损没挂」的 bug 全绿放过（线上实测踩过）。
    生产形态见 `prod_orders()`。
    """
    return [{"symbol": "BTC", "price": 6300.0, "qty": 1.0, "order_id": "stop-1",
             "reduce_only": True}]


def prod_orders(trigger_int: int = 630_000, price_int: int = 629_400,
                qty_int: int = 100_000, reduce_only: bool = True,
                coi: int = 777_000_111, symbol: str = "BTC",
                pending_confirm: bool = False) -> List[Dict[str, Any]]:
    """**生产形态**：`ExchangeOrder.as_dict()` 的真实键（实测自 oracle1 的活挂单）。"""
    return [{"order_id": "1125898605399395", "client_order_index": coi, "market_id": 1,
             "symbol": symbol, "reduce_only": reduce_only,
             "trigger_price_int": trigger_int, "trigger_price_raw": f"{trigger_int/100:.3f}",
             "price_int": price_int, "qty_int": qty_int, "status": "pending",
             "pending_confirm": pending_confirm}]


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
        self.assertTrue(all(m.split(".")[0] in ("app", "__future__", "bisect", "html",
                                                "json", "logging", "math", "time",
                                                "dataclasses", "decimal", "typing")
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


class DataSourceProbeTests(PanelFixture):
    """`/status` 的「数据源新鲜度」不许自己发网络请求。

    实测（2026-10-28）：容器重启后第一次 `/status` 要 24s —— 因为状态条为了拿一个时间戳，
    经 `sources.bars` 把 3 标的 × 8 页 K 线全拉了一遍。现在有 `cached_bars` 时只读缓存，
    缓存没铺满就退回「最后一个权益点」的口径。
    """

    def _bars(self, end_s: float, count: int = 3):
        from app.strategy import Bar

        return [Bar(open_ms=int((end_s - (count - i) * BAR_S) * 1000), open=100.0, high=101.0,
                    low=99.0, close=100.0, volume=1.0) for i in range(count)]

    def test_status_never_calls_the_network_probe(self) -> None:
        def boom(_symbol):
            raise AssertionError("状态条不该为新鲜度去拉 K 线")

        sources = views.Sources(bars=boom, cached_bars=lambda s: [])
        panel = self.panel(sources=sources)
        payload = panel.status()                       # 不抛 = 没走网络路径
        self.assertEqual(payload["data_source"]["name"], "equity_curve")

    def test_status_prefers_cached_bars(self) -> None:
        bars = self._bars(self.now - BAR_S)
        sources = views.Sources(bars=lambda s: None, cached_bars=lambda s: bars)
        payload = self.panel(sources=sources).status()
        self.assertEqual(payload["data_source"]["name"], "bar")
        self.assertLessEqual(payload["data_source"]["age_s"], BAR_S + 1)


class HtmlTests(PanelFixture):
    def _html(self) -> str:
        return self.seeded_panel().page()

    def test_homepage_contains_its_blocks_only(self) -> None:
        """首页 ①–⑤（+人工操作）；事件流与订单明细在记录页。"""
        html = self._html()
        for name in views.HOMEPAGE_BLOCKS:
            with self.subTest(block=name):
                self.assertIn(f'id="block-{name}"', html)
        self.assertNotIn('id="block-events"', html)
        self.assertNotIn('id="block-records"', html)
        self.assertIn('href="/records"', html)                    # 首页有入口

    def test_records_page_has_its_blocks(self) -> None:
        html = self.seeded_panel().records_page()
        for name in views.RECORDS_BLOCKS:
            with self.subTest(block=name):
                self.assertIn(f'id="block-{name}"', html)
        self.assertIn('id="funds"', html)                         # 资金概览
        self.assertIn("成交后余额", html)                          # 用户要的那一列
        self.assertIn('href="/"', html)                           # 返回概览

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

    def test_theme_follows_the_system_without_a_toggle(self) -> None:
        """明暗跟随系统：只有 prefers-color-scheme，没有按钮、没有 localStorage 偏好。

        用户明确要求「不用给单独的按钮」——所以这条测试同时钉住两件事：
        ① 亮色覆盖存在；② 页面上没有任何主题切换控件。
        """
        html = self._html()
        self.assertIn("@media (prefers-color-scheme: light)", html)
        self.assertIn("color-scheme: dark light", html)
        # 颜色全部走变量（图表也是），否则亮色下会出现黑底图表/看不清的灰字
        self.assertIn("var(--chart-bg)", html)
        self.assertIn("var(--panel)", html)
        for forbidden in ("btn-theme", "data-theme", "toggleTheme", "prefers-dark",
                          'name="theme"'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, html)

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
        html = self.seeded_panel().records_page()                 # 事件流在记录页
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;script&gt;", html)

    def test_html_shows_key_numbers(self) -> None:
        html = self._html()
        self.assertIn("1,450.00", html)                 # 权益
        self.assertIn("-50.00", html)                   # 累计盈亏
        self.assertIn("持仓中", html)
        self.assertIn("已覆盖", html)

    def test_html_renders_with_no_data(self) -> None:
        """空库（0 值）也得出页面 —— 降级路径必须被渲染过。"""
        panel = self.panel()
        html = panel.page()
        for name in views.HOMEPAGE_BLOCKS:
            self.assertIn(f'id="block-{name}"', html)
        self.assertIn("数据不足", html)                  # 曲线占位文案
        records = panel.records_page()                   # 记录页空库也要能开
        self.assertIn('id="block-records"', records)
        self.assertIn("还没有成交记录", records)


class RecordsTests(PanelFixture):
    """记录页：订单明细 + 「成交后余额」+ 资金概览（数据全部来自库，零网络）。"""

    def _seed_trades(self):
        store = self.store
        # 两个权益点：T0+1h 1450，T0+2h 1500
        store.record_equity(1450.0, ts=T0 + 3600, source="exchange")
        store.record_equity(1500.0, ts=T0 + 7200, source="exchange")
        # 开仓成交（在第一个权益点之前）→ 余额应取 1450（滞后 30 分钟）
        store.record_trade(symbol="BTC", side=1, price=6500.0, qty=1.0, kind="entry",
                           fee=0.5, fill_id="f1", ts=T0 + 1800)
        store.record_trade(symbol="BTC", side=1, price=6600.0, qty=1.0, kind="exit",
                           fee=0.6, realized_pnl=99.0, fill_id="f2", ts=T0 + 5400)

    def test_records_block_has_rows_kinds_and_equity_after(self) -> None:
        self._seed_trades()
        block = self.panel().records()
        self.assertEqual(block["count"], 2)
        newest, oldest = block["trades"][0], block["trades"][1]     # 新→旧
        self.assertEqual(newest["kind_text"], "离场")
        self.assertEqual(oldest["kind_text"], "开仓")
        self.assertEqual((oldest["side_text"], newest["side_text"]), ("多", "多"))
        self.assertAlmostEqual(oldest["notional"], 6500.0, places=6)
        # 「成交后余额」= 该笔之后最近的权益快照，并带上滞后
        self.assertAlmostEqual(newest["equity_after"], 1500.0, places=6)
        self.assertAlmostEqual(newest["equity_after_lag_s"], 1800.0, places=3)
        self.assertAlmostEqual(oldest["equity_after"], 1450.0, places=6)

    def test_equity_after_is_none_when_no_later_snapshot(self) -> None:
        self.store.record_equity(1000.0, ts=T0, source="exchange")
        self.store.record_trade(symbol="ETH", side=-1, price=2500.0, qty=0.5, kind="entry",
                                fill_id="f3", ts=T0 + 60)
        row = self.panel().records()["trades"][0]
        self.assertIsNone(row["equity_after"])                     # 不许拿更早的权益冒充
        self.assertEqual(row["side_text"], "空")

    def test_funds_summary_matches_the_store(self) -> None:
        self._seed_trades()
        funds = self.panel().records()["funds"]
        self.assertAlmostEqual(funds["equity"], 1500.0, places=6)
        self.assertEqual(funds["trade_count"], 2)
        self.assertAlmostEqual(funds["fee_total"], 1.1, places=6)
        self.assertAlmostEqual(funds["realized_total"], 99.0, places=6)

    def test_records_page_shows_the_balance_column(self) -> None:
        self._seed_trades()
        html = self.panel().records_page()
        self.assertIn("成交后余额", html)
        self.assertIn("1,500.00", html)
        self.assertIn("离场", html)


# --------------------------------------------------------------------------- 5. 降级


class StopCoverageTests(PanelFixture):
    """「止损覆盖」的判据必须能吃**生产形态**的挂单（`ExchangeOrder.as_dict()`）。

    2026-10-29 线上实测的假警报：交易所明明挂着一张 SOL 止损（trigger 105.257、
    reduce_only、剩余 1.191，status=pending），面板却报「交易所没有这张止损单」。
    根因是 `as_dict()` 只有 `price_int`/`qty_int`/`trigger_price_int`，而面板按
    `price`/`qty` 取值 → 恒为 None → 每一张真实止损单都被跳过。
    """

    def _stops(self, orders: List[Dict[str, Any]], *, store_seed: bool = True) -> Dict[str, Any]:
        if store_seed:
            seed(self.store)
        panel = self.panel(sources=views.Sources(orders=lambda: orders))
        return panel.stops()

    def test_production_shape_is_recognised_by_trigger_price(self) -> None:
        block = self._stops(prod_orders())
        self.assertTrue(block["all_protected"], block["warnings"])
        row = block["stops"][0]
        self.assertEqual(row["exchange_status"], "present")
        self.assertEqual(row["note"], "已覆盖")
        # 数量从 qty_int + size_decimals 换算回 base 单位（100000 / 10^5 = 1.0）
        self.assertAlmostEqual(row["order_qty"], 1.0)

    def test_match_by_client_order_index_wins(self) -> None:
        """coi 命中时连触发价都不用比（老仓位/换价重挂都靠它）。"""
        seed(self.store)
        self.store.conn.execute("UPDATE positions SET stop_order_id='coi:777000111'")
        self.store.conn.commit()
        block = self.panel(sources=views.Sources(orders=lambda: prod_orders(trigger_int=630_500))).stops()
        self.assertEqual(block["stops"][0]["exchange_status"], "present")

    def test_limit_price_alone_is_not_a_stop(self) -> None:
        """只有 `price_int`（触发后最差成交价）不算止损 —— 不能退回按它比。"""
        orders = [{"symbol": "BTC", "reduce_only": True, "price_int": 630_000,
                   "qty_int": 100_000, "client_order_index": 1, "market_id": 1}]
        block = self._stops(orders)
        self.assertEqual(block["stops"][0]["exchange_status"], "missing")
        self.assertIn("没有", block["stops"][0]["note"])

    def test_one_tick_off_still_matches_five_ticks_does_not(self) -> None:
        seed(self.store)
        near = self.panel(sources=views.Sources(orders=lambda: prod_orders(trigger_int=630_001))).stops()
        self.assertEqual(near["stops"][0]["exchange_status"], "present")
        far = self.panel(sources=views.Sources(orders=lambda: prod_orders(trigger_int=630_005))).stops()
        self.assertEqual(far["stops"][0]["exchange_status"], "missing")

    def test_trigger_raw_string_is_converted_with_price_decimals(self) -> None:
        """解析层拿不到 decimals 时 `trigger_price_int` 是 None，只用原始串也要能比。"""
        orders = [{"symbol": "BTC", "reduce_only": True, "trigger_price_raw": "6300.00",
                   "qty_int": 100_000, "client_order_index": 1, "market_id": 1}]
        block = self._stops(orders)
        self.assertEqual(block["stops"][0]["exchange_status"], "present")

    def test_partial_size_and_non_reduce_only_are_flagged(self) -> None:
        seed(self.store)
        partial = self.panel(sources=views.Sources(
            orders=lambda: prod_orders(qty_int=50_000))).stops()
        self.assertEqual(partial["stops"][0]["exchange_status"], "partial")
        bad = self.panel(sources=views.Sources(
            orders=lambda: prod_orders(reduce_only=False))).stops()
        self.assertEqual(bad["stops"][0]["exchange_status"], "mismatch")

    def test_pending_confirm_note_says_awaiting_next_read(self) -> None:
        seed(self.store)
        block = self.panel(sources=views.Sources(
            orders=lambda: prod_orders(pending_confirm=True))).stops()
        self.assertEqual(block["stops"][0]["exchange_status"], "present")
        self.assertIn("下一轮读数复核", block["stops"][0]["note"])


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
        panel = self._broken()
        html = panel.page()
        for name in views.HOMEPAGE_BLOCKS:
            self.assertIn(f'id="block-{name}"', html)
        records = panel.records_page()                   # 记录页降级不崩
        self.assertIn('id="block-records"', records)
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

    # ---- 2026-10-28：距触发线 / 破线后止损 / 结论去重（用户拍板）
    def test_row_exposes_distances_and_stop_preview(self) -> None:
        """距上/下轨与破线后止损必须是可复算的派生值（不参与判定，只喂展示）。"""
        bars = flat_bars(1_740_000_000_000, 420, 100.0)
        panel = views.Panel(lambda: self.store,
                            sources=views.Sources(bars=lambda s: bars,
                                                  prices=lambda: {"BTC": 100.0}),
                            now=lambda: T0)
        row = panel.symbols_block()["symbols"][0]
        for key in ("dist_long_pct", "dist_short_pct", "ma200_gap_pct",
                    "stop_long_pct", "stop_short_pct", "verdict",
                    "long_enabled", "short_enabled", "held_side"):
            self.assertIn(key, row)
        self.assertAlmostEqual(row["dist_long_pct"],
                               (row["ma_max"] - row["price"]) / row["price"], places=12)
        self.assertAlmostEqual(row["stop_long_pct"],
                               (row["price"] - row["ma_min"]) / row["price"], places=12)
        self.assertTrue(row["long_enabled"] and row["short_enabled"])

    def test_direction_switch_reaches_explain_entry(self) -> None:
        """关掉做多后，面板不得再判「可开多」（与执行器同一份 sides）。"""
        bars = flat_bars(1_740_000_000_000, 420, 100.0)
        panel = views.Panel(lambda: self.store,
                            sources=views.Sources(bars=lambda s: bars),
                            sides=(1,), now=lambda: T0)
        row = panel.symbols_block()["symbols"][0]
        self.assertFalse(row["short_enabled"])
        self.assertTrue(row["long_enabled"])

    def test_html_carries_the_new_columns_and_no_duplicate_numbers(self) -> None:
        bars = flat_bars(1_740_000_000_000, 420, 100.0)
        panel = views.Panel(lambda: self.store,
                            sources=views.Sources(bars=lambda s: bars,
                                                  prices=lambda: {"BTC": 100.0}),
                            now=lambda: T0)
        block = panel.symbols_block()
        page = views._symbols_html(block)
        for head in ("距上轨（开多）", "距下轨（开空）", "MA200（做空）", "破线后止损", "结论"):
            self.assertIn(head, page)
        self.assertNotIn("6MA 带宽%", page)          # 三列合并进「密集」一格
        self.assertNotIn("连续密集根数", page)        # 根数在「密集」里，不再单列
        row = block["symbols"][0]
        self.assertIn(html.escape(row["verdict"]), page)


class SymbolVerdictTests(unittest.TestCase):
    """结论列：只说还差什么，不许复述数字（用户明确要求）。"""

    def _row(self, **kw: Any) -> dict:
        base = {"held_side": 0, "long_enabled": True, "short_enabled": True,
                "fired": False, "side": 0, "blocked_by": "squeeze",
                "gap_pct": 0.004, "dense_bars": 0, "min_dense_bars": 6,
                "dist_long_pct": 0.01, "dist_short_pct": 0.03,
                "stop_long_ok": True, "stop_short_ok": True, "below_ma200": True}
        base.update(kw)
        return base

    def _verdict(self, **kw: Any) -> str:
        return views._symbol_verdict(self._row(**kw))

    def test_squeeze_states(self) -> None:
        self.assertEqual(self._verdict(gap_pct=0.004), "等密集（带宽未进门槛）")
        self.assertEqual(self._verdict(gap_pct=-0.002, dense_bars=3), "等密集根数")
        self.assertEqual(self._verdict(gap_pct=-0.002, dense_bars=6), "等密集")

    def test_breakout_picks_the_nearer_viable_side(self) -> None:
        self.assertEqual(self._verdict(blocked_by="breakout", gap_pct=-0.002, dense_bars=6),
                         "等破上轨")                       # 0.01 < 0.03
        self.assertEqual(self._verdict(blocked_by="breakout", gap_pct=-0.002, dense_bars=6,
                                       dist_long_pct=0.05, dist_short_pct=0.01), "等破下轨")

    def test_breakout_respects_ma200_and_stop(self) -> None:
        # 空单止损不合格 → 只剩上轨这一个方向可等
        self.assertEqual(self._verdict(blocked_by="breakout", stop_short_ok=False), "等破上轨")
        # 空单在 MA200 上方也不行
        self.assertEqual(self._verdict(blocked_by="breakout", below_ma200=False), "等破上轨")
        # 多空都不合格 → 说明白
        self.assertEqual(self._verdict(blocked_by="breakout", stop_long_ok=False,
                                       stop_short_ok=False),
                         "等破线（另一侧前置条件未满足）")

    def test_fired_and_held_and_disabled(self) -> None:
        self.assertEqual(self._verdict(fired=True, side=1), "可开多")
        self.assertEqual(self._verdict(fired=True, side=-1), "可开空")
        self.assertEqual(self._verdict(held_side=1), "持仓中")
        self.assertEqual(self._verdict(long_enabled=False, short_enabled=False),
                         "多空双向都已关闭")

    def test_never_repeats_column_numbers(self) -> None:
        """结论里不允许出现数字：带宽/根数/百分比都在前面的列里。"""
        cases = [
            self._verdict(gap_pct=0.004),
            self._verdict(gap_pct=-0.002, dense_bars=3),
            self._verdict(blocked_by="breakout"),
            self._verdict(blocked_by="regime"),
            self._verdict(blocked_by="stop"),
            self._verdict(fired=True, side=1),
        ]
        for text in cases:
            # MA200 是线名不是「被复述的数字」，先剔掉再查
            self.assertFalse(any(ch.isdigit() for ch in text.replace("MA200", "")), text)


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


# --------------------------------------------------------------------------- 出入金跳变提示 + 指标缓存


class UnexplainedJumpTests(unittest.TestCase):
    """`_unexplained_equity_jumps`（审计 P2-3 的纯函数）：跳变 × 无成交 × 无流水覆盖。"""

    def _points(self, rows):
        return [{"ts": ts, "equity": eq, "cum_flow": cum} for ts, eq, cum in rows]

    def test_big_jump_without_fills_or_flow_is_flagged(self) -> None:
        # 101.0089 → 500.0089（+399，事故原样），区间无成交、cum_flow 不变 → 提示
        pts = self._points([(T0, 101.0089, 0.0), (T0 + 1800, 500.0089, 0.0)])
        jumps = views._unexplained_equity_jumps(pts, [])
        self.assertEqual(len(jumps), 1)
        self.assertAlmostEqual(jumps[0]["delta"], 399.0, places=6)
        self.assertEqual(jumps[0]["ts"], T0 + 1800)

    def test_jump_covered_by_flow_is_not_flagged(self) -> None:
        # 同样的跳变，但 cum_flow 同步 +399（流水已入账）→ 不提示
        pts = self._points([(T0, 101.0089, 0.0), (T0 + 1800, 500.0089, 399.0)])
        self.assertEqual(views._unexplained_equity_jumps(pts, []), [])

    def test_jump_with_a_fill_in_interval_is_not_flagged(self) -> None:
        # 区间内有一笔成交 → 可能是盈亏，不提示（fail-quiet，与推断同一方向）
        pts = self._points([(T0, 100.0, 0.0), (T0 + 1800, 400.0, 0.0)])
        self.assertEqual(views._unexplained_equity_jumps(pts, [T0 + 900]), [])

    def test_jump_covered_by_a_backfilled_flow_is_not_flagged(self) -> None:
        # 补录过的流水在当前锚点之前（cum_flow 列看不到）→ 靠 account_flows 的
        # 时间戳判覆盖，不许误报（2026-10-28 线上实测的第一版就栽在这）
        pts = self._points([(T0, 101.0089, 0.0), (T0 + 1800, 500.0089, 0.0)])
        self.assertEqual(
            views._unexplained_equity_jumps(pts, [], [T0 + 900]), [])

    def test_small_swing_is_not_flagged(self) -> None:
        # 5U 阈值以下（含 5% 比例）的波动 → 静默：持仓浮盈浮亏 30 分钟内常见
        pts = self._points([(T0, 500.0, 0.0), (T0 + 1800, 503.0, 0.0)])
        self.assertEqual(views._unexplained_equity_jumps(pts, []), [])

    def test_latest_first_and_capped_at_three(self) -> None:
        pts = []
        for k in range(6):                       # 6 次跳变，每次 +100
            pts.append((T0 + k * 1800, 100.0 + k * 100.0, 0.0))
        jumps = views._unexplained_equity_jumps(self._points(pts), [])
        self.assertEqual(len(jumps), 3)
        self.assertEqual([j["ts"] for j in jumps],
                         [T0 + 5 * 1800, T0 + 4 * 1800, T0 + 3 * 1800])

    def test_records_block_surfaces_the_warning(self) -> None:
        store = make_store(self.tmp)
        self.addCleanup(store.close)
        store.record_equity(101.0089, ts=T0, source="exchange")
        store.record_equity(500.0089, ts=T0 + 1800, source="exchange")
        panel = views.Panel(lambda: store, now=lambda: T0 + 3600.0)
        block = panel.records()
        self.assertTrue(any("跳变" in w and "backfill_flow" in w
                            for w in block["warnings"]))
        self.assertEqual(len(block["funds"]["unexplained_jumps"]), 1)
        self.assertIn("跳变", panel.records_page())            # HTML 里可见

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.store = make_store(self.tmp)
        self.addCleanup(self.store.close)


class SymbolIndicatorsCacheTests(unittest.TestCase):
    """`_indicators_for`：同一代 bar 只算一次，换代重算（现价列不受缓存影响）。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = make_store(tmp.name)
        self.addCleanup(self.store.close)
        self.calls = {"n": 0}
        real = strategy.compute_indicators

        def counting(bars):
            self.calls["n"] += 1
            return real(bars)

        self._patcher = unittest.mock.patch.object(strategy, "compute_indicators",
                                                   side_effect=counting)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_same_generation_computes_once(self) -> None:
        from tests.test_admin import flat_bars as admin_flat_bars
        bars = admin_flat_bars(1_740_000_000_000, 300, 100.0)
        panel = views.Panel(lambda: self.store,
                            sources=views.Sources(bars=lambda symbol: bars,
                                                  prices=lambda: {"BTC": 100.0}),
                            now=lambda: 1_740_000_000_000 / 1000.0)
        panel.symbols_block()
        panel.symbols_block()
        panel.symbols_block()
        self.assertEqual(self.calls["n"], 3)             # 三次请求 × 三标的，各算一次

        newer = admin_flat_bars(1_740_000_000_000 + BAR_MS, 300, 100.0)
        panel.sources.bars = lambda symbol: newer        # bar 换代
        panel.symbols_block()
        self.assertEqual(self.calls["n"], 6)             # 换代后重算
        # 缓存只留当前一代（三标的各一条）
        self.assertEqual(len(panel._symbol_ind_cache), 3)


if __name__ == "__main__":
    unittest.main()
