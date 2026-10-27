# 状态持久化层（T5）：`app/store.py` + `app/state.py`

> 状态：2026-10-27 T5 实现。纯 stdlib `sqlite3`，**不发任何网络请求**。
> 上游：`docs/PLAN.md` T5；下游：T6 引擎、T8 执行器、T9 回撤、T10 面板。

---

## 1. 职责边界

```
        ┌──────────────┐   只读/原子写    ┌───────────────┐
        │ app/store.py │◀────────────────│  T8 executor  │  唯一写者
        │  (SQLite)    │────────────────▶│  T9 dd.py     │
        └──────┬───────┘                 │  T10 面板(只读)│
               │ 读                       └───────────────┘
        ┌──────▼───────┐
        │ app/state.py │ 装配 RiskState（+ 可选注入的交易所读数）
        └──────┬───────┘
               │
        ┌──────▼───────┐
        │  app/risk.py │ 7 个闸门，纯函数
        └──────────────┘
```

* **`app/store.py`**：只负责「把字写进 SQLite 并读回来」。**不 import 任何网络模块**
  （`url…lib` / `req…uests` / `h…ttpx` / `so…cket` 一个都不出现，`tests/test_store.py`
  有一条测试直接读源码断言这一点）。它也因此不 import `app.config`、`app.market`、
  `app.lighter` 里的任何东西 —— 路径由构造参数/鸭子类型给出。
* **`app/state.py`**：把 store（+ 可注入的权益/标记价/交易所持仓）装配成
  `app.risk.RiskState`，并给每个字段标**证据来源**。它只读，唯一的写是显式的
  `ensure_day_start()`。
* **T5 不写 Lighter 账户 API 调用**（那是 T8）：`equity_provider` 是注入点。

### 与 PLAN 命名的对照

PLAN T5 写的是 `db.py` + `positions/orders/fills/events/snapshots`；本任务（T5 任务书）
把表定为 `positions/trades/equity_curve/daily_counters/dd_anchors`。两者是同一件事的两种
拆法，落库后对应关系：

| PLAN 名 | 本实现 | 说明 |
|---|---|---|
| `positions` | `positions` | 去掉 `PENDING`/`IGNORE`，状态机见 §3 |
| `orders` + `fills` | `trades` | 一张流水表；`order_id` + `fill_id` 双列承载幂等键（§5） |
| `events` | `events` | 保留（T10b 的三个写操作审计也要落这里） |
| `snapshots` | `equity_curve` | 权益时间序列 + TWR 指数 |
| — | `daily_counters` | 当日下单计数 / 日初权益 / 当日已实现盈亏 |
| — | `dd_anchors` + `dd_state` | 回撤锚点（可清理）+ 回撤活状态（持久）。见 §6 |
| `configuration` | **不做** | 「DB > env > 默认」的参数层没有消费者（参数在 env + 部署流程里），等真有需求再加版本 3 |

---

## 2. Schema（`PRAGMA user_version = 2`）

### `positions` —— 仓位与状态机

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `symbol` | TEXT | 大写，如 `BTC` |
| `side` | INTEGER | `+1` 多 / `-1` 空（`CHECK`） |
| `state` | TEXT | `IDLE` / `OPEN` / `CLOSING` / `CLOSED`（`CHECK`） |
| `bar_ms` | INTEGER | 信号 bar（毫秒，与 `strategy.py` 同口径） |
| `qty_int` / `qty` | INTEGER / REAL | 整数 tick + 反算的 base 数量 |
| `size_decimals` / `price_decimals` | INTEGER | 精度，随行落库 |
| `entry_price_int` / `entry_price` | INTEGER / REAL | 入场价 |
| `stop_price_int` / `stop_price` | INTEGER / REAL | 止损价（**取整后**的真实挂单价） |
| `exit_price_int` / `exit_price` / `exit_order_id` | | 平仓侧（v2 补列） |
| `notional` / `risk_amount` / `equity_at_entry` | REAL | 开仓时的口径 |
| `entry_order_id` / `stop_order_id` / `client_order_index` | | 交易所侧标识（`client_order_index` 是 Lighter 的 uint48 幂等键，v2 补列） |
| `opened_at` / `closed_at` / `exit_reason` / `net_pnl` | | 时间与结算 |
| `created_at` / `updated_at` | REAL | |
| — | UNIQUE | `(symbol, bar_ms)` → 同一根 bar 同一标的只开一次 |

