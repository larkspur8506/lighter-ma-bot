"""调度与执行器（T8）：把一轮 tick 跑完 —— 拉数据 → 算信号 → 决策 → 下单 → 记账 → 对账。

职责边界
--------
前面几层各自只管一件事：`app.market` 取数、`app.strategy` 出信号、`app.sizing` 定仓、
`app.risk` 判闸门、`app.lifecycle` 定状态、`app.exchange` 发单、`app.store` 落库。
本模块是**唯一**把它们串起来的地方，也是唯一同时看得见「本地 DB」和「交易所」的东西 ——
所以**对账**只能在这里做。

一轮 tick 的顺序（顺序本身是设计的一部分，改顺序等于改语义）::

    读交易所（账户/持仓/挂单）            ← 先看真相，再看本地
      → 对账（纯分析，只产报告，不改库）
      → 记权益点（DD / 日初权益的输入）
      → 取 K 线（剔未收盘 + 新鲜度校验）/ 算指标 / 判最后一根 bar 的信号
      → 装配 RiskState（DB + 交易所并集）
      → 逐标的：决策 → 发单 → 记账
      → 失败计数 / 熔断

为什么先对账再动手
------------------
旧系统（HL 现役）的教训是 PR #44/#45：先判 mismatch 再同步 fills，结果「止损刚成交」被当成
「本地有仓、交易所无仓」的灾难，触发了错误的处置。所以这里的顺序是
**先读全量交易所状态 → 同步/分析 → 再判动作**；任何「本地和交易所不一致」的结论都在
**本 tick 动手之前**得出，而不是在动完手之后猜。

四条硬规则（写死，有测试）
--------------------------
1. **同一根 bar 不重复下单**。两道锁：`store.UNIQUE(symbol, bar_ms)`（决策层
   `same_bar_already_traded` 直接跳过）+ 确定性的 `clientOrderIndex`
   （`sha256("ltab:{sym}:{bar_ms}:{purpose}")`）。第二道是给「本地记完账就崩了、
   下一轮又跑一遍」这种半状态兜底的 —— 交易所按 `clientOrderIndex` 去重。
2. **对账只报警、不改本地状态**。`reconcile()` 是**纯函数**（输入本地行 + 交易所快照，
   输出 `ReconcileReport`），它没有 store 的写句柄，**结构上不可能**"顺手修正"。
   发现不一致的唯一后果是：落 `events` + 报警 + **阻断新开仓**（fail-closed）。
3. **异常不带着不确定状态继续**。任何未预期异常 → 落 `events` → **立即停止本轮**
   （后面的标的不再处理）→ 计入连续失败。连续失败达到阈值 → 熔断：只挡开仓，
   不挡离场/止损（否则亏损仓会被熔断锁死）。
4. **减仓永远不被挡**。熔断、对账阻断、DD 熔断都只影响 `action='entry'`；
   `exit` / `stop` 照常执行（`app/lifecycle.py` 的 `decide_exit()` 签名里根本没有风控入参）。

「不自动修正」的理由（也是本模块最容易被误改的一条）
----------------------------------------------------
本地和交易所对不上时，**哪边是对的未知**：

* 本地有仓、交易所没有 —— 可能被强平 / 手动平仓，也可能本地多记了一笔；
* 交易所有仓、本地没有 —— 可能漏记，也可能交易所在另一账户/子账户；
* 数量不符 —— 可能部分成交、可能有人手动加减仓；
* 止损单丢了 —— 可能成交了、被撤了、过期了，也可能只是这条查询没授权。

自动"修正"要么把真实持仓抹掉（覆盖本地 = 忘掉一个真实敞口），要么凭空造一个仓位
（覆盖交易所 = 记一笔不存在的账）。两种都在**掩盖真相**，而真相是唯一能让人做出正确
处置的东西。所以这里的选择是：**报警 + 阻断新开仓**，等人（或 T9 的 watchdog）拿着
证据决定是 adopt 还是 flatten。唯一的例外是**有正面证据**的止损成交结案
（`_settle_stop_fill`：交易所有这个标的的仓已平 + 我们挂的止损单也不在了）——
那不是"修正状态"，而是"承认一个已经发生的事实"，并且会落 `stop_fill_settled` 事件。

交易所读取（实测 vs 推断）
--------------------------
* `GET /api/v1/account?by=index&value=<idx>` —— 无需 auth（实测过），返回
  `{code,total,accounts:[{..., positions:[{market_id,symbol,sign,position,...}]}]}`。
* `GET /api/v1/accountActiveOrders?account_index=<idx>` —— **需要** auth header。
  SDK 的对应方法是 `lighter.api.order_api.OrderApi.account_active_orders(
  authorization=..., account_index=...)`（`GET /api/v1/accountActiveOrders`，
  见 `order_api.py` 的 `_account_active_orders_serialize`）。
  持仓的 SDK 对应方法是 `AccountApi.account(by=..., value=...)`（`/api/v1/account`）。
* 两个端点的**响应结构**按 SDK 的 pydantic 模型（`DetailedAccount.positions[]` /
  `Orders.orders[]`）实现解析，字段名逐字对齐模型；没有对真实响应做过端到端抓包，
  因此解析器对**字段缺失/类型不对**一律 fail-loud（`ReaderError`），不猜。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import (Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

from app import strategy, symbol_registry
from app.exchange import OrderResult, _site_root, client_order_index
from app.lifecycle import (ACTION_ENTRY, ACTION_EXIT, ACTION_SKIP, ACTION_STOP,
                           IDLE, OPEN, Decision, Lifecycle, Rejection)
from app.risk import RiskLimits, RiskState
from app.sizing import OrderPlan, SizingConfig, from_int_qty, to_int_price
from app.state import StateAssembler
from app.store import OPEN_STATES, StateStore

__all__ = [
    "BLOCK_CIRCUIT_OPEN", "BLOCK_RECONCILE",
    "DEFAULT_MAX_CONSECUTIVE_FAILURES", "EXECUTOR_STAGE",
    "ISSUE_ORDERS_UNAVAILABLE", "ISSUE_POSITION_MISSING", "ISSUE_POSITION_UNKNOWN",
    "ISSUE_QTY_MISMATCH", "ISSUE_SIDE_MISMATCH", "ISSUE_STOP_ORDER_MISSING",
    "ISSUE_CODES", "SEVERITY_DANGER", "SEVERITY_GRACE", "SEVERITY_WARN",
    "AccountReader", "ExchangeAccount", "ExchangeOrder", "ExchangePosition",
    "Executor", "ExecutorError", "LighterAccountReader", "ReaderError", "ReconcileIssue",
    "ReconcileReport", "SymbolOutcome", "TickReport", "TRANSFER_FLOW_EPS",
    "account_transaction_time_from_payload", "infer_transfer_flow", "orders_from_payload",
    "pending_signal", "positions_from_payload", "reconcile", "transfers_to_flows",
]

logger = logging.getLogger(__name__)

#: 执行器自己的拒绝码前缀（`Decision.rejections[].stage`），与 sizing/risk/lifecycle 区分开。
EXECUTOR_STAGE = "executor"

#: 阻断开仓的三种理由（落到 `Decision.rejections[].code`，面板/日志按它聚合）。
BLOCK_RECONCILE = "reconcile_blocked"              # 对账发现不一致（fail-closed）
BLOCK_CIRCUIT_OPEN = "circuit_open"                # 连续失败熔断

#: 连续失败阈值：达到就熔断（不再尝试下单）。默认 3 —— 一次 tick 最多 3 个动作，
#: 3 次连续硬失败几乎不可能是瞬时抖动。
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

# ------------------------------------------------------------------ 对账不变式

SEVERITY_DANGER = "danger"          # 必须人工处置；阻断新开仓
SEVERITY_GRACE = "grace"            # 有止损成交证据，等结案；不阻断
SEVERITY_WARN = "warn"              # 记录/告警，但不阻断（只用于降级读取）

ISSUE_POSITION_MISSING = "position_missing_on_exchange"   # 本地有仓，交易所没有
ISSUE_POSITION_UNKNOWN = "position_unknown_locally"       # 交易所有仓，本地没有
ISSUE_QTY_MISMATCH = "position_qty_mismatch"              # 数量不一致
ISSUE_SIDE_MISMATCH = "position_side_mismatch"            # 方向不一致
ISSUE_STOP_ORDER_MISSING = "stop_order_missing"           # 本地有仓但交易所没有止损单
ISSUE_ORDERS_UNAVAILABLE = "orders_unavailable"           # 挂单列表读不到 → 无法证明止损还在

#: 全部不一致码（T8 验收的四类在前四条；后两条是同一族的补充/降级）。
ISSUE_CODES: Tuple[str, ...] = (
    ISSUE_POSITION_MISSING, ISSUE_POSITION_UNKNOWN, ISSUE_QTY_MISMATCH,
    ISSUE_STOP_ORDER_MISSING, ISSUE_SIDE_MISMATCH, ISSUE_ORDERS_UNAVAILABLE,
)

#: 默认容差（tick 数）：止损单按触发价比对时双侧 ±1 tick（任务书要求）。
DEFAULT_STOP_TOLERANCE_TICKS = 1


# ------------------------------------------------------------------ 异常

class ExecutorError(RuntimeError):
    """执行器的可预期错误基类（配置/前置条件问题，不是"业务异常"）。"""


class ReaderError(ExecutorError):
    """交易所账户读数拿不到/解析不了。**fail-loud**：不猜、不降级成空持仓。"""


# ------------------------------------------------------------------ 交易所快照

@dataclass(frozen=True)
class ExchangePosition:
    """交易所侧一个持仓（`DetailedAccount.positions[]` 的一行，只取对账要用的字段）。

    `qty` 是**正数**的 base 数量（字符串原样保留，比较用 `Decimal`）；方向在 `side`
    （`+1` 多 / `-1` 空，来自 `sign`）。`position == "0"` 的行由解析器过滤掉 ——
    交易所会把"曾经交易过、现在没仓"的市场也列出来。
    """

    symbol: str
    side: int
    qty: Decimal
    qty_str: str = ""
    market_id: Optional[int] = None
    avg_entry_price: Optional[float] = None
    #: 交易所自己算的浮动盈亏（USDC，**已含资金费**）。面板必须用这个数：
    #: 本地按「4h 收盘价 − 记账入场价」现算会滞后最多一根 4h，甚至恒为 0（入场价就是那根 bar 的收盘）。
    unrealized_pnl: Optional[float] = None
    #: 仓位当前价值（= 数量 × 标记价）。有了它才能反推**标记价**给面板显示「现价」。
    position_value: Optional[float] = None
    #: 交易所给的强平价（本地按杠杆估的只是近似）。
    liquidation_price: Optional[float] = None
    #: 本仓**累计**已付资金费（正 = 付出，负 = 收到；交易所 `total_funding_paid_out`）。
    funding_paid: Optional[float] = None
    #: 逐仓口径（市场切 isolated 后交易所**开仓时自动划入** ≈名义/杠杆，平仓自动释放；
    #: 2026-10-29 实测）。`None` = 响应里没有这两个字段（旧数据/未知）→ 抵押守护跳过不猜。
    margin_mode: Optional[int] = None
    allocated_margin: Optional[float] = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "side": self.side, "qty": str(self.qty),
                "market_id": self.market_id, "avg_entry_price": self.avg_entry_price,
                "unrealized_pnl": self.unrealized_pnl,
                "position_value": self.position_value,
                "liquidation_price": self.liquidation_price,
                "funding_paid": self.funding_paid}


@dataclass(frozen=True)
class ExchangeOrder:
    """交易所侧一个活动挂单（`Order` 模型的关键字段子集）。

    `client_order_index` 是我们自己写进去的确定性幂等键，因此它是**止损单身份**的
    第一判据；`trigger_price_int` 只在拿不到 coi 时兜底（±1 tick）。
    """

    order_id: str
    client_order_index: Optional[int]
    market_id: Optional[int]
    symbol: str = ""
    reduce_only: bool = False
    trigger_price_int: Optional[int] = None
    #: 交易所原样返回的触发价（**小数串**，如 "99.057"）。`trigger_price_int` 只能表达
    #: 「本来就是整数 tick」的情形，小数串要按标的的 price_decimals 才能换算成 tick，
    #: 而解析层拿不到 decimals ⇒ 原样留着，由 `match_stop_order` 按标的精度比。
    trigger_price_raw: Optional[str] = None
    price_int: Optional[int] = None
    qty_int: Optional[int] = None
    #: 交易所侧的订单到期时刻（epoch **毫秒**）。挂单到期会被交易所自动废单 ——
    #: 止损守护靠它判断「临期换单」；老库没有这一列时也用它回填。
    order_expiry_ms: Optional[int] = None
    status: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"order_id": self.order_id, "client_order_index": self.client_order_index,
                "market_id": self.market_id, "symbol": self.symbol,
                "reduce_only": self.reduce_only,
                "trigger_price_int": self.trigger_price_int,
                # 交易所原样触发价（小数串）：面板的「止损覆盖」要比对**触发价**，
                # 而触发价和真正挂在簿上的 `price_int`（触发后最差成交价 = 触发价 + 滑点）
                # 是两个数。少了这个字段，面板只能拿 price 去比 stop_price ⇒ 永远差一点。
                "trigger_price_raw": self.trigger_price_raw,
                "price_int": self.price_int, "qty_int": self.qty_int,
                "status": self.status}


@dataclass(frozen=True)
class ExchangeAccount:
    """一次交易所读数（一个 tick 读一次，整轮共用这一份）。

    `orders_error` 非空表示**挂单列表没读到**（没授权 / 网络 / 结构不认识）：
    持仓仍然可用，但"止损单还在不在"无法证明 —— 对账会据此出
    `orders_unavailable` 并阻断开仓（fail-closed）。
    """

    equity: Optional[float] = None
    positions: Tuple[ExchangePosition, ...] = ()
    orders: Tuple[ExchangeOrder, ...] = ()
    orders_error: str = ""
    as_of: float = 0.0
    #: 账户最近一次链上/账户级变动的时刻（epoch 秒；`/account` 的 `transaction_time`，
    #: 微秒 → 秒）。入金/出金会推进它 —— 流水推断（:func:`infer_transfer_flow`）用它把
    #: 权益跳变定位到正确的时间窗。读不到保持 0（调用方按「未知」处理，不猜）。
    transaction_time: float = 0.0

    def position_for(self, symbol: str) -> Optional[ExchangePosition]:
        wanted = str(symbol or "").strip().upper()
        for pos in self.positions:
            if pos.symbol == wanted:
                return pos
        return None

    def symbols(self) -> Tuple[str, ...]:
        return tuple(p.symbol for p in self.positions)

    def as_dict(self) -> Dict[str, Any]:
        return {"equity": self.equity, "as_of": self.as_of,
                "positions": [p.as_dict() for p in self.positions],
                "orders": [o.as_dict() for o in self.orders],
                "orders_error": self.orders_error}


# ------------------------------------------------------------------ 解析（纯函数）

def _dec(value: Any, what: str) -> Decimal:
    """`str/float/int` → `Decimal`；不可解析 → `ReaderError`（不猜 0）。"""
    if isinstance(value, Decimal):
        return value
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ReaderError(f"{what} is not a number: {value!r}") from exc
    if not out.is_finite():
        raise ReaderError(f"{what} is not finite: {value!r}")
    return out


def _opt_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _finite_or_none(value: Any) -> Optional[float]:
    """有穷浮点或 None（不是 0 兜底：0 / 缺列都当「不知道」）。"""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def positions_from_payload(payload: Mapping[str, Any], *,
                           account_index: Optional[int] = None) -> Tuple[ExchangePosition, ...]:
    """`GET /api/v1/account` 的响应 → `ExchangePosition` 元组（**零持仓行被丢弃**）。

    结构（SDK `DetailedAccounts` / `DetailedAccount`）::

        {"code": 200, "total": 1,
         "accounts": [{"index": 1, "collateral": "...", "total_asset_value": "...",
                       "positions": [{"market_id": 1, "symbol": "BTC", "sign": 1,
                                      "position": "0.04156", ...}]}]}

    `code != 200` / 没有 `accounts` / 选定账户不存在 → `ReaderError`。
    """
    if not isinstance(payload, Mapping):
        raise ReaderError(f"account payload must be a mapping, got {type(payload).__name__}")
    code = _opt_int(payload.get("code"))
    if code is not None and code != 200:
        raise ReaderError(f"account endpoint returned code {code}: "
                          f"{payload.get('message') or 'no message'}")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ReaderError("account payload has no 'accounts' list")
    chosen: Optional[Mapping[str, Any]] = None
    for item in accounts:
        if not isinstance(item, Mapping):
            raise ReaderError("account payload contains a non-mapping account entry")
        if account_index is None or _opt_int(item.get("index")) == int(account_index):
            chosen = item
            break
    if chosen is None:
        raise ReaderError(f"account {account_index} is not in the payload "
                          f"({len(accounts)} account(s) returned)")
    rows = chosen.get("positions")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ReaderError("account.positions is not a list")
    out: List[ExchangePosition] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReaderError("account.positions contains a non-mapping entry")
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            raise ReaderError("account position row has no symbol")
        qty_dec = _dec(row.get("position", "0"), f"{symbol}.position")
        if qty_dec == 0:
            continue                                   # 零持仓 = 平仓，不是持仓
        sign = _opt_int(row.get("sign"))
        if sign not in (1, -1):
            raise ReaderError(f"{symbol}: position sign must be +1/-1, got {row.get('sign')!r}")
        avg = row.get("avg_entry_price")
        out.append(ExchangePosition(
            symbol=symbol, side=int(sign), qty=abs(qty_dec),
            qty_str=str(row.get("position")),
            market_id=_opt_int(row.get("market_id")),
            avg_entry_price=(float(avg) if avg not in (None, "") else None),
            # 交易所口径的浮盈/仓位价值/强平价（都是字符串）——面板显示盈亏只用这一份，
            # 不再拿「最后一根已收盘 4h bar 的收盘价」去现算（那会滞后最多 4 小时）。
            unrealized_pnl=_opt_float(row.get("unrealized_pnl")),
            position_value=_opt_float(row.get("position_value")),
            liquidation_price=_opt_float(row.get("liquidation_price")),
            funding_paid=_opt_float(row.get("total_funding_paid_out")),
            margin_mode=_opt_int(row.get("margin_mode")),
            allocated_margin=_opt_float(row.get("allocated_margin")),
            raw=dict(row)))
    return tuple(out)


def _opt_float(value: Any) -> Optional[float]:
    """宽容的 float 转换（账户响应里的数字都是字符串）：不可用 → `None`。"""
    if value is None or value == "":
        return None
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def account_equity_from_payload(payload: Mapping[str, Any], *,
                                account_index: Optional[int] = None) -> Optional[float]:
    """从同一份账户响应里取权益：优先 `total_asset_value`，退回 `collateral`。

    Lighter 的 `collateral` 是经典模式下的保证金余额，`total_asset_value` 含未实现盈亏。
    风控要的是"账户权益"，所以优先后者；两者都缺 → `None`（fail-closed，**不猜**）。
    """
    if not isinstance(payload, Mapping):
        return None
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return None
    for item in accounts:
        if not isinstance(item, Mapping):
            continue
        if account_index is not None and _opt_int(item.get("index")) != int(account_index):
            continue
        for key in ("total_asset_value", "collateral"):
            out = _opt_float(item.get(key))
            if out is not None and out > 0:
                return out
        return None
    return None


def account_transaction_time_from_payload(payload: Mapping[str, Any], *,
                                          account_index: Optional[int] = None) -> float:
    """从同一份账户响应里取 `transaction_time`（**微秒** → epoch 秒）。

    入金/出金/账户级变动会推进它。取不到或脏值 → `0.0`（调用方按「未知」处理）。
    与 :func:`account_equity_from_payload` 选同一份 sub-account（按 `index` 过滤）。
    """
    if not isinstance(payload, Mapping):
        return 0.0
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return 0.0
    for item in accounts:
        if not isinstance(item, Mapping):
            continue
        if account_index is not None and _opt_int(item.get("index")) != int(account_index):
            continue
        raw = _opt_float(item.get("transaction_time"))
        if raw is None or raw <= 0:
            return 0.0
        return raw / 1_000_000.0 if raw >= 1e12 else raw
    return 0.0


def orders_from_payload(payload: Mapping[str, Any]) -> Tuple[ExchangeOrder, ...]:
    """`GET /api/v1/accountActiveOrders` 的响应 → `ExchangeOrder` 元组。

    结构（SDK `Orders`）：`{"code": 200, "orders": [Order, ...]}`。注意订单里的市场字段
    叫 **`market_index`**（持仓里叫 `market_id`）—— 这是实测字段名的差异，写错就会
    "永远找不到止损单"。
    """
    if not isinstance(payload, Mapping):
        raise ReaderError(f"orders payload must be a mapping, got {type(payload).__name__}")
    code = _opt_int(payload.get("code"))
    if code is not None and code != 200:
        raise ReaderError(f"activeOrders endpoint returned code {code}: "
                          f"{payload.get('message') or 'no message'}")
    rows = payload.get("orders")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ReaderError("orders payload has no 'orders' list")
    out: List[ExchangeOrder] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReaderError("orders payload contains a non-mapping entry")
        coi = _opt_int(row.get("client_order_index"))
        trigger = row.get("trigger_price")
        raw_trigger = str(trigger).strip() if trigger not in (None, "") else None
        symbol = str(row.get("market_symbol") or row.get("symbol") or "").strip().upper()
        order = ExchangeOrder(
            order_id=str(row.get("order_id") or ""),
            client_order_index=coi,
            market_id=_opt_int(row.get("market_index", row.get("market_id"))),
            symbol=symbol,
            reduce_only=bool(row.get("reduce_only")),
            trigger_price_int=(_opt_int(trigger) if str(trigger or "").strip().isdigit()
                               else None),
            trigger_price_raw=raw_trigger,
            price_int=_opt_int(row.get("base_price")),
            qty_int=_opt_int(row.get("base_size")),
            order_expiry_ms=_opt_int(row.get("order_expiry")),
            status=str(row.get("status") or ""),
            raw=dict(row))
        if order.trigger_price_int is None and raw_trigger:
            # 交易所给的是小数串（实测 `"trigger_price": "98.890"`）。这里就地按标的精度
            # 换算成整数 tick —— 否则面板/对账看到的永远是 `trigger_price_int: null`。
            ticks = _trigger_ticks(order, _price_decimals_for(symbol))
            if ticks is not None:
                order = replace(order, trigger_price_int=ticks)
        out.append(order)
    return tuple(out)


# ------------------------------------------------------------------ 读取器

class AccountReader:
    """交易所账户读数的抽象（单测注入假实现，实盘用 `LighterAccountReader`）。

    只有一个方法：`async read() -> ExchangeAccount`。**不许返回半份数据**：
    持仓拿不到就抛 `ReaderError`（对账的结论必须建立在完整读数上）。
    """

    async def read(self) -> ExchangeAccount:                     # pragma: no cover - 协议
        raise NotImplementedError

    async def aclose(self) -> None:                              # pragma: no cover
        return None


def _default_http_json(url: str, headers: Optional[Mapping[str, str]] = None,
                       timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, headers=dict(headers or {}))
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:   # noqa: S310
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:                                        # noqa: BLE001 - 只是错误详情
            detail = ""
        raise ReaderError(f"GET {url} -> HTTP {exc.code} {detail}".strip()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ReaderError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ReaderError(f"GET {url} returned non-JSON body: {body[:120]!r}") from exc


def _optional_float(raw: Any) -> Optional[float]:
    """宽松转 float（垃圾输入 → `None`）；有限性/区间由调用方判断。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def transfers_to_flows(payload: Any) -> Tuple[Dict[str, float], ...]:
    """`/transfer/history` 载荷 → `app.dd` 的流水格式（`{"ts": epoch_s, "amount": 带符号 USDC}`）。

    服务端没有稳定的字段文档（本账户 `transfers` 至今为空，拿不到真实样本），所以
    按「认得出的才收」解析：时间戳与金额任一认不出、方向不明确（没有 in/out 语义
    字段）的条目**整条丢弃**——把方向猜反的流水喂进 TWR，比少一笔流水更危险。
    """
    if isinstance(payload, Mapping):
        rows = payload.get("transfers")
    else:
        rows = payload
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return ()

    _TS_KEYS = ("ts", "timestamp", "time", "created_at", "created", "updated_at")
    _AMOUNT_KEYS = ("amount", "usdc", "value", "size")
    _IN = {"in", "deposit", "credit", "deposited", "1", "+1", "inflow"}
    _OUT = {"out", "withdraw", "withdrawal", "debit", "withdrawn", "-1", "outflow"}
    _DIRECTION_KEYS = ("direction", "side", "flow", "transfer_type", "type")

    flows: List[Dict[str, float]] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        ts: Optional[float] = None
        for key in _TS_KEYS:
            value = _optional_float(row.get(key))
            if value is None or not math.isfinite(value) or value <= 0:
                continue
            ts = value / 1000.0 if value >= 1e12 else value
            break
        if ts is None:
            continue
        magnitude: Optional[float] = None
        for key in _AMOUNT_KEYS:
            value = _optional_float(row.get(key))
            if value is None or not math.isfinite(value):
                continue
            magnitude = abs(value)
            break
        if magnitude is None:
            continue
        direction = ""
        for key in _DIRECTION_KEYS:
            text = str(row.get(key) or "").strip().lower()
            if text:
                direction = text
                break
        if direction in _IN:
            amount = magnitude
        elif direction in _OUT:
            amount = -magnitude
        else:
            continue                                  # 方向不明确 → 整条丢弃
        flows.append({"ts": ts, "amount": amount})
    return tuple(flows)


