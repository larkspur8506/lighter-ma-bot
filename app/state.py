"""运行时状态装配：`store`（+ 可选注入）→ `app.risk.RiskState`。

职责边界
--------
`app/store.py` 只负责「把字写进 SQLite 并读回来」；`app/risk.py` 只负责「拿到一份状态后
逐闸判定」。本模块是两者之间的**唯一装配点**：把库里的仓位/权益/计数/回撤锚点拼成
`RiskState`（T4 的 7 个闸门直接吃），并**明确标注每个字段的证据来源**，让面板和日志能回答
「这个数字是哪来的」。

**只读为主**：本模块不写 `positions` / `trades`（单一写者是 T8 executor），唯一的写操作是
调用方显式请求的 `ensure_day_start()`（一天一条的日初权益锚）。权益怎么从 Lighter 账户 API
取是 T8 的事 —— 这里 `equity_provider` 可注入，**T5 不写任何账户 API 调用**。

三条不做的事（每条都是踩过的坑）
--------------------------------
1. **不猜权益**。`equity_provider` 给了就必须给出正数；给不出来 → `equity=None`（
   `state_unavailable` 闸拒单），**不**悄悄退回库里的旧值来「让它能跑」。要旧值请显式
   传 `allow_stale_equity=True`，那时来源会标成 `store_stale`。
2. **不把缺失的回撤锚点当成 0 回撤**。见下节。
3. **不把「有仓位但算不出名义」当成 0 名义**。任何一仓的名义算不出 → 总名义 = `None`
   → `total_notional` 闸拒单（fail-closed），绝不 `sum()` 出一个偏小的数去「勉强放行」。

回撤锚点丢失 ≠ 回撤归零（本模块最重要的一条规则）
--------------------------------------------------
`dd_anchors` 只保留 30 天（面板要历史、表不能无限长），但 TWR 峰值是**回撤判据的全部
依据**：峰值丢了，回撤就算成 0，熔断等于被静默关掉 —— 而熔断恰恰是「永不自解」的那道闸。
因此装配顺序是**证据强弱**，而不是「哪张表有就用哪张」：

============================  ================  ====================================
证据                           来源标记          说明
============================  ================  ====================================
`dd_state` 行（持久、单行）    `dd_state`        权威。只有 `reset_dd_peak()` 能改小
`dd_anchors` 最新一行           `dd_anchor`      降级：峰值可能曾更高（历史被清理）
都没有                          `unavailable`     **index/peak = None → 拒单**
============================  ================  ====================================

**绝不**「从 `equity_curve` 最近 30 天重新算一个峰值」充数：那正是旧系统
（`larkspur8506/crypto-ma-alert` 的 `hl_snapshots` 30 天窗口）踩过的坑 —— 窗口一滚，回撤
凭空归零，熔断再也触发不了，且没有任何告警。真要重建基线只有一个正当入口：
T9 `dd.py` 对**全新账户**的一次性 bootstrap（空库、无持仓、无成交），或人工
`POST /admin/dd/reset`（带 admin token、落 `events` 审计）。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from app.risk import RiskState, drawdown as risk_drawdown
from app.store import StateStore, day_bounds, utc_day

__all__ = [
    "DD_SOURCE_ANCHOR", "DD_SOURCE_STATE", "DD_SOURCE_UNAVAILABLE",
    "EQUITY_SOURCE_PROVIDER", "EQUITY_SOURCE_STORE", "EQUITY_SOURCE_STALE",
    "EQUITY_SOURCE_UNAVAILABLE", "AccountSnapshot", "DDReading", "StateAssembler",
    "assemble_risk_state",
]

# 权益来源标记（面板状态条 / 日志直接用字符串判断，别解析文案）
EQUITY_SOURCE_PROVIDER = "provider"          # 注入的 provider（T8：Lighter 账户 API）
EQUITY_SOURCE_STORE = "store"                # 库里最后一个权益点
EQUITY_SOURCE_STALE = "store_stale"          # provider 失败后**显式**退回的旧值
EQUITY_SOURCE_UNAVAILABLE = "unavailable"    # 拿不到 → 拒单

DD_SOURCE_STATE = "dd_state"
DD_SOURCE_ANCHOR = "dd_anchor"
DD_SOURCE_UNAVAILABLE = "unavailable"


def _finite(value: Any) -> Optional[float]:
    """安全转 float：None / 垃圾 / NaN / ±inf → None。NaN 会让比较全部为 False。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


