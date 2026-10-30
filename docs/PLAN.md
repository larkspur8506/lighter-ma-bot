# lighter-ma-bot 开发计划

> 状态：2026-10-27 定稿。执行方式：**子代理逐任务实现 + 独立审核**（我负责编排、审查、验收）。
> 依据：旧仓库 `larkspur8506/crypto-ma-alert`（HL 现役）实测 + Lighter 官方文档全量 + 公开接口实测。

---

## 0. 定位

**一个纯自动交易系统**：在 Lighter（zkLighter）永续上跑 BTC/ETH/SOL 4h 双均线密集突破策略，**没有任何人工下单环节**。

与旧系统的关系：**并行运行，互不干扰**。旧系统（HL）继续 live，不降级不改动；新系统独立仓库、独立 DB、独立容器、独立 Lighter 账户。

### 0.1 阶段划分（2026-10-27 调整）

本计划分两个阶段，**当前只做第一阶段**：

| 阶段 | 范围 | 状态 |
|---|---|---|
| **阶段一：开发期** | T1–T11 全部开发 + 离线验证（回测、dry-run、纸面链路） | **进行中** |
| **阶段二：上线期** | T12/T13 + 真实账户、真实资金 | **暂缓**，等策略验证完成、正式转移前再做 |

**为什么分阶段**：现阶段 HL 仍在验证策略阶段、资金处于测试阶段，**没有必要也不应该**
为了「让 T13 能跑」而提前创建真实账户、入金、注册 API key。真实上线涉及真钱与不可逆
操作（入金、开户、签 key），应该留到策略验证完成、真正要转移资金的时候再做，且届时
用最新代码重新验证一遍。

**阶段一不受此影响**：T1–T11 全部用**注入的假账户 / dry-run / 只读实测**完成并验证，
**不需要任何真实凭据**。`scripts/offline_sign.py` 和真实下单链路在阶段一就已实现并测试
（签名产物可离线验证），到阶段二只需配置凭据并跑演练。

---

## 1. 决策点（已确认）

| 决策 | 结论 |
|---|---|
| 旧系统是否暂停 | **不停**，并行运行 |
| L1 钱包 | **新建专用 Ethereum 钱包**，只放 Lighter 的钱 |
| 账户模式 | **Simple Trading Account（Classic）**，`account_trading_mode=0` |
| 机器人群组 | **独立 sub-account**，只放机器人有权亏的钱 |
| 部署机器 | **待定**（见 1.1） |
| 当前阶段 | **只做开发**，真实上线（T12/T13）推迟到正式转移前 |

### 1.1 部署约束（2026-10-27 实测）

Lighter 对**交易端点**（`/api/v1/sendTx`、`/api/v1/sendTxBatch`、WS `/stream`）做服务端 IP
地域拦截，命中返回 `{"code":20558,"message":"You are accessing Lighter from a restricted
jurisdiction..."}`。**读端点**（candles / orderBookDetails / account / nextNonce /
accountsByL1Address）全部正常。

实测各机器出口：

| 机器 | 出口 | `sendTx` |
|---|---|---|
| 家网本机 | 印度 孟买 `137.23.52.79` | ✅ `20001`（过地域检查，仅参数无效） |
| oracle1 | 美国 圣何塞 `64.181.225.174` | ❌ `20558` |
| oracle2 | 美国 圣何塞 `152.70.122.121` | ❌ `20558` |
| google | 美国 俄勒冈 `35.212.253.78` | ❌ `20558` |

对照：Hyperliquid 的下单端点在同一台美国机器上返回 `422`（请求已进入撮合逻辑），
说明 HL 只在前端层拦，**Lighter 是在 API 服务端层拦**。因此「HL 能跑」不能推出
「Lighter 能跑」。

**推论**：真实下单必须部署在**非受限地区**机器。美国节点只能做只读（回测、面板数据源）。

---

## 2. 明确丢弃的旧系统遗留物

| 丢弃 | 原因 |
|---|---|
| 人工下单确认流（`/c/{pid}` 签名链接、pending 票、忽略按钮） | 纯自动系统不需要人介入 |
| `PENDING` / `IGNORE` 两个状态 | 没有人工确认 → 不需要「等待」态 |
| MEXC 合约面值/张数换算（`app/sizing.py` 的 `SPECS`） | Lighter 是整数化连续数量 |
| 手动补录成交价 / `actual_entry` 覆盖语义 | 交易所给成交价，无需人工 |
| `auto_closed` 两段式关账 | 成交由交易所确认后直接关账 |
| 诊断源降级链（`binance-fapi` / `okx-swap`） | fail-closed 原则下不降级 |
| 面板的入场卡片/开盘按钮/止损体检等面向人工的 UI | 无人看 |
| 入场信号推送 | 纯自动，不需要人知道要开仓 |

