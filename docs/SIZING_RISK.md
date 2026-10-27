# 仓位与风控（T4）

本文件说明 `app/sizing.py` / `app/symbol_registry.py` / `app/risk.py` 的口径。三份代码 +
`tests/test_sizing.py` / `tests/test_symbol_registry.py` / `tests/test_risk.py` 是同一批交付：
**文档里的每个数字都能在测试里找到一条写死的断言。**

---

## 1. 职责与边界

| 模块 | 职责 | 输入 | 输出 | 网络 |
|---|---|---|---|---|
| `app/symbol_registry.py` | 本系统**允许交易哪些标的**及其 Lighter 元数据 | symbol | `SymbolSpec` / `MarketInfo` | 只在**显式传入 client** 时查询 `orderBookDetails` |
| `app/sizing.py` | 从「信号 + 权益 + 市场规则」算出下单数量 / 止损价 / 杠杆 | `SizingRequest` + `MarketInfo` + `SizingConfig` | `OrderPlan` 或 `Rejection` | **无**（`app.market` 只在 `TYPE_CHECKING` 下作类型引用；`tests/test_sizing.py::NoNetworkImportTests` 用 AST 断言） |
| `app/risk.py` | 开仓前的一道闸 | `RiskState` + 名义 + `RiskLimits` | `RiskDecision`（每个闸门一条 `GateResult`） | **无** |

**信号与执行解耦**：`app/strategy.py` 只产信号（冻结，sha256 见下），本层把信号变成
「下多少」的**建议**，`exchange.py`（T7）才是唯一写路径。三者都不碰签名器。

```
Signal(side, entry_price, stop_price)  ─┐
Config.risk_pct (+ SizingConfig)       ─┼─► sizing.plan_order ─► OrderPlan ─┐
MarketInfo (orderBookDetails)          ─┘                                  ├─► risk.check_entry ─► 下单 / 拒单
RiskState (equity, positions, DD)      ────────────────────────────────────┘
```

---

## 2. 风险定仓公式

```
风险金额  risk_amount   = 权益 × risk_pct                 # risk_pct 是比例：0.0133 = 1.33%
止损距离  stop_distance = |入场价 − 止损价|
仓位数量  base_qty      = risk_amount ÷ stop_distance     # base 币数量
名义      notional      = base_qty × 入场价
保证金    margin        = notional ÷ 杠杆
```

**杠杆不参与仓位大小的计算**，它只决定保证金占用与强平价。这是旧系统
（`crypto-ma-alert`，HL 现役）的口径，原样继承。

`risk_pct` 是**比例不是百分数**：`SizingConfig(risk_pct=13.3)` 会直接 `ValueError`
（这是「13.3% 写成了百分数」的典型错法，静默当 13.3% 会把账户一把亏光）。

### 2.1 完整算例（BTC，文档与 `test_btc_worked_example` 一致）

| 项 | 值 | 推导 |
|---|---|---|
| 权益 | 10,000.00 USDC | 输入 |
| `risk_pct` | 0.0133（1.33%） | `RISK_PCT` / `SizingConfig` 默认值 |
| 入场价 | 80,000.0 | 信号 `entry_price`（下一根 bar 开盘 / 当前 mid） |
| 止损价 | 76,800.0（−4%） | 信号 `stop_price`（信号 bar 的 `ma_min`） |
| 风险金额 | **133.00 USDC** | 10,000 × 0.0133 |
| 止损距离 | **3,200.0** | 80,000 − 76,800 |
| 原始数量 | 0.0415625 BTC | 133 ÷ 3,200 |
| 精度（`size_decimals`=5） | 4156 tick | floor(0.0415625 × 10⁵) = floor(4156.25) |
| **最终数量** | **0.04156 BTC** | 4156 ÷ 10⁵（少买了 0.0000025，≈0.2 USDC 名义） |
| 名义 | **3,324.80 USDC** | 0.04156 × 80,000 |
| 杠杆（用户配置） | **5x** | 交易所上限 50x，取小的 |
| 保证金 | **664.96 USDC** | 3,324.80 ÷ 5 |
| 止损价（对齐 tick） | 76,800.0 → 768000 | `price_decimals`=1，多单 ceil |
| **实际风险** | **132.992 USDC** | 0.04156 × 3,200 |

实际风险 132.992 ≤ 目标 133.00：**floor 永远只会让风险变小**。

---

## 3. 杠杆模型

Lighter 的初始保证金率来自 `MarketInfo.min_initial_margin_fraction`（**万分比整数**）：

```
交易所最大杠杆 = 10000 ÷ min_initial_margin_fraction
                BTC/ETH: 10000/200 = 50x
                SOL:     10000/400 = 25x
```

