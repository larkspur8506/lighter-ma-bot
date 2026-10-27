# 策略移植说明（T2）

本文记录 `app/strategy.py` 的来源、为什么必须一字节不改、测试如何防止它漂移，
以及 `scripts/xvalidate.py` 相对旧版的差异。**验收判据**（PLAN.md T2）：
「同一组 K 线喂新旧两版，信号集合（bar_ms + 方向 + 止损价）**逐笔完全一致**」。

---

## 1. `app/strategy.py` —— 逐字复制，口径冻结

| 项 | 值 |
|---|---|
| 来源仓库 | `larkspur8506/crypto-ma-alert` |
| 来源文件 | `app/strategy.py` |
| 行数 | 528 |
| 字节数 | 25,962 |
| **sha256** | `868266752317212c24a8dcdd4937c775944d24035846ffba86805f34b22ece07` |

复制方式就是 `cp`，然后逐字节校验（sha256 + `cmp` 都要过）。**这里没有"合理的
重命名/加 docstring/格式化"** —— 一个字节不同就不是同一份回测口径了。

**为什么一字节不改**：这个模块是旧系统（HL 现役）唯一的信号口径来源，700 笔回测
与全部实盘成交都由它产生。改动任何一处（常数、比较符顺序、窗口口径）都会让
「回测证明有效」这句话不再成立，而且这种改动通常不会被代码评审发现 —— 它看起来
仍然"像"同一套策略。因此本项目的规则是：

> **信号口径属于 `app/strategy.py`；所有 Lighter 侧的改造只发生在信号之后**
> （sizing → 下单 → 对账 → 风控 → 展示）。

它是纯 stdlib（`__future__` / `math` / `dataclasses` / `typing`），没有第三方依赖，
回测脚本、dry-run 回放、线上 tick 三条路径 import 的是同一份文件。

---

## 2. `tests/test_strategy_port.py` —— 两条互相独立的断言

一个只 import 被测模块再和自己比对的测试什么都证明不了。所以这里从三个方向夹住它：

### ① 字节身份（`FrozenSourceTest`）

- sha256 与字节数硬编码为复制时的值 → 任何编辑（哪怕只是重排空格）立刻红。
- 额外用 `ast` 检查该模块的 import 全部属于 stdlib → 防止"顺手引入 pandas/numpy"，
  那会让三条路径看到不同的代码。

### ② 与独立参考实现的一致性（`ReferenceParityTest`）

`tests/reference_strategy.py` 是**另一份手写实现**（照规则文字重写，不是
`app/strategy.py` 的拷贝，也不 import 它）。同一组 bar 喂两个实现，逐笔比较
`(bar_index, bar_open_ms, side, entry, stop, stop_dist)`。覆盖：设计好的平台突破序列
（多空各一笔）、三个种子的随机游走（20/33/15 笔）、只多/只空、`short_regime_ma=0`
的开关、以及根数不足 376 时两边都必须零信号。

这条断言防的是**语义漂移**：把 250 根窗口改成 240、把 `<=` 改成 `<`、把止损从
"本根均线带"改成"上一根"，文件看上去仍然合理，但两版会在同一组 bar 上分叉。

### ③ 写死的数值快照（`FrozenSnapshotTest`）

具体到 (bar 下标, bar 开盘毫秒, 方向, 入场价, 止损价)：

```
平台序列： (420, 1606048000000, +1, 104.2, 100.03379333333334)
           (740, 1610656000000, -1,  95.8, 104.13085333333333)
```

再加一条**整份信号集合的 sha256**（`d524b786…` / `0635d963…`），把"每一笔的每一个
字段"都钉死，不必把 33 行贴进测试。

这条断言防的是 ②④ 都防不住的那种事故：**同时**改了 `app/strategy.py` 和参考实现，
两边一起错。数值快照是唯一还站着的那道墙。

数值的独立性：这批数字是复制前用**旧仓库原文件**跑出来的，并与 `tests/reference_strategy.py`
的输出逐笔对过（见 §4 的交叉验证输出），三者一致后才写进测试。此后不再从被测代码
重新生成。

### 附加的边界用例

`IndicatorTest` / `SqueezeFlagTest` / `ScanContractTest` 钉住易错细节：SMA 前 `n-1` 根为
`None` 而 EMA 从第 0 根就有值（首值播种）、`quantile` 的线性插值、`band` 直到第 120 根
才出现、密集标记要 **250 根窗口 + 连续 6 根**（首根可能为真的下标 = 254；有 `None`
前缀时顺延）、单根宽 bar 打断连击、`dense_run()` 往回数的口径、`scan()` 永不在最后一根
出信号（入场价是下一根开盘）、以及 `/why` 诊断的六步推导与 `evaluate_entry` 的返回值
必须对得上（`drift` 恒为 False）。

---

## 3. `scripts/xvalidate.py` —— 与旧版的差异

旧版把 `app/strategy.py` 与**回测引擎**放在同一份行情上对比，要求信号集合逐笔一致。
结构保留，两处替换：

