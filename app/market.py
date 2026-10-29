"""市场数据层：从 Lighter 拉 4h K 线 + 行情快照。

只做**读**：取数、规整、自检。这里没有任何下单、签名、持仓逻辑（那些属于 T7/T8）。

设计约束（来自 `docs/PLAN.md` T3）：

* **单一事实来源**：这套拉取逻辑原先写在 `scripts/xvalidate.py` 里。T3 把它上提到
  本模块，`scripts/xvalidate.py` 改为 import 这里 —— 交叉验证工具与被测系统必须用
  同一份取数代码，否则「验证通过」只说明两份实现互相自洽。
* **fail-closed**：主源（Lighter）失败就抛 `FeedError`，**绝不换源**（不退回币安/MEXC，
  也不静默补一根假 bar）。唯一的例外是 `market_id` 在元信息端点不可达时退回
  `FALLBACK_MARKET_IDS`（并在 `warnings` 里留痕）—— 这只影响「查哪张合约」，
  不影响 bar 数据本身。
* **只返回已收盘的 bar**：最后一根可能还在形成中，其 `close` 不是收盘价。剔除的
  那根在 `BarsResult.dropped_unclosed` 里明确列出，不静默丢弃。
* **新鲜度校验**：最后一根已收盘 bar 的收盘时间距现在 ≤ 8h（2 个 4h 周期），
  否则视为数据源已死，抛 `StaleFeedError`。
* **缺口报告**：相邻 bar 的 `open_ms` 间隔不是 4h 时记一条 `Gap`，既不填补也不丢弃。

口径（写清楚，免得后续各自解释）：

* **价格**：`Snapshot.price` 默认取盘口 **best bid / best ask 的中间价**
  （`price_source == "mid"`）。盘口拿不到时依次退到「最近一根已收盘 4h bar 的收盘价」
  （`"last_close"`）→ 「`orderBookDetails` 的 `last_trade_price`」（`"last_trade"`）。
  三者都拿不到就抛 `FeedError`。中间价是**未成交价**，做名义金额估算够用，
  但不能当成交价入账。
* **盘口档位**：`/orderBookOrders` 返回的是**逐单**（同一个价位有多条），本模块按价格
  聚合后给档位（`size` = 该价位剩余量之和），买单降序、卖单升序。`limit` 是**单数**
  上限，所以聚合后的档位数 ≤ limit。

实测备注（2026-10-27，mainnet）：

* `GET /orderBookDetails` 返回 `{code, order_book_details: [...], spot_order_book_details: [...]}`，
  perp 的元信息（`price_decimals` / `size_decimals` / `min_base_amount` /
  `min_quote_amount` / `min_initial_margin_fraction` / `mark_price` / `last_trade_price`）
  都在这里，**全市场一次请求**，因此它是元信息的唯一来源。
* `GET /orderBooks` **不返回盘口深度** —— 它只有元信息字段（没有 `bids` / `asks`），
  与文档「盘口」的描述不符。真正的深度在 `GET /orderBookOrders?market_id=N&limit=N`
  （`limit` 必填，缺省直接 400）。所以深度走 `orderBookOrders`，`/orderBooks` 不再使用。
* `GET /candles?market_id=N&resolution=4h&start_timestamp=0&end_timestamp=E&count_back=K`
  ：返回**以 E 结尾**的最近若干根（实测总是 **500 根**，升序），行形状
  `{t, o, h, l, c, v, V, i}`，`t` 为**毫秒**，`v` 是基础币成交量，`V` 是计价币成交量。
  把 `E` 设成上一页里最早那根 `t - 1` 就能一直往前翻。**`count_back` 实测被忽略**
  （`start_timestamp=0` 时无论传 4 / 10 / 500 都是 500 根；传非 0 的 start 也不稳定），
  所以 `PAGE_LIMIT=500` 是唯一安全值 —— 好在翻页只依赖「500 根、以 E 结尾」这一条，
  终止条件用的是 `earliest <= since` 与「短页」，不依赖服务端尊重 count_back。
* `testnet` 的 market_id 与 mainnet **不同**（BTC 在 testnet 不是 1），所以 id 必须
  运行时从 `orderBookDetails` 解析，`FALLBACK_MARKET_IDS` 只作兜底。

限流：Lighter 公开 API 约 **60 req/min**。`RateLimiter`（滑动窗口 + 最小间隔）保证
请求间隔，`_get_json` 加上指数退避 + 抖动的重试。全部纯 stdlib，时钟与 sleep 可注入，
测试不用真等。
"""

from __future__ import annotations

import datetime as dt
import json
import random
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.proxy import http_get_with_proxy
from app.strategy import Bar

