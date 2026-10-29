"""面板（T10a/T10c）：6 区块的只读渲染 —— JSON API + 自包含 HTML。

========================================================================
硬约束（有测试钉住，改代码前先读这一节）
========================================================================

**本模块绝不 import `app.exchange`**（也不 import `app.executor` —— 它会把
`app.exchange` 拖进 import 图）。理由不是洁癖：`app.exchange` 是**唯一**持私钥、
唯一能下单的模块（见它的 docstring 第 0 节）。面板每天被刷新几百次，一次 import 就
把"私钥路径"接进了 HTTP 进程；一个能读私钥的进程不该同时对外提供网页。所以面板只吃
两样东西：

1. **DB**（`app.store.StateStore`，只读查询）；
2. **Lighter 的公开只读接口**（K 线 / 盘口 —— `app.market`，无鉴权、无签名、无私钥）。

交易所侧的**私有**读数（账户权益 / 挂单 / 持仓）由**调用方注入**：`app.main` 在启动时
把 provider 塞进 :class:`Sources`，本模块只拿到已经取好的普通 dict/list。于是：

* 面板进程即使一个凭据都没有也能跑（降级成 0 值 + WARNING，见 :class:`Panel`）；
* 面板拿不到的东西，宁可显示「未知」也不显示「一切正常」。

========================================================================
六个区块（`docs/PLAN.md` 第 5.5 节）
========================================================================

============  ==========================  ==========================================
区块           方法                        看什么
============  ==========================  ==========================================
① 状态条        :meth:`Panel.status`        熔断/kill/数据源新鲜度/上次 tick/权益
② 仓位表        :meth:`Panel.positions`     每标的一行：方向、浮盈、止损距离、强平
③ 止损覆盖      :meth:`Panel.stops`         **交易所到底有没有这张止损单**（裸奔检测）
④ 盈亏曲线      :meth:`Panel.pnl`           实线=累计盈亏(U)，虚线=TWR 指数(%)
⑤ 三标的动态    :meth:`Panel.symbols`       带宽 vs 密集门槛、连续密集根数、SMA200
⑥ 事件流        :meth:`Panel.events`        最近 20 条，**含跳过**（否则用户以为系统坏了）
============  ==========================  ==========================================

**盈亏曲线的口径红线**：TWR 只用 `app.dd` 里的函数（`bootstrap` / `advance` / `net_flow`
/ `normalise_points`），本模块**一行都不重算**。面板自己另算一套是旧系统踩过的坑
（三个回撤口径混用：面板说 12%、熔断按 25%、告警按别的数），所以
:func:`pnl_curve` 还会把「逐点 `advance` 的结果」与「`dd.bootstrap` 一步重放的结果」
对账，把差值写进 `consistency` —— 漂移在运行时立刻看得见，而不是等两个数字打起来。

========================================================================
降级策略（验收项：绝不 500）
========================================================================

每个区块**独立**包在 :meth:`Panel._guard` 里：一个区块炸了，只让那一个区块变成
「0 值 + WARNING + degraded=true」，其余区块照常出。DB 打不开、provider 抛异常、
行情接口 502、数据只有 1 个点 —— 全都走这条路。**没有一个路径会抛到 HTTP 层**。

「0 值」不等于「正常」：每个降级载荷都带 `degraded: true` 与 `warnings: [...]`，
状态条据此变红。这是刻意的取舍 —— 面板宁可喊「我不知道」，也不能静默显示一个
好看的 0。
"""

from __future__ import annotations

import bisect
import html
import json
import logging
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from app import dd as dd_module
from app import strategy
from app.dd import TWR_BASE, TWR_DISPLAY_BASE, TwrState, advance, bootstrap, net_flow, \
    normalise_points
from app.risk import RiskLimits, drawdown as risk_drawdown
from app.store import StateStore

__all__ = [
    "BLOCK_EVENTS", "BLOCK_PNL", "BLOCK_POSITIONS", "BLOCK_RECORDS", "BLOCK_STATUS",
    "BLOCK_STOPS", "BLOCK_SYMBOLS", "BLOCKS", "DEFAULT_SYMBOLS", "HOMEPAGE_BLOCKS",
    "KILL_EVENT_KIND", "LEVERAGE", "MAX_CURVE_POINTS", "MAX_EVENTS", "MAX_RECORD_ROWS",
    "Panel", "RECORDS_BLOCKS", "Sources", "event_action", "kill_state", "pnl_curve",
    "render_html", "render_records_html", "set_kill",
]

#: 成交类型的展示文案（`trades.kind` 只有这三个取值，见 app/store 的 CHECK）
_TRADE_KIND_TEXT = {"entry": "开仓", "exit": "离场", "stop": "止损"}

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 常量

#: PLAN 的三标的（顺序固定：面板表格与回测报告都用这个顺序）
DEFAULT_SYMBOLS: Tuple[str, ...] = ("BTC", "ETH", "SOL")

BLOCK_STATUS = "status"
BLOCK_POSITIONS = "positions"
BLOCK_STOPS = "stops"
BLOCK_PNL = "pnl"
BLOCK_SYMBOLS = "symbols"
BLOCK_EVENTS = "events"

#: 六个区块（顺序 = 页面顺序；`render_html` 与测试都吃这个元组）
BLOCKS: Tuple[str, ...] = (BLOCK_STATUS, BLOCK_POSITIONS, BLOCK_STOPS,
                           BLOCK_PNL, BLOCK_SYMBOLS, BLOCK_EVENTS)

#: 记录页的两个区块（订单明细 + 执行器事件流）。事件流从首页挪到记录页：首页只回答
#: 「现在怎么样」，历史与流水放记录页，两者职责不再混在一屏里。
BLOCK_RECORDS = "records"
HOMEPAGE_BLOCKS: Tuple[str, ...] = (BLOCK_STATUS, BLOCK_POSITIONS, BLOCK_STOPS,
                                    BLOCK_PNL, BLOCK_SYMBOLS)
RECORDS_BLOCKS: Tuple[str, ...] = (BLOCK_RECORDS, BLOCK_EVENTS)
MAX_RECORD_ROWS = 200          # 记录页默认展示的成交笔数（一屏够用，翻页留到以后）
EQUITY_AFTER_MAX_LAG_S = 6 * 3600.0   # 「成交后余额」最多回看这么久内的权益快照

#: 事件流的条数（PLAN 第 5.5 节：最近 20 条）
MAX_EVENTS = 20

#: 曲线最多画多少个点（再多手机上也看不清，且 SVG 会变重）
MAX_CURVE_POINTS = 500

#: 逐仓杠杆（`docs/SIZING_RISK.md`：与回测口径一致的 5x）。只用于面板估算保证金/强平。
LEVERAGE = 5.0

#: kill 开关的持久化载体：`events` 表里最后一条 `kind='admin_kill'` 就是当前状态。
#: 为什么要落库而不是内存变量：容器重启后「我今天不想交易」必须还生效，否则一个
#: 部署就把最后一道人工闸门悄悄打开了。为什么不用新表：不动 schema（T5 的迁移表
#: 只许追加已发布的语句，加列要慎重），事件表本身就是审计流水。
KILL_EVENT_KIND = "admin_kill"

#: 一次性写进 `events` 的每个管理动作（成功/拒绝都记，审计要能回答「谁试过什么」）
ADMIN_EVENT_KIND = "admin_action"

#: 心跳事件的 kind 与 T9 `watchdog.default_heartbeat_probe()` 读的是同一个字符串。
HEARTBEAT_KIND = "heartbeat"

#: 数据源新鲜度阈值：最后一根已收盘 4h bar 的收盘时间超过 8h 就算陈旧
#: （与 `app.market.FRESH_MAX_AGE_MS` / `app.watchdog` 同一个数，不另发明）。
STALE_AFTER_S = 8 * 3600.0

#: 心跳超期阈值：与 `app.watchdog.DEFAULT_HEARTBEAT_MAX_AGE_S` 同值（1.5 个 4h 周期）。
HEARTBEAT_MAX_AGE_S = 6 * 3600.0

BAR_MS = 4 * 3600 * 1000

#: 事件 kind → 面板「动作」列。`skip` 类必须单独可见：用户看到「开仓 0 笔」时的第一反应
#: 是「系统坏了」，而真相通常是「未过密集门槛」—— 那条事件必须能显示出来。
ACTION_ENTRY = "开仓"
ACTION_EXIT = "离场"
ACTION_STOP = "止损"
ACTION_KILL = "熔断"
ACTION_SKIP = "跳过"
ACTION_ADMIN = "人工"
ACTION_HEARTBEAT = "心跳"
ACTION_CHECK = "校验"
ACTION_OTHER = "其他"


# ------------------------------------------------------------------ 小工具


