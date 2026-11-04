#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送文案（app/alerts.py）的离线单测：字段、对齐、缺失值口径。

模板与 HL 侧 `app/hl/executor.py::fmt_*_alert` 一致 —— 改这里的断言就要同时改那边。
"""
import datetime as dt
import unittest

from app import alerts


class EntryAlertTests(unittest.TestCase):
    def setUp(self):
        self.text = alerts.fmt_entry_alert(
            coin="SOL", side=-1, qty=0.117, entry_px=99.445, stop_px=102.306,
            notional=11.6, margin=2.3, liq=116.1, risk_u=1.33, equity=101.0,
            signal_px=99.3, oid="coi:1", stop_oid="coi:2",
            when=dt.datetime(2026, 11, 4, 20, 3, tzinfo=alerts.DISPLAY_TZ),
            leverage=5, isolated=True)

    def test_header(self):
        self.assertTrue(self.text.startswith("【Lighter】✅ 已开仓 · SOL 做空 5x 逐仓"))
        self.assertEqual(self.text.splitlines()[1], "")

    def test_fields_in_hl_order(self):
        labels = [ln.split("  ")[0].strip() for ln in self.text.splitlines()[2:]]
        self.assertEqual(labels, ["成交价", "数量", "止损价", "保证金", "强平价",
                                  "风险", "滑点", "时间", "单号"])

    def test_values(self):
        self.assertIn("99.445", self.text)
        self.assertIn("名义 11.6 U", self.text)
        self.assertIn("+2.88%", self.text)          # (102.306-99.445)/99.445
        self.assertIn("≈2.3 U", self.text)
        self.assertIn("116.100", self.text)
        self.assertIn("权益的 1.3%", self.text)
        self.assertIn("20:03", self.text)

    def test_missing_values_render_dash(self):
        text = alerts.fmt_entry_alert(
            coin="BTC", side=1, qty=0.001, entry_px=100.0, stop_px=98.0,
            notional=None, margin=None, liq=None, risk_u=None, equity=None,
            signal_px=None, leverage=None)
        self.assertIn("强平价", text)
        self.assertIn("—", text)
        self.assertNotIn("0.000000", text)


class ExitAlertTests(unittest.TestCase):
    def _p(self, **kw):
        base = dict(coin="SOL", side=-1, entry_px=99.445, exit_px=93.612,
                    reason="exit", net_pnl=6.96, fees=0.10, r_multiple=1.10,
                    opened_at=dt.datetime(2026, 10, 30, 5, 0).timestamp(),
                    when=dt.datetime(2026, 11, 4, 20, 3, tzinfo=alerts.DISPLAY_TZ))
        base.update(kw)
        return alerts.fmt_exit_alert(**base)

    def test_profit_header_and_reason(self):
        text = self._p()
        self.assertTrue(text.startswith("【Lighter】📈 策略止盈 · SOL 做空"))
        self.assertIn("离场原因  收盘站上 SMA20", text)
        self.assertIn("+6.96 U（+1.10R）", text)

    def test_loss_header(self):
        text = self._p(net_pnl=-3.2, r_multiple=-0.5)
        self.assertTrue(text.startswith("【Lighter】📉 策略离场"))

    def test_stop_reason(self):
        text = self._p(reason="stop", net_pnl=-4.0, r_multiple=-1.0)
        self.assertTrue(text.startswith("【Lighter】🛑 止损离场"))
        self.assertIn("止损单被触发", text)

    def test_long_reason_wording(self):
        self.assertIn("收盘跌破 SMA20", self._p(side=1))

    def test_pnl_breakdown(self):
        text = self._p()
        self.assertIn("价差 +7.06 U · 手续费 0.10 U", text)

    def test_holding_time(self):
        text = self._p()
        self.assertIn("持仓时长", text)
        self.assertIn("天", text)

    def test_settled_marker(self):
        text = self._p(settled=True)
        self.assertIn("（离线结案）", text.splitlines()[0])

    def test_unknown_pnl_is_dash(self):
        text = self._p(net_pnl=None, r_multiple=None, fees=None)
        self.assertIn("本次盈亏  —", text)


class LiquidationAlertTests(unittest.TestCase):
    def test_template(self):
        text = alerts.fmt_liquidation_alert(coin="ETH", side=1, entry_px=3000.0,
                                            liq_px=2900.0, qty=0.01, loss_u=-1.0)
        self.assertTrue(text.startswith("【Lighter】💥 强制平仓"))
        self.assertIn("强平价", text)


class AlignmentTests(unittest.TestCase):
    def test_value_column_is_fixed(self):
        """两列模板：值一律从第 _COL 个显示宽度开始（中文按 2 计）。"""
        for label in ("时间", "成交价", "保证金", "强平价"):
            row = alerts._row(label, "X")
            head = row[:row.index("X")]
            self.assertEqual(alerts._disp_width(head), alerts._COL, label)

    def test_no_tab_or_markdown(self):
        text = alerts.fmt_exit_alert(coin="SOL", side=1, entry_px=1.0, exit_px=1.0,
                                    reason="exit", net_pnl=0.0, fees=0.0,
                                    r_multiple=0.0, opened_at=None)
        self.assertNotIn("\t", text)
        self.assertNotIn("**", text)


if __name__ == "__main__":
    unittest.main()