**保留并逐字移植**：`app/strategy.py`（528 行）——这是 700 笔回测 + 实盘验证过的口径，**一字节不改**。

---

## 3. 技术栈

- Python 3.14
- `lighter-sdk==1.1.8`（PyPI，**Apache-2.0**）——官方 SDK 直接用：
  - `lighter.SignerClient` — 签名
  - `lighter.ApiClient` + 各 `*Api` — 只读 REST
  - `lighter.create_api_key()` — 本地生成密钥对
  - **WASM 签名器是二进制依赖**：`lighter/signers/lighter-signer-linux-amd64.so`（11.8MB，随 wheel 打包，Docker 需 amd64）
- `eth-account>=0.13.4`（L1 签名，仅离线脚本用）
- `fastapi` / `uvicorn` / `apscheduler`
- SQLite（stdlib `sqlite3`）

**许可**：旧仓库自研代码直接复制；`lighter-sdk` Apache-2.0 作为依赖引用不传染。

---

## 4. 目录结构

```
lighter-ma-bot/
├── app/
│   ├── strategy.py          # 逐字复制旧仓库（口径冻结）
│   ├── symbol_registry.py   # 单一元数据源（symbol → market_id/decimals/最小下单）
│   ├── config.py            # DB > env > 代码默认
│   ├── db.py                # positions/orders/fills/events/snapshots
│   ├── engine.py            # 纯状态机 walk()（IDLE→OPEN→CLOSED）
│   ├── market.py            # Lighter 4h K 线，fail-closed
│   ├── sizing.py            # 整数化 sizing（纯函数）
│   ├── notify.py            # 只推异常（成交失败/对账异常/熔断/数据源异常）
│   ├── main.py              # FastAPI + 调度
│   ├── views.py             # 单页监控 + JSON API
│   └── lighter/
│       ├── info.py          # 只读：orderBookDetails/candles/account/feeTiers
│       ├── exchange.py      # 唯一写路径（持 API 私钥，懒加载 SDK）
│       ├── executor.py      # 状态机 + 风控 + 对账 + 幂等
│       ├── sizing_lt.py     # 精度换算（纯函数）
│       ├── dd.py            # TWR 回撤熔断
│       └── watchdog.py      # 独立看守
├── scripts/
│   ├── backtest.py          # Lighter 全历史回测
│   ├── xvalidate.py         # 信号级交叉验证（回测 vs 线上）
│   └── setup_key.py         # 注册 API key / 建 sub-account（离线，人工跑一次）
├── tests/
├── docs/DESIGN.md
├── docs/RUNBOOK.md
├── Dockerfile
└── requirements.txt
```

**三条硬边界**（沿用旧系统已验证的设计）：
1. **信号与执行解耦**：`strategy.py` 只产信号，`executor.py` 只把信号变成订单
2. **只读与写路径分离**：`info.py` 无凭据；`exchange.py` 是唯一持私钥、唯一能下单的模块；其他模块不得 import 签名器
3. **单一写者**：`positions`/`orders`/`fills` 只由 `executor.py` 写

---

## 5. 关键差异设计表（HL → Lighter）

