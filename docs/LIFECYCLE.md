# 持仓状态机与决策核心（T6）

`app/lifecycle.py` 把「信号 → 动作」编排起来，**但不下单**（下单在 T7 `exchange.py`，
对账/重试在 T8 `executor.py`）。本文件说明三件事：**状态怎么走**、**每根 bar 判什么**、
以及两条**写死的红线**（减仓不受熔断阻挡、离场判据必须来自 `app/strategy.py`）。

---

## 1. 状态机

状态名与 `app/store.py::POSITION_STATES` 是**同一份**（模块里直接
`STATES = tuple(POSITION_STATES)`，不写第二份字面量），转换表也直接取
`store.ALLOWED_TRANSITIONS`（单测钉住两者相等）。

```
                     entry（sizing + risk 都通过，store.record_entry）
        IDLE ────────────────────────────────────────────────▶ OPEN
                                                                 │
                                       exit / stop（离场单已发出）│
                                                                 ▼
                                                              CLOSING
                                                                 │
                          ┌──────────────────────────────────────┤
   撤单失败 / 离场单被拒  │                                      │ 成交确认（record_exit/record_stop）
   （abort_close）        │                                      ▼
                          └──────────────▶ OPEN               CLOSED（终态）
```

| 转换 | 触发条件 | 落库（T5） | 事件 |
|---|---|---|---|
| `IDLE → OPEN` | 开仓决策 `action='entry'`：`sizing.plan_order()` **且** `risk.check_entry()` 都通过，随后由 T8/T7 实际成交，`Lifecycle.apply_entry()` 记仓位行 | `store.record_entry()` → `positions.state='OPEN'` + 开仓成交 + 当日计数 | `open`（store）+ `transition`（本模块） |
| `OPEN → CLOSING` | 离场决策 `action='exit'`（收盘破 SMA20）或 `action='stop'`（触及止损价），且离场单已发出 | `store.begin_close()` | `closing`（store）+ `transition` |
| `CLOSING → CLOSED` | 交易所确认成交（或对账确认交易所侧已无仓）：`complete_close(kind='exit'|'stop')` | `store.record_exit()` / `store.record_stop()` → `positions.state='CLOSED'` + 平仓成交 + 已实现盈亏 | `exit`/`stop`（store）+ `transition` |
| `OPEN → CLOSED` | 对账直接确认「交易所无仓、库里还没进 CLOSING」的收敛路径（T8 用） | `store.record_exit()` | `exit`/`stop` + `transition` |
| `CLOSING → OPEN` | **唯一往回走的路径**：撤单失败 / 离场单被拒 / 交易所侧仍有仓 → `abort_close()` | `store.set_position_state(…, 'OPEN')` | `transition`（reason 写明 `close_rejected` 等） |
| `CLOSED → *` | **禁止**（终态）：抛 `IllegalTransition`，库里的 `_check_transition()` 也拦 | — | — |

### 没有第五个状态：失败态 = 停在 `CLOSING`

- 离场单发出但迟迟没成交 → 仓位**留在 `CLOSING`**。它仍然是「有仓位」：风控继续把它算进
  `open_positions`（`store.OPEN_STATES = (OPEN, CLOSING)`），所以宽限期内**不可能**在同一
  标的开出第二仓。
- 明确失败（单被拒 / 撤单失败 / 交易所侧还在）→ `abort_close()` 回 `OPEN`，**显式**回退并落
  事件。绝不「静默当作已平掉」—— 那就是「本地无仓、交易所有仓」的对账事故。
- 对账不符（交易所说没仓、库里 `OPEN`）→ 走 `OPEN → CLOSED` 直接收敛（T8 顺序：先
  `_sync_fills` 再判 mismatch）。

### `CLOSED` 是**行**的终态，不是标的的

`positions` 有 `UNIQUE(symbol, bar_ms)`，一笔交易一行。一笔关账后该标的的**槽位**回到
`IDLE`（`Lifecycle.state(symbol)` 读的就是槽位），下一笔信号**新开一行**。所以：

- 「`CLOSED` 之后又能开仓」= 新行 `IDLE → OPEN`；
- 「`CLOSED → OPEN` 非法」= 想改老行。

这两句话不矛盾，是同一张表的两面：转换表管的是**行**的状态列。

