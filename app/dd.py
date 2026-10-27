"""回撤口径（TWR，剔出入金）+ 熔断决策 + 持久化（T9）。

这个模块回答三个问题，且**只回答**这三个：

1. **现在回撤多少？** —— 用 TWR 收益指数算，**剔出入金**。充钱不是收益，提现不是亏损。
2. **这个回撤意味着什么？** —— 交给 `app.risk.dd_thresholds()` / `dd_action()` 判档位
   （本模块**不另写一套阈值口径**，见下节）。
3. **这个结论放在哪、谁能改？** —— `dd_state`（单行、持久）+ `dd_anchors`（30 天滚动、
   供面板画历史）。降峰值只有一个入口：`:meth:`DrawdownGuard.reset_peak``（显式人工）。

为什么必须是 TWR，而不是「(峰值权益 − 当前权益) / 峰值权益」
------------------------------------------------------------
旧系统实测（`larkspur8506/crypto-ma-alert` 的 `app/hl/dd.py`）：2026-10-21 账户转入
901.80U，账户权益曲线的「回撤」当天瞬间归零 —— 一笔入金把 30% 的真实回撤抹掉了，
熔断因此永远触发不了。反过来提现会被算成亏损，可能凭空触发熔断。两个方向的错都不可接受，
所以判据用资金加权收益指数：

    r_k     = (equity_k − flow_k) / equity_{k−1} − 1       # flow_k 落在 (t_{k−1}, t_k]
    index_k = index_{k−1} · (1 + r_k)                       # 从锚点起算，锚点处 = 1.0
    peak    = max(peak, index)                              # **只升不降**
    DD      = (peak − index) / peak

`flow_k` 用**带符号**净流水（正 = 入金，负 = 出金），区间左端**严格开区间**：落在上一步那一
时刻的流水已经体现在那一步的权益里，再加一遍等于重复计入。

阈值口径：**只调用 `app.risk`，一个字都不另写**
-----------------------------------------------
面板显示的阈值、熔断用的阈值、告警用的阈值必须是同一个函数算出来的。旧系统的教训是
「三个回撤口径混用」：同一个账户，面板说 24%、告警按 25%、熔断按 30%，谁也说不清
哪个是对的。所以：

* 档位表只有 `app.risk.RISK_TIERS` 一份；
* 选档只有 `app.risk.dd_thresholds(risk_pct, warn_override, kill_override)` 一份；
* 判定只有 `app.risk.dd_action(drawdown, risk_pct, ...)` 一份。

本模块只做「喂给它一个数」和「把结论落库」两件事。

脏值一律取**最严档**
--------------------
`equity` / `index` / `peak` 读不出来、是 `None`、是 `NaN`、是 `±inf` —— 全部按最坏情况处理：
`dd_action(None)` 直接返回 `"kill"`，`dd_thresholds(None)` 直接落入最严档（25%/30%）。
理由：`NaN` 参与的比较**全是 False**，任何「`dd >= kill`」的写法都会静默放行 ——
那等于把熔断关掉，而且关得毫无痕迹。

熔断语义：**只挡开新仓，不挡离场**
----------------------------------
`kill` 只影响 `app.risk.check_entry()` 的 `drawdown` 闸。平仓/止损/撤单**永不放行检查**：

* `app.lifecycle.decide_exit()` 的签名里连 `risk_state` 都没有 —— 结构性保证，不是约定；
* 交易所在交易所侧的止损单本来就不经过本进程。

DD 熔断时把亏损仓锁在场上，是明确要避免的事故（旧系统 README 的「单向出口」）。本模块
提供 :func:`entry_blocked` / :meth:`DdVerdict.blocked` 给开仓路径用，
**不提供任何**「熔断时禁止离场」的开关。

熔断**永不自解**
----------------
峰值只能被 `reset_peak()` 降低，而 `reset_peak()` 必须由人显式调用并带理由（落 `events`）。
没有任何自动路径会：重建一个更低的峰值、把 `dd_state` 删掉后从权益曲线重新猜一个峰值、
或者「超时自动恢复」。回撤变小只有一条合法途径 —— 账户真赚钱了，指数涨回去。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from app.risk import (
    RISK_TIERS,
    RiskLimits,
    RiskState,
    dd_action,
    dd_thresholds,
    drawdown as risk_drawdown,
)
from app.state import (
    DD_SOURCE_STATE,
    DD_SOURCE_UNAVAILABLE,
    DDReading,
    StateAssembler,
)
from app.store import BASE_TWR_INDEX, DD_ANCHOR_KEEP_DAYS, StateStore, StoreError

__all__ = [
    "DD_ACTION_KILL", "DD_ACTION_OK", "DD_ACTION_WARN", "DEFAULT_ANCHOR_MIN_INTERVAL_S",
    "DEFAULT_MAX_EQUITY_POINTS", "EVENT_BOOTSTRAP", "EVENT_KILL", "EVENT_OK", "EVENT_RESET",
    "EVENT_RESET_AUDIT", "EVENT_SKIPPED", "EVENT_WARN", "TWR_BASE", "TWR_DISPLAY_BASE",
    "DdError", "DdVerdict",
    "DrawdownGuard", "TwrState", "advance", "bootstrap", "cum_flow", "entry_blocked",
    "evaluate", "net_flow", "normalise_points",
]

logger = logging.getLogger(__name__)

#: 指数基准。与 `app/store.BASE_TWR_INDEX` **同一个数**：库里的列按 1.0 为基准存
#: （`docs/STATE_STORE.md` §`equity_curve`），面板把它 ×100 显示成「收益指数 100」。
TWR_BASE = BASE_TWR_INDEX
TWR_DISPLAY_BASE = 100.0

DD_ACTION_OK = "ok"
DD_ACTION_WARN = "warn"
DD_ACTION_KILL = "kill"

#: 事件类型（`events.kind`；面板事件流/T10 告警按这几个字符串分类）
EVENT_BOOTSTRAP = "dd_bootstrap"
EVENT_WARN = "dd_warn"
EVENT_KILL = "dd_kill"
EVENT_OK = "dd_ok"
#: `store.reset_dd_peak()` 写的**状态变更**事件（T5 已有契约，T9 不重复写一条同样的）。
EVENT_RESET = "dd_reset"
#: 调用方/操作人**审计**事件（`by` + 重置前的指数）。与上一条是两件事：
#: 一条记「峰值被改了」，一条记「谁调的、从多少改到多少」。
EVENT_RESET_AUDIT = "dd_reset_audit"
EVENT_SKIPPED = "dd_skipped"

#: 一次重放最多读多少个权益点。4h 一根 bar ⇒ ~6 点/天 ⇒ 5 万点 ≈ 22 年。
#: 取全量重放（而不是「从上一个点增量推」）是**有意**的：重放永远从原始权益重算，
#: 不依赖「上一步算对了」，库里被外力改坏一个点也会被下一轮纠回来。
DEFAULT_MAX_EQUITY_POINTS = 50_000

#: 锚点检查点（`dd_anchors`）的最小写入间隔：每 tick 都写会让 30 天窗口里塞进上万行，
#: 而面板只需要「回撤历史」的采样。1h 一条 = 30 天最多 720 行。
DEFAULT_ANCHOR_MIN_INTERVAL_S = 3600.0


class DdError(RuntimeError):
    """回撤层的可预期错误（锚点非法 / 拒绝重置 / 权益读不出来）。"""


# ------------------------------------------------------------------ 数值小工具


def _finite(value: Any) -> Optional[float]:
    """安全转 float：`None` / 垃圾 / `NaN` / `±inf` → `None`。

    `NaN` 必须在这里被挡掉：它参与的任何比较都是 False，会让「回撤 >= 熔断线」
    这类判断静默失效（= 熔断被关掉且没有一点痕迹）。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _field(item: Any, name: str) -> Any:
    """从 dict / sqlite3.Row / 对象读字段（流水既有 dict 也有 SDK 对象）。"""
    if isinstance(item, Mapping):
        return item.get(name)
    try:
        return item[name]
    except (IndexError, KeyError, TypeError):
        return getattr(item, name, None)


