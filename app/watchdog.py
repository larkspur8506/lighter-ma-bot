"""健康监控（watchdog）：每 5 分钟看一眼系统还活着吗、数据还在动吗、报警还发得出去吗。

职责边界
--------
* **判定是纯函数**：`check_health(snapshot) -> HealthReport`。输入是一份
  `HealthSnapshot`（探针结果 + 时间戳），输出是逐项结论。没有网络、没有 DB、没有时钟，
  所以每条规则都能用一个 dict 直接钉死（`tests/test_watchdog.py` 全部这么写）。
* **采集是注入的**：`Watchdog.collect()` 调探针（容器 / `/health` / 心跳 / bar_state /
  推送 / DB 可写），每个探针都可替换；默认实现只用标准库 + 已有的 `app.market` 新鲜度口径。
* **执行器不抛异常**：`Watchdog.run_once()` 里任何一层崩掉都会被接住、落 `events` +
  写日志，然后返回一份带 `error` 的报告。watchdog 自己挂掉不能带崩主循环 ——
  但**也绝不能静默**（静默等于「监控把自己也监控没了」）。

六项检查（PLAN T9）
-------------------
====================  ====================  ============================================
检查                  阈值（默认）           触发即
====================  ====================  ============================================
`container`           进程内 PID 1          PID 1 不存在 / 已是僵尸 → error
`health`              200 + `ok != false`   `/health` 不通 / 非 200 → error
`heartbeat`           6h                    调度器超过 6h 没有 tick → error；从没有过 → warn
`bar_state`           8h（= 4h × 2）        最后一根**已收盘** bar 的收盘时刻太旧 → error
`push`                近 6h 失败 ≥ 3 条     1–2 条 → warn；≥3 条 → error
`db_write`            能写（临时表）        `BEGIN IMMEDIATE` 拿不到写锁 / 写入失败 → error
====================  ====================  ============================================

阈值为什么是这些数
------------------
* **心跳 6h**：调度周期是 4h 一根 bar（`:03` / `:33`）。容忍窗口必须 > 一个周期（否则每次
  收盘之间都会误报），但 < 两个周期（否则「漏掉一整根 bar」不算事故）。6h = 1.5 个周期，
  恰好卡在「漏一根就该喊人」的位置。
* **bar 8h**：直接复用 `app.market.FRESH_MAX_AGE_MS = 2 × BAR_MS`。整个系统对「K 线新鲜」
  只有一个口径，watchdog 不另发明一个数 —— 否则「取数说新鲜、监控说停摆」这种事一定会发生。
* **推送 6h / 3 条**：一条推送失败可能只是网络抖动；同一窗口内 3 条说明通道真的坏了。
  只在 1–2 条时 warn，避免单次抖动就喊人。

为什么「容器存活」只看 PID 1
----------------------------
watchdog 跑在容器**里面**，看不到 docker/systemd 对容器的判定；它能看到的最强证据是
「PID 1 还在、而且不是僵尸」。容器被 `docker rm` 掉、OOM kill 掉时容器内的进程随之消失，
这条检查自然也不会再跑 —— 那属于**外部**监控（T12 的 compose healthcheck / 宿主机 cron
拉 `/health`）的职责。这里不假装能做到做不到的事：进程内的检查只覆盖「进程还在但已经
半死」这一类故障（HTTP 线程卡住、DB 变只读、调度器停了）。
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.market import BAR_MS, FRESH_MAX_AGE_MS
from app.store import StateStore

__all__ = [
    "CHECK_BAR_STATE", "CHECK_CONTAINER", "CHECK_DB_WRITE", "CHECK_HEALTH",
    "CHECK_HEARTBEAT", "CHECK_PUSH", "DEFAULT_BAR_STALL_AGE_S",
    "DEFAULT_HEARTBEAT_MAX_AGE_S", "DEFAULT_HEALTH_TIMEOUT_S", "DEFAULT_HEALTH_URL",
    "DEFAULT_INTERVAL_S", "DEFAULT_PUSH_FAIL_ALARM", "DEFAULT_PUSH_WINDOW_S",
    "EVENT_REPORT", "EVENT_WATCHDOG_ERROR", "HEARTBEAT_KIND", "STATUS_ERROR", "STATUS_OK",
    "STATUS_SKIPPED", "STATUS_WARN", "BarStateProbe", "CheckResult", "HealthReport",
    "HealthSnapshot", "Watchdog", "WatchdogConfig", "WatchdogConfigError", "check_health",
    "default_container_probe", "default_db_probe", "default_health_probe",
    "default_heartbeat_probe", "record_heartbeat", "run_once",
]

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 常量：检查名

CHECK_CONTAINER = "container"
CHECK_HEALTH = "health"
CHECK_HEARTBEAT = "heartbeat"
CHECK_BAR_STATE = "bar_state"
CHECK_PUSH = "push"
CHECK_DB_WRITE = "db_write"

CHECK_ORDER: Tuple[str, ...] = (CHECK_CONTAINER, CHECK_HEALTH, CHECK_HEARTBEAT,
                                CHECK_BAR_STATE, CHECK_PUSH, CHECK_DB_WRITE)

# ------------------------------------------------------------------ 常量：状态

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

# ------------------------------------------------------------------ 常量：默认阈值

DEFAULT_INTERVAL_S = 300.0                        # 5 分钟一轮（PLAN T9）
DEFAULT_HEARTBEAT_MAX_AGE_S = 6 * 3600.0          # 1.5 个 4h 周期
DEFAULT_BAR_STALL_AGE_S = FRESH_MAX_AGE_MS / 1000.0   # 8h = 2 根 4h bar
DEFAULT_PUSH_WINDOW_S = 6 * 3600.0
DEFAULT_PUSH_FAIL_ALARM = 3
DEFAULT_HEALTH_TIMEOUT_S = 5.0
DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/health"
DEFAULT_CONTAINER_PID_PATH = "/proc/1/stat"

#: 事件类型
EVENT_REPORT = "watchdog_report"
EVENT_WATCHDOG_ERROR = "watchdog_error"
HEARTBEAT_KIND = "heartbeat"
ALERT_FAILED_KIND = "alert_failed"                # 与 T8 executor 的推送失败事件同名同义

#: 允许的时钟轻微超前后缀（心跳时间戳比 now 还新）：只影响措辞，不影响判定。
_CLOCK_SKEW_TOLERANCE_S = 300.0


class WatchdogConfigError(ValueError):
    """watchdog 配置非法。**启动时就报**，不静默回默认值（回默认值等于悄悄关掉检查）。"""


def _finite(value: Any, default: Optional[float] = None) -> Optional[float]:
    """安全转 float：`None` / 垃圾 / `NaN` / `±inf` → `default`。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