索引：`(symbol, state)`、`(state)`、`(client_order_index)`。

### `trades` —— 成交流水

| 列 | 说明 |
|---|---|
| `ts` | 成交时刻（epoch 秒，**不是**入库时刻） |
| `symbol` / `side` / `price` / `price_int` / `qty` / `qty_int` | 成交明细（`price > 0` 有 `CHECK`） |
| `fee` / `fee_token` | 手续费原值与币种 |
| `kind` | `entry` / `exit` / `stop`（`CHECK`） |
| `order_id` / `fill_id` | 交易所订单号 / 成交号（幂等键来源） |
| `position_id` / `realized_pnl` | 归属仓位 / 该笔已实现盈亏 |
| `bar_ms` / `raw` | 溯源（`raw` 存交易所原始 JSON） |
| `idempotency_key` | **NOT NULL UNIQUE**，见 §5 |
| `created_at` | 入库时刻 |

索引：`(ts)`、`(symbol, ts)`、`(position_id)`。

### `equity_curve` —— 权益曲线

`ts`（PRIMARY KEY，秒）、`equity`、`twr_index`（资金加权收益指数，基准 `1.0`）、
`cum_flow`（累计净入金，TWR 剔出/入金用）、`source`（`exchange` / `store` /
`injected`）、`created_at`。

`ts` 做主键 ⇒ 同一秒重复采样是 **upsert 覆盖**，不会长出重复点（面板/对账重复写是常态）。
`twr_index` 的 upsert 用 `COALESCE(excluded, old)`：后写的 `NULL` 不会把已有指数冲掉。

### `daily_counters` —— 每日计数器

`day`（`'YYYY-MM-DD'` UTC，PK）、`trades_today`、`day_start_equity`、`realized_pnl`
（v2 补列）、`updated_at`。

`day_start_equity` **只写一次**：同一天第二次 `ensure_day()` 传入新值不会覆盖。理由：
日初权益是「当日亏损 5%」的分母，跟着权益浮动就永远触发不了。

### `dd_anchors` —— 滚动回撤锚点（**可清理**）

`ts`（PK）、`equity`、`cum_flow`、`twr_index`、`twr_peak`、`created_at`。
只保留 30 天（`DD_ANCHOR_KEEP_DAYS`），供面板画回撤历史。

### `dd_state` —— 回撤活状态（**持久、单行**）

`name`（PK，固定 `'live'`）、`anchor_ts`、`anchor_equity`、`anchor_cum_flow`、
`twr_index`、`twr_peak`、`reset_count`、`last_reset_at`、`reset_reason`、`updated_at`。

### `events` —— 执行器事件流

`id`、`ts`、`kind`（`open`/`closing`/`exit`/`stop`/`dd_reset`/…）、`symbol`、`message`、
`data`（JSON）、`created_at`。索引 `(ts)`。T10 的「最近 20 条」和 T10b 的写操作审计都用它。

---

## 3. 仓位状态机（对齐 PLAN T6）

```
IDLE ──▶ OPEN ──▶ CLOSING ──▶ CLOSED
          │          │
          └──────────┴──────▶ CLOSED      （直接关账：成交已确认）
                     ◀────── OPEN         （离场请求被拒 → 退回持仓）
CLOSED 是终态：同一标的下一笔交易新开一行，绝不把老行改回 OPEN。
```

* `open_positions()` / `open_symbols()` 把 **`OPEN` 和 `CLOSING` 都算有仓**。
  `CLOSING` 的语义是「离场单已发出、交易所还没确认」——仓位还在交易所上。少算一个
  `CLOSING` 的后果是宽限期内给同一标的开出第二仓。
* 每标的至多一仓：`record_entry()` 在**同一事务里**先查 `OPEN/CLOSING` 再插入，
  第二个并发写者不会挤过去（`BEGIN IMMEDIATE`）。