生效杠杆 = `min(用户配置 leverage, 交易所最大杠杆)`。

* **默认 5x**，与旧系统逐仓 5x、700 笔回测口径一致（`SizingConfig.leverage`，env `LIGHTER_LEVERAGE` 可配）。
* 配置**高于**交易所上限时：降级到上限，并在 `OrderPlan.warnings` 写一条
  `leverage clamped to the exchange cap: configured 60x > 50x (...)` —— 不静默无视配置。
* **不用交易所最大杠杆当默认值**：40x/50x 只是「能开多大」，不是「该开多大」。仓位大小
  由风险定，杠杆只影响保证金和强平距离；用满杠杆会让一次插针直接强平。

### 3.1 权益不足 → 拒单（不静默缩仓）

```
required_margin = notional ÷ 生效杠杆
required_margin > 权益  ⟹  Rejection("insufficient_margin")
```

不「自动改成能开得下的仓位」：那等于偷偷改了用户下的风险比例，而且回测（按目标风险）
与实盘（缩小后）会永久对不上。例：0.25% 止损、1.33% 风险 → 名义 = 133/0.0025 = 53,200，
5x 下保证金 10,640 > 权益 10,000 → 拒单。

---

## 4. Lighter 精度处理

数量与价格在 Lighter 都是**整数**：`整数 = value × 10^decimals`。

| 标的 | `market_id` | `price_decimals` | `size_decimals` | `min_base_amount` | `min_quote_amount` | mif | 交易所最大杠杆 |
|---|---|---|---|---|---|---|---|
| BTC | 1 | 1 | 5 | 0.00007 | 10.0 | 200 | 50x |
| ETH | 0 | 2 | 4 | 0.0020 | 10.0 | 200 | 50x |
| SOL | 2 | 3 | 3 | 0.055 | 10.0 | 400 | 25x |

（实测 2026-10-27，`GET /api/v1/orderBookDetails`，mainnet。**运行时以 `MarketInfo` 为准**，
上表只是 `symbol_registry` 的兜底值。）

规则：

1. **数量一律向下取整（floor）**：宁可少买，不可多买。多买会让实际风险超过 `risk_pct`。
   实现用 `Decimal(repr(x))` 做放大，避免 `0.00029 × 1e5 = 28.999999999999996` 这类
   float 残差吃掉一个 tick。
2. **取整后数量 = 0 → 拒单**（`qty_below_tick`）。例：0.000012 BTC 在 4 位精度下是
   0.12 个 tick → 0。这条是硬边界：**绝不下 0 数量的单**。
3. **`min_base_amount`**：`base_qty < min_base` → 拒单（`below_min_base`）。例：0.000012 BTC
   在 5 位精度下是 1.2 tick → floor 1 tick = 0.00001 < 0.00007 → 拒单
   （所以「截断到 0」和「截到 1 tick 但仍不够最小量」两条路都是拒单，没有漏网）。
4. **`min_quote_amount`**（名义下限，实测 10 USDC）：名义不足 → 拒单（`below_min_quote`）。
   BTC 上 0.00007 × 80,000 = 5.6 USDC，所以 BTC 的实际门槛是名义 10（≈0.000125 BTC）。
5. **止损价按「朝入场价方向」取整**：多单 `ceil`、空单 `floor`。这样取整后止损距离
   **只会变小**，实际风险 ≤ 目标风险。一律 floor 的话空单止损会被推远，等于放大风险。
6. **反算校验 `round_trip_check`**：floor 造成名义损失必须 ≤ `max(一个 size tick 的名义, 目标 × 0.01%)`，
   否则拒单（`round_trip_mismatch`，对应 PLAN §9 的「精度取整导致风险偏离」风险项）。

---

## 5. 标的注册表与 `market_id`

* 白名单**只有** BTC / ETH / SOL（`symbol_registry.REGISTRY`，加标的只改这一个文件的
  一行 `SymbolSpec`）。
* **未注册标的 = 显性拦截**：`spec_strict()` 抛 `SymbolNotConfigured`，`sizing.plan_order()`
  返回 `symbol_not_registered` 拒单。旧系统「漏配=显性拦截」的原则保留 —— 一个没登记过的
  symbol 不可能被静默按别人的精度下单。（旧系统的坑：`SPECS` 漏一个标的，`size_order` 抛
  `KeyError` 被 tick 的 `except` 吞成「处理异常」，整个标的的信号一个都出不来。）
* **`market_id` 不硬编码在业务代码里**：`resolve_market_info(symbol, client)` 优先用运行时
  `orderBookDetails`；端点不可达/缺该标的时回退注册表兜底值，并**一定**在
  `ResolvedMarket.warnings` 留一条 warning。运行时值与兜底值不一致（testnet 常见）时只
  **记 warning 不拒单** —— 运行时的值才是权威，warning 是给人看的漂移证据。