---

## 2. 每根 bar 判什么

`decide(symbol, bars, signal=…, equity=…, market_info=…, risk_state=…)`：

```
有活仓（OPEN/CLOSING）？ ──是──▶ 离场分支 decide_exit()   ← 完全不看风控
        │否
        ▼
   开仓分支 decide_entry()   ← sizing → risk，两者都过才切 OPEN 意图
```

### 2.1 离场分支（`decide_exit`）

判定顺序（**止损优先**）：

1. 仓位不是活仓（`IDLE`/`CLOSED`）→ `skip / no_position`（不该被问到；问到就报出来）。
2. 已经是 `CLOSING` → `hold`：离场单已发，**不重复发单**，等交易所确认。
3. 还没到入场根（`bar.open_ms < positions.bar_ms + 4h`）→ `hold`。
4. **止损**：多单 `bar.low ≤ positions.stop_price`、空单 `bar.high ≥ stop_price`
   → `action='stop'`，`exit_price = stop_price`（成交价是止损价，滑点交给 T7 的
   reduce-only 腿）。仓位上没有止损价 → 不收静默：记一条
   `Rejection(stage='lifecycle', code='bad_position')`，信号离场照常评。
5. **信号离场**：`app.strategy.exit_triggered(bars, ind, i, side)`
   → `action='exit'`，`exit_price = bar.close`。
6. 都没触发 → `hold`。

**止损 vs 信号离场必须分得清**：前者 `kind='stop'`、后者 `kind='exit'`（`trades.kind`
不同、`positions.exit_reason` 不同）。同一根 bar 既破 SMA20 又触止损 → 记 `stop`
（成交价只能是止损价，不能是更低的收盘价 —— 否则回测的 R 倍数会与实盘对不上）。

**入场当根只允许止损**：信号 bar 的下一根就是入场根，MA20 离场从**再下一根**起才生效。
这是旧系统 `engine.walk()` 的 `is_entry_bar` 守卫，也是回测引擎的 `u > ei` 条件 —— 少了
这条，入场那根的正常回撤会被当成离场信号，一笔交易变成零根。

### 2.2 开仓分支（`decide_entry`）

前置检查（`stage='lifecycle'`，全部结构化，不静默）：

| 检查 | 拒绝码 |
|---|---|
| 该标的已有 `OPEN`/`CLOSING` 仓位 | `symbol_already_open` |
| 该 bar 已有仓位行（`UNIQUE(symbol, bar_ms)`） | `same_bar_already_traded` |
| 本根没有信号 | `no_signal` |
| 信号不属于正在判定的 bar（回放错位） | `signal_bar_mismatch` |
| 定仓用的权益 ≠ 风控看到的权益 | `equity_mismatch` |

然后 **sizing → risk**，任一拒绝 → `action='skip'` + `Rejection(stage, code, message, detail)`，
**绝不产生开仓意图**。risk 的每个被挡闸门各出一条 rejection（`kill_switch` / `daily_loss` /
`daily_trade_limit` / `max_positions` / `duplicate_symbol` / `symbol_notional` /
`total_notional` / `drawdown` / `bad_notional` / `state_unavailable`），面板能一次看全。

### 2.3 决策顺序：**先 sizing，后 risk**

不是因为习惯，而是因为 `risk.check_entry()` 的关键入参就是 `notional`：每标的名义上限
（权益 × 1.5）、总名义上限（权益 × 2.5）、`bad_notional` 闸全都要用真实下单量，而真实下单量
只有 `sizing.plan_order()` 算得出来（风险定仓 → 精度 floor → 最小量校验）。先跑 risk 就得先
猜一个名义，等于让风控去看一个不是真实下单量的数。

fail-safe 也不受影响：两个函数都是**纯函数、无副作用**（不下单、不写库），且**两者都通过**
才产出意图 —— risk 是最后一道闸，任何一条不满足都不会有 `EntryIntent` 漏出去。sizing 先跑
还多一层保护：市场规则缺失（未登记标的 / 冻结 / 缺 `min_initial_margin_fraction`）在 sizing
就 fail-closed，连名义都算不出来，自然不会去问风控「这笔能不能下」。

