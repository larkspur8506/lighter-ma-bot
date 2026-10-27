"""风控闸门：每次**开新仓**之前的一道 fail-closed 检查（纯函数，不碰网络/DB/交易所）。

设计原则
--------
* **fail-closed**：配置缺失、状态读不到、口径算不出 → **拒绝**开仓。放任一个
  「状态未知」的账户下单，等于把风控交给运气。
* **每个闸门都能单独解释为什么拒**：返回 `GateResult(code, message, detail)`，
  面板（T10）和告警（notify）直接吃结构，不用解析文案。
* **只管开仓，不管离场**：kill / 熔断 / DD Guard 全部只挡「开新仓」。已有仓位的止损单
  在交易所侧照常有效，平仓/撤单永远放行 —— 这是旧系统验证过的单向出口。
* **纯函数**：不读 env（`RiskLimits.from_env()` 是显式调用）、不碰 DB、不碰网络。
  状态由调用方（T8 executor）从 DB/交易所组装成 `RiskState` 传进来。

闸门清单与默认值（来源见每条的注释）
------------------------------------
==========================  =====================  ==================================
闸门                        默认                   触发即
==========================  =====================  ==================================
`kill_switch`               off                    拒绝开新仓（人工/自动 kill）
`state_unavailable`         —                      账户状态读不到 → 拒绝
`daily_loss`                -5% 权益（当日）       拒绝开新仓
`daily_trade_limit`         4 笔/日                拒绝开新仓
`max_positions`             3（每标的至多 1 仓）   拒绝开新仓
`duplicate_symbol`          —                      同标的重仓 → 拒绝
`symbol_notional`           权益 × 1.5             拒绝开新仓
`total_notional`            权益 × 2.5             拒绝开新仓
`drawdown`                  ？→ 按风险档位          ≥ 熔断线 → 拒绝；≥ 告警线 → 只 warn
`bad_notional`              —                      名义 ≤0/非有限 → 拒绝
==========================  =====================  ==================================

持仓上限为什么是 3 且**每标的至多一仓**：`docs/PLAN.md` T6 明确「状态 `IDLE → OPEN →
CLOSED`，**每标的至多一仓**」，面板状态条也写「持仓 N/3」。所以上限是「三个标的各 1 仓」，
同一标的出现第二个仓位一律拒绝（`duplicate_symbol`），不是「允许同标的多仓、总数 ≤ 3」。

回撤（DD Guard）阈值——**照抄，不现编**
------------------------------------
来源：`docs/PLAN.md` T9（"照抄 `risk-sizing-tier.md`，不现编"）与旧仓库
`app/hl/dd.py::TIERS`，两边数值一致：

    (risk_pct 上限, 告警线, 熔断线)
    (0.015, 0.25, 0.30)      # ≤1.5% 档（含 1.33% 实盘档）：告警 25% / 熔断 30%
    (0.025, 0.30, 0.40)      # ≤2.5% 档：告警 30% / 熔断 40%
    (inf,   0.40, 0.50)      # 更大档：告警 40% / 熔断 50%

**风险档越高，阈值越宽**（不是越严）：`risk_pct` 越大，相同策略的历史最大回撤就越深，
用低档的 30% 去挡 3% 档等于给一条必然出现 40%+ 回撤的曲线装 30% 的熔断，长期误触发
（旧系统 `TIERS` 的注释就是这么写的）。所以这里严格按 PLAN 的三档照抄。

T9（`app/lighter/dd.py`）实现 TWR 指数与持久化时**必须 import 这里的
`dd_thresholds()` / `dd_action()`**，不要另写一份 —— 否则面板显示的阈值和熔断用的阈值
会各说各话（旧系统的历史教训）。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.sizing import DEFAULT_RISK_PCT

__all__ = [
    "DEFAULT_MAX_DAILY_LOSS_PCT", "DEFAULT_MAX_POSITIONS", "DEFAULT_MAX_SYMBOL_NOTIONAL_MULT",
    "DEFAULT_MAX_TOTAL_NOTIONAL_MULT", "DEFAULT_MAX_TRADES_PER_DAY", "DD_MAX_KILL",
    "DD_MIN_PCT", "GATE_BAD_NOTIONAL", "GATE_DAILY_LOSS", "GATE_DAILY_TRADES",
    "GATE_DD", "GATE_DUPLICATE_SYMBOL", "GATE_KILL", "GATE_MAX_POSITIONS",
    "GATE_STATE", "GATE_SYMBOL_NOTIONAL", "GATE_TOTAL_NOTIONAL", "RISK_TIERS",
    "GateResult", "RiskConfigError", "RiskDecision", "RiskLimits", "RiskState",
    "check_entry", "dd_action", "dd_thresholds", "drawdown",
]

# ------------------------------------------------------------------ 默认值
# 来源：旧仓库 `docs/HL_EXECUTOR_PLAN.md` §3.7「风控闸门（默认值，全部可配）」。
# 那一版是实盘跑过参数，这里原样沿用（换市场不换风控）。
DEFAULT_MAX_DAILY_LOSS_PCT = 0.05       # -5% 权益 → 当日熔断
DEFAULT_MAX_TRADES_PER_DAY = 4          # 防重试风暴
DEFAULT_MAX_POSITIONS = 3               # BTC/ETH/SOL 各 1 仓
DEFAULT_MAX_SYMBOL_NOTIONAL_MULT = 1.5  # 每标的最大名义 = 权益 × 1.5
DEFAULT_MAX_TOTAL_NOTIONAL_MULT = 2.5   # 总名义上限 = 权益 × 2.5

# 回撤阈值规则（与旧 `dd.py` 的 MIN_PCT / MAX_KILL 对齐）：
DD_MIN_PCT = 0.05                       # 覆盖值下界（低于 5% 是噪音线）
DD_MAX_KILL = 0.95                      # 熔断线上限（留 5%，避免「必触发」）

RISK_TIERS: Tuple[Tuple[float, float, float], ...] = (
    (0.015, 0.25, 0.30),
    (0.025, 0.30, 0.40),
    (float("inf"), 0.40, 0.50),
)

# ------------------------------------------------------------------ 闸门名 / 拒绝码
GATE_KILL = "kill_switch"
GATE_STATE = "state_unavailable"
GATE_DAILY_LOSS = "daily_loss"
GATE_DAILY_TRADES = "daily_trade_limit"
GATE_MAX_POSITIONS = "max_positions"
GATE_DUPLICATE_SYMBOL = "duplicate_symbol"
GATE_SYMBOL_NOTIONAL = "symbol_notional"
GATE_TOTAL_NOTIONAL = "total_notional"
GATE_DD = "drawdown"
GATE_BAD_NOTIONAL = "bad_notional"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_BLOCK = "block"

# 环境变量键（`RiskLimits.from_env()` 读这些；与 `.env.example` 的 Risk 段对应）
ENV_RISK_PCT = "RISK_PCT"
ENV_MAX_DAILY_LOSS_PCT = "MAX_DAILY_LOSS_PCT"
ENV_MAX_TRADES_PER_DAY = "MAX_TRADES_PER_DAY"
ENV_MAX_POSITIONS = "MAX_POSITIONS"
ENV_MAX_SYMBOL_NOTIONAL_MULT = "MAX_SYMBOL_NOTIONAL_MULT"
ENV_MAX_TOTAL_NOTIONAL_MULT = "MAX_TOTAL_NOTIONAL_MULT"
ENV_DD_WARN_PCT = "DD_WARN_PCT"
ENV_DD_KILL_PCT = "DD_KILL_PCT"
ENV_KILL = "KILL_SWITCH"

_TRUTHY = frozenset({"1", "true", "t", "yes", "y", "on"})


class RiskConfigError(ValueError):
    """风控配置非法。**启动时就报**，不静默回默认值（回默认值等于悄悄放宽风控）。"""


# ------------------------------------------------------------------ 阈值函数


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    """安全转 float：None/垃圾/NaN/±inf → `default`。

    NaN 会让所有比较都返回 False —— 也就是「闸门静默放行」。必须挡在入口。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def dd_thresholds(risk_pct: Any = DEFAULT_RISK_PCT,
                  warn_override: Any = 0.0, kill_override: Any = 0.0) -> Dict[str, Any]:
    """按 `risk_pct` 选档 → `{"warn", "kill", "tier_warn", "tier_kill", "auto", "rejected"}`。

    * `risk_pct` 读不出（None/脏值/NaN）→ 用**最严档**（25/30）：宁可早一步喊人，
      也别因为一个读坏的配置把熔断线放到 40%。
    * `warn_override` / `kill_override` 为 0/空 = 用档位值。
    * 覆盖值非法（越界或 `warn >= kill`）→ **两者一起**退回档位默认，并置 `rejected=True`
      （只退回一个会造出「告警 40% / 熔断 30%」这种自相矛盾的组合）。
    """
    risk = _num(risk_pct)
    if risk is None:
        risk = RISK_TIERS[0][0]
    tier_warn, tier_kill = RISK_TIERS[-1][1], RISK_TIERS[-1][2]
    for cap, warn, kill in RISK_TIERS:
        if risk <= cap:
            tier_warn, tier_kill = warn, kill
            break

    raw_warn = _num(warn_override, 0.0) or 0.0
    raw_kill = _num(kill_override, 0.0) or 0.0
    warn = _norm_override(raw_warn)
    kill = _norm_override(raw_kill)
    out_of_range = (raw_warn > 0 and warn is None) or (raw_kill > 0 and kill is None)
    warn = tier_warn if warn is None else warn
    kill = tier_kill if kill is None else kill
    rejected = bool(out_of_range) or not (warn < kill)
    if rejected:
        warn, kill = tier_warn, tier_kill
    return {"warn": float(warn), "kill": float(kill),
            "tier_warn": float(tier_warn), "tier_kill": float(tier_kill),
            "auto": warn_override in (0, 0.0, None) and kill_override in (0, 0.0, None),
            "rejected": bool(rejected)}


