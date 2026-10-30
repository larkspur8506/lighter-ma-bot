"""`app/notify.py` 的契约测试 + 执行器三处成功路径的推送 —— **全离线**。

钉住的东西：

1. **三 env 全缺 = no-op**：`notifier_from_env({})` 返回 `None`（执行器 `_alert` 退回
   只打日志 —— 与接入前逐字节一致）。空白值也算「没配」。
2. **只配 Telegram** → 打桩 HTTP 收到 `sendMessage`：URL 里带 bot token、payload 里带
   `chat_id` 与正文。只配企业微信 → payload 是 `{"msgtype":"text",...}`。
3. **半个 Telegram 配置**（只有 token 或只有 chat_id）→ 渠道跳过，整体仍是 no-op。
4. **长消息分块**：每块不超渠道上限、顺序不变、拼起来等于原文（不丢字）。
5. **线程化**：发送慢（600ms）时 `__call__` 立即返回，消息仍然送达（tick 主路径不能等）。
6. **失败只 warning**：HTTP 抛异常 → 不抛给调用方、留一条含异常类型的 warning。
7. **执行器三处成功路径各一条【Lighter】告警**：开仓 / 离场 / 离线止损结案，
   复用 `tests.test_executor` 的 Fake 桩（同一条实盘路径，不另造一套）。

所有断言都是「交易所会收到什么」这种可核对的事实，不做自证式断言。
"""

from __future__ import annotations

import logging
import threading
import time
import unittest

import app.executor as executor
from app import notify
from app.notify import (TELEGRAM_MAX_CHARS, WECHAT_MAX_CHARS, Notifier, TelegramBot,
                        WeChatWebhook, notifier_from_env, split_message)
from app.lifecycle import ACTION_ENTRY
from tests.test_executor import (BTC, T0, Base as ExecutorCase, bars_result, quiet_bars,
                                 stop_hit_bars)

TG_TOKEN = "123456:AAbbCCddEEffGG"
TG_CHAT = "-1001234567890"
WECHAT_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=11111111-2222"


class StubHttp:
    """`http(url, payload, timeout=...)` 的打桩：记录调用、可注入延迟/异常。"""

    def __init__(self, *, delay: float = 0.0, error: str = "") -> None:
        self.calls: list = []
        self.delay = float(delay)
        self.error = str(error)

    def __call__(self, url, payload, timeout=None):
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise RuntimeError(self.error)
        self.calls.append((str(url), dict(payload)))
        return 200


# --------------------------------------------------------------------------- 装配


class BuildNotifierTests(unittest.TestCase):
    def test_no_env_at_all_is_a_noop(self):
        """三个全缺 → None。执行器拿到的 alert 仍是 None（只打日志）。"""
        self.assertIsNone(notifier_from_env({}))

    def test_blank_values_count_as_unset(self):
        self.assertIsNone(notifier_from_env({"WECHAT_WEBHOOK_URL": "   ",
                                             "TELEGRAM_BOT_TOKEN": "",
                                             "TELEGRAM_CHAT_ID": " "}))

    def test_telegram_only_builds_one_channel(self):
        http = StubHttp()
        notifier = notifier_from_env({"TELEGRAM_BOT_TOKEN": TG_TOKEN,
                                      "TELEGRAM_CHAT_ID": TG_CHAT}, http=http)
        self.assertIsNotNone(notifier)
        self.assertEqual([s.name for s in notifier.senders], ["telegram"])

    def test_wechat_only_builds_one_channel(self):
        http = StubHttp()
        notifier = notifier_from_env({"WECHAT_WEBHOOK_URL": WECHAT_URL}, http=http)
        self.assertIsNotNone(notifier)
        self.assertEqual([s.name for s in notifier.senders], ["wechat"])

    def test_both_channels_build_in_order(self):
        http = StubHttp()
        notifier = notifier_from_env({"WECHAT_WEBHOOK_URL": WECHAT_URL,
                                      "TELEGRAM_BOT_TOKEN": TG_TOKEN,
                                      "TELEGRAM_CHAT_ID": TG_CHAT}, http=http)
        self.assertEqual([s.name for s in notifier.senders], ["wechat", "telegram"])

    def test_half_a_telegram_config_is_skipped_with_a_warning(self):
        """只有 token 没有 chat_id：发不出去 → 跳过渠道，且**必须**留痕。"""
        with self.assertLogs("app.notify", level=logging.WARNING) as captured:
            notifier = notifier_from_env({"TELEGRAM_BOT_TOKEN": TG_TOKEN})
        self.assertIsNone(notifier)
        self.assertIn("telegram", "\n".join(captured.output))

    def test_repr_never_leaks_the_token_or_the_webhook_key(self):
        bot = TelegramBot(TG_TOKEN, TG_CHAT)
        hook = WeChatWebhook(WECHAT_URL)
        self.assertNotIn(TG_TOKEN, repr(bot))
        self.assertNotIn("11111111-2222", repr(hook))