* 兜底值与 `app.market.FALLBACK_MARKET_IDS` 必须一致：注册表 import 时校验，不一致直接
  `ImportError`（两处常量漂移会让「查哪张合约」在不同角落给出不同答案）。

---

## 6. 风控闸门（`app/risk.py`）

`check_entry(symbol, notional, state, limits)` 一次跑完 7 个闸门，返回
`RiskDecision(allowed, gates)`。**每个闸门都返回独立的 `GateResult(gate, severity, code,
message, detail)`** —— 面板/告警按 `code` 判断，不解析文案。

| 闸门 | `code` | 默认阈值 | 触发后果 | 来源 |
|---|---|---|---|---|
| 人工/自动 kill | `kill_switch` | off | 拒绝开新仓（**不挡离场**） | 旧 `HL_EXECUTOR_PLAN.md` §3.6 |
| 账户状态 | `state_unavailable` | — | 权益读不到/≤0 → 拒绝 | fail-closed |
| 当日亏损 | `daily_loss` | −5% 权益 | 拒绝开新仓 | 旧计划 §3.7 |
| 单日下单笔数 | `daily_trade_limit` | 4 笔 | 拒绝开新仓（防重试风暴） | 旧计划 §3.7 |
| 最大同时持仓 | `max_positions` | 3 | 拒绝开新仓 | PLAN T6「每标的至多一仓」，面板「N/3」 |
| 同标的重仓 | `duplicate_symbol` | — | 拒绝（同标的第二个仓位） | PLAN T6 状态机 |
| 每标的名义上限 | `symbol_notional` | 权益 × 1.5 | 拒绝 | 旧计划 §3.7 |
| 总名义上限 | `total_notional` | 权益 × 2.5 | 拒绝 | 旧计划 §3.7 |
| 回撤熔断 | `drawdown` | 按风险档位（见 §7） | ≥ 熔断线拒绝；≥ 告警线只 warn | PLAN T9 |
| 名义非法 | `bad_notional` | — | 名义 ≤0/NaN → 拒绝 | fail-closed |

**持仓上限 = 3 且每标的至多 1 仓**：PLAN T6 写的是 `IDLE → OPEN → CLOSED`，
「每标的至多一仓」；面板状态条写「持仓 N/3」。所以 `max_positions=3` 是「三个标的各 1 仓」，
同标的开第二仓一律 `duplicate_symbol`，**不是**「允许同标的多仓、总数 ≤ 3」。

---

## 7. 回撤保护（DD Guard）与风险档位联动

阈值**照抄不现编**（`docs/PLAN.md` T9 + 旧仓库 `app/hl/dd.py::TIERS`）：

| `risk_pct` 档 | 告警线 | 熔断线 |
|---|---|---|
| ≤ 1.5%（含 1.33% 实盘档） | 25% | 30% |
| ≤ 2.5%（2% 档） | 30% | 40% |
| 更大（3% 档及以上） | 40% | 50% |

**风险档越高阈值越宽**：`risk_pct` 越大，同一策略的历史最大回撤就越深。用低档的 30% 去挡
3% 档，等于给一条必然出现 40%+ 回撤的曲线装一条 30% 的熔断线 —— 长期误触发（旧 `TIERS`
的注释原文如此）。三档数值与 PLAN 第 T9 节逐字一致。

* `risk_pct` 读不出（None/NaN）→ 用**最严档**（25/30）：宁可早喊人，不要漏报。
* 手工覆盖（`DD_WARN_PCT` / `DD_KILL_PCT`）：必须 `0.05 ≤ warn < kill ≤ 0.95`，否则
  **两者一起**退回档位默认并在 `rejected` 标记（只退一个会造出「告警 40% / 熔断 30%」）。
* 回撤口径是 **TWR 指数**的峰谷回撤（剔出入金）：`DD = (peak_index − index) / peak_index`。
  T9 实现 `dd.py` 时**必须** import 这里的 `dd_thresholds()` / `dd_action()`，不要另写一份
  —— 面板显示阈值与熔断判据必须是同一个函数（旧系统「三个回撤口径混用」的坑）。
* `dd_index` / `dd_peak_index` 缺失 → **拒绝开新仓**（`drawdown` + `detail.state_unavailable`）。
  首次上线由 T9 bootstrap 后再 tick。

---

## 8. fail-closed 清单

### `sizing.plan_order()` 的拒单码