def _norm_override(value: float) -> Optional[float]:
    """覆盖值归一：0 → None（= 用档位值）；越界 → None（= 忽略这个值）。"""
    if value <= 0.0:
        return None
    return value if DD_MIN_PCT <= value <= DD_MAX_KILL else None


def dd_action(drawdown_pct: Any, risk_pct: Any = DEFAULT_RISK_PCT,
              warn_override: Any = 0.0, kill_override: Any = 0.0) -> str:
    """回撤值 → `"ok"` / `"warn"` / `"kill"`（`kill` = 拒绝开新仓）。

    读不出回撤（None/NaN）→ `"kill"`：口径不可用时按最坏情况处理（fail-closed）。
    阈值来自 `dd_thresholds()`（**唯一实现**）：档位默认 + 可选手工覆盖，
    所以面板显示的阈值和闸门用的阈值永远是同一个数。
    """
    dd = _num(drawdown_pct)
    if dd is None:
        return "kill"
    thresholds = dd_thresholds(risk_pct, warn_override, kill_override)
    warn, kill = thresholds["warn"], thresholds["kill"]
    if dd >= kill:
        return "kill"
    if dd >= warn:
        return "warn"
    return "ok"


def drawdown(index: Any, peak_index: Any) -> Optional[float]:
    """DD = (peak − index) / peak（正数 = 回撤幅度）；算不出 → `None`（**不猜**）。"""
    i, p = _num(index), _num(peak_index)
    if i is None or p is None or p <= 0 or i < 0:
        return None
    return max(0.0, (p - i) / p)