单测 `test_sizing_runs_before_risk_and_risk_sees_the_sized_notional` 用 spy 钉住顺序与
数据流（risk 收到的 `notional` = 3380.145884 = 32.43902 × 104.2）；
`test_sizing_rejection_skips_risk_entirely` 则要求 sizing 拒单时 risk **根本不被调用**。

---

## 3. 红线一：**减仓不受熔断阻挡**

> **熔断（DD kill）、每日亏损上限、kill switch、日交易笔数上限 —— 全部只挡「开新仓」。
> 平仓 / 止损 / 撤单永远放行。**

实现方式不是「记得判一下」，而是**结构上拿不到风控状态**：

```python
def decide_exit(position, bars, ind, i, *, symbol=None) -> Decision:
    ...   # 没有 risk_state、没有 limits、没有 kill
```

* `decide_exit()` 的签名里**没有** `risk_state` / `limits`（单测
  `test_exit_decision_takes_no_risk_inputs` 断言这一点）。
* `decide()` 只在**开仓分支**使用 `risk_state` / `limits`；有活仓时直接走离场分支。
* `app/risk.py::check_entry()` 本身也只管开仓（其 docstring 写明「只管开仓，不管离场」，
  7 个闸门全部是 entry-only）。

为什么必须写死：DD 熔断的触发条件通常是「已经亏了 30%」，此时如果熔断把平仓也挡住，
**仓位就被锁在亏损里**，唯一的出口只剩交易所侧的止损单 —— 而止损单可能没挂上/被撤。
旧系统（HL 现役）踩过这个坑之后把这条写成了单向出口，这里原样继承。

对应测试（都会在 CI 里跑，可亲自验证）：

* `ExitDecisionTests.test_drawdown_circuit_breaker_still_allows_the_exit`
  —— 40% 回撤（≫ 30% 熔断）+ `RiskLimits(kill=True)`，离场决策照样是 `action='exit'`、
  `rejections=()`。
* `ExitDecisionTests.test_daily_loss_limit_still_allows_the_exit` —— 当日亏 20%（> 5% 上限）时
  平仓成立，而**同一份**账户状态下去开新仓会被 `daily_loss` 拒（反面也写死）。
* `ExitDecisionTests.test_drawdown_kill_blocks_entries_in_the_same_account_state` —— 反面：
  同一熔断状态确实挡住开仓。

---

## 4. 红线二：离场判据来自冻结口径

信号离场**只**调用 `app.strategy.exit_triggered(bars, ind, i, side)`：

```python
def exit_triggered(bars, ind, i, side=1) -> bool:
    """第 i 根收盘是否触发离场：做多=收盘 < SMA20；做空=收盘 > SMA20。"""
```

* 本模块**不重写**这条判据、不复制常数（不做 `close < sma20` 的内联比较），调用点是
  `strategy.exit_triggered(...)`（模块属性查找），所以单测可以用 spy 证明它真的被调用、
  参数就是这根 bar：`test_exit_uses_the_frozen_exit_triggered_function`。
* 严格不等式：收盘**恰好等于** SMA20 不触发（`test_close_exactly_on_sma20_does_not_exit`）。
* `app/strategy.py` 是 sha256 冻结文件（`8682667…ece07`），`tests/test_strategy_port.py`
  每次 CI 都会重算哈希 —— 本任务没有、也不允许改它。
* 止损判据（`bar.low ≤ stop` / `bar.high ≥ stop`）不来自 strategy：它是**交易所侧止损单**
  的本地对偶，旧系统 `engine.walk()` 用的也是这两行，口径一致。

---

## 5. 决策结果是纯数据

`Decision` 是 frozen dataclass，`as_dict()` 可 `json.dumps`（T10 面板直接渲染，无需再解析文案）：

| 字段 | 含义 |
|---|---|
| `action` | `entry` / `exit` / `stop` / `hold` / `skip` |
| `from_state` / `to_state` | 这条决策会引发的转换（不转换时两者相同） |
| `bar_ms` / `bar_index` | 判定所在 bar 的开盘时间 / 下标 |
| `side` / `position_id` | 方向 / 仓位行 |
| `entry_price` / `stop_price` / `exit_price` | 参考价 / 止损价 / 离场价 |
| `qty_int` / `notional` / `price_decimals` / `size_decimals` | 整数化的下单量与精度（T7 直接用） |
| `plan` | 完整 `OrderPlan`（含杠杆、保证金、实际风险、warnings） |
| `signal` | 触发信号的摘要（bar_index / side / entry / stop / band） |
| `rejections` | 结构化拒绝理由元组（`stage` / `code` / `message` / `detail`） |
| `reason` | 一句人能读的话 |
| `detail` | 附加机读字段（`kind`、`trigger`、`is_entry_bar`、`sma20`、`close`…） |

