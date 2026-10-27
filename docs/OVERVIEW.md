# lighter-ma-bot — 开发期总纲

> 2026-10-27 开发期（T1–T11）收工时的状态快照。
> **本文是入口**：先看这里，再按需翻各模块文档。
> **当前阶段**：只做开发。真实上线（T12 部署 / T13 上线演练）属阶段二，见 §7。

---

## 1. 这是什么

在 **Lighter (zkLighter)** 永续上跑 **BTC/ETH/SOL 4h 双均线密集突破**策略的**纯自动交易系统** ——
**没有任何人工下单环节**。

与旧系统 `larkspur8506/crypto-ma-alert`（HL 现役）的关系：**并行运行，互不干扰**。
旧系统继续 live，不降级不改动；新系统独立仓库、独立 DB、独立容器、独立 Lighter 账户。

**唯一继承旧系统的东西是信号口径**：`app/strategy.py` 逐字节复制（sha256 冻结），
Lighter 侧的改造只发生在**信号之后**（定仓 → 下单 → 对账 → 风控 → 展示）。

---

## 2. 架构

```
                    ┌──────────────────────────────────────┐
   Lighter API ───► │ app/market.py    K线/盘口/元信息      │  只读，限流退避
   (主网/测试网)    │ app/proxy.py     代理注入(可选)        │
                    └──────────────────────────────────────┘
                                        │ Bar (4h, 已收盘)
                                        ▼
                    ┌──────────────────────────────────────┐
                    │ app/strategy.py  ★冻结★ 信号口径      │  528 行, sha256 冻结
                    └──────────────────────────────────────┘
                                        │ Signal
                                        ▼
                    ┌──────────────────────────────────────┐
                    │ app/lifecycle.py  决策编排            │  sizing→risk
                    │ app/sizing.py     整数化定仓          │  floor, 不多买
                    │ app/risk.py       7 道 fail-closed 闸  │  只挡开仓
                    └──────────────────────────────────────┘
                                        │ Decision
                                        ▼
                    ┌──────────────────────────────────────┐
                    │ app/exchange.py   ★唯一写路径★        │  官方 SDK
                    │ app/executor.py   tick 编排 + 对账    │
                    └──────────────────────────────────────┘
                                        │
                    ┌──────────────────┴───────────────────┐
                    │ app/store.py   SQLite 状态           │
                    │ app/state.py   → RiskState 装配       │
                    │ app/dd.py      TWR 回撤 + 熔断        │
                    └──────────────────────────────────────┘
                                        │
                    ┌──────────────────┴───────────────────┐
                    │ app/views.py + app/main.py  监控面板   │
                    │ app/watchdog.py 健康监控              │
                    └──────────────────────────────────────┘
```

### 单一真相来源（这是整个设计的核心）

同一个概念**只有一份实现**，其它地方一律 import：

| 概念 | 唯一来源 | 曾经踩过的坑 |
|---|---|---|
| 信号口径 | `app/strategy.py` | —— |
| 回撤/档位/熔断判定 | `app/risk.py` 的 `dd_thresholds`/`dd_action` | 旧系统「三个回撤口径混用」 |
| TWR 收益指数 | `app/dd.py` 的 `advance`/`bootstrap` | 出金被当成收益，亏损被抹掉 |
| 行情获取 | `app/market.py` | —— |
| 状态转换合法性 | `app/store.py` 的 `ALLOWED_TRANSITIONS` | —— |

**验证方式**：`app/dd.py` 里的 `dd_thresholds` 与 `app/risk.py` 的是**同一个对象**
（`is` 为 True，有测试断言）。任何「另写一套」的实现都会立刻被测出来。

---

## 3. 已验证的能力（都是实跑过的，不是设计意图）

