"""MR 腿（QQQ）在面板上的显示契约 —— 全离线（临时库 + 假 `Sources.mr`）。

钉住（2026-11-02 方案 panel-design-20261102.md 第五节验收项）：

1. **持仓**：仓位表出现 QQQ 行，入场价/现价/浮盈/离场线/距离场% 全部来自 `status.mr`，
   且止损两列显示 `—`（无止损腿，不能留个 0.00 让人误读）。
2. **启用但空仓**：仍有一行（用户拍板"启用后常驻"），`position_id` 为空 ⇒ 开仓价/数量显示 `—`，
   状态列给出相位文案（等窗口 / 等开盘 / 窗口已过）。
3. **停用且空仓**：整行不出现（正常态静默）。
4. **对账不平**：状态列「对账不平」，并进 warnings。
5. **标的动态**：`symbols_block()["mr"]` 带 rsi2/离场线/距离场%，HTML 里出现独立的
   「超跌腿」子表（不与 crypto 的列混用）。
6. 缺字段一律 `—`，不猜（rsi2/离场线缺失时不得渲染成 0.00）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any, Dict, Optional

from app import views
from app.risk import RiskLimits
from app.store import StateStore

T0 = 1_760_000_000.0


def make_store(tmp: str) -> StateStore:
    return StateStore(os.path.join(tmp, "state.db"))


def mr_status(*, enabled: bool = True, holding: bool = False, recon: str = "ok",
              phase: str = "in_window", with_signal: bool = True) -> Dict[str, Any]:
    """一份与 `MrLeg._on_tick_inner` 同形的 status（字段名逐字对齐 app/mr.py）。"""
    st: Dict[str, Any] = {"enabled": enabled, "state": "holding" if holding else "idle",
                          "recon": recon, "last_session_date": "2026-10-30",
                          "sessions_usable": 15, "mark": 697.0}
    if with_signal:
        st["signal"] = {
            "last_session_date": "2026-10-30", "rsi2": 8.48, "threshold": 10.0,
            "entry_flag": True, "exit_flag": False, "exit_line": 704.21,
            "sessions_usable": 15, "next_eval_et": "2026-11-03T16:00:00-05:00",
            "window": {"date": "2026-11-02", "start_et": "09:30", "deadline_et": "12:30"},
            "phase": phase,
        }
    if holding:
        st["pos"] = {
            "signal_date": "2026-10-30", "entry_session": "2026-11-02",
            "qty": 0.3568, "entry_price": 697.10, "notional": 248.7,
            "entry_ts": T0 - 6 * 3600, "upnl": 0.46, "funding_paid": 0.01,
            "mark": 698.40, "upnl_pct": 0.1864,
            "exit_line": 704.21 if with_signal else None,
            "dist_to_exit_pct": 0.8318 if with_signal else None, "liq_price": None,
            "next_eval_et": "2026-11-03T16:00:00-05:00",
        }
    return st


class MrPanelFixture(unittest.TestCase):
    now = T0

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = make_store(tmp.name)
        self.addCleanup(self.store.close)

    def panel(self, mr: Optional[Dict[str, Any]]) -> views.Panel:
        return views.Panel(lambda: self.store,
                           sources=views.Sources(mr=(lambda: mr) if mr is not None else None),
                           limits=RiskLimits(risk_pct=0.0133), now=lambda: self.now)

    def html_page(self, mr: Optional[Dict[str, Any]]) -> str:
        return views.render_html(self.panel(mr).snapshot())

    def rows(self, mr: Optional[Dict[str, Any]]):
        return self.panel(mr).positions()["positions"]


class MrPositionRowTests(MrPanelFixture):
    def test_holding_row_shows_price_pnl_exit_line(self) -> None:
        row = [r for r in self.rows(mr_status(holding=True)) if r.get("is_mr")][0]
        self.assertEqual(row["symbol"], "QQQ")
        self.assertEqual(row["position_id"], "MR")
        self.assertEqual(row["entry_price"], 697.10)
        self.assertEqual(row["price"], 698.40)
        self.assertAlmostEqual(row["upnl"], 0.46, places=6)
        self.assertAlmostEqual(row["upnl_pct"], 0.001864, places=9)   # 0.1864% → 分数
        self.assertEqual(row["exit_price"], 704.21)
        self.assertAlmostEqual(row["dist_to_exit_pct"], 0.008318, places=9)
        self.assertEqual(row["notional"], 248.7)
        self.assertEqual(row["margin"], 248.7)                       # 1x 逐仓
        self.assertTrue(row["no_stop"])
        self.assertIsNone(row["liq_price"])
        self.assertEqual(row["holding_text"], "6.0 小时前")            # 6 小时前开仓

    def test_holding_html_has_badge_and_dashes_for_stop(self) -> None:
        page = self.html_page(mr_status(holding=True))
        self.assertIn("QQQ", page)
        self.assertIn("badge-mr", page)
        self.assertIn("704.21", page)
        self.assertIn("698.40", page)
        # 无止损腿：QQQ 行的止损两列必须是 —，不能是 0.00
        qqq = page.split("QQQ <span class='badge-mr'>MR</span>", 1)[1][:260]
        self.assertIn("<td>—</td><td>—</td>", qqq)

    def test_flat_has_no_position_row(self) -> None:
        """空仓不进仓位表（2026-11-02 用户拍板：无仓位不显示），状态看动态卡。"""
        self.assertEqual([r for r in self.rows(mr_status(phase="signal_ready"))
                          if r.get("is_mr")], [])
        mr = self.panel(mr_status(phase="signal_ready")).symbols_block()["mr"]
        self.assertEqual(mr["status"], "信号已出，等 2026-11-02 开盘")

    def test_disabled_flat_row_absent(self) -> None:
        self.assertEqual([r for r in self.rows(mr_status(enabled=False))
                          if r.get("is_mr")], [])

    def test_recon_err_is_flagged(self) -> None:
        row = [r for r in self.rows(mr_status(holding=True, recon="err"))
               if r.get("is_mr")][0]
        self.assertEqual(row["status"], "对账不平")
        self.assertTrue(row["warnings"])

    def test_phase_texts(self) -> None:
        for phase, text in (("in_window", "窗口内等成交"), ("expired", "窗口已过，等明日"),
                            ("no_signal", "空仓"), ("no_data", "无数据")):
            mr = self.panel(mr_status(phase=phase)).symbols_block()["mr"]
            self.assertEqual(mr["status"], text, phase)

    def test_missing_signal_fields_render_dashes(self) -> None:
        row = [r for r in self.rows(mr_status(holding=True, with_signal=False))
               if r.get("is_mr")][0]
        self.assertIsNone(row["exit_price"])          # 不猜 0.0
        self.assertIsNone(row["dist_to_exit_pct"])
        self.assertIn("—", self.html_page(mr_status(holding=True, with_signal=False)))


class MrDynamicsTests(MrPanelFixture):
    def test_symbols_block_carries_mr(self) -> None:
        block = self.panel(mr_status(holding=True)).symbols_block()
        mr = block["mr"]
        self.assertEqual(mr["symbol"], "QQQ")
        self.assertTrue(mr["holding"])
        self.assertAlmostEqual(mr["dist_to_entry_pct"], (698.40 / 697.10 - 1) * 100, places=6)
        self.assertEqual(mr["rsi2"], 8.48)
        self.assertEqual(mr["exit_line"], 704.21)
        self.assertAlmostEqual(mr["dist_to_exit_pct"], 0.8318, places=6)

    def test_flat_still_reports_rsi_and_exit_line(self) -> None:
        mr = self.panel(mr_status()).symbols_block()["mr"]
        self.assertFalse(mr["holding"])
        self.assertIsNone(mr["dist_to_entry_pct"])
        self.assertEqual(mr["rsi2"], 8.48)
        self.assertEqual(mr["exit_line"], 704.21)

    def test_html_has_second_leg_subtable(self) -> None:
        page = self.html_page(mr_status(holding=True))
        self.assertIn("超跌腿", page)
        self.assertIn("mr-row", page)
        self.assertIn("RSI2", page)

    def test_disabled_no_subtable(self) -> None:
        page = self.html_page(mr_status(enabled=False))
        self.assertNotIn("超跌腿", page)


class MrWindowTests(unittest.TestCase):
    """入场窗口文案：09:30 ET 起算 3 小时 = 12:30（曾算成 12:00）。"""

    def test_entry_window_deadline(self) -> None:
        from app import mr, mr_signal
        ss = [mr_signal.SessionBar(date=d, open=c, high=c, low=c, close=c, bars=6,
                                   last_bar_ms=0, last_bar_et="15:00")
              for d, c in zip(["2026-10-26", "2026-10-27", "2026-10-28",
                               "2026-10-29", "2026-10-30"],
                              [95.0, 94.0, 93.0, 92.0, 88.0])]
        win = mr._entry_window(ss, 1793630100000)      # 2026-11-02 09:35 ET
        self.assertEqual(win["date"], "2026-11-02")
        self.assertEqual(win["start_et"], "09:30")
        self.assertEqual(win["deadline_et"], "12:30")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