# ------------------------------------------------------------------ 配置


@dataclass(frozen=True)
class RiskLimits:
    """风控阈值。全部有文档化默认值；填错 → `RiskConfigError`（fail-loud）。"""

    risk_pct: float = DEFAULT_RISK_PCT
    max_daily_loss_pct: float = DEFAULT_MAX_DAILY_LOSS_PCT
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY
    max_positions: int = DEFAULT_MAX_POSITIONS
    max_symbol_notional_mult: float = DEFAULT_MAX_SYMBOL_NOTIONAL_MULT
    max_total_notional_mult: float = DEFAULT_MAX_TOTAL_NOTIONAL_MULT
    dd_warn_pct: float = 0.0              # 0 = 按 risk_pct 档位自动
    dd_kill_pct: float = 0.0              # 0 = 按 risk_pct 档位自动
    kill: bool = False                    # 人工/自动 kill：只挡开新仓

    def __post_init__(self) -> None:
        risk = _num(self.risk_pct)
        if risk is None or not (0.0 < risk <= 1.0):
            raise RiskConfigError(f"risk_pct must be in (0, 1]: {self.risk_pct!r}")
        loss = _num(self.max_daily_loss_pct)
        if loss is None or not (0.0 < loss < 1.0):
            raise RiskConfigError(f"max_daily_loss_pct must be in (0, 1): {self.max_daily_loss_pct!r}")
        trades = _num(self.max_trades_per_day)
        if trades is None or trades < 0 or trades != int(trades):
            raise RiskConfigError(f"max_trades_per_day must be a non-negative integer: "
                                  f"{self.max_trades_per_day!r}")
        positions = _num(self.max_positions)
        if positions is None or positions < 0 or positions != int(positions):
            raise RiskConfigError(f"max_positions must be a non-negative integer: "
                                  f"{self.max_positions!r}")
        for name, value in (("max_symbol_notional_mult", self.max_symbol_notional_mult),
                            ("max_total_notional_mult", self.max_total_notional_mult)):
            mult = _num(value)
            if mult is None or mult <= 0:
                raise RiskConfigError(f"{name} must be > 0: {value!r}")
        if float(self.max_symbol_notional_mult) > float(self.max_total_notional_mult):
            raise RiskConfigError(
                "max_symbol_notional_mult must be <= max_total_notional_mult "
                f"({self.max_symbol_notional_mult} > {self.max_total_notional_mult})"
            )
        # 覆盖值的合法性交给 dd_thresholds()（唯一实现）；这里只要求「填了就是合法数」
        thresholds = dd_thresholds(self.risk_pct, self.dd_warn_pct, self.dd_kill_pct)
        if thresholds["rejected"]:
            raise RiskConfigError(
                f"invalid DD threshold override (warn={self.dd_warn_pct!r}, "
                f"kill={self.dd_kill_pct!r}): must satisfy {DD_MIN_PCT} <= warn < kill "
                f"<= {DD_MAX_KILL}, or 0 for the risk-tier default"
            )

    # ---------------------------------------------------------------- 派生

    @property
    def thresholds(self) -> Dict[str, Any]:
        """生效的 DD 阈值（档位 + 覆盖）。"""
        return dd_thresholds(self.risk_pct, self.dd_warn_pct, self.dd_kill_pct)

    @property
    def dd_warn(self) -> float:
        return float(self.thresholds["warn"])

    @property
    def dd_kill(self) -> float:
        return float(self.thresholds["kill"])

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "risk_pct": float(self.risk_pct),
            "max_daily_loss_pct": float(self.max_daily_loss_pct),
            "max_trades_per_day": int(self.max_trades_per_day),
            "max_positions": int(self.max_positions),
            "max_symbol_notional_mult": float(self.max_symbol_notional_mult),
            "max_total_notional_mult": float(self.max_total_notional_mult),
            "dd_warn_pct": self.dd_warn,
            "dd_kill_pct": self.dd_kill,
            "dd_thresholds_auto": bool(self.thresholds["auto"]),
            "kill": bool(self.kill),
        }
        return out

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "RiskLimits":
        """从环境变量构造；缺项用默认值，**填了但不合法 → `RiskConfigError`**。"""
        source: Mapping[str, str] = os.environ if env is None else env
        return cls(
            risk_pct=_env_float(source, ENV_RISK_PCT, DEFAULT_RISK_PCT),
            max_daily_loss_pct=_env_float(source, ENV_MAX_DAILY_LOSS_PCT,
                                          DEFAULT_MAX_DAILY_LOSS_PCT),
            max_trades_per_day=_env_int(source, ENV_MAX_TRADES_PER_DAY,
                                        DEFAULT_MAX_TRADES_PER_DAY),
            max_positions=_env_int(source, ENV_MAX_POSITIONS, DEFAULT_MAX_POSITIONS),
            max_symbol_notional_mult=_env_float(source, ENV_MAX_SYMBOL_NOTIONAL_MULT,
                                                DEFAULT_MAX_SYMBOL_NOTIONAL_MULT),
            max_total_notional_mult=_env_float(source, ENV_MAX_TOTAL_NOTIONAL_MULT,
                                               DEFAULT_MAX_TOTAL_NOTIONAL_MULT),
            dd_warn_pct=_env_float(source, ENV_DD_WARN_PCT, 0.0),
            dd_kill_pct=_env_float(source, ENV_DD_KILL_PCT, 0.0),
            kill=_env_bool(source, ENV_KILL, False),
        )

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "RiskLimits":
        """从 `app.config.Config` 取 `risk_pct`，其余可显式覆盖（DB 参数走这里）。"""
        risk = getattr(config, "risk_pct", None)
        if risk is None:
            raise RiskConfigError("config has no risk_pct — refusing to build risk limits")
        data: Dict[str, Any] = {"risk_pct": float(risk)}
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)