# ------------------------------------------------------------------ 结论对象


@dataclass(frozen=True)
class CheckResult:
    """单项检查的结论。`status` 四态，`code` 是稳定机读码（T10 面板/告警吃它）。"""

    check: str
    status: str = STATUS_OK
    code: str = ""
    message: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_OK, STATUS_SKIPPED)

    @property
    def failed(self) -> bool:
        return self.status == STATUS_ERROR

    @property
    def warned(self) -> bool:
        return self.status == STATUS_WARN

    def as_dict(self) -> Dict[str, Any]:
        return {"check": self.check, "status": self.status,
                "code": self.code or self.check, "message": self.message,
                "detail": dict(self.detail)}

    def __str__(self) -> str:
        return f"[{self.status}] {self.check}: {self.code} {self.message}".strip()


@dataclass(frozen=True)
class HealthSnapshot:
    """一次健康检查的输入快照（探针结果 + 时刻）。**没有行为**，纯数据。

    每个字段为 `None` 的语义都是「这一项没接上」→ 对应检查 `skipped`（而不是假装 ok）。
    """

    now: float
    container: Optional[Mapping[str, Any]] = None
    health: Optional[Mapping[str, Any]] = None
    heartbeat_ts: Optional[float] = None
    heartbeat_source: str = "missing"
    bar_state: Optional[Mapping[str, Mapping[str, Any]]] = None
    push_failures: Optional[int] = None
    push_window_s: float = DEFAULT_PUSH_WINDOW_S
    db_writable: Optional[bool] = None
    db_detail: Mapping[str, Any] = field(default_factory=dict)
    #: 采集阶段抛异常的探针（`(name, "ExcType: msg")`），由 `run_once` 落 events。
    probe_errors: Tuple[Tuple[str, str], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "now": self.now,
            "container": dict(self.container) if self.container is not None else None,
            "health": dict(self.health) if self.health is not None else None,
            "heartbeat_ts": self.heartbeat_ts, "heartbeat_source": self.heartbeat_source,
            "bar_state": ({k: dict(v) for k, v in (self.bar_state or {}).items()}
                          if self.bar_state is not None else None),
            "push_failures": self.push_failures, "push_window_s": self.push_window_s,
            "db_writable": self.db_writable, "db_detail": dict(self.db_detail),
            "probe_errors": [list(pair) for pair in self.probe_errors],
        }


@dataclass(frozen=True)
class HealthReport:
    """一次健康检查的结论：逐项 `CheckResult` + 汇总状态。"""

    generated_at: float
    checks: Tuple[CheckResult, ...] = ()
    interval_s: float = DEFAULT_INTERVAL_S
    #: 判定过程本身崩了（`run_once` 兜底路径）；非空时 `status` 一定是 `error`。
    error: str = ""

    # ---------------------------------------------------------------- 汇总

    @property
    def errors(self) -> Tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == STATUS_ERROR)

    @property
    def warnings(self) -> Tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == STATUS_WARN)

    @property
    def skipped(self) -> Tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == STATUS_SKIPPED)

    @property
    def status(self) -> str:
        """`error` > `warn` > `ok`。**`skipped` 按 `warn` 算**。

        为什么算不成 ok：`skipped` 的意思是「这一项根本没查」，而监控最典型的失效
        就是「以为在查、其实一直没查」。宁可让运维看见一条黄灯去接线，也不要一个
        永远绿灯、什么都看不见的 watchdog。
        """
        if self.error or self.errors:
            return STATUS_ERROR
        if self.warnings or self.skipped:
            return STATUS_WARN
        if not self.checks:                    # 什么都没查 = 不可信，不能报 ok
            return STATUS_WARN
        return STATUS_OK

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def codes(self) -> Tuple[str, ...]:
        """所有非 ok 项（`warn` / `error` / `skipped`）的机读码。

        `status` 变化检测与告警去重都用它 —— 所以「某一项从没接上变成接上了」也会
        算一次变化，事件流里能看见。
        """
        return tuple(c.code or c.check for c in self.checks
                     if c.status in (STATUS_WARN, STATUS_ERROR, STATUS_SKIPPED))

    @property
    def needs_alert(self) -> bool:
        return bool(self.error) or self.status != STATUS_OK

    def check(self, name: str) -> Optional[CheckResult]:
        for item in self.checks:
            if item.check == name:
                return item
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at, "status": self.status, "ok": self.ok,
            "interval_s": self.interval_s, "error": self.error,
            "codes": list(self.codes),
            "checks": [c.as_dict() for c in self.checks],
        }

    def summary(self) -> str:
        """一行可读摘要（日志/推送正文）。"""
        if self.error:
            return f"watchdog: {STATUS_ERROR} — {self.error}"
        bad = [c for c in self.checks
               if c.status in (STATUS_WARN, STATUS_ERROR, STATUS_SKIPPED)]
        head = f"watchdog: {self.status} ({len(self.checks)} checks"
        head += f", {len(bad)} not ok)" if bad else ")"
        if not bad:
            return head
        return head + " — " + "; ".join(f"{c.check}={c.code or c.check}" for c in bad)

    def __str__(self) -> str:
        return self.summary()