#: 权益差小于这个值视为「没有变动」。交易所权益是 6 位小数的串，余额不动时读数
#: 可能有个位 1e-9 的抖动；1e-6（0.000001 USDC）以下不可能是一笔真实出入金。
TRANSFER_FLOW_EPS = 1e-6


def infer_transfer_flow(*, prev_ts: Any, prev_equity: Any, account: "ExchangeAccount",
                        fills_since_prev: bool,
                        eps: float = TRANSFER_FLOW_EPS) -> Optional[Dict[str, float]]:
    """无成交、无持仓时，权益差**只能**来自出入金 —— 据此推断一条流水。

    背景（2026-10-28 实测）：`/transfer/history` 对 UDA 桥接入金**永远返回空**
    （SDK 的 `TransferHistoryItem` 类型枚举里没有 deposit 类，官方前端走 WS 私有频道），
    所以首次 bootstrap 之后再入金，TWR/盈亏曲线会把权益跳变全额当成交易盈利
    （实测 399 USDC 入金 → `cum_pnl=399`、TWR 显示 +395%）。

    本函数是「认得出的才收」的推断版，三条硬前提缺一不可：

    * **上一格权益有效**（`prev_ts > 0`、`prev_equity > 0`）—— 没有基准就不存在「跳变」；
    * **当前无持仓**（`account.positions` 为空）—— 有持仓时权益随 mark 价浮动，
      无法把跳变拆成「盈亏 + 流水」，**保守放弃**（宁可少剔一笔，不能猜反方向）；
    * **区间内无成交**（`fills_since_prev=False`）—— 有成交时跳变可能含手续费/
      已实现盈亏，同样放弃。

    满足前提时 `amount = equity_now − prev_equity`（正 = 净入金，负 = 净出金），
    `ts` 优先用 `account.transaction_time`（入金的真实到账时刻，落进正确的重放窗口），
    无效/不在 `(prev_ts, as_of]` 内时退回 `account.as_of`。返回 `None` 表示「不推断」，
    **不区分**「没有变动」和「不敢推断」—— 调用方需要后者时自己看前提。
    """
    base_ts = _opt_float(prev_ts)
    base_eq = _opt_float(prev_equity)
    if base_ts is None or base_ts <= 0 or base_eq is None or base_eq <= 0:
        return None
    equity = _opt_float(account.equity if account is not None else None)
    if equity is None:
        return None
    if account.positions:
        return None
    if fills_since_prev:
        return None
    delta = equity - float(base_eq)
    if abs(delta) <= float(eps):
        return None

    as_of = _opt_float(account.as_of) or float(base_ts)
    tt = _opt_float(account.transaction_time)
    if tt is not None and float(base_ts) < tt <= as_of:
        stamp = float(tt)
    else:
        stamp = max(float(base_ts), as_of)
    return {"ts": stamp, "amount": delta}


