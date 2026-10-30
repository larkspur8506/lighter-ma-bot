# 执行器（T8）：一轮 tick 的编排、对账与停机

`app/executor.py` 是把 T2–T7 串起来的那一层：**它同时看得见本地 DB 和交易所**，所以
对账只能在这里做。本文写清楚三件事：一轮 tick 到底按什么顺序做了什么、对账的不变量
清单、以及不一致/异常时**为什么**只报警不自动修正。

相关：`docs/PLAN.md`（T8）、`docs/EXCHANGE.md`（T7 下单层）、`docs/LIFECYCLE.md`（T6
状态机）、`docs/STATE_STORE.md`（T5 落库）。

---

## 1. 一轮 tick 的流程

```
run_once(now_ms)
│
├─ 0. 熔断？ ── 是 → 本轮只做「不涉及开仓」的事，并把所有 entry 挡掉（不跳过整轮！）
│
├─ 1. reader.read() → ExchangeAccount          ← 交易所真相（账户/持仓/挂单，一次读完）
│     └─ 抛错（网络/结构不认识/没授权）→ 记 executor_error → 整轮停手（不猜）
│
├─ 2. reconcile(local_rows, account) → ReconcileReport   ← 纯分析，不写库
│     ├─ 有 issue        → 落 events + 报警（永远）
│     └─ 有 blocker      → 本轮 blocks += reconcile_blocked
│
├─ 3. store.record_equity(account.equity)      ← 权益点（DD / 日初权益的输入）
│
├─ 4. 每个标的：market.bars(symbol)            ← 已收盘 bar（未收盘由 market 剔除）+ 新鲜度校验
│     ├─ bars < MIN_BARS_REQUIRED → 该标的 skip('warmup')（不是错误）
│     ├─ compute_indicators(bars)
│     └─ pending_signal(最后一根已收盘 bar, entry_ref = 未收盘 bar 的 open) → Signal|None
│
├─ 5. StateAssembler.snapshot()                ← 库里持仓 ∪ 交易所持仓（并集 = 更严）
│     ├─ ensure_day_start()                    ← 当日日初权益（只写一次）
│     └─ → RiskState（equity / day_start / trades_today / notional / DD index）
│
├─ 6. 每个标的：lifecycle.decide(...) → Decision
│     ├─ 有活仓 → decide_exit（**不看风控**：熔断/DD 都不能拦离场）
│     └─ 无活仓 → decide_entry（先 sizing 后 risk）
│
├─ 7. 执行器闸门：action == 'entry' 且 blocks 非空 → 降级成 skip(code=blocks[0])
│
├─ 7b. 信号时效闸：action == 'entry' 且 bar 收盘已超 N 分钟
│      → 降级成 skip(code='stale_signal') + 落 entry_skipped_stale_signal（**不算失败**；
│        离场/止损不经过这里，见 §3.3）
│
├─ 8. 执行
│     ├─ entry：place_entry → place_stop → lifecycle.apply_entry（记仓位 + 开仓腿）
│     ├─ exit ：begin_close(OPEN→CLOSING) → place_exit → complete_close(→CLOSED)
│     │          └─ 被拒 → abort_close（CLOSING→OPEN，显式回滚，绝不静默当平掉）
│     └─ stop ：有「交易所已平 + 止损单已不在」证据 → 直接 complete_close(record_stop)
│                无证据 → 同上走 place_exit（reduce_only）兜底
│
└─ 9. 失败计数 / 熔断；返回 TickReport（status ∈ ok | blocked | error）
      └─ 任何未预期异常：落 events → **本轮立即停止**（后面的标的一个都不再处理）
```

顺序不是随便排的：

* **先读交易所，再动手**。任何「本地 vs 交易所」的结论都必须在下单之前得出。
  旧系统 PR #44/#45 的教训就是先判 mismatch 再同步 fills，把「止损刚成交」当成
  「本地有仓、交易所无仓」的灾难。
* **先记权益，再决策**。`RiskState.day_start_equity` / DD 都从权益曲线来；权益缺一轮，
  当日亏损闸就会拿到 `None` → fail-closed 拒掉所有开仓。
* **先取完全部数据，再逐个下单**。取数失败时还一张单都没发，停手是干净的。

### 1.1 为什么用 `evaluate_entry()` 而不是 `scan()`

