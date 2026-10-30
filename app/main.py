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
`app.store`，以及 `app.notify`（推送出口，纯 stdlib：只有 urllib/threading）。也就是说：
一个没有凭据的部署（面板 / 演练 / 预览）能把这个进程完整跑起来，而**私钥路径根本不在
import 图里**。

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
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app import notify, views, watchdog
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


async def _warm_panel_state(cache: "PanelCache", symbols: Sequence[str], *,
                            reader: Any = None, prices: Any = None) -> None:
    """启动时把账户读数与现价塞进面板缓存（**只读，绝不下单**）。

    为什么需要：面板的现价/挂单/权益都来自 `PanelCache`，而它只由 tick 填。重启后到下一轮
    tick 之间（最长 30 分钟）面板会显示「没有现价」「挂单读数尚未采集」两条 —— 看起来像
    故障，其实只是冷的。这里在启动后立刻读一次（只读接口 + 公开盘口），把窗口压到几秒。

    `reader` / `prices` 可注入（测试用）；凭据缺失或网络失败只记日志，不影响只读面板。
    """
    try:
        if reader is None:
            from app.config import Config
            from app.executor import LighterAccountReader
            from app.exchange import ExchangeClient

            cfg = Config.from_env()
            # dry_run=True：这个客户端**只用来签 auth token 读挂单**，任何下单都会被 LiveGate 拦。
            exchange = ExchangeClient.from_config(cfg, env=os.environ, dry_run=True)
            reader = LighterAccountReader(base_url=cfg.base_url,
                                          account_index=cfg.account_index,
                                          auth_provider=lambda: exchange.auth.get())
    except Exception:                                     # noqa: BLE001 - 没凭据就不预热
        logger.info("panel state warm-up skipped: credentials not configured")
        return

    equity = None
    orders = None
    positions: Dict[str, Any] = {}
    error = ""
    try:
        account = await asyncio.to_thread(_read_account_sync, reader)
        equity = getattr(account, "equity", None)
        orders = [o.as_dict() for o in (getattr(account, "orders", ()) or ())]
        # 交易所口径的持仓读数（浮盈/仓位价值/强平价）：重启后到第一轮 tick 之间面板也能
        # 显示真实浮盈，而不是拿「最后一根 4h 收盘」现算出来的 0。
        positions = {p.symbol: p.as_dict() for p in
                     (getattr(account, "positions", ()) or ())}
        error = str(getattr(account, "orders_error", "") or "")
    except Exception as exc:                              # noqa: BLE001
        logger.warning("panel state warm-up (account) failed", exc_info=True)
        error = f"{type(exc).__name__}: {exc}"

    mid: Dict[str, float] = {}
    try:
        if prices is not None:
            mid = {str(k).upper(): float(v) for k, v in dict(prices() or {}).items()}
        else:
            from app.market import DEFAULT_ENVIRONMENT, MarketClient

            client = MarketClient((os.environ.get("LIGHTER_ENVIRONMENT") or "").strip()
                                  or DEFAULT_ENVIRONMENT)
            for symbol in symbols:
                try:
                    value = await asyncio.to_thread(
                        lambda s=symbol: client.order_book(s, depth=5).mid)
                except Exception:                         # noqa: BLE001 - 单个标的失败不影响其它
                    logger.warning("panel state warm-up: no mid for %s", symbol, exc_info=True)
                    continue
                if value:
                    mid[str(symbol).upper()] = float(value)
    except Exception:                                     # noqa: BLE001
        logger.warning("panel state warm-up (prices) failed", exc_info=True)

    if equity is None and not mid and orders is None:
        return
    cache.update(equity=equity, orders=orders, prices=mid or None,
                 positions=positions or None,
                 ts=time.time(), error=error)
    logger.info("panel state warmed: equity=%s orders=%s prices=%s positions=%s",
                equity, None if orders is None else len(orders), len(mid), len(positions))