# --------------------------------------------------------------------------- 发送


class SendTests(unittest.TestCase):
    def test_telegram_payload_and_url(self):
        http = StubHttp()
        notifier = notifier_from_env({"TELEGRAM_BOT_TOKEN": TG_TOKEN,
                                      "TELEGRAM_CHAT_ID": TG_CHAT}, http=http)
        notifier.deliver("【Lighter】开仓 BTC 多 1 @ 100 ｜ 止损 95 ｜ 风险 5.00U")

        self.assertEqual(len(http.calls), 1)
        url, payload = http.calls[0]
        self.assertEqual(url, f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage")
        self.assertEqual(payload["chat_id"], TG_CHAT)
        self.assertIn("【Lighter】开仓", payload["text"])

    def test_wechat_payload_is_a_text_message(self):
        http = StubHttp()
        notifier = notifier_from_env({"WECHAT_WEBHOOK_URL": WECHAT_URL}, http=http)
        notifier.deliver("【Lighter】离场 BTC stop @ 95 ｜ pnl -12.50U")

        self.assertEqual(len(http.calls), 1)
        url, payload = http.calls[0]
        self.assertEqual(url, WECHAT_URL)
        self.assertEqual(payload, {"msgtype": "text",
                                   "text": {"content":
                                            "【Lighter】离场 BTC stop @ 95 ｜ pnl -12.50U"}})

    def test_both_channels_get_the_same_message(self):
        http = StubHttp()
        notifier = notifier_from_env({"WECHAT_WEBHOOK_URL": WECHAT_URL,
                                      "TELEGRAM_BOT_TOKEN": TG_TOKEN,
                                      "TELEGRAM_CHAT_ID": TG_CHAT}, http=http)
        notifier.deliver("ping")
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[0][1]["text"]["content"], "ping")
        self.assertEqual(http.calls[1][1]["text"], "ping")

    def test_a_failing_channel_does_not_stop_the_other_and_never_raises(self):
        """企业微信炸了，Telegram 仍然要发出去；异常只 warning（fail-open）。"""
        calls = []

        def http(url, payload, timeout=None):
            if "weixin" in url:
                raise OSError("connection reset")
            calls.append((url, dict(payload)))
            return 200

        notifier = notifier_from_env({"WECHAT_WEBHOOK_URL": WECHAT_URL,
                                      "TELEGRAM_BOT_TOKEN": TG_TOKEN,
                                      "TELEGRAM_CHAT_ID": TG_CHAT}, http=http)
        with self.assertLogs("app.notify", level=logging.WARNING) as captured:
            notifier.deliver("ping")                     # 不抛
        self.assertEqual(len(calls), 1)                  # Telegram 照常送达
        joined = "\n".join(captured.output)
        self.assertIn("OSError", joined)                 # 留痕要带异常类型
        self.assertIn("connection reset", joined)

    def test_empty_message_sends_nothing(self):
        http = StubHttp()
        notifier = notifier_from_env({"TELEGRAM_BOT_TOKEN": TG_TOKEN,
                                      "TELEGRAM_CHAT_ID": TG_CHAT}, http=http)
        notifier("entry", "   ")
        notifier("entry", "")
        time.sleep(0.05)
        self.assertEqual(http.calls, [])


