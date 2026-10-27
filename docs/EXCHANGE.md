# 交易执行层（`app/exchange.py`）

本文是 T7 的实现说明与运维规约。代码里的注释写「为什么这样写」；这里写「系统层面必须知道什么」。

一句话边界：**`app/exchange.py` 是唯一持私钥、唯一能下单的模块**（PLAN 第 4 节「三条硬边界」之二）。
`app/main.py` / `app/market.py` / 监控面板（T10）**永远不 import 它**；`tests/test_exchange.py`
用静态扫描钉住这条（`ModuleBoundaryTests.test_only_exchange_imports_the_signer`）。

---

## 1. 两种签名（T7 只做第一种）

Lighter 有两套签名，混起来是这类系统最经典的事故来源。

| | L2 交易签名 | L1 钱包签名 |
|---|---|---|
| 字段 | `tx_info.Sig` | `tx_info.L1Sig` |
| 用什么签 | **API 私钥** + `api_key_index` + `account_index` | **L1 钱包私钥** + `chain_id` |
| 谁做 | `lighter.SignerClient` 内部的 WASM signer，本模块调用 | `eth_account`（`SignerClient.__decode_and_sign_tx_info` 里 `Account.from_key(...).sign_message`） |
| chain_id | 参与签名（mainnet `304` / testnet `300`） | 同左 |
| 什么时候需要 | 下单、撤单、改单、改杠杆/保证金、划转、auth token | **注册 API key（`ChangePubKey`）**、fast withdrawal、转给别人、带费的 integrator 授权 |
| 谁持有 | 服务器（`.env` 里的 `LIGHTER_API_PRIVATE_KEY`） | **只有用户本机**，绝不进仓库/容器 |

**本仓库的运行路径里没有 L1 私钥。** `scripts/offline_sign.py` 是唯一会用它的东西，
而且它是「用户在自己电脑上手动跑一次」的工具，不是服务的一部分（`Dockerfile` 不 COPY `scripts/`）。

`chain_id` 由 `LighterSdk` **显式传给** SDK（`CHAIN_IDS = {mainnet: 304, testnet: 300}`），
不让 SDK 按 URL 猜；并且构造前先校验 `base_url` 的 host 属于该 environment，不一致直接
`NotConfigured`。理由：签错链 == 签了一张在另一个网络上的废纸，而 SDK 的默认推断只认
URL 字符串。

---

## 2. auth token 生命周期

认证 token 只用于**需要 `Authorization` 头的 REST/WS 读接口**（`accountActiveOrders`、
`accountLimits`、私有 WS 频道……）。下单不需要它 —— 下单靠的是 L2 签名。

```
token = "{expiry_unix}:{account_index}:{api_key_index}:{32 hex 随机段}"
```

* 生成：`SignerClient.create_auth_token_with_expiry(deadline=<秒>, api_key_index=...)`
  （WASM `CreateAuthToken`），默认 `deadline=600`（10 分钟），**上限 8 小时**。
* 每次都不同（随机段），所以**每次都签名，不便宜** → 必须缓存。
* `app.exchange.AuthTokenCache`：
  * `ttl_s` 默认 600，合法区间 `[60, 8h]`（越界 `ValueError`）；
  * `skew_s = 120`：**过期前 2 分钟**就重铸，不等到 401；
  * 过期时间从 **token 自己**（第一段）解析，不靠本地计时器 —— 机器时钟跳变不会让缓存永不失效；
  * `get()` 命中缓存不碰 signer；`refresh()` 强制重铸；`invalidate()` 清空（收到 401 时用）。
* 铸不出来（未注册的 key / signer 不可用）→ `NotConfigured`，fail-closed。

---

## 3. nonce 规则

**每个 API key 一条独立 nonce 流**，默认 `new = previous + 1`。
`GET /api/v1/nextNonce?account_index=&api_key_index=` 取下一个值。

### 3.1 什么时候会消耗 nonce（决定「能不能重试」）

| 拒绝位置 | 例子 | 消耗 nonce? |
|---|---|---|
| API server（`sendTx` 返回错误码） | 签名错、nonce 错、限流、`ExpiredAt` 太近、余额不够挂单 | **否** |
| sequencer 应用前 | `ExpiredAt` 已过、市场未激活、integrator 费率非法、nonce 错 | **否** |
| sequencer 应用时 | 非法价量、**重复 `clientOrderIndex`**、reduce-only 方向错、`orderExpiry` 已过、撤未知单 | **是**（记成 no-op） |