def _read_account_sync(reader: Any) -> Any:
    """在**没有事件循环**的线程里调 `reader.read()`（协程）。

    `LighterAccountReader.read()` 是 async 的（内部已经 `to_thread` 做网络），这里在预热线程
    里用 `asyncio.run` 跑它 —— 预热本来就在 `to_thread` 里，互不干扰。
    """
    return asyncio.run(reader.read())


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
        #: 交易所侧持仓读数（symbol → ExchangePosition.as_dict()）。面板的「浮盈/现价/强平」
        #: 用**交易所口径**：本地按 4h 收盘现算会滞后最多一根 bar（曾因此在仓位表显示 0 盈亏）。
        self.positions_value: Dict[str, Dict[str, Any]] = {}
        #: 「有持仓的标的」的最新盘口中间价（symbol → {price, ts}）。后台每 ~60s 刷一次，
        #: 只刷有仓的（0~3 个请求/分钟）；空仓标的仍用最后一根 4h 收盘——用户拍板的平衡口径。
        self.marks_value: Dict[str, Dict[str, float]] = {}
        #: 最近一次**当前资金费率**（symbol → {rate, exchange, ts}）。Lighter 周期 1 小时。
        self.funding_value: Dict[str, Dict[str, Any]] = {}
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
               positions: Optional[Mapping[str, Any]] = None,
               ts: Optional[float] = None, error: str = "") -> None:
        if equity is not None:
            self.equity_value = float(equity)
        if orders is not None:
            self.orders_value = [dict(o) for o in orders]
        if prices is not None:
            self.prices_value = {str(k).upper(): float(v) for k, v in dict(prices).items()}
        if positions is not None:
            self.positions_value = {str(k).upper(): dict(v) for k, v in dict(positions).items()}
        self.updated_at = float(ts) if ts is not None else time.time()
        self.error = str(error or "")

    # ---------------------------------------------------------------- 读（注入给 views）

    def equity(self) -> Optional[float]:
        return self.equity_value

    def orders(self) -> List[Dict[str, Any]]:
        if self.orders_value is None:
            # 冷启动（刚重启、还没跑过 tick）和「本进程没有凭据」都会走到这里，但两者的
            # 处置完全不同：前者等下一轮 tick 或启动预热即可，后者要去看配置。
            # 文案里把「会自动恢复」写出来，免得被当成故障。
            raise RuntimeError("交易所挂单读数尚未采集（冷启动窗口：等启动预热或下一轮 "
                               "tick 后自动出现；若长期如此，检查本进程的凭据配置）")
        return list(self.orders_value)

    def prices(self) -> Dict[str, float]:
        return dict(self.prices_value)

    def set_mark(self, symbol: str, price: Any, *, ts: Optional[float] = None) -> None:
        """记一笔**公开盘口**的最新中间价（只给「有持仓」的标的用）。"""
        try:
            value = float(price)
        except (TypeError, ValueError):
            return
        if value != value or value <= 0 or value in (float("inf"), float("-inf")):
            return
        self.marks_value[str(symbol).upper()] = {
            "price": value, "ts": float(ts if ts is not None else time.time())}

    def marks(self) -> Dict[str, Dict[str, float]]:
        """最近一次盘口中间价（symbol → {price, ts}）。可能是空的 = 还没刷新/没有持仓。"""
        return {k: dict(v) for k, v in self.marks_value.items()}

    def set_funding(self, symbol: str, rate: Any, *, exchange: str = "lighter",
                    ts: Optional[float] = None) -> None:
        """记一笔**当前资金费率**（Lighter 的周期是 1 小时）。"""
        try:
            value = float(rate)
        except (TypeError, ValueError):
            return
        if value != value:
            return
        self.funding_value[str(symbol).upper()] = {
            "rate": value, "exchange": str(exchange or ""), "ts": float(ts if ts is not None else time.time())}

    def funding(self) -> Dict[str, Dict[str, Any]]:
        """最近一次资金费率（symbol → {rate, exchange, ts}）。空 = 还没刷新。"""
        return {k: dict(v) for k, v in self.funding_value.items()}

    def positions(self) -> Dict[str, Dict[str, Any]]:
        """交易所侧持仓读数（可能为空 dict = 还没采集过/空仓，不是「未知」）。"""
        return {k: dict(v) for k, v in self.positions_value.items()}


