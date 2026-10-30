"""通知出口：企业微信 webhook + Telegram（纯 stdlib，线程化，全可选）。

========================================================================
为什么需要这一层
========================================================================

2026-10-30 之前执行器的 `alert` 是 `None`（`main._tick()` 传的）—— 于是 `Executor._alert()`
只会 `logger.warning`。开仓、离场、止损成交这些**人对钱负责的时刻**没有任何推送，
只有翻 `/events` 面板才能看到。这一层把执行器事件接到和 HL 侧**同一套渠道、同一套 env
键名**上，运维只需要维护一份凭据。

========================================================================
设计上的四个硬约束（都是踩过的坑）
========================================================================

1. **env 三键全部可选**：`WECHAT_WEBHOOK_URL` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`。
   缺哪个渠道就跳过哪个；三个全缺 → `build_notifier()` 返回 `None`，执行器行为与接入前
   逐字节一致（只打日志）。要静默就清空 env，**不新增任何开关项** —— 每多一个开关，
   实盘上就多一种「以为开了其实关着」的状态。
2. **纯 stdlib（urllib）**，不引入 aiohttp / requests：tick 在事件循环里跑，通知只是旁路，
   为它拉一个异步 HTTP 依赖不划算（也把这个模块的 import 图缩到最小）。
3. **每条消息一个 `threading.Thread(daemon=True)`**：tick 主路径零新增延迟。交易事件频率
   极低（一天个位数），不值得为它建线程池或队列；`daemon=True` 保证进程退出不会被一个
   卡住的 HTTP 请求拖住（进程被 SIGTERM 时不需要等通知发完）。
4. **失败只 `logger.warning`**（含异常类型 / 企微 errcode+errmsg，**不含凭据**），不抛、
   **不落 `events` 表**：`_alert` 的失败分支已经会把异常落库并计数，这里再落一条就是
   「推送失败 → 一条事件 → 又一条推送」的噪音反馈环，还可能被对账当成真实异常。

长消息按渠道上限分块 —— 但**两个渠道的口径不同**：Telegram 的上限是字符
（单条 4096 字符，留余量取 3800），企业微信 `text.content` 的上限是 **2048 字节**
（UTF-8；一个中文 3 字节）。所以企微必须按字节切：按字符数切的话 1800 个汉字约
5400 字节，超限部分被渠道**静默截断**（不报错、不重试，看起来「发成功了」）。

成功判定也必须读响应体：企业微信对无效 webhook **照样回 HTTP 200**，真实结论在
`errcode` 里（实测 93000 = invalid webhook url）。只看状态码 = 把「一条都没发出去」
记成成功。判定与重试口径与 HL 侧已验证实现一致（3 次尝试 + 退避），纯 stdlib 自写。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "HTTP_TIMEOUT_S", "SEND_ATTEMPTS", "SEND_BACKOFF", "TELEGRAM_BOT_TOKEN_ENV",
    "TELEGRAM_CHAT_ID_ENV", "TELEGRAM_MAX_CHARS", "WECHAT_MAX_BYTES", "WECHAT_WEBHOOK_ENV",
    "Notifier", "TelegramBot", "WeChatWebhook", "build_notifier",
    "notifier_from_env", "split_message", "split_message_bytes",
]

logger = logging.getLogger(__name__)

#: 与 HL 侧完全同名（凭据只维护一份）。三个都**不是**必填项。
WECHAT_WEBHOOK_ENV = "WECHAT_WEBHOOK_URL"
TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "TELEGRAM_CHAT_ID"

#: 单条上限：Telegram 按**字符**（官方 4096，留余量取 3800）。
TELEGRAM_MAX_CHARS = 3800
#: 企业微信 `text.content` 上限 2048 **字节**（UTF-8），取 1900 留余量（与 HL 同值同语义）。
#: 一份中文正文按字节切 ≈ 633 个汉字/条 —— 按字符 1800 切会超限 3 倍被静默截断。
WECHAT_MAX_BYTES = 1900

#: 失败重试：3 次尝试 + 退避（1s、3s）。口径与 HL 侧已验证实现一致。
SEND_ATTEMPTS = 3
SEND_BACKOFF = (1.0, 3.0)

#: HTTP 超时。推送慢不能拖住线程（daemon 线程卡住也不影响交易，但会积压线程）。
HTTP_TIMEOUT_S = 10.0

TELEGRAM_API_BASE = "https://api.telegram.org/bot"

#: `http(url, payload, timeout=...)` —— 可注入，测试里打桩用（不联网）。
HttpPost = Callable[..., Any]


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> List[str]:
    """按 `limit` 字符分块；优先在换行处切，避免把一行拆成两条消息。

    找不到像样的换行（或换行太靠前，切出来只剩一小截）就按硬边界切 —— 分块的目的是
    「每条都发得出去」，不是「每条都好看」。
    """
    body = "" if text is None else str(text)
    cap = max(1, int(limit))
    if len(body) <= cap:
        return [body]
    chunks: List[str] = []
    rest = body
    while len(rest) > cap:
        window = rest[:cap]
        cut = window.rfind("\n")
        if cut < cap // 2:            # 没有换行 / 换行太靠前 → 硬切
            cut = cap
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks or [body]


def _byte_cut(text: str, max_bytes: int) -> int:
    """返回**最大的** `k`，使 `text[:k]` 的 UTF-8 字节数 ≤ `max_bytes`。

    Python 的 str 切片永远落在字符边界上 —— 所以按字符下标回退就天然「不会切出半个
    多字节字符」（这正是字节切分最容易写错的地方）。用二分找 k：一遍线性扫也能做，
    但这里是 O(k log k) 且没有逐字符 encode 的常数爆炸。
    """
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(text[:mid].encode("utf-8")) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    # 单个字符就超过上限时（只可能出现在病态的小上限上）至少前进一个字符，
    # 否则循环不收敛；代价是那一条会略超上限，但绝不会死循环。
    return max(1, lo)


def split_message_bytes(text: str, max_bytes: int = WECHAT_MAX_BYTES) -> List[str]:
    """按 **UTF-8 字节数**分块（企业微信用）：每块的 `encode('utf-8')` 长度 ≤ `max_bytes`。

    为什么不用 `split_message(字符)`：企微 `text.content` 的硬限是 2048 **字节**。
    1800 个汉字 = 5400 字节，超限部分被渠道静默截断 —— 一条「止损成交」可能只剩前半句，
    而且发送侧看起来完全成功。
    """
    body = "" if text is None else str(text)
    cap = max(1, int(max_bytes))
    if len(body.encode("utf-8")) <= cap:
        return [body]
    chunks: List[str] = []
    rest = body
    while len(rest.encode("utf-8")) > cap:
        cut = _byte_cut(rest, cap)
        newline = rest.rfind("\n", 0, cut)
        if newline >= cut // 2:       # 换行在靠后位置 → 顺着换行切（少切半行）
            cut = newline
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks or [body]


def _post_json(url: str, payload: Mapping[str, Any],
               timeout: float = HTTP_TIMEOUT_S) -> Tuple[bool, str]:
    """POST 一个 JSON 体 → `(是否 2xx, 响应体文本)`；**任何**失败都折成 `(False, 原因)`，不抛。

    为什么必须读响应体：企业微信对无效 webhook 照样回 HTTP 200，真实结论在 `errcode`
    （实测 93000 = invalid webhook url，零日志零痕迹）。`HTTPError` 的错误页也读出来
    —— 网关 502 页面对排障有用。响应体无论用不用都要读干净（连接才回收）。
    """
    data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        str(url), data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:  # noqa: S310
            raw = response.read() or b""
            return True, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read() or b""
        except Exception:                                     # noqa: BLE001 - 错误页读不到也要有原因
            raw = b""
        return False, "HTTP %s %s" % (getattr(exc, "code", "?"),
                                      raw.decode("utf-8", "replace")[:200])
    except Exception as exc:                                  # noqa: BLE001 - 网络层一律降级为「这次没成」
        return False, "%s: %s" % (type(exc).__name__, exc)


class _HttpChannel:
    """两个 HTTP 渠道的共同骨架：分块 → 逐块发 → **读响应判定** → 重试。

    为什么抽成一个骨架：渠道之间只有三处不同（怎么切、payload 形状、响应怎么算成功），
    而「重试 / 退避 / 失败留痕」三件事最容易写错且**必须**一致，所以只写一遍。
    所有失败都收敛成一条 `logger.warning`（含渠道名与 errcode/errmsg，不含凭据），
    `send()` 绝不抛 —— 推送是旁路，tick 主路径不能因为它失败。
    """

    name = ""
    #: 目标 URL（子类提供：企微是 webhook 地址，TG 由 token 拼出来）。
    url: str = ""
    #: 实例级可覆盖（测试把退避调成 0，避免用例里真的等 4 秒）。
    attempts: int = SEND_ATTEMPTS
    backoff: Sequence[float] = SEND_BACKOFF

    def __init__(self, *, http: Optional[HttpPost] = None,
                 timeout: float = HTTP_TIMEOUT_S,
                 attempts: int = SEND_ATTEMPTS,
                 backoff: Sequence[float] = SEND_BACKOFF) -> None:
        self.timeout = float(timeout)
        self._http: HttpPost = http or _post_json
        self.attempts = max(1, int(attempts))
        self.backoff = tuple(float(x) for x in backoff)

    # -- 子类实现 ----------------------------------------------------------
    def chunks(self, text: str) -> List[str]:
        raise NotImplementedError

    def payload(self, chunk: str) -> dict:
        raise NotImplementedError

    def _accept(self, body: str) -> str:
        """成功 → `""`；失败 → 原因（不含凭据）。"""
        raise NotImplementedError

    def _scrub(self, reason: str) -> str:
        """把原因里可能出现的凭据抹掉（子类按自己的凭据位置覆盖）。"""
        return reason

    # -- 发送 --------------------------------------------------------------
    def send(self, text: str) -> bool:
        """逐块发送，返回是否**全部**送达。

        某一块最终失败就停：后面的块同渠道、同凭据，多半同样发不出去，继续打只是
        多浪费 6 秒 × N。调用方（执行器）不看返回值 —— 失败只留痕。
        """
        for chunk in self.chunks(text):
            if not chunk.strip():
                continue
            if not self._send_chunk(chunk):
                return False
        return True

    def _send_chunk(self, chunk: str) -> bool:
        reason = ""
        for attempt in range(1, self.attempts + 1):
            try:
                ok, body = self._http(self.url, self.payload(chunk), self.timeout)
            except Exception as exc:                          # noqa: BLE001 - 网络异常也算一次尝试
                ok, body = False, "%s: %s" % (type(exc).__name__, exc)
            if ok:
                reason = self._accept(str(body))
                if not reason:
                    return True
            else:
                reason = str(body)
            if attempt < self.attempts and self.backoff:
                time.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
        logger.warning("notify: %s send failed after %d attempts: %s",
                       self.name, self.attempts, self._scrub(reason)[:200])
        return False


class WeChatWebhook(_HttpChannel):
    """企业微信群机器人 webhook（`POST {"msgtype":"text","text":{"content":...}}`）。"""

    name = "wechat"
    #: 正文上限按**字节**（不是字符），见 `split_message_bytes`。
    max_bytes = WECHAT_MAX_BYTES

    def __init__(self, url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.url = str(url or "").strip()

    def __repr__(self) -> str:
        # webhook URL 里的 `key=` 就是凭据 —— 进了日志等于泄漏，只留渠道名。
        return "WeChatWebhook(configured=%s)" % ("yes" if self.url else "no")

    def chunks(self, text: str) -> List[str]:
        return split_message_bytes(text, self.max_bytes)

    def payload(self, chunk: str) -> dict:
        return {"msgtype": "text", "text": {"content": str(chunk)}}

    def _accept(self, body: str) -> str:
        """`errcode == 0` 才算发出去了；非 JSON 响应一律算失败（见下）。"""
        try:
            data = json.loads(body) if body.strip() else {}
        except Exception:                                     # noqa: BLE001 - 代理错误页不是 JSON
            # 企微正常响应必是 JSON。网关/代理的错误页（HTML、502 文本）以前被当成
            # 「成功」，重试机会就此丢掉 —— 这里当成失败，交给重试 + 留痕。
            return "non-JSON response: %s" % (body or "")[:120]
        if not isinstance(data, dict):
            return "unexpected JSON payload: %s" % (body or "")[:120]
        try:
            code = int(data.get("errcode", 0) or 0)
        except (TypeError, ValueError):
            return "bad errcode field: %s" % (body or "")[:120]
        if code == 0:
            return ""
        return "errcode=%s errmsg=%s" % (code, data.get("errmsg", ""))

    def _scrub(self, reason: str) -> str:
        # 响应体可能回显整个 URL（`key=` 是凭据）—— 日志前抹掉。
        key = self.url.rsplit("key=", 1)[-1] if "key=" in self.url else ""
        return reason.replace(key, "***") if key else reason


class TelegramBot(_HttpChannel):
    """Telegram Bot API `sendMessage`（bot token + chat_id）。"""

    name = "telegram"

    def __init__(self, token: str, chat_id: str, *, api_base: str = TELEGRAM_API_BASE,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.token = str(token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.api_base = str(api_base)

    def __repr__(self) -> str:
        # token 是完整凭据（能读能发）—— 任何 repr/日志里都不出现。
        return "TelegramBot(chat_id=%r, configured=%s)" % (
            self.chat_id, "yes" if self.token else "no")

    @property
    def url(self) -> str:
        return f"{self.api_base}{self.token}/sendMessage"

    def chunks(self, text: str) -> List[str]:
        # 仍按字符：Telegram 的上限是 4096 字符，不是字节。
        return split_message(text, TELEGRAM_MAX_CHARS)

    def payload(self, chunk: str) -> dict:
        return {"chat_id": self.chat_id, "text": str(chunk),
                "disable_web_page_preview": True}

    def _accept(self, body: str) -> str:
        try:
            data = json.loads(body) if body.strip() else {}
        except Exception:                                     # noqa: BLE001
            return "non-JSON response: %s" % (body or "")[:120]
        if isinstance(data, dict) and data.get("ok"):
            return ""
        return "ok=false: %s" % (body or "")[:160]

    def _scrub(self, reason: str) -> str:
        return reason.replace(self.token, "***") if self.token else reason


class Notifier:
    """把一条消息投给所有已配置渠道。

    对外只需要一个签名：`__call__(kind, text)` —— 与 `Executor.alert` 注入的
    `Callable[[str, str], Any]` 完全一致。`kind` 只进日志/线程名，不进正文
    （正文由执行器组装，措辞与事件表对齐）。
    """

    def __init__(self, senders: Sequence[Any] = ()) -> None:
        self.senders: tuple = tuple(s for s in senders if s is not None)

    def __repr__(self) -> str:
        return "Notifier(channels=%s)" % (",".join(str(s.name) for s in self.senders) or "none")

    def __bool__(self) -> bool:
        return bool(self.senders)

    def __call__(self, kind: str, text: str) -> None:
        """**立即返回**：真正的 HTTP 在 daemon 线程里跑（tick 主路径不能等它）。"""
        message = "" if text is None else str(text)
        if not message.strip() or not self.senders:
            return
        threading.Thread(target=self.deliver, args=(message,),
                         name=f"notify:{kind or 'alert'}", daemon=True).start()

    def deliver(self, message: str) -> None:
        """同步投递（测试/脚本直接调用）。单个渠道失败不影响其它渠道，且绝不抛出。

        分块交给每个渠道自己（`send()` 里的 `chunks()`）—— 企微按字节、TG 按字符，
        混在一个函数里迟早会用错口径。
        """
        for sender in self.senders:
            try:
                sender.send(message)
            except Exception as exc:                          # noqa: BLE001 - 渠道实现有 bug 也不能炸 tick
                logger.warning("notify: %s send raised (%s): %s",
                               getattr(sender, "name", "?"), type(exc).__name__, exc)


def build_notifier(*, wechat_webhook_url: str = "", telegram_bot_token: str = "",
                   telegram_chat_id: str = "",
                   http: Optional[HttpPost] = None) -> Optional[Notifier]:
    """按「哪个渠道配全了就装哪个」装配通知器；一个都没配全 → `None`（no-op）。

    `http` 只为测试注入；生产走 `_post_json`。
    """
    senders: List[Any] = []
    url = str(wechat_webhook_url or "").strip()
    if url:
        senders.append(WeChatWebhook(url, http=http))
    token = str(telegram_bot_token or "").strip()
    chat_id = str(telegram_chat_id or "").strip()
    if token and chat_id:
        senders.append(TelegramBot(token, chat_id, http=http))
    elif token or chat_id:
        # 半个 Telegram 配置发不出去 —— 跳过渠道但要留痕（静默跳过 = 以为是配好的）。
        logger.warning("notify: telegram channel skipped — %s and %s must both be set",
                       TELEGRAM_BOT_TOKEN_ENV, TELEGRAM_CHAT_ID_ENV)
    return Notifier(senders) if senders else None


def _env(source: Mapping[str, str], key: str) -> str:
    return str(source.get(key) or "").strip()


def notifier_from_env(env: Optional[Mapping[str, str]] = None, *,
                      http: Optional[HttpPost] = None) -> Optional[Notifier]:
    """从环境变量装配（键名与 HL 侧一致）。三键全缺 → `None`。"""
    source: Mapping[str, str] = os.environ if env is None else env
    return build_notifier(
        wechat_webhook_url=_env(source, WECHAT_WEBHOOK_ENV),
        telegram_bot_token=_env(source, TELEGRAM_BOT_TOKEN_ENV),
        telegram_chat_id=_env(source, TELEGRAM_CHAT_ID_ENV),
        http=http)
