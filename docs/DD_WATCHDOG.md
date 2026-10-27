# 回撤熔断（`app/dd.py`）与健康监控（`app/watchdog.py`）

> 对应 `docs/PLAN.md` T9。上游/下游：`app/risk.py`（阈值与闸门，T4）、`app/store.py`
> （`dd_state` / `dd_anchors` / `equity_curve` / `events`，T5）、`app/state.py`（回撤读数，
> T5）、`app/executor.py`（每轮 tick 调 `guard.update()`，T8）、T10（面板 + 通知 + 调度）。

一句话：**回撤用资金加权收益指数（TWR）算，剔出入金；阈值只认 `app/risk` 那三个函数；
熔断只挡开新仓、永不自解；watchdog 每 5 分钟确认「还活着、数据还在动、报警还发得出去」。**

---

## 1. TWR：为什么不能用「权益峰值回撤」

旧系统（`larkspur8506/crypto-ma-alert`，HL 现役）实盘踩过：

* 2026-10-21 账户转入 **901.80U**，账户权益曲线的「回撤」当天瞬间归零。一笔入金把真实
  的 30% 回撤抹掉了 —— 熔断因此永远触发不了，而且**没有一点告警**。
* 反向也一样：提现会被算成亏损，可能凭空触发熔断、白白停掉一个有浮盈的账户。

所以判据用**资金加权收益指数**：

```
r_k     = (equity_k − flow_k) / equity_{k−1} − 1          # flow_k 落在 (t_{k−1}, t_k]
index_k = index_{k−1} · (1 + r_k)                         # 锚点处 index = 1.0
peak    = max(peak, index)                                # 只升不降
DD      = (peak − index) / peak
```

实现要点（`app/dd.py`）：

| 规则 | 为什么 |
|---|---|
| `flow` 用**带符号**净流水（入金 +、出金 −） | 出金减少权益，不剔就是假亏损 |
| 区间**左开右闭** `(t_{k−1}, t_k]` | 落在上一步那一刻的流水已经体现在那一步的权益里，再加一遍等于重复计入（入金被当成收益 ⇒ 抹掉亏损） |
| 脏时间戳/脏金额的流水**直接不计入** | 宁可少剔一笔，也不要把算不清的流水摊到错误区间 |
| 每一步都要有严格递增的时间戳 | 同一秒重复推进会把这段收益率算两遍（手动 tick / 重启追赶都会撞上） |
| 上一格权益 ≤ 0 → 跳过这一步（`skipped + 1`），指数不动 | 收益率无定义，不猜 |
| `equity − flow < 0` → 指数**钳到 0**（DD = 100%），标 `degraded` | 负指数会让 DD 变成 >100% 的怪数，且 `store` 拒收 `twr_index < 0` |

**逐点重放，而不是「锚点 → 现在」一步算**：锚点距今可能跨过若干次入金，一步算等于把那
些入金当成亏损/收益。旧系统实测：一步算 `88.75` vs 重放 `97.68`，DD 差 7.8 个百分点 ——
足以让一个没亏到线的账户凭空触发 25% 告警。

### 指数基准：库里 `1.0`，面板 `100`

| 位置 | 基准 | 说明 |
|---|---|---|
| `equity_curve.twr_index` / `dd_state.twr_index` / `dd_state.twr_peak` | **1.0** | T5 定的列口径（`app/store.BASE_TWR_INDEX`，`docs/STATE_STORE.md` §`equity_curve`），`reset_dd_peak()` 的默认值也是 1.0 |
| 面板 / 推送文案 | **100** | `DdVerdict.display_index` / `TwrState.display_index` = 库值 × 100（`TWR_DISPLAY_BASE`） |

两者只差一个标量，**回撤比例完全相同**（`(peak − index)/peak` 与基准无关）。统一在库里
存 1.0 是为了不和 T5 已有契约打架；面板要「收益指数 100」就直接乘。
`app/dd.py` 里没有第二套数值口径：`TWR_BASE = app.store.BASE_TWR_INDEX`。

### 一个算例（`tests/test_dd.py` 里逐字写死）

