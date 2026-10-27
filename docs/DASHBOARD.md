# 监控面板（T10）

> 代码：`app/views.py`（6 区块渲染 + 自包含 HTML）、`app/main.py`（应用 + 三个写操作 + 调度）。
> 测试：`tests/test_views.py` / `tests/test_admin.py` / `tests/test_main.py`（全离线）。
> 计划依据：`docs/PLAN.md` §5.5（6 区块详规）、T10 / T10a / T10b / T10c。

面板服务的是**同一个进程**（`uvicorn app.main:app`），不做第二个服务：一个容器、一个 DB、
一个面板，少一个能写错地方的东西。

---

## 0. 一条硬约束：面板永远不碰私钥路径

**`app/views.py` 不 import `app.exchange`（也不 import `app.executor`，它会把
`app.exchange` 拖进 import 图）。** 这条有 AST 测试钉住
（`tests/test_views.py::ModuleBoundaryTests`），不是因为洁癖：

* `app.exchange` 是**唯一**持 API 私钥、唯一能下单的模块。面板每天被刷新几百次；
  一个能读私钥的进程不该同时对外提供网页。
* 面板读的东西只有两样：**DB**（`app.store` 的只读查询）+ **Lighter 公开只读接口**
  （K 线/盘口，`app.market`，无鉴权、无签名）。
* 交易所侧的**私有**读数（权益 / 挂单 / 现价）由**调用方注入**：`app.main` 在 tick 之后
  把它们放进 `PanelCache`，`app.views` 只拿到已经取好的普通 dict/list。
* 唯一的例外是 `POST /admin/flatten` 的默认实现 —— 它的 `import app.exchange` 写在
  **函数体内部**，只在真的有人按下「清仓」时才加载。`import app.main` 的 import 图里
  没有私钥模块（有测试断言 `/health` 不碰 DB、不碰凭据）。

后果（也是设计目标）：**面板进程在完全没有凭据的环境里也能跑起来**，只是相关区块降级成
「未知 + WARNING」，而不是起不来。

---

## 1. 六个区块

顺序 = 页面顺序，也是 `views.BLOCKS` 的顺序。每块都独立降级（见 §4）。

### ① 状态条 `id="block-status"`

永远可见的一行：状态灯 + 权益 + 持仓 N/3 + kill + 回撤 + 模式 + 上次 tick + 数据源。

| 字段 | 含义 |
|---|---|
| `level` | `ok` / `warn` / `alert`；页面顶栏颜色由它决定 |
| `reasons` | 变红/变黄的原因（人能读的一句话，逐条） |
| `equity` / `equity_source` | 权益与来源（`provider` = 交易所读数，`store` = 库里最后一个点，`unavailable`） |
| `open_count` / `max_positions` | 持仓数 / 上限（`RiskLimits.max_positions`） |
| `kill` / `kill_reason` / `kill_known` | 人工停手开关（见 §5） |
| `dd` | 回撤读数：`index` / `peak` / `drawdown` / `action` / `warn` / `kill` / `source` |
| `last_tick_ts` / `last_tick_text` | 最近一次 tick 心跳（T9 watchdog 读同一张表） |
| `data_source` | `{ok, name, age_s, last_ts, detail}`：K 线源新鲜度（无行情源时退回权益点年龄） |

判定优先级：**alert > warn > ok**。`alert` 的理由：回撤熔断、回撤口径不可用（fail-closed）、
数据源异常。`warn`：回撤告警、kill 打开、任何区块带 WARNING。

### ② 仓位表 `id="block-positions"`

每个标的一行（`BTC` / `ETH` / `SOL` 恒显示，没仓也占位）：

symbol ｜ 方向 ｜ 开仓价 ｜ 现价 ｜ 浮盈(U) ｜ 浮盈% ｜ 数量 ｜ 止损价 ｜ 距止损% ｜
名义(U) ｜ 保证金(U) ｜ 强平≈ ｜ 持仓时长 ｜ 状态

* **状态只有三种**：`持仓中` / `等待开仓` / `无仓位`（`CLOSING` 时额外显示 `离场中`，
  因为那时候交易所可能已经发过单，藏起来会误导）。
* `等待开仓` = 没仓位**且**开仓没被阻断；被阻断时显示 `无仓位`（`entry_blocked=true`）。
* 名义按**当前价**算（看的是现在的敞口）；保证金 = 名义 / 5（逐仓 5x，与回测口径一致）；
  `强平≈` 是 `entry × (1 − 1/5)` 的**估算**（不含维持保证金与手续费），只用来判断
  「止损会不会先被强平吃掉」。
* 现价来源缺失时按开仓价估算，并带 `price_source="entry_price"` + WARNING。

