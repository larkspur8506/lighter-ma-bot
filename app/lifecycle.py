"""持仓状态机与决策核心（T6）：把「信号 → 动作」编排起来，但**不下单**。

职责边界
--------
本模块只回答两个问题：**现在该不该开仓 / 该不该离场**，以及**状态怎么走**。
真正发单是 T7（`exchange.py`）的事，执行器的对账/重试是 T8 的事。所以这里有：

* **纯决策**：`decide_entry()` / `decide_exit()` / `decide()` 是**纯函数** —— 输入显式、
  输出 `Decision`（frozen dataclass），不读库、不发网络请求、不改任何状态。
* **状态机**：`Lifecycle.transition()` 校验转换 + 落 `events`；`open/begin_close/
  complete_close/abort_close` 是先经 `app/store.py` 落库、再落事件的一层薄封装。
* **记账**：`apply_entry()` / `apply_exit()` 把决策写进 T5 的表（`record_entry` /
  `begin_close` / `record_exit` / `record_stop`）。dry-run 与单测靠它落账；实盘里
  T8 拿到真实成交价后调同一批方法（或用 `transition()` 只补事件）。

状态机（`docs/PLAN.md` T6，与 `app/store.py::ALLOWED_TRANSITIONS` **同一张表**）::

    IDLE ──entry──▶ OPEN ──exit/stop──▶ CLOSING ──fill──▶ CLOSED
                     │                     │
                     └──reconcile(无仓)────▶│
                                           └──撤单失败/离场被拒──▶ OPEN（回退）

* `CLOSED` 是**行**的终态：同一标的下一笔交易**新开一行**（`UNIQUE(symbol, bar_ms)`），
  绝不把老行改回 `OPEN` → `CLOSED → OPEN` 一律抛 `IllegalTransition`。
* `CLOSING` 仍然是「有仓位」（交易所侧没确认平掉），风控继续把它算进 `open_positions`；
  它唯一的出口是 `CLOSED`（成交确认）或 `OPEN`（撤单失败 / 离场被拒的回退）。
* 没有第五个状态：**失败态就是停在 `CLOSING`**（有仓、等确认），由 T8 的对账收敛；
  本模块提供 `abort_close()` 这条显式回退路径，绝不静默把状态改回去。

三条硬规则（写死，有测试）
--------------------------
1. **减仓不受熔断阻挡**。`decide_exit()` 的函数签名里**没有** `risk_state` / `limits` ——
   它拿不到风控状态，也就不可能被 `kill_switch` / `daily_loss` / `drawdown` 拦住。
   `risk.check_entry()` 的 docstring 也写明「只管开仓，不管离场」；DD 熔断时**必须**
   仍然能平仓/止损，否则亏损中的仓位会被熔断锁死（旧系统的教训）。
2. **离场判据是冻结口径**：信号离场一律调用 `app.strategy.exit_triggered(bars, ind, i,
   side)`（收盘价 vs SMA20），本模块不重写判据、不复制常数。
3. **止损优先于信号离场**：同一根 bar 既触止损又破 SMA20 → 记 `kind='stop'`（成交价是
   止损价，不是收盘价）；且**入场当根只允许止损**（MA20 从入场根的下一根起才生效，
   与旧系统 `walk()` / 回测的 `u > ei` 守卫一致）。

决策顺序：**先 sizing，后 risk**（见 `decide_entry()` 的 docstring 论证）
--------------------------------------------------------------------
`risk.check_entry()` 的核心输入就是名义 `notional`（每标的/总名义上限按它算），而这个
名义只有 `sizing.plan_order()` 算得出来 —— 所以顺序是被依赖关系决定的，不是偏好。
两边都是纯函数、都无副作用；**两者都通过**才产出 `EntryIntent`，任一拒绝都返回
**结构化** `Rejection(stage, code, message, detail)`，绝不静默跳过。

为什么不发网络请求
------------------
`app.market` 只在 `TYPE_CHECKING` 下作类型引用（`MarketInfo` 是鸭子类型即可），
`app.store` 只被用来落库/落事件。所以本模块可以被纯离线单测直接驱动。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Sequence, Tuple

from app import strategy
from app.risk import RiskDecision, RiskLimits, RiskState, check_entry
from app.sizing import (OrderPlan, SizingConfig, SizingDecision, SizingRequest, plan_order,
                        to_int_price)
from app.store import ALLOWED_TRANSITIONS, OPEN_STATES, POSITION_STATES

if TYPE_CHECKING:                                            # pragma: no cover
    from app.market import MarketInfo
    from app.store import StateStore

__all__ = [
    "ACTION_ENTRY", "ACTION_EXIT", "ACTION_HOLD", "ACTION_SKIP", "ACTION_STOP",
    "BAR_INTERVAL_MS", "CLOSED", "CLOSING", "IDLE", "INTENT_ACTIONS", "INTERVAL_MS",
    "LEGAL_TRANSITIONS",
    "OPEN", "REJECT_CODES", "REJECT_BAD_POSITION", "REJECT_EQUITY_MISMATCH",
    "REJECT_NO_POSITION", "REJECT_NO_SIGNAL", "REJECT_SAME_BAR", "REJECT_SIGNAL_BAR",
    "REJECT_SYMBOL_BUSY", "STAGE_LIFECYCLE", "STAGE_RISK", "STAGE_SIZING", "STATES",
    "Decision", "IllegalTransition", "Lifecycle", "LifecycleError", "PositionView",
    "Rejection", "Transition", "decide", "decide_entry", "decide_exit",
]

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 状态机
# 状态名与 `app/store.py` **同一份**（`POSITION_STATES`），不另写字面量 —— 否则执行器与
# 库里的状态名一旦分叉，`set_position_state()` 会在运行时才炸。
STATES: Tuple[str, ...] = tuple(POSITION_STATES)
IDLE, OPEN, CLOSING, CLOSED = STATES

#: 允许的转换（PLAN T6）。内容与 `store.ALLOWED_TRANSITIONS` 相同，单测钉住两者相等。
LEGAL_TRANSITIONS: Mapping[str, Tuple[str, ...]] = dict(ALLOWED_TRANSITIONS)

#: 4h K 线宽度（与旧系统 `engine.INTERVAL_MS` 同值）：`positions.bar_ms` 记的是**信号 bar**
#: 的开盘时间，入场根 = 信号 bar + 一个间隔。
BAR_INTERVAL_MS = 4 * 3600 * 1000
INTERVAL_MS = BAR_INTERVAL_MS                     # 旧系统的名字，保留别名便于对读

# ------------------------------------------------------------------ 动作
ACTION_ENTRY = "entry"          # 开仓意图（sizing + risk 都通过）
ACTION_EXIT = "exit"            # 信号离场（收盘跌破/升破 SMA20），`kind='exit'`
ACTION_STOP = "stop"            # 止损离场（触及止损价），`kind='stop'`
ACTION_HOLD = "hold"            # 有仓位，本根不动
ACTION_SKIP = "skip"            # 有信号但不开仓 / 无信号（带结构化 reason）

# ------------------------------------------------------------------ 拒绝码
STAGE_LIFECYCLE = "lifecycle"   # 本模块自己的前置检查（同 bar / 已有仓 / 信号对不上 …）
STAGE_SIZING = "sizing"         # `app.sizing.plan_order()` 的拒单码原样透传
STAGE_RISK = "risk"             # `app.risk.check_entry()` 的闸门码原样透传

REJECT_NO_SIGNAL = "no_signal"                  # 本根没有入场信号
REJECT_SYMBOL_BUSY = "symbol_already_open"      # 该标的已有 OPEN/CLOSING 仓位（每标的至多一仓）
REJECT_SAME_BAR = "same_bar_already_traded"     # 该 bar 已经开过仓（`UNIQUE(symbol, bar_ms)`）
REJECT_SIGNAL_BAR = "signal_bar_mismatch"       # 信号不属于正在判定的这根 bar（回放错位）
REJECT_NO_POSITION = "no_position"              # 要判离场却没有活仓
REJECT_BAD_POSITION = "bad_position"            # 仓位缺关键字段（如没有止损价）
REJECT_EQUITY_MISMATCH = "equity_mismatch"      # 定仓用的权益 ≠ 风控看到的权益

REJECT_CODES: Tuple[str, ...] = (
    REJECT_NO_SIGNAL, REJECT_SYMBOL_BUSY, REJECT_SAME_BAR, REJECT_SIGNAL_BAR,
    REJECT_NO_POSITION, REJECT_BAD_POSITION, REJECT_EQUITY_MISMATCH,
)

#: 决策「有意图」的动作（T7/T8 只对这三种动作发单）
INTENT_ACTIONS: Tuple[str, ...] = (ACTION_ENTRY, ACTION_EXIT, ACTION_STOP)


# ------------------------------------------------------------------ 异常
class LifecycleError(RuntimeError):
    """本模块的可预期错误（状态机非法转换 / 仓位数据不可用）。"""


class IllegalTransition(LifecycleError):
    """非法的状态转换（如 `CLOSED → OPEN`、`IDLE → CLOSED`）。

    **绝不静默**：调用方拿到的是异常，不是「什么也没发生」。`CLOSED` 是行的终态，
    重开一笔要新开一行（`store.record_entry`，`UNIQUE(symbol, bar_ms)` 保证一 bar 一笔）。
    """


# ------------------------------------------------------------------ 纯数据结果
@dataclass(frozen=True)
class Rejection:
    """一条**结构化**拒绝理由：`code` 给程序判断、`message` 给人看、`detail` 给面板。"""

    stage: str
    code: str
    message: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"stage": self.stage, "code": self.code, "message": self.message,
                "detail": dict(self.detail)}

    def __str__(self) -> str:
        return f"[{self.stage}] {self.code}: {self.message}"


@dataclass(frozen=True)
class Transition:
    """一次状态转换（已落 `events`）。`event_id` 是 `store.events` 的行号。"""

    symbol: str
    from_state: str
    to_state: str
    reason: str = ""
    position_id: Optional[int] = None
    bar_ms: Optional[int] = None
    event_id: Optional[int] = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "from": self.from_state, "to": self.to_state,
                "reason": self.reason, "position_id": self.position_id,
                "bar_ms": self.bar_ms, "event_id": self.event_id,
                "detail": dict(self.detail)}

    def __str__(self) -> str:
        pid = "" if self.position_id is None else f" #{self.position_id}"
        return f"{self.symbol}{pid} {self.from_state}→{self.to_state} ({self.reason})"


@dataclass(frozen=True)
class Decision:
    """一次决策的**纯数据**结果（T10 面板直接渲染，`as_dict()` 可 JSON 序列化）。

    `action` ∈ `{entry, exit, stop, hold, skip}`：

    ==========  ==========================================================
    `entry`     开仓意图：`plan` 有值（含 `qty_int` / `stop_price` / `notional`）
    `exit`      信号离场意图：`exit_price` = 触发根的收盘价
    `stop`      止损离场意图：`exit_price` = 仓位的止损价
    `hold`      有仓位但本根不动（`reason` 说明为什么不判离场）
    `skip`      不开仓（`rejections` 必非空，说明被谁拒了）
    ==========  ==========================================================

    `from_state` / `to_state` 给出「这条决策会引发的状态转换」（不转换时两者相同）。
    T7/T8 只对 `is_intent` 为真的决策发单。
    """

    symbol: str
    action: str
    from_state: str
    to_state: str
    bar_ms: Optional[int] = None
    bar_index: Optional[int] = None
    side: int = 0
    position_id: Optional[int] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    exit_price: Optional[float] = None
    qty_int: Optional[int] = None
    price_decimals: Optional[int] = None
    size_decimals: Optional[int] = None
    notional: Optional[float] = None
    plan: Optional[OrderPlan] = None
    signal: Optional["strategy.Signal"] = None
    rejections: Tuple[Rejection, ...] = ()
    reason: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- 便捷

    @property
    def is_intent(self) -> bool:
        """是否需要 T7/T8 发单（开仓 / 信号离场 / 止损离场）。"""
        return self.action in INTENT_ACTIONS

    @property
    def ok(self) -> bool:
        """是否产出了意图（`skip` / `hold` 为假）。"""
        return self.is_intent

    @property
    def transitioned(self) -> bool:
        """这条决策是否会引起状态转换。"""
        return self.from_state != self.to_state

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(r.code for r in self.rejections)

    @property
    def reason_text(self) -> str:
        """一句人能读的理由（拒绝时拼接各条 rejection）。"""
        if self.reason:
            return self.reason
        return "; ".join(str(r) for r in self.rejections) or self.action

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "action": self.action, "is_intent": self.is_intent,
            "from_state": self.from_state, "to_state": self.to_state,
            "transitioned": self.transitioned,
            "bar_ms": self.bar_ms, "bar_index": self.bar_index, "side": self.side,
            "position_id": self.position_id, "entry_price": self.entry_price,
            "stop_price": self.stop_price, "exit_price": self.exit_price,
            "qty_int": self.qty_int, "price_decimals": self.price_decimals,
            "size_decimals": self.size_decimals, "notional": self.notional,
            "plan": self.plan.as_dict() if self.plan is not None else None,
            "signal": _signal_dict(self.signal),
            "rejections": [r.as_dict() for r in self.rejections],
            "codes": list(self.codes), "reason": self.reason_text,
            "detail": dict(self.detail),
        }

    def __str__(self) -> str:
        state = (f"{self.from_state}→{self.to_state}" if self.transitioned
                 else self.from_state)
        if self.action == ACTION_ENTRY and self.plan is not None:
            return (f"{self.symbol} ENTRY {self.plan.base_qty:g} @ {self.plan.entry_price:g} "
                    f"stop {self.plan.stop_price:g} ({state})")
        if self.action in (ACTION_EXIT, ACTION_STOP):
            return f"{self.symbol} {self.action.upper()} @ {self.exit_price:g} ({state})"
        return f"{self.symbol} {self.action.upper()} ({state}) {self.reason_text}"


def _signal_dict(sig: Optional["strategy.Signal"]) -> Optional[Dict[str, Any]]:
    if sig is None:
        return None
    return {"bar_index": int(sig.bar_index), "bar_open_ms": int(sig.bar_open_ms),
            "side": int(sig.side), "entry_price": float(sig.entry_price),
            "stop_price": float(sig.stop_price), "stop_dist": float(sig.stop_dist),
            "band": float(sig.band)}


@dataclass(frozen=True)
class PositionView:
    """判离场需要的仓位字段（`store.positions` 一行的**只读**投影）。

    `bar_ms` 是**信号 bar** 的开盘时间（`record_entry(bar_ms=...)`），入场根 =
    `bar_ms + INTERVAL_MS`。用独立 dataclass 而不是裸 dict：决策函数因此不依赖 DB，
    也不会因为某张表新增一列就改变语义。
    """

    position_id: Optional[int]
    symbol: str
    side: int
    state: str = OPEN
    qty_int: int = 0
    size_decimals: int = 0
    entry_price: float = 0.0
    entry_price_int: Optional[int] = None
    stop_price: Optional[float] = None
    stop_price_int: Optional[int] = None
    price_decimals: int = 0
    bar_ms: Optional[int] = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "PositionView":
        """从 `store.positions` 的一行构造（缺字段用 dataclass 默认值，不猜数）。"""
        if not isinstance(row, Mapping):
            raise LifecycleError(f"position row must be a mapping, got {type(row).__name__}")
        return cls(
            position_id=None if row.get("id") is None else int(row["id"]),
            symbol=str(row.get("symbol") or "").upper(),
            side=int(row.get("side") or 0),
            state=str(row.get("state") or OPEN).upper(),
            qty_int=int(row.get("qty_int") or 0),
            size_decimals=int(row.get("size_decimals") or 0),
            entry_price=float(row.get("entry_price") or 0.0),
            entry_price_int=(None if row.get("entry_price_int") is None
                             else int(row["entry_price_int"])),
            stop_price=(None if row.get("stop_price") is None else float(row["stop_price"])),
            stop_price_int=(None if row.get("stop_price_int") is None
                            else int(row["stop_price_int"])),
            price_decimals=int(row.get("price_decimals") or 0),
            bar_ms=None if row.get("bar_ms") is None else int(row["bar_ms"]),
        )

    @classmethod
    def coerce(cls, position: Any) -> "PositionView":
        return position if isinstance(position, cls) else cls.from_row(position)

    @property
    def is_long(self) -> bool:
        return self.side == 1

    @property
    def entry_bar_ms(self) -> Optional[int]:
        """入场根的开盘时间（信号 bar + 一个 4h 间隔）。"""
        return None if self.bar_ms is None else int(self.bar_ms) + INTERVAL_MS

    @property
    def live(self) -> bool:
        return self.state in OPEN_STATES


# ------------------------------------------------------------------ 入口决策


def decide_entry(signal: Optional["strategy.Signal"], *,
                 equity: Optional[float],
                 market_info: Optional["MarketInfo"],
                 risk_state: Optional[RiskState],
                 limits: Optional[RiskLimits] = None,
                 sizing_config: Optional[SizingConfig] = None,
                 position: Any = None,
                 bar_taken: bool = False,
                 bars: Optional[Sequence["strategy.Bar"]] = None,
                 i: Optional[int] = None) -> Decision:
    """开仓决策（**纯函数**）：`sizing.plan_order()` → `risk.check_entry()`。

    顺序为什么是 **sizing 在前、risk 在后**
    ----------------------------------------
    `risk.check_entry()` 的关键入参是 `notional`（每标的名义上限 = 权益 × 1.5、总名义上限 =
    权益 × 2.5、`bad_notional` 闸），而这个 `notional` 只有 `sizing.plan_order()` 算得出来
    （风险定仓 + 精度 floor + 最小量之后的数量 × 价格）。所以顺序是**依赖关系**决定的：
    先 risk 就得先瞎猜一个名义，那等于让风控去看一个不是真实下单量的数。

    fail-safe 的方向也不受影响：两个函数都是**纯函数、无副作用**（不下单、不写库），
    且**两者都通过才产出意图** —— risk 是最后一道闸，任何一条不满足都不会有
    `EntryIntent` 漏出去。反过来，sizing 先跑还多一层保护：市场规则缺失
    （未注册标的 / 冻结 / 无 `min_initial_margin_fraction`）在 sizing 就 fail-closed，
    连名义都算不出来，自然也不会去问风控「这笔能不能下」。

    前置检查（都在本层，`stage=lifecycle`）
    --------------------------------------
    1. `position` 是活仓（`OPEN`/`CLOSING`）→ `symbol_already_open`（每标的至多一仓）
    2. `bar_taken`（该 bar 已有仓位行）→ `same_bar_already_traded`（与 T5 的
       `UNIQUE(symbol, bar_ms)` 呼应；重启后重放同一根 bar 不会开出第二仓）
    3. `signal is None` → `no_signal`
    4. 信号 bar 与正在判定的 bar 不一致 → `signal_bar_mismatch`（回放错位，不猜）
    5. `equity` 与 `risk_state.equity` 不一致 → `equity_mismatch`（定仓与风控必须看同一个权益，
       否则「按 A 算仓、按 B 放行」）

    返回：`action='entry'` 且 `plan` 有值，或 `action='skip'` 且 `rejections` 非空。
    """
    sym = str((signal.symbol if signal is not None else "") or
              (getattr(position, "symbol", "") if position is not None else "")).upper()
    idx = None if i is None else int(i)
    bar = bars[idx] if (bars and idx is not None and 0 <= idx < len(bars)) else None
    bar_ms = int(bar.open_ms) if bar is not None else (
        int(signal.bar_open_ms) if signal is not None else None)

    base = dict(symbol=sym, from_state=IDLE, to_state=IDLE, bar_ms=bar_ms, bar_index=idx,
                side=int(signal.side) if signal is not None else 0, signal=signal)

    def skip(code: str, message: str, detail: Optional[Mapping[str, Any]] = None,
             stage: str = STAGE_LIFECYCLE) -> Decision:
        return Decision(action=ACTION_SKIP,
                        rejections=(Rejection(stage=stage, code=code, message=message,
                                              detail=dict(detail or {})),),
                        reason=message, detail={"stage": stage}, **base)

    # ① 已有活仓 → 一标的至多一仓
    pv: Optional[PositionView] = None
    if position is not None:
        pv = PositionView.coerce(position)
        if pv.live:
            return skip(REJECT_SYMBOL_BUSY,
                        f"{sym or pv.symbol} already has an {pv.state} position "
                        f"(id={pv.position_id}) — one position per symbol",
                        {"position_id": pv.position_id, "state": pv.state})

    # ② 同一根 bar 只开一仓（T5 的 UNIQUE(symbol, bar_ms)）
    if bar_taken:
        return skip(REJECT_SAME_BAR,
                    f"{sym}: a position already exists for bar {bar_ms} — "
                    f"one entry per bar (UNIQUE(symbol, bar_ms))",
                    {"bar_ms": bar_ms})

    # ③ 没有信号
    if signal is None:
        return skip(REJECT_NO_SIGNAL, f"{sym or 'symbol'}: no entry signal on this bar",
                    {"bar_ms": bar_ms})

    # ④ 信号必须属于正在判定的这根 bar
    if bar is not None and int(signal.bar_open_ms) != int(bar.open_ms):
        return skip(REJECT_SIGNAL_BAR,
                    f"{sym}: signal belongs to bar {signal.bar_open_ms} but the decision bar "
                    f"is {bar.open_ms} — refusing to act on a stale/misaligned signal",
                    {"signal_bar_ms": int(signal.bar_open_ms), "bar_ms": int(bar.open_ms)})

    # ⑤ 定仓与风控必须看同一份权益
    eq = _num(equity)
    if risk_state is not None and risk_state.equity is not None and eq is not None:
        if abs(float(risk_state.equity) - eq) > 1e-9:
            return skip(REJECT_EQUITY_MISMATCH,
                        f"{sym}: sizing equity {eq:g} != risk-state equity "
                        f"{float(risk_state.equity):g} — refusing to size and gate on "
                        f"different account states",
                        {"equity": eq, "risk_state_equity": float(risk_state.equity)})

    # ⑥ sizing：市场规则 + 风险定仓 + 精度 + 最小量
    request = SizingRequest(symbol=sym or signal.symbol, side=int(signal.side),
                            entry_price=float(signal.entry_price),
                            stop_price=float(signal.stop_price),
                            equity=float(eq or 0.0))
    sizing_decision: SizingDecision = plan_order(request, market_info,
                                                 config=sizing_config)
    if not sizing_decision.ok or sizing_decision.plan is None:
        rej = sizing_decision.rejection
        code = rej.code if rej is not None else "sizing_failed"
        message = rej.message if rej is not None else f"{sym}: sizing produced no plan"
        return skip(code, message, dict(rej.detail) if rej is not None else {}, STAGE_SIZING)
    plan = sizing_decision.plan

    # ⑦ risk：最后一道闸（拿着**真实**名义去问）
    risk_decision: RiskDecision = check_entry(
        plan.symbol, plan.notional,
        risk_state if risk_state is not None else RiskState(), limits=limits)
    if not risk_decision.allowed:
        blocked = risk_decision.blocked
        rejections = tuple(
            Rejection(stage=STAGE_RISK, code=g.code or g.gate, message=g.message or g.code,
                      detail=dict(g.detail)) for g in blocked)
        return Decision(symbol=plan.symbol or sym, action=ACTION_SKIP, from_state=IDLE,
                        to_state=IDLE, bar_ms=bar_ms, bar_index=idx, side=int(plan.side),
                        entry_price=float(plan.entry_price),
                        stop_price=float(plan.stop_price), qty_int=int(plan.qty_int),
                        price_decimals=int(plan.price_decimals),
                        size_decimals=int(plan.size_decimals), notional=float(plan.notional),
                        plan=plan, signal=signal, rejections=rejections,
                        reason=risk_decision.reason, detail={"stage": STAGE_RISK})

    # ⑧ 两个都通过 → 开仓意图
    return Decision(
        symbol=plan.symbol or sym, action=ACTION_ENTRY, from_state=IDLE, to_state=OPEN,
        bar_ms=bar_ms, bar_index=idx, side=int(plan.side),
        entry_price=float(plan.entry_price), stop_price=float(plan.stop_price),
        qty_int=int(plan.qty_int), price_decimals=int(plan.price_decimals),
        size_decimals=int(plan.size_decimals), notional=float(plan.notional),
        plan=plan, signal=signal,
        reason=(f"sizing ok + risk allowed: {plan.base_qty:g} "
                f"{'long' if plan.side == 1 else 'short'} @ {plan.entry_price:g} "
                f"stop {plan.stop_price:g} notional {plan.notional:.2f}"),
        detail={"stage": "approved", "leverage": float(plan.leverage),
                "risk_amount": float(plan.risk_amount),
                "risk_pct_actual": float(plan.risk_pct_actual),
                "warnings": list(plan.warnings)},
    )


# ------------------------------------------------------------------ 离场决策


def decide_exit(position: Any, bars: Sequence["strategy.Bar"],
                ind: Mapping[str, Sequence[Optional[float]]], i: int, *,
                symbol: Optional[str] = None) -> Decision:
    """离场决策（**纯函数**）：止损优先，其次冻结口径的信号离场。

    **注意函数签名里没有 `risk_state` / `limits`** —— 减仓/止损永远不被
    `kill_switch`、`daily_loss`、`drawdown` 这些「只挡开新仓」的闸拦住。这不是省略，
    是写死的规则（`docs/LIFECYCLE.md`「减仓不受熔断阻挡」）：DD 熔断时把亏损仓位锁在
    场上，是旧系统明确要避免的事故。

    判定顺序
    --------
    1. 仓位不是活仓（`IDLE`/`CLOSED`）→ `skip / no_position`（不该被问到，问到就报出来）
    2. 已经是 `CLOSING` → `hold`：离场单已发、等交易所确认，**不重复发单**
    3. 还没到入场根（`bar.open_ms < position.bar_ms + 4h`）→ `hold`
    4. **止损**：多单 `bar.low <= stop`、空单 `bar.high >= stop` → `action='stop'`，
       `exit_price = stop`（成交价是止损价；滑点由 T7 的 reduce-only 腿处理）
    5. **信号离场**：`app.strategy.exit_triggered(bars, ind, i, side)`（收盘 vs SMA20）
       → `action='exit'`，`exit_price = bar.close`。
       入场当根**不判** MA20（只允许止损），与旧系统 `walk()` / 回测的 `u > ei` 守卫一致。
    6. 都没触发 → `hold`

    止损与信号离场必须分得清：前者写 `kind='stop'`、后者 `kind='exit'`（收益统计/面板
    配色/复盘都依赖这个区别），所以这里是两个动作，不是「一个平仓带个 flag」。
    """
    pv = PositionView.coerce(position)
    sym = str(symbol or pv.symbol).upper()
    idx = int(i)
    if bars is None or idx < 0 or idx >= len(bars):
        raise LifecycleError(f"{sym}: bar index {i} out of range (bars={0 if bars is None else len(bars)})")
    bar = bars[idx]
    base = dict(symbol=sym, from_state=pv.state, to_state=pv.state, bar_ms=int(bar.open_ms),
                bar_index=idx, side=int(pv.side), position_id=pv.position_id,
                entry_price=float(pv.entry_price) if pv.entry_price else None,
                stop_price=None if pv.stop_price is None else float(pv.stop_price),
                qty_int=int(pv.qty_int) if pv.qty_int else None,
                price_decimals=int(pv.price_decimals), size_decimals=int(pv.size_decimals))

    def hold(reason: str) -> Decision:
        return Decision(action=ACTION_HOLD, reason=reason, **base)

    # ① 没有活仓
    if not pv.live:
        return Decision(
            action=ACTION_SKIP, reason=f"{sym}: position {pv.position_id} is {pv.state}",
            rejections=(Rejection(stage=STAGE_LIFECYCLE, code=REJECT_NO_POSITION,
                                  message=f"{sym}: no live position to exit (state={pv.state})",
                                  detail={"position_id": pv.position_id,
                                          "state": pv.state}),),
            **base)

    # ② 已经在平仓中：离场单已发，等确认，不重复发
    if pv.state == CLOSING:
        return hold(f"{sym}: already CLOSING — waiting for the exchange to confirm")

    # ③ 还没到入场根
    entry_bar_ms = pv.entry_bar_ms
    if entry_bar_ms is not None and int(bar.open_ms) < int(entry_bar_ms):
        return hold(f"{sym}: bar {int(bar.open_ms)} precedes the entry bar {entry_bar_ms}")

    # ④ 止损优先
    if pv.stop_price is None:
        # 没有止损价 = 仓位不可能被 T7 正常开出来；不静默、也不假装能判止损。
        rej = Rejection(stage=STAGE_LIFECYCLE, code=REJECT_BAD_POSITION,
                        message=(f"{sym}: position {pv.position_id} has no stop price — "
                                 f"stop-trigger cannot be evaluated; the signal exit is "
                                 f"still evaluated below"),
                        detail={"position_id": pv.position_id})
        stop_hit = False
    else:
        rej = None
        stop_hit = (float(bar.low) <= float(pv.stop_price) if pv.is_long
                    else float(bar.high) >= float(pv.stop_price))

    is_entry_bar = entry_bar_ms is not None and int(bar.open_ms) == int(entry_bar_ms)

    if stop_hit:
        return Decision(
            action=ACTION_STOP, from_state=pv.state, to_state=CLOSING,
            exit_price=float(pv.stop_price),
            reason=(f"{sym}: {'low' if pv.is_long else 'high'} "
                    f"{float(bar.low) if pv.is_long else float(bar.high):g} hit stop "
                    f"{float(pv.stop_price):g}"),
            detail={"kind": "stop", "is_entry_bar": is_entry_bar,
                    "trigger": "stop_price",
                    "bar_low": float(bar.low), "bar_high": float(bar.high)},
            **{k: v for k, v in base.items() if k not in ("from_state", "to_state")})

    triggered = False if is_entry_bar else bool(
        strategy.exit_triggered(bars, ind, idx, int(pv.side)))

    rejections = () if rej is None else (rej,)
    if triggered:
        sma20 = ind["sma20"][idx]
        return Decision(
            action=ACTION_EXIT, from_state=pv.state, to_state=CLOSING,
            exit_price=float(bar.close), rejections=rejections,
            reason=(f"{sym}: close {float(bar.close):g} "
                    f"{'<' if pv.is_long else '>'} SMA20 {float(sma20):g} "
                    f"(strategy.exit_triggered)"),
            detail={"kind": "exit", "is_entry_bar": is_entry_bar, "trigger": "sma20",
                    "close": float(bar.close),
                    "sma20": None if sma20 is None else float(sma20)},
            **{k: v for k, v in base.items() if k not in ("from_state", "to_state")})

    return Decision(
        action=ACTION_HOLD, from_state=pv.state, to_state=pv.state,
        rejections=rejections,
        reason=(f"{sym}: no exit trigger on bar {int(bar.open_ms)}"
                + ("" if is_entry_bar else " (close on the stop side of SMA20)")),
        detail={"is_entry_bar": is_entry_bar, "stop_evaluable": rej is None,
                "close": float(bar.close)},
        **{k: v for k, v in base.items() if k not in ("from_state", "to_state")})


def decide(symbol: str, bars: Sequence["strategy.Bar"], *,
           ind: Optional[Mapping[str, Sequence[Optional[float]]]] = None,
           i: Optional[int] = None,
           signal: Optional["strategy.Signal"] = None,
           equity: Optional[float] = None,
           market_info: Optional["MarketInfo"] = None,
           risk_state: Optional[RiskState] = None,
           position: Any = None,
           bar_taken: bool = False,
           limits: Optional[RiskLimits] = None,
           sizing_config: Optional[SizingConfig] = None) -> Decision:
    """一次 tick 的决策（**纯函数**）：有活仓判离场，否则判开仓。

    `ind` 不给就用 `app.strategy.compute_indicators(bars)` 现算（回放/测试方便；
    实盘 tick 由调用方传入，别每根 bar 重算）。`i` 缺省 = 最后一根（最新已收盘 bar）。

    **离场分支完全跳过风控**（见 `decide_exit()`）：`risk_state` 只进开仓分支。
    """
    n = len(bars or ())
    if n == 0:
        raise LifecycleError(f"{symbol}: no bars to decide on")
    idx = n - 1 if i is None else int(i)
    if idx < 0 or idx >= n:
        raise LifecycleError(f"{symbol}: bar index {idx} out of range (bars={n})")
    indicators = ind if ind is not None else strategy.compute_indicators(bars)

    pv: Optional[PositionView] = None
    if position is not None:
        pv = PositionView.coerce(position)
    if pv is not None and pv.live:
        out = decide_exit(pv, bars, indicators, idx, symbol=symbol)
        return out

    return decide_entry(signal, equity=equity, market_info=market_info,
                        risk_state=risk_state, limits=limits,
                        sizing_config=sizing_config, position=pv, bar_taken=bar_taken,
                        bars=bars, i=idx)


# ------------------------------------------------------------------ 编排器


class Lifecycle:
    """状态机 + 决策的编排器（**不发网络请求**，只读写 T5 的库）。

    用法::

        lc = Lifecycle(store, limits=RiskLimits(), sizing_config=SizingConfig())
        d = lc.decide("BTC", bars, signal=sig, equity=snap.equity,
                      market_info=info, risk_state=snap.to_risk_state())
        if d.action == "entry":
            lc.apply_entry(d)                  # 记账（真实下单在 T7/T8）

    `store` 可以是任何提供 `record_entry` / `begin_close` / `record_exit` /
    `record_stop` / `set_position_state` / `log_event` / `position` / `open_positions` /
    `position_by_bar` 的对象 —— 单测可以塞一个假库。
    """

    def __init__(self, store: "StateStore", *,
                 limits: Optional[RiskLimits] = None,
                 sizing_config: Optional[SizingConfig] = None) -> None:
        if store is None:
            raise LifecycleError("Lifecycle requires a store (T5 StateStore)")
        self.store = store
        self.limits = limits if limits is not None else RiskLimits()
        self.sizing_config = sizing_config if sizing_config is not None else SizingConfig()

    def __repr__(self) -> str:
        return f"Lifecycle(store={type(self.store).__name__})"

    # ---------------------------------------------------------------- 状态视图

    def open_row(self, symbol: str) -> Optional[Dict[str, Any]]:
        """该标的的活仓行（`OPEN`/`CLOSING`），没有返回 `None`。"""
        wanted = str(symbol or "").strip().upper()
        for row in self.store.open_positions():
            if str(row.get("symbol") or "").strip().upper() == wanted:
                return dict(row)
        return None

    def position_view(self, symbol: str) -> Optional[PositionView]:
        row = self.open_row(symbol)
        return None if row is None else PositionView.from_row(row)

    def state(self, symbol: str) -> str:
        """该标的的**当前**状态（槽位视角）。

        有活仓 → 那一行的状态；没有活仓 → `IDLE`。`CLOSED` 是**行**的终态（历史上那一笔
        已关账），槽位回到 `IDLE`，下一笔是新行 —— 所以「`CLOSED` 之后又能开仓」与
        「`CLOSED → OPEN` 非法」并不矛盾：前者是新行 `IDLE → OPEN`，后者是改老行。
        """
        row = self.open_row(symbol)
        return IDLE if row is None else str(row.get("state") or IDLE).upper()

    def bar_taken(self, symbol: str, bar_ms: Optional[int]) -> bool:
        """该 bar 是否已有仓位行（`UNIQUE(symbol, bar_ms)`）。"""
        if bar_ms is None:
            return False
        return self.store.position_by_bar(symbol, int(bar_ms)) is not None

    # ---------------------------------------------------------------- 决策

    def decide(self, symbol: str, bars: Sequence["strategy.Bar"], *,
               ind: Optional[Mapping[str, Sequence[Optional[float]]]] = None,
               i: Optional[int] = None,
               signal: Optional["strategy.Signal"] = None,
               equity: Optional[float] = None,
               market_info: Optional["MarketInfo"] = None,
               risk_state: Optional[RiskState] = None) -> Decision:
        """读库拿当前仓位 → 调 `decide()`（纯函数）→ 返回 `Decision`。"""
        sym = str(symbol or "").strip().upper()
        position = self.position_view(sym)
        taken = self.bar_taken(sym, None if signal is None else int(signal.bar_open_ms))
        return decide(sym, bars, ind=ind, i=i, signal=signal, equity=equity,
                      market_info=market_info, risk_state=risk_state,
                      position=position, bar_taken=taken,
                      limits=self.limits, sizing_config=self.sizing_config)

    # ---------------------------------------------------------------- 状态机

    def transition(self, symbol: str, to_state: str, *,
                   from_state: Optional[str] = None,
                   position_id: Optional[int] = None,
                   reason: str = "",
                   bar_ms: Optional[int] = None,
                   detail: Optional[Mapping[str, Any]] = None,
                   ts: Optional[float] = None) -> Transition:
        """校验一次状态转换并落 `events`（不改库里的 `state` —— 落库由语义方法负责）。

        * `from_state` / `to_state` 不在 `LEGAL_TRANSITIONS` 里 → `IllegalTransition`。
        * `from_state` 缺省：给了 `position_id` 取该行当前状态，否则取槽位状态。
        * 事件 `kind='transition'`，`data={from,to,reason,position_id,bar_ms}` —— T10 的
          「执行器事件流」直接渲染，审计也能按 `position_id` 串起一笔交易的完整生命周期。
        """
        sym = str(symbol or "").strip().upper()
        target = _norm_state(to_state)
        if target not in STATES:
            raise IllegalTransition(f"{sym}: unknown position state {to_state!r}")
        if from_state is not None:
            source = _norm_state(from_state)
            if source not in STATES:
                raise IllegalTransition(f"{sym}: unknown position state {from_state!r}")
        elif position_id is not None:
            row = self.store.position(int(position_id))
            if row is None:
                raise LifecycleError(f"{sym}: position {position_id} not found")
            source = _norm_state(row.get("state"))
        else:
            source = self.state(sym)

        _check_transition(sym, source, target, position_id=position_id)
        payload = {"from": source, "to": target, "reason": reason,
                   "position_id": None if position_id is None else int(position_id),
                   "bar_ms": None if bar_ms is None else int(bar_ms)}
        if detail:
            payload.update({k: v for k, v in dict(detail).items() if k not in payload})
        event_id = self.store.log_event(
            "transition",
            f"{sym} {source}→{target}" + (f" ({reason})" if reason else ""),
            symbol=sym, data=payload, ts=ts)
        return Transition(symbol=sym, from_state=source, to_state=target, reason=reason,
                          position_id=None if position_id is None else int(position_id),
                          bar_ms=None if bar_ms is None else int(bar_ms),
                          event_id=event_id, detail=dict(detail or {}))

    # ---------------------------------------------------------------- 落库

    def apply_entry(self, decision: Decision, *,
                    entry_price_int: Optional[int] = None,
                    order_id: Optional[str] = None,
                    stop_order_id: Optional[str] = None,
                    equity: Optional[float] = None,
                    ts: Optional[float] = None) -> Transition:
        """把开仓意图记进库（`IDLE → OPEN`）：仓位行 + 开仓成交 + `events`。

        **不下单**。`entry_price_int` 缺省用 `plan.entry_price` 按 `price_decimals` floor；
        实盘里 T8 应传入真实成交价的整数表示（否则仓位行记的是参考价）。
        """
        if decision.action != ACTION_ENTRY or decision.plan is None:
            raise LifecycleError(f"apply_entry needs an entry intent, got {decision.action!r}")
        plan = decision.plan
        price_int = (int(entry_price_int) if entry_price_int is not None
                     else to_int_price(float(plan.entry_price), int(plan.price_decimals)))
        result = self.store.record_entry(
            plan.symbol or decision.symbol, int(plan.side),
            qty_int=int(plan.qty_int), size_decimals=int(plan.size_decimals),
            entry_price_int=price_int, price_decimals=int(plan.price_decimals),
            stop_price_int=int(plan.stop_price_int), bar_ms=int(decision.bar_ms),
            market_id=int(plan.market_id), entry_order_id=order_id,
            stop_order_id=stop_order_id,
            equity_at_entry=_num(equity) if equity is not None else plan.equity,
            ts=ts)
        position_id = int(result.position_id or 0) or None
        return self.transition(
            plan.symbol or decision.symbol, OPEN, from_state=IDLE, position_id=position_id,
            reason="entry", bar_ms=decision.bar_ms, ts=ts,
            detail={"qty_int": int(plan.qty_int), "entry_price_int": price_int,
                    "stop_price_int": int(plan.stop_price_int),
                    "notional": float(plan.notional),
                    "event": "open", "trade_id": result.trade_id})

    def begin_close(self, position_id: int, *, reason: str = "exit",
                    bar_ms: Optional[int] = None, ts: Optional[float] = None) -> Transition:
        """`OPEN → CLOSING`：离场/止损单已发出，等交易所确认。"""
        row = self.store.position(int(position_id))
        if row is None:
            raise LifecycleError(f"position {position_id} not found")
        sym = str(row.get("symbol") or "")
        source = _norm_state(row.get("state"))
        _check_transition(sym, source, CLOSING, position_id=int(position_id))
        self.store.begin_close(int(position_id), ts=ts, reason=reason)
        return self.transition(sym, CLOSING, from_state=source, position_id=int(position_id),
                              reason=reason, bar_ms=bar_ms, ts=ts)

    def complete_close(self, position_id: int, *, exit_price_int: int,
                       kind: str = "exit", reason: Optional[str] = None,
                       fill_id: Optional[str] = None, fee: float = 0.0,
                       realized_pnl: Optional[float] = None,
                       price_decimals: Optional[int] = None,
                       bar_ms: Optional[int] = None,
                       ts: Optional[float] = None) -> Transition:
        """`CLOSING`/`OPEN → CLOSED`：成交确认后关账（`kind` = `exit` / `stop`）。"""
        trade_kind = str(kind or "exit").strip().lower()
        if trade_kind not in ("exit", "stop"):
            raise LifecycleError(f"exit kind must be 'exit' or 'stop', got {kind!r}")
        row = self.store.position(int(position_id))
        if row is None:
            raise LifecycleError(f"position {position_id} not found")
        sym = str(row.get("symbol") or "")
        source = _norm_state(row.get("state"))
        _check_transition(sym, source, CLOSED, position_id=int(position_id))
        kwargs: Dict[str, Any] = dict(exit_price_int=int(exit_price_int),
                                      price_decimals=price_decimals, fill_id=fill_id,
                                      fee=fee, realized_pnl=realized_pnl,
                                      reason=reason or trade_kind, ts=ts)
        if trade_kind == "stop":
            self.store.record_stop(int(position_id), **kwargs)
        else:
            self.store.record_exit(int(position_id), kind="exit", **kwargs)
        return self.transition(sym, CLOSED, from_state=source, position_id=int(position_id),
                              reason=reason or trade_kind, bar_ms=bar_ms, ts=ts,
                              detail={"kind": trade_kind, "exit_price_int": int(exit_price_int)})

    def abort_close(self, position_id: int, *, reason: str = "close_rejected",
                    ts: Optional[float] = None) -> Transition:
        """`CLOSING → OPEN`：**显式回退**（撤单失败 / 离场单被拒 / 交易所侧还有仓）。

        这是状态机里唯一的「往回走」路径。它必须显式发生并落事件：静默把仓位当平掉，
        就是「本地无仓、交易所有仓」的对账事故。
        """
        row = self.store.position(int(position_id))
        if row is None:
            raise LifecycleError(f"position {position_id} not found")
        sym = str(row.get("symbol") or "")
        source = _norm_state(row.get("state"))
        _check_transition(sym, source, OPEN, position_id=int(position_id))
        self.store.set_position_state(int(position_id), OPEN, ts=ts)
        return self.transition(sym, OPEN, from_state=source, position_id=int(position_id),
                              reason=reason, ts=ts)

    # ---------------------------------------------------------------- 便捷入口

    def apply_exit(self, decision: Decision, *, exit_price_int: Optional[int] = None,
                   position: Any = None, ts: Optional[float] = None,
                   fee: float = 0.0, realized_pnl: Optional[float] = None,
                   fill_id: Optional[str] = None) -> Tuple[Transition, ...]:
        """把离场意图按 `OPEN → CLOSING → CLOSED` 走完，返回两步转换。

        实盘里 T7 的离场单可能几秒才成交，那两步应当分开调（`begin_close` 发单时、
        `complete_close` 收到成交回报时）；本方法给 dry-run / 单测用（假设成交在
        同一 tick 内完成）。**止损离场（`action='stop'`）用仓位里的止损价整数表示**，
        信号离场用 `exit_price` 按 `price_decimals` floor。
        """
        if decision.action not in (ACTION_EXIT, ACTION_STOP):
            raise LifecycleError(f"apply_exit needs an exit intent, got {decision.action!r}")
        if decision.position_id is None:
            raise LifecycleError("apply_exit needs decision.position_id")
        pid = int(decision.position_id)
        row = self.store.position(pid)
        if row is None:
            raise LifecycleError(f"position {pid} not found")
        pv = PositionView.coerce(position) if position is not None else PositionView.from_row(row)
        kind = "stop" if decision.action == ACTION_STOP else "exit"

        if exit_price_int is not None:
            price_int = int(exit_price_int)
        elif kind == "stop" and pv.stop_price_int is not None:
            price_int = int(pv.stop_price_int)
        else:
            decimals = int(decision.price_decimals if decision.price_decimals is not None
                           else pv.price_decimals)
            price_int = to_int_price(float(decision.exit_price), decimals)

        first = self.begin_close(pid, reason=kind, bar_ms=decision.bar_ms, ts=ts)
        second = self.complete_close(pid, exit_price_int=price_int, kind=kind,
                                     reason=kind, fill_id=fill_id, fee=fee,
                                     realized_pnl=realized_pnl, bar_ms=decision.bar_ms,
                                     ts=ts)
        return (first, second)

    def apply(self, decision: Decision, **kwargs: Any) -> Tuple[Transition, ...]:
        """按 `action` 派发到 `apply_entry` / `apply_exit`（`hold`/`skip` 返回空元组）。"""
        if decision.action == ACTION_ENTRY:
            return (self.apply_entry(decision, **kwargs),)
        if decision.action in (ACTION_EXIT, ACTION_STOP):
            return self.apply_exit(decision, **kwargs)
        return ()


# ------------------------------------------------------------------ 内部工具


def _norm_state(value: Any) -> str:
    return str(value or "").strip().upper()


def _check_transition(symbol: str, source: str, target: str, *,
                      position_id: Optional[int] = None) -> None:
    """状态机校验：非法转换**抛错**，绝不静默放过。"""
    allowed = LEGAL_TRANSITIONS.get(source)
    if allowed is None:
        raise IllegalTransition(f"{symbol}: unknown position state {source!r}")
    if target not in allowed:
        raise IllegalTransition(
            f"{symbol}: illegal position transition {source} -> {target} "
            f"(allowed from {source}: {allowed or 'none — terminal'})"
            + ("" if position_id is None else f" [position {position_id}]"))


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out or out in (float("inf"), float("-inf")) else out