# ------------------------------------------------------------------ 状态


@dataclass(frozen=True)
class RiskState:
    """一次开仓检查需要的账户状态快照（由 T8 executor 从 DB + 交易所组装）。

    **所有字段默认 `None`** —— 「没填」与「填了 0」语义不同：前者是状态未知
    （fail-closed → 拒绝），后者是真实的 0。调用方必须显式给出每一个用到的字段，
    这样「忘了传某个字段」会在第一次跑测试时就暴露成拒单，而不是被 0 静默放行。
    """

    equity: Optional[float] = None
    day_start_equity: Optional[float] = None
    trades_today: Optional[int] = None
    open_positions: Tuple[str, ...] = ()
    open_notional_total: Optional[float] = None
    open_notional_by_symbol: Mapping[str, float] = field(default_factory=dict)
    dd_index: Optional[float] = None
    dd_peak_index: Optional[float] = None

    # ---------------------------------------------------------------- 便捷

    def symbols(self) -> Tuple[str, ...]:
        return tuple(str(s).strip().upper() for s in self.open_positions)

    def notional_for(self, symbol: str) -> float:
        wanted = str(symbol).strip().upper()
        for key, value in (self.open_notional_by_symbol or {}).items():
            if str(key).strip().upper() == wanted:
                return _num(value, 0.0) or 0.0
        return 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "equity": self.equity,
            "day_start_equity": self.day_start_equity,
            "trades_today": self.trades_today,
            "open_positions": list(self.symbols()),
            "open_notional_total": self.open_notional_total,
            "open_notional_by_symbol": {str(k).upper(): v
                                        for k, v in (self.open_notional_by_symbol or {}).items()},
            "dd_index": self.dd_index,
            "dd_peak_index": self.dd_peak_index,
        }