| 能力 | 证据 |
|---|---|
| 信号口径逐字移植 | sha256 `8682667…`，与旧仓库**远端当前版本**逐字节一致（`cmp` 通过） |
| 新旧引擎信号一致 | Lighter 全历史 3889 根，**1251 笔信号逐笔一致**（入场价/止损价最大差 0） |
| K 线数据完整性 | 升序、去重、缺口逐条报告、未收盘 bar 点名剔除、新鲜度 ≤8h |
| 定仓精度 | 数量**向下取整**，风险**只减不增**（0.04156 BTC，实际风险 132.992 ≤ 目标 133.000） |
| 风控闸门 | 7 道全部 fail-closed，每条拒绝带结构化 reason |
| **减仓不受熔断阻挡** | `decide_exit()` 签名里**根本没有** `risk_state` 参数 —— 结构性保证，不是 if 判断 |
| 对账 | 6 类不一致全部报出并阻断开仓；`reconcile()` **拿不到 store**，结构上不可能改库 |
| 止损成交不误报 | 三条件缺一不可：挂单列表读到了 + 该标的已无仓 + 我们的止损单确实不在 |
| 回撤剔除出金 | 入金 1000 后 DD 仍是 10%（不剔会变 0%，亏损被抹掉） |
| 熔断永不自解 | `reset_dd_peak` 必须显式 + 落审计 |
| 真实 SDK 签名 | entry/stop 的 `tx_info`、`Sig`（80 字节）、`ReduceOnly`、`TriggerPrice` 全部实测 |
| 重试不重复下单 | 只签一次、nonce 恒定、payload 逐字节相同；传输错误先查 `/tx?by=hash` |
| 代理出口 | oracle1 + 日本/德国节点 → Lighter `sendTx` 从 `20558` 变 `20001`（过地域检查） |
| 面板不碰私钥 | `import app.views` / `import app.main` 后 `app.exchange`/`lighter` **不在** `sys.modules` |
| 降级不崩 | DB 不可用 → 8 端点全 200 + 0 值 + WARNING，不是 500 |
| 连接不泄漏 | 全量测试 `ResourceWarning` 计数 = 0（修前 5） |

---

## 4. 回测结果（Lighter 全历史，1.77 年 / 3889 根 / 起 10000 USDC）

| 风险档 | 笔数 | 胜率 | 均R | CAGR | 终值 | 最大回撤 | 熔断档 |
|---|---|---|---|---|---|---|---|
| **1.33%** | 208 | 33.7% | +0.22 | **+25.65%** | 1.50x | 22.16% | 25%/30% |
| 2.00% | 208 | 33.7% | +0.22 | +33.80% | 1.68x | 31.89% | 30%/40% |
| 3.00% | 208 | 33.7% | +0.22 | +40.73% | 1.83x | **45.08%** | 40%/50% |

**信号漏斗**：破线 6874 → 过密集 1582 → 信号 1251 → **成交 208**（跳过 1043 笔 = 持仓中）。

### 三个必须知道的结论

1. **收益几乎全来自 ETH/SOL。** BTC 单独 63 笔、CAGR 仅 +2.3%；SOL 70 笔却 +18.4%。
   **样本太小，不足以说策略在 Lighter 上稳健。**
2. **3% 档接近熔断线。** 回撤 45.08% vs kill 50% —— 风险翻倍收不到线性收益，回撤却翻倍。
   **1.33% 是合理默认值。**
3. **滑点默认 0，而 Lighter 深度只有 HL 的 1/5。** 敏感性：5bps 滑点 → CAGR 从
   +25.65% 掉到 +18.47%（**-7 个点**）。**薄盘口上的实盘滑点纪律是核心风险。**

### 口径说明

README/PLAN 早期写的「299 笔」是**旧口径**。冻结口径下是 **1251 笔信号 → 208 笔成交**
（差额是「持仓中跳过」）。详见 `docs/BACKTEST.md`。

---

## 5. 已知限制（诚实清单）

**回测/建模未覆盖**：
- 强平（止损 ≤10% 时强平 -19.5% 永远更晚，未建模）
- 部分成交、精度取整/最小下单量、限频
- **滑点默认 0**（最需要警惕的一条）
- Lighter 4h 历史只有 1.77 年（HL 研究用了 6.1 年）

**工程未覆盖**：
- **WebSocket 未接** —— 全走 REST 轮询。官方 SDK 的 WS 走 `websockets` 库，
  **默认不读代理环境变量**（需显式传 `proxy=`）。将来启用要单独处理。
- `app/exchange.py` 的 aiohttp session 在进程退出时释放（随进程退出，非累积泄漏）
- `default_tick_runner` 里 `ExchangeClient.close()` 是 async，而 `TickScheduler.close()`
  不能 await —— 严谨收尾需要把 runner 的 close 钩子改成 async
- **Lighter 交易端点有服务端 IP 地域拦截**（`code 20558`）—— 见 §6

---

## 6. 部署约束（2026-10-27 实测，**必读**）

Lighter 对**交易端点**（`/api/v1/sendTx`、`/api/v1/sendTxBatch`、WS `/stream`）做服务端
IP 地域拦截，命中返回：

```json
{"code":20558,"message":"You are accessing Lighter from a restricted jurisdiction..."}
```

**读端点全部正常**（candles / orderBookDetails / account / nextNonce / accountsByL1Address）。

各机器出口实测：

