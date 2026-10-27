"""交易执行层：**唯一**持私钥、**唯一**能下单的模块（PLAN 第 4 节「三条硬边界」之二）。

本模块把 T4 的 `OrderPlan`（已经是整数 tick）变成 Lighter 订单，签名，提交，并把交易所
返回的东西归一化成 `{ok, code, error}`。它不做策略判断、不写数据库、不决定仓位大小。

========================================================================
0. 安全模型（先读这一节，再读代码）
========================================================================

* **默认 dry-run**：`ExchangeClient(...)` 的 `dry_run` 默认 `True`。dry-run 下**不签名、
  不发任何网络请求**，只打印将要提交的订单。要签名做人工核对，必须显式传
  `preview_signature=True`（仍然不发请求）。
* **真实下单要三道独立开关**（缺一道就抛 `LiveTradingDisabled`，一行都不发）：
  1. 环境变量 `LIGHTER_LIVE_TRADING=1`（显式表态「这台机器允许真下单」）；
  2. 环境变量 `LIGHTER_LIVE_ACCOUNT_INDEX=<account_index>`，且必须**等于**配置里的
     `account_index`（防止把测试账户的开关带进实盘账户）；
  3. 调用方显式传入 `confirm_live=<account_index>`（T8/T10 的写操作逐个传，不是全局默认）。
* **私钥只在这里出现**：`app/` 下除本模块外没有任何 `lighter` import（`tests/test_exchange.py`
  用静态扫描钉住这条）。私钥不落盘、不进日志、不进异常消息 —— 所有异常消息只带
  字段名与数值，`__repr__` 里私钥打码。
* **L1 私钥根本不在本仓库的运行路径上**：注册 API key（`ChangePubKey`）需要钱包签名，
  那是 `scripts/offline_sign.py`，**在用户本机跑**，不在服务器上跑。

========================================================================
1. 两种签名（T7 只做第一种）
========================================================================

====================  ======================  =============================
签名                  用什么签                什么时候需要
====================  ======================  =============================
**L2 交易签名**        API 私钥 + api_key_index  下单/撤单/改单/杠杆/划转（本模块）
（`Sig` 字段）         + account_index         SDK 内部用 WASM signer 完成
**L1 签名**            钱包私钥 + chain_id      注册 API key（`ChangePubKey`）、
（`L1Sig` 字段）       （304 mainnet/300 test） fast withdrawal、转给别人
====================  ======================  =============================

`chain_id` 由 SDK 按 URL 推断（`signer_client.py` 的 `__init__`：mainnet → 304，
testnet → 300）；本模块显式传 `LIGHTER_ENVIRONMENT` 对应的值之后再断言一次，
免得将来换 URL 时静默落到 304 去签测试网的交易。

========================================================================
2. 实测确认的 SDK 行为（2026-10-27，`lighter-sdk==1.1.8`）
========================================================================

下面每一条都是在本机跑真实 SDK 得到的，不是读文档推断的：

* `lighter.create_api_key()` → `(private_key, public_key, err)`；私钥是 **0x + 80 hex**
  （40 字节），公钥是 0x + 128 hex。
* `SignerClient(url, account_index, api_private_keys={idx: pk}, nonce_management_type=...)`
  的构造函数**必须**在有 running event loop 的上下文里调用 —— 它内部要建
  `aiohttp.TCPConnector`，同步上下文里直接 `RuntimeError: no running event loop`。
  因此本模块的 SDK 是**懒加载**的，且只在 async 方法里建。
* 构造函数**不发网络请求**（nonce 是懒取的：`nonce_manager.NonceManager` 的 docstring 与
  `self.nonce = {}` 都印证），所以「只签名不提交」可以完全离线。
* `sign_create_order(...)` 返回 `(tx_type, tx_info_json, tx_hash, error)`，`tx_type=14`，
  `tx_hash` 是 **80 个 hex 字符**；**不发网络请求**。`tx_info` 是 16 个键的紧凑 JSON：
  `AccountIndex, ApiKeyIndex, MarketIndex, ClientOrderIndex, BaseAmount, Price, IsAsk,
  Type, TimeInForce, ReduceOnly, TriggerPrice, OrderExpiry, ExpiredAt, Nonce, Sig,
  L2TxAttributes`。
* **签名不是逐位确定的**：`ExpiredAt = now_ms + 599002`（约 +10 分钟，服务端给的交易
  过期时间）由 signer 自己填，所以同一组参数连签两次，`ExpiredAt`/`Sig`/`tx_hash` 都会变。
  其余 13 个字段逐字节相同 —— 测试据此断言「参数编码确定」，而不是断言 `tx_hash` 相等。
* `Sig` 是 **base64 的 80 字节**（108 字符，带 `=` 结尾）—— 直觉上的「64 字节 ECDSA
  签名」是错的，按 80 字节断言才对。
* **止损单的 `orderExpiry` 必须是真时间戳**：`order_expiry=0` 直接被 signer 拒
  （`OrderExpiry is invalid`）；传 SDK 的 `DEFAULT_28_DAY_ORDER_EXPIRY = -1` 会被展开成
  `now + 28 天`。止损单的 `TimeInForce` **必须是 IOC(0)**：传 GTT(1) 会得到
  `OrderTimeInForce is not valid`（signer 校验 type↔TIF 组合）。
* `ClientOrderIndex` 上限是 `2^48-1`（越界报 `ClientOrderIndex should not be larger than
  281474976710655`）。`api_key_index` 不在 `api_private_keys` 里时 signer 直接报
  `client is not created for apiKeyIndex: N accountIndex: M`。
* `sign_cancel_order` → `tx_type=15`；`sign_cancel_all_orders(tif=0, time=0,
  cancel_all_market_index=M)` → `tx_type=16`，且 `L2TxAttributes = {"5": M}`
  （指定市场时才有；`NIL_MARKET_INDEX=255` 时为 `null`，表示全市场）。
* `create_auth_token_with_expiry(deadline=<seconds>, api_key_index=...)` → `(auth, err)`，
  结构 `{expiry_unix}:{account_index}:{api_key_index}:{32 hex}`，每次调用随机不同
  （随机段），所以**缓存是刚需**：每生成一次都要走 signer。
* 错误响应是 **HTTP 400 + JSON `{"code": <int>, "message": "<str>"}`**（实测只读端点
  `/account?by=index&value=<不存在>` → `400 {"code":29404,"message":"not found"}`）。
  SDK 的 `ApiException.from_response` 在 400 时会带 `data=ResultCode(code, message)` 抛出；
  429/405 只带 HTTP status（文档：超限返回 429 或 405）。

========================================================================
3. nonce 规则（照抄文档 + 实测）
========================================================================

* 每个 **API key 一条独立 nonce 流**，默认 `new = previous + 1`；
  `GET /api/v1/nextNonce?account_index=&api_key_index=` 取下一个值。
* 被 **API server** 拒的（签名错、nonce 错、限流、余额不够挂单……）**不消耗 nonce**；
  被 **sequencer 应用时**拒成 no-op 的（非法价量、重复 `clientOrderIndex`、reduce-only
  方向错、`orderExpiry` 已过）**消耗** nonce。
* 文档的三条重试规则，本模块逐条实现：
  1. API server 拒 → 修好后**用同一个 nonce** 重发；
  2. `code=200` 但结果未知（超时/断连）→ **先查 `GET /api/v1/tx?by=hash`**，
     比对 `nextNonce`，**绝不盲目重签**；
  3. 用新 nonce 重签一个可能已经执行的单 = **可能下两次单**。
* 所以本模块的做法是：**签一次，之后所有重试都重发同一份 `tx_info`**（同一个 nonce、
  同一个 `tx_hash`）。这从结构上排除了「重试换 nonce」——测试直接断言两次尝试的
  `Nonce` 完全相同。
* 唯一例外是 `21104 invalid nonce`：这单肯定没进 sequencer（API server 拒的），
  但本地流已与链上不同步。本模块**不自动重签**，而是 `resync()` 一次 nonce 流
  并抛 `NonceConflict`，由调用方（T8）重新下单 —— 自动重签会让「谁在重试」变得模糊，
  而且 T8 有 `clientOrderIndex` 幂等键来兜底。

========================================================================
4. 订单类型选择（为什么是这几个）
========================================================================

* **入场 = MARKET + IOC，`price` = 最差可接受价**（PLAN 差异表「IO limit + 滑点上限」）。
  Lighter 的 `ORDER_TYPE_MARKET` 语义就是「在 `price` 以内立即成交，成交不了的部分撤掉」
  （docs `api/orders`：taker 单的 `price` 是 worst acceptable price）。用 MARKET/IOC
  而不是 GTT limit 的理由：这是**信号触发的追价单**，留着挂单会让系统在下一根 bar
  重新评估时还挂着上一根的意图（一仓模型下等于两个仓位）。`entry_slippage_bps`
  默认 30bp，`price_int = mid_int × (1 + slip)`（卖方向下）。
* **止损 = STOP_LOSS(2) + IOC + reduce_only=1**，`TriggerPrice = plan.stop_price_int`，
  `Price` = 触发后市价可接受的最差价（比触发价再让 `stop_slippage_bps`）。
  **触发口径是 mark price，不是 last price**：docs「Trigger Conditions」写的是
  `markPrice <= triggerPrice`（卖出止损）/ `markPrice >= triggerPrice`（买入止损）。
  选 mark 的理由：last price 会被一笔薄档成交瞬时打穿，造成假触发；mark price 掺了
  指数价，抗插针。这正是 PLAN 差异表「止损触发：last price → **mark price**」那一行。
  `orderExpiry` 取 **28 天**（`DEFAULT_28_DAY_ORDER_EXPIRY`）—— 止损单必须活得比持仓久，
  但文档允许的上限是 30 天，取 28 天留两天余量；持仓真的超过 28 天时 T8 的
  对账会因为「本地有仓/交易所没挂单」告警（宁可告警，也不要一个永不过期的挂单）。
* **离场 = MARKET + IOC + reduce_only=1**：信号平仓不追求价格，追求确定性出场。
* **`reduce_only=1` 的两条腿（止损、离场）不可能开出新仓**：即使方向算错、数量填错，
  交易所最多把它缩到 0，不会变成反向持仓。这是「减仓不受熔断阻挡」能成立的前提
  （PLAN T8：kill 只挡开新仓）。
* 可选的 **POST_ONLY 入场**（`place_entry(..., post_only=True)`）留给不想付 taker 费的
  场景：`ORDER_TYPE_LIMIT(0) + POST_ONLY(2)` + 真实 `orderExpiry`。默认不用 ——
  双均线突破需要成交，挂不上等于没信号。

关于 `price_protection`：`sendTx` 的表单里有一个布尔字段 `price_protection`
（OpenAPI spec 里 **default: true**，散文文档没有解释语义）。本模块**不覆盖**这个默认值
（`ExchangeClient(price_protection=None)` 时不传），因为服务端的语义没有文档背书；
真正的滑点上限由我们自己编码进 `price`（taker 单的 worst acceptable price），
这是文档明确规定的机制。如果服务端的 price protection 判定我们的最差价「离 mark 太远」，
会返回 `21734`/`21733` —— 按不可重试的 `rejected` 抛错（重试同样的价只会同样被拒）。

========================================================================
5. 错误分类（可重试 vs 不可重试）
========================================================================

分类函数是 `classify_error()`，返回 `(kind, retryable)`；`kind` 是**稳定字符串**
（面板/告警/测试按 `kind` 判断，不解析 message 文案）。

===============================  ==========================================  ========
kind                             典型 code / 条件                            重试?
===============================  ==========================================  ========
`transport`                      连接失败、超时、DNS、`aiohttp.ClientError`    是* 
`rate_limited`                   HTTP 429 / 405；code 21506（pending 太多）    是
`server_error`                   HTTP 5xx / 408 / 425                         是
`busy`                           code 21719/21720/21721/21722（挂单数满）       是
`nonce_conflict`                 code 21104 / 21105                           否**
`api_key_not_registered`         code 21108 / 21109 / 21110                   否
`insufficient_margin`            code 21739 / 21507 / 21508 / 21111 / 21112    否
`rejected`                       其余 4xx（精度非法、价格越界、reduce-only 方向 否
                                 错、市场冻结、重复 clientOrderIndex……）
===============================  ==========================================  ========

(*) `transport` 的重试**必须先查 `tx?by=hash`**：可能已经下单成功只是响应丢了。
(**) 见第 3 节：不自动重签，resync + 抛错，让 T8 用幂等键重来。

========================================================================
6. `code=200` 只表示「API 收下了」
========================================================================

文档原话：`200` 表示 API accepted，「still has to execute before the key can sign orders」/
「The sequencer can still reject it」。所以 `OrderResult.ok=True` 只代表**事务已提交**，
`confirmed=True` 才是「交易所确认过状态」。要确认就 `await wait_for_tx(tx_hash)` 或让
T8 走 WS `account_tx`。本模块的 `wait_for_tx` 轮询 `GET /tx?by=hash`（status：
`0 failed / 1 pending / 2 executed / 3 pending-final`），轮询间隔与超时都可注入（测试不真等）。

========================================================================
7. 与 T5 幂等键的对齐
========================================================================

`clientOrderIndex` 用 PLAN 差异表的公式：`sha256("ltab:{sym}:{bar_ms}:{purpose}")` 取
**高 48 bit**（`int.from_bytes(digest[:6], "big")`，0 改成 1 —— 0 是前端订单的保留值）。
`OrderResult.idempotency_key` 给 T5 的 `trades.idempotency_key` 用，优先级与 T5 一致：

    fill:<id>  >  order:<order_id>:<side>:<price>:<qty>  >  local:<client_order_index>

在拿到交易所 `fill_id` 之前，本模块给的是 `order:<client_order_index>:<side>:<price>:<qty>`
（`side` 用 `+1/-1`，`price`/`qty` 是整数 tick）—— 同一根 bar 的同一条腿重放会得到同一个键，
所以重试不会在本地记出第二笔成交。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Dict, Iterator, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from app.proxy import (
    ProxyConfigError, configuration_proxy_for, parse_proxy_url, redact_proxy_url,
)
from app.sizing import OrderPlan, to_int_price

__all__ = [
    # 常量
    "AUTH_TOKEN_DEFAULT_TTL_S", "AUTH_TOKEN_MAX_TTL_S", "AUTH_TOKEN_REFRESH_SKEW_S",
    "CHAIN_IDS", "CODE_OK", "DEFAULT_ENTRY_SLIPPAGE_BPS", "DEFAULT_STOP_SLIPPAGE_BPS",
    "LIVE_ACCOUNT_KEY", "LIVE_FLAG_KEY", "NIL_MARKET_INDEX",
    "ORDER_TYPE_LIMIT", "ORDER_TYPE_MARKET", "ORDER_TYPE_STOP_LOSS",
    "ORDER_TIME_IN_FORCE_GTT", "ORDER_TIME_IN_FORCE_IOC", "ORDER_TIME_IN_FORCE_POST_ONLY",
    "STOP_ORDER_EXPIRY_MS", "MAX_ORDER_EXPIRY_MS", "MIN_ORDER_EXPIRY_MS",
    "TX_TYPE_L2_CANCEL_ALL_ORDERS", "TX_TYPE_L2_CANCEL_ORDER", "TX_TYPE_L2_CREATE_ORDER",
    # 异常 / 结果
    "AmbiguousOrder", "ExchangeError", "LiveTradingDisabled", "NonceConflict",
    "NotConfigured", "OrderRejected", "OrderResult",
    # 数据
    "AuthTokenCache", "LiveGate", "NonceStream", "OrderSpec",
    # 纯函数
    "build_entry_order", "build_exit_order", "build_stop_order", "classify_error",
    "client_order_index", "idempotency_key_for", "price_int_from_float",
    "worst_price_int",
    # 客户端
    "ExchangeClient", "LighterSdk", "SdkBoundary",
]

# ------------------------------------------------------------------ 常量

# chain_id 表：与 signer_client 的 URL 推断一致，这里显式声明并**显式传给 SDK**，
# 不让它按 URL 自己猜（猜错 = 在另一个网络上签交易）。
CHAIN_IDS: Dict[str, int] = {"mainnet": 304, "testnet": 300}
API_HOSTS: Dict[str, str] = {
    "mainnet": "mainnet.zklighter.elliot.ai",
    "testnet": "testnet.zklighter.elliot.ai",
}

# SDK 的 `SignerClient(url=...)` 期望的是**站点根**（`https://<host>`），它自己会把
# `resource_path='/api/v1/...'` 拼上去。而我们对外的 base_url 是**带 `/api/v1` 的**
# （`app.market.BASE_URLS`，给 urllib 直连用）。
#
# 两者混用会得到重复路径 `/api/v1/api/v1/nextNonce` —— CloudFront 找不到资源，
# 回 **403 Forbidden**，看起来像「被地域限制」，实际是路径拼错（实测）。
def _site_root(base_url: str) -> str:
    """把 base_url 归一成**站点根**：`https://<host>`（去掉尾部 `/api/v1`）。

    全项目只有这一个地方做这件事：`app.executor` 的账户读取器拼的是
    `/api/v1/account` 这类**完整**路径，而 `config.base_url` 也带 `/api/v1`，
    不归一化就会拼出重复前缀（实测 403）。
    """
    parts = urlsplit(base_url)
    path = (parts.path or "").rstrip("/")
    if path.endswith("/api/v1"):
        path = path[: -len("/api/v1")]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


# 旧名保留：SDK 侧的语义就是这个（它要站点根）。
_sdk_api_root = _site_root

CODE_OK = 200

# L2 交易类型（docs `reference/data-structures-constants-and-errors`）。
TX_TYPE_L2_CREATE_ORDER = 14
TX_TYPE_L2_CANCEL_ORDER = 15
TX_TYPE_L2_CANCEL_ALL_ORDERS = 16

# 订单类型 / TimeInForce（`SignerClient.ORDER_TYPE_*` 实测值）。
ORDER_TYPE_LIMIT = 0
ORDER_TYPE_MARKET = 1
ORDER_TYPE_STOP_LOSS = 2

ORDER_TIME_IN_FORCE_IOC = 0
ORDER_TIME_IN_FORCE_GTT = 1
ORDER_TIME_IN_FORCE_POST_ONLY = 2

NIL_MARKET_INDEX = 255          # `SignerClient.NIL_MARKET_INDEX`：cancel-all 用「全市场」

# auth token 的有效期（Lighter 上限 8 小时）。SDK 的 `create_auth_token_with_expiry`
# 的入参是**相对秒数**，它会自己加上当前时间戳；传绝对时间戳会被服务端判成
# 「deadline 超过 8 小时上限」并返回 `invalid auth: invalid deadline`（实测）。
# 取 1 小时：远短于上限，也短到「万一刷新失败」时不会拖太久。
AUTH_TOKEN_TTL_S = 3600

DEFAULT_ENTRY_SLIPPAGE_BPS = 30   # 0.30%：双均线突破的追价上限
DEFAULT_STOP_SLIPPAGE_BPS = 100   # 1.00%：止损触发后允许的滑点（市价扫单，宽一点更可能成交）
DEFAULT_EXIT_SLIPPAGE_BPS = 50    # 0.50%：信号平仓

# `orderExpiry` 区间（docs：5 分钟 ~ 30 天）。止损单挂 28 天，留 2 天余量。
MIN_ORDER_EXPIRY_MS = 5 * 60 * 1000
MAX_ORDER_EXPIRY_MS = 30 * 24 * 3600 * 1000
STOP_ORDER_EXPIRY_MS = 28 * 24 * 3600 * 1000

# auth token：SDK 默认 10 分钟，上限 8 小时；提前 2 分钟刷新。
AUTH_TOKEN_DEFAULT_TTL_S = 10 * 60
AUTH_TOKEN_MAX_TTL_S = 8 * 3600
AUTH_TOKEN_REFRESH_SKEW_S = 120

# 真实下单的三道开关（见模块 docstring 第 0 节）。
LIVE_FLAG_KEY = "LIGHTER_LIVE_TRADING"
LIVE_ACCOUNT_KEY = "LIGHTER_LIVE_ACCOUNT_INDEX"
_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})

# 重试策略。抖动关掉：重试节奏要可断言，而限流退避本来就有服务端 429 兜底。
RETRY_BASE_S = 0.5
RETRY_MAX_S = 8.0
DEFAULT_MAX_ATTEMPTS = 3

# ------------------------------------------------------------------ 错误码表
# 数字全部来自 docs「Full error list」（`llms-full.txt`）实测粘贴，不要凭记忆改。

RETRYABLE_CODES = frozenset({
    21506,   # too many pending txs
    21719,   # maximum pending order count reached
    21720,   # maximum pending order count per market reached
    21721,   # maximum twap order count reached
    21722,   # maximum conditional order count reached
    21601,   # order book is full（重试基本没用，但它是瞬时的、不动库存，归可重试）
})
NONCE_CONFLICT_CODES = frozenset({21104, 21105})       # invalid nonce / non-increasing nonce
NOT_REGISTERED_CODES = frozenset({21108, 21109, 21110})  # invalid pubkey / key not found / bad idx
INSUFFICIENT_CODES = frozenset({
    21739,   # not enough margin to create the order
    21507,   # account below maintenance margin
    21508,   # account below initial margin
    21111,   # pre-liquidation
    21112,   # in liquidation
})
DUPLICATE_CODES = frozenset({21728})                   # clientOrderIndex exists → 幂等命中
RETRYABLE_HTTP = frozenset({405, 408, 425, 429})        # 文档：超限返回 429 或 405


# ------------------------------------------------------------------ 异常


class ExchangeError(RuntimeError):
    """执行层错误基类。`as_dict()` 就是 PLAN 要的 `{ok, code, error}`。

    `code` 是**字符串**（稳定的 kind，见 `classify_error`），交易所的数字码在
    `exchange_code` 里。消息里**绝不能**出现私钥/签名 —— 子类构造时只接受字段名与数值。
    """

    kind = "exchange_error"

    def __init__(self, message: str, *, exchange_code: Optional[int] = None,
                 retryable: bool = False, detail: Optional[Mapping[str, Any]] = None,
                 http_status: Optional[int] = None) -> None:
        self.message = str(message)
        self.exchange_code = exchange_code
        self.retryable = bool(retryable)
        self.http_status = http_status
        self.detail: Dict[str, Any] = dict(detail or {})
        super().__init__(self.message)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ok": False,
            "code": self.kind,
            "error": self.message,
            "retryable": self.retryable,
        }
        if self.exchange_code is not None:
            out["exchange_code"] = self.exchange_code
        if self.http_status is not None:
            out["http_status"] = self.http_status
        if self.detail:
            out["detail"] = dict(self.detail)
        return out

    def __str__(self) -> str:
        return self.message


class NotConfigured(ExchangeError):
    """缺 SDK / 缺凭据 / 配置非法 —— fail-closed，绝不用默认值顶上。"""

    kind = "not_configured"


class LiveTradingDisabled(NotConfigured):
    """真实下单开关没开（默认状态）。这不是 bug，是本模块最重要的默认值。"""

    kind = "live_trading_disabled"


class OrderRejected(ExchangeError):
    """交易所明确拒绝（不可重试的那种）。"""

    kind = "rejected"


class NonceConflict(ExchangeError):
    """`21104/21105`：本地 nonce 流与链上不同步。不自动重签，见模块 docstring 第 3 节。"""

    kind = "nonce_conflict"

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class AmbiguousOrder(ExchangeError):
    """提交结果未知（超时/断连）且查不到 tx 状态 —— **不要**重签，交给对账（T8）。"""

    kind = "ambiguous"


# ------------------------------------------------------------------ 结果


@dataclass(frozen=True)
class OrderResult:
    """一次下单/撤单的可序列化结果。

    `ok=True` 且 `dry_run=False` 时，含义是「API 接受了这个事务」（`code=200`）；
    `confirmed=True` 才是「交易所确认执行过」。`dry_run=True` 时表示「这份订单是合法的，
    但我什么都没发」。
    """

    ok: bool
    action: str                      # entry / stop / exit / cancel_all / cancel_all_markets
    code: Optional[int] = None       # 交易所数字码；dry-run 为 None
    error: str = ""
    kind: str = "ok"                 # 失败时是 classify_error 的 kind
    symbol: str = ""
    market_id: Optional[int] = None
    side: int = 0
    qty_int: Optional[int] = None
    price_int: Optional[int] = None
    trigger_price_int: Optional[int] = None
    reduce_only: bool = False
    order_type: Optional[int] = None
    time_in_force: Optional[int] = None
    client_order_index: Optional[int] = None
    idempotency_key: str = ""
    nonce: Optional[int] = None
    tx_type: Optional[int] = None
    tx_hash: str = ""
    attempts: int = 0
    retryable: bool = False
    dry_run: bool = False
    signed: bool = False
    confirmed: bool = False
    signature_digest: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code if self.code is not None else self.kind,
            "error": self.error,
            "kind": self.kind,
            "action": self.action,
            "symbol": self.symbol,
            "market_id": self.market_id,
            "side": self.side,
            "qty_int": self.qty_int,
            "price_int": self.price_int,
            "trigger_price_int": self.trigger_price_int,
            "reduce_only": self.reduce_only,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "client_order_index": self.client_order_index,
            "idempotency_key": self.idempotency_key,
            "nonce": self.nonce,
            "tx_type": self.tx_type,
            "tx_hash": self.tx_hash,
            "attempts": self.attempts,
            "retryable": self.retryable,
            "dry_run": self.dry_run,
            "signed": self.signed,
            "confirmed": self.confirmed,
            "signature_digest": dict(self.signature_digest),
        }


# ------------------------------------------------------------------ 纯函数：价格/幂等键


def _check_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def worst_price_int(price_int: int, slippage_bps: int, *, is_ask: bool) -> int:
    """把「期望价」放宽成「最差可接受价」（整数 tick，方向朝不利侧取整）。

    * 买单（`is_ask=False`）：`price × (1 + bps)`，**向上**取整 —— 少算一个 tick
      就会变成「最差价低于盘口」，IOC 直接不成交。
    * 卖单（`is_ask=True`）：`price × (1 - bps)`，**向下**取整。

    例（实测写死的值见测试）：`650000 @ 30bp` 买 → `651950`；卖 → `648050`。
    """
    base = _check_int(price_int, "price_int", minimum=1)
    bps = _check_int(slippage_bps, "slippage_bps", minimum=0)
    if bps == 0:
        return base
    if is_ask:
        return base * (10_000 - bps) // 10_000          # floor：卖得更便宜一点可以接受
    num = base * (10_000 + bps)
    return -((-num) // 10_000)                          # ceil：买得更贵一点可以接受


def price_int_from_float(price: float, price_decimals: int) -> int:
    """float 价格 → 整数 tick（**floor**，与 `app.sizing.to_int_price` 同口径）。"""
    return to_int_price(price, price_decimals)


def client_order_index(symbol: str, bar_ms: int, purpose: str) -> int:
    """PLAN 差异表的幂等键：`sha256("ltab:{sym}:{bar_ms}:{purpose}")` 的高 48 bit。

    0 会被改成 1：`clientOrderIndex=0` 是前端订单的保留值，用它会让「撤单/查询」指错单。
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    bar = _check_int(bar_ms, "bar_ms", minimum=1)
    pur = str(purpose or "").strip().lower()
    if not pur:
        raise ValueError("purpose is required")
    digest = hashlib.sha256(f"ltab:{sym}:{bar}:{pur}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:6], "big")           # 48 bit
    return value or 1


# 构建函数的参数名 `client_order_index` 与上面的函数同名，内部统一用这个别名，
# 免得写出 `globals()["client_order_index"]` 那种「能跑但没人看得懂」的调用。
_derive_coi = client_order_index


def _resolve_coi(explicit: Optional[int], symbol: str, bar_ms: Optional[int],
                 now_ms: int, purpose: str) -> int:
    coi = explicit if explicit is not None else _derive_coi(
        symbol, now_ms if bar_ms is None else bar_ms, purpose)
    _check_int(coi, "client_order_index", minimum=1)
    if coi > 2 ** 48 - 1:
        raise ValueError(f"client_order_index must fit in uint48, got {coi}")
    return coi


def idempotency_key_for(spec: "OrderSpec") -> str:
    """T5 的 `trades.idempotency_key` 中间档：`order:<coi>:<side>:<price>:<qty>`。

    `side` 用**持仓方向**（与 `trades.side` 同义：+1 多 / -1 空），不是订单方向 ——
    否则多单的止损腿会变成 `-1`，与 T5 记账口径不一致。
    """
    return f"order:{spec.client_order_index}:{spec.position_side}:{spec.price_int}:{spec.qty_int}"


# ------------------------------------------------------------------ 订单规格


@dataclass(frozen=True)
class OrderSpec:
    """一份**完全成形**的 Lighter 订单（全部整数 tick）—— 送进 signer 的就是这些字段。

    构造只走 `build_entry_order` / `build_stop_order` / `build_exit_order`，它们负责
    「`OrderPlan` → 整数 tick」的映射与 fail-loud 校验；不要手搓。

    两个 `side` 字段含义不同，请不要混用（这是最容易写错的地方）：

    * `side` —— **订单方向**（`+1` 买 / `-1` 卖）。签名用的 `IsAsk` 由它推导。
    * `position_side` —— **持仓方向**（`+1` 多 / `-1` 空）。平仓腿的订单方向与它相反。
    """

    purpose: str                     # entry / stop / exit / cancel_all / cancel_all_markets
    symbol: str
    market_id: int
    side: int                        # 订单方向：+1 买 / -1 卖
    qty_int: int
    price_int: int
    order_type: int
    time_in_force: int
    reduce_only: bool
    trigger_price_int: int
    order_expiry_ms: int
    client_order_index: int
    position_side: int = 0           # 持仓方向（entry 与 side 相同；平仓腿相反）

    @property
    def is_ask(self) -> int:
        return 0 if self.side > 0 else 1

    @property
    def action(self) -> str:
        return self.purpose

    def as_dict(self) -> Dict[str, Any]:
        return {
            "purpose": self.purpose,
            "symbol": self.symbol,
            "market_id": self.market_id,
            "side": self.side,
            "position_side": self.position_side,
            "is_ask": self.is_ask,
            "qty_int": self.qty_int,
            "price_int": self.price_int,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "reduce_only": self.reduce_only,
            "trigger_price_int": self.trigger_price_int,
            "order_expiry_ms": self.order_expiry_ms,
            "client_order_index": self.client_order_index,
        }

    def describe(self) -> str:
        """dry-run 打印用的一行人类可读描述（不含任何 secret）。"""
        side = "BUY" if self.side > 0 else "SELL"
        kind = {ORDER_TYPE_LIMIT: "LIMIT", ORDER_TYPE_MARKET: "MARKET",
                ORDER_TYPE_STOP_LOSS: "STOP_LOSS"}.get(self.order_type, f"TYPE{self.order_type}")
        tif = {ORDER_TIME_IN_FORCE_IOC: "IOC", ORDER_TIME_IN_FORCE_GTT: "GTT",
               ORDER_TIME_IN_FORCE_POST_ONLY: "POST_ONLY"}.get(
                   self.time_in_force, f"TIF{self.time_in_force}")
        bits = [
            f"[dry-run] {self.purpose.upper()} {side} {self.qty_int} (int ticks)"
            f" {self.symbol} market_id={self.market_id}",
            f"type={kind}+{tif} price_int={self.price_int}",
            f"reduce_only={int(self.reduce_only)}",
            f"trigger_price_int={self.trigger_price_int}"
            f" order_expiry_ms={self.order_expiry_ms}",
            f"client_order_index={self.client_order_index}",
        ]
        return " | ".join(bits)

    def sign_kwargs(self) -> Dict[str, Any]:
        """传给 `SignerClient.sign_create_order` 的参数（nonce/api_key_index 由调用方补）。"""
        return {
            "market_index": self.market_id,
            "client_order_index": self.client_order_index,
            "base_amount": self.qty_int,
            "price": self.price_int,
            "is_ask": self.is_ask,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "reduce_only": self.reduce_only,
            "trigger_price": self.trigger_price_int,
            "order_expiry": self.order_expiry_ms,
        }


def _require_plan(plan: OrderPlan) -> OrderPlan:
    if not isinstance(plan, OrderPlan):
        raise ValueError(f"plan must be an app.sizing.OrderPlan, got {type(plan).__name__}")
    _check_int(plan.qty_int, "plan.qty_int", minimum=1)
    _check_int(plan.stop_price_int, "plan.stop_price_int", minimum=1)
    _check_int(plan.market_id, "plan.market_id", minimum=0)
    if plan.side not in (1, -1):
        raise ValueError(f"plan.side must be +1 or -1, got {plan.side!r}")
    if plan.price_decimals < 0 or plan.size_decimals < 0:
        raise ValueError("plan decimals must be >= 0")
    if plan.entry_price <= 0:
        raise ValueError(f"plan.entry_price must be > 0, got {plan.entry_price!r}")
    return plan


def _entry_price_int(plan: OrderPlan) -> int:
    return price_int_from_float(plan.entry_price, plan.price_decimals)


def build_entry_order(plan: OrderPlan, *, now_ms: int, bar_ms: Optional[int] = None,
                      slippage_bps: int = DEFAULT_ENTRY_SLIPPAGE_BPS,
                      client_order_index: Optional[int] = None,
                      post_only: bool = False,
                      limit_price_int: Optional[int] = None) -> OrderSpec:
    """`OrderPlan` → 入场订单（MARKET/IOC + 最差价；或 POST_ONLY 限价）。

    * `qty_int` 原样用 `plan.qty_int`（T4 已经 floor 过，这里**不再**动数量）。
    * 价格用 `plan.entry_price`（bar 收盘的参考价）→ 整数 tick → 加滑点放宽。
      `plan` 里没有 `entry_price_int` 字段：入场价不是 T4 的产出（它只保证止损价对齐），
      所以这里用 `price_decimals` 自己换算，并且**不**声称它是成交价。
    * `post_only=True` 时改用 `ORDER_TYPE_LIMIT + POST_ONLY`，并必须给真实
      `orderExpiry`（GTT 才允许挂单）。
    """
    p = _require_plan(plan)
    now = _check_int(now_ms, "now_ms", minimum=1)
    bps = _check_int(slippage_bps, "slippage_bps", minimum=0)
    coi = _resolve_coi(client_order_index, p.symbol, bar_ms, now, "entry")

    reference = _check_int(limit_price_int, "limit_price_int", minimum=1) \
        if limit_price_int is not None else _entry_price_int(p)
    price = reference if post_only else worst_price_int(reference, bps, is_ask=p.side < 0)
    if post_only:
        expiry = _clamp_expiry(now, now + STOP_ORDER_EXPIRY_MS)
        order_type, tif = ORDER_TYPE_LIMIT, ORDER_TIME_IN_FORCE_POST_ONLY
    else:
        expiry, order_type, tif = 0, ORDER_TYPE_MARKET, ORDER_TIME_IN_FORCE_IOC

    return OrderSpec(
        purpose="entry", symbol=p.symbol, market_id=p.market_id, side=p.side,
        position_side=p.side,
        qty_int=p.qty_int, price_int=price, order_type=order_type, time_in_force=tif,
        reduce_only=False, trigger_price_int=0, order_expiry_ms=expiry,
        client_order_index=coi,
    )


def build_stop_order(plan: OrderPlan, *, now_ms: int, bar_ms: Optional[int] = None,
                     slippage_bps: int = DEFAULT_STOP_SLIPPAGE_BPS,
                     client_order_index: Optional[int] = None) -> OrderSpec:
    """`OrderPlan` → 止损单：STOP_LOSS + IOC + **reduce_only** + mark price 触发。

    `TriggerPrice` 直接用 `plan.stop_price_int` —— T4 已经按「朝入场价方向」对齐到 tick
    （多单 ceil / 空单 floor），再动它就会破坏「实际风险 ≤ 目标风险」这个不变式。
    `Price`（触发后的最差成交价）在触发价基础上再让 `slippage_bps`。

    `orderExpiry` 必须是真时间戳（实测：0 会被 signer 拒成 `OrderExpiry is invalid`），
    取 28 天；`TimeInForce` 必须是 IOC（实测：GTT 会被拒成 `OrderTimeInForce is not valid`）。
    """
    p = _require_plan(plan)
    now = _check_int(now_ms, "now_ms", minimum=1)
    bps = _check_int(slippage_bps, "slippage_bps", minimum=0)
    coi = _resolve_coi(client_order_index, p.symbol, bar_ms, now, "stop")

    entry_int = _entry_price_int(p)
    stop_int = _check_int(p.stop_price_int, "plan.stop_price_int", minimum=1)
    if p.side > 0 and stop_int >= entry_int:
        raise ValueError(
            f"{p.symbol} long stop {stop_int} must be below entry {entry_int} "
            f"(price_int, price_decimals={p.price_decimals})")
    if p.side < 0 and stop_int <= entry_int:
        raise ValueError(
            f"{p.symbol} short stop {stop_int} must be above entry {entry_int} "
            f"(price_int, price_decimals={p.price_decimals})")

    # 平仓腿：订单方向与持仓**相反**（多单止损 = 卖出）。
    order_side = -p.side
    price = worst_price_int(stop_int, bps, is_ask=order_side < 0)
    return OrderSpec(
        purpose="stop", symbol=p.symbol, market_id=p.market_id, side=order_side,
        position_side=p.side,
        qty_int=p.qty_int, price_int=price, order_type=ORDER_TYPE_STOP_LOSS,
        time_in_force=ORDER_TIME_IN_FORCE_IOC, reduce_only=True,
        trigger_price_int=stop_int,
        order_expiry_ms=_clamp_expiry(now, now + STOP_ORDER_EXPIRY_MS),
        client_order_index=coi,
    )


def build_exit_order(*, symbol: str, market_id: int, side: int, qty_int: int,
                     price_decimals: int, exit_price: float, now_ms: int,
                     bar_ms: Optional[int] = None,
                     slippage_bps: int = DEFAULT_EXIT_SLIPPAGE_BPS,
                     client_order_index: Optional[int] = None) -> OrderSpec:
    """信号离场单：MARKET + IOC + **reduce_only**（只减仓，永不反手）。

    `side` 是**持仓**方向（+1 多），订单方向自动取反（卖平多）。这一点很容易写错，
    所以参数名用 `side` 而不是 `is_ask`，并在这里显式换算。
    """
    if side not in (1, -1):
        raise ValueError(f"side must be +1 or -1, got {side!r}")
    now = _check_int(now_ms, "now_ms", minimum=1)
    qty = _check_int(qty_int, "qty_int", minimum=1)
    mid = _check_int(market_id, "market_id", minimum=0)
    bps = _check_int(slippage_bps, "slippage_bps", minimum=0)
    base = price_int_from_float(exit_price, int(price_decimals))
    coi = _resolve_coi(client_order_index, symbol, bar_ms, now, "exit")
    order_side = -side                                  # 多单平仓 = 卖出
    return OrderSpec(
        purpose="exit", symbol=str(symbol).upper(), market_id=mid, side=order_side,
        position_side=side,
        qty_int=qty, price_int=worst_price_int(base, bps, is_ask=order_side < 0),
        order_type=ORDER_TYPE_MARKET, time_in_force=ORDER_TIME_IN_FORCE_IOC,
        reduce_only=True, trigger_price_int=0, order_expiry_ms=0,
        client_order_index=coi,
    )


def _clamp_expiry(now_ms: int, expiry_ms: int) -> int:
    """把 `orderExpiry` 收到文档允许的 `[now+5min, now+30d]` 区间内（fail-loud 地）。

    这个本地校验比 signer 严格：signer **不**拿墙上时钟校验过期时间（实测传一个已经过去的
    时间戳它照签不误），而 sequencer 会把「`orderExpiry` 已过」的单当成 no-op **消耗 nonce**。
    所以过期判断必须在我们这边做，不能指望签名环节报错。
    """
    lo, hi = now_ms + MIN_ORDER_EXPIRY_MS, now_ms + MAX_ORDER_EXPIRY_MS
    if not (lo <= expiry_ms <= hi):
        raise ValueError(
            f"order_expiry_ms must be within [now+5min, now+30d], "
            f"got {expiry_ms - now_ms}ms from now")
    return expiry_ms


# ------------------------------------------------------------------ 错误分类


def classify_error(*, exchange_code: Optional[int] = None,
                   http_status: Optional[int] = None,
                   message: str = "") -> Tuple[str, bool]:
    """`(kind, retryable)`。表格见模块 docstring 第 5 节。

    判定顺序：数字码优先于 HTTP status（数字码更具体），status 优先于文案；
    文案只用来兜底识别限流（个别网关只回文案不回码）。
    """
    code = exchange_code
    if code is not None:
        if code in NOT_REGISTERED_CODES:
            return "api_key_not_registered", False
        if code in NONCE_CONFLICT_CODES:
            return "nonce_conflict", False
        if code in INSUFFICIENT_CODES:
            return "insufficient_margin", False
        if code in DUPLICATE_CODES:
            return "duplicate", False
        if code in RETRYABLE_CODES:
            return "busy", True
    if http_status is not None:
        if http_status in RETRYABLE_HTTP:
            return "rate_limited", True
        if http_status >= 500:
            return "server_error", True
    low = (message or "").lower()
    if "rate limit" in low or "too many requests" in low:
        return "rate_limited", True
    return "rejected", False


def _parse_api_error_body(body: Optional[str]) -> Tuple[Optional[int], str]:
    """从 `ApiException.body`（可能是 JSON，也可能是 SDK 拼的多行文本）里挖 code/message。"""
    text = (body or "").strip()
    if not text:
        return None, ""
    candidate = text
    if "HTTP response body:" in text:
        candidate = text.split("HTTP response body:", 1)[1].strip()
    try:
        payload = json.loads(candidate)
    except (ValueError, TypeError):
        return None, text.splitlines()[-1] if text.splitlines() else ""
    if isinstance(payload, dict):
        code = payload.get("code")
        msg = payload.get("message") or payload.get("error") or ""
        return (int(code) if isinstance(code, int) else None), str(msg)
    return None, str(payload)


# ------------------------------------------------------------------ 真实下单开关


@dataclass(frozen=True)
class LiveGate:
    """三道开关的组合（见模块 docstring 第 0 节）。**任何一道不满足都拒绝下单。**"""

    enabled: bool = False
    declared_account: Optional[int] = None
    confirm: Optional[int] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *,
                 confirm: Optional[int] = None) -> "LiveGate":
        source: Mapping[str, str] = env if env is not None else os.environ
        raw_flag = str(source.get(LIVE_FLAG_KEY) or "").strip().lower()
        raw_account = str(source.get(LIVE_ACCOUNT_KEY) or "").strip()
        declared: Optional[int] = None
        if raw_account:
            try:
                declared = int(raw_account, 10)
            except ValueError:
                raise NotConfigured(
                    f"{LIVE_ACCOUNT_KEY} must be a base-10 integer") from None
        return cls(enabled=raw_flag in _TRUTHY, declared_account=declared,
                   confirm=None if confirm is None else int(confirm))

    def assert_allowed(self, account_index: int) -> None:
        if not self.enabled:
            raise LiveTradingDisabled(
                f"live trading is disabled: set {LIVE_FLAG_KEY}=1 to enable it "
                f"(dry-run is the default, and nothing is ever sent without this)")
        if self.declared_account is None:
            raise LiveTradingDisabled(
                f"live trading is enabled but {LIVE_ACCOUNT_KEY} is unset — refusing to "
                f"guess which account this run is for")
        if int(self.declared_account) != int(account_index):
            raise LiveTradingDisabled(
                f"{LIVE_ACCOUNT_KEY}={self.declared_account} does not match the configured "
                f"account_index={account_index}")
        if self.confirm is None:
            raise LiveTradingDisabled(
                "live trading needs an explicit per-call confirmation "
                "(confirm_live=<account_index>); it is not a global default")
        if int(self.confirm) != int(account_index):
            raise LiveTradingDisabled(
                f"confirm_live={self.confirm} does not match the configured "
                f"account_index={account_index}")