class LighterAccountReader(AccountReader):
    """REST 读数器：`/api/v1/account` + `/api/v1/accountActiveOrders`。

    * 持仓端点**无需 auth**（实测），所以第一次读就能做「本地 vs 交易所」的仓位对账；
    * 挂单端点**需要** auth（`Authorization` header，T7 的 `AuthTokenCache` 铸的就是它）。
      拿不到 auth → 挂单读失败 → `ExchangeAccount.orders_error` 非空 → 对账报
      `orders_unavailable` 并阻断开仓，而不是假装"没有挂单"。

    `http_json` 可注入（单测完全离线）。
    """

    def __init__(self, *, base_url: str, account_index: int,
                 auth_provider: Optional[Callable[[], Optional[str]]] = None,
                 http_json: Optional[Callable[..., Any]] = None,
                 timeout: float = 20.0,
                 clock: Callable[[], float] = time.time) -> None:
        # base_url 归一化成**站点根**：`config.base_url` 是含 `/api/v1` 的
        # （`app.market.BASE_URLS`），而这里的路径参数本身就带 `/api/v1/...`。
        # 直接拼会得到 `/api/v1/api/v1/account` ⇒ CloudFront 403（实测：
        # 正确路径 200，重复路径 403）。归一化放在这里，调用方传哪种形式都对。
        self.base_url = _site_root(str(base_url))
        self.account_index = int(account_index)
        self.auth_provider = auth_provider
        self._http_json = http_json or _default_http_json
        self.timeout = float(timeout)
        self.clock = clock

    def __repr__(self) -> str:
        return (f"LighterAccountReader(account_index={self.account_index}, "
                f"base_url={self.base_url!r}, auth={'yes' if self.auth_provider else 'no'})")

    def _url(self, path: str, **params: Any) -> str:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return f"{self.base_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")

    def _auth_header(self) -> Dict[str, str]:
        if self.auth_provider is None:
            return {}
        token = self.auth_provider()
        return {"Authorization": str(token)} if token else {}

    def _get(self, url: str, headers: Mapping[str, str]) -> Any:
        return self._http_json(url, headers=dict(headers), timeout=self.timeout)

    async def active_orders(self) -> Tuple[ExchangeOrder, ...]:
        """只读活挂单（不读账户/持仓）。止损守护用它轮询「新挂的单可见了吗」。

        单独一个方法是为了省往返：换单的验证要轮询好几秒，每次都连账户一起读太浪费。
        读失败**抛 ReaderError**（调用方按「还不知道」处理，绝不当成「没有挂单」）。
        """
        orders_url = self._url("/api/v1/accountActiveOrders", account_index=self.account_index)
        return orders_from_payload(
            await asyncio.to_thread(self._get, orders_url, self._auth_header()))

    async def read(self) -> ExchangeAccount:
        account_url = self._url("/api/v1/account", by="index", value=self.account_index)
        payload = await asyncio.to_thread(self._get, account_url, {})
        positions = positions_from_payload(payload, account_index=self.account_index)
        equity = account_equity_from_payload(payload, account_index=self.account_index)
        transaction_time = account_transaction_time_from_payload(
            payload, account_index=self.account_index)
        orders: Tuple[ExchangeOrder, ...] = ()
        orders_error = ""
        orders_url = self._url("/api/v1/accountActiveOrders", account_index=self.account_index)
        try:
            orders = orders_from_payload(
                await asyncio.to_thread(self._get, orders_url, self._auth_header()))
        except ReaderError as exc:
            # 挂单读不到**不是**致命错误（持仓仍然有效），但必须显式记录：
            # 对账据此报 `orders_unavailable` 并阻断开仓，绝不当作"没有挂单"。
            orders_error = f"{type(exc).__name__}: {exc}"
            logger.warning("active orders unavailable: %s", orders_error)
        return ExchangeAccount(equity=equity, positions=positions, orders=orders,
                               orders_error=orders_error, as_of=self.clock(),
                               transaction_time=transaction_time)

    def transfers(self) -> Tuple[Dict[str, float], ...]:
        """出入金流水（`/api/v1/transfer/history`，需要 auth；TWR 剔出入金的输入）。

        读失败由调用方决定怎么降级（tick 里按「无流水推进 + 落事件」处理），
        本方法只负责把 HTTP/解析错误如实抛出来。
        """
        url = self._url("/api/v1/transfer/history", account_index=self.account_index)
        payload = self._get(url, self._auth_header())
        return transfers_to_flows(payload)


# ------------------------------------------------------------------ 对账

@dataclass(frozen=True)
class ReconcileIssue:
    """一条对账不一致（纯数据，`as_dict()` 直接进 `events.data` / 面板）。"""

    code: str
    symbol: str
    message: str
    severity: str = SEVERITY_DANGER
    blocker: bool = True
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def danger(self) -> bool:
        return self.severity == SEVERITY_DANGER

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "symbol": self.symbol, "message": self.message,
                "severity": self.severity, "blocker": bool(self.blocker),
                "detail": dict(self.detail)}

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code} {self.symbol}: {self.message}"


@dataclass(frozen=True)
class ReconcileReport:
    """一次对账的**只读**结论。`has_blockers` 为真 → 本 tick 不新开仓。"""

    as_of: float = 0.0
    local_count: int = 0
    exchange_count: int = 0
    order_count: int = 0
    issues: Tuple[ReconcileIssue, ...] = ()
    orders_available: bool = True

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def has_blockers(self) -> bool:
        return any(i.blocker for i in self.issues)

    @property
    def blockers(self) -> Tuple[ReconcileIssue, ...]:
        return tuple(i for i in self.issues if i.blocker)

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(i.code for i in self.issues)

    def by_code(self, code: str) -> Tuple[ReconcileIssue, ...]:
        return tuple(i for i in self.issues if i.code == code)

    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted({i.symbol for i in self.issues if i.symbol}))

    def as_dict(self) -> Dict[str, Any]:
        return {"as_of": self.as_of, "ok": self.ok, "has_blockers": self.has_blockers,
                "local_count": self.local_count, "exchange_count": self.exchange_count,
                "order_count": self.order_count, "orders_available": self.orders_available,
                "codes": list(self.codes),
                "issues": [i.as_dict() for i in self.issues]}

    def __str__(self) -> str:
        if not self.issues:
            return (f"reconcile OK (local={self.local_count}, "
                    f"exchange={self.exchange_count}, orders={self.order_count})")
        blockers = sum(1 for i in self.issues if i.blocker)
        head = (f"reconcile {len(self.issues)} issue(s), {blockers} blocker(s) "
                f"(local={self.local_count}, exchange={self.exchange_count}, "
                f"orders={self.order_count})")
        return "\n".join([head] + [f"  - {issue}" for issue in self.issues])