`strategy.scan()` 的循环上界是 `len(bars) - 1`（它要用 `bars[i+1].open` 当入场价，
回放语义），所以它**结构上不会产出最后一根 bar 的信号** —— 而实盘 tick 要判的恰恰是
最后一根**已收盘** bar。`pending_signal()` 调的是 `scan()` 内部**同一个**
`evaluate_entry()`，参数逐一对齐（同一个 `squeeze_flags(ind["band"])`），不复制任何
常数。测试 `test_pending_signal_matches_scan_on_the_same_bar` 逐字段钉住两者在重叠区间
上完全一致。

入场参考价取「正在形成的那根 bar 的开盘价」（`market.bars()` 剔掉的那根），这与回测
「信号 bar 收盘 → 次根开盘入场」的口径一致；拿不到就退回最后一根收盘价。

### 1.2 同一根 bar 不重复下单（幂等）

两道锁，缺一不可：

1. **存储层**：`positions UNIQUE(symbol, bar_ms)`。同一根 bar 的第二笔开仓在
   `decide_entry()` 就被 `same_bar_already_traded` 挡掉（`lifecycle.bar_taken()`）。
2. **交易所层**：`clientOrderIndex = sha256("ltab:{sym}:{bar_ms}:{purpose}")` 的高 48 bit，
   `purpose ∈ {entry, stop, exit}`。这是给「本地记完账就崩了、下一轮又跑一遍」这种
   半状态兜底的 —— 交易所按 `clientOrderIndex` 去重，第二张单不会成交。

仓位行里 `entry_order_id` / `stop_order_id` 存的是 **`coi:<clientOrderIndex>`**，
不是交易所回传的 order id：coi 是我们自己算的确定性幂等键，因此**不需要**等链上订单号
就能在挂单列表里认出"这张止损单是我们的"。

---

## 2. 对账的不变量清单

`reconcile()` 是**纯函数**：`(本地活仓行, ExchangeAccount) → ReconcileReport`。
它没有 store 句柄，**结构上不可能改本地状态**（`test_reconcile_never_writes_to_the_store`
在调用前后逐字段比对 `positions` 表、并比对 `events` / `trades` 行数）。

| code | 它守的不变量 | 触发条件 |
|---|---|---|
| `position_missing_on_exchange` | 本地每个活仓在交易所都必须有真实持仓 | 本地 `OPEN`/`CLOSING`，交易所该标的无仓 |
| `position_unknown_locally` | 交易所每个持仓本地都必须有账 | 交易所有非零仓，本地无活仓行 |
| `position_qty_mismatch` | 本地 `qty_int` 与交易所 `position` 必须相等（容差半个 tick） | `|local − exchange| > tick/2` |
| `position_side_mismatch` | 方向必须一致 | `side` 符号不同 |
| `stop_order_missing` | 有仓就必须有 resting 的 `reduce_only` 止损单 | 本地 `stop_price_int` 非空，但挂单里找不到匹配的止损单 |
| `orders_unavailable` | 止损单是否还在必须**可证** | `GET /accountActiveOrders` 读失败（没 auth / 网络 / 结构不认识） |

两条补充口径：

* **数量比对用整数 tick 语义**：`abs(exchange − local) <= tick/2`，只吸收字符串→Decimal
  的表示误差；差 1 个 tick 就是不一致（`test_half_tick_tolerance`）。
* **止损单身份**：先按 `client_order_index` 精确匹配；没有 coi 的老仓位退回
  「`reduce_only` + 同市场 + 触发价 ±1 tick」兜底；`reduce_only=False` 的单**永远不算**
  止损单（`test_non_reduce_only_order_is_not_a_stop`）。

---

## 3. 每种不一致的处理策略

| 情况 | severity | 阻断新开仓 | 本地状态 | 处置 |
|---|---|---|---|---|
| 本地有仓 + 交易所无仓 + **止损单仍挂着** | `danger` | 是 | **不改** | 报警；人工判断是强平/手动平仓/漏记 |
| 本地有仓 + 交易所无仓 + **止损单也没了**（我们确实挂过） | `grace` | 否 | **当场结案**：成交回报里有真实平仓成交 → `stop_fill_settled`；一笔成交都没有 → 连续 `LIGHTER_GRACE_TICKS` 轮后 `position_voided`（`void_position`） | 有成交按成交结案，无成交作废幽灵仓 |
| 交易所有仓 + 本地无账 | `danger` | 是 | **不补记** | 报警；人工决定 adopt（补账）或 flatten（平掉） |
| 数量不一致 | `danger` | 是 | **不改** | 报警；不要"按交易所改本地"或反之 |
| 方向不一致 | `danger` | 是 | **不改** | 报警；方向反了说明有一侧彻底错了 |
| 止损单丢失 | `danger` | 是 | **不补挂** | 报警；由人决定补挂还是平仓 |
| 挂单列表读不到 | `danger` | 是 | — | 报警；先把 auth / 网络修好 |