# ------------------------------------------------------------------ 流水（剔出入金）


def net_flow(flows: Optional[Iterable[Any]], lo: Optional[float] = None,
             hi: Optional[float] = None) -> float:
    """Σ 落在 **(lo, hi]** 的带符号流水（正 = 净入金，负 = 净出金）。

    左端严格开区间：落在上一步/锚点那一时刻的流水**已经体现在那一格的权益里**，
    再加一遍就是重复计入（入金会被当成收益，等于凭空抹掉一段亏损）。

    每条流水的形状：`{"ts": epoch_s, "amount": 带符号 USDC}`（也接受 `value` / `flow`
    作为金额字段、或带同名属性的对象）。脏时间戳按「不计入」处理 —— 宁可少剔一笔，
    也不要把一笔算不清的流水摊到错误的区间里。
    """
    lo_f = _finite(lo)
    hi_f = _finite(hi)
    total = 0.0
    for item in flows or ():
        ts = _finite(_field(item, "ts"))
        if ts is None:
            continue
        if lo_f is not None and ts <= lo_f:
            continue
        if hi_f is not None and ts > hi_f:
            continue
        amount = _finite(_field(item, "amount"))
        if amount is None:
            amount = _finite(_field(item, "value"))
        if amount is None:
            amount = _finite(_field(item, "flow"))
        total += float(amount or 0.0)
    return total


def cum_flow(flows: Optional[Iterable[Any]], upto: Optional[float] = None) -> float:
    """Σ flows(≤ upto) —— 累计净入金（**只写不读**，留痕/面板文案用）。"""
    return net_flow(flows, None, upto)


# ------------------------------------------------------------------ 权益点