def _finite(value: Any) -> Optional[float]:
    """安全转 float：`None` / 垃圾 / `NaN` / `±inf` → `None`（`NaN` 会让比较全部为 False）。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _zero(value: Any) -> float:
    """降级用的「0 值」：读不出来就给 0.0（只用于展示，绝不参与风控判定）。"""
    out = _finite(value)
    return 0.0 if out is None else out


def _pct(value: Any, digits: int = 2) -> str:
    out = _finite(value)
    return "—" if out is None else f"{out * 100:.{digits}f}%"


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """从 dict / sqlite3.Row / 对象读字段。"""
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return getattr(row, key, default)


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stop_coi_of(row: Any) -> Optional[int]:
    """仓位行里的止损单身份：`positions.stop_order_id` 形如 `coi:<clientOrderIndex>`。"""
    raw = str(_row_get(row, "stop_order_id") or "").strip()
    if raw.startswith("coi:"):
        raw = raw[4:]
    return _int_or_none(raw)


def _order_trigger_tick(order: Mapping[str, Any], price_decimals: int) -> Optional[int]:
    """挂单**触发价** → 整数 tick。

    优先用解析层给的 `trigger_price_int`；拿不到就按该标的的 `price_decimals`
    把原始小数串（交易所返回 `"105.257"` 这种字符串）换算成 tick。
    **不要**退回 `price_int`：那是触发后的最差成交价（= 触发价 + 滑点），比止损价高/低一截。
    """
    ticks = _int_or_none(order.get("trigger_price_int"))
    if ticks is not None:
        return ticks
    raw = order.get("trigger_price_raw")
    if raw in (None, ""):
        return None
    try:
        return int((Decimal(str(raw).strip()) * (Decimal(10) ** int(price_decimals))).to_integral_value())
    except (ArithmeticError, ValueError):
        return None


def _loads(value: Any) -> Dict[str, Any]:
    """`events.data` 是 JSON 字符串；解析失败 → `{}`（事件正文仍然照常显示）。"""
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, Mapping) else {}


def _dur(seconds: Any) -> str:
    """秒 → 人类可读（面板上「上次 tick 12 分钟前」）。"""
    hours = _finite(seconds)
    if hours is None or hours < 0:
        return "—"
    if hours < 90:
        return f"{hours:.0f} 秒前"
    if hours < 5400:
        return f"{hours / 60:.0f} 分钟前"
    if hours < 172800:
        return f"{hours / 3600:.1f} 小时前"
    return f"{hours / 86400:.1f} 天前"


def event_action(kind: Any) -> str:
    """事件 kind → 面板动作列（纯字符串映射，不猜业务）。

    顺序有讲究：`stop_fill_settled` / `stop_order_failed` 里既有 `stop` 也有别的词，
    先判 `stop`；`admin_kill` 里有 `kill` 但也有 `admin`，先判 `admin`。
    「跳过」类事件（`skip` / `skipped` / `dd_skipped`）必须映射成「跳过」而不是「其他」。
    """
    text = str(kind or "").strip().lower()
    if not text:
        return ACTION_OTHER
    if "skip" in text:
        return ACTION_SKIP
    if text.startswith("admin") or text.startswith("kill_switch") or text.startswith("dd_reset"):
        return ACTION_ADMIN
    if "kill" in text or "circuit" in text:
        return ACTION_KILL
    if "heartbeat" in text:
        return ACTION_HEARTBEAT
    if "stop" in text:
        return ACTION_STOP
    if "exit" in text or "clos" in text:
        return ACTION_EXIT
    if "entry" in text or text in ("open", "entry_filled"):
        return ACTION_ENTRY
    if any(word in text for word in ("reconcile", "audit", "watchdog", "tick", "check")):
        return ACTION_CHECK
    return ACTION_OTHER


# ------------------------------------------------------------------ kill 开关


def kill_state(store: Any) -> Dict[str, Any]:
    """当前 kill 状态（`events` 里最后一条 `admin_kill`）。

    读不出来 → `known=False` + `kill=False`，调用方必须把 `known=False` 当「不知道」，
    **不能**当成「kill 关着所以可以开仓」。
    """
    out = {"kill": False, "known": False, "ts": None, "by": "", "reason": ""}
    if store is None:
        return out
    try:
        rows = store.events(kind=KILL_EVENT_KIND, limit=1)
    except Exception as exc:                                  # noqa: BLE001 - 面板不抛
        logger.warning("could not read kill state: %s", exc)
        return out
    if not rows:
        return dict(out, known=True)
    row = rows[0]
    data = _loads(_row_get(row, "data"))
    return {"kill": bool(data.get("kill", False)), "known": True,
            "ts": _finite(_row_get(row, "ts")),
            "by": str(data.get("by") or ""), "reason": str(data.get("reason") or "")}


def set_kill(store: StateStore, enabled: Any, *, by: str = "admin", reason: str = "",
             ts: Optional[float] = None) -> Dict[str, Any]:
    """落一条 `admin_kill` 事件（**持久化**+审计）。返回写入后的状态。

    只挡开新仓：`app/risk._gate_kill()` 是唯一消费点，离场/止损路径不经过它
    （`app/lifecycle.decide_exit()` 的签名里根本没有 kill/risk 入参）。
    """
    flag = bool(enabled)
    payload = {"kill": flag, "by": str(by or "admin"), "reason": str(reason or "")}
    store.log_event(KILL_EVENT_KIND,
                    f"kill switch {'ON' if flag else 'OFF'} by {payload['by']}"
                    f"{(': ' + payload['reason']) if payload['reason'] else ''}",
                    data=payload, ts=ts)
    return {"kill": flag, "known": True, "ts": _finite(ts), **payload}


# ------------------------------------------------------------------ 盈亏曲线


def _flows_from_points(points: Sequence[Any]) -> List[Dict[str, float]]:
    """从 `equity_curve.cum_flow`（累计净入金）还原**逐段**流水。

    为什么用这一列而不是另建一张流水表：`app.dd.DrawdownGuard` 每轮写权益点时把
    `anchor_cum_flow` 一起写进 `cum_flow`，所以「累计净入金」在库里已经有权威值。
    差值 = 该区间的净流水，**左开右闭**口径与 `app.dd.net_flow()` 完全一致。

    `cum_flow` 缺失/变小的点不造流水（宁可少剔一笔，也不要把一笔算不清的流水摊到
    错误的区间里 —— 与 `app.dd.net_flow()` 的注释同一条理由）。
    """
    flows: List[Dict[str, float]] = []
    prev: Optional[float] = None
    for row in points:
        ts = _finite(_row_get(row, "ts"))
        if ts is None:
            continue
        cum = _finite(_row_get(row, "cum_flow"))
        if cum is None:
            cum = 0.0 if prev is None else prev
        if prev is not None and cum != prev:
            flows.append({"ts": ts, "amount": cum - prev})
        prev = cum
    return flows


#: 入金跳变检测阈值（2026-10-28 审计 P2-3）：区间无成交且权益变化 ≥ max(JUMP_ABS_U,
#: JUMP_PCT × 上一格权益) 时提示「可能有出入金没被流水覆盖」。5% 是「无成交时持仓
#: 浮动盈亏 30 分钟内大概率到不了」的量级——宁可漏报小出入金，不能每次持仓波动都喊。
JUMP_ABS_U = 5.0
JUMP_PCT = 0.05


def _unexplained_equity_jumps(points: Sequence[Any],
                              trade_ts: Sequence[float],
                              flow_ts: Sequence[float] = (),
                              *, abs_u: float = JUMP_ABS_U,
                              pct: float = JUMP_PCT) -> List[Dict[str, Any]]:
    """找出「权益跳变但没有任何成交解释、也没有流水覆盖」的区间（纯函数，可测）。

    背景：`infer_transfer_flow` 在区间内有持仓/成交时**保守放弃**推断——那笔出入金
    就会静默混进 cum_pnl（2026-10-28 的 399 USDC 事故）。这里给面板一个显眼的提示，
    而不是让人对着失真的盈亏曲线发呆。**只是提示**，不参与任何计算。

    判定：相邻两个权益点之间 (t0, t1] 内 (a) 没有任何成交时间戳，(b) 没有任何流水
    时间戳（`account_flows` 表，**含**锚点之前补录的历史流水—— cum_flow 列只反映
    当前锚点之后的流水，只看它会误报已补录的跳变），(c) cum_flow 列差为 0，
    (d) |Δequity| ≥ max(abs_u, pct × equity_t0)。返回最新的在前，最多 3 条。
    """
    stamps = sorted(float(t) for t in list(trade_ts) + list(flow_ts)
                    if t and t > 0)
    cleaned: List[Tuple[float, float, float]] = []
    for row in points:
        ts = _finite(_row_get(row, "ts"))
        eq = _finite(_row_get(row, "equity"))
        cum = _finite(_row_get(row, "cum_flow"))
        if ts is None or eq is None:
            continue
        cleaned.append((float(ts), float(eq), float(cum if cum is not None else 0.0)))
    rows: List[Dict[str, Any]] = []
    for (t0, e0, c0), (t1, e1, c1) in zip(cleaned, cleaned[1:]):
        delta = e1 - e0
        if abs(delta) < max(abs_u, pct * e0):
            continue
        if c1 != c0:
            continue                                  # 有流水覆盖 → 已剔除，不提示
        i = bisect.bisect_right(stamps, t0)
        j = bisect.bisect_right(stamps, t1)
        if j > i:
            continue                                  # 区间内有成交 → 可能是盈亏
        rows.append({"ts": t1, "delta": delta,
                     "equity_before": e0, "equity_after": e1})
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:3]


def pnl_curve(points: Sequence[Any], *, anchor_ts: Optional[float] = None,
              anchor_equity: Optional[float] = None,
              flows: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """权益点 → 双口径曲线（**TWR 只用 `app.dd` 的函数**）。

    * 实线 `cum_pnl`：`equity − anchor_equity − Σ流水(anchor, ts]`，单位 U（已剔出入金）；
    * 虚线 `twr_pct`：`app.dd` 的资金加权收益指数 ×100，单位 %。

    返回里带 `consistency`：逐点 `app.dd.advance()` 的结果 vs
    `app.dd.bootstrap()` 一次性重放的结果。两者必然相等（同一个函数、同一份流水），
    不相等就说明有人在这里塞了第二套算法 —— 面板会把它标红，而不是各说各话。
    """
    rows = normalise_points(points)
    warning: List[str] = []
    if flows is None:
        flows = _flows_from_points(points)

    a_ts = _finite(anchor_ts)
    a_eq = _finite(anchor_equity)
    if a_ts is None or a_eq is None or a_ts <= 0 or a_eq <= 0:
        if rows:
            a_ts, a_eq = rows[0]
            warning.append("dd_state 没有可用锚点 —— 曲线以最早一个权益点为锚点"
                           "（回撤可能被低估，状态条同时给出 dd 读数）")
        else:
            return {"points": [], "cum_pnl": 0.0, "twr_index": TWR_BASE,
                    "twr_pct": TWR_DISPLAY_BASE, "twr_peak": TWR_BASE,
                    "peak_pct": TWR_DISPLAY_BASE, "drawdown_pct": 0.0,
                    "anchor_ts": None, "anchor_equity": None, "steps": 0, "skipped": 0,
                    "consistency": {"match": True, "index_delta": 0.0, "peak_delta": 0.0},
                    "warnings": ["没有权益点 —— 曲线为空（0 值 + WARNING，不是 500）"]}

    state = TwrState(index=TWR_BASE, peak=TWR_BASE, anchor_ts=float(a_ts),
                     anchor_equity=float(a_eq), last_ts=float(a_ts),
                     last_equity=float(a_eq))
    series: List[Dict[str, Any]] = [{
        "ts": float(a_ts), "equity": float(a_eq), "cum_pnl": 0.0,
        "twr_index": TWR_BASE, "twr_pct": TWR_DISPLAY_BASE,
    }]
    skipped = 0
    for ts, equity in rows:
        if ts <= float(state.last_ts):
            continue
        flow = net_flow(flows, float(state.last_ts), ts)
        nxt = advance(state, ts=ts, equity=equity, flow=flow)
        if nxt is None:                       # 脏点：`advance` 已经按「不猜」返回 None
            skipped += 1
            continue
        state = nxt
        series.append({
            "ts": float(ts), "equity": float(equity),
            "cum_pnl": float(equity) - float(a_eq) - net_flow(flows, float(a_ts), float(ts)),
            "twr_index": float(state.index),
            "twr_pct": float(state.index) * TWR_DISPLAY_BASE,
        })

    # 对账：一次性重放（`bootstrap`）必须给出同一个 index/peak。
    check = bootstrap(rows, flows, anchor_ts=float(a_ts), anchor_equity=float(a_eq))
    index_delta = abs(float(check.index) - float(state.index))
    peak_delta = abs(float(check.peak) - float(state.peak))
    match = index_delta <= 1e-12 and peak_delta <= 1e-12
    if not match:
        warning.append(f"TWR 口径漂移：逐点 advance={state.index:.12f}/{state.peak:.12f} "
                       f"vs bootstrap={check.index:.12f}/{check.peak:.12f}")
    if skipped:
        warning.append(f"{skipped} 个权益点无法推进 TWR（时间戳/权益非法）—— 已跳过")

    if len(series) > MAX_CURVE_POINTS:
        series = series[-MAX_CURVE_POINTS:]

    drawdown = risk_drawdown(state.index, state.peak)
    return {
        "points": series,
        "cum_pnl": float(series[-1]["cum_pnl"]),
        "twr_index": float(state.index),
        "twr_pct": float(state.index) * TWR_DISPLAY_BASE,
        "twr_peak": float(state.peak),
        "peak_pct": float(state.peak) * TWR_DISPLAY_BASE,
        "drawdown_pct": drawdown,
        "anchor_ts": float(a_ts), "anchor_equity": float(a_eq),
        "steps": int(state.steps), "skipped": int(skipped),
        "consistency": {"match": bool(match), "index_delta": index_delta,
                        "peak_delta": peak_delta},
        "warnings": warning,
    }


# ------------------------------------------------------------------ 数据源注入


@dataclass
class Sources:
    """面板可选的**外部**读数（全部可不给；不给 = 对应区块降级）。

    * `equity`  ：`() -> float`，账户权益（USDC）。生产上由 Lighter 只读账户接口提供。
    * `orders`  ：`() -> [{symbol, price, qty, order_id, reduce_only}]`，交易所**活挂单**。
                  止损覆盖区块全靠它 —— 没有它就只能显示「未知」，绝不显示「在」。
    * `bars`    ：`(symbol) -> [strategy.Bar]`，**已收盘**的 4h K 线（`app.market` 给）。
    * `prices`  ：`() -> {symbol: 最新价}`，仓位表的「现价」。

    每个都是**尽力而为**：抛异常 = 该区块降级（0 值 + WARNING），不冒泡。
    """

    equity: Optional[Callable[[], Any]] = None
    orders: Optional[Callable[[], Any]] = None
    bars: Optional[Callable[[str], Any]] = None
    prices: Optional[Callable[[], Any]] = None
    #: `(symbol) -> [strategy.Bar]`，**只读缓存、绝不发网络请求**。
    #: `/status` 用它算「数据源新鲜度」：没有它时状态条会为了一个时间戳去拉 3 标的 × 8 页
    #: K 线 —— 实测重启后第一次 `/status` 要 24s（之后才快，因为缓存被顺手暖了）。
    cached_bars: Optional[Callable[[str], Any]] = None


# ------------------------------------------------------------------ 面板


class Panel:
    """6 区块装配器。**只读**：除 kill 开关外不写库（`set_kill` 是独立函数）。

    依赖注入的形态（测试全离线）::

        panel = Panel(lambda: StateStore(tmp_db), sources=Sources(...), clock=fake)
        panel.snapshot()   # -> {"as_of", "blocks": {...}, "warnings": [...]}
        panel.page()       # -> 自包含 HTML

    `store_factory` 而不是 `store`：DB 打不开必须**每次请求都重试**（数据库可能只是
    正在迁移/短暂锁住），而不是启动时失败一次就永远 500 —— 那是 T1 的 `/health` 之外
    唯一还被允许"永远可用"的东西。首次成功后缓存连接。

    `credentials` 是**注入的探针**（`() -> {"configured": bool, "missing": [...],
    "detail": str}`），只用于状态条。面板自己不读 env、不读配置：它连配置层的 import
    都不该有（见模块 docstring 的 import 边界）。`None` = 没人告诉我 ⇒ 状态条显示
    「凭据 未知」，不猜、不报警。

    **`close()` 不是可选的礼貌**：`store_factory()` 建出来的 `StateStore` 内部是一个
    长连接 `sqlite3.Connection`。面板服务是 7×24 常驻进程，一次 `create_app()` 建一个
    Panel、第一个请求建一个连接 —— 没有 `close()` 就等于每重启/每建一个 app 漏一个
    文件描述符，并让 SQLite 的 `-wal`/`-shm` 一直被持有。所以 Panel **拥有**它懒建的
    store：:meth:`close` 关掉它（幂等），`app.main` 的 lifespan 在应用关闭时调它。

    `owns_store=False` 是给「工厂返回的是别人（例如测试夹具 / 共享进程）的 store」用的：
    那时 Panel 只丢弃引用，不关别人的连接。
    """

    def __init__(self, store_factory: Callable[[], Any], *,
                 sources: Optional[Sources] = None,
                 limits: Optional[RiskLimits] = None,
                 mode: str = "live",
                 now: Callable[[], float] = time.time,
                 symbols: Sequence[str] = DEFAULT_SYMBOLS,
                 credentials: Optional[Callable[[], Any]] = None,
                 max_events: int = MAX_EVENTS,
                 max_curve_points: int = MAX_CURVE_POINTS,
                 stale_after_s: float = STALE_AFTER_S,
                 heartbeat_max_age_s: float = HEARTBEAT_MAX_AGE_S,
                 sides: Sequence[int] = (1, -1),
                 owns_store: bool = True) -> None:
        if store_factory is None:
            raise ValueError("Panel requires a store factory")
        self._factory = store_factory
        self._store: Optional[Any] = None
        self._store_error: str = ""
        self._closed: bool = False
        self._owns_store: bool = bool(owns_store)
        self._credentials_probe = credentials
        self.sources = sources if sources is not None else Sources()
        self.limits = limits if limits is not None else RiskLimits()
        self.mode = str(mode or "live").lower()
        self.clock = now
        self.symbols: Tuple[str, ...] = tuple(str(s).strip().upper() for s in symbols
                                              if str(s).strip())
        self.max_events = max(1, int(max_events))
        self.max_curve_points = max(2, int(max_curve_points))
        self.stale_after_s = float(stale_after_s)
        self.heartbeat_max_age_s = float(heartbeat_max_age_s)
        # 方向开关与执行器同源（`Executor(sides=...)`）。今天两侧都开；面板必须拿同一份，
        # 否则关掉某个方向后面板还会说「可开多」——那种假阳性比没有这一列更糟。
        self.sides: Tuple[int, ...] = tuple(int(s) for s in sides if int(s) in (1, -1)) or (1, -1)
        # 三标的动态的指标记忆化（审计 P3）：`compute_indicators` 要扫每标的 ~3900 根
        # bar，原来每个 /symbols 请求重算 3 份。按「代」失效——任一标的的最后一根 bar
        # 换代（bar_ms 前进）就整体清空；同一代内每个标的小程序只算一次。现价相关的列
        # 照常每次重算，不受缓存影响。
        self._symbol_ind_gen: Optional[Tuple[int, int]] = None
        self._symbol_ind_cache: Dict[str, Any] = {}

    def __repr__(self) -> str:
        state = ("closed" if self._closed
                 else ("ok" if self._store is not None else "pending"))
        return f"Panel(symbols={self.symbols}, mode={self.mode!r}, store={state})"

    # ---------------------------------------------------------------- 基础设施

    def _now(self) -> float:
        value = _finite(self.clock())
        return value if value is not None else time.time()

    @property
    def closed(self) -> bool:
        """已调用过 :meth:`close`（**之后不会再建新连接**）。"""
        return self._closed

    def close(self) -> None:
        """关掉懒建的 store。**幂等**；store 未建 / 无 `close()` / 关失败都不抛。

        为什么必须存在：`StateStore` 内部是长连接 `sqlite3.Connection`。面板进程 7×24
        常驻，`create_app()` 每次建一个 Panel、第一个请求建一个连接 —— 没有这条路径
        连接永不释放（fd 泄漏 + SQLite 的 `-wal`/`-shm` 一直被持有），进程被反复创建
        时（测试、重启、多 worker）会累积。实测证据：修之前全量测试有 5 条
        `ResourceWarning: unclosed database`，修之后 0 条。

        关失败只记 WARNING，不抛：这发生在**关闭路径**上，抛出去只会把一次干净的退出
        变成崩溃，而泄漏本身已经被"至少试过"了。
        """
        if self._closed:
            return
        self._closed = True
        store, self._store = self._store, None
        if store is None or not self._owns_store:
            return
        closer = getattr(store, "close", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception as exc:                              # noqa: BLE001 - 关闭路径
            logger.warning("could not close the panel store: %s", exc)

    def __enter__(self) -> "Panel":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def store(self) -> Optional[Any]:
        """拿 store（首次成功即缓存；失败不抛，返回 `None` 让区块降级）。

        **关闭之后一律返回 `None`**，不再建新连接：shutdown 期间或之后的一次请求
        不该复活一个没人会再关掉的 `sqlite3.Connection` —— 那正是这次修掉的泄漏。
        降级载荷里会带 `panel is closed` 的理由，而不是一个看起来正常的空面板。
        """
        if self._store is not None:
            return self._store
        if self._closed:
            self._store_error = self._store_error or "panel is closed"
            return None
        try:
            self._store = self._factory()
            self._store_error = ""
        except Exception as exc:                              # noqa: BLE001 - 降级不崩
            if not self._store_error:
                logger.warning("panel store unavailable: %s", exc)
            self._store_error = f"{type(exc).__name__}: {exc}"
            return None
        return self._store

    @property
    def store_error(self) -> str:
        return self._store_error

    def _credential_status(self) -> Dict[str, Any]:
        """交易凭据状态（来自注入的探针；**绝不**自己读 env / 配置）。

        `known=False` = 没人告诉我（例如测试里直接注入的 Panel）⇒ 不猜、不报警。
        这条信息存在的唯一理由是让状态条能把「没有数据库」和「没有私钥」分开说 ——
        前者是面板瞎了，后者只是不能下单。
        """
        unknown: Dict[str, Any] = {"known": False, "configured": None, "missing": [],
                                   "detail": ""}
        if self._credentials_probe is None:
            return unknown
        try:
            payload = dict(self._credentials_probe() or {})
        except Exception as exc:                              # noqa: BLE001 - 降级不崩
            logger.warning("credential probe failed: %s", exc)
            return dict(unknown, detail=f"{type(exc).__name__}: {exc}")
        missing = [str(key) for key in payload.get("missing") or ()]
        payload["known"] = True
        payload["configured"] = bool(payload.get("configured", not missing))
        payload["missing"] = missing
        payload.setdefault("detail", "")
        return payload

    def _guard(self, name: str, builder: Callable[[], Dict[str, Any]],
               fallback: Callable[[str], Dict[str, Any]]) -> Dict[str, Any]:
        """区块级隔离：一个区块的任何异常都只降级它自己。"""
        try:
            payload = builder()
        except Exception as exc:                              # noqa: BLE001 - 验收项：不 500
            logger.warning("panel block %s degraded: %s", name, exc, exc_info=True)
            payload = fallback(f"{name}: {type(exc).__name__}: {exc}")
        payload.setdefault("block", name)
        payload.setdefault("warnings", [])
        payload.setdefault("degraded", False)
        return payload

    # ---------------------------------------------------------------- ① 状态条

    def status(self) -> Dict[str, Any]:
        return self._guard(BLOCK_STATUS, self._status, _status_fallback)

    def _status(self) -> Dict[str, Any]:
        now = self._now()
        warnings: List[str] = []
        credentials = self._credential_status()
        store = self.store()
        if store is None:
            # 只有真的拿不到 DB 才说「数据库不可用」。
            payload = _status_fallback(f"数据库不可用: {self.store_error or 'unknown'}",
                                       as_of=now)
            payload["credentials"] = credentials
            return payload

        equity = None
        equity_source = "unavailable"
        if self.sources.equity is not None:
            try:
                equity = _finite(self.sources.equity())
                equity_source = "provider" if equity is not None else "unavailable"
            except Exception as exc:                          # noqa: BLE001
                warnings.append(f"权益 provider 失败: {type(exc).__name__}: {exc}")
        if equity is None:
            last = store.last_equity()
            equity = _finite(_row_get(last, "equity"))
            equity_source = "store" if equity is not None else "unavailable"
        if equity is None:
            warnings.append("权益不可用（provider 与 DB 都没有可用的点）")

        open_positions = store.open_positions()
        open_count = len(open_positions)

        kill = kill_state(store)
        if not kill["known"]:
            warnings.append("kill 状态读不出来（当作「不知道」，不是「关着」）")

        beat = store.events(kind=HEARTBEAT_KIND, limit=1)
        last_tick_ts = _finite(_row_get(beat[0], "ts")) if beat else None
        last_tick_age = None if last_tick_ts is None else max(0.0, now - last_tick_ts)
        if last_tick_ts is None:
            warnings.append("还没有任何 tick 心跳 —— 调度器可能没起来")
        elif last_tick_age is not None and last_tick_age > self.heartbeat_max_age_s:
            warnings.append(f"上次 tick {_dur(last_tick_age)}（超过 "
                            f"{self.heartbeat_max_age_s / 3600:.0f}h 阈值）")

        verdict = self._dd_verdict(store, now, warnings)
        dd_payload = {
            "index": verdict.get("index"), "peak": verdict.get("peak"),
            "drawdown": verdict.get("drawdown"), "action": verdict.get("action"),
            "warn": verdict.get("warn"), "kill": verdict.get("kill"),
            "source": verdict.get("source"), "available": verdict.get("available"),
            "degraded": verdict.get("degraded"),
        }

        # 数据源新鲜度：最后一根**已收盘** bar 的收盘时刻（`app.market` 的口径），
        # 没有行情 provider 时退回「最后一个权益点」的年龄（那是 tick 活着的证据）。
        data_source = self._data_source(store, now, warnings)

        # 缺凭据 ≠ 缺数据库：句子必须分开写（缺凭据不影响读库，只影响下单/调度器）。
        if credentials["known"] and credentials["configured"] is False:
            warnings.append(str(credentials.get("detail") or
                                "交易凭据未配置（只读面板不受影响；下单 / 调度器不可用）"))

        # 等级聚合：任何一条「需要人立刻看」的理由 → alert；其余提示 → warn。
        # 顺序固定（alert 优先于 warn），避免「后面一条 warn 把前面的 alert 降级」这种
        # 只有特定数据组合才出现的静默降级。
        reasons: List[str] = []
        alert = False
        warn = False
        if dd_payload["available"] and dd_payload["action"] == dd_module.DD_ACTION_KILL:
            alert = True
            reasons.append(f"回撤熔断：{_pct(dd_payload['drawdown'])} ≥ "
                           f"{_pct(dd_payload['kill'], 0)}（只挡新开仓，离场/止损不受影响）")
        elif dd_payload["available"] and dd_payload["action"] == dd_module.DD_ACTION_WARN:
            warn = True
            reasons.append(f"回撤告警：{_pct(dd_payload['drawdown'])} ≥ "
                           f"{_pct(dd_payload['warn'], 0)}")
        if not dd_payload["available"]:
            alert = True
            reasons.append("回撤口径不可用（fail-closed：开仓被拒）")
        if kill["kill"]:
            warn = True
            reasons.append(f"kill 已开：停开新仓（{kill.get('reason') or '无理由'}）")
        if not data_source["ok"]:
            alert = True
            reasons.append(f"数据源异常：{data_source['detail']}")
        if warnings:
            warn = True
        level = "alert" if alert else ("warn" if warn else "ok")
        text = {"ok": "正常", "warn": "降级", "alert": "告警"}[level]

        return {
            "as_of": now, "mode": self.mode,
            "ok": level == "ok", "level": level, "state": text,
            "reasons": reasons,
            "equity": _zero(equity) if equity is not None else 0.0,
            "equity_known": equity is not None,
            "equity_source": equity_source,
            "open_count": open_count,
            "max_positions": int(self.limits.max_positions),
            "kill": bool(kill["kill"]), "kill_known": bool(kill["known"]),
            "kill_reason": kill.get("reason") or "",
            "kill_by": kill.get("by") or "",
            "last_tick_ts": last_tick_ts,
            "last_tick_age_s": last_tick_age,
            "last_tick_text": _dur(last_tick_age) if last_tick_age is not None else "从未 tick",
            "data_source": data_source,
            "credentials": credentials,
            "dd": dd_payload,
            "warnings": warnings,
            "degraded": bool(warnings),
        }

    def _dd_verdict(self, store: Any, now: float,
                    warnings: List[str]) -> Dict[str, Any]:
        """复用 `app.dd.DrawdownGuard` 的判定（面板不另算一套回撤）。"""
        try:
            guard = dd_module.DrawdownGuard(store, limits=self.limits, clock=self.clock)
            return guard.verdict(now).as_dict()
        except Exception as exc:                              # noqa: BLE001
            warnings.append(f"回撤读数失败: {type(exc).__name__}: {exc}")
            return {"index": None, "peak": None, "drawdown": None,
                    "action": dd_module.DD_ACTION_KILL, "warn": self.limits.dd_warn,
                    "kill": self.limits.dd_kill, "source": "unavailable",
                    "available": False, "degraded": True}

    def _data_source(self, store: Any, now: float,
                     warnings: List[str]) -> Dict[str, Any]:
        best: Optional[float] = None                  # 最后一根已收盘 bar 的收盘时刻
        name = "bar"
        errors: List[str] = []
        # 有 `cached_bars` 就只用缓存：**状态条不许为了一个时间戳去发网络请求**
        # （实测：冷启动后第一次 /status 因为拉 K 线要 24s）。
        probe = self.sources.cached_bars if self.sources.cached_bars is not None \
            else self.sources.bars
        cache_only = self.sources.cached_bars is not None
        rows: Dict[str, List[Any]] = {}
        if probe is not None:
            for symbol in self.symbols:
                try:
                    rows[symbol] = list(probe(symbol) or [])
                except Exception as exc:                  # noqa: BLE001
                    rows[symbol] = []
                    errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            if cache_only and any(not bars for bars in rows.values()):
                rows = {}                                  # 缓存没铺满 → 走权益点口径
                errors = []
        if rows:
            for symbol in self.symbols:
                bars = rows.get(symbol) or []
                if not bars:
                    errors.append(f"{symbol}: 没有 K 线")
                    continue
                close_ms = _finite(_row_get(bars[-1], "open_ms")) or 0.0
                stamp = (close_ms + BAR_MS) / 1000.0
                best = stamp if best is None else min(best, stamp)
        else:
            last = store.last_equity()
            stamp = _finite(_row_get(last, "ts"))
            name = "equity_curve"
            best = stamp
            if stamp is None:
                errors.append("没有权益点")
        age = None if best is None else max(0.0, now - best)
        ok = age is not None and age <= self.stale_after_s
        detail = "ok"
        if age is None:
            detail = "; ".join(errors) or "没有可用数据源"
        elif not ok:
            detail = f"最后一根已收盘 bar 是 {_dur(age)}（阈值 {self.stale_after_s / 3600:.0f}h）"
        elif errors:
            detail = "部分标的取数失败: " + "; ".join(errors)
            warnings.append(detail)
        return {"ok": bool(ok and not errors), "name": name, "age_s": age,
                "last_ts": best, "detail": detail}

    # ---------------------------------------------------------------- ② 仓位表

    def positions(self) -> Dict[str, Any]:
        return self._guard(BLOCK_POSITIONS, self._positions, _positions_fallback)

    def _positions(self) -> Dict[str, Any]:
        store = self.store()
        if store is None:
            return _positions_fallback(f"数据库不可用: {self.store_error or 'unknown'}",
                                       symbols=self.symbols)
        now = self._now()
        warnings: List[str] = []
        prices: Dict[str, float] = {}
        if self.sources.prices is not None:
            try:
                raw = self.sources.prices() or {}
                prices = {str(k).upper(): _zero(v) for k, v in dict(raw).items()}
            except Exception as exc:                          # noqa: BLE001
                warnings.append(f"现价 provider 失败: {type(exc).__name__}: {exc}")
        try:
            kill = kill_state(store)
            verdict = self._dd_verdict(store, now, warnings)
        except Exception as exc:                              # noqa: BLE001
            warnings.append(f"风控状态读取失败: {type(exc).__name__}: {exc}")
            kill, verdict = {"kill": False}, {"action": None}
        entry_blocked = bool(kill.get("kill")) or (verdict.get("action") == dd_module.DD_ACTION_KILL)

        rows = list(store.open_positions())
        by_symbol: Dict[str, Mapping[str, Any]] = {}
        for row in rows:
            symbol = str(_row_get(row, "symbol") or "").upper()
            if symbol:
                by_symbol[symbol] = row

        out: List[Dict[str, Any]] = []
        symbols = list(self.symbols) + [s for s in by_symbol if s not in self.symbols]
        for symbol in symbols:
            row = by_symbol.get(symbol)
            if row is None:
                out.append({
                    "symbol": symbol, "side": 0, "side_text": "—",
                    "state": "IDLE", "status": "无仓位" if entry_blocked else "等待开仓",
                    "entry_price": 0.0, "price": _zero(prices.get(symbol)),
                    "price_source": "provider" if symbol in prices else "unavailable",
                    "upnl": 0.0, "upnl_pct": 0.0, "qty": 0.0,
                    "stop_price": 0.0, "dist_to_stop_pct": None,
                    "notional": 0.0, "margin": 0.0, "liq_price": None,
                    "opened_at": None, "holding_s": None, "holding_text": "—",
                    "position_id": None, "warnings": [],
                })
                continue
            out.append(self._position_row(row, symbol, prices, now))

        if not prices:
            warnings.append("没有现价 —— 浮盈/名义/强平按开仓价估算（标记为估算）")
        return {"as_of": now, "positions": out,
                "open_count": sum(1 for r in out if r["position_id"]),
                "entry_blocked": entry_blocked, "symbols": symbols,
                "warnings": warnings, "degraded": bool(warnings)}

    def _position_row(self, row: Any, symbol: str, prices: Mapping[str, float],
                      now: float) -> Dict[str, Any]:
        side = int(_row_get(row, "side") or 0)
        entry = _zero(_row_get(row, "entry_price"))
        qty = _zero(_row_get(row, "qty"))
        stop = _finite(_row_get(row, "stop_price"))
        state = str(_row_get(row, "state") or "")
        price = _finite(prices.get(symbol))
        price_source = "provider" if price is not None else "entry_price"
        if price is None:
            price = entry
        # 名义按**当前价**算（面板看的是现在的敞口）；没有现价才退回记账时的名义。
        notional = abs(price * qty) if qty else _zero(_row_get(row, "notional"))
        upnl = (price - entry) * qty * side if entry > 0 else 0.0
        upnl_pct = ((price - entry) / entry * side) if entry > 0 else 0.0
        dist = None
        if stop is not None and stop > 0 and price > 0:
            dist = (price - stop) / price * side        # 正数 = 现价离止损还有多远
        liq = None
        if entry > 0 and side:
            liq = entry * (1 - side / LEVERAGE)          # 逐仓 5x 近似（不含维持保证金）
        opened = _finite(_row_get(row, "opened_at"))
        holding = None if opened is None else max(0.0, now - opened)
        return {
            "symbol": symbol, "side": side,
            "side_text": {1: "多", -1: "空"}.get(side, "—"),
            "state": state,
            "status": {"OPEN": "持仓中", "CLOSING": "离场中"}.get(state, "持仓中"),
            "entry_price": entry, "price": price, "price_source": price_source,
            "upnl": upnl, "upnl_pct": upnl_pct, "qty": qty,
            "stop_price": 0.0 if stop is None else stop,
            "dist_to_stop_pct": dist,
            "notional": notional, "margin": notional / LEVERAGE if LEVERAGE else 0.0,
            "liq_price": liq,
            "opened_at": opened, "holding_s": holding, "holding_text": _dur(holding),
            "position_id": _row_get(row, "id"),
            "warnings": [] if stop is not None else ["止损价缺失（仓位裸奔）"],
        }

    # ---------------------------------------------------------------- ③ 止损覆盖

    def stops(self) -> Dict[str, Any]:
        return self._guard(BLOCK_STOPS, self._stops, _stops_fallback)

    def _stops(self) -> Dict[str, Any]:
        store = self.store()
        if store is None:
            return _stops_fallback(f"数据库不可用: {self.store_error or 'unknown'}")
        now = self._now()
        warnings: List[str] = []
        orders: List[Dict[str, Any]] = []
        orders_known = self.sources.orders is not None
        if orders_known:
            try:
                raw = self.sources.orders() or []
                for item in raw:
                    orders.append({
                        "symbol": str(_row_get(item, "symbol") or "").upper(),
                        "price": _finite(_row_get(item, "price")),
                        "qty": _finite(_row_get(item, "qty")),
                        "order_id": _row_get(item, "order_id"),
                        "reduce_only": _row_get(item, "reduce_only"),
                        # ↓ 生产形态（`ExchangeOrder.as_dict()`）：没有 `price`/`qty`，
                        #   只有 int 形态 + 触发价。少了这几个键，止损单永远比不上 ⇒ 假警报。
                        "client_order_index": _int_or_none(_row_get(item, "client_order_index")),
                        "market_id": _int_or_none(_row_get(item, "market_id")),
                        "trigger_price_int": _int_or_none(_row_get(item, "trigger_price_int")),
                        "trigger_price_raw": _row_get(item, "trigger_price_raw"),
                        "qty_int": _int_or_none(_row_get(item, "qty_int")),
                        # 刚开仓那一轮由 tick 并进来的「交易所已受理」单：还没被真读数复核过
                        "pending_confirm": bool(_row_get(item, "pending_confirm")),
                    })
            except Exception as exc:                          # noqa: BLE001
                warnings.append(f"挂单 provider 失败: {type(exc).__name__}: {exc}")
                orders_known = False
        else:
            warnings.append("没有挂单数据源 —— 交易所侧状态记「未知」（不显示「在」）")

        rows = list(store.open_positions())
        out: List[Dict[str, Any]] = []
        for row in rows:
            symbol = str(_row_get(row, "symbol") or "").upper()
            stop = _finite(_row_get(row, "stop_price"))
            qty = _zero(_row_get(row, "qty"))
            decimals = int(_row_get(row, "price_decimals") or 0)
            size_decimals = int(_row_get(row, "size_decimals") or 0)
            tol = max(1e-9, 1.5 * (10.0 ** -decimals)) if decimals >= 0 else 1e-9
            stop_coi = _stop_coi_of(row)
            stop_int = _int_or_none(_row_get(row, "stop_price_int"))
            if stop_int is None and stop is not None:
                stop_int = int(round(stop * (10.0 ** decimals)))
            # 判据顺序：coi（唯一键）→ 触发价 ±1 tick（老仓位没写 coi 时）→ 价格字段（旧形态/测试注入）。
            match = None
            for order in orders:
                if order["symbol"] and order["symbol"] != symbol:
                    continue
                if stop_coi is not None and order["client_order_index"] == stop_coi:
                    match = order
                    break
                if stop_int is not None:
                    ticks = _order_trigger_tick(order, decimals)
                    if ticks is not None and abs(ticks - int(stop_int)) <= 1:
                        match = order
                        break
                price = order["price"]
                if stop is not None and price is not None and abs(price - stop) <= tol:
                    match = order
                    break
            order_qty = None
            expiry_ts = _finite(_row_get(row, "stop_expiry_ts"))
            days_left = None if expiry_ts is None else round((expiry_ts - now) / 86400.0, 1)
            if stop is None:
                status, protected = "missing", False
                note = "止损价缺失（仓位裸奔）"
            elif not orders_known:
                status, protected = "unknown", False
                note = "交易所侧状态未知（无挂单数据源）"
            elif match is None:
                status, protected = "missing", False
                note = f"交易所**没有**这张止损单（应在 {stop:g}）"
            else:
                order_qty = match["qty"]
                if order_qty is None and match["qty_int"] is not None and size_decimals >= 0:
                    order_qty = match["qty_int"] / (10.0 ** size_decimals)
                covered = order_qty is None or order_qty + 1e-12 >= qty
                if match["reduce_only"] is False:
                    status, protected = "mismatch", False
                    note = "止损单不是 reduce-only（可能反向开仓）"
                elif not covered:
                    status, protected = "partial", False
                    note = f"挂单数量 {order_qty:g} < 仓位 {qty:g}"
                else:
                    status, protected = "present", True
                    note = ("已下单（交易所已受理，下一轮读数复核）" if match["pending_confirm"]
                            else "已覆盖")
                    # 交易所到期会自动废单（我们下 28 天）：临期要说出来，别让人以为永久有效。
                    if days_left is not None and days_left <= 7:
                        note += (f" · 止损将在 {days_left:g} 天后到期（系统会自动换单）"
                                 if days_left > 0 else " · 止损已过到期时刻（等下一轮补挂）")
            out.append({
                "symbol": symbol, "position_id": _row_get(row, "id"),
                "side": int(_row_get(row, "side") or 0),
                "stop_price": 0.0 if stop is None else stop,
                "qty": qty,
                "order_qty": None if match is None else order_qty,
                "order_id": _row_get(row, "stop_order_id") or (match or {}).get("order_id"),
                "exchange_status": status, "protected": protected,
                "checked_at": now, "note": note,
                "expires_at": expiry_ts, "days_left": days_left,
            })
        uncovered = [r["symbol"] for r in out if not r["protected"]]
        if uncovered:
            warnings.append("止损未确认覆盖: " + ", ".join(uncovered))
        return {"as_of": now, "stops": out, "orders_known": orders_known,
                "uncovered": uncovered, "all_protected": not uncovered,
                "warnings": warnings, "degraded": bool(warnings)}

    # ---------------------------------------------------------------- ④ 盈亏曲线

    def pnl(self) -> Dict[str, Any]:
        return self._guard(BLOCK_PNL, self._pnl, _pnl_fallback)

    def _pnl(self) -> Dict[str, Any]:
        store = self.store()
        if store is None:
            return _pnl_fallback(f"数据库不可用: {self.store_error or 'unknown'}")
        now = self._now()
        warnings: List[str] = []
        points = store.equity_curve(limit=max(self.max_curve_points, 2))
        state = store.dd_state() or {}
        curve = pnl_curve(points,
                          anchor_ts=_finite(_row_get(state, "anchor_ts")),
                          anchor_equity=_finite(_row_get(state, "anchor_equity")))
        warnings.extend(curve.get("warnings") or [])
        # 峰值只升不降：库里的 twr_peak 是**证据**，重放窗口被截时不许比它低。
        stored_peak = _finite(_row_get(state, "twr_peak"))
        peak = max(float(curve["twr_peak"]), stored_peak or 0.0)
        drawdown = risk_drawdown(curve["twr_index"], peak)
        return {
            "as_of": now,
            "points": curve["points"],
            "cum_pnl": curve["cum_pnl"],
            "twr_index": curve["twr_index"], "twr_pct": curve["twr_pct"],
            "twr_peak": curve["twr_peak"],
            "peak": peak, "peak_pct": peak * TWR_DISPLAY_BASE,
            "drawdown_pct": drawdown,
            "anchor_ts": curve["anchor_ts"], "anchor_equity": curve["anchor_equity"],
            "steps": curve["steps"], "skipped": curve["skipped"],
            "consistency": curve["consistency"],
            "units": {"cum_pnl": "U", "twr_pct": "%"},
            "warnings": warnings, "degraded": bool(warnings),
        }

    # ---------------------------------------------------------------- ⑤ 三标的动态

    def symbols_block(self) -> Dict[str, Any]:
        return self._guard(BLOCK_SYMBOLS, self._symbols, _symbols_fallback)

    def _indicators_for(self, symbol: str, bars: Sequence[Any]) -> Tuple[Any, Any]:
        """`compute_indicators` + `squeeze_flags` 的按代记忆化（见 `__init__` 注释）。"""
        gen = (int(_row_get(bars[-1], "open_ms") or 0), len(bars))
        if gen != self._symbol_ind_gen:
            self._symbol_ind_gen = gen
            self._symbol_ind_cache.clear()                # bar 换代 → 整体失效
        cached = self._symbol_ind_cache.get(symbol)
        if cached is None:
            ind = strategy.compute_indicators(bars)
            cached = (ind, strategy.squeeze_flags(ind["band"]))
            self._symbol_ind_cache[symbol] = cached
        return cached

    #: `Panel.symbols` 是标的元组（属性名），所以区块方法叫 `symbols_block()`
    #: 以免撞名；`GET /symbols` 调它。
    def _symbols(self) -> Dict[str, Any]:
        now = self._now()
        warnings: List[str] = []
        if self.sources.bars is None:
            return _symbols_fallback("没有 K 线数据源（app.market 未接入）",
                                     symbols=self.symbols, as_of=now)
        prices: Dict[str, float] = {}
        if self.sources.prices is not None:
            try:
                prices = {str(k).upper(): _zero(v) for k, v in
                          dict(self.sources.prices() or {}).items()}
            except Exception as exc:                          # noqa: BLE001
                warnings.append(f"现价 provider 失败: {type(exc).__name__}: {exc}")

        out: List[Dict[str, Any]] = []
        held: Dict[str, Mapping[str, Any]] = {}
        try:
            store = self.store()
            if store is not None:
                for prow in store.open_positions():
                    held[str(_row_get(prow, "symbol") or "").upper()] = prow
        except Exception as exc:                              # noqa: BLE001 - 读不到就当没持仓
            warnings.append(f"持仓读取失败: {type(exc).__name__}: {exc}")
        for symbol in self.symbols:
            try:
                out.append(self._symbol_row(symbol, prices, now, held=held.get(symbol)))
            except Exception as exc:                          # noqa: BLE001 - 单标的不崩全表
                warnings.append(f"{symbol}: {type(exc).__name__}: {exc}")
                out.append(_symbol_empty(symbol, reason=f"计算失败: {exc}"))
        if any(row.get("warnings") for row in out):
            for row in out:
                warnings.extend(f"{row['symbol']}: {w}" for w in row.get("warnings") or [])
        return {"as_of": now, "symbols": out,
                "warnings": warnings, "degraded": bool(warnings)}

    def _symbol_row(self, symbol: str, prices: Mapping[str, float],
                    now: float, held: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        bars = list(self.sources.bars(symbol) or [])          # type: ignore[misc]
        if not bars:
            return _symbol_empty(symbol, reason="没有 K 线")
        ind, squeeze = self._indicators_for(symbol, bars)
        i = len(bars) - 1
        price = _finite(prices.get(symbol))
        if price is None:
            price = _finite(_row_get(bars[i], "close"))
        why = strategy.explain_entry(symbol, bars, ind, squeeze, i,
                                     entry_price=price if price else None,
                                     allow_long=(1 in self.sides),
                                     allow_short=(-1 in self.sides))
        values = why.get("values") or {}
        band = _finite(values.get("band_prev"))
        thr = _finite(values.get("band_thr"))
        next_close_ms = int(_row_get(bars[i], "open_ms") or 0) + BAR_MS
        # ---- 距触发线（入场判据用的是 **上一根** 的均线带上下沿）与破线后止损（用 **本根**）----
        # 这几个数只是「还差多远」的预览：判定永远来自 explain_entry / evaluate_entry，
        # 这里算出来的距离不参与任何开仓决策。
        ma_max = _finite(values.get("ma_max_prev"))
        ma_min = _finite(values.get("ma_min_prev"))
        ma_max_now = _finite((ind.get("ma_max") or [None])[i])
        ma_min_now = _finite((ind.get("ma_min") or [None])[i])
        sma200 = _finite((ind.get("sma200") or [None])[i])

        def _gap(num: Optional[float], ref: Optional[float]) -> Optional[float]:
            """现价离参考线的比例（正 = 现价在参考线下方，需要上涨才碰到）。"""
            if num is None or not ref:
                return None
            return (ref - num) / num

        dist_long = _gap(price, ma_max)        # 正=还需上涨多少才破上轨
        dist_short = _gap(ma_min, price)       # 正=还需下跌多少才破下轨
        stop_long = None if not (price and ma_min_now) else (price - ma_min_now) / price
        stop_short = None if not (price and ma_max_now) else (ma_max_now - price) / price

        def stop_ok(dist: Optional[float]) -> Optional[bool]:
            if dist is None:
                return None
            return bool(strategy.STOP_MIN <= dist <= strategy.STOP_MAX)

        row: Dict[str, Any] = {
            "symbol": symbol,
            "price": price,
            "band_pct": band,
            "threshold_pct": thr,
            "gap_pct": None if band is None or thr is None else band - thr,
            "dense_bars": int(values.get("dense_cnt") or 0),
            "min_dense_bars": strategy.MIN_SQUEEZE_BARS,
            "sma20": _finite((ind.get("sma20") or [None])[i]),
            "sma200": sma200,
            "ma_max": ma_max,
            "ma_min": ma_min,
            # 距触发线：>0 表示还要朝那个方向走多少才破线；<0 表示现价已经越过去了（等收盘确认）
            "dist_long_pct": dist_long,
            "dist_short_pct": dist_short,
            "above_upper": bool(price and ma_max and price > ma_max),
            "below_lower": bool(price and ma_min and price < ma_min),
            # 做空趋势过滤（只有做空才对它提要求）
            "ma200_gap_pct": _gap(price, sma200),
            "below_ma200": bool(price and sma200 and price < sma200),
            # 破线后止损距离（按当前价与本根均线带估算，仅展示）
            "stop_long_pct": stop_long,
            "stop_short_pct": stop_short,
            "stop_long_ok": stop_ok(stop_long),
            "stop_short_ok": stop_ok(stop_short),
            # 方向开关（与执行器同源；今天两侧都开，见 Panel.sides）
            "long_enabled": 1 in self.sides,
            "short_enabled": -1 in self.sides,
            "held_side": int(held.get("side") or 0) if held else 0,
            "next_bar_close_ms": next_close_ms,
            "next_bar_close_ts": next_close_ms / 1000.0,
            "blocked_by": why.get("blocked_by"),
            "reason": why.get("reason"),
            "fired": bool(why.get("fired")),
            "side": int(why.get("side") or 0),
            "bars": len(bars),
            "as_of": now,
            "warnings": ["K 线不足，指标为 0 值"] if len(bars) < strategy.MIN_BARS_REQUIRED else [],
        }
        row["verdict"] = _symbol_verdict(row)
        return row

    # ---------------------------------------------------------------- ⑥ 事件流

    def events(self) -> Dict[str, Any]:
        return self._guard(BLOCK_EVENTS, self._events, _events_fallback)

    def _events(self) -> Dict[str, Any]:
        store = self.store()
        if store is None:
            return _events_fallback(f"数据库不可用: {self.store_error or 'unknown'}")
        now = self._now()
        rows = store.events(limit=self.max_events)
        items: List[Dict[str, Any]] = []
        for row in rows:
            kind = str(_row_get(row, "kind") or "")
            action = event_action(kind)
            data = _loads(_row_get(row, "data"))
            items.append({
                "id": _row_get(row, "id"),
                "ts": _finite(_row_get(row, "ts")),
                "kind": kind,
                "symbol": _row_get(row, "symbol"),
                "message": str(_row_get(row, "message") or ""),
                "action": action,
                "skipped": action == ACTION_SKIP,
                "alert": action in (ACTION_KILL,),
                "data": data,
            })
        skipped = [item for item in items if item["skipped"]]
        return {"as_of": now, "events": items, "count": len(items),
                "skipped_count": len(skipped), "limit": self.max_events,
                "warnings": [], "degraded": False}

    # ---------------------------------------------------------------- 记录页

    def records(self) -> Dict[str, Any]:
        """订单明细 + 资金概览（记录页）。数据全部来自库，不发任何网络请求。"""
        return self._guard(BLOCK_RECORDS, self._records, _records_fallback)

    def _records(self) -> Dict[str, Any]:
        store = self.store()
        if store is None:
            return _records_fallback(f"数据库不可用: {self.store_error or 'unknown'}")
        now = self._now()
        warnings: List[str] = []
        try:
            trades = store.trades(limit=MAX_RECORD_ROWS)
        except Exception as exc:                              # noqa: BLE001 - 单区块降级
            return _records_fallback(f"读成交失败: {type(exc).__name__}: {exc}")
        try:
            points = store.equity_curve(limit=500)
        except Exception as exc:                              # noqa: BLE001
            points = []
            warnings.append(f"读权益曲线失败: {type(exc).__name__}: {exc}")

        stamps = [(_finite(_row_get(p, "ts")) or 0.0, _finite(_row_get(p, "equity")))
                  for p in points]

        rows: List[Dict[str, Any]] = []
        fee_total = 0.0
        for trade in trades:
            ts = _finite(_row_get(trade, "ts")) or 0.0
            qty = _finite(_row_get(trade, "qty")) or 0.0
            price = _finite(_row_get(trade, "price")) or 0.0
            fee = _finite(_row_get(trade, "fee")) or 0.0
            fee_total += fee
            side = int(_row_get(trade, "side") or 0)
            kind = str(_row_get(trade, "kind") or "")
            equity_after, lag = _equity_after(stamps, ts)
            rows.append({
                "id": int(_row_get(trade, "id") or 0),
                "ts": ts,
                "symbol": str(_row_get(trade, "symbol") or ""),
                "side": side,
                "side_text": "多" if side > 0 else ("空" if side < 0 else "—"),
                "kind": kind,
                "kind_text": _TRADE_KIND_TEXT.get(kind, kind or "—"),
                "qty": qty, "price": price, "notional": qty * price,
                "fee": fee, "fee_token": str(_row_get(trade, "fee_token") or ""),
                "realized_pnl": _finite(_row_get(trade, "realized_pnl")),
                "equity_after": equity_after, "equity_after_lag_s": lag,
                "position_id": _row_get(trade, "position_id"),
            })

        last = points[-1] if points else None
        first = points[0] if points else None
        state = {}
        try:
            state = store.dd_state() or {}
        except Exception as exc:                              # noqa: BLE001
            warnings.append(f"读回撤状态失败: {type(exc).__name__}: {exc}")
        # 入金没被流水覆盖的权益跳变（审计 P2-3）：`infer_transfer_flow` 在区间内有
        # 持仓/成交时保守放弃，那笔出入金会静默混进 cum_pnl —— 这里显眼地提示出来。
        try:
            all_ts = [t for t in (_finite(_row_get(r, "ts"))
                                  for r in store.trades(limit=5000)) if t]
            flow_ts = [t for t in (_finite(_row_get(r, "ts"))
                                   for r in store.flows()) if t]
            jumps = _unexplained_equity_jumps(points, all_ts, flow_ts)
        except Exception as exc:                              # noqa: BLE001 - 提示失败不影响区块
            jumps = []
            warnings.append(f"权益跳变检测失败: {type(exc).__name__}: {exc}")
        for jump in jumps:
            stamp = time.strftime("%m-%d %H:%M", time.gmtime(jump["ts"]))
            sign = "+" if jump["delta"] > 0 else ""
            warnings.append(
                f"权益在 {stamp} UTC 跳变 {sign}{jump['delta']:.2f} U 且区间内无成交、"
                "无流水覆盖 —— 如有出入金请用 scripts/backfill_flow.py 补录"
                "（有持仓时的价格波动也会触发本提示，可忽略）")
        funds = {
            "equity": _finite(_row_get(last, "equity")),
            "equity_ts": _finite(_row_get(last, "ts")),
            "net_flow": _finite(_row_get(last, "cum_flow")),
            "twr_index": _finite(_row_get(state, "twr_index")),
            "twr_peak": _finite(_row_get(state, "twr_peak")),
            "anchor_ts": _finite(_row_get(first, "ts")),
            "anchor_equity": _finite(_row_get(first, "equity")),
            "trade_count": int(store.trade_count() or 0),
            "fee_total": fee_total,
            "realized_total": float(store.realized_pnl() or 0.0),
            "unexplained_jumps": jumps,
            "shown": len(rows),
            "limit": MAX_RECORD_ROWS,
        }
        return {"as_of": now, "funds": funds, "trades": rows,
                "count": len(rows), "warnings": warnings,
                "degraded": bool(warnings), "block": BLOCK_RECORDS}

    def records_page(self) -> str:
        """记录页：资金概览 + 订单明细 + 执行器事件流（首页不再渲染事件流）。"""
        return render_records_html(self.status(), self.records(), self.events(),
                                   mode=self.mode, title="lighter-ma-bot 记录")

    # ---------------------------------------------------------------- 汇总 / HTML

    def snapshot(self) -> Dict[str, Any]:
        """6 区块一次取全（HTML 页与 `/` 用它；JSON 端点各取一块）。"""
        blocks = {
            BLOCK_STATUS: self.status(),
            BLOCK_POSITIONS: self.positions(),
            BLOCK_STOPS: self.stops(),
            BLOCK_PNL: self.pnl(),
            BLOCK_SYMBOLS: self.symbols_block(),
            BLOCK_EVENTS: self.events(),
        }
        warnings: List[str] = []
        for name, payload in blocks.items():
            for warning in payload.get("warnings") or []:
                warnings.append(f"[{name}] {warning}")
        status = blocks[BLOCK_STATUS]
        return {"as_of": self._now(), "mode": self.mode,
                "level": status.get("level", "warn"),
                "state": status.get("state", "降级"),
                "reasons": status.get("reasons") or [],
                "blocks": blocks, "block_order": list(BLOCKS),
                "warnings": warnings,
                "degraded": any(p.get("degraded") for p in blocks.values())}

    def page(self) -> str:
        """自包含 HTML（无 CDN、无外部 JS/CSS、曲线是内联 SVG）。"""
        return render_html(self.snapshot(), symbols=self.symbols, mode=self.mode)


# ------------------------------------------------------------------ 降级载荷
#
# 每个都返回「0 值 + WARNING」的形状，字段与正常载荷**完全一致** —— 否则前端的
# 取值逻辑会在降级时炸掉，而「降级路径没被渲染过」正是这类 bug 的温床。


def _status_fallback(warning: str, *, as_of: Optional[float] = None) -> Dict[str, Any]:
    return {
        "as_of": _zero(as_of), "mode": "unknown", "ok": False, "level": "alert",
        "state": "告警", "reasons": [warning], "equity": 0.0, "equity_known": False,
        "equity_source": "unavailable", "open_count": 0, "max_positions": 0,
        "kill": False, "kill_known": False, "kill_reason": "", "kill_by": "",
        "last_tick_ts": None, "last_tick_age_s": None, "last_tick_text": "未知",
        "data_source": {"ok": False, "name": "unavailable", "age_s": None,
                        "last_ts": None, "detail": warning},
        "credentials": {"known": False, "configured": None, "missing": [], "detail": ""},
        "dd": {"index": None, "peak": None, "drawdown": None,
               "action": dd_module.DD_ACTION_KILL, "warn": None, "kill": None,
               "source": "unavailable", "available": False, "degraded": True},
        "warnings": [warning], "degraded": True,
    }


def _positions_fallback(warning: str, *,
                        symbols: Sequence[str] = DEFAULT_SYMBOLS) -> Dict[str, Any]:
    rows = [{"symbol": s, "side": 0, "side_text": "—", "state": "IDLE",
             "status": "无仓位", "entry_price": 0.0, "price": 0.0,
             "price_source": "unavailable", "upnl": 0.0, "upnl_pct": 0.0, "qty": 0.0,
             "stop_price": 0.0, "dist_to_stop_pct": None, "notional": 0.0,
             "margin": 0.0, "liq_price": None, "opened_at": None, "holding_s": None,
             "holding_text": "—", "position_id": None, "warnings": []}
            for s in symbols]
    return {"as_of": 0.0, "positions": rows, "open_count": 0, "entry_blocked": True,
            "symbols": list(symbols), "warnings": [warning], "degraded": True}


def _stops_fallback(warning: str) -> Dict[str, Any]:
    return {"as_of": 0.0, "stops": [], "orders_known": False, "uncovered": [],
            "all_protected": False, "warnings": [warning], "degraded": True}


def _pnl_fallback(warning: str) -> Dict[str, Any]:
    return {"as_of": 0.0, "points": [], "cum_pnl": 0.0, "twr_index": TWR_BASE,
            "twr_pct": TWR_DISPLAY_BASE, "twr_peak": TWR_BASE,
            "peak": TWR_BASE, "peak_pct": TWR_DISPLAY_BASE, "drawdown_pct": 0.0,
            "anchor_ts": None, "anchor_equity": None, "steps": 0, "skipped": 0,
            "consistency": {"match": True, "index_delta": 0.0, "peak_delta": 0.0},
            "units": {"cum_pnl": "U", "twr_pct": "%"},
            "warnings": [warning], "degraded": True}


def _symbols_fallback(warning: str, *, symbols: Sequence[str] = DEFAULT_SYMBOLS,
                      as_of: float = 0.0) -> Dict[str, Any]:
    return {"as_of": as_of,
            "symbols": [_symbol_empty(s, reason=warning) for s in symbols],
            "warnings": [warning], "degraded": True}


def _symbol_verdict(row: Mapping[str, Any]) -> str:
    """三标的动态的「结论」列：**只说还差什么，不复述任何数字**。

    数字都在前面的列里（密集 3/6✔、距上/下轨 ±x%、MA200、止损距离），这里再念一遍就是噪音。
    优先级 = 引擎自己的六步顺序（根数 → 密集 → 破线 → MA200 → 止损），所以「第一个卡点」
    与实盘拒单原因永远一致；`fired` 直接来自 evaluate_entry，不是这里推的。
    """
    if int(row.get("held_side") or 0):
        return "持仓中"
    long_on = bool(row.get("long_enabled", True))
    short_on = bool(row.get("short_enabled", True))
    if not long_on and not short_on:
        return "多空双向都已关闭"
    blocked = str(row.get("blocked_by") or "")
    if row.get("fired"):
        side = int(row.get("side") or 0)
        return "可开多" if side == 1 else ("可开空" if side == -1 else "可开仓")
    if blocked == "no_data":
        return "无数据"
    if blocked == "bars":
        return "K 线不足"
    if blocked == "squeeze":
        gap = _finite(row.get("gap_pct"))
        dense = int(row.get("dense_bars") or 0)
        need = int(row.get("min_dense_bars") or 0)
        if gap is not None and gap > 0:
            return "等密集（带宽未进门槛）"
        if dense < need:
            return "等密集根数"
        return "等密集"
    if blocked == "breakout":
        # 说方向而不是说数字：哪一边更近、且那一边的前置条件（方向开关/MA200/止损）成立。
        long_ok = long_on and row.get("stop_long_ok") is not False
        short_ok = (short_on and row.get("stop_short_ok") is not False
                    and bool(row.get("below_ma200")))
        if long_ok and short_ok:
            dl = _finite(row.get("dist_long_pct"))
            ds = _finite(row.get("dist_short_pct"))
            if dl is not None and ds is not None:
                return "等破上轨" if abs(dl) <= abs(ds) else "等破下轨"
            return "等破线"
        if long_ok:
            return "等破上轨"
        if short_ok:
            return "等破下轨"
        return "等破线（另一侧前置条件未满足）"
    if blocked == "regime":
        return "空单需先跌破 MA200"
    if blocked == "stop":
        return "止损距离不合格"
    if blocked == "unknown":
        return "条件未明"
    return "等条件"


def _symbol_empty(symbol: str, *, reason: str = "") -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "symbol": symbol, "price": 0.0, "band_pct": None, "threshold_pct": None,
        "gap_pct": None, "dense_bars": 0, "min_dense_bars": strategy.MIN_SQUEEZE_BARS,
        "sma20": None, "sma200": None, "ma_max": None, "ma_min": None,
        "dist_long_pct": None, "dist_short_pct": None,
        "above_upper": False, "below_lower": False,
        "ma200_gap_pct": None, "below_ma200": False,
        "stop_long_pct": None, "stop_short_pct": None,
        "stop_long_ok": None, "stop_short_ok": None,
        "long_enabled": True, "short_enabled": True, "held_side": 0,
        "next_bar_close_ms": None, "next_bar_close_ts": None,
        "blocked_by": "no_data", "reason": reason or "无数据", "fired": False,
        "side": 0, "bars": 0, "as_of": 0.0,
        "warnings": [reason or "无数据"],
    }
    row["verdict"] = _symbol_verdict(row)
    return row


def _events_fallback(warning: str) -> Dict[str, Any]:
    return {"as_of": 0.0, "events": [], "count": 0, "skipped_count": 0,
            "limit": MAX_EVENTS, "warnings": [warning], "degraded": True}


# ------------------------------------------------------------------ HTML


_CSS = """
/* 明暗跟随系统：不设按钮、不存偏好 —— 浏览器的 prefers-color-scheme 就是唯一开关。
   所有颜色走 CSS 变量；内联 SVG 图表用同一批变量（SVG 在文档里，能读到 var()）。 */