```
锚点 T0: 权益 10000                      index=1.0  peak=1.0  DD=0%
T0+4h : 权益  9000                      index=0.9  peak=1.0  DD=10%
T0+8h : 权益 10000，同时入金 1000
        剔入金: (10000 − 1000)/9000 = 1.0 ⇒ index=0.9·1.0=0.9   DD 仍是 **10%**
        不剔  : 10000/9000        = 1.111 ⇒ index=0.9·1.111=1.0 DD 变成 **0%**（亏损被抹掉）
```

出金同理：`10000 → 提现 1000（权益 8000）` 剔流水后 DD 仍是 `(8000+1000)/10000` 对应的
**10%**，不剔会算成 **20%**（把提现当亏损）。

---

## 2. 档位：阈值**只有一份**

`app/risk.py`（T4）已经落地 `RISK_TIERS` / `dd_thresholds()` / `dd_action()`。
`app/dd.py` **只 import、不重写**（`tests/test_dd.py::test_dd_module_reuses_the_risk_functions`
断言的是 `is` 同一对象，不是「相等」）。

| `risk_pct` 落档 | 告警线 `warn` | 熔断线 `kill` |
|---|---|---|
| ≤ 1.5%（含实盘 1.33% 档） | 25% | 30% |
| ≤ 2.5%（含 2% 档） | 30% | 40% |
| > 2.5%（含 3% 档） | 40% | 50% |

**风险档越高阈值越宽**，不是越严：`risk_pct` 越大，同一策略的历史最大回撤就越深
（旧系统统计：2% 档 P(DD>30%)=60%，3% 档 96%）。升了档却留着上一档的线，等于给一条必然
出现 40%+ 回撤的曲线装 30% 的熔断，长期误触发。

判定顺序（`app.risk.dd_action()`）：

```
DD ≥ kill  → "kill"   （拒绝开新仓）
DD ≥ warn  → "warn"   （只告警，照常开仓）
否则        → "ok"
```

**压在线上就算触发**（`>=`，不是 `>`）。有一个诚实的边界说明：权益 10000 → 9000 → 7000
名义上是「正好 30%」，但因为指数是逐步累乘的（`1.0 × 0.9 × 7000/9000 = 0.7000000000000001`），
实测 DD = `0.29999999999999993` → **warn**。`dd_action()` 里**没有** epsilon（加了就是第二套
口径：面板显示 30.00%、熔断按 29.99% 算）。所以面板/推送要报 `action` 与四位以上小数，
别只报四舍五入的百分比 —— 否则用户会看到「30.00% 却只告警」以为熔断坏了。
（`tests/test_dd.py::GuardStoreTests::test_value_a_hair_below_the_line_warns_and_that_is_the_honest_answer`）

### 脏值 → 最严档

| 输入 | 结果 | 为什么 |
|---|---|---|
| `index` / `peak` 为 `None` / `NaN` / `±inf` / `peak ≤ 0` | `dd_action(None)` → **`kill`** | `NaN` 参与的比较全是 `False`，任何 `dd >= kill` 都会**静默放行** —— 等于把熔断关掉且不留痕迹 |
| `risk_pct` 为 `None` / 垃圾 / `NaN` / `inf` | `dd_thresholds(None)` → 最严档 **25%/30%** | 宁可早一步喊人，也别因为一个读坏的配置把线放到 40% |
| `index > peak`（状态被外力改坏） | DD 记 0，但 `degraded=True` + 原因文案 | 负数 DD 会被下游 `max(0,…)` 静默抹平；标出来才有人修（`store.audit()` 的 `dd_peak_below_index` 是同一件事） |

`DdVerdict.strictest` / `available=False` 就是给面板用的「这条结论是按最坏情况给的」标记。

---

## 3. 峰值只升不降 + 锚点生命周期

### 三层保护

1. **算法层**：`peak = max(库里峰值, 本次重放峰值)`。重放窗口被截断、历史点被删、锚点被挪，
   都不可能把峰值降低。
2. **存储层**：`store.save_dd_state()` 在 `new_peak < stored_peak` 时**抛 `StoreError`**
   （T5 已实现，T9 只是不去绕过它）。
3. **入口层**：唯一允许降峰值的方法是 `store.reset_dd_peak()`，只由
   `DrawdownGuard.reset_peak()`（人工、带理由）调用。**自动路径永不调用** ——
   `tests/test_dd.py::ResetPeakTests::test_update_never_calls_reset` 用
   `mock.patch(side_effect=AssertionError)` 把它钉死。

