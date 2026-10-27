"""Signal-level cross-validation: `app.strategy` (the live engine) vs
`scripts/backtest_engine` (a second, independent implementation of the same
rules).

This is the port of the previous system's `scripts/xvalidate.py`, with two
deliberate changes:

* **Data source: Lighter instead of MEXC/Binance.** The old tool pulled 4h
  candles from `contract.mexc.com` (BTC/ETH) and `data-api.binance.vision`
  (SOL, longer history). This system trades on Lighter, so it validates on
  Lighter's own 4h candles — the venue the bot will actually see. The `candles`
  endpoint returns at most 500 bars per call, so the fetch pages backwards with
  `end_timestamp = earliest_seen_t - 1` until the requested floor is reached
  (Lighter's first 4h bar is 2025-01-17T08:00Z).
* **Backtest engine: in-repo instead of an agent skill path.** The old tool
  loaded `finance/strategy-backtest/scripts/ma_trend_backtest.py` from a skill
  directory with an `exec`-truncation trick. That path does not exist in this
  repository, in CI or in the container, so the two primitives it needed live in
  `scripts/backtest_engine.py` (stdlib only, no pandas).

The property under test is unchanged and is the reason the tool exists: for the
same bars, the two engines must emit the *same set of signals* — same bar open
time, same direction, same stop price. A single extra, missing or shifted signal
means the live code and the backtest disagree, which voids every backtest
conclusion.

Usage:
    python3 scripts/xvalidate.py                          # BTC, ETH, SOL from Lighter
    python3 scripts/xvalidate.py --symbols BTC,SOL
    python3 scripts/xvalidate.py --cache-dir .cache       # reuse cached bars if present
    python3 scripts/xvalidate.py --bars-from-file bars.json   # fully offline
    python3 scripts/xvalidate.py --market-ids BTC=1,ETH=0,SOL=2

Exit code: 0 = every symbol identical, 1 = divergence or a symbol that could not
be validated (including too little history), 2 = nothing reached validation at
all (fetch failed, empty series).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.strategy import (                       # noqa: E402
    MIN_BARS_REQUIRED, Bar, compute_indicators, scan, squeeze_flags,
)
# 取数逻辑的唯一实现是 `app.market`（T3 从这里上提）。xvalidate 只 import，
# 不再自带一份 —— 否则「交叉验证通过」可能只是两份取数代码互相自洽。
from app.market import (                         # noqa: E402
    EARLIEST_MS, FALLBACK_MARKET_IDS, MAINNET_API, MAX_PAGES, PAGE_LIMIT,
    FeedError, bars_from_rows, default_client, drop_unclosed, fmt_ms,
)
from scripts.backtest_engine import (            # noqa: E402
    REGIME_MA, build_frame, signals as engine_signals, squeeze_pct_flags,
)

API = MAINNET_API
CANDLES_URL = f"{API}/candles"           # kept for anyone grepping the endpoint
DETAILS_URL = f"{API}/orderBookDetails"

STOP_MIN, STOP_MAX = 0.015, 0.10
DEFAULT_SYMBOLS = "BTC,ETH,SOL"


# ------------------------------------------------------------------ network

def resolve_market_ids(timeout: float = 20.0) -> Dict[str, int]:
    """symbol -> perp market_id from `orderBookDetails`; empty if unreachable.

    Thin view over `app.market.default_client().market_details()` — two extra
    fields (`price_decimals` …) are dropped here because the validator only
    needs the id.
    """
    try:
        details = default_client().market_details()
    except FeedError as exc:                     # network down / blocked / bad payload
        print(f"WARNING: orderBookDetails unavailable ({exc}); falling back to the "
              f"documented market ids {FALLBACK_MARKET_IDS}", file=sys.stderr)
        return {}
    return {symbol: info.market_id for symbol, info in details.items()}


def fetch_lighter(market_id: int, since_ms: int = EARLIEST_MS,
                  now_ms: Optional[int] = None) -> List[dict]:
    """Page Lighter's 4h candles back to ``since_ms``; returns raw rows ascending.

    Delegates to `app.market.MarketClient.fetch_rows` (rate-limited + retried);
    see that method for the measured paging contract.
    """
    return default_client().fetch_rows(market_id, since_ms=since_ms, now_ms=now_ms,
                                       page_limit=PAGE_LIMIT, max_pages=MAX_PAGES)


# --------------------------------------------------------------------- bars

# `_bars_from_lighter_rows` / `drop_unclosed` / `_fmt` are re-exported from
# `app.market` on purpose: the validator must judge the live engine on exactly
# the bars the live engine would see, so it may not carry its own converter.
_bars_from_lighter_rows = bars_from_rows


def load_bars_file(path: str) -> Dict[str, List[dict]]:
    """Read bars from JSON: either raw candle rows, or ``{symbol: rows}``."""
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        return {"*": payload}
    if isinstance(payload, dict):
        if "c" in payload:                       # a saved API response
            return {"*": payload["c"]}
        if "candles" in payload:                 # {"symbol": ..., "candles": [...]}
            return {str(payload.get("symbol", "*")).upper(): payload["candles"]}
        return {str(k).upper(): v for k, v in payload.items()}
    raise ValueError(f"{path}: expected a candle list or a symbol -> candles mapping")


_fmt = fmt_ms


# --------------------------------------------------------------- validation

def validate(symbol: str, bars: List[Bar]) -> bool:
    """Compare both engines on one symbol's bars. Returns True if identical."""
    print(f"\n=== {symbol}  {len(bars)} 根  {_fmt(bars[0].open_ms)} → {_fmt(bars[-1].open_ms)} ===")
    if len(bars) < MIN_BARS_REQUIRED:
        print(f"  ❌ K 线只有 {len(bars)} 根，不足 {MIN_BARS_REQUIRED} 根"
              f"（250 根分位窗口 + 120 根预热 + 6 根连续）")
        return False

    # A) live engine (long + short)
    live = scan(bars, symbol=symbol, sides=(1, -1))
    live_map = {(s.bar_open_ms, s.side): (s.entry_price, s.stop_price) for s in live}
    n_long = sum(1 for s in live if s.side == 1)
    print(f"  实盘引擎：多 {n_long} 笔 / 空 {len(live) - n_long} 笔")

    # B) reference engine on the same bars
    frame = build_frame(bars)
    sq = squeeze_pct_flags(frame)
    open_, close = frame["open"], frame["close"]
    sma200, open_ms = frame["sma200"], frame["open_ms"]
    bt_map: Dict[tuple, tuple] = {}
    for (t, side, stop) in engine_signals(frame, sq, "breakout"):
        entry = float(open_[t + 1])
        dist = abs(entry - stop) / entry
        if not (STOP_MIN <= dist <= STOP_MAX):
            continue
        if side == -1 and (sma200[t] is None or close[t] >= sma200[t]):
            continue
        bt_map[(int(open_ms[t]), side)] = (entry, stop)
    bt_long = sum(1 for key in bt_map if key[1] == 1)
    print(f"  回测引擎：多 {bt_long} 笔 / 空 {len(bt_map) - bt_long} 笔")

    only_live = sorted(set(live_map) - set(bt_map))
    only_bt = sorted(set(bt_map) - set(live_map))
    print(f"  仅实盘有: {len(only_live)}  仅回测有: {len(only_bt)}")
    for key in only_live[:5]:
        print(f"    LIVE-ONLY {_fmt(key[0])} side {key[1]:+d}")
    for key in only_bt[:5]:
        print(f"    BT-ONLY   {_fmt(key[0])} side {key[1]:+d}")

    common = sorted(set(live_map) & set(bt_map))
    d_entry = max((abs(live_map[k][0] - bt_map[k][0]) for k in common), default=0.0)
    d_stop = max((abs(live_map[k][1] - bt_map[k][1]) for k in common), default=0.0)
    print(f"  共同信号 {len(common)} 笔；入场价最大差 {d_entry:.2e}，止损价最大差 {d_stop:.2e}")

    same = not only_live and not only_bt and d_entry < 1e-9 and d_stop < 1e-9
    print(f"  → {'✅ 信号集合逐笔完全一致' if same else '❌ 存在差异，必须先修'}")

    # Indicators: the same numbers, computed twice (also shows how far the
    # reference engine's arithmetic sits from the live one).
    ind = compute_indicators(bars)
    live_sq = squeeze_flags(ind["band"])
    sq_diff = sum(1 for a, b in zip(live_sq, sq) if a != b)
    diffs: List[float] = []
    for i in range(len(bars)):
        if ind["band"][i] is None or frame["band"][i] is None:
            continue
        spread = abs(ind["band"][i] - frame["band"][i])
        if ind["sma200"][i] is not None and sma200[i] is not None:
            spread = max(spread, abs(ind["sma200"][i] - sma200[i]))
        diffs.append(spread)
    print(f"  band/sma200 最大绝对差 = {max(diffs) if diffs else 0.0:.3e}；"
          f"密集标记差异根数 = {sq_diff}")
    return same


