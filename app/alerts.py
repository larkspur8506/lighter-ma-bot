#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送文案（与 HL 侧 `app/hl/executor.py::fmt_*_alert` 同一套模板）。

模板形状（两边必须一致，改一边就要改另一边）::

    【Lighter】📈 策略止盈 · SOL 做空

    离场原因  收盘站上 SMA20
    平仓价    93.612（开仓 99.445）
    本次盈亏  +1.43 U（+1.07R）
    盈亏构成  价差 +1.43 U · 手续费 0.00 U
    持仓时长  5 天 15 小时
    时间      11-04 20:03 北京时间

约定：
- 标题行「【场馆】+ emoji + 结论 + 标的 + 方向」，其后固定一个空行；
- 正文一律 **两列**：`字段名` 补到 6 个显示宽度（中文按 2 计）后接值，
  这样手机等宽字体里数字能对齐；不写表格符、不写 markdown；
- 算不出来的数字写 `—`，**绝不把 None 打成 0**；
- 时间统一用 DISPLAY_TZ（默认北京时间），与 HL 一致。
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any, Optional

#: 展示时区：与 HL 侧同源（`DISPLAY_TZ_OFFSET`，默认 +8）
DISPLAY_TZ = dt.timezone(dt.timedelta(hours=float(os.environ.get("DISPLAY_TZ_OFFSET", "8") or 8)))
TZ_LABEL = os.environ.get("DISPLAY_TZ_LABEL", "北京时间")

_HEADER = "【Lighter】"

#: 两列对齐用的字段列宽（显示宽度：中日韩字符算 2）
_COL = 10


def _disp_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _row(label: str, value: str) -> str:
    pad = max(2, _COL - _disp_width(label))
    return f"{label}{' ' * pad}{value}"


def _fmt_u(x: Any, nd: int = 2) -> str:
    """金额/价格：千分位、固定小数；None → `—`。"""
    try:
        if x is None:
            return "—"
        return f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_signed(x: Any, nd: int = 2) -> str:
    try:
        if x is None:
            return "—"
        return f"{float(x):+,.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _side_word(side: Any) -> str:
    try:
        return "做多" if int(side) >= 0 else "做空"
    except (TypeError, ValueError):
        return "—"