### 三张表的分工

| 表 | 保留 | 谁写 | 判据地位 |
|---|---|---|---|
| `dd_state`（单行 `name='live'`） | 永久 | `dd.py` 每轮 `update()` | **权威**。峰值的事实来源 |
| `dd_anchors` | 30 天滚动（`DD_ANCHOR_KEEP_DAYS`） | `dd.py`，默认 1h 一条 | 面板画回撤历史用；**过期不影响判定** |
| `equity_curve` | 永久 | T8 executor + `dd.py`（回写 `twr_index`） | 重放的原料；面板虚线 |

锚点写入节流：`DEFAULT_ANCHOR_MIN_INTERVAL_S = 3600s`。4h 一根 bar 时每轮都写也可以，但
如果将来把 tick 调到 5 分钟，30 天窗口会塞进 8600 行 —— 节流到 1h 一条（≤720 行）让面板
仍然有曲线，表也不会无限长。

### 锚点被剪 ≠ 回撤归零

`app/state.py::StateAssembler.dd_reading()` 按**证据强弱**读，`app/dd.py` 的
`DrawdownGuard.verdict()` **直接调它**（不重算第二套）：

| 证据 | `source` | 可用？ |
|---|---|---|
| `dd_state` 行 | `dd_state` | 可用（权威） |
| `dd_state` 缺失 → 最新 `dd_anchors` 行 | `dd_anchor` | 可用但 `degraded=True`（真峰值可能更高） |
| 两者都没有 | `unavailable` | **不可用** → `index/peak = None` → `dd_action(None)` = `kill` → 风控**拒单** |

**绝不**「从 `equity_curve` 最近 30 天重算一个峰值」充数：窗口一滚，回撤凭空归零，熔断再也
触发不了 —— 这正是旧系统 `hl_snapshots` 30 天窗口的事故。

`store.prune_dd_anchors()` 有两条保护（T5）：最新一行永不删；**绝不碰 `dd_state`**。
所以「锚点被剪」这件事对判定**没有影响**：`tests/test_dd.py::AnchorLifecycleTests`
（`DELETE FROM dd_anchors` 后 DD 仍是 31%、仍然 `kill`）。

### 什么时候可以 bootstrap

只有一种自动路径：`DrawdownGuard.bootstrap()` / `update()` 发现 **`dd_state` 不存在**时，
以「库里最早那个权益点」为锚点重放一次，建立 `index = peak = 1.0` 的基线。

* 锚点选最早的点而不是「现在」：一个跑了两周、`dd_state` 被人删掉的账户，用「现在」当锚点
  会把已有回撤全部抹成 0。
* 新基线还取 `max(重放峰值, 最新锚点的 twr_peak)`（`_anchor_peak_floor()`）—— 重放窗口比
  历史短时，锚点里的峰值是**证据**，新基线不许比它低。
* 已有 `dd_state` 时 `bootstrap()` 抛 `DdError`，不覆盖。

---

## 4. 熔断语义：**挡开仓，不挡离场**

这是本模块最重要的一条规则。

| 动作 | 熔断时 | 实现 |
|---|---|---|
| 开新仓 | **拒绝** | `app.risk.check_entry()` 的 `drawdown` 闸（`GATE_DD`） |
| 信号离场（收盘 vs SMA20） | **照常** | `app.lifecycle.decide_exit()` 签名里**没有** `risk_state` |
| 止损离场 | **照常** | 同上；且交易所侧的止损单本来就不经过本进程 |
| 撤单 / 减仓 / flatten | **照常** | T10b 的 `POST /admin/flatten` 是唯一的「比 kill 更强」操作 |

DD 熔断时把亏损仓锁在场上，是明确要避免的事故（旧系统 README 的「单向出口」）。
所以 `app/dd.py` 提供 `entry_blocked(verdict)` / `DdVerdict.blocked` 给**开仓**路径，
**不提供任何**「熔断时禁止离场」的开关 —— `tests/test_dd.py` 有一条断言
`assertFalse(hasattr(dd_module, "exit_blocked"))` 把这条路堵死。