| 维度 | HL（旧） | Lighter（新） | 设计 |
|---|---|---|---|
| 数量 | 连续 | **整数化** `value × 10^size_decimals` | `sizing_lt.py`：目标名义 → ÷价格 → ×10^dec → **floor** |
| 价格 | 浮点 | **整数化** `value × 10^price_decimals` | 同上；`min_quote_amount=$10` 硬门槛 |
| 下单类型 | market | **IO limit + 滑点上限** | `price` = 最差可接受价（mid×(1+slip)） |
| 幂等键 | cloid（16B hex） | **`clientOrderIndex`（uint48，跨市场唯一）** | `sha256("ltab:{sym}:{bar_ms}:{purpose}")` 取前 48 bit |
| nonce | 毫秒时间戳 | **每 key 独立流，next+1** | `skipNonce=0`；每 key 串行 |
| 死手开关 | `scheduleCancel` | **`orderExpiry`（5min–30d）** | 止损单带 `orderExpiry=30d`（远期，不提前消失） |
| 手续费 | 百分比字符串 | **tick 编码** | `ticks × 0.0001`；standard 档 0/0 |
| 止损触发 | last price | **mark price** | SL/TP 用 `triggerPrice` + `reduceOnly` |
| 权益口径 | `marginSummary.accountValue` | **`account.collateral` / `total_asset_value`** | Classic 模式；**不把 upnl 二次相加** |
| 账户 | 地址 | **L1 地址 → account_index** | 机器人用独立 sub-account |
| 提币 | agent 不可提 | API key 可提（只到自己钱包） | sub-account 隔离 = 损失上限 |
| 杠杆 | 40x/25x/20x | BTC 50x / ETH 50x / SOL 25x | 逐仓 5x（与回测口径一致） |
| K 线 | `candleSnapshot` 5000 根 | `candles` 500 根/次需翻页 | **总深度 1.77 年** |
| 限流 | 1200 权重/分/IP | **标准账户 60 请求/分** | 上 Plus/Premium 档（24,000/分，140ms taker） |

---

## 5.5 监控面板设计（T10 的详规）

### 保留什么：只读监控，6 个区块

按「用户 90% 打开只问三件事：系统正常吗 / 仓位怎样 / 最近表现」组织。

**① 状态条（顶部一行，永远可见）**
`● 正常 ｜ 权益 $X ｜ 持仓 N/3 ｜ kill:off ｜ 模式 live ｜ 上次 tick 12 分钟前 ｜ 数据源 ok`
- 异常时（数据源异常 / kill 触发 / 对账 mismatch / 熔断）**这一行变红并写明原因** —— 这是唯一需要告警级别的信息

**② 仓位表（每标的一行）**
symbol ｜ 方向 ｜ 开仓价 ｜ 现价 ｜ 浮盈/浮亏 ｜ 止损价 ｜ 距止损% ｜ 名义 ｜ 保证金 ｜ 强平价 ｜ 持仓时长 ｜ 状态标记
- 状态标记只有三种：`持仓中` / `等待开仓` / `无仓位`。**没有「待人工确认」**

**③ 止损单覆盖（独立区块，必须常驻不可折叠）**
每个持仓一行：symbol ｜ 止损价 ｜ **交易所挂单状态（在/不在）** ｜ 数量是否覆盖仓位 ｜ 最后校验时间
- 「不在」或「数量不足」→ **红字告警**（旧系统踩过的坑：止损单没挂上 = 裸奔）

**④ 盈亏曲线（两个口径并列）**
- 实线 = 累计盈亏（已剔除出入金），单位 U
- 虚线 = TWR 收益指数（资金加权），单位 %
- 峰谷回撤以 % 计（TWR 口径，与 `dd.py` 熔断判据一致）

**⑤ 三标的动态**
symbol ｜ 当前价 ｜ 6MA 带宽% ｜ **距密集门槛**（带宽 vs 该标的过去 250 根 20% 分位）｜ 连续密集根数 ｜ SMA20 ｜ SMA200 ｜ 下一根 bar 收盘时间
- 「距密集门槛」是**「为什么没开单」的唯一证据**（旧系统的核心洞察，必须保留）

**⑥ 执行器事件流（最近 20 条）**
时间 ｜ 标的 ｜ 动作（开仓/止损/离场/熔断/跳过）｜ 结果 ｜ 关键数值 ｜ 错误
- **「跳过」也要显示**（如「未过密集门槛」）—— 否则用户以为系统坏了

### 丢掉什么（明确不进新项目）

| 丢弃 | 原因 |
|---|---|
| 入场信号卡片 / 开盘按钮 | 纯自动，没人点 |
| 成交价补录表单 | 交易所给价 |
| `/admin/rules` 参数表单 | 参数在 DB 改，机器人自己读；或直接改文件 |
| 模式切换 UI（off/dry/live） | 用 env + 部署流程控制；UI 切换反而危险（误点直接上线） |
| 手动补 tick 按钮 | 定时任务自动跑 |
| 止损距离体检 / MA200 趋势失效提示 | 面向人工入场决策 |
| 多标的参数表 | 单一策略 |
| `ui_theme.py` 主题配色 | 新项目重做简洁版，不继承 |
| 微信/Telegram 的**信号**类推送 | 只推异常 |