def _fmt_duration(seconds: Any) -> str:
    """持仓时长：分钟 / 小时 / 天+小时（与 HL 同口径）。"""
    try:
        mins = max(0, int(float(seconds) // 60))
    except (TypeError, ValueError):
        return ""
    if mins >= 1440:
        days, hours = mins // 1440, mins % 1440 // 60
        return f"{days} 天" + (f" {hours} 小时" if hours else "")
    if mins >= 60:
        hours, minutes = mins // 60, mins % 60
        return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")
    return f"{mins} 分钟"


def _stamp(when: Optional[dt.datetime]) -> str:
    when = when or dt.datetime.now(DISPLAY_TZ)
    return f"{when.strftime('%m-%d %H:%M')} {TZ_LABEL}"


def fmt_entry_alert(*, coin: str, side: int, qty: float, entry_px: float,
                    stop_px: float, notional: Optional[float], margin: Optional[float],
                    liq: Optional[float], risk_u: Optional[float], equity: Optional[float],
                    signal_px: Optional[float], oid: Any = None, stop_oid: Any = None,
                    when: Optional[dt.datetime] = None, leverage: Optional[float] = None,
                    isolated: bool = True, dry_run: bool = False) -> str:
    """开仓回报（字段顺序与 HL 完全一致）。"""
    lev = f"{float(leverage):g}x" if leverage else "—"
    mode = " 逐仓" if isolated else ""
    stop_pct = None
    try:
        if entry_px and stop_px:
            stop_pct = (float(stop_px) - float(entry_px)) / float(entry_px) * 100.0
    except (TypeError, ValueError):
        stop_pct = None
    drift = None
    try:
        if entry_px and signal_px:
            drift = (float(entry_px) - float(signal_px)) / float(signal_px) * 100.0
    except (TypeError, ValueError):
        drift = None
    risk_pct = None
    try:
        if risk_u is not None and equity:
            risk_pct = float(risk_u) / float(equity) * 100.0
    except (TypeError, ValueError):
        risk_pct = None

    head = f"{_HEADER}✅ 已开仓 · {coin} {_side_word(side)} {lev}{mode}"
    if dry_run:
        head += "（dry-run）"
    lines = [
        head,
        "",
        _row("成交价", _fmt_u(entry_px, 3)),
        _row("数量", f"{float(qty):g} {coin}（名义 {_fmt_u(notional, 1)} U）" if qty else "—"),
        _row("止损价", f"{_fmt_u(stop_px, 3)}（{stop_pct:+.2f}%）" if stop_pct is not None else _fmt_u(stop_px, 3)),
        _row("保证金", f"≈{_fmt_u(margin, 1)} U"),
        _row("强平价", _fmt_u(liq, 3)),
        _row("风险", f"{_fmt_u(risk_u)} U（权益的 {risk_pct:.1f}%）" if risk_pct is not None else f"{_fmt_u(risk_u)} U"),
        _row("滑点", f"{drift:+.2f}%（信号价 {_fmt_u(signal_px, 3)}）" if drift is not None else "—"),
        _row("时间", _stamp(when)),
    ]
    if oid is not None or stop_oid is not None:
        lines.append(_row("单号", f"开仓 {oid if oid is not None else '—'} · 止损 {stop_oid if stop_oid is not None else '—'}"))
    return "\n".join(lines)


def fmt_exit_alert(*, coin: str, side: int, entry_px: Optional[float], exit_px: Optional[float],
                   reason: str, net_pnl: Optional[float], fees: Optional[float] = None,
                   r_multiple: Optional[float] = None, opened_at: Optional[float] = None,
                   when: Optional[dt.datetime] = None, dry_run: bool = False,
                   settled: bool = False) -> str:
    """平仓回报：说清**为什么平**（止损 / 策略离场）+ 价格 + 盈亏（含手续费）。"""
    net = None if net_pnl is None else float(net_pnl)
    if str(reason) == "stop":
        head, why = "🛑 止损离场", "止损单被触发"
    elif str(reason) in ("exit", "ma20"):
        head = ("📈 策略止盈" if (net or 0) > 0 else ("📉 策略离场" if (net or 0) < 0 else "➖ 策略离场"))
        why = "收盘跌破 SMA20" if int(side or 1) >= 0 else "收盘站上 SMA20"
    else:
        head, why = "已平仓", str(reason)
    title = f"{_HEADER}{head} · {coin} {_side_word(side)}"
    if settled:
        title += "（离线结案）"
    if dry_run:
        title += "（dry-run）"

    gross = None
    try:
        if net is not None and fees is not None:
            gross = net + float(fees)
    except (TypeError, ValueError):
        gross = None

    lines = [
        title,
        "",
        _row("离场原因", why),
        _row("平仓价", f"{_fmt_u(exit_px, 3)}（开仓 {_fmt_u(entry_px, 3)}）"),
        _row("本次盈亏",
             ("—" if net is None else
              f"{_fmt_signed(net)} U"
              + (f"（{_fmt_signed(r_multiple)}R）" if r_multiple is not None else ""))),
        _row("盈亏构成", f"价差 {_fmt_signed(gross)} U · 手续费 {_fmt_u(fees)} U"
                        if gross is not None else "—"),
    ]
    hold = ""
    try:
        if opened_at is not None:
            now = (when or dt.datetime.now(DISPLAY_TZ)).timestamp()
            hold = _fmt_duration(now - float(opened_at))
    except (TypeError, ValueError):
        hold = ""
    if hold:
        lines.append(_row("持仓时长", hold))
    lines.append(_row("时间", _stamp(when)))
    return "\n".join(lines)


def fmt_liquidation_alert(*, coin: str, side: int, entry_px: Optional[float],
                          liq_px: Optional[float], qty: Optional[float],
                          loss_u: Optional[float], when: Optional[dt.datetime] = None) -> str:
    """强平（交易所侧清算）—— 保留同一模板；当前只在人工/外部平仓被识别时使用。"""
    return "\n".join([
        f"{_HEADER}💥 强制平仓 · {coin} {_side_word(side)}",
        "",
        _row("原因", "价格触及强平价，仓位被交易所清算"),
        _row("强平价", _fmt_u(liq_px, 3)),
        _row("开仓价", _fmt_u(entry_px, 3)),
        _row("数量", f"{float(qty):g} {coin}" if qty else "—"),
        _row("本次盈亏", f"{_fmt_signed(loss_u)} U"),
        _row("时间", _stamp(when)),
    ])
