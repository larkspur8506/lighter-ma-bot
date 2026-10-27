"""Second-opinion backtest engine — a stdlib port of the pandas engine that
validated the strategy in the previous system.

**What it is.** `finance/strategy-backtest/scripts/ma_trend_backtest.py` (the
pandas engine behind the 700-trade / 6.1-year study) defines the strategy twice
over: `squeeze_pct_flags()` and `signals()`. `scripts/xvalidate.py` used to load
those two functions out of the agent skill directory with an `exec`-truncation
trick. The skill directory is not part of this repository (and does not exist in
CI, in the container, or on a fresh checkout), so the two primitives are
reimplemented here in plain Python.

**Why a reimplementation is acceptable here.** The engine is only ever used as
an *independent* witness: `xvalidate.py` feeds the same bars to `app.strategy`
and to this module and requires identical signals. Deliberately, this module
does **not** import `app.strategy` — sharing an indicator helper would hide
exactly the kind of divergence the tool exists to catch. What *is* carried over
verbatim is the arithmetic of the original engine: rolling windows, the
`ewm(span=n, adjust=False)` seeding, linearly interpolated rolling quantiles,
and the `raw[:win] = False` mask.

**Faithfulness notes (read before "fixing" anything here).**

* The pandas engine was fed a `dropna()`-ed frame, so its index started at the
  first bar with a full SMA120 (bar 119) and `raw[:win] = False` then pushed the
  first possible squeeze one bar further than the live rule allows. The one-bar
  difference is unobservable in practice: `app.strategy` needs six consecutive
  raw flags, and a raw flag is only computable from bar 368, so the first bar at
  which a squeeze can exist at all is 373 while this engine's first is 374 —
  `xvalidate.py` prints a squeeze-flag diff count so the boundary stays visible
  instead of being assumed away.
* Rolling means are re-summed per window rather than accumulated (the live rule
  does the same, and it avoids float drift making "close exactly at the mean"
  flip). A pandas accumulation differs in the last bits only; the tool reports
  the resulting band/SMA200 difference so the reader can see it is ~1e-13.

Everything here is pure stdlib on purpose: the cross-validation tool must run on
the trading host with nothing installed but the interpreter.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

SMA_WINDOWS = (20, 60, 120)
EMA_WINDOWS = (20, 60, 120)
BAND_WINDOW = 250
BAND_QUANTILE = 0.20
MIN_SQUEEZE_BARS = 6
REGIME_MA = 200


# --------------------------------------------------------------- primitives

def rolling_mean(values: Sequence[float], n: int) -> List[Optional[float]]:
    """`df[col].rolling(n).mean()` — ``None`` until the window is full."""
    out: List[Optional[float]] = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = sum(values[i - n + 1:i + 1]) / n
    return out


def ewm_mean(values: Sequence[float], span: int) -> List[Optional[float]]:
    """`df[col].ewm(span=span, adjust=False).mean()` — seeded with the first value."""
    out: List[Optional[float]] = [None] * len(values)
    if not values:
        return out
    alpha = 2.0 / (span + 1.0)
    acc = float(values[0])
    out[0] = acc
    for i in range(1, len(values)):
        acc = alpha * float(values[i]) + (1.0 - alpha) * acc
        out[i] = acc
    return out


def rolling_quantile(values: Sequence[Optional[float]], n: int, q: float,
                     ) -> List[Optional[float]]:
    """`df[col].rolling(n).quantile(q)` — full window required, linear interpolation."""
    out: List[Optional[float]] = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = [v for v in values[i - n + 1:i + 1] if v is not None]
        if len(window) != n:
            continue
        ordered = sorted(window)
        pos = q * (len(ordered) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(ordered) - 1)
        frac = pos - lo
        out[i] = ordered[lo] * (1.0 - frac) + ordered[hi] * frac
    return out


def _consecutive(raw: Sequence[bool], min_sq: int) -> List[bool]:
    """`_consecutive()` from the pandas engine: flag runs of at least ``min_sq``."""
    if min_sq <= 1:
        return list(raw)
    out = [False] * len(raw)
    for i in range(min_sq - 1, len(raw)):
        if all(raw[i - k] for k in range(min_sq)):
            out[i] = True
    return out


# ------------------------------------------------------------------ frame

def build_frame(bars) -> Dict[str, list]:
    """Indicator frame over a list of `app.strategy.Bar`-shaped bars.

    Same columns the pandas engine produced in `to_df()` / `load()`: the six
    means, their envelope, the band, SMA200 and the raw OHLC series.
    """
    closes = [float(b.close) for b in bars]
    frame: Dict[str, list] = {
        "open_ms": [int(b.open_ms) for b in bars],
        "open": [float(b.open) for b in bars],
        "high": [float(b.high) for b in bars],
        "low": [float(b.low) for b in bars],
        "close": closes,
    }
    for n in SMA_WINDOWS:
        frame[f"sma{n}"] = rolling_mean(closes, n)
    for n in EMA_WINDOWS:
        frame[f"ema{n}"] = ewm_mean(closes, n)

    keys = [f"sma{n}" for n in SMA_WINDOWS] + [f"ema{n}" for n in EMA_WINDOWS]
    ma_max: List[Optional[float]] = [None] * len(bars)
    ma_min: List[Optional[float]] = [None] * len(bars)
    band: List[Optional[float]] = [None] * len(bars)
    for i in range(len(bars)):
        vals = [frame[k][i] for k in keys]
        if any(v is None for v in vals):
            continue
        top, bottom = max(vals), min(vals)              # type: ignore[type-var]
        ma_max[i], ma_min[i] = top, bottom
        if closes[i] > 0:
            band[i] = (float(top) - float(bottom)) / closes[i]
    frame["ma_max"], frame["ma_min"], frame["band"] = ma_max, ma_min, band
    frame["sma200"] = rolling_mean(closes, REGIME_MA)
    return frame


# ----------------------------------------------------------------- signals

def squeeze_pct_flags(frame: Dict[str, List[Optional[float]]], q: float = BAND_QUANTILE,
                      win: int = BAND_WINDOW, min_sq: int = MIN_SQUEEZE_BARS) -> List[bool]:
    """Pandas engine's `squeeze_pct_flags`: band in its own `q` quantile, `min_sq` bars.

    The `raw[:win] = False` mask is kept as-is: in the original it removed the
    NaN tail introduced by `dropna()` + `rolling(win)`. Here the same mask means
    "the first bar at which the window could have been full is bar 369". See the
    module docstring for why that off-by-one cannot change a signal.
    """
    band = frame["band"]
    threshold = rolling_quantile(band, win, q)
    raw = [False] * len(band)
    for i, value in enumerate(band):
        limit = threshold[i]
        raw[i] = value is not None and limit is not None and value <= limit
    first_full = next((i for i, v in enumerate(band) if v is not None), len(band))
    for i in range(min(first_full + win, len(raw))):
        raw[i] = False
    return _consecutive(raw, min_sq)


def signals(frame: Dict[str, List[Optional[float]]], squeeze: Sequence[bool],
            mode: str = "breakout") -> List[Tuple[int, int, float]]:
    """Pandas engine's `signals()` — ``[(signal_bar_index, side, stop_price)]``.

    Only ``mode="breakout"`` is ported: it is the rule the live engine and this
    system use. (``pullback`` was an experiment in the study script and is not
    part of the frozen strategy, so porting it would invite a mode nobody runs.)
    """
    if mode != "breakout":
        raise ValueError(f"unsupported mode {mode!r}; only 'breakout' is part of the strategy")
    close = frame["close"]
    ma_max = frame["ma_max"]
    ma_min = frame["ma_min"]
    out: List[Tuple[int, int, float]] = []
    for t in range(1, len(close) - 1):
        if not squeeze[t - 1]:
            continue
        top, bottom = ma_max[t - 1], ma_min[t - 1]
        if top is None or bottom is None:
            continue
        last_close = float(close[t])    # type: ignore[arg-type]  (never None: closes are floats)
        side = 0
        if last_close > float(top):
            side = 1
        elif last_close < float(bottom):
            side = -1
        if side == 0:
            continue
        stop = ma_min[t] if side == 1 else ma_max[t]
        if stop is None:
            continue
        out.append((t, side, float(stop)))
    return out