### ③ 止损单覆盖 `id="block-stops"`（常驻，不可折叠）

**旧系统踩过的坑**：止损单没挂上 = 裸奔。所以这一块独立成区，且必须能一眼看出「在/不在」。

symbol ｜ 方向 ｜ 止损价 ｜ 仓位数量 ｜ 交易所挂单（在/不在/数量不足/未知）｜ 挂单数量 ｜
受保护 ｜ 最后校验 ｜ 说明

* 判定：同标的有挂单 + 价格在 **1.5 个 tick** 容差内 + `qty >= 仓位数量` + `reduce_only`。
* 三种非保护状态都**红字**：`missing`（不在）、`partial`（数量不足）、`mismatch`
  （不是 reduce-only）。
* `unknown`（没有挂单数据源）**不算保护**，但也**不报「不在」** —— 把「不知道」渲染成
  「红警报」会让真警报失效。`PanelCache.orders()` 从没采集过时**抛异常**而不是返回 `[]`，
  就是为了这个区别。

### ④ 盈亏曲线 `id="block-pnl"`（双口径）

* **实线 = 累计盈亏**，单位 U，**已剔出入金**：`equity − anchor_equity − Σ流水(anchor, ts]`。
* **虚线 = TWR 收益指数**，单位 %（库里按 1.0 存，显示 ×100）。
* 回撤 `drawdown_pct` = `(peak − index)/peak`，与 `app.risk.drawdown` 同一个算式。
* 峰值取 `max(重放峰值, 库里 dd_state.twr_peak)` —— **峰值只升不降**（重放窗口被截断时
  不许把回撤算小）。
* 流水从哪来：`equity_curve.cum_flow`（`app.dd` 每轮写权益点时顺手写的累计净入金）的
  **差分**。不另建流水表，也不让面板自己维护一份「入金记录」——两份就会分叉。

**口径红线**：TWR 只用 `app.dd` 的 `bootstrap` / `advance` / `net_flow` / `normalise_points`，
面板**一行都不重算**。而且 `pnl_curve()` 会把「逐点 `advance`」与「`bootstrap` 一次性重放」
对账，把差值写进 `consistency`（`match` / `index_delta` / `peak_delta`），页面上显示
「口径一致 / 口径漂移!」。这条是旧系统「三个回撤口径混用」的直接对策：漂移必须在运行时
立刻可见，而不是等两个数字打起来再查。

### ⑤ 三标的动态 `id="block-symbols"`

symbol ｜ 当前价 ｜ 6MA 带宽% ｜ 密集门槛% ｜ 距门槛 ｜ 连续密集根数 ｜ SMA20 ｜ SMA200 ｜
下一根 bar 收盘(UTC) ｜ 结论

* **「距密集门槛」是「为什么没开单」的唯一证据**（PLAN 第 5.5 节）。
* 全部数值来自 `app.strategy` 的**同一个函数**：`compute_indicators()` → `squeeze_flags()`
  → `explain_entry()`（六步诊断）。面板不复制窗口/分位常数，也不自己写一套判定 ——
  否则「面板说能开、执行器不开」这种 bug 比拼错数字更难查。
* 单标的取数失败只让那一行降级（`blocked_by="no_data"`），不影响另外两个。

### ⑥ 执行器事件流 `id="block-events"`

最近 20 条：时间(UTC) ｜ 标的 ｜ 动作 ｜ kind ｜ 内容。

动作分类（`views.event_action()`）：开仓 / 离场 / 止损 / 熔断 / **跳过** / 人工 / 心跳 /
校验 / 其他。

**「跳过」必须显示**：`app.executor` 把「未过密集门槛」这类结论放在 `TickReport.outcomes`
里、**没有写库**；`app.main.record_tick_outcomes()` 在每轮 tick 后把它们落进 `events`
（`kind="skip"`）。不写下来的话，面板就只剩「开仓 0 笔」，用户会以为系统坏了 ——
而真相通常是「均线不密集」。

---

## 2. 端点表

### 只读（**永远 200**，永不 500）

| 端点 | 内容 | 主要字段 |
|---|---|---|
| `GET /health` | 容器存活 | `ok` / `service` / `version` / `uptime_s`（**不碰 DB/网络/凭据**） |
| `GET /status` | ① 状态条 | 见 §1① |
| `GET /positions` | ② 仓位表 | `positions[]` / `open_count` / `entry_blocked` |
| `GET /stops` | ③ 止损覆盖 | `stops[]` / `orders_known` / `uncovered[]` / `all_protected` |
| `GET /pnl` | ④ 盈亏曲线 | `points[]` / `cum_pnl` / `twr_pct` / `peak_pct` / `drawdown_pct` / `consistency` |
| `GET /symbols` | ⑤ 三标的动态 | `symbols[]`（含 `blocked_by` / `reason`） |
| `GET /events` | ⑥ 事件流 | `events[]` / `skipped_count` |
| `GET /` | 自包含 HTML 页 | 6 个 `id="block-*"` 区块 |
| `GET /admin/state?k=` | 写面板现状（kill / 调度器 / 缓存年龄） | 需要 token |