`Transition` 同样是纯数据（`symbol` / `from_state` / `to_state` / `reason` /
`position_id` / `bar_ms` / `event_id`），`event_id` 就是 `events` 表的行号，面板/审计可
按 `position_id` 串起一笔交易的完整生命周期。

---

## 6. 事件（`events` 表）

每一个状态转换都会写一行 `kind='transition'`：

```
message: "BTC IDLE→OPEN (entry)"
data:    {"from":"IDLE","to":"OPEN","reason":"entry","position_id":1,"bar_ms":1606048000000,
          "qty_int":3243902,"entry_price_int":1042,"stop_price_int":1001,"notional":3380.145884}
```

`store.record_entry` / `begin_close` / `record_exit` 自己还会写语义事件
（`open` / `closing` / `exit` / `stop`）—— 两者并存：前者是状态机轨迹，后者是业务动作。
`test_every_transition_step_lands_in_events` 断言一个完整回合恰好 3 条 `transition`
（`IDLE→OPEN`、`OPEN→CLOSING`、`CLOSING→CLOSED`），且 `Transition.event_id` 能对上库里
的行号。

---

## 7. 接口速查

```python
from app.lifecycle import Lifecycle, decide, decide_entry, decide_exit, PositionView

lc = Lifecycle(store, limits=RiskLimits(), sizing_config=SizingConfig())

# 纯决策（无 I/O，回放/测试/回测都能用）
d = decide_entry(sig, equity=eq, market_info=info, risk_state=rs, bars=bars, i=i, bar_taken=False)
d = decide_exit(position_row, bars, ind, i)
d = decide("BTC", bars, i=i, signal=sig, equity=eq, market_info=info, risk_state=rs)

# 读库拿当前仓位再决策
d = lc.decide("BTC", bars, signal=sig, equity=eq, market_info=info, risk_state=rs)

# 记账（**不是**下单）
lc.apply_entry(d)                       # IDLE → OPEN（record_entry + 事件）
lc.begin_close(pid, reason="stop")      # OPEN → CLOSING（发单时）
lc.complete_close(pid, exit_price_int=1001, kind="stop")   # CLOSING → CLOSED（成交回报）
lc.abort_close(pid, reason="close_rejected")               # CLOSING → OPEN（回退）
lc.apply_exit(d)                        # 上面两步的便捷版（dry-run / 单测）
lc.state("BTC")                         # 槽位状态：IDLE / OPEN / CLOSING
```

`Lifecycle.transition(symbol, to_state, from_state=…, position_id=…)` 是唯一的状态机守卫：
非法转换抛 `IllegalTransition`（`LifecycleError` 子类），**不静默**。

---

## 8. 边界（本模块不做什么）

* **不下单**：没有 `SignerClient`、没有 `client_order_index` 生成、没有滑点参数 →
  T7。`apply_*` 只把已发生的事实记进库。
* **不对账**：交易所侧与库的差异由 T8 解决；本模块只提供 `abort_close()` 与
  `OPEN → CLOSED` 两条收敛路径。
* **不跑整段回放**：`decide()` 是「一根 bar 一步」（= 旧系统 `engine.walk()` 的循环体）。
  整段历史的开平仓回合与 R 倍数统计属于回测/交叉验证（`scripts/backtest_engine.py`、
  `scripts/xvalidate.py`，T11），它们复用同一份 `app/strategy.py` 与本模块的决策，不另写
  一套口径。
* **不发网络请求**：源码里没有 `urllib` / `requests` / `httpx` / `socket`，
  `app.market` 只在 `TYPE_CHECKING` 下作类型引用（单测
  `test_module_does_not_import_a_network_client` 用 AST + 运行时 `vars()` 双重断言）。
* **不读 env / 不建配置**：`RiskLimits` / `SizingConfig` 由调用方构造后注入（T3/T4 的
  配置层负责）。