「taker 单（IOC/market）与 TWAP **不会**在入口做保证金检查」；成交时保证金不够 → 撮合阶段
取消订单，但交易本身成功、**消耗** nonce。

### 3.2 本模块的实现（只有一条规则）

> **签一次，之后所有重试都重发同一份 `tx_info`。**

因为 `tx_info` 里带着 `Nonce` 和 `Sig`，重发同一份 = nonce 恒定。这让「重试换 nonce
导致下两次单」在结构上不可能发生（测试 `NonceRetryTests.test_retry_reuses_the_same_signed_payload_and_nonce`
断言三次尝试的 `tx_info` 逐字节相同、`sign_create_order` 只调用一次）。

配套细节：

* `NonceStream.allocate()` 只在**首次签名前**调一次；`rollback(nonce)` 只在「API server 拒
  （确定没进 sequencer）」且它是**最后一个**分配值时把 nonce 还回去；
* 传输错误（超时/断连）**不回滚**（可能已被消耗），而是**先查 `GET /tx?by=hash`**：
  * `status=2 executed` → 定论成功，不再发；
  * `status=3 pending-final` → 已接受，继续等；
  * `status=0 failed` + `nextNonce > 我们用的 nonce` → **消耗了**：直接报错，绝不重发；
  * `status=0 failed` + `nextNonce == 我们用的 nonce` → 没消耗：重发同一份；
  * 查不到 → 重发同一份（sequencer 只接受第一个同 nonce 的交易）。
* **`21104/21105`（invalid nonce）**：只 `resync()` 一次本地流，然后抛 `NonceConflict`，
  **不自动重签**。自动重签会让「谁在重试」变模糊；T8 有 `clientOrderIndex` 幂等键，
  重来时不会变成第二笔仓。
* 签名后额外自检：`tx_info.Nonce` 必须等于刚分配的 nonce，否则 `ExchangeError` 且**不发送**
  （防止 SDK 内部换 nonce 导致账目分叉）。

> 本模块用 `NonceManagerType.NONE`（`NoOpNonceManager`）关掉 SDK 的 nonce 自动管理：
> SDK 的 `OptimisticNonceManager` 在缓存未命中时会走**同步** `requests.get`（阻塞 async 进程），
> 且 `nonce=-1` 时会让 signer 自己去取 nonce。nonce 必须由我们显式管。

### 3.3 幂等键

`clientOrderIndex` = `sha256("ltab:{SYMBOL}:{bar_ms}:{purpose}")` 的**高 48 bit**
（`0` 改成 `1`，因为 0 是前端订单保留值）。`purpose ∈ {entry, stop, exit}`。
同一根 bar、同一条腿重放 → 同一个 index → 交易所用 `21728 clientOrderIndex exists` 拒绝重复单。

`OrderResult.idempotency_key` 给 T5 的 `trades.idempotency_key` 用：

```
fill:<id>  >  order:<client_order_index>:<side>:<price>:<qty>  >  local:<client_order_index>
```

`side` 是**持仓方向**（`+1/-1`，与 `trades.side` 同义），`price`/`qty` 是整数 tick。

---

## 4. 订单类型与价格保护

| 腿 | type | TIF | reduce_only | 价格 | 触发 |
|---|---|---|---|---|---|
| 入场（默认） | `MARKET (1)` | `IOC (0)` | `0` | `entry_price × (1 ± slip_bps)`，买向上取整/卖向下 | – |
| 入场（可选） | `LIMIT (0)` | `POST_ONLY (2)` | `0` | `limit_price_int` 原价 | – |
| 止损 | `STOP_LOSS (2)` | `IOC (0)` | **`1`** | 触发价再让 `stop_slippage_bps` | `TriggerPrice = plan.stop_price_int` |
| 离场（信号） | `MARKET (1)` | `IOC (0)` | **`1`** | `exit_price × (1 ± slip_bps)` | – |

**为什么入场用 MARKET/IOC 而不是挂 limit**：这是信号触发的追价单。留着挂单会让下一根 bar
评估时仍然挂着上一根的意图（一仓模型下等于两个仓位）。Lighter 的 taker 单 `price` 就是
「最差可接受价」——成交不了的部分自动取消，所以「IO limit + 滑点上限」这个口径由
`MARKET + price` 完整表达。

**为什么止损/离场必须 `reduce_only=1`**：即使方向或数量算错，交易所最多把仓位缩到 0，
不会变成反向持仓。这是 PLAN T8「kill 只挡开新仓、不挡离场」能成立的前提。

**触发用 mark price，不是 last price**（文档 `trading/order-types-and-matching`）：