* 非法迁移一律 `InvalidTransition`，不静默改状态。

---

## 4. 事务与原子性

所有写走 `StateStore.write_transaction()`：

```python
with self.write_transaction() as conn:   # BEGIN IMMEDIATE
    ...
# 正常 → COMMIT；任何 BaseException（含 KeyboardInterrupt）→ ROLLBACK
```

* **IMMEDIATE 而不是 DEFERRED**：复合写全是「先查再写」。DEFERRED 事务先拿读锁、
  再升级写锁，两个写者同时升级必有一方 `SQLITE_BUSY`（`busy_timeout` 也救不了）。
* **可嵌套**：内层不加新事务，由最外层统一提交/回滚。所以「开仓」这种复合写是一个
  **不可分割**的单位：

  | 操作 | 一个事务里做的事 |
  |---|---|
  | `record_entry` | `positions` 一行（`OPEN`）+ `trades` 一行（`entry`）+ `daily_counters` 计数 +1 + `events` 一条 |
  | `record_exit` / `record_stop` | `positions` → `CLOSED`（价格/时间/盈亏）+ `trades` 一行（`exit`/`stop`）+ `daily_counters.realized_pnl` 累加 + `events` 一条 |
  | `begin_close` | `positions` → `CLOSING` + `events` 一条 |
  | `reset_dd_peak` | `dd_state` 更新（含 `reset_count+1`）+ `events` 一条 |

* **要防的半状态**：`OPEN` 仓位存在但**没有开仓成交**。那会让对账无法证明这笔仓从哪来
  （旧系统 PR #44/#45 踩过）。`record_entry` 把仓位与成交写在同一个事务里，成交插入失败
  → 仓位一起回滚；`Store.audit()` 还有一条 `open_without_entry_trade` 检查兜住「有人绕过
  store 直接写库」的情况。

---

## 5. 幂等键

### 成交（`trades.idempotency_key`，表上 `NOT NULL UNIQUE`）

由 `store.trade_key()` 唯一计算（**唯一事实来源**，测试直接断言它的字符串）：

| 优先级 | 证据 | 键 |
|---|---|---|
| 1 | 交易所成交 id | `fill:<fill_id>` |
| 2 | 只有订单号 | `order:<order_id>:<side>:<price>:<qty>`（优先整数 tick，缺则浮点 12 位） |
| 3 | 都没有（本地/补录） | `local:<SYMBOL>:<kind>:<ts_ms>:<side>:<price>:<qty>` |

为什么第 2 层把价和量放进键里：**同一订单号可能有多笔部分成交**（不同价/不同量），那些是
不同的成交，必须各占一行；而「同订单、同价、同量」重复同步是同一条记录，必须去重。
单纯用 `order_id` 做唯一键会把部分成交合并成一笔（数量对不上，对账直接崩）。

写入用 `INSERT OR IGNORE` + 冲突后**回查真实 id**（忽略时 `lastrowid` 会残留上一次插入的
id，可能是别的表 —— 旧系统为此专门写过注释）。

### 仓位

`UNIQUE(symbol, bar_ms)`：同一根 bar 的同一标的只开一次。重复 `record_entry()` 返回原
`position_id`、不动任何数据（`WriteResult.idempotent == True`）。
`record_exit()` 对已 `CLOSED` 的仓位直接返回（不改 `closed_at`、不重复记成交、不重复累加
当日盈亏）。

---

## 6. 回撤锚点的生命周期 —— 锚点过期 ≠ 回撤归零

### 两张表的分工

| 表 | 保留期 | 谁写 | 能不能改小峰值 |
|---|---|---|---|
| `dd_anchors` | **30 天**（滚动清理） | T9 `dd.py`（每轮 append） | 不适用（只是检查点） |
| `dd_state` | **永久**（单行） | T9 `dd.py`；人工 reset | `save_dd_state()` **拒绝**调低；只有 `reset_dd_peak()` 能 |

`prune_dd_anchors(keep_days=30)` 有两条保护：