同一个瞬间的两条断言（`CircuitBreakerSemanticsTests::test_entry_blocked_and_exit_allowed_in_the_same_instant`）：

```
DD = 40%（1.33% 档，kill 线 30%）
check_entry("BTC", 1000, risk_state)  → allowed=False, codes=("drawdown",)
lifecycle.decide_exit(position, bars, ind, 44) → action="stop"（low 104 < stop 105）
```

熔断状态**跨重启保持**：`dd_state` 在库里，新建一个 `DrawdownGuard` 读同一个文件得到同样的
`kill`（`test_kill_survives_a_restart`）。

### 熔断**永不自解**

| 路径 | 会解熔断吗 |
|---|---|
| 时间流逝 / 重启 | ❌ |
| `dd_anchors` 被剪 | ❌（峰值在 `dd_state`） |
| 权益曲线被截断 | ❌（峰值 max 保护） |
| 账真赚钱、指数涨回去 | ✅ 这是**唯一**的自动路径 |
| `DrawdownGuard.reset_peak(reason=..., by=...)` | ✅ 人工显式；落两条事件（见下） |

重置的三条硬要求（`DrawdownGuard.reset_peak()`）：

1. `reason` 必须非空（审计要能回答「谁为什么重置的」）；
2. 指数为 0 / 读不出来时**拒绝**（权益已亏光 ⇒ DD=100% ⇒ 必须继续拦着等人核对；
   把峰值抹成 0 只会把爆仓藏起来）；
3. 落 `events`：
   * `dd_reset`（`store.reset_dd_peak()` 写的**状态变更**：峰值改成了多少、reason）；
   * `dd_reset_audit`（本方法写的**操作审计**：`by`、重置前的指数）。

重置后 `index = peak = 1.0`、锚点 = 现在（`store.reset_dd_peak()` 的口径）—— 回撤从此刻
重新计量，「人工解除熔断」才真的能恢复开仓。注意这会**重置收益指数的刻度**：面板曲线在
重置点会出现一次跳变，这是有意的（旧值无法与新基数续算）。

---

## 5. watchdog：六项检查

`check_health(snapshot) -> HealthReport` 是**纯函数**（不读时钟、不碰网络/DB），
`Watchdog.run_once()` 负责采集 + 落 events + 推送。

| 检查 | 判据 | 默认阈值 | 触发 |
|---|---|---|---|
| `container` | 容器内 PID 1 存在且不是僵尸（`/proc/1/stat` 的状态位） | — | `container_dead` / `container_unknown` / `container_unwired` |
| `health` | `GET /health` 200 且 JSON `ok != false` | 5s 超时 | `health_unreachable` |
| `heartbeat` | 调度器最后一次 tick（`events` 里的 `heartbeat`，退化为最新权益点）距今 | **6h** | `heartbeat_stale` / `heartbeat_missing`（warn）/ `heartbeat_in_future`（warn） |
| `bar_state` | 最后一根**已收盘** bar 的收盘时刻距今 | **8h** = `app.market.FRESH_MAX_AGE_MS` = 2 × 4h | `bar_stalled` / `bar_state_unreadable` |
| `push` | 近 6h 内 `events.kind='alert_failed'` 的条数 | 6h / **≥3 条** error，1–2 条 warn | `push_failing` / `push_failures` |
| `db_write` | 真写一次（`BEGIN IMMEDIATE` + 临时表 + DROP） | — | `db_not_writable` |

### 阈值为什么是这些数

* **心跳 6h**：调度周期 4h（`:03` / `:33`）。容忍窗口必须 > 一个周期（否则每次收盘之间都
  误报），< 两个周期（否则「漏掉一整根 bar」不算事故）。6h = 1.5 个周期，正好卡在
  「漏一根就该喊人」的位置。
* **bar 8h**：**复用** `app.market.FRESH_MAX_AGE_MS`（`2 × BAR_MS`）。整个系统对「K 线新鲜」
  只有一个口径，watchdog 不另发明一个数 —— 否则「取数说新鲜、监控说停摆」一定会发生。
  `tests/test_watchdog.py::StructureTests::test_bar_stall_limit_reuses_the_market_freshness_constant`
  钉住它。