阻断的实现方式是**把开仓意图降级成显式 skip**（`Decision.action='skip'` +
`rejections=[Rejection(stage='executor', code='reconcile_blocked')]`），不是静默丢弃：
面板和日志都能看到"这里本来要开仓，被执行器挡了"。

**触发阻断的范围是一轮**：下一轮重新读、重新判。不一致持续存在 → 阻断持续存在
（自然粘滞），不需要额外的"锁"状态。

### 3.1 `grace` 为什么不是"自动修正"

`grace` 只在一个非常窄的条件下成立，且**每一环都是交易所给的正面证据**：

1. 本地这行是活仓，且 `stop_order_id` 非空（**我们确实挂过**止损单）；
2. 交易所该标的的持仓为 0；
3. 挂单列表**读到了**（`orders_error` 为空），且里面**没有**这张止损单；
4. 本轮的决策确实是止损（价格穿止损价）。

四条同时成立，唯一自洽的解释就是"止损单成交了"。这时用 `record_stop()` 把这笔记成
止损平仓（成交价 = 止损价），并落 `stop_fill_settled` 事件（带 `evidence=flat_and_stop_gone`）。
这不是"猜一个状态写回去"，而是**承认一个已经发生的成交**；相反，如果不结案，本地会
永远卡在 `OPEN`、每轮报一次假的 mismatch —— 这正是旧系统把正常止损误报成事故的原因。

反过来，止损单**还挂着**、仓却没了 → 这不是止损成交（止损单没触发就已经没仓了），
属于"强平/手动平仓/漏记"，一律 `danger` + 阻断 + 报警。

### 3.1.1 `grace` 必须被结案，不能是终点（2026-10-30 线上事故）

上面的四条只是**判据**，不是**出路**。原先只有"该标的下一次决策是止损"时才结案
（`_execute_exit` → `_stop_fill_evidence`）；如果信号一直说 `hold`（价格根本没到止损价），
`grace` 就每轮重复报、**永远不结案**。2026-10-30 的幽灵仓就是这么活了 13 轮：

* 交易所：无仓、无挂单、无该标的成交；权益 6.5 小时一分未动；
* 本地：`positions` 一行 `OPEN`（1.287 SOL 空）+ 一行 `trades`（计划价、`fill_id` 为空）；
* 面板：`/stops` 每轮报"止损未覆盖 SOL"，cron 看守每半小时告警一次。

⇒ 现在 `grace` 在**当轮**就结案（`Executor._settle_grace_issues()`，跑在信号之前）：

1. 读 `GET /api/v1/trades`（限 20 笔、只有 desc、**无 cursor**）；
2. 找到「方向相反、时间在开仓之后、数量够 99%」的成交 → `complete_close(kind=stop|exit,
   fill_id=…, realized_pnl=逐笔 pnl 合计)` + `stop_fill_settled`（成交价用**加权均价**，
   不再假装是止损价）；
3. 一笔都没有、**且成交窗口覆盖得住仓位生命周期**（窗口里最老的一笔 ≤ 开仓时刻）→ 连续
   `LIGHTER_GRACE_TICKS`（默认 2 轮 ≈ 1 小时）后 `void_position()`：删掉该仓位 `fill_id` 为空
   的成交行、回退当日 `daily_counters`、仓位置 `CLOSED`/`reason=no_fill`/`qty=0`、
   落 `position_voided` + 报警；
4. 成交读不到 / 窗口盖不住 → **什么都不做**（"查不到"不等于"没有"），只记
   `trades_unavailable` 或 warning，下一轮再判。

红线：**有 `fill_id`（交易所成交回报确认过）的成交行，`void_position` 一律拒绝作废**。

### 3.1.2 最差价的锚：实时 mid，不是 4h 收盘（2026-10-30）

订单是 `MARKET + IOC + 最差价`，而「最差价」原来的锚是**上一根 4h 处盘的收盘价**——
tick 在收盘后 3~4 分钟才发单，这段漂移（实测 p90=18bp、max=58bp）会把 30bp 的带宽吃光，
剩下的容忍度是负的 ⇒ IOC 零成交。同一根信号的对照（2026-10-29 20:03）：