__all__ = [
    "BAR_MS", "BASE_URLS", "DEFAULT_ENVIRONMENT", "EARLIEST_MS", "FALLBACK_MARKET_IDS",
    "FRESH_MAX_AGE_MS", "MAINNET_API", "MAX_PAGES", "PAGE_LIMIT", "REQUESTS_PER_MINUTE",
    "RESOLUTION", "TESTNET_API",
    "BarsResult", "FeedError", "Gap", "Level", "MarketClient", "MarketInfo", "OrderBook",
    "RateLimiter", "Snapshot", "StaleFeedError",
    "base_url", "bars_from_rows", "check_freshness", "default_client", "detect_gaps",
    "drop_unclosed", "fmt_ms", "split_unclosed",
]

# ------------------------------------------------------------------ 常量

MAINNET_API = "https://mainnet.zklighter.elliot.ai/api/v1"
TESTNET_API = "https://testnet.zklighter.elliot.ai/api/v1"
BASE_URLS: Dict[str, str] = {"mainnet": MAINNET_API, "testnet": TESTNET_API}
DEFAULT_ENVIRONMENT = "mainnet"

RESOLUTION = "4h"
BAR_MS = 4 * 3600 * 1000            # 4h，毫秒
PAGE_LIMIT = 500                    # /candles 单次 count_back 上限（服务端硬限制）
EARLIEST_MS = 1_737_100_800_000     # 2025-01-17T08:00:00Z —— Lighter 第一根 4h bar
MAX_PAGES = 80                      # 硬止损：~40k 根，远超 Lighter 现有历史
FRESH_MAX_AGE_MS = 2 * BAR_MS       # 8h：最后一根已收盘 bar 的收盘时间距现在 ≤ 8h
REQUESTS_PER_MINUTE = 60            # Lighter 公开 API 限流

# 兜底映射，实测 2026-10-27（mainnet）。**不是**事实来源：
# `orderBookDetails` 优先，构造时传入的 `market_ids` 覆盖两者。官方明确 market_id 会变，
# 所以全仓库只在这一处出现这三个数字。
FALLBACK_MARKET_IDS: Dict[str, int] = {"BTC": 1, "ETH": 0, "SOL": 2}

USER_AGENT = "lighter-ma-bot/market"

# 与 `_urllib_get_json` 里写死的头保持一致 —— 走代理的 getter 由 `app.proxy` 提供，
# 头由这里传进去（`app.proxy` 反过来不依赖 `app.market`，依赖是单向的）。
_JSON_HEADERS: Dict[str, str] = {"User-Agent": USER_AGENT, "Accept": "application/json"}

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


# ------------------------------------------------------------------ 异常


class FeedError(RuntimeError):
    """数据源失败（网络、限流耗尽、响应结构不对、数据自检不通过）。

    fail-closed：调用方**不要**换成另一个数据源，也不要拿旧 bar 凑数 —— 直接让这一轮
    tick 失败（上层记事件并跳过），下一轮重试。
    """


class StaleFeedError(FeedError):
    """最后一根已收盘 bar 太旧（收盘时间距现在 > `FRESH_MAX_AGE_MS`）。"""


# ------------------------------------------------------------------ 小工具


def fmt_ms(ms: int) -> str:
    """毫秒时间戳 -> `YYYY-MM-DD HH:MM`（UTC），用于日志/报告。"""
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def base_url(environment: Optional[str] = None) -> str:
    """`mainnet` / `testnet` -> API base URL。未知环境直接报错（不做默认猜测）。"""
    name = (environment or DEFAULT_ENVIRONMENT).strip().lower()
    try:
        return BASE_URLS[name]
    except KeyError:
        raise ValueError(
            f"unknown Lighter environment {environment!r}; expected one of {sorted(BASE_URLS)}"
        ) from None


def _now_ms_default() -> int:
    return int(time.time() * 1000)