### 保留的三个写操作

| 操作 | 端点 | 为什么必须留 |
|---|---|---|
| **手动 kill**（停开新仓） | `POST /admin/kill` | 风控的最后一道人工闸门。熔断是自动的，但「我今天不想交易」需要人工手段。**kill 只挡开新仓，不挡离场** |
| **手动平仓**（市价清仓 + 撤所有挂单） | `POST /admin/flatten` | 「系统判断有 bug，我现在就要出来」。这是**唯一**比 kill 更强的操作，必须有 |
| **重置回撤峰值** | `POST /admin/dd/reset` | 回撤熔断「永不自解」，人工介入后需要重置 peak，否则熔断后再也开不了仓 |

三者全部：带 admin token 鉴权（`?k=<token>`）、**不可撤销操作需二次确认**、每次操作落 `events` 表可审计。

### 部署形态

- 独立域名（一个服务一个域名），1Panel 反代到 `127.0.0.1:<port>`
- 访问控制：admin token（query 参数）—— 面板只读为主，token 主要保护那三个写操作
- 响应式单页（窄屏表格转卡片），手机可用
- HTTPS + token；考虑 IP 白名单

---

## 6. ⚠️ 证据基础变化（必须写进 README 与监控页）

用 Lighter 自己的 4h K 线、复刻线上口径（含密集门槛）实算：

| 标的 | 4h bar 数 | 折合年数 | 突破信号 | **过密集门槛（实际入场）** | 笔/年 |
|---|---|---|---|---|---|
| BTC | 3,888 | 1.775 | 1,278 | **87** | 49.0 |
| ETH | 3,886 | 1.774 | 1,241 | **118** | 66.5 |
| SOL | 3,884 | 1.774 | 1,392 | **94** | 53.0 |
| 合计 | 11,658 | 1.775 | 3,911 | **299 笔** | 168 |

（笔/年 = 笔数 ÷ 该标的自己的 bar 数 ÷ 2,190。合计按最长分母 299 ÷ 1.775 = 168.4 取整为 168。）

旧回测：**700 笔 / 6.10 年**（年化 115 笔，Binance 4h 全历史）。

→ **迁到 Lighter 后历史证据从 700 笔缩到 299 笔（覆盖率 43%）**，且这 299 笔是 Lighter 自己的 K 线产生的。
旧回测只能证明「策略有效」，**不能证明在 Lighter 执行场所同样有效**。新系统的回测必须用 Lighter K 线重跑（`scripts/backtest.py`），并把两个结果并列报告。

---

## 7. 任务分解（11 个开发 task + 2 个阶段二 task）

每个 task = 一次子代理实现 + 一次独立审核 + 我的验收。子代理在 worktree 里干活，走 PR → CI → merge。

**当前范围：T1–T11**（阶段一）。T12/T13 属阶段二，见 0.1。

**任务标题后的 `[已完成]` 是实际落地时的文件名/口径**（与本节最初的草稿有出入，
以实际实现为准 —— 草稿写于定稿前，落地时按 T2 冻结口径和 T4 的整数化设计做了调整）。
「上线四步」已从 T11 拆出，见 T11/T12/T13。

### T1 — 仓库骨架 + CI `[已完成 e65c4f6]`
- 建 `requirements.txt`（`lighter-sdk==1.1.8`）、`Dockerfile`（装 WASM 签名器）、`.gitignore`、`README.md`（含第 6 节证据表）
- GitHub Actions：test → build
- **验收**：`import lighter` 成功；CI 绿；容器起得来
- *落地补充*：默认 CMD 必须真能起（曾用 `--entrypoint` 覆盖导致假绿，已修）；签名器检查改真实加载探针；依赖显式钉版（含 `httpx2`）；私钥类文件不入库不入镜像

### T2 — `strategy.py` 逐字移植 + 基线 `[已完成 8ef1896]`
- 从旧仓库复制 `app/strategy.py`（528 行，**一字节不改**）
- 同时复制旧仓库 `scripts/xvalidate.py` 作对照工具
- **验收**：同一组 K 线喂新旧两版，信号集合（bar_ms + 方向 + 止损价）**逐笔完全一致**
- *落地*：sha256 `8682667…` 冻结；测试三路夹住（字节身份 / 独立参考实现 / 写死数值）；数据源改为 Lighter 自身 4h K 线，1251 笔信号逐笔一致