# ------------------------------------------------------------------ nonce


class NonceStream:
    """单 API key 的 nonce 流：懒取、+1 分配、可回滚（API 拒的 tx 不消耗 nonce）。

    **绝不**在重试路径上调 `allocate()` —— 重试复用的是已签好的那份 `tx_info`
    （`ExchangeClient._send_signed`），所以 nonce 天生不变。`rollback()` 只在
    「这单确定没进 sequencer，且以后也不发了」时用。
    """

    def __init__(self, fetch_next: Callable[[], Awaitable[int]],
                 *, clock: Callable[[], float] = time.time) -> None:
        self._fetch_next = fetch_next
        self._next: Optional[int] = None
        self._clock = clock
        self.fetched_at: Optional[float] = None
        self.allocations = 0
        self.rollbacks = 0
        self.fetches = 0

    @property
    def cached(self) -> Optional[int]:
        return self._next

    async def allocate(self) -> int:
        if self._next is None:
            await self.resync()
        assert self._next is not None
        nonce = self._next
        self._next = nonce + 1
        self.allocations += 1
        return nonce

    async def resync(self) -> int:
        """丢掉本地缓存，重新问 `nextNonce`。"""
        value = await self._fetch_next()
        self._next = int(value)
        self.fetched_at = self._clock()
        self.fetches += 1
        return self._next

    def rollback(self, nonce: int) -> bool:
        """把「刚分配但确认没被消耗」的 nonce 还回去。

        只有它还是**最后一个**分配值时才允许回滚（否则会覆盖后续已发出的 nonce）。
        """
        if self._next is not None and self._next == int(nonce) + 1:
            self._next = int(nonce)
            self.rollbacks += 1
            return True
        return False

    def note_consumed(self, nonce: int) -> None:
        """确认 nonce 已被消耗（sequencer 应用成 no-op 也算）→ 保证缓存领先于它。"""
        if self._next is None or self._next <= int(nonce):
            self._next = int(nonce) + 1