# ------------------------------------------------------------------ 结果


@dataclass(frozen=True)
class GateResult:
    """单个闸门的结论。`severity` 是 `ok` / `warn` / `block` 三态，`code` 是稳定机读码。"""

    gate: str
    severity: str = SEVERITY_OK
    code: str = ""
    message: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.severity != SEVERITY_BLOCK

    @property
    def blocked(self) -> bool:
        return self.severity == SEVERITY_BLOCK

    def as_dict(self) -> Dict[str, Any]:
        return {"gate": self.gate, "severity": self.severity,
                "code": self.code or self.gate, "message": self.message,
                "detail": dict(self.detail)}

    def __str__(self) -> str:
        return f"[{self.severity}] {self.gate}: {self.code} {self.message}".strip()


@dataclass(frozen=True)
class RiskDecision:
    """一次开仓闸门的完整结论：`allowed` + 每个闸门的明细。"""

    allowed: bool
    gates: Tuple[GateResult, ...]
    limits: RiskLimits

    @property
    def blocked(self) -> Tuple[GateResult, ...]:
        return tuple(g for g in self.gates if g.blocked)

    @property
    def warnings(self) -> Tuple[GateResult, ...]:
        return tuple(g for g in self.gates if g.severity == SEVERITY_WARN)

    @property
    def codes(self) -> Tuple[str, ...]:
        """全部拒绝码（空元组 = 放行）。"""
        return tuple(g.code or g.gate for g in self.blocked)

    @property
    def reason(self) -> str:
        """一句人能读的拒绝理由（放行时为空串）。T10 面板/告警直接用。"""
        return "; ".join(f"{g.gate}: {g.message or g.code}" for g in self.blocked)

    def gate(self, name: str) -> Optional[GateResult]:
        for g in self.gates:
            if g.gate == name:
                return g
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "blocked": list(self.codes),
            "reason": self.reason,
            "gates": [g.as_dict() for g in self.gates],
            "limits": self.limits.as_dict(),
        }

    def __str__(self) -> str:
        return "allowed" if self.allowed else f"blocked: {self.reason}"