def _urllib_get_json(url: str, timeout: float = 30.0) -> Any:
    """默认 HTTP 实现：GET + 解析 JSON。异常原样抛给调用方分类。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw)


# ------------------------------------------------------------------ 限流


class RateLimiter:
    """滑动窗口限流 + 最小请求间隔。

    `per_minute=60` 即每 60s 最多 60 次，且任意两次之间至少隔 1s —— 后者是真正防
    「一分钟末尾突刺打进 60 次」的那一条。`clock` / `sleep` 可注入，测试用假时钟。
    """

    def __init__(self, per_minute: int = REQUESTS_PER_MINUTE, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 window: float = 60.0) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        if window <= 0:
            raise ValueError("window must be positive")
        self.per_minute = int(per_minute)
        self.window = float(window)
        self.min_interval = self.window / self.per_minute
        self._clock = clock
        self._sleep = sleep
        self._stamps: Deque[float] = deque()
        self._last: Optional[float] = None
        self.waited_s = 0.0            # 累计等待，供报告/测试观察

    def _prune(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= self.window:
            self._stamps.popleft()

    def acquire(self) -> float:
        """占用一个配额；需要等待就 `sleep`。返回本次实际等待的秒数。"""
        waited = 0.0
        for _ in range(64):                     # 防御：注入的 clock/sleep 不自洽时别死循环
            now = self._clock()
            self._prune(now)
            wait = 0.0
            if self._last is not None:
                wait = max(wait, self._last + self.min_interval - now)
            if len(self._stamps) >= self.per_minute:
                wait = max(wait, self._stamps[0] + self.window - now)
            # 1e-9 容差：浮点减法会留下 1e-16 量级的「永远差一点」，不加容差会空转。
            if wait <= 1e-9:
                break
            self._sleep(wait)
            waited += wait
        else:
            raise RuntimeError(
                "rate limiter made no progress after 64 sleeps — the injected "
                "clock/sleep pair is inconsistent"
            )
        now = self._clock()
        self._stamps.append(now)
        self._last = now
        self.waited_s += waited
        return waited


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True)
class Gap:
    """两根相邻 bar 之间的空洞。`missing` = 应有而缺失的根数。"""

    after_ms: int        # 缺口前那根 bar 的开盘时间
    before_ms: int       # 缺口后那根 bar 的开盘时间
    missing: int

    @property
    def span_ms(self) -> int:
        return self.before_ms - self.after_ms

    def __str__(self) -> str:
        return (f"{fmt_ms(self.after_ms)} → {fmt_ms(self.before_ms)} "
                f"（缺 {self.missing} 根）")


@dataclass(frozen=True)
class BarsResult:
    """一次 K 线拉取的全部结果与自检痕迹。

    `bars` 是**唯一**可以喂给 `app.strategy` 的字段：已收盘、升序、无重复。
    """

    symbol: str
    market_id: int
    bars: Tuple[Bar, ...]
    dropped_unclosed: Tuple[Bar, ...] = ()
    gaps: Tuple[Gap, ...] = ()
    rows_fetched: int = 0
    pages: int = 0
    since_ms: int = EARLIEST_MS
    now_ms: int = 0
    last_close_age_ms: int = 0
    warnings: Tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def first_ms(self) -> Optional[int]:
        return self.bars[0].open_ms if self.bars else None

    @property
    def last_ms(self) -> Optional[int]:
        return self.bars[-1].open_ms if self.bars else None

    @property
    def last_close_ms(self) -> Optional[int]:
        return self.bars[-1].open_ms + BAR_MS if self.bars else None

    @property
    def is_fresh(self) -> bool:
        return self.last_close_age_ms <= FRESH_MAX_AGE_MS

    def summary(self) -> Dict[str, Any]:
        """给日志/探针用的摘要（不含大对象）。"""
        return {
            "symbol": self.symbol,
            "market_id": self.market_id,
            "bars": len(self.bars),
            "first": fmt_ms(self.bars[0].open_ms) if self.bars else None,
            "last": fmt_ms(self.bars[-1].open_ms) if self.bars else None,
            "last_close": fmt_ms(self.last_close_ms) if self.last_close_ms else None,
            "last_close_age_h": round(self.last_close_age_ms / 3_600_000, 3),
            "dropped_unclosed": [fmt_ms(b.open_ms) for b in self.dropped_unclosed],
            "gaps": [str(g) for g in self.gaps],
            "rows_fetched": self.rows_fetched,
            "pages": self.pages,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class MarketInfo:
    """一个 perp 市场的元信息 / 交易规则（`orderBookDetails` 一行）。

    小数位与最小量的权威来源，T7 下单直接用这些字段，不要自己猜。
    `min_initial_margin_fraction` 等是**万分比整数**（500 = 5%）。
    """

    symbol: str
    market_id: int
    market_type: str = "perp"
    status: str = "active"
    price_decimals: int = 0
    size_decimals: int = 0
    min_base_amount: float = 0.0
    min_quote_amount: float = 0.0
    min_initial_margin_fraction: int = 0
    maintenance_margin_fraction: int = 0
    taker_fee: float = 0.0
    maker_fee: float = 0.0
    last_trade_price: Optional[float] = None
    mark_price: Optional[float] = None
    index_price: Optional[float] = None
    is_frozen: bool = False

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MarketInfo":
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            raise FeedError("orderBookDetails row has no symbol")
        try:
            market_id = int(row["market_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedError(f"orderBookDetails row for {symbol} has no usable market_id") from exc

        def num(key: str) -> float:
            value = row.get(key)
            if value is None or value == "":
                return 0.0
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise FeedError(f"orderBookDetails {symbol}.{key} is not numeric") from exc

        def opt(key: str) -> Optional[float]:
            value = row.get(key)
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def integer(key: str) -> int:
            value = row.get(key)
            if value is None or value == "":
                return 0
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        # `supported_*_decimals` 与 `*_decimals` 并存且实测一致，取前者、后者兜底。
        price_decimals = integer("supported_price_decimals") or integer("price_decimals")
        size_decimals = integer("supported_size_decimals") or integer("size_decimals")
        return cls(
            symbol=symbol,
            market_id=market_id,
            market_type=str(row.get("market_type") or "perp"),
            status=str(row.get("status") or "unknown"),
            price_decimals=price_decimals,
            size_decimals=size_decimals,
            min_base_amount=num("min_base_amount"),
            min_quote_amount=num("min_quote_amount"),
            min_initial_margin_fraction=integer("min_initial_margin_fraction"),
            maintenance_margin_fraction=integer("maintenance_margin_fraction"),
            taker_fee=num("taker_fee"),
            maker_fee=num("maker_fee"),
            last_trade_price=opt("last_trade_price"),
            mark_price=opt("mark_price"),
            index_price=opt("index_price"),
            is_frozen=bool(row.get("is_frozen") or False),
        )


@dataclass(frozen=True)
class Level:
    """盘口一档：价格 + 该价位剩余量（同价位的逐单已合并）。"""

    price: float
    size: float

    def __str__(self) -> str:
        return f"{self.price:g}×{self.size:g}"


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    market_id: int
    bids: Tuple[Level, ...] = ()          # 降序（最优买价在前）
    asks: Tuple[Level, ...] = ()          # 升序（最优卖价在前）
    total_bids: int = 0                   # 服务端报告的挂单总数（不是档位数）
    total_asks: int = 0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    @property
    def spread_bps(self) -> Optional[float]:
        mid, spread = self.mid, self.spread
        if mid is None or spread is None or mid <= 0:
            return None
        return spread / mid * 10_000.0


@dataclass(frozen=True)
class Snapshot:
    """一次行情快照。`price` 的口径见模块 docstring（默认盘口中间价）。"""

    symbol: str
    market_id: int
    price: float
    price_source: str                     # "mid" | "last_close" | "last_trade"
    taken_at_ms: int
    mid: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    spread_bps: Optional[float] = None
    bids: Tuple[Level, ...] = ()
    asks: Tuple[Level, ...] = ()
    mark_price: Optional[float] = None
    index_price: Optional[float] = None
    last_trade_price: Optional[float] = None
    info: Optional[MarketInfo] = None

    def summary(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market_id": self.market_id,
            "price": self.price,
            "price_source": self.price_source,
            "bid": self.best_bid,
            "ask": self.best_ask,
            "spread": self.spread,
            "spread_bps": None if self.spread_bps is None else round(self.spread_bps, 3),
            "mark": self.mark_price,
            "levels": (len(self.bids), len(self.asks)),
        }


# ------------------------------------------------------------------ 纯函数：行 -> bar


def bars_from_rows(rows: Iterable[Mapping[str, Any]]) -> List[Bar]:
    """原始 candle 行 -> `app.strategy.Bar`，**按 open_ms 升序、无重复**。

    去重键是 `open_ms`；同一时间戳出现多次时**后出现的那份覆盖**前者（翻页边界重复
    时后一页才是重新取到的数据）。行结构不对直接抛 `FeedError` —— 结构变了属于数据源
    故障，不能跳过坏行继续算指标。
    """
    by_ts: Dict[int, Bar] = {}
    for index, row in enumerate(rows):
        try:
            open_ms = int(row["t"])
            bar = Bar(
                open_ms=open_ms,
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=float(row.get("v") or 0.0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedError(f"candle row #{index} is malformed: {exc!r}") from exc
        if open_ms <= 0:
            raise FeedError(f"candle row #{index} has a non-positive timestamp")
        if bar.high < bar.low:
            raise FeedError(f"candle row #{index} has high < low ({bar.open_ms})")
        by_ts[open_ms] = bar
    return [by_ts[ts] for ts in sorted(by_ts)]


def detect_gaps(bars: Sequence[Bar]) -> List[Gap]:
    """相邻 bar 间隔不是 `BAR_MS` 的地方记一条 `Gap`。不填补、不丢弃。"""
    gaps: List[Gap] = []
    for prev, cur in zip(bars, bars[1:]):
        delta = cur.open_ms - prev.open_ms
        if delta == BAR_MS:
            continue
        missing = max(0, delta // BAR_MS - 1)
        gaps.append(Gap(after_ms=prev.open_ms, before_ms=cur.open_ms, missing=missing))
    return gaps


def split_unclosed(bars: Sequence[Bar], now_ms: Optional[int] = None) -> Tuple[List[Bar], List[Bar]]:
    """拆成 `(已收盘, 未收盘)`。判据：`open_ms + 4h <= now` 才算收盘。"""
    now = int(now_ms if now_ms is not None else _now_ms_default())
    closed: List[Bar] = []
    unclosed: List[Bar] = []
    for bar in bars:
        (closed if bar.open_ms + BAR_MS <= now else unclosed).append(bar)
    return closed, unclosed


def drop_unclosed(bars: Sequence[Bar], now_ms: Optional[int] = None) -> List[Bar]:
    """只保留已收盘的 bar。"""
    return split_unclosed(bars, now_ms)[0]


def check_freshness(bars: Sequence[Bar], now_ms: Optional[int] = None,
                    max_age_ms: int = FRESH_MAX_AGE_MS) -> int:
    """新鲜度校验：最后一根已收盘 bar 的收盘时间距现在不得超过 `max_age_ms`。

    返回实际 age（毫秒，可能为很小的负值 —— 时钟轻微偏移）。超限抛 `StaleFeedError`。
    """
    if not bars:
        raise StaleFeedError("no closed bars to check for freshness")
    now = int(now_ms if now_ms is not None else _now_ms_default())
    last_close = bars[-1].open_ms + BAR_MS
    age = now - last_close
    if last_close > now + BAR_MS:
        # 收盘时间比现在还晚一整根 —— 时间戳坏了，不是「新鲜」。
        raise FeedError(f"last bar opens at {fmt_ms(bars[-1].open_ms)}, "
                        f"far in the future relative to now ({fmt_ms(now)})")
    if age > max_age_ms:
        raise StaleFeedError(
            f"feed is stale: last closed bar closed {age / 3_600_000:.2f}h ago "
            f"(limit {max_age_ms / 3_600_000:.0f}h)"
        )
    return age


# ------------------------------------------------------------------ 客户端


class MarketClient:
    """Lighter 只读客户端：K 线 / 元信息 / 盘口 / 快照。

    所有 HTTP 都经过 `_get_json`（限流 + 退避重试）。`http_get` / `sleep` / `clock` /
    `now_ms` / `rng` 全部可注入，因此单测完全离线且不用真等。

    `proxy_url`（`LIGHTER_PROXY_URL`，空 = 直连）只影响默认 getter：配了就把
    `app.proxy.http_get_with_proxy` 装成默认 `http_get`（只对 Lighter 域名走代理）。
    显式传 `http_get` 时它优先 —— 注入的 transport 永远盖过配置。

    用法::

        client = MarketClient()                      # mainnet
        result = client.bars("BTC", since_ms=...)     # BarsResult
        snapshot = client.snapshot("BTC", depth=5)
    """

    def __init__(self, environment: str = DEFAULT_ENVIRONMENT, *,
                 api_base: Optional[str] = None,
                 http_get: Optional[Callable[..., Any]] = None,
                 timeout: float = 30.0,
                 per_minute: int = REQUESTS_PER_MINUTE,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 now_ms: Optional[Callable[[], int]] = None,
                 max_retries: int = 4,
                 backoff_base: float = 1.0,
                 backoff_cap: float = 30.0,
                 jitter: float = 0.5,
                 rng: Optional[random.Random] = None,
                 details_ttl: float = 300.0,
                 market_ids: Optional[Mapping[str, int]] = None,
                 proxy_url: Optional[str] = None) -> None:
        self.environment = (environment or DEFAULT_ENVIRONMENT).strip().lower()
        self.api_base = (api_base or base_url(self.environment)).rstrip("/")
        self.timeout = float(timeout)
        # `http_get` 显式注入优先（测试就是这么做的）；否则配了代理就用带代理的 getter，
        # 没配就用默认的纯 urllib 直连。默认行为必须保持「直连」。
        if http_get is not None:
            self._http_get = http_get
        elif proxy_url:
            self._http_get = http_get_with_proxy(proxy_url, headers=_JSON_HEADERS)
        else:
            self._http_get = _urllib_get_json
        self._sleep = sleep
        self._clock = clock
        self._now_ms = now_ms or _now_ms_default
        self._rng = rng if rng is not None else random.Random()
        self.limiter = RateLimiter(per_minute, clock=clock, sleep=sleep)
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.jitter = float(jitter)
        self.details_ttl = float(details_ttl)
        self.market_id_overrides: Dict[str, int] = {
            str(k).upper(): int(v) for k, v in (market_ids or {}).items()
        }
        self.requests = 0                      # 累计请求数（含重试），供探针报告
        self.warnings: List[str] = []          # 非致命告警（如 market_id 退回兜底表）
        self._details_cache: Optional[Dict[str, MarketInfo]] = None
        self._details_at: Optional[float] = None

    # ---------------------------------------------------------------- HTTP

    def _retry_delay(self, attempt: int, retry_after: Optional[float]) -> float:
        base = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
        delay = base * (1.0 + self._rng.random() * self.jitter)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return min(self.backoff_cap * 2, delay)

    @staticmethod
    def _retry_reason(exc: BaseException) -> Optional[str]:
        """返回可重试原因；返回 None 表示不该重试。"""
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code in _RETRYABLE_STATUS:
                return f"HTTP {exc.code}"
            return None
        if isinstance(exc, urllib.error.URLError):
            return f"connection error ({exc.reason})"
        if isinstance(exc, json.JSONDecodeError):
            return "malformed JSON body"
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return type(exc).__name__
        if isinstance(exc, OSError):
            return f"OS error ({exc.__class__.__name__})"
        return None

    @staticmethod
    def _retry_after(exc: BaseException) -> Optional[float]:
        if isinstance(exc, urllib.error.HTTPError) and exc.headers:
            raw = exc.headers.get("Retry-After")
            if raw:
                try:
                    return max(0.0, float(raw))
                except (TypeError, ValueError):
                    return None
        return None

    def _get_json(self, path: str) -> Any:
        """GET `{api_base}/{path}`，限流 + 指数退避重试 + 抖动。失败抛 `FeedError`。"""
        url = f"{self.api_base}/{path}"
        attempts = 0
        while True:
            attempts += 1
            self.limiter.acquire()
            self.requests += 1
            retry_after: Optional[float] = None
            try:
                payload = self._http_get(url, timeout=self.timeout)
            except Exception as exc:                     # noqa: BLE001 - 分类后再决定
                reason = self._retry_reason(exc)
                if reason is None:
                    raise FeedError(f"GET {path} failed permanently: {exc!r}") from exc
                retry_after = self._retry_after(exc)
            else:
                if not isinstance(payload, Mapping):
                    raise FeedError(f"GET {path} returned {type(payload).__name__}, expected an object")
                raw_code = payload.get("code")
                if raw_code is None:
                    return payload
                try:
                    code = int(raw_code)
                except (TypeError, ValueError) as exc:
                    raise FeedError(f"GET {path} returned a non-numeric code") from exc
                if code == 200:
                    return payload
                if code < 500:
                    raise FeedError(f"GET {path} returned API code {code}")
                reason = f"API code {code}"

            if attempts > self.max_retries:
                raise FeedError(
                    f"GET {path} failed after {attempts} attempts (last: {reason})"
                )
            self._sleep(self._retry_delay(attempts, retry_after))

    # ------------------------------------------------------------ 元信息

    def market_details(self, *, force: bool = False) -> Dict[str, MarketInfo]:
        """symbol -> `MarketInfo`（仅 `market_type=perp` 且 `status=active`），带 TTL 缓存。"""
        now = self._clock()
        if (not force and self._details_cache is not None
                and self._details_at is not None and now - self._details_at < self.details_ttl):
            return dict(self._details_cache)
        payload = self._get_json("orderBookDetails")
        rows = payload.get("order_book_details") or []
        out: Dict[str, MarketInfo] = {}
        for row in rows:
            info = MarketInfo.from_row(row)
            if info.market_type != "perp" or info.status != "active":
                continue
            out[info.symbol] = info
        if not out:
            raise FeedError("orderBookDetails returned no active perp markets")
        self._details_cache = out
        self._details_at = now
        return dict(out)

    def market_info(self, symbol: str, *, force: bool = False) -> MarketInfo:
        """严格版：元信息必须能取到（fail-closed），否则抛 `FeedError`。

        T7 下单前必须走这条路拿 `price_decimals` / `size_decimals` / 最小量。
        """
        wanted = str(symbol).upper()
        details = self.market_details(force=force)
        info = details.get(wanted)
        if info is None:
            raise FeedError(
                f"{wanted} is not an active perp market in orderBookDetails "
                f"(known: {', '.join(sorted(details))[:200]})"
            )
        return info

    def market_id_of(self, symbol: str) -> int:
        """symbol -> market_id：显式覆盖 > `orderBookDetails` > `FALLBACK_MARKET_IDS`。

        元信息端点**不可达**时退回兜底表并记一条 `warnings`（只影响查哪张合约，
        不影响 bar 数据本身）。两条都不成立则抛 `FeedError`。
        """
        wanted = str(symbol).upper()
        if wanted in self.market_id_overrides:
            return self.market_id_overrides[wanted]
        try:
            details = self.market_details()
        except FeedError as exc:
            details = {}
            self._warn(f"orderBookDetails unavailable ({exc}); using the documented "
                       f"fallback market ids {FALLBACK_MARKET_IDS}")
        if wanted in details:
            return details[wanted].market_id
        if wanted in FALLBACK_MARKET_IDS:
            return FALLBACK_MARKET_IDS[wanted]
        raise FeedError(
            f"unknown symbol {wanted!r}: not in orderBookDetails, no fallback id, "
            f"no override"
        )

    def _warn(self, message: str) -> None:
        self.warnings.append(message)

    # -------------------------------------------------------------- K 线

    def fetch_rows(self, market_id: int, *, since_ms: Optional[int] = None,
                   now_ms: Optional[int] = None,
                   page_limit: int = PAGE_LIMIT,
                   max_pages: int = MAX_PAGES) -> List[dict]:
        """向前翻页拉原始 candle 行，直到覆盖 `since_ms`。返回升序、去重后的原始行。

        翻页契约（实测）：`start_timestamp=0 & end_timestamp=E & count_back=K` 返回
        「以 E 结尾的最近 K 根」。因此下一轮的 `E = 本页最早那根 t - 1`。
        页数用尽仍未到 `since_ms` 时抛 `FeedError`（不静默截断历史）。
        """
        return self._fetch_rows(market_id, since_ms=since_ms, now_ms=now_ms,
                                page_limit=page_limit, max_pages=max_pages)[0]

    def _fetch_rows(self, market_id: int, *, since_ms: Optional[int] = None,
                    now_ms: Optional[int] = None, page_limit: int = PAGE_LIMIT,
                    max_pages: int = MAX_PAGES) -> Tuple[List[dict], int]:
        """`fetch_rows` 的实现，额外返回实际请求页数。"""
        since = int(EARLIEST_MS if since_ms is None else since_ms)
        end = int(now_ms if now_ms is not None else self._now_ms())
        seen: Dict[int, dict] = {}
        pages = 0
        while True:
            if pages >= max_pages:
                raise FeedError(
                    f"market {market_id}: paged {max_pages} times without reaching "
                    f"since_ms={fmt_ms(since)}; history is truncated"
                )
            path = (f"candles?market_id={int(market_id)}&resolution={RESOLUTION}"
                    f"&start_timestamp=0&end_timestamp={end}&count_back={int(page_limit)}")
            payload = self._get_json(path)
            pages += 1
            page = payload.get("c") or []
            if not page:
                break
            earliest: Optional[int] = None
            for index, row in enumerate(page):
                try:
                    stamp = int(row["t"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise FeedError(
                        f"market {market_id}: candle row #{index} on page {pages} has no "
                        f"usable timestamp"
                    ) from exc
                seen[stamp] = row                    # 后取到的那份覆盖前者
                earliest = stamp if earliest is None else min(earliest, stamp)
            if earliest is None:
                break
            if earliest <= since:
                break
            if len(page) < int(page_limit):
                break                                # 短页 = 已到该市场历史起点
            end = earliest - 1
        return [seen[stamp] for stamp in sorted(seen)], pages

    def bars(self, symbol: str, *, since_ms: Optional[int] = None,
             now_ms: Optional[int] = None,
             max_age_ms: int = FRESH_MAX_AGE_MS,
             page_limit: int = PAGE_LIMIT,
             max_pages: int = MAX_PAGES,
             strict_freshness: bool = True) -> BarsResult:
        """拉 4h K 线并做完所有自检，返回 `BarsResult`。

        `result.bars` 直接喂 `app.strategy`：升序、去重、已收盘。
        `dropped_unclosed` 是被剔除的未收盘 bar（正常情况下 1 根，即当前正在形成的那根），
        `gaps` 是检测到的缺口。`strict_freshness=False` 时只报告不抛 `StaleFeedError`
        （给「事后补数据」这类场景用，实盘 tick 不该用它）。
        """
        wanted = str(symbol).upper()
        market_id = self.market_id_of(wanted)
        now = int(now_ms if now_ms is not None else self._now_ms())
        since = int(EARLIEST_MS if since_ms is None else since_ms)
        rows, pages = self._fetch_rows(market_id, since_ms=since, now_ms=now,
                                       page_limit=page_limit, max_pages=max_pages)
        all_bars = bars_from_rows(rows)
        if since > EARLIEST_MS:
            all_bars = [b for b in all_bars if b.open_ms >= since]
        closed, unclosed = split_unclosed(all_bars, now)
        if not closed:
            raise FeedError(
                f"{wanted}: no closed 4h bars in {len(rows)} row(s) "
                f"(since {fmt_ms(since)}, now {fmt_ms(now)})"
            )
        gaps = detect_gaps(closed)
        if strict_freshness:
            age = check_freshness(closed, now, max_age_ms=max_age_ms)
        else:
            age = now - (closed[-1].open_ms + BAR_MS)
        return BarsResult(
            symbol=wanted,
            market_id=market_id,
            bars=tuple(closed),
            dropped_unclosed=tuple(unclosed),
            gaps=tuple(gaps),
            rows_fetched=len(rows),
            pages=pages,
            since_ms=since,
            now_ms=now,
            last_close_age_ms=age,
            warnings=tuple(self.warnings),
        )

    def bar_list(self, symbol: str, **kwargs: Any) -> List[Bar]:
        """只要 `List[Bar]` 的便捷入口（自检照样执行）。"""
        return list(self.bars(symbol, **kwargs).bars)

    # -------------------------------------------------------------- 盘口

    def funding_rates(self, *, exchange: str = "lighter") -> Dict[str, float]:
        """各标的**当前资金费率**（公开端点 `/funding-rates`，无需签名）。

        返回 `{symbol: rate}`（只取 `exchange` 那一行；Lighter 的资金费周期是 **1 小时**，
        所以这个 rate 就是「每小时」的费率）。费率是多交易所并列返回的，必须按 exchange 过滤：
        拿 binance 的费率当 Lighter 的会误导人。
        """
        payload = self._get_json("funding-rates")
        rows = payload.get("funding_rates") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise FeedError("funding-rates payload has no 'funding_rates' list")
        wanted = str(exchange or "").strip().lower()
        out: Dict[str, float] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("exchange") or "").strip().lower() != wanted:
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            try:
                rate = float(row.get("rate"))
            except (TypeError, ValueError):
                continue
            if symbol and rate == rate:                       # 排除 NaN
                out[symbol] = rate
        return out

    def order_book(self, symbol: str, *, depth: int = 10) -> OrderBook:
        """盘口前 `depth` 档（按价格聚合后的档位）。

        深度来自 `/orderBookOrders`：`limit` 是**挂单数**上限（必填，缺省 400），
        因此聚合后的档位数 ≤ depth。
        """
        wanted = str(symbol).upper()
        market_id = self.market_id_of(wanted)
        limit = max(1, int(depth))
        payload = self._get_json(
            f"orderBookOrders?market_id={market_id}&limit={limit}")
        bids = _aggregate_levels(payload.get("bids"), descending=True)
        asks = _aggregate_levels(payload.get("asks"), descending=False)
        return OrderBook(
            symbol=wanted,
            market_id=market_id,
            bids=tuple(bids[:limit]),
            asks=tuple(asks[:limit]),
            total_bids=int(payload.get("total_bids") or 0),
            total_asks=int(payload.get("total_asks") or 0),
        )

    def snapshot(self, symbol: str, *, depth: int = 10) -> Snapshot:
        """行情快照：价格（口径见模块 docstring）+ 盘口前 N 档 + 标记价/指数价。"""
        wanted = str(symbol).upper()
        now = int(self._now_ms())
        book = self.order_book(wanted, depth=depth)
        info: Optional[MarketInfo] = None
        try:
            info = self.market_info(wanted)
        except FeedError as exc:
            self._warn(f"{wanted}: market details unavailable for snapshot ({exc})")

        price: Optional[float] = None
        source = ""
        if book.mid is not None:
            price, source = book.mid, "mid"
        else:
            # 盘口空（极端行情/维护中）：退到最近一根已收盘 bar 的收盘价。
            try:
                last = self.bars(wanted, since_ms=now - 2 * BAR_MS, now_ms=now)
                price, source = last.bars[-1].close, "last_close"
            except FeedError as exc:
                self._warn(f"{wanted}: no closed bar for snapshot fallback ({exc})")
            if price is None and info is not None and info.last_trade_price:
                price, source = info.last_trade_price, "last_trade"
        if price is None:
            raise FeedError(f"{wanted}: no price available (empty book, no closed bar, "
                            f"no last_trade_price)")

        return Snapshot(
            symbol=wanted,
            market_id=book.market_id,
            price=float(price),
            price_source=source,
            taken_at_ms=now,
            mid=book.mid,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            spread=book.spread,
            spread_bps=book.spread_bps,
            bids=book.bids,
            asks=book.asks,
            mark_price=info.mark_price if info else None,
            index_price=info.index_price if info else None,
            last_trade_price=info.last_trade_price if info else None,
            info=info,
        )


def _aggregate_levels(rows: Optional[Iterable[Mapping[str, Any]]],
                     *, descending: bool) -> List[Level]:
    """逐单 -> 按价格聚合的档位。同一价位多条合并；非正价格/零量跳过。"""
    merged: Dict[float, float] = {}
    for row in rows or ():
        try:
            price = float(row["price"])
            size = float(row.get("remaining_base_amount") or 0.0)
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedError(f"order book level is malformed: {exc!r}") from exc
        if price <= 0 or size <= 0:
            continue
        merged[price] = merged.get(price, 0.0) + size
    return [Level(price=p, size=merged[p]) for p in sorted(merged, reverse=descending)]


# ------------------------------------------------------------------ 默认单例


_default_clients: Dict[str, MarketClient] = {}


def default_client(environment: str = DEFAULT_ENVIRONMENT) -> MarketClient:
    """进程内共享的客户端（同一份限流器 —— 配额是全局限的，不是每标的）。"""
    name = (environment or DEFAULT_ENVIRONMENT).strip().lower()
    client = _default_clients.get(name)
    if client is None:
        client = MarketClient(name)
        _default_clients[name] = client
    return client