# ------------------------------------------------------------------ auth token


class AuthTokenCache:
    """认证 token 缓存：按需生成，**过期前 `skew_s` 秒**刷新。

    为什么要缓存：生成一次要过 signer（`CreateAuthToken` 走 WASM），每次请求都生成
    等于给每个 REST 调用加一次签名开销；而 token 里带随机段，本身也不可复用推导。
    为什么要刷新而不是「用到失败再换」：token 过期直接 401，把「中途失败」变成
    「这一轮 tick 白跑」，不如提前换。

    token 结构（实测）：`{expiry_unix}:{account_index}:{api_key_index}:{32 hex}`。
    过期时间从 token 自己解析，不靠本地计时器 —— 免得机器时钟跳变后缓存永不失效。
    """

    def __init__(self, mint: Callable[[int], Tuple[str, Optional[str]]], *,
                 ttl_s: int = AUTH_TOKEN_DEFAULT_TTL_S,
                 skew_s: int = AUTH_TOKEN_REFRESH_SKEW_S,
                 clock: Callable[[], float] = time.time) -> None:
        ttl = int(ttl_s)
        if not (60 <= ttl <= AUTH_TOKEN_MAX_TTL_S):
            raise ValueError(
                f"auth token ttl must be within [60, {AUTH_TOKEN_MAX_TTL_S}]s, got {ttl}")
        self._mint = mint
        self.ttl_s = ttl
        self.skew_s = int(skew_s)
        self._clock = clock
        self._token: Optional[str] = None
        self.expires_at: Optional[float] = None
        self.mints = 0

    # ---------------------------------------------------------------- 查询

    @staticmethod
    def parse_expiry(token: str) -> Optional[float]:
        try:
            head, *_rest = str(token).split(":")
            return float(int(head, 10))
        except (ValueError, TypeError, AttributeError):
            return None

    def is_fresh(self) -> bool:
        if not self._token or self.expires_at is None:
            return False
        return self._clock() + self.skew_s < self.expires_at

    # ---------------------------------------------------------------- 取用

    def get(self) -> str:
        if self.is_fresh():
            assert self._token is not None
            return self._token
        return self.refresh()

    def refresh(self) -> str:
        token, err = self._mint(self.ttl_s)
        if err or not token:
            raise NotConfigured(
                "could not mint an auth token (the API key may not be registered, or the "
                f"signer is unavailable): {str(err or 'empty token')[:200]}")
        expiry = self.parse_expiry(token)
        if expiry is None:
            raise NotConfigured("auth token has an unexpected format (no expiry prefix)")
        self._token = token
        self.expires_at = expiry
        self.mints += 1
        return token

    def invalidate(self) -> None:
        self._token = None
        self.expires_at = None