def normalise_points(points: Optional[Iterable[Any]]) -> List[Tuple[float, float]]:
    """权益点 → `[(ts, equity)]`，按 ts 升序；脏点直接丢弃（**不猜**它的值）。

    接受 dict / sqlite3.Row / 对象 / `(ts, equity)` 二元组。丢脏点是有意的：
    拿 `0` 或 `None` 顶上去会造出一个假的收益率。
    """
    out: List[Tuple[float, float]] = []
    for item in points or ():
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            ts, equity = _finite(item[0]), _finite(item[1])
        else:
            ts, equity = _finite(_field(item, "ts")), _finite(_field(item, "equity"))
        if ts is None or ts <= 0 or equity is None:
            continue
        out.append((float(ts), float(equity)))
    out.sort(key=lambda pair: pair[0])
    return out


# ------------------------------------------------------------------ TWR 状态


@dataclass(frozen=True)
class TwrState:
    """一条 TWR 链的计算结果（纯数据，不含任何「该不该熔断」的判断）。"""

    index: float
    peak: float
    anchor_ts: float
    anchor_equity: float
    last_ts: float
    last_equity: float
    steps: int = 0
    skipped: int = 0
    anchor_cum_flow: float = 0.0
    degraded: bool = False
    reason: str = ""

    # ---------------------------------------------------------------- 派生

    @property
    def drawdown(self) -> Optional[float]:
        """`(peak − index) / peak`；算不出 → `None`（**不猜 0**）。"""
        return risk_drawdown(self.index, self.peak)

    @property
    def display_index(self) -> float:
        """面板口径（基准 100）：库里的 1.0 基准 × 100。"""
        return float(self.index) * TWR_DISPLAY_BASE

    @property
    def display_peak(self) -> float:
        return float(self.peak) * TWR_DISPLAY_BASE

    def evaluate(self, risk_pct: Any = None, warn_override: Any = 0.0,
                 kill_override: Any = 0.0, *, source: str = "twr") -> "DdVerdict":
        """按当前 risk_pct 档位判 `ok`/`warn`/`kill`（委托 :func:`evaluate`）。"""
        return evaluate(self.index, self.peak, risk_pct=risk_pct,
                        warn_override=warn_override, kill_override=kill_override,
                        source=source, degraded=self.degraded, reason=self.reason)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "peak": self.peak, "drawdown": self.drawdown,
            "display_index": self.display_index, "display_peak": self.display_peak,
            "anchor_ts": self.anchor_ts, "anchor_equity": self.anchor_equity,
            "anchor_cum_flow": self.anchor_cum_flow,
            "last_ts": self.last_ts, "last_equity": self.last_equity,
            "steps": int(self.steps), "skipped": int(self.skipped),
            "degraded": bool(self.degraded), "reason": self.reason,
        }


def _validate_anchor(anchor_ts: Any, anchor_equity: Any) -> Tuple[float, float]:
    ts, equity = _finite(anchor_ts), _finite(anchor_equity)
    if ts is None or ts <= 0:
        raise DdError(f"anchor_ts must be a positive finite epoch second, got {anchor_ts!r}")
    if equity is None or equity <= 0:
        raise DdError(f"anchor_equity must be a positive finite number, got {anchor_equity!r}")
    return ts, equity


def bootstrap(points: Optional[Iterable[Any]] = None, flows: Optional[Iterable[Any]] = None,
              *, anchor_ts: Any, anchor_equity: Any,
              base: float = TWR_BASE) -> TwrState:
    """从锚点起**逐点重放** TWR（锚点处 index = peak = `base`）。

    为什么不能「锚点 → 当前」一步算：锚点距今可能跨过若干次入金，一步算等于把那些
    入金当成亏损/收益。逐点重放把每一步的流水单独扣掉，入金前后指数自然连续
    （旧系统实测：一步算 88.75 vs 重放 97.68，DD 差 7.8 个百分点 —— 足以让没亏到线的
    账户凭空触发 25% 告警）。

    `points` 里 `ts <= anchor_ts` 的点一律跳过：锚点本身就是起点，把锚点那一刻的权益
    再算一步会凭空造出一个收益率。
    """
    a_ts, a_eq = _validate_anchor(anchor_ts, anchor_equity)
    start = _finite(base)
    if start is None or start <= 0:
        raise DdError(f"base must be a positive finite number, got {base!r}")

    index = float(start)
    peak = float(start)
    skipped = 0
    neg = False
    steps = 0
    prev_ts, prev_eq = a_ts, a_eq
    for ts, equity in normalise_points(points):
        if ts <= a_ts or ts < prev_ts:
            continue
        if prev_eq > 0:
            flow_k = net_flow(flows, prev_ts, ts)
            ratio = (equity - flow_k) / prev_eq
            if ratio < 0.0:
                # 权益 − 入金 < 0：要么数据坏了，要么账户真的亏光。两种都按「归零」处理
                # （index = 0 ⇒ DD = 100% ⇒ 熔断），绝不产出负指数（负指数会让
                # DD 变成 >100% 的怪数，且 store 拒收 twr_index < 0）。
                index = 0.0
                neg = True
            else:
                index = index * ratio
        else:
            skipped += 1                     # 上一格没有本金：不猜，指数不动
        peak = max(peak, index)
        prev_ts, prev_eq = ts, equity
        steps += 1

    reason = ""
    if neg:
        reason = ("a step implied a negative index (equity − flow < 0) — clamped to 0, "
                  "drawdown is 100%")
    elif skipped:
        reason = f"{skipped} step(s) skipped because the previous equity was non-positive"
    return TwrState(index=index, peak=peak, anchor_ts=a_ts, anchor_equity=a_eq,
                    last_ts=prev_ts, last_equity=prev_eq, steps=steps, skipped=skipped,
                    degraded=neg, reason=reason)


