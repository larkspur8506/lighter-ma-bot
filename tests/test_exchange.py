"""`app/exchange.py` 的契约测试 —— **全离线**（除了 `RealSignerOfflineTests` 里对
「真实 SDK 的纯签名」调用，那条路**不发任何网络请求**，测试会断言 transport 未被调用）。

钉住的东西（T7 验收项，逐条对应）：

1. **默认不下单**：没有 `LIGHTER_LIVE_TRADING=1` + `LIGHTER_LIVE_ACCOUNT_INDEX` +
   `confirm_live` 三道开关，`place_entry`/`place_stop`/`cancel_all` 一律抛
   `LiveTradingDisabled`，且替身 transport 一次都没被调用。
2. **dry-run 不发请求、不签名**：`sign_create_order` 与 `send_tx` 的调用计数都是 0。
3. **重试复用同一 nonce**：三次尝试的 `tx_info` 逐字节相同（因此 `Nonce` 相同）、
   `sign_create_order` 只被调一次。
4. **可重试 vs 不可重试**：`21506` 重试、`21706` 不重试、`21104` 走 nonce 冲突、
   `21108` 归成「API key 未注册」。
5. **整数 tick 映射**：`OrderPlan` → Lighter 订单的每个整数都写死（旁边给算式）。
6. **只签名不提交**：真实 SDK 签出来的 `tx_info` 结构/Sig 长度/字段值全部断言。
7. **auth token 缓存与刷新**：只铸一次、快过期前才重铸。
8. **私钥不泄漏**：异常消息、`as_dict()`、`repr()` 里都没有 API 私钥。
9. **只有 `app/exchange.py` 碰 signer / 下单 API**（静态扫描）。
10. **`scripts/offline_sign.py` 不接受命令行私钥**（argv 里有私钥就拒绝运行）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import pathlib
import re
import types
import unittest
from collections import deque
from unittest import mock

import app.exchange as exchange
from app.exchange import (
    AUTH_TOKEN_DEFAULT_TTL_S, CODE_OK, DEFAULT_ENTRY_SLIPPAGE_BPS,
    STOP_ORDER_EXPIRY_MS, AmbiguousOrder, AuthTokenCache, ExchangeClient, ExchangeError,
    LiveGate, LiveTradingDisabled, LiveTradingDisabled as _Disabled,  # noqa: F401
    NonceConflict, NonceStream, NotConfigured, OrderRejected, OrderResult, OrderSpec,
    SdkBoundary, build_entry_order, build_exit_order, build_stop_order, classify_error,
    client_order_index, idempotency_key_for, price_int_from_float, worst_price_int,
)
from app.sizing import OrderPlan

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW_MS = 1_790_000_000_000            # 固定「现在」（2026-09-21 左右，任意值，只要固定）
NOW_S = NOW_MS / 1000.0

API_PK = "0x" + "ab" * 40              # 40 字节 API 私钥（0x + 80 hex）——假值，仅用于测试
L1_ADDRESS = "0x" + "cd" * 20


# --------------------------------------------------------------------------- 夹具


def make_config(**over):
    """真实 `Config` 对象（走 `from_env`），secret 用假值。"""
    from app.config import Config
    env = {
        "LIGHTER_ACCOUNT_INDEX": "12345",
        "LIGHTER_API_KEY_INDEX": "4",
        "LIGHTER_API_PRIVATE_KEY": API_PK,
        "LIGHTER_L1_ADDRESS": L1_ADDRESS,
        "ADMIN_TOKEN": "admin-token-for-tests",
    }
    env.update(over)
    return Config.from_env(env)


def make_plan(**over) -> OrderPlan:
    """BTC 多单：entry 65000.0 @1dp → 650000 ticks；stop 64000.0 → 640000 ticks。

    数量 4156 = 0.04156 BTC @5dp（sizing 已经 floor 过的值，这里只当输入用）。
    """
    base = dict(
        symbol="BTC", market_id=1, side=1, entry_price=65000.0, stop_price=64000.0,
        stop_price_int=640000, qty_int=4156, base_qty=0.04156, notional=2701.4,
        margin=540.28, leverage=5.0, exchange_max_leverage=50.0,
        risk_amount=41.56, risk_amount_target=41.6, stop_dist=0.015384615384615385,
        price_decimals=1, size_decimals=5, equity=3128.0,
    )
    base.update(over)
    return OrderPlan(**base)


class Clock:
    """虚拟时钟：`sleep()` 推进时间，测试不用真等。"""

    def __init__(self, start: float = NOW_S) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


class Resp:
    """`RespSendTx` / `ResultCode` 的最小替身（只带 exchange 会读的字段）。"""

    def __init__(self, code: int = CODE_OK, message=None, tx_hash: str = "h" * 80) -> None:
        self.code = code
        self.message = message
        self.tx_hash = tx_hash
        self.predicted_execution_time_ms = 300
        self.volume_quota_remaining = 1_000_000


class ApiError(Exception):
    """`lighter.exceptions.ApiException` 的替身：status + JSON body。"""

    def __init__(self, status: int, code: int, message: str) -> None:
        self.status = status
        self.body = json.dumps({"code": code, "message": message})
        self.data = None
        super().__init__(f"({status}) body={self.body}")


class FakeSdk(SdkBoundary):
    """记录所有调用、按脚本返回的 SDK 替身。**绝不联网。**"""

    def __init__(self, *, account_index: int = 12345, api_key_index: int = 4,
                 nonce: int = 100, send_results=None, clock: Clock | None = None) -> None:
        self.account_index = account_index
        self.api_key_index = api_key_index
        self.chain_id = 304
        self.nonce = nonce
        self.send_results = deque(send_results or [])
        self.signed: list[dict] = []
        self.cancel_signed: list[dict] = []
        self.leverage_signed: list[dict] = []
        self.margin_signed: list[dict] = []
        self.sent: list[tuple[int, str]] = []
        self.tx_states: dict[str, dict] = {}
        self.next_nonce_calls = 0
        self.closed = False
        self.clock = clock or Clock()
        self.fail_next_nonce: Exception | None = None
        self.nonce_sequence: deque | None = None   # 需要「先 11 后 12」这类剧本时用

    # -------------------------------------------------------------- 签名
    def sign_create_order(self, **kwargs):
        self.signed.append(dict(kwargs))
        payload = {
            "AccountIndex": self.account_index,
            "ApiKeyIndex": kwargs["api_key_index"],
            "MarketIndex": kwargs["market_index"],
            "ClientOrderIndex": kwargs["client_order_index"],
            "BaseAmount": kwargs["base_amount"],
            "Price": kwargs["price"],
            "IsAsk": kwargs["is_ask"],
            "Type": kwargs["order_type"],
            "TimeInForce": kwargs["time_in_force"],
            "ReduceOnly": int(bool(kwargs["reduce_only"])),
            "TriggerPrice": kwargs["trigger_price"],
            "OrderExpiry": kwargs["order_expiry"],
            "ExpiredAt": int(self.clock() * 1000) + 599_002,
            "Nonce": kwargs["nonce"],
            "Sig": base64.b64encode(b"\x01" * 80).decode("ascii"),
            "L2TxAttributes": None,
        }
        tx_info = json.dumps(payload, separators=(",", ":"))
        # 真实 tx_hash 是 40 字节 hex（80 字符）；sha256 只有 64 字符，这里拼到 80 只为形状一致。
        digest = (hashlib.sha256(tx_info.encode()).hexdigest() * 2)[:80]
        return 14, tx_info, digest, None

    def sign_cancel_all_orders(self, **kwargs):
        self.cancel_signed.append(dict(kwargs))
        payload = {"AccountIndex": self.account_index, "Nonce": kwargs["nonce"],
                   "TimeInForce": kwargs["time_in_force"],
                   "L2TxAttributes": (None if kwargs["cancel_all_market_index"] == 255
                                      else {})}
        tx_info = json.dumps(payload, separators=(",", ":"))
        return 16, tx_info, "c" * 80, None

    def sign_cancel_order(self, **kwargs):
        """单张撤单（止损换单用）：market_index + order_index → tx_type=15。"""
        self.cancel_signed.append(dict(kwargs))
        payload = {"AccountIndex": self.account_index, "Nonce": kwargs["nonce"],
                   "MarketIndex": kwargs["market_index"],
                   "OrderIndex": kwargs["order_index"]}
        return 15, json.dumps(payload, separators=(",", ":")), "d" * 80, None

    def sign_update_leverage(self, **kwargs):
        """逐仓模式 + 杠杆（fraction=10000/leverage、margin_mode 0/1）。"""
        self.leverage_signed.append(dict(kwargs))
        payload = {"AccountIndex": self.account_index, "Nonce": kwargs["nonce"],
                   "MarketIndex": kwargs["market_index"],
                   "Fraction": kwargs["fraction"], "MarginMode": kwargs["margin_mode"]}
        return 12, json.dumps(payload, separators=(",", ":")), "e" * 80, None

    def sign_update_margin(self, **kwargs):
        """逐仓抵押划入/撤出（usdc_amount 整数 ×1e6、direction ADD=1/REMOVE=0）。"""
        self.margin_signed.append(dict(kwargs))
        payload = {"AccountIndex": self.account_index, "Nonce": kwargs["nonce"],
                   "MarketIndex": kwargs["market_index"],
                   "USDCAmount": kwargs["usdc_amount"], "Direction": kwargs["direction"]}
        return 13, json.dumps(payload, separators=(",", ":")), "f" * 80, None

    def mint_auth_token(self, ttl_s: int):
        expires = int(self.clock()) + int(ttl_s)
        return f"{expires}:{self.account_index}:{self.api_key_index}:" + "0f" * 16, None

    # -------------------------------------------------------------- 网络
    async def send_tx(self, tx_type: int, tx_info: str):
        self.sent.append((tx_type, tx_info))
        if self.send_results:
            item = self.send_results.popleft()
            if isinstance(item, BaseException):
                raise item
            return item
        return Resp()

    async def next_nonce(self) -> int:
        self.next_nonce_calls += 1
        if self.fail_next_nonce is not None:
            raise self.fail_next_nonce
        if self.nonce_sequence:
            return self.nonce_sequence.popleft()
        return self.nonce

    async def tx_status(self, tx_hash: str):
        return self.tx_states.get(tx_hash)

    async def close(self) -> None:
        self.closed = True


def wall_now_ms() -> int:
    """真实 SDK 会用自己的墙上时钟填 `ExpiredAt`，所以「只签名」的测试用真实时间当 now。"""
    import time
    return int(time.time() * 1000)


def live_env(account_index: int = 12345, **over) -> dict:
    env = {
        exchange.LIVE_FLAG_KEY: "1",
        exchange.LIVE_ACCOUNT_KEY: str(account_index),
    }
    env.update(over)
    return env


def make_client(sdk: FakeSdk | None = None, *, dry_run: bool = False,
                env: dict | None = None, confirm_live: int | None = 12345,
                clock: Clock | None = None, **kw) -> ExchangeClient:
    clk = clock or Clock()
    return ExchangeClient.from_config(
        make_config(), env=live_env() if env is None else env, sdk=sdk or FakeSdk(clock=clk),
        dry_run=dry_run, confirm_live=confirm_live, clock=clk,
        sleep=clk.sleep, **kw)


# --------------------------------------------------------------------------- 1. 整数 tick 映射


class TickMappingTests(unittest.TestCase):
    """`OrderPlan` → Lighter 订单。所有期望值旁边给算式。"""

    def test_long_entry_is_market_ioc_with_the_worst_price(self):
        plan = make_plan()
        spec = build_entry_order(plan, now_ms=NOW_MS, bar_ms=NOW_MS,
                                 slippage_bps=DEFAULT_ENTRY_SLIPPAGE_BPS)
        # entry 65000.0 @ price_decimals=1 → 650000 ticks
        # 买方向加 30bp：650000 × 10030 ÷ 10000 = 651950（整数，无余数）
        self.assertEqual(spec.price_int, 651950)
        self.assertEqual(spec.qty_int, 4156)          # 数量原样：sizing 已经 floor 过
        self.assertEqual(spec.market_id, 1)
        self.assertEqual(spec.side, 1)
        self.assertEqual(spec.is_ask, 0)              # 多单 = 买 = is_ask 0
        self.assertEqual(spec.order_type, exchange.ORDER_TYPE_MARKET)
        self.assertEqual(spec.time_in_force, exchange.ORDER_TIME_IN_FORCE_IOC)
        self.assertFalse(spec.reduce_only)            # 开仓腿**不是** reduce-only
        self.assertEqual(spec.trigger_price_int, 0)   # 普通单没有触发价
        self.assertEqual(spec.order_expiry_ms, 0)     # IOC 的 expiry 必须是 0（实测）
        self.assertEqual(spec.purpose, "entry")

    def test_short_entry_flips_is_ask_and_rounds_down(self):
        plan = make_plan(symbol="ETH", market_id=0, side=-1, entry_price=3000.0,
                         stop_price=3100.0, stop_price_int=310000, price_decimals=2,
                         qty_int=12345)
        spec = build_entry_order(plan, now_ms=NOW_MS, bar_ms=NOW_MS, slippage_bps=30)
        # 3000.00 @2dp → 300000；卖方向减 30bp：300000 × 9970 ÷ 10000 = 299100
        self.assertEqual(spec.price_int, 299100)
        self.assertEqual(spec.is_ask, 1)
        self.assertEqual(spec.side, -1)

    def test_stop_order_is_reduce_only_mark_triggered_with_real_expiry(self):
        plan = make_plan()
        spec = build_stop_order(plan, now_ms=NOW_MS, bar_ms=NOW_MS, slippage_bps=100)
        self.assertEqual(spec.order_type, exchange.ORDER_TYPE_STOP_LOSS)   # 2
        self.assertEqual(spec.time_in_force, exchange.ORDER_TIME_IN_FORCE_IOC)  # 2 型只收 IOC
        self.assertTrue(spec.reduce_only)              # 平仓腿：不可能开新仓
        self.assertEqual(spec.trigger_price_int, 640000)   # 用 plan 的对齐值，不重算
        # 卖方向再让 100bp：640000 × 9900 ÷ 10000 = 633600
        self.assertEqual(spec.price_int, 633600)
        self.assertEqual(spec.order_expiry_ms, NOW_MS + STOP_ORDER_EXPIRY_MS)
        self.assertGreaterEqual(spec.order_expiry_ms - NOW_MS, exchange.MIN_ORDER_EXPIRY_MS)
        self.assertLessEqual(spec.order_expiry_ms - NOW_MS, exchange.MAX_ORDER_EXPIRY_MS)

    def test_short_stop_worst_price_rounds_up(self):
        plan = make_plan(symbol="SOL", market_id=2, side=-1, entry_price=150.0,
                         stop_price=153.0, stop_price_int=15300, price_decimals=2,
                         qty_int=100000)
        spec = build_stop_order(plan, now_ms=NOW_MS, bar_ms=NOW_MS, slippage_bps=100)
        # 买入止损，加 100bp：15300 × 10100 ÷ 10000 = 15453
        self.assertEqual(spec.price_int, 15453)
        self.assertEqual(spec.is_ask, 0)
        self.assertTrue(spec.reduce_only)

    def test_exit_is_market_ioc_reduce_only_and_inverts_the_side(self):
        spec = build_exit_order(symbol="BTC", market_id=1, side=1, qty_int=4156,
                                price_decimals=1, exit_price=64500.0, now_ms=NOW_MS,
                                bar_ms=NOW_MS, slippage_bps=50)
        # 64500.0 @1dp → 645000；卖方向减 50bp：645000 × 9950 ÷ 10000 = 641775
        self.assertEqual(spec.price_int, 641775)
        self.assertEqual(spec.is_ask, 1)               # 多单 → 卖出平仓
        self.assertTrue(spec.reduce_only)
        self.assertEqual(spec.order_type, exchange.ORDER_TYPE_MARKET)
        self.assertEqual(spec.order_expiry_ms, 0)

    def test_post_only_entry_is_limit_with_gtt_expiry(self):
        plan = make_plan()
        spec = build_entry_order(plan, now_ms=NOW_MS, bar_ms=NOW_MS,
                                 limit_price_int=649000, post_only=True)
        self.assertEqual(spec.order_type, exchange.ORDER_TYPE_LIMIT)          # 0
        self.assertEqual(spec.time_in_force, exchange.ORDER_TIME_IN_FORCE_POST_ONLY)  # 2
        self.assertEqual(spec.price_int, 649000)       # post-only 不加滑点：它是挂单价
        self.assertGreaterEqual(spec.order_expiry_ms, NOW_MS + exchange.MIN_ORDER_EXPIRY_MS)

    def test_stop_on_the_wrong_side_is_rejected(self):
        plan = make_plan(stop_price_int=660000, stop_price=66000.0)   # 多单止损高于入场
        with self.assertRaises(ValueError) as ctx:
            build_stop_order(plan, now_ms=NOW_MS, bar_ms=NOW_MS)
        self.assertIn("must be below entry", str(ctx.exception))

    def test_worst_price_rounding_is_toward_the_unfavourable_side(self):
        # 买：ceil(1001 × 1.003) = ceil(1004.003) = 1005
        self.assertEqual(worst_price_int(1001, 30, is_ask=False), 1005)
        # 卖：floor(1001 × 0.997) = floor(997.997) = 997
        self.assertEqual(worst_price_int(1001, 30, is_ask=True), 997)
        self.assertEqual(worst_price_int(650000, 0, is_ask=False), 650000)

    def test_price_int_from_float_floors(self):
        self.assertEqual(price_int_from_float(65000.05, 1), 650000)
        self.assertEqual(price_int_from_float(65000.99, 1), 650009)

    def test_client_order_index_is_a_stable_48_bit_key(self):
        a = client_order_index("BTC", NOW_MS, "entry")
        b = client_order_index("btc", NOW_MS, "ENTRY")     # 大小写不敏感
        stop = client_order_index("BTC", NOW_MS, "stop")
        self.assertEqual(a, b)
        self.assertNotEqual(a, stop)
        self.assertNotEqual(a, client_order_index("BTC", NOW_MS + 1, "entry"))
        self.assertLessEqual(a, 2 ** 48 - 1)
        self.assertGreaterEqual(a, 1)                      # 0 是前端保留值
        # 公式可复现：sha256("ltab:BTC:<bar>:entry") 的高 6 字节
        digest = hashlib.sha256(f"ltab:BTC:{NOW_MS}:entry".encode()).digest()
        self.assertEqual(a, int.from_bytes(digest[:6], "big"))

    def test_idempotency_key_matches_the_store_priority_middle_rung(self):
        spec = build_entry_order(make_plan(), now_ms=NOW_MS, bar_ms=NOW_MS)
        key = idempotency_key_for(spec)
        self.assertEqual(key, f"order:{spec.client_order_index}:1:651950:4156")
        self.assertTrue(key.startswith("order:"))          # T5 的中间档


# --------------------------------------------------------------------------- 2. dry-run


class DryRunTests(unittest.TestCase):
    def test_default_client_is_dry_run_and_touches_nothing(self):
        sdk = FakeSdk()
        client = ExchangeClient.from_config(make_config(), sdk=sdk)   # 全部默认
        self.assertTrue(client.dry_run)
        result = asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)
        self.assertFalse(result.signed)
        self.assertEqual(result.price_int, 651950)
        self.assertEqual(sdk.signed, [])       # 没签名
        self.assertEqual(sdk.sent, [])         # 没发请求
        self.assertEqual(sdk.next_nonce_calls, 0)   # 连 nonce 都没取

    def test_dry_run_stop_and_cancel_all_also_touch_nothing(self):
        sdk = FakeSdk()
        client = ExchangeClient.from_config(make_config(), sdk=sdk)
        asyncio.run(client.place_stop(make_plan(), bar_ms=NOW_MS))
        asyncio.run(client.place_exit(symbol="BTC", market_id=1, side=1, qty_int=4156,
                                      price_decimals=1, exit_price=64500.0, bar_ms=NOW_MS))
        asyncio.run(client.cancel_all(1))
        asyncio.run(client.cancel_all_orders())
        self.assertEqual(sdk.signed, [])
        self.assertEqual(sdk.cancel_signed, [])
        self.assertEqual(sdk.sent, [])

    def test_preview_signature_signs_locally_but_never_sends(self):
        sdk = FakeSdk()
        client = ExchangeClient.from_config(make_config(), sdk=sdk, preview_signature=True)
        result = asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertTrue(result.dry_run)
        self.assertTrue(result.signed)
        self.assertEqual(len(sdk.signed), 1)
        self.assertEqual(sdk.sent, [])                     # 关键：仍然不发
        digest = dict(result.signature_digest)
        self.assertEqual(digest["tx_type"], 14)
        self.assertEqual(digest["sig_len"], 108)           # base64 的 80 字节
        self.assertEqual(digest["tx_hash_len"], 80)
        self.assertEqual(len(digest["tx_hash_prefix"]), 12)
        self.assertEqual(len(digest["tx_hash_suffix"]), 8)
        self.assertEqual(digest["nonce"], 0)               # 预览用的 nonce，永不发送
        self.assertGreater(digest["tx_info_len"], 200)


# --------------------------------------------------------------------------- 3. 真实下单开关


class LiveGateTests(unittest.TestCase):
    def _disabled(self, env, confirm):
        client = make_client(env=env, confirm_live=confirm)
        with self.assertRaises(LiveTradingDisabled):
            asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertEqual(client.sdk().sent, [])            # 一行都没发
        self.assertEqual(client.sdk().signed, [])
        return client

    def test_no_switch_is_disabled_by_default(self):
        self._disabled(env={}, confirm=None)

    def test_flag_alone_is_not_enough(self):
        self._disabled(env={exchange.LIVE_FLAG_KEY: "1"}, confirm=None)

    def test_flag_and_account_without_confirmation_flag(self):
        self._disabled(env=live_env(), confirm=None)

    def test_confirmation_for_a_different_account_is_refused(self):
        self._disabled(env=live_env(), confirm=999)

    def test_declared_account_must_match_the_configured_account(self):
        self._disabled(env=live_env(account_index=777), confirm=12345)

    def test_all_three_switches_together_allow_one_send(self):
        sdk = FakeSdk()
        client = make_client(sdk=sdk, env=live_env(), confirm_live=12345)
        result = asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertTrue(result.ok)
        self.assertFalse(result.dry_run)
        self.assertEqual(result.code, CODE_OK)
        self.assertEqual(result.nonce, 100)
        self.assertEqual(len(sdk.signed), 1)
        self.assertEqual(len(sdk.sent), 1)
        self.assertEqual(sdk.sent[0][0], 14)               # TxTypeL2CreateOrder
        self.assertEqual(json.loads(sdk.sent[0][1])["Nonce"], 100)
        self.assertFalse(result.confirmed)                 # code=200 ≠ 已确认执行

    def test_gate_reports_the_missing_switch(self):
        gate = LiveGate(enabled=False)
        with self.assertRaises(LiveTradingDisabled) as ctx:
            gate.assert_allowed(12345)
        self.assertIn(exchange.LIVE_FLAG_KEY, str(ctx.exception))

    def test_non_numeric_declared_account_fails_loud(self):
        with self.assertRaises(NotConfigured):
            LiveGate.from_env({exchange.LIVE_FLAG_KEY: "1",
                               exchange.LIVE_ACCOUNT_KEY: "abc"})


# --------------------------------------------------------------------------- 4. nonce


class NonceRetryTests(unittest.TestCase):
    def test_retry_reuses_the_same_signed_payload_and_nonce(self):
        sdk = FakeSdk(nonce=100, send_results=[
            ConnectionError("connection reset by peer"),
            asyncio.TimeoutError("read timeout"),
            Resp(CODE_OK),
        ])
        client = make_client(sdk=sdk)
        result = asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))

        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(sdk.signed), 1)               # 只签一次
        self.assertEqual(len(sdk.sent), 3)
        payloads = [json.loads(info) for _t, info in sdk.sent]
        self.assertEqual({p["Nonce"] for p in payloads}, {100})       # nonce 恒定
        self.assertEqual({info for _t, info in sdk.sent}, {sdk.sent[0][1]})  # 逐字节相同
        self.assertEqual(client.nonces.cached, 101)        # 流只推进一次
        self.assertEqual(client.nonces.allocations, 1)

    def test_busy_code_is_retried_with_the_same_nonce(self):
        sdk = FakeSdk(nonce=57, send_results=[
            Resp(21506, "too many pending txs. Please try again later"),
            Resp(21506, "too many pending txs. Please try again later"),
            Resp(CODE_OK),
        ])
        client = make_client(sdk=sdk)
        result = asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(sdk.signed), 1)
        self.assertEqual({json.loads(i)["Nonce"] for _t, i in sdk.sent}, {57})

    def test_non_retryable_code_is_not_retried_and_the_nonce_is_released(self):
        sdk = FakeSdk(nonce=42, send_results=[
            Resp(21706, "invalid order base or quote amount")])
        client = make_client(sdk=sdk)
        with self.assertRaises(OrderRejected) as ctx:
            asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        exc = ctx.exception
        self.assertEqual(exc.exchange_code, 21706)
        self.assertEqual(exc.kind, "rejected")
        self.assertFalse(exc.retryable)
        self.assertEqual(len(sdk.sent), 1)                 # 没有重试
        self.assertEqual(client.nonces.cached, 42)         # API 拒 → nonce 未消耗 → 还回去
        self.assertEqual(client.nonces.rollbacks, 1)

    def test_all_attempts_failing_transport_raises_ambiguous(self):
        sdk = FakeSdk(nonce=7, send_results=[
            ConnectionError("x"), ConnectionError("y"), ConnectionError("z")])
        client = make_client(sdk=sdk)
        with self.assertRaises(AmbiguousOrder) as ctx:
            asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertEqual(ctx.exception.detail["attempts"], 3)
        self.assertEqual(ctx.exception.detail["nonce"], 7)
        self.assertEqual(len(sdk.sent), 3)
        self.assertEqual({json.loads(i)["Nonce"] for _t, i in sdk.sent}, {7})
        # 结果未知 → **不回滚** nonce（可能已被消耗）
        self.assertEqual(client.nonces.cached, 8)
        self.assertEqual(client.nonces.rollbacks, 0)

    def test_transport_error_with_a_confirmed_tx_does_not_resend(self):
        sdk = FakeSdk(nonce=11, send_results=[ConnectionError("reset")])
        client = make_client(sdk=sdk)
        first_hash = None
        # 先算一次 tx_hash（fake 用 tx_info 的 sha256 前 80 位）
        spec = build_entry_order(make_plan(), now_ms=NOW_MS, bar_ms=NOW_MS)
        probe = FakeSdk()
        _t, info, tx_hash, _e = probe.sign_create_order(
            **spec.sign_kwargs(), skip_nonce=0, nonce=11, api_key_index=4)
        first_hash = tx_hash
        sdk.tx_states[first_hash] = {"status": 2}          # executed
        result = asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertTrue(result.ok)
        self.assertTrue(result.confirmed)
        self.assertEqual(len(sdk.sent), 1)                 # 查到已执行 → 不重发
        self.assertEqual(client.nonces.cached, 12)         # 已消耗

    def test_transport_error_with_a_failed_tx_consuming_the_nonce_stops(self):
        sdk = FakeSdk(nonce=11, send_results=[ConnectionError("reset")])
        client = make_client(sdk=sdk)
        probe = FakeSdk()
        spec = build_entry_order(make_plan(), now_ms=NOW_MS, bar_ms=NOW_MS)
        _t, _i, tx_hash, _e = probe.sign_create_order(
            **spec.sign_kwargs(), skip_nonce=0, nonce=11, api_key_index=4)
        sdk.tx_states[tx_hash] = {"status": 0}             # failed
        sdk.nonce_sequence = deque([11, 12])                 # 分配 11；查状态时已变成 12
        result = asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, "failed")
        self.assertEqual(len(sdk.sent), 1)                 # 绝不重发
        self.assertIn("nonce consumed", result.error)

    def test_nonce_conflict_resyncs_and_never_resigns(self):
        sdk = FakeSdk(nonce=31, send_results=[Resp(21104, "invalid nonce")])
        client = make_client(sdk=sdk)
        with self.assertRaises(NonceConflict) as ctx:
            asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertEqual(len(sdk.sent), 1)                 # 只发了一次
        self.assertEqual(len(sdk.signed), 1)               # 没有重签
        self.assertEqual(client.nonces.fetches, 2)         # 初始取 + resync
        self.assertEqual(ctx.exception.detail["nonce"], 31)

    def test_nonce_stream_rolls_back_only_the_latest_allocation(self):
        stream = NonceStream(lambda: _fixed(50))
        self.assertEqual(asyncio.run(stream.allocate()), 50)
        self.assertEqual(asyncio.run(stream.allocate()), 51)
        self.assertFalse(stream.rollback(50))              # 不是最后一个 → 拒绝回滚
        self.assertTrue(stream.rollback(51))
        self.assertEqual(stream.cached, 51)                # 复用 51
        self.assertEqual(asyncio.run(stream.allocate()), 51)

    def test_signer_using_a_different_nonce_is_refused_before_sending(self):
        sdk = FakeSdk(nonce=5)
        original = sdk.sign_create_order

        def lying(**kwargs):
            kwargs["nonce"] = kwargs["nonce"] + 3          # 故意用错 nonce
            return original(**kwargs)

        sdk.sign_create_order = lying                     # type: ignore[method-assign]
        client = make_client(sdk=sdk)
        with self.assertRaises(ExchangeError) as ctx:
            asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertIn("refusing to submit", str(ctx.exception))
        self.assertEqual(sdk.sent, [])


async def _fixed(value: int) -> int:
    return value


# --------------------------------------------------------------------------- 5. 错误分类


class ErrorClassificationTests(unittest.TestCase):
    TABLE = (
        # (exchange_code, http_status, message, expected kind, expected retryable)
        (21506, None, "", "busy", True),
        (21719, None, "", "busy", True),
        (21104, None, "", "nonce_conflict", False),
        (21105, None, "", "nonce_conflict", False),
        (21108, None, "invalid PublicKey,please run changePubKey", "api_key_not_registered",
         False),
        (21109, None, "", "api_key_not_registered", False),
        (21110, None, "", "api_key_not_registered", False),
        (21739, None, "", "insufficient_margin", False),
        (21507, None, "", "insufficient_margin", False),
        (21706, None, "", "rejected", False),
        (21728, None, "", "duplicate", False),
        (None, 429, "", "rate_limited", True),
        (None, 405, "", "rate_limited", True),
        (None, 503, "", "server_error", True),
        (None, 400, "", "rejected", False),
        (None, None, "rate limit exceeded", "rate_limited", True),
        (None, None, "", "rejected", False),
    )

    def test_table(self):
        for code, status, msg, kind, retryable in self.TABLE:
            with self.subTest(code=code, status=status, msg=msg):
                got_kind, got_retry = classify_error(exchange_code=code,
                                                     http_status=status, message=msg)
                self.assertEqual((got_kind, got_retry), (kind, retryable))

    def test_insufficient_margin_is_reported_as_such(self):
        sdk = FakeSdk(nonce=3, send_results=[Resp(21739, "not enough margin to create the order")])
        client = make_client(sdk=sdk)
        with self.assertRaises(OrderRejected) as ctx:
            asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertEqual(ctx.exception.kind, "insufficient_margin")
        self.assertFalse(ctx.exception.retryable)

    def test_unregistered_api_key_points_at_the_offline_script(self):
        sdk = FakeSdk(nonce=3, send_results=[
            Resp(21108, "invalid PublicKey,please run changePubKey")])
        client = make_client(sdk=sdk)
        with self.assertRaises(NotConfigured) as ctx:
            asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertEqual(ctx.exception.kind, "api_key_not_registered")
        self.assertIn("offline_sign.py", str(ctx.exception))

    def test_http_429_exception_is_retried(self):
        sdk = FakeSdk(nonce=9, send_results=[ApiError(429, 21506, "too many"), Resp(CODE_OK)])
        client = make_client(sdk=sdk)
        result = asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(sdk.sent), 2)

    def test_http_400_with_json_body_is_classified_and_not_retried(self):
        sdk = FakeSdk(nonce=9, send_results=[ApiError(400, 21711, "invalid expiry")])
        client = make_client(sdk=sdk)
        with self.assertRaises(OrderRejected) as ctx:
            asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        self.assertEqual(ctx.exception.exchange_code, 21711)
        self.assertEqual(len(sdk.sent), 1)


# --------------------------------------------------------------------------- 6. auth token


class AuthTokenTests(unittest.TestCase):
    def test_token_is_minted_once_and_refreshed_before_expiry(self):
        clock = Clock(start=1_700_000_000.0)
        mints: list[int] = []

        def mint(ttl: int):
            mints.append(ttl)
            return f"{int(clock()) + ttl}:12345:4:" + "0f" * 16, None

        cache = AuthTokenCache(mint, ttl_s=AUTH_TOKEN_DEFAULT_TTL_S, clock=clock)
        first = cache.get()
        self.assertEqual(cache.mints, 1)
        self.assertEqual(mints, [600])
        self.assertEqual(cache.parse_expiry(first), 1_700_000_600.0)

        # 内部 1 秒后：仍然新鲜，**不重铸**
        clock.now += 1
        self.assertEqual(cache.get(), first)
        self.assertEqual(cache.mints, 1)

        # 差 121 秒过期（> skew 120）→ 仍然新鲜
        clock.now += 600 - 1 - 121
        self.assertTrue(cache.is_fresh())
        self.assertEqual(cache.get(), first)
        self.assertEqual(cache.mints, 1)

        # 只剩 119 秒 → 提前重铸
        clock.now += 2
        self.assertFalse(cache.is_fresh())
        second = cache.get()
        self.assertNotEqual(second, first)
        self.assertEqual(cache.mints, 2)

    def test_token_format_is_expiry_account_key_random(self):
        token = "1790000600:12345:4:0123456789abcdef0123456789abcdef"
        self.assertEqual(AuthTokenCache.parse_expiry(token), 1790000600.0)
        parts = token.split(":")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[1], "12345")
        self.assertEqual(parts[2], "4")
        self.assertEqual(len(parts[3]), 32)

    def test_ttl_outside_the_allowed_window_is_refused(self):
        with self.assertRaises(ValueError):
            AuthTokenCache(lambda ttl: ("x", None), ttl_s=10)
        with self.assertRaises(ValueError):
            AuthTokenCache(lambda ttl: ("x", None), ttl_s=9 * 3600)

    def test_failed_mint_is_fail_closed(self):
        cache = AuthTokenCache(lambda ttl: ("", "api key not registered"))
        with self.assertRaises(NotConfigured) as ctx:
            cache.get()
        self.assertIn("not registered", str(ctx.exception))

    def test_client_wires_the_cache_to_the_sdk(self):
        clock = Clock()
        sdk = FakeSdk(clock=clock)
        client = make_client(sdk=sdk, clock=clock)
        token = client.auth.get()
        self.assertEqual(client.auth.get(), token)
        self.assertEqual(client.auth.mints, 1)
        clock.now += 300                                  # 仍新鲜
        self.assertEqual(client.auth.get(), token)
        self.assertEqual(client.auth.mints, 1)


# ------------------------------------------------------- 6b. 逐仓模式与抵押


class IsolatedMarginTests(unittest.TestCase):
    def test_ensure_isolated_signs_fraction_and_mode(self):
        sdk = FakeSdk(nonce=80)
        client = make_client(sdk=sdk)
        result = asyncio.run(client.ensure_isolated(market_id=2, leverage=5.0))
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "update_leverage")
        self.assertEqual(sdk.leverage_signed[0]["market_index"], 2)
        self.assertEqual(sdk.leverage_signed[0]["fraction"], 2000)     # 10000/5
        self.assertEqual(sdk.leverage_signed[0]["margin_mode"], 1)     # isolated
        self.assertEqual(sdk.sent[0][0], 12)

    def test_ensure_isolated_rejects_bad_leverage(self):
        client = make_client(sdk=FakeSdk())
        with self.assertRaises(ValueError):
            asyncio.run(client.ensure_isolated(market_id=2, leverage=0.0))

    def test_ensure_isolated_dry_run_sends_nothing(self):
        sdk = FakeSdk()
        client = make_client(sdk=sdk, dry_run=True)
        result = asyncio.run(client.ensure_isolated(market_id=2, leverage=5.0))
        self.assertTrue(result.dry_run)
        self.assertEqual(sdk.sent, [])
        self.assertEqual(sdk.leverage_signed, [])

    def test_ensure_isolated_is_gated_too(self):
        client = make_client(sdk=FakeSdk(), env={}, confirm_live=None)
        with self.assertRaises(LiveTradingDisabled):
            asyncio.run(client.ensure_isolated(market_id=2, leverage=5.0))

    def test_allocate_margin_converts_usdc_to_raw_units(self):
        sdk = FakeSdk(nonce=81)
        client = make_client(sdk=sdk)
        result = asyncio.run(client.allocate_margin(market_id=2, usdc_amount=2.4))
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "update_margin_add")
        self.assertEqual(sdk.margin_signed[0]["market_index"], 2)
        self.assertEqual(sdk.margin_signed[0]["usdc_amount"], 2_400_000)  # ×1e6
        self.assertEqual(sdk.margin_signed[0]["direction"], 1)            # ADD
        self.assertEqual(sdk.sent[0][0], 13)

    def test_allocate_margin_rejects_non_positive_amount(self):
        client = make_client(sdk=FakeSdk())
        with self.assertRaises(ValueError):
            asyncio.run(client.allocate_margin(market_id=2, usdc_amount=0.0))

    def test_allocate_margin_dry_run_sends_nothing(self):
        sdk = FakeSdk()
        client = make_client(sdk=sdk, dry_run=True)
        result = asyncio.run(client.allocate_margin(market_id=2, usdc_amount=2.4))
        self.assertTrue(result.dry_run)
        self.assertEqual(sdk.sent, [])
        self.assertEqual(sdk.margin_signed, [])


# --------------------------------------------------------------------------- 7. 撤单


class CancelTests(unittest.TestCase):
    def test_cancel_all_for_one_market(self):
        sdk = FakeSdk(nonce=70)
        client = make_client(sdk=sdk)
        result = asyncio.run(client.cancel_all(1))
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "cancel_all")
        self.assertEqual(sdk.cancel_signed[0]["cancel_all_market_index"], 1)
        self.assertEqual(sdk.cancel_signed[0]["time_in_force"], 0)     # immediate
        self.assertEqual(sdk.sent[0][0], 16)                          # CancelAllOrders

    def test_cancel_single_order_signs_tx15_with_market_and_order_index(self):
        sdk = FakeSdk(nonce=72)
        client = make_client(sdk=sdk)
        result = asyncio.run(client.cancel_order(market_id=2, order_index=1125898605399395))
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "cancel")
        self.assertEqual(sdk.cancel_signed[0]["market_index"], 2)
        self.assertEqual(sdk.cancel_signed[0]["order_index"], 1125898605399395)
        self.assertEqual(sdk.sent[0][0], 15)                          # CancelOrder

    def test_cancel_single_order_dry_run_sends_nothing(self):
        sdk = FakeSdk()
        client = make_client(sdk=sdk, dry_run=True)
        result = asyncio.run(client.cancel_order(market_id=2, order_index=9))
        self.assertTrue(result.dry_run)
        self.assertEqual(sdk.sent, [])
        self.assertEqual(sdk.cancel_signed, [])

    def test_cancel_single_order_is_gated_too(self):
        sdk = FakeSdk()
        client = make_client(sdk=sdk, env={}, confirm_live=None)
        with self.assertRaises(LiveTradingDisabled):
            asyncio.run(client.cancel_order(market_id=2, order_index=9))

    def test_cancel_all_across_every_market_uses_the_nil_index(self):
        sdk = FakeSdk(nonce=71)
        client = make_client(sdk=sdk)
        result = asyncio.run(client.cancel_all_orders())
        self.assertEqual(result.action, "cancel_all_markets")
        self.assertEqual(sdk.cancel_signed[0]["cancel_all_market_index"],
                         exchange.NIL_MARKET_INDEX)

    def test_cancel_all_is_gated_too(self):
        sdk = FakeSdk()
        client = make_client(sdk=sdk, env={}, confirm_live=None)
        with self.assertRaises(LiveTradingDisabled):
            asyncio.run(client.cancel_all(1))
        self.assertEqual(sdk.cancel_signed, [])


# --------------------------------------------------------------------------- 8. 秘密


class SecretHandlingTests(unittest.TestCase):
    def test_private_key_never_appears_in_repr_errors_or_results(self):
        sdk = FakeSdk(nonce=1, send_results=[Resp(21706, "invalid order base amount")])
        client = make_client(sdk=sdk)
        self.assertNotIn(API_PK, repr(client))
        self.assertNotIn(API_PK[2:], repr(client))
        self.assertIn("***", repr(client))
        with self.assertRaises(ExchangeError) as ctx:
            asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        payload = json.dumps({k: str(v) for k, v in ctx.exception.as_dict().items()})
        self.assertNotIn(API_PK[2:20], payload)
        self.assertNotIn("ab" * 20, str(ctx.exception))

    def test_config_redaction_is_still_in_effect(self):
        cfg = make_config()
        self.assertNotIn(API_PK, repr(cfg))

    def test_order_result_dict_has_the_ok_code_error_triplet(self):
        client = ExchangeClient.from_config(make_config(), sdk=FakeSdk())
        result = asyncio.run(client.place_entry(make_plan(), bar_ms=NOW_MS))
        d = result.as_dict()
        self.assertEqual(d["ok"], True)
        self.assertIn("code", d)
        self.assertIn("error", d)
        self.assertTrue(d["dry_run"])
        json.dumps(d)                                     # 必须可序列化（T10 面板）

    def test_error_dict_has_the_triplet_and_no_secret(self):
        exc = OrderRejected("boom", exchange_code=21706)
        d = exc.as_dict()
        self.assertEqual(d["ok"], False)
        self.assertEqual(d["code"], "rejected")
        self.assertEqual(d["error"], "boom")
        self.assertFalse(d["retryable"])


# --------------------------------------------------------------------------- 9. 真实 SDK（只签名，不联网）


class RealSignerOfflineTests(unittest.TestCase):
    """唯一会加载 WASM signer 的测试。**只签不提交**，并断言 transport 未被调用。"""

    @classmethod
    def setUpClass(cls):
        import lighter
        cls.lighter = lighter
        private_key, public_key, error = lighter.create_api_key()
        assert error is None, error
        cls.api_private_key = private_key
        cls.api_public_key = public_key

    def _sdk(self):
        return exchange.LighterSdk(
            base_url="https://mainnet.zklighter.elliot.ai/api/v1",
            account_index=12345, api_key_index=4,
            api_private_key=self.api_private_key, environment="mainnet")

    def test_api_key_shape_matches_the_config_validator(self):
        self.assertEqual(len(self.api_private_key), 82)        # 0x + 80 hex = 40 字节
        self.assertTrue(self.api_private_key.startswith("0x"))
        self.assertEqual(len(self.api_public_key), 82)         # 0x + 80 hex = 40 字节
        # config 的校验规则必须接受它（否则 offline_sign 产出的 key 会被挡在门外）
        make_config(LIGHTER_API_PRIVATE_KEY=self.api_private_key)

    def test_chain_id_is_pinned_per_environment(self):
        async def run():
            sdk = self._sdk()
            try:
                self.assertEqual(sdk.chain_id, 304)
            finally:
                await sdk.close()
        asyncio.run(run())
        # URL 与 environment 不一致 → 在**建 signer 之前**就拒绝（不会泄漏 aiohttp session）
        for url, env in (("https://testnet.zklighter.elliot.ai/api/v1", "mainnet"),
                         ("https://mainnet.zklighter.elliot.ai/api/v1", "testnet")):
            with self.subTest(url=url, env=env), self.assertRaises(NotConfigured) as ctx:
                exchange.LighterSdk(base_url=url, account_index=1, api_key_index=4,
                                    api_private_key=self.api_private_key,
                                    environment=env)
            self.assertIn("does not belong to environment", str(ctx.exception))
        # testnet 的 chain_id 是 300
        async def testnet_run():
            sdk = exchange.LighterSdk(
                base_url="https://testnet.zklighter.elliot.ai/api/v1", account_index=1,
                api_key_index=4, api_private_key=self.api_private_key,
                environment="testnet")
            try:
                self.assertEqual(sdk.chain_id, 300)
            finally:
                await sdk.close()
        asyncio.run(testnet_run())

    def test_reserved_api_key_indexes_are_refused(self):
        for bad in (0, 3, 255):
            with self.subTest(index=bad), self.assertRaises(NotConfigured):
                exchange.LighterSdk(base_url="https://mainnet.zklighter.elliot.ai/api/v1",
                                    account_index=1, api_key_index=bad,
                                    api_private_key=self.api_private_key,
                                    environment="mainnet")

    def test_sdk_url_has_no_duplicated_api_prefix(self):
        """SDK 拿到的 url 必须是**站点根**，不能带 `/api/v1`。

        SDK 自己会拼 `resource_path='/api/v1/...'`，所以把含 `/api/v1` 的
        base_url 直接传进去会得到 `/api/v1/api/v1/nextNonce` ⇒ CloudFront 403。

        这个 403 长得跟「被地域限制」一模一样（都是 `Server: CloudFront`），
        是这类 bug 最难查的地方：真正的线索在**实际请求的 URL** 里。
        """
        self.assertEqual(exchange._sdk_api_root("https://mainnet.zklighter.elliot.ai/api/v1"),
                         "https://mainnet.zklighter.elliot.ai")
        self.assertEqual(exchange._sdk_api_root("https://mainnet.zklighter.elliot.ai/api/v1/"),
                         "https://mainnet.zklighter.elliot.ai")
        # 本来就没有 /api/v1 的也不能被改坏
        self.assertEqual(exchange._sdk_api_root("https://mainnet.zklighter.elliot.ai"),
                         "https://mainnet.zklighter.elliot.ai")

        async def run():
            sdk = self._sdk()
            try:
                self.assertEqual(sdk._signer.url, "https://mainnet.zklighter.elliot.ai")
                self.assertNotIn("/api/v1/api/v1", sdk._signer.url)
            finally:
                await sdk.close()
        asyncio.run(run())

    def test_sdk_http_identity_is_configured(self):
        """SDK 的 HTTP 层必须带上 User-Agent，且鉴权请求必须带 Authorization。

        两者都是**实测**出来的必要条件，不是风格偏好：

        * 缺 User-Agent ⇒ CloudFront 按 aiohttp 默认 UA 判成脚本客户端 ⇒ 403；
        * 缺 Authorization ⇒ SDK 的 `nextNonce` 端点没声明鉴权
          （`_auth_settings: List[str] = []`）⇒ 请求不带 Authorization ⇒ 403。

        这个测试**不发网络请求**：只断言构造后的 SDK 对象上这些属性就位。
        """
        async def run():
            sdk = self._sdk()
            try:
                signer = sdk._signer
                api_client = signer.api_client

                # --- User-Agent ---
                self.assertEqual(api_client.user_agent, exchange.SDK_USER_AGENT)
                self.assertEqual(api_client.default_headers["User-Agent"],
                                 exchange.SDK_USER_AGENT)
                session = api_client.rest_client.pool_manager
                self.assertEqual(session.headers["User-Agent"], exchange.SDK_USER_AGENT)

                # --- Authorization（显式头，不依赖 SDK 的 auth_settings）---
                headers = sdk.auth_headers()
                self.assertIn("Authorization", headers)
                self.assertEqual(headers["User-Agent"], exchange.SDK_USER_AGENT)
                token = headers["Authorization"]
                # auth token 的形状：<deadline>:<account_index>:<api_key_index>:<sig>
                parts = token.split(":")
                self.assertGreaterEqual(len(parts), 4)
                self.assertEqual(parts[1], "12345")          # account_index
                self.assertEqual(parts[2], "4")               # api_key_index

                # 缓存：同样的调用拿到同一个 token（不重复签）
                self.assertEqual(sdk.auth_headers()["Authorization"], token)
            finally:
                await sdk.close()
        asyncio.run(run())

    def test_auth_token_failure_is_not_swallowed(self):
        """签不出 auth token 时必须**拒绝发请求**，绝不静默降级成「不带 Authorization」。

        静默降级的失败模式在实盘里表现为「偶发下单失败」，最难查。
        """
        async def run():
            sdk = self._sdk()
            try:
                def _boom(ttl_s):
                    return None, "signer says no"

                sdk._auth_cache._mint = _boom
                with self.assertRaises(NotConfigured) as ctx:
                    sdk.auth_headers()
                self.assertIn("could not mint an auth token", str(ctx.exception))
            finally:
                await sdk.close()
        asyncio.run(run())

    def test_sign_only_never_touches_the_transport(self):
        spec = build_entry_order(make_plan(), now_ms=NOW_MS, bar_ms=NOW_MS)

        async def run():
            sdk = self._sdk()
            called = []

            async def forbidden(*args, **kwargs):
                called.append((args, kwargs))
                raise AssertionError("sign_create_order must not talk to the network")

            original = sdk._signer.tx_api.send_tx            # noqa: SLF001 - 断言用
            sdk._signer.tx_api.send_tx = forbidden           # type: ignore[method-assign]
            try:
                result = sdk.sign_create_order(**spec.sign_kwargs(), skip_nonce=0,
                                               nonce=7, api_key_index=4)
            finally:
                sdk._signer.tx_api.send_tx = original        # type: ignore[method-assign]
                await sdk.close()
            return result, called
        (tx_type, tx_info, tx_hash, err), called = asyncio.run(run())
        self.assertIsNone(err)
        self.assertEqual(called, [])                         # 传输层一次都没被调用
        self.assertEqual(tx_type, 14)

    def test_signed_payload_encodes_the_integer_ticks_exactly(self):
        now = wall_now_ms()
        spec = build_entry_order(make_plan(), now_ms=now, bar_ms=NOW_MS)

        async def run():
            sdk = self._sdk()
            try:
                return sdk.sign_create_order(**spec.sign_kwargs(), skip_nonce=0,
                                             nonce=7, api_key_index=4)
            finally:
                await sdk.close()
        tx_type, tx_info, tx_hash, err = asyncio.run(run())
        self.assertIsNone(err)
        payload = json.loads(tx_info)
        self.assertEqual(sorted(payload), [
            "AccountIndex", "ApiKeyIndex", "BaseAmount", "ClientOrderIndex", "ExpiredAt",
            "IsAsk", "L2TxAttributes", "MarketIndex", "Nonce", "OrderExpiry", "Price",
            "ReduceOnly", "Sig", "TimeInForce", "TriggerPrice", "Type"])
        self.assertEqual(payload["MarketIndex"], 1)
        self.assertEqual(payload["ClientOrderIndex"], spec.client_order_index)
        self.assertEqual(payload["BaseAmount"], 4156)
        self.assertEqual(payload["Price"], 651950)
        self.assertEqual(payload["IsAsk"], 0)
        self.assertEqual(payload["Type"], 1)                 # MARKET
        self.assertEqual(payload["TimeInForce"], 0)          # IOC
        self.assertEqual(payload["ReduceOnly"], 0)
        self.assertEqual(payload["TriggerPrice"], 0)
        self.assertEqual(payload["OrderExpiry"], 0)
        self.assertEqual(payload["Nonce"], 7)
        self.assertEqual(payload["AccountIndex"], 12345)
        self.assertEqual(payload["ApiKeyIndex"], 4)
        self.assertIsNone(payload["L2TxAttributes"])
        # 交易过期时间由 signer 自己填：约 +10 分钟（实测 now+599002ms）
        self.assertGreater(payload["ExpiredAt"], now)
        self.assertLess(payload["ExpiredAt"], now + 15 * 60 * 1000)
        # Sig = base64(80 字节)，108 字符带 '=' 结尾（实测；不是直觉上的 64 字节）
        sig = base64.b64decode(payload["Sig"], validate=True)
        self.assertEqual(len(sig), 80)
        self.assertEqual(len(payload["Sig"]), 108)
        # tx_hash = 40 字节 hex
        self.assertEqual(len(tx_hash), 80)
        int(tx_hash, 16)                                     # 必须是合法 hex
        # tx_info 是紧凑 JSON（无空格）：签名对象的规范化编码
        self.assertNotIn(", ", tx_info)
        self.assertNotIn(": ", tx_info)

    def test_two_signatures_differ_only_in_expiry_and_sig(self):
        spec = build_entry_order(make_plan(), now_ms=NOW_MS, bar_ms=NOW_MS)

        async def run():
            sdk = self._sdk()
            try:
                return [sdk.sign_create_order(**spec.sign_kwargs(), skip_nonce=0,
                                              nonce=7, api_key_index=4) for _ in range(2)]
            finally:
                await sdk.close()
        first, second = asyncio.run(run())
        a, b = json.loads(first[1]), json.loads(second[1])
        differing = {key for key in a if a[key] != b[key]}
        # signer 自己填 ExpiredAt（now+~10min），所以「同一组参数」的差别只可能在这两个
        # 字段上；其余 14 个字段的编码必须是确定的。
        self.assertLessEqual(differing, {"ExpiredAt", "Sig"})
        if a["ExpiredAt"] == b["ExpiredAt"]:
            self.assertEqual(first[1], second[1])
            self.assertEqual(first[2], second[2])

    def test_stop_order_signs_as_stop_loss_with_reduce_only_and_real_expiry(self):
        now = wall_now_ms()
        spec = build_stop_order(make_plan(), now_ms=now, bar_ms=NOW_MS, slippage_bps=100)

        async def run():
            sdk = self._sdk()
            try:
                return sdk.sign_create_order(**spec.sign_kwargs(), skip_nonce=0,
                                             nonce=8, api_key_index=4)
            finally:
                await sdk.close()
        _tx_type, tx_info, _tx_hash, err = asyncio.run(run())
        self.assertIsNone(err)
        payload = json.loads(tx_info)
        self.assertEqual(payload["Type"], 2)                 # STOP_LOSS
        self.assertEqual(payload["TimeInForce"], 0)          # 实测：非 IOC 会被 signer 拒
        self.assertEqual(payload["ReduceOnly"], 1)
        self.assertEqual(payload["TriggerPrice"], 640000)
        self.assertEqual(payload["Price"], 633600)
        self.assertGreater(payload["OrderExpiry"], now + 27 * 24 * 3600 * 1000)

    def test_post_only_entry_signs_with_gtt_expiry(self):
        now = wall_now_ms()
        spec = build_entry_order(make_plan(), now_ms=now, bar_ms=NOW_MS,
                                 limit_price_int=649000, post_only=True)

        async def run():
            sdk = self._sdk()
            try:
                return sdk.sign_create_order(**spec.sign_kwargs(), skip_nonce=0,
                                             nonce=9, api_key_index=4)
            finally:
                await sdk.close()
        _tx, tx_info, _hash, err = asyncio.run(run())
        self.assertIsNone(err)
        payload = json.loads(tx_info)
        self.assertEqual(payload["Type"], 0)                 # LIMIT
        self.assertEqual(payload["TimeInForce"], 2)          # POST_ONLY
        self.assertEqual(payload["Price"], 649000)
        self.assertGreaterEqual(payload["OrderExpiry"], now + exchange.MIN_ORDER_EXPIRY_MS)

    def test_cancel_all_payload_carries_the_market_attribute(self):
        async def run():
            sdk = self._sdk()
            try:
                return (sdk.sign_cancel_all_orders(time_in_force=0, timestamp_ms=0,
                                                   cancel_all_market_index=1, skip_nonce=0,
                                                   nonce=10, api_key_index=4),
                        sdk.sign_cancel_all_orders(time_in_force=0, timestamp_ms=0,
                                                   cancel_all_market_index=255, skip_nonce=0,
                                                   nonce=11, api_key_index=4))
            finally:
                await sdk.close()
        (t1, i1, _h1, e1), (t2, i2, _h2, e2) = asyncio.run(run())
        self.assertIsNone(e1)
        self.assertIsNone(e2)
        self.assertEqual(t1, 16)
        self.assertEqual(json.loads(i1)["L2TxAttributes"], {"5": 1})   # 实测形状
        self.assertIsNone(json.loads(i2)["L2TxAttributes"])            # 全市场 = null


# --------------------------------------------------------------------------- 10. 模块边界


class ModuleBoundaryTests(unittest.TestCase):
    def test_only_exchange_imports_the_signer(self):
        offenders = []
        for path in sorted((ROOT / "app").glob("*.py")):
            if path.name == "exchange.py":
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*(import lighter|from lighter)", text, re.MULTILINE) or \
                    "signer_client" in text or "sign_create_order" in text or \
                    "send_tx" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"these modules must not touch the signer: {offenders}")

    def test_exchange_does_not_import_lighter_at_module_scope(self):
        text = (ROOT / "app" / "exchange.py").read_text(encoding="utf-8")
        tree = __import__("ast").parse(text)
        top_level = []
        for node in tree.body:
            if isinstance(node, __import__("ast").Import):
                top_level += [a.name for a in node.names]
            elif isinstance(node, __import__("ast").ImportFrom):
                top_level.append(node.module or "")
        self.assertNotIn("lighter", top_level)
        self.assertFalse([m for m in top_level if m.startswith("lighter")])

    def test_main_module_still_claims_to_be_the_only_write_path(self):
        # T1 的 docstring 承诺「exchange.py 是唯一写路径」，这里只是把它当契约记住。
        self.assertTrue(callable(exchange.ExchangeClient.place_entry))

    def test_requirements_are_untouched(self):
        # T7 不引入任何新依赖（lighter-sdk 已在 T1 定好）。这个 sha256 是
        # requirements.txt 在 T7 分支上的值；改依赖必须显式改这里 —— 那正是重点。
        digest = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            "8a11a5704b5a30db860f3c73d24b4bb1ffaced7968e8e7d071684af8d2c41059")
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("lighter-sdk==1.1.8", text)


# --------------------------------------------------------------------------- 11. offline_sign.py


class OfflineSignScriptTests(unittest.TestCase):
    def setUp(self):
        import importlib
        self.mod = importlib.import_module("scripts.offline_sign")

    def test_argv_with_a_private_key_flag_is_refused(self):
        for argv in (["--private-key", "0x" + "aa" * 32],
                     ["--l1-private-key=0x" + "bb" * 32],
                     ["--eth-private-key", "0x" + "cc" * 32]):
            with self.subTest(argv=argv):
                with self.assertRaises(self.mod.PrivacyError):
                    self.mod.reject_secrets_in_argv(argv)

    def test_argv_with_a_bare_key_value_is_refused(self):
        with self.assertRaises(self.mod.PrivacyError):
            self.mod.reject_secrets_in_argv(["0x" + "dd" * 32])

    def test_main_returns_2_and_prints_nothing_secret(self):
        with mock.patch("sys.stderr") as err:
            code = self.mod.main(["--private-key", "0x" + "ee" * 32])
        self.assertEqual(code, 2)
        printed = "".join(str(c.args[0]) for c in err.write.call_args_list)
        self.assertNotIn("ee" * 10, printed)                # 不回显私钥
        self.assertIn("command line", printed)

    def test_parser_has_no_private_key_option(self):
        parser = self.mod.build_parser()
        options = {opt for action in parser._actions for opt in action.option_strings}
        self.assertNotIn("--private-key", options)
        self.assertNotIn("--l1-private-key", options)
        self.assertEqual({o for o in options if "private" in o or "secret" in o}, set())
        # argv 里唯一和 key 有关的选项是 index（不是密钥本身）
        self.assertEqual({o for o in options if "key" in o}, {"--api-key-index",
                                                             "--save-api-key"})

    def test_api_key_index_must_be_in_the_app_range(self):
        for bad in ("0", "3", "255"):
            with self.subTest(index=bad):
                self.assertEqual(self.mod.main(["--api-key-index", bad]), 2)

    def test_missing_l1_key_is_reported_without_echoing_anything(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("getpass.getpass", return_value=""):
            self.assertEqual(self.mod.main(["--api-key-index", "4"]), 2)

    def test_script_never_writes_the_l1_key_to_disk(self):
        text = (ROOT / "scripts" / "offline_sign.py").read_text(encoding="utf-8")
        self.assertNotIn("open(os.environ", text)
        self.assertIn("getpass", text)                       # 交互输入走 getpass
        self.assertIn("LIGHTER_L1_PRIVATE_KEY", text)        # 环境变量路径
        self.assertNotIn("argparse.FileType", text)
        # 除了 --save-api-key（API 私钥，不是 L1 私钥）之外没有写文件的地方
        self.assertEqual(text.count("os.open("), 1)


# --------------------------------------------------------------------------- 12. 其它不变式


class SpecInvariantTests(unittest.TestCase):
    def test_describe_has_no_secrets_and_mentions_the_ticks(self):
        spec = build_entry_order(make_plan(), now_ms=NOW_MS, bar_ms=NOW_MS)
        text = spec.describe()
        self.assertIn("dry-run", text)
        self.assertIn("651950", text)
        self.assertIn("4156", text)
        self.assertIn("reduce_only=0", text)

    def test_order_spec_is_frozen_and_serialisable(self):
        spec = build_stop_order(make_plan(), now_ms=NOW_MS, bar_ms=NOW_MS)
        with self.assertRaises(Exception):
            spec.price_int = 1                               # type: ignore[misc]
        json.dumps(spec.as_dict())
        self.assertEqual(spec.as_dict()["is_ask"], 1)

    def test_sign_kwargs_match_the_sdk_signature(self):
        # 用真实 SDK 的方法签名核对：字段名/顺序都对得上，防止 SDK 升级后静默错位。
        from lighter.signer_client import SignerClient
        params = list(inspect.signature(SignerClient.sign_create_order).parameters)
        spec = build_entry_order(make_plan(), now_ms=NOW_MS, bar_ms=NOW_MS)
        for key in spec.sign_kwargs():
            self.assertIn(key, params)

    def test_wait_for_tx_confirms_and_times_out(self):
        clock = Clock()
        sdk = FakeSdk(clock=clock)
        client = make_client(sdk=sdk, clock=clock)
        sdk.tx_states["good"] = {"status": 2}
        self.assertEqual(asyncio.run(client.wait_for_tx("good", timeout_s=1))["confirmed"], True)
        sdk.tx_states["bad"] = {"status": 0}
        self.assertFalse(asyncio.run(client.wait_for_tx("bad", timeout_s=1))["ok"])
        out = asyncio.run(client.wait_for_tx("missing", timeout_s=1, interval_s=0.5))
        self.assertEqual((out["ok"], out["code"]), (False, "timeout"))


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