def expected_stop_index(row: Mapping[str, Any]) -> Optional[int]:
    """从本地仓位行解析"我们挂出去的那个止损单的 `clientOrderIndex`"。

    约定：`stop_order_id` 存 `coi:<clientOrderIndex>`（T8 记账时写入）。它是**确定性**
    幂等键（`sha256("ltab:{sym}:{bar_ms}:stop")`），因此不需要交易所回传 order id 也能
    在挂单列表里认人 —— 这一点在"链上订单很快消失"的止损场景里很关键。
    """
    raw = str(row.get("stop_order_id") or "").strip()
    if raw.lower().startswith("coi:"):
        try:
            return int(raw[4:])
        except ValueError:
            return None
    return None


def _price_decimals_for(symbol: Any) -> Optional[int]:
    """标的的 price_decimals（用于把交易所返回的小数价格串换算成整数 tick）。

    只在注册表里有该标的时返回；未知标的返回 None ⇒ 调用方**放弃**这条兜底匹配
    （fail-open 会把别人的单当成自己的止损，宁可不匹配）。
    """
    try:
        spec = symbol_registry.spec(str(symbol or "").strip().upper())
    except Exception:                                          # noqa: BLE001 - 未知标的
        return None
    return None if spec is None else int(spec.price_decimals)


def _trigger_ticks(order: ExchangeOrder, price_decimals: Optional[int]) -> Optional[int]:
    """止损单的触发价 → 整数 tick。

    交易所字段 `trigger_price` 是**小数串**（`Order.trigger_price: str`，如 "99.057"），
    而 `base_price` / `base_size` 是整数 tick。旧解析用 `str.isdigit()` 判空 ⇒ 所有小数
    触发价都被当成 None，面板的「止损覆盖」与 ±1 tick 兜底匹配对小数的 STOP 永远失效
    （2026-10-28 实测：我们自己的止损单 coi 匹配没问题，`trigger_price_int` 恒为 null）。
    整数串原样可用（历史/测试数据）；小数串按标的精度换算。
    """
    if order.trigger_price_int is not None:
        return int(order.trigger_price_int)
    raw = order.trigger_price_raw
    if not raw or price_decimals is None:
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return int((value * (10 ** int(price_decimals))).to_integral_value())


def match_stop_order(row: Mapping[str, Any], orders: Sequence[ExchangeOrder], *,
                     tolerance_ticks: int = DEFAULT_STOP_TOLERANCE_TICKS
                     ) -> Tuple[Optional[ExchangeOrder], str]:
    """在挂单里找"这个仓位的止损单"。返回 `(order, matched_by)`，找不到 `(None, "")`。

    判据顺序：
    1. `client_order_index` 精确相等（首选，唯一键）；
    2. `reduce_only` + 同市场 + 触发价与 `stop_price_int` 相差 ≤ `tolerance_ticks`
       （兜底：老仓位/别人手工挂的单没写 coi）。
    """
    coi = expected_stop_index(row)
    market = row.get("market_id")
    stop_int = row.get("stop_price_int")
    candidates = [o for o in orders if o.reduce_only]
    if coi is not None:
        for order in candidates:
            if order.client_order_index == coi:
                if market is not None and order.market_id is not None \
                        and int(order.market_id) != int(market):
                    continue
                return order, "client_order_index"
    if stop_int:
        pdec = _price_decimals_for(row.get("symbol"))
        for order in candidates:
            if market is not None and order.market_id is not None \
                    and int(order.market_id) != int(market):
                continue
            ticks = _trigger_ticks(order, pdec)
            if ticks is None:
                continue
            if abs(int(ticks) - int(stop_int)) <= int(tolerance_ticks):
                return order, "trigger_price"
    return None, ""


def _qty_matches(local_qty_int: int, size_decimals: int, exchange_qty: Decimal) -> bool:
    """数量比对：整数 tick 语义，容差 = 半个 tick（只吸收字符串→Decimal 的表示误差）。"""
    local = Decimal(int(local_qty_int)).scaleb(-int(size_decimals))
    tick = Decimal(1).scaleb(-int(size_decimals))
    return abs(exchange_qty - local) <= tick / 2


def reconcile(*, local_positions: Iterable[Mapping[str, Any]],
              account: ExchangeAccount,
              now: Optional[float] = None,
              stop_tolerance_ticks: int = DEFAULT_STOP_TOLERANCE_TICKS) -> ReconcileReport:
    """**纯函数**对账：本地仓位行 × 交易所快照 → `ReconcileReport`。

    没有 store 句柄 → **结构上不可能改本地状态**（这是 T8 验收项 4 的机械保证）。

    四类必须发现的不一致：
    ==========================================  ==============================
    `position_missing_on_exchange`              本地有活仓，交易所没有
    `position_unknown_locally`                  交易所有仓，本地没有
    `position_qty_mismatch`                     数量不一致
    `stop_order_missing`                        本地有仓但交易所没有止损单
    ==========================================  ==============================

    另加两条同族检查：`position_side_mismatch`（方向反了）、`orders_unavailable`
    （挂单列表读不到，无法证明止损还在）。

    "本地有仓 / 交易所没有"分两种结局：
    * 止损单**还在**挂单列表里 → `danger`：仓凭空消失而保护单还在（强平/手动平仓/漏记），
      这种必须人工处置，自动处置就是在猜；
    * 止损单**不在了**（且我们确实挂过）→ `grace`：极可能是止损成交，
      由 `Executor._settle_stop_fill()` 用 `record_stop` 结案（见模块 docstring 的例外说明）。
    """
    stamp = float(account.as_of if now is None else now)
    local_rows = [dict(r) for r in local_positions
                  if str(r.get("state") or "").upper() in OPEN_STATES]
    local_by_symbol: Dict[str, Dict[str, Any]] = {}
    for row in local_rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            raise ExecutorError("local position row has no symbol")
        local_by_symbol[symbol] = row
    exchange_by_symbol: Dict[str, ExchangePosition] = {}
    for pos in account.positions:
        exchange_by_symbol[pos.symbol] = pos

    issues: List[ReconcileIssue] = []
    if account.orders_error:
        issues.append(ReconcileIssue(
            code=ISSUE_ORDERS_UNAVAILABLE, symbol="",
            message=("active orders could not be read, so a resting stop cannot be "
                     "verified: " + account.orders_error),
            severity=SEVERITY_DANGER, blocker=True,
            detail={"error": account.orders_error}))

    for symbol, row in sorted(local_by_symbol.items()):
        pos = exchange_by_symbol.get(symbol)
        stop_order, matched_by = match_stop_order(row, account.orders,
                                                  tolerance_ticks=stop_tolerance_ticks)
        if pos is None:
            had_stop = expected_stop_index(row) is not None
            if had_stop and stop_order is None and not account.orders_error:
                issues.append(ReconcileIssue(
                    code=ISSUE_POSITION_MISSING, symbol=symbol,
                    message=("local position is live but the exchange is flat and the resting "
                             "stop is gone — most likely the stop filled; awaits settlement"),
                    severity=SEVERITY_GRACE, blocker=False,
                    detail={"position_id": row.get("id"), "state": row.get("state"),
                            "qty_int": row.get("qty_int"), "evidence": "stop_order_gone"}))
            else:
                issues.append(ReconcileIssue(
                    code=ISSUE_POSITION_MISSING, symbol=symbol,
                    message=("local position is live but the exchange reports no position "
                             "(liquidation / manual close / stale local row?)"),
                    severity=SEVERITY_DANGER, blocker=True,
                    detail={"position_id": row.get("id"), "state": row.get("state"),
                            "qty_int": row.get("qty_int"),
                            "stop_order_present": stop_order is not None,
                            "orders_available": not account.orders_error,
                            "evidence": ("stop_order_still_active" if stop_order is not None
                                         else "no_stop_was_placed")}))
            continue

        local_side = int(row.get("side") or 0)
        if local_side != int(pos.side):
            issues.append(ReconcileIssue(
                code=ISSUE_SIDE_MISMATCH, symbol=symbol,
                message=f"side differs: local {local_side:+d}, exchange {pos.side:+d}",
                severity=SEVERITY_DANGER, blocker=True,
                detail={"position_id": row.get("id"), "local_side": local_side,
                        "exchange_side": int(pos.side)}))
        local_qty_int = int(row.get("qty_int") or 0)
        size_decimals = int(row.get("size_decimals") or 0)
        if not _qty_matches(local_qty_int, size_decimals, pos.qty):
            issues.append(ReconcileIssue(
                code=ISSUE_QTY_MISMATCH, symbol=symbol,
                message=(f"qty differs: local {local_qty_int} "
                         f"(={from_int_qty(local_qty_int, size_decimals):g}) vs exchange "
                         f"{pos.qty_str or pos.qty}"),
                severity=SEVERITY_DANGER, blocker=True,
                detail={"position_id": row.get("id"), "local_qty_int": local_qty_int,
                        "size_decimals": size_decimals, "exchange_qty": str(pos.qty)}))
        if row.get("stop_price_int") is not None and stop_order is None \
                and not account.orders_error:
            issues.append(ReconcileIssue(
                code=ISSUE_STOP_ORDER_MISSING, symbol=symbol,
                message=("position has a stop price but no matching reduce-only order is "
                         "resting on the exchange — the position is unprotected"),
                severity=SEVERITY_DANGER, blocker=True,
                detail={"position_id": row.get("id"),
                        "stop_price_int": row.get("stop_price_int"),
                        "stop_order_id": row.get("stop_order_id"),
                        "active_orders": len(account.orders)}))

    for symbol, pos in sorted(exchange_by_symbol.items()):
        if symbol in local_by_symbol:
            continue
        issues.append(ReconcileIssue(
            code=ISSUE_POSITION_UNKNOWN, symbol=symbol,
            message=(f"exchange holds {pos.qty_str or pos.qty} (side {pos.side:+d}) "
                     f"but the store has no live position row"),
            severity=SEVERITY_DANGER, blocker=True,
            detail={"exchange_qty": str(pos.qty), "exchange_side": int(pos.side),
                    "market_id": pos.market_id}))

    return ReconcileReport(as_of=stamp, local_count=len(local_by_symbol),
                           exchange_count=len(exchange_by_symbol),
                           order_count=len(account.orders), issues=tuple(issues),
                           orders_available=not account.orders_error)


# ------------------------------------------------------------------ 信号

def pending_signal(symbol: str, bars: Sequence["strategy.Bar"],
                   ind: Mapping[str, Sequence[Optional[float]]], i: int,
                   entry_price: Optional[float] = None, *,
                   sides: Sequence[int] = (1, -1),
                   short_regime_ma: int = strategy.REGIME_MA) -> Optional["strategy.Signal"]:
    """判第 `i` 根（**最后一根已收盘**）bar 的入场信号 —— 实盘 tick 用的就是这一根。

    为什么不直接用 `strategy.scan()`：`scan()` 的循环上界是 `len(bars)-1`（它需要
    `bars[i+1].open` 作为入场价，回放时才有意义），所以它**结构上不产出最后一根 bar 的
    信号**。实盘 tick 恰恰要判最后一根已收盘 bar（当下正在形成的那根 bar 的开盘价就是
    入场参考）。这里调用的是 `scan()` 内部**同一个** `evaluate_entry()`，参数逐一对齐
    （`squeeze_flags(ind["band"])` 默认窗口/分位），不复制任何常数；单测
    `test_pending_signal_matches_scan_on_the_same_bar` 钉住两者在重叠区间上完全一致。
    """
    squeeze = strategy.squeeze_flags(ind["band"])
    return strategy.evaluate_entry(
        symbol, bars, ind, squeeze, int(i), entry_price=entry_price,
        allow_long=(1 in sides), allow_short=(-1 in sides),
        short_regime_ma=short_regime_ma)


# ------------------------------------------------------------------ 报告