1. **最新一行永不删**（即使它比窗口还老）。空表是「回撤无从重建」的最坏输入。
2. **绝不碰 `dd_state`**。峰值的事实来源在那里。

### 装配顺序（`StateAssembler.dd_reading()`）

| 顺序 | 证据 | `source` | 可用 | 降级标记 |
|---|---|---|---|---|
| 1 | `dd_state` 行 | `dd_state` | ✅ | 只有 `peak<index`（夹住）或超过 `dd_max_age_s` 才 `degraded` |
| 2 | `dd_anchors` 最新一行 | `dd_anchor` | ✅ | 恒 `degraded`（峰值可能曾更高 → 回撤只是**下界**） |
| 3 | 都没有 | `unavailable` | ❌ | `index=peak=None` |

**第 3 档不是「0% 回撤」**，而是「状态未知」：`RiskState.dd_index = dd_peak_index = None`
→ `app/risk.py::_gate_drawdown` 用 `GATE_DD` **拒开新仓**（fail-closed）。
这正是要防的事：把缺失的峰值当成 0，等于**静默关掉熔断**，而且没有任何告警 —— 旧系统
`hl_snapshots` 30 天窗口就踩过这个坑（第 31 天回撤归零，熔断再也触发不了）。

### 明确禁止的做法

**不要**「从 `equity_curve` 最近 30 天重算一个峰值」充数。窗口一滚，峰值就掉到窗口内的
最高点，回撤被系统性低估。代码里没有任何自动路径做这件事；`tests/test_state.py::
test_equity_curve_alone_never_bootstraps_a_peak` 专门钉住它。

真正合法的两条重建路径：

1. **全新账户的一次性 bootstrap**（T9 `dd.py`）：空库、无持仓、无成交时，以一个明确的
   锚点（时刻 + 权益）建立 `dd_state` 的 `index = peak = 1.0`。
2. **人工重置**：`POST /admin/dd/reset`（T10b，带 admin token、二次确认）
   → `store.reset_dd_peak(reason=...)`，`reset_count+1` + `events` 审计。
   熔断「永不自解」（PLAN T9），所以自动路径永远不调用它。

### 其它保护

* `save_dd_state()` 在 `new_peak < stored_peak` 时 **抛 `StoreError`**，不做「悄悄调低」。
* `twr_peak < twr_index`（状态被外力改坏）→ 夹到 `index`、`degraded=True`，
  同时 `audit()` 报 `dd_peak_below_index`（负数回撤被下游 `max(0, …)` 静默抹平是最难查的）。
* `dd_state.updated_at` 超过 `dd_max_age_s`（默认 24h）→ `degraded=True` + 原因文案，
  数值仍可用（陈旧证据好过完全不能交易），watchdog 应当对此报警。

---

## 7. 并发：WAL + busy_timeout

| PRAGMA | 值 | 为什么 |
|---|---|---|
| `journal_mode` | `WAL` | 读不阻塞写：面板进程随时能读，机器人照常写 |
| `busy_timeout` | `5000` ms | 面板/运维脚本在写时等一会儿，而不是立刻 `database is locked` |
| `synchronous` | `NORMAL` | WAL 下的官方推荐：进程崩溃不丢已提交事务、不损坏库 |
| `foreign_keys` | `ON` | 为后续真正的 FK 留口（当前表间引用是逻辑引用） |

进程内：单连接 + `RLock`（旧系统实测无锁并发会静默丢数据）。写事务用
`BEGIN IMMEDIATE`，见 §4。`:memory:` 库的 `journal_mode` 只能是 `memory`，属正常。

`checkpoint()` 把 WAL 合并回主库（备份前调一次，见 `Store.checkpoint()`）；不调也没关系，
SQLite 会在最后一个连接关闭时自动 checkpoint。

---

## 8. 面板只读接口（T10）

`Store.panel_snapshot()` 一次给齐 DB 侧数据：