* **推送 6h / 3 条**：一条失败可能只是网络抖动；同一窗口 3 条说明通道真坏了。1–2 条只
  warn，避免单次抖动就喊人。
* 全部可在 `WatchdogConfig` / 环境变量里覆盖（`WATCHDOG_HEARTBEAT_MAX_AGE_S` 等），
  填了非法值 → `WatchdogConfigError`（启动就报，不静默回默认）。

### 为什么「容器存活」只看 PID 1

watchdog 跑在容器**里面**，看不到 docker/systemd 的判定。它能拿到的最强证据是「PID 1
还在、且不是僵尸」。容器被 `rm` / OOM 掉时容器内进程随之消失，这条检查自然也不会再跑 ——
那属于**外部**监控（T12 的 compose healthcheck / 宿主机 cron 拉 `/health`）。
进程内检查覆盖的是另一类故障：**进程还在但已经半死**（HTTP 线程卡住、DB 变只读、调度器停了）。

### 四态与「不算 ok」的规则

| `status` | 含义 | 对整体 |
|---|---|---|
| `ok` | 查过，没过线 | — |
| `warn` | 过了提醒线 | 整体 `warn` |
| `error` | 过了事故线 | 整体 `error` |
| `skipped` | **这一项没接上，根本没查** | 整体 **`warn`**（不是 `ok`） |

`skipped` 算 `warn` 是有意的：监控最典型的失效是「以为在查、其实一直没查」。宁可让运维
看见一条黄灯去接线，也不要一个永远绿灯、什么都看不见的 watchdog。`HealthReport.codes`
把 `skipped` 也算进「非 ok 项」，所以「某项从没接上变成接上了」也会被当成一次结论变化留痕。

### 异常处理：不崩，但也不静默

| 故障 | 行为 |
|---|---|
| 某个探针抛异常 | 该探针 → `None` + `probe_errors`；`check_health()` 把那一项改判 `error/probe_error`；`run_once()` 再写一条 `events.kind='watchdog_error'` |
| 判定本身崩（`check_health` 抛） | 外层兜底：写 `watchdog_error` + 日志，返回 `HealthReport(status='error', error=...)` |
| 时钟读不出来 | `_now_safe()` 不抛；`collect()` 用不成立的时间戳拒绝本轮（把 0 当 now 会把所有 age 算成天文数字），`watchdog_error` 里带上时钟的异常文本 |
| `events` 写不进去 | `_safe_event()` 只剩日志（`logger.exception`），`run_once()` 照常返回报告 |
| 推送钩子抛异常 | 写 `events.kind='alert_failed'`（这个 kind 又会被 `push` 检查统计到 —— 有意为之：推送失败会自己出现在下一轮的健康报告里） |

`run_once()` **永不抛异常**（`test_broken_clock_does_not_raise` /
`test_broken_database_does_not_raise` / `test_probe_exception_is_collected_and_does_not_stop_the_run`）。

### 事件去重

`watchdog_report` 只在**结论变化**时写（对比上一条 `watchdog_report` 的 `codes`）。5 分钟
一轮 ⇒ 288 条/天，不去重会把事件流（面板「最近 20 条」）彻底淹掉。`watchdog_error` **不**
去重：探针崩了每次都该看见。

| `events.kind` | 谁写 | 含义 |
|---|---|---|
| `heartbeat` | T10 调度器（`record_heartbeat()`） | 「我这一轮 tick 活着」 |
| `watchdog_report` | watchdog，结论变化时 | 一轮健康结论（含逐项 status/code） |
| `watchdog_error` | watchdog | 探针/判定崩溃（每次都写） |
| `alert_failed` | T8 executor / watchdog | 推送失败（`push` 检查的数据源） |
| `dd_bootstrap` / `dd_warn` / `dd_kill` / `dd_ok` | `dd.py` | 基线建立 + 动作**变化**（不变化不写） |
| `dd_reset` / `dd_reset_audit` | store / `dd.py` | 峰值重置的状态变更 / 操作审计 |
| `dd_skipped` / `dd_ledger_error` / `dd_anchor_error` | `dd.py` | 权益脏、回写指数失败、锚点写入失败 |

---

## 6. 交给 T10 的接线清单

T9 **不做**的事（都在 T10）：

