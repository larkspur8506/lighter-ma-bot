"""Lighter 全历史回测（T11）—— 用**冻结的信号口径**跑真实历史 K 线。

为什么这个脚本长这样
--------------------
旧系统（HL 现役）的 `ma_trend_backtest.py` 用 pandas 把策略实现了**第二遍**
（`squeeze_pct_flags()` / `signals()`），于是回测与实盘是两套代码：改一处忘另一处，
回测数字就开始描述一个系统并不会做的决策。这里反过来：**信号只来自
`app.strategy`**（`scan` / `compute_indicators` / `exit_triggered`，sha256 冻结），
**回撤只来自 `app.dd`**（与熔断、面板同一条 TWR 链），本模块只负责「拿信号走一遍
历史」，一个判定都不重写。

`scripts/backtest_engine.py` 里那份**独立**实现仍然有用 —— 但它的用途只有交叉验证
（`scripts/xvalidate.py` 用它当第二证人），不参与任何统计。

成交假设（代码里逐条对应，报告开头逐条打印）
------------------------------------------
1. **信号**：`app.strategy.scan(bars, symbol=..., sides=(1,-1))`，多空双向。
2. **进场**：信号 bar 的**下一根开盘价**（与 `Signal.entry_price` 同值）。滑点默认 0
   （`--slippage-bps`），一律朝不利方向。
3. **止损**：`Signal.stop_price`；多单 `bar.low <= stop` / 空单 `bar.high >= stop` 触发，
   按止损价成交；若该 bar **跳空开在止损之外**，按开盘价成交（更差的那个）。
4. **离场**：`app.strategy.exit_triggered`（收盘 vs SMA20）。**入场当根不判**
   —— 与 `app.lifecycle.decide_exit` 的 `is_entry_bar` 守卫完全一致。
5. **同一根 bar 内同时触及止损与信号离场 → 按止损**（保守假设：4h 内无法知道先后，
   止损是更差的结果，不能假设「先出信号价」）。
6. **手续费**：Lighter taker 0（`--fee-bps` 默认 0）。HL 是 0.045%/边 —— 不可直接搬。
7. **资金费**：Lighter `/api/v1/fundings` 的**真实逐小时历史费率**，逐 4h bar 按持仓
   名义计息；`direction` 决定哪一边付。
   **单位换算**：API 的 `rate` 是「每小时百分比」（`0.0012` = 0.0012%/h），
   转成小数分数要 /100。交叉验证：同期 `/api/v1/funding-rates` 给 Lighter BTC
   `9.5999e-05`（8h 口径的小数 = 0.0096%/8h = 0.0012%/h）—— 两个端点自洽。
8. **定仓**：`notional = 权益 × risk_pct / 止损距离`，`qty = notional / 进场价`。
   **不做交易所精度取整**（那是 `app.sizing` 的职责，实盘才需要；回测要的是连续口径）。
9. **复利**：每笔按**入场时的盯市权益**定仓（与实盘看到账户权益同一个口径）。
10. **回撤**：`app.dd.bootstrap/advance` 逐点推 TWR 指数，`app.dd.evaluate` 取回撤与
    档位判定（= 熔断/面板用的那个函数）；入金流水恒为 0（回测没有外部入金）。

口径边界（**不是** bug，是刻意的）
--------------------------------
* 一个标的同一时刻至多一仓（与实盘 `REJECT_SYMBOL_BUSY` 一致）；持仓期间的信号被跳过
  并计数（`skipped_busy`），不假装能反手。
* 强平不建模：止损距离上限 3.5%（策略 v2；v1 是 10%）配合 5x 逐仓，强平线（-19.5%）
  永远晚于止损（`app.strategy` 的 STOP_MAX 注释即此假设）。
* 资金费按「bar 收盘名义」一次性计提整根 4h 的费率；bar 内止损离场也付这一根的费率
  （略偏保守）。

用法
----
    python3 scripts/backtest.py                                  # BTC/ETH/SOL 全历史
    python3 scripts/backtest.py --json                           # 机器可读
    python3 scripts/backtest.py --cache-dir .cache               # 复用缓存（离线可跑）
    python3 scripts/backtest.py --bars-from-file bars.json --no-funding   # 完全离线
    python3 scripts/backtest.py --fee-bps 4.5 --slippage-bps 2   # 情景：按 HL 费率
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 信号口径与回撤口径的**唯一**来源：import 它们，不复制它们。
from app.dd import advance as dd_advance            # noqa: E402
from app.dd import bootstrap as dd_bootstrap        # noqa: E402
from app.dd import evaluate as dd_evaluate          # noqa: E402
from app.market import (                            # noqa: E402
    EARLIEST_MS, FALLBACK_MARKET_IDS, MAX_PAGES, PAGE_LIMIT, FeedError,
    bars_from_rows, default_client, drop_unclosed, fmt_ms,
)
from app.risk import RISK_TIERS, dd_thresholds       # noqa: E402  (档位/阈值的唯一来源)
from app.strategy import (                          # noqa: E402
    MIN_BARS_REQUIRED, Bar, Signal, compute_indicators, exit_triggered, scan,
)

# ---------------------------------------------------------------- 常量

DEFAULT_SYMBOLS = "BTC,ETH,SOL"
MARKET_IDS: Dict[str, int] = dict(FALLBACK_MARKET_IDS)   # BTC=1 ETH=0 SOL=2（仅兜底）
BAR_MS = 4 * 3600 * 1000
BAR_S = BAR_MS // 1000

DEFAULT_EQUITY = 10_000.0          # 与 README 算例同一起点，方便对读
# 三档风险：1.33% = 旧系统实盘口径；2% / 3% 是旧研究里跑过的对照档
TIER_RISK_PCTS: Tuple[float, ...] = (0.0133, 0.02, 0.03)
DEFAULT_FEE_BPS = 0.0              # Lighter taker 0
DEFAULT_SLIPPAGE_BPS = 0.0
SIDES = (1, -1)                    # 多 + 空（旧系统的单边时代已结束）

# 资金费端点（与 `app.market` 的 candles 同一 host，但那是 T3 的取数职责，
# 资金费历史没有对应的 app 模块 —— 只在回测脚本里读，实盘不碰它）。
FUNDING_API = "https://mainnet.zklighter.elliot.ai/api/v1/fundings"
FUNDING_RESOLUTION = "1h"
#: API 的 `rate` 是「每小时百分比」→ 小数分数要 /100（见模块 docstring 第 7 条）
FUNDING_PCT_PER_HOUR = 100.0
FUNDING_WINDOW_S = 25 * 86400       # 单次请求窗口（端点单次上限 750 条，25 天 = 600 条）
FUNDING_MAX_PAGES = 80

#: 单笔交易的离场原因（统计与面板配色都靠这个字符串区分）
EXIT_STOP = "stop"        # 触及止损（含跳空）
EXIT_SIGNAL = "signal"    # SMA20 信号离场
EXIT_EOD = "eod"          # 数据末尾仍在场 → 按最后一根收盘平掉


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class Trade:
    """一笔已平仓交易（全部金额为 USD，`r_*` 为 R 倍数）。"""

    symbol: str
    side: int
    entry_bar: int
    exit_bar: int
    entry_ms: int
    exit_ms: int
    entry_price: float
    stop_price: float
    exit_price: float
    reason: str
    qty: float
    notional: float
    risk_usd: float
    equity_at_entry: float
    gross_pnl: float          # 未计成本
    funding: float            # 正 = 支出
    fees: float               # 正 = 支出
    r_gross: float            # gross_pnl / 初始风险
    r_net: float              # (gross_pnl - funding - fees) / 初始风险

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.funding - self.fees

    @property
    def bars_held(self) -> int:
        return int(self.exit_bar - self.entry_bar)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "side": self.side,
            "entry_bar": self.entry_bar, "exit_bar": self.exit_bar,
            "entry_ms": self.entry_ms, "exit_ms": self.exit_ms,
            "entry_price": self.entry_price, "stop_price": self.stop_price,
            "exit_price": self.exit_price, "reason": self.reason,
            "qty": self.qty, "notional": self.notional, "risk_usd": self.risk_usd,
            "equity_at_entry": self.equity_at_entry,
            "gross_pnl": self.gross_pnl, "funding": self.funding, "fees": self.fees,
            "net_pnl": self.net_pnl, "r_gross": self.r_gross, "r_net": self.r_net,
            "bars_held": self.bars_held,
        }


@dataclass(frozen=True)
class FundingRates:
    """逐小时资金费率，已折算成「该方向每小时要支付的**分数**」。

    `direction == "long"` ⇒ 多头付钱（空头收钱）；`"short"` 反之。两条前缀和分别
    记录「多头累计应付」与「空头累计应付」，所以任何区间都能 O(log n) 求和。
    """

    symbol: str
    ts: Tuple[int, ...]
    cum_long: Tuple[float, ...]
    cum_short: Tuple[float, ...]
    hours: int = 0
    first_ts: int = 0
    last_ts: int = 0
    mean_rate: float = 0.0        # 逐小时分数均值（正 = 多头平均付钱）

    @classmethod
    def from_rows(cls, symbol: str, rows: Iterable[Mapping[str, Any]],
                  *, rate_divisor: float = FUNDING_PCT_PER_HOUR) -> "FundingRates":
        ts: List[int] = []
        cum_long: List[float] = []
        cum_short: List[float] = []
        acc_l = acc_s = 0.0
        total = 0.0
        for row in sorted(rows, key=lambda r: int(r["timestamp"])):
            stamp = int(row["timestamp"])
            if ts and stamp <= ts[-1]:
                continue                       # 同秒重复：后一份覆盖不了前缀和，直接跳
            rate = float(row["rate"]) / rate_divisor          # %/h → 分数/h
            if not math.isfinite(rate):
                continue
            direction = str(row.get("direction") or "long").strip().lower()
            if direction == "short":
                acc_s += rate
                acc_l -= rate
            else:
                acc_l += rate
                acc_s -= rate
            ts.append(stamp)
            cum_long.append(acc_l)
            cum_short.append(acc_s)
            total += rate
        mean = (total / len(ts)) if ts else 0.0
        return cls(symbol=symbol, ts=tuple(ts), cum_long=tuple(cum_long),
                   cum_short=tuple(cum_short), hours=len(ts),
                   first_ts=ts[0] if ts else 0, last_ts=ts[-1] if ts else 0,
                   mean_rate=mean)

    def paid(self, side: int, start_s: int, end_s: int) -> float:
        """`(start_s, end_s]` 内该方向应付的费率之和（正 = 支出，负 = 收取）。"""
        if not self.ts or end_s <= start_s:
            return 0.0
        lo = bisect.bisect_right(self.ts, int(start_s))
        hi = bisect.bisect_right(self.ts, int(end_s))
        if hi <= lo:
            return 0.0
        cum = self.cum_long if int(side) == 1 else self.cum_short
        head = cum[lo - 1] if lo else 0.0
        return float(cum[hi - 1] - head)

    def coverage(self, start_s: int, end_s: int) -> float:
        """窗口内「有费率数据的整点」占比（诊断用，不参与计算）。"""
        expected = max(0, int(end_s - start_s) // 3600)
        if expected <= 0:
            return 0.0
        lo = bisect.bisect_right(self.ts, int(start_s))
        hi = bisect.bisect_right(self.ts, int(end_s))
        return max(0, hi - lo) / expected

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "hours": self.hours,
                "first_ts": self.first_ts, "last_ts": self.last_ts,
                "mean_rate_per_hour": self.mean_rate}


@dataclass
class Series:
    """一个标的的回测输入：K 线 + 指标 + 信号 + （可选）资金费序列。"""

    symbol: str
    bars: List[Bar]
    ind: Dict[str, List[Optional[float]]]
    signals: List[Signal]
    funding: Optional[FundingRates] = None

    @property
    def index_of_ms(self) -> Dict[int, int]:
        return {bar.open_ms: i for i, bar in enumerate(self.bars)}

    def entry_index(self, signal: Signal) -> Optional[int]:
        """信号的进场下标（下一根）；末根信号没有下一根 → `None`。"""
        nxt = int(signal.bar_index) + 1
        return nxt if 0 <= nxt < len(self.bars) else None


@dataclass
class RunResult:
    """一次回测（一个场景 × 一档风险 × 一种成本口径）的全部产出。"""

    label: str
    risk_pct: float
    start_equity: float
    final_equity: float
    index: float = 1.0
    peak: float = 1.0
    max_dd: float = 0.0
    kill_steps: int = 0
    first_kill_ts: Optional[int] = None
    trades: List[Trade] = field(default_factory=list)
    points: List[Tuple[int, float]] = field(default_factory=list)
    skipped_busy: int = 0
    skipped_bust: int = 0
    skipped_bad_stop: int = 0
    funding_total: float = 0.0
    fees_total: float = 0.0
    dd_warn: float = 0.0
    dd_kill: float = 0.0


@dataclass
class _Open:
    """在场仓位（内部结构，平仓后变成 `Trade`）。"""

    symbol: str
    side: int
    qty: float
    entry_price: float
    stop_price: float
    entry_bar: int
    entry_ms: int
    notional: float
    risk_usd: float
    equity_at_entry: float
    funding: float = 0.0
    fees: float = 0.0
    last_close: float = 0.0


# ---------------------------------------------------------------- 小工具


def _mean(values: Sequence[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def _fmt_ts(ms: int) -> str:
    return fmt_ms(int(ms))


def _slip(price: float, side: int, bps: float, *, entering: bool) -> float:
    """滑点一律朝**持仓方不利**方向：多头进贵出便宜，空头进便宜出贵。"""
    frac = float(bps) / 10_000.0
    if frac <= 0.0:
        return float(price)
    adverse = frac if int(side) == 1 else -frac
    return float(price) * (1.0 + adverse) if entering else float(price) * (1.0 - adverse)


# ---------------------------------------------------------------- 信号


def signals_for(symbol: str, bars: Sequence[Bar], sides: Sequence[int] = SIDES) -> List[Signal]:
    """一个标的的信号 —— **直接委托冻结的 `app.strategy.scan`**。

    这个函数存在的唯一目的是让「回测用的信号」有名字、可被测试替换（注入假信号），
    它自己不做任何判定。任何在此处复制 `squeeze/band/破线` 逻辑的改动都是 bug。
    """
    return scan(bars, symbol=symbol, sides=tuple(sides))


def build_series(symbol: str, bars: List[Bar],
                 funding: Optional[FundingRates] = None,
                 signals: Optional[Sequence[Signal]] = None) -> Series:
    """组装一个标的的输入。`signals=None` → 走冻结的 `app.strategy.scan`。

    `signals` 只给测试用：注入手写信号才能把「引擎算术」与「信号判定」分开验证
    （否则一个手算样本要先通过 250 根分位窗口，验证的就变成策略而不是引擎）。
    """
    return Series(symbol=symbol, bars=bars, ind=compute_indicators(bars),
                  signals=list(signals) if signals is not None else signals_for(symbol, bars),
                  funding=funding)


# ---------------------------------------------------------------- 模拟


def simulate(series_by_symbol: Mapping[str, Series], *,
             risk_pct: float = TIER_RISK_PCTS[0],
             start_equity: float = DEFAULT_EQUITY,
             fee_bps: float = DEFAULT_FEE_BPS,
             slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
             use_funding: bool = True,
             funding_scale: float = 1.0,
             label: str = "") -> RunResult:
    """把信号走一遍历史（多标的共享一条权益曲线）。

    逐根 4h bar 的顺序（与 `app.lifecycle` 的时序一致）：

    1. **进场**：上一根收盘产生的信号，在本根**开盘价**成交（同一标的至多一仓）。
    2. **计提资金费**：本根 4h 的费率（按本根收盘名义）。
    3. **离场**：先止损（`low/high` 触及，含跳空），再判 `exit_triggered`（当根不判）。
    4. **盯市**：`已实现权益 + Σ 未实现`，喂给 `app.dd` 推一格 TWR。
    """
    risk = float(risk_pct)
    if not (0.0 < risk <= 1.0):
        raise ValueError(f"risk_pct must be in (0, 1]: {risk_pct!r}")

    entries: Dict[str, Dict[int, List[Signal]]] = {}
    bar_index: Dict[str, Dict[int, int]] = {}
    for symbol, series in series_by_symbol.items():
        idx_of = series.index_of_ms
        bar_index[symbol] = idx_of
        by_ms: Dict[int, List[Signal]] = {}
        for sig in series.signals:
            nxt = series.entry_index(sig)
            if nxt is None:
                continue
            by_ms.setdefault(series.bars[nxt].open_ms, []).append(sig)
        entries[symbol] = by_ms

    timeline = sorted({bar.open_ms for series in series_by_symbol.values()
                       for bar in series.bars})
    if not timeline:
        return RunResult(label=label, risk_pct=risk, start_equity=float(start_equity),
                         final_equity=float(start_equity), dd_warn=dd_thresholds(risk)["warn"],
                         dd_kill=dd_thresholds(risk)["kill"])

    result = RunResult(label=label, risk_pct=risk, start_equity=float(start_equity),
                       final_equity=float(start_equity),
                       dd_warn=dd_thresholds(risk)["warn"],
                       dd_kill=dd_thresholds(risk)["kill"])

    realized = float(start_equity)
    equity_prev = float(start_equity)
    open_pos: Dict[str, _Open] = {}
    last_close: Dict[str, float] = {}
    twr = None
    anchor_ts = int(timeline[0] // 1000)
    last_ts = int(timeline[-1] // 1000)

    def close_out(pos: _Open, *, exit_bar: int, exit_ms: int, exit_ref: float,
                  reason: str, bar_close: float) -> None:
        nonlocal realized
        exit_price = _slip(exit_ref, pos.side, slippage_bps, entering=False)
        exit_fee = pos.qty * exit_price * float(fee_bps) / 10_000.0
        gross = pos.qty * (exit_price - pos.entry_price) * pos.side
        fees = pos.fees + exit_fee
        pnl_net = gross - fees - pos.funding
        realized += pnl_net
        result.fees_total += exit_fee
        result.funding_total += pos.funding
        result.trades.append(Trade(
            symbol=pos.symbol, side=pos.side, entry_bar=pos.entry_bar, exit_bar=exit_bar,
            entry_ms=pos.entry_ms, exit_ms=exit_ms, entry_price=pos.entry_price,
            stop_price=pos.stop_price, exit_price=exit_price, reason=reason,
            qty=pos.qty, notional=pos.notional, risk_usd=pos.risk_usd,
            equity_at_entry=pos.equity_at_entry, gross_pnl=gross, funding=pos.funding,
            fees=fees, r_gross=gross / pos.risk_usd if pos.risk_usd else 0.0,
            r_net=pnl_net / pos.risk_usd if pos.risk_usd else 0.0))

    for stamp in timeline:
        bar_ms = int(stamp)

        # ---- ① 进场（上一根的信号，本根开盘成交）
        for symbol, series in series_by_symbol.items():
            idx = bar_index[symbol].get(bar_ms)
            if idx is None:
                continue
            bar = series.bars[idx]
            last_close[symbol] = float(bar.close)
            for sig in entries[symbol].get(bar_ms, ()):      # noqa: B007 (清晰优先)
                if symbol in open_pos:
                    result.skipped_busy += 1
                    continue
                if equity_prev <= 0.0:
                    result.skipped_bust += 1
                    continue
                entry_price = _slip(bar.open, sig.side, slippage_bps, entering=True)
                stop = float(sig.stop_price)
                if entry_price <= 0 or stop <= 0:
                    result.skipped_bad_stop += 1
                    continue
                if (sig.side == 1 and stop >= entry_price) or (sig.side == -1 and stop <= entry_price):
                    result.skipped_bad_stop += 1      # 滑点把止损推到入场价另一侧，无法下单
                    continue
                dist = abs(entry_price - stop) / entry_price
                if dist <= 0.0:
                    result.skipped_bad_stop += 1
                    continue
                notional = equity_prev * risk / dist
                qty = notional / entry_price
                if qty <= 0 or notional <= 0:
                    result.skipped_bust += 1
                    continue
                pos = _Open(symbol=symbol, side=int(sig.side), qty=qty,
                            entry_price=entry_price, stop_price=stop,
                            entry_bar=idx, entry_ms=bar_ms, notional=notional,
                            risk_usd=qty * abs(entry_price - stop),
                            equity_at_entry=equity_prev,
                            last_close=float(bar.close))
                pos.fees = notional * float(fee_bps) / 10_000.0
                result.fees_total += pos.fees
                open_pos[symbol] = pos

        # ---- ② 资金费 + ③ 离场
        for symbol in list(open_pos):
            pos = open_pos[symbol]
            series = series_by_symbol[symbol]
            idx = bar_index[symbol].get(bar_ms)
            if idx is None:
                continue                              # 该标的这根缺数据：不判、只等
            bar = series.bars[idx]
            pos.last_close = float(bar.close)

            if use_funding and series.funding is not None:
                rate = series.funding.paid(pos.side, bar_ms // 1000, bar_ms // 1000 + BAR_S)
                pos.funding += pos.qty * float(bar.close) * rate * float(funding_scale)

            exit_ref: Optional[float] = None
            reason = ""
            # **止损优先**（保守假设）：同一根 bar 内先算止损，再算信号离场。
            if pos.side == 1:
                if float(bar.low) <= pos.stop_price:
                    exit_ref = min(pos.stop_price, float(bar.open))
                    reason = EXIT_STOP
            else:
                if float(bar.high) >= pos.stop_price:
                    exit_ref = max(pos.stop_price, float(bar.open))
                    reason = EXIT_STOP
            if exit_ref is None and idx > pos.entry_bar:
                if exit_triggered(series.bars, series.ind, idx, pos.side):
                    exit_ref = float(bar.close)
                    reason = EXIT_SIGNAL

            is_last = bar_ms == timeline[-1]
            if exit_ref is None and is_last:
                exit_ref = float(bar.close)
                reason = EXIT_EOD

            if exit_ref is not None:
                close_out(pos, exit_bar=idx, exit_ms=bar_ms, exit_ref=exit_ref,
                          reason=reason, bar_close=float(bar.close))
                del open_pos[symbol]

        # ---- ④ 盯市 + TWR（与熔断/面板同一个 app.dd 链）
        unrealized = 0.0
        for pos in open_pos.values():
            mark = last_close.get(pos.symbol, pos.last_close)
            unrealized += pos.qty * (mark - pos.entry_price) * pos.side - pos.fees - pos.funding
        equity = realized + unrealized
        close_ts = int(bar_ms // 1000) + BAR_S
        if twr is None:
            twr = dd_bootstrap([], anchor_ts=anchor_ts, anchor_equity=float(start_equity))
        twr = dd_advance(twr, ts=close_ts, equity=equity) or twr
        result.points.append((close_ts, equity))
        verdict = dd_evaluate(twr.index, twr.peak, risk_pct=risk)
        if verdict.drawdown is not None and verdict.drawdown > result.max_dd:
            result.max_dd = float(verdict.drawdown)
        if verdict.action == "kill":
            result.kill_steps += 1
            if result.first_kill_ts is None:
                result.first_kill_ts = close_ts
        equity_prev = equity

    result.final_equity = float(realized)
    result.index = float(twr.index if twr is not None else 1.0)
    result.peak = float(twr.peak if twr is not None else 1.0)
    return result


# ---------------------------------------------------------------- 统计


def summarise(result: RunResult, *, years: float,
              bars_count: Optional[int] = None,
              signal_count: Optional[int] = None) -> Dict[str, Any]:
    """一次回测 → 一份 JSON 可序列化的统计（**唯一**的统计实现）。

    `scripts/xvalidate.py` 也调这个函数 —— 否则「回测统计」会有两份口径。
    """
    trades = result.trades
    n = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    r_gross = [t.r_gross for t in trades]
    r_net = [t.r_net for t in trades]
    win_r = [t.r_net for t in wins]
    loss_r = [t.r_net for t in losses]
    gross_pnl = sum(t.gross_pnl for t in trades)
    net_pnl = sum(t.net_pnl for t in trades)
    start = float(result.start_equity)
    final = float(result.final_equity)
    multiple = final / start if start > 0 else 0.0
    if years > 0 and multiple > 0:
        cagr = multiple ** (1.0 / years) - 1.0
    else:
        cagr = 0.0
    return {
        "label": result.label,
        "risk_pct": result.risk_pct,
        "bars": bars_count,
        "signals": signal_count,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n) if n else 0.0,
        "avg_r": _mean(r_gross),
        "avg_r_net": _mean(r_net),
        "median_r": float(statistics.median(r_net)) if r_net else 0.0,
        "best_r": max(r_net) if r_net else 0.0,
        "worst_r": min(r_net) if r_net else 0.0,
        "avg_win_r": _mean(win_r),
        "avg_loss_r": _mean(loss_r),
        "total_r": float(sum(r_net)),
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "funding_usd": result.funding_total,
        "fees_usd": result.fees_total,
        "start_equity": start,
        "final_equity": final,
        "multiple": multiple,
        "years": years,
        "cagr": cagr,
        "twr_index": result.index,
        "twr_peak": result.peak,
        "max_dd": result.max_dd,
        "dd_warn": result.dd_warn,
        "dd_kill": result.dd_kill,
        "kill_steps": result.kill_steps,
        "first_kill_ts": result.first_kill_ts,
        "stop_exits": sum(1 for t in trades if t.reason == EXIT_STOP),
        "signal_exits": sum(1 for t in trades if t.reason == EXIT_SIGNAL),
        "eod_exits": sum(1 for t in trades if t.reason == EXIT_EOD),
        "avg_bars_held": _mean([t.bars_held for t in trades]),
        "long_trades": sum(1 for t in trades if t.side == 1),
        "short_trades": sum(1 for t in trades if t.side == -1),
        "skipped_busy": result.skipped_busy,
        "skipped_bust": result.skipped_bust,
        "skipped_bad_stop": result.skipped_bad_stop,
        "by_symbol": _by_symbol(trades),
    }


def _by_symbol(trades: Sequence[Trade]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for symbol in sorted({t.symbol for t in trades}):
        sub = [t for t in trades if t.symbol == symbol]
        wins = sum(1 for t in sub if t.net_pnl > 0)
        out[symbol] = {
            "trades": len(sub), "wins": wins,
            "win_rate": wins / len(sub) if sub else 0.0,
            "avg_r": _mean([t.r_gross for t in sub]),
            "avg_r_net": _mean([t.r_net for t in sub]),
            "gross_pnl": sum(t.gross_pnl for t in sub),
            "net_pnl": sum(t.net_pnl for t in sub),
            "funding_usd": sum(t.funding for t in sub),
        }
    return out


def run_scenario(label: str, series_by_symbol: Mapping[str, Series], *,
                 risk_pct: float, start_equity: float = DEFAULT_EQUITY,
                 fee_bps: float = DEFAULT_FEE_BPS, slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
                 use_funding: bool = True, funding_scale: float = 1.0,
                 years: Optional[float] = None,
                 include_trades: bool = False) -> Dict[str, Any]:
    """跑一个场景并直接返回统计 dict（回测与 xvalidate 共用这个入口）。"""
    result = simulate(series_by_symbol, risk_pct=risk_pct, start_equity=start_equity,
                      fee_bps=fee_bps, slippage_bps=slippage_bps,
                      use_funding=use_funding, funding_scale=funding_scale, label=label)
    span = years
    if span is None:
        first = min(s.bars[0].open_ms for s in series_by_symbol.values() if s.bars)
        last = max(s.bars[-1].open_ms for s in series_by_symbol.values() if s.bars)
        span = ((last + BAR_MS) - first) / (365.25 * 86400 * 1000)
    stats = summarise(result, years=span,
                      bars_count=sum(len(s.bars) for s in series_by_symbol.values()),
                      signal_count=sum(len(s.signals) for s in series_by_symbol.values()))
    if include_trades:
        stats["trades_detail"] = [t.as_dict() for t in result.trades]
    return stats


# ---------------------------------------------------------------- 取数


def load_rows_file(path: str) -> Dict[str, List[dict]]:
    """读本地 bar JSON：原始 candle 行数组，或 ``{symbol: rows}``。"""
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        return {"*": payload}
    if isinstance(payload, dict):
        if "c" in payload:
            return {"*": payload["c"]}
        if "candles" in payload:
            return {str(payload.get("symbol", "*")).upper(): payload["candles"]}
        return {str(key).upper(): value for key, value in payload.items()}
    raise ValueError(f"{path}: expected a candle list or a symbol -> candles mapping")


def _get_json(url: str, *, timeout: float = 40.0, attempts: int = 4) -> Any:
    """GET → JSON（stdlib；重试 + 指数退避）。只在回测脚本里用。"""
    last: Optional[BaseException] = None
    for attempt in range(max(1, attempts)):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(min(8.0, 1.5 * (attempt + 1)))
    raise FeedError(f"GET {url} failed after {attempts} attempts: {last}")


def fetch_funding(symbol: str, market_id: int, *, since_ms: int = EARLIEST_MS,
                  now_s: Optional[int] = None, quiet: bool = False) -> List[dict]:
    """逐小时资金费历史（Lighter `/api/v1/fundings`，按 25 天窗口翻页）。

    端点契约（实测）：`resolution` 只接受 `1h` / `1d`；`start_timestamp` /
    `end_timestamp` 是**秒**；单次最多 750 条；`count_back=0` = 窗口内全给。
    """
    now = int(now_s if now_s is not None else time.time())
    start = int(since_ms // 1000)
    rows: List[dict] = []
    for _ in range(FUNDING_MAX_PAGES):
        if start >= now:
            break
        end = min(start + FUNDING_WINDOW_S, now)
        url = (f"{FUNDING_API}?market_id={market_id}&resolution={FUNDING_RESOLUTION}"
               f"&start_timestamp={start}&end_timestamp={end}&count_back=0")
        payload = _get_json(url)
        chunk = payload.get("fundings") or []
        rows.extend(chunk)
        if not quiet:
            print(f"  {symbol} 资金费 {_fmt_ts(start * 1000)} → {_fmt_ts(end * 1000)}："
                  f"{len(chunk)} 条", file=sys.stderr)
        start = end + 1
        time.sleep(0.2)
    rows.sort(key=lambda row: int(row["timestamp"]))
    dedup: Dict[int, dict] = {}
    for row in rows:
        dedup[int(row["timestamp"])] = row
    return [dedup[stamp] for stamp in sorted(dedup)]


def _funding_cache_path(cache_dir: Optional[str], symbol: str) -> Optional[str]:
    if not cache_dir:
        return None
    return os.path.join(cache_dir, f"FUNDING_{symbol}.json")


def load_funding(symbol: str, market_id: int, *, cache_dir: Optional[str],
                 from_file: Optional[str], quiet: bool = False,
                 fetch: bool = True) -> Optional[FundingRates]:
    """资金费：本地文件 → 缓存 → 网络；任何一环拿不到就返回 `None`（报告里如实标注）。"""
    rows: Optional[List[dict]] = None
    if from_file:
        with open(from_file, encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict) and "fundings" in payload:
            rows = payload["fundings"]
        elif isinstance(payload, dict) and symbol.upper() in payload:
            rows = payload[symbol.upper()]
        elif isinstance(payload, list):
            rows = payload
        if rows is None:
            raise ValueError(f"{from_file}: no funding rows for {symbol}")
    path = _funding_cache_path(cache_dir, symbol)
    if rows is None and path and os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("fundings") if isinstance(payload, dict) else payload
        if not quiet:
            print(f"（{symbol} 资金费使用缓存 {path}）", file=sys.stderr)
    if rows is None and fetch:
        try:
            rows = fetch_funding(symbol, market_id, quiet=quiet)
        except (FeedError, OSError) as exc:
            print(f"WARNING: {symbol} 资金费拉取失败（{exc}）；该标的按不计资金费处理",
                  file=sys.stderr)
            return None
        if path:
            os.makedirs(cache_dir or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"symbol": symbol, "market_id": market_id,
                           "resolution": FUNDING_RESOLUTION, "fundings": rows}, handle)
    if not rows:
        return None
    return FundingRates.from_rows(symbol, rows)


def load_bars(symbol: str, *, cache_dir: Optional[str], from_file: Optional[str],
              market_ids: Mapping[str, int], since_ms: int,
              keep_open_bar: bool = False, quiet: bool = False,
              fetch: bool = True) -> Optional[List[Bar]]:
    """K 线：本地文件 → 缓存 → `app.market`（唯一取数实现）。"""
    rows: Optional[List[dict]] = None
    if from_file:
        table = load_rows_file(from_file)
        rows = table.get(symbol) or table.get("*")
        if rows is None:
            print(f"❌ {from_file} 里没有 {symbol} 的 K 线", file=sys.stderr)
            return None
    path = os.path.join(cache_dir, f"{symbol}.json") if cache_dir else None
    if rows is None and path and os.path.isfile(path):
        table = load_rows_file(path)
        rows = table.get("*") or table.get(symbol)
        if rows is not None and not quiet:
            print(f"（{symbol} K 线使用缓存 {path}）", file=sys.stderr)
    if rows is None and fetch:
        market_id = market_ids.get(symbol)
        if market_id is None:
            print(f"❌ {symbol}: 没有 market_id（用 --market-ids 指定）", file=sys.stderr)
            return None
        try:
            rows = default_client().fetch_rows(market_id, since_ms=since_ms,
                                               page_limit=PAGE_LIMIT, max_pages=MAX_PAGES)
        except (FeedError, OSError) as exc:
            print(f"❌ {symbol}: 拉取 Lighter K 线失败：{exc}", file=sys.stderr)
            return None
        if path:
            os.makedirs(cache_dir or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(rows, handle)
    if not rows:
        return None
    bars = bars_from_rows(rows)
    bars = [bar for bar in bars if bar.open_ms >= since_ms]
    if not keep_open_bar:
        bars = drop_unclosed(bars)
    return bars or None


# ---------------------------------------------------------------- 报告

ASSUMPTIONS: Tuple[Tuple[str, str], ...] = (
    ("信号口径", "app.strategy.scan（冻结 sha256 8682667…，多空双向）"),
    ("进场", "信号 bar 的下一根开盘价（= Signal.entry_price）"),
    ("止损", "Signal.stop_price；low/high 触及即成交；跳空开在止损外按开盘价（更差）"),
    ("离场", "app.strategy.exit_triggered（收盘 vs SMA20）；入场当根不判（同 lifecycle）"),
    ("同 bar 冲突", "止损与信号离场同时触发 → 按止损（保守）"),
    ("手续费", "Lighter taker 0（--fee-bps 可覆盖）"),
    ("资金费", "Lighter /fundings 真实逐小时费率；逐 4h 按 bar 收盘名义计提"),
    ("滑点", "默认 0（--slippage-bps 可覆盖，一律朝不利方向）"),
    ("定仓", "notional = 权益 × risk_pct / 止损距离；不做交易所精度取整"),
    ("复利", "每笔按入场时的盯市权益定仓（同实盘口径）"),
    ("回撤", "app.dd TWR 链（bootstrap/advance）+ app.dd.evaluate 判档"),
    ("仓位上限", "每标的同时至多 1 仓；持仓期间的信号跳过并计数"),
    ("强平", "不建模（止损 ≤10% 时强平 -19.5% 永远更晚）"),
)


def print_assumptions(*, fee_bps: float, slippage_bps: float, use_funding: bool,
                      funding_scale: float) -> None:
    print("成交假设（逐条对应 docs/BACKTEST.md）：")
    for name, text in ASSUMPTIONS:
        print(f"  · {name}：{text}")
    print(f"  · 本run：fee={fee_bps:g}bps  滑点={slippage_bps:g}bps  "
          f"资金费={'开' if use_funding else '关'}"
          + (f"（×{funding_scale:g}）" if use_funding and funding_scale != 1.0 else ""))


def _pct(value: Optional[float], digits: int = 2) -> str:
    return "—" if value is None else f"{value * 100:+.{digits}f}%"


def _num(value: Optional[float], digits: int = 2) -> str:
    return "—" if value is None else f"{value:+.{digits}f}"


def print_stats_row(stats: Mapping[str, Any]) -> None:
    print(f"  {stats['label']:<14} {stats['risk_pct'] * 100:>5.2f}%  "
          f"{stats['trades']:>4d}  {stats['win_rate'] * 100:>5.1f}%  "
          f"{_num(stats['avg_r']):>6}  {_num(stats['avg_r_net']):>6}  "
          f"{_pct(stats['cagr']):>8}  {stats['multiple']:>6.2f}x  "
          f"{stats['max_dd'] * 100:>6.2f}%  {stats['funding_usd']:>+10.1f}")


def print_stats_header() -> None:
    print(f"  {'场景':<12} {'风险':>6}  {'笔数':>4}  {'胜率':>6}  "
          f"{'均R':>6}  {'均R净':>6}  {'CAGR':>8}  {'终值':>7}  "
          f"{'最大回撤':>7}  {'资金费USDC':>10}")


def print_detail(stats: Mapping[str, Any], *, top: int = 3) -> None:
    print(f"    ├ 明细：止损 {stats['stop_exits']} / 信号 {stats['signal_exits']} / "
          f"数据末尾 {stats['eod_exits']}；多 {stats['long_trades']} 空 {stats['short_trades']}；"
          f"平均持仓 {stats['avg_bars_held']:.1f} 根")
    print(f"    ├ R 分布：最好 {stats['best_r']:+.2f} / 最差 {stats['worst_r']:+.2f} / "
          f"中位 {stats['median_r']:+.2f}；获利笔均 {stats['avg_win_r']:+.2f}R，"
          f"亏损笔均 {stats['avg_loss_r']:+.2f}R")
    print(f"    ├ 权益：{stats['start_equity']:.0f} → {stats['final_equity']:.0f} "
          f"（×{stats['multiple']:.3f}，{stats['years']:.2f} 年，TWR 指数 "
          f"{stats['twr_index']:.4f}）；毛 PnL {stats['gross_pnl']:+.1f} / "
          f"净 PnL {stats['net_pnl']:+.1f}（资金费 {stats['funding_usd']:+.1f}，"
          f"手续费 {stats['fees_usd']:.1f}）")
    dd_line = (f"    ├ 回撤档位：warn {stats['dd_warn'] * 100:.0f}% / "
               f"kill {stats['dd_kill'] * 100:.0f}%；熔断触发 {stats['kill_steps']} 根 bar")
    if stats.get("first_kill_ts"):
        dd_line += f"（首次 {_fmt_ts(int(stats['first_kill_ts']) * 1000)}）"
    print(dd_line)
    print(f"    └ 跳过：持仓中 {stats['skipped_busy']} / 权益耗尽 {stats['skipped_bust']} / "
          f"止损非法 {stats['skipped_bad_stop']}")


# ---------------------------------------------------------------- CLI


def _parse_floats(raw: str) -> List[float]:
    out = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if chunk:
            out.append(float(chunk))
    return out


def _parse_market_ids(raw: Optional[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        symbol, _, value = chunk.partition("=")
        if not symbol or not value:
            raise SystemExit(f"--market-ids: expected SYMBOL=ID, got {chunk!r}")
        out[symbol.strip().upper()] = int(value)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Lighter 全历史回测（信号=app.strategy，回撤=app.dd）")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS, help=f"默认 {DEFAULT_SYMBOLS}")
    ap.add_argument("--market-ids", default=None, help="覆盖 market_id，如 BTC=1,ETH=0,SOL=2")
    ap.add_argument("--bars-from-file", default=None, help="本地 bar JSON（完全离线）")
    ap.add_argument("--funding-from-file", default=None, help="本地资金费 JSON（完全离线）")
    ap.add_argument("--cache-dir", default=None, help="<SYM>.json / FUNDING_<SYM>.json 缓存目录")
    ap.add_argument("--since", default=None, help="只回测该日期之后（YYYY-MM-DD）")
    ap.add_argument("--keep-open-bar", action="store_true", help="保留未收盘的最后一根")
    ap.add_argument("--equity", type=float, default=DEFAULT_EQUITY, help="初始权益（默认 10000）")
    ap.add_argument("--risk", default=",".join(str(x) for x in TIER_RISK_PCTS),
                    help="风险档，逗号分隔（默认 0.0133,0.02,0.03）")
    ap.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS, help="单边手续费 bps")
    ap.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS,
                    help="单边滑点 bps（不利方向）")
    ap.add_argument("--no-funding", action="store_true", help="不计资金费")
    ap.add_argument("--funding-scale", type=float, default=1.0, help="资金费倍数（压力测试）")
    ap.add_argument("--no-network", action="store_true", help="只读文件/缓存，不联网")
    ap.add_argument("--trades", type=int, default=0, help="打印前 N 笔交易明细（默认 0）")
    ap.add_argument("--json", nargs="?", const="-", default=None,
                    help="输出 JSON（缺省路径 = stdout）")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    if not symbols:
        raise SystemExit("--symbols is empty")
    market_ids = {**MARKET_IDS, **_parse_market_ids(args.market_ids)}
    risks = _parse_floats(args.risk) or list(TIER_RISK_PCTS)
    for risk in risks:
        if not (0.0 < risk <= 1.0):
            raise SystemExit(f"--risk: {risk} 不在 (0, 1] 内")

    since_ms = EARLIEST_MS
    if args.since:
        since_ms = int(dt.datetime.strptime(args.since, "%Y-%m-%d")
                       .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

    use_network = not args.no_network
    series_by_symbol: Dict[str, Series] = {}
    notes: List[str] = []
    for symbol in symbols:
        bars = load_bars(symbol, cache_dir=args.cache_dir, from_file=args.bars_from_file,
                         market_ids=market_ids, since_ms=since_ms,
                         keep_open_bar=args.keep_open_bar, fetch=use_network)
        if not bars:
            notes.append(f"{symbol}: 没有可用 K 线，已跳过")
            continue
        if len(bars) < MIN_BARS_REQUIRED:
            notes.append(f"{symbol}: 只有 {len(bars)} 根 < {MIN_BARS_REQUIRED} 根，算不出分位，已跳过")
            continue
        funding = None
        if not args.no_funding:
            funding = load_funding(symbol, market_ids.get(symbol, -1),
                                   cache_dir=args.cache_dir, from_file=args.funding_from_file,
                                   fetch=use_network)
            if funding is None:
                notes.append(f"{symbol}: 没有资金费数据 → 该标的按不计资金费处理")
        series_by_symbol[symbol] = build_series(symbol, bars, funding)

    if not series_by_symbol:
        print("❌ 没有任何标的可回测：" + "；".join(notes or ["（无）"]))
        return 2

    span_start = min(s.bars[0].open_ms for s in series_by_symbol.values())
    span_end = max(s.bars[-1].open_ms for s in series_by_symbol.values()) + BAR_MS
    years = (span_end - span_start) / (365.25 * 86400 * 1000)

    scenarios: List[Tuple[str, Dict[str, Series]]] = [
        (symbol, {symbol: series}) for symbol, series in series_by_symbol.items()
    ]
    if len(series_by_symbol) > 1:
        scenarios.append(("+".join(series_by_symbol), dict(series_by_symbol)))

    report: Dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": {
            "source": "bars-from-file" if args.bars_from_file else
                      ("cache" if args.cache_dir else "lighter"),
            "start_ms": span_start, "end_ms": span_end,
            "start": _fmt_ts(span_start), "end": _fmt_ts(span_end),
            "years": years,
            "symbols": {
                symbol: {
                    "bars": len(series.bars),
                    "signals": len(series.signals),
                    "long_signals": sum(1 for s in series.signals if s.side == 1),
                    "short_signals": sum(1 for s in series.signals if s.side == -1),
                    "first": _fmt_ts(series.bars[0].open_ms),
                    "last": _fmt_ts(series.bars[-1].open_ms),
                    "funding_hours": series.funding.hours if series.funding else 0,
                    "funding_mean_rate_per_hour": (series.funding.mean_rate
                                                   if series.funding else None),
                    "funding_coverage": (series.funding.coverage(span_start // 1000,
                                                                 span_end // 1000)
                                         if series.funding else None),
                }
                for symbol, series in series_by_symbol.items()
            },
        },
        "assumptions": {
            "signal_engine": "app.strategy.scan",
            "drawdown_engine": "app.dd.bootstrap/advance + app.dd.evaluate",
            "entry": "next bar open",
            "stop": "Signal.stop_price, intrabar low/high, gap fills at open",
            "exit": "strategy.exit_triggered (close vs SMA20), not on the entry bar",
            "same_bar_conflict": "stop wins (conservative)",
            "fee_bps": args.fee_bps,
            "slippage_bps": args.slippage_bps,
            "funding": (not args.no_funding),
            "funding_scale": args.funding_scale,
            "funding_unit": "API rate is % per hour; fraction = rate / 100",
            "sizing": "notional = equity * risk_pct / stop_dist (no exchange rounding)",
            "compounding": "equity at entry, marked to market",
            "start_equity": args.equity,
            "tiers": risks,
            "detail": [{"name": name, "text": text} for name, text in ASSUMPTIONS],
        },
        "notes": notes,
        "runs": {},
    }

    for risk in risks:
        key = f"{risk * 100:.2f}%"
        report["runs"][key] = {}
        for label, subset in scenarios:
            gross = run_scenario(label, subset, risk_pct=risk, start_equity=args.equity,
                                 fee_bps=0.0, slippage_bps=0.0, use_funding=False,
                                 years=years)
            net = run_scenario(label, subset, risk_pct=risk, start_equity=args.equity,
                               fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
                               use_funding=not args.no_funding,
                               funding_scale=args.funding_scale, years=years,
                               include_trades=bool(args.trades))
            report["runs"][key][label] = {"gross": gross, "net": net}

    if args.json:
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.json == "-":
            print(payload)
        else:
            with open(args.json, "w", encoding="utf-8") as handle:
                handle.write(payload)
            print(f"（JSON 已写入 {args.json}）", file=sys.stderr)
        return 0

    # ---------------- 人类可读报告 ----------------
    print("=" * 100)
    print(f"Lighter 全历史回测  {_fmt_ts(span_start)} → {_fmt_ts(span_end)}"
          f"（{years:.2f} 年，起 {args.equity:.0f} USDC）")
    print("=" * 100)
    for symbol, block in report["data"]["symbols"].items():
        print(f"  {symbol}: {block['bars']} 根 4h bar；信号 {block['signals']} 笔"
              f"（多 {block['long_signals']} / 空 {block['short_signals']}）"
              f"；资金费 {block['funding_hours']} 小时点"
              + (f"（覆盖 {block['funding_coverage'] * 100:.1f}%，均值 "
                 f"{block['funding_mean_rate_per_hour'] * 100:.4f}%/h）"
                 if block["funding_hours"] else "（无）"))
    if notes:
        for note in notes:
            print(f"  ⚠ {note}")
    print()
    print_assumptions(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
                      use_funding=not args.no_funding, funding_scale=args.funding_scale)

    for risk in risks:
        key = f"{risk * 100:.2f}%"
        print()
        print("-" * 100)
        print(f"风险档 {key}（每笔风险 = 入场权益 × {key}；warn "
              f"{next(iter(report['runs'][key].values()))['net']['dd_warn'] * 100:.0f}% / "
              f"kill {next(iter(report['runs'][key].values()))['net']['dd_kill'] * 100:.0f}%）")
        print("-" * 100)
        print("【不扣成本（纯策略）】")
        print_stats_header()
        for label in report["runs"][key]:
            print_stats_row(report["runs"][key][label]["gross"])
        print("【扣资金费" + ("" if args.fee_bps == 0 else " + 手续费")
              + ("" if args.slippage_bps == 0 else " + 滑点") + "（真实口径）】")
        print_stats_header()
        for label in report["runs"][key]:
            print_stats_row(report["runs"][key][label]["net"])
        print()
        print(f"  合计（{len(series_by_symbol)} 标的组合）明细：")
        combined_label = "+".join(series_by_symbol)
        if combined_label in report["runs"][key]:
            print_detail(report["runs"][key][combined_label]["net"])
        for label in report["runs"][key]:
            if label == combined_label:
                continue
            print_detail(report["runs"][key][label]["net"])

    if args.trades:
        print()
        print(f"前 {args.trades} 笔交易（1.33% 档、含成本、合计场景）：")
        key = f"{risks[0] * 100:.2f}%"
        combined_label = "+".join(series_by_symbol)
        rows = report["runs"][key][combined_label]["net"]["trades_detail"][:args.trades]
        print(f"  {'标的':<4}{'方向':<4}{'进场':<18}{'离场':<18}{'进场价':>10}"
              f"{'止损价':>10}{'离场价':>10}{'R净':>7}{'原因':>8}")
        for row in rows:
            print(f"  {row['symbol']:<5}{'多' if row['side'] == 1 else '空':<4}"
                  f"{_fmt_ts(row['entry_ms']):<19}{_fmt_ts(row['exit_ms']):<19}"
                  f"{row['entry_price']:>10.2f}{row['stop_price']:>10.2f}"
                  f"{row['exit_price']:>10.2f}{row['r_net']:>+7.2f}{row['reason']:>8}")

    print()
    print("口径提醒：回测不代表未来。Lighter 4h 历史只有 "
          f"{years:.2f} 年（HL 研究 6.1 年），深度约 HL 的 1/5，"
          "资金费按历史真实费率计、手续费 0。详见 docs/BACKTEST.md。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
