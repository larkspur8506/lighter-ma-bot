# lighter-ma-bot 开发计划

> 状态：2026-10-27 定稿。执行方式：**子代理逐任务实现 + 独立审核**（我负责编排、审查、验收）。
> 依据：旧仓库 `larkspur8506/crypto-ma-alert`（HL 现役）实测 + Lighter 官方文档全量 + 公开接口实测。

---

## 0. 定位

**一个纯自动交易系统**：在 Lighter（zkLighter）永续上跑 BTC/ETH/SOL 4h 双均线密集突破策略，**没有任何人工下单环节**。

与旧系统的关系：**并行运行，互不干扰**。旧系统（HL）继续 live，不降级不改动；新系统独立仓库、独立 DB、独立容器、独立 Lighter 账户。

---

## 1. 决策点（已确认）

| 决策 | 结论 |
|---|---|
| 旧系统是否暂停 | **不停**，并行运行 |
| L1 钱包 | **新建专用 Ethereum 钱包**，只放 Lighter 的钱 |
| 账户模式 | **Simple Trading Account（Classic）**，`account_trading_mode=0` |
| 机器人群组 | **独立 sub-account**，只放机器人有权亏的钱 |

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

| 标的 | 历史长度 | 突破信号 | **过密集门槛（实际入场）** | 笔/年 |
|---|---|---|---|---|
| BTC | 1.77 年 | 1,278 | **87** | 49.0 |
| ETH | 1.77 年 | 1,241 | **118** | 66.5 |
| SOL | 1.77 年 | 1,392 | **94** | 53.0 |
| 合计 | | 3,911 | **299 笔** | 169 |

旧回测：**700 笔 / 6.10 年**（年化 115 笔，Binance 4h 全历史）。

→ **迁到 Lighter 后历史证据从 700 笔缩到 299 笔（覆盖率 43%）**，且这 299 笔是 Lighter 自己的 K 线产生的。
旧回测只能证明「策略有效」，**不能证明在 Lighter 执行场所同样有效**。新系统的回测必须用 Lighter K 线重跑（`scripts/backtest.py`），并把两个结果并列报告。

---

## 7. 任务分解（13 个 task）

每个 task = 一次子代理实现 + 一次独立审核 + 我的验收。子代理在 worktree 里干活，走 PR → CI → merge。

### T1 — 仓库骨架 + CI
- 建 `requirements.txt`（`lighter-sdk==1.1.8`）、`Dockerfile`（装 WASM 签名器）、`.gitignore`、`README.md`（含第 6 节证据表）
- GitHub Actions：test → build
- **验收**：`import lighter` 成功；CI 绿；容器起得来

### T2 — `strategy.py` 逐字移植 + 基线
- 从旧仓库复制 `app/strategy.py`（528 行，**一字节不改**）
- 同时复制旧仓库 `scripts/xvalidate.py` 作对照工具
- **验收**：同一组 K 线喂新旧两版，信号集合（bar_ms + 方向 + 止损价）**逐笔完全一致**

### T3 — `symbol_registry.py` + `market.py`
- 注册表：symbol → `{market_id, price_decimals, size_decimals, min_base_amount, min_quote_amount, min_initial_margin_fraction}`，**运行时从 `orderBookDetails` 拉取并缓存，禁止硬编码 market_id**（官方明确会变）
- `market.py`：`candles` 翻页、只保留已收盘 bar、**fail-closed**（主源失败抛 `FeedError`，绝不换源）、新鲜度校验（最后一根收盘 ≤ 8h）
- **验收**：最新 bar 与官方 UI 一致；时间戳单调；根数 ≥ 376；未收盘 bar 被剔除

### T4 — `sizing_lt.py`（整数化 sizing）
纯函数：
- `target_notional(equity, risk_pct, stop_dist) -> float`
- `to_int_qty(notional, price, size_decimals) -> int`（**floor**）
- `to_int_price(price, price_decimals) -> int`
- `round_trip_check(...) -> bool`（反算名义偏差 ≤ 1 tick）
- **验收**：BTC sizeDec=5 / ETH=4 / SOL=3 边界各有用例；名义误差 < 0.01%；不产生 0 股或低于 `min_base_amount` 的单

### T5 — `db.py` + 迁移
新表：`positions`（无 pending/ignore）、`orders`（`client_order_index` 幂等键）、`fills`、`events`、`snapshots`。保留「DB > env > 默认」优先级。
- **验收**：迁移脚本幂等；`Config().apply_db()` 回读值正确（**旧系统踩过的坑**：数字被存成字符串会静默失效）

### T6 — `engine.py` 状态机（去人工流）
- 状态 `IDLE → OPEN → CLOSED`，每标的至多一仓
- `walk()` 纯函数，输出入场/离场/止损事件
- **验收**：用旧系统 dry-run 回放的同一组 bar，新引擎的**开平仓回合**逐笔一致（入场 bar、方向、止损价、离场 bar、R 倍数）

### T7 — `exchange.py`（唯一写路径）
- 懒加载 `SignerClient`；缺 SDK/缺凭据 → **fail-closed**
- `place_market_with_slippage(coin, side, notional, slip_bps) -> tx_hash`
- `place_stop_loss(coin, side, qty, trigger_price, expiry_ms)`
- `cancel_all(coin)` / `cancel_all_orders()`
- **验收**：异常归一化成 `{ok, code, error}`；**`code=200` 只表示已接受**，必须等 `tx` 确认或 WS `account_tx`

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

### T11 — 回测 + 上线验证
- `scripts/backtest.py`：Lighter 全历史 1.77 年、299 笔 → 均R/胜率/最大回撤/CAGR
- `scripts/xvalidate.py`：回测引擎 vs 线上 `engine.py` 信号逐笔一致
- 上线四步 `off → dry → live → 常态化`，每步有验收
- **验收**：dry 跑满至少 2 根 4h bar，信号数与回测预期一致

---

## 8. 一次性人工步骤（需用户参与）

| # | 步骤 | 谁能做 |
|---|---|---|
| 1 | 新建专用 Ethereum 钱包 | **用户**（涉及私钥） |
| 2 | 用 UDA（`bridge.lighter.xyz`）从 Arbitrum/Base 入金 USDC，**首次入金自动建账户** | **用户** |
| 3 | 建 sub-account（机器人专用） | agent（需 L1 签名配合） |
| 4 | 注册 API key（链上 `ChangePubKey`） | agent 写脚本，**用户离线签名**（私钥不落服务器） |
| 5 | 切 Plus/Premium 档 | agent |

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
