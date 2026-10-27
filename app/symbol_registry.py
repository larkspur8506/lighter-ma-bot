"""标的注册表：本系统**只允许**交易这里登记过的标的（纯 stdlib、import 无副作用）。

为什么要一张表
--------------
旧仓库（`larkspur8506/crypto-ma-alert`）的血泪教训：加一个标的要同步改四处元数据
（`market.HL_COIN_NAMES` / `config` 默认 `SYMBOLS` / `sizing.SPECS` / HL 动态规格），
漏任何一处都**静默坏** —— `SPECS` 漏了会让 `size_order(symbol)` 抛 `KeyError`，被 tick 的
per-symbol `try/except` 吞成一句「处理异常」，整个标的的信号一个都出不来，而单测不会红。
所以这里把「系统认识哪些标的、各标的的交易所元数据长什么样」收进**一张表**，
`app/sizing.py` / `app/risk.py` 都只从这里取，不再各自维护一份。

显性拦截原则（保留）
--------------------
**不在表里的标的 = 配置错误，必须显性拒绝**，不是「优雅跳过」：
`get()` / `spec_strict()` 抛 `SymbolNotConfigured`，`spec()` 返回 `None` 供 sizing 把它
转成结构化拒单（`symbol_not_registered`）。旧系统「漏配=显性拦截」的原则原样保留：
一个没登记过的 symbol 绝不可能被静默按别人的精度下单。

加一个标的只改这一个文件
------------------------
在 `_REGISTRY` 里加一行 `SymbolSpec(...)` 即可（三处元数据都在同一行里）。当前**只有
BTC / ETH / SOL 三个** —— 不要擅自扩充：策略的证据基础（299 笔信号）就是这三个标的。

market_id 不硬编码在业务代码里
------------------------------
Lighter 的 `market_id` 官方明确会变（testnet 与 mainnet 不同：BTC 在 testnet 不是 1）。
所以业务代码走 `resolve_market_info()`：**优先**运行时从 `orderBookDetails` 解析，
解析不到才回退本表的兜底值，并**必定**在 `ResolvedMarket.warnings` 里留痕。
`SymbolSpec.fallback_market_id` 只是兜底常量，读它的地方只有 `resolve_market_info()`
和测试（`tests/test_symbol_registry.py` 断言它与 `app.market.FALLBACK_MARKET_IDS` 一致，
一旦两边漂移就会红）。

数值来源（实测 2026-10-27，`GET /api/v1/orderBookDetails`，mainnet）
-------------------------------------------------------------------
================  =========  ======  ======  ==============  ==============  =====  =====
symbol            market_id  price   size    min_base        min_quote       mif    max lev
                            dec     dec     (base)          (quote)                (x)
================  =========  ======  ======  ==============  ==============  =====  =====
BTC               1          1       5       0.00007         10.0            200    50
ETH               0          2       4       0.0020          10.0            200    50
SOL               2          3       3       0.055           10.0            400    25
================  =========  ======  ======  ==============  ==============  =====  =====

`mif` = `min_initial_margin_fraction`，**万分比整数**：初始保证金率 = mif/10000，
因此交易所允许的最大杠杆 = 10000/mif（200 → 5% → 50x；400 → 25x）。
这些值会随 Lighter 调整而变化，所以它们只是**兜底**；运行时一律以 `MarketInfo` 为准。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Protocol, Tuple

from app.market import FALLBACK_MARKET_IDS, FeedError, MarketInfo

__all__ = [
    "REGISTRY", "MarketDetailsSource", "ResolvedMarket", "SymbolNotConfigured",
    "SymbolSpec", "describe", "get", "is_registered", "known_symbols", "max_leverage",
    "resolve_market_info", "spec", "spec_strict", "with_market_id",
]


class SymbolNotConfigured(Exception):
    """symbol 未在本注册表中登记（元数据缺失，属配置错误，调用方应 fail-loud）。"""


class MarketDetailsSource(Protocol):
    """`resolve_market_info()` 需要的最小能力：一次 `orderBookDetails` 的 symbol→info 映射。

    `app.market.MarketClient` 满足它；测试注入的 fake 也满足它 —— 这样注册表不必
    依赖具体客户端类型，也就不会为了「解析一个 id」把网络栈拖进纯函数测试。
    """

    def market_details(self) -> Dict[str, MarketInfo]:  # pragma: no cover - 协议
        ...


@dataclass(frozen=True)
class SymbolSpec:
    """一个标的的 Lighter 元数据（**兜底值**；运行时以 `MarketInfo` 为准）。

    字段名与 `app.market.MarketInfo` 对齐，便于 `as_market_info()` 直接构造，
    也便于调用方在没有实时元信息时用鸭子类型读同一批属性。
    """

    symbol: str
    fallback_market_id: int
    price_decimals: int
    size_decimals: int
    min_base_amount: float
    min_quote_amount: float
    min_initial_margin_fraction: int
    maintenance_margin_fraction: int = 0
    verified_at: str = ""
    notes: str = ""

    # ---------------------------------------------------------------- 派生

    @property
    def exchange_max_leverage(self) -> Optional[float]:
        """交易所允许的最大杠杆 = 10000 / `min_initial_margin_fraction`。

        mif 缺失/非法 → `None`（**不猜**）。BTC/ETH 200 → 50x，SOL 400 → 25x。
        """
        mif = int(self.min_initial_margin_fraction or 0)
        if mif <= 0:
            return None
        return 10_000.0 / float(mif)

    def as_market_info(self, market_id: Optional[int] = None, **overrides: Any) -> MarketInfo:
        """把兜底值包成一个 `MarketInfo`（只填本表已知字段，其余用 dataclass 默认值）。

        `status` 固定写 `"active"`：兜底路径的语义是「元信息端点不可达，用登记过的
        常量继续跑」，若这里写 `"unknown"` 会被 sizing 的 `market_not_tradeable` 挡掉，
        等于把「端点故障」升级成「熔断」—— 那是 T3/T9 该决定的事，不是这里。
        `is_frozen` 保持 False（同理：端点的冻结标记拿不到，不编造）。
        """
        fields: Dict[str, Any] = {
            "symbol": self.symbol,
            "market_id": int(self.fallback_market_id if market_id is None else market_id),
            "market_type": "perp",
            "status": "active",
            "price_decimals": int(self.price_decimals),
            "size_decimals": int(self.size_decimals),
            "min_base_amount": float(self.min_base_amount),
            "min_quote_amount": float(self.min_quote_amount),
            "min_initial_margin_fraction": int(self.min_initial_margin_fraction),
            "maintenance_margin_fraction": int(self.maintenance_margin_fraction),
        }
        fields.update(overrides)
        return MarketInfo(**fields)


# ---------------------------------------------------------------- 注册表本体
#
# 只有这三行是「加标的要改的地方」。数值 = 2026-10-27 mainnet `orderBookDetails` 实测。
_REGISTRY: Dict[str, SymbolSpec] = {
    "BTC": SymbolSpec(
        symbol="BTC", fallback_market_id=1,
        price_decimals=1, size_decimals=5,
        min_base_amount=0.00007, min_quote_amount=10.0,
        min_initial_margin_fraction=200, maintenance_margin_fraction=120,
        verified_at="2026-10-27", notes="50x max (mif=200); min order ≈ $5.6 @80k → quote 10 binds",
    ),
    "ETH": SymbolSpec(
        symbol="ETH", fallback_market_id=0,
        price_decimals=2, size_decimals=4,
        min_base_amount=0.0020, min_quote_amount=10.0,
        min_initial_margin_fraction=200, maintenance_margin_fraction=120,
        verified_at="2026-10-27", notes="50x max (mif=200); min order ≈ $4.6 @2.3k → quote 10 binds",
    ),
    "SOL": SymbolSpec(
        symbol="SOL", fallback_market_id=2,
        price_decimals=3, size_decimals=3,
        min_base_amount=0.055, min_quote_amount=10.0,
        min_initial_margin_fraction=400, maintenance_margin_fraction=240,
        verified_at="2026-10-27", notes="25x max (mif=400); min_base 0.055 binds first",
    ),
}
if tuple(_REGISTRY) != ("BTC", "ETH", "SOL"):                # pragma: no cover
    raise ImportError(f"registry must hold exactly BTC/ETH/SOL in order, got {tuple(_REGISTRY)}")

REGISTRY = _REGISTRY                       # 只读视图（module-level 名字给测试/工具用）


# ---------------------------------------------------------------- 查询

def known_symbols() -> List[str]:
    """已登记的全部 symbol（顺序稳定：BTC, ETH, SOL）。"""
    return list(_REGISTRY.keys())


def is_registered(symbol: Any) -> bool:
    """是否登记过（大小写不敏感）。注意：**不等于**「当前可交易」。"""
    return _norm(symbol) in _REGISTRY


def spec_strict(symbol: Any) -> SymbolSpec:
    """取一条元数据；未注册抛 `SymbolNotConfigured`（显性拦截，不返回 None）。"""
    wanted = _norm(symbol)
    entry = _REGISTRY.get(wanted)
    if entry is None:
        raise SymbolNotConfigured(
            f"{wanted or symbol!r}: not registered in app/symbol_registry.py "
            f"(known: {', '.join(known_symbols())}) — adding a symbol means adding a "
            f"SymbolSpec line, and an unregistered symbol is never traded"
        )
    return entry


def spec(symbol: Any) -> Optional[SymbolSpec]:
    """同 `spec_strict()`，但未注册返回 `None`（供 sizing 转成结构化拒单，不抛）。"""
    return _REGISTRY.get(_norm(symbol))


def get(symbol: Any) -> SymbolSpec:
    """`spec_strict()` 的别名（与旧仓库 `symbol_registry.get()` 同名，便于迁移）。"""
    return spec_strict(symbol)


def max_leverage(symbol: Any) -> Optional[float]:
    """交易所允许的最大杠杆（10000/mif）；未注册或 mif 缺失 → `None`。

    注意：这是**交易所**上限，不是本系统用的杠杆。系统自己的杠杆上限是
    `app.sizing.SizingConfig.leverage`（默认 5x，可配），取两者较小值生效。
    """
    entry = spec(symbol)
    return None if entry is None else entry.exchange_max_leverage


def _norm(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


# ---------------------------------------------------------------- 运行时解析
#
# 与 `app/market.py::MarketClient.market_id_of()` 协同的**唯一入口**：
# 元信息能拿到就用它（官方会改 market_id，尤其 testnet），拿不到才回退本表常量，
# 并且**一定**留一条 warning —— 静默回退是下一个人排查「为什么查的是另一张合约」时
# 最恨的东西。


@dataclass(frozen=True)
class ResolvedMarket:
    """一次解析的结果：生效的 `MarketInfo` + 来源 + 告警。

    `source` 取值：
      * `"orderBookDetails"` —— 运行时解析（正常路径）
      * `"registry_fallback"` —— 元信息端点不可达/缺该标的，用本表兜底（**带 warning**）
    """

    spec: SymbolSpec
    info: MarketInfo
    source: str
    warnings: Tuple[str, ...] = ()

    @property
    def used_fallback(self) -> bool:
        return self.source != "orderBookDetails"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.spec.symbol,
            "market_id": self.info.market_id,
            "source": self.source,
            "warnings": list(self.warnings),
        }


def resolve_market_info(symbol: Any, client: Optional[MarketDetailsSource] = None) -> ResolvedMarket:
    """symbol -> 生效的 `MarketInfo`（运行时优先，注册表兜底，必留 warning）。

    参数
    ----
    symbol
        必须在注册表里，否则抛 `SymbolNotConfigured`（显性拦截）。
    client
        `app.market.MarketClient` 或任何提供 `market_details() -> dict[str, MarketInfo]`
        的对象（测试注入 fake）。`None` = 不查询运行时，直接用注册表兜底（不产生 warning
        —— 这是显式选择，不是「端点坏了」）。

    行为
    ----
    1. 运行时可用且含该标的 → 用它；若其 `market_id`/精度与注册表不一致，记 warning
       （不拒绝：官方会改，运行时的值才是对的，warning 是给人看的漂移证据）。
    2. 运行时抛 `FeedError` / 其它异常 / 缺该标的 → 回退注册表常量，**记 warning**。
    """
    entry = spec_strict(symbol)
    warnings: List[str] = []

    if client is None:
        return ResolvedMarket(spec=entry, info=entry.as_market_info(), source="registry",
                              warnings=())

    details: Dict[str, MarketInfo] = {}
    try:
        details = dict(client.market_details() or {})
    except FeedError as exc:
        warnings.append(
            f"{entry.symbol}: orderBookDetails unavailable ({exc}); using registry fallback "
            f"(market_id={entry.fallback_market_id}, price_decimals={entry.price_decimals}, "
            f"size_decimals={entry.size_decimals}) verified {entry.verified_at}"
        )
    except Exception as exc:                                 # noqa: BLE001 - 兜底必须不抛
        warnings.append(
            f"{entry.symbol}: orderBookDetails failed unexpectedly "
            f"({type(exc).__name__}: {exc}); using registry fallback "
            f"(market_id={entry.fallback_market_id})"
        )

    info = details.get(entry.symbol)
    if info is None:
        if not warnings:
            warnings.append(
                f"{entry.symbol}: not present in orderBookDetails; using registry fallback "
                f"(market_id={entry.fallback_market_id}) verified {entry.verified_at}"
            )
        return ResolvedMarket(spec=entry, info=entry.as_market_info(), source="registry_fallback",
                              warnings=tuple(warnings))

    warnings.extend(drift_warnings(entry, info))
    return ResolvedMarket(spec=entry, info=info, source="orderBookDetails",
                          warnings=tuple(warnings))


def drift_warnings(entry: SymbolSpec, info: MarketInfo) -> List[str]:
    """注册表兜底值 vs 运行时元信息的差异（**只在有差异时**返回条目）。

    这些差异不会让系统拒单 —— 运行时值才是权威；但必须能被看到，否则「注册表里写的
    和实际下单用的不一样」会在事后对账时变成谜。
    """
    out: List[str] = []
    if int(info.market_id) != int(entry.fallback_market_id):
        out.append(
            f"{entry.symbol}: runtime market_id {info.market_id} differs from registry "
            f"fallback {entry.fallback_market_id} — runtime wins"
        )
    if int(info.price_decimals) != int(entry.price_decimals):
        out.append(
            f"{entry.symbol}: runtime price_decimals {info.price_decimals} != registry "
            f"{entry.price_decimals} — runtime wins"
        )
    if int(info.size_decimals) != int(entry.size_decimals):
        out.append(
            f"{entry.symbol}: runtime size_decimals {info.size_decimals} != registry "
            f"{entry.size_decimals} — runtime wins"
        )
    if int(info.min_initial_margin_fraction) != int(entry.min_initial_margin_fraction):
        out.append(
            f"{entry.symbol}: runtime min_initial_margin_fraction "
            f"{info.min_initial_margin_fraction} != registry {entry.min_initial_margin_fraction} "
            f"— runtime wins (max leverage changes)"
        )
    return out


def describe(symbol: Optional[Any] = None) -> List[Dict[str, Any]]:
    """给监控面板（T10）用的可序列化视图：一个标的或全部。"""
    entries = [spec_strict(symbol)] if symbol is not None else list(_REGISTRY.values())
    return [
        {
            "symbol": e.symbol,
            "market_id_fallback": e.fallback_market_id,
            "price_decimals": e.price_decimals,
            "size_decimals": e.size_decimals,
            "min_base_amount": e.min_base_amount,
            "min_quote_amount": e.min_quote_amount,
            "min_initial_margin_fraction": e.min_initial_margin_fraction,
            "exchange_max_leverage": e.exchange_max_leverage,
            "verified_at": e.verified_at,
        }
        for e in entries
    ]


def with_market_id(symbol: Any, market_id: int) -> SymbolSpec:
    """返回把兜底 market_id 换成给定值的新 `SymbolSpec`（不修改注册表本体）。

    给「运行时解析到 id、但下游只认 `SymbolSpec`」的场景用；注册表仍然只读，
    避免一个进程里解析出的 id 污染其它进程/测试。
    """
    return replace(spec_strict(symbol), fallback_market_id=int(market_id))


# `app.market.FALLBACK_MARKET_IDS` 与本表兜底值必须一致：两处都只是常量兜底，
# 但漂移会让「哪张合约」在代码不同角落给出不同答案。测试会断言（这里 import 时
# 只做一次廉价一致性检查，失败即 import 失败 —— 属于配置错误，不能静默）。
_MISMATCH = {
    symbol: (entry.fallback_market_id, FALLBACK_MARKET_IDS.get(symbol))
    for symbol, entry in _REGISTRY.items()
    if FALLBACK_MARKET_IDS.get(symbol) != entry.fallback_market_id
}
if _MISMATCH:                                                # pragma: no cover
    raise ImportError(
        "symbol_registry fallback market_ids disagree with app.market.FALLBACK_MARKET_IDS: "
        + ", ".join(f"{s}: registry={a} market={b}" for s, (a, b) in _MISMATCH.items())
    )