| | 参考价 | 上限 | 当时市价 | 结果 |
|---|---|---|---|---|
| HL（crypto-ma-alert） | **实时 mid** = `all_mids()` | mid × 0.997 | 实时 | **成交 99.446**（比收盘价差 39bp） |
| Lighter（旧） | 4h 收盘 99.835 | 99.535 | 99.1 | 零成交 |

⇒ 现在 `Executor._live_reference_int()` 在**发单那一刻**取 `market.snapshot(symbol).mid`
（公开盘口，无鉴权），把整数 tick 作为 `limit_price_int` 传给 `build_entry_order` /
`build_exit_order`，滑点仍然按 `worst_price_int` 施加。要点：

* **入场**有偏离闸：mid 与信号价相差 >3%（`DEFAULT_LIVE_REF_MAX_DEVIATION`）视为脏数据，
  退回信号价并落 `live_quote_rejected`；取不到报价 → `live_quote_failed` + 退回信号价，
  **不阻断开仓**（锚的选择不是交易前提）。
* **离场**（信号平仓/止损离场）**不设偏离闸** —— 减仓单必须追着现在的价格走，宁可滑点更大
  也不能因为「离收盘价太远」而放弃退出。
* 定仓/止损距离仍按 `plan.entry_price`（信号价）算，口径不变；变的只是「订单能接受多差的价格」。

### 3.2 受理 ≠ 成交（`LIGHTER_FILL_CONFIRM_S`）

Lighter 的 `POST /sendTx` 返回 `code=200` 只表示**进了 sequencer**；`/tx?by=hash` 的
`status=2` 只表示**事务执行成功**。两者都不意味着**有成交**：我们的开仓/离场都是
`MARKET + IOC + 最差价`（`worst_price_int(参考价, 30bp)`），参考价取的是**上一根已收盘
bar 的收盘价**，而 tick 在 bar 收盘后 ~3 分钟才跑 —— 价格若已朝有利方向跑掉超过 30bp，
卖单的最差价就落到盘口之上，IOC 直接零成交。

2026-10-30 20:03 的实例：SOL 4h 收盘 99.835（20:00），20:03:51 下单时市价已 99.1，
最差价 99.535 在盘口之上 ⇒ 零成交；本地却把"受理"当"成交"记了仓。

⇒ 现在两条路径都必须**看到仓位变化**才记账（`LIGHTER_FILL_CONFIRM_S`，默认 12 秒轮询）：

* 开仓：等账户里真的出现该标的的仓位；**零成交** → 不记仓位、不记成交、不挂止损，
  落 `entry_not_filled` + 报警；**部分成交** → 用交易所的数量重建 plan（止损数量跟着走）
  并落 `entry_partial_fill`；
* 离场/止损：等该标的的仓**真的不见**；仍持仓 → `abort_close` 回退 `OPEN`，
  落 `exit_not_filled` + 报警（这一侧更危险：记成 CLOSED 就是裸仓）。

`0` = 只读一次（离线测试用）。这条是**观测与记账**的护栏，不改任何策略/定仓口径。

### 3.3 信号时效闸（`LIGHTER_MAX_SIGNAL_AGE_MIN`，默认 30 分钟）

信号是拿「**最后一根已收盘 4h bar**」判的，而这根 bar 收盘后 4 小时内每一轮 tick 都会
重判**同一根** bar。正常情况第一轮（收盘后 ~3 分钟，tick 在 :03/:33）就建仓了；但那一轮
若被挡（对账阻断 / 熔断 / 取数失败 / 仓位槽被幽灵仓占着），等仓位槽空出来时，机器人会拿
一根已经过期几小时的 bar 建仓 —— 这不是假设：**2026-10-30 实盘，20:00 那根 bar 的信号
在 03:03 才执行（晚了 3 小时）**，虽然那次价格恰好没跑，但这是规则外的偏差。

⇒ 只允许在「信号 bar **收盘时刻**（`bar_ms + 4h`）之后 N 分钟内」建仓（`_run_tick` 里
顺序是：决策 → **时效闸** → 执行）。`N` = `LIGHTER_MAX_SIGNAL_AGE_MIN`，默认 **30**，
`0` = 关闭。

为什么是 30 分钟：实测（Lighter SOL 1m K 线，14 天 84 个 4h 边界，相对 bar 收盘价的漂移）

| 距收盘 | p50 | p90 | 漂移 > 30bp 的比例 |
|---|---|---|---|
| +3 分钟（第一轮） | 8.3bp | 19.3bp | 5% |
| +33 分钟（第二轮） | 20.8bp | 63.5bp | **36%** |
| +63 分钟（第三轮） | 24.8bp | 73.0bp | 45% |