def isolated_margin_on(env: Optional[Mapping[str, str]] = None) -> bool:
    """逐仓保证金开关，默认**开**（`LIGHTER_ISOLATED_MARGIN=0` 关）。

    开 = 每次开仓前把市场切到 isolated + 目标杠杆（幂等），成交后划入抵押
    （≈名义/杠杆）。账户 2026-10-29 起三个市场已切逐仓，机器人**必须**配抵押；
    关掉等于回退全仓时代的行为（市场级设置仍在，但不再显式划入）。
    """
    raw = str((env if env is not None else os.environ).get("LIGHTER_ISOLATED_MARGIN", "1")
              or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def stop_guard_on(env: Optional[Mapping[str, str]] = None) -> bool:
    """止损守护开关，默认**开**（`LIGHTER_STOP_GUARD=0` 关）。

    只影响「补挂/换单」这一个保护动作：关了它，行为退回到 2026-10-29 之前
    （止损只在开仓那一刻挂，到期后无人补）。资金路径的其他部分不受影响。
    """
    raw = str((env if env is not None else os.environ).get("LIGHTER_STOP_GUARD", "1")
              or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def entry_slippage_bps(env: Optional[Mapping[str, str]] = None) -> Optional[int]:
    """开仓「最差价」的滑点带宽（bps），默认 30（`LIGHTER_ENTRY_SLIPPAGE_BPS`）。

    为什么留这个旋钮：tick 在 4h bar 收盘后 ~3 分钟才发单，而开仓是 `MARKET+IOC+最差价`。
    实测（Lighter 1m K 线，近 10 天 60 个 4h 边界）：收盘后 3.5 分钟的价格漂移
    p50=10bp / p90=18bp / max=58bp，**>30bp 的占 5%**（而且三例全部发生在 20:00 UTC）。
    带宽越窄 → 越贴 bar 收盘、越容易零成交（现在会记 `entry_not_filled` 并报警，不再记假仓）；
    带宽越宽 → 更不容易漏信号，但追价更狠。**默认 30 不动**，要不要放宽是策略口径决定。
    """
    raw = (env if env is not None else os.environ).get("LIGHTER_ENTRY_SLIPPAGE_BPS", "")
    try:
        value = int(str(raw).strip()) if str(raw).strip() else 30
    except ValueError:
        return None                     # None = 用 ExchangeClient 的默认（30bp）
    return value if value > 0 else None


def fill_confirm_seconds(env: Optional[Mapping[str, str]] = None) -> float:
    """开仓后等「账户里真的出现仓位」的窗口（秒），默认 12（`LIGHTER_FILL_CONFIRM_S`）。

    `sendTx` 的 code=200 只表示进了 sequencer，IOC 单可能「执行成功但零成交」——
    开仓必须等到交易所侧真的出现仓位才记账（2026-10-30 幽灵仓事故的直接修法）。
    `0` = 只读一次（立刻判）。负数/脏值退回默认。
    """
    raw = (env if env is not None else os.environ).get("LIGHTER_FILL_CONFIRM_S", "")
    try:
        value = float(str(raw).strip()) if str(raw).strip() else 12.0
    except ValueError:
        return 12.0
    return value if value >= 0 else 12.0


def grace_ticks(env: Optional[Mapping[str, str]] = None) -> int:
    """`grace` 连续多少轮仍未结案就作废幽灵仓，默认 2（`LIGHTER_GRACE_TICKS`）。

    成交回报里能找到真实平仓成交时**第一轮**就按 stop/exit 结案，不看这个计数。
    """
    raw = (env if env is not None else os.environ).get("LIGHTER_GRACE_TICKS", "")
    try:
        value = int(str(raw).strip()) if str(raw).strip() else 2
    except ValueError:
        return 2
    return value if value >= 1 else 2


def max_signal_age_minutes(env: Optional[Mapping[str, str]] = None) -> float:
    """信号时效窗（分钟），默认 30（`LIGHTER_MAX_SIGNAL_AGE_MIN`，`0` = 关闭）。

    只允许在「信号 bar 收盘后 N 分钟内」建仓。tick 固定在 :03/:33，所以 30 分钟等于
    「**只有 4h 收盘后的第一轮**能开仓」。超龄不当失败：当轮不建仓、落
    `entry_skipped_stale_signal` 事件，离场/止损/熔断一律不受影响。

    为什么是 30：实测（Lighter SOL 1m，14 天 84 个 4h 边界，相对 bar 收盘价的漂移）
    +3 分钟 p50 8.3bp / p90 19.3bp / 超 30bp 占 5%；+33 分钟 p50 20.8bp / p90 63.5bp /
    超 30bp 占 **36%**；+63 分钟 p50 24.8bp / p90 73.0bp / 超 30bp 占 45%。也就是说
    第二轮的漂移中位数是第一轮的 2.5 倍，超过三成的信号会吃掉整个 30bp 滑点带宽。

    空 = 30；`0` = 关闭；负数/脏值退回 30（脏配置宁可保守，不要静默关掉闸门）。
    """
    raw = (env if env is not None else os.environ).get("LIGHTER_MAX_SIGNAL_AGE_MIN", "")
    try:
        value = float(str(raw).strip()) if str(raw).strip() else 30.0
    except ValueError:
        return 30.0
    if not math.isfinite(value):        # nan / ±inf（"inf"、"1e999" 按脏值处理，别静默关闸）
        return 30.0
    return value if value >= 0 else 30.0


def merge_accepted_stops(cache: PanelCache, report: Any) -> int:
    """把本轮「交易所已受理」的止损单并进面板的挂单缓存；返回并入张数。

    为什么需要：tick **开头**读账户挂单，而止损单是开仓之后几秒才下的 ⇒ 面板在下一轮
    tick（最长 30 分钟）之前完全看不到它，会把刚挂好的止损报成「没挂」。这里只并
    `ok=True`（= API 已受理 code=200）的那张，并标 `pending_confirm`；下一轮真读数覆盖它。

    **纯观测**：只写面板缓存，不参与任何交易决策（对账走 `ExchangeAccount.orders` 的实时值）。
    `orders_value is None`（从未采集过挂单）时什么都不做 —— 保持「未知」，不编造。
    """
    current = cache.orders_value
    if current is None:
        return 0
    merged = [dict(o) for o in current]
    seen = {o.get("client_order_index") for o in merged}
    added = 0
    for outcome in getattr(report, "outcomes", ()) or ():
        stop = getattr(outcome, "stop_order", None) or {}
        if not stop.get("ok") or stop.get("dry_run"):
            continue
        coi = stop.get("client_order_index")
        if coi is None or coi in seen:
            continue
        merged.append({"symbol": str(stop.get("symbol") or "").upper(),
                       "market_id": stop.get("market_id"),
                       "client_order_index": coi,
                       "trigger_price_int": stop.get("trigger_price_int"),
                       "price_int": stop.get("price_int"),
                       "qty_int": stop.get("qty_int"),
                       "reduce_only": True, "order_id": "", "status": "pending",
                       "pending_confirm": True})
        seen.add(coi)
        added += 1
    # 止损守护本轮补挂/换单的那几张（`TickReport.stop_guards`）：同样只写观测缓存。
    for guard in getattr(report, "stop_guards", ()) or ():
        item = dict(guard)
        replaced = item.pop("replaced_coi", None)
        coi = item.get("client_order_index")
        if coi is None:
            continue
        if replaced is not None:
            merged = [o for o in merged if o.get("client_order_index") != replaced]
        if coi in seen:
            continue
        merged.append(item)
        seen.add(coi)
        added += 1
    if added:
        cache.update(orders=merged, error=getattr(cache, "error", ""))
        logger.info("[panel] 本轮新挂的止损单已并入面板缓存（%d 张，等下一轮读数复核）", added)
    return added


def mark_refresh_interval_s(env: Optional[Mapping[str, str]] = None) -> float:
    """有持仓标的的盘口刷新间隔（秒）。0 / 负数 = 关掉（退回「只有 tick 时的标记价」）。"""
    raw = str((env if env is not None else os.environ).get("PANEL_MARK_REFRESH_S", "60")
              or "60").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 60.0
    if value != value or value < 0:                       # NaN / 负数
        return 60.0
    return value


async def _refresh_position_marks(cache: "PanelCache", *,
                                  store: Callable[[], Any], market: Any,
                                  interval_s: float = 60.0,
                                  sleep: Callable[[float], Any] = asyncio.sleep) -> None:
    """后台只给**有持仓**的标的刷盘口中间价（空仓标的继续用最后一根 4h 收盘）。

    为什么是这个口径（用户 2026-10-29 拍板「有仓按实时价、没仓按 4h 收盘，比较平衡」）：
    - 有仓的标的浮盈/距止损都要「现在」，30 分钟一轮 tick 的标记价太旧；
    - 空仓的标的只看个大概行情，为了它每分钟多打请求不值。
    成本：0~3 个公开盘口请求/分钟（无鉴权、无签名、不在请求路径上）。

    **只写观测缓存**：交易路径（tick/下单/对账/止损守护）一律不读 `cache.marks()`。
    任何异常只记日志，绝不抛出（后台任务死掉不能让面板变慢或崩）。
    """
    while True:
        symbols: List[str] = []
        try:
            rows = store().open_positions() or ()
            symbols = [str(r.get("symbol") or "").strip().upper() for r in rows
                       if str(r.get("symbol") or "").strip()]
        except Exception:                                  # noqa: BLE001 - DB 抖动不算故障
            logger.warning("mark refresh: could not read open positions", exc_info=True)
        for symbol in dict.fromkeys(symbols):              # 去重且保序
            try:
                quote = await asyncio.to_thread(
                    lambda s=symbol: market.order_book(s, depth=5).mid)
            except Exception:                              # noqa: BLE001 - 单个标的失败不影响其它
                logger.warning("mark refresh: no mid for %s", symbol, exc_info=True)
                continue
            if quote:
                cache.set_mark(symbol, quote)
        try:
            await sleep(max(5.0, float(interval_s)))
        except asyncio.CancelledError:                     # pragma: no cover - 关闭路径
            return


def funding_refresh_interval_s(env: Optional[Mapping[str, str]] = None) -> float:
    """资金费率刷新间隔（秒），默认 600。0 = 关（面板不显示费率）。费率按小时结算，不必刷太快。"""
    raw = str((env if env is not None else os.environ).get("PANEL_FUNDING_REFRESH_S", "600")
              or "600").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 600.0
    if value != value or value < 0:
        return 600.0
    return value


async def _refresh_funding_rates(cache: "PanelCache", *, market: Any,
                                 interval_s: float = 600.0,
                                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
    """后台刷「当前资金费率」（公开端点，一次请求拿全部市场）。

    为什么要它：仓位表要回答「这个仓每小时被收/收多少资金费」。费率列表是多交易所并列的，
    只取 `exchange='lighter'` 那一行（拿 binance 的当 Lighter 的会误导人）。
    **只写观测缓存**；任何异常只记日志，绝不抛。
    """
    while True:
        try:
            rates = await asyncio.to_thread(market.funding_rates)
        except Exception:                                  # noqa: BLE001 - 拿不到就沿用上一次
            logger.warning("funding refresh failed", exc_info=True)
            rates = None
        for symbol, rate in (rates or {}).items():
            cache.set_funding(symbol, rate, exchange="lighter")
        try:
            await sleep(max(30.0, float(interval_s)))
        except asyncio.CancelledError:                     # pragma: no cover - 关闭路径
            return


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

_SHARED_EXCHANGE_CLIENTS: Dict[Tuple[str, int], Any] = {}


def _shared_exchange_client(config: Any, *, confirm_live: Optional[int] = None,
                            extra: Optional[Mapping[str, Any]] = None) -> Any:
    """进程级单例 `ExchangeClient`（对抗审查 P2-2，2026-10-30）。

    每个实例持有**独立的** `NonceStream`（懒取一次交易所 next_nonce 后本地自增）。
    过去 flatten 每次新建 client：tick 的单还在途（已签名未上链确认）时它向交易所
    查到的 next_nonce 与 tick 已用的相同 → 互相 `nonce_conflict`，紧急清仓单被自己的
    第二把 client 拒掉。tick 与 flatten 共用同一把 client 后这条并发路径消失。
    键 = (environment, confirm_live)；生产单账户，实际最多一份。
    """
    key = (str(getattr(config, "environment", "") or ""), int(confirm_live or 0))
    client = _SHARED_EXCHANGE_CLIENTS.get(key)
    if client is None:
        from app.exchange import ExchangeClient      # noqa: PLC0415 - 私钥模块懒加载
        client = ExchangeClient.from_config(config, env=os.environ, dry_run=False,
                                            confirm_live=confirm_live,
                                            **(dict(extra or {})))
        _SHARED_EXCHANGE_CLIENTS[key] = client
    return client


async def _flatten_confirm_flat(reader: Any, symbol: str, *, timeout_s: float = 15.0,
                                interval_s: float = 1.0) -> bool:
    """轮询账户读数直到该标的的仓位**真的**消失（对抗审查 P1-2：受理 ≠ 成交）。

    读失败按「未确认」处理（fail-closed：不撤挂单）并重试到超时；超时返回 False，
    由调用方把该标的标成 failed 并**保留**它的挂单（撤掉还在位的止损 = 制造裸仓）。
    """
    attempts = max(1, int(timeout_s / interval_s) + 1)
    for attempt in range(attempts):
        snap = None
        try:
            snap = await reader.read()
        except Exception:                                     # noqa: BLE001 - 读失败 ≠ 没仓
            snap = None
        pos_for = getattr(snap, "position_for", None)
        if callable(pos_for):
            pos = pos_for(symbol)
            if pos is None or float(getattr(pos, "qty", 0) or 0) <= 0:
                return True
        if attempt + 1 < attempts:
            await asyncio.sleep(interval_s)
    return False



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

    async def _run() -> Dict[str, Any]:
        client = _shared_exchange_client(
            config,
            confirm_live=config.account_index if confirm_live is None else confirm_live)
        # 实时 mid 锚与成交确认（对抗审查 P1-2，2026-10-30）：与 tick 侧同一套组件。
        # ① 过去用 4h 前的入场价 ±30bp 当清仓限价 —— 急跌/急拉超过 30bp 时（恰恰是
        #    最需要清仓的时刻）限价落在簿的另一侧 → 零成交，却报 ok=true；
        # ② 过去 `ok` 只取「进了 sequencer」，没有任何成交确认，随后无条件撤掉全部
        #    挂单（包括仍在位的止损）—— 紧急按钮自己制造裸仓。
        from app.executor import LighterAccountReader      # noqa: PLC0415 - 懒加载
        from app.market import MarketClient                # noqa: PLC0415
        from app.sizing import to_int_price                # noqa: PLC0415
        market = MarketClient(config.environment)
        reader = LighterAccountReader(base_url=config.base_url,
                                      account_index=int(config.account_index),
                                      auth_provider=lambda: client.auth.get())
        steps: List[Dict[str, Any]] = []
        provisional: List[str] = []
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
            # 锚实时 mid；取不到就退回入场价（=旧行为）并在 steps 里留痕
            limit_ref: Optional[int] = None
            try:
                snap = await asyncio.to_thread(market.snapshot, symbol)
                raw = getattr(snap, "mid", None) or getattr(snap, "price", None)
                mid = float(raw) if raw is not None else 0.0
                if mid > 0:
                    limit_ref = to_int_price(mid, int(row.get("price_decimals") or 0))
            except Exception as exc:                          # noqa: BLE001 - 降级不阻断
                steps.append({"symbol": symbol, "ok": None,
                              "error": f"实时报价不可用，清仓限价退回入场价锚: "
                                       f"{type(exc).__name__}"})
            try:
                result = await client.place_exit(
                    symbol=symbol, market_id=int(market_id), side=side, qty_int=qty_int,
                    price_decimals=int(row.get("price_decimals") or 0),
                    exit_price=float(row.get("entry_price") or 0.0),
                    limit_price_int=limit_ref)
            except Exception as exc:                          # noqa: BLE001 - 逐条记录
                steps.append({"symbol": symbol, "ok": False,
                              "error": f"{type(exc).__name__}: {exc}"})
                continue
            steps.append({"symbol": symbol, "ok": bool(getattr(result, "ok", False)),
                          "code": getattr(result, "code", None),
                          "error": getattr(result, "error", "")})
            if getattr(result, "ok", False):
                provisional.append(symbol)
        # 受理 ≠ 成交：确认「交易所真的没仓了」才允许撤挂单；没平掉的标的保留其
        # 止损单（撤了 = 制造裸仓），并明确标 failed。
        still_open: List[str] = []
        for symbol in provisional:
            if await _flatten_confirm_flat(reader, symbol):
                closed.append(symbol)
            else:
                still_open.append(symbol)
                steps.append({"symbol": symbol, "ok": False,
                              "error": "清仓单被受理但确认期内仓位仍在交易所 —— "
                                       "未撤该标的挂单（止损单保留）"})
        cancel = None
        if not still_open:
            try:
                cancel = await client.cancel_all_orders()
            except Exception as exc:                          # noqa: BLE001
                steps.append({"symbol": "*", "ok": False,
                              "error": f"撤单失败: {type(exc).__name__}: {exc}"})
        ok = (all(step.get("ok") for step in steps if step.get("ok") is not None)
              if steps else True) and not still_open
        error = "" if ok else "; ".join(
            f"{step.get('symbol')}: {step.get('error')}"
            for step in steps if step.get("ok") is False)[:400]
        return {"ok": bool(ok), "closed": closed,
                "cancelled": bool(getattr(cancel, "ok", False)), "steps": steps,
                "error": error, "still_open": still_open}

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
                         positions=cache.positions, marks=cache.marks,
                         funding=cache.funding,
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
            app.state.warm_task = asyncio.create_task(_warm_panel_bars(cache, symbols))
            # 账户/现价也预热一次：否则重启后到下一轮 tick 之间面板会显示
            # 「没有现价」「挂单读数尚未采集」两条（看起来像故障，其实只是冷的）。
            app.state.warm_state_task = asyncio.create_task(_warm_panel_state(cache, symbols))
            logger.info("panel warm-up scheduled (bars + account/prices)")
            # 有持仓标的的盘口中间价：后台每 PANEL_MARK_REFRESH_S 秒刷一次（只刷有仓的）。
            # 用户拍板的口径：有仓按实时价、空仓按最后一根 4h 收盘。0 = 关。
            # 资金费率（公开端点，一次拿全部市场）：每 PANEL_FUNDING_REFRESH_S 秒刷一次。
            mark_interval = mark_refresh_interval_s()
            funding_interval = funding_refresh_interval_s()
            if mark_interval > 0 or funding_interval > 0:
                try:
                    from app.market import DEFAULT_ENVIRONMENT, MarketClient

                    mark_env = ((os.environ.get("LIGHTER_ENVIRONMENT") or "").strip()
                                or DEFAULT_ENVIRONMENT)
                    price_client = MarketClient(mark_env)
                    if mark_interval > 0:
                        app.state.mark_task = asyncio.create_task(
                            _refresh_position_marks(cache, store=factory, market=price_client,
                                                    interval_s=mark_interval))
                        logger.info("position mark refresh scheduled (every %ss)", mark_interval)
                    if funding_interval > 0:
                        app.state.funding_task = asyncio.create_task(
                            _refresh_funding_rates(cache, market=price_client,
                                                   interval_s=funding_interval))
                        logger.info("funding rate refresh scheduled (every %ss)", funding_interval)
                except Exception:                          # noqa: BLE001 - 起不来只影响新鲜度
                    logger.warning("could not start the price/funding refresh tasks", exc_info=True)
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
            mark_task = getattr(_app.state, "mark_task", None)
            if mark_task is not None:
                mark_task.cancel()
            funding_task = getattr(_app.state, "funding_task", None)
            if funding_task is not None:
                funding_task.cancel()

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

    # -------------------------------------------------- 手机主屏图标 / PWA 静态资源
    # 2026-10-29：用户把面板加到手机桌面当 app 用，补 apple-touch-icon / favicon /
    # manifest（图标 = 靛紫渐变 $ 圆环，随镜像打包，自包含约束不受影响）。
    _static_dir = Path(__file__).resolve().parent / "static"

    @app.get("/apple-touch-icon.png")
    def apple_touch_icon() -> FileResponse:
        return FileResponse(_static_dir / "apple-touch-icon.png", media_type="image/png")

    @app.get("/favicon.ico")
    def favicon_ico() -> FileResponse:
        return FileResponse(_static_dir / "favicon.ico", media_type="image/x-icon")

    @app.get("/favicon-32.png")
    def favicon_32() -> FileResponse:
        return FileResponse(_static_dir / "favicon-32.png", media_type="image/png")

    @app.get("/icon-192.png")
    def icon_192() -> FileResponse:
        return FileResponse(_static_dir / "icon-192.png", media_type="image/png")

    @app.get("/icon-512.png")
    def icon_512() -> FileResponse:
        return FileResponse(_static_dir / "icon-512.png", media_type="image/png")

    @app.get("/manifest.webmanifest")
    def webmanifest() -> FileResponse:
        return FileResponse(_static_dir / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @app.get("/symbols")
    def symbols_block() -> JSONResponse:
        return JSONResponse(content=panel.symbols_block())

    @app.get("/events")
    def events() -> JSONResponse:
        return JSONResponse(content=panel.events())

    @app.get("/records", response_class=HTMLResponse)
    def records_page() -> HTMLResponse:
        """记录页：资金概览 + 订单明细 + 执行器事件流（首页不再渲染事件流）。"""
        return HTMLResponse(content=panel.records_page())

    @app.get("/api/records")
    def records_json() -> JSONResponse:
        """记录页的 JSON 形态（只读，排查/脚本用）。"""
        return JSONResponse(content=panel.records())

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


def _stored_flows_for_replay(store: Any) -> List[Dict[str, float]]:
    """库里已持久化的推断流水 → 重放用流水列表（锚点之后、左开）。

    `update()` 每轮都从锚点**全量重放**权益窗口，所以历史流水每轮都要重新喂进去，
    不是只喂当轮新增的那条。锚点本身那一格的流水已体现在锚点权益里（左开区间），
    `ts == anchor_ts` 的行要剔掉。库读失败 → 空列表（流水缺失只失真，不挂 tick）。
    """
    stored = store.dd_state() or {}
    anchor_ts = stored.get("anchor_ts")
    try:
        rows = store.flows(since=float(anchor_ts)) if anchor_ts else store.flows()
    except Exception:                                        # noqa: BLE001
        return []
    floor = float(anchor_ts) if anchor_ts else 0.0
    out: List[Dict[str, float]] = []
    for row in rows or ():
        ts = row.get("ts") if isinstance(row, Mapping) else None
        amount = row.get("amount") if isinstance(row, Mapping) else None
        try:
            ts_f, amount_f = float(ts), float(amount)        # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if ts_f > floor and amount_f != 0.0:
            out.append({"ts": ts_f, "amount": amount_f})
    return out


def _inferred_flow_for_tick(store: Any, executor: Any) -> Optional[Dict[str, float]]:
    """本轮 tick 的流水推断：权益跳变 + 无持仓 + 无成交 ⇒ 出入金（落库 + 落事件）。

    背景：`/transfer/history` 对 UDA 桥接入金恒空（`app/executor.infer_transfer_flow`
    的 docstring 有完整因果）。返回 `None` = 没有新流水（含幂等命中 —— 那条流水已在
    `_stored_flows_for_replay` 里，不能再 append 一遍，否则本轮重放重复计息）。
    """
    from app.executor import infer_transfer_flow   # noqa: PLC0415 - 延迟 import（模块边界）

    account = getattr(executor, "last_account", None)
    if account is None:
        return None
    try:
        prev = store.last_equity()
    except Exception:                                        # noqa: BLE001
        return None
    if not prev:
        return None
    prev_ts = prev.get("ts")
    prev_equity = prev.get("equity")
    fills_since = True                                       # 读不到成交记录时按「有成交」保守处理
    try:
        rows = store.trades(since=float(prev_ts or 0.0), limit=1000)
        fills_since = any(float(t.get("ts") or 0.0) > float(prev_ts or 0.0)
                          for t in rows or ())
    except Exception:                                        # noqa: BLE001
        fills_since = True
    flow = infer_transfer_flow(prev_ts=prev_ts, prev_equity=prev_equity,
                               account=account, fills_since_prev=fills_since)
    if flow is None:
        return None
    try:
        recorded = store.record_flow(
            flow["amount"], ts=flow["ts"], source="inferred",
            note=(f"equity {float(prev_equity):.6f} -> {float(account.equity):.6f}, "
                  "no fills / no positions in interval"))
    except Exception:                                        # noqa: BLE001 - 流水落库失败不挂 tick
        return None
    if not recorded:
        return None                                          # 幂等命中，库里已有
    direction = "入金" if float(flow["amount"]) > 0 else "出金"
    store.log_event(
        "flow_inferred",
        f"权益 {float(prev_equity):.6f} -> {float(account.equity):.6f}，区间内无成交、"
        f"当前无持仓，推断为{direction} {abs(float(flow['amount'])):.6f} USDC"
        "（/transfer/history 看不到 UDA 流水，由权益跳变推断）",
        data={"ts": flow["ts"], "amount": flow["amount"],
              "prev_equity": float(prev_equity), "equity": float(account.equity)},
        ts=time.time())
    return flow


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
    from app.market import MarketClient

    config = Config.from_env()
    market = MarketClient(config.environment)
    extra: Dict[str, Any] = {}
    _slip = entry_slippage_bps()
    if _slip is not None:
        extra["entry_slippage_bps"] = _slip
    exchange = _shared_exchange_client(config, confirm_live=config.account_index,
                                       extra=extra)
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
        # 通知出口：与 HL 侧同渠道、同 env 键名（WECHAT_WEBHOOK_URL /
        # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）。三个 env 全缺 → None，执行器
        # `_alert` 退回「只打日志」，行为与接入前逐字节一致（要静默就清空 env，
        # 不引入新开关）。构造是纯内存操作（不发网络），HTTP 在 `app.notify` 的
        # daemon 线程里跑，所以这里**不给 tick 主路径加任何延迟**。
        executor = Executor(store=store, market=market, exchange=exchange, reader=reader,
                            symbols=symbols, limits=_limits(),
                            alert=notify.notifier_from_env(),
                            stop_guard=stop_guard_on(),
                            margin_isolated=isolated_margin_on(),
                            fill_confirm_s=fill_confirm_seconds(),
                            grace_ticks=grace_ticks(),
                            max_signal_age_min=max_signal_age_minutes())
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
                         positions={p.symbol: p.as_dict() for p in
                                    (getattr(account, "positions", ()) or ())},
                         prices=dict(getattr(executor, "_prices", {}) or {}),
                         ts=time.time(),
                         error=str(getattr(account, "orders_error", "") or ""))
        # 刚并进缓存的那张止损单是开仓之后才下的：tick 开头那次读数看不到它。
        merge_accepted_stops(cache, report)
        await _advance_drawdown(store, executor, reader)
        return report

    async def _advance_drawdown(store: Any, executor: Any, reader: Any) -> None:
        """`docs/DD_WATCHDOG.md` §6 的接线项：每轮 tick 在 `run_once()` 之后推进 TWR。

        首次没有 `dd_state` 时 `update()` 内部走 `bootstrap()`（以库里最早的权益点为锚）。
        权益未知（账户读失败）就跳过本轮推进并落事件 —— 面板/风控会按
        「回撤口径不可用」fail-closed，绝不拿旧 equity 硬算。流水读不到则按
        「无流水」推进并落事件（有出入金的时段 TWR 会失真，但开仓闸不会瞎掉）。

        流水三个来源，全部喂给重放（`update()` 每轮从锚点重放全窗口）：
        ① `reader.transfers()`（`/transfer/history`）—— 对 UDA 入金恒空，但账户间
           转账/出金可能出现，留着；
        ② 库里已持久化的推断流水（`account_flows`）—— 每轮都要喂回去，否则下一轮
           重放又把入金算成盈利；
        ③ 本轮新推断的流水（`_inferred_flow_for_tick`，落库 + 落事件）。
        """
        equity = getattr(getattr(executor, "last_account", None), "equity", None)
        if equity is None:
            store.log_event("dd_update_failed", "权益未知，本轮未推进回撤（fail-closed）",
                            data={"reason": "equity_unavailable"}, ts=time.time())
            return
        flows: List[Mapping[str, float]] = []
        transfer_reader = getattr(reader, "transfers", None)
        if callable(transfer_reader):
            try:
                flows.extend(await asyncio.to_thread(transfer_reader))
            except Exception as exc:                          # noqa: BLE001 - 流水失败不挡 dd
                store.log_event(
                    "dd_update_failed",
                    "出入金流水读取失败，按无流水推进（若该时段有出入金，TWR 会失真）: "
                    f"{type(exc).__name__}: {exc}",
                    data={"reason": "flows_unavailable"}, ts=time.time())
        flows.extend(_stored_flows_for_replay(store))
        inferred = _inferred_flow_for_tick(store, executor)
        if inferred is not None:
            flows.append(inferred)
        try:
            state = DrawdownGuard(store, limits=_limits()).update(
                equity=float(equity), ts=time.time(), flows=tuple(flows))
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
