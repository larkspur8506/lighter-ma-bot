#!/usr/bin/env python3
"""离线探针：拉一次真实 Lighter 数据，把 `app/market.py` 的自检结果打出来。

存在的意义是「一条命令看出数据层是否健康」，不依赖 DB、不依赖签名器、不下单：

    python3 scripts/probe_market.py                       # BTC/ETH/SOL，最近 500 根
    python3 scripts/probe_market.py --symbols BTC --depth 5
    python3 scripts/probe_market.py --bars 1200 --env testnet
    python3 scripts/probe_market.py --json                # 给脚本/CI 消费

打印内容（每个标的）：K 线根数与时间范围、翻页次数、被剔除的未收盘 bar、缺口、
最后一根已收盘 bar 的「年龄」（新鲜度）、当前价与其口径、盘口前 N 档与 spread、
标记价/指数价/最后成交价、下单所需的交易规则（小数位/最小量/保证金）。

退出码：0 = 全部标的健康；1 = 有标的拉取失败或数据陈旧（STALE）；2 = 参数错误。
缺口（gap）只报告不改退出码 —— Lighter 偶发缺根是事实，是否致命由上层决定。

注意：探针**不会**用旧数据凑数。拿不到就是拿不到，宁可红着退。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.market import (                                          # noqa: E402
    BAR_MS, BASE_URLS, DEFAULT_ENVIRONMENT, EARLIEST_MS, FRESH_MAX_AGE_MS, FeedError,
    MarketClient, fmt_ms,
)

DEFAULT_SYMBOLS = "BTC,ETH,SOL"


def _age_h(ms: int) -> float:
    return ms / 3_600_000.0


def _num(value: Optional[float]) -> str:
    """人类可读的数字：去掉浮点尾巴（0.09999999999126885 -> 0.1）。"""
    if value is None:
        return "n/a"
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def probe_symbol(client: MarketClient, symbol: str, *, bars_wanted: int,
                 depth: int) -> Dict[str, Any]:
    """拉一个标的的全部信息，返回可直接 JSON 化的 dict。异常向上抛。"""
    now = int(client._now_ms())                                  # noqa: SLF001 - 探针看内部时钟
    since = max(EARLIEST_MS, now - bars_wanted * BAR_MS)
    # 先用「不严」的方式拿全量：陈旧时也要能看到内容，是否致命由本函数自己判。
    result = client.bars(symbol, since_ms=since, now_ms=now, strict_freshness=False)
    snapshot = client.snapshot(symbol, depth=depth)

    age = result.last_close_age_ms
    stale = age > FRESH_MAX_AGE_MS
    info = snapshot.info
    return {
        "symbol": symbol,
        "market_id": result.market_id,
        "bars": len(result.bars),
        "first": fmt_ms(result.bars[0].open_ms),
        "last": fmt_ms(result.bars[-1].open_ms),
        "last_close": fmt_ms(result.bars[-1].open_ms + BAR_MS),
        "last_close_age_h": round(_age_h(age), 3),
        "fresh": not stale,
        "asked_bars": bars_wanted,
        "pages": result.pages,
        "rows_fetched": result.rows_fetched,
        "dropped_unclosed": [fmt_ms(b.open_ms) for b in result.dropped_unclosed],
        "gaps": [{"after": fmt_ms(g.after_ms), "before": fmt_ms(g.before_ms),
                  "missing": g.missing} for g in result.gaps],
        "price": snapshot.price,
        "price_source": snapshot.price_source,
        "best_bid": snapshot.best_bid,
        "best_ask": snapshot.best_ask,
        "spread": snapshot.spread,
        "spread_bps": None if snapshot.spread_bps is None else round(snapshot.spread_bps, 3),
        "mark_price": snapshot.mark_price,
        "index_price": snapshot.index_price,
        "last_trade_price": snapshot.last_trade_price,
        "levels": {
            "bids": [{"price": l.price, "size": l.size} for l in snapshot.bids],
            "asks": [{"price": l.price, "size": l.size} for l in snapshot.asks],
        },
        "rules": None if info is None else {
            "price_decimals": info.price_decimals,
            "size_decimals": info.size_decimals,
            "min_base_amount": info.min_base_amount,
            "min_quote_amount": info.min_quote_amount,
            "min_initial_margin_pct": info.min_initial_margin_fraction / 100.0,
            "maintenance_margin_pct": info.maintenance_margin_fraction / 100.0,
            "taker_fee": info.taker_fee,
            "maker_fee": info.maker_fee,
            "is_frozen": info.is_frozen,
        },
        "warnings": list(result.warnings),
        "stale_threshold_h": _age_h(FRESH_MAX_AGE_MS),
    }


def print_symbol(data: Dict[str, Any], depth: int) -> None:
    print(f"\n=== {data['symbol']}  market_id={data['market_id']} ===")
    print(f"  K 线      {data['bars']} 根   {data['first']} → {data['last']} UTC"
          f"   （请求 {data['asked_bars']} 根）")
    print(f"  翻页      {data['pages']} 页 / 原始 {data['rows_fetched']} 行")
    state = "新鲜" if data["fresh"] else "STALE"
    print(f"  新鲜度    最后一根收盘 {data['last_close']}，距今 "
          f"{data['last_close_age_h']:.2f}h（阈值 {data['stale_threshold_h']:.0f}h）"
          f"  → {state}")
    if data["dropped_unclosed"]:
        print(f"  剔除未收盘 {'、'.join(data['dropped_unclosed'])}  "
              f"（这根还在形成，close 不是收盘价）")
    else:
        print("  剔除未收盘 （无：最后一根已收盘）")
    if data["gaps"]:
        for gap in data["gaps"]:
            print(f"  缺口      {gap['after']} → {gap['before']}  缺 {gap['missing']} 根")
    else:
        print("  缺口      无（相邻间隔均为 4h）")

    spread_bps = "n/a" if data["spread_bps"] is None else f"{data['spread_bps']:.3f}"
    print(f"  价格      {_num(data['price'])}  口径={data['price_source']}   "
          f"bid {_num(data['best_bid'])} / ask {_num(data['best_ask'])}  "
          f"spread {_num(data['spread'])}（{spread_bps} bps）")
    print(f"  参考      mark {_num(data['mark_price'])}  index {_num(data['index_price'])}  "
          f"last_trade {_num(data['last_trade_price'])}")
    if data["levels"]["bids"] or data["levels"]["asks"]:
        bids, asks = data["levels"]["bids"], data["levels"]["asks"]
        print(f"  盘口前 {depth} 档（按价格聚合，同价位已合并）：")
        for index in range(max(len(bids), len(asks))):
            left = (f"买 {bids[index]['price']:>10g} × {bids[index]['size']:<10g}"
                    if index < len(bids) else "")
            right = (f"卖 {asks[index]['price']:>10g} × {asks[index]['size']:<10g}"
                     if index < len(asks) else "")
            print(f"    {left:<32} | {right}".rstrip())
    rules = data["rules"]
    if rules is None:
        print("  交易规则  取不到（orderBookDetails 不可用）—— T7 会 fail-closed")
    else:
        print(f"  交易规则  price_dec={rules['price_decimals']} "
              f"size_dec={rules['size_decimals']} "
              f"min_base={_num(rules['min_base_amount'])} "
              f"min_quote={_num(rules['min_quote_amount'])} "
              f"初始保证金={rules['min_initial_margin_pct']:.2f}% "
              f"维持保证金={rules['maintenance_margin_pct']:.2f}% "
              f"frozen={rules['is_frozen']}")
    for warning in data["warnings"]:
        print(f"  WARNING   {warning}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lighter 市场数据层探针（K 线/缺口/未收盘剔除/盘口/交易规则）")
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS,
                        help=f"逗号分隔（默认 {DEFAULT_SYMBOLS}）")
    parser.add_argument("--bars", type=int, default=500,
                        help="往回取多少根 4h K 线（默认 500，即一页）")
    parser.add_argument("--depth", type=int, default=5, help="盘口档位（默认 5）")
    parser.add_argument("--env", dest="environment", default=DEFAULT_ENVIRONMENT,
                        choices=sorted(BASE_URLS), help="mainnet / testnet（默认 mainnet）")
    parser.add_argument("--rate", type=int, default=60,
                        help="每分钟最多请求数（默认 60，即 Lighter 公开限流）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给脚本消费）")
    parser.add_argument("--allow-stale", action="store_true",
                        help="数据陈旧时仍以退出码 0 结束（只用于事后补数据）")
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("--symbols is empty", file=sys.stderr)
        return 2
    if args.bars <= 0 or args.depth <= 0:
        print("--bars and --depth must be positive", file=sys.stderr)
        return 2

    client = MarketClient(args.environment, per_minute=args.rate)
    if not args.json:
        now = int(client._now_ms())                              # noqa: SLF001
        print(f"Lighter {args.environment}  {client.api_base}")
        print(f"现在 {dt.datetime.fromtimestamp(now / 1000, dt.timezone.utc):%Y-%m-%d %H:%M}"
              f" UTC   每标的回看 {args.bars} 根 4h")

    results: List[Dict[str, Any]] = []
    failures = 0
    stale = 0
    for symbol in symbols:
        try:
            data = probe_symbol(client, symbol, bars_wanted=args.bars, depth=args.depth)
        except FeedError as exc:
            print(f"\n=== {symbol} ===\n  ❌ 取数失败: {exc}", file=sys.stderr)
            failures += 1
            continue
        results.append(data)
        stale += 0 if data["fresh"] else 1
        if not args.json:
            print_symbol(data, args.depth)

    if args.json:
        print(json.dumps({"environment": args.environment,
                          "base_url": client.api_base,
                          "requests": client.requests,
                          "symbols": results},
                         indent=2, ensure_ascii=False))
    else:
        gaps = sum(len(d["gaps"]) for d in results)
        print(f"\n合计: {len(results)}/{len(symbols)} 个标的成功 · "
              f"{sum(d['bars'] for d in results)} 根已收盘 4h bar · "
              f"{sum(d['pages'] for d in results)} 页 · "
              f"{client.requests} 次请求 · 缺口 {gaps} 处 · "
              f"{'无告警' if not client.warnings else '告警: ' + '；'.join(client.warnings)}")

    if failures:
        print(f"❌ {failures} 个标的取数失败", file=sys.stderr)
        return 1
    if stale and not args.allow_stale:
        print(f"❌ {stale} 个标的数据陈旧（用 --allow-stale 可忽略）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