| `code` | 触发条件 |
|---|---|
| `symbol_not_registered` | 标的不在注册表（白名单只有 BTC/ETH/SOL） |
| `market_not_tradeable` | `is_frozen=True` 或 `status != "active"` |
| `missing_market_rules` | `MarketInfo` 缺失 / `mif ≤ 0` / 精度非法（无法推杠杆上限或无法整数化） |
| `bad_input` | 权益 ≤0 或非有限、入场/止损价 ≤0、`side` 不是 ±1、止损价小于一个 tick |
| `stop_on_wrong_side` | 多单止损不低于入场价（或空单止损不高于） |
| `qty_below_tick` | floor 后数量 = 0 |
| `below_min_base` | `base_qty < min_base_amount` |
| `below_min_quote` | 名义 `< min_quote_amount` |
| `insufficient_margin` | `notional ÷ 杠杆 > 权益` |
| `round_trip_mismatch` | floor 造成的名义偏差 > 一个 tick / 0.01% |

### `risk.check_entry()` 的 fail-closed 行为

| 状态 | 结果 |
|---|---|
| `RiskState()` 全空 | 拒绝（`state_unavailable` + `daily_loss` + `drawdown` 多条） |
| 权益缺失/≤0/NaN | 拒绝 |
| `day_start_equity` 缺失 | 拒绝（`daily_loss`，`detail.state_unavailable=True`） |
| `trades_today` 缺失 | 拒绝（`daily_trade_limit`） |
| 有持仓但 `open_notional_total` 缺失且无 by-symbol 明细 | 拒绝（`total_notional`） |
| DD 状态缺失 / peak ≤0 / index <0 | 拒绝（`drawdown`） |
| 名义 ≤0 / NaN / inf | 拒绝（`bad_notional`） |
| env 配置填了非法值（`MAX_POSITIONS=-2`、`KILL_SWITCH=maybe`、`RISK_PCT=13.3`） | `RiskConfigError`，启动即失败（不静默回默认） |

「kill / 熔断只挡开新仓」是**单向出口**：`check_entry()` 只被入场路径调用；平仓、撤单、
止损单挂单永远不看这些闸门。已有仓位的止损单在交易所侧照常有效。

---

## 9. 配置项

| env | 模块 | 默认 | 说明 |
|---|---|---|---|
| `RISK_PCT` | `SizingConfig` / `RiskLimits` | 0.0133 | 单笔风险比例（旧系统实盘 1.33%）。`app/config.py` 的字段默认值是 0.01，**运行时以 env 为准**；要复刻旧系统请在 `.env` 写 `RISK_PCT=0.0133` |
| `LIGHTER_LEVERAGE` | `SizingConfig` | 5.0 | 用户侧杠杆上限（生效值再与交易所上限取小） |
| `MAX_DAILY_LOSS_PCT` | `RiskLimits` | 0.05 | 当日亏损熔断线 |
| `MAX_TRADES_PER_DAY` | `RiskLimits` | 4 | 单日下单笔数上限 |
| `MAX_POSITIONS` | `RiskLimits` | 3 | 最大同时持仓（每标的至多 1 仓） |
| `MAX_SYMBOL_NOTIONAL_MULT` | `RiskLimits` | 1.5 | 每标的名义上限 = 权益 × 该值 |
| `MAX_TOTAL_NOTIONAL_MULT` | `RiskLimits` | 2.5 | 总名义上限 = 权益 × 该值 |
| `DD_WARN_PCT` / `DD_KILL_PCT` | `RiskLimits` | 0（= 按档位） | 手工覆盖 DD 阈值 |
| `KILL_SWITCH` | `RiskLimits` | off | 手动停开新仓 |

---

## 10. 测试覆盖（全离线）

`python -m unittest discover -s tests -t .`（CI 就是这个命令）。

* `tests/test_sizing.py`：公式、三种精度的 floor 边界（含「截断到 0 → 拒单」）、
  `min_base` / `min_quote` 拒单、权益不足拒单、未注册标的拒单、冻结市场拒单、
  杠杆取小与 clamp warning、止损朝入场价对齐、round-trip、配置校验、**AST 断言不 import
  网络模块**。
* `tests/test_symbol_registry.py`：三个标的逐字段断言、未注册抛 `SymbolNotConfigured`、
  `market_id` 运行时优先 / 兜底必留 warning / 异常也回退不抛、与
  `app.market.FALLBACK_MARKET_IDS` 一致。
* `tests/test_risk.py`：每个闸门单独触发、DD 三档阈值与覆盖值、缺失状态全部拒绝、
  结构化 `code`/`message`/`detail`、`as_dict()` 可 JSON 序列化（给 T10 面板）。

`app/strategy.py` 未改动：`sha256 = 868266752317212c24a8dcdd4937c775944d24035846ffba86805f34b22ece07`。
`requirements.txt` 未改动（纯 stdlib，没有新增第三方依赖）。
