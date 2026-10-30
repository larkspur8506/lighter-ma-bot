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
4. **失败只 `logger.warning`**（含异常类型），不抛、**不落 `events` 表**：`_alert` 的失败
   分支已经会把异常落库并计数，这里再落一条就是「推送失败 → 一条事件 → 又一条推送」
   的噪音反馈环，还可能被对账当成真实异常。

长消息按渠道上限分块（Telegram 单条上限约 4096 字符，留余量取 3800；企业微信 text
消息正文上限 2048 **字节**，中文按 UTF-8 三字节算，取 1800 字符留足余量）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from typing import Any, Callable, List, Mapping, Optional, Sequence

__all__ = [
    "HTTP_TIMEOUT_S", "TELEGRAM_BOT_TOKEN_ENV", "TELEGRAM_CHAT_ID_ENV",
    "TELEGRAM_MAX_CHARS", "WECHAT_MAX_CHARS", "WECHAT_WEBHOOK_ENV",
    "Notifier", "TelegramBot", "WeChatWebhook", "build_notifier",
    "notifier_from_env", "split_message",
]

logger = logging.getLogger(__name__)

#: 与 HL 侧完全同名（凭据只维护一份）。三个都**不是**必填项。
WECHAT_WEBHOOK_ENV = "WECHAT_WEBHOOK_URL"
TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "TELEGRAM_CHAT_ID"

#: 单条上限（字符）。Telegram 官方 4096，留余量；企业微信 text 正文 2048 字节，
#: 中文 3 字节/字 ⇒ 取 1800 字符既能容纳全中文，也不会被渠道截断。
TELEGRAM_MAX_CHARS = 3800
WECHAT_MAX_CHARS = 1800

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


def _post_json(url: str, payload: Mapping[str, Any], timeout: float = HTTP_TIMEOUT_S) -> int:
    """POST 一个 JSON 体，返回 HTTP 状态码；非 2xx 由 urllib 抛 `HTTPError`。

    不读响应体（两个渠道的响应对我们都没有信息量），但**必须**把响应读干净并关闭，
    否则连接不会回收（stdlib 的 `urlopen` 返回的是需要显式 close 的对象）。
    """
    data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        str(url), data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=float(timeout)) as response:  # noqa: S310
        return int(getattr(response, "status", 0) or 0)


class WeChatWebhook:
    """企业微信群机器人 webhook（`POST {"msgtype":"text","text":{"content":...}}`）。"""

    name = "wechat"
    limit = WECHAT_MAX_CHARS

    def __init__(self, url: str, *, http: Optional[HttpPost] = None,
                 timeout: float = HTTP_TIMEOUT_S) -> None:
        self.url = str(url or "").strip()
        self.timeout = float(timeout)
        self._http: HttpPost = http or _post_json

    def __repr__(self) -> str:
        # webhook URL 里的 `key=` 就是凭据 —— 进了日志等于泄漏，只留渠道名。
        return "WeChatWebhook(configured=%s)" % ("yes" if self.url else "no")

    def payload(self, text: str) -> dict:
        return {"msgtype": "text", "text": {"content": str(text)}}

    def send(self, text: str) -> None:
        self._http(self.url, self.payload(text), self.timeout)


class TelegramBot:
    """Telegram Bot API `sendMessage`（bot token + chat_id）。"""

    name = "telegram"
    limit = TELEGRAM_MAX_CHARS

    def __init__(self, token: str, chat_id: str, *, http: Optional[HttpPost] = None,
                 timeout: float = HTTP_TIMEOUT_S,
                 api_base: str = TELEGRAM_API_BASE) -> None:
        self.token = str(token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.timeout = float(timeout)
        self.api_base = str(api_base)
        self._http: HttpPost = http or _post_json

    def __repr__(self) -> str:
        # token 是完整凭据（能读能发）—— 任何 repr/日志里都不出现。
        return "TelegramBot(chat_id=%r, configured=%s)" % (
            self.chat_id, "yes" if self.token else "no")

    @property
    def url(self) -> str:
        return f"{self.api_base}{self.token}/sendMessage"

    def payload(self, text: str) -> dict:
        return {"chat_id": self.chat_id, "text": str(text),
                "disable_web_page_preview": True}

    def send(self, text: str) -> None:
        self._http(self.url, self.payload(text), self.timeout)


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
        """同步投递（测试/脚本直接调用）。单个渠道失败不影响其它渠道，且绝不抛出。"""
        for sender in self.senders:
            for chunk in split_message(message, int(getattr(sender, "limit",
                                                             TELEGRAM_MAX_CHARS))):
                if not chunk.strip():
                    continue
                try:
                    sender.send(chunk)
                except Exception as exc:                      # noqa: BLE001 - 推送失败不致命
                    logger.warning("notify: %s send failed (%s): %s",
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