# --------------------------------------------------------------------------- 分块


class SplitMessageTests(unittest.TestCase):
    def test_short_message_is_one_chunk(self):
        self.assertEqual(split_message("hi", 10), ["hi"])

    def test_long_message_is_chunked_without_losing_text(self):
        body = "\n".join(f"第 {i} 行：这是一条测试消息" for i in range(600))
        chunks = split_message(body, TELEGRAM_MAX_CHARS)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), TELEGRAM_MAX_CHARS)
        self.assertEqual("".join(c.replace("\n", "") for c in chunks),
                         body.replace("\n", ""))

    def test_a_single_huge_line_is_hard_split(self):
        body = "x" * (TELEGRAM_MAX_CHARS * 2 + 7)
        chunks = split_message(body, TELEGRAM_MAX_CHARS)
        self.assertEqual([len(c) for c in chunks], [TELEGRAM_MAX_CHARS, TELEGRAM_MAX_CHARS, 7])

    def test_a_long_message_reaches_the_channel_in_several_calls(self):
        http = StubHttp()
        notifier = notifier_from_env({"WECHAT_WEBHOOK_URL": WECHAT_URL}, http=http)
        body = "y" * (WECHAT_MAX_CHARS * 2 + 10)
        notifier.deliver(body)
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(sum(len(c[1]["text"]["content"]) for c in http.calls), len(body))


# --------------------------------------------------------------------------- 线程化


class ThreadingTests(unittest.TestCase):
    def test_call_returns_immediately_while_the_send_is_still_running(self):
        """发送慢 600ms：`__call__`（执行器 `alert` 的签名）必须立刻返回。"""
        http = StubHttp(delay=0.6)
        notifier = notifier_from_env({"TELEGRAM_BOT_TOKEN": TG_TOKEN,
                                      "TELEGRAM_CHAT_ID": TG_CHAT}, http=http)

        started = time.monotonic()
        notifier("entry", "【Lighter】开仓 BTC 多 1 @ 100")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.2, f"alert 阻塞了 {elapsed:.3f}s（tick 主路径不许等通知）")
        self.assertEqual(http.calls, [], "还没发完就该返回了")

        deadline = time.monotonic() + 3.0
        while not http.calls and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(http.calls), 1, "线程里最终必须发出这一条")

    def test_the_sender_runs_on_a_daemon_thread(self):
        """daemon=True：进程退出不会被卡住的通知线程拖住。"""
        seen = []
        started = time.monotonic()
        http = StubHttp(delay=0.05)

        def recording(url, payload, timeout=None):
            seen.append(threading.current_thread().daemon)
            return http(url, payload, timeout)

        notifier = notifier_from_env({"TELEGRAM_BOT_TOKEN": TG_TOKEN,
                                      "TELEGRAM_CHAT_ID": TG_CHAT}, http=recording)
        notifier("exit", "ping")
        deadline = time.monotonic() + 3.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(seen, [True])
        self.assertGreaterEqual(time.monotonic() - started, 0.0)

    def test_a_notifier_without_channels_is_falsy_and_silent(self):
        notifier = Notifier(())
        self.assertFalse(notifier)
        notifier("entry", "ping")                        # 不抛、不建线程


# --------------------------------------------------------------------------- 执行器