# ------------------------------------------------------------------ 纯函数判定


def check_health(snapshot: HealthSnapshot, *,
                 heartbeat_max_age_s: float = DEFAULT_HEARTBEAT_MAX_AGE_S,
                 bar_stall_age_s: float = DEFAULT_BAR_STALL_AGE_S,
                 push_fail_alarm: int = DEFAULT_PUSH_FAIL_ALARM,
                 interval_s: float = DEFAULT_INTERVAL_S) -> HealthReport:
    """输入快照 → 逐项结论（**纯函数**：不读时钟、不碰网络/DB）。

    `snapshot` 缺项（`None`）= 这一项没接上 → `skipped`，并在 `detail` 里说明「谁没接上」。
    `skipped` 不算 `ok`：`HealthReport.status` 只在六项都真的查过且没过线时才是 `ok`。
    """
    now = _finite(snapshot.now)
    if now is None:
        raise WatchdogConfigError(f"snapshot.now must be a finite epoch second, got "
                                  f"{snapshot.now!r}")
    checks: Tuple[CheckResult, ...] = (
        _check_container(snapshot),
        _check_health_endpoint(snapshot),
        _check_heartbeat(snapshot, now, float(heartbeat_max_age_s)),
        _check_bar_state(snapshot, now, float(bar_stall_age_s)),
        _check_push(snapshot, int(push_fail_alarm)),
        _check_db_write(snapshot),
    )
    return HealthReport(generated_at=now, checks=_apply_probe_errors(checks, snapshot),
                        interval_s=float(interval_s))


def _apply_probe_errors(checks: Sequence[CheckResult],
                        snapshot: HealthSnapshot) -> Tuple[CheckResult, ...]:
    """采集阶段抛过异常的探针 → 那一项改判 `error/probe_error`。

    「探针崩了」绝不能被 `skipped` 掩盖成 `ok`：一个每次抛 `TypeError` 的探针会让
    `HealthReport.status == "ok"`，而实际上这一项**根本没查**。这是监控系统最典型的
    静默失效（旧系统踩过：心跳探针读到 `None` 而整轮仍报绿）。
    """
    failures = {str(name): str(text) for name, text in snapshot.probe_errors}
    if not failures:
        return tuple(checks)
    out: List[CheckResult] = []
    for item in checks:
        text = failures.get(item.check)
        if text is None:
            out.append(item)
            continue
        out.append(replace(item, status=STATUS_ERROR, code="probe_error",
                           message=f"the {item.check} probe raised: {text}"))
    return tuple(out)


def _check_container(snapshot: HealthSnapshot) -> CheckResult:
    info = snapshot.container
    if info is None:
        return CheckResult(CHECK_CONTAINER, STATUS_SKIPPED, "container_unwired",
                           "no container probe wired — liveness is not being checked")
    alive = info.get("alive")
    detail = dict(info)
    if alive is None:
        return CheckResult(CHECK_CONTAINER, STATUS_WARN, "container_unknown",
                           f"container liveness is unknown: {info.get('error') or 'no signal'}",
                           detail)
    if not alive:
        return CheckResult(CHECK_CONTAINER, STATUS_ERROR, "container_dead",
                           f"the container's PID 1 is not running "
                           f"(state={info.get('pid1_state')!r})", detail)
    return CheckResult(CHECK_CONTAINER, STATUS_OK, detail=detail)


def _check_health_endpoint(snapshot: HealthSnapshot) -> CheckResult:
    info = snapshot.health
    if info is None:
        return CheckResult(CHECK_HEALTH, STATUS_SKIPPED, "health_unwired",
                           "no /health probe wired — the HTTP surface is not being checked")
    detail = dict(info)
    if info.get("ok"):
        return CheckResult(CHECK_HEALTH, STATUS_OK, detail=detail)
    return CheckResult(CHECK_HEALTH, STATUS_ERROR, "health_unreachable",
                       f"/health is not healthy ({info.get('error') or info.get('status')!r})",
                       detail)