def advance(state: TwrState, *, ts: Any, equity: Any, flow: float = 0.0,
            base: float = TWR_BASE) -> Optional[TwrState]:
    """推进一格（纯函数，返回**新**状态）。输入不可用 → `None`（交给调用方决定）。

    三条规则：

    * `ts <= last_ts`（时钟回拨 / 同一秒重复）→ 原样返回：再推一次会把这段收益率算两遍。
    * 上一格权益 ≤ 0 → 收益率无定义，指数不动、`skipped + 1`、时间窗照常前进
      （不前进的话这段永远算不完）。
    * 峰值 `max(旧峰值, 新指数)` —— **只升不降**。
    """
    t, eq = _finite(ts), _finite(equity)
    if t is None or eq is None:
        return None
    if t <= float(state.last_ts):
        return state
    index = float(state.index)
    skipped = int(state.skipped)
    if float(state.last_equity) > 0:
        ratio = (eq - float(_finite(flow) or 0.0)) / float(state.last_equity)
        index = 0.0 if ratio < 0.0 else index * ratio
    else:
        skipped += 1
    return replace(state, index=index, peak=max(float(state.peak), index),
                   last_ts=t, last_equity=eq, steps=int(state.steps) + 1, skipped=skipped)


# ------------------------------------------------------------------ 判定


@dataclass(frozen=True)
class DdVerdict:
    """一次回撤判定（面板、熔断、告警**共用的那一份结论**）。

    * `action` 只可能是 `ok` / `warn` / `kill`，来自 `app.risk.dd_action()`；
    * `warn` / `kill` 是**生效的**阈值（档位 + 可选覆盖），来自 `app.risk.dd_thresholds()`；
    * `available=False` 表示口径不可用 —— 此时 `action` 一定是 `kill`（fail-closed）；
    * `degraded=True` 表示数值可用但证据偏弱（状态陈旧 / 峰值被夹 / 回放丢了一步）。
    """

    action: str = DD_ACTION_KILL
    drawdown: Optional[float] = None
    index: Optional[float] = None
    peak: Optional[float] = None
    warn: float = RISK_TIERS[0][1]
    kill: float = RISK_TIERS[0][2]
    risk_pct: Optional[float] = None
    source: str = DD_SOURCE_UNAVAILABLE
    available: bool = False
    degraded: bool = False
    reason: str = ""
    thresholds_rejected: bool = False

    # ---------------------------------------------------------------- 派生

    @property
    def blocked(self) -> bool:
        """是否拒绝**开新仓**（`kill`）。注意：与「能不能离场」无关。"""
        return self.action == DD_ACTION_KILL

    @property
    def warned(self) -> bool:
        return self.action == DD_ACTION_WARN

    @property
    def strictest(self) -> bool:
        """是否因为口径不可用而落到了最严档（`drawdown is None` ⇒ 直接 kill）。"""
        return self.drawdown is None

    @property
    def display_index(self) -> Optional[float]:
        return None if self.index is None else float(self.index) * TWR_DISPLAY_BASE

    @property
    def display_peak(self) -> Optional[float]:
        return None if self.peak is None else float(self.peak) * TWR_DISPLAY_BASE

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action, "drawdown": self.drawdown,
            "index": self.index, "peak": self.peak,
            "display_index": self.display_index, "display_peak": self.display_peak,
            "warn": self.warn, "kill": self.kill, "risk_pct": self.risk_pct,
            "source": self.source, "available": self.available,
            "degraded": self.degraded, "reason": self.reason,
            "thresholds_rejected": self.thresholds_rejected,
        }

    def __str__(self) -> str:
        dd = "unavailable" if self.drawdown is None else f"{self.drawdown:.2%}"
        return (f"[{self.action}] dd={dd} "
                f"(warn {self.warn:.0%} / kill {self.kill:.0%}, source={self.source})")