| 面板区块 | 取数 |
|---|---|
| ① 状态条 | `open_count` / `last_equity` / `trades_today` / `dd_state` / `audit()` |
| ② 仓位表 | `positions`（`open_positions()`：`OPEN`+`CLOSING`） |
| ③ 止损单覆盖 | `positions[].stop_price` / `stop_order_id`（挂单在不在由 T8 对账填） |
| ④ 盈亏曲线 | `equity_curve`（`equity` 实线 + `twr_index` 虚线，同一份 TWR 函数由 T9 提供） |
| ⑤ 三标的动态 | 不在本层（T6/T9 的 `symbol_state` 类缓存，后续版本再加表） |
| ⑥ 事件流 | `events`（含「跳过」类事件） |

其它查询：`trades(limit, symbol, since, kind, position_id)`、`open_notional_by_symbol()`、
`daily_counters(limit)`、`trades_today()`、`realized_pnl(since=)`、`dd_state()`、
`latest_dd_anchor()`、`audit()`、`integrity_check()`。

**面板永远不通过本层做写操作**（除了 T10b 的三个 admin 端点会调 `reset_dd_peak()` /
`log_event()`），也永远不 import `exchange.py`。

---

## 9. 迁移策略

* `PRAGMA user_version` 单调递增；代码常量 `SCHEMA_VERSION = 2`。
* `MIGRATIONS = {1: (...), 2: (...)}`，**每个版本一组语句**。
  新库（`user_version=0`）依次跑 1..N；老库从自己的版本往上跑 —— **同一组语句**，
  所以「新建的库」和「升级后的库」结构必然一致，不存在 schema 分叉。
* **每个版本的 DDL 与版本号在同一个事务里提交**：迁移中途语法错/断电 → 整体回滚，
  不会留下「表加了一半、版本号没动」的库（下次会重复 ALTER 而失败）。
  实现见 `StateStore.migrate()`；测试 `test_failed_migration_rolls_back_completely`。
* `user_version > SCHEMA_VERSION`（代码比库旧）→ `SchemaError`，**拒绝启动**。
  猜着往旧代码上跑，比拒绝启动危险得多。
* 已发布的迁移语句**永不修改**，只能追加新版本。

已实现的版本：

| 版本 | 内容 |
|---|---|
| 1 | 7 张表 + 基础索引 |
| 2 | `positions.exit_price/exit_price_int/exit_order_id/client_order_index`、`trades.client_order_index`、`daily_counters.realized_pnl`、两个索引 |

---

## 10. `app/state.py` 的装配规则

| 字段 | 来源（按优先级） | 缺失时 |
|---|---|---|
| `equity` | 注入 `equity_provider` → `equity_curve` 最后一点 → （`allow_stale_equity=True` 时才允许 provider 失败后退回库值，标 `store_stale`） | `None` → `GATE_STATE` 拒单 |
| `day_start_equity` | `daily_counters.day_start_equity` → 日界前最后一个权益点（`curve_prev`）→ 当天第一个权益点（`curve_first`） | `None` → `GATE_DAILY_LOSS` 拒单 |
| `trades_today` | `max(计数行, trades 表开仓腿行数)` —— 取更严的一侧 | `None` → `GATE_DAILY_TRADES` 拒单 |
| `open_positions` | store 的 `OPEN/CLOSING` **∪** 注入的交易所持仓（并集 = 更严） | 读不出来 → `()` + 警告（store 是唯一写者，正常不会失败） |
| `open_notional_by_symbol` / `_total` | `qty × price`，`price` 来自注入的 `price_provider`，缺则用**库里的成交价** | 任何一仓算不出 → `by_symbol={}` + `total=None` → `GATE_TOTAL_NOTIONAL` 拒单 |
| `dd_index` / `dd_peak_index` | 见 §6 | `None` → `GATE_DD` 拒单 |

注入点的降级行为：

* `equity_provider` 抛错 / 返回 `None` / `NaN` / 负数 → `equity=None`（**不**用旧值充数）。
* `price_provider` 抛错 / 无值 → 退回库里的 `entry_price`（真实成交价，不是编的）+ warning。
* `positions_provider` 抛错 → 退回 store 的持仓 + warning。**不能**退回空集：漏报持仓的
  后果是「同标的开第二仓」，比多报严重得多。