def _check_heartbeat(snapshot: HealthSnapshot, now: float, max_age_s: float) -> CheckResult:
    stamp = _finite(snapshot.heartbeat_ts)
    source = snapshot.heartbeat_source or "unknown"
    if stamp is None:
        return CheckResult(CHECK_HEARTBEAT, STATUS_WARN, "heartbeat_missing",
                           "no scheduler heartbeat recorded yet — either this is a fresh "
                           "install or the scheduler has never ticked",
                           {"source": source})
    age = now - stamp
    detail = {"heartbeat_ts": stamp, "age_s": age, "max_age_s": max_age_s, "source": source}
    if stamp > now + _CLOCK_SKEW_TOLERANCE_S:
        return CheckResult(CHECK_HEARTBEAT, STATUS_WARN, "heartbeat_in_future",
                           f"heartbeat ts {stamp:.0f} is {age / -3600.0:.2f}h in the future — "
                           f"clock skew or a bad writer", detail)
    if age > max_age_s:
        return CheckResult(CHECK_HEARTBEAT, STATUS_ERROR, "heartbeat_stale",
                           f"the scheduler has not ticked for {age / 3600.0:.2f}h "
                           f"(limit {max_age_s / 3600.0:.1f}h) — entries cannot be placed "
                           f"and stops are only protected by the exchange", detail)
    return CheckResult(CHECK_HEARTBEAT, STATUS_OK, detail=detail)


def _check_bar_state(snapshot: HealthSnapshot, now: float, stall_age_s: float) -> CheckResult:
    state = snapshot.bar_state
    if not state:
        return CheckResult(CHECK_BAR_STATE, STATUS_SKIPPED, "bar_state_unwired",
                           "no bar_state probe wired — feed progress is not being checked")
    now_ms = now * 1000.0
    stalled: List[str] = []
    unknown: List[str] = []
    ages: Dict[str, Any] = {}
    for symbol in sorted(state):
        entry = state.get(symbol) or {}
        last_close = _finite(entry.get("last_close_ms"))
        if last_close is None:
            unknown.append(str(symbol))
            ages[str(symbol)] = {"error": entry.get("error") or "no last closed bar"}
            continue
        age_s = (now_ms - float(last_close)) / 1000.0
        ages[str(symbol)] = {"last_close_ms": float(last_close), "age_s": age_s}
        if age_s > stall_age_s:
            stalled.append(str(symbol))
    detail = {"limit_s": stall_age_s, "symbols": ages,
              "stalled": stalled, "unknown": unknown}
    if unknown:
        return CheckResult(CHECK_BAR_STATE, STATUS_ERROR, "bar_state_unreadable",
                           f"cannot read the last closed bar for {unknown} — the feed is "
                           f"broken for those symbols", detail)
    if stalled:
        worst = max(ages[s]["age_s"] for s in stalled)
        return CheckResult(CHECK_BAR_STATE, STATUS_ERROR, "bar_stalled",
                           f"{stalled}: last closed bar is {worst / 3600.0:.2f}h old "
                           f"(limit {stall_age_s / 3600.0:.1f}h) — the feed has stopped "
                           f"advancing", detail)
    return CheckResult(CHECK_BAR_STATE, STATUS_OK, detail=detail)


def _check_push(snapshot: HealthSnapshot, alarm: int) -> CheckResult:
    failures = snapshot.push_failures
    if failures is None:
        return CheckResult(CHECK_PUSH, STATUS_SKIPPED, "push_unwired",
                           "no push-failure probe wired (notification lands in T10)")
    window_h = float(snapshot.push_window_s) / 3600.0
    detail = {"failures": int(failures), "window_s": float(snapshot.push_window_s),
              "alarm_at": int(alarm)}
    if int(failures) >= int(alarm):
        return CheckResult(CHECK_PUSH, STATUS_ERROR, "push_failing",
                           f"{int(failures)} notification(s) failed in the last {window_h:.1f}h "
                           f"(alarm at {int(alarm)}) — alerts are not reaching anyone", detail)
    if int(failures) > 0:
        return CheckResult(CHECK_PUSH, STATUS_WARN, "push_failures",
                           f"{int(failures)} notification(s) failed in the last {window_h:.1f}h",
                           detail)
    return CheckResult(CHECK_PUSH, STATUS_OK, detail=detail)


def _check_db_write(snapshot: HealthSnapshot) -> CheckResult:
    writable = snapshot.db_writable
    detail = dict(snapshot.db_detail)
    if writable is None:
        return CheckResult(CHECK_DB_WRITE, STATUS_SKIPPED, "db_unwired",
                           "no DB write probe wired — a read-only database would go unnoticed")
    if not writable:
        return CheckResult(CHECK_DB_WRITE, STATUS_ERROR, "db_not_writable",
                           f"the database rejected a write: {detail.get('error') or 'unknown'}",
                           detail)
    return CheckResult(CHECK_DB_WRITE, STATUS_OK, detail=detail)


# ------------------------------------------------------------------ 配置