| 维度 | 旧版 | 本仓库 |
|---|---|---|
| 数据源 | MEXC 合约（BTC/ETH）+ Binance 现货（SOL，历史更长） | **Lighter 4h K 线**：`GET /api/v1/candles?market_id=…&resolution=4h`，**翻页**（`end_timestamp = 已取到最早那根 t - 1`），500 根/次，回到 Lighter 首根 4h bar（2025-01-17T08:00Z） |
| market_id | 无此概念 | 运行时查 `orderBookDetails`（`market_type=perp` 且 `status=active`）；查不到才用文档化的兜底表 `{BTC:1, ETH:0, SOL:2}`，且 `--market-ids` 覆盖优先级最高。**不在调用点散落硬编码** |
| 回测引擎 | 用 `exec` 截断法从技能目录 `finance/strategy-backtest/scripts/ma_trend_backtest.py` 取 `squeeze_pct_flags` / `signals` | 仓库内 `scripts/backtest_engine.py`：同一套算术的 **stdlib 移植**（不 import pandas，也**不 import `app.strategy`** —— 共享指标函数会把要查的分叉藏起来） |
| 离线 | 必须联网 | `--bars-from-file FILE` 完全离线；`--cache-dir DIR` 命中 `<SYMBOL>.json` 就不发请求（首次拉取后自动落盘） |
| 未收盘 bar | 源不返回 | 默认剔除最后一根未收盘的（未收盘的 close 不是 close）；`--keep-open-bar` 保留 |

用法：

```bash
python3 scripts/xvalidate.py                                   # BTC/ETH/SOL，Lighter 全历史
python3 scripts/xvalidate.py --cache-dir .cache                # 有缓存就不联网
python3 scripts/xvalidate.py --bars-from-file bars.json        # 完全离线
python3 scripts/xvalidate.py --symbols BTC --keep-open-bar
python3 scripts/xvalidate.py --market-ids BTC=1,ETH=0,SOL=2    # 覆盖 market_id
```

退出码：`0` 全部一致；`1` 有差异或某个标的无法验证（含历史不足）；`2` 没有任何标的
走到验证（取数失败/空序列）。**一个只会打印"一致"的工具没有价值**，所以
`tests/test_xvalidate_offline.py` 里专门用假数据把"不一致"和"历史不足"两条路径也跑了一遍。

### 一个已知的一根 bar 边界差异（不是 bug，已量化）

旧 pandas 引擎喂进去的是 `dropna()` 后的 DataFrame，`raw[:win] = False` 因此把第一根
可用密集标记推到了 bar **374**；而线上规则在 bar **373** 就可能成立（`raw` 从 368 起算，
连续 6 根 → 373）。这个偏差只可能影响 bar 373 一根：实测 SOL 全历史上**确实有且只有
这一根**密集标记不同（`密集标记差异根数 = 1`），而它**没有产生任何信号**
（信号要求 `squeeze[i-1]`，bar 373 的密集标记只可能影响 bar 374 的信号资格）。
工具把这个差异数打出来而不是假装它不存在。

---

## 4. 本地实证（2026-10-27）

```
$ sha256sum app/strategy.py
868266752317212c24a8dcdd4937c775944d24035846ffba86805f34b22ece07  app/strategy.py
$ cmp app/strategy.py <旧仓库副本>   → 无输出（逐字节相同）

$ python -m unittest discover -s tests -t . -v
Ran 61 tests ... OK

$ python3 scripts/xvalidate.py --cache-dir .cache
=== BTC  3887 根  2025-01-17 08:00 → 2026-10-27 00:00 ===
  实盘引擎：多 203 笔 / 空 178 笔
  回测引擎：多 203 笔 / 空 178 笔
  仅实盘有: 0  仅回测有: 0
  共同信号 381 笔；入场价最大差 0.00e+00，止损价最大差 0.00e+00
  → ✅ 信号集合逐笔完全一致
=== ETH  3887 根 ...
  实盘引擎：多 243 笔 / 空 172 笔 / 回测引擎：多 243 笔 / 空 172 笔
  仅实盘有: 0  仅回测有: 0   共同信号 415 笔；入场价/止损价最大差 0.00e+00
  → ✅ 信号集合逐笔完全一致
=== SOL  3887 根 ...
  实盘引擎：多 270 笔 / 空 185 笔 / 回测引擎：多 270 笔 / 空 185 笔
  仅实盘有: 0  仅回测有: 0   共同信号 455 笔；入场价/止损价最大差 0.00e+00
  → ✅ 信号集合逐笔完全一致（密集标记差异 1 根，见上文边界说明）
总体: ✅ 实盘引擎与回测引擎口径一致（含做空）
```

合计 **1,251 笔信号**（BTC 381 / ETH 415 / SOL 455）逐笔一致，入场价与止损价的最大
差都是 0（不是"小于 1e-6"，是 0：两边算术一致，参考引擎也逐窗口重新求和）。

**历史深度**：Lighter 4h bar 共 3887 根（剔除未收盘那根），起点 2025-01-17 08:00Z，
需要的最低根数是 376（250 分位窗口 + 120 预热 + 6 连续），**深度充足**，不是勉强跑通。

> 注意：这里的 1,251 笔是「冻结规则的原始突破信号」（未过密集门槛的后置筛选），
> 与 PLAN.md §6 表里的 3,911 / 299 两个数字口径不同，不要混用。

---

## 5. 维护约定

1. **不要动 `app/strategy.py`。** 需要不同口径 → 在信号之后做，或者开新模块。
2. 若某天确实必须改（例如交易所 bar 定义变了），必须同时：更新 sha256 常量、重算
   数值快照、在本文顶部记一笔"口径变更 + 原因 + 生效日期"，并重跑 Lighter 全历史回测
   （T11 `scripts/backtest.py`）。
3. `tests/reference_strategy.py` 与 `scripts/backtest_engine.py` 都是**独立证人**，
   不允许为了"让测试变绿"去改它们；它们和 `app/strategy.py` 打架时，输的永远是后者
   的 **bug 报告**，不是把证人改口。