第二轮的中位漂移是第一轮的 2.5 倍，超过三分之一的信号会整个吃掉 30bp 的滑点带宽（见
3.1.2 与 `LIGHTER_ENTRY_SLIPPAGE_BPS`）。30 分钟正好等于「收盘后第一轮」的边界。

**语义边界**（写死，别顺手扩大）：

* 只拦 `action='entry'`：`exit` / `stop` 永远放行 —— 闸门再严也不能锁死亏损仓；
* **不算失败**：当轮不建仓而已，不抛异常、不计连续失败、不开熔断，`TickReport.status`
  仍是 `ok`，`blocked_entries` 里也没有它；
* 降级方式与 `_apply_entry_guard` 一致：`entry` → 显式 `skip`，`from_state` 不变，
  `rejections` 里追加一条 `stage='executor' / code='stale_signal'`（`detail` 带
  `bar_ms` / `age_min` / `limit_min`），并落事件 `entry_skipped_stale_signal`；
* 边界是**闭区间**：年龄 == 30 分钟放行，> 30 分钟才拦；
* `bar_ms` 缺失（理论上不会：决策一定带 bar）时**不拦** —— 拦截需要证据，不猜。

回滚：`.env` 里设 `LIGHTER_MAX_SIGNAL_AGE_MIN=0` 即退回「每轮都可以建仓」的旧行为，
不需要改代码。

---

## 4. 停机与熔断

### 4.1 单轮停手（不带不确定状态继续）

以下任一情况 → 落 `events` → **本轮立即停止**，后面的标的一个都不处理：

* `reader.read()` 失败（账户/持仓读不到）；
* 任何一个标的的 `market.bars()` 失败（含 `StaleFeedError` 新鲜度超限）；
* 决策抛异常（`lifecycle` 的状态机/契约错误）；
* 下单/记账过程中的任何未预期异常（`place_entry` 抛错、`apply_entry` 冲突、DB 故障…）。

留痕是三重的：`events` 表（`executor_error` / `tick_aborted`）+ `TickReport.status='error'`
且 `error` 非空 + 连续失败计数。**没有 `except: pass`**（
`test_no_silent_exception_swallowing_in_source` 用 AST 扫每一个 handler：裸 except、
空 handler、只 `continue`/只赋值的 handler 都算不通过）。

唯一一处"兜底的兜底"是"连 `events` 都写不进去"（DB 坏了）：那里仍然 `logger.exception`
并把嵌套错误写进 `report.error`，绝不静默。

### 4.2 熔断（连续失败）

* **计数**：一次硬失败 = 一笔下单未被接受（`ok=False` / 未确认 / 被拒）或一次异常。
  语义是**连续**：本轮结束时如果失败数与轮初相同（本轮没有新失败）→ 清零。
* **阈值**：`max_consecutive_failures`，默认 3。
* **熔断后**：所有 `entry` 被降级成 `skip(code='circuit_open')`；**离场/止损照常执行**，
  权益照常记录，对账照常做。原因见 `docs/PLAN.md`：熔断不能把亏损仓锁死。
* **复位**：只有显式 `Executor.reset_circuit(reason=...)`（T10 面板的管理操作）。
  **不会**自动复位 —— 自动复位等于熔断没用。
* 熔断状态写 `events`：`circuit_open` / `circuit_reset`。

### 4.3 dry-run 的取舍

`dry_run` 只看 `exchange.dry_run`。dry-run 下**不发单**，本地账变成了"纸上持仓"，
交易所永远是空的 —— 所以对账的不一致在 dry-run 里**照常报警但不阻断**
（否则模拟盘第一轮开仓后每轮都被自己挡死）。需要严格模式时传
`enforce_in_dry_run=True`。

---

## 5. 「不自动修正、只报警并阻断」的理由

**核心：本地和交易所对不上时，哪边是对的未知。**

* 本地有仓、交易所没有 —— 可能是被强平、被手动平仓，也可能是本地多记了一笔；
* 交易所有仓、本地没有 —— 可能是漏记，也可能交易所在另一个子账户、另一把 API key；
* 数量不符 —— 可能是部分成交、可能是有人手动加减仓、也可能 `size_decimals` 用错了；
* 止损单丢了 —— 可能成交了、可能被撤了、可能 28 天过期了，也可能只是这次查询没授权。

两种"自动修正"都是灾难：