# ------------------------------------------------------------------ SDK 边界


class SdkBoundary:
    """本模块需要的 SDK 能力（真实实现 `LighterSdk`，测试用假的替身）。

    抽这一层的唯一目的：让「重试复用同一 nonce」「dry-run 不发请求」「只签名不提交」
    这三件事能在**完全离线**的条件下被断言。真实实现只是对 `SignerClient` 的一一转发。
    """

    account_index: int
    api_key_index: int
    chain_id: int

    def sign_create_order(self, **kwargs: Any) -> Tuple[Any, Any, Any, Any]:
        raise NotImplementedError

    def sign_cancel_all_orders(self, **kwargs: Any) -> Tuple[Any, Any, Any, Any]:
        raise NotImplementedError

    def sign_cancel_order(self, **kwargs: Any) -> Tuple[Any, Any, Any, Any]:
        raise NotImplementedError

    def mint_auth_token(self, ttl_s: int) -> Tuple[str, Optional[str]]:
        raise NotImplementedError

    async def send_tx(self, tx_type: int, tx_info: str) -> Any:
        raise NotImplementedError

    async def next_nonce(self) -> int:
        raise NotImplementedError

    async def tx_status(self, tx_hash: str) -> Optional[Mapping[str, Any]]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


_CONFIGURATION_LOCK = threading.RLock()


