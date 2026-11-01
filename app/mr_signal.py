"""MR（超跌反弹）信号模块 — 纯 stdlib，冻结同码（lighter-ma-bot / crypto-ma-alert 共用）。

规则（冻结版 v1，2026-11-01，回测口径见 strategy-backtest 技能）：

- 数据：场所 1h K 线 → 按 ``America/New_York`` 聚合为美股交易日 OHLC。
  会话 = 当日 09:30–16:00 ET 之间**开盘**的 bar（DST 由 zoneinfo 逐时刻换算；
  2026-11-01 自检确认固定 UTC 窗口在冬令时整体偏移 1 小时，故禁止固定窗口）。
  小时 bar 在整点开盘（ET 9:00/10:00/...），没有恰好在 09:30 开盘的 bar，因此
  会话首根 bar 是 10:00 ET——这只影响台账里的「参考开盘价」，不影响任何信号：
  RSI2/SMA5 只用会话收盘价，末根 15:00 ET bar 的收盘恰是 16:00 价格，与日线
  收盘对齐（夏/冬令时都成立）。夜盘与周末的永续 bar 全部丢弃；周六/周日不是
  会话；半日市由「窗口内没有更多 bar」天然表达。
- 指标：RSI2（Wilder 平滑，alpha=1/2）与 SMA5，都基于会话收盘价。
- 入场：会话收盘 RSI2 < 10 → 次一会话开盘市价买入；开盘后 ``ENTRY_WINDOW_H``
  小时内未成交则放弃该信号（fail-closed，宁可错过）。单槽：持仓中忽略新信号。
- 离场：持仓中，某会话收盘 > SMA5 → 该会话收盘后市价卖出；入场会话当日不评估
  离场（与回测引擎 ``range(ei+1, ...)`` 一致）。发现即出场，不追补历史价。
- 无止损。全史最差单笔约 -17.8%（仓位按它定价），加止损的 26 年实测有害。

本模块是 MR 腿**唯一**的信号口径来源：两个仓库的执行器、测试、验证脚本全部
import 它；两边文件必须逐字节一致（conformance 测试断言）。只依赖 stdlib。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

__all__ = [
    "SESSION_TZ", "SESSION_START", "SESSION_END", "RSI_PERIOD", "RSI_THRESHOLD",
    "SMA_WINDOW", "ENTRY_WINDOW_H", "MIN_SESSION_BARS", "SessionBar",
    "aggregate_sessions", "rsi2_wilder", "sma", "session_signals",
    "entry_decision", "exit_decision", "next_business_day",
]

SESSION_TZ = "America/New_York"
SESSION_START = (9, 30)          # ET 时、分（含）
SESSION_END = (16, 0)            # ET 时、分（不含：bar 按开盘时刻归属）
RSI_PERIOD = 2
RSI_THRESHOLD = 10.0
SMA_WINDOW = 5
ENTRY_WINDOW_H = 3.0             # 会话开盘后多少小时内必须成交，否则放弃该信号
MIN_SESSION_BARS = 3             # 少于这个数的会话不可用（数据缺失 fail-closed）
#: 会话「有效」的最晚首 bar 开盘 ET 时刻：常规日 10:00、半日市 12:00。
#: 交易所盘中长时间缺数（如 Lighter 停机）时，末根 bar 早于它 → 该会话不可用。
MIN_LAST_BAR_ET = (12, 0)


@dataclass(frozen=True)
class SessionBar:
    """一个美股交易日（ET 日历日）的会话 OHLC。"""

    date: str                     # 'YYYY-MM-DD'（ET 日历日）
    open: float
    high: float
    low: float
    close: float
    bars: int
    last_bar_ms: int              # 会话内最后一根 bar 的开盘时刻（毫秒）
    last_bar_et: str              # 最后一根 bar 的开盘 ET 时刻 'HH:MM'（审计用）

    def as_dict(self) -> dict:
        return {
            "date": self.date, "open": self.open, "high": self.high,
            "low": self.low, "close": self.close, "bars": self.bars,
            "last_bar_ms": self.last_bar_ms, "last_bar_et": self.last_bar_et,
        }


def _parse_hourly_row(row: Mapping[str, Any]) -> Optional[tuple]:
    """原始 candle 行 → (ET datetime, open, high, low, close)。字段缺失/非法返回 None。"""
    try:
        t_ms = int(row["t"])
        o = float(row["o"])
        h = float(row["h"])
        low = float(row["l"])
        c = float(row["c"])
    except (KeyError, TypeError, ValueError):
        return None
    if t_ms <= 0 or o <= 0 or h <= 0 or low <= 0 or c <= 0:
        return None
    if h < max(o, c) or low > min(o, c):
        return None                                    # OHLC 自洽性（腾讯数据事故的教训）
    et = datetime.fromtimestamp(t_ms / 1000.0, tz=ZoneInfo(SESSION_TZ))
    return et, o, h, low, c


def aggregate_sessions(hourly_rows: Sequence[Mapping[str, Any]]) -> List[SessionBar]:
    """1h K 线（升序）→ 美股交易日会话 OHLC（升序）。

    归属规则：bar 的 **开盘时刻**（ET）落在 [09:30, 16:00) 内且为周一至周五。
    每个会话取首根 bar 的 open、末根 bar 的 close、全体 high/low。
    少于 ``MIN_SESSION_BARS`` 根、或末根 bar 早于 ``MIN_LAST_BAR_ET``
    （盘中数据缺失）的会话直接丢弃。
    """
    buckets: "dict[str, list]" = {}
    for row in hourly_rows:
        parsed = _parse_hourly_row(row)
        if parsed is None:
            continue
        et, o, h, low, c = parsed
        start = datetime.combine(et.date(), _time(*SESSION_START),
                                 tzinfo=ZoneInfo(SESSION_TZ))
        end = datetime.combine(et.date(), _time(*SESSION_END),
                               tzinfo=ZoneInfo(SESSION_TZ))
        if et.weekday() >= 5 or not (start <= et < end):
            continue
        buckets.setdefault(et.strftime("%Y-%m-%d"), []).append((et, o, h, low, c))

    out: List[SessionBar] = []
    for date in sorted(buckets):
        rows = buckets[date]
        if len(rows) < MIN_SESSION_BARS:
            continue
        last_et = rows[-1][0]
        if (last_et.hour, last_et.minute) < MIN_LAST_BAR_ET:
            continue                                   # 盘中数据缺失 → 弃用该会话
        out.append(SessionBar(
            date=date,
            open=rows[0][1], high=max(r[2] for r in rows),
            low=min(r[3] for r in rows), close=rows[-1][4],
            bars=len(rows), last_bar_ms=int(last_et.timestamp() * 1000),
            last_bar_et=last_et.strftime("%H:%M"),
        ))
    return out


def _time(h: int, m: int):
    """datetime.time 不在 __all__ 里直接暴露，包一层避免调用点重复 import。"""
    from datetime import time as _t
    return _t(h, m)


def _rsi2_val(up: float, dn: float) -> Optional[float]:
    if dn <= 0:
        return 100.0 if up > 0 else None      # 双零 → pandas 是 NaN（无信号），不是 50
    return 100.0 - 100.0 / (1.0 + up / dn)


def rsi2_wilder(closes: Sequence[float]) -> List[Optional[float]]:
    """RSI2，与回测引擎（pandas ``ewm(alpha=1/2, adjust=False)``）**逐位一致**。

    首差值直接播种 EMA（不是 Wilder 的 SMA 种子——两种种子的短历史值差很多，
    会破坏与回测的信号一致性）。``rsi[i]`` 基于到 ``closes[i]`` 为止的价差；
    前 1 个位置与「前两收盘持平」的退化情形返回 None（pandas 是 NaN → 无信号）。
    """
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < 3:
        return out
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    alpha = 1.0 / RSI_PERIOD
    up = deltas[0] if deltas[0] > 0 else 0.0
    dn = -deltas[0] if deltas[0] < 0 else 0.0
    out[1] = _rsi2_val(up, dn)                                # closes[1]：首差播种
    for i in range(2, n):
        d = deltas[i - 1]
        up = (1 - alpha) * up + alpha * max(d, 0.0)
        dn = (1 - alpha) * dn + alpha * max(-d, 0.0)
        out[i] = _rsi2_val(up, dn)
    return out


def sma(values: Sequence[float], window: int = SMA_WINDOW) -> List[Optional[float]]:
    """简单移动平均，前 ``window-1`` 个为 None。"""
    out: List[Optional[float]] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - window:i + 1]) / window)
    return out


def session_signals(sessions: Sequence[SessionBar]) -> List[dict]:
    """逐会话给出信号字段：``{date, close, rsi2, sma5, entry, exit_above}``。

    ``entry``：该会话收盘 RSI2 < 阈值（次日开盘买的依据）。
    ``exit_above``：该会话收盘 > SMA5（持仓时收盘后卖的依据）。
    """
    closes = [s.close for s in sessions]
    rsis = rsi2_wilder(closes)
    smas = sma(closes)
    out: List[dict] = []
    for i, s in enumerate(sessions):
        rsi = rsis[i]
        m5 = smas[i]
        out.append({
            "date": s.date, "close": s.close, "rsi2": rsi, "sma5": m5,
            "entry": bool(rsi is not None and rsi < RSI_THRESHOLD),
            "exit_above": bool(m5 is not None and s.close > m5),
        })
    return out


def completed_upto(sessions: Sequence[SessionBar], now_ms: int) -> int:
    """已「收盘完成」的会话数：末根 bar 开盘 + 1h（bar 时长）<= now。

    返回值 k 表示 ``sessions[:k]`` 已完成；负数或越界由调用方钳制。
    """
    k = 0
    for s in sessions:
        if s.last_bar_ms + 3600_000 <= now_ms:
            k += 1
        else:
            break
    return k


def next_business_day(date_str: str) -> str:
    """下一个美股工作日（周一–周五，不含节假日；节假日靠 venue 无会话天然跳过）。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def entry_decision(*, sessions: Sequence[SessionBar], now_ms: int,
                   flat: bool) -> Optional[dict]:
    """入场判定。返回 ``{signal_date, entry_session_date, deadline_ms}`` 或 None。

    条件（全部满足）：
    - 空仓（单槽）；
    - 最近一个**已完成**会话 S 给出入场信号；
    - 「今天」（ET）= S 的下一个工作日 —— 信号隔日有效，隔两天就作废；
    - now 在 [今日会话开盘, 开盘 + ENTRY_WINDOW_H] 内。
    今日会话开盘按 ET 09:30 估计（首根整点 bar 10:00 开盘前 30 分钟；tick 在
    ET 09:33 落地，两个 DST 档都成立——按 10:00 估会白白浪费开盘那一次 tick）。
    """
    if not flat or not sessions:
        return None
    now = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    today_et = now.astimezone(ZoneInfo(SESSION_TZ)).strftime("%Y-%m-%d")
    k = completed_upto(sessions, now_ms)
    if k <= 0:
        return None
    sigs = session_signals(sessions)
    if not sigs[k - 1]["entry"]:
        return None
    signal = sessions[k - 1]
    if next_business_day(signal.date) != today_et:
        return None
    open_today = (datetime.strptime(today_et, "%Y-%m-%d")
                  .replace(hour=9, minute=30, tzinfo=ZoneInfo(SESSION_TZ)))
    open_ms = int(open_today.timestamp() * 1000)
    deadline_ms = open_ms + int(ENTRY_WINDOW_H * 3600_000)
    if not (open_ms <= now_ms <= deadline_ms):
        return None
    return {"signal_date": signal.date, "entry_session_date": today_et,
            "deadline_ms": deadline_ms}


def exit_decision(*, sessions: Sequence[SessionBar], now_ms: int,
                  entry_session_date: str) -> Optional[str]:
    """离场判定。返回触发离场的会话日期，或 None。

    在**入场会话之后**的已完成会话中，找最近一个收盘 > SMA5 的；命中即市价卖出。
    """
    k = completed_upto(sessions, now_ms)
    if k <= 0:
        return None
    for i in range(k - 1, -1, -1):
        s = sessions[i]
        if s.date <= entry_session_date:
            return None
        sig = session_signals(sessions[: k])[i]
        if sig["exit_above"]:
            return s.date
    return None