# ------------------------------------------------------------------ 主入口


def check_entry(symbol: str, notional: float, state: RiskState,
                limits: Optional[RiskLimits] = None) -> RiskDecision:
    """开新仓前的完整闸门检查。**任何一项不满足都不放行**（fail-closed）。

    参数
    ----
    symbol
        要开的标的（大小写不敏感）。
    notional
        `app.sizing.plan_order()` 算出的名义（USDC）。≤0 / 非有限 → 拒。
    state
        账户状态快照（见 `RiskState`）。字段缺失 = 状态未知 = 拒绝。
    limits
        风控阈值；`None` 用文档化默认值。

    返回
    ----
    `RiskDecision`：`allowed` 为真才允许下单；否则 `codes` / `reason` 说明原因，
    `gates` 里每个闸门都有独立的 `code` + `message` + `detail`。
    """
    lim = limits if limits is not None else RiskLimits()
    wanted = str(symbol or "").strip().upper()
    gates: List[GateResult] = [
        _gate_kill(lim),
        _gate_state(state),
        _gate_daily_loss(state, lim),
        _gate_daily_trades(state, lim),
        _gate_positions(wanted, state, lim),
        _gate_notional(wanted, notional, state, lim),
        _gate_drawdown(state, lim),
    ]
    return RiskDecision(allowed=not any(g.blocked for g in gates), gates=tuple(gates),
                        limits=lim)


# ------------------------------------------------------------------ 各闸门


def _gate_kill(limits: RiskLimits) -> GateResult:
    if limits.kill:
        return GateResult(GATE_KILL, SEVERITY_BLOCK, GATE_KILL,
                          "kill switch is on — new entries are disabled "
                          "(exits and stop orders are unaffected)",
                          {"kill": True})
    return GateResult(GATE_KILL, SEVERITY_OK, detail={"kill": False})


def _gate_state(state: RiskState) -> GateResult:
    equity = _num(getattr(state, "equity", None))
    if equity is None or equity <= 0:
        return GateResult(GATE_STATE, SEVERITY_BLOCK, GATE_STATE,
                          f"account equity is unavailable or non-positive ({state.equity!r}) — "
                          f"refusing to size anything without a known equity",
                          {"equity": state.equity})
    return GateResult(GATE_STATE, SEVERITY_OK, detail={"equity": equity})


