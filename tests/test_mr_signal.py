"""`app/mr_signal.py` 的契约测试 —— **全离线、纯函数**。

钉住的东西：
  * 会话聚合：周末丢弃、夏/冬令时都对齐 16:00 收盘、半日市、盘中缺数 fail-closed、
    OHLC 自洽行过滤；
  * RSI2（Wilder）与 SMA5 的手算期望值；
  * 入场判定：信号隔日有效、窗口 [09:30, +3h)、单槽、错过即 None；
  * 离场判定：入场会话当日不评估、发现即出场；
  * `completed_upto` 的「已收盘完成」语义。

期望值全部手算（不拿被测代码算完再断言它自己）。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app import mr_signal


def _hourly(date: str, close: float, *, hours=(14, 15, 16, 17, 18, 19),
            open_price=None) -> list:
    """一个美股会话的 1h 原始行（EDT 假设：14:00–19:00 UTC = 10:00–15:00 ET 开盘）。"""
    base = int(datetime.strptime(date, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)
    rows = []
    for i, h in enumerate(hours):
        t = base + h * 3600_000
        o = (open_price if (open_price is not None and i == 0)
             else close + 0.1 * (len(hours) - i))
        rows.append({"t": t, "o": o, "h": max(o, close), "l": min(o, close),
                     "c": close})
    return rows


def _ms(utc_iso: str) -> int:
    return int(datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)
               .timestamp() * 1000)


class TestAggregateSessions(unittest.TestCase):
    def test_summer_weekday_aggregates_to_session_close_at_1600(self):
        rows = _hourly("2026-04-06", 100.0, open_price=100.5)
        sessions = mr_signal.aggregate_sessions(rows)
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s.date, "2026-04-06")
        self.assertEqual(s.close, 100.0)          # 15:00 ET bar 的收盘 = 16:00 价格
        self.assertEqual(s.bars, 6)
        self.assertEqual(s.last_bar_et, "15:00")
        self.assertEqual(s.open, 100.5)

    def test_winter_offset_by_one_utc_hour(self):
        # 冬令时：19:00 UTC = 14:00 EST（不是 15:00）。14:00–19:00 UTC 的 bar 在
        # 冬令时覆盖 9:00–14:00 ET → 10:00–14:00 ET 开盘的 bar 只有 15–19 时段。
        rows = _hourly("2026-01-05", 50.0, hours=(14, 15, 16, 17, 18, 19))
        sessions = mr_signal.aggregate_sessions(rows)
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s.bars, 5)               # 15:00–19:00 UTC = 10:00–14:00 EST
        self.assertEqual(s.last_bar_et, "14:00")
        # 冬令时正确的会话应包含 20:00 UTC（15:00 EST）那根 bar
        rows_winter = _hourly("2026-01-05", 50.0,
                              hours=(15, 16, 17, 18, 19, 20))
        s2 = mr_signal.aggregate_sessions(rows_winter)[0]
        self.assertEqual(s2.bars, 6)
        self.assertEqual(s2.last_bar_et, "15:00")  # 20:00 UTC = 15:00 EST → 收盘价口径

    def test_weekend_rows_dropped(self):
        rows = _hourly("2026-04-04", 100.0) + _hourly("2026-04-05", 101.0)
        self.assertEqual(mr_signal.aggregate_sessions(rows), [])

    def test_short_session_dropped_as_data_gap(self):
        # 只有 3 根（10:00/11:00/12:00 ET）的会话在 24/7 永续上等于「午后缺数」：
        # 拿 12:00 收盘当全天收盘会喂给 RSI/离场一条假收盘 → 整日弃用（MIN_SESSION_BARS=5）。
        rows = _hourly("2025-12-24", 60.0, hours=(15, 16, 17))
        self.assertEqual(mr_signal.aggregate_sessions(rows), [])

    def test_four_bar_session_dropped(self):
        # 缺 14:00/15:00 两根 → 仍不足以代表全天收盘
        rows = _hourly("2026-01-06", 50.0, hours=(15, 16, 17, 18))
        self.assertEqual(mr_signal.aggregate_sessions(rows), [])

    def test_five_bar_session_kept(self):
        # 允许缺 1 小时（末根 14:00 ET，收盘最多早 1 小时）
        rows = _hourly("2026-01-06", 50.0, hours=(15, 16, 17, 18, 19))
        sessions = mr_signal.aggregate_sessions(rows)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].bars, 5)

    def test_session_with_stale_last_bar_dropped(self):
        # 交易所盘中缺数：最后一根 bar 11:00 ET（16:00 UTC EST）→ 会话弃用
        rows = _hourly("2026-01-05", 50.0, hours=(15, 16))
        self.assertEqual(mr_signal.aggregate_sessions(rows), [])

    def test_ohlc_inconsistent_row_ignored(self):
        rows = _hourly("2026-04-06", 100.0) + [
            {"t": _ms("2026-04-06T19:30:00Z"), "o": 100.2, "h": 99.0,
             "l": 100.3, "c": 100.1}]
        sessions = mr_signal.aggregate_sessions(rows)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].bars, 6)     # 脏行被丢弃，不影响计数


class TestIndicators(unittest.TestCase):
    def test_rsi2_matches_backtest_ewm_convention(self):
        # 与 pandas ewm(alpha=0.5, adjust=False)（首差播种）手算一致：
        # deltas = [1, -0.5, -0.3, 0.8] → up=[1,.5,.25,.4625], dn=[0,.25,.375,.16875]
        closes = [100.0, 101.0, 100.5, 100.2, 101.0]
        out = mr_signal.rsi2_wilder(closes)
        self.assertIsNone(out[0])
        for i in (1, 2, 3, 4):
            self.assertIsNotNone(out[i])
        self.assertAlmostEqual(float(out[1]), 100.0, places=6)
        self.assertAlmostEqual(float(out[2]), 66.6667, places=3)
        self.assertAlmostEqual(float(out[3]), 47.619, places=3)
        self.assertAlmostEqual(float(out[4]), 79.2453, places=3)

    def test_rsi2_constant_decline_is_zero(self):
        out = mr_signal.rsi2_wilder([110.0, 109.0, 108.0, 107.0])
        self.assertAlmostEqual(out[-1], 0.0, places=6)

    def test_rsi2_flat_start_is_none(self):
        out = mr_signal.rsi2_wilder([100.0, 100.0, 100.5, 100.2])
        self.assertIsNone(out[1])                 # 前两收盘持平 → 无定义

    def test_sma5(self):
        out = mr_signal.sma([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        self.assertEqual(out[:4], [None, None, None, None])
        self.assertAlmostEqual(out[4], 3.0)
        self.assertAlmostEqual(out[5], 4.0)


def _sessions():
    rows = []
    closes = [("2026-04-06", 100.0), ("2026-04-07", 99.0),
              ("2026-04-08", 98.0), ("2026-04-09", 97.0),
              ("2026-04-10", 96.0), ("2026-04-13", 95.0),
              ("2026-04-14", 94.0), ("2026-04-15", 93.0),
              ("2026-04-16", 93.5), ("2026-04-17", 95.5)]
    for date, c in closes:
        rows.extend(_hourly(date, c))
    return mr_signal.aggregate_sessions(rows)


class TestEntryDecision(unittest.TestCase):
    def test_signal_enters_next_session_morning(self):
        sessions = _sessions()
        d = mr_signal.entry_decision(sessions=sessions, flat=True,
                                     now_ms=_ms("2026-04-16T13:40:00Z"))
        self.assertIsNotNone(d)
        self.assertEqual(d["signal_date"], "2026-04-15")
        self.assertEqual(d["entry_session_date"], "2026-04-16")

    def test_outside_window_rejected(self):
        sessions = _sessions()
        self.assertIsNone(mr_signal.entry_decision(
            sessions=sessions, flat=True, now_ms=_ms("2026-04-16T18:00:00Z")))
        self.assertIsNone(mr_signal.entry_decision(
            sessions=sessions, flat=True, now_ms=_ms("2026-04-16T13:15:00Z")))

    def test_wrong_day_rejected(self):
        sessions = _sessions()
        # 周六：信号日 04-15 的入场日是 04-16（已过），今天不是入场日 → None
        self.assertIsNone(mr_signal.entry_decision(
            sessions=sessions, flat=True, now_ms=_ms("2026-04-17T13:40:00Z")))
        # 无信号的收盘 → None
        self.assertIsNone(mr_signal.entry_decision(
            sessions=sessions, flat=True, now_ms=_ms("2026-04-11T13:40:00Z")))

    def test_not_flat_rejected(self):
        sessions = _sessions()
        self.assertIsNone(mr_signal.entry_decision(
            sessions=sessions, flat=False, now_ms=_ms("2026-04-13T13:40:00Z")))


class TestExitDecision(unittest.TestCase):
    def test_entry_day_not_evaluated_exit_next_day(self):
        sessions = _sessions()
        # 入场会话 04-16（close 93.5 < sma5 94.3）→ 不离场
        self.assertIsNone(mr_signal.exit_decision(
            sessions=sessions, now_ms=_ms("2026-04-16T20:10:00Z"),
            entry_session_date="2026-04-16"))
        # 04-17 close 95.5 > sma5 94.2 → 离场
        d = mr_signal.exit_decision(
            sessions=sessions, now_ms=_ms("2026-04-17T20:10:00Z"),
            entry_session_date="2026-04-16")
        self.assertEqual(d, "2026-04-17")

    def test_completed_upto_excludes_partial_today(self):
        sessions = _sessions()
        # 04-16 13:40 UTC：04-16 会话未完成（bar 未走完），完成数 = 8（04-06..04-15）
        k = mr_signal.completed_upto(sessions, _ms("2026-04-16T13:40:00Z"))
        self.assertEqual(k, 8)
        # 04-17 20:10 UTC：04-17 的最后一根 bar（19:00 UTC）+1h = 20:00 已过
        k2 = mr_signal.completed_upto(sessions, _ms("2026-04-17T20:10:00Z"))
        self.assertEqual(k2, 10)


class TestNextBusinessDay(unittest.TestCase):
    def test_friday_to_monday(self):
        self.assertEqual(mr_signal.next_business_day("2026-04-10"), "2026-04-13")

    def test_midweek(self):
        self.assertEqual(mr_signal.next_business_day("2026-04-07"), "2026-04-08")


if __name__ == "__main__":
    unittest.main()
