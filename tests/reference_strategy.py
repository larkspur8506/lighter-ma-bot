"""Independent reference implementation of the frozen 6MA signal rules.

**What this is.** A second, hand-written implementation of the rules that
`app/strategy.py` encodes, written from the rule text alone (the module
docstring of the frozen strategy + `docs/STRATEGY_PORT.md`). It was *not*
copied from `app/strategy.py`, and it lives in `tests/` on purpose: its only
job is to give `tests/test_strategy_port.py` something that can disagree.

**Why it exists.** `app/strategy.py` is a byte-for-byte copy of the signal
engine that produced 700 backtested trades and is live on Hyperliquid. It must
never be edited, so the usual "write a test that pins the code" trick is not
available — a test that imports the module and compares it to itself proves
nothing. Two independent things are therefore asserted in the test module:

1. this reimplementation and `app.strategy` produce the same signals on the
   same bars (catches semantic drift in either one), and
2. a hard-coded table of concrete signals (bar index / direction / entry /
   stop) is reproduced exactly (catches a change that flips both
   implementations at once, e.g. edited constants on both sides).

**Rules implemented** (frozen v2, 2026-10-12, long + short):

- six means of the close: SMA20/60/120 and EMA20/60/120; band = (max - min)/close
- dense ("squeeze") = band <= the 20th percentile of the last 250 bands,
  holding for at least 6 consecutive bars
- entry: close > previous bar's six-mean max -> long; close < previous bar's
  six-mean min (and close < SMA200) -> short
- fill / entry price = next bar's open
- stop = *this* bar's six-mean min (long) / max (short); the stop distance must
  land inside [1.5%, 10%] or the signal is dropped

**Never edit this file to make a failing test pass.** If it disagrees with
`app/strategy.py`, `app/strategy.py` is the thing under test and the frozen
file wins; the disagreement is a bug in this file (or a real port regression).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

SMA_WINDOWS = (20, 60, 120)
EMA_WINDOWS = (20, 60, 120)
BAND_WINDOW = 250
BAND_QUANTILE = 0.20
MIN_SQUEEZE_BARS = 6
STOP_MIN = 0.015
STOP_MAX = 0.10
REGIME_MA = 200


# --------------------------------------------------------------- indicators

def sma(values: Sequence[float], n: int) -> List[Optional[float]]:
    """Mean of the trailing ``n`` values; ``None`` until ``n`` values exist."""
    out: List[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if i + 1 < n:
            continue
        window = values[i - n + 1:i + 1]
        out[i] = sum(window) / n
    return out


def ema(values: Sequence[float], n: int) -> List[Optional[float]]:
    """Exponential mean seeded with the first value (pandas ``ewm(adjust=False)``)."""
    out: List[Optional[float]] = [None] * len(values)
    if not values:
        return out
    alpha = 2.0 / (n + 1.0)
    acc = float(values[0])
    out[0] = acc
    for i in range(1, len(values)):
        acc = alpha * float(values[i]) + (1.0 - alpha) * acc
        out[i] = acc
    return out


def quantile(values: Sequence[float], q: float) -> Optional[float]:
    """Linearly interpolated quantile (pandas ``Series.quantile`` convention)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    weight = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * weight


def band_block(closes: Sequence[float]) -> Tuple[List[Optional[float]],
                                                List[Optional[float]],
                                                List[Optional[float]]]:
    """Return (ma_max, ma_min, band) aligned with ``closes``."""
    n = len(closes)
    curves: List[List[Optional[float]]] = [sma(closes, w) for w in SMA_WINDOWS]
    curves += [ema(closes, w) for w in EMA_WINDOWS]

    ma_max: List[Optional[float]] = [None] * n
    ma_min: List[Optional[float]] = [None] * n
    band: List[Optional[float]] = [None] * n
    for i in range(n):
        at_i = [curve[i] for curve in curves]
        if any(v is None for v in at_i):
            continue
        top, bottom = max(at_i), min(at_i)          # type: ignore[type-var]
        ma_max[i], ma_min[i] = top, bottom
        if closes[i] > 0:
            band[i] = (top - bottom) / closes[i]
    return ma_max, ma_min, band


def squeeze_flags(band: Sequence[Optional[float]], window: int = BAND_WINDOW,
                  q: float = BAND_QUANTILE, need: int = MIN_SQUEEZE_BARS) -> List[bool]:
    """True where the band has sat inside its own ``q`` quantile for ``need`` bars."""
    n = len(band)
    single = [False] * n
    for i in range(window - 1, n):
        if band[i] is None:
            continue
        history = [band[j] for j in range(i - window + 1, i + 1) if band[j] is not None]
        if len(history) != window:
            continue
        threshold = quantile(history, q)
        single[i] = threshold is not None and band[i] <= threshold

    flags = [False] * n
    streak = 0
    for i in range(n):
        streak = streak + 1 if single[i] else 0
        flags[i] = streak >= need
    return flags


# ------------------------------------------------------------------ signals

def find_signals(bars, symbol: str = "", start: int = 0, sides=(1,),
                 short_regime_ma: int = REGIME_MA,
                 stop_min: float = STOP_MIN, stop_max: float = STOP_MAX,
                 ) -> List[Tuple[int, int, int, float, float, float]]:
    """Return ``[(bar_index, bar_open_ms, side, entry, stop, dist), ...]``.

    ``side`` is +1 long / -1 short; ``entry`` is the next bar's open and
    ``stop`` the current bar's band edge, exactly as the live rule defines it.
    """
    closes = [float(b.close) for b in bars]
    ma_max, ma_min, band = band_block(closes)
    squeeze = squeeze_flags(band)
    regime: Optional[List[Optional[float]]] = None

    found: List[Tuple[int, int, int, float, float, float]] = []
    for i in range(max(start, 1), len(bars) - 1):
        if not squeeze[i - 1]:
            continue
        if ma_max[i - 1] is None or ma_min[i - 1] is None or band[i - 1] is None:
            continue

        px = float(bars[i + 1].open)
        if px <= 0:
            continue
        close = closes[i]

        if 1 in sides and close > ma_max[i - 1]:        # type: ignore[operator]
            side = 1
        elif -1 in sides and close < ma_min[i - 1]:     # type: ignore[operator]
            side = -1
            if short_regime_ma:
                if regime is None:
                    regime = sma(closes, short_regime_ma)
                ref = regime[i] if i < len(regime) else None
                if ref is None or close >= ref:
                    continue
        else:
            continue

        stop = ma_min[i] if side == 1 else ma_max[i]
        if stop is None:
            continue
        if (side == 1 and stop >= px) or (side == -1 and stop <= px):
            continue
        dist = abs(px - stop) / px
        if not (stop_min <= dist <= stop_max):
            continue
        found.append((i, int(bars[i].open_ms), side, px, float(stop), dist))
    return found