@dataclass(frozen=True)
class WatchdogConfig:
    """watchdog 的阈值与地址（全部有文档化默认值；填错 → `WatchdogConfigError`）。"""

    interval_s: float = DEFAULT_INTERVAL_S
    heartbeat_max_age_s: float = DEFAULT_HEARTBEAT_MAX_AGE_S
    bar_stall_age_s: float = DEFAULT_BAR_STALL_AGE_S
    push_window_s: float = DEFAULT_PUSH_WINDOW_S
    push_fail_alarm: int = DEFAULT_PUSH_FAIL_ALARM
    health_url: str = DEFAULT_HEALTH_URL
    health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S
    symbols: Tuple[str, ...] = ("BTC", "ETH", "SOL")

    def __post_init__(self) -> None:
        for name in ("interval_s", "heartbeat_max_age_s", "bar_stall_age_s",
                     "push_window_s", "health_timeout_s"):
            value = _finite(getattr(self, name))
            if value is None or value <= 0:
                raise WatchdogConfigError(f"{name} must be a positive finite number, "
                                          f"got {getattr(self, name)!r}")
        alarm = _finite(self.push_fail_alarm)
        if alarm is None or alarm < 1 or alarm != int(alarm):
            raise WatchdogConfigError(f"push_fail_alarm must be a positive integer, "
                                      f"got {self.push_fail_alarm!r}")
        if not str(self.health_url or "").strip():
            raise WatchdogConfigError("health_url must not be empty")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "WatchdogConfig":
        """从环境变量构造（缺项用默认值；填了但不合法 → `WatchdogConfigError`）。"""
        source: Mapping[str, str] = os.environ if env is None else env

        def num(key: str, default: float) -> float:
            raw = (source.get(key) or "").strip()
            if not raw:
                return float(default)
            try:
                return float(raw)
            except ValueError:
                raise WatchdogConfigError(f"{key} must be a number, got {raw!r}") from None

        def integer(key: str, default: int) -> int:
            raw = (source.get(key) or "").strip()
            if not raw:
                return int(default)
            try:
                return int(raw, 10)
            except ValueError:
                raise WatchdogConfigError(f"{key} must be a base-10 integer, "
                                          f"got {raw!r}") from None

        symbols_raw = (source.get("WATCHDOG_SYMBOLS") or "").strip()
        symbols = (tuple(s.strip().upper() for s in symbols_raw.split(",") if s.strip())
                   if symbols_raw else cls.symbols)
        return cls(
            interval_s=num("WATCHDOG_INTERVAL_S", DEFAULT_INTERVAL_S),
            heartbeat_max_age_s=num("WATCHDOG_HEARTBEAT_MAX_AGE_S",
                                    DEFAULT_HEARTBEAT_MAX_AGE_S),
            bar_stall_age_s=num("WATCHDOG_BAR_STALL_AGE_S", DEFAULT_BAR_STALL_AGE_S),
            push_window_s=num("WATCHDOG_PUSH_WINDOW_S", DEFAULT_PUSH_WINDOW_S),
            push_fail_alarm=integer("WATCHDOG_PUSH_FAIL_ALARM", DEFAULT_PUSH_FAIL_ALARM),
            health_url=(source.get("WATCHDOG_HEALTH_URL") or DEFAULT_HEALTH_URL).strip(),
            health_timeout_s=num("WATCHDOG_HEALTH_TIMEOUT_S", DEFAULT_HEALTH_TIMEOUT_S),
            symbols=tuple(symbols),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "interval_s": float(self.interval_s),
            "heartbeat_max_age_s": float(self.heartbeat_max_age_s),
            "bar_stall_age_s": float(self.bar_stall_age_s),
            "push_window_s": float(self.push_window_s),
            "push_fail_alarm": int(self.push_fail_alarm),
            "health_url": str(self.health_url),
            "health_timeout_s": float(self.health_timeout_s),
            "symbols": list(self.symbols),
        }


# ------------------------------------------------------------------ 探针（默认实现）