@contextmanager
def _inject_configuration_proxy(proxy_url: Optional[str]) -> Iterator[Optional[str]]:
    """在 `SignerClient(...)` 构造期间给 SDK 的 `Configuration.proxy` 赋值。

    为什么必须这么绕（实测 `lighter-sdk==1.1.8`，三条都是读源码 + 实跑确认的）：

    * SDK 的 `SignerClient.__init__` 里写死了 `Configuration(host=url)`，没有入参可传；
    * `Configuration.__init__` **不接受** `proxy=` 关键字参数 —— `self.proxy: Optional[str]
      = None` 是构造完才赋的一个普通属性（`configuration.py:190`）。所以「传 proxy= 进去」
      这条路不存在，只能构造完**立刻**赋值；
    * 时机必须是**在 `ApiClient` 被建之前**：`RestClientObject.__init__`（`rest.py:76`）
      把 `configuration.proxy` 抄进 `self.proxy`，`request()` 再把它塞进每次调用的
      `proxy=` 参数（`rest.py:159-162`）。构造完（`rest_client.proxy` 已定）再改
      `Configuration` 就没用了 —— 那正是「以为走代理其实直连」。

    做法：把 `lighter.signer_client` 模块里的 `Configuration` 名字临时换成一个包装：
    先按原样构造，再给 `proxy` 赋值（原本已有值就不覆盖），构造完在 `finally` 里还原。

    * 不动 `lighter.configuration.Configuration` 本体，也不动任何环境变量；
    * 加锁，因为替换的是模块级全局名字（同进程并发构造会互相踩）。
    """
    if not proxy_url:
        yield None
        return
    import lighter.signer_client as signer_client            # 函数内 import：模块级不碰 lighter

    with _CONFIGURATION_LOCK:
        original = signer_client.Configuration

        def _configuration_with_proxy(*args: Any, **kwargs: Any) -> Any:
            configuration = original(*args, **kwargs)
            if getattr(configuration, "proxy", None) in (None, ""):
                configuration.proxy = proxy_url
            return configuration

        signer_client.Configuration = _configuration_with_proxy   # type: ignore[assignment]
        try:
            yield proxy_url
        finally:
            signer_client.Configuration = original                # type: ignore[assignment]


def _sdk_rest_proxy(signer: Any) -> Optional[str]:
    """读回 SDK **实际生效**的代理：`SignerClient.api_client.rest_client.proxy`。

    `rest.py:76` 把 `Configuration.proxy` 抄进 `rest_client.proxy`，那个值就是每次请求
    真正传给 aiohttp 的 `proxy=`。用它自证「代理确实用上了」—— 否则就是「以为走代理
    其实直连」，最后以 20558（restricted jurisdiction）的形式在实盘里炸出来。
    """
    rest_client = getattr(getattr(signer, "api_client", None), "rest_client", None)
    return getattr(rest_client, "proxy", None)


# CloudFront 在 Lighter 域名前面，会按 User-Agent 拦掉「像脚本」的客户端。
# 实测：aiohttp 默认 UA（`Python/3.x aiohttp/...`）打 /nextNonce → 403 CloudFront；
# 换成这个 UA 后同一个请求 200。SDK 的 `rest.py` 建 `aiohttp.ClientSession` 时
# **不传 headers**，所以 UA 只能是 aiohttp 的默认值 —— 必须在这里补上。
SDK_USER_AGENT = "lighter-ma-bot/0.2.0"


def _configure_sdk_user_agent(signer: Any, *, user_agent: str = SDK_USER_AGENT) -> None:
    """给 SDK 的 HTTP 层补上 User-Agent。

    **实测出来的必要条件**，不是洁癖：`rest.py` 建
    `aiohttp.ClientSession(connector=..., trust_env=True)` 时不传 `headers=`，
    aiohttp 于是用默认 UA。CloudFront 对着 Lighter 域名会把这种 UA 判成脚本
    客户端，返回 **403 Forbidden**（响应头 `Server: CloudFront`）。换成
    `lighter-ma-bot/0.2.0` 后同一请求返回 200。

    Authorization **不在这里设**：SDK 的 `nextNonce` 端点在 OpenAPI spec 里
    把 `auth_settings` 写成空列表（`api/transaction_api.py:_next_nonce_serialize`
    里 `_auth_settings: List[str] = []`），所以 `Configuration.api_key` 根本
    不会被读进请求头。鉴权只能**显式**传 `_headers={"Authorization": ...}`，
    见 `LighterSdk.auth_headers()`。
    """
    api_client = getattr(signer, "api_client", None)
    rest_client = getattr(api_client, "rest_client", None)
    if api_client is None:
        raise NotConfigured(
            "the SDK did not expose an ApiClient object; cannot configure "
            "User-Agent (refusing to run with a default aiohttp User-Agent, "
            "which CloudFront rejects with 403)")
    api_client.user_agent = user_agent
    api_client.default_headers["User-Agent"] = user_agent
    session = getattr(rest_client, "pool_manager", None)
    if session is not None:
        session.headers["User-Agent"] = user_agent