def _parse_market_ids(raw: Optional[str]) -> Dict[str, int]:
    if not raw:
        return {}
    out: Dict[str, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        symbol, _, value = chunk.partition("=")
        if not symbol or not value:
            raise SystemExit(f"--market-ids: expected SYMBOL=ID, got {chunk!r}")
        out[symbol.strip().upper()] = int(value)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Lighter 4h K 线：实盘信号引擎 vs 回测引擎 逐笔交叉验证")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS,
                    help=f"逗号分隔（默认 {DEFAULT_SYMBOLS}）")
    ap.add_argument("--market-ids", default=None,
                    help="覆盖 market_id，如 BTC=1,ETH=0,SOL=2（默认查 orderBookDetails）")
    ap.add_argument("--bars-from-file", default=None,
                    help="本地 bar JSON（原始 candle 行，或 {symbol: rows}），完全不联网")
    ap.add_argument("--cache-dir", default=None,
                    help="该目录下 <SYMBOL>.json 存在则直接读，否则拉取后写入")
    ap.add_argument("--since", default=None,
                    help="只取该日期之后（YYYY-MM-DD，默认 Lighter 全历史 2025-01-17）")
    ap.add_argument("--keep-open-bar", action="store_true",
                    help="保留尚未收盘的最后一根（默认剔除：未收盘的 close 不是 close）")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("--symbols is empty")

    override_ids = _parse_market_ids(args.market_ids)
    since_ms = EARLIEST_MS
    if args.since:
        since_ms = int(dt.datetime.strptime(args.since, "%Y-%m-%d")
                       .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

    file_bars: Optional[Dict[str, List[dict]]] = None
    if args.bars_from_file:
        file_bars = load_bars_file(args.bars_from_file)

    # Resolved lazily: a run served entirely from cache must not touch the network.
    market_ids: Optional[Dict[str, int]] = None

    ok_all = True
    validated = 0
    for symbol in symbols:
        rows: Optional[List[dict]] = None

        if file_bars is not None:
            rows = file_bars.get(symbol) or file_bars.get("*")
            if rows is None:
                print(f"\n=== {symbol} ===\n  ❌ {args.bars_from_file} 里没有 {symbol} 的 K 线")
                ok_all = False
                continue
        else:
            cache_path = os.path.join(args.cache_dir, f"{symbol}.json") if args.cache_dir else None
            if cache_path and os.path.isfile(cache_path):
                cached = load_bars_file(cache_path)
                rows = cached.get("*") or cached.get(symbol)
                if rows is not None:
                    print(f"（{symbol} 使用缓存 {cache_path}）")
            if rows is None:
                if market_ids is None:
                    market_ids = resolve_market_ids()
                market_id = (override_ids.get(symbol) or market_ids.get(symbol)
                             or FALLBACK_MARKET_IDS.get(symbol))
                if market_id is None:
                    print(f"\n=== {symbol} ===\n  ❌ 找不到 market_id（orderBookDetails 无此标的，"
                          f"也没有 --market-ids 覆盖）")
                    ok_all = False
                    continue
                source = ("--market-ids" if symbol in override_ids else
                          "orderBookDetails" if symbol in market_ids else "fallback map")
                print(f"（{symbol} market_id={market_id} 来自 {source}）")
                try:
                    rows = fetch_lighter(market_id, since_ms)
                except (FeedError, OSError) as exc:
                    print(f"\n=== {symbol} ===\n  ❌ 拉取 Lighter K 线失败：{exc}")
                    ok_all = False
                    continue
                if cache_path:
                    os.makedirs(args.cache_dir, exist_ok=True)
                    with open(cache_path, "w", encoding="utf-8") as handle:
                        json.dump(rows, handle)
            if not rows:
                print(f"\n=== {symbol} ===\n  ❌ 没有取到任何 K 线")
                ok_all = False
                continue

        bars = _bars_from_lighter_rows(rows)
        bars = [b for b in bars if b.open_ms >= since_ms]
        if not args.keep_open_bar:
            bars = drop_unclosed(bars)
        if not bars:
            print(f"\n=== {symbol} ===\n  ❌ 过滤后没有 K 线")
            ok_all = False
            continue

        validated += 1
        ok_all &= validate(symbol, bars)

    print()
    if validated == 0:
        print("❌ 没有标的被验证（历史不足或取数失败）")
        return 2
    print("总体:", "✅ 实盘引擎与回测引擎口径一致（含做空）" if ok_all
          else "❌ 存在差异，必须先修")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