:root {
  color-scheme: dark light;
  --bg: #12151a; --panel: #1a1f27; --panel-2: #232a34;
  --border: #262d38; --row: #232a34;
  --text: #e6e8eb; --muted: #9aa4b2; --dim: #6b7684; --link: #6ea8fe;
  --ok: #38c172; --warn: #e3a008; --bad: #e3342f;
  --ok-bg: #16241d; --ok-bd: #2f6f4f;
  --warn-bg: #241f14; --warn-bd: #7a6320; --warn-fg: #e3c169;
  --bad-bg: #2a1616; --bad-bd: #8a2f2f; --bad-fg: #ffb4b4;
  --btn-hover-bd: #3a4556; --chart-bg: #151a21; --chart-grid: #2b333f;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f5f7fa; --panel: #ffffff; --panel-2: #eef1f5;
    --border: #d9dee6; --row: #e9edf2;
    --text: #161a1f; --muted: #5a6673; --dim: #8b95a3; --link: #1f6feb;
    --ok: #17864f; --warn: #9a6a05; --bad: #c0392b;
    --ok-bg: #e9f6ee; --ok-bd: #a5d6b8;
    --warn-bg: #fdf5e2; --warn-bd: #e2cb92; --warn-fg: #7a5a0d;
    --bad-bg: #fdecea; --bad-bd: #e6b0a8; --bad-fg: #8c1d18;
    --btn-hover-bd: #b7c0cc; --chart-bg: #ffffff; --chart-grid: #dde3ea;
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 12px; background: var(--bg); color: var(--text);
       font: 13px/1.45 -apple-system, "Segoe UI", "Noto Sans SC", sans-serif; }