# ------------------------------------------------------------------ 回撤读数


@dataclass(frozen=True)
class DDReading:
    """一次回撤读数（含**证据强度**，面板和闸门读同一份）。

    * `available=False` 时 `index` / `peak_index` 都是 `None` —— 调用方必须把它当成
      「状态未知」处理（`RiskState` 的 `drawdown` 闸会拒单），**不能**当成 0%。
    * `degraded=True`：用了较弱的证据（历史锚点 / 过期状态 / 峰值被夹住），
      数值仍然可用，但面板该黄灯。
    """

    index: Optional[float] = None
    peak_index: Optional[float] = None
    source: str = DD_SOURCE_UNAVAILABLE
    available: bool = False
    degraded: bool = False
    reason: str = ""
    anchor_ts: Optional[float] = None
    updated_at: Optional[float] = None
    history_available: bool = False
    reset_count: int = 0

    @property
    def drawdown_pct(self) -> Optional[float]:
        """回撤比例（0.25 = 25%）；算不出 → `None`（**不猜 0**）。"""
        return risk_drawdown(self.index, self.peak_index)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "peak_index": self.peak_index,
            "drawdown": self.drawdown_pct, "source": self.source,
            "available": self.available, "degraded": self.degraded,
            "reason": self.reason, "anchor_ts": self.anchor_ts,
            "updated_at": self.updated_at, "history_available": self.history_available,
            "reset_count": int(self.reset_count),
        }


# ------------------------------------------------------------------ 账户快照


@dataclass(frozen=True)
class AccountSnapshot:
    """一次 tick 要用的账户状态（`RiskState` 的超集 + 每个字段的证据来源）。"""

    as_of: float
    day: str
    equity: Optional[float] = None
    equity_source: str = EQUITY_SOURCE_UNAVAILABLE
    day_start_equity: Optional[float] = None
    day_start_source: str = "missing"
    trades_today: Optional[int] = None
    trades_today_source: str = "missing"
    open_positions: Tuple[str, ...] = ()
    positions_source: str = "store"
    open_notional_total: Optional[float] = None
    open_notional_by_symbol: Mapping[str, float] = field(default_factory=dict)
    dd: DDReading = field(default_factory=DDReading)
    warnings: Tuple[str, ...] = ()

    # ---------------------------------------------------------------- 转换

    def to_risk_state(self) -> RiskState:
        """喂 `app.risk.check_entry()`。

        `open_positions` 是**标的元组**（与 T4 的 `RiskState.open_positions` 同型），
        不是计数 —— T4 用它判「同标的重仓」和「总数上限」，一个整数满足不了这两条。
        `dd_index` / `dd_peak_index` 在锚点不可用时是 `None`，交给闸门 fail-closed。
        """
        return RiskState(
            equity=self.equity,
            day_start_equity=self.day_start_equity,
            trades_today=self.trades_today,
            open_positions=tuple(self.open_positions),
            open_notional_total=self.open_notional_total,
            open_notional_by_symbol=dict(self.open_notional_by_symbol),
            dd_index=self.dd.index,
            dd_peak_index=self.dd.peak_index,
        )

    # ---------------------------------------------------------------- 展示

    @property
    def ok(self) -> bool:
        """关键字段是否齐全（状态条「正常/降级」用；不等于「允许开仓」）。"""
        return (self.equity is not None and self.day_start_equity is not None
                and self.trades_today is not None and self.dd.available)

    def describe(self) -> str:
        """一行可读摘要（塞进日志/面板状态条）。"""
        equity = "?" if self.equity is None else f"{self.equity:,.2f}"
        day_start = "?" if self.day_start_equity is None else f"{self.day_start_equity:,.2f}"
        trades = "?" if self.trades_today is None else str(self.trades_today)
        dd = self.dd.drawdown_pct
        dd_text = "unavailable" if dd is None else f"{dd:.2%}"
        notional = "?" if self.open_notional_total is None else f"{self.open_notional_total:,.2f}"
        return (f"equity={equity} ({self.equity_source}) "
                f"day_start={day_start} ({self.day_start_source}) "
                f"trades_today={trades} ({self.trades_today_source}) "
                f"open={list(self.open_positions) or '[]'} "
                f"notional={notional} "
                f"dd={dd_text} ({self.dd.source})"
                + (f" warnings={len(self.warnings)}" if self.warnings else ""))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "as_of": self.as_of, "day": self.day,
            "equity": self.equity, "equity_source": self.equity_source,
            "day_start_equity": self.day_start_equity,
            "day_start_source": self.day_start_source,
            "trades_today": self.trades_today,
            "trades_today_source": self.trades_today_source,
            "open_positions": list(self.open_positions),
            "positions_source": self.positions_source,
            "open_notional_total": self.open_notional_total,
            "open_notional_by_symbol": dict(self.open_notional_by_symbol),
            "dd": self.dd.as_dict(),
            "warnings": list(self.warnings),
            "ok": self.ok,
        }