### T3 — `symbol_registry.py` + `market.py` `[已完成 a70ea40]`
- 注册表：symbol → `{market_id, price_decimals, size_decimals, min_base_amount, min_quote_amount, min_initial_margin_fraction}`，**运行时从 `orderBookDetails` 拉取并缓存，禁止硬编码 market_id**（官方明确会变）
- `market.py`：`candles` 翻页、只保留已收盘 bar、**fail-closed**（主源失败抛 `FeedError`，绝不换源）、新鲜度校验（最后一根收盘 ≤ 8h）
- **验收**：最新 bar 与官方 UI 一致；时间戳单调；根数 ≥ 376；未收盘 bar 被剔除
- *落地*：注册表并入 `market.py`（共享限流器）；`/orderBooks` 不返回深度，深度在 `/orderBookOrders`；testnet 的 market_id 与 mainnet 不同，必须运行时解析

### T4 — `sizing_lt.py`（整数化 sizing）`[已完成 24b881f]`
纯函数：
- `target_notional(equity, risk_pct, stop_dist) -> float`
- `to_int_qty(notional, price, size_decimals) -> int`（**floor**）
- `to_int_price(price, price_decimals) -> int`
- `round_trip_check(...) -> bool`（反算名义偏差 ≤ 1 tick）
- **验收**：BTC sizeDec=5 / ETH=4 / SOL=3 边界各有用例；名义误差 < 0.01%；不产生 0 股或低于 `min_base_amount` 的单
- *落地*：文件名 `app/sizing.py`；另加 `app/symbol_registry.py` 与 `app/risk.py`（7 道 fail-closed 闸门）

### T5 — `db.py` + 迁移 `[已完成 17399a8]`
新表：`positions`（无 pending/ignore）、`orders`（`client_order_index` 幂等键）、`fills`、`events`、`snapshots`。保留「DB > env > 默认」优先级。
- **验收**：迁移脚本幂等；`Config().apply_db()` 回读值正确（**旧系统踩过的坑**：数字被存成字符串会静默失效）
- *落地*：文件名 `app/store.py` + `app/state.py`；表名 `trades`/`equity_curve`/`daily_counters`/`dd_anchors`；不变式 `sum(entry qty) == position.qty`

### T6 — `engine.py` 状态机（去人工流）`[已完成 742f3e6]`
- 状态 `IDLE → OPEN → CLOSED`，每标的至多一仓
- `walk()` 纯函数，输出入场/离场/止损事件
- **验收**：用旧系统 dry-run 回放的同一组 bar，新引擎的**开平仓回合**逐笔一致（入场 bar、方向、止损价、离场 bar、R 倍数）
- *落地*：文件名 `app/lifecycle.py`；状态机 `IDLE→OPEN→CLOSING→CLOSED`；**减仓不受熔断阻挡**（结构性保证）；`walk()` 未做（`decide()` 即循环体，整段回放归 T11，避免第二套口径）

### T7 — `exchange.py`（唯一写路径）`[已完成 f9ff5e0]`
- 懒加载 `SignerClient`；缺 SDK/缺凭据 → **fail-closed**
- `place_market_with_slippage(coin, side, notional, slip_bps) -> tx_hash`
- `place_stop_loss(coin, side, qty, trigger_price, expiry_ms)`
- `cancel_all(coin)` / `cancel_all_orders()`
- **验收**：异常归一化成 `{ok, code, error}`；**`code=200` 只表示已接受**，必须等 `tx` 确认或 WS `account_tx`
- *落地*：入口改为 `place_entry(plan)` / `place_stop(plan)` / `place_exit(...)`（以 T4 的 `OrderPlan` 整数 tick 为准）；三道门（env flag + 环境匹配 + 每次调用 `confirm_live=<account_index>`）；重试**只重发不重签**（nonce 恒定），传输错误先查 `/tx?by=hash`；另有 `scripts/offline_sign.py`（用户本机跑，私钥只走 env/getpass，不进 argv）

### T8 — `executor.py`（状态机 + 风控 + 对账）
- 幂等：`client_order_index` 唯一键
- **对账顺序**：先 `_sync_fills` → 再判 mismatch（旧系统 PR #44/#45 的教训）
- **fill 自证**：reduce-only 腿 + 双侧容差 ±1 tick → 正常止损成交，**不 kill**
- 风控：当日亏损 5% 快线（自动）+ 回撤告警/熔断（按 risk_pct 档）
- **kill 只挡开新仓，不挡离场**（交易所止损单照常有效）
- **验收**：单元测试覆盖「止损成交后本地有仓/交易所无仓」→ 走 closing 宽限期 → 自动关账，**不误报 mismatch**