@dataclass(frozen=True)
class SymbolOutcome:
    """一个标的在本轮 tick 里的结局（面板/日志直接渲染）。"""

    symbol: str
    action: str = ACTION_SKIP
    bar_ms: Optional[int] = None
    reason: str = ""
    decision: Optional[Mapping[str, Any]] = None
    order: Optional[Mapping[str, Any]] = None
    stop_order: Optional[Mapping[str, Any]] = None
    filled: bool = False
    settled_stop_fill: bool = False
    ok: bool = True
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "action": self.action, "bar_ms": self.bar_ms,
                "reason": self.reason, "decision": dict(self.decision or {}),
                "order": dict(self.order or {}) if self.order else None,
                "stop_order": dict(self.stop_order or {}) if self.stop_order else None,
                "filled": self.filled, "settled_stop_fill": self.settled_stop_fill,
                "ok": self.ok, "detail": dict(self.detail)}

    def __str__(self) -> str:
        extra = ""
        if self.order:
            extra = f" order={self.order.get('kind')}/{self.order.get('tx_hash') or 'dry'}"
        return f"{self.symbol} {self.action.upper()}{' (filled)' if self.filled else ''}{extra} — {self.reason}"


@dataclass(frozen=True)
class TickReport:
    """一轮 tick 的完整结果（幂等：同一 bar 重跑只会产生 `skip`）。"""

    started_at: float = 0.0
    finished_at: float = 0.0
    status: str = "ok"                     # ok | error | circuit_open | blocked
    equity: Optional[float] = None
    outcomes: Tuple[SymbolOutcome, ...] = ()
    reconcile: Optional[ReconcileReport] = None
    blocked_entries: Tuple[str, ...] = ()
    block_reasons: Tuple[str, ...] = ()
    circuit_open: bool = False
    failures: int = 0
    error: str = ""
    #: 本轮止损守护做了什么（补挂/换单成功的那几张单；面板把它并进挂单缓存）
    stop_guards: Tuple[Mapping[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok" and not self.error

    @property
    def actions(self) -> Tuple[SymbolOutcome, ...]:
        return tuple(o for o in self.outcomes if o.action in (ACTION_ENTRY, ACTION_EXIT,
                                                              ACTION_STOP))

    def as_dict(self) -> Dict[str, Any]:
        return {"started_at": self.started_at, "finished_at": self.finished_at,
                "status": self.status, "ok": self.ok, "equity": self.equity,
                "blocked_entries": list(self.blocked_entries),
                "block_reasons": list(self.block_reasons),
                "circuit_open": self.circuit_open, "failures": self.failures,
                "error": self.error,
                "reconcile": self.reconcile.as_dict() if self.reconcile else None,
                "outcomes": [o.as_dict() for o in self.outcomes]}

    def __str__(self) -> str:
        lines = [f"tick {self.status} equity={self.equity} "
                 f"failures={self.failures} circuit={'OPEN' if self.circuit_open else 'closed'}"]
        if self.reconcile is not None:
            lines.append(str(self.reconcile))
        if self.blocked_entries:
            lines.append(f"  blocked entries: {', '.join(self.blocked_entries)} "
                         f"({', '.join(self.block_reasons)})")
        for outcome in self.outcomes:
            lines.append(f"  - {outcome}")
        if self.error:
            lines.append(f"  ERROR: {self.error}")
        return "\n".join(lines)


# ------------------------------------------------------------------ 执行器

class Executor:
    """一轮 tick 的编排器。

    依赖全部注入（单测可以全离线）::

        Executor(store=..., market=..., exchange=..., reader=...)

    * `market`：需要 `bars(symbol) -> BarsResult` 与 `market_details() -> {sym: MarketInfo}`
      （`app.market.MarketClient` 就是）；
    * `exchange`：需要 `place_entry/place_stop/place_exit(...) -> OrderResult` 与
      `wait_for_tx(hash)`（`app.exchange.ExchangeClient` 就是）；
    * `reader`：`AccountReader`（交易所账户/持仓/挂单）。

    `dry_run` 的判定只看 `exchange.dry_run`：dry-run 下**不发单**，本地记账就成了"纸上
    持仓"，因此对账的不一致不会阻断（否则模拟盘第一轮开仓后每轮都被自己挡死），
    但仍然**照常报警** —— 这条权衡写在 `docs/EXECUTOR.md`。
    """

    def __init__(self, *, store: StateStore, market: Any, exchange: Any,
                 reader: AccountReader,
                 symbols: Sequence[str] = ("BTC", "ETH", "SOL"),
                 lifecycle: Optional[Lifecycle] = None,
                 assembler: Optional[StateAssembler] = None,
                 limits: Optional[RiskLimits] = None,
                 sizing_config: Optional[SizingConfig] = None,
                 sides: Sequence[int] = (1, -1),
                 short_regime_ma: int = strategy.REGIME_MA,
                 max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
                 require_confirmation: bool = True,
                 stop_tolerance_ticks: int = DEFAULT_STOP_TOLERANCE_TICKS,
                 enforce_in_dry_run: bool = False,
                 stop_guard: bool = True,
                 margin_isolated: bool = True,
                 alert: Optional[Callable[[str, str], Any]] = None,
                 clock: Callable[[], float] = time.time,
                 now_ms: Optional[Callable[[], int]] = None) -> None:
        if store is None:
            raise ExecutorError("Executor requires a StateStore")
        if market is None or exchange is None or reader is None:
            raise ExecutorError("Executor requires market, exchange and reader")
        self.store = store
        self.market = market
        self.exchange = exchange
        self.reader = reader
        self.symbols: Tuple[str, ...] = tuple(str(s).strip().upper() for s in symbols)
        if not self.symbols:
            raise ExecutorError("Executor needs at least one symbol")
        self.lifecycle = lifecycle if lifecycle is not None else Lifecycle(
            store, limits=limits if limits is not None else RiskLimits(),
            sizing_config=sizing_config if sizing_config is not None
            else SizingConfig())
        self.assembler = assembler if assembler is not None else StateAssembler(store)
        self.sides: Tuple[int, ...] = tuple(int(s) for s in sides)
        if set(self.sides) - {1, -1}:
            raise ExecutorError(f"sides must be a subset of (1, -1), got {self.sides!r}")
        self.short_regime_ma = int(short_regime_ma)
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self.require_confirmation = bool(require_confirmation)
        self.stop_tolerance_ticks = max(0, int(stop_tolerance_ticks))
        self.enforce_in_dry_run = bool(enforce_in_dry_run)
        #: 止损守护（补挂/换单）。默认开；`LIGHTER_STOP_GUARD=0` 关。
        self.stop_guard = bool(stop_guard)
        #: 逐仓保证金：开仓前把市场切到 isolated + 目标杠杆（幂等），成交后划入抵押
        #: （≈名义/杠杆，交易所会归一；平仓时交易所自动释放，无需 REMOVE）。
        #: 默认开（账户 2026-10-29 起已切逐仓）；`LIGHTER_ISOLATED_MARGIN=0` 关。
        self.margin_isolated = bool(margin_isolated)
        #: 逐仓保证金：开仓前把市场切到 isolated + 目标杠杆（幂等），成交后划入抵押
        #: （≈名义/杠杆，交易所会归一；平仓时交易所自动释放，无需 REMOVE）。
        #: 默认开（账户 2026-10-29 起已切逐仓）；`LIGHTER_ISOLATED_MARGIN=0` 关。
        self.margin_isolated = bool(margin_isolated)
        self.alert = alert
        self.clock = clock
        self._now_ms = now_ms or (lambda: int(self.clock() * 1000))
        self._prices: Dict[str, float] = {}
        #: 本轮各标的的已收盘 K 线（面板复用；不在面板路径上取数）
        self._bars: Dict[str, Tuple[Any, ...]] = {}
        #: 连续硬失败计数（异常 / 下单未被接受）。一轮干净 tick 清零。
        self._failures = 0
        self._circuit_open = False
        #: 最近一次成功读取的交易所快照（`_settle_stop_fill` 与面板要读它）。
        self.last_account: Optional[ExchangeAccount] = None
        self.last_report: Optional[TickReport] = None
        #: 记账用的权益（`apply_entry` 的 `equity_at_entry`），每轮刷新。
        self._equity: Optional[float] = None

    def __repr__(self) -> str:
        return (f"Executor(symbols={self.symbols}, failures={self._failures}, "
                f"circuit={'open' if self._circuit_open else 'closed'}, "
                f"dry_run={self.dry_run})")

    # ---------------------------------------------------------------- 状态

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.exchange, "dry_run", False))

    @property
    def failures(self) -> int:
        return self._failures

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open

    def reset_circuit(self, *, reason: str = "manual") -> None:
        """人工复位熔断（T10 面板的管理操作会调它）。**不会**自动复位。"""
        was = self._circuit_open
        self._circuit_open = False
        self._failures = 0
        if was:
            self._event("circuit_reset", f"circuit reset ({reason})",
                        data={"reason": reason})

    # ---------------------------------------------------------------- 一轮 tick

    async def run_once(self, now_ms: Optional[int] = None) -> TickReport:
        """跑一轮。**不抛异常**：所有失败都落 `events` + 反映在 `TickReport` 上。

        这不是"静默吞异常"：每一条异常路径都会 (a) 写 `events` 表、(b) 计入连续失败、
        (c) 让 `report.status != 'ok'` 且 `report.error` 非空、(d) 必要时打开熔断。
        测试 `test_unexpected_exception_is_recorded_and_stops_the_round` 逐条钉住。
        """
        started = float(self.clock())
        stamp_ms = int(now_ms if now_ms is not None else self._now_ms())
        report = TickReport(started_at=started, status="ok",
                            circuit_open=self._circuit_open)
        # 熔断**不**跳过整轮：仓位管理（离场/止损）与权益记录必须继续，否则熔断会顺手
        # 把亏损仓锁死、把权益曲线留个洞。熔断只做一件事：在本轮把开仓动作挡掉。
        if self._circuit_open:
            self._event("tick_blocked", "circuit open — entries are blocked this tick",
                        data={"failures": self._failures})
        try:
            report = await self._run_tick(report, stamp_ms)
        except Exception as exc:                                  # noqa: BLE001 - 兜底：必须留痕
            try:
                failure = self._note_failure("tick", exc)
            except Exception as nested:                           # noqa: BLE001 - 连 events 都写不进
                # 只有 DB 本身坏了才会走到这里：日志是最后一道留痕，绝不静默。
                logger.exception("could not persist the tick failure")
                failure = f"tick: {type(exc).__name__}: {exc} (events write failed: {nested})"
            report = replace(report, status="error", finished_at=float(self.clock()),
                             failures=self._failures, circuit_open=self._circuit_open,
                             error=failure)
        else:
            report = replace(report, finished_at=float(self.clock()))
        report = replace(report, circuit_open=self._circuit_open, failures=self._failures)
        self.last_report = report
        return report

    async def _run_tick(self, report: TickReport, stamp_ms: int) -> TickReport:
        now_s = float(self.clock())
        failures_at_start = self._failures
        # ① 交易所真相（失败即整轮停手：没有读数就没有对账结论）
        account = await self.reader.read()
        self.last_account = account
        # ①b 止损守护（2026-10-29）：先补保护、再对账 —— 缺止损就补挂、临期就换单。
        #     放在对账之前是刻意的：刚补上的止损不该再触发一次「缺止损」的封锁/告警。
        #     **只加保护**：不动仓位、不改信号、不碰离场判定。
        stop_guards: Tuple[Mapping[str, Any], ...] = ()
        if self.stop_guard and not self.dry_run:
            account, guards = await self._guard_stops(account)
            stop_guards = tuple(guards)
            if stop_guards:
                self.last_account = account
        # ①c 逐仓抵押守护（2026-10-29）：allocated 低于 名义/杠杆 才补差额（幂等）。
        #     实测交易所开仓时已自动划入，正常这里什么都不做；只修偏低，**从不撤出**。
        await self._guard_margins(account)
        # ② 对账（纯分析；先对账后动手）
        rec = self.reconcile(account)
        blocks: List[str] = []
        blocked_entries: List[str] = []
        if rec.issues:
            # 报警永远做（含 grace：止损成交也要留痕）；阻断只在"交易所读数是权威"的
            # 时候做 —— dry-run 下交易所永远没有仓（我们从没发过单），阻断会把自己的
            # 模拟盘锁死。见 docs/EXECUTOR.md。
            self._report_reconcile(rec)
            if rec.has_blockers and (not self.dry_run or self.enforce_in_dry_run):
                blocks.append(BLOCK_RECONCILE)
        # ③ 权益点（先写，日初权益/回撤才有输入）
        if account.equity is not None:
            self.store.record_equity(float(account.equity), ts=now_s, source="exchange")
        self._equity = account.equity

        # ④ 取数 + 信号（先全部取完，任何一个标的取数失败 → 本轮停手）
        feeds: Dict[str, Dict[str, Any]] = {}
        for symbol in self.symbols:
            feeds[symbol] = await self._load_symbol(symbol, stamp_ms)

        # ⑤ 装配 RiskState（库里持仓 ∪ 交易所持仓；并集 = 更严）
        if self.assembler.equity_provider is None:
            self.assembler.equity_provider = lambda: account.equity
        if self.assembler.positions_provider is None:
            self.assembler.positions_provider = lambda: tuple(account.positions)
        if self.assembler.price_provider is None:
            self.assembler.price_provider = lambda sym: self._prices.get(str(sym).upper())
        self.assembler.ensure_day_start(now_ts=now_s)
        snapshot = self.assembler.snapshot(now_ts=now_s)
        risk_state = snapshot.to_risk_state()

        if self._circuit_open and BLOCK_CIRCUIT_OPEN not in blocks:
            blocks.append(BLOCK_CIRCUIT_OPEN)

        outcomes: List[SymbolOutcome] = []
        for symbol in self.symbols:
            feed = feeds[symbol]
            decision = await self._decide(symbol, feed, risk_state)
            decision, block_code = self._apply_entry_guard(decision, blocks=tuple(blocks))
            if block_code:
                blocked_entries.append(symbol)
            try:
                outcome = await self._execute(symbol, decision, feed, account)
            except Exception as exc:                              # noqa: BLE001 - 必须留痕
                failure = self._note_failure(symbol, exc)
                outcomes.append(SymbolOutcome(
                    symbol=symbol, action=decision.action, bar_ms=decision.bar_ms,
                    reason=failure, ok=False,
                    decision=decision.as_dict()))
                # 任务书要求：任何未预期异常 → 停止本轮，不带不确定状态继续
                self._event("tick_aborted", f"{symbol}: {failure}", symbol=symbol,
                            data={"error": failure, "symbol": symbol})
                return replace(report, status="error", outcomes=tuple(outcomes),
                               reconcile=rec, equity=account.equity,
                               blocked_entries=tuple(blocked_entries),
                               block_reasons=tuple(blocks), error=failure,
                               stop_guards=stop_guards)
            outcomes.append(outcome)

        # 一轮跑完且本轮没有任何新的硬失败 → 连续失败清零（成功一次就重置是有意的：
        # 熔断要的是"连续"，不是"累计"）。
        if self._failures == failures_at_start:
            self._failures = 0
        status = "blocked" if blocked_entries else "ok"
        return replace(report, status=status, outcomes=tuple(outcomes), reconcile=rec,
                       equity=account.equity,
                       blocked_entries=tuple(blocked_entries),
                       block_reasons=tuple(blocks), stop_guards=stop_guards)

    # ---------------------------------------------------------------- 取数

    async def _load_symbol(self, symbol: str, stamp_ms: int) -> Dict[str, Any]:
        """拉一个标的的 K 线 + 指标 + 信号。**只认已收盘 bar**（`market.bars()` 已剔除）。

        **必须 off-loop**：`market.bars()` 是同步 urllib + 分页，三标的合计几十秒；
        直接在事件循环里调会把 uvicorn 的只读端点一起卡住（2026-10-28 实测：tick 期间
        `/status` 偶发十几秒无响应/空 body）。⇒ 一律 `asyncio.to_thread`。
        """
        result = await asyncio.to_thread(self.market.bars, symbol, now_ms=stamp_ms)
        bars = list(getattr(result, "bars", ()) or ())
        if not bars:
            raise ExecutorError(f"{symbol}: no closed bars returned")
        if len(bars) < strategy.MIN_BARS_REQUIRED:
            return {"symbol": symbol, "bars": bars, "ind": {}, "signal": None,
                    "bar_ms": int(bars[-1].open_ms), "warm": False,
                    "reason": (f"warmup: {len(bars)} closed bars < "
                               f"{strategy.MIN_BARS_REQUIRED} required"),
                    "result": result}
        ind = strategy.compute_indicators(bars)
        i = len(bars) - 1
        unclosed = tuple(getattr(result, "dropped_unclosed", ()) or ())
        # 入场参考价 = 正在形成的那根 bar 的开盘价（= 下单时的市价近似）；
        # 取不到（无未收盘 bar / 数据源只给已收盘）就退回最后一根收盘价。
        entry_ref = float(unclosed[0].open) if unclosed else float(bars[-1].close)
        signal = pending_signal(symbol, bars, ind, i, entry_ref, sides=self.sides,
                                short_regime_ma=self.short_regime_ma)
        self._prices[symbol] = float(bars[-1].close)
        # 面板要用的同一份 K 线（tick 是唯一取数者；面板只读，见 `PanelCache.set_bars`）。
        self._bars[symbol] = tuple(bars)
        return {"symbol": symbol, "bars": bars, "ind": ind, "signal": signal,
                "bar_ms": int(bars[-1].open_ms), "warm": True, "reason": "",
                "entry_ref": entry_ref, "result": result}

    # ---------------------------------------------------------------- 决策

    async def _decide(self, symbol: str, feed: Mapping[str, Any],
                      risk_state: RiskState) -> Decision:
        if not feed.get("warm"):
            return Decision(symbol=symbol, action=ACTION_SKIP, from_state=IDLE,
                            to_state=IDLE, bar_ms=feed.get("bar_ms"),
                            rejections=(Rejection(stage=EXECUTOR_STAGE, code="warmup",
                                                  message=str(feed.get("reason") or ""),
                                                  detail={"bars": len(feed.get("bars") or ())}),),
                            reason=str(feed.get("reason") or ""))
        # `market_details()` 也是 HTTP（orderBookDetails）→ 同样 off-loop。
        details = await asyncio.to_thread(self.market.market_details)
        info = details.get(symbol)
        return self.lifecycle.decide(
            symbol, feed["bars"], ind=feed["ind"], i=len(feed["bars"]) - 1,
            signal=feed["signal"], equity=self._equity, market_info=info,
            risk_state=risk_state)

    @staticmethod
    def _apply_entry_guard(decision: Decision, *, blocks: Sequence[str] = ()
                           ) -> Tuple[Decision, str]:
        """阻断开仓（对账/熔断）→ 把意图**降级成显式 skip**，并带上结构化原因。

        只拦 `action='entry'`：`exit`/`stop` 永远放行（熔断不能锁死亏损仓）。
        返回 `(降级后的决策, 生效的阻断码)`，没被拦时阻断码是空串。
        """
        if decision.action != ACTION_ENTRY or not blocks:
            return decision, ""
        code = str(blocks[0])
        guarded = replace(
            decision, action=ACTION_SKIP, to_state=decision.from_state,
            rejections=decision.rejections + (
                Rejection(stage=EXECUTOR_STAGE, code=code,
                          message=f"entry blocked by the executor ({code}); fail-closed",
                          detail={"original_action": ACTION_ENTRY,
                                  "blocks": list(blocks),
                                  "bar_ms": decision.bar_ms}),),
            reason=f"entry blocked by the executor ({code})",
            detail={**dict(decision.detail), "blocked": True, "blocks": list(blocks)})
        return guarded, code

    # ---------------------------------------------------------------- 执行

    async def _execute(self, symbol: str, decision: Decision, feed: Mapping[str, Any],
                       account: ExchangeAccount) -> SymbolOutcome:
        if decision.action == ACTION_ENTRY:
            return await self._execute_entry(decision, feed, account)
        if decision.action in (ACTION_EXIT, ACTION_STOP):
            return await self._execute_exit(decision, feed, account)
        return SymbolOutcome(symbol=symbol, action=decision.action,
                             bar_ms=decision.bar_ms, reason=decision.reason_text,
                             decision=decision.as_dict())

    async def _execute_entry(self, decision: Decision, feed: Mapping[str, Any],
                             account: ExchangeAccount) -> SymbolOutcome:
        plan = decision.plan
        if plan is None:                                          # pragma: no cover - 契约保护
            raise ExecutorError(f"{decision.symbol}: entry intent without an OrderPlan")
        bar_ms = int(decision.bar_ms or 0)
        symbol = str(plan.symbol)
        entry_coi = client_order_index(symbol, bar_ms, "entry")
        stop_coi = client_order_index(symbol, bar_ms, "stop")

        if self.margin_isolated:
            # 开仓前把市场切到逐仓 + 目标杠杆（幂等；被拒 = 资金路径设置失败，
            # **不发单**直接拒绝本轮意图 —— 不允许在未知保证金模式下开仓）。
            mode = await self._place(self.exchange.ensure_isolated(
                market_id=int(plan.market_id), leverage=float(plan.leverage or 5.0)),
                label=f"{symbol} margin mode")
            if not self._accepted(mode):
                return self._reject(symbol, decision, mode, label="margin mode")

        order = await self._place(self.exchange.place_entry(plan, bar_ms=bar_ms),
                                  label=f"{symbol} entry")
        if not self._accepted(order):
            return self._reject(symbol, decision, order, label="entry")

        stop = await self._place(self.exchange.place_stop(plan, bar_ms=bar_ms),
                                 label=f"{symbol} stop")
        stop_ok = self._accepted(stop)
        if not stop_ok:
            # 仓位已经开出来了但保护单没挂上：**照样记账**（真实敞口必须留痕），
            # 然后报警 + 本轮失败。下一轮对账会报 `stop_order_missing` 并阻断新开仓。
            self._event("stop_order_failed", f"{symbol}: entry filled but the stop was rejected",
                        symbol=symbol, data={"kind": stop.kind, "code": stop.code,
                                             "error": stop.error, "tx_hash": stop.tx_hash})
            self._alert("stop_order_failed", f"{symbol} 开仓成功但止损单挂单失败：{stop.error}")
        transition = self.lifecycle.apply_entry(
            decision, order_id=f"coi:{entry_coi}",
            stop_order_id=(f"coi:{stop_coi}" if stop_ok else None),
            equity=self._equity, ts=float(self.clock()))
        if stop_ok and transition.position_id:
            # 记下这张止损单的**到期时刻**：交易所到期就废单，止损守护据此临期换单。
            # 记不上不影响交易（只是守护少一个判据，缺失时会按「无到期信息」跳过换单）。
            expiry_ms = stop.order_expiry_ms or 0
            try:
                self.store.set_stop_order(int(transition.position_id),
                                          stop_order_id=f"coi:{stop_coi}",
                                          stop_expiry_ts=(expiry_ms / 1000.0) if expiry_ms else None,
                                          ts=float(self.clock()))
            except Exception as exc:                              # noqa: BLE001
                logger.warning("could not persist the stop expiry: %s", exc)
        self._event("entry", f"{symbol} opened {plan.base_qty:g} @ {plan.entry_price:g} "
                             f"(stop {plan.stop_price:g})", symbol=symbol,
                    data={"bar_ms": bar_ms, "qty_int": int(plan.qty_int),
                          "entry_coi": entry_coi, "stop_coi": stop_coi,
                          "notional": float(plan.notional),
                          "dry_run": bool(self.dry_run),
                          "position_id": transition.position_id})
        outcome = SymbolOutcome(
            symbol=symbol, action=ACTION_ENTRY, bar_ms=bar_ms,
            reason=f"opened {plan.base_qty:g} @ {plan.entry_price:g}",
            decision=decision.as_dict(), order=order.as_dict(),
            stop_order=stop.as_dict(), filled=True, ok=stop_ok,
            detail={"position_id": transition.position_id, "entry_coi": entry_coi,
                    "stop_coi": stop_coi, "stop_ok": stop_ok})
        if not stop_ok:
            self._failures += 1
            self._maybe_open_circuit()
        return outcome

    async def _execute_exit(self, decision: Decision, feed: Mapping[str, Any],
                            account: ExchangeAccount) -> SymbolOutcome:
        pid = int(decision.position_id or 0)
        row = self.store.position(pid) if pid else None
        if row is None:
            raise ExecutorError(f"{decision.symbol}: exit intent without a live position row")
        symbol = str(row.get("symbol") or decision.symbol)
        kind = "stop" if decision.action == ACTION_STOP else "exit"
        price_decimals = int(row.get("price_decimals") or 0)
        bar_ms = int(decision.bar_ms or 0)
        if kind == "stop" and row.get("stop_price_int"):
            exit_price_int = int(row["stop_price_int"])
        else:
            exit_price_int = to_int_price(float(decision.exit_price or 0.0), price_decimals)
            if exit_price_int <= 0:
                raise ExecutorError(f"{symbol}: exit price {decision.exit_price!r} is unusable")

        # 止损成交自证（模块 docstring 里那个"唯一例外"）：交易所有该标的的仓已经平了，
        # 且我们挂的止损单也不在了 → 这不是"修正状态"，而是承认已发生的事实。
        if kind == "stop" and self._stop_fill_evidence(row, account):
            transition = self.lifecycle.complete_close(
                pid, exit_price_int=exit_price_int, kind="stop", reason="stop_fill",
                bar_ms=bar_ms, ts=float(self.clock()))
            self._event("stop_fill_settled",
                        f"{symbol}: exchange is flat and the stop order is gone — "
                        f"settled as a stop fill", symbol=symbol,
                        data={"position_id": pid, "exit_price_int": exit_price_int,
                              "evidence": "flat_and_stop_gone"})
            return SymbolOutcome(
                symbol=symbol, action=ACTION_STOP, bar_ms=bar_ms,
                reason="stop fill confirmed by the exchange read — settled",
                decision=decision.as_dict(), filled=True, settled_stop_fill=True,
                detail={"position_id": pid, "exit_price_int": exit_price_int})

        if str(row.get("state")).upper() == OPEN:
            self.lifecycle.begin_close(pid, reason=kind, bar_ms=bar_ms,
                                       ts=float(self.clock()))
        order = await self._place(
            self.exchange.place_exit(symbol=symbol, market_id=int(row.get("market_id") or 0),
                                     side=int(row.get("side") or 0),
                                     qty_int=int(row.get("qty_int") or 0),
                                     price_decimals=price_decimals,
                                     exit_price=float(decision.exit_price or 0.0),
                                     bar_ms=bar_ms),
            label=f"{symbol} {kind}")
        if not self._accepted(order):
            # 离场被拒 → 显式回退 CLOSING → OPEN（绝不静默把仓位当平掉）
            self.lifecycle.abort_close(pid, reason=f"{kind}_rejected", ts=float(self.clock()))
            return self._reject(symbol, decision, order, label=kind, position_id=pid)
        transition = self.lifecycle.complete_close(
            pid, exit_price_int=exit_price_int, kind=kind, reason=kind, bar_ms=bar_ms,
            ts=float(self.clock()))
        self._event(kind, f"{symbol} closed ({kind}) @ {decision.exit_price}",
                    symbol=symbol, data={"position_id": pid, "bar_ms": bar_ms,
                                         "exit_price_int": exit_price_int,
                                         "dry_run": bool(self.dry_run)})
        return SymbolOutcome(symbol=symbol, action=decision.action, bar_ms=bar_ms,
                             reason=f"{kind} filled", decision=decision.as_dict(),
                             order=order.as_dict(), filled=True,
                             detail={"position_id": pid,
                                     "exit_price_int": exit_price_int})

    def _stop_fill_evidence(self, row: Mapping[str, Any], account: ExchangeAccount) -> bool:
        """止损成交的**正面证据**：该标的在交易所已无仓，且我们挂的止损单也不在了。

        两个条件缺一不可，且挂单列表必须是**读到了**的（`orders_error` 非空时无法证明
        "单子不在了"→ 不给证据 → 不做任何自动结案）。
        """
        if account.orders_error:
            return False
        symbol = str(row.get("symbol") or "").strip().upper()
        if account.position_for(symbol) is not None:
            return False
        if expected_stop_index(row) is None:
            return False
        order, _ = match_stop_order(row, account.orders,
                                    tolerance_ticks=self.stop_tolerance_ticks)
        return order is None

    async def _place(self, awaitable: Awaitable[OrderResult], *, label: str) -> OrderResult:
        """发单 + 确认。`wait_for_tx` 只在"已接受但未确认"时才调用。"""
        result = await awaitable
        if result.dry_run or not result.ok or result.confirmed:
            return result
        if not result.tx_hash:
            return replace(result, ok=False, kind="unconfirmed",
                           error=f"{label}: accepted without a tx hash — outcome unknown")
        verdict = await self.exchange.wait_for_tx(result.tx_hash)
        confirmed = bool(verdict.get("confirmed"))
        ok = bool(result.ok and verdict.get("ok"))
        return replace(result, ok=ok, confirmed=confirmed,
                       error=("" if ok else
                              f"{label}: {verdict.get('code')}: {verdict.get('error')}"))

    def _accepted(self, result: OrderResult) -> bool:
        if not result.ok:
            return False
        if result.dry_run:
            return True
        return bool(result.confirmed) if self.require_confirmation else True

    def _reject(self, symbol: str, decision: Decision, result: OrderResult, *, label: str,
                position_id: Optional[int] = None) -> SymbolOutcome:
        failure = f"{label} rejected: {result.kind}: {result.error}"
        self._failures += 1
        self._event("order_rejected", f"{symbol} {failure}", symbol=symbol,
                    data={"kind": result.kind, "code": result.code, "error": result.error,
                          "action": result.action, "tx_hash": result.tx_hash,
                          "position_id": position_id})
        self._alert("order_rejected", f"{symbol} {failure}")
        self._maybe_open_circuit()
        return SymbolOutcome(symbol=symbol, action=decision.action,
                             bar_ms=decision.bar_ms, reason=failure,
                             decision=decision.as_dict(), order=result.as_dict(), ok=False,
                             detail={"position_id": position_id})

    # ---------------------------------------------------------------- 对账入口

    def reconcile(self, account: Optional[ExchangeAccount] = None) -> ReconcileReport:
        """读本地活仓 × 交易所快照 → 报告（**不写库、不改状态**）。"""
        snap = account if account is not None else self.last_account
        if snap is None:
            raise ExecutorError("reconcile needs an exchange snapshot (call read() first)")
        return reconcile(local_positions=self.store.open_positions(), account=snap,
                         now=float(self.clock()),
                         stop_tolerance_ticks=self.stop_tolerance_ticks)

    # ---------------------------------------------------------------- 止损守护
    #
    # 为什么需要（2026-10-29 线上事实）：Lighter 的挂单有 `orderExpiry`（我们下 28 天，
    # 交易所上限 30 天），**到期由交易所自动废单**；而在此之前整个仓库只有「开仓那一刻」
    # 会挂止损（`place_stop` 只有一个调用点），对账只会报 `stop_order_missing` 并阻断开仓。
    # ⇒ 持仓一旦跨过到期时刻就是**裸奔且没有通知**。这一节只做两件事，都在保护侧：
    #   1) 交易所没有止损（但仓还在）→ 按原止损价重挂，数量取本地仓位量；
    #   2) 止损快到期（≤ STOP_RENEW_BEFORE_S）→ 先挂新单、确认在位、再撤旧单。
    # 绝不动仓位、不改信号、不触离场；dry-run 下整段跳过。

    #: 距到期多久开始换单。24h 给「连续两个 tick 失败」留出重试余量（tick 30 分钟一轮）。
    STOP_RENEW_BEFORE_S: float = 24 * 3600.0
    #: 挂完新单后等它出现在 activeOrders 的轮询（实测传播 0.8~1.5s，上限给到 ~6s）
    STOP_VERIFY_ATTEMPTS: int = 4
    STOP_VERIFY_INTERVAL_S: float = 1.5

    def _stop_plan_from_row(self, row: Mapping[str, Any]) -> OrderPlan:
        """用**仓位行**拼一个只够挂止损的 `OrderPlan`（字段全部来自建行时的冻结值）。"""
        price_dec = int(row.get("price_decimals") or 0)
        size_dec = int(row.get("size_decimals") or 0)
        stop_int = int(row.get("stop_price_int") or 0)
        qty_int = int(row.get("qty_int") or 0)
        return OrderPlan(
            symbol=str(row.get("symbol") or "").upper(),
            market_id=int(row.get("market_id") or 0),
            side=int(row.get("side") or 0),
            entry_price=float(row.get("entry_price") or 0.0),
            stop_price=stop_int / (10.0 ** price_dec) if price_dec >= 0 else 0.0,
            stop_price_int=stop_int,
            qty_int=qty_int,
            base_qty=qty_int / (10.0 ** size_dec) if size_dec >= 0 else 0.0,
            notional=0.0, margin=0.0, leverage=0.0, exchange_max_leverage=0.0,
            risk_amount=0.0, risk_amount_target=0.0, stop_dist=0.0,
            price_decimals=price_dec, size_decimals=size_dec,
            equity=float(self._equity or 0.0))

    async def _wait_stop_visible(self, coi: int) -> bool:
        """等新挂的止损出现在交易所活挂单里。拿不到读数器能力时按「已受理」算通过。"""
        fn = getattr(self.reader, "active_orders", None)
        if fn is None:
            return True
        for _ in range(self.STOP_VERIFY_ATTEMPTS):
            try:
                orders = await fn()
            except Exception as exc:                              # noqa: BLE001 - 读不到继续等
                logger.warning("stop guard: reading active orders failed: %s", exc)
                orders = ()
            if any(getattr(o, "client_order_index", None) == coi for o in orders or ()):
                return True
            await asyncio.sleep(self.STOP_VERIFY_INTERVAL_S)
        return False

    async def _replace_stop(self, row: Mapping[str, Any], *, reason: str,
                            old: Optional[ExchangeOrder] = None
                            ) -> Optional[Tuple[ExchangeOrder, Dict[str, Any]]]:
        """挂一张新的止损单（可选：换掉 `old`）。返回 `(新单快照, 面板用 dict)`，失败 None。"""
        symbol = str(row.get("symbol") or "").upper()
        try:
            plan = self._stop_plan_from_row(row)
        except Exception as exc:                                  # noqa: BLE001
            self._event("stop_guard_failed", f"{symbol}: 无法从仓位行拼出止损计划：{exc}",
                        symbol=symbol, data={"reason": reason, "error": str(exc)})
            return None
        coi = client_order_index(symbol, int(self._now_ms()), f"stop{reason}")
        try:
            res = await self._place(self.exchange.place_stop(plan, client_order_index=coi),
                                    label=f"{symbol} stop({reason})")
        except Exception as exc:                                  # noqa: BLE001 - 下单异常也要留痕
            self._event("stop_guard_failed",
                        f"{symbol}: 补挂止损失败（{reason}）：{type(exc).__name__}: {exc}",
                        symbol=symbol, data={"reason": reason, "coi": coi,
                                             "error": f"{type(exc).__name__}: {exc}"})
            self._alert("stop_guard_failed", f"{symbol} 止损补挂/换单失败：{exc}")
            return None
        if not self._accepted(res):
            self._event("stop_guard_failed",
                        f"{symbol}: 止损单未被受理（{reason}）：{res.error or res.kind}",
                        symbol=symbol, data={"reason": reason, "coi": coi,
                                             "code": res.code, "error": res.error,
                                             "kind": res.kind})
            self._alert("stop_guard_failed", f"{symbol} 止损单未被交易所受理：{res.error or res.kind}")
            return None

        visible = await self._wait_stop_visible(coi)
        if old is not None and not visible:
            # 换单的**红线**：新单没确认在位就绝不撤旧单（否则可能两头落空 = 裸奔）。
            self._event("stop_guard_unverified",
                        f"{symbol}: 新止损单已受理但未在活挂单里看到，保留旧单等待下一轮",
                        symbol=symbol, data={"reason": reason, "coi": coi,
                                             "old_coi": old.client_order_index})
            return None

        expiry_ms = res.order_expiry_ms or 0
        expiry_ts = (expiry_ms / 1000.0) if expiry_ms else None
        try:
            self.store.set_stop_order(int(row.get("id") or 0),
                                      stop_order_id=f"coi:{coi}",
                                      stop_expiry_ts=expiry_ts, ts=float(self.clock()))
        except Exception as exc:                                  # noqa: BLE001 - 记账失败不影响挂单
            logger.warning("stop guard: could not persist the new stop coi: %s", exc)

        cancelled = ""
        if old is not None and str(old.order_id or "").isdigit():
            try:
                cres = await self.exchange.cancel_order(
                    market_id=int(plan.market_id), order_index=int(old.order_id))
                if not self._accepted(cres):
                    cancelled = cres.error or cres.kind or "rejected"
            except Exception as exc:                              # noqa: BLE001
                cancelled = f"{type(exc).__name__}: {exc}"
            if cancelled:
                # 两张 reduce-only 止损并存不危险（多的一张没有仓位可减），但不干净 → 留痕。
                self._event("stop_cancel_failed",
                            f"{symbol}: 旧止损单撤销失败（{cancelled}）—— 现在有两张止损单",
                            symbol=symbol, data={"old_order_index": old.order_id,
                                                 "coi": coi, "error": cancelled})

        new_order = ExchangeOrder(
            order_id=str(old.order_id) if old is not None else "",
            client_order_index=coi, market_id=plan.market_id, symbol=symbol,
            reduce_only=True, trigger_price_int=plan.stop_price_int,
            price_int=res.price_int, qty_int=plan.qty_int, status="pending")
        guard = {"symbol": symbol, "market_id": plan.market_id, "client_order_index": coi,
                 "trigger_price_int": plan.stop_price_int, "price_int": res.price_int,
                 "qty_int": plan.qty_int, "reduce_only": True, "order_id": "",
                 "status": "pending", "pending_confirm": True,
                 # 换单时旧单已撤（或撤失败）：面板缓存里要把它从「还在挂着」里拿掉
                 "replaced_coi": old.client_order_index if old is not None else None}
        what = "补挂" if old is None else "换单"
        self._event("stop_guard",
                    f"{symbol}: 止损{what}完成（{reason}，coi={coi}"
                    + (f"，撤旧单失败：{cancelled}" if cancelled else "") + "）",
                    symbol=symbol, data={"reason": reason, "coi": coi,
                                         "old_coi": None if old is None else old.client_order_index,
                                         "stop_price_int": plan.stop_price_int,
                                         "qty_int": plan.qty_int,
                                         "expiry_ts": expiry_ts, "cancel_error": cancelled})
        self._alert("stop_guard", f"{symbol} 止损{what}完成（{reason}）")
        return new_order, guard

    async def _guard_margins(self, account: ExchangeAccount) -> None:
        """逐仓抵押守护：`allocated_margin` 低于 名义/杠杆 时补划差额（只补不撤）。

        交易所开仓时已自动划入 ≈名义/杠杆、平仓自动释放（2026-10-29 实测），正常这里
        什么都不做；2% 容差内的偏低不补（免得每次 tick 都为舍入差发一笔 tx）。
        `allocated_margin` 读不到（旧数据/字段缺失）→ 跳过不猜；补划被拒 → 事件 + 报警，
        **不平仓**（止损已在位，强平口径本就是逐仓）。
        """
        if not self.margin_isolated or self.dry_run:
            return
        leverage = float(getattr(self.lifecycle.sizing_config, "leverage", 0.0) or 5.0)
        if leverage <= 0:
            leverage = 5.0
        live = {str(p.symbol): p for p in account.positions}
        for row in self.store.open_positions():
            symbol = str(row.get("symbol") or "").upper()
            pos = live.get(symbol)
            if pos is None or not pos.position_value or pos.allocated_margin is None:
                continue                                   # 没仓/没读数：不猜
            required = float(pos.position_value) / leverage
            shortfall = required - float(pos.allocated_margin)
            if shortfall <= required * 0.02:
                continue
            try:
                res = await self._place(self.exchange.allocate_margin(
                    market_id=int(pos.market_id or row.get("market_id") or 0),
                    usdc_amount=shortfall), label=f"{symbol} margin topup")
            except Exception as exc:                       # noqa: BLE001 - 留痕不中断
                self._event("margin_topup_failed", f"{symbol}: {exc}", symbol=symbol,
                            data={"error": str(exc)})
                self._alert("margin_topup_failed", f"{symbol} 逐仓抵押补划失败：{exc}")
                continue
            if self._accepted(res):
                self._event("margin_topup", f"{symbol} 补划抵押 {shortfall:.4f} USDC",
                            symbol=symbol,
                            data={"shortfall": round(shortfall, 6),
                                  "allocated": float(pos.allocated_margin),
                                  "required": round(required, 6)})
            else:
                self._event("margin_topup_failed", f"{symbol}: topup rejected",
                            symbol=symbol, data={"code": res.code, "error": res.error})
                self._alert("margin_topup_failed", f"{symbol} 逐仓抵押补划失败：{res.error}")

    async def _guard_stops(self, account: ExchangeAccount
                           ) -> Tuple[ExchangeAccount, List[Dict[str, Any]]]:
        """补保护：缺失 → 补挂；临期 → 换单。返回（更新后的读数, 新挂单列表）。"""
        guards: List[Dict[str, Any]] = []
        if account.orders_error:
            # 挂单读不到 ⇒ 不能断定「没有止损」（fail-closed：什么都不做，交给对账报告）
            return account, guards
        if not account.positions:
            return account, guards
        now_s = float(self.clock())
        orders = list(account.orders)
        for row in self.store.open_positions():
            if str(row.get("state") or "").upper() != OPEN:
                continue                       # CLOSING：正在离场，别动它的保护单
            symbol = str(row.get("symbol") or "").upper()
            pos = account.position_for(symbol)
            if pos is None:
                continue                       # 交易所已无仓：对账/结算负责
            # 方向/数量不符时**先别挂**：拿一行的 qty/stop 去挂一张数量不对的保护单没有意义，
            # 交给对账（它会报 blocker 并阻断开仓），修好再补。
            if int(pos.side) != int(row.get("side") or 0):
                continue
            expected_qty = (Decimal(int(row.get("qty_int") or 0))
                            .scaleb(-int(row.get("size_decimals") or 0)))
            if pos.qty != expected_qty:
                continue
            if not int(row.get("stop_price_int") or 0) or not int(row.get("qty_int") or 0):
                continue                       # 缺止损价/数量的行没法定量，跳过
            stop_order, _ = match_stop_order(row, orders,
                                             tolerance_ticks=self.stop_tolerance_ticks)
            if stop_order is None:
                placed = await self._replace_stop(row, reason="missing")
            else:
                expiry = _finite_or_none(row.get("stop_expiry_ts"))
                if expiry is None and stop_order.order_expiry_ms:
                    # v4 之前建的仓没有这一列：用交易所返回的 order_expiry 回填（一次性自愈），
                    # 否则这一仓永远不会被判定成「临期」。
                    expiry = stop_order.order_expiry_ms / 1000.0
                    try:
                        self.store.set_stop_order(
                            int(row.get("id") or 0),
                            stop_order_id=(str(row.get("stop_order_id") or "")
                                           or f"coi:{stop_order.client_order_index}"),
                            stop_expiry_ts=expiry, ts=float(self.clock()))
                    except Exception as exc:                  # noqa: BLE001 - 回填失败不影响本轮判定
                        logger.warning("stop guard: could not backfill the stop expiry: %s", exc)
                if expiry is None or expiry - now_s > self.STOP_RENEW_BEFORE_S:
                    continue
                placed = await self._replace_stop(row, reason="expiring", old=stop_order)
            if placed is None:
                continue
            new_order, guard = placed
            orders.append(new_order)
            guards.append(guard)
        if guards:
            account = replace(account, orders=tuple(orders))
        return account, guards


    @property
    def blocking(self) -> bool:
        """最近一次读到的快照是否处于"阻断开仓"状态（面板状态条用）。"""
        if self._circuit_open:
            return True
        if self.last_account is None:
            return True
        report = self.reconcile(self.last_account)
        return report.has_blockers and not self.dry_run

    def _report_reconcile(self, report: ReconcileReport) -> None:
        """把不一致落 `events` + 报警。**只读**：没有任何 store 写仓位的方法调用。"""
        for issue in report.issues:
            self._event("reconcile_mismatch", str(issue), symbol=issue.symbol or None,
                        data=issue.as_dict())
        self._alert("reconcile_mismatch",
                    f"对账发现 {len(report.issues)} 处不一致"
                    f"（{sum(1 for i in report.issues if i.blocker)} 处阻断开仓）："
                    + "；".join(str(i) for i in report.blockers[:5]))

    # ---------------------------------------------------------------- 失败 / 熔断

    def _note_failure(self, where: str, exc: BaseException) -> str:
        """记一次硬失败（落 events + 计数 + 可能熔断），返回一句描述。"""
        failure = f"{where}: {type(exc).__name__}: {exc}"
        self._failures += 1
        self._event("executor_error", failure,
                    data={"where": where, "error": failure,
                          "failures": self._failures,
                          "circuit_open": self._circuit_open})
        self._alert("executor_error", failure)
        self._maybe_open_circuit()
        return failure

    def _maybe_open_circuit(self) -> None:
        if self._failures >= self.max_consecutive_failures and not self._circuit_open:
            self._circuit_open = True
            self._event("circuit_open",
                        f"circuit opened after {self._failures} consecutive failure(s) — "
                        f"no new entries until reset", 
                        data={"failures": self._failures})
            self._alert("circuit_open",
                        f"熔断：连续 {self._failures} 次失败，已停止开新仓")

    # ---------------------------------------------------------------- 副作用封装

    def _event(self, kind: str, message: str, *, symbol: Optional[str] = None,
               data: Optional[Mapping[str, Any]] = None) -> None:
        """落 `events` 表（T5）+ 同步打日志。DB 写失败**不**吞：抛出去让本轮停下。"""
        logger.info("[%s] %s", kind, message)
        self.store.log_event(kind, message, symbol=symbol, data=dict(data or {}))

    def _alert(self, kind: str, text: str) -> None:
        """推送（T9/T12 注入；默认只打日志）。推送失败**必须**留痕，不能静默。"""
        if self.alert is None:
            logger.warning("[alert:%s] %s", kind, text)
            return
        try:
            self.alert(kind, text)
        except Exception as exc:                                  # noqa: BLE001 - 推送失败不致命
            logger.exception("alert hook failed for %s", kind)
            try:
                self.store.log_event("alert_failed", f"{kind}: {type(exc).__name__}: {exc}",
                                     data={"kind": kind, "text": text,
                                           "error": f"{type(exc).__name__}: {exc}"})
            except Exception:                                     # noqa: BLE001 - 最后一道
                logger.exception("could not persist the alert failure either")