def evaluate(index: Any, peak: Any, *, risk_pct: Any = None,
             warn_override: Any = 0.0, kill_override: Any = 0.0,
             source: str = "twr", available: bool = True,
             degraded: bool = False, reason: str = "") -> DdVerdict:
    """`(index, peak, risk_pct)` → `DdVerdict`。**唯一**的判定入口。

    * 阈值：`app.risk.dd_thresholds()`（唯一实现）；
    * 动作：`app.risk.dd_action()`（唯一实现）；
    * `index` / `peak` 脏（`None`/`NaN`/`inf`/`peak <= 0`）→ 回撤算不出 → `dd_action(None)`
      → `kill`（**最严**，不是放行）；
    * `risk_pct` 脏 → `dd_thresholds(None)` 直接落最严档（25%/30%），
      于是同一个回撤值在脏配置下**更早**触发；
    * `index > peak`（状态被外力改坏）→ 回撤按 0 算但标 `degraded`（`store.audit()` 的
      `dd_peak_below_index` 是同一件事的持久化视角）。
    """
    dd = risk_drawdown(index, peak)
    risk = _finite(risk_pct)
    thresholds = dd_thresholds(risk_pct, warn_override, kill_override)
    action = dd_action(dd, risk_pct, warn_override, kill_override)

    notes: List[str] = []
    if reason:
        notes.append(reason)
    if dd is None:
        notes.append("drawdown is unavailable (index/peak missing, non-finite or peak <= 0) — "
                     "fail-closed: treating it as the kill line")
        action = DD_ACTION_KILL
    else:
        idx, pk = _finite(index), _finite(peak)
        if idx is not None and pk is not None and idx > pk + 1e-12:
            degraded = True
            notes.append("index > peak (state is corrupt) — drawdown is reported as 0 but "
                         "the stored peak must be repaired by a human")
    if thresholds["rejected"]:
        notes.append("DD threshold override was rejected (out of range or warn >= kill) — "
                     "the risk-tier default is in force")
    return DdVerdict(action=action, drawdown=dd,
                     index=_finite(index), peak=_finite(peak),
                     warn=float(thresholds["warn"]), kill=float(thresholds["kill"]),
                     risk_pct=risk, source=source,
                     available=bool(available and dd is not None),
                     degraded=bool(degraded or thresholds["rejected"]),
                     reason="; ".join(notes),
                     thresholds_rejected=bool(thresholds["rejected"]))


def entry_blocked(verdict: DdVerdict) -> bool:
    """给**开仓**路径用的判断。`kill` ⇒ True。

    **没有**配套的 `exit_blocked()`：离场永不被回撤熔断阻挡（见模块 docstring）。
    """
    return bool(getattr(verdict, "blocked", True))


# ------------------------------------------------------------------ 持久化 + 编排