HTML 是**自包含**的：样式内联在 `<style>`、曲线是内联 `<svg>`、**没有** `<link>` /
`<script src>` / `@import` / `http(s)://` 外链。运行环境可能没有外网，一个依赖 CDN 的
面板在关键时刻就是空白页（有测试逐个字符串钉住）。

### 写操作（唯一写面板，全部要 `ADMIN_TOKEN`）

| 端点 | 参数 | 语义 |
|---|---|---|
| `POST /admin/kill` | `on=1`（默认）/ `on=0`、`reason`、`by` | 停开新仓；**不挡离场**。`on=0` 放开 |
| `POST /admin/flatten` | `confirm=yes`（**必填**）、`reason`、`by` | 市价清仓 + 撤所有挂单。唯一的「比 kill 更强」操作 |
| `POST /admin/dd/reset` | `reason`（**必填**）、`by` | 重置回撤峰值。熔断**永不自解**，人工介入后靠它重开 |

响应统一带 `ok` / `degraded` / `warnings`；写操作成功时带 `audit_event_id`。

状态码：

| 码 | 何时 |
|---|---|
| 200 | 正常执行；**以及** DB 不可用 / 交易所拒绝时的降级（`ok=false` + `warnings`） |
| 400 | `flatten` 缺 `confirm`、`dd/reset` 缺 `reason`（客户端错误） |
| 401 | 缺 token / 错 token / `ADMIN_TOKEN` 未配置（fail-closed） |

**为什么降级用 200 而不是 5xx**：5xx 会让反代/探针把面板判死，而面板本身（其他区块）是
好的；真实结论在 body 的 `ok=false` + `warnings` 里，审计事件也照写。只有「你是谁」和
「你有没有说要清仓」是 4xx —— 那不是系统故障。

---

## 3. 鉴权：`?k=<token>` 的权衡

PLAN 规定 query 参数。**照做，但必须写明代价**：

| 通道 | 进访问日志？ | 说明 |
|---|---|---|
| `?k=<token>` | **会** | uvicorn/1Panel/nginx 的 access log、浏览器历史、`Referer`（点到外链时会带出去） |
| `X-Admin-Token: <token>` | 不会（头不进 URL） | **推荐**；curl/脚本/手机书签都能用 |

因此实现上：

* **两个通道都收**（query 优先）；文档推荐 header；
* 比较用 `hmac.compare_digest`（定长比较，不泄漏前缀）；
* token **绝不**进 `events`、绝不进响应、绝不进日志 —— `_audit()` 的 `data` 是白名单字段
  （`endpoint` / `result` / `by` / `reason`），**不是**整个 request（有测试断言库里搜不到 token）；
* `ADMIN_TOKEN` 为空 = **拒绝所有写操作**（fail-closed），而不是「没设密码所以放开」；
* 反向代理侧建议再加一层 IP 白名单 + HTTPS（PLAN §5.5 部署形态）。

**推论**：如果用 `?k=`，请假设 token 会出现在日志里 —— 用一个一次性的、可轮换的值，
别复用别处的密码。真要长期用，关掉该域名的 query 日志或改用 header。

---

## 4. 降级策略（验收项：绝不 500）

`Panel._guard()` 把**每个区块**单独包起来。任何异常 → 该块变成「0 值 + WARNING +
`degraded: true`」，其余区块照常出，页面照常渲染。

| 故障 | 表现 |
|---|---|
| DB 打不开 / 被锁 / 迁移中 | 每块 0 值 + WARNING；状态条 `alert`；**下一次请求会自动重试**（store 首次失败不缓存失败） |
| 权益读不出来 | `equity=0.0` + `equity_known=false` + WARNING（**不猜**一个数） |
| 挂单源缺失 | 止损覆盖全部 `unknown` + WARNING（**不显示「在」**，也不报红「不在」） |
| K 线源缺失 / 单标的失败 | ⑤ 降级（`blocked_by="no_data"`），其余标的与其他区块不受影响 |
| 行情 provider 抛异常 | 该 provider 对应的块降级，其他块不动 |
| tick 从没跑过 | `last_tick_text="从未 tick"` + WARNING |