class ExecutorNotifyTests(ExecutorCase):
    """三处成功路径各产出一条告警（复用 test_executor 的 Fake 桩）。"""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.executor = self.make_executor(alert=self._record)

    def _record(self, kind, text):
        self.sent.append((str(kind), str(text)))

    def _texts(self, kind):
        return [text for k, text in self.sent if k == kind]

    def test_entry_success_pushes_one_alert(self):
        report = self.run_tick()
        self.assertEqual(report.outcomes[0].action, ACTION_ENTRY)
        texts = self._texts("entry")
        self.assertEqual(len(texts), 1, self.sent)
        text = texts[0]
        self.assertIn("【Lighter】", text)
        self.assertIn("开仓", text)
        self.assertIn(BTC, text)
        self.assertIn("多", text)                    # 信号是做多
        self.assertIn("止损", text)
        self.assertIn("风险", text)
        self.assertTrue(text.endswith("U"), text)

    def test_failed_entry_paths_do_not_emit_a_success_alert(self):
        """止损单挂失败 → 只允许 `stop_order_failed`（那是裸仓，比「开仓成功」更要紧）。"""
        # 让 place_stop 被拒（交易所返回 ok=False）——这条路径更该看，不该被「开仓成功」冲淡
        from app.exchange import OrderResult
        self.exchange.reject["stop"] = OrderResult(
            ok=False, action="stop", code=400, kind="rejected", error="stop rejected")
        self.run_tick()
        self.assertEqual(self._texts("entry"), [])
        self.assertTrue(any(k == "stop_order_failed" for k, _ in self.sent), self.sent)

    def _stop_hit(self):
        """涨破止损 → 走 `_execute_exit(kind='stop')` 的成功路径。"""
        self.market.feeds[BTC] = bars_result(stop_hit_bars(), unclosed=())
        row = self.seed_position(entry_price_int=1120, stop_price_int=1050,
                                 bar_ms=T0, qty_int=415600)
        self.exchange.positions[BTC] = {"side": 1, "qty_int": 415600, "market_id": 1}
        self.exchange.orders = []
        return row, self.make_executor(fill_confirm_s=0, alert=self._record)

    def test_exit_success_pushes_one_alert(self):
        row, ex = self._stop_hit()
        self.run_tick(ex)
        self.assertEqual(self.store.position(int(row["id"]))["state"], "CLOSED")

        texts = self._texts("stop")
        self.assertEqual(len(texts), 1, self.sent)
        text = texts[0]
        self.assertIn("【Lighter】", text)
        self.assertIn("离场", text)
        self.assertIn(BTC, text)
        self.assertIn("105", text)                   # 止损价 105.0（交易所口径整数还原）
        self.assertIn("pnl", text)

    def test_offline_stop_fill_settlement_pushes_one_alert(self):
        """`_settle_grace_issues` 的 settled 分支：真实成交在交易所发生，本地事后认账。"""
        row = self.seed_position(stop_order_id="coi:999")
        self.exchange.positions.pop(BTC, None)
        self.exchange.orders = []
        self.market.feeds[BTC] = bars_result(quiet_bars(), unclosed=())
        self.reader._trades = (executor.ExchangeTrade(
            trade_id="37135831346", symbol=BTC, side=-1, qty=float(row["qty"]),
            qty_str=str(row["qty"]), price=float(row["stop_price"]),
            ts=float(row["opened_at"]) + 5.0, pnl=-12.5),)
        ex = self.make_executor(fill_confirm_s=0, grace_ticks=2, alert=self._record)
        self.run_tick(ex)

        self.assertEqual(self.store.position(int(row["id"]))["state"], "CLOSED")
        texts = self._texts("stop_fill_settled")
        self.assertEqual(len(texts), 1, self.sent)
        text = texts[0]
        self.assertIn("【Lighter】", text)
        self.assertIn("止损成交（离线结案）", text)
        self.assertIn(BTC, text)
        self.assertIn("-12.50U", text)               # pnl 来自成交回报，不是本地价差

    def test_a_broken_alert_callback_never_breaks_the_trade(self):
        """fail-open：推送回调抛异常时，开仓照样记账（DB 里必须有仓位）。"""
        def boom(kind, text):
            raise RuntimeError("notify exploded")

        ex = self.make_executor(alert=boom)
        # 执行器 `_alert` 的失败分支会 `logger.exception` —— 这里显式吃掉那段噪音
        with self.assertLogs("app.executor", level="ERROR"):
            report = self.run_tick(ex)
        self.assertEqual(report.outcomes[0].action, ACTION_ENTRY)
        self.assertTrue(report.outcomes[0].filled)
        self.assertEqual(len(self.store.open_positions()), 1)
        self.assertIn("alert_failed", [e["kind"] for e in self.events()])


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
