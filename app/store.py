"""状态持久化层（SQLite，纯 stdlib `sqlite3`，**不发任何网络请求**）。

职责
----
把「当前持仓 / 成交流水 / 权益曲线 / 每日计数器 / 回撤锚点」落盘，重启后能完整恢复：

============================  ==================================================
表                            内容
============================  ==================================================
`positions`                   仓位 + 状态机（`IDLE → OPEN → CLOSING → CLOSED`）
`trades`                      成交/订单流水（开仓 / 平仓 / 止损），幂等键唯一
`equity_curve`                权益时间序列 + TWR 指数（面板曲线 / 回撤输入）
`daily_counters`              每个 UTC 日的下单计数与日初权益
`dd_anchors`                  TWR 回撤锚点（**滚动，只留 30 天**）
`dd_state`                    TWR 回撤的**持久**活状态（单行，不自动清理）
`events`                      执行器事件流（面板「最近 20 条」+ 写操作审计）
============================  ==================================================

四条硬约束
----------
1. **不发网络请求**。本模块只 import `sqlite3` / `json` / `os` / `time` / `logging`
   等 stdlib 存储原语；权益怎么从交易所取是 T8 的事（`app/state.py` 里可注入
   `equity_provider`）。这条边界让存储层可以被无网单测直接驱动，也让「面板进程」永远
   拿不到私钥路径。
2. **原子性**。复合写（开仓 = 仓位 + 成交 + 事件 + 当日计数；平仓 = 状态 + 成交 + 计数）
   全部在**一个事务**里完成，要么全落盘要么全不落盘。绝不允许出现
   「有 `OPEN` 仓位但没有开仓成交」这种半状态 —— 那会让面板显示一个无法对账的仓位。
3. **幂等**。成交按交易所 `fill_id`（没有则 `order_id + 方向 + 价 + 量`，再没有则本地
   时间键）做唯一键；仓位按 `UNIQUE(symbol, bar_ms)`。同一笔成交重复写入**不产生重复行**
   （`INSERT OR IGNORE` + 冲突后回查真实 id）。这是 T8「先 `_sync_fills` 再判 mismatch」
   的前提：重复对账是常态，不是异常。
4. **并发**。`journal_mode=WAL` + `busy_timeout`（默认 5000ms）+ 进程内 `RLock`。
   机器人单进程写、面板进程同时读 —— WAL 让读不阻塞写，`BEGIN IMMEDIATE` 让写立刻拿到
   锁，避免「先读后写」的 `SQLITE_BUSY` 死角。

回撤锚点为什么有两张表（`dd_anchors` + `dd_state`）
--------------------------------------------------
旧系统（`larkspur8506/crypto-ma-alert`）的教训：`hl_snapshots` 只保留 30 天，若每轮都
「从快照重算峰值」，那么第 31 天回撤会**凭空归零** —— 等于自动关掉熔断，而且没人会发现。
所以这里拆成两层：

* `dd_anchors`：滚动检查点，**只留 30 天**（面板要画历史，不能无限长）。会被清理。
* `dd_state`：**唯一权威**的活状态（`twr_index` / `twr_peak` / 锚点时刻与权益），单行、
  `name='live'`，**只有** `reset_dd_peak()`（= T10b 的 `POST /admin/dd/reset`，需 admin token）
  能改小它。自动清理永不触碰。

结论：**锚点被清理 ≠ 回撤归零**。锚点没了 → `dd_state` 继续给出同一个峰值；`dd_state` 也
没了 → `app/state.py` 返回 `index=peak=None`，`app/risk.py` 的 `drawdown` 闸**拒单**
（fail-closed），而不是「当作 0% 回撤放行」。详见 `docs/STATE_STORE.md`。

迁移
----
`PRAGMA user_version` 单调递增，每次迁移**在自己的事务里**执行（DDL + 版本号一起提交），
所以迁移中断不会留下「表建了一半、版本号没动」的库。新库 = 从 0 依次跑 1..N，与老库
升级走**同一组语句**，不存在「新建的库 schema 和升级后的库 schema 不一样」这种漂移。
`user_version > SCHEMA_VERSION`（代码比库旧）→ `SchemaError`，绝不猜着往下跑。

面板只读接口
------------
`Store.panel_snapshot()` 一次给齐 T10 的 6 区块需要的 DB 数据（当前持仓、权益曲线、最近
N 笔成交、当日计数、回撤状态、事件流）；`Store.audit()` 给出「半状态」不变量检查结果
（`OPEN`/`CLOSING` 仓位必须有开仓成交），T8 watchdog / T10 状态条可直接吃。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ALLOWED_TRANSITIONS", "BASE_TWR_INDEX", "DEFAULT_BUSY_TIMEOUT_MS", "DD_ANCHOR_KEEP_DAYS",
    "DD_STATE_KEY", "MIGRATIONS", "OPEN_STATES", "POSITION_STATES", "SCHEMA_VERSION",
    "TRADE_KINDS", "AuditFinding", "InvalidTransition", "MigrationError", "SchemaError",
    "StateStore", "Store", "StoreError", "WriteResult", "day_bounds", "utc_day",
]

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 常量

SCHEMA_VERSION = 2                      # 当前代码认得的库版本（PRAGMA user_version）
DEFAULT_BUSY_TIMEOUT_MS = 5000          # 面板进程同时读时，写方最多等 5s 再报 busy
BASE_TWR_INDEX = 1.0                    # TWR 指数基准（建仓/重置时 = 1.0）
DD_STATE_KEY = "live"                   # dd_state 的单行主键
DD_ANCHOR_KEEP_DAYS = 30.0              # dd_anchors 滚动保留窗口（对齐旧系统 hl_snapshots）

POSITION_STATES: Tuple[str, ...] = ("IDLE", "OPEN", "CLOSING", "CLOSED")
OPEN_STATES: Tuple[str, ...] = ("OPEN", "CLOSING")   # 交易所侧还有仓位的两个状态

#: 状态机（PLAN T6：`IDLE → OPEN → CLOSING → CLOSED`，每标的至多一仓）。
#: `CLOSING → OPEN` 是「撤单失败/离场请求被拒 → 退回持仓」的合法回退；`CLOSED` 是终态，
#: 同一标的下一笔交易**新开一行**（保留历史），不把老行改回 `OPEN`。
ALLOWED_TRANSITIONS: Mapping[str, Tuple[str, ...]] = {
    "IDLE": ("OPEN",),
    "OPEN": ("CLOSING", "CLOSED"),
    "CLOSING": ("OPEN", "CLOSED"),
    "CLOSED": (),
}

TRADE_KINDS: Tuple[str, ...] = ("entry", "exit", "stop")


class StoreError(RuntimeError):
    """存储层可预期错误（路径非法 / 只支持 sqlite / 数据不自洽 …）。"""


class SchemaError(StoreError):
    """库 schema 与本代码不兼容（版本比代码新，或表结构被外力改坏）。"""


class MigrationError(StoreError):
    """迁移失败。**已回滚**，库仍是迁移前的状态。"""


class InvalidTransition(StoreError):
    """非法的仓位状态迁移（如把 `CLOSED` 改回 `OPEN`、同标的同时开两仓）。"""


# ------------------------------------------------------------------ 小工具


def now() -> float:
    """当前 epoch 秒。集中一处，方便测试用显式时间戳绕开时钟。"""
    return time.time()


def utc_day(ts: Optional[float] = None) -> str:
    """epoch 秒 → `'YYYY-MM-DD'`（**UTC**，与交易所结算/日志口径一致）。"""
    stamp = now() if ts is None else float(ts)
    return time.strftime("%Y-%m-%d", time.gmtime(stamp))


def day_bounds(day: str) -> Tuple[float, float]:
    """`'YYYY-MM-DD'` → `(当天 00:00:00 UTC, 次日 00:00:00 UTC)`，左闭右开。"""
    text = str(day).strip()
    try:
        start = _dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        raise StoreError(f"day must be 'YYYY-MM-DD', got {day!r}") from None
    return start.timestamp(), (start + _dt.timedelta(days=1)).timestamp()


def _finite(value: Any, default: Optional[float] = None) -> Optional[float]:
    """安全转 float：None / 垃圾 / NaN / ±inf → `default`。

    NaN 是存储层的真陷阱：`float('nan')` 写进 SQLite 会变成 NULL（绕过 `NOT NULL` 之外的
    所有比较），读回来又是一个「看着像数字」的值。统一挡在入口。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _int(value: Any, name: str) -> int:
    """严格取整数（拒绝 bool 之外的字符串/浮点脏值，避免「数字被存成字符串」）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 10)
    except ValueError:
        raise StoreError(f"{name} must be a base-10 integer, got {value!r}") from None


def _qty_float(qty_int: int, size_decimals: int) -> float:
    """整数 tick → base 数量（`qty_int / 10^dec`）。"""
    return float(qty_int) / float(10 ** int(size_decimals))


def _price_float(price_int: Optional[int], price_decimals: int) -> Optional[float]:
    if price_int is None:
        return None
    return float(price_int) / float(10 ** int(price_decimals))


def trade_key(*, fill_id: Any = None, order_id: Any = None, symbol: Any = None,
              kind: Any = None, side: Any = None, price_int: Any = None,
              qty_int: Any = None, price: Any = None, qty: Any = None,
              ts_ms: Any = None) -> str:
    """成交的幂等键（**唯一事实来源**，测试直接断言它的取值）。

    分层依据是「取到多细的证据」，越细越不会误合并：

    1. 有交易所成交 id（`fill_id`，Lighter 的 `trade_id` / HL 的 `tid`）→ `fill:<id>`。
       一笔成交一个 id，最可靠。
    2. 只有订单号 → `order:<id>:<side>:<price>:<qty>`（优先整数 tick，缺则退回浮点）。
       **故意把价和量放进键里**：同一个订单号可能产生多笔部分成交（不同价/不同量），
       那些是**不同的成交**，必须各占一行；而「同一订单、同价、同量」重复同步 =
       同一条记录，必须去重。
    3. 都没有（本地记账/手工补录）→ `local:<symbol>:<kind>:<ts_ms>:<side>:<price>:<qty>`。
    """
    price_part = _key_num(price_int, price)
    qty_part = _key_num(qty_int, qty)
    if fill_id not in (None, ""):
        return f"fill:{fill_id}"
    if order_id not in (None, ""):
        return f"order:{order_id}:{int(side or 0)}:{price_part}:{qty_part}"
    return (f"local:{str(symbol or '').upper()}:{str(kind or '')}:{int(ts_ms or 0)}:"
            f"{int(side or 0)}:{price_part}:{qty_part}")


def _key_num(int_value: Any, float_value: Any) -> str:
    """幂等键里的数值部分：整数 tick 优先（精确），否则退回浮点（12 位有效数字）。"""
    if int_value is not None:
        try:
            return str(int(int_value))
        except (TypeError, ValueError):
            pass
    value = _finite(float_value)
    return "?" if value is None else f"{value:.12g}"


class WriteResult(dict):
    """一次写操作的结果（`dict` 子类，既能 `.id` 访问也能直接 JSON 化）。

    字段：`position_id` / `trade_id` / `inserted_position` / `inserted_trade` /
    `state` / `idempotent`（= 什么都没新增，说明是重复写入）。
    """

    @property
    def position_id(self) -> Optional[int]:
        return self.get("position_id")

    @property
    def trade_id(self) -> Optional[int]:
        return self.get("trade_id")

    @property
    def inserted_position(self) -> bool:
        return bool(self.get("inserted_position"))

    @property
    def inserted_trade(self) -> bool:
        return bool(self.get("inserted_trade"))

    @property
    def state(self) -> Optional[str]:
        """写完之后该仓位的状态（`record_trade` 之类不碰仓位时为 `None`）。"""
        value = self.get("state")
        return None if value is None else str(value)

    @property
    def idempotent(self) -> bool:
        """True = 这次调用没有新增任何行（重复写入 / 已处理过）。"""
        return not (self.inserted_position or self.inserted_trade)


# ------------------------------------------------------------------ schema

#: 版本 1：T5 初始 schema。
_MIGRATION_1: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS positions (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol            TEXT    NOT NULL,
        side              INTEGER NOT NULL CHECK (side IN (-1, 1)),
        state             TEXT    NOT NULL CHECK (state IN ('IDLE','OPEN','CLOSING','CLOSED')),
        bar_ms            INTEGER NOT NULL,          -- 信号 bar（毫秒），与 strategy 口径一致
        market_id         INTEGER,
        qty_int           INTEGER NOT NULL DEFAULT 0,
        qty               REAL    NOT NULL DEFAULT 0.0,
        size_decimals     INTEGER NOT NULL DEFAULT 0,
        entry_price_int   INTEGER,
        entry_price       REAL,
        price_decimals    INTEGER NOT NULL DEFAULT 0,
        stop_price_int    INTEGER,
        stop_price        REAL,
        notional          REAL,
        risk_amount       REAL,
        equity_at_entry   REAL,
        entry_order_id    TEXT,
        stop_order_id     TEXT,
        opened_at         REAL,
        closed_at         REAL,
        exit_reason       TEXT,
        net_pnl           REAL,
        created_at        REAL    NOT NULL,
        updated_at        REAL    NOT NULL,
        UNIQUE (symbol, bar_ms)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        ts                  REAL    NOT NULL,        -- 成交时刻（epoch 秒）
        symbol              TEXT    NOT NULL,
        side                INTEGER NOT NULL CHECK (side IN (-1, 1)),
        price               REAL    NOT NULL CHECK (price > 0),
        price_int           INTEGER,
        qty                 REAL    NOT NULL,
        qty_int             INTEGER CHECK (qty_int IS NULL OR qty_int <> 0),
        fee                 REAL    NOT NULL DEFAULT 0.0,
        fee_token           TEXT,
        kind                TEXT    NOT NULL CHECK (kind IN ('entry','exit','stop')),
        order_id            TEXT,
        fill_id             TEXT,
        position_id         INTEGER,
        realized_pnl        REAL,
        bar_ms              INTEGER,
        raw                 TEXT,
        idempotency_key     TEXT    NOT NULL UNIQUE,
        created_at          REAL    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equity_curve (
        ts          REAL PRIMARY KEY,                -- epoch 秒；同秒重复写 = 覆盖
        equity      REAL NOT NULL,
        twr_index   REAL,                            -- 资金加权收益指数（基准 1.0）
        cum_flow    REAL NOT NULL DEFAULT 0.0,       -- 累计净入金（TWR 剔出/入金用）
        source      TEXT,                            -- exchange | store | injected
        created_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_counters (
        day                TEXT PRIMARY KEY,         -- 'YYYY-MM-DD'（UTC）
        trades_today       INTEGER NOT NULL DEFAULT 0,
        day_start_equity   REAL,
        updated_at         REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dd_anchors (
        ts          REAL PRIMARY KEY,                -- 锚点时刻（epoch 秒）
        equity      REAL NOT NULL,
        cum_flow    REAL NOT NULL DEFAULT 0.0,
        twr_index   REAL NOT NULL,
        twr_peak    REAL NOT NULL,
        created_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dd_state (
        name             TEXT PRIMARY KEY,           -- 固定 'live'（单行）
        anchor_ts        REAL NOT NULL,
        anchor_equity    REAL NOT NULL,
        anchor_cum_flow  REAL NOT NULL DEFAULT 0.0,
        twr_index        REAL NOT NULL,
        twr_peak         REAL NOT NULL,
        reset_count      INTEGER NOT NULL DEFAULT 0,
        last_reset_at    REAL,
        reset_reason     TEXT,
        updated_at       REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL NOT NULL,
        kind        TEXT NOT NULL,
        symbol      TEXT,
        message     TEXT NOT NULL,
        data        TEXT,
        created_at  REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_positions_symbol_state ON positions(symbol, state)",
    "CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(state)",
    "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)",
    "CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades(symbol, ts)",
    "CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)",
)

#: 版本 2：补齐「平仓侧」与「客户端幂等键」（T5 内部第二次迭代；老库走这条 ALTER 路径）。
_MIGRATION_2: Tuple[str, ...] = (
    "ALTER TABLE positions ADD COLUMN exit_price REAL",
    "ALTER TABLE positions ADD COLUMN exit_price_int INTEGER",
    "ALTER TABLE positions ADD COLUMN exit_order_id TEXT",
    "ALTER TABLE positions ADD COLUMN client_order_index INTEGER",
    "ALTER TABLE trades ADD COLUMN client_order_index INTEGER",
    "ALTER TABLE daily_counters ADD COLUMN realized_pnl REAL NOT NULL DEFAULT 0.0",
    "CREATE INDEX IF NOT EXISTS idx_positions_client_key ON positions(client_order_index)",
    "CREATE INDEX IF NOT EXISTS idx_daily_counters_day ON daily_counters(day)",
)

#: `{目标版本: 该版本的语句}`。新增版本只往后追加，**永不改已发布的语句** —— 那是老库
#: 升级路径唯一的依据（改一个字符，已升级过的库和新建的库就会分叉）。
MIGRATIONS: Mapping[int, Tuple[str, ...]] = {1: _MIGRATION_1, 2: _MIGRATION_2}


class AuditFinding(dict):
    """`Store.audit()` 的一条不变量违规（`code` 稳定、`detail` 给面板/日志）。"""

    @property
    def code(self) -> str:
        return str(self.get("code", ""))


# ------------------------------------------------------------------ 主类


class StateStore:
    """SQLite 状态库。单连接 + `RLock` 串行化（进程内），WAL 让别的进程能同时读。

    典型用法::

        with StateStore("./data/state.db") as store:
            store.record_entry(...)
            store.panel_snapshot()

    测试用 `StateStore(":memory:")` 或临时目录里的文件（都不碰网络）。
    """

    def __init__(self, path: str = ":memory:", *,
                 busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
                 migrate: bool = True,
                 synchronous: str = "NORMAL") -> None:
        if path in (None, ""):
            raise StoreError("database path is required (use ':memory:' for tests)")
        self.path = str(path)
        self._lock = threading.RLock()
        self._txn_depth = 0
        self._closed = False
        if not self._is_memory:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=max(0.001, busy_timeout_ms / 1000.0),
                                    check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # isolation_level=None → 自己管事务（显式 BEGIN/COMMIT），这样「复合写」的边界
        # 由本模块决定，而不是由 sqlite3 模块按 DML 猜。
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._apply_pragmas(synchronous=synchronous)
        if migrate:
            try:
                self.migrate()
            except BaseException:
                # 迁移失败/库比代码新：别把连接漏在这里（Windows 上会锁文件，
                # 而且「半开的库」比「没打开的库」更难排查）。
                self.conn.close()
                self._closed = True
                raise

    # ------------------------------------------------------------ 构造 / 关闭

    @property
    def _is_memory(self) -> bool:
        return self.path == ":memory:" or self.path.startswith("file::memory:")

    @classmethod
    def from_config(cls, config: Any, **kwargs: Any) -> "StateStore":
        """按 `app.config.Config` 建库（**只鸭子类型读属性**，不 import `app.config`）。

        优先级：显式 `db_url`（必须是 `sqlite:///`）> `sqlite_path` > `db_path`。
        `db_url` 指向非 sqlite 时 **fail-closed 抛错**，绝不悄悄退回 `db_path` ——
        那会让「以为数据在 postgres」的配置静默写进一个本地文件。
        """
        db_url = (getattr(config, "db_url", "") or "").strip()
        if db_url:
            if not db_url.startswith("sqlite:///"):
                raise StoreError(
                    f"unsupported db_url scheme (only sqlite:/// is supported): "
                    f"{db_url.split('://', 1)[0]}://"
                )
            path = db_url[len("sqlite:///"):]
        else:
            path = (getattr(config, "sqlite_path", None)
                    or getattr(config, "db_path", "") or "")
        if not path:
            raise StoreError("config carries neither db_url nor db_path")
        return cls(os.path.expanduser(path), **kwargs)

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self.conn.in_transaction:
                    self.conn.rollback()
            finally:
                self.conn.close()
                self._closed = True

    def __repr__(self) -> str:
        return f"StateStore(path={self.path!r}, version={self.schema_version})"

    # ------------------------------------------------------------ pragmas / 迁移

    def _apply_pragmas(self, *, synchronous: str = "NORMAL") -> None:
        """WAL + busy_timeout + 外键 + synchronous。

        * `WAL`：读不阻塞写。`:memory:` 库的 journal_mode 只能是 `memory`，那就留着。
        * `busy_timeout`：另一个进程（面板/运维脚本）在写时，等一会儿再报错，而不是立刻
          `database is locked`。
        * `synchronous=NORMAL`：WAL 下的**官方推荐**组合 —— 进程崩溃不丢已提交事务、
          不损坏库；只有 OS 掉电可能丢最后一个事务。用 `FULL` 会让每笔成交多一次 fsync，
          在小主机上没有必要。
        """
        mode = "memory"
        if not self._is_memory:
            row = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()
            mode = str(row[0] if row is not None else "").lower()
            if mode != "wal":
                # 只读目录 / 网络文件系统会退化成别的模式：不致命，但要留痕。
                logger.warning("[store] journal_mode=%s (WAL unavailable) for %s", mode, self.path)
        self.conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        self.conn.execute("PRAGMA foreign_keys=ON")
        if mode == "wal":
            self.conn.execute(f"PRAGMA synchronous={str(synchronous).upper()}")
        self._journal_mode = mode

    @property
    def journal_mode(self) -> str:
        return self._journal_mode

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        """把库迁到 `SCHEMA_VERSION`，返回原版本号。幂等：已是最新则原地返回。

        每个版本的 DDL 与 `PRAGMA user_version` **在同一个事务里**提交 —— 迁移中断不会
        留下「表加了列但版本号没动」的库（下次会重复 ALTER 而失败），也不会留下
        「版本号动了但列没加」的库（下次会跳过迁移）。
        """
        with self._lock:
            current = self.schema_version
            if current > SCHEMA_VERSION:
                raise SchemaError(
                    f"database schema version {current} is newer than this code "
                    f"({SCHEMA_VERSION}) — refusing to run against a newer database"
                )
            while current < SCHEMA_VERSION:
                target = current + 1
                statements = MIGRATIONS.get(target)
                if not statements:
                    raise MigrationError(f"no migration registered for version {target}")
                try:
                    self.conn.execute("BEGIN IMMEDIATE")
                    for statement in statements:
                        self.conn.execute(statement)
                    self.conn.execute(f"PRAGMA user_version={int(target)}")
                    self.conn.execute("COMMIT")
                except sqlite3.Error as exc:
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.Error:                    # pragma: no cover - 已回滚
                        pass
                    raise MigrationError(
                        f"migration to version {target} failed and was rolled back: {exc}"
                    ) from exc
                logger.info("[store] migrated %s -> v%d", self.path, target)
                current = target
            return current

    # ------------------------------------------------------------ 事务

    @contextmanager
    def write_transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """写事务（可嵌套：内层不加新事务，由最外层提交/回滚）。

        `BEGIN IMMEDIATE`：进事务就抢写锁。默认的 `BEGIN`（DEFERRED）会在「先读后写」的
        中间升级锁，两个写者同时升级就必有一方 `SQLITE_BUSY` —— 即使 `busy_timeout` 也
        救不了（读锁持有者优先）。存储层的复合写全是「先查再写」，所以必须 IMMEDIATE。

        **异常一定回滚**（`BaseException`，含 `KeyboardInterrupt`）：写一半被 Ctrl-C
        也要么全有要么全无。
        """
        with self._lock:
            outermost = self._txn_depth == 0
            self._txn_depth += 1
            try:
                if outermost:
                    self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield self.conn
            except BaseException:
                if outermost:
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.Error:                    # pragma: no cover - 已回滚
                        pass
                raise
            else:
                if outermost:
                    try:
                        self.conn.execute("COMMIT")
                    except sqlite3.Error:
                        try:
                            self.conn.execute("ROLLBACK")
                        except sqlite3.Error:                # pragma: no cover
                            pass
                        raise
            finally:
                self._txn_depth -= 1

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self.conn

    # ------------------------------------------------------------ 小查询原语

    def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        with self._read() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row is not None else None

    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def _scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self._fetchone(sql, params)
        if not row:
            return default
        value = next(iter(row.values()))
        return default if value is None else value

    # ------------------------------------------------------------ 仓位：写

    def record_entry(self, symbol: str, side: int, *, qty_int: int, size_decimals: int,
                     entry_price_int: int, price_decimals: int,
                     stop_price_int: Optional[int], bar_ms: int,
                     market_id: Optional[int] = None,
                     entry_order_id: Optional[str] = None,
                     stop_order_id: Optional[str] = None,
                     client_order_index: Optional[int] = None,
                     equity_at_entry: Optional[float] = None,
                     fee: float = 0.0, fee_token: Optional[str] = None,
                     fill_id: Optional[str] = None,
                     ts: Optional[float] = None) -> WriteResult:
        """开仓：仓位行（`OPEN`）+ 开仓成交 + 事件 + 当日计数 **一个事务**。

        幂等：
        * `UNIQUE(symbol, bar_ms)` —— 同一根 bar 的同一标的只开一次；重复调用返回原
          `position_id` 且不改任何数据（`idempotent=True`）。
        * 成交走 `idempotency_key`（`fill_id` > `order_id+价+量` > 本地键），重复同步不加行。

        前置检查（fail-loud，不静默）：
        * `side` 必须是 ±1；`qty_int > 0`；`entry_price_int > 0`；`stop_price_int` 必须在
          入场价的正确一侧（多单止损 < 入场，空单止损 > 入场）。
        * 该标的不能已有 `OPEN`/`CLOSING` 仓位（PLAN T6「每标的至多一仓」）→
          `InvalidTransition`。这条检查与插入在**同一事务**里，两个并发写者不会同时通过。
        """
        symbol_u = self._symbol(symbol)
        side_i = self._side(side)
        qty = _int(qty_int, "qty_int")
        if qty <= 0:
            raise StoreError(f"qty_int must be > 0, got {qty}")
        size_dec = _int(size_decimals, "size_decimals")
        price_dec = _int(price_decimals, "price_decimals")
        entry_int = _int(entry_price_int, "entry_price_int")
        if entry_int <= 0:
            raise StoreError(f"entry_price_int must be > 0, got {entry_int}")
        stop_int = None if stop_price_int is None else _int(stop_price_int, "stop_price_int")
        if stop_int is not None:
            if stop_int <= 0:
                raise StoreError(f"stop_price_int must be > 0, got {stop_int}")
            if side_i > 0 and stop_int >= entry_int:
                raise StoreError(f"long stop {stop_int} must be below entry {entry_int}")
            if side_i < 0 and stop_int <= entry_int:
                raise StoreError(f"short stop {stop_int} must be above entry {entry_int}")
        bar = _int(bar_ms, "bar_ms")
        stamp = now() if ts is None else _finite(ts, default=now())

        qty_f = _qty_float(qty, size_dec)
        entry_f = _price_float(entry_int, price_dec) or 0.0
        stop_f = _price_float(stop_int, price_dec)
        notional = qty_f * entry_f
        risk_amount = abs(entry_f - stop_f) * qty_f if stop_f is not None else None
        day = utc_day(stamp)

        with self.write_transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM positions WHERE symbol=? AND bar_ms=?",
                (symbol_u, bar)).fetchone()
            if existing is not None:
                # 同一根 bar 重复记账：仓位不动，成交仍按幂等键尝试一次（补 fill_id
                # 之类的后到信息由 T8 的 update 接口处理，这里不覆盖）。
                trade_id, inserted_trade = self._insert_trade_row(conn, dict(
                    ts=stamp, symbol=symbol_u, side=side_i, price=entry_f,
                    price_int=entry_int, qty=qty_f, qty_int=qty, fee=_finite(fee, default=0.0),
                    fee_token=fee_token, kind="entry", order_id=entry_order_id,
                    fill_id=fill_id, position_id=int(existing["id"]),
                    realized_pnl=None, bar_ms=bar, raw=None,
                    client_order_index=client_order_index,
                ))
                return WriteResult(position_id=int(existing["id"]), trade_id=trade_id,
                                   inserted_position=False, inserted_trade=inserted_trade,
                                   state=str(existing["state"]))

            live = conn.execute(
                f"SELECT id, state FROM positions WHERE symbol=? AND state IN "
                f"({','.join('?' * len(OPEN_STATES))})",
                (symbol_u, *OPEN_STATES)).fetchone()
            if live is not None:
                raise InvalidTransition(
                    f"{symbol_u} already has an {live['state']} position (id={live['id']}) — "
                    f"one position per symbol (PLAN T6)"
                )

            cursor = conn.execute(
                """INSERT INTO positions(symbol, side, state, bar_ms, market_id, qty_int, qty,
                        size_decimals, entry_price_int, entry_price, price_decimals,
                        stop_price_int, stop_price, notional, risk_amount, equity_at_entry,
                        entry_order_id, stop_order_id, client_order_index, opened_at,
                        created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol_u, side_i, "OPEN", bar, market_id, qty, qty_f, size_dec,
                 entry_int, entry_f, price_dec, stop_int, stop_f, notional, risk_amount,
                 _finite(equity_at_entry), entry_order_id, stop_order_id,
                 client_order_index, stamp, stamp, stamp),
            )
            position_id = int(cursor.lastrowid or 0)

            # ⚠️ 这一步失败必须把上面那条仓位一起回滚 —— 否则就是
            # 「有 OPEN 仓位、没有开仓成交」的半状态（对账时无法证明这笔仓从哪来）。
            trade_id, inserted_trade = self._insert_trade_row(conn, dict(
                ts=stamp, symbol=symbol_u, side=side_i, price=entry_f, price_int=entry_int,
                qty=qty_f, qty_int=qty, fee=_finite(fee, default=0.0), fee_token=fee_token,
                kind="entry", order_id=entry_order_id, fill_id=fill_id,
                position_id=position_id, realized_pnl=None, bar_ms=bar, raw=None,
                client_order_index=client_order_index,
            ))

            self._bump_counter(conn, day, entries=1, realized_pnl=0.0)
            self._ensure_day_start(conn, day, _finite(equity_at_entry), stamp)
            self._log_event(conn, kind="open", symbol=symbol_u,
                            message=(f"{symbol_u} {'long' if side_i > 0 else 'short'} "
                                     f"{qty_f:g} @ {entry_f:g}"
                                     + (f" stop {stop_f:g}" if stop_f is not None else "")),
                            data={"position_id": position_id, "qty_int": qty,
                                  "entry_price_int": entry_int,
                                  "stop_price_int": stop_int,
                                  "notional": notional, "bar_ms": bar},
                            ts=stamp)
            return WriteResult(position_id=position_id, trade_id=trade_id,
                               inserted_position=True, inserted_trade=inserted_trade,
                               state="OPEN")

    def begin_close(self, position_id: int, *, ts: Optional[float] = None,
                    reason: Optional[str] = None) -> WriteResult:
        """`OPEN → CLOSING`（已发出离场/止损单，等交易所确认）。

        `CLOSING` 仍然是「有仓位」：交易所侧还没确认平掉，风控必须继续把它算进
        `open_positions`（否则同一标的会在宽限期内开出第二仓）。
        """
        stamp = now() if ts is None else _finite(ts, default=now())
        with self.write_transaction() as conn:
            row = conn.execute("SELECT * FROM positions WHERE id=?", (int(position_id),)).fetchone()
            if row is None:
                raise StoreError(f"position {position_id} not found")
            state = str(row["state"])
            if state == "CLOSING":
                return WriteResult(position_id=int(position_id), trade_id=None,
                                   inserted_position=False, inserted_trade=False,
                                   state="CLOSING")
            if state == "CLOSED":
                raise InvalidTransition(f"position {position_id} is already CLOSED")
            self._check_transition(state, "CLOSING")
            conn.execute("UPDATE positions SET state='CLOSING', updated_at=? WHERE id=?",
                         (stamp, int(position_id)))
            self._log_event(conn, kind="closing", symbol=str(row["symbol"]),
                            message=f"{row['symbol']} closing" + (f" ({reason})" if reason else ""),
                            data={"position_id": int(position_id), "reason": reason}, ts=stamp)
            return WriteResult(position_id=int(position_id), trade_id=None,
                               inserted_position=False, inserted_trade=False,
                               state="CLOSING")

    def record_exit(self, position_id: int, *, exit_price_int: int, price_decimals: Optional[int] = None,
                    kind: str = "exit", exit_order_id: Optional[str] = None,
                    fill_id: Optional[str] = None, fee: float = 0.0,
                    fee_token: Optional[str] = None, realized_pnl: Optional[float] = None,
                    reason: Optional[str] = None, client_order_index: Optional[int] = None,
                    ts: Optional[float] = None) -> WriteResult:
        """平仓：`OPEN/CLOSING → CLOSED` + 平仓成交 + 事件 + 当日已实现盈亏，一个事务。

        幂等：仓位已经是 `CLOSED` → 直接返回 `idempotent=True`（不重复记成交、不改
        `closed_at`、不重复累加当日盈亏）。成交本身仍按幂等键去重。

        `realized_pnl` 缺省时按**本地价差口径**算：`(exit − entry) × qty × side − Σfee`。
        交易所给的净额（fills 的 `closed_pnl`）更权威 —— T8 应当显式传进来；这里算的只是
        「没拿到交易所口径时不要留空」的兜底，并落 `events` 说明来源。
        """
        trade_kind = str(kind or "exit").strip().lower()
        if trade_kind not in ("exit", "stop"):
            raise StoreError(f"exit kind must be 'exit' or 'stop', got {kind!r}")
        exit_int = _int(exit_price_int, "exit_price_int")
        if exit_int <= 0:
            raise StoreError(f"exit_price_int must be > 0, got {exit_int}")
        stamp = now() if ts is None else _finite(ts, default=now())
        day = utc_day(stamp)

        with self.write_transaction() as conn:
            row = conn.execute("SELECT * FROM positions WHERE id=?", (int(position_id),)).fetchone()
            if row is None:
                raise StoreError(f"position {position_id} not found")
            record = dict(row)
            state = str(record["state"])
            if state == "CLOSED":
                return WriteResult(position_id=int(position_id), trade_id=None,
                                   inserted_position=False, inserted_trade=False,
                                   state="CLOSED")
            if state == "IDLE":
                raise InvalidTransition(f"position {position_id} is IDLE — nothing to close")
            self._check_transition(state, "CLOSED")

            price_dec = (_int(record["price_decimals"], "price_decimals")
                         if price_decimals is None else _int(price_decimals, "price_decimals"))
            exit_f = _price_float(exit_int, price_dec) or 0.0
            qty_f = float(record["qty"] or 0.0)
            entry_f = float(record["entry_price"] or 0.0)
            side_i = int(record["side"])
            gross = (exit_f - entry_f) * qty_f * side_i
            fee_total = _finite(fee, default=0.0) or 0.0
            pnl = _finite(realized_pnl)
            pnl_source = "exchange"
            if pnl is None:
                pnl = gross - fee_total
                pnl_source = "local_price_diff"

            conn.execute(
                """UPDATE positions
                      SET state='CLOSED', closed_at=?, exit_price=?, exit_price_int=?,
                          exit_order_id=?, exit_reason=?, net_pnl=?, updated_at=?
                    WHERE id=?""",
                (stamp, exit_f, exit_int, exit_order_id, reason or trade_kind, pnl, stamp,
                 int(position_id)))
            trade_id, inserted_trade = self._insert_trade_row(conn, dict(
                ts=stamp, symbol=str(record["symbol"]), side=-side_i, price=exit_f,
                price_int=exit_int, qty=qty_f, qty_int=int(record["qty_int"] or 0),
                fee=fee_total, fee_token=fee_token, kind=trade_kind,
                order_id=exit_order_id, fill_id=fill_id, position_id=int(position_id),
                realized_pnl=pnl, bar_ms=record.get("bar_ms"), raw=None,
                client_order_index=client_order_index,
            ))
            self._bump_counter(conn, day, entries=0, realized_pnl=pnl)
            self._log_event(conn, kind=trade_kind, symbol=str(record["symbol"]),
                            message=(f"{record['symbol']} closed {qty_f:g} @ {exit_f:g} "
                                     f"pnl {pnl:+.4f} ({pnl_source})"),
                            data={"position_id": int(position_id), "exit_price_int": exit_int,
                                  "realized_pnl": pnl, "pnl_source": pnl_source,
                                  "reason": reason, "fee": fee_total}, ts=stamp)
            return WriteResult(position_id=int(position_id), trade_id=trade_id,
                               inserted_position=False, inserted_trade=inserted_trade,
                               state="CLOSED")

    def record_stop(self, position_id: int, **kwargs: Any) -> WriteResult:
        """止损成交（= `record_exit(kind="stop")`，语义别名，避免调用方拼字符串）。"""
        kwargs.pop("kind", None)
        return self.record_exit(position_id, kind="stop", **kwargs)

    def record_trade(self, *, symbol: str, side: int, price: float, qty: float, kind: str,
                     price_int: Optional[int] = None, qty_int: Optional[int] = None,
                     fee: float = 0.0, fee_token: Optional[str] = None,
                     order_id: Optional[str] = None, fill_id: Optional[str] = None,
                     position_id: Optional[int] = None, realized_pnl: Optional[float] = None,
                     bar_ms: Optional[int] = None, client_order_index: Optional[int] = None,
                     raw: Optional[Mapping[str, Any]] = None,
                     ts: Optional[float] = None) -> WriteResult:
        """记一笔成交（T8 的 `_sync_fills` 用；不改仓位状态）。

        幂等键见 `trade_key()`：**同一笔成交重复拉取不产生第二行**。
        """
        symbol_u = self._symbol(symbol)
        side_i = self._side(side)
        trade_kind = str(kind or "").strip().lower()
        if trade_kind not in TRADE_KINDS:
            raise StoreError(f"trade kind must be one of {TRADE_KINDS}, got {kind!r}")
        price_f = _finite(price)
        qty_f = _finite(qty)
        if price_f is None or price_f <= 0:
            raise StoreError(f"price must be a positive finite number, got {price!r}")
        if qty_f is None or qty_f == 0:
            raise StoreError(f"qty must be a non-zero finite number, got {qty!r}")
        stamp = now() if ts is None else _finite(ts, default=now())
        row = dict(
            ts=stamp, symbol=symbol_u, side=side_i, price=price_f,
            price_int=None if price_int is None else _int(price_int, "price_int"),
            qty=qty_f, qty_int=None if qty_int is None else _int(qty_int, "qty_int"),
            fee=_finite(fee, default=0.0), fee_token=fee_token, kind=trade_kind,
            order_id=None if order_id is None else str(order_id),
            fill_id=None if fill_id is None else str(fill_id),
            position_id=None if position_id is None else int(position_id),
            realized_pnl=_finite(realized_pnl), bar_ms=None if bar_ms is None else _int(bar_ms, "bar_ms"),
            raw=None if raw is None else json.dumps(raw, sort_keys=True, default=str),
            client_order_index=client_order_index,
        )
        with self.write_transaction() as conn:
            trade_id, inserted = self._insert_trade_row(conn, row)
        return WriteResult(position_id=row["position_id"], trade_id=trade_id,
                           inserted_position=False, inserted_trade=inserted,
                           state=None)

    # ------------------------------------------------------------ 仓位：读

    def position(self, position_id: int) -> Optional[Dict[str, Any]]:
        return self._fetchone("SELECT * FROM positions WHERE id=?", (int(position_id),))

    def position_by_bar(self, symbol: str, bar_ms: int) -> Optional[Dict[str, Any]]:
        return self._fetchone("SELECT * FROM positions WHERE symbol=? AND bar_ms=?",
                              (self._symbol(symbol), _int(bar_ms, "bar_ms")))

    def open_positions(self) -> List[Dict[str, Any]]:
        """当前**有仓位**的行（`OPEN` + `CLOSING`），按开仓时间升序。

        `CLOSING` 也算 —— 离场单已发但交易所没确认，仓位还在（见 `begin_close`）。
        """
        placeholders = ",".join("?" * len(OPEN_STATES))
        return self._fetchall(
            f"SELECT * FROM positions WHERE state IN ({placeholders}) "
            f"ORDER BY COALESCE(opened_at, created_at), id", OPEN_STATES)

    def open_symbols(self) -> Tuple[str, ...]:
        """当前有仓位的标的（**元组**，与 `app.risk.RiskState.open_positions` 同型）。"""
        return tuple(sorted({str(p["symbol"]).upper() for p in self.open_positions()}))

    def open_notional_by_symbol(self) -> Dict[str, float]:
        """按标的汇总的当前名义（用**记录在库的入场价**；标记价由调用方覆盖）。"""
        out: Dict[str, float] = {}
        for row in self.open_positions():
            symbol = str(row["symbol"]).upper()
            qty = _finite(row.get("qty"), 0.0) or 0.0
            price = _finite(row.get("entry_price"), 0.0) or 0.0
            out[symbol] = out.get(symbol, 0.0) + qty * price
        return out

    def positions(self, *, state: Optional[str] = None, symbol: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
        """历史/当前仓位（面板用），`id DESC`。"""
        clauses, params = [], []
        if state is not None:
            wanted = str(state).strip().upper()
            if wanted not in POSITION_STATES:
                raise StoreError(f"unknown position state {state!r}")
            clauses.append("state=?")
            params.append(wanted)
        if symbol is not None:
            clauses.append("symbol=?")
            params.append(self._symbol(symbol))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        return self._fetchall(f"SELECT * FROM positions {where} ORDER BY id DESC LIMIT ?",
                              params)

    def position_count(self, *, state: Optional[str] = None) -> int:
        if state is None:
            return int(self._scalar("SELECT COUNT(*) AS n FROM positions", default=0) or 0)
        return int(self._scalar("SELECT COUNT(*) AS n FROM positions WHERE state=?",
                                (str(state).upper(),), default=0) or 0)

    def set_position_state(self, position_id: int, state: str, *,
                           ts: Optional[float] = None) -> Dict[str, Any]:
        """通用状态迁移入口（带状态机校验）。面板/运维脚本用；执行器走上面的语义方法。"""
        target = str(state).strip().upper()
        if target not in POSITION_STATES:
            raise StoreError(f"unknown position state {state!r}")
        stamp = now() if ts is None else _finite(ts, default=now())
        with self.write_transaction() as conn:
            row = conn.execute("SELECT * FROM positions WHERE id=?", (int(position_id),)).fetchone()
            if row is None:
                raise StoreError(f"position {position_id} not found")
            current = str(row["state"])
            if current == target:
                return dict(row)
            self._check_transition(current, target)
            conn.execute("UPDATE positions SET state=?, updated_at=? WHERE id=?",
                         (target, stamp, int(position_id)))
        out = self.position(position_id)
        assert out is not None
        return out

    # ------------------------------------------------------------ 成交：读

    def trades(self, *, limit: int = 50, symbol: Optional[str] = None,
               since: Optional[float] = None, kind: Optional[str] = None,
               position_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """最近成交（面板「最近 N 笔」）。可按标的/时间/类型/仓位过滤。"""
        clauses, params = [], []
        if symbol is not None:
            clauses.append("symbol=?")
            params.append(self._symbol(symbol))
        if since is not None:
            clauses.append("ts>=?")
            params.append(_finite(since, default=0.0))
        if kind is not None:
            wanted = str(kind).strip().lower()
            if wanted not in TRADE_KINDS:
                raise StoreError(f"unknown trade kind {kind!r}")
            clauses.append("kind=?")
            params.append(wanted)
        if position_id is not None:
            clauses.append("position_id=?")
            params.append(int(position_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        return self._fetchall(
            f"SELECT * FROM trades {where} ORDER BY ts DESC, id DESC LIMIT ?", params)

    def trade(self, trade_id: int) -> Optional[Dict[str, Any]]:
        return self._fetchone("SELECT * FROM trades WHERE id=?", (int(trade_id),))

    def trade_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        return self._fetchone("SELECT * FROM trades WHERE idempotency_key=?", (str(key),))

    def trade_count(self) -> int:
        return int(self._scalar("SELECT COUNT(*) AS n FROM trades", default=0) or 0)

    def realized_pnl(self, *, since: Optional[float] = None) -> float:
        """已实现盈亏合计（**只算平仓/止损腿**，开仓腿的 `realized_pnl` 恒为 NULL）。"""
        if since is None:
            value = self._scalar("SELECT COALESCE(SUM(realized_pnl), 0.0) AS s FROM trades "
                                 "WHERE kind IN ('exit','stop')")
        else:
            value = self._scalar("SELECT COALESCE(SUM(realized_pnl), 0.0) AS s FROM trades "
                                 "WHERE kind IN ('exit','stop') AND ts>=?",
                                 (_finite(since, default=0.0),))
        return float(value or 0.0)

    # ------------------------------------------------------------ 权益曲线

    def record_equity(self, equity: float, *, ts: Optional[float] = None,
                      twr_index: Optional[float] = None, cum_flow: Optional[float] = None,
                      source: str = "exchange") -> Dict[str, Any]:
        """写一个权益点（同一 `ts` 覆盖：面板/对账重复采样不制造重复点）。"""
        equity_f = _finite(equity)
        if equity_f is None:
            raise StoreError(f"equity must be a finite number, got {equity!r}")
        stamp = now() if ts is None else _finite(ts, default=now())
        twr = _finite(twr_index)
        if twr is not None and twr < 0:
            raise StoreError(f"twr_index must be >= 0, got {twr}")
        flow = _finite(cum_flow, 0.0) or 0.0
        created = now()
        with self.write_transaction() as conn:
            conn.execute(
                """INSERT INTO equity_curve(ts, equity, twr_index, cum_flow, source, created_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(ts) DO UPDATE SET
                       equity=excluded.equity,
                       twr_index=COALESCE(excluded.twr_index, equity_curve.twr_index),
                       cum_flow=excluded.cum_flow,
                       source=excluded.source,
                       created_at=excluded.created_at""",
                (stamp, equity_f, twr, flow, str(source), created),
            )
        out = self.equity_point(stamp)
        assert out is not None
        return out

    def equity_point(self, ts: float) -> Optional[Dict[str, Any]]:
        return self._fetchone("SELECT * FROM equity_curve WHERE ts=?", (_finite(ts, default=0.0),))

    def last_equity(self) -> Optional[Dict[str, Any]]:
        return self._fetchone("SELECT * FROM equity_curve ORDER BY ts DESC LIMIT 1")

    def first_equity(self, *, since: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """`ts >= since` 的**最早**一个权益点（当日日初权益的兜底来源）。"""
        if since is None:
            return self._fetchone("SELECT * FROM equity_curve ORDER BY ts ASC LIMIT 1")
        return self._fetchone("SELECT * FROM equity_curve WHERE ts>=? ORDER BY ts ASC LIMIT 1",
                              (_finite(since, default=0.0),))

    def previous_equity(self, before_ts: float) -> Optional[Dict[str, Any]]:
        """`ts < before_ts` 的**最后**一个权益点（「日初那一刻之前的值」的代理）。"""
        return self._fetchone("SELECT * FROM equity_curve WHERE ts<? ORDER BY ts DESC LIMIT 1",
                              (_finite(before_ts, default=0.0),))

    def equity_curve(self, *, since: Optional[float] = None, until: Optional[float] = None,
                     limit: int = 500) -> List[Dict[str, Any]]:
        """权益曲线（**按时间升序**返回最近 `limit` 个点；面板画图直接吃）。"""
        clauses, params = [], []
        if since is not None:
            clauses.append("ts>=?")
            params.append(_finite(since, default=0.0))
        if until is not None:
            clauses.append("ts<=?")
            params.append(_finite(until, default=0.0))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        rows = self._fetchall(
            f"SELECT * FROM equity_curve {where} ORDER BY ts DESC LIMIT ?", params)
        return list(reversed(rows))

    def equity_point_count(self) -> int:
        return int(self._scalar("SELECT COUNT(*) AS n FROM equity_curve", default=0) or 0)

    # ------------------------------------------------------------ 每日计数器

    def daily_counter(self, day: Optional[str] = None) -> Optional[Dict[str, Any]]:
        key = str(day) if day is not None else utc_day()
        day_bounds(key)                      # 校验 'YYYY-MM-DD'（坏 key 会被静默查空）
        return self._fetchone("SELECT * FROM daily_counters WHERE day=?", (key,))

    def ensure_day(self, day: Optional[str] = None, *, day_start_equity: Optional[float] = None,
                   ts: Optional[float] = None) -> Dict[str, Any]:
        """确保当天有计数行（缺则建，`day_start_equity` 只写一次）。

        「只写一次」是有意的：日初权益是当天的**基准**，同一天第二次调用传入新值不应
        把它改掉 —— 否则「当日亏损 5%」的判据会跟着权益浮动，永远触发不了。
        """
        key = str(day or utc_day(now() if ts is None else ts))
        stamp = now() if ts is None else _finite(ts, default=now())
        start = _finite(day_start_equity)
        with self.write_transaction() as conn:
            row = conn.execute("SELECT * FROM daily_counters WHERE day=?", (key,)).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO daily_counters(day, trades_today, day_start_equity,
                                                  realized_pnl, updated_at)
                       VALUES(?,?,?,?,?)""",
                    (key, 0, start, 0.0, stamp))
            elif start is not None and row["day_start_equity"] is None:
                conn.execute("UPDATE daily_counters SET day_start_equity=?, updated_at=? "
                             "WHERE day=?", (start, stamp, key))
        out = self.daily_counter(key)
        assert out is not None
        return out

    def trades_today(self, day: Optional[str] = None) -> int:
        """当天**下单笔数**：库内计数行与 `trades` 表的开仓腿取**较大值**。

        为什么要两者取大：计数行会因运维/迁移丢（低估 → 允许重试风暴），`trades` 表可能
        因为只同步了部分成交而少算。取大者是**更严**的一侧，符合 fail-closed。
        """
        key = str(day or utc_day())
        counter = self.daily_counter(key)
        stored = int(counter["trades_today"]) if counter is not None else 0
        return max(stored, self.entry_trade_count(key))

    def entry_trade_count(self, day: Optional[str] = None) -> int:
        """当天 `trades` 表里的**开仓腿**行数（不走计数行，用于交叉校验）。"""
        key = str(day or utc_day())
        start, end = day_bounds(key)
        return int(self._scalar(
            "SELECT COUNT(*) AS n FROM trades WHERE kind='entry' AND ts>=? AND ts<?",
            (start, end), default=0) or 0)

    def daily_counters(self, *, limit: int = 30) -> List[Dict[str, Any]]:
        return self._fetchall("SELECT * FROM daily_counters ORDER BY day DESC LIMIT ?",
                              (max(1, int(limit)),))

    # ------------------------------------------------------------ 回撤锚点

    def dd_state(self) -> Optional[Dict[str, Any]]:
        """持久回撤活状态（`name='live'`）。没有行 = 从没 bootstrap 过 / 被人工删了。"""
        return self._fetchone("SELECT * FROM dd_state WHERE name=?", (DD_STATE_KEY,))

    def save_dd_state(self, *, twr_index: float, twr_peak: float, anchor_ts: float,
                      anchor_equity: float, anchor_cum_flow: float = 0.0,
                      ts: Optional[float] = None) -> Dict[str, Any]:
        """写回撤活状态（T9 `dd.py` 每轮算完调；`reset_count` 等审计列保持不变）。

        **不得用来降低峰值**：`twr_peak` 小于库内现值时直接拒（抛 `StoreError`）。
        合法的「降峰值」只有一条路：`reset_dd_peak()`（人工 + admin token）。
        """
        index = _finite(twr_index)
        peak = _finite(twr_peak)
        if index is None or index < 0:
            raise StoreError(f"twr_index must be a finite non-negative number, got {twr_index!r}")
        if peak is None or peak <= 0:
            raise StoreError(f"twr_peak must be a finite positive number, got {twr_peak!r}")
        if peak + 1e-12 < index:
            raise StoreError(f"twr_peak ({peak}) must be >= twr_index ({index})")
        a_ts = _finite(anchor_ts)
        a_eq = _finite(anchor_equity)
        if a_ts is None or a_ts <= 0 or a_eq is None or a_eq <= 0:
            raise StoreError(f"invalid dd anchor (ts={anchor_ts!r}, equity={anchor_equity!r})")
        stamp = now() if ts is None else _finite(ts, default=now())
        flow = _finite(anchor_cum_flow, 0.0) or 0.0
        with self.write_transaction() as conn:
            row = conn.execute("SELECT twr_peak FROM dd_state WHERE name=?",
                               (DD_STATE_KEY,)).fetchone()
            if row is not None:
                stored_peak = _finite(row["twr_peak"])
                if stored_peak is not None and peak < stored_peak - 1e-12:
                    raise StoreError(
                        f"refusing to lower the TWR peak from {stored_peak} to {peak} — "
                        f"a lower peak silently hides drawdown; use reset_dd_peak() "
                        f"(admin-only) if that is really intended"
                    )
            conn.execute(
                """INSERT INTO dd_state(name, anchor_ts, anchor_equity, anchor_cum_flow,
                                        twr_index, twr_peak, reset_count, updated_at)
                   VALUES(?,?,?,?,?,?,0,?)
                   ON CONFLICT(name) DO UPDATE SET
                       anchor_ts=excluded.anchor_ts,
                       anchor_equity=excluded.anchor_equity,
                       anchor_cum_flow=excluded.anchor_cum_flow,
                       twr_index=excluded.twr_index,
                       twr_peak=excluded.twr_peak,
                       updated_at=excluded.updated_at""",
                (DD_STATE_KEY, a_ts, a_eq, flow, index, peak, stamp),
            )
        out = self.dd_state()
        assert out is not None
        return out

    def reset_dd_peak(self, *, anchor_ts: float, anchor_equity: float,
                      reason: str, twr_index: float = BASE_TWR_INDEX,
                      anchor_cum_flow: float = 0.0, ts: Optional[float] = None) -> Dict[str, Any]:
        """**唯一**允许把峰值调低/重置的入口（T10b `POST /admin/dd/reset`，需 admin token）。

        熔断「永不自解」（PLAN T9）—— 所以自动路径永远不调用本方法。重置后指数与峰值都
        回到基准 `1.0`，并把 `reset_count + 1`、`last_reset_at`、`reset_reason` 落库供审计。
        """
        index = _finite(twr_index, BASE_TWR_INDEX) or BASE_TWR_INDEX
        if index <= 0:
            raise StoreError(f"twr_index must be > 0 when resetting, got {twr_index!r}")
        a_ts = _finite(anchor_ts)
        a_eq = _finite(anchor_equity)
        if a_ts is None or a_ts <= 0 or a_eq is None or a_eq <= 0:
            raise StoreError(f"invalid dd anchor (ts={anchor_ts!r}, equity={anchor_equity!r})")
        text = str(reason or "").strip()
        if not text:
            raise StoreError("reset_dd_peak requires a non-empty reason (audit trail)")
        stamp = now() if ts is None else _finite(ts, default=now())
        flow = _finite(anchor_cum_flow, 0.0) or 0.0
        with self.write_transaction() as conn:
            conn.execute(
                """INSERT INTO dd_state(name, anchor_ts, anchor_equity, anchor_cum_flow,
                                        twr_index, twr_peak, reset_count, last_reset_at,
                                        reset_reason, updated_at)
                   VALUES(?,?,?,?,?,?,1,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                       anchor_ts=excluded.anchor_ts,
                       anchor_equity=excluded.anchor_equity,
                       anchor_cum_flow=excluded.anchor_cum_flow,
                       twr_index=excluded.twr_index,
                       twr_peak=excluded.twr_peak,
                       reset_count=dd_state.reset_count + 1,
                       last_reset_at=excluded.last_reset_at,
                       reset_reason=excluded.reset_reason,
                       updated_at=excluded.updated_at""",
                (DD_STATE_KEY, a_ts, a_eq, flow, index, index, stamp, text, stamp),
            )
            self._log_event(conn, kind="dd_reset", symbol=None,
                            message=f"DD peak reset to {index} by admin: {text}",
                            data={"anchor_ts": a_ts, "anchor_equity": a_eq,
                                  "twr_index": index, "reason": text}, ts=stamp)
        out = self.dd_state()
        assert out is not None
        return out

    def append_dd_anchor(self, ts: float, equity: float, *, twr_index: float,
                         twr_peak: float, cum_flow: float = 0.0) -> Dict[str, Any]:
        """追加一个**可清理**的锚点检查点（面板画回撤历史用）。"""
        stamp = _finite(ts)
        if stamp is None or stamp <= 0:
            raise StoreError(f"anchor ts must be a positive finite number, got {ts!r}")
        eq = _finite(equity)
        index = _finite(twr_index)
        peak = _finite(twr_peak)
        if eq is None or index is None or peak is None:
            raise StoreError("anchor equity/twr_index/twr_peak must be finite numbers")
        with self.write_transaction() as conn:
            conn.execute(
                """INSERT INTO dd_anchors(ts, equity, cum_flow, twr_index, twr_peak, created_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(ts) DO UPDATE SET
                       equity=excluded.equity, cum_flow=excluded.cum_flow,
                       twr_index=excluded.twr_index, twr_peak=excluded.twr_peak""",
                (stamp, eq, _finite(cum_flow, 0.0) or 0.0, index, peak, now()))
        out = self._fetchone("SELECT * FROM dd_anchors WHERE ts=?", (stamp,))
        assert out is not None
        return out

    def latest_dd_anchor(self) -> Optional[Dict[str, Any]]:
        return self._fetchone("SELECT * FROM dd_anchors ORDER BY ts DESC LIMIT 1")

    def dd_anchors(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM dd_anchors ORDER BY ts DESC LIMIT ?",
                              (max(1, int(limit)),))
        return list(reversed(rows))

    def dd_anchor_count(self) -> int:
        return int(self._scalar("SELECT COUNT(*) AS n FROM dd_anchors", default=0) or 0)

    def prune_dd_anchors(self, *, keep_days: float = DD_ANCHOR_KEEP_DAYS,
                         ts: Optional[float] = None) -> int:
        """清理 30 天前的锚点，返回删除行数。

        两条保护：
        1. **最新一行永不删**（即使它比窗口还老）。表被清空是「回撤无从重建」的最坏
           输入，不做这种自伤。
        2. **绝不碰 `dd_state`**。峰值的事实来源在那里，删锚点不影响回撤判定 ——
           这正是「锚点过期 ≠ 回撤归零」的实现点。
        """
        stamp = now() if ts is None else _finite(ts, default=now())
        days = _finite(keep_days, DD_ANCHOR_KEEP_DAYS)
        if days is None or days <= 0:
            raise StoreError(f"keep_days must be > 0, got {keep_days!r}")
        cutoff = stamp - days * 86400.0
        with self.write_transaction() as conn:
            newest = conn.execute("SELECT MAX(ts) AS m FROM dd_anchors").fetchone()
            newest_ts = _finite(newest["m"] if newest is not None else None)
            cursor = conn.execute(
                "DELETE FROM dd_anchors WHERE ts < ? AND ts < ?",
                (cutoff, newest_ts if newest_ts is not None else 0.0))
            removed = int(cursor.rowcount or 0)
        return removed

    # ------------------------------------------------------------ 事件

    def log_event(self, kind: str, message: str, *, symbol: Optional[str] = None,
                  data: Optional[Mapping[str, Any]] = None,
                  ts: Optional[float] = None) -> int:
        stamp = now() if ts is None else _finite(ts, default=now())
        with self.write_transaction() as conn:
            return self._log_event(conn, kind=kind, message=message, symbol=symbol,
                                   data=data, ts=stamp)

    def events(self, *, limit: int = 20, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        if kind is None:
            return self._fetchall("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?",
                                  (max(1, int(limit)),))
        return self._fetchall(
            "SELECT * FROM events WHERE kind=? ORDER BY ts DESC, id DESC LIMIT ?",
            (str(kind), max(1, int(limit))))

    def event_count(self) -> int:
        return int(self._scalar("SELECT COUNT(*) AS n FROM events", default=0) or 0)

    # ------------------------------------------------------------ 面板 / 自检

    def panel_snapshot(self, *, ts: Optional[float] = None, trade_limit: int = 20,
                       curve_limit: int = 500) -> Dict[str, Any]:
        """T10 面板要的全部 DB 侧数据（**只读**，一次查询组装）。"""
        stamp = now() if ts is None else _finite(ts, default=now())
        day = utc_day(stamp)
        start, end = day_bounds(day)
        positions = self.open_positions()
        by_symbol = self.open_notional_by_symbol()
        return {
            "as_of": stamp,
            "day": day,
            "positions": positions,
            "open_symbols": list(self.open_symbols()),
            "open_count": len(positions),
            "open_notional_by_symbol": by_symbol,
            "open_notional_total": sum(by_symbol.values()),
            "trades": self.trades(limit=trade_limit),
            "trade_count": self.trade_count(),
            "equity_curve": self.equity_curve(since=start - 30 * 86400.0, limit=curve_limit),
            "last_equity": self.last_equity(),
            "daily": self.daily_counter(day),
            "trades_today": self.trades_today(day),
            "realized_pnl_today": self.realized_pnl(since=start),
            "dd_state": self.dd_state(),
            "dd_anchor": self.latest_dd_anchor(),
            "dd_anchor_count": self.dd_anchor_count(),
            "events": self.events(limit=trade_limit),
        }

    def audit(self) -> List[AuditFinding]:
        """不变量自检（面板状态条 / watchdog 吃）。空列表 = 干净。

        目前三条：
        * `open_without_entry_trade`：`OPEN`/`CLOSING` 仓位没有开仓成交 —— 原子性保证
          不该出现；出现即说明有人绕过本模块直接写库，或历史上出过半状态。
        * `trade_without_position`：成交指向不存在的仓位 id。
        * `dd_peak_below_index`：峰值 < 指数（回撤会算成负数被人为截断成 0 → 低估）。
        """
        findings: List[AuditFinding] = []
        placeholders = ",".join("?" * len(OPEN_STATES))
        rows = self._fetchall(
            f"""SELECT p.id, p.symbol, p.state FROM positions p
                 WHERE p.state IN ({placeholders})
                   AND NOT EXISTS (SELECT 1 FROM trades t
                                    WHERE t.position_id=p.id AND t.kind='entry')""",
            OPEN_STATES)
        for row in rows:
            findings.append(AuditFinding(code="open_without_entry_trade", detail=dict(row)))

        orphans = self._fetchall(
            """SELECT t.id, t.position_id FROM trades t
                WHERE t.position_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.id=t.position_id)""")
        for row in orphans:
            findings.append(AuditFinding(code="trade_without_position", detail=dict(row)))

        state = self.dd_state()
        if state is not None:
            index = _finite(state.get("twr_index"))
            peak = _finite(state.get("twr_peak"))
            if index is None or peak is None or peak <= 0 or index > peak + 1e-9:
                findings.append(AuditFinding(code="dd_peak_below_index", detail=dict(state)))
        return findings

    def integrity_check(self) -> str:
        """SQLite 自身的结构检查（`ok` = 正常）。"""
        with self._read() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row is not None else "unknown"

    def checkpoint(self) -> None:
        """把 WAL 合并回主库（运维/备份前调用；不影响在线读）。"""
        if self._is_memory or self.journal_mode != "wal":
            return
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _symbol(symbol: Any) -> str:
        text = str(symbol or "").strip().upper()
        if not text:
            raise StoreError("symbol is required")
        return text

    @staticmethod
    def _side(side: Any) -> int:
        value = _int(side, "side")
        if value not in (-1, 1):
            raise StoreError(f"side must be +1 (long) or -1 (short), got {side!r}")
        return value

    @staticmethod
    def _check_transition(current: str, target: str) -> None:
        allowed = ALLOWED_TRANSITIONS.get(current)
        if allowed is None:
            raise InvalidTransition(f"unknown position state {current!r}")
        if target not in allowed:
            raise InvalidTransition(
                f"illegal position transition {current} -> {target} "
                f"(allowed from {current}: {allowed or 'none (terminal)'})"
            )

    def _insert_trade_row(self, conn: sqlite3.Connection,
                          row: Mapping[str, Any]) -> Tuple[int, bool]:
        """插一笔成交（幂等）。返回 `(trade_id, inserted)`。

        唯一键在**写入前**由 `trade_key()` 算好并落库（`idempotency_key` 是表上的
        `NOT NULL UNIQUE`），所以去重是数据库保证的，不是「我查了一下」的竞态。

        `INSERT OR IGNORE` 被忽略时 `cursor.rowcount == 0` 且 `lastrowid` 会残留上一次
        成功插入的 id（可能来自别的表）—— 必须回查真实行，绝不能用 `lastrowid`。
        """
        key = trade_key(fill_id=row.get("fill_id"), order_id=row.get("order_id"),
                        symbol=row.get("symbol"), kind=row.get("kind"),
                        side=row.get("side"), price_int=row.get("price_int"),
                        qty_int=row.get("qty_int"), price=row.get("price"),
                        qty=row.get("qty"),
                        ts_ms=int(round(float(row.get("ts") or 0.0) * 1000.0)))
        cursor = conn.execute(
            """INSERT OR IGNORE INTO trades(ts, symbol, side, price, price_int, qty, qty_int,
                    fee, fee_token, kind, order_id, fill_id, position_id, realized_pnl,
                    bar_ms, raw, client_order_index, idempotency_key, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (float(row["ts"]), str(row["symbol"]), int(row["side"]), float(row["price"]),
             row.get("price_int"), float(row["qty"]), row.get("qty_int"),
             float(row.get("fee") or 0.0), row.get("fee_token"), str(row["kind"]),
             row.get("order_id"), row.get("fill_id"), row.get("position_id"),
             _finite(row.get("realized_pnl")), row.get("bar_ms"), row.get("raw"),
             row.get("client_order_index"), key, now()),
        )
        if cursor.rowcount:
            return int(cursor.lastrowid or 0), True
        existing = conn.execute("SELECT id FROM trades WHERE idempotency_key=?",
                                (key,)).fetchone()
        return (int(existing["id"]) if existing is not None else 0), False

    def _bump_counter(self, conn: sqlite3.Connection, day: str, *, entries: int,
                      realized_pnl: float) -> None:
        stamp = now()
        conn.execute(
            """INSERT INTO daily_counters(day, trades_today, day_start_equity, realized_pnl,
                                          updated_at)
               VALUES(?,?,NULL,?,?)
               ON CONFLICT(day) DO UPDATE SET
                   trades_today=daily_counters.trades_today + ?,
                   realized_pnl=daily_counters.realized_pnl + ?,
                   updated_at=excluded.updated_at""",
            (day, max(0, int(entries)), float(realized_pnl), stamp,
             max(0, int(entries)), float(realized_pnl)),
        )

    def _ensure_day_start(self, conn: sqlite3.Connection, day: str,
                          equity: Optional[float], ts: float) -> None:
        """同一天第一次拿到权益时写 `day_start_equity`；之后不再改（见 `ensure_day`）。"""
        if equity is None:
            return
        conn.execute("UPDATE daily_counters SET day_start_equity=COALESCE(day_start_equity, ?) "
                     "WHERE day=?", (float(equity), day))

    def _log_event(self, conn: sqlite3.Connection, *, kind: str, message: str,
                   symbol: Optional[str] = None, data: Optional[Mapping[str, Any]] = None,
                   ts: Optional[float] = None) -> int:
        stamp = now() if ts is None else float(ts)
        cursor = conn.execute(
            "INSERT INTO events(ts, kind, symbol, message, data, created_at) VALUES(?,?,?,?,?,?)",
            (stamp, str(kind), None if symbol is None else str(symbol).upper(), str(message),
             None if data is None else json.dumps(dict(data), sort_keys=True, default=str),
             now()))
        return int(cursor.lastrowid or 0)


#: 短别名（`from app.store import Store` 也能用；本项目统一叫 `StateStore`）。
Store = StateStore