def _gate_daily_loss(state: RiskState, limits: RiskLimits) -> GateResult:
    equity = _num(getattr(state, "equity", None))
    day_start = _num(getattr(state, "day_start_equity", None))
    if day_start is None or day_start <= 0 or equity is None:
        return GateResult(GATE_DAILY_LOSS, SEVERITY_BLOCK, GATE_DAILY_LOSS,
                          "day-start equity is unavailable — cannot evaluate the daily "
                          "loss limit (fail-closed)",
                          {"state_unavailable": True, "day_start_equity": state.day_start_equity,
                           "equity": state.equity})
    loss = (day_start - equity) / day_start
    detail = {"day_start_equity": day_start, "equity": equity,
              "loss_pct": loss, "max_daily_loss_pct": float(limits.max_daily_loss_pct)}
    if loss >= float(limits.max_daily_loss_pct):
        return GateResult(GATE_DAILY_LOSS, SEVERITY_BLOCK, GATE_DAILY_LOSS,
                          f"daily loss {loss:.2%} reached the limit "
                          f"{float(limits.max_daily_loss_pct):.2%}", detail)
    return GateResult(GATE_DAILY_LOSS, SEVERITY_OK, detail=detail)


def _gate_daily_trades(state: RiskState, limits: RiskLimits) -> GateResult:
    trades = _num(getattr(state, "trades_today", None))
    if trades is None or trades < 0 or trades != int(trades):
        return GateResult(GATE_DAILY_TRADES, SEVERITY_BLOCK, GATE_DAILY_TRADES,
                          f"trades_today is unavailable ({state.trades_today!r}) — "
                          f"cannot evaluate the daily order limit (fail-closed)",
                          {"state_unavailable": True, "trades_today": state.trades_today})
    detail = {"trades_today": int(trades), "max_trades_per_day": int(limits.max_trades_per_day)}
    if int(trades) >= int(limits.max_trades_per_day):
        return GateResult(GATE_DAILY_TRADES, SEVERITY_BLOCK, GATE_DAILY_TRADES,
                          f"{int(trades)} orders already placed today (limit "
                          f"{int(limits.max_trades_per_day)}) — refusing a retry storm", detail)
    return GateResult(GATE_DAILY_TRADES, SEVERITY_OK, detail=detail)


def _gate_positions(symbol: str, state: RiskState, limits: RiskLimits) -> GateResult:
    try:
        open_symbols = state.symbols()
    except Exception:                                        # noqa: BLE001 - 坏状态 = 拒绝
        return GateResult(GATE_MAX_POSITIONS, SEVERITY_BLOCK, GATE_MAX_POSITIONS,
                          "open_positions is unreadable (fail-closed)",
                          {"state_unavailable": True})
    detail = {"open_positions": list(open_symbols), "max_positions": int(limits.max_positions),
              "symbol": symbol}
    if symbol and symbol in open_symbols:
        return GateResult(GATE_DUPLICATE_SYMBOL, SEVERITY_BLOCK, GATE_DUPLICATE_SYMBOL,
                          f"{symbol} already has an open position — one position per "
                          f"symbol (PLAN T6 IDLE→OPEN→CLOSED)", detail)
    if len(open_symbols) >= int(limits.max_positions):
        return GateResult(GATE_MAX_POSITIONS, SEVERITY_BLOCK, GATE_MAX_POSITIONS,
                          f"{len(open_symbols)} open positions reached the limit "
                          f"{int(limits.max_positions)}", detail)
    return GateResult(GATE_MAX_POSITIONS, SEVERITY_OK, detail=detail)


