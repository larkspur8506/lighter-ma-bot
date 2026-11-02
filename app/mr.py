"""MR（超跌反弹）腿的执行编排 — 挂在现有 tick 里跑，不新建进程/容器/凭据。

设计边界（2026-11-01 定稿）：

- **单槽**：同一时刻至多一笔持仓；持仓中忽略一切新信号。
- **仓位**：名义 = （账户权益 − MR 仓贡献）× 0.5；逐仓保证金 = 名义（1x）。
  权益读交易所 `total_asset_value`，MR 仓贡献 = allocated_margin + uPnL ——
  这样 crypto 腿自己的守卫口径（账户权益）天然**剔除**了 MR 的占用。
- **纯市价零挂单**：入场/离场都是 MARKET+IOC，`reduce_only` 只在离场为 1。
  绝不调用任何账户级撤单（`cancel_all_orders()` 会误伤 crypto 腿的止损单；
  `cancel_all(market_id)` 只在 mr market 上是安全的，但 MR 根本不挂单，用不到）。
- **幂等**：台账 `mr_trades.signal_date` 唯一约束 + 「只在空仓时入场」是真正的护栏；
  coi 按「当前 tick 分钟」派生（同一轮重放同键，跨轮重试换键——交易所会拒已消耗的 coi）。
- **成交只认交易所持仓**：`ok=True` 只代表 code=200 进了 sequencer（见 `OrderResult`
  docstring）。入场必须看到 mkt129 持仓出现才写 OPEN（数量/价格取交易所口径），
  离场必须看到持仓消失才写 CLOSED；确认窗口内没发生 → 不记账、下轮重试。
  下单前先写一条 `mr_entry_intent` / `mr_exit_intent` 事件：确认因读错误或进程重启
  丢失时，下一轮凭这条意图把交易所真实持仓补记进台账（只认同一 signal_date），
  避免「有仓无台账 → recon_err 冻结、要人工改库」。
- **对账 fail-closed**：本地台账与交易所 mkt129 持仓不一致（任一方向）→
  不做任何动作 + 事件告警，等人工。**不自动吃掉未知仓位**。
- **kill 语义**：全局 `/admin/kill` 与 MR 专属 `/admin/mr` 都只挡**新入场**，
  持仓照常按信号离场（和 crypto 腿一致——kill 不挡离场）。
- **基金级闸门（2026-11-02 第五轮）**：入场前还要过 crypto 腿那一份 `RiskState` +
  `RiskLimits` 里的**基金级**拒绝码（kill / 回撤熔断 / 当日亏损 / 状态不可用）。
  旧版只读 `admin_kill`，而 Lighter 的 DD 熔断只是 `check_entry` 的一个拒绝码、
  不写 kill 事件 ⇒ 熔断期间 MR 仍能开 0.5×权益的裸多（HL 侧因为 DD 会 `hl_set_kill`
  反而是挡住的）。标的级闸门（max_positions / symbol_notional）不套到 MR 上。
- **占用口径 = 价格敞口（名义）**，不是保证金：定仓基数 = 权益 − |数量 × 标记价|。
  同一个 MR 敞口在 5x 下保证金只有 1/5，按保证金扣会让 crypto 腿在 MR 持仓期间被放大。
- MR 默认停用上线：没有 `mr_admin` 事件 = 停用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from . import mr_signal
from .exchange import ExchangeClient, client_order_index
from .executor import AccountReader, ExchangeAccount
from .market import MarketClient
from .risk import RiskLimits
from .risk import check_entry as risk_check_entry
from .risk import dd_action as risk_dd_action
from .risk import drawdown as risk_drawdown
from .store import StateStore

logger = logging.getLogger(__name__)

#: MR 开关事件（最后一条为准；没有事件 = 停用）。
MR_EVENT_KIND = "mr_admin"
#: 全局 kill 事件的 kind（与 views.KILL_EVENT_KIND 同值；不 import views 防环）。
_KILL_EVENT_KIND = "admin_kill"
#: 基金级闸门里**只挡开仓、且与「账户整体风险」有关**的那几个码（2026-11-02 第五轮）。
#: `max_positions` / `symbol_notional` / `total_notional` 这类是 crypto 腿的标的级闸门，
#: MR 有独立单槽，套上去只会误杀。
_MR_FUND_BLOCKS = ("kill_switch", "state_unavailable", "daily_loss", "drawdown")
#: 对账不一致告警的最小间隔（秒）——同一问题不刷屏。
_RECON_ALERT_INTERVAL_S = 6 * 3600.0
#: 重复拒单/未成交告警的最小间隔（秒）。
_ORDER_ALERT_INTERVAL_S = 30 * 60.0
#: 下单意图事件（成交确认丢失时的认领凭据）：写在下单**之前**，每轮重试各一条。
_ENTRY_INTENT_KIND = "mr_entry_intent"
_EXIT_INTENT_KIND = "mr_exit_intent"



@dataclass(frozen=True)
class MrConfig:
    """MR 腿参数。改动 = 改这里 + 走 PR（参数面刻意最小）。"""

    symbol: str = "QQQ"
    market_id: int = 129                # Lighter QQQ 永续（0/0 费，实测 $1M/日）
    size_decimals: int = 4              # 最小 0.0075 QQQ
    price_decimals: int = 2
    exposure: float = 0.5               # 名义 = 权益 × 0.5
    min_base_qty: float = 0.0075
    min_quote_notional: float = 10.0    # 名义下限 $10
    leverage: float = 1.0               # 逐仓保证金 = 名义
    entry_slippage_bps: int = 15        # 最差价带宽（IOC 保护）
    exit_slippage_bps: int = 15
    candle_count: int = 500             # 1h×500 ≈ 21 天会话
    #: 成交确认：下单后轮询交易所持仓，确认「仓真的出现/真的消失」才记账。
    #: Lighter 的 `ok=True` 只代表 code=200 进了 sequencer（见 OrderResult docstring），
    #: 拿它当成交会写出假 OPEN/假 CLOSED —— crypto 腿已用同一套确认（PR #40）。
    fill_confirm_s: float = 12.0
    fill_poll_s: float = 1.0
    #: funding 基线（%名义/小时）。实测 2026-01-22~11-01 中位 0.0004%/h（3.5%/年）。
    funding_baseline_pct_per_h: float = 0.0004
    #: 自动停用门槛：实际 funding 超过基线的这个倍数，**且**已付金额超过
    #: `funding_disable_min_pct`（%名义）。2026-11-02 第四轮复核：旧值 2 倍落在正常
    #: 分布中间 —— 用实测费率复算 9 个月 12 笔回合有 7 笔触发（持仓时段均值
    #: 10.19%/年 vs 全史中位 3.5%/年）；10 倍 + 0.10% 绝对门槛在样本内 0/12 触发，
    #: 只拦真正的费率尖峰（年化 ≥35%）。
    funding_disable_multiple: float = 10.0
    funding_disable_min_pct: float = 0.10
    #: 下单意图的认领窗口（秒）：成交确认因读错误/进程重启而丢失时，下一轮可凭
    #: 这条意图把交易所真实持仓补记进台账，避免「有仓无台账 → recon_err 冻结」。
    adopt_window_s: float = 6 * 3600.0


@dataclass
class MrLeg:
    """MR 腿。tick 每轮调用一次 `on_tick()`；其余一律不碰。"""

    store: StateStore
    exchange: ExchangeClient
    reader: AccountReader
    market_client: MarketClient
    config: MrConfig = field(default_factory=MrConfig)
    clock: Callable[[], float] = time.time
    #: 告警出口（tick 侧接 executor 的清洗告警；测试注入假函数）。
    alert: Optional[Callable[[str], Awaitable[None]]] = None
    #: 本轮 crypto 腿的风控状态（`RiskState`，含 DD / 日亏 / 成交笔数口径）。
    #: `main._tick` 每轮传入 —— **基金级闸门（回撤熔断等）靠它**；缺失时只做 DD 兜底。
    risk_state: Optional[Any] = None
    #: 本轮生效的 `RiskLimits`（阈值随 risk_pct 档位联动，不能在这里另算一份）。
    limits: Optional[RiskLimits] = None
    _recon_bad_since: Optional[float] = None
    _recon_alerted_at: float = 0.0
    #: 订单类告警的去重时间戳（key → 上次发送时刻）；入场窗口内每轮重试不刷屏。
    _order_alerted_at: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # `_to_int` 硬编码 ×10000；size_decimals 若被改，台账 qty_int 与下单数量错位
        if self.config.size_decimals != 4:
            raise ValueError("MrConfig.size_decimals 固定为 4（_to_int 契约）")

    # ------------------------------------------------------------------ 读

    def _mr_enabled(self) -> bool:
        rows = self.store.events(kind=MR_EVENT_KIND, limit=1)
        if not rows:
            return False
        payload = _event_payload(rows[0])
        return bool(payload.get("enabled", False))

    def _global_kill(self) -> bool:
        rows = self.store.events(kind=_KILL_EVENT_KIND, limit=1)
        if not rows:
            return False
        payload = _event_payload(rows[0])
        return bool(payload.get("kill", False))   # 读不懂当「不知道」→ 不当开仓绿灯

    def _exchange_mr_position(self, account: ExchangeAccount):
        for pos in account.positions:
            if getattr(pos, "market_id", None) == self.config.market_id:
                return pos
        return None

    def _mr_position_contribution(self, pos: Any) -> float:
        """MR 仓从定仓基数里占走的部分 = **价格敞口（数量 × 标记价）**。

        2026-11-02 第五轮：原来是「allocated_margin + uPnL」，即**按保证金**扣。
        同一个 MR 敞口，1x 时扣满额、5x 时只扣 1/5 —— 扣多少取决于杠杆设置而不是
        风险大小（HL 侧 5x 就是这样，crypto 腿在 MR 持仓期间被放大 1.8 倍）。改成
        名义之后两所一致，也与「MR 不挂止损、它是敞口风险」的形状匹配。
        """
        value = getattr(pos, "position_value", None)
        if value is not None:
            return max(abs(float(value)), 0.0)
        allocated = getattr(pos, "allocated_margin", None)
        upnl = getattr(pos, "unrealized_pnl", None)
        if allocated is not None:
            return max(float(allocated) + (float(upnl) if upnl is not None else 0.0), 0.0)
        return 0.0

    def _fund_block(self, notional: float) -> str:
        """基金级开仓闸门（kill / 回撤熔断 / 当日亏损 / 状态不可用）→ 拒绝码或空串。

        2026-11-02 第五轮 P1：MR 原来只读 `admin_kill`，而 Lighter 的**回撤熔断**只是
        `risk.check_entry()` 的一个拒绝码、**不写 kill 事件** ⇒ 熔断期间 MR 仍能开
        0.5×权益的裸多（HL 侧相反：DD 命中会 `hl_set_kill(origin=dd)`，MR 被挡）。
        现在复用同一份 `check_entry`（同一 `RiskState` / 同一 `RiskLimits`），只白名单
        基金级那几个码；标的级闸门（max_positions / symbol_notional）不套到 MR 上。
        """
        limits = self.limits or RiskLimits()
        state = self.risk_state
        if state is not None:
            try:
                decision = risk_check_entry("QQQ", float(notional), state, limits)
            except Exception as exc:                          # noqa: BLE001
                logger.warning("mr: 基金级闸门异常（按不可用处理）：%s", exc)
                return "state_unavailable"
            for gate in decision.gates:
                code = gate.code or gate.gate
                if gate.blocked and code in _MR_FUND_BLOCKS:
                    return code
            return ""
        # 没拿到 RiskState（装配退化 / 单测）：只做 DD 兜底 —— 回撤是最不该被绕过的一条。
        try:
            row = self.store.dd_state() or {}
            dd = risk_drawdown(row.get("twr_index"), row.get("twr_peak"))
        except Exception as exc:                              # noqa: BLE001
            logger.warning("mr: 读 dd_state 失败（跳过 DD 闸门）：%s", exc)
            return ""
        if dd is None:
            return ""
        if risk_dd_action(dd, limits.risk_pct, limits.dd_warn_pct,
                          limits.dd_kill_pct) == "kill":
            return "drawdown"
        return ""

    async def _alert_once(self, message: str) -> None:
        logger.warning("mr: %s", message)
        self.store.log_event(MR_EVENT_KIND + "_alert", message)
        if self.alert is not None:
            try:
                await self.alert(message)
            except Exception as exc:                          # noqa: BLE001
                logger.warning("mr: alert delivery failed: %s", exc)

    async def _alert_throttled(self, key: str, message: str,
                               interval_s: float = _ORDER_ALERT_INTERVAL_S) -> None:
        """同一类订单问题在 `interval_s` 内只发一次（入场窗口每轮重试不刷屏）。"""
        now_s = self.clock()
        last = float(self._order_alerted_at.get(key, 0.0) or 0.0)
        if last and now_s - last < interval_s:
            logger.warning("mr: %s（同类告警已去重）", message)
            return
        self._order_alerted_at[key] = now_s
        await self._alert_once(message)

    async def _await_position(self, *, want: bool,
                              confirm_s: Optional[float] = None):
        """轮询交易所持仓，直到「出现」（want=True）或「消失」（want=False）。

        返回最后一次读到的 mkt129 持仓（None = 没有仓/没读到）。`ok=True` 只代表受理，
        **成交只认交易所持仓** —— 超时就按当前真相返回，由调用方决定记不记账。

        单次读失败（503/429/网络抖动）**不中断轮询**：确认窗口只有 12s，一次抖动就
        把整轮打崩会让「已成交但没有台账」变成对账冻结（MR 没有止损，裸仓更贵）。
        返回 ``(pos, read_ok)``：`read_ok=False` 表示整轮都没读到真相 —— 离场方
        必须按「未知」处理（绝不当成「仓已消失」写出假 CLOSED）。
        """
        cfg = self.config
        budget = float(cfg.fill_confirm_s if confirm_s is None else confirm_s)
        poll = max(float(cfg.fill_poll_s), 0.0)
        attempts = 1 if poll <= 0 or budget <= 0 else int(budget / poll) + 1
        pos = None
        read_ok = False
        started = time.monotonic()
        for i in range(max(attempts, 1)):
            try:
                account = await self.reader.read()
            except Exception as exc:                              # noqa: BLE001
                logger.warning("mr: 成交确认读账户失败（继续轮询）：%s: %s",
                               type(exc).__name__, exc)
            else:
                read_ok = True
                pos = self._exchange_mr_position(account)
                if (pos is not None) is bool(want):
                    return pos, True
            # 墙钟上限（2026-11-02 第五轮）：单次读可能因为重试/超时比自己久得多
            # （HL 侧一次失败读实测 ≈26s），固定 13 次会变成几分钟的阻塞 ——
            # 而 MR tick 是持着 `_tick_lock` 跑的，会顶住主 tick。
            elapsed = time.monotonic() - started
            if elapsed >= budget:
                logger.warning("mr: 成交确认超时（%.1fs ≥ %.1fs，%d 次读），按当前真相返回",
                               elapsed, budget, i + 1)
                break
            if i + 1 < attempts:
                await asyncio.sleep(max(min(poll, budget - elapsed), 0.0))
        return pos, read_ok

    # ------------------------------------------------------------------ 主流程

    async def on_tick(self, now_ms: Optional[int] = None,
                      account: Optional[ExchangeAccount] = None,
                      risk_state: Optional[Any] = None,
                      limits: Optional[RiskLimits] = None) -> dict:
        """每轮 tick 调一次。返回面板 status 块；异常不外抛（MR 挂了不能拖死 crypto 腿）。

        `risk_state`/`limits` 由 `main._tick` 从 crypto 腿那一轮带过来（同一份权益口径、
        同一套阈值）—— 基金级闸门（回撤熔断 / 当日亏损）就靠它们。
        """
        cfg = self.config
        self.risk_state = risk_state
        self.limits = limits
        status: dict = {"enabled": False, "kill": False, "state": "disabled",
                        "recon": "unknown", "error": "", "updated_at": self.clock(),
                        "fund_gate": "risk_state" if risk_state is not None else "dd_only"}
        try:
            status = await self._on_tick_inner(now_ms=now_ms, account=account,
                                               status=status)
        except Exception as exc:                              # noqa: BLE001
            logger.exception("mr: tick failed")
            status.update(state="error", error=f"{type(exc).__name__}: {exc}",
                          updated_at=self.clock())
            await self._alert_once(f"MR tick 异常：{type(exc).__name__}: {exc}")
        return status

    async def _on_tick_inner(self, *, now_ms: Optional[int], status: dict,
                             account: Optional[ExchangeAccount]) -> dict:
        cfg = self.config
        now = int(now_ms if now_ms is not None else int(self.clock() * 1000))
        enabled = self._mr_enabled()
        kill = self._global_kill()
        status.update(enabled=enabled, kill=kill)

        open_row = self.store.mr_open_trade()
        if account is None:
            account = await self.reader.read()
        pos = self._exchange_mr_position(account)

        # ---- 认领：确认窗口丢失的成交（读错误 / 进程重启）-------------------------
        # 有意图、无台账才认领；没有意图的未知仓仍然 fail-closed（不自动吃掉）。
        if open_row is None and pos is not None:
            if await self._adopt_entry(pos, now, status):
                open_row = self.store.mr_open_trade()
        if (open_row is not None and pos is None
                and not self._entry_is_fresh(open_row, now)):
            if await self._adopt_exit(open_row, now, status):
                open_row = None

        # ---- 对账（双向）：本地 OPEN ↔ 交易所持仓 --------------------------------
        holding = False
        recon_ok = True
        if open_row is not None and pos is None and self._entry_is_fresh(open_row, now):
            holding = True                                    # 刚下单，持仓行还没出现
        elif open_row is not None and pos is None:
            recon_ok = False
            await self._recon_fail("台账有 OPEN 仓但交易所没有 mkt129 持仓（人工核对）",
                                   status)
        elif open_row is None and pos is not None:
            recon_ok = False
            await self._recon_fail("交易所有 mkt129 持仓但台账没有 OPEN（人工核对，不自动吃）",
                                   status)
        elif open_row is not None and pos is not None:
            holding = True
        if recon_ok:
            self._recon_clear(status)

        if status.get("recon") == "err":
            status["state"] = "recon_err"
            return status
        if status.get("adopted") == "exit":
            return status            # 本轮刚补记完离场，不再评估新入场

        # 停用且空仓：连 K 线都不拉（省一次 API/tick）；有仓或启用才继续
        if not enabled and not holding:
            status["state"] = "disabled"
            return status

        # ---- 拉场所 1h K 线并聚合会话 --------------------------------------------
        rows = self.market_client.candles_1h(cfg.market_id, count_back=cfg.candle_count)
        if not rows:
            status.update(state="no_data", error="1h candles empty")
            await self._alert_once("MR 拿不到 1h K 线（空响应）")
            return status
        sessions, dropped = mr_signal.aggregate_sessions_ex(rows)
        self._note_dropped_sessions(dropped, now, status)
        if len(sessions) < 6:
            status.update(state="no_data", error=f"usable sessions={len(sessions)}")
            return status

        status["last_session_date"] = sessions[-1].date
        status["sessions_usable"] = len(sessions)

        # 注意：这里没有 `if not enabled: return` —— 停用只挡新入场（快路径在上方
        # 已拦「停用+空仓」），**停用+持仓必须照常评估离场**，否则仓位被永久困住。
        if not holding:
            await self._maybe_enter(now_ms=now, sessions=sessions, rows=rows,
                                    account=account, status=status)
        else:
            await self._maybe_exit(now_ms=now, sessions=sessions, rows=rows,
                                   open_row=open_row, pos=pos, status=status)
        return status

    # ------------------------------------------------------------------ 对账辅助

    def _entry_is_fresh(self, open_row: Mapping[str, Any], now_ms: int) -> bool:
        ts = open_row.get("entry_ts")
        if not ts:
            return False
        return (now_ms / 1000.0 - float(ts)) < 2 * 3600.0

    # ------------------------------------------------------------------ 认领

    def _recent_intent(self, kind: str, now_s: float):
        """最近一条「下单意图」事件 → (ts, payload)；超出认领窗口返回 None。"""
        for row in self.store.events(kind=kind, limit=5):
            ts = float(row.get("ts") or 0.0)
            if ts <= 0 or abs(now_s - ts) > float(self.config.adopt_window_s):
                continue
            payload = _event_payload(row)
            if payload.get("signal_date"):
                return ts, payload
        return None

    async def _adopt_entry(self, pos: Any, now_ms: int, status: dict) -> bool:
        """有意图、无台账、交易所有仓 → 按交易所口径补记 OPEN（不再要求人工改库）。"""
        found = self._recent_intent(_ENTRY_INTENT_KIND, now_ms / 1000.0)
        if found is None:
            return False
        ts, payload = found
        signal_date = str(payload.get("signal_date") or "")
        if not signal_date or self.store.mr_trade_by_signal(signal_date) is not None:
            return False
        qty = float(getattr(pos, "qty", 0) or 0)
        if getattr(pos, "side", 1) != 1:
            # MR 只做多：交易所这一格如果是空头（手工/其它系统开的），不按 MR 入场认领。
            logger.warning("mr: mkt129 是空头（side=%s），跳过入场认领", getattr(pos, "side", None))
            return False
        fill_px = getattr(pos, "avg_entry_price", None)
        entry_price = float(fill_px) if fill_px and float(fill_px) > 0 else float(payload.get("ref_price") or 0)
        if qty <= 0 or entry_price <= 0:
            return False
        notional = qty * entry_price
        trade_id = self.store.mr_record_entry(
            signal_date=signal_date,
            entry_session=str(payload.get("entry_session") or signal_date),
            qty=qty, qty_int=_to_int(qty), entry_price=entry_price,
            entry_order_id="", entry_ts=ts, notional=notional, margin=notional,
            equity_at_entry=payload.get("equity"),
            raw={"adopted": True, "adopt_reason": "fill_confirmation_lost",
                 "intent_ts": ts, "fill_source": "exchange_position"})
        status["state"] = "entered"
        status["adopted"] = "entry"
        self.store.log_event(
            "mr_entry_adopted",
            f"MR 入场成交确认丢失，按交易所持仓补记台账 #{trade_id}："
            f"QQQ {qty} @≈{entry_price:.2f}（信号 {signal_date}）",
            data={"signal_date": signal_date, "qty": qty, "entry_price": entry_price})
        await self._alert_once(
            f"MR 入场确认丢失，已按交易所持仓补记台账 #{trade_id}："
            f"QQQ {qty} @≈{entry_price:.2f}（信号 {signal_date}）")
        return True

    async def _adopt_exit(self, open_row: Mapping[str, Any], now_ms: int,
                          status: dict) -> bool:
        """有离场意图、台账 OPEN、交易所已无仓 → 按最近 1h 收盘补记 CLOSED。

        只认同一 signal_date 的离场意图（防止把外部平仓/手工干预吃成策略离场）。
        """
        found = self._recent_intent(_EXIT_INTENT_KIND, now_ms / 1000.0)
        if found is None:
            return False
        ts, payload = found
        if str(payload.get("signal_date")) != str(open_row.get("signal_date")):
            return False
        try:
            rows = self.market_client.candles_1h(self.config.market_id, count_back=5)
        except Exception as exc:                              # noqa: BLE001
            logger.warning("mr: 认领离场时取价失败（下轮再试）：%s", exc)
            return False
        if not rows:
            return False
        px = float(rows[-1]["c"])
        qty = float(open_row.get("qty") or 0)
        entry_price = float(open_row.get("entry_price") or 0)
        if px <= 0 or qty <= 0:
            return False
        net_pnl = (px - entry_price) * qty
        self.store.mr_close_trade(
            int(open_row["id"]), exit_price=px, exit_order_id="",
            exit_ts=now_ms / 1000.0, exit_reason="sma5_adopted",
            exit_signal_date=(str(payload.get("exit_signal_date") or "") or None),
            funding_pct=None, net_pnl=net_pnl,
            raw={"adopted": True, "price_source": "last_1h_close", "intent_ts": ts})
        status["state"] = "exited"
        status["adopted"] = "exit"
        self.store.log_event(
            "mr_exit_adopted",
            f"MR 离场成交确认丢失，交易所已空仓 → 按最近收盘 {px:.2f} 补记 CLOSED"
            f"（信号 {open_row.get('signal_date')}）",
            data={"signal_date": open_row.get("signal_date"), "exit_price": px})
        await self._alert_once(
            f"MR 离场确认丢失，交易所已空仓 → 按最近收盘 {px:.2f} 补记 CLOSED"
            f"（台账 #{open_row.get('id')}，净 {net_pnl:+.2f} USDC，价格为估算）")
        return True

    async def _recon_fail(self, message: str, status: dict) -> None:
        status["recon"] = "err"
        now_s = self.clock()
        if self._recon_bad_since is None:
            self._recon_bad_since = now_s
        if now_s - self._recon_alerted_at >= _RECON_ALERT_INTERVAL_S:
            self._recon_alerted_at = now_s
            await self._alert_once(f"MR 对账不平：{message}")
        logger.error("mr recon: %s", message)

    def _recon_clear(self, status: dict) -> None:
        status["recon"] = "ok"
        self._recon_bad_since = None

    def _note_dropped_sessions(self, dropped, now_ms: int, status: dict) -> None:
        """「整个会话因数据不足被弃用」→ 落一条审计事件（同一会话只报一次）。

        2026-11-02 第五轮 P2：末根闸把「用错收盘价」换成了「整天静默丢弃」——
        信号日整天消失、连事件都没有。这里只报**已收盘**的会话（当天盘中的半截
        会话是正常状态，不报），也不推送（避免把数据抖动变成告警噪音）。
        """
        if not dropped:
            return
        tz = ZoneInfo(mr_signal.SESSION_TZ)
        reported = []
        for item in dropped[-5:]:
            try:
                close_ms = int(datetime.strptime(str(item["date"]), "%Y-%m-%d")
                               .replace(hour=16, minute=0, tzinfo=tz).timestamp() * 1000)
            except (TypeError, ValueError):
                continue
            if close_ms + 3600_000 > now_ms:                  # 还没收盘：正常
                continue
            key = "session_dropped:" + str(item["date"])
            if self._order_alerted_at.get(key):
                continue
            self._order_alerted_at[key] = now_ms / 1000.0
            reported.append(item)
            self.store.log_event(
                "mr_session_dropped",
                f"MR 会话 {item['date']} 数据不足已弃用（bars={item['bars']}、"
                f"末根 {item['last_bar_et']} ET、{item['reason']}）",
                data=item)
        if reported:
            status["dropped_sessions"] = [str(r["date"]) for r in reported]

    def _enabled_since(self) -> float:
        """最近一次「启用」事件的时刻（0 = 从未启用）。给 `_mark_expired_signal` 去噪。"""
        try:
            rows = self.store.events(kind=MR_EVENT_KIND, limit=5)
        except Exception:                                     # noqa: BLE001
            return 0.0
        for row in rows:
            if _event_payload(row).get("enabled"):
                return float(row.get("ts") or 0.0)
        return 0.0

    # ------------------------------------------------------------------ 入场

    async def _maybe_enter(self, *, now_ms: int, sessions, rows, account: ExchangeAccount,
                           status: dict) -> None:
        cfg = self.config
        if status.get("kill"):
            status["state"] = "flat"
            return
        decision = mr_signal.entry_decision(sessions=sessions, now_ms=now_ms, flat=True)
        status["state"] = "flat"
        if decision is None:
            self._mark_expired_signal(sessions=sessions, now_ms=now_ms)
            return
        signal_date = decision["signal_date"]
        if self.store.mr_trade_by_signal(signal_date) is not None:
            return                                           # 该信号已有台账（CLOSED/SKIPPED）

        last_close = float(rows[-1]["c"])
        equity = account.equity
        if equity is None or equity <= 0:
            status["state"] = "no_equity"
            return
        notional = equity * cfg.exposure
        qty = int(notional / last_close * (10 ** cfg.size_decimals)) / (10 ** cfg.size_decimals)
        if qty < cfg.min_base_qty or notional < cfg.min_quote_notional:
            reason = (f"权益不足：equity={equity:.2f} notional={notional:.2f} "
                      f"qty={qty}（min {cfg.min_base_qty} QQQ / ${cfg.min_quote_notional}）")
            self.store.mr_mark_skipped(signal_date, reason)
            await self._alert_once(f"MR 信号 {signal_date} 放弃：{reason}")
            return
        if notional > equity:
            self.store.mr_mark_skipped(signal_date, "notional > equity（保证金不足保护）")
            return

        block = self._fund_block(notional)
        if block:
            # 基金级闸门（回撤熔断 / 当日亏损 / kill）：**不写 SKIPPED** —— 信号本身
            # 还有效，熔断解除后的下一个 tick 仍可入场（写 SKIPPED 会把它永久作废）。
            status.update(state=f"blocked_{block}", error=block)
            await self._alert_throttled(
                f"fund_block_{block}",
                f"MR 入场被基金级风控挡住（{block}）—— 本轮不入场，信号保留到下次 tick",
                interval_s=6 * 3600.0)
            return

        coi = _attempt_coi(cfg.symbol, "mr_entry", now_ms)
        try:
            await self.exchange.ensure_isolated(market_id=cfg.market_id,
                                                leverage=cfg.leverage)
        except Exception as exc:                              # noqa: BLE001 - 前置失败不下单
            status.update(state="margin_mode_failed",
                          error=f"{type(exc).__name__}: {exc}")
            await self._alert_throttled(
                "entry_margin", f"MR 入场前置失败（保证金模式）：{type(exc).__name__}: {exc}")
            return
        self.store.log_event(
            _ENTRY_INTENT_KIND,
            f"MR 入场下单意图：QQQ {qty} @≈{last_close:.2f}（信号 {signal_date}）",
            data={"signal_date": signal_date,
                  "entry_session": decision["entry_session_date"],
                  "planned_qty": qty, "ref_price": last_close,
                  "equity": equity, "coi": coi}, ts=now_ms / 1000.0)
        result = await self.exchange.place_market(
            symbol=cfg.symbol, market_id=cfg.market_id, qty_int=_to_int(qty),
            price_decimals=cfg.price_decimals, ref_price=last_close, is_entry=True,
            slippage_bps=cfg.entry_slippage_bps, client_order_index=coi)
        if not getattr(result, "ok", False):
            # 下单被拒：不写台账（下轮同信号重试 / 对账裁决），只告警
            status.update(state="order_rejected",
                          error=str(getattr(result, "error", "")
                                    or getattr(result, "kind", "") or "unknown"))
            await self._alert_throttled(
                "entry_rejected",
                f"MR 入场单被拒（不下台账，下轮重试）：{status['error'][:200]}")
            return
        # 「受理」不等于「成交」：必须看到交易所真的有 mkt129 持仓才记账（PR #40 同一课）
        filled, _seen = await self._await_position(want=True)
        if filled is None:
            status.update(state="entry_not_filled",
                          error="受理但确认窗口内没有持仓（IOC 零成交）")
            await self._alert_throttled(
                "entry_not_filled",
                "MR 入场单已受理但交易所没有出现持仓（IOC 零成交）——不写台账，下轮重试")
            return
        filled_qty = float(getattr(filled, "qty", 0) or 0)
        if filled_qty <= 0:
            status.update(state="entry_not_filled", error="交易所持仓数量为 0")
            return
        fill_px = getattr(filled, "avg_entry_price", None)
        entry_price = float(fill_px) if fill_px and float(fill_px) > 0 else float(last_close)
        actual_notional = filled_qty * entry_price
        trade_id = self.store.mr_record_entry(
            signal_date=signal_date, entry_session=decision["entry_session_date"],
            qty=filled_qty, qty_int=_to_int(filled_qty), entry_price=entry_price,
            entry_order_id=str(getattr(result, "order_id", "") or ""),
            entry_ts=now_ms / 1000.0, notional=actual_notional,
            margin=actual_notional, equity_at_entry=equity,
            raw={"ref_price": last_close, "planned_qty": qty,
                 "filled_qty": filled_qty, "fill_source": "exchange_position",
                 "result": _result_brief(result)})
        if abs(filled_qty - qty) > 1e-9:
            self.store.log_event("mr_entry_partial_fill",
                                 f"MR 入场部分成交：计划 {qty} / 实际 {filled_qty}",
                                 data={"signal_date": signal_date, "planned": qty,
                                       "filled": filled_qty})
        status.update(state="entered", last_fill_at=now_ms / 1000.0)
        await self._alert_once(
            f"MR 入场：QQQ {filled_qty} @≈{entry_price:.2f}（名义 {actual_notional:.2f}，"
            f"信号 {signal_date}），台账 #{trade_id}")

    def _mark_expired_signal(self, *, sessions, now_ms: int) -> None:
        """信号日已整体错过（含「当天窗口已过」）却没有台账 → 记 SKIPPED，可审计。

        旧实现要求 `entry_day < today`，所以当天 12:30 之后才恢复运行的信号
        （entry_day == today 但 deadline 已过）永远不会有审计行。现在按
        **入场截止时刻**判定，并回看最近 5 个会话（覆盖多日停机）。
        """
        sigs = mr_signal.session_signals(sessions)
        k = mr_signal.completed_upto(sessions, now_ms)
        tz = ZoneInfo(mr_signal.SESSION_TZ)
        enabled_since = self._enabled_since()
        for i in range(max(k - 5, 0), k):
            if not sigs[i]["entry"]:
                continue
            s = sessions[i]
            entry_day = mr_signal.next_business_day(s.date)
            open_ms = int(datetime.strptime(entry_day, "%Y-%m-%d")
                          .replace(hour=9, minute=30, tzinfo=tz).timestamp() * 1000)
            deadline_ms = open_ms + int(mr_signal.ENTRY_WINDOW_H * 3600_000)
            if now_ms <= deadline_ms:
                continue
            if now_ms - deadline_ms > 14 * 86400_000:      # 太老的信号不追溯
                continue
            if enabled_since and deadline_ms / 1000.0 < enabled_since:
                # 那条信号是在腿**停用期间**过期的 —— 不是「启用后错过」，别补 SKIPPED
                # 刷噪音（2026-11-02 第五轮）。
                continue
            if self.store.mr_trade_by_signal(s.date) is None:
                self.store.mr_mark_skipped(s.date, "窗口错过（停机/延迟）")

    # ------------------------------------------------------------------ 离场

    async def _maybe_exit(self, *, now_ms: int, sessions, rows, open_row, pos,
                          status: dict) -> None:
        cfg = self.config
        entry_session = str(open_row.get("entry_session") or open_row.get("signal_date"))
        exit_date = mr_signal.exit_decision(sessions=sessions, now_ms=now_ms,
                                            entry_session_date=entry_session)
        status["state"] = "holding"
        status["pos"] = {
            "signal_date": open_row.get("signal_date"),
            "entry_session": entry_session,
            "qty": open_row.get("qty"),
            "entry_price": open_row.get("entry_price"),
            "notional": open_row.get("notional"),
            "upnl": getattr(pos, "unrealized_pnl", None),
            "funding_paid": getattr(pos, "funding_paid", None),
        }
        if exit_date is None:
            return
        if pos is None:
            # 台账有 OPEN 但交易所还没看到仓（刚下单的宽限窗口）——没有仓可平
            status["state"] = "holding"
            return
        last_close = float(rows[-1]["c"])
        # 数量优先取交易所实际持仓（部分成交时台账 qty 会偏大 → 超量平仓被拒）
        pos_qty = getattr(pos, "qty", None)
        exit_qty_int = (int(round(float(pos_qty) * 10000)) if pos_qty is not None
                        else int(open_row["qty_int"]))
        coi = _attempt_coi(cfg.symbol, "mr_exit", now_ms)
        self.store.log_event(
            _EXIT_INTENT_KIND,
            f"MR 离场下单意图：QQQ {exit_qty_int}（信号日 {open_row.get('signal_date')}）",
            data={"signal_date": open_row.get("signal_date"),
                  "exit_signal_date": exit_date, "qty_int": exit_qty_int,
                  "ref_price": last_close, "coi": coi}, ts=now_ms / 1000.0)
        result = await self.exchange.place_market(
            symbol=cfg.symbol, market_id=cfg.market_id,
            qty_int=exit_qty_int,
            price_decimals=cfg.price_decimals, ref_price=last_close, is_entry=False,
            slippage_bps=cfg.exit_slippage_bps, client_order_index=coi)
        if not getattr(result, "ok", False):
            # 平仓被拒：不写 CLOSED（保持 holding，下轮重试）；若信号窗口翻回去，
            # 仓位会留着并持续告警——比写假平仓进台账安全
            status.update(state="order_rejected",
                          error=str(getattr(result, "error", "")
                                    or getattr(result, "kind", "") or "unknown"))
            await self._alert_throttled(
                "exit_rejected", f"MR 离场被拒（下轮重试）：{status['error'][:200]}")
            return
        # 受理 ≠ 成交：必须看到交易所的仓真的没了才关账 —— 否则「台账 CLOSED + 裸多仓」
        left, left_seen = await self._await_position(want=False)
        if left is not None or not left_seen:
            if left is not None and left_seen:
                # 部分成交：先把这个已平掉的片段入账。否则最终决算只按**剩余**数量算
                # 价差，前面平掉那一截的盈亏就丢了（2026-11-02 第五轮 P3）。
                prev_qty = float(getattr(pos, "qty", 0) or 0)
                left_qty = float(getattr(left, "qty", 0) or 0)
                filled_qty = prev_qty - left_qty
                if filled_qty > 1e-12:
                    self.store.mr_trade_append_partial(
                        int(open_row["id"]), qty=filled_qty,
                        price=last_close, ts=now_ms / 1000.0)
                    self.store.log_event(
                        "mr_exit_partial",
                        f"MR 离场部分成交 {filled_qty:g}（剩余 {left_qty:g}）—— "
                        f"该片段按参考价 {last_close:.2f} 记账",
                        data={"signal_date": open_row.get("signal_date"),
                              "filled": filled_qty, "left": left_qty,
                              "ref_price": last_close})
            status.update(state="exit_not_filled",
                          error=f"确认窗口内仍有持仓 {getattr(left, 'qty', '?')}")
            await self._alert_throttled(
                "exit_not_filled",
                "MR 离场单已受理但交易所仍有持仓或读不到持仓（IOC 零成交/部分成交/"
                "确认读失败）——台账保持 OPEN，下轮重试")
            return
        funding_paid = getattr(pos, "funding_paid", None)
        notional = float(open_row.get("notional") or 0.0)
        funding_pct = (float(funding_paid) / notional * 100.0
                       if funding_paid is not None and notional > 0 else None)
        entry_price = float(open_row.get("entry_price") or 0.0)
        qty = float(pos_qty) if pos_qty is not None else float(open_row.get("qty") or 0.0)
        partials = _partial_exits(open_row)
        partial_pnl = sum((float(p.get("price") or 0.0) - entry_price)
                          * float(p.get("qty") or 0.0) for p in partials)
        net_pnl = ((last_close - entry_price) * qty
                   - (float(funding_paid) if funding_paid is not None else 0.0)
                   + partial_pnl)
        self.store.mr_close_trade(
            int(open_row["id"]), exit_price=last_close,
            exit_order_id=str(getattr(result, "order_id", "") or ""),
            exit_ts=now_ms / 1000.0, exit_reason="sma5", exit_signal_date=exit_date,
            funding_pct=funding_pct, net_pnl=net_pnl,
            raw={"ref_price": last_close, "result": _result_brief(result),
                 "partial_exits": partials, "partial_pnl": partial_pnl})
        # 即时止损触发器：实际付出 funding 超基线 10 倍且绝对额 >0.10% 名义 → 自动停用
        # （2026-11-02 第四轮复核：旧值 2 倍会在多数正常交易后自停 —— 实测 12 笔回合
        # 有 7 笔触发；10 倍 + 绝对门槛在样本内 0/12 触发，只拦真正的费率尖峰）
        if funding_pct is not None and funding_pct > 0:
            hours = max(now_ms / 1000.0 - float(open_row.get("entry_ts") or 0.0),
                        0.0) / 3600.0
            expected = cfg.funding_baseline_pct_per_h * hours
            if (expected > 0
                    and funding_pct > cfg.funding_disable_multiple * expected
                    and funding_pct > cfg.funding_disable_min_pct):
                reason = (f"funding {funding_pct:.3f}% 超基线 "
                          f"{cfg.funding_disable_multiple:g} 倍"
                          f"（预期 {expected:.3f}%，持仓 {hours:.1f}h）")
                self.store.log_event(MR_EVENT_KIND, f"MR 腿自动停用：{reason}",
                                     data={"enabled": False, "by": "auto",
                                           "reason": reason})
                await self._alert_once(f"MR 腿自动停用：{reason}")
        status.update(state="exited", last_fill_at=now_ms / 1000.0)
        await self._alert_once(
            f"MR 离场：QQQ 平多 @≈{last_close:.2f}（净 {net_pnl:+.2f} USDC，"
            f"funding {funding_pct if funding_pct is not None else '?'}%），"
            f"信号日 {exit_date}")


# ------------------------------------------------------------------ 工具

def _partial_exits(row: Mapping[str, Any]) -> list:
    """台账 `raw.partial_exits`（部分成交片段）→ list；解析失败返回 []。"""
    raw = row.get("raw") if isinstance(row, Mapping) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, Mapping):
        return []
    parts = raw.get("partial_exits")
    if not isinstance(parts, list):
        return []
    return [dict(p) for p in parts if isinstance(p, Mapping)]


def _event_payload(row: Mapping[str, Any]) -> dict:
    """事件行的 data 列（JSON 字符串或 dict）→ dict；解析失败返回 {}。"""
    data = row.get("data")
    if isinstance(data, Mapping):
        return dict(data)
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except ValueError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _attempt_coi(symbol: str, purpose: str, now_ms: int) -> int:
    """按「当前 tick 分钟」派生 coi：同一轮重放同键（交易所侧去重），跨轮重试自然换键。

    为什么不再用 signal_date 当键：交易所对**已消耗**的 coi 会拒单，而 MR 的重试
    （零成交/拒单/部分成交）必然跨 tick —— 固定键会让重试永远被 duplicate 拒掉。
    真正的幂等保护是台账 `mr_trades.signal_date` 唯一约束 + 「只在空仓时入场」。
    """
    return client_order_index(symbol, max(int(now_ms) // 60_000, 1), purpose)



def _to_int(qty: float) -> int:
    """qty（浮点，size_decimals 已取整）→ 整数 base 单位。"""
    return int(round(qty * 10000))


def _result_brief(result: Any) -> dict:
    out: dict = {}
    for key in ("order_id", "accepted", "status", "submit_id"):
        val = getattr(result, key, None)
        if val is not None:
            out[key] = val if isinstance(val, (int, float, bool, str)) else str(val)
    return out