def default_container_probe(stat_path: str = DEFAULT_CONTAINER_PID_PATH) -> Dict[str, Any]:
    """容器内「进程还在吗」：读 `/proc/1/stat` 的进程状态。

    `Z`（僵尸）/`X`（已死）算不活；读不到文件 → `alive=None`（`container_unknown`），
    不猜。进程内的 watchdog 看不到 docker 的容器状态，这一点写在模块 docstring 里。
    """
    try:
        with open(stat_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        return {"alive": None, "error": f"{type(exc).__name__}: {exc}",
                "stat_path": stat_path}
    try:
        state = text.rsplit(")", 1)[1].split()[0]
    except (IndexError, AttributeError):
        return {"alive": None, "error": "unparseable /proc/1/stat", "stat_path": stat_path}
    return {"alive": state not in ("Z", "X"), "pid1_state": state, "stat_path": stat_path}


def default_health_probe(url: str = DEFAULT_HEALTH_URL,
                         timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S) -> Dict[str, Any]:
    """`GET {url}`：200 且（JSON 里 `ok` 不为 false）才算健康。异常归一化成 dict。"""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=float(timeout_s)) as response:  # noqa: S310
            status = int(getattr(response, "status", 0) or 0)
            body = response.read(64 * 1024)
    except Exception as exc:                                  # noqa: BLE001 - 探针不抛
        return {"ok": False, "status": None, "url": url,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": (time.monotonic() - started) * 1000.0}
    parsed: Any = None
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        parsed = None
    flag = parsed.get("ok") if isinstance(parsed, Mapping) else None
    return {"ok": status == 200 and flag is not False, "status": status, "url": url,
            "body_ok": flag, "latency_ms": (time.monotonic() - started) * 1000.0}


def default_db_probe(store: StateStore) -> Dict[str, Any]:
    """真写一次再撤掉：`BEGIN IMMEDIATE` + 临时表。

    为什么不用「读一下」代替：「库还能读」和「库还能写」是两件事 —— 磁盘满、只读挂载、
    WAL 目录权限掉了，读全都正常，写全失败。临时表只在连接内存在，写完即删，不污染数据。
    """
    started = time.monotonic()
    try:
        with store.write_transaction() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _watchdog_probe(n INTEGER)")
            conn.execute("DELETE FROM _watchdog_probe")
            conn.execute("INSERT INTO _watchdog_probe(n) VALUES(1)")
            row = conn.execute("SELECT n FROM _watchdog_probe").fetchone()
            conn.execute("DROP TABLE _watchdog_probe")
    except Exception as exc:                                  # noqa: BLE001 - 探针不抛
        return {"writable": False, "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": (time.monotonic() - started) * 1000.0}
    value = row[0] if row is not None else None
    return {"writable": value == 1, "value": value,
            "latency_ms": (time.monotonic() - started) * 1000.0}


def default_heartbeat_probe(store: StateStore) -> Tuple[Optional[float], str]:
    """调度器最后一次 tick 的时刻。

    优先读 `events` 里 `kind='heartbeat'`（T10 调度器每轮调 :func:`record_heartbeat` 写一条）；
    没有就用**最新权益点**的时刻 —— T8 的 tick 每轮都会 `record_equity()`，所以它天然是
    一个「调度器活着」的痕迹。两者都没有 → `(None, 'missing')`（检查报 `heartbeat_missing`）。
    """
    try:
        rows = store.events(kind=HEARTBEAT_KIND, limit=1)
    except Exception:                                         # noqa: BLE001 - 探针不抛
        rows = []
    for row in rows:
        stamp = _finite((row or {}).get("ts"))
        if stamp is not None:
            return stamp, HEARTBEAT_KIND
    try:
        last = store.last_equity()
    except Exception:                                         # noqa: BLE001
        last = None
    stamp = _finite((last or {}).get("ts"))
    if stamp is not None:
        return stamp, "equity_curve"
    return None, "missing"


def default_push_probe(store: StateStore, *,
                       window_s: float = DEFAULT_PUSH_WINDOW_S,
                       now_ts: Optional[float] = None) -> int:
    """近 `window_s` 内 `events` 里 `alert_failed` 的条数（T8 与 watchdog 自己都写这个 kind）。"""
    stamp = _finite(now_ts)
    if stamp is None:
        stamp = time.time()
    cutoff = stamp - float(window_s)
    rows = store.events(kind=ALERT_FAILED_KIND, limit=500)
    return sum(1 for row in rows if (_finite((row or {}).get("ts")) or 0.0) >= cutoff)


def record_heartbeat(store: StateStore, *, ts: Optional[float] = None,
                     source: str = "scheduler",
                     data: Optional[Mapping[str, Any]] = None) -> int:
    """写一条心跳事件（**T10 调度器每轮调一次**，watchdog 用它判「调度器还在跑吗」）。"""
    stamp = float(ts) if ts is not None else time.time()
    payload = dict(data or {})
    payload.setdefault("source", str(source))
    return store.log_event(HEARTBEAT_KIND, f"tick heartbeat from {source}",
                           data=payload, ts=stamp)


class BarStateProbe:
    """`symbol -> 最后一根**已收盘** bar 的收盘时刻（ms）`。

    口径与 `app.market` 完全一致：`open_ms + BAR_MS <= now` 才算收盘，
    `BarsResult.last_close_ms` 就是「最后一根已收盘 bar 的收盘时刻」。
    单标的取数失败不抛：把 `{"error": ...}` 记进去，由 `_check_bar_state()` 报
    `bar_state_unreadable`（一只标的的 feed 坏了也是事故）。
    """

    def __init__(self, market: Any, symbols: Iterable[str],
                 *, clock_ms: Optional[Callable[[], int]] = None) -> None:
        self.market = market
        self.symbols: Tuple[str, ...] = tuple(str(s).strip().upper() for s in symbols)
        self.clock_ms = clock_ms

    def __call__(self) -> Dict[str, Dict[str, Any]]:
        now_ms = int(self.clock_ms()) if self.clock_ms is not None else None
        out: Dict[str, Dict[str, Any]] = {}
        for symbol in self.symbols:
            try:
                kwargs = {} if now_ms is None else {"now_ms": now_ms}
                result = self.market.bars(symbol, **kwargs)
            except Exception as exc:                          # noqa: BLE001 - 单标的失败不致命
                out[symbol] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            last_close = getattr(result, "last_close_ms", None)
            bars = list(getattr(result, "bars", ()) or ())
            if last_close is None and bars:
                last_close = int(getattr(bars[-1], "open_ms", 0)) + BAR_MS
            out[symbol] = {
                "last_close_ms": None if last_close is None else int(last_close),
                "bars": len(bars),
            }
        return out


# ------------------------------------------------------------------ 执行器


class Watchdog:
    """每 5 分钟跑一轮：采集 → 判定 → 落 `events` → （有异常时）推送。

    依赖全部可注入，因此单测可以全离线::

        wd = Watchdog(store, config=WatchdogConfig(), health_probe=lambda: {...},
                      container_probe=lambda: {"alive": True}, ...)
        report = wd.run_once(now_ts=...)

    **不抛异常**：`collect()` 里每个探针单独 try，`run_once()` 外层再兜一层。任何异常都会
    (a) 落 `events`（`watchdog_error`）、(b) 写日志、(c) 让 `report.status='error'`。
    这**不是**静默吞异常 —— 静默吞等于监控失明还没人知道。
    """

    def __init__(self, store: StateStore, *, config: Optional[WatchdogConfig] = None,
                 health_probe: Optional[Callable[[], Any]] = None,
                 container_probe: Optional[Callable[[], Any]] = None,
                 heartbeat_probe: Optional[Callable[[], Any]] = None,
                 bar_state_probe: Optional[Callable[[], Any]] = None,
                 push_probe: Optional[Callable[[], Any]] = None,
                 db_probe: Optional[Callable[[], Any]] = None,
                 market: Any = None,
                 alert: Optional[Callable[[str, str], Any]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        if store is None:
            raise WatchdogConfigError("Watchdog requires a StateStore")
        self.store = store
        self.config = config if config is not None else WatchdogConfig()
        self.clock = clock
        self.alert = alert
        self.market = market
        self._health_probe = health_probe or (
            lambda: default_health_probe(self.config.health_url, self.config.health_timeout_s))
        self._container_probe = container_probe or default_container_probe
        self._heartbeat_probe = heartbeat_probe or (lambda: default_heartbeat_probe(store))
        self._bar_state_probe = bar_state_probe
        if self._bar_state_probe is None and market is not None:
            self._bar_state_probe = BarStateProbe(market, self.config.symbols)
        self._push_probe = push_probe or (
            lambda: default_push_probe(store, window_s=self.config.push_window_s))
        self._db_probe = db_probe or (lambda: default_db_probe(store))
        #: 最近一次报告（面板/T10 状态条读它，不必再查库）。
        self.last_report: Optional[HealthReport] = None
        #: 最近一次采集/判定异常（诊断用；同时已落 events）。
        self.last_error: str = ""
        #: 时钟探针自己的异常文本（`_now_safe` 记下来，好让「时钟坏了」也能进 events）。
        self._clock_error: str = ""

    def __repr__(self) -> str:
        return (f"Watchdog(interval={float(self.config.interval_s):.0f}s, "
                f"last={self.last_report.status if self.last_report else 'never'})")

    # ---------------------------------------------------------------- 采集

    def collect(self, now_ts: Optional[float] = None) -> HealthSnapshot:
        """调全部探针 → 快照。**单个探针抛异常不影响其它探针**（记进 `probe_errors`）。"""
        now = self._now_safe(now_ts)
        if now <= 0:
            # 时钟彻底读不出来时不能「用 0 判一遍」——那会把所有年龄算成天文数字。
            detail = f" ({self._clock_error})" if self._clock_error else ""
            raise WatchdogConfigError(
                f"the clock did not return a finite epoch second{detail}")
        errors: List[Tuple[str, str]] = []

        container = self._call("container", self._container_probe, errors)
        health = self._call("health", self._health_probe, errors)
        heartbeat = self._call("heartbeat", self._heartbeat_probe, errors)
        heartbeat_ts, heartbeat_source = _unpack_heartbeat(heartbeat)
        bar_state = self._call("bar_state", self._bar_state_probe, errors) \
            if self._bar_state_probe is not None else None
        push = self._call("push", self._push_probe, errors)
        db = self._call(CHECK_DB_WRITE, self._db_probe, errors)

        push_failures = None
        if isinstance(push, bool):
            push_failures = int(push)
        elif push is not None:
            value = _finite(push)
            push_failures = None if value is None else int(value)

        db_writable, db_detail = _unpack_db(db)
        return HealthSnapshot(
            now=now,
            container=container if isinstance(container, Mapping) else None,
            health=health if isinstance(health, Mapping) else None,
            heartbeat_ts=heartbeat_ts, heartbeat_source=heartbeat_source,
            bar_state=bar_state if isinstance(bar_state, Mapping) else None,
            push_failures=push_failures, push_window_s=float(self.config.push_window_s),
            db_writable=db_writable, db_detail=db_detail,
            probe_errors=tuple(errors),
        )

    def _call(self, name: str, probe: Optional[Callable[[], Any]],
              errors: List[Tuple[str, str]]) -> Any:
        if probe is None:
            return None
        try:
            return probe()
        except Exception as exc:                              # noqa: BLE001 - 探针异常不致命
            text = f"{type(exc).__name__}: {exc}"
            errors.append((name, text))
            logger.warning("watchdog probe %r raised: %s", name, text)
            return None

    # ---------------------------------------------------------------- 一轮

    def run_once(self, now_ts: Optional[float] = None) -> HealthReport:
        """跑一轮。**永不抛异常**：失败落 `events` + 日志，并反映在返回的报告上。"""
        report: Optional[HealthReport] = None
        snapshot: Optional[HealthSnapshot] = None
        try:
            snapshot = self.collect(now_ts)
            report = check_health(
                snapshot,
                heartbeat_max_age_s=float(self.config.heartbeat_max_age_s),
                bar_stall_age_s=float(self.config.bar_stall_age_s),
                push_fail_alarm=int(self.config.push_fail_alarm),
                interval_s=float(self.config.interval_s),
            )
        except Exception as exc:                              # noqa: BLE001 - 兜底：必须留痕
            text = f"health check failed: {type(exc).__name__}: {exc}"
            self.last_error = text
            logger.exception("watchdog run_once failed")
            self._safe_event(EVENT_WATCHDOG_ERROR, text, data={"error": text})
            report = HealthReport(generated_at=self._now_safe(now_ts), checks=(), error=text,
                                  interval_s=float(self.config.interval_s))

        # 探针异常逐条留痕（这类事件不做去重：探针崩了每次都该看见）。
        if snapshot is not None:
            for name, text in snapshot.probe_errors:
                self.last_error = f"probe {name}: {text}"
                self._safe_event(EVENT_WATCHDOG_ERROR, f"probe {name} raised: {text}",
                                 data={"probe": name, "error": text})
        assert report is not None
        self.last_report = report
        self._maybe_log(report)
        if report.needs_alert:
            self._alert(report)
        return report

    def _now_safe(self, now_ts: Optional[float] = None) -> float:
        """取一个时间戳，**绝不抛**（时钟坏掉时退回 0 并留日志）。"""
        stamp = _finite(now_ts)
        if stamp is not None:
            return float(stamp)
        try:
            value = _finite(self.clock())
        except Exception as exc:                              # noqa: BLE001 - 时钟都坏了
            self._clock_error = f"{type(exc).__name__}: {exc}"
            logger.exception("watchdog clock raised")
            value = None
        return float(value) if value is not None else 0.0

    # ---------------------------------------------------------------- 副作用封装

    def _maybe_log(self, report: HealthReport) -> None:
        """只在**结论变化**时落 `watchdog_report`（否则 288 条/天的噪音会淹掉事件流）。"""
        previous = self._previous_codes()
        codes = tuple(report.codes)
        if previous is not None and previous == codes:
            return
        detail = {"status": report.status, "codes": list(codes),
                  "previous_codes": None if previous is None else list(previous),
                  "checks": [{"check": c.check, "status": c.status, "code": c.code or c.check,
                              "message": c.message} for c in report.checks
                             if c.status in (STATUS_WARN, STATUS_ERROR)]}
        self._safe_event(EVENT_REPORT, report.summary(), data=detail)

    def _previous_codes(self) -> Optional[Tuple[str, ...]]:
        try:
            rows = self.store.events(kind=EVENT_REPORT, limit=1)
        except Exception:                                     # noqa: BLE001
            return None
        if not rows:
            return None
        raw = (rows[0] or {}).get("data")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            return tuple(str(code) for code in payload.get("codes", []))
        except (ValueError, AttributeError, TypeError):
            return None

    def _alert(self, report: HealthReport) -> None:
        """推送（T10 注入 notify；默认只打日志）。推送失败**必须**留痕。"""
        if self.alert is None:
            logger.warning("[alert:watchdog] %s", report.summary())
            return
        try:
            self.alert("watchdog", report.summary())
        except Exception as exc:                              # noqa: BLE001 - 推送失败不致命
            logger.exception("watchdog alert hook failed")
            self._safe_event(ALERT_FAILED_KIND,
                             f"watchdog: {type(exc).__name__}: {exc}",
                             data={"kind": "watchdog", "text": report.summary(),
                                   "error": f"{type(exc).__name__}: {exc}"})

    def _safe_event(self, kind: str, message: str, *,
                    data: Optional[Mapping[str, Any]] = None) -> None:
        """落 events，**自己绝不抛**：DB 写不进去时只剩日志（最后一道留痕）。"""
        logger.info("[%s] %s", kind, message)
        try:
            self.store.log_event(kind, message, data=dict(data or {}))
        except Exception:                                     # noqa: BLE001 - 最后一道
            logger.exception("watchdog could not persist the %r event", kind)


# ------------------------------------------------------------------ 小工具


def _unpack_heartbeat(value: Any) -> Tuple[Optional[float], str]:
    """心跳探针的返回值归一：`float` / `(ts, source)` / `{"ts":…, "source":…}`。"""
    if value is None:
        return None, "missing"
    if isinstance(value, Mapping):
        return _finite(value.get("ts")), str(value.get("source") or "unknown")
    if isinstance(value, (tuple, list)) and len(value) >= 1:
        source = str(value[1]) if len(value) > 1 and value[1] else "unknown"
        return _finite(value[0]), source
    return _finite(value), "unknown"


def _unpack_db(value: Any) -> Tuple[Optional[bool], Dict[str, Any]]:
    """DB 探针的返回值归一：`bool` / `{"writable": bool, "error": …}`。"""
    if value is None:
        return None, {}
    if isinstance(value, bool):
        return value, {}
    if isinstance(value, Mapping):
        writable = value.get("writable", value.get("ok"))
        detail = dict(value)
        return (None if writable is None else bool(writable)), detail
    return bool(value), {}


def run_once(store: StateStore, **kwargs: Any) -> HealthReport:
    """一次性调用（T10 定时任务/测试用）::

        run_once(store, market=market, alert=notify)   # 采集 + 判定 + 落 events
    """
    now_ts = kwargs.pop("now_ts", None)
    return Watchdog(store, **kwargs).run_once(now_ts=now_ts)