### T9 — `dd.py` + `watchdog.py`
- 回撤用 **TWR（剔出入金）**
- 三档阈值（照抄 `risk-sizing-tier.md`，**不现编**）：1.33% 档告警 25%/熔断 30%；2% 档 30%/40%；3% 档 40%/50%
- watchdog 每 5 分钟：容器存活/health/调度器/近 6h 推送失败/bar_state 停摆/心跳超期
- **验收**：注入一次假亏损 → 熔断触发且只挡新仓；kill 永不自解（只有 reconcile 类可自解）

### T10 — `main.py` + `views.py` + `notify.py`（监控页）
- 调度：4h 收盘后 tick（`:03` / `:33`）
- 只读监控页，**6 个区块**（见第 5.5 节详规）
- 通知**只推异常**（成交失败、对账异常、熔断、数据源异常）
- **验收**：真服务 + 真 HTTP（丢弃型 DB）冒烟：`/health` 200、页面含 6 区块、非法凭据时降级不崩（返回 0 值 + WARNING，不 500）

### T10a — 只读监控页 + JSON API
- 端点：`GET /health`、`GET /status`、`GET /positions`、`GET /stops`、`GET /pnl`、`GET /symbols`、`GET /events`
- 6 区块：状态条 / 仓位表 / **止损单覆盖** / 盈亏曲线（双口径）/ 三标的动态 / 执行器事件流
- **硬约束**：面板读的数据**全部来自 DB + Lighter 只读接口**，**绝不直接调用 `exchange.py`**（面板永远不碰私钥路径）
- **验收**：真服务真 HTTP 冒烟；页面含 6 区块；非法凭据降级不崩

### T10b — 三个写操作（唯一的写面板）
- `POST /admin/kill`（手动停开新仓，**不挡离场**）
- `POST /admin/flatten`（市价清仓 + 撤所有挂单 —— 唯一的「比 kill 更强」操作）
- `POST /admin/dd/reset`（重置回撤峰值；熔断永不自解，人工介入后必须能重开）
- 全部：带 admin token 鉴权（`?k=<token>`）、**不可撤销操作需二次确认**、每次操作落 `events` 表可审计
- **验收**：无 token → 401；flatten 未确认 → 拒绝；每次操作落库

### T10c — 盈亏曲线渲染 + 执行器事件流
- 曲线：**实线 = 累计盈亏（已剔除出入金，单位 U）**；**虚线 = TWR 收益指数（资金加权，单位 %）**；峰谷回撤以 % 计（TWR 口径）
- **硬约束**：曲线与 `dd.py` 熔断判据**必须用同一个 TWR 计算函数**，不能两处各算一套（旧系统踩过「三个回撤口径混用」的坑）
- 事件流：最近 20 条，含**「跳过」**类事件（如「未过密集门槛」）——否则用户以为系统坏了
- **验收**：曲线口径与 `dd.py` 一致（同一测试断言两个口径相等）

### T11 — 回测（全历史）`[已完成 cf22bae]`
- `scripts/backtest.py`：Lighter 全历史 1.77 年 → 均R/胜率/最大回撤/CAGR（三档风险 × 4 场景 × 有/无资金费）
- `scripts/xvalidate.py`：回测引擎 vs 线上信号逐笔一致（T2 已完成基础版，此处补全历史与统计）
- **验收**：回测数字与旧系统基线可比（同口径、同数据源差异要标注）；信号集合逐笔一致
- *落地*：信号只 `import app.strategy`（sha256 冻结）、回撤只走 `app.dd` 的 TWR 链；
  资金费用 Lighter `/fundings` 真实逐小时费率（rate=%/h，direction=long 多头付）；
  同 bar 止损优先（保守）+ 跳空按开盘价 + 入场当根不判离场；
  实测 1,251 笔信号逐笔一致 → **成交 145 笔（策略 v2，止损上限 3.5%；v1 是 208 笔）**
  （每标的至多一仓 + v2 的止损距离过滤），
  1.33% 档 CAGR +47.69% / 终值 2.003x / 最大回撤 17.95%（含资金费），见 `docs/BACKTEST.md`
