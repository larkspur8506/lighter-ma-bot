"""仓位计算：风险定仓 + 杠杆上限 + Lighter 整数化精度（**纯函数，不碰网络**）。

职责边界
--------
输入：`SizingRequest`（symbol / 方向 / 入场参考价 / 止损价 / 权益）+ 该标的的
      `MarketInfo`（市场规则）+ `SizingConfig`（风险比例、杠杆上限）。
输出：`SizingDecision` —— 要么一个 `OrderPlan`（下多少、止损挂什么价、多大杠杆），
      要么一个 `Rejection`（**结构化**的 `code` + `message` + `detail`，给 T10 面板和
      日志用）。永远不抛「网络/配置」类异常给调用方去猜：可预期的拒单走返回值，
      只有**编程错误**（`SizingConfig` 本身非法）才在构造时 fail-loud。

本模块**不 import 任何网络模块**（`urllib` / `requests` / `MarketClient` 一概没有；
`MarketInfo` 只在 `TYPE_CHECKING` 下作为类型引用）。因此它可以被无网络的单测直接驱动，
也可以被回测脚本复用。

口径（与旧系统 `larkspur8506/crypto-ma-alert` 一致，只换市场规则）
------------------------------------------------------------------
1. **风险定仓**：`风险金额 = 权益 × risk_pct`（默认 1.33%，旧系统实盘口径）
   仓位数量（base）= `风险金额 ÷ |入场价 − 止损价|`
   名义 = `仓位数量 × 入场价`
   —— 杠杆**不参与**仓位大小的计算，它只决定占用保证金和强平价。旧系统在 MEXC
   把这一条写在 `sizing.py` 的模块 docstring 里，这里原样继承。
2. **杠杆上限**：`名义 ÷ 杠杆 ≤ 权益` 必须成立，否则拒单（`insufficient_margin`）。
   杠杆取 `min(用户配置, 交易所上限)`；交易所上限 = `10000 / min_initial_margin_fraction`
   （实测 BTC/ETH mif=200 → 50x，SOL mif=400 → 25x）。交易所上限更高时按用户的
   配置走（默认 5x，与旧系统回测口径一致），配置高于交易所上限时**降级到交易所上限
   并记 warning**（不是静默无视配置）。
3. **精度**：Lighter 的数量/价格都是整数，按 `10^size_decimals` / `10^price_decimals`
   放大。数量**一律向下取整（floor）** —— 宁可少买一点，也不许多买（多买会让实际风险
   超过 `risk_pct`，而且 floor 之后仍然满足「名义 ≤ 权益×杠杆」）。
   止损价按「**朝入场价方向**取整」：多单止损 ceil、空单止损 floor，即取整后止损距离
   **只会变小不会变大**，实际风险 ≤ 目标风险。
4. **最小下单量**：`min_base_amount`（base 数量下限）、`min_quote_amount`（名义下限，
   Lighter 实测 10 USDC）。低于任何一个 → 拒单，绝不产生 0 数量或低于交易所门槛的单。
5. **fail-closed**：任何一个输入缺失/非法（权益 ≤ 0、市场冻结、`mif` 缺失、精度非法、
   未注册标的……）都**拒单**，不猜、不降级、不下单。

`Config.risk_pct` 是比例（0–1），不是百分数：`RISK_PCT=0.0133` 表示 1.33%。
本模块自己的默认值 `DEFAULT_RISK_PCT = 0.0133` 只在**没有**传 config 时生效；
运行时应当用 `SizingConfig.from_config(Config.from_env())`（env `RISK_PCT` 为准）。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from app import symbol_registry

if TYPE_CHECKING:                                            # pragma: no cover
    from app.market import MarketInfo

__all__ = [
    "DEFAULT_LEVERAGE", "DEFAULT_RISK_PCT", "LEVERAGE_KEY", "RISK_PCT_KEY",
    "REJECT_BAD_INPUT", "REJECT_BELOW_MIN_BASE", "REJECT_BELOW_MIN_QUOTE",
    "REJECT_INSUFFICIENT_MARGIN", "REJECT_MARKET_NOT_TRADEABLE",
    "REJECT_MISSING_MARKET_RULES", "REJECT_QTY_BELOW_TICK", "REJECT_ROUND_TRIP",
    "REJECT_STOP_ON_WRONG_SIDE", "REJECT_SYMBOL_NOT_REGISTERED", "REJECT_CODES",
    "OrderPlan", "Rejection", "SizingConfig", "SizingDecision", "SizingRequest",
    "align_stop_price", "from_int_qty", "plan_order", "round_trip_check",
    "target_notional", "to_int_base_qty", "to_int_price", "to_int_qty",
]

# ------------------------------------------------------------------ 默认值

DEFAULT_RISK_PCT = 0.0133          # 1.33%：旧系统（HL 现役）实盘口径
DEFAULT_LEVERAGE = 5.0             # 逐仓 5x：与 700 笔回测口径一致；**不是**交易所上限
RISK_PCT_KEY = "RISK_PCT"          # 与 app/config.py 同名（比例，0–1）
LEVERAGE_KEY = "LIGHTER_LEVERAGE"  # 本模块新增：用户侧的杠杆上限

# 精度上限：Lighter 实测 0–5。给一个上限是为了挡住「把 1e9 当 decimals」这类坏元数据
# （Decimal 10**1e9 会把内存吃光，等于一个 OOM 的下单路径）。
_MAX_DECIMALS = 8
_MAX_LEVERAGE = 100.0              # 交易所上限是 50x；100x 只作为「明显填错了」的护栏

# ------------------------------------------------------------------ 拒单码
# 每个码都是**稳定字符串**：面板/告警/测试按码判断，不解析 message（文案会改，码不会）。

REJECT_SYMBOL_NOT_REGISTERED = "symbol_not_registered"   # 不在 symbol_registry
REJECT_MARKET_NOT_TRADEABLE = "market_not_tradeable"      # 冻结 / 非 active
REJECT_MISSING_MARKET_RULES = "missing_market_rules"      # mif/decimals 缺失或非法
REJECT_BAD_INPUT = "bad_input"                            # 权益/价格/方向 非法
REJECT_STOP_ON_WRONG_SIDE = "stop_on_wrong_side"          # 多单止损不低于入场价（或反之）
REJECT_QTY_BELOW_TICK = "qty_below_tick"                  # 精度取整后数量 = 0
REJECT_BELOW_MIN_BASE = "below_min_base"                  # < min_base_amount
REJECT_BELOW_MIN_QUOTE = "below_min_quote"                # < min_quote_amount（名义下限）
REJECT_INSUFFICIENT_MARGIN = "insufficient_margin"        # 名义/杠杆 > 权益
REJECT_ROUND_TRIP = "round_trip_mismatch"                 # 反算名义偏差超容差

REJECT_CODES = (
    REJECT_SYMBOL_NOT_REGISTERED, REJECT_MARKET_NOT_TRADEABLE, REJECT_MISSING_MARKET_RULES,
    REJECT_BAD_INPUT, REJECT_STOP_ON_WRONG_SIDE, REJECT_QTY_BELOW_TICK,
    REJECT_BELOW_MIN_BASE, REJECT_BELOW_MIN_QUOTE, REJECT_INSUFFICIENT_MARGIN,
    REJECT_ROUND_TRIP,
)


# ------------------------------------------------------------------ 配置


@dataclass(frozen=True)
class SizingConfig:
    """用户侧的下单参数（**不是**市场规则；市场规则来自 `MarketInfo`）。

    `risk_pct`：单笔风险占权益的比例（0.0133 = 1.33%）。必须 `0 < p <= 1`。
    `leverage`：用户配置的杠杆上限（默认 5x）。必须 `1 <= lev <= 100`。
                生效值 = `min(leverage, 交易所上限)`。

    构造即校验：非法值**抛 `ValueError`**（fail-loud）。理由：这是部署配置，
    错了就该在启动时报出来，而不是每根 bar 都静默拒单 —— 后者看起来像「策略没信号」。
    """

    risk_pct: float = DEFAULT_RISK_PCT
    leverage: float = DEFAULT_LEVERAGE

    def __post_init__(self) -> None:
        risk = _finite(self.risk_pct)
        if risk is None or not (0.0 < risk <= 1.0):
            raise ValueError(f"risk_pct must be in (0, 1] (a fraction, not a percent): {self.risk_pct!r}")
        lev = _finite(self.leverage)
        if lev is None or not (1.0 <= lev <= _MAX_LEVERAGE):
            raise ValueError(f"leverage must be in [1, {_MAX_LEVERAGE:g}]: {self.leverage!r}")

    # -------------------------------------------------------------- 构造

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SizingConfig":
        """从环境变量构造（`RISK_PCT` / `LIGHTER_LEVERAGE`）。

        缺失用文档化默认值；**填了但不合法就抛 `ValueError`**（不静默回默认 ——
        一个写错的 `RISK_PCT=13.3`（本意 13.3%）变成 0.0133 是灾难，变成 13.3 更灾难，
        所以只能报错让人改）。
        """
        source: Mapping[str, str] = os.environ if env is None else env
        risk = _env_float(source, RISK_PCT_KEY, DEFAULT_RISK_PCT)
        leverage = _env_float(source, LEVERAGE_KEY, DEFAULT_LEVERAGE)
        return cls(risk_pct=risk, leverage=leverage)

    @classmethod
    def from_config(cls, config: Any) -> "SizingConfig":
        """从 `app.config.Config` 构造（用它已经校验过的 `risk_pct`）。

        `leverage` 目前不在 `Config` 里（T3 的字段集），所以这里读 `getattr`，
        缺失时用 `DEFAULT_LEVERAGE`；等 T5/T8 把杠杆做成配置项后自动生效，
        不需要改本模块。
        """
        risk = getattr(config, "risk_pct", None)
        if risk is None:
            raise ValueError("config has no risk_pct — refusing to size without a risk budget")
        return cls(risk_pct=float(risk),
                   leverage=float(getattr(config, "leverage", DEFAULT_LEVERAGE)))


# ------------------------------------------------------------------ 输入/输出


@dataclass(frozen=True)
class SizingRequest:
    """一次定仓请求。价格用 float（信号来自 `app.strategy.Signal`）。"""

    symbol: str
    side: int                 # +1 做多 / -1 做空
    entry_price: float        # 入场参考价（下一根 bar 开盘价 / 当前 mid）
    stop_price: float         # 策略止损价（信号 bar 的 ma_min / ma_max）
    equity: float             # 账户权益（USDC，Classic 模式 collateral）

    @property
    def is_long(self) -> bool:
        return self.side == 1

    @property
    def stop_distance(self) -> float:
        """|入场 − 止损|（绝对价差，正数）。"""
        return abs(float(self.entry_price) - float(self.stop_price))


@dataclass(frozen=True)
class OrderPlan:
    """通过全部检查的下单计划（数量已是**整数化 + floor** 后的最终值）。"""

    symbol: str
    market_id: int
    side: int
    entry_price: float            # 参考价（未取整）
    stop_price: float             # **取整后**的止损价（实际会挂的价）
    stop_price_int: int           # 止损价的整数表示（× 10^price_decimals）
    qty_int: int                  # 下单数量（整数表示，× 10^size_decimals）
    base_qty: float               # qty_int / 10^size_decimals
    notional: float               # base_qty × entry_price
    margin: float                 # notional / leverage
    leverage: float               # 生效杠杆 = min(配置, 交易所上限)
    exchange_max_leverage: float  # 交易所上限 = 10000/mif
    risk_amount: float            # 实际风险 = base_qty × |entry − 取整后止损|
    risk_amount_target: float     # 目标风险 = equity × risk_pct
    stop_dist: float              # |entry − 取整后止损| / entry
    price_decimals: int
    size_decimals: int
    equity: float = 0.0
    warnings: Tuple[str, ...] = ()

    @property
    def is_long(self) -> bool:
        return self.side == 1

    @property
    def risk_pct_actual(self) -> float:
        """实际风险占权益的比例（对应配置里的 `risk_pct`；因为 floor，应略小于目标）。"""
        return (self.risk_amount / self.equity) if self.equity > 0 else 0.0

    @property
    def risk_drift(self) -> float:
        """实际风险相对目标风险的偏差（正数 = 实际更大，应当 ≤ 0）。"""
        if self.risk_amount_target <= 0:
            return 0.0
        return self.risk_amount / self.risk_amount_target - 1.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "market_id": self.market_id, "side": self.side,
            "entry_price": self.entry_price, "stop_price": self.stop_price,
            "stop_price_int": self.stop_price_int, "qty_int": self.qty_int,
            "base_qty": self.base_qty, "notional": self.notional,
            "margin": self.margin, "leverage": self.leverage,
            "exchange_max_leverage": self.exchange_max_leverage,
            "risk_amount": self.risk_amount, "risk_amount_target": self.risk_amount_target,
            "stop_dist": self.stop_dist, "price_decimals": self.price_decimals,
            "size_decimals": self.size_decimals, "equity": self.equity,
            "risk_pct_actual": self.risk_pct_actual,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Rejection:
    """结构化拒单：`code` 给程序判断，`message` 给人看，`detail` 给面板/日志。"""

    code: str
    message: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class SizingDecision:
    """`plan` 与 `rejection` 互斥；`ok` 只有在前者非空时为真。"""

    symbol: str
    plan: Optional[OrderPlan] = None
    rejection: Optional[Rejection] = None

    @property
    def ok(self) -> bool:
        return self.plan is not None and self.rejection is None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ok": self.ok,
            "plan": self.plan.as_dict() if self.plan is not None else None,
            "rejection": self.rejection.as_dict() if self.rejection is not None else None,
        }

    def __str__(self) -> str:
        if self.ok and self.plan is not None:
            p = self.plan
            return (f"{p.symbol} {'long' if p.is_long else 'short'} qty={p.base_qty:g} "
                    f"notional={p.notional:.2f} lev={p.leverage:g} stop={p.stop_price:g}")
        return f"{self.symbol} rejected: {self.rejection}"


# ------------------------------------------------------------------ 纯数学工具


def target_notional(equity: float, risk_pct: float, stop_dist: float) -> float:
    """风险定仓的目标名义：`权益 × risk_pct ÷ 止损距离(比例)`。

    `stop_dist` 必须是**比例**（0.04 = 4%），不是绝对价差 —— 旧系统在 MEXC 版里
    就是 `risk_u / stop_dist_pct`，这里保持一致以免两套口径混用。
    非法输入抛 `ValueError`（这是数学工具，不是判定点；判定在 `plan_order` 里）。
    """
    eq, risk, dist = _finite(equity), _finite(risk_pct), _finite(stop_dist)
    if eq is None or eq <= 0:
        raise ValueError(f"equity must be a positive number: {equity!r}")
    if risk is None or risk <= 0:
        raise ValueError(f"risk_pct must be a positive number: {risk_pct!r}")
    if dist is None or dist <= 0:
        raise ValueError(f"stop_dist must be a positive fraction: {stop_dist!r}")
    return eq * risk / dist


def _scaled(value: float, decimals: int) -> Decimal:
    """`value × 10^decimals` 的**精确**十进制表示。

    用 `Decimal(repr(x))` 而不是 `Decimal(x)`：float 转 Decimal 的二进制展开会把
    `0.03325` 变成 `0.03324999999999999922…`，floor 之后少一个 tick；
    `repr()` 给的是最短往返表示（`0.03325`），正是「人填进去的那个数」。
    """
    return Decimal(repr(float(value))) * (Decimal(10) ** int(decimals))


def to_int_qty(notional: float, price: float, size_decimals: int) -> int:
    """名义 ÷ 价格 → 整数化数量（**floor**）。

    例：`to_int_qty(3325.0, 80000.0, 5)` = `floor(0.0415625 × 1e5)` = 4156。
    数量为 0 是**合法返回值**（由调用方判 `below_min` / `qty_below_tick`）；
    非法输入抛 `ValueError`。
    """
    n, p = _finite(notional), _finite(price)
    if n is None or n < 0:
        raise ValueError(f"notional must be >= 0: {notional!r}")
    if p is None or p <= 0:
        raise ValueError(f"price must be > 0: {price!r}")
    return to_int_base_qty(n / p, size_decimals)


def to_int_base_qty(base_qty: float, size_decimals: int) -> int:
    """base 数量 → 整数表示（**floor**）。`0.000012`@4dec → `0`（不足一个 tick）。"""
    dec = _decimals(size_decimals)
    qty = _finite(base_qty)
    if qty is None or qty < 0:
        raise ValueError(f"base_qty must be >= 0: {base_qty!r}")
    # 加一个极小量再 floor，挡住 `1.9999999999` 这类「本应是整数」的浮点残差
    # （残差量级 ~1e-15，远小于一个 tick，不影响真正的截断语义）。
    return int((_scaled(qty, dec) + Decimal("1e-9")).to_integral_value(rounding=ROUND_FLOOR))


def from_int_qty(qty_int: int, size_decimals: int) -> float:
    """整数表示 → base 数量（float）。`4156`@5dec → `0.04156`。"""
    dec = _decimals(size_decimals)
    if not isinstance(qty_int, int) or qty_int < 0:
        raise ValueError(f"qty_int must be a non-negative int: {qty_int!r}")
    return float(Decimal(int(qty_int)) / (Decimal(10) ** dec))


def to_int_price(price: float, price_decimals: int, *, rounding: str = "floor") -> int:
    """价格 → 整数表示。默认 floor；`rounding="ceil"` 用于多单止损（见 `align_stop_price`）。"""
    dec = _decimals(price_decimals)
    p = _finite(price)
    if p is None or p <= 0:
        raise ValueError(f"price must be > 0: {price!r}")
    mode = ROUND_FLOOR if rounding == "floor" else ROUND_CEILING if rounding == "ceil" else None
    if mode is None:
        raise ValueError(f"rounding must be 'floor' or 'ceil': {rounding!r}")
    scaled = _scaled(p, dec)
    if mode is ROUND_FLOOR:
        return int((scaled + Decimal("1e-9")).to_integral_value(rounding=ROUND_FLOOR))
    return int((scaled - Decimal("1e-9")).to_integral_value(rounding=ROUND_CEILING))


def align_stop_price(entry_price: float, stop_price: float, side: int,
                     price_decimals: int) -> Tuple[float, int]:
    """把止损价对齐到 tick，方向**朝入场价**（多单 ceil / 空单 floor）。

    为什么不是一律 floor：止损取整只会让「止损距离」变得更小或不变，于是
    `实际风险 = 数量 × |入场 − 止损|` 永不超过目标风险（fail-safe 的方向）。
    一律 floor 的话，空单止损会变得更远 —— 风险反而被放大。

    返回 `(对齐后的 float 价格, 整数表示)`。
    """
    dec = _decimals(price_decimals)
    entry, stop = _finite(entry_price), _finite(stop_price)
    if entry is None or entry <= 0 or stop is None or stop <= 0:
        raise ValueError(f"entry/stop must be > 0: entry={entry_price!r} stop={stop_price!r}")
    if side not in (1, -1):
        raise ValueError(f"side must be +1 or -1: {side!r}")
    rounding = "ceil" if side == 1 else "floor"
    price_int = to_int_price(stop, dec, rounding=rounding)
    if price_int <= 0:
        # 止损价小于一个 tick：无法表示 → 交给调用方按 bad_input 拒单。
        raise ValueError(f"stop price {stop!r} is below one tick at {dec} decimals")
    return float(Decimal(price_int) / (Decimal(10) ** dec)), price_int


def round_trip_check(target_notional_value: float, base_qty: float, price: float,
                     size_decimals: int, *, rel_tol: float = 1e-4) -> bool:
    """反算校验：floor 之后的实际名义与目标名义的偏差是否在容差内。

    容差 = `max(一个 size tick 的名义, 目标 × 0.01%)`。
    两个条件同时成立才算通过：
      * 实际名义 **不高于** 目标（floor 只能往下，高于说明算错了）；
      * 偏差 ≤ 容差。
    """
    target, qty, px = _finite(target_notional_value), _finite(base_qty), _finite(price)
    if target is None or qty is None or px is None or target <= 0 or qty < 0 or px <= 0:
        return False
    actual = qty * px
    if actual > target + 1e-9:
        return False
    tick_notional = px * 10.0 ** (-_decimals(size_decimals))
    allowed = max(tick_notional, target * float(rel_tol))
    return (target - actual) <= allowed + 1e-9


# ------------------------------------------------------------------ 主入口


def plan_order(request: SizingRequest, info: "MarketInfo",
               config: Optional[SizingConfig] = None) -> SizingDecision:
    """一次定仓：通过全部约束 → `OrderPlan`；任何一条不满足 → `Rejection`。

    `info` 是该标的的 `MarketInfo`（运行时从 `orderBookDetails` 拿的，或
    `symbol_registry.SymbolSpec.as_market_info()` 的兜底）。本函数只读它的属性，
    因此测试可以传任何鸭子类型对象。
    """
    cfg = config if config is not None else SizingConfig()
    symbol = str(request.symbol or "").strip().upper()

    # ① 显性拦截：未注册标的
    entry = symbol_registry.spec(symbol)
    if entry is None:
        return _reject(symbol, REJECT_SYMBOL_NOT_REGISTERED,
                       f"{symbol or request.symbol!r} is not in the symbol registry "
                       f"(known: {', '.join(symbol_registry.known_symbols())})",
                       {"symbol": symbol, "known": symbol_registry.known_symbols()})

    # ② 输入合法性
    equity = _finite(request.equity)
    entry_price = _finite(request.entry_price)
    stop_price = _finite(request.stop_price)
    bad: List[str] = []
    if equity is None or equity <= 0:
        bad.append(f"equity must be > 0, got {request.equity!r}")
    if entry_price is None or entry_price <= 0:
        bad.append(f"entry_price must be > 0, got {request.entry_price!r}")
    if stop_price is None or stop_price <= 0:
        bad.append(f"stop_price must be > 0, got {request.stop_price!r}")
    if request.side not in (1, -1):
        bad.append(f"side must be +1 or -1, got {request.side!r}")
    if bad:
        return _reject(symbol, REJECT_BAD_INPUT, "; ".join(bad),
                       {"equity": request.equity, "entry_price": request.entry_price,
                        "stop_price": request.stop_price, "side": request.side})
    if equity is None or entry_price is None or stop_price is None:   # pragma: no cover
        # 上面已经把 None 记进 `bad` 并返回；这里只是把「非空」写死给类型检查器看。
        return _reject(symbol, REJECT_BAD_INPUT, "non-finite sizing inputs", {})

    # ③ 市场可交易性 + 规则完整性（fail-closed：规则缺失就不算，不猜）
    if info is None:
        return _reject(symbol, REJECT_MISSING_MARKET_RULES,
                       "no MarketInfo supplied — refusing to size without market rules", {})
    status = str(getattr(info, "status", "") or "").strip().lower()
    if bool(getattr(info, "is_frozen", False)):
        return _reject(symbol, REJECT_MARKET_NOT_TRADEABLE,
                       f"{symbol}: market is frozen", {"status": status})
    if status and status != "active":
        return _reject(symbol, REJECT_MARKET_NOT_TRADEABLE,
                       f"{symbol}: market status is {status!r}, not 'active'",
                       {"status": status})

    price_decimals = getattr(info, "price_decimals", None)
    size_decimals = getattr(info, "size_decimals", None)
    mif = getattr(info, "min_initial_margin_fraction", None)
    min_base = _finite(getattr(info, "min_base_amount", 0.0)) or 0.0
    min_quote = _finite(getattr(info, "min_quote_amount", 0.0)) or 0.0
    try:
        price_decimals = _decimals(price_decimals)
        size_decimals = _decimals(size_decimals)
    except (TypeError, ValueError) as exc:
        return _reject(symbol, REJECT_MISSING_MARKET_RULES,
                       f"{symbol}: unusable decimals ({exc})", {"info": repr(info)})
    mif_value = _finite(mif)
    mif_i = int(mif_value) if mif_value is not None else 0
    if mif_i <= 0:
        return _reject(symbol, REJECT_MISSING_MARKET_RULES,
                       f"{symbol}: min_initial_margin_fraction is missing/≤0 — cannot derive "
                       f"the exchange leverage cap",
                       {"min_initial_margin_fraction": getattr(info, "min_initial_margin_fraction", None)})

    # ④ 方向与止损方向
    if request.side == 1 and not (stop_price < entry_price):
        return _reject(symbol, REJECT_STOP_ON_WRONG_SIDE,
                       f"long requires stop < entry (entry={entry_price:g}, stop={stop_price:g})",
                       {"entry_price": entry_price, "stop_price": stop_price, "side": 1})
    if request.side == -1 and not (stop_price > entry_price):
        return _reject(symbol, REJECT_STOP_ON_WRONG_SIDE,
                       f"short requires stop > entry (entry={entry_price:g}, stop={stop_price:g})",
                       {"entry_price": entry_price, "stop_price": stop_price, "side": -1})

    # ⑤ 止损价先按 tick 对齐（朝入场价取整：多单 ceil / 空单 floor）。
    #    放在定仓之前，是因为「对齐后的止损距离」才是真正会成交的止损距离；
    #    同时能在算数量之前就挡住「止损离入场不足一个 tick」这种非法输入
    #    （否则会先被 insufficient_margin 拦下，报出一个误导性的原因）。
    try:
        stop_aligned, stop_int = align_stop_price(entry_price, stop_price, request.side,
                                                  price_decimals)
    except ValueError as exc:
        return _reject(symbol, REJECT_BAD_INPUT, f"{symbol}: {exc}",
                       {"stop_price": stop_price, "price_decimals": price_decimals})
    stop_dist_abs = abs(entry_price - stop_aligned)
    if stop_dist_abs <= 0:
        return _reject(symbol, REJECT_BAD_INPUT,
                       f"{symbol}: stop {stop_price:g} collapses onto entry "
                       f"{entry_price:g} at price_decimals={price_decimals}",
                       {"stop_price": stop_price, "entry_price": entry_price,
                        "price_decimals": price_decimals})
    if (request.side == 1 and stop_aligned >= entry_price) or \
            (request.side == -1 and stop_aligned <= entry_price):
        return _reject(symbol, REJECT_BAD_INPUT,
                       f"{symbol}: stop {stop_price:g} rounds to {stop_aligned:g}, which is on "
                       f"the wrong side of entry {entry_price:g} at "
                       f"price_decimals={price_decimals}",
                       {"stop_price": stop_price, "stop_aligned": stop_aligned,
                        "entry_price": entry_price, "price_decimals": price_decimals})

    # ⑥ 杠杆：配置 vs 交易所上限
    warnings: List[str] = []
    exchange_max_lev = 10_000.0 / float(mif_i)
    if exchange_max_lev < 1.0:
        return _reject(symbol, REJECT_MISSING_MARKET_RULES,
                       f"{symbol}: min_initial_margin_fraction={mif_i} implies max leverage "
                       f"{exchange_max_lev:g}x < 1 — market rules are unusable",
                       {"min_initial_margin_fraction": mif_i})
    leverage = min(float(cfg.leverage), exchange_max_lev)
    if float(cfg.leverage) > exchange_max_lev:
        warnings.append(
            f"leverage clamped to the exchange cap: configured {cfg.leverage:g}x > "
            f"{exchange_max_lev:g}x (min_initial_margin_fraction={mif_i})"
        )

    # ⑦ 风险定仓 → 名义 → 保证金
    risk_amount_target = equity * cfg.risk_pct
    base_qty_raw = risk_amount_target / stop_dist_abs
    notional_target = base_qty_raw * entry_price
    required_margin = notional_target / leverage

    # ⑧ 权益不足：**拒单**（不静默减仓 —— 减仓等于偷偷改了用户下的风险比例）
    if required_margin > equity + 1e-9:
        return _reject(
            symbol, REJECT_INSUFFICIENT_MARGIN,
            f"{symbol}: notional {notional_target:.2f} needs margin "
            f"{required_margin:.2f} at {leverage:g}x but equity is {equity:.2f} "
            f"(risk {cfg.risk_pct:.4%} over a {stop_dist_abs / entry_price:.4%} stop)",
            {"notional": notional_target, "required_margin": required_margin,
             "equity": equity, "leverage": leverage, "exchange_max_leverage": exchange_max_lev},
        )

    # ⑨ 精度：数量 floor（宁可少买，不可多买）
    qty_int = to_int_base_qty(base_qty_raw, size_decimals)
    if qty_int <= 0:
        return _reject(
            symbol, REJECT_QTY_BELOW_TICK,
            f"{symbol}: {base_qty_raw:.10g} base units truncates to 0 at "
            f"size_decimals={size_decimals}",
            {"base_qty_raw": base_qty_raw, "size_decimals": size_decimals,
             "notional_target": notional_target, "equity": equity},
        )
    base_qty = from_int_qty(qty_int, size_decimals)
    notional = base_qty * entry_price

    # ⑩ 最小下单量（base 与 quote 两条都要过）
    if base_qty < min_base - 1e-15:
        return _reject(
            symbol, REJECT_BELOW_MIN_BASE,
            f"{symbol}: {base_qty:g} < min_base_amount {min_base:g} "
            f"(qty_int={qty_int} at size_decimals={size_decimals})",
            {"base_qty": base_qty, "min_base_amount": min_base, "qty_int": qty_int},
        )
    if min_quote > 0 and notional < min_quote - 1e-9:
        return _reject(
            symbol, REJECT_BELOW_MIN_QUOTE,
            f"{symbol}: notional {notional:.2f} < min_quote_amount {min_quote:.2f}",
            {"notional": notional, "min_quote_amount": min_quote, "base_qty": base_qty},
        )

    # ⑪ 实际风险用**对齐后**的止损距离（永远 ≤ 目标风险）
    risk_amount = base_qty * stop_dist_abs

    # ⑫ 反算校验（floor 造成的偏差必须在一个 tick / 0.01% 之内）
    if not round_trip_check(notional_target, base_qty, entry_price, size_decimals):
        return _reject(
            symbol, REJECT_ROUND_TRIP,
            f"{symbol}: round-trip notional {notional:.6f} deviates from target "
            f"{notional_target:.6f} by more than one tick / 0.01%",
            {"notional": notional, "notional_target": notional_target,
             "base_qty": base_qty, "size_decimals": size_decimals},
        )

    plan = OrderPlan(
        symbol=symbol,
        market_id=_market_id(getattr(info, "market_id", entry.fallback_market_id),
                             entry.fallback_market_id),
        side=int(request.side),
        entry_price=entry_price,
        stop_price=stop_aligned,
        stop_price_int=stop_int,
        qty_int=qty_int,
        base_qty=base_qty,
        notional=notional,
        margin=notional / leverage,
        leverage=leverage,
        exchange_max_leverage=exchange_max_lev,
        risk_amount=risk_amount,
        risk_amount_target=risk_amount_target,
        stop_dist=stop_dist_abs / entry_price,
        price_decimals=price_decimals,
        size_decimals=size_decimals,
        equity=equity,
        warnings=tuple(warnings),
    )
    return SizingDecision(symbol=symbol, plan=plan)


# ------------------------------------------------------------------ 内部工具


def _reject(symbol: str, code: str, message: str,
            detail: Mapping[str, Any]) -> SizingDecision:
    return SizingDecision(symbol=symbol, rejection=Rejection(code=code, message=message,
                                                             detail=detail))


def _market_id(value: Any, fallback: int) -> int:
    """`MarketInfo.market_id` → int；坏值回退注册表兜底值（不让 int() 的异常逃出去）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _finite(value: Any) -> Optional[float]:
    """安全转 float：None/字符串垃圾/NaN/±inf → `None`（**绝不**让 NaN 混进比较）。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _decimals(value: Any) -> int:
    """校验一个小数位数：0..8 的整数。非整数/越界 → `ValueError`（坏元数据）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        f = _finite(value)
        if f is None or f != int(f):
            raise ValueError(f"decimals must be an integer, got {value!r}")
        value = int(f)
    if not (0 <= value <= _MAX_DECIMALS):
        raise ValueError(f"decimals must be in [0, {_MAX_DECIMALS}], got {value!r}")
    return int(value)


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{key} must be a number, got {raw!r}") from None
