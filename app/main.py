"""HTTP 入口：监控面板 + JSON API + 三个管理写操作（T10）。

========================================================================
这个模块做什么
========================================================================

* **只读端点**（永远 200，永远不抛）：`/health` `/status` `/positions` `/stops`
  `/pnl` `/symbols` `/events` `/`（自包含 HTML 页）。
* **三个写操作**（`ADMIN_TOKEN` 鉴权）：`POST /admin/kill` `/admin/flatten`
  `/admin/dd/reset`；另有一个只读的 `GET /admin/state`（写面板的现状，同样要 token）。
* **调度**：4h 收盘后 tick（UTC `:03` / `:33`），每轮写心跳（T9 watchdog 读它）。

========================================================================
import 边界（评审会看这一条）
========================================================================

模块级 import 里**没有** `app.exchange`、没有 `app.executor`、没有 `lighter`：
本模块只 `import app.views`（面板）、`app.dd`（回撤）、`app.risk`、`app.watchdog`、
`app.store`。也就是说：一个没有凭据的部署（面板 / 演练 / 预览）能把这个进程完整跑
起来，而**私钥路径根本不在 import 图里**。

会碰到 `app.exchange` 的只有两处，import 都写在函数体内部、只在运行时按需加载：
`POST /admin/flatten` 的默认实现（:func:`_default_flatten`）与调度器的 tick runner
（:func:`default_tick_runner`）。这是刻意的：**读面板永远不加载私钥模块**。

========================================================================
运行依赖：只读面板 vs 下单
========================================================================

两件事，两个前置条件，**不许混为一谈**：

* **只读面板 + admin 审计** 只需要 **DB 位置**（`DB_URL=sqlite:///<path>` 或
  `DB_PATH=<path>`）。:func:`default_store_factory` 只看这两项 —— 它不调
  `app.config.Config.from_env()`，因为 `StateStore.from_config()` 只鸭子类型读
  `db_url` / `db_path`，从来不需要凭据。
* **下单 / 调度器** 才需要 Lighter 凭据（`LIGHTER_ACCOUNT_INDEX` 等）+ `ADMIN_TOKEN`。
  缺它们时：`/admin/flatten` 返回 `ok=false` + 原因（但**仍然落审计**），调度器不启动；
  只读端点与 admin 审计照常工作。

状态条把这两件事分开报：拿不到 DB 才说「数据库不可用」；缺凭据说的是
「交易凭据未配置（只读面板不受影响）」。混成一句话会让运维去查库，而真正缺的是私钥。

面板的私有读数（权益 / 挂单 / 现价）由**注入的 provider**提供（:class:`PanelCache`，
由 tick 填充），`app.views` 只拿到已经取好的普通 dict —— 它自己没有能力调用交易所。

========================================================================
鉴权的权衡（token 在 URL 里）
========================================================================

PLAN 规定 `?k=<token>`。照做的代价：**URL 会进访问日志**（uvicorn、1Panel/nginx 的
access log、浏览器历史、Referer）。所以这里：

* **同时**接受 `X-Admin-Token` 头（推荐；头不进 URL，日志里留不下）；
* token 用 `hmac.compare_digest` 比较（定长比较，不泄漏前缀）；
* token **绝不**出现在任何日志 / 事件 / 响应里 —— 审计事件只记 `endpoint`/`result`/`by`，
  不记 request line（`_audit()` 的 data 是白名单字段，不是整个 request）；
* `ADMIN_TOKEN` 为空 = **拒绝所有写操作**（fail-closed，不是"没设密码就放开"）。

========================================================================
降级（验收项：绝不 500）
========================================================================

读端点全部走 `app.views.Panel`，它是**逐区块** try/except 的：DB 打不开 / tick 还没跑 /
provider 抛异常 → 0 值 + WARNING + 200。写端点在 DB 不可用时返回
`200 {"ok": false, "degraded": true, "warnings": [...]}`；只有**鉴权失败**（401）与
**缺少二次确认/理由**（400）是 4xx —— 那两条是客户端错误，不是系统故障。
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import views, watchdog
from app.dd import DdError, DrawdownGuard
from app.risk import RiskLimits
from app.store import StateStore

__all__ = [
    "ADMIN_TOKEN_ENV", "CONFIRM_VALUES", "CREDENTIAL_ENV_KEYS", "DB_PATH_ENV", "DB_URL_ENV",
    "PanelCache", "SQLITE_URL_PREFIX", "StorePathError", "TICK_MINUTES", "TickScheduler",
    "VERSION", "create_app", "credential_status", "default_store_factory",
    "default_tick_runner", "next_tick_at", "record_tick_outcomes", "resolve_store_path",
]

logger = logging.getLogger(__name__)

SERVICE = "lighter-ma-bot"
VERSION = "0.2.0"

_STARTED_AT = time.time()

ADMIN_TOKEN_ENV = "ADMIN_TOKEN"
ADMIN_TOKEN_HEADER = "X-Admin-Token"
ADMIN_TOKEN_QUERY = "k"

#: `flatten` 的二次确认值（大小写不敏感）。刻意**不接受**空字符串或随便一个词：
#: 不可撤销操作必须有一句话真的说出口。
CONFIRM_VALUES = frozenset({"yes", "y", "1", "true", "confirm", "flatten"})

#: 4h bar 收盘后 tick 的分钟位（UTC）：`:03`（收盘 +3 分钟，等交易所把 bar 落定）
#: 与 `:33`（同一根 bar 的复检 —— 执行器按 `(symbol, bar_ms)` 幂等，重跑只会产生 skip）。
TICK_MINUTES: Tuple[int, ...] = (3, 33)

HEARTBEAT_KIND = watchdog.HEARTBEAT_KIND


# ================================================================== 契约：DB 路径
#
# 这里刻意**只**看 DB 位置，不看交易凭据。曾经的实现是
# `default_store_factory()` → `Config.from_env()` → `StateStore.from_config()`，
# 于是「没配私钥」被当成了「没有数据库」：面板 6 个区块全部降级、admin 写操作不落
# 审计、状态条报「数据库不可用」。而 `StateStore.from_config()` 本身只鸭子类型读
# `db_url` / `db_path` 两个属性 —— 它从来不需要凭据。建库与配凭据是两件事。

DB_URL_ENV = "DB_URL"
DB_PATH_ENV = "DB_PATH"
SQLITE_URL_PREFIX = "sqlite:///"

#: 交易凭据的 env 键（`app.config.REQUIRED_KEYS` 的**镜像**；`tests/test_main.py`
#: 有一条断言钉住两者相等，所以这里不会悄悄漂移）。刻意**不** import `app.config`：
#: 面板的 import 图里连配置层都不需要 —— 这里只是几个字符串，读的是 `os.environ`。
CREDENTIAL_ENV_KEYS: Tuple[str, ...] = (
    "LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX", "LIGHTER_API_PRIVATE_KEY",
    "LIGHTER_L1_ADDRESS", ADMIN_TOKEN_ENV,
)


class StorePathError(RuntimeError):
    """DB 位置缺失或形式不对。**与交易凭据无关** —— 建库只看 `DB_URL` / `DB_PATH`。"""


def _env_text(source: Mapping[str, str], key: str) -> str:
    return str(source.get(key) or "").strip()


class _StorePathConfig:
    """递给 `StateStore.from_config()` 的鸭子类型配置（只有它读的两个属性）。

    为什么不直接把 `app.config.Config` 递进去：`Config.from_env()` 要求**全部交易
    凭据**，而建库只需要一个路径。把这两件事耦合起来，代价是「没配私钥 ⇒ 没有数据库」，
    即一个只想读库（面板 / 审计 / 演练）的部署连一条 event 都写不进去。
    """

    __slots__ = ("db_url", "db_path")

    def __init__(self, *, db_url: str = "", db_path: str = "") -> None:
        self.db_url = db_url
        self.db_path = db_path


def resolve_store_path(env: Optional[Mapping[str, str]] = None) -> _StorePathConfig:
    """按 env 解析 DB 位置。优先级：**`DB_URL` > `DB_PATH`**；两者都没有 ⇒ 抛错。

    * `DB_URL` 必须是 `sqlite:///<path>`。别的 scheme（`postgresql://` 等）
      **fail-closed 抛错**，绝不悄悄退回 `DB_PATH` —— 那会让「以为数据在 postgres」
      的部署把审计写进一个本地文件，等到要查「谁在什么时候清过仓」才发现库是空的。
    * 两者都没有 ⇒ 抛 :class:`StorePathError`，消息里**点名**要设哪个变量。
      刻意不用 `./data/state.db` 之类默认值兜底：容器里 cwd 不对时库会落在没人备份的
      角落，而面板看起来一切正常 —— 这正是最贵的一种「正常」。
    """
    source = os.environ if env is None else env
    db_url = _env_text(source, DB_URL_ENV)
    db_path = _env_text(source, DB_PATH_ENV)

    if db_url:
        if not db_url.startswith(SQLITE_URL_PREFIX):
            scheme = db_url.split("://", 1)[0] if "://" in db_url else db_url
            raise StorePathError(
                f"{DB_URL_ENV} 只支持 {SQLITE_URL_PREFIX}<path>（fail-closed，"
                f"不会退回 {DB_PATH_ENV}）；拿到的是 {scheme}://")
        if not db_url[len(SQLITE_URL_PREFIX):].strip():
            raise StorePathError(
                f"{DB_URL_ENV} 缺少路径：应为 {SQLITE_URL_PREFIX}<path> 或改用 {DB_PATH_ENV}")
        return _StorePathConfig(db_url=db_url, db_path=db_path)

    if db_path:
        return _StorePathConfig(db_url="", db_path=db_path)

    raise StorePathError(
        f"没有数据库位置：必须设置 {DB_URL_ENV}={SQLITE_URL_PREFIX}<path> 或 "
        f"{DB_PATH_ENV}=<path>（只读面板只需要这一项，不需要 Lighter 凭据）")


def credential_status(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """交易凭据的状态（**给状态条用**，不用于任何鉴权决定）。

    与「数据库不可用」分开表达的理由：两者的后果完全不同。没有 DB = 面板本身瞎了；
    没有凭据 = 面板看得见、但下单/调度器起不来。混成一句话会让运维去查库，而真正
    缺的是私钥。
    """
    source = os.environ if env is None else env
    missing = [key for key in CREDENTIAL_ENV_KEYS if not _env_text(source, key)]
    # 真钱模式的第二道门：`LIGHTER_LIVE_TRADING=1` 但 `LIGHTER_LIVE_ACCOUNT_INDEX`
    # 没设时，**所有真实下单都会被 LiveGate 拒绝**，而面板/心跳看起来一切正常
    # （2026-10-28 实测踩到：.env 里两行粘在一起，`LIVE_ACCOUNT_INDEX` 被吞进
    # `LIGHTER_LEVERAGE` 的值里，系统「跑着但永远不会下单」）。⇒ 必须在状态条上说出来。
    live_gate: Dict[str, Any] = {"enabled": False, "declared_account": None,
                                 "ok": True, "detail": ""}
    try:
        from app.exchange import LiveGate
        gate = LiveGate.from_env(source)
        live_gate["enabled"] = bool(gate.enabled)
        live_gate["declared_account"] = gate.declared_account
        if gate.enabled and gate.declared_account is None:
            live_gate["ok"] = False
            live_gate["detail"] = (
                "LIGHTER_LIVE_TRADING=1 但 LIGHTER_LIVE_ACCOUNT_INDEX 未设置 —— "
                "所有真实下单都会被 LiveGate 拒绝（系统看起来在跑，但永远不会下单）")
    except Exception as exc:                                  # noqa: BLE001 - 面板不崩
        live_gate["ok"] = False
        live_gate["detail"] = f"live gate 状态不可读: {type(exc).__name__}: {exc}"
    detail = ("" if not missing else
              ("交易凭据未配置（只读面板不受影响；下单 / 调度器不可用）：缺 "
               + ", ".join(missing)))
    if live_gate["detail"]:
        detail = (detail + " ｜ " if detail else "") + str(live_gate["detail"])
    return {
        "known": True,
        "configured": not missing,
        "missing": missing,
        "admin_token_set": bool(_env_text(source, ADMIN_TOKEN_ENV)),
        "live_gate": live_gate,
        "detail": detail,
    }


def default_store_factory() -> StateStore:
    """按 `DB_URL` / `DB_PATH` 建 store。**不读任何交易凭据**。

    **懒调用**：store 不可用时面板每个请求都会重试一次 —— 数据库可能正在迁移或者被
    短锁占住，启动时失败一次不该让面板永久 500。
    """
    return StateStore.from_config(resolve_store_path())


def _limits_from_env() -> RiskLimits:
    try:
        return RiskLimits.from_env(os.environ)
    except Exception as exc:                                  # noqa: BLE001 - 面板不崩
        logger.warning("invalid risk limits in env, using defaults: %s", exc)
        return RiskLimits()


async def _warm_panel_bars(cache: "PanelCache", symbols: Sequence[str],
                           fetch: Optional[Callable[[str], Any]] = None) -> None:
    """启动时预热面板的 K 线缓存（后台任务，失败只记日志）。

    为什么需要：面板首屏要 3 标的的全量 4h K 线，冷拉一次 ≈25s（实测）。重启后如果没人
    跑过 tick，第一个打开面板的人就要等这么久。启动时先在后台拉一遍，把这段等待挪出
    用户请求路径；此后由每轮 tick 的 `cache.set_bars` 持续刷新。
    """
    if fetch is None:
        def fetch(symbol: str) -> Any:
            from app.market import DEFAULT_ENVIRONMENT, MarketClient

            env = (os.environ.get("LIGHTER_ENVIRONMENT") or "").strip() or DEFAULT_ENVIRONMENT
            client = getattr(fetch, "_client", None)
            if client is None:
                client = MarketClient(env)
                fetch._client = client                        # type: ignore[attr-defined]
            return client.bar_list(symbol)

    for symbol in symbols:
        try:
            bars = await asyncio.to_thread(fetch, symbol)
        except Exception:                                     # noqa: BLE001 - 预热失败不影响服务
            logger.warning("panel bars warm-up failed for %s", symbol, exc_info=True)
            continue
        cache.set_bars(symbol, bars)
        logger.info("panel bars warmed: %s (%d bars)", symbol, len(bars))


# ================================================================== tick 缓存


class PanelCache:
    """调度器每轮 tick 后填的「交易所私有读数」缓存（面板只读它）。

    为什么要有这一层：`app.views` 必须是同步的纯函数（它被 HTML/JSON 两条路径共用），
    而交易所读数是 async 的。与其在视图里绕 event loop，不如让**唯一的 writer**
    （tick）把结果放进来 —— 面板永远读「最近一次已知的真相」，读不到就是 `None`
    （= 未知），而不是现场发一次可能挂住的请求。

    `orders()` 在**从未采集过**时抛异常而不是返回 `[]`：返回空列表会让止损覆盖区块
    把每张止损单都报成「不在」（红色假警报），而真相是「我们还不知道」。
    """

    def __init__(self) -> None:
        self.equity_value: Optional[float] = None
        self.orders_value: Optional[List[Dict[str, Any]]] = None
        self.prices_value: Dict[str, float] = {}
        self.updated_at: Optional[float] = None
        self.error: str = ""
        # tick 已经拉过的 K 线（symbol → (ts, bars)）。面板直接复用，别再自己去交易所
        # 拉一遍：一次冷拉 3 标的 × 8 页 ≈27s（实测），而且会和 tick 用**不同的起点**
        # 算出略微不同的 EMA（EMA 用首根收盘播种）—— 那正是「两个面板不一致」的来源。
        self.bars_value: Dict[str, Tuple[float, Any]] = {}

    # ---------------------------------------------------------------- 写

    def set_bars(self, symbol: str, bars: Any, *, ts: Optional[float] = None) -> None:
        """记下 tick 本轮用的 K 线（面板只读，不产生新的取数）。"""
        if bars is None:
            return
        self.bars_value[str(symbol).upper()] = (
            float(ts) if ts is not None else time.time(), tuple(bars))

    def bars(self, symbol: str, *, max_age_s: float = 3600.0,
             clock: Optional[Callable[[], float]] = None) -> Optional[Any]:
        """最近一轮 tick 的 K 线；过期/没有就返回 None（调用方自己去取或降级）。"""
        hit = self.bars_value.get(str(symbol).upper())
        if hit is None:
            return None
        stamp, bars = hit
        if ((clock or time.time)() - stamp) > float(max_age_s):
            return None
        return list(bars)

    def update(self, *, equity: Any = None, orders: Optional[Sequence[Any]] = None,
               prices: Optional[Mapping[str, Any]] = None,
               ts: Optional[float] = None, error: str = "") -> None:
        if equity is not None:
            self.equity_value = float(equity)
        if orders is not None:
            self.orders_value = [dict(o) for o in orders]
        if prices is not None:
            self.prices_value = {str(k).upper(): float(v) for k, v in dict(prices).items()}
        self.updated_at = float(ts) if ts is not None else time.time()
        self.error = str(error or "")

    # ---------------------------------------------------------------- 读（注入给 views）

    def equity(self) -> Optional[float]:
        return self.equity_value

    def orders(self) -> List[Dict[str, Any]]:
        if self.orders_value is None:
            raise RuntimeError("交易所挂单读数尚未采集（调度器还没跑过一轮 tick，"
                               "或本进程没有配置凭据）")
        return list(self.orders_value)

    def prices(self) -> Dict[str, float]:
        return dict(self.prices_value)


class MarketBarSource:
    """`app.market` 的公开只读 K 线（**无鉴权、无签名**），带 TTL 缓存。

    每次 `/symbols` 都打一次交易所是浪费（一轮 3 个标的 = 3 个 HTTP 请求），而 4h
    bar 每 4 小时才变一次。缓存 120s：面板看到的永远是"最近 2 分钟内"的数据，够新，
    也不会把限流额度烧在刷页面上。
    """

    def __init__(self, market: Any, *, ttl_s: float = 120.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.market = market
        self.ttl_s = float(ttl_s)
        self.clock = clock
        self._cache: Dict[str, Tuple[float, Any]] = {}

    def __call__(self, symbol: str) -> Any:
        key = str(symbol).upper()
        now = self.clock()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] <= self.ttl_s:
            return hit[1]
        bars = self.market.bar_list(key)
        self._cache[key] = (now, bars)
        return bars


# ================================================================== 调度器


def next_tick_at(now: float, minutes: Sequence[int] = TICK_MINUTES) -> float:
    """严格晚于 `now` 的下一个 `hh:<m>`（UTC）的 epoch 秒。

    `minutes` 按分钟位解释（`(3, 33)` = 每小时的第 3、33 分钟）。实现只往前试两个
    小时：`minutes` 至少有一项落在 `[0, 59]`，所以两小时内必有一命中。
    """
    stamp = float(now)
    hour_start = math.floor(stamp / 3600.0) * 3600.0
    for hour_offset in (0, 1):
        for minute in sorted({int(m) % 60 for m in minutes}):
            candidate = hour_start + hour_offset * 3600.0 + minute * 60.0
            if candidate > stamp + 1e-6:
                return candidate
    return hour_start + 3600.0 + min(int(m) % 60 for m in minutes) * 60.0


class TickScheduler:
    """4h 收盘后 tick 的 asyncio 定时器（不引入新依赖）。

    两条硬规则：

    1. **一次失败不能杀死调度器**：`tick()` 兜住所有异常（runner 抛的、`events`
       写不进的），记日志 + 落 `tick_failed` 事件 + 计数，然后**继续**下一轮。
       一个因为交易所 502 就永久停摆的调度器比没有调度器更危险 —— 它看起来还在。
    2. **每轮都留心跳**：tick 开始与结束各写一条 `heartbeat`（T9 watchdog 按它判
       「调度器还在跑吗」）。runner 崩了也要写 —— 心跳证明的是"调度器活着"，
       不是"tick 成功了"（后者看事件流与状态条）。
    """

    def __init__(self, runner: Callable[[], Any], *, store: Optional[Any] = None,
                 store_factory: Optional[Callable[[], Any]] = None,
                 minutes: Sequence[int] = TICK_MINUTES,
                 clock: Callable[[], float] = time.time,
                 sleep: Optional[Callable[[float], Awaitable[None]]] = None) -> None:
        if runner is None:
            raise ValueError("TickScheduler requires a runner")
        self.runner = runner
        self._store = store
        self._store_factory = store_factory
        #: 只关**自己建的** store：`store=` 注入的是别人的连接（测试夹具 / 共享进程），
        #: 关闭它会把别人的库抽走。`store_factory=` 建出来的才是本对象的。
        self._store_owned = False
        self.minutes = tuple(int(m) % 60 for m in minutes)
        self.clock = clock
        self._sleep = sleep or asyncio.sleep
        self.started = False
        self.runs = 0
        self.failures = 0
        self.last_run_ts: Optional[float] = None
        self.last_error: str = ""
        self.last_status: str = "never"
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._closed = False

    def __repr__(self) -> str:
        return (f"TickScheduler(runs={self.runs}, failures={self.failures}, "
                f"last={self.last_status!r}, started={self.started})")

    # ---------------------------------------------------------------- store

    def store(self) -> Optional[Any]:
        if self._store is not None:
            return self._store
        if self._store_factory is None or self._closed:
            return None
        try:
            self._store = self._store_factory()
            self._store_owned = True
        except Exception as exc:                              # noqa: BLE001
            logger.warning("scheduler store unavailable: %s", exc)
            return None
        return self._store

    def close(self) -> None:
        """关掉调度器**自己建的** store（+ runner 自己的 store）。**幂等**。

        为什么需要：`create_app(enable_scheduler=True)` 会让调度器按同一个
        `store_factory` 建**自己**的连接（与面板是两条连接，落在同一个 SQLite 文件上）。
        面板那边由 `Panel.close()` 收尾，这里必须对称，否则只要开了调度器，每次进程
        退出就漏一个 `sqlite3.Connection`。

        `store=` 注入进来的连接**不关**（那是调用方的）。同理 `default_tick_runner`
        在 tick 里自建的第三个 store 挂在自己的 `close` 上，这里一并调用。
        关失败只记 WARNING —— 关闭路径不该抛。
        """
        if self._closed:
            return
        self._closed = True
        store, self._store = self._store, None
        owned, self._store_owned = self._store_owned, False
        if store is not None and owned:
            closer = getattr(store, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:                      # noqa: BLE001 - 关闭路径
                    logger.warning("could not close the scheduler store: %s", exc)
        runner_closer = getattr(self.runner, "close", None)
        if callable(runner_closer):
            try:
                runner_closer()
            except Exception as exc:                          # noqa: BLE001 - 关闭路径
                logger.warning("could not close the tick runner: %s", exc)

    # ---------------------------------------------------------------- 一轮

    async def tick(self) -> Dict[str, Any]:
        """跑一轮（**永不抛**）。返回这一轮的摘要（日志 / 测试用）。"""
        started = float(self.clock())
        store = self.store()
        self._beat(store, started, {"phase": "start", "runs": self.runs})
        status, detail = "ok", ""
        try:
            result = self.runner()
            if inspect.isawaitable(result):
                result = await result
            detail = "" if result is None else str(result)[:400]
            self.last_error = ""          # 好的一轮清掉历史错误：面板读的是「当前」状态
        except Exception as exc:                              # noqa: BLE001 - 规则 1
            status = "error"
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("tick failed — the scheduler keeps running")
            self._event(store, "tick_failed",
                        f"tick failed: {self.last_error}（调度器继续）",
                        {"error": self.last_error, "runs": self.runs})
        finally:
            self.runs += 1
            self.last_status = status
            self.last_run_ts = float(self.clock())
            self._beat(store, self.last_run_ts,
                       {"phase": "end", "status": status, "runs": self.runs,
                        "failures": self.failures, "error": self.last_error[:200]})
        return {"status": status, "detail": detail, "runs": self.runs,
                "failures": self.failures, "ts": self.last_run_ts,
                "error": self.last_error}

    def _beat(self, store: Optional[Any], ts: float, data: Mapping[str, Any]) -> None:
        if store is None:
            return
        try:
            watchdog.record_heartbeat(store, ts=ts, source="scheduler", data=dict(data))
        except Exception as exc:                              # noqa: BLE001
            logger.warning("could not record a heartbeat: %s", exc)

    def _event(self, store: Optional[Any], kind: str, message: str,
               data: Mapping[str, Any]) -> None:
        if store is None:
            return
        try:
            store.log_event(kind, message, data=dict(data))
        except Exception as exc:                              # noqa: BLE001
            logger.warning("could not record %s: %s", kind, exc)

    # ---------------------------------------------------------------- 循环

    async def run_forever(self) -> None:
        self.started = True
        while not self._stopping:
            due = next_tick_at(float(self.clock()), self.minutes)
            try:
                # 分段睡：`stop()` 最多等 30s 就生效，而不是等满一个 30 分钟窗口。
                while not self._stopping:
                    remaining = due - float(self.clock())
                    if remaining <= 0:
                        break
                    await self._sleep(min(remaining, 30.0))
            except asyncio.CancelledError:                    # pragma: no cover - 关闭路径
                raise
            if self._stopping:
                break
            await self.tick()
        self.started = False

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._closed = False                  # 重新起来：允许再建一次连接（stop 里关过）
        self._task = asyncio.get_event_loop().create_task(self.run_forever())
        self.started = True

    async def stop(self) -> None:
        """停下循环，**并释放调度器自己的 store**（`close()` 幂等，可重复调）。

        关闭顺序有讲究：先 cancel + await 任务，再关库 —— 反过来会让正在跑的一轮 tick
        往一个已经关掉的连接里写（`ProgrammingError: Cannot operate on a closed
        database`），而那一轮的 `finally` 还要写心跳。
        """
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):       # noqa: BLE001 - 关闭路径
                pass
        self.started = False
        self.close()


# ================================================================== 事件记录


def record_tick_outcomes(store: Optional[Any], report: Any, *,
                         ts: Optional[float] = None) -> List[int]:
    """把一轮 tick 的 `SymbolOutcome` 落进 `events`（**含 skip**）。

    为什么必须落 skip：`app.executor` 把「未过密集门槛」这类结论放在
    `TickReport.outcomes` 里，并没有写库。而面板事件流是用户唯一能看见「为什么今天
    一单没开」的地方 —— 不写下来，面板就只剩「开仓 0 笔」，用户会以为系统坏了。
    """
    ids: List[int] = []
    if store is None or report is None:
        return ids
    for outcome in list(getattr(report, "outcomes", ()) or ()):
        action = str(getattr(outcome, "action", "") or "")
        symbol = str(getattr(outcome, "symbol", "") or "") or None
        reason = str(getattr(outcome, "reason", "") or "")
        kind = action or "tick_outcome"
        data = {"action": action, "bar_ms": getattr(outcome, "bar_ms", None),
                "reason": reason, "ok": bool(getattr(outcome, "ok", True))}
        message = f"{symbol or '-'} {action or '?'}: {reason}"[:500]
        try:
            ids.append(int(store.log_event(kind, message, symbol=symbol, data=data, ts=ts)))
        except Exception as exc:                              # noqa: BLE001
            logger.warning("could not record the tick outcome: %s", exc)
    return ids


# ================================================================== 鉴权


def _configured_token() -> str:
    return (os.environ.get(ADMIN_TOKEN_ENV) or "").strip()


def _supplied_token(request: Request, k: Optional[str]) -> str:
    if k:
        return str(k)
    return (request.headers.get(ADMIN_TOKEN_HEADER) or "").strip()


def _auth_error(request: Request, k: Optional[str], token: str) -> Optional[str]:
    """返回错误文案（`None` = 通过）。**绝不**把 token 写进返回值 / 日志。"""
    supplied = _supplied_token(request, k)
    if not token:
        return f"{ADMIN_TOKEN_ENV} 未配置 —— 所有写操作一律拒绝（fail-closed）"
    if not supplied:
        return f"缺少 admin token（?{ADMIN_TOKEN_QUERY}=<token> 或 {ADMIN_TOKEN_HEADER} 头）"
    if not hmac.compare_digest(supplied, token):
        return "admin token 不正确"
    return None


def _audit(store: Optional[Any], *, endpoint: str, result: str, by: str = "admin",
           reason: str = "", extra: Optional[Mapping[str, Any]] = None,
           ts: Optional[float] = None) -> Optional[int]:
    """写一条 `admin_action` 事件（成功、失败、被拒**都写**）。

    审计要能回答三个问题：谁（`by`）、做了什么（`endpoint`）、结果如何（`result`）。
    **刻意不记 request line / query string** —— 那里面可能带着 `?k=<token>`。

    「结果如何」包含**失败与被拒**：一次因为缺凭据而失败的 `flatten` 也必须留痕，
    否则审计只能证明「成功过什么」，而它存在的意义恰恰是「谁试过什么」。
    """
    if store is None:
        # 写不进审计本身就是一条要喊出来的事（DB 真的拿不到时才可能走到这里）。
        logger.warning("admin audit for %s=%s could not be recorded: no store",
                       endpoint, result)
        return None
    data: Dict[str, Any] = {"endpoint": endpoint, "result": str(result),
                            "by": str(by or "admin"), "reason": str(reason or "")}
    data.update(dict(extra or {}))
    try:
        return int(store.log_event(views.ADMIN_EVENT_KIND,
                                   f"admin {endpoint} -> {result}"
                                   + (f" ({reason})" if reason else ""),
                                   data=data, ts=ts))
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("could not record the admin audit event: %s", exc)
        return None


def _warn(*messages: str) -> List[str]:
    return [str(m) for m in messages if str(m or "").strip()]


# ================================================================== 默认 impl


async def _default_flatten(store: Any, *, reason: str = "", by: str = "admin",
                           confirm_live: Optional[int] = None) -> Dict[str, Any]:
    """默认的「市价清仓 + 撤所有挂单」实现（**阶段二未实测**：阶段一没有凭据）。

    **async**：唯一的调用者是 `POST /admin/flatten` 的 handler（已经在 event loop 里）。
    不要把它写成 sync + `asyncio.run()` —— 在运行中的 loop 里会抛
    `RuntimeError: Cannot run the event loop while another loop is running`（实测踩过）。

    行为：对每一行 `open_positions()` 按反向 side 下 reduce-only 市价单，然后撤全市场
    挂单。任何一步失败都**不吞**：返回 `ok=false` + 逐条 `steps`，由调用方再记一条
    端点级审计。

    为什么 import 写在函数里：这样 `import app.main`（面板、`/health`、所有只读端点）
    的 import 图里**没有** `app.exchange`。私钥模块只在有人真的按下「清仓」时加载。

    `confirm_live` 显式传给 `ExchangeClient`：三道门之一是「每次调用显式确认
    account_index」，这里不替调用方做决定 —— 没配凭据就抛 `NotConfigured`，
    由上层降级成 `ok=false`。
    """
    from app.config import Config

    try:
        config = Config.from_env()
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "degraded": True, "closed": [], "cancelled": False,
                "error": f"凭据不可用，无法清仓: {type(exc).__name__}: {exc}",
                "steps": []}

    import app.exchange as exchange_module                    # noqa: PLC0415 - 见 docstring

    async def _run() -> Dict[str, Any]:
        client = exchange_module.ExchangeClient.from_config(
            config, env=os.environ, dry_run=False,
            confirm_live=config.account_index if confirm_live is None else confirm_live)
        steps: List[Dict[str, Any]] = []
        closed: List[str] = []
        for row in store.open_positions():
            symbol = str(row.get("symbol") or "").upper()
            market_id = row.get("market_id")
            qty_int = int(row.get("qty_int") or 0)
            side = -int(row.get("side") or 0)                 # 反向平仓
            if market_id is None or qty_int <= 0 or side == 0:
                steps.append({"symbol": symbol, "ok": False,
                              "error": "缺少 market_id/qty_int，无法市价平仓"})
                continue
            try:
                result = await client.place_exit(
                    symbol=symbol, market_id=int(market_id), side=side, qty_int=qty_int,
                    price_decimals=int(row.get("price_decimals") or 0),
                    exit_price=float(row.get("entry_price") or 0.0))
            except Exception as exc:                          # noqa: BLE001 - 逐条记录
                steps.append({"symbol": symbol, "ok": False,
                              "error": f"{type(exc).__name__}: {exc}"})
                continue
            steps.append({"symbol": symbol, "ok": bool(getattr(result, "ok", False)),
                          "code": getattr(result, "code", None),
                          "error": getattr(result, "error", "")})
            if getattr(result, "ok", False):
                closed.append(symbol)
        cancel = None
        try:
            cancel = await client.cancel_all_orders()
        except Exception as exc:                              # noqa: BLE001
            steps.append({"symbol": "*", "ok": False,
                          "error": f"撤单失败: {type(exc).__name__}: {exc}"})
        ok = all(step.get("ok") for step in steps) if steps else True
        error = "" if ok else "; ".join(
            f"{step.get('symbol')}: {step.get('error')}"
            for step in steps if not step.get("ok"))[:400]
        return {"ok": bool(ok), "closed": closed,
                "cancelled": bool(getattr(cancel, "ok", False)), "steps": steps,
                "error": error}

    try:
        # 本函数是 **async** 的（端点 await 它）：flatten 只有一条调用路径 ——
        # HTTP handler，那里已经在一个 event loop 里。写成 sync + `asyncio.run()`
        # 会在 handler 里直接炸 "Cannot run the event loop while another loop is running"
        # （实测踩过），所以这里不做「同步/异步双入口」的猜测。
        return await _run()
    except Exception as exc:                                  # noqa: BLE001 - 降级不崩
        return {"ok": False, "closed": [], "cancelled": False,
                "error": f"{type(exc).__name__}: {exc}", "steps": []}


# ================================================================== 应用工厂


def _default_panel_sources(cache: "PanelCache",
                           *, market_source: bool = False) -> views.Sources:
    """生产装配的面板数据源：tick 的 PanelCache（权益/挂单/现价）+ 可选公开 K 线。

    T10 落地时漏了这一步：`create_app()` 从没把 cache / `app.market` 接进
    `views.Sources`，面板的仓位现价、止损覆盖、三标的动态永远「无数据源」。
    K 线是**真 HTTP**，所以只在真正的生产入口（模块级 `app`，`market_source=True`）
    打开 —— 离线测试与降级测试的装配不因一次 `/symbols` 发网络请求。
    """
    bars: Optional[Callable[[str], Any]] = None
    if market_source:
        def bars(symbol: str) -> Any:                         # noqa: F811 - 闭包赋值
            # ① 优先用最近一轮 tick 拉过的 K 线：同一份数据、同一个起点 ⇒ 面板与
            #    交易决策口径完全一致，也省掉一次 3 标的 × 8 页的冷拉（实测 ≈27s）。
            cached = cache.bars(symbol)
            if cached:
                return cached
            # ② 缓存空/过期（还没跑过 tick、或 tick 停了）→ 自己去公开接口取。
            from app.market import DEFAULT_ENVIRONMENT, MarketClient
            source = getattr(bars, "_source", None)
            if source is None:
                env = ((os.environ.get("LIGHTER_ENVIRONMENT") or "").strip()
                       or DEFAULT_ENVIRONMENT)
                source = MarketBarSource(MarketClient(env))
                bars._source = source                         # type: ignore[attr-defined]
            return source(symbol)
    return views.Sources(equity=cache.equity, orders=cache.orders,
                         prices=cache.prices, bars=bars,
                         cached_bars=cache.bars)


async def _admin_params(request: Request) -> Dict[str, str]:
    """把 admin 写操作的参数**从 query、表单、JSON body 三处合并**成一个 dict。

    为什么必须合并（2026-10-28 线上实测的坑）：这三个 handler 原先只声明普通参数
    （`on: int = 1`、`confirm: str = ""`…），FastAPI 只从 **query** 取值；而面板的
    JS 用 `application/x-www-form-urlencoded` 提交 body ⇒ 参数被静默丢弃：

    * 「恢复开仓」按钮实际把 kill **打开**（`on` 落到默认 1，`reason` 也丢了）；
    * 「强制清仓」「重置回撤峰值」永远 400（`confirm` / `reason` 读成空串）；
    * 任何 `curl -d 'on=0'` 的脚本同理。

    这是「静默回落」类 bug，所以：① 三个来源都收；② 关键参数不再给默认值
    （`kill` 的 `on` 缺失即 400），让漏参数的调用**响亮失败**而不是反向操作。
    """
    params: Dict[str, str] = {str(k): str(v) for k, v in request.query_params.items()}
    ctype = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in ctype:
            body = await request.json()
            if isinstance(body, Mapping):
                params.update({str(k): str(v) for k, v in body.items()})
        elif "form" in ctype or "urlencoded" in ctype:
            form = await request.form()
            params.update({str(k): str(v) for k, v in form.items()})
    except Exception:                                        # noqa: BLE001 - 脏 body 不该 500
        logger.warning("admin request body could not be parsed", exc_info=True)
    return params


def create_app(*, panel: Optional[views.Panel] = None,
               store_factory: Optional[Callable[[], Any]] = None,
               sources: Optional[views.Sources] = None,
               limits: Optional[RiskLimits] = None,
               admin_token: Optional[str] = None,
               mode: str = "live",
               symbols: Sequence[str] = views.DEFAULT_SYMBOLS,
               clock: Callable[[], float] = time.time,
               flatten: Optional[Callable[..., Any]] = None,
               dd_reset: Optional[Callable[..., Any]] = None,
               runner: Optional[Callable[[], Any]] = None,
               scheduler: Optional[TickScheduler] = None,
               enable_scheduler: bool = False,
               credentials: Optional[Callable[[], Any]] = None,
               cache: Optional[PanelCache] = None,
               market_source: bool = False) -> FastAPI:
    """建 app。所有依赖都可注入 —— 测试用 `create_app(panel=..., admin_token=...)`
    就能全离线跑完 6 个只读端点与 3 个写操作。

    `credentials`：交易凭据状态的探针（默认 :func:`credential_status`，读
    `os.environ`）。它只进**状态条**，不参与鉴权 —— 用途是让状态条能区分
    「没有数据库」与「没有私钥」这两件后果完全不同的事。
    """
    resolved_limit = limits if limits is not None else _limits_from_env()
    factory = store_factory or default_store_factory
    cache = cache if cache is not None else PanelCache()
    if panel is None and sources is None:
        sources = _default_panel_sources(cache, market_source=market_source)
    panel = panel if panel is not None else views.Panel(
        factory, sources=sources, limits=resolved_limit, mode=mode, now=clock,
        symbols=symbols,
        credentials=credentials if credentials is not None else credential_status)
    flatten_impl = flatten or _default_flatten
    token = (_configured_token() if admin_token is None else str(admin_token).strip())
    sched_box: Dict[str, Any] = {"scheduler": scheduler, "error": ""}

    def _store() -> Optional[Any]:
        return panel.store()

    def _degraded(action: str, message: str, *, store: Optional[Any] = None,
                  reason: str = "", by: str = "admin") -> JSONResponse:
        """统一的「降级」响应：0 值 + WARNING + 非 500。"""
        _audit(store, endpoint=action, result="degraded", by=by,
               reason=reason or message)
        return JSONResponse(content={
            "ok": False, "degraded": True, "action": action, "error": message,
            "closed": [], "cancelled": False, "warnings": [message]})

    def _unauthorized(action: str, message: str, *, store: Optional[Any],
                      reason: str = "", by: str = "anonymous") -> JSONResponse:
        _audit(store, endpoint=action, result="rejected", by=by,
               reason=reason or "auth")
        return JSONResponse(status_code=401, content={
            "ok": False, "degraded": False, "action": action, "error": message,
            "warnings": [message]})

    # ---------------------------------------------------------------- 生命周期

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        sched = sched_box["scheduler"]
        if sched is None and enable_scheduler:
            built = _build_scheduler(factory, runner=runner, limits=resolved_limit,
                                     cache=cache, sources_getter=lambda: panel.sources,
                                     clock=clock)
            if isinstance(built, TickScheduler):
                sched_box["scheduler"] = built
                try:
                    built.start()
                    logger.info("tick scheduler started (UTC minutes %s)",
                                sorted(built.minutes))
                except Exception as exc:                      # noqa: BLE001 - 启动失败不崩
                    sched_box["error"] = f"{type(exc).__name__}: {exc}"
                    logger.exception("could not start the tick scheduler")
                    _audit(_store(), endpoint="scheduler", result="failed",
                           reason=sched_box["error"])
            else:
                sched_box["error"] = str(built)
                logger.warning("tick scheduler disabled: %s", built)
                _audit(_store(), endpoint="scheduler", result="disabled",
                       reason=str(built))
        elif isinstance(sched_box["scheduler"], TickScheduler):
            try:
                sched_box["scheduler"].start()
            except Exception as exc:                          # noqa: BLE001
                sched_box["error"] = f"{type(exc).__name__}: {exc}"
                logger.exception("could not start the injected scheduler")
        # 启动时预热面板的 K 线缓存（后台任务）：把「首屏 25s」挪出用户请求路径。
        # 关掉用 PANEL_WARM_BARS=0（离线测试/不装行情源的部署）。
        if market_source and os.environ.get("PANEL_WARM_BARS", "1") != "0":
            warm = asyncio.create_task(_warm_panel_bars(cache, symbols))
            app.state.warm_task = warm
            logger.info("panel bar warm-up scheduled")
        try:
            yield
        finally:
            # 关闭顺序：先停调度器（它会关掉自己建的连接），再关面板的懒建连接。
            # 反过来关会让正在跑的一轮 tick 往已关闭的库写。
            current = sched_box.get("scheduler")
            if isinstance(current, TickScheduler):
                try:
                    await current.stop()          # stop() 内部调 close()（幂等）
                except Exception:                 # noqa: BLE001 - 关闭路径不许抛
                    logger.exception("could not stop the tick scheduler")
                current.close()
            elif current is not None:
                closer = getattr(current, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:             # noqa: BLE001 - 关闭路径不许抛
                        logger.warning("could not close the injected scheduler", exc_info=True)
            # 面板最后关：所有只读端点都读它，它的 store 是懒建的、没人替它收尾。
            panel.close()

    # No docs/openapi routes: the panel is the only intended surface. Fewer default
    # routes = less to accidentally expose.
    app = FastAPI(title=SERVICE, version=VERSION, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.panel = panel
    app.state.limits = resolved_limit
    app.state.scheduler_box = sched_box
    app.state.cache = cache

    # ---------------------------------------------------------------- 只读端点

    @app.get("/health")
    def health() -> Dict[str, Any]:
        """存活探针。**依赖自由**：不碰 DB、不碰网络、不碰凭据（T1 的契约不许破坏）。"""
        return {"ok": True, "service": SERVICE, "version": VERSION,
                "uptime_s": round(time.time() - _STARTED_AT, 3)}

    @app.get("/status")
    def status() -> JSONResponse:
        return JSONResponse(content=panel.status())

    @app.get("/positions")
    def positions() -> JSONResponse:
        return JSONResponse(content=panel.positions())

    @app.get("/stops")
    def stops() -> JSONResponse:
        return JSONResponse(content=panel.stops())

    @app.get("/pnl")
    def pnl() -> JSONResponse:
        return JSONResponse(content=panel.pnl())

    @app.get("/symbols")
    def symbols_block() -> JSONResponse:
        return JSONResponse(content=panel.symbols_block())

    @app.get("/events")
    def events() -> JSONResponse:
        return JSONResponse(content=panel.events())

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        """自包含 HTML（内联 CSS + 内联 SVG，无 CDN、无外部 JS）。"""
        return HTMLResponse(content=panel.page())

    @app.get("/admin")
    def admin_entry(request: Request) -> RedirectResponse:
        """`/admin` 是**旧系统（HL 面板）**的路径，本应用的面板在根路径。

        保留这条路由只为「把旧链接粘过来」的场景：307 到 `/?k=...`（token 留在 query 里，
        页面 JS 收到后存 localStorage 并从地址栏抹掉）。没有 query 就普通重定向到 `/`。
        不加这条时 `/admin` 直接 404 —— 2026-10-28 就是这么给了用户一个坏链接。
        """
        query = request.url.query
        return RedirectResponse(url="/" + (f"?{query}" if query else ""), status_code=307)

    # ---------------------------------------------------------------- 写操作

    @app.post("/admin/kill")
    async def admin_kill(request: Request) -> JSONResponse:
        """手动停开新仓（**不挡离场**）。

        `on=0` 关掉（重新允许开仓）。语义与 `app.dd.entry_blocked()` /
        `app.risk._gate_kill()` 完全一致：只挡 `action='entry'`，离场与交易所侧的
        止损单照常有效。

        参数从 query 或 body 都认（`_admin_params`）；**`on` 必须显式给**——
        没有默认值：缺参数时返回 400，而不是静默当成 1 把仓位停掉。
        """
        params = await _admin_params(request)
        k = params.get("k", "")
        reason = params.get("reason", "")
        by = params.get("by") or "admin"
        err = _auth_error(request, k, token)
        store = _store()
        if err:
            return _unauthorized("kill", err, store=store, by=by)
        raw_on = str(params.get("on", "")).strip()
        if not raw_on:
            message = ("admin/kill 必须显式带 on=0|1：缺省曾被静默当成 1，"
                       "于是「恢复开仓」的按钮实际把新仓停掉了")
            _audit(store, endpoint="kill", result="rejected", by=by,
                   reason="missing on")
            return JSONResponse(status_code=400, content={
                "ok": False, "degraded": False, "action": "kill",
                "error": message, "warnings": [message]})
        if raw_on not in ("0", "1"):
            message = f"admin/kill 的 on 只能是 0 或 1，收到 {raw_on!r}"
            _audit(store, endpoint="kill", result="rejected", by=by,
                   reason=f"invalid on={raw_on!r}")
            return JSONResponse(status_code=400, content={
                "ok": False, "degraded": False, "action": "kill",
                "error": message, "warnings": [message]})
        if store is None:
            return _degraded("kill", "数据库不可用，无法写 kill 状态: "
                                     f"{panel.store_error or 'unknown'}",
                             reason=reason, by=by)
        wanted = raw_on == "1"
        try:
            state = views.set_kill(store, wanted, by=by, reason=reason, ts=clock())
        except Exception as exc:                              # noqa: BLE001 - 降级不崩
            return _degraded("kill", f"写 kill 状态失败: {type(exc).__name__}: {exc}",
                             reason=reason, by=by)
        event_id = _audit(store, endpoint="kill", result="ok" if wanted else "off",
                          by=by, reason=reason,
                          extra={"kill": state["kill"], "on": raw_on})
        return JSONResponse(content={"ok": True, "degraded": False, "action": "kill",
                                     "kill": state["kill"], "state": state,
                                     "audit_event_id": event_id, "warnings": []})

    @app.post("/admin/flatten")
    async def admin_flatten(request: Request) -> JSONResponse:
        """市价清仓 + 撤所有挂单（**唯一的「比 kill 更强」操作**）。

        不可撤销 ⇒ 必须二次确认（`confirm=yes`）。没确认 → **400**，并落一条
        `admin_action` 审计（谁试过什么）。参数从 query 或 body 都认。
        """
        params = await _admin_params(request)
        k = params.get("k", "")
        confirm = params.get("confirm", "")
        reason = params.get("reason", "")
        by = params.get("by") or "admin"
        err = _auth_error(request, k, token)
        store = _store()
        if err:
            return _unauthorized("flatten", err, store=store, by=by)
        if str(confirm or "").strip().lower() not in CONFIRM_VALUES:
            message = "flatten 是不可撤销操作：必须带 confirm=yes（市价清仓 + 撤所有挂单）"
            _audit(store, endpoint="flatten", result="rejected", by=by,
                   reason="missing confirmation")
            return JSONResponse(status_code=400, content={
                "ok": False, "degraded": False, "action": "flatten",
                "error": message, "warnings": [message]})
        if store is None:
            return _degraded("flatten", "数据库不可用，无法清仓: "
                                        f"{panel.store_error or 'unknown'}",
                             reason=reason, by=by)
        try:
            result = flatten_impl(store, reason=reason, by=by)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:                              # noqa: BLE001 - 降级不崩
            result = {"ok": False, "closed": [], "cancelled": False,
                      "error": f"{type(exc).__name__}: {exc}", "steps": []}
        payload = dict(result or {})
        ok = bool(payload.get("ok"))
        event_id = _audit(store, endpoint="flatten", result="ok" if ok else "failed",
                          by=by, reason=reason,
                          extra={"closed": list(payload.get("closed") or []),
                                 "cancelled": bool(payload.get("cancelled")),
                                 "error": str(payload.get("error") or "")[:300]})
        payload.update({"action": "flatten", "audit_event_id": event_id,
                        "warnings": [] if ok else _warn(
                            str(payload.get("error") or "清仓未全部成功"))})
        payload.setdefault("degraded", False)
        return JSONResponse(content=payload)

    @app.post("/admin/dd/reset")
    async def admin_dd_reset(request: Request) -> JSONResponse:
        """重置回撤峰值（熔断**永不自解**，人工介入后必须能重开）。

        `reason` 必填：`app.dd.DrawdownGuard.reset_peak()` 拒绝空理由（审计要能回答
        「谁为什么重置的」）。指数为 0（账户打光 / 状态坏）同样拒绝 —— 把峰值抹成 0
        只会把爆仓藏起来。参数从 query 或 body 都认。
        """
        params = await _admin_params(request)
        k = params.get("k", "")
        reason = params.get("reason", "")
        by = params.get("by") or "admin"
        err = _auth_error(request, k, token)
        store = _store()
        if err:
            return _unauthorized("dd_reset", err, store=store, by=by)
        if not str(reason or "").strip():
            message = "dd/reset 必须带非空 reason（审计要记录「为什么重置峰值」）"
            _audit(store, endpoint="dd_reset", result="rejected", by=by,
                   reason="missing reason")
            return JSONResponse(status_code=400, content={
                "ok": False, "degraded": False, "action": "dd_reset",
                "error": message, "warnings": [message]})
        if store is None:
            return _degraded("dd_reset", "数据库不可用，无法重置回撤峰值: "
                                         f"{panel.store_error or 'unknown'}",
                             reason=reason, by=by)

        def _reset() -> Dict[str, Any]:
            guard = DrawdownGuard(store, limits=resolved_limit, clock=clock)
            return guard.reset_peak(reason=reason, by=by, ts=clock()).as_dict()

        try:
            if dd_reset is not None:
                result = dd_reset(store, reason=reason, by=by)
                if inspect.isawaitable(result):
                    result = await result
                payload = result if isinstance(result, Mapping) else {"verdict": result}
            else:
                payload = {"verdict": await asyncio.get_event_loop().run_in_executor(
                    None, _reset)}
        except DdError as exc:
            # 拒绝重置是**业务拒绝**，不是系统故障：200 + ok=false + WARNING。
            _audit(store, endpoint="dd_reset", result="rejected", by=by, reason=str(exc))
            return JSONResponse(content={"ok": False, "degraded": True,
                                         "action": "dd_reset", "error": str(exc),
                                         "warnings": [str(exc)]})
        except Exception as exc:                              # noqa: BLE001 - 降级不崩
            message = f"重置回撤峰值失败: {type(exc).__name__}: {exc}"
            _audit(store, endpoint="dd_reset", result="failed", by=by, reason=message)
            return JSONResponse(content={"ok": False, "degraded": True,
                                         "action": "dd_reset", "error": message,
                                         "warnings": [message]})
        event_id = _audit(store, endpoint="dd_reset", result="ok", by=by, reason=reason,
                          extra={"verdict": payload.get("verdict")})
        return JSONResponse(content={"ok": True, "degraded": False,
                                     "action": "dd_reset", "reason": reason,
                                     "verdict": payload.get("verdict"),
                                     "audit_event_id": event_id, "warnings": []})

    @app.get("/admin/state")
    def admin_state(request: Request, k: str = "") -> JSONResponse:
        """写面板的现状（kill / 调度器 / 缓存年龄）。**同样要 token**。"""
        err = _auth_error(request, k, token)
        store = _store()
        if err:
            return _unauthorized("admin_state", err, store=store)
        sched = sched_box.get("scheduler")
        return JSONResponse(content={
            "ok": True, "version": VERSION,
            "kill": views.kill_state(store) if store is not None else {"known": False},
            "scheduler": {
                "configured": isinstance(sched, TickScheduler),
                "started": bool(getattr(sched, "started", False)),
                "runs": int(getattr(sched, "runs", 0) or 0),
                "failures": int(getattr(sched, "failures", 0) or 0),
                "last_run_ts": getattr(sched, "last_run_ts", None),
                "last_status": getattr(sched, "last_status", "never"),
                "error": sched_box.get("error", ""),
                "minutes": list(sorted(getattr(sched, "minutes", TICK_MINUTES))),
            },
            "cache": {"updated_at": cache.updated_at, "error": cache.error,
                      "orders_known": cache.orders_value is not None},
            "warnings": []})

    return app


# ================================================================== 生产装配


def _build_scheduler(store_factory: Callable[[], Any], *,
                     runner: Optional[Callable[[], Any]] = None,
                     limits: RiskLimits, cache: PanelCache,
                     sources_getter: Callable[[], views.Sources],
                     clock: Callable[[], float]) -> Any:
    """按环境装配真实调度器；装配不出来就返回**原因字符串**（不抛）。

    调度器**才**需要交易凭据（`Config.from_env()` 在 `default_tick_runner()` 里调用）。
    没凭据 ⇒ 返回原因字符串 ⇒ 调用方记一条 `scheduler disabled` 审计并继续提供只读面板。
    这是刻意的：**面板 + 审计只需要 DB**（见 :func:`default_store_factory`），而调度器
    不会假装自己在交易。
    """
    if runner is None:
        try:
            runner = default_tick_runner(limits=limits, cache=cache,
                                         sources_getter=sources_getter)
        except Exception as exc:                              # noqa: BLE001
            return f"无法装配 tick runner: {type(exc).__name__}: {exc}"
    return TickScheduler(runner, store_factory=store_factory, clock=clock)


def default_tick_runner(*, limits: RiskLimits, cache: PanelCache,
                        sources_getter: Callable[[], views.Sources],
                        symbols: Sequence[str] = views.DEFAULT_SYMBOLS
                        ) -> Callable[[], Awaitable[Any]]:
    """真实的 tick runner：`Executor.run_once()` + 落 skip 事件 + 刷新面板缓存。

    这条路径**会**用到 `app.exchange`（下单）—— 但它是**唯一的 writer**，且只在调度器
    里跑；只读面板永远不经过这里。import 写在函数体里，`import app.main` 仍然不加载
    私钥模块。

    `sources_getter` 用来拿到面板的 `Sources`（调用方已注入；这里只为接口对称而保留，
    面板的 K 线走公开接口，不经 exchange）。
    """
    from app.config import Config
    from app.executor import Executor, LighterAccountReader
    from app.exchange import ExchangeClient
    from app.market import MarketClient

    config = Config.from_env()
    market = MarketClient(config.environment)
    exchange = ExchangeClient.from_config(config, env=os.environ, dry_run=False,
                                          confirm_live=config.account_index)
    reader = LighterAccountReader(base_url=config.base_url,
                                  account_index=config.account_index,
                                  auth_provider=lambda: exchange.auth.get())
    box: Dict[str, Any] = {"store": None}

    def _limits() -> RiskLimits:
        """每轮重读 kill 开关：环境变量 kill 与 DB 里的 admin kill **任一**为真即生效。"""
        store = box["store"]
        db_kill = False
        if store is not None:
            try:
                db_kill = bool(views.kill_state(store).get("kill"))
            except Exception:                                 # noqa: BLE001
                db_kill = False
        if not db_kill:
            return limits
        return RiskLimits(risk_pct=limits.risk_pct,
                          max_daily_loss_pct=limits.max_daily_loss_pct,
                          max_trades_per_day=limits.max_trades_per_day,
                          max_positions=limits.max_positions,
                          max_symbol_notional_mult=limits.max_symbol_notional_mult,
                          max_total_notional_mult=limits.max_total_notional_mult,
                          dd_warn_pct=limits.dd_warn_pct, dd_kill_pct=limits.dd_kill_pct,
                          kill=True)

    async def _tick() -> Any:
        store = box["store"]
        if store is None:
            store = box["store"] = StateStore.from_config(config)
        executor = Executor(store=store, market=market, exchange=exchange, reader=reader,
                            symbols=symbols, limits=_limits(), alert=None)
        report = await executor.run_once()
        record_tick_outcomes(store, report, ts=time.time())
        # 面板复用本轮取到的 K 线：不额外取数、且与决策用同一份序列（含同一个起点）
        for symbol, bars in (getattr(executor, "_bars", {}) or {}).items():
            cache.set_bars(symbol, bars, ts=time.time())
        account = getattr(executor, "last_account", None)
        if account is not None:
            cache.update(equity=getattr(account, "equity", None),
                         orders=[order.as_dict() for order in
                                 (getattr(account, "orders", ()) or ())],
                         prices=dict(getattr(executor, "_prices", {}) or {}),
                         ts=time.time(),
                         error=str(getattr(account, "orders_error", "") or ""))
        await _advance_drawdown(store, executor, reader)
        return report

    async def _advance_drawdown(store: Any, executor: Any, reader: Any) -> None:
        """`docs/DD_WATCHDOG.md` §6 的接线项：每轮 tick 在 `run_once()` 之后推进 TWR。

        首次没有 `dd_state` 时 `update()` 内部走 `bootstrap()`（以库里最早的权益点为锚）。
        权益未知（账户读失败）就跳过本轮推进并落事件 —— 面板/风控会按
        「回撤口径不可用」fail-closed，绝不拿旧 equity 硬算。流水读不到则按
        「无流水」推进并落事件（有出入金的时段 TWR 会失真，但开仓闸不会瞎掉）。
        """
        equity = getattr(getattr(executor, "last_account", None), "equity", None)
        if equity is None:
            store.log_event("dd_update_failed", "权益未知，本轮未推进回撤（fail-closed）",
                            data={"reason": "equity_unavailable"}, ts=time.time())
            return
        flows: Sequence[Mapping[str, float]] = ()
        transfer_reader = getattr(reader, "transfers", None)
        if callable(transfer_reader):
            try:
                flows = tuple(await asyncio.to_thread(transfer_reader))
            except Exception as exc:                          # noqa: BLE001 - 流水失败不挡 dd
                store.log_event(
                    "dd_update_failed",
                    "出入金流水读取失败，按无流水推进（若该时段有出入金，TWR 会失真）: "
                    f"{type(exc).__name__}: {exc}",
                    data={"reason": "flows_unavailable"}, ts=time.time())
        try:
            state = DrawdownGuard(store, limits=_limits()).update(
                equity=float(equity), ts=time.time(), flows=flows)
            logger.info("dd advanced: index=%.6f peak=%.6f action=%s",
                        state.index, state.peak, state.action)
        except Exception as exc:                              # noqa: BLE001 - DD 不挂掉 tick
            store.log_event("dd_update_failed",
                            f"回撤推进失败: {type(exc).__name__}: {exc}",
                            data={"reason": "update_failed"}, ts=time.time())

    def _close() -> None:
        """关掉 tick 里自建的 store（**第三个**连接）。

        runner 是它唯一的持有者：`TickScheduler.close()` 发现 runner 上有 `close` 就调它。
        不关的话，只要调度器跑过至少一轮，进程退出就再多漏一个 `sqlite3.Connection`。
        """
        store, box["store"] = box["store"], None
        closer = getattr(store, "close", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception as exc:                              # noqa: BLE001 - 关闭路径
            logger.warning("could not close the tick runner store: %s", exc)

    _tick.close = _close                                     # type: ignore[attr-defined]

    sources_getter()                                          # 保持接口对称（见 docstring）
    return _tick


# 模块级 app：`uvicorn app.main:app` 用它。
#
# 刻意**不**在 import 时读凭据 / 连库：`/health` 必须永远 200（T1 契约），而容器可能
# 先起服务、后注入环境变量。DB 与 runner 都是懒建的。`market_source=True` 只在这里
# 打开：生产面板的 K 线走 `app.market` 公开接口（懒建，首个请求才发 HTTP）。
app = create_app(enable_scheduler=(os.environ.get("PANEL_SCHEDULER", "1") != "0"),
                 market_source=True)