```
Stop-Loss Sell / Take-Profit Buy :  markPrice <= triggerPrice
Stop-Loss Buy  / Take-Profit Sell:  markPrice >= triggerPrice
```

last price 会被一笔薄档成交瞬时打穿 → 假触发；mark price 掺了指数价 → 抗插针。

**`orderExpiry`**（文档：5 分钟 ~ 30 天）：

* IOC 单必须 `0`；
* 止损/挂单必须是**未来时间戳**。实测：传 `0` 给止损单 → signer 直接拒
  （`OrderExpiry is invalid`）；传 SDK 的 `DEFAULT_28_DAY_ORDER_EXPIRY = -1` → 展开成 `now + 28 天`。
  本模块显式算 `now_ms + 28 天`（留 2 天余量，不够 30 天上限）；
* signer **不**拿墙上时钟校验过期时间（实测：传过去的时间戳照签），而 sequencer 会把
  「`orderExpiry` 已过」当 no-op 并**消耗 nonce** → 所以过期判断由我们在
  `_clamp_expiry()` 里做（`[now+5min, now+30d]`，越界 `ValueError`）。

**`price_protection`**：`sendTx` 的表单字段，OpenAPI spec 里 `default: true`，散文文档无解释。
本模块**不覆盖**默认值；滑点上限由我们编码进 `price`。如果服务端判定最差价离 mark 太远，
会回 `21734`/`21733`（不可重试）。

---

## 5. 错误分类

`classify_error()` 返回 `(kind, retryable)`。`kind` 是**稳定字符串**（面板/告警/测试按它判断，
不解析文案）。数字码优先于 HTTP status，status 优先于文案。

| kind | 触发条件 | 重试? | 处理 |
|---|---|---|---|
| `transport` | 连接失败、超时、`aiohttp.ClientError`、DNS | 是* | 先查 `tx?by=hash` 再重发同一份签名 |
| `rate_limited` | HTTP `429` / `405`；文案含 rate limit | 是 | 指数退避（0.5s→1s→2s…≤8s） |
| `server_error` | HTTP `5xx` / `408` / `425` | 是 | 同上 |
| `busy` | `21506` pending 太多、`21719/21720` 挂单数满、`21601` 盘口满、`21721/21722` | 是 | 同上 |
| `nonce_conflict` | `21104` / `21105` | 否** | resync nonce 流 + `NonceConflict` |
| `api_key_not_registered` | `21108`（invalid PublicKey）、`21109`、`21110` | 否 | `NotConfigured`，提示跑 `scripts/offline_sign.py` |
| `insufficient_margin` | `21739`、`21507`、`21508`、`21111`（pre-liquidation）、`21112`（liquidating） | 否 | `OrderRejected` |
| `duplicate` | `21728` clientOrderIndex exists | 否 | 幂等命中：当作「这条腿已经下过」 |
| `rejected` | 其余 4xx（精度非法 `21706`、价格越界 `21734`、reduce-only 方向错 `21738`、市场冻结 `21638`、过期 `21711`…） | 否 | `OrderRejected`，**明确报错，不重试** |

\* `transport` 的重试**必须先查交易状态**（见 3.2）。
\** 不自动重签，见 3.2。

**`code=200` 只表示「API 收下了」**。文档原话：sequencer 仍可能拒（保证金不够、价格越界）。
所以 `OrderResult.ok=True` 的含义是「事务已提交」，`confirmed=True` 才是「交易所确认执行」。
需要确认就 `await client.wait_for_tx(tx_hash)`（轮询 `GET /tx?by=hash`，
`0 failed / 1 pending / 2 executed / 3 pending-final`）或让 T8 走 WS `account_tx`。

所有异常都能 `as_dict()` 成 PLAN 要的三元组：

```python
{"ok": False, "code": "insufficient_margin", "error": "entry BTC rejected: not enough margin…",
 "retryable": False, "exchange_code": 21739, "detail": {...}}
```

`OrderResult.as_dict()` 同样有 `ok` / `code` / `error`，且可 `json.dumps`（T10 面板直接渲染）。

---

## 6. dry-run 与真实下单的切换

**默认 dry-run**，而且 dry-run 之下**连签名都不做**（不取 nonce、不建 SDK）。