1. **按交易所覆盖本地**（把本地仓改成交易所的样子）→ 忘掉一个真实敞口。
   「本地有仓、交易所没有」如果照着交易所擦掉，实际敞口会在系统里凭空消失，
   下一次同标的新开仓就变成**双层仓位**，而止损只覆盖其中一层。
2. **按本地覆盖交易所**（把交易所有、本地没有的仓补记成我们的）→ 记一笔不存在的账。
   净额 PnL、DD、敞口上限全部污染，而且这类错误**没有任何后续信号会自己暴露**。

所以本模块的选择是：**报警 + 阻断新开仓**，把决策权交回给人（或 T9 的 watchdog 拿着
更完整的证据）。阻断是 fail-closed 的关键一半：不一致期间"不开新仓"永远不会错，
而"继续开新仓"可能在一个未知敞口上再叠一层。

**唯一的例外**是有正面证据的止损成交结案（3.1），验证清单见上。

### 5.1 一个已知的成本

阻断会让"误报"的代价变得可见：如果 `orders_unavailable`（比如 auth token 过期），
系统会停止开新仓。这是有意的 —— 无法证明"止损还在"时继续开新仓，等于在裸奔。
修 auth 比事后发现一个没有止损的仓位便宜得多。

---

## 6. 交易所读取：哪些是实测的、哪些是推断的

| 项 | 结论 | 依据 |
|---|---|---|
| `GET /api/v1/account?by=index&value=<idx>` **无需 auth** | 实测 | 任务书里的实测记录；本次也按此实现并允许不带 auth |
| 该端点返回 `accounts[].positions[]`，字段 `market_id/symbol/sign/position/avg_entry_price` | 按 SDK 模型实现 | `lighter/models/detailed_account.py::DetailedAccount`、`account_position.py::AccountPosition` |
| `GET /api/v1/accountActiveOrders?account_index=<idx>` **需要 auth** | 按 SDK 签名推断 | `lighter/api/order_api.py::account_active_orders(authorization=..., account_index=...)`；`_account_active_orders_serialize` 里 `resource_path='/api/v1/accountActiveOrders'`，`authorization` 进 header |
| 挂单字段市场叫 **`market_index`**（持仓里叫 `market_id`） | 按 SDK 模型实现 | `lighter/models/order.py::Order` |
| 挂单里 `reduce_only` / `trigger_price` / `base_price` / `base_size` / `status` / `client_order_index` | 按 SDK 模型实现 | 同上 |
| 持仓 SDK 方法名 | `AccountApi.account(by, value)` → `DetailedAccounts` | `lighter/api/account_api.py` |
| 挂单 SDK 方法名 | `OrderApi.account_active_orders(...)`（注意在 **OrderApi**，不在 AccountApi） | `lighter/api/order_api.py` |
| 权益取 `total_asset_value`，退回 `collateral` | 推断（字段名来自 SDK 模型） | `DetailedAccount.total_asset_value` / `collateral` 都是字符串金额；两者都缺 → `None`（fail-closed） |

解析器对**结构不符一律 fail-loud**（`ReaderError`）：`code != 200`、缺 `accounts`、
账户不存在、`positions` 不是列表、`sign` 不是 ±1、数字不是数字。不猜、不降级成空持仓。
唯一的降级是挂单端点失败 → `orders_error` 非空 → 对账出 `orders_unavailable` 并阻断。

---

## 7. 依赖与边界

* **不引入新依赖**：`app/executor.py` 只用 stdlib（`asyncio/json/logging/time/urllib/
  dataclasses/decimal/typing`）+ 本仓库的 `app.*`（`test_module_only_imports_stdlib_plus_the_repo`
  钉住）。
* **不直接碰 signer**：执行器只调 `ExchangeClient.place_*` / `wait_for_tx`，
  不 import `lighter`、不碰私钥（`test_executor_does_not_touch_the_signer_directly`）。
* **DD 状态由 T9 提供**：`dd_state` 为空时 `RiskState.dd_index=None` → risk 的 DD 闸
  fail-closed 拒单。单测里显式 `save_dd_state()` 造一个 0% 回撤的状态。
* **成交价回填**：T8 用**参考价**记账（`plan.entry_price` / 决策的 `exit_price`），
  真实成交价与手续费的回填属于「fills 同步」，留给下一个里程碑；对账的
  `grace` 结案也只记止损价，不假装知道滑点后的成交价。
* 单测 `tests/test_executor.py` 全离线：临时目录里的真 SQLite + 手搓的 K 线 / 交易所 /
  读取器，35 个用例。