h1 { font-size: 15px; margin: 0 0 6px; font-weight: 600; letter-spacing: .02em; }
h2 { font-size: 13px; margin: 0 0 8px; font-weight: 600; color: var(--muted);
     text-transform: uppercase; letter-spacing: .06em; }
a { color: var(--link); }
.wrap { max-width: 1080px; margin: 0 auto; }
.bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
       padding: 10px 12px; border-radius: 8px; background: var(--panel);
       border: 1px solid var(--border); margin-bottom: 12px; }
.bar.ok { border-color: var(--ok-bd); background: var(--ok-bg); }
.bar.warn { border-color: var(--warn-bd); background: var(--warn-bg); }
.bar.alert { border-color: var(--bad-bd); background: var(--bad-bg); }
.dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.dot.ok { background: var(--ok); } .dot.warn { background: var(--warn); }
.dot.alert { background: var(--bad); }
.kv { color: var(--muted); } .kv b { color: var(--text); font-weight: 600; }
.reasons { margin: 0 0 12px; padding: 8px 12px; border-radius: 8px;
           background: var(--bad-bg); border: 1px solid var(--bad-bd); color: var(--bad-fg); }
section { background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
          padding: 12px; margin-bottom: 12px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 5px 7px; border-bottom: 1px solid var(--row);
         white-space: nowrap; }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