- *口径更正*：本节原写的「299 笔」是早期估算，与冻结口径不可混用（`docs/STRATEGY_PORT.md` §4
  已注明）；冻结漏斗为 6,874 根破线 → 1,582 根过密集 → 1,251 笔信号 → 208 笔成交（v1）/ 145 笔（v2）

### T12 — 部署（阶段二）

> **阶段二任务，当前不做。** 等策略验证完成、正式转移前再做；届时用最新代码重新验证一遍。

- Docker Compose + systemd（或 1Panel）部署；配置走 env；镜像 tag 化可回滚
- **验收**：容器起来后 `/health` 200；重启后状态从 DB 恢复

### T13 — 上线演练（阶段二）

> **阶段二任务，当前不做。** 涉及真钱与不可逆操作。

- 上线四步 `off → dry → live → 常态化`，每步有验收
- **前置**（需用户参与，一次性）：
  1. 新建专用 Ethereum 钱包（只放 Lighter 的钱）
  2. 用 UDA（`bridge.lighter.xyz`）从 Arbitrum 入金 USDC，**首次入金自动建账户**
  3. 建 sub-account（机器人专用）
  4. 注册 API key（`scripts/offline_sign.py`，**用户离线签名**，私钥不落服务器）
- **部署机器前置**：必须是**非受限地区**机器（见 1.1；美国 IP 会被 `sendTx` 拒）
- **验收**：dry 跑满至少 2 根 4h bar，信号数与回测预期一致

---

## 8. 一次性人工步骤（需用户参与）—— **阶段二再做**

> 这些步骤涉及真钱与不可逆操作，**当前阶段不做**。等策略验证完成、正式转移资金前，
> 用当时的最新代码重新走一遍。

| # | 步骤 | 谁能做 |
|---|---|---|
| 1 | 新建专用 Ethereum 钱包 | **用户**（涉及私钥） |
| 2 | 用 UDA（`bridge.lighter.xyz`）从 Arbitrum/Base 入金 USDC，**首次入金自动建账户** | **用户** |
| 3 | 建 sub-account（机器人专用） | agent（需 L1 签名配合） |
| 4 | 注册 API key（链上 `ChangePubKey`） | agent 写脚本，**用户离线签名**（私钥不落服务器） |
| 5 | 切 Plus/Premium 档 | agent |

> **补充（2026-10-27 实测）**：第 2 步的入金链**支持 Arbitrum**（UDA 覆盖 Ethereum /
> Arbitrum / Base / Optimism / Polygon / BNB / HyperEVM / Monad / Robinhood / Solana / Tron /
> Bitcoin）。入金后用 `GET /api/v1/accountsByL1Address?l1_address=0x...` 查
> `account_index`（**只读、无需鉴权**），`sub_accounts` 里 `account_type == 0` 的那项是 master。
>
> 注意：**Lighter RH（Robinhood Chain）是完全独立的交易所**，账户/余额/API key/nonce 全部
> 不互通，抵押是 **USDG**（不是 USDC），签名 chain ID `466324`，且 market_id 要重新读。
> 实测美国 IP 访问 RH 的 `/sendTx` 同样被 `20558` 拒 —— **RH 不是绕过地域限制的路径**。

---

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| SDK 签名错误 → 下错单 | 全走官方 SDK；`dry` 模式先跑 |
| `clientOrderIndex` 撞车 → 重复开仓 | sha256 派生 + 跨市场唯一 |
| 精度取整导致风险偏离 | `round_trip_check`；偏离 > 0.1% 拒单 |
| 对账误判 → 误 kill | 先 sync fills 再判；fill 自证；宽限期 |
| API key 泄露 | 独立 sub-account，只放小额；L1 私钥不过服务器 |
| Lighter 序化器故障 | **Escape Hatch**：可用 Ethereum blob 自行重建状态并链上提款 |
| 429 限流 | Plus/Premium 档；批量操作排队 |

**回滚**：单容器、单 DB、镜像 tag 化 → `docker compose up -d` 换 tag 即回滚。旧系统全程不动。

---

## 10. 编排方式

- 每个 task 派**一个实现子代理**（在 git worktree 里做，PR → CI → merge）
- 每个 task 派**一个独立审核子代理**（对抗审查：找 bug，不是找风格）
- 我做**验收**：亲自读关键 diff、跑验证命令、核对判据
- **一次只推进一个 task**（依赖是链式的），T3/T4 可并行
- 子代理自报不算数——**每个 task 我都要独立取证**