class LighterSdk(SdkBoundary):
    """官方 `lighter.SignerClient` 的薄封装（**唯一** import lighter 的地方）。

    关键选择：`nonce_management_type=NONE`（`NoOpNonceManager`）。SDK 自己的
    `OptimisticNonceManager.next_nonce()` 在缓存未命中的第一次会走 **同步** `requests.get`
    （`nonce_manager.get_nonce_from_api`）——在 async 进程里那是阻塞调用，而且它会在
    `nonce=-1` 时把 nonce 交给 signer 自己去取。本模块坚持「nonce 由 `NonceStream`
    显式管理 + 每次签名都显式传 nonce」，因此把 SDK 的 nonce 管理彻底关掉。
    """

    def __init__(self, *, base_url: str, account_index: int, api_key_index: int,
                 api_private_key: str, environment: str,
                 price_protection: Optional[bool] = None,
                 proxy_url: Optional[str] = None) -> None:
        try:
            import lighter                                   # 懒加载：只是 import 也要在 async 里
        except Exception as exc:                             # pragma: no cover - 环境问题
            raise NotConfigured(
                f"lighter-sdk is not importable ({type(exc).__name__}); the trading layer is "
                f"fail-closed without it") from None
        if not base_url:
            raise NotConfigured("base_url is required to build the signer client")
        if not api_private_key:
            raise NotConfigured("an API private key is required to build the signer client")
        if int(api_key_index) < 4 or int(api_key_index) > 254:
            # 0–3 是 Lighter 官方前端保留位；255 是「查全部 key」的只读哨兵值。
            raise NotConfigured(
                f"api_key_index must be in 4..254 (0-3 are reserved by Lighter's apps), "
                f"got {int(api_key_index)}")
        expected_chain = CHAIN_IDS.get(str(environment).lower())
        if expected_chain is None:
            raise NotConfigured(f"unknown environment {environment!r}")
        expected_host = API_HOSTS[str(environment).lower()]
        host = urlsplit(base_url).hostname or ""
        if host != expected_host:
            # 在**建 signer 之前**校验：URL 与 environment 不一致时宁可拒绝，
            # 也不要让 chain_id 跟着 URL 漂到另一个网络上去签。
            raise NotConfigured(
                f"base_url host {host!r} does not belong to environment "
                f"{environment!r} (expected {expected_host!r})")

        from lighter import nonce_manager as _nonce_manager
        from lighter.signer_client import SignerClient

        # SDK 的 url 必须是**站点根**：它自己会拼 `resource_path='/api/v1/...'`。
        # 直接把我们对外的 base_url（含 `/api/v1`）传进去会得到
        # `/api/v1/api/v1/nextNonce` ⇒ CloudFront 403（看起来像地域限制，其实是路径错）。
        sdk_url = _sdk_api_root(base_url)

        # 代理：**只**在 base_url 的 host 属于 Lighter 时才注入（`should_proxy` 的判定），
        # 并且走 `Configuration.proxy` 这个 SDK 原生字段，**不**设 `HTTPS_PROXY` ——
        # `rest.py` 的 `aiohttp.ClientSession(trust_env=True)` 会读环境变量，一设就把同
        # 进程里其它 aiohttp 用例也带偏了。SDK 里 `Configuration(host=url)` 是写死的
        # (`signer_client.py:420`)，所以靠 `_inject_configuration_proxy` 在构造期间注入。
        # 另外 `proxy_url` 为空 / URL 非法时这一层什么都不做（非法在 Config 那层已经拦下）。
        effective_proxy = configuration_proxy_for(base_url, proxy_url)

        # 构造函数必须有 running loop（内部建 aiohttp connector）——调用方保证在 async 里。
        # `chain_id` **显式传入**：SDK 自己会按 URL 猜（mainnet→304 / testnet→300），
        # 猜错的后果是签了个「另一个网络」的交易，所以这里不给它猜的机会。
        with _inject_configuration_proxy(effective_proxy):
            self._signer = SignerClient(
                url=sdk_url,
                account_index=int(account_index),
                api_private_keys={int(api_key_index): str(api_private_key)},
                nonce_management_type=_nonce_manager.NonceManagerType.NONE,
                chain_id=int(expected_chain),
            )
        if effective_proxy:
            # 自证：`rest.py:76` 会把 `Configuration.proxy` 抄进 `rest_client.proxy`，而
            # 那个值就是每次请求传给 aiohttp 的 `proxy=`。没抄到 = 以为走代理其实直连 =
            # 实盘里以 20558 的形式炸出来。宁可在这里拒绝启动。
            applied = _sdk_rest_proxy(self._signer)
            if applied != effective_proxy:
                applied_label = redact_proxy_url(applied) or "None"
                # 签名器已经建好（它的 aiohttp session 没人接手）—— 这条路径只在 SDK 行为
                # 变化时触发，进程随即拒绝启动，所以不做收尾关闭：宁可留下一句
                # ResourceWarning，也不要让「代理没生效」悄悄跑下去。
                raise NotConfigured(
                    "the SDK did not apply the configured proxy "
                    f"({redact_proxy_url(effective_proxy)}): rest_client.proxy is "
                    f"{applied_label!r}. Refusing to run — a silently direct "
                    "connection hits error 20558 (restricted jurisdiction) at the "
                    "first sendTx, long after startup.")
        self.proxy_url = effective_proxy
        self.account_index = int(account_index)
        self.api_key_index = int(api_key_index)
        self.chain_id = int(self._signer.chain_id)
        self._price_protection = price_protection
        if self.chain_id != expected_chain:                   # 防御：SDK 忽略了入参
            raise NotConfigured(
                f"signer chain_id {self.chain_id} != expected {expected_chain} for "
                f"environment {environment!r} (refusing to sign on the wrong chain)")

        # HTTP 身份（User-Agent）：SDK 自带的 HTTP 层没有 UA，CloudFront 会按
        # aiohttp 的默认 UA 把请求判成脚本客户端并回 403。必须在 signer 建好之后
        # 配置（Configuration 是 SDK 内部建的，没有入参可传）。
        _configure_sdk_user_agent(self._signer)

        # auth token 缓存：token 有 TTL（上限 8 小时），每次请求现签太贵；
        # 用失败才换又会把「中途失败」变成「这一轮 tick 白跑」。缓存按过期前
        # `skew_s` 秒刷新，过期时间从 token 自己解析（不信本地时钟）。
        self._auth_cache = AuthTokenCache(
            self.mint_auth_token, ttl_s=AUTH_TOKEN_TTL_S, skew_s=AUTH_TOKEN_REFRESH_SKEW_S)

        del lighter                                          # 不在模块级留引用

    # ---------------------------------------------------------------- 签名

    def sign_create_order(self, **kwargs: Any) -> Tuple[Any, Any, Any, Any]:
        return self._signer.sign_create_order(**kwargs)

    def sign_cancel_all_orders(self, **kwargs: Any) -> Tuple[Any, Any, Any, Any]:
        return self._signer.sign_cancel_all_orders(**kwargs)

    def sign_cancel_order(self, **kwargs: Any) -> Tuple[Any, Any, Any, Any]:
        return self._signer.sign_cancel_order(**kwargs)

    def mint_auth_token(self, ttl_s: int) -> Tuple[str, Optional[str]]:
        token, err = self._signer.create_auth_token_with_expiry(
            deadline=int(ttl_s), api_key_index=self.api_key_index)
        return token, err

    def auth_headers(self) -> Dict[str, str]:
        """返回带 Authorization 的请求头。

        为什么必须**显式**传，而不能靠 `Configuration.api_key`：SDK 的
        `nextNonce` 端点在 OpenAPI spec 里把 `auth_settings` 写成空列表
        （`api/transaction_api.py:_next_nonce_serialize` 里 `_auth_settings:
        List[str] = []`），于是 `api_client.param_serialize` 的
        `for auth in auth_settings:` 一次都不执行 —— **不管 `Configuration.api_key`
        里有没有 token，请求都不会带上 Authorization 头**。

        实测（mainnet，经日本代理）：手工加头 → `/nextNonce` 200
        `{"code":200,"nonce":1}`；不加 → 403 CloudFront。

        SDK 的 `*_with_http_info` 都接受 `_headers=` 入参，且
        `_header_params = _headers or {}` 会原样带上 —— 所以走这条路。

        失败**不静默**：签不出 token 就抛 `NotConfigured`，绝不退化成
        「不带 Authorization 头也试试」—— 那种失败模式在实盘里表现为
        「偶发下单失败」，是最难查的一种。
        """
        token = self._auth_cache.get()
        return {"Authorization": token, "User-Agent": SDK_USER_AGENT}

    # ---------------------------------------------------------------- 网络

    async def send_tx(self, tx_type: int, tx_info: str) -> Any:
        # 直接调 TransactionApi，而不是 SignerClient.create_order 的包装：
        # 包装把 `send_tx` 的异常压成 `(None, None, <最后一行字符串>)`，数字错误码就丢了，
        # 而 T7 的错误分类必须拿到数字码。这里拿到的仍是同一个 API 响应对象
        # （200 → RespSendTx；400 → 抛 ApiException 且 `data` 是 ResultCode）。
        return await self._signer.tx_api.send_tx(
            tx_type=tx_type, tx_info=tx_info, price_protection=self._price_protection)

    async def next_nonce(self) -> int:
        # `_headers=` 是必须的：SDK 的 nextNonce 端点没声明鉴权
        # （`_auth_settings: List[str] = []`），不显式给 Authorization 就 403。
        # 细节见 `auth_headers()` 的 docstring。
        resp = await self._signer.tx_api.next_nonce(
            account_index=self.account_index, api_key_index=self.api_key_index,
            _headers=self.auth_headers())
        return int(resp.nonce)

    async def tx_status(self, tx_hash: str) -> Optional[Mapping[str, Any]]:
        # `_headers=` 同 `next_nonce`：SDK 的 transaction_api 端点都没声明鉴权
        # （`_auth_settings: List[str] = []`），不显式给 Authorization 就是 403。
        try:
            tx = await self._signer.tx_api.tx(by="hash", value=tx_hash,
                                              _headers=self.auth_headers())
        except Exception as exc:                              # 未找到 / 400 / 404
            body = getattr(exc, "body", None) or str(exc)
            code, _msg = _parse_api_error_body(body)
            if code in (404, 29404) or getattr(exc, "status", None) in (400, 404):
                return None
            raise
        return tx.to_dict() if hasattr(tx, "to_dict") else dict(tx)  # type: ignore[arg-type]

    async def close(self) -> None:
        await self._signer.close()


# ------------------------------------------------------------------ 执行层