| 机器 | 出口 | `sendTx` |
|---|---|---|
| 家网本机 | 印度 孟买 `137.23.52.79` | ✅ `20001`（过地域检查） |
| oracle1 | 美国 圣何塞 `64.181.225.174` | ❌ `20558` |
| oracle2 | 美国 圣何塞 `152.70.122.121` | ❌ `20558` |
| google | 美国 俄勒冈 `35.212.253.78` | ❌ `20558` |

**对照**：Hyperliquid 的下单端点在同一台美国机器返回 `422`（请求已进撮合逻辑）——
HL 只在前端层拦，**Lighter 是在 API 服务端层拦**。「HL 能跑」推不出「Lighter 能跑」。

**已就位的解法**：oracle1 上跑 sing-box，本地 HTTP 代理 `127.0.0.1:8118`，
4 个节点（日本×2 / 德国×1 / SS 备用）+ `urltest` 自动故障转移，**只对 lighter 域名分流**。
详见 `docs/PROXY.md` 与技能 `devops/lighter-proxy-egress`。

> **注意**：代理是**补丁**不是**架构**。更稳的结构是把执行器放在**非受限地区机器**上，
> 不依赖代理。代理节点失效时，交易系统会哑。

---

## 7. 上线前 checklist（阶段二，当前不做）

### 7.1 一次性人工步骤（**用户**，涉及私钥与真钱）

| # | 步骤 | 说明 |
|---|---|---|
| 1 | 新建**专用** Ethereum/Arbitrum 钱包 | 别用主钱包 —— 只放机器人有权亏的钱 |
| 2 | 入金 ≥1 USDC（Arbitrum，走 UDA `bridge.lighter.xyz`） | **首笔入金自动建账户** |
| 3 | 查 `account_index` | `GET /api/v1/accountsByL1Address?l1_address=0x...`（**只读、无需鉴权**），<br>`sub_accounts` 里 `account_type == 0` 那项是 master |
| 4 | 注册 API key | 跑 `scripts/offline_sign.py`（**用户本机**，私钥只走 env/getpass，不进 argv） |
| 5 | （可选）建 sub-account | `SignCreateSubAccount`，隔离爆炸半径 |

**注意**：Lighter RH（Robinhood Chain）是**完全独立的交易所** —— 账户/余额/API key/nonce
全部不互通，抵押是 **USDG**（不是 USDC），chain ID `466324`，market_id 要重读，
且美国 IP 访问同样被 `20558` 拒。**不是绕过地域限制的路径。**

### 7.2 部署

- 机器：**非受限地区**（见 §6）
- 镜像 tag 化，回滚 = 换 tag
- 配置走 env（`.env`），**私钥不进仓库**（`.gitignore`/`.dockerignore` 已覆盖）

### 7.3 上线四步

`off → dry → live → 常态化`，每步有验收。
**dry 至少跑满 2 根 4h bar，信号数与回测预期一致**（回测漏斗：信号 1251 / 成交 208）。

### 7.4 上线后重点盯

- 首次成交的**实际滑点** vs 回测假设（5bps 敏感度 -7 个点）
- 对账告警（`position_missing_on_exchange` 等 6 类）
- 止损单覆盖（面板「止损单覆盖」区块）
- 熔断是否误触发 / 永不自解
- 代理出口是否仍是预期国家（`urltest` 日志）

---

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| `PLAN.md` | 总体计划、阶段划分、部署约束、任务分解 |
| `STRATEGY_PORT.md` | 信号口径怎么移植的、怎么防止漂移 |
| `SIZING_RISK.md` | 定仓公式、精度处理、7 道风控闸 |
| `STATE_STORE.md` | schema、迁移、原子性、幂等键、DD 锚点生命周期 |
| `LIFECYCLE.md` | 状态机、离场判据、减仓不受熔断阻挡 |
| `EXCHANGE.md` | 两种签名、token 生命周期、nonce 规则、错误分类、私钥规约 |
| `EXECUTOR.md` | tick 编排、对账不变量、停机熔断 |
| `DD_WATCHDOG.md` | TWR 算法、档位表、watchdog 检查项 |
| `DASHBOARD.md` | 6 区块、端点、鉴权、降级、面板运行依赖 |
| `PROXY.md` | 代理接入、域名判定、运维配置 |
| `BACKTEST.md` | 回测假设、结果、与旧系统基线的分层对比 |

---

## 9. 复现

```bash
# 测试（810 条，全离线）
python -m unittest discover -s tests -t .

# 信号交叉验证（真实网络）
python scripts/xvalidate.py --cache-dir .cache

# 全历史回测（真实网络）
python scripts/backtest.py --cache-dir .cache

# 面板冒烟
PANEL_SCHEDULER=0 ADMIN_TOKEN=x DB_PATH=/tmp/s.db \
  python -m uvicorn app.main:app --port 8100
```
