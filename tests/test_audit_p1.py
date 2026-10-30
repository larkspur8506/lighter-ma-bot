"""对抗审查 P1 回归（2026-10-30，修复对象 = cde6e4db..db814786 上实锤的三个 P1）。

- P1-1 CLOSING 僵尸仓：`begin_close` 之后任何异常（place_exit 抛异常 / 确认读数失败）
  必须当场收尸——交易所仍有仓 → abort 回 OPEN；交易所已平 → 按证据结案。
  超龄 CLOSING（60 分钟）由 tick 级兜底 `_reap_stale_closing` 强制收尸。
- P1-3 `place_stop` 抛异常（非拒绝）时，真实仓位必须**照样记账**（异常并入
  stop_order_failed 路径），缺失的止损交给下一轮守护。
- P2-2/P1-2（flatten 侧）：共享 client 单例 + `_flatten_confirm_flat` 的 fail-closed 语义。

每条测试都对应一个曾真实复现的缺陷；修前在旧代码上全部命中。
"""
import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from app.exchange import client_order_index
from app.lifecycle import ACTION_EXIT, decide_exit
from app.strategy import compute_indicators
from tests.test_executor import (BAR_MS, BTC, Base, FakeExchange, PLAN_QTY_INT,
                                 SIGNAL_BARS)


def _exit_decision(exe, row):
    """给已持仓的行造一个离场 decision（不管信号是否真触发——单测直打执行路径）。"""
    pv = exe.lifecycle.position_view(BTC)
    d = decide_exit(pv, SIGNAL_BARS, compute_indicators(SIGNAL_BARS), len(SIGNAL_BARS) - 1)
    # 统一走 exit 腿（decision.exit_price 给个合法价，避开 to_int_price(0) 的前置拒绝）
    return replace(d, action=ACTION_EXIT, exit_price=100.0)


class RaisingExitExchange(FakeExchange):
    """模拟真实 ExchangeClient 的行为：拒单/网络错误以**异常**抛出，而不是 OrderResult。"""

    async def place_exit(self, **kw):
        raise RuntimeError("simulated: exit tx rejected/ambiguous at the sequencer")


class ClosingRecoveryTests(Base):
    def _seed(self, ex, *, position_on_exchange=True):
        if position_on_exchange:
            ex.positions[BTC] = {"side": 1, "qty_int": PLAN_QTY_INT, "market_id": 1,
                                 "size_decimals": 5}
        self.executor = self.make_executor(exchange=ex)
        self.reader.exchange = ex
        return self.seed_position()

    def test_exit_exception_with_position_still_open_aborts_to_open(self):
        """P1-1：离场异常 + 交易所仍有仓 → abort 回 OPEN（守护恢复接管），绝不留 CLOSING。"""
        ex = RaisingExitExchange()
        row = self._seed(ex)
        pid = int(row["id"])
        d = _exit_decision(self.executor, row)
        account = asyncio.run(self.reader.read())
        with self.assertRaises(RuntimeError):
            asyncio.run(self.executor._execute_exit(d, {}, account))
        self.assertEqual(self.store.position(pid)["state"], "OPEN")
        self.assertTrue(any(e["kind"] == "closing_recovery" for e in self.events()),
                        "收尸必须落事件")
        self.assertTrue(any(k == "closing_recovery" for k, _ in self.alerts))

    def test_exit_exception_with_exchange_flat_settles_from_evidence(self):
        """P1-1：离场异常但交易所已无仓 → 按证据结案（CLOSED），也不留 CLOSING。"""
        ex = RaisingExitExchange()
        ex.positions.pop(BTC, None)                      # 交易所已平
        row = self._seed(ex, position_on_exchange=False)
        pid = int(row["id"])
        d = _exit_decision(self.executor, row)
        account = asyncio.run(self.reader.read())
        with self.assertRaises(RuntimeError):
            asyncio.run(self.executor._execute_exit(d, {}, account))
        self.assertEqual(self.store.position(pid)["state"], "CLOSED")
        self.assertTrue(any(e["kind"] == "closing_recovery" for e in self.events()))
        self.assertTrue(any(k == "closing_recovery" for k, _ in self.alerts))

    def test_stale_closing_with_position_on_exchange_is_reaped(self):
        """P1-1 兜底：CLOSING 超 60 分钟且交易所仍有仓 → tick 强制 abort 回 OPEN。

        覆盖「12s 成交确认窗内进程被杀」这类快速收尸管不到的漏网。
        """
        ex = FakeExchange()
        ex.positions[BTC] = {"side": 1, "qty_int": PLAN_QTY_INT, "market_id": 1,
                             "size_decimals": 5}
        self.executor = self.make_executor(exchange=ex)
        self.reader.exchange = ex
        row = self.seed_position()
        pid = int(row["id"])
        self.store.begin_close(pid, ts=float(self.clock.now) - 7200.0,
                               reason="simulated_restart_mid_confirm")
        self.assertEqual(self.store.position(pid)["state"], "CLOSING")
        self.run_tick()
        self.assertEqual(self.store.position(pid)["state"], "OPEN")
        self.assertTrue(any(e["kind"] == "closing_recovery" for e in self.events()))
        self.assertTrue(any(k == "closing_recovery" for k, _ in self.alerts))

    def test_fresh_closing_row_is_left_alone(self):
        """刚进入 CLOSING 的行（12s 确认窗内）不许被兜底收尸打扰——那是确认窗的正常节奏。"""
        ex = FakeExchange()
        ex.positions[BTC] = {"side": 1, "qty_int": PLAN_QTY_INT, "market_id": 1,
                             "size_decimals": 5}
        self.executor = self.make_executor(exchange=ex)
        self.reader.exchange = ex
        row = self.seed_position()
        pid = int(row["id"])
        self.store.begin_close(pid, ts=float(self.clock.now), reason="just_started")
        self.run_tick()
        self.assertEqual(self.store.position(pid)["state"], "CLOSING")
        self.assertFalse(any(k == "closing_recovery" for k, _ in self.alerts))