class ExchangeClient:
    """Lighter 交易执行层。**默认 dry-run**。

    典型用法（T8）：

        client = ExchangeClient.from_config(cfg)                 # dry_run=True
        await client.place_entry(plan, bar_ms=bar_ms)            # 只打印
        client = ExchangeClient.from_config(cfg, env=os.environ, confirm_live=cfg.account_index)
        result = await client.place_entry(plan, bar_ms=bar_ms)   # 三道开关齐全才真发

    参数
    ----
    config: `app.config.Config`（要 `account_index` / `api_key_index` / `api_private_key` /
            `environment` / `base_url`）。
    sdk:    可注入的 `SdkBoundary`（默认懒建真实 `LighterSdk`）。给测试用。
    dry_run: 默认 `True`。`False` 时仍要过 `LiveGate`。
    preview_signature: dry-run 下是否**只签名**以产出摘要（不发请求）。
    """

    def __init__(self, config: Any, *, sdk: Optional[SdkBoundary] = None,
                 dry_run: bool = True, preview_signature: bool = False,
                 live_gate: Optional[LiveGate] = None,
                 entry_slippage_bps: int = DEFAULT_ENTRY_SLIPPAGE_BPS,
                 stop_slippage_bps: int = DEFAULT_STOP_SLIPPAGE_BPS,
                 exit_slippage_bps: int = DEFAULT_EXIT_SLIPPAGE_BPS,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 price_protection: Optional[bool] = None,
                 clock: Callable[[], float] = time.time,
                 now_ms: Optional[Callable[[], int]] = None,
                 sleep: Optional[Callable[[float], Awaitable[None]]] = None) -> None:
        self.account_index = int(getattr(config, "account_index"))
        self.api_key_index = int(getattr(config, "api_key_index"))
        self.environment = str(getattr(config, "environment", "mainnet"))
        self.base_url = str(getattr(config, "base_url"))
        self._api_private_key = str(getattr(config, "api_private_key", ""))
        # 代理同样走 `LIGHTER_PROXY_URL`（`Config.proxy_url`，空串 = 直连）。这里再校验一次
        # 是因为 `ExchangeClient` 也接受任意 config 形状（测试/脚本），不能假设上游一定校验过。
        self.proxy_url = parse_proxy_url(getattr(config, "proxy_url", "") or "") or ""
        self.dry_run = bool(dry_run)
        self.preview_signature = bool(preview_signature)
        self.live_gate = live_gate if live_gate is not None else LiveGate()
        self.entry_slippage_bps = int(entry_slippage_bps)
        self.stop_slippage_bps = int(stop_slippage_bps)
        self.exit_slippage_bps = int(exit_slippage_bps)
        self.max_attempts = max(1, int(max_attempts))
        self.price_protection = price_protection
        self._clock = clock
        self._now_ms = now_ms or (lambda: int(clock() * 1000))
        self._sleep = sleep or asyncio.sleep
        self._sdk = sdk
        self._owns_sdk = sdk is None
        self._nonces: Optional[NonceStream] = None
        self._auth: Optional[AuthTokenCache] = None

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_config(cls, config: Any, *, env: Optional[Mapping[str, str]] = None,
                    sdk: Optional[SdkBoundary] = None, dry_run: bool = True,
                    confirm_live: Optional[int] = None, **kwargs: Any) -> "ExchangeClient":
        """从 `Config` 建客户端；`confirm_live` 是**每次运行**的显式确认（不是默认值）。"""
        gate = LiveGate.from_env(env, confirm=confirm_live)
        return cls(config, sdk=sdk, dry_run=dry_run, live_gate=gate, **kwargs)

    def __repr__(self) -> str:
        proxy_label = redact_proxy_url(self.proxy_url) or "<direct>"
        return (f"ExchangeClient(account_index={self.account_index}, "
                f"api_key_index={self.api_key_index}, environment={self.environment!r}, "
                f"dry_run={self.dry_run}, proxy_url={proxy_label!r}, "
                f"api_private_key='***')")

    __str__ = __repr__

    # ---------------------------------------------------------------- 惰性资源

    def sdk(self) -> SdkBoundary:
        """拿 SDK（第一次调用时才建，且必须在 async 上下文里——见 `LighterSdk`）。"""
        if self._sdk is None:
            self._sdk = LighterSdk(
                base_url=self.base_url, account_index=self.account_index,
                api_key_index=self.api_key_index, api_private_key=self._api_private_key,
                environment=self.environment, price_protection=self.price_protection,
                proxy_url=self.proxy_url)
        return self._sdk

    @property
    def nonces(self) -> NonceStream:
        if self._nonces is None:
            self._nonces = NonceStream(lambda: self.sdk().next_nonce(), clock=self._clock)
        return self._nonces

    @property
    def auth(self) -> AuthTokenCache:
        if self._auth is None:
            self._auth = AuthTokenCache(lambda ttl: self.sdk().mint_auth_token(ttl),
                                        clock=self._clock)
        return self._auth

    async def aclose(self) -> None:
        if self._sdk is not None and self._owns_sdk:
            await self._sdk.close()
            self._sdk = None

    # ---------------------------------------------------------------- 下单入口

    async def place_entry(self, plan: OrderPlan, *, bar_ms: Optional[int] = None,
                          client_order_index: Optional[int] = None,
                          post_only: bool = False,
                          limit_price_int: Optional[int] = None) -> OrderResult:
        """开仓。`bar_ms` 参与 `clientOrderIndex` 的推导（同一根 bar 同一个键）。"""
        spec = build_entry_order(
            plan, now_ms=self._now_ms(), bar_ms=bar_ms, slippage_bps=self.entry_slippage_bps,
            client_order_index=client_order_index, post_only=post_only,
            limit_price_int=limit_price_int)
        return await self._submit(spec)

    async def place_stop(self, plan: OrderPlan, *, bar_ms: Optional[int] = None,
                         client_order_index: Optional[int] = None) -> OrderResult:
        """挂止损（reduce_only + mark price 触发）。"""
        spec = build_stop_order(
            plan, now_ms=self._now_ms(), bar_ms=bar_ms, slippage_bps=self.stop_slippage_bps,
            client_order_index=client_order_index)
        return await self._submit(spec)

    async def place_exit(self, *, symbol: str, market_id: int, side: int, qty_int: int,
                         price_decimals: int, exit_price: float,
                         bar_ms: Optional[int] = None,
                         client_order_index: Optional[int] = None) -> OrderResult:
        """信号平仓（reduce_only + MARKET/IOC）。"""
        spec = build_exit_order(
            symbol=symbol, market_id=market_id, side=side, qty_int=qty_int,
            price_decimals=price_decimals, exit_price=exit_price, now_ms=self._now_ms(),
            bar_ms=bar_ms, slippage_bps=self.exit_slippage_bps,
            client_order_index=client_order_index)
        return await self._submit(spec)

    async def cancel_all(self, market_id: int) -> OrderResult:
        """撤掉某个市场的所有挂单（`SignCancelAllOrders` + market index）。"""
        _check_int(market_id, "market_id", minimum=0)
        if market_id > 254:
            raise ValueError(f"market_id must be <= 254, got {market_id}")
        return await self._submit_cancel_all(market_id)

    async def cancel_all_orders(self) -> OrderResult:
        """撤掉**全市场**的所有挂单（`NIL_MARKET_INDEX=255`）。"""
        return await self._submit_cancel_all(NIL_MARKET_INDEX)

    # ---------------------------------------------------------------- 提交

    async def _submit(self, spec: OrderSpec) -> OrderResult:
        base = self._base_result(spec)
        if self.dry_run:
            return await self._dry_run(spec, base)
        self.live_gate.assert_allowed(self.account_index)      # 抛 LiveTradingDisabled
        return await self._send_spec(spec, base)

    async def _submit_cancel_all(self, market_id: int) -> OrderResult:
        action = "cancel_all_markets" if market_id == NIL_MARKET_INDEX else "cancel_all"
        base = OrderResult(ok=True, action=action, market_id=market_id)
        if self.dry_run:
            label = ("ALL markets" if market_id == NIL_MARKET_INDEX else f"market {market_id}")
            print(f"[dry-run] CANCEL_ALL {label} (nothing signed, nothing sent)")
            return replace(base, dry_run=True)

        self.live_gate.assert_allowed(self.account_index)
        sdk = self.sdk()
        nonce = await self.nonces.allocate()
        tx_type, tx_info, tx_hash, err = sdk.sign_cancel_all_orders(
            time_in_force=0, timestamp_ms=0, cancel_all_market_index=market_id,
            skip_nonce=0, nonce=nonce, api_key_index=self.api_key_index)
        if err:
            # 签名就没过 = 肯定没进 sequencer，nonce 未消耗 → 还回去。
            self.nonces.rollback(nonce)
            kind, retryable = classify_error(message=str(err))
            raise OrderRejected(
                f"cancel-all signing failed: {err}", retryable=retryable,
                detail={"market_id": market_id, "signer_error": str(err)})
        return await self._send_signed(base, tx_type=tx_type, tx_info=tx_info,
                                       tx_hash=tx_hash, nonce=nonce,
                                       action_label="cancel_all")

    def _base_result(self, spec: OrderSpec) -> OrderResult:
        return OrderResult(
            ok=True, action=spec.purpose, symbol=spec.symbol, market_id=spec.market_id,
            # `side` 报**持仓方向**（与 store 的 trades.side 同义）；订单方向在
            # `signature_digest`/`OrderSpec` 里，不必在结果里再放一份容易看错的数字。
            side=spec.position_side, qty_int=spec.qty_int, price_int=spec.price_int,
            trigger_price_int=spec.trigger_price_int, reduce_only=spec.reduce_only,
            order_type=spec.order_type, time_in_force=spec.time_in_force,
            client_order_index=spec.client_order_index,
            idempotency_key=idempotency_key_for(spec))

    async def _dry_run(self, spec: OrderSpec, base: OrderResult) -> OrderResult:
        """打印订单；只有显式 `preview_signature=True` 才签名（仍不发请求）。"""
        print(spec.describe())
        if not self.preview_signature:
            return replace(base, dry_run=True, signed=False)
        sdk = self.sdk()                                       # 需要凭据；不发网络请求
        digest = self._sign_digest(sdk, spec, nonce=None)
        print(f"[dry-run] signature preview: {json.dumps(dict(digest), sort_keys=True)}")
        return replace(base, dry_run=True, signed=True, signature_digest=digest,
                       tx_type=digest.get("tx_type"))

    # ---------------------------------------------------------------- 签名

    def _sign_digest(self, sdk: SdkBoundary, spec: OrderSpec,
                     *, nonce: Optional[int]) -> Dict[str, Any]:
        """签一份**用于预览**的订单，只回摘要（长度/前后缀），不提交、不用真实 nonce。

        预览用 nonce=0 是安全的：这份 tx_info 永远不会被发送，而 signer 只要求
        nonce 是非负整数（真实发送路径一定用 `NonceStream` 分配的值）。
        """
        use = 0 if nonce is None else int(nonce)
        tx_type, tx_info, tx_hash, err = sdk.sign_create_order(
            **spec.sign_kwargs(), skip_nonce=0, nonce=use,
            api_key_index=self.api_key_index)
        if err:
            raise NotConfigured(f"signing failed for the preview: {err}")
        parsed = json.loads(tx_info)
        sig = str(parsed.get("Sig") or "")
        return {
            "tx_type": tx_type,
            "tx_info_len": len(tx_info),
            "tx_hash_prefix": str(tx_hash)[:12],
            "tx_hash_suffix": str(tx_hash)[-8:],
            "tx_hash_len": len(str(tx_hash)),
            "sig_len": len(sig),
            "sig_prefix": sig[:8],
            "nonce": parsed.get("Nonce"),
            "expired_at_ms": parsed.get("ExpiredAt"),
        }

    # ---------------------------------------------------------------- 发送 + 重试

    async def _send_spec(self, spec: OrderSpec, base: OrderResult) -> OrderResult:
        """签**一次**，然后带着这一份 `tx_info` 重试（nonce 因而不变）。"""
        sdk = self.sdk()
        nonce = await self.nonces.allocate()
        tx_type, tx_info, tx_hash, err = sdk.sign_create_order(
            **spec.sign_kwargs(), skip_nonce=0, nonce=nonce,
            api_key_index=self.api_key_index)
        if err:
            self.nonces.rollback(nonce)
            raise OrderRejected(
                f"signing failed for {spec.symbol} {spec.purpose}: {err}",
                detail={"symbol": spec.symbol, "purpose": spec.purpose,
                        "signer_error": str(err)})
        parsed = json.loads(tx_info)
        if int(parsed.get("Nonce", -1)) != int(nonce):
            raise ExchangeError(
                f"signer used nonce {parsed.get('Nonce')} but {nonce} was allocated — "
                f"refusing to submit (nonce accounting would diverge)")
        sig = str(parsed.get("Sig") or "")
        digest = {"tx_type": tx_type, "tx_info_len": len(tx_info),
                  "tx_hash_prefix": str(tx_hash)[:12], "tx_hash_suffix": str(tx_hash)[-8:],
                  "sig_len": len(sig), "sig_prefix": sig[:8]}
        return await self._send_signed(base, tx_type=tx_type, tx_info=tx_info,
                                       tx_hash=tx_hash, nonce=nonce,
                                       action_label=spec.purpose, digest=digest)

    async def _send_signed(self, base: OrderResult, *, tx_type: int, tx_info: str,
                           tx_hash: str, nonce: int, action_label: str,
                           digest: Optional[Mapping[str, Any]] = None) -> OrderResult:
        """提交已签好的事务；重试**只重发**，绝不重签（nonce 因此恒定）。

        每次可重试失败后、重发之前，若结果可能已经落地（传输错误），先查
        `GET /tx?by=hash`：已执行就结束；已失败且 nonce 未被消耗才重发；查不到也重发
        （同一份签名，最多两次执行同一个 nonce，sequencer 只会接受第一次）。
        """
        sdk = self.sdk()
        last_kind, last_msg = "transport", ""
        last_code: Optional[int] = None
        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            attempts = attempt
            try:
                resp = await sdk.send_tx(tx_type, tx_info)
            except Exception as exc:                            # noqa: BLE001 - 分类后重抛
                kind, retryable, code, msg, status = self._classify_exception(exc)
                last_kind, last_msg, last_code = kind, msg, code
                if kind == "nonce_conflict":
                    # API server 拒的：这单没进 sequencer，nonce 未消耗。不重签、不重发。
                    await self._handle_nonce_conflict(nonce, msg, code)   # 必定抛错
                if not retryable:
                    self.nonces.rollback(nonce)
                    raise self._error_for(kind, msg, code=code, http_status=status, base=base)
                # 结果可能已经落地：先查 tx 状态，再决定是否重发同一份签名。
                resolved = await self._resolve_ambiguous(tx_hash, nonce)
                if resolved is not None:
                    return replace(base, ok=resolved["ok"], code=resolved["code"],
                                   error=resolved["error"], kind=resolved["kind"],
                                   nonce=nonce, tx_type=tx_type, tx_hash=tx_hash,
                                   attempts=attempts, confirmed=resolved["confirmed"],
                                   signed=True, signature_digest=dict(digest or {}))
                if attempt == self.max_attempts:
                    break
                await self._sleep(min(RETRY_BASE_S * 2 ** (attempt - 1), RETRY_MAX_S))
                continue

            code, msg = self._read_send_response(resp)
            if code == CODE_OK:
                return replace(base, ok=True, code=CODE_OK, error="", kind="accepted",
                               nonce=nonce, tx_type=tx_type, tx_hash=tx_hash,
                               attempts=attempts, signed=True,
                               signature_digest=dict(digest or {}))
            kind, retryable = classify_error(exchange_code=code, message=msg)
            last_kind, last_msg, last_code = kind, msg, code
            if kind == "nonce_conflict":
                await self._handle_nonce_conflict(nonce, msg, code)       # 必定抛错
            if not retryable:
                # API server 拒 = nonce 没被消耗 → 还回去，下一单复用它。
                self.nonces.rollback(nonce)
                raise self._error_for(kind, msg, code=code, base=base)
            if attempt == self.max_attempts:
                break
            await self._sleep(min(RETRY_BASE_S * 2 ** (attempt - 1), RETRY_MAX_S))

        # 重试用尽：结果未知 → **不回滚** nonce（可能已被消耗），交给对账。
        raise AmbiguousOrder(
            f"{action_label} {base.symbol or ''} outcome unknown after {attempts} "
            f"attempt(s): {last_kind}: {last_msg}".strip(),
            exchange_code=last_code, retryable=True,
            detail={"tx_hash": tx_hash, "nonce": nonce, "attempts": attempts,
                    "last_kind": last_kind})

    # ---------------------------------------------------------------- 响应/异常解析

    @staticmethod
    def _read_send_response(resp: Any) -> Tuple[Optional[int], str]:
        """`send_tx` 的返回可能是 `RespSendTx`（200）或 `ResultCode`（被拒）。"""
        code = getattr(resp, "code", None)
        msg = getattr(resp, "message", None) or ""
        if isinstance(resp, Mapping):
            code = resp.get("code", code)
            msg = resp.get("message", msg) or ""
        return (int(code) if isinstance(code, int) else None), str(msg)

    @staticmethod
    def _classify_exception(exc: BaseException) -> Tuple[str, bool, Optional[int], str,
                                                         Optional[int]]:
        """异常 → `(kind, retryable, exchange_code, message, http_status)`。"""
        body = getattr(exc, "body", None)
        status = getattr(exc, "status", None)
        code, msg = _parse_api_error_body(body)
        if code is None:
            data = getattr(exc, "data", None)
            data_code = getattr(data, "code", None)
            if isinstance(data_code, int):
                code = data_code
                msg = msg or str(getattr(data, "message", "") or "")
        if code is not None or isinstance(status, int):
            kind, retryable = classify_error(exchange_code=code, http_status=status,
                                            message=msg or str(exc))
            return kind, retryable, code, msg or str(exc)[:300], status
        # 传输层（连接失败/超时）：结果未知，可重试但要先查状态。
        return "transport", True, None, f"{type(exc).__name__}: {exc}"[:300], None

    @staticmethod
    def _error_for(kind: str, message: str, *, code: Optional[int],
                   base: OrderResult, http_status: Optional[int] = None) -> ExchangeError:
        """`kind` → 具体异常类。`kind` 同时写成**实例属性**，`as_dict()["code"]` 才是它。"""
        detail = {"symbol": base.symbol, "market_id": base.market_id,
                  "purpose": base.action,
                  "client_order_index": base.client_order_index}
        text = f"{base.action} {base.symbol or ''} rejected: {message}".strip()
        if kind == "api_key_not_registered":
            exc: ExchangeError = NotConfigured(
                text + " — the API key is not registered for this account; run "
                       "scripts/offline_sign.py on your own machine (it needs the L1 key) "
                       "and verify with GET /api/v1/apikeys",
                exchange_code=code, detail=detail, http_status=http_status)
        elif kind == "nonce_conflict":
            exc = NonceConflict(
                text + " — nonce stream resynced; re-place the order "
                       "(the clientOrderIndex makes that retry idempotent)",
                exchange_code=code, detail=detail, http_status=http_status)
        else:
            exc = OrderRejected(text, exchange_code=code, retryable=False, detail=detail,
                                http_status=http_status)
        exc.kind = kind                                      # 实例属性覆盖类属性
        return exc

    async def _handle_nonce_conflict(self, nonce: int, message: str,
                                     code: Optional[int]) -> None:
        """`21104/21105`：**不重签**，只把本地 nonce 流拉回链上值，然后抛错给调用方。"""
        try:
            await self.nonces.resync()
        except Exception:                                        # noqa: BLE001
            pass                                                 # resync 失败也照样抛 NonceConflict
        raise NonceConflict(
            f"invalid nonce {nonce}: {message} (local sequence resynced; order NOT re-signed)",
            exchange_code=code, detail={"nonce": nonce})

    async def _resolve_ambiguous(self, tx_hash: str,
                                 nonce: int) -> Optional[Dict[str, Any]]:
        """传输错误后查 `GET /tx?by=hash`：已执行 → 定论；未执行 → None（可以重发）。"""
        sdk = self.sdk()
        try:
            status = await sdk.tx_status(tx_hash)
        except Exception:                                        # noqa: BLE001
            return None
        if not status:
            return None
        raw = status.get("status")
        try:
            state = int(raw)
        except (TypeError, ValueError):
            return None
        if state == 2:                                           # executed
            self.nonces.note_consumed(nonce)
            return {"ok": True, "code": CODE_OK, "error": "", "kind": "accepted",
                    "confirmed": True}
        if state == 3:                                           # pending - final state
            self.nonces.note_consumed(nonce)
            return {"ok": True, "code": CODE_OK, "error": "", "kind": "accepted",
                    "confirmed": False}
        if state == 0:                                           # failed
            try:
                next_nonce = await sdk.next_nonce()
            except Exception:                                    # noqa: BLE001
                next_nonce = None
            if next_nonce is not None and int(next_nonce) > int(nonce):
                self.nonces.note_consumed(nonce)                 # 消耗了 → 不能重发
                return {"ok": False, "code": None,
                        "error": "transaction failed in the sequencer (nonce consumed); "
                                 "not resending — reconcile positions before retrying",
                        "kind": "failed", "confirmed": False}
            return None                                          # 未消耗 → 重发同一份
        return None                                              # pending → 重发同一份（幂等）

    # ---------------------------------------------------------------- 确认

    async def wait_for_tx(self, tx_hash: str, *, timeout_s: float = 10.0,
                          interval_s: float = 0.5) -> Dict[str, Any]:
        """轮询 `GET /tx?by=hash` 直到终局。返回 `{ok, code, error, status, confirmed}`。

        `status`：`0 failed / 1 pending / 2 executed / 3 pending-final`。
        超时返回 `ok=False, code="timeout", confirmed=False` —— 调用方（T8）据此走对账，
        **不要**在这里重发。
        """
        if not tx_hash:
            raise ValueError("tx_hash is required")
        deadline = self._clock() + float(timeout_s)
        last_error = ""
        while True:
            try:
                data = await self.sdk().tx_status(tx_hash)
            except Exception as exc:                             # noqa: BLE001
                data = None
                last_error = f"{type(exc).__name__}: {exc}"[:200]
            else:
                last_error = ""
            if data:
                state = data.get("status")
                try:
                    state_i = int(state)
                except (TypeError, ValueError):
                    state_i = -1
                if state_i == 2:
                    return {"ok": True, "code": CODE_OK, "error": "", "status": state_i,
                            "confirmed": True, "tx_hash": tx_hash}
                if state_i == 0:
                    return {"ok": False, "code": "failed", "error": "transaction failed",
                            "status": state_i, "confirmed": False, "tx_hash": tx_hash}
            if self._clock() >= deadline:
                return {"ok": False, "code": "timeout",
                        "error": (last_error or "transaction not confirmed before timeout"),
                        "status": None, "confirmed": False, "tx_hash": tx_hash}
            await self._sleep(float(interval_s))