| 事项 | T9 提供 | T10 要做 |
|---|---|---|
| 每轮 tick 推进 TWR | `DrawdownGuard.update(equity=…, ts=…, flows=…)` | 在 `executor.run_once()` 之后调一次，`flows` 从交易所出入金流水来 |
| 心跳 | `watchdog.record_heartbeat(store)` | 调度器每轮调一次 |
| `/health` 探针 | `default_health_probe(url)` | 保证 `/health` 200；容器内 URL 用 `127.0.0.1:8000` |
| bar_state 探针 | `BarStateProbe(market, symbols)` 或 `Watchdog(market=…)` | 把 `MarketClient` 传进来 |
| 推送失败统计 | `default_push_probe(store)` | 推送模块失败时写 `events.kind='alert_failed'`（T8 已有这个约定） |
| 告警推送 | `Watchdog(alert=…)` 钩子 | 接 `notify`；只推异常（`report.needs_alert`） |
| 定时 | `WatchdogConfig.interval_s = 300` | 5 分钟一轮 |
| 面板 | `DdVerdict.as_dict()`（含 `display_index` / `display_peak` / `action` / `warn` / `kill`）、`HealthReport.as_dict()` | 直接渲染，不重算 |
| 人工重置 | `DrawdownGuard.reset_peak(reason=…, by=…)` | `POST /admin/dd/reset`（admin token + 二次确认） |

**检查项接线状态**：T9 阶段六项里 `push` 必然 `skipped`（没有通知模块），`bar_state` 只有在
构造时传了 `market` 才真的查。上线（T12）前六项必须全部接线 —— 否则报告会一直是 `warn`，
而「一直黄灯」和「一直绿灯」一样会让人忽略它。

---

## 7. 测试地图（`tests/test_dd.py` + `tests/test_watchdog.py`，104 条，全离线）

| 验收项 | 测试 |
|---|---|
| TWR 剔出入金（入金不稀释 / 出金不放大） | `BootstrapTests::test_deposit_does_not_shrink_the_drawdown`、`test_withdrawal_does_not_create_a_fake_drawdown`、`GuardStoreTests::test_deposit_through_the_guard_keeps_the_drawdown` |
| 峰值只升不降 | `GuardStoreTests::test_peak_never_goes_down_even_when_the_replay_window_is_thin`、`test_store_refuses_a_lower_peak`、`AdvanceTests::test_peak_only_rises` |
| 三档阈值联动 | `TierLinkageTests::test_three_tiers_with_hardcoded_lines`、`test_reported_thresholds_match_the_tier_table`、`test_action_always_equals_risk_dd_action` |
| 只用 `app.risk` 的函数 | `TierLinkageTests::test_dd_module_reuses_the_risk_functions`（`is` 同一对象） |
| 脏值 → 最严档 | `DirtyValueTests`（8 种脏输入） |
| 熔断挡开仓 | `CircuitBreakerSemanticsTests::test_kill_blocks_new_entries_with_only_the_drawdown_gate` |
| **熔断不挡离场** | `test_kill_does_not_block_a_stop_exit`、`test_kill_does_not_block_a_signal_exit`、`test_entry_blocked_and_exit_allowed_in_the_same_instant`、`test_decide_exit_signature_has_no_risk_state` |
| 锚点被剪不归零 / 全丢 → 拒单 | `AnchorLifecycleTests::test_pruning_all_anchors_does_not_reset_the_drawdown`、`test_losing_state_and_anchors_blocks_entries_instead_of_reading_zero` |
| 重置显式 + 落审计 | `ResetPeakTests`（`test_update_never_calls_reset` / `test_reset_is_audited_and_restores_entries` / `test_reset_refuses_a_wiped_out_account`） |
| watchdog 各检查项分别触发 | `ContainerCheckTests` / `HealthEndpointTests` / `HeartbeatTests` / `BarStateTests` / `PushCheckTests` / `DbWriteCheckTests` |
| watchdog 异常不崩主循环且留痕 | `WatchdogRunTests::test_probe_exception_is_collected_and_does_not_stop_the_run`、`test_judging_crash_is_recorded_and_returns_an_error_report`、`test_broken_clock_does_not_raise`、`test_broken_database_does_not_raise` |