class StopExceptionTests(Base):
    def test_stop_exception_still_books_the_entry(self):
        """P1-3：place_stop 抛异常（非拒绝）→ 照样 apply_entry 记账 + 告警，不留无账裸仓。"""
        class RaisingStopExchange(FakeExchange):
            async def place_stop(self, plan, *, bar_ms=None, client_order_index=None, **kw):
                raise RuntimeError("simulated: stop tx ambiguous at the sequencer")

        ex = RaisingStopExchange()
        self.executor = self.make_executor(exchange=ex)
        self.reader.exchange = ex
        report = self.run_tick()
        rows = self.store.open_positions()
        self.assertEqual(len(rows), 1, "异常路径也必须把真实仓位记进账")
        self.assertEqual(rows[0]["state"], "OPEN")
        self.assertEqual(len(ex.entries), 1)
        self.assertTrue(any(e["kind"] == "stop_order_exception" for e in self.events()))
        self.assertTrue(any(e["kind"] == "stop_order_failed" for e in self.events()))
        self.assertTrue(any(k == "stop_order_failed" for k, _ in self.alerts))
        self.assertFalse(report.circuit_open or False)


class SharedClientTests(unittest.TestCase):
    def test_shared_exchange_client_is_a_singleton_per_key(self):
        """P2-2：flatten 与 tick 必须共用同一把 client（各自独立 nonce 流会互撞）。"""
        from app import main as m
        import app.exchange as exchange_module
        created = []

        class _Stub:
            @classmethod
            def from_config(cls, *a, **k):
                instance = cls()
                created.append(instance)
                return instance

        cfg = SimpleNamespace(environment="prod")
        with mock.patch.object(exchange_module, "ExchangeClient", _Stub):
            c1 = m._shared_exchange_client(cfg, confirm_live=1)
            c2 = m._shared_exchange_client(cfg, confirm_live=1)
            c3 = m._shared_exchange_client(cfg, confirm_live=2)
        self.assertIs(c1, c2, "同键必须复用同一实例")
        self.assertIsNot(c1, c3, "不同键（confirm_live 不同）各自一份")
        self.assertEqual(len(created), 2)
        m._SHARED_EXCHANGE_CLIENTS.clear()


class FlattenConfirmFlatTests(unittest.TestCase):
    def test_flat_when_position_absent(self):
        from app.main import _flatten_confirm_flat

        async def _run():
            return await _flatten_confirm_flat(_Reader(None), BTC, timeout_s=0.5)

        self.assertTrue(asyncio.run(_run()))

    def test_not_flat_when_position_present(self):
        from app.main import _flatten_confirm_flat

        pos = SimpleNamespace(qty=1.2)

        async def _run():
            return await _flatten_confirm_flat(_Reader(pos), BTC, timeout_s=0.5)

        self.assertFalse(asyncio.run(_run()))

    def test_reader_errors_are_not_treated_as_flat(self):
        """读失败 ≠ 没仓：fail-closed，绝不据此撤挂单。"""
        from app.main import _flatten_confirm_flat

        async def _run():
            return await _flatten_confirm_flat(_Reader(RuntimeError("boom")), BTC,
                                               timeout_s=0.5)

        self.assertFalse(asyncio.run(_run()))


class _Reader:
    """LighterAccountReader 的最小替身：positions 直接给定（None=无仓）。"""

    def __init__(self, pos):
        self._pos = pos

    async def read(self):
        if isinstance(self._pos, Exception):
            raise self._pos
        return SimpleNamespace(position_for=lambda symbol: self._pos)


if __name__ == "__main__":                                    # pragma: no cover
    unittest.main()
