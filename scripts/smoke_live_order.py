#!/usr/bin/env python3
"""一次**真实**的最小下单往返 —— 只用来打穿写路径，不是策略的一部分。

为什么需要它：T7/T8 的 814 个单测全部离线（「只签名不提交」），部署验收也只验证了
读路径与 nonce/auth。也就是说「签名 → sendTx → 挂止损 → 撤单 → 平仓」这条真钱链路
在第一次真实信号之前从未被执行过；而 403/20558/代理注入失效这类问题**只在真实调用里
才看得见**。本脚本用一个远小于正常仓位的名义（默认 ~12 USDC，交易所最小额附近）
把整条链路走一遍，并在任何失败点尽力收尾（撤单 + 市价平掉）。

安全边界（全部硬编码，不做成可调参数）：

* 必须显式 `--confirm-live <account_index>`，且等于配置里的 account_index；
* 只做 **一个标的、多方向、单笔**，名义上限 `--notional`（默认 12，硬上限 25）；
* 开始前账户必须**空仓、无挂单**，否则拒绝（不碰任何已有风险）；
* 结束时必须是**空仓 + 零挂单**，否则以非零码退出并要求人工处理。

用法（在容器内跑，代理与凭据都来自环境变量）：

    docker exec -i -u bot lighter-ma-bot python /srv/scripts/smoke_live_order.py \
        --confirm-live 764377

    # 只签名、不发请求（先看订单长什么样）：
    ... --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config                       # noqa: E402
from app.exchange import (                          # noqa: E402
    ExchangeClient, LiveTradingDisabled, NotConfigured, OrderResult,
)
from app.executor import LighterAccountReader      # noqa: E402
from app.market import MarketClient                # noqa: E402
from app.sizing import OrderPlan                   # noqa: E402
from app import symbol_registry                    # noqa: E402

HARD_MAX_NOTIONAL = 25.0        # 硬上限：本脚本只做最小往返，不允许被拿来当交易工具
STOP_DISTANCE = 0.03            # 3% 止损距离（与策略口径无关，只为验证挂单/撤单）
DEFAULT_NOTIONAL = 12.0         # SOL min_quote ≈ 10 USDC


def _money(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.4f}"


@dataclass
class Step:
    name: str
    ok: bool
    detail: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {"step": self.name, "ok": self.ok, **self.detail}


def _plan(symbol: str, price: float, notional: float, equity: float,
          market_id: int) -> OrderPlan:
    """按「最小合法数量」构造 OrderPlan（不走策略定仓：这里只验证链路）。

    精度一律向下取整（与 `app/sizing` 同一约定）；若 floor 后低于交易所最小量，
    往上补到 min_base —— 这台脚本的名义是硬上限保护，补量不会失控。
    """
    spec = symbol_registry.spec_strict(symbol)
    pdec, sdec = int(spec.price_decimals), int(spec.size_decimals)
    scale = 10 ** sdec
    qty_float = notional / price
    qty_int = int(qty_float * scale)                       # floor
    min_base_int = int(round(float(spec.min_base_amount) * scale))
    min_quote_int = int(round(float(spec.min_quote_amount) * scale / price))
    qty_int = max(qty_int, min_base_int, min_quote_int)
    base_qty = qty_int / scale
    stop_price = round(price * (1 - STOP_DISTANCE), pdec)
    stop_price_int = int(round(stop_price * (10 ** pdec)))
    max_lev = float(spec.exchange_max_leverage or 5.0)
    return OrderPlan(
        symbol=spec.symbol,
        market_id=int(market_id),
        side=1,
        entry_price=float(price),
        stop_price=float(stop_price),
        stop_price_int=stop_price_int,
        qty_int=qty_int,
        base_qty=base_qty,
        notional=base_qty * price,
        margin=(base_qty * price) / 5.0,
        leverage=5.0,
        exchange_max_leverage=max_lev,
        risk_amount=base_qty * abs(price - stop_price),
        risk_amount_target=0.0,
        stop_dist=STOP_DISTANCE,
        price_decimals=pdec,
        size_decimals=sdec,
        equity=equity,
        warnings=("smoke round-trip: 名义为单位数最小额，不是策略仓位",),
    )


async def _run(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    print(json.dumps({
        "config": {"base_url": cfg.base_url, "account_index": cfg.account_index,
                   "api_key_index": cfg.api_key_index,
                   "environment": cfg.environment,
                   "proxy": bool(cfg.proxy_url)},
        "mode": "dry-run" if args.dry_run else "LIVE",
        "symbol": args.symbol, "notional_cap": args.notional,
    }, ensure_ascii=False))

    if not args.dry_run and int(args.confirm_live) != int(cfg.account_index):
        print(json.dumps({"ok": False, "error": "confirm-live 与配置的 account_index 不一致，"
                                                "拒绝真实下单"}, ensure_ascii=False))
        return 2

    market = MarketClient(cfg.environment)
    market_id = market.market_id_of(args.symbol)
    book = market.order_book(args.symbol, depth=5)
    mid = book.mid
    if not mid or mid <= 0:
        print(json.dumps({"ok": False, "error": "拿不到盘口 mid，拒绝在无价状态下下单"}))
        return 2

    exchange = ExchangeClient.from_config(
        cfg, env=os.environ, dry_run=bool(args.dry_run),
        confirm_live=int(args.confirm_live) if not args.dry_run else None)
    reader = LighterAccountReader(base_url=cfg.base_url,
                                  account_index=cfg.account_index,
                                  auth_provider=lambda: exchange.auth.get())
    steps = []

    def emit(step: Step) -> None:
        steps.append(step)
        print(json.dumps(step.as_dict(), ensure_ascii=False))

    try:
        account = await reader.read()
        emit(Step("precheck_account", bool(account.equity), {
            "equity": account.equity, "positions": [p.symbol for p in account.positions],
            "orders": len(account.orders), "orders_error": account.orders_error}))
        if not args.dry_run and (account.positions or account.orders):
            print(json.dumps({"ok": False,
                              "error": "账户已有持仓或挂单，最小往返测试拒绝执行"}))
            return 3

        plan = _plan(args.symbol, float(mid), float(args.notional),
                     float(account.equity or 0.0), market_id)
        emit(Step("plan", plan.notional <= HARD_MAX_NOTIONAL, plan.as_dict()))
        if plan.notional > HARD_MAX_NOTIONAL:
            print(json.dumps({"ok": False, "error": f"名义 {plan.notional:.2f} 超过硬上限 "
                                                    f"{HARD_MAX_NOTIONAL}"}))
            return 2

        entry: OrderResult = await exchange.place_entry(plan, bar_ms=int(time.time() // 240) * 240_000)
        emit(Step("place_entry", entry.ok, entry.as_dict()))
        if not entry.ok:
            return 4
        opened = False

        if not args.dry_run:
            ok, account, waited = await _wait_for(
                reader.read, lambda a: a.position_for(plan.symbol) is not None)
            opened = ok
            emit(Step("read_position", ok,
                      {"waited_s": waited,
                       "positions": [p.as_dict() for p in account.positions],
                       "orders": len(account.orders)}))
            if not ok:
                print(json.dumps({"ok": False,
                                  "error": "开仓 tx 已发出但 15s 内账户里没有仓位："
                                           "要么未成交、要么账本不一致，需人工核对"}))
                return 5

        stop: OrderResult = await exchange.place_stop(plan, bar_ms=int(time.time() // 240) * 240_000)
        emit(Step("place_stop", stop.ok, stop.as_dict()))
        if not stop.ok:
            raise RuntimeError("止损挂单失败")

        if not args.dry_run:
            # 实测：`accountActiveOrders` 对新挂单有秒级传播延迟（2s 时读到空列表，
            # 半分钟后才出现）⇒ 一次性读会误判「止损没挂上」，必须轮询。
            ok, account, waited = await _wait_for(
                reader.read, lambda a: any(o.reduce_only for o in a.orders))
            emit(Step("verify_stop_resting", ok,
                      {"waited_s": waited,
                       "orders": [o.as_dict() for o in account.orders]}))
            if not ok:
                raise RuntimeError("15s 内止损单没有出现在挂单列表里")

        cancel: OrderResult = await exchange.cancel_all_orders()
        emit(Step("cancel_all", cancel.ok, cancel.as_dict()))

        if not args.dry_run:
            ok, account, waited = await _wait_for(reader.read, lambda a: not a.orders)
            emit(Step("verify_orders_cleared", ok,
                      {"waited_s": waited, "orders": len(account.orders)}))
            if not ok:
                raise RuntimeError("撤单后仍有挂单")

        exit_result: OrderResult = await exchange.place_exit(
            symbol=plan.symbol, market_id=plan.market_id, side=plan.side,
            qty_int=plan.qty_int, price_decimals=plan.price_decimals,
            exit_price=float(mid), bar_ms=int(time.time() // 240) * 240_000)
        emit(Step("place_exit", exit_result.ok, exit_result.as_dict()))

        if not args.dry_run:
            ok, account, waited = await _wait_for(reader.read, lambda a: not a.positions)
            emit(Step("verify_flat", ok,
                      {"waited_s": waited,
                       "positions": [p.symbol for p in account.positions],
                       "equity": account.equity}))
            if not ok:
                raise RuntimeError("平仓后仍有持仓")

        ended_clean = all(s.ok for s in steps) if not args.dry_run else True
        print(json.dumps({"ok": ended_clean, "steps": len(steps),
                          "tx_hashes": [s.as_dict().get("tx_hash") for s in steps
                                        if s.as_dict().get("tx_hash")]},
                         ensure_ascii=False))
        return 0 if ended_clean else 6
    except Exception as exc:                               # noqa: BLE001 - 先收尾再报错
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}",
                          "cleanup": "开始收尾（撤单 + 平仓）"}, ensure_ascii=False))
        if not args.dry_run:
            await _emergency_flat(exchange, reader, plan)
        raise
    finally:
        try:
            await exchange.aclose()
        except Exception:                                  # noqa: BLE001
            pass


async def _wait_for(read, predicate, *, timeout_s: float = 15.0,
                    interval: float = 0.5):
    """轮询交易所读数直到 `predicate` 成立；返回 `(ok, 最后一次读数, 等待秒数)`。

    为什么必须轮询：`accountActiveOrders` 对新挂单有秒级传播延迟（实测 2s 时读不到、
    半分钟后才出现）。一次性读会把「还没传播到」误判成「没挂上」。
    """
    started = time.time()
    while True:
        snapshot = await read()
        if predicate(snapshot):
            return True, snapshot, round(time.time() - started, 1)
        if time.time() - started >= timeout_s:
            return False, snapshot, round(time.time() - started, 1)
        await asyncio.sleep(interval)


async def _emergency_flat(exchange, reader, plan) -> None:
    """失败路径：撤掉所有挂单，再把仓位按对手价市价平掉。**尽力而为**。

    这条路径是给「止损没挂上就报错退出」这种情况兜底的 —— 没有它，脚本一报错就会把
    一个裸仓留在交易所（第一次实跑就是这么留下 0.117 SOL 的）。
    """
    try:
        account = await reader.read()
    except Exception as exc:                               # noqa: BLE001
        print(json.dumps({"cleanup": "failed", "error": f"读取账户失败：{exc}"},
                         ensure_ascii=False))
        return
    position = account.position_for(plan.symbol) if plan else None
    if account.orders:
        try:
            res = await exchange.cancel_all_orders()
            print(json.dumps({"cleanup": "cancel_all", "ok": res.ok,
                              "error": res.error}, ensure_ascii=False))
        except Exception as exc:                           # noqa: BLE001
            print(json.dumps({"cleanup": "cancel_all failed", "error": str(exc)},
                             ensure_ascii=False))
    if position is None:
        print(json.dumps({"cleanup": "flat", "note": "没有持仓，无需平仓"},
                         ensure_ascii=False))
        return
    try:
        from app.market import MarketClient as _MC
        cfg = Config.from_env()
        mid = _MC(cfg.environment).order_book(position.symbol, depth=5).mid
        res = await exchange.place_exit(
            symbol=position.symbol, market_id=int(position.market_id or plan.market_id),
            side=position.side, qty_int=int(round(float(position.qty) * 10 ** int(plan.size_decimals))),
            price_decimals=int(plan.price_decimals), exit_price=float(mid),
            bar_ms=int(time.time() // 240) * 240_000)
        print(json.dumps({"cleanup": "exit", "ok": res.ok, "error": res.error,
                          "tx_hash": res.tx_hash}, ensure_ascii=False))
    except Exception as exc:                               # noqa: BLE001
        print(json.dumps({"cleanup": "exit failed",
                          "error": f"{type(exc).__name__}: {exc}",
                          "manual": f"手工平掉 {position.symbol} {position.side} {position.qty}"},
                         ensure_ascii=False))


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="最小真实下单往返（写路径冒烟）")
    parser.add_argument("--symbol", default="SOL")
    parser.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL,
                        help=f"名义 USDC（默认 {DEFAULT_NOTIONAL}，硬上限 {HARD_MAX_NOTIONAL}）")
    parser.add_argument("--confirm-live", type=str, default="",
                        help="必须等于配置的 account_index，否则拒绝真实下单")
    parser.add_argument("--dry-run", action="store_true", help="只签名、不发请求")
    args = parser.parse_args(argv)
    if not args.dry_run and not args.confirm_live:
        parser.error("真实下单必须显式给 --confirm-live <account_index>")
    try:
        return asyncio.run(_run(args))
    except LiveTradingDisabled as exc:
        print(json.dumps({"ok": False, "error": f"live gate 拒绝：{exc}"}, ensure_ascii=False))
        return 7
    except NotConfigured as exc:
        print(json.dumps({"ok": False, "error": f"配置/凭据问题：{exc}"}, ensure_ascii=False))
        return 8
    except Exception as exc:                               # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