class DrawdownGuard:
    """把 `app/store` 的回撤表 + `app/state` 的读数 + `app/risk` 的口径接起来。

    依赖注入（单测可全离线）::

        guard = DrawdownGuard(store, limits=RiskLimits(risk_pct=0.0133), clock=fake)
        verdict = guard.update(equity=9900.0, ts=..., flows=deposits)
        verdict = guard.verdict()          # 只读；面板/开仓前都用这个

    三个写入口，各有严格边界：

    * :meth:`update` —— 每轮 tick 调一次：重放 TWR、落 `dd_state`、追加锚点、按需剪枝。
      **只升峰值**（`max(库里峰值, 本次重放峰值)`），永远不调 `reset_dd_peak`。
    * :meth:`bootstrap` —— 只在 `dd_state` **不存在**时建立基线（全新账户 / 人工删行后
      的第一次）。峰值取「重放峰值」与「库里最新锚点峰值」的较大者，绝不比历史证据低。
    * :meth:`reset_peak` —— **显式人工**重置。自动路径永远不调它（熔断永不自解）。

    `verdict()` **不自己算一套回撤**：它调 `app/state.StateAssembler.dd_reading()`，
    也就是说「锚点没了 → available=False → 风控拒单」这条规则只有一处实现。
    """

    def __init__(self, store: StateStore, *, limits: Optional[RiskLimits] = None,
                 clock=time.time, assembler: Optional[StateAssembler] = None,
                 anchor_keep_days: float = DD_ANCHOR_KEEP_DAYS,
                 anchor_min_interval_s: float = DEFAULT_ANCHOR_MIN_INTERVAL_S,
                 max_points: int = DEFAULT_MAX_EQUITY_POINTS,
                 prune: bool = True) -> None:
        if store is None:
            raise DdError("DrawdownGuard requires a StateStore")
        self.store = store
        self.limits = limits if limits is not None else RiskLimits()
        self.clock = clock
        self.assembler = assembler if assembler is not None else StateAssembler(store, clock=clock)
        self.anchor_keep_days = float(anchor_keep_days)
        self.anchor_min_interval_s = float(anchor_min_interval_s)
        self.max_points = max(2, int(max_points))
        self.prune = bool(prune)

    def __repr__(self) -> str:
        return (f"DrawdownGuard(risk_pct={float(self.limits.risk_pct):.4%}, "
                f"warn={self.limits.dd_warn:.0%}, kill={self.limits.dd_kill:.0%})")

    # ---------------------------------------------------------------- 只读

    def reading(self, now_ts: Optional[float] = None) -> DDReading:
        """回撤读数（**复用** `app/state.py` 的证据强弱规则，不重算一套）。"""
        return self.assembler.dd_reading(now_ts=self._stamp(now_ts))

    def verdict(self, now_ts: Optional[float] = None) -> DdVerdict:
        """当前结论（只读、无副作用；开仓前/T10 面板都吃这一份）。"""
        reading = self.reading(now_ts)
        if not reading.available:
            # 口径不可用 → 最严档（dd_action(None) = kill）。这不是「回撤 0%」。
            return evaluate(reading.index, reading.peak_index, risk_pct=self.limits.risk_pct,
                            warn_override=self.limits.dd_warn_pct,
                            kill_override=self.limits.dd_kill_pct,
                            source=reading.source, available=False,
                            degraded=True, reason=reading.reason)
        return evaluate(reading.index, reading.peak_index, risk_pct=self.limits.risk_pct,
                        warn_override=self.limits.dd_warn_pct,
                        kill_override=self.limits.dd_kill_pct,
                        source=reading.source, available=True,
                        degraded=reading.degraded, reason=reading.reason)

    def risk_state(self, state: RiskState, now_ts: Optional[float] = None) -> RiskState:
        """把回撤读数注入一份 `RiskState`（`check_entry()` 的 `drawdown` 闸吃它）。

        只覆盖 `dd_index` / `dd_peak_index`；其余字段（权益、持仓、当日笔数）保持调用方
        给的证据。`reading.available=False` 时两个字段都是 `None` → 闸门 fail-closed。
        """
        reading = self.reading(now_ts)
        return replace(state, dd_index=reading.index, dd_peak_index=reading.peak_index)

    def as_dict(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        return {"verdict": self.verdict(now_ts).as_dict(),
                "limits": self.limits.as_dict(),
                "dd_state": dict(self.store.dd_state() or {}),
                "anchor_count": self.store.dd_anchor_count()}

    # ---------------------------------------------------------------- 写：bootstrap

    def bootstrap(self, *, equity: Any, ts: Optional[float] = None,
                  flows: Optional[Iterable[Any]] = None) -> TwrState:
        """建立 `dd_state` 基线（**只在没有 dd_state 时**；已有就抛 `DdError`）。

        锚点选「库里最早的那个权益点」，而不是「现在」：一个跑了两周、`dd_state` 被人删掉的
        账户，用「现在」当锚点会把已有的回撤全部抹成 0 —— 那正是「锚点丢失 = 回撤归零」
        这个事故的另一种写法。没有任何权益点时才退化成「以当前权益为锚点」。
        """
        stamp = self._stamp(ts)
        equity_f = self._equity(equity)
        if self.store.dd_state() is not None:
            raise DdError("dd_state already exists — use update(); only a human may re-base "
                          "it (reset_peak)")
        points = self._points()
        anchor_ts, anchor_equity = (points[0] if points else (stamp, equity_f))
        if anchor_ts >= stamp:
            anchor_ts, anchor_equity = stamp, equity_f
        state = bootstrap(points + [(stamp, equity_f)], flows,
                          anchor_ts=anchor_ts, anchor_equity=anchor_equity)
        peak = max(float(state.peak), self._anchor_peak_floor())
        state = replace(state, peak=peak,
                        anchor_cum_flow=self._cum_flow(flows, stamp))
        self.store.save_dd_state(twr_index=state.index, twr_peak=state.peak,
                                 anchor_ts=state.anchor_ts, anchor_equity=state.anchor_equity,
                                 anchor_cum_flow=state.anchor_cum_flow, ts=stamp)
        self._write_equity_point(stamp, equity_f, state)
        self._append_anchor(stamp, equity_f, state, force=True)
        self._event(EVENT_BOOTSTRAP,
                    f"TWR baseline established: index={state.index:.6f} "
                    f"peak={state.peak:.6f} from anchor ts={state.anchor_ts:.0f} "
                    f"equity={state.anchor_equity:.2f}",
                    data=state.as_dict())
        return state

    # ---------------------------------------------------------------- 写：推进

    def update(self, *, equity: Any, ts: Optional[float] = None,
               flows: Optional[Iterable[Any]] = None,
               append_anchor: bool = True) -> DdVerdict:
        """每轮 tick 调一次：重放 TWR → 落 `dd_state` → 追加锚点 → 剪枝 → 返回结论。

        三条不变式：

        1. **峰值只升不降**：`new_peak = max(库里峰值, 重放峰值)`。重放窗口被截断、
           锚点被移动、历史点被删，都不可能把峰值降低 —— `store.save_dd_state()` 也会
           在峰值下调时抛 `StoreError`（双保险）。
        2. **不自动重置**：本方法不调用 `reset_dd_peak()`，永远不。
        3. **脏输入不写库**：`equity` 读不出来 → 记 `dd_skipped` 事件，返回当前结论
           （不可用时是 kill），绝不写一个猜的值。

        `dd_state` 不存在时自动走 :meth:`bootstrap`（全新账户的第一次），
        并把 `degraded` 标出来 —— 面板要知道这条基线是刚建的。
        """
        stamp = self._stamp(ts)
        equity_f = _finite(equity)
        if equity_f is None or equity_f <= 0:
            self._event(EVENT_SKIPPED,
                        f"equity is unavailable or non-positive ({equity!r}) — TWR not "
                        f"advanced; the drawdown gate stays fail-closed",
                        data={"equity": repr(equity), "ts": stamp})
            return self.verdict(stamp)

        stored = self.store.dd_state()
        bootstrapped = False
        if stored is None:
            state = self.bootstrap(equity=equity_f, ts=stamp, flows=flows)
            bootstrapped = True
            previous_action = None
        else:
            anchor_ts = _finite(stored.get("anchor_ts"))
            anchor_equity = _finite(stored.get("anchor_equity"))
            stored_peak = _finite(stored.get("twr_peak"))
            if (anchor_ts is None or anchor_ts <= 0
                    or anchor_equity is None or anchor_equity <= 0):
                raise DdError(f"dd_state has an invalid anchor (ts={anchor_ts!r}, "
                              f"equity={anchor_equity!r}) — refusing to continue; "
                              f"an admin must reset the peak explicitly")
            points = self._points()
            state = bootstrap(points + [(stamp, equity_f)], flows,
                              anchor_ts=anchor_ts, anchor_equity=anchor_equity)
            state = replace(state, anchor_cum_flow=self._cum_flow(flows, stamp))
            if stored_peak is not None and float(state.peak) < stored_peak - 1e-12:
                # 重放没看到历史高点（窗口被截、点被删）→ 用库里的峰值，绝不降。
                state = replace(state, peak=stored_peak,
                                reason=("; ".join(filter(None, [
                                    state.reason,
                                    f"replayed peak {state.peak:.6f} < stored peak "
                                    f"{stored_peak:.6f} — keeping the stored peak "
                                    f"(peaks never go down)"])).strip()))
            previous_action = self._action_of(stored.get("twr_index"),
                                             stored.get("twr_peak"))
            self.store.save_dd_state(twr_index=state.index, twr_peak=state.peak,
                                     anchor_ts=state.anchor_ts,
                                     anchor_equity=state.anchor_equity,
                                     anchor_cum_flow=state.anchor_cum_flow, ts=stamp)
            self._write_equity_point(stamp, equity_f, state)
            if append_anchor:
                self._append_anchor(stamp, equity_f, state)
            if self.prune:
                self._prune(stamp)

        verdict = state.evaluate(self.limits.risk_pct, self.limits.dd_warn_pct,
                                 self.limits.dd_kill_pct,
                                 source=DD_SOURCE_STATE)
        verdict = replace(verdict, degraded=bool(verdict.degraded or bootstrapped),
                          reason=("; ".join(filter(None, [
                              verdict.reason,
                              "baseline was just bootstrapped from stored equity history"
                              if bootstrapped else ""])).strip()))
        self._log_transition(previous_action, verdict, state, stamp)
        return verdict

    # ---------------------------------------------------------------- 写：人工重置

    def reset_peak(self, *, reason: Any, equity: Any = None, ts: Optional[float] = None,
                   by: Any = "admin") -> DdVerdict:
        """**显式人工**重置峰值（T10b `POST /admin/dd/reset` 调它）。

        这是唯一能把 `twr_peak` 降下来的路径，三条硬要求：

        * `reason` 必须非空（审计要能回答「谁为什么重置的」）；
        * 指数为 0 或读不出来时**拒绝**（权益已亏光 ⇒ DD=100% ⇒ 必须继续拦着等人核对，
          把峰值抹成 0 只会把爆仓藏起来）；
        * 必须落 `events` 审计：`store.reset_dd_peak()` 写 `kind='dd_reset'`（状态变更，
          带 reason 与新指数），本方法再写一条 `kind='dd_reset_audit'`（谁调的、重置前的
          指数是多少）。两条各记一件事，不重复。

        重置后 index = peak = `1.0`、锚点 = 现在（`store.reset_dd_peak()` 的口径）：
        回撤从此刻重新计量，「人工解除熔断」才真的能恢复开仓。
        """
        text = "" if reason is None else str(reason).strip()
        if not text:
            raise DdError("reset_peak requires a non-empty reason (audit trail)")
        stamp = self._stamp(ts)
        state = self.store.dd_state()
        if state is None:
            raise DdError("no dd_state row to reset — run bootstrap() first")
        index = _finite(state.get("twr_index"))
        if index is None or index <= 0.0:
            raise DdError(f"refusing to reset the DD peak: TWR index is {index!r} "
                          f"(account wiped out or state corrupt) — a human must reconcile")
        equity_f = _finite(equity)
        if equity_f is None or equity_f <= 0:
            last = self.store.last_equity()
            equity_f = _finite((last or {}).get("equity"))
        if equity_f is None or equity_f <= 0:
            equity_f = _finite(state.get("anchor_equity"))
        if equity_f is None or equity_f <= 0:
            raise DdError("refusing to reset the DD peak: no usable equity for the new anchor")

        self.store.reset_dd_peak(anchor_ts=stamp, anchor_equity=equity_f, reason=text,
                                 twr_index=TWR_BASE, ts=stamp)
        self._write_equity_point(stamp, equity_f,
                                 TwrState(index=TWR_BASE, peak=TWR_BASE, anchor_ts=stamp,
                                          anchor_equity=equity_f, last_ts=stamp,
                                          last_equity=equity_f))
        self._append_anchor(stamp, equity_f,
                            TwrState(index=TWR_BASE, peak=TWR_BASE, anchor_ts=stamp,
                                     anchor_equity=equity_f, last_ts=stamp,
                                     last_equity=equity_f), force=True)
        self._event(EVENT_RESET_AUDIT,
                    f"DD peak reset to {TWR_BASE:.2f} by {by}: {text} "
                    f"(previous index={index:.6f})",
                    data={"by": str(by), "reason": text, "previous_index": index,
                          "anchor_ts": stamp, "anchor_equity": equity_f})
        return self.verdict(stamp)

    # ---------------------------------------------------------------- 内部

    def _stamp(self, now_ts: Optional[float]) -> float:
        if now_ts is None:
            value = _finite(self.clock())
            return value if value is not None else time.time()
        value = _finite(now_ts)
        if value is None:
            raise DdError(f"now_ts must be a finite epoch second, got {now_ts!r}")
        return value

    @staticmethod
    def _equity(value: Any) -> float:
        equity = _finite(value)
        if equity is None or equity <= 0:
            raise DdError(f"equity must be a positive finite number, got {value!r}")
        return equity

    def _points(self) -> List[Tuple[float, float]]:
        rows = self.store.equity_curve(limit=self.max_points)
        return normalise_points(rows)

    def _anchor_peak_floor(self) -> float:
        """库里已有证据里的最高峰值（最新锚点的 `twr_peak`）；没有证据 → 0。

        为什么要这个下限：`dd_state` 被删之后再 bootstrap，重放窗口可能只覆盖最近一段，
        算出来的峰值会低于曾经的真实峰值 → 回撤被低估。锚点表里记的峰值是**证据**，
        新基线不许比它低。
        """
        anchor = self.store.latest_dd_anchor()
        peak = _finite((anchor or {}).get("twr_peak"))
        return float(peak) if peak is not None and peak > 0 else 0.0

    @staticmethod
    def _cum_flow(flows: Optional[Iterable[Any]], stamp: float) -> float:
        total = cum_flow(flows, stamp)
        return float(total)

    def _write_equity_point(self, stamp: float, equity: float, state: TwrState) -> None:
        """把 TWR 指数写回权益点（面板虚线 + `dd.py` 用**同一个**数）。"""
        try:
            self.store.record_equity(equity, ts=stamp, twr_index=state.index,
                                     cum_flow=state.anchor_cum_flow, source="dd")
        except StoreError as exc:                      # 例如 index < 0 被拒
            self._event("dd_ledger_error", f"could not write twr_index to equity_curve: {exc}",
                        data={"ts": stamp, "index": state.index})

    def _append_anchor(self, stamp: float, equity: float, state: TwrState,
                       *, force: bool = False) -> None:
        if not force:
            latest = self.store.latest_dd_anchor()
            latest_ts = _finite((latest or {}).get("ts"))
            if latest_ts is not None and stamp - latest_ts < self.anchor_min_interval_s:
                return
        try:
            self.store.append_dd_anchor(stamp, equity, twr_index=state.index,
                                        twr_peak=state.peak,
                                        cum_flow=state.anchor_cum_flow)
        except StoreError as exc:
            self._event("dd_anchor_error", f"could not append a dd anchor: {exc}",
                        data={"ts": stamp, "equity": equity})

    def _prune(self, stamp: float) -> None:
        try:
            self.store.prune_dd_anchors(keep_days=self.anchor_keep_days, ts=stamp)
        except StoreError as exc:                     # 剪枝失败不该影响本轮判定
            logger.warning("prune_dd_anchors failed: %s", exc)

    def _action_of(self, index: Any, peak: Any) -> Optional[str]:
        dd = risk_drawdown(index, peak)
        if dd is None:
            return None
        return dd_action(dd, self.limits.risk_pct, self.limits.dd_warn_pct,
                         self.limits.dd_kill_pct)

    def _log_transition(self, previous: Optional[str], verdict: DdVerdict,
                        state: TwrState, stamp: float) -> None:
        """只在**动作变化**时落事件（面板事件流不能被「每轮一条 dd ok」淹掉）。"""
        current = verdict.action
        if previous == current:
            return
        dd_text = "unavailable" if verdict.drawdown is None else f"{verdict.drawdown:.2%}"
        data = {"action": current, "previous": previous, "drawdown": verdict.drawdown,
                "index": state.index, "peak": state.peak,
                "warn": verdict.warn, "kill": verdict.kill,
                "risk_pct": float(self.limits.risk_pct), "ts": stamp}
        if current == DD_ACTION_KILL:
            self._event(EVENT_KILL,
                        f"dd {dd_text} >= kill {verdict.kill:.0%} (risk_pct "
                        f"{float(self.limits.risk_pct):.4%}) — new entries are blocked; "
                        f"exits and stop orders are unaffected", data=data)
        elif current == DD_ACTION_WARN:
            self._event(EVENT_WARN,
                        f"dd {dd_text} >= warn {verdict.warn:.0%} (risk_pct "
                        f"{float(self.limits.risk_pct):.4%})", data=data)
        else:
            self._event(EVENT_OK,
                        f"dd back to ok ({dd_text}) — previous action {previous}",
                        data=data)

    def _event(self, kind: str, message: str, *,
               data: Optional[Mapping[str, Any]] = None) -> None:
        """落 `events`（日志 + 库）。库写失败**不吞**：回撤状态落不下来比报警更重要。"""
        logger.info("[%s] %s", kind, message)
        self.store.log_event(kind, message, data=dict(data or {}))