th:first-child, td:first-child { text-align: left; }
tr:last-child td { border-bottom: none; }
.pos { color: var(--ok); } .neg { color: var(--bad); } .dim { color: var(--dim); }
.bad { color: var(--bad); font-weight: 600; }
.warnlist { margin: 8px 0 0; padding: 6px 10px; border-radius: 6px;
            background: var(--warn-bg); border: 1px solid var(--warn-bd); color: var(--warn-fg);
            font-size: 12px; }
.adminbar { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
.navlink { font-size: 12px; font-weight: 400; margin-left: 8px; letter-spacing: 0; }
button { padding: 6px 12px; border-radius: 6px; border: 1px solid var(--border);
         background: var(--panel-2); color: var(--text); cursor: pointer; font: inherit; }
button:hover { border-color: var(--btn-hover-bd); }
button.danger { border-color: var(--bad-bd); background: var(--bad-bg); color: var(--bad-fg); }
code { background: var(--panel-2); padding: 1px 5px; border-radius: 4px; }
.tag { display: inline-block; padding: 1px 6px; border-radius: 4px;
       background: var(--panel-2); color: var(--muted); font-size: 11px; }
.tag.skip { background: var(--warn-bg); color: var(--warn-fg); }
.tag.kill { background: var(--bad-bg); color: var(--bad-fg); }
svg { display: block; width: 100%; height: auto; }
footer { color: var(--dim); font-size: 11px; padding: 4px 2px 20px; }
/* ⑤ 三标的动态：宽屏一行网格；窄屏 = 数字行（可折行）+ 结论行（2026-10-28） */
.dyn { width: 100%; font-size: 12.5px; }
.dyn-row { display: grid; align-items: baseline; gap: 3px 10px; padding: 6px 2px;
           border-bottom: 1px solid var(--line);
           grid-template-columns: 46px 92px 104px 1.25fr 1.25fr 1fr 1.35fr 60px 1.1fr; }
.dyn-row.head { color: var(--muted); font-size: 11px; font-weight: 600; }
.dyn-row .nm { font-weight: 600; }
.dyn-row .verdict { font-weight: 600; }
.warn { color: var(--warn); }
@media (max-width: 640px) {
  .dyn-row.head { display: none; }
  .dyn-row { display: flex; flex-wrap: wrap; gap: 2px 10px; }
  .dyn-row > span { white-space: nowrap; }
  .dyn-row .next { display: none; }
  .dyn-row .verdict { flex: 1 0 100%; margin-top: 4px; padding-top: 4px;
                      border-top: 1px dashed var(--line2); }
}
@media (max-width: 640px) { body { padding: 8px; } th, td { padding: 4px 5px; } }
"""


def _money(value: Any) -> str:
    out = _finite(value)
    return "—" if out is None else f"{out:,.2f}"


def _qty_text(value: Any) -> str:
    out = _finite(value)
    return "—" if out is None else f"{out:g}"


def _cls(value: Any) -> str:
    out = _finite(value)
    if out is None or out == 0:
        return "dim"
    return "pos" if out > 0 else "neg"


def _svg_curve(points: Sequence[Mapping[str, Any]], *, width: int = 720,
               height: int = 220) -> str:
    """双口径折线（内联 SVG，无 JS 库）：实线=累计盈亏，虚线=TWR 收益指数。"""
    pts = [p for p in points or () if _finite(p.get("ts")) is not None]
    if len(pts) < 2:
        return ('<svg viewBox="0 0 %d %d" role="img" aria-label="盈亏曲线">'
                '<text x="12" y="28" fill="var(--dim)" font-size="12">'
                '数据不足（至少需要 2 个权益点）</text></svg>' % (width, height))
    pad_l, pad_r, pad_t, pad_b = 48, 52, 14, 24
    ts0 = float(pts[0]["ts"])
    ts1 = float(pts[-1]["ts"])
    span = (ts1 - ts0) or 1.0
    pnl_vals = [_zero(p.get("cum_pnl")) for p in pts]
    twr_vals = [_zero(p.get("twr_pct")) for p in pts]
    p_lo, p_hi = min(pnl_vals + [0.0]), max(pnl_vals + [0.0])
    t_lo, t_hi = min(twr_vals), max(twr_vals)
    if p_hi - p_lo < 1e-9:
        p_hi, p_lo = p_lo + 1.0, p_lo - 1.0
    if t_hi - t_lo < 1e-9:
        t_hi, t_lo = t_hi + 1.0, t_lo - 1.0
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b

    def x_of(ts: float) -> float:
        return pad_l + (float(ts) - ts0) / span * inner_w

    def y_pnl(v: float) -> float:
        return pad_t + inner_h - (v - p_lo) / (p_hi - p_lo) * inner_h

    def y_twr(v: float) -> float:
        return pad_t + inner_h - (v - t_lo) / (t_hi - t_lo) * inner_h

    solid = " ".join(f"{x_of(float(p['ts'])):.1f},{y_pnl(_zero(p.get('cum_pnl'))):.1f}"
                     for p in pts)
    dashed = " ".join(f"{x_of(float(p['ts'])):.1f},{y_twr(_zero(p.get('twr_pct'))):.1f}"
                      for p in pts)
    zero_y = y_pnl(0.0)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'role="img" aria-label="盈亏曲线：实线累计盈亏(U)，虚线 TWR 收益指数(%)">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="var(--chart-bg)"/>',
        f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width - pad_r}" y2="{zero_y:.1f}" '
        f'stroke="var(--chart-grid)" stroke-width="1" stroke-dasharray="3 3"/>',
        f'<polyline points="{solid}" fill="none" stroke="var(--ok)" stroke-width="1.8"/>',
        f'<polyline points="{dashed}" fill="none" stroke="var(--link)" stroke-width="1.6" '
        f'stroke-dasharray="5 4"/>',
        f'<text x="{pad_l - 6}" y="{pad_t + 10}" fill="var(--ok)" font-size="11" '
        f'text-anchor="end">{p_hi:+.2f}</text>',
        f'<text x="{pad_l - 6}" y="{pad_t + inner_h:.1f}" fill="var(--ok)" font-size="11" '
        f'text-anchor="end">{p_lo:+.2f}</text>',
        f'<text x="{width - pad_r + 6}" y="{pad_t + 10}" fill="var(--link)" font-size="11">'
        f'{t_hi:.2f}%</text>',
        f'<text x="{width - pad_r + 6}" y="{pad_t + inner_h:.1f}" fill="var(--link)" '
        f'font-size="11">{t_lo:.2f}%</text>',
        f'<text x="{pad_l}" y="{height - 6}" fill="var(--dim)" font-size="10">'
        f'{time.strftime("%m-%d %H:%M", time.gmtime(ts0))} UTC</text>',
        f'<text x="{width - pad_r}" y="{height - 6}" fill="var(--dim)" font-size="10" '
        f'text-anchor="end">{time.strftime("%m-%d %H:%M", time.gmtime(ts1))} UTC</text>',
        "</svg>",
    ]
    return "".join(parts)


def _warn_html(warnings: Sequence[Any]) -> str:
    items = [html.escape(str(w)) for w in warnings or ()]
    if not items:
        return ""
    return '<div class="warnlist">⚠ ' + "<br>⚠ ".join(items) + "</div>"


def _status_html(block: Mapping[str, Any], mode: str) -> str:
    level = str(block.get("level") or "warn")
    dd = block.get("dd") or {}
    credentials = block.get("credentials") or {}
    if not credentials.get("known"):
        credentials_text = "未知"
    else:
        credentials_text = "已配置" if credentials.get("configured") else "未配置"
    parts = [
        f'<section id="block-status" class="bar {html.escape(level)}">',
        f'<span class="dot {html.escape(level)}"></span>',
        f'<b>{html.escape(str(block.get("state") or "降级"))}</b>',
        f'<span class="kv">权益 <b>{_money(block.get("equity"))} U</b>'
        f' <span class="dim">({html.escape(str(block.get("equity_source")))})</span></span>',
        f'<span class="kv">持仓 <b>{int(block.get("open_count") or 0)}'
        f'/{int(block.get("max_positions") or 0)}</b></span>',
        f'<span class="kv">kill <b>{"ON" if block.get("kill") else "off"}</b></span>',
        f'<span class="kv">回撤 <b>{_pct(dd.get("drawdown"))}</b>'
        f' <span class="dim">({html.escape(str(dd.get("action") or "?"))})</span></span>',
        f'<span class="kv">模式 <b>{html.escape(mode)}</b></span>',
        f'<span class="kv">凭据 <b>{credentials_text}</b></span>',
        f'<span class="kv">上次 tick <b>{html.escape(str(block.get("last_tick_text")))}</b></span>',
        f'<span class="kv">数据源 <b>{"ok" if (block.get("data_source") or {}).get("ok") else "异常"}</b>'
        f' <span class="dim">{html.escape(str((block.get("data_source") or {}).get("name")))}</span></span>',
        "</section>",
    ]
    reasons = block.get("reasons") or []
    if reasons:
        parts.append('<div class="reasons" id="block-status-reasons">'
                     + "<br>".join(html.escape(str(r)) for r in reasons) + "</div>")
    return "".join(parts)


def _positions_html(block: Mapping[str, Any]) -> str:
    head = ("<tr><th>标的</th><th>方向</th><th>开仓价</th><th>现价</th><th>浮盈(U)</th>"
            "<th>浮盈%</th><th>数量</th><th>止损价</th><th>距止损%</th><th>名义(U)</th>"
            "<th>保证金(U)</th><th>强平≈</th><th>持仓时长</th><th>状态</th></tr>")
    rows = []
    for row in block.get("positions") or ():
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('symbol')))}</td>"
            f"<td>{html.escape(str(row.get('side_text')))}</td>"
            f"<td>{_money(row.get('entry_price')) if row.get('position_id') else '—'}</td>"
            f"<td>{_money(row.get('price'))}</td>"
            f"<td class=\"{_cls(row.get('upnl'))}\">{_money(row.get('upnl'))}</td>"
            f"<td class=\"{_cls(row.get('upnl_pct'))}\">{_pct(row.get('upnl_pct'))}</td>"
            f"<td>{_qty_text(row.get('qty')) if row.get('position_id') else '—'}</td>"
            f"<td>{_money(row.get('stop_price')) if row.get('position_id') else '—'}</td>"
            f"<td>{_pct(row.get('dist_to_stop_pct'))}</td>"
            f"<td>{_money(row.get('notional'))}</td>"
            f"<td>{_money(row.get('margin'))}</td>"
            f"<td>{_money(row.get('liq_price'))}</td>"
            f"<td>{html.escape(str(row.get('holding_text')))}</td>"
            f"<td>{html.escape(str(row.get('status')))}</td>"
            "</tr>")
    return ('<section id="block-positions"><h2>② 仓位表</h2>'
            f"<table>{head}{''.join(rows)}</table>{_warn_html(block.get('warnings'))}"
            "</section>")


def _stops_html(block: Mapping[str, Any]) -> str:
    head = ("<tr><th>标的</th><th>方向</th><th>止损价</th><th>仓位数量</th>"
            "<th>交易所挂单</th><th>挂单数量</th><th>受保护</th><th>最后校验</th><th>说明</th></tr>")
    rows = []
    for row in block.get("stops") or ():
        status = str(row.get("exchange_status"))
        label = {"present": "在", "missing": "不在", "partial": "数量不足",
                 "mismatch": "不是 reduce-only", "unknown": "未知"}.get(status, status)
        css = "pos" if row.get("protected") else "bad"
        checked = _finite(row.get("checked_at"))
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('symbol')))}</td>"
            f"<td>{ {1: '多', -1: '空'}.get(int(row.get('side') or 0), '—') }</td>"
            f"<td>{_money(row.get('stop_price'))}</td>"
            f"<td>{_qty_text(row.get('qty'))}</td>"
            f"<td class=\"{css}\">{html.escape(label)}</td>"
            f"<td>{_qty_text(row.get('order_qty'))}</td>"
            f"<td class=\"{css}\">{'是' if row.get('protected') else '否'}</td>"
            f"<td>{html.escape(time.strftime('%m-%d %H:%M', time.gmtime(checked))) if checked else '—'}</td>"
            f"<td>{html.escape(str(row.get('note')))}</td>"
            "</tr>")
    empty = "" if rows else ('<tr><td colspan="9" class="dim">没有持仓 —— 无需止损覆盖</td></tr>')
    return ('<section id="block-stops"><h2>③ 止损单覆盖（裸奔检测，常驻不可折叠）</h2>'
            f"<table>{head}{''.join(rows)}{empty}</table>"
            f"{_warn_html(block.get('warnings'))}</section>")


def _pnl_html(block: Mapping[str, Any]) -> str:
    consistency = block.get("consistency") or {}
    match = consistency.get("match")
    badge = ('<span class="tag">口径一致</span>' if match
             else '<span class="tag kill">口径漂移!</span>')
    return ('<section id="block-pnl"><h2>④ 盈亏曲线（双口径）</h2>'
            f'<div class="kv">累计盈亏（实线，已剔出入金）'
            f' <b class="{_cls(block.get("cum_pnl"))}">{_money(block.get("cum_pnl"))} U</b>'
            f' ｜ TWR 收益指数（虚线）<b>{_money(block.get("twr_pct"))}%</b>'
            f' ｜ 峰值 {_money(block.get("peak_pct"))}%'
            f' ｜ 回撤 {_pct(block.get("drawdown_pct"))} {badge}</div>'
            f'{_svg_curve(block.get("points") or [])}'
            f'{_warn_html(block.get("warnings"))}</section>')


def _symbols_html(block: Mapping[str, Any]) -> str:
    """三标的动态：一行数字 + 一行结论（窄屏两行，宽屏合并成一行网格）。

    设计取舍（2026-10-28 用户拍板）：
    - 回答「还差什么才开单」：距上轨/距下轨（含触发价）、MA200 上下、破线后止损距离；
    - 不再重复：带宽/门槛/距门槛 三列合并进「密集」一格（数值进 title），结论列只说差什么；
    - SMA20（离场线）从这张表移走，持仓时显示在结论里。
    """
    head = ("<div class='dyn-row head'><span>标的</span><span>当前价</span><span>密集</span>"
            "<span>距上轨（开多）</span><span>距下轨（开空）</span><span>MA200（做空）</span>"
            "<span>破线后止损</span><span>收盘(UTC)</span><span>结论</span></div>")

    def _mark(flag: Any) -> str:
        return "" if flag is None else ("✔" if flag else "✘")

    def _dist(value: Any) -> tuple:
        """距触发线的显示：正 = 还差这么多才破线；负 = 现价已越过（等收盘确认）。"""
        out = _finite(value)
        if out is None:
            return "—", "dim"
        if out < 0:
            return f"已越过 {abs(out) * 100:.2f}%（等收盘）", "warn"
        return f"{out * 100:.2f}%", ("warn" if out <= 0.005 else "dim")

    def _stops(row: Mapping[str, Any]) -> tuple:
        parts = []
        for label, key, ok_key in (("多", "stop_long_pct", "stop_long_ok"),
                                   ("空", "stop_short_pct", "stop_short_ok")):
            dist = _finite(row.get(key))
            if dist is None:
                parts.append(f"{label} —")
            elif dist < 0:
                parts.append(f"{label} 方向不符")     # 止损参考线跑到入场价同侧，该方向不可用
            else:
                parts.append(f"{label} {dist * 100:.2f}%{_mark(row.get(ok_key))}")
        ok = bool(row.get("stop_long_ok")) or bool(row.get("stop_short_ok"))
        return " · ".join(parts), ("pos" if ok else "neg")

    rows = []
    for row in block.get("symbols") or ():
        gap = _finite(row.get("gap_pct"))
        dense = int(row.get("dense_bars") or 0)
        need = int(row.get("min_dense_bars") or 0)
        band = _finite(row.get("band_pct"))
        thr = _finite(row.get("threshold_pct"))
        dense_ok = gap is not None and gap <= 0
        if dense_ok:
            dense_txt = f"{dense}/{need} ✔"
        elif gap is None:
            dense_txt = f"{dense}/{need} ✘"
        else:
            dense_txt = f"{dense}/{need} ✘{gap * 100:+.2f}pp"
        dense_title = (f"带宽 {band * 100:.2f}% vs 门槛 {thr * 100:.2f}%"
                       if band is not None and thr is not None else "带宽/门槛未知")
        d_long, css_long = _dist(row.get("dist_long_pct"))
        d_short, css_short = _dist(row.get("dist_short_pct"))
        m200 = _finite(row.get("ma200_gap_pct"))
        if m200 is None:
            m200_txt, m200_css = "—", "dim"
        else:
            # _gap 的符号约定：正 = 均线在现价上方 ⇒ 现价在均线「下方」
            m200_txt = f"{'下方' if m200 > 0 else '上方'} {abs(m200) * 100:.2f}% {_mark(row.get('below_ma200'))}"
            m200_css = "pos" if row.get("below_ma200") else "neg"
        stop_txt, stop_css = _stops(row)
        nxt = _finite(row.get("next_bar_close_ts"))
        held = int(row.get("held_side") or 0)
        if held:
            verdict = f"持仓中（{'多' if held == 1 else '空'}）· 离场线 SMA20 {_money(row.get('sma20'))}"
            verdict_css = "dim"
        else:
            verdict = str(row.get("verdict") or "—")
            verdict_css = "pos" if verdict in ("可开多", "可开空") else ""
        rows.append(
            "<div class='dyn-row'>"
            f"<span class='nm'>{html.escape(str(row.get('symbol')))}</span>"
            f"<span>{_money(row.get('price'))}</span>"
            f"<span class=\"{'pos' if dense_ok else 'neg'}\" title=\"{html.escape(dense_title)}\">"
            f"{dense_txt}</span>"
            f"<span class=\"{css_long}\"><span class=\"dim\">↑{_money(row.get('ma_max'))}</span> {d_long}</span>"
            f"<span class=\"{css_short}\"><span class=\"dim\">↓{_money(row.get('ma_min'))}</span> {d_short}</span>"
            f"<span class=\"{m200_css}\">{m200_txt}</span>"
            f"<span class=\"{stop_css}\">{stop_txt}</span>"
            f"<span class=\"dim next\">{html.escape(time.strftime('%m-%d %H:%M', time.gmtime(nxt)) if nxt else '—')}</span>"
            f"<span class=\"verdict {verdict_css}\">{html.escape(verdict)}</span>"
            "</div>")
    return ('<section id="block-symbols"><h2>⑤ 三标的动态（还差什么才开单）</h2>'
            f"<div class='dyn'>{head}{''.join(rows)}</div>"
            f"{_warn_html(block.get('warnings'))}</section>")


def _equity_after(points: Sequence[Tuple[float, Optional[float]]], ts: float,
                  *, max_lag_s: float = EQUITY_AFTER_MAX_LAG_S
                  ) -> Tuple[Optional[float], Optional[float]]:
    """某一笔成交之后的账户权益（「下单后的余额」那一列）。

    权益点由每轮 tick 写（约 30 分钟一个），成交发生在两个点之间 ⇒ 取**该笔之后最近的
    一个快照**，并把它滞后了多久一起返回（面板上标出来，别让人误以为那是成交瞬间的余额）。
    超出 `max_lag_s`（默认 6h）或根本没有更晚的点 → `(None, None)`：宁可显示「—」，
    也不要拿一笔更早的权益冒充「成交后」。
    """
    if not points or ts <= 0:
        return None, None
    best: Optional[Tuple[float, Optional[float]]] = None
    for stamp, equity in points:                     # points 按 ts 升序，先命中就是最近的一个
        if stamp >= ts:
            best = (stamp, equity)
            break
    if best is None:
        return None, None
    lag = max(0.0, best[0] - ts)
    if lag > max_lag_s:
        return None, None
    return best[1], lag


def _events_html(block: Mapping[str, Any], *, heading: str = "") -> str:
    head = ("<tr><th>时间(UTC)</th><th>标的</th><th>动作</th><th>事件</th>"
            "<th>内容</th></tr>")
    rows = []
    for item in block.get("events") or ():
        ts = _finite(item.get("ts"))
        action = str(item.get("action") or "")
        css = "tag skip" if item.get("skipped") else ("tag kill" if item.get("alert") else "tag")
        rows.append(
            "<tr>"
            f"<td>{html.escape(time.strftime('%m-%d %H:%M:%S', time.gmtime(ts)) if ts else '—')}</td>"
            f"<td>{html.escape(str(item.get('symbol') or '—'))}</td>"
            f"<td><span class=\"{css}\">{html.escape(action)}</span></td>"
            f"<td class=\"dim\">{html.escape(str(item.get('kind') or ''))}</td>"
            f"<td style=\"text-align:left\">{html.escape(str(item.get('message') or ''))}</td>"
            "</tr>")
    empty = "" if rows else ('<tr><td colspan="5" class="dim">还没有事件</td></tr>')
    title = heading or f'⑥ 执行器事件流（最近 {int(block.get("limit") or MAX_EVENTS)} 条）'
    return ('<section id="block-events"><h2>' + html.escape(title) + '</h2>'
            f"<table>{head}{''.join(rows)}{empty}</table>"
            f"{_warn_html(block.get('warnings'))}</section>")


def _records_fallback(warning: str, *, as_of: Optional[float] = None) -> Dict[str, Any]:
    """记录页的降级载荷（字段与正常载荷完全一致，0 值 + WARNING）。"""
    return {
        "as_of": _zero(as_of), "block": BLOCK_RECORDS,
        "funds": {"equity": 0.0, "equity_ts": None, "net_flow": 0.0, "twr_index": None,
                  "twr_peak": None, "anchor_ts": None, "anchor_equity": None,
                  "trade_count": 0, "fee_total": 0.0, "realized_total": 0.0,
                  "unexplained_jumps": [], "shown": 0, "limit": MAX_RECORD_ROWS},
        "trades": [], "count": 0, "warnings": [warning], "degraded": True,
    }


def _funds_html(block: Mapping[str, Any]) -> str:
    """① 资金概览：库里的权益/流水口径（与盈亏区块同源，不另算一套）。"""
    funds = block.get("funds") or {}
    equity = _finite(funds.get("equity"))
    twr = _finite(funds.get("twr_index"))
    peak = _finite(funds.get("twr_peak"))
    drawdown = None
    if twr is not None and peak:
        drawdown = max(0.0, 1.0 - twr / peak)
    equity_ts = _finite(funds.get("equity_ts"))
    return ('<section id="funds"><h2>① 资金概览（库内权益与流水口径）</h2>'
            '<div class="kv">'
            f'最新权益 <b>{_money(equity)} U</b>'
            f'{"（" + time.strftime("%m-%d %H:%M", time.gmtime(equity_ts)) + " UTC）" if equity_ts else ""}'
            f' ｜ 净入金 {_money(funds.get("net_flow"))} U'
            f' ｜ TWR <b>{_pct(twr)}</b>'
            f' ｜ 峰值 {_pct(peak)} ｜ 当前回撤 {_pct(drawdown)}'
            '</div>'
            '<div class="kv">'
            f'成交 {int(funds.get("trade_count") or 0)} 笔（本页展示最近 '
            f'{int(funds.get("shown") or 0)} 笔）'
            f' ｜ 已实现盈亏合计 <b class="{_cls(funds.get("realized_total"))}">'
            f'{_money(funds.get("realized_total"))} U</b>'
            f' ｜ 手续费合计 {_money(funds.get("fee_total"))} U'
            '</div>'
            '<div class="dim" style="margin-top:6px;font-size:11px">'
            '口径：权益/净入金取自 equity_curve（每轮 tick 写一次）；已实现盈亏只算平仓/止损腿。'
            '「成交后余额」= 该笔之后最近一次权益快照，括号内是滞后的时长。'
            '</div>'
            f'{_warn_html(block.get("warnings"))}</section>')


def _records_html(block: Mapping[str, Any]) -> str:
    """② 订单明细：标的 / 方向 / 数量 / 成交价 / 名义 / 手续费 / 成交后余额。"""
    head = ("<tr><th>时间(UTC)</th><th>标的</th><th>方向</th><th>类型</th>"
            "<th>数量</th><th>成交价</th><th>名义 (U)</th><th>手续费 (U)</th>"
            "<th>已实现 (U)</th><th>成交后余额 (U)</th></tr>")
    rows = []
    for trade in block.get("trades") or ():
        ts = _finite(trade.get("ts"))
        side = int(trade.get("side") or 0)
        side_css = "pos" if side > 0 else ("neg" if side < 0 else "dim")
        after = _finite(trade.get("equity_after"))
        lag = _finite(trade.get("equity_after_lag_s"))
        after_text = ("—" if after is None else
                      f"{after:,.2f}" + (f" <span class=\"dim\">+{_dur(lag)}</span>"
                                         if lag else ""))
        realized = _finite(trade.get("realized_pnl"))
        rows.append(
            "<tr>"
            f"<td>{html.escape(time.strftime('%m-%d %H:%M:%S', time.gmtime(ts)) if ts else '—')}</td>"
            f"<td>{html.escape(str(trade.get('symbol') or '—'))}</td>"
            f"<td class=\"{side_css}\">{html.escape(str(trade.get('side_text') or '—'))}</td>"
            f"<td>{html.escape(str(trade.get('kind_text') or '—'))}</td>"
            f"<td>{_qty_text(trade.get('qty'))}</td>"
            f"<td>{_money(trade.get('price'))}</td>"
            f"<td>{_money(trade.get('notional'))}</td>"
            f"<td>{_money(trade.get('fee'))}</td>"
            f"<td class=\"{_cls(realized)}\">{'—' if realized is None else _money(realized)}</td>"
            f"<td>{after_text}</td>"
            "</tr>")
    empty = "" if rows else ('<tr><td colspan="10" class="dim">还没有成交记录'
                             '（信号 → 下单 → 成交后这里才会有行）</td></tr>')
    return ('<section id="block-records"><h2>② 订单明细（最近 '
            f'{int(block.get("limit") or MAX_RECORD_ROWS)} 笔，新→旧）</h2>'
            f"<table>{head}{''.join(rows)}{empty}</table>"
            f"{_warn_html(block.get('warnings'))}</section>")


def render_records_html(status: Mapping[str, Any], records: Mapping[str, Any],
                        events: Mapping[str, Any], *, mode: str = "live",
                        title: str = "lighter-ma-bot 记录") -> str:
    """记录页（自包含 HTML）：资金概览 + 订单明细 + 执行器事件流。

    为什么单独一页：首页回答「现在怎么样」，记录页回答「发生过什么、钱怎么变的」。
    两边共用同一份 `_CSS` 与内联脚本，没有外链。
    """
    generated = _finite(records.get("as_of")) or time.time()
    body = [
        _status_html(status, mode),
        _funds_html(records),
        _records_html(records),
        _events_html(events, heading="③ 执行器事件流（最近 "
                     f"{int((events or {}).get('limit') or MAX_EVENTS)} 条）"),
    ]
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body><div class="wrap">
<h1>{html.escape(title)} <a class="navlink" href="/">← 返回概览</a></h1>
{''.join(body)}
<footer>生成于 {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(generated))} UTC ·
记录页（只读：成交与权益均来自本地库；账号私有读数由 tick 注入）</footer>
<script>{_ADMIN_JS}</script>
</div></body></html>
"""


# ------------------------------------------------------------------ 人工操作区


#: 三个写操作的面板入口（kill / flatten / dd reset）。token 走 `?k=<ADMIN_TOKEN>`
#: 链接注入：JS 读到后存进 localStorage 并立刻从地址栏抹掉（不进 nginx 日志、
#: 不进收藏夹以外的任何记录）。没有 token 时点按钮会弹一次输入框。
_ADMIN_JS = """
(function () {
  'use strict';
  var qs = new URLSearchParams(location.search);
  var fromUrl = qs.get('k');
  if (fromUrl) {
    try { localStorage.setItem('lt_admin_token', fromUrl); } catch (e) {}
    qs.delete('k');
    history.replaceState(null, '', location.pathname + (qs.toString() ? '?' + qs.toString() : ''));
  }
  function token() {
    try { return localStorage.getItem('lt_admin_token') || ''; } catch (e) { return ''; }
  }
  function msg(text, bad) {
    var el = document.getElementById('admin-msg');
    el.style.display = 'block';
    el.textContent = text;
    el.style.borderColor = bad ? 'var(--bad-bd)' : 'var(--ok-bd)';
  }
  function post(path, params) {
    var t = token();
    if (!t) {
      t = prompt('输入 ADMIN_TOKEN（会保存在本浏览器）');
      if (!t) { return; }
      try { localStorage.setItem('lt_admin_token', t); } catch (e) {}
    }
    params.k = t;
    msg('发送中…', false);
    fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
      body: new URLSearchParams(params).toString()
    }).then(function (resp) {
      return resp.json().then(function (data) { return {status: resp.status, data: data}; });
    }).then(function (r) {
      var d = r.data || {};
      if (r.status === 200 && d.ok) {
        msg('成功：' + JSON.stringify(d).slice(0, 300), false);
      } else {
        msg('失败 (' + r.status + ')：' + (d.error || JSON.stringify(d).slice(0, 200)), true);
      }
    }).catch(function (err) { msg('请求失败：' + err, true); });
  }
  function bind(id, fn) {
    var el = document.getElementById(id);
    if (el) { el.addEventListener('click', fn); }
  }
  bind('btn-kill-on', function () { post('/admin/kill', {on: '1', reason: 'panel: stop entries'}); });
  bind('btn-kill-off', function () { post('/admin/kill', {on: '0', reason: 'panel: resume entries'}); });
  bind('btn-flatten', function () {
    if (!confirm('强制清仓：市价平掉所有仓位并撤销全部挂单，不可撤销。确认？')) { return; }
    var typed = prompt('输入 yes 确认清仓');
    if ((typed || '').trim().toLowerCase() !== 'yes') { return; }
    post('/admin/flatten', {confirm: 'yes', reason: 'panel: manual flatten'});
  });
  bind('btn-dd-reset', function () {
    var reason = prompt('重置回撤峰值的原因（必填，会写入审计）');
    if (!reason || !reason.trim()) { return; }
    post('/admin/dd/reset', {reason: reason.trim()});
  });
})();
"""


def _admin_html() -> str:
    """⑦ 人工操作区：kill / 强制清仓 / 重置回撤峰值（三个写操作的唯一 UI 入口）。"""
    return (
        '<section id="block-admin"><h2>⑥ 人工操作（admin token）</h2>'
        '<div class="adminbar">'
        '<button id="btn-kill-on" type="button">⛔ 停止开仓（kill ON）</button>'
        '<button id="btn-kill-off" type="button">▶ 恢复开仓（kill OFF）</button>'
        '<button id="btn-flatten" type="button" class="danger">强制清仓 + 撤销全部挂单</button>'
        '<button id="btn-dd-reset" type="button">↺ 重置回撤峰值</button>'
        '</div>'
        '<div class="kv dim" id="admin-note" style="margin-top:8px">'
        '用带 <code>?k=ADMIN_TOKEN</code> 的面板链接打开一次，令牌会存进本浏览器并从地址栏抹掉，'
        '之后直接点按钮。kill 只停开新仓（离场与交易所侧止损照常）；'
        '清仓市价成交、不可撤销，需二次确认。</div>'
        '<div id="admin-msg" class="warnlist" style="display:none"></div>'
        '</section>'
    )


def render_html(snapshot: Mapping[str, Any], *, symbols: Sequence[str] = DEFAULT_SYMBOLS,
                mode: str = "live", title: str = "lighter-ma-bot 监控面板") -> str:
    """把 :meth:`Panel.snapshot` 的载荷渲染成**一个**自包含 HTML 文档。

    自包含 = 没有 `<script src>`、没有 `<link href>`、没有 `@import`、没有字体 CDN；
    曲线是内联 `<svg>`。运行环境可能没有外网，一个依赖 CDN 的面板在关键时刻就是空白页。
    """
    blocks = snapshot.get("blocks") or {}
    status = blocks.get(BLOCK_STATUS) or _status_fallback("状态条缺失")
    generated = _finite(snapshot.get("as_of")) or time.time()
    # 事件流已挪到记录页（首页只回答「现在怎么样」），编号随之从 ⑦ 变 ⑥。
    body = [
        _status_html(status, mode),
        _positions_html(blocks.get(BLOCK_POSITIONS) or _positions_fallback("缺失", symbols=symbols)),
        _stops_html(blocks.get(BLOCK_STOPS) or _stops_fallback("缺失")),
        _pnl_html(blocks.get(BLOCK_PNL) or _pnl_fallback("缺失")),
        _symbols_html(blocks.get(BLOCK_SYMBOLS) or _symbols_fallback("缺失", symbols=symbols)),
        _admin_html(),
    ]
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body><div class="wrap">
<h1>{html.escape(title)} <a class="navlink" href="/records">记录页 →</a></h1>
{''.join(body)}
<footer>生成于 {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(generated))} UTC ·
监控面板（读 DB + 公开行情；私有读数由 tick 注入） · 人工操作见⑥：
kill / 清仓 / 重置回撤，用带 ?k=&lt;ADMIN_TOKEN&gt; 的链接打开一次即保存到本浏览器 ·
成交与事件流见记录页</footer>
<script>{_ADMIN_JS}</script>
</div></body></html>
"""