* 每个字段都带 `*_source`，`AccountSnapshot.warnings` 汇总所有降级；`describe()` 给一行摘要。

---

## 11. 测试覆盖（全离线：临时目录 + `:memory:`）

`tests/test_store.py`（54 项）

* 建库 / 7 张表 + 索引 / `user_version=2` / `migrate()` 幂等；
* 老 v1 库就地升级（数据保留、新列 NULL、升级后能走 v2 代码路径）；
* 迁移失败整体回滚（版本号仍 0、半张表都不留）；库比代码新 → `SchemaError`；
* 往返读写：开仓（含 `notional`/`risk_amount` 手算值）、平仓（含 `net_pnl`）、空单符号、
  止损腿、权益曲线顺序/覆盖、成交过滤、跨连接重开；
* 幂等：`trade_key` 四种形态的**字面值**、同 order id ×3 = 1 行、同 fill id ×3 = 1 行、
  部分成交**不合并**、同 bar 重复开仓、重复平仓不重复记账；
* 原子性：成交写入抛错 → 仓位/事件/计数全无；平仓中途失败 → 仓位仍 `OPEN`；
  第二连接占写锁 → `OperationalError`（不是半状态）；嵌套事务整体回滚；
  `audit()` 抓出手写半状态；
* WAL：`journal_mode=wal`、`busy_timeout=5000`、只读连接并发读、`checkpoint()`；
* 崩溃恢复：写 → 不 checkpoint 直接关连接 → 重开状态正确；
* 回撤锚点：`save_dd_state` 拒绝调低峰值、`reset_dd_peak` 是唯一降峰值路径且留审计、
  30 天清理只删 `dd_anchors`、最新一行永不删、`dd_state` 完全不受影响；
* 非法输入 fail-loud、`from_config` 只认 `sqlite:///`、`app/store.py` 不含网络模块字样。

`tests/test_state.py`（39 项）

* 装配出的每个字段的**手算期望值**（3200 / 5000 / 8200 名义、30% 回撤、2 笔当日成交）；
* `open_positions` 是 **`tuple`**（不是计数），并能直接驱动 T4 的 7 个闸门
  （30% 回撤 → `drawdown` 熔断，只有这一个码）；
* 回撤锚点：30 天清理后回撤**仍是 30%**、锚点全删但 `dd_state` 在 → 仍 30%、
  `dd_state` 没了 → `dd_anchor` 降级、两者都没了 → `None` + `GATE_DD` 拒单、
  只有权益曲线 → 仍然不可用（不 bootstrap）、峰值 < 指数 → 夹住 + `audit` 报警、
  陈旧状态 → `degraded` 但仍可用；
* 权益注入：provider 优先 / 抛错 / NaN / 负数 / `allow_stale_equity`；
* 日初权益三条来源 + 全缺失 → `GATE_DAILY_LOSS`；
* 名义：标记价覆盖、坏标记价退回成交价、`qty=0` → 总名义 `None` + `GATE_TOTAL_NOTIONAL`；
* 持仓注入：并集 + 双向 warning、provider 失败退回 store；
* `snapshot()` 只读（调用前后行数不变）、`ensure_day_start()` 只写一次。

---

## 12. 后续任务怎么接

* **T8 executor**：`StateAssembler(store, equity_provider=<lighter account>, price_provider=<mark>,
  positions_provider=<positions>)` → `risk_state()` → `check_entry()`；写只走
  `store.record_entry/begin_close/record_exit/record_stop/record_trade`。
  对账顺序仍是「先 `_sync_fills`（`record_trade` 天然幂等）→ 再判 mismatch」。
* **T9 dd.py**：读 `store.dd_state()` / `latest_dd_anchor()` 取锚点，算完 TWR 后
  `save_dd_state()` + `append_dd_anchor()` + 定期 `prune_dd_anchors()`；
  **必须 import `app.risk.dd_thresholds()`**，不要另写阈值。
* **T10 面板**：只调 `panel_snapshot()` / `audit()`，不 import `exchange.py`。
* **T10b admin**：`reset_dd_peak(reason=...)` / `log_event()`，每次都落 `events`。