**「0 值」不等于「正常」**：每个降级载荷都带 `degraded=true` 与非空 `warnings`，状态条据此
变红。宁可喊「我不知道」，也不能静默显示一个好看的 0。

写操作同理：DB 不可用 → `200 {"ok": false, "degraded": true, "warnings": [...]}`；
`flatten` 实现抛异常 → 同一形状（`error` + `warnings`）；不 500，不静默。

---

## 5. kill 的语义（与 `app/dd.py` 一致）

* **kill 只挡开新仓**：唯一消费点是 `app.risk._gate_kill()` → `RiskLimits.kill`；
  `check_entry()` 会返回 `GATE_KILL`。
* **不挡离场**：`app.lifecycle.decide_exit()` 的签名里**没有**风控入参 —— 结构上不可能被
  kill/熔断拦住（`docs/LIFECYCLE.md`「减仓不受熔断阻挡」）。
  `tests/test_admin.py::KillTests::test_kill_does_not_block_exits` 同一个瞬间两侧都断言：
  开仓被挡、止损照常。
* **持久化在 `events`**：最后一条 `kind='admin_kill'` 就是当前状态（`data.kill`）。
  为什么要落库而不是内存变量：容器重启后「我今天不想交易」必须还生效，否则一个部署就把
  最后一道人工闸门悄悄打开了。为什么不加表：不动 schema（T5 的迁移表只许追加已发布的
  语句），事件表本身就是审计流水。
* tick 时每轮重读：**环境变量 kill（`KILL_SWITCH`）与 DB 里的 admin kill，任一为真即生效**。

`flatten` 比 kill 强：kill 只是不开新仓，flatten 是「现在就出来」，所以它要二次确认
（`confirm=yes`），且每次调用（含被拒）都落 `admin_action`。

`dd/reset` 只能显式调用：`app.dd.DrawdownGuard.reset_peak()` 要求非空 `reason`、
拒绝把峰值降到 0（指数 0 = 账户打光或状态坏，抹平只会把爆仓藏起来），并且写两条事件
（`dd_reset` = 状态变更，`dd_reset_audit` = 谁调的、从多少改到多少），端点再加一条
`admin_action`。

---

## 6. 调度与心跳

* 周期：**UTC `:03` / `:33`**，即每小时第 3、33 分钟（`app.main.TICK_MINUTES`）。
  `:03` 是 4h 收盘 + 3 分钟（等交易所把 bar 落定）；`:33` 是同一根 bar 的复检 ——
  执行器按 `(symbol, bar_ms)` **幂等**，重跑只会产生 `skip`。两轮都跑而不是一轮，
  是因为「收盘那一刻交易所还没落定」比「多跑一次」贵得多。
* 实现：纯 `asyncio` 定时器（`TickScheduler`），**不新增依赖**（`apscheduler` 虽在
  requirements 里，但这里不需要它）。
* **一次失败不能杀死调度器**：`tick()` 兜住一切异常（含 runner 抛的、`events` 写不进的），
  记日志 + 落 `tick_failed` + 计数，然后继续下一轮。一个因为交易所 502 就永久停摆的
  调度器，比没有调度器更危险 —— 它看起来还在。
* **心跳**：每轮 tick 开始与结束各写一条 `heartbeat`（`kind` 与 T9
  `watchdog.default_heartbeat_probe()` 读的完全一致），`data` 带 `phase` / `status` /
  `runs` / `failures` / `error`。runner 崩了也写 —— 心跳证明的是「调度器活着」，
  不是「tick 成功了」（后者看事件流与状态条）。
* 没有凭据时（阶段一常态）：`_build_scheduler()` 返回**原因字符串**，记一条
  `admin_action(scheduler, disabled)` 审计，面板继续以只读方式提供服务。
  **调度器不会假装自己在交易。**

---

## 7. 已知边界 / 留给阶段二

| 项 | 现状 |
|---|---|
| `POST /admin/flatten` 的真实下单 | 已实现（`place_exit` + `cancel_all_orders`），但**阶段一没有凭据、未实测**；T13 演练时验证 |
| `强平≈` | 逐仓 5x 近似（`entry × (1 − 1/5)`），不含维持保证金；只作参考 |
| `/symbols` 的 K 线 | 公开接口 + 120s TTL 缓存；冷启动第一屏要等一次 HTTP |
| 面板自身的鉴权（只读页） | **不需要 token**（PLAN 的定位：token 保护三个写操作）。要隐藏页面请用反代 IP 白名单 |
| 每请求重连 DB | store 首次成功后缓存连接；失败不缓存（短暂锁能自愈） |
| 多进程 | 未考虑（单容器单进程；SQLite 的 `busy_timeout=5s` 覆盖面板读与 tick 写） |