```python
from app.config import Config
from app.exchange import ExchangeClient

cfg = Config.from_env()

# 1) 默认：只打印将要提交的订单（价格/数量/类型/reduce_only/expiry/coi）
client = ExchangeClient.from_config(cfg)
await client.place_entry(plan, bar_ms=bar_ms)
# [dry-run] ENTRY BUY 4156 (int ticks) BTC market_id=1 | type=MARKET+IOC price_int=651950 | ...

# 2) 想在真下单前核对签名产物（长度/前后缀），仍然不发请求
client = ExchangeClient.from_config(cfg, preview_signature=True)
# [dry-run] signature preview: {"nonce": 0, "sig_len": 108, "tx_hash_len": 80,
#                               "tx_hash_prefix": "5d6f7b45f856", ...}

# 3) 真实下单：三道开关，缺一道就 LiveTradingDisabled
#    a. LIGHTER_LIVE_TRADING=1
#    b. LIGHTER_LIVE_ACCOUNT_INDEX=<account_index>   # 必须等于配置里的 account_index
#    c. 调用方显式传 confirm_live=<account_index>     # 逐次确认，不是全局默认
client = ExchangeClient.from_config(cfg, env=os.environ, dry_run=False,
                                    confirm_live=cfg.account_index)
result = await client.place_entry(plan, bar_ms=bar_ms)
```

为什么三道而不是一道：

* `LIGHTER_LIVE_TRADING` —— 「这台机器/这个环境允许真钱下单」；
* `LIGHTER_LIVE_ACCOUNT_INDEX` —— 防止把测试账户的开关带进实盘账户（也会在部署时暴露
  「`.env` 复制错了」这种事故）；
* `confirm_live` —— 写操作逐次确认，默认值不存在，所以「以为默认会下单」不可能发生。

`dry_run=False` 但开关不全 → `LiveTradingDisabled`，**一行都不发**（测试逐条覆盖：
无开关、只有开关、开关+account、confirm 给错账户、declared 与 account 不一致）。

只读监控面板（T10）**永远不碰这个类**：它的数据来自 DB + `app/market.py` 的只读接口。

---

## 7. 私钥处理规约

| 规约 | 实现 |
|---|---|
| 私钥**不落盘** | 只在 `Config` 的内存字段与 `SignerClient` 内部；本仓库没有任何写 key 的代码 |
| 私钥**不进日志/异常** | 异常消息只带字段名与数值；`Config.__repr__` / `ExchangeClient.__repr__` 打码成 `***`；`test_private_key_never_appears_in_repr_errors_or_results` 断言 |
| 私钥**不进 argv** | `scripts/offline_sign.py` 只从 `$LIGHTER_L1_PRIVATE_KEY` 或 `getpass` 读；argv 里出现疑似私钥（参数名含 private/secret，或值形如 `0x`+64/80 hex）**直接拒绝运行**（退出码 2） |
| **L1 私钥不上服务器** | 只有 `scripts/offline_sign.py` 用；在用户本机跑；容器不 COPY `scripts/` |
| 服务器只放 API key | API key **不能**转给别的 L1 地址、不能 fast withdrawal（docs「Cannot do alone」） |
| 损失上限 | 机器人跑在**独立 sub-account**（PLAN 第 8 节第 3 步），爆仓/被盗只影响这个子账户 |
| 轮换 | key 丢了或怀疑泄漏：**换一个 `api_key_index` 重新注册**（注册会覆盖同 index 的旧 key）；旧 index 的 key 在链上依旧有效，所以要么覆盖它、要么把资金挪走 |

**注册 API key（一次性，用户本机）**：

```bash
# 私钥走环境变量（不进 history、不进 ps）
LIGHTER_L1_PRIVATE_KEY=0x… python scripts/offline_sign.py --environment mainnet \
    --api-key-index 4 --dry-run     # 先看计划
LIGHTER_L1_PRIVATE_KEY=0x… python scripts/offline_sign.py --environment mainnet \
    --api-key-index 4               # 真注册（会要你输入 REGISTER 确认）
```

脚本会：打印 L1 地址（打码）/ account_index / api_key_index / 新公钥 → 用 L1 私钥签
`ChangePubKey` → 提交 `sendTx` → **轮询 `/apikeys` 直到公钥真的出现**（`code=200` 不等于生效）
→ 打印写进服务器 `.env` 的四行（API 私钥只显示一次，或用 `--save-api-key <path>` 写 0600 文件，
且拒绝写到当前目录）。它**不会**打印 L1 私钥、不会把它写进任何文件。

服务器侧验收（不碰私钥内容）：

```bash
python -c "from app.config import Config; print(Config.from_env())"   # 只打印打码后的视图
curl -s "https://mainnet.zklighter.elliot.ai/api/v1/apikeys?account_index=<N>&api_key_index=4"
```