# ------------------------------------------------------------------ 装配器


class StateAssembler:
    """把 store（+ 可注入的交易所读数）装配成 `AccountSnapshot` / `RiskState`。

    三个注入点都是**可选**的，默认实现只读 store（因此 T5 阶段就能全离线测）：

    `equity_provider`
        无参可调用对象，返回账户权益（正数，USDC）。T8 会注入 Lighter 账户 API 的读数。
        抛异常或返回非正数 → `equity=None`（fail-closed），除非显式
        `allow_stale_equity=True`（那时退回库里最后一个点，来源标 `store_stale`）。
    `price_provider`
        `symbol -> 标记价`。给了就用它算名义/浮盈口径；没给或给坏了退回库里的
        `entry_price`（**记录在案的真实成交价**，不是编的），并留一条 warning。
    `positions_provider`
        无参可调用对象，返回交易所侧持仓（每项带 `symbol`）。给了就与库内持仓做**并集**
        校验：交易所侧有、库里没有 → 记 warning（T8 的对账要处理），但 `open_positions`
        取**并集**（漏报一个持仓的后果是「同标的开第二仓」，比多报严重得多）。

    装配结果里每个字段都带 `*_source`，面板/日志不用猜。
    """

    def __init__(self, store: StateStore, *,
                 equity_provider: Optional[Callable[[], Any]] = None,
                 price_provider: Optional[Callable[[str], Any]] = None,
                 positions_provider: Optional[Callable[[], Any]] = None,
                 allow_stale_equity: bool = False,
                 dd_max_age_s: Optional[float] = 24 * 3600.0,
                 clock: Callable[[], float] = time.time) -> None:
        if store is None:
            raise ValueError("StateAssembler requires a StateStore")
        self.store = store
        self.equity_provider = equity_provider
        self.price_provider = price_provider
        self.positions_provider = positions_provider
        self.allow_stale_equity = bool(allow_stale_equity)
        self.dd_max_age_s = None if dd_max_age_s is None else float(dd_max_age_s)
        self.clock = clock

    # ---------------------------------------------------------------- 主入口

    def snapshot(self, now_ts: Optional[float] = None) -> AccountSnapshot:
        """装配一次（**只读**：不改库、不发网络请求）。"""
        stamp = self._stamp(now_ts)
        day = utc_day(stamp)
        warnings: List[str] = []

        equity, equity_source = self._equity(warnings)
        day_start, day_start_source = self._day_start(day, warnings)
        trades_today, trades_source = self._trades_today(day, warnings)
        symbols, positions_source = self._open_symbols(warnings)
        by_symbol, total = self._notional(symbols, warnings)
        dd = self.dd_reading(now_ts=stamp)
        if not dd.available:
            warnings.append(f"dd unavailable: {dd.reason}")
        elif dd.degraded:
            warnings.append(f"dd degraded ({dd.source}): {dd.reason}")

        return AccountSnapshot(
            as_of=stamp, day=day,
            equity=equity, equity_source=equity_source,
            day_start_equity=day_start, day_start_source=day_start_source,
            trades_today=trades_today, trades_today_source=trades_source,
            open_positions=symbols, positions_source=positions_source,
            open_notional_total=total, open_notional_by_symbol=by_symbol,
            dd=dd, warnings=tuple(warnings),
        )

    def risk_state(self, now_ts: Optional[float] = None) -> RiskState:
        """`snapshot().to_risk_state()` 的快捷方式（T8 executor 每次开仓前调一次）。"""
        return self.snapshot(now_ts).to_risk_state()

    # ---------------------------------------------------------------- 回撤

    def dd_reading(self, now_ts: Optional[float] = None) -> DDReading:
        """按证据强弱读回撤状态。**没有证据 → 不可用（`index=None`），永不返回 0。**"""
        stamp = self._stamp(now_ts)
        state: Dict[str, Any] = self.store.dd_state() or {}
        anchor = self.store.latest_dd_anchor()
        history_available = anchor is not None

        verdict = self._dd_from_state(state, stamp)
        if verdict is not None:
            return DDReading(index=verdict[0], peak_index=verdict[1],
                             source=DD_SOURCE_STATE, available=True,
                             degraded=verdict[2], reason=verdict[3],
                             anchor_ts=self._num(state.get("anchor_ts")),
                             updated_at=self._num(state.get("updated_at")),
                             history_available=history_available,
                             reset_count=int(state.get("reset_count") or 0))

        if anchor is not None:
            index = _finite(anchor.get("twr_index"))
            peak = _finite(anchor.get("twr_peak"))
            if index is not None and peak is not None and index >= 0 and peak > 0:
                degraded = True
                reason = ("dd_state row missing — falling back to the newest dd_anchors row; "
                          "the true peak may be higher (anchors are pruned after "
                          "30 days), treat the drawdown as a lower bound")
                if peak + 1e-12 < index:
                    peak = index
                    reason += "; peak < index in anchor, clamped to index"
                return DDReading(index=index, peak_index=peak, source=DD_SOURCE_ANCHOR,
                                 available=True, degraded=degraded, reason=reason,
                                 anchor_ts=self._num(anchor.get("ts")),
                                 updated_at=self._num(anchor.get("updated_at")
                                                      or anchor.get("created_at")),
                                 history_available=True)

        return DDReading(
            index=None, peak_index=None, source=DD_SOURCE_UNAVAILABLE, available=False,
            degraded=False,
            reason=("no dd_state row and no usable dd_anchors row — refusing to assume 0% "
                    "drawdown (that would silently disable the circuit breaker). "
                    "Bootstrap TWR state (T9 dd.py) or reset the peak via the admin endpoint"),
            history_available=history_available)

    def _dd_from_state(self, state: Optional[Mapping[str, Any]],
                       stamp: float) -> Optional[Tuple[float, float, bool, str]]:
        """`dd_state` 行 → `(index, peak, degraded, reason)`；行不成立 → `None`。"""
        if not state:
            return None
        index = _finite(state.get("twr_index"))
        peak = _finite(state.get("twr_peak"))
        if index is None or index < 0 or peak is None or peak <= 0:
            return None
        degraded = False
        reason = ""
        if peak + 1e-12 < index:
            # 峰值必须 ≥ 指数；反了说明状态被外力改坏。夹到 index（回撤 0）并标记降级 ——
            # 比继续返回一个负数（下游 `max(0, …)` 会静默变成 0）更容易被发现。
            peak = index
            degraded = True
            reason = "twr_peak < twr_index in dd_state — clamped to index (state is corrupt)"
        updated_at = self._num(state.get("updated_at"))
        if (self.dd_max_age_s is not None and updated_at is not None
                and stamp - updated_at > self.dd_max_age_s):
            degraded = True
            age_h = (stamp - updated_at) / 3600.0
            reason = (reason + "; " if reason else "") + (
                f"dd_state is stale ({age_h:.1f}h old, limit {self.dd_max_age_s / 3600.0:.1f}h) "
                f"— dd.py/watchdog should be updating it")
        return index, peak, degraded, reason

    # ---------------------------------------------------------------- 各字段

    def _equity(self, warnings: List[str]) -> Tuple[Optional[float], str]:
        if self.equity_provider is not None:
            try:
                raw = self.equity_provider()
            except Exception as exc:                                  # noqa: BLE001
                warnings.append(f"equity_provider raised {type(exc).__name__}: {exc}")
                raw = None
            value = _finite(raw)
            if value is not None and value > 0:
                return value, EQUITY_SOURCE_PROVIDER
            warnings.append(f"equity_provider returned {raw!r} (need a positive finite number)")
            if not self.allow_stale_equity:
                return None, EQUITY_SOURCE_UNAVAILABLE
            last = self.store.last_equity()
            stale = _finite((last or {}).get("equity"))
            if stale is not None and stale > 0:
                warnings.append("falling back to the last stored equity point "
                                "(allow_stale_equity=True) — it may be out of date")
                return stale, EQUITY_SOURCE_STALE
            return None, EQUITY_SOURCE_UNAVAILABLE

        last = self.store.last_equity()
        value = _finite((last or {}).get("equity"))
        if value is not None and value > 0:
            return value, EQUITY_SOURCE_STORE
        return None, EQUITY_SOURCE_UNAVAILABLE

    def _day_start(self, day: str, warnings: List[str]) -> Tuple[Optional[float], str]:
        """日初权益：计数行（权威） > 日初前最后一个权益点 > 当天第一个权益点。"""
        counter = self.store.daily_counter(day)
        value = _finite((counter or {}).get("day_start_equity"))
        if value is not None and value > 0:
            return value, "counter"
        start, _end = day_bounds(day)
        prev = self.store.previous_equity(start)
        value = _finite((prev or {}).get("equity"))
        if value is not None and value > 0:
            return value, "curve_prev"
        first = self.store.first_equity(since=start)
        value = _finite((first or {}).get("equity"))
        if value is not None and value > 0:
            warnings.append("day_start_equity taken from the first equity point of the day "
                            "(no counter row and no point before the day boundary)")
            return value, "curve_first"
        warnings.append("day_start_equity unavailable — the daily-loss gate will block entries "
                        "(fail-closed) until an equity point or counter row exists")
        return None, "missing"

    def _trades_today(self, day: str, warnings: List[str]) -> Tuple[Optional[int], str]:
        counter = self.store.daily_counter(day)
        stored = (counter or {}).get("trades_today")
        try:
            counted = int(self.store.entry_trade_count(day))
        except Exception as exc:                                      # noqa: BLE001
            warnings.append(f"trades_today unreadable ({type(exc).__name__}: {exc}) — "
                            f"the daily order limit cannot be evaluated")
            return None, "unavailable"
        stored_int = None if stored is None else int(stored)
        if stored_int is not None and stored_int > counted:
            # 计数行更大：迁移/运维动过，或成交没同步全 —— 取大者（更严的一侧）
            return stored_int, "counter"
        return counted, "trades"

    def _open_symbols(self, warnings: List[str]) -> Tuple[Tuple[str, ...], str]:
        """库内 `OPEN`/`CLOSING` 的标的 ∪ 交易所侧持仓标的（并集 = 更严）。"""
        try:
            symbols = set(self.store.open_symbols())
        except Exception as exc:                                      # noqa: BLE001
            warnings.append(f"store positions unreadable ({type(exc).__name__}: {exc}) — "
                            f"refusing to guess the position set")
            return (), "unavailable"
        if self.positions_provider is None:
            return tuple(sorted(symbols)), "store"
        try:
            raw = self.positions_provider() or ()
            external = {str(self._field(item, "symbol") or "").strip().upper()
                        for item in raw}
            external.discard("")
        except Exception as exc:                                      # noqa: BLE001
            warnings.append(f"positions_provider raised {type(exc).__name__}: {exc} — "
                            f"using the stored positions only")
            return tuple(sorted(symbols)), "store_fallback"
        missing = external - symbols
        if missing:
            warnings.append(f"exchange reports positions not in the store: {sorted(missing)} "
                            f"— reconciliation (T8) must adopt or flatten them")
        extra = symbols - external
        if extra:
            warnings.append(f"store has positions the exchange does not report: {sorted(extra)} "
                            f"— closing grace period or a mismatch (T8 decides)")
        return tuple(sorted(symbols | external)), "store+exchange"

    def _notional(self, symbols: Sequence[str],
                  warnings: List[str]) -> Tuple[Dict[str, float], Optional[float]]:
        """按标的算名义；**任何一仓算不出 → 总名义 `None`（fail-closed）**。"""
        if not symbols:
            return {}, 0.0
        rows = self.store.open_positions()
        by_symbol: Dict[str, float] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            qty = self._num(row.get("qty"))
            price = self._price(symbol, row, warnings)
            if qty is None or qty <= 0 or price is None:
                warnings.append(
                    f"cannot compute notional for {symbol or '?'} "
                    f"(qty={row.get('qty')!r}, price={price!r}) — "
                    f"the total-notional gate will block entries (fail-closed)")
                return {}, None
            by_symbol[symbol] = by_symbol.get(symbol, 0.0) + qty * price
        for symbol in symbols:
            if symbol not in by_symbol:
                # 交易所侧有仓位、库里没有：名义不明（数量未知）→ 不给一个编的数。
                warnings.append(f"no stored position row for {symbol} — notional unknown, "
                                f"total-notional gate will block entries (fail-closed)")
                return {}, None
        return by_symbol, sum(by_symbol.values())

    def _price(self, symbol: str, row: Mapping[str, Any],
               warnings: List[str]) -> Optional[float]:
        if self.price_provider is not None:
            try:
                value = _finite(self.price_provider(symbol))
            except Exception as exc:                                  # noqa: BLE001
                warnings.append(f"price_provider({symbol}) raised {type(exc).__name__}: {exc}")
                value = None
            if value is not None and value > 0:
                return value
            warnings.append(f"price_provider({symbol}) gave no usable price — "
                            f"falling back to the recorded entry price")
        entry = _finite(row.get("entry_price"))
        return entry if (entry is not None and entry > 0) else None

    # ---------------------------------------------------------------- 写（唯一一个）

    def ensure_day_start(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        """确保当天有计数行，并把**当前权益**写成日初权益（只写一次，之后不改）。

        这是本模块唯一的写操作，且必须由调用方**显式**调（T8 在每个 tick 开始时调）。
        `snapshot()` 永远不写 —— 让「读状态」保持无副作用，测试和面板才能随便调。
        """
        stamp = self._stamp(now_ts)
        equity, _source = self._equity([])
        return self.store.ensure_day(utc_day(stamp), day_start_equity=equity, ts=stamp)

    # ---------------------------------------------------------------- 小工具

    def _stamp(self, now_ts: Optional[float]) -> float:
        if now_ts is None:
            value = _finite(self.clock())
            return value if value is not None else time.time()
        value = _finite(now_ts)
        if value is None:
            raise ValueError(f"now_ts must be a finite epoch second, got {now_ts!r}")
        return value

    @staticmethod
    def _num(value: Any) -> Optional[float]:
        return _finite(value)

    @staticmethod
    def _field(item: Any, name: str) -> Any:
        """从 dict 或对象读字段（交易所侧持仓既可能是 dict 也可能是 SDK 对象）。"""
        if isinstance(item, Mapping):
            return item.get(name)
        return getattr(item, name, None)


# ------------------------------------------------------------------ 便捷函数


def assemble_risk_state(store: StateStore, *, now_ts: Optional[float] = None,
                        equity_provider: Optional[Callable[[], Any]] = None,
                        price_provider: Optional[Callable[[str], Any]] = None,
                        positions_provider: Optional[Callable[[], Any]] = None,
                        allow_stale_equity: bool = False) -> RiskState:
    """一次性装配（单次调用场景；循环里请复用 `StateAssembler` 实例）。"""
    assembler = StateAssembler(
        store, equity_provider=equity_provider, price_provider=price_provider,
        positions_provider=positions_provider, allow_stale_equity=allow_stale_equity)
    return assembler.risk_state(now_ts)