def _gate_notional(symbol: str, notional: Any, state: RiskState,
                   limits: RiskLimits) -> GateResult:
    value = _num(notional)
    if value is None or value <= 0:
        return GateResult(GATE_BAD_NOTIONAL, SEVERITY_BLOCK, GATE_BAD_NOTIONAL,
                          f"notional is unavailable or non-positive ({notional!r})", {})
    equity = _num(getattr(state, "equity", None))
    if equity is None or equity <= 0:
        return GateResult(GATE_SYMBOL_NOTIONAL, SEVERITY_BLOCK, GATE_SYMBOL_NOTIONAL,
                          "equity is unavailable — cannot evaluate notional caps (fail-closed)",
                          {"state_unavailable": True, "equity": state.equity})

    existing_symbol = state.notional_for(symbol)
    symbol_cap = equity * float(limits.max_symbol_notional_mult)
    symbol_after = existing_symbol + value
    detail = {"notional": value, "existing_symbol_notional": existing_symbol,
              "symbol_after": symbol_after, "symbol_cap": symbol_cap, "equity": equity}
    if symbol_after > symbol_cap + 1e-9:
        return GateResult(GATE_SYMBOL_NOTIONAL, SEVERITY_BLOCK, GATE_SYMBOL_NOTIONAL,
                          f"{symbol}: notional after entry {symbol_after:.2f} exceeds "
                          f"{float(limits.max_symbol_notional_mult):g}x equity "
                          f"({symbol_cap:.2f})", detail)

    total = _num(getattr(state, "open_notional_total", None))
    if total is None:
        # 总名义没给：用「各标的名义之和」当兜底；再没有（by_symbol 也空）就 fail-closed。
        by_symbol = getattr(state, "open_notional_by_symbol", None) or {}
        if not by_symbol and not state.symbols():
            total = 0.0
        elif by_symbol:
            total = sum(_num(v, 0.0) or 0.0 for v in by_symbol.values())
        else:
            return GateResult(GATE_TOTAL_NOTIONAL, SEVERITY_BLOCK, GATE_TOTAL_NOTIONAL,
                              "open_notional_total is unavailable while positions are open — "
                              "cannot evaluate the total notional cap (fail-closed)",
                              {"state_unavailable": True,
                               "open_positions": list(state.symbols())})
    total_cap = equity * float(limits.max_total_notional_mult)
    total_after = float(total) + value
    detail.update({"open_notional_total": float(total), "total_after": total_after,
                   "total_cap": total_cap})
    if total_after > total_cap + 1e-9:
        return GateResult(GATE_TOTAL_NOTIONAL, SEVERITY_BLOCK, GATE_TOTAL_NOTIONAL,
                          f"total notional after entry {total_after:.2f} exceeds "
                          f"{float(limits.max_total_notional_mult):g}x equity "
                          f"({total_cap:.2f})", detail)
    return GateResult(GATE_SYMBOL_NOTIONAL, SEVERITY_OK, detail=detail)


def _gate_drawdown(state: RiskState, limits: RiskLimits) -> GateResult:
    dd = drawdown(getattr(state, "dd_index", None), getattr(state, "dd_peak_index", None))
    thresholds = limits.thresholds
    if dd is None:
        return GateResult(GATE_DD, SEVERITY_BLOCK, GATE_DD,
                          "drawdown state is unavailable (dd_index/dd_peak_index missing) — "
                          "fail-closed: no new entries until TWR state is bootstrapped",
                          {"dd_index": state.dd_index, "dd_peak_index": state.dd_peak_index,
                           "warn": thresholds["warn"], "kill": thresholds["kill"]})
    detail = {"drawdown": dd, "warn": thresholds["warn"], "kill": thresholds["kill"],
              "index": state.dd_index, "peak_index": state.dd_peak_index,
              "risk_pct": float(limits.risk_pct)}
    action = dd_action(dd, limits.risk_pct, limits.dd_warn_pct, limits.dd_kill_pct)
    if action == "kill":
        return GateResult(GATE_DD, SEVERITY_BLOCK, GATE_DD,
                          f"drawdown {dd:.2%} >= kill threshold {thresholds['kill']:.2%} "
                          f"(risk tier for risk_pct={float(limits.risk_pct):.4%})", detail)
    if action == "warn":
        return GateResult(GATE_DD, SEVERITY_WARN, GATE_DD,
                          f"drawdown {dd:.2%} >= warn threshold {thresholds['warn']:.2%}",
                          detail)
    return GateResult(GATE_DD, SEVERITY_OK, detail=detail)


# ------------------------------------------------------------------ env 解析


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        raise RiskConfigError(f"{key} must be a number, got {raw!r}") from None


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw, 10)
    except ValueError:
        raise RiskConfigError(f"{key} must be a base-10 integer, got {raw!r}") from None


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in _TRUTHY:
        return True
    if raw in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise RiskConfigError(f"{key} must be a boolean (1/0/true/false/on/off), got {raw!r}")