`/apikeys` 返回的 `public_key` 应该等于 `offline_sign.py` 打印的公钥（去掉 `0x`，小写）。

---

## 8. 实测确认的 SDK 行为（`lighter-sdk==1.1.8`，2026-10-27）

这些是**跑过**的结论，不是读文档推断的：

| 结论 | 证据 |
|---|---|
| `create_api_key()` → `(pk, pub, err)`，两者都是 `0x` + 80 hex（40 字节） | `tests/test_exchange.py::RealSignerOfflineTests.test_api_key_shape_matches_the_config_validator` |
| `SignerClient(...)` 必须在 running event loop 里构造（内部建 aiohttp connector），同步上下文报 `RuntimeError: no running event loop` | 本机探针；因此 SDK 在 `ExchangeClient.sdk()` 里懒建 |
| 构造与 `sign_create_order` **都不发网络请求** | 把 `tx_api.send_tx` 换成会抛 `AssertionError` 的桩，签名照常返回 |
| `sign_create_order` → `(14, tx_info, tx_hash, None)`；`tx_hash` 80 hex | 同上 |
| `tx_info` 16 个键，紧凑 JSON | `test_signed_payload_encodes_the_integer_ticks_exactly` |
| `Sig` 是 base64 的 **80 字节**（108 字符带 `=`） | 同上（`base64.b64decode(..., validate=True)` 解出 80 字节） |
| `ExpiredAt = now_ms + 599002`（约 +10 分钟），由 signer 自己填 → **同一组参数两次签名不是逐字节相同** | `test_two_signatures_differ_only_in_expiry_and_sig`（差异只允许出现在 `ExpiredAt`/`Sig`） |
| 止损单 `order_expiry=0` → `OrderExpiry is invalid`；`-1` → `now + 28 天` | 探针 |
| 止损单 `time_in_force=1`（GTT）→ `OrderTimeInForce is not valid`（只收 IOC） | 探针 |
| `client_order_index > 2^48-1` → `ClientOrderIndex should not be larger than 281474976710655` | 探针 |
| 未注册的 `api_key_index` → `client is not created for apiKeyIndex: N accountIndex: M` | 探针 |
| `sign_cancel_order` → `tx_type=15`；`sign_cancel_all_orders` → `16`，带市场时 `L2TxAttributes={"5": <market>}`，全市场（255）为 `null` | `test_cancel_all_payload_carries_the_market_attribute` |
| auth token 形如 `expiry:account:key:32hex`，两次调用不同 | `AuthTokenTests` + 探针 |
| 错误响应是 **HTTP 400 + `{"code","message"}`**（`/account?by=index&value=<不存在>` → `400 {"code":29404,...}`） | `curl` 实测 |
| SDK 在 400 时抛 `ApiException`，`data` 是 `ResultCode(code, message)`；429/405 只带 status | 读 `api_client.response_deserialize` + `exceptions.from_response` |

**查文档推断（未实盘验证）**：

* 各错误码的数字语义（来自 docs「Full error list」，我们只按表分类）；
* `price_protection` 的服务端语义（spec 只说 default true）；
* 「sequencer 应用时拒绝会消耗 nonce」「taker 单不做入口保证金检查」——文档明确写了，
  但没有账户可以实测；
* 速率限制的具体数值（Standard 60 req/min；429/405 混用）。

---

## 9. 给 T8 / T10 的接口

```python
result = await client.place_entry(plan, bar_ms=bar_ms)   # 入场
result = await client.place_stop(plan, bar_ms=bar_ms)    # 止损（reduce_only + mark）
result = await client.place_exit(symbol=..., market_id=..., side=..., qty_int=...,
                                 price_decimals=..., exit_price=..., bar_ms=...)  # 平仓
result = await client.cancel_all(market_id)              # 撤某市场
result = await client.cancel_all_orders()                # 撤全部
status = await client.wait_for_tx(result.tx_hash, timeout_s=10)   # 确认
```

* `result.idempotency_key` → 传给 `store.record_entry(..., client_order_index=result.client_order_index)`
  与 `trades.idempotency_key`；
* `result.tx_hash` / `result.client_order_index` / `result.code` → 落 `orders` 表；
* 异常统一 `as_dict()` = `{ok, code, error, ...}`，`kind` 可用于告警分级：
  `insufficient_margin` / `api_key_not_registered` / `nonce_conflict` 属于**必须人工看**的；
  `rate_limited` / `busy` / `transport` 属于**自己会好**的；
  `AmbiguousOrder` 必须走对账（T8 `_sync_fills` 在前，判 mismatch 在后）。
