"""双均线（6MA）趋势系统核心逻辑 — 纯 stdlib，无第三方依赖。

规则（冻结版 v2，2026-10-12 多空双向）：

- 指标：SMA20/60/120 + EMA20/60/120（收盘价）+ SMA200（做空趋势过滤）
- 均线密集：band = (6均线max − 6均线min) / 收盘，要求 band ≤ 过去 250 根的最窄 20% 分位，
  且连续 ≥ 6 根成立
- 入场：本根收盘 > 上一根的 6均线max → 做多 / 收盘 < 上一根的 6均线min 且收盘 < SMA200 → 做空
  → 次根开盘市价入场；止损 = **本根**的 6均线min（做多）/ 6均线max（做空）
  → 止损距离必须落在 [1.5%, 10%]，否则放弃这笔
- 离场：做多=收盘 < SMA20；做空=收盘 > SMA20（止损优先于离场；无固定止盈）

本模块是**唯一**的信号口径来源：回测脚本、dry-run 回放、线上定时任务全部 import 它，
避免「实盘代码与回测口径不一致」。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

SMA_WINDOWS = (20, 60, 120)
EMA_WINDOWS = (20, 60, 120)
MA_KEYS = [f"sma{n}" for n in SMA_WINDOWS] + [f"ema{n}" for n in EMA_WINDOWS]

BAND_WINDOW = 250          # 分位回看窗口（根）
BAND_QUANTILE = 0.20       # 最窄的 20%
MIN_SQUEEZE_BARS = 6       # 密集需连续成立的根数
STOP_MIN = 0.015           # 止损距离下限（太窄是噪音）
STOP_MAX = 0.10            # 止损距离上限（配合逐仓 5x：强平 -19.5% 永远晚于止损）

WARMUP_BARS = 120          # SMA120 需要的最小根数
REGIME_MA = 200            # 做空趋势过滤：只在收盘价低于该均线时做空（回测验证过）
# 算一次有效分位需要：250 根窗口 + 120 根预热（band 在前 119 根为 None）
MIN_BARS_REQUIRED = BAND_WINDOW + WARMUP_BARS + 6


@dataclass(frozen=True)
class Bar:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Signal:
    symbol: str
    bar_index: int            # 突破/跌破 bar 的下标
    bar_open_ms: int          # 该 bar 的开盘时间（幂等键的一部分）
    entry_price: float        # 次根开盘价（= 提醒发出时的市价）
    stop_price: float         # 做多=band 下沿；做空=band 上沿
    stop_dist: float          # 止损距离（正数）
    ma_max: float
    ma_min: float
    band: float
    side: int = 1             # +1 做多 / -1 做空

    @property
    def is_long(self) -> bool:
        return self.side == 1

    @property
    def action(self) -> str:
        return "做多" if self.side == 1 else "做空"

    @property
    def open_button(self) -> str:
        return "开多" if self.side == 1 else "开空"

    @property
    def close_side(self) -> str:
        return "卖出平仓" if self.side == 1 else "买入平仓"


# ---------------------------------------------------------------- 指标

def sma_series(values: Sequence[float], n: int) -> List[Optional[float]]:
    """简单均线；前 n-1 根为 None。

    逐根重新求和（而非滚动加减），避免长序列上的浮点漂移导致
    「收盘恰好等于均线」被误判为跌破/突破。
    """
    out: List[Optional[float]] = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = sum(values[i - n + 1:i + 1]) / n
    return out


def ema_series(values: Sequence[float], n: int) -> List[Optional[float]]:
    """指数均线，首值播种（与 pandas ewm(span=n, adjust=False) 口径一致），从第 0 根就有值。"""
    out: List[Optional[float]] = [None] * len(values)
    if not values:
        return out
    k = 2.0 / (n + 1)
    prev = float(values[0])
    out[0] = prev
    for i in range(1, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def quantile(values: Sequence[float], q: float) -> Optional[float]:
    """线性插值分位（与 pandas Series.quantile 默认口径一致）。"""
    if not values:
        return None
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def compute_indicators(bars: Sequence[Bar]) -> Dict[str, List[Optional[float]]]:
    """返回 sma20/60/120、ema20/60/120、ma_max、ma_min、band（逐根对齐）。"""
    closes = [b.close for b in bars]
    out: Dict[str, List[Optional[float]]] = {}
    for n in SMA_WINDOWS:
        out[f"sma{n}"] = sma_series(closes, n)
    for n in EMA_WINDOWS:
        out[f"ema{n}"] = ema_series(closes, n)

    n_bars = len(bars)
    ma_max: List[Optional[float]] = [None] * n_bars
    ma_min: List[Optional[float]] = [None] * n_bars
    band: List[Optional[float]] = [None] * n_bars
    for i in range(n_bars):
        vals = [out[k][i] for k in MA_KEYS]
        if any(v is None for v in vals):
            continue
        hi, lo = max(vals), min(vals)          # type: ignore[arg-type]
        ma_max[i], ma_min[i] = hi, lo
        if closes[i] > 0:
            band[i] = (hi - lo) / closes[i]
    out["ma_max"], out["ma_min"], out["band"] = ma_max, ma_min, band
    out["sma200"] = sma_series(closes, REGIME_MA)      # 做空趋势过滤用
    return out


# ---------------------------------------------------------------- 密集 / 信号

def squeeze_flags(band: Sequence[Optional[float]], window: int = BAND_WINDOW,
                  q: float = BAND_QUANTILE, min_bars: int = MIN_SQUEEZE_BARS) -> List[bool]:
    """连续 min_bars 根满足「band ≤ 过去 window 根的 q 分位」即为 True。

    分位窗口含当前根（与回测口径一致）。历史不足 window 根的位置一律 False。
    """
    n = len(band)
    raw = [False] * n
    for i in range(window - 1, n):
        cur = band[i]
        if cur is None:
            continue
        hist = [band[j] for j in range(i - window + 1, i + 1) if band[j] is not None]
        if len(hist) < window:
            continue
        thr = quantile(hist, q)
        if thr is not None and cur <= thr:
            raw[i] = True

    out = [False] * n
    run = 0
    for i in range(n):
        run = run + 1 if raw[i] else 0
        if run >= min_bars:
            out[i] = True
    return out


def evaluate_entry(symbol: str, bars: Sequence[Bar], ind: Dict[str, List[Optional[float]]],
                   squeeze: Sequence[bool], i: int, entry_price: Optional[float] = None,
                   stop_min: float = STOP_MIN, stop_max: float = STOP_MAX,
                   allow_long: bool = True, allow_short: bool = True,
                   short_regime_ma: int = REGIME_MA) -> Optional[Signal]:
    """判断第 i 根是否为入场信号（突破=做多 / 跌破=做空）。

    entry_price 传次根开盘价；不传则用第 i 根收盘价（仅用于回放对齐）。
    做空额外要求：收盘价低于 short_regime_ma 均线（趋势过滤，回测验证；0 表示不过滤）。
    """
    if i < 1 or i >= len(bars):
        return None
    if not squeeze[i - 1]:
        return None
    ma_max, ma_min, band = ind["ma_max"], ind["ma_min"], ind["band"]
    if ma_max[i - 1] is None or ma_min[i - 1] is None or band[i - 1] is None:
        return None

    px = float(entry_price if entry_price is not None else bars[i].close)
    if px <= 0:
        return None
    close = bars[i].close

    side = 0
    if allow_long and close > ma_max[i - 1]:
        side = 1
    elif allow_short and close < ma_min[i - 1]:
        side = -1
        if short_regime_ma:
            key = f"sma{short_regime_ma}"
            if key not in ind:
                # 可配置窗口不在 compute_indicators 的固定清单里 → 现算，绝不能静默吞掉做空信号
                ind[key] = sma_series([b.close for b in bars], short_regime_ma)
            regime = ind[key]
            if regime[i] is None or close >= regime[i]:
                return None
    if side == 0:
        return None

    stop = float(ma_min[i]) if side == 1 else float(ma_max[i])
    if side == 1 and stop >= px:
        return None
    if side == -1 and stop <= px:
        return None
    dist = abs(px - stop) / px
    if not (stop_min <= dist <= stop_max):
        return None
    return Signal(symbol=symbol, bar_index=i, bar_open_ms=bars[i].open_ms, entry_price=px,
                  stop_price=stop, stop_dist=dist, ma_max=float(ma_max[i - 1]),
                  ma_min=float(ma_min[i - 1]), band=float(band[i - 1]), side=side)


def exit_triggered(bars: Sequence[Bar], ind: Dict[str, List[Optional[float]]], i: int,
                   side: int = 1) -> bool:
    """第 i 根收盘是否触发离场：做多=收盘 < SMA20；做空=收盘 > SMA20。"""
    sma20 = ind["sma20"][i]
    if sma20 is None:
        return False
    return bars[i].close < sma20 if side == 1 else bars[i].close > sma20


def scan(bars: Sequence[Bar], symbol: str = "", start: int = 0, sides=(1,),
         short_regime_ma: int = REGIME_MA) -> List[Signal]:
    """对整段历史扫一遍信号（回放 / 回测用）。"""
    ind = compute_indicators(bars)
    sq = squeeze_flags(ind["band"])
    out: List[Signal] = []
    for i in range(max(start, 1), len(bars) - 1):
        nxt = bars[i + 1].open if i + 1 < len(bars) else None
        s = evaluate_entry(symbol, bars, ind, sq, i, entry_price=nxt,
                           allow_long=(1 in sides), allow_short=(-1 in sides),
                           short_regime_ma=short_regime_ma)
        if s:
            out.append(s)
    return out


# ---------------------------------------------------------------- 密集根数（展示用）
def dense_run(band: Sequence[Optional[float]], i: int, window: int = BAND_WINDOW,
              q: float = BAND_QUANTILE, raw: Optional[Sequence[bool]] = None) -> int:
    """以第 i 根结尾的「连续密集」根数（0 = 不密集）。

    面板要的是「还差几根」，信号要的是「连续 ≥ min_squeeze_bars 根」—— 同一套判定、
    两种问法。这里用 `squeeze_flags(min_bars=1)`（=「单根成立」）取原始判定再往回数，
    **不复制窗口/分位常数、也不另写一套判定**，否则面板的密集根数与信号口径会分叉。

    `raw` 是已经算好的「单根密集」序列（调用方若刚好有一份就传进来，省一次
    O(根数×窗口) 的重复计算 —— 5000 根上这份计算约 0.4s，面板冷启动时按标的累加可观）。
    传入时**只做「往回数」这一步**，判定仍来自 squeeze_flags 的输出。
    """
    if i < 0 or not band:
        return 0
    flags = list(raw) if raw is not None else squeeze_flags(band, window, q, 1)
    n, j = 0, min(i, len(flags) - 1)
    while j >= 0 and flags[j]:
        n += 1
        j -= 1
    return n


# ---------------------------------------------------------------- 诊断（/why，只读）
# `/why` 的口径红线：**答案必须由 evaluate_entry 自己给出**。
#
# 这里的六步只是把 evaluate_entry 内部读的那几个量，按它自己的早退顺序摊开给人看
# （同样的 `ind[i-1]`、同样的 `squeeze[i-1]`、同样的 cfg 阈值）；「有没有信号 / 什么方向」
# 直接取 evaluate_entry 的返回值。为什么这样写：诊断器自己重写一遍判定，就等于把同一套
# 规则维护两份 —— 改一处忘另一处，面板就会开始解释一个系统并不会做的决定
# （「面板说能开、执行器不开」这种 bug 比拼错数字更难查）。所以本函数最后还会把
# 「六步推导的结论」与 evaluate_entry 的真实返回值对账，不一致就打 `drift=True`：
# 漂移必须在运行时立刻看得见，而不是静默给出两个答案。
WHY_OK = "ok"

# 六步的键（顺序 = evaluate_entry 内部早退的顺序；`blocked_by` = 第一个不成立的那步）
WHY_STEP_BARS = "bars"          # ① 根数够不够（+ 下标/价格合法性）
WHY_STEP_SQUEEZE = "squeeze"    # ② 带状信号 vs 分位阈值（读 squeeze[i-1]）
WHY_STEP_BREAKOUT = "breakout"  # ③ close vs ma_max/ma_min（+ 方向开关）
WHY_STEP_REGIME = "regime"      # ④ close vs SMA200（只影响做空）
WHY_STEP_STOP = "stop"          # ⑤ 止损距离是否落在 [stop_min, stop_max]
WHY_STEP_DECISION = "decision"  # ⑥ 多 / 空 / 无（结论，取自 evaluate_entry）
WHY_STEPS = (WHY_STEP_BARS, WHY_STEP_SQUEEZE, WHY_STEP_BREAKOUT, WHY_STEP_REGIME,
             WHY_STEP_STOP, WHY_STEP_DECISION)


def explain_entry(symbol: str, bars: Sequence[Bar], ind: Dict[str, List[Optional[float]]],
                  squeeze: Sequence[bool], i: int, entry_price: Optional[float] = None,
                  stop_min: float = STOP_MIN, stop_max: float = STOP_MAX,
                  allow_long: bool = True, allow_short: bool = True,
                  short_regime_ma: int = REGIME_MA, band_window: int = BAND_WINDOW,
                  band_quantile: float = BAND_QUANTILE,
                  min_squeeze_bars: int = MIN_SQUEEZE_BARS,
                  squeeze_raw: Optional[Sequence[bool]] = None) -> Dict[str, Any]:
    """`/why` 诊断：第 i 根为什么开多 / 开空 / 不开（**纯只读、纯函数**）。

    参数与 `evaluate_entry` 一一对应（同一个 i、同一份 ind/squeeze、同一组 cfg 阈值），
    调用方传什么，诊断与实盘判定看到的就是什么。

    返回 dict（JSON 可序列化，可直接落库给面板读）::

        {"symbol", "bar_index", "bar_open_ms",
         "fired": bool, "side": 1|-1|0,
         "blocked_by": "ok"|"bars"|"squeeze"|"breakout"|"regime"|"stop"|"unknown",
         "reason": 中文一句话（拦截原因 / 成立原因）,
         "steps": [{"key","ok","detail"}, ...],   # 六步逐项
         "values": {...逐项用到的数字...},
         "signal": {...} | None,                  # evaluate_entry 的真实返回值
         "drift": bool}                           # 六步推导 vs 真实返回值不一致

    **绝不改变信号判定结果**：`fired`/`side`/`signal` 全部取自 evaluate_entry，
    `steps` 只做解释且与结果对账（drift）。
    """
    n = len(bars)
    steps: List[Dict[str, Any]] = []
    blocked_by: Optional[str] = None
    why_txt = ""

    def _block(key: str, text: str) -> None:
        """记下第一个拦截点（后面的步骤照常展开，面板才能一次看全貌）。"""
        nonlocal blocked_by, why_txt
        if blocked_by is None:
            blocked_by, why_txt = key, text

    # ---------------- ① 根数 / 下标 / 价格：能不能算、拿没拿到可比的前一根 ----------------
    idx_ok = 1 <= i < n
    close = float(bars[i].close) if 0 <= i < n else 0.0
    px = float(entry_price if entry_price is not None else close) if 0 <= i < n else 0.0
    bars_ok = n >= MIN_BARS_REQUIRED and idx_ok and px > 0
    bars_detail = f"{n} 根（≥{MIN_BARS_REQUIRED} 才够算 250 根分位）"
    if not idx_ok:
        bars_detail = f"下标 {i} 越界（需 1 ≤ i < {n}）"
    elif px <= 0:
        bars_detail = f"入场参考价 {px} 非法（行情异常）"
    steps.append({"key": WHY_STEP_BARS, "ok": bars_ok, "detail": bars_detail})
    if n < MIN_BARS_REQUIRED:
        _block(WHY_STEP_BARS, f"K 线只有 {n} 根，不足 {MIN_BARS_REQUIRED} 根（算不出 250 根分位）")
    elif not idx_ok:
        _block(WHY_STEP_BARS, "下标越界：没有可比较的前一根")
    elif px <= 0:
        _block(WHY_STEP_BARS, "入场参考价 ≤ 0（行情异常）")

    # ---------------- ② 均线密集：squeeze[i-1]（与 evaluate_entry 同一个下标） ----------------
    # prev 必须显式 ≥0 判断：`i - 1 == -1` 在 Python 里是「最后一根」，会把末根当成前一根
    # （数据越多越隐蔽：面板上表现为「明明没密集却显示密集」）。
    band_seq: List[Optional[float]] = list(ind.get("band") or [])
    prev = i - 1
    band_prev = band_seq[prev] if 0 <= prev < len(band_seq) else None
    sq_ok = bool(squeeze[prev]) if 0 <= prev < len(squeeze) else False
    # 阈值只作展示：同一窗口、同一分位、同一个 strategy.quantile。**判定只认 squeeze[i-1]**。
    thr: Optional[float] = None
    lo = i - band_window
    if band_prev is not None and lo >= 0:
        hist: List[float] = [float(v) for v in band_seq[lo:i] if v is not None]
        if len(hist) >= band_window:
            thr = quantile(hist, band_quantile)
    # 展示用的「连续密集根数」：squeeze_raw 由调用方给（同一套 squeeze_flags(min_bars=1)
    # 的输出）就省一次重算；不给则现算。判定仍然是上面那行 `sq_ok`。
    dense = dense_run(band_seq, prev, band_window, band_quantile, raw=squeeze_raw)
    band_txt = "—" if band_prev is None else f"{band_prev * 100:.2f}%"
    thr_txt = "—" if thr is None else f"{thr * 100:.2f}%"
    steps.append({"key": WHY_STEP_SQUEEZE, "ok": sq_ok,
                  "detail": (f"密集 {dense} 根，带宽 {band_txt} ≤ 门槛 {thr_txt}" if sq_ok
                             else f"未密集：带宽 {band_txt} vs 门槛 {thr_txt}，连续 {dense}/{min_squeeze_bars} 根")})
    if not sq_ok:
        _block(WHY_STEP_SQUEEZE,
               f"均线不密集：带宽 {band_txt} 未压到门槛 {thr_txt} 以内，或连续不足 "
               f"{min_squeeze_bars} 根（现 {dense} 根）")

    # ---------------- ③ 破线方向：close vs ma_max/ma_min[i-1] ----------------
    ma_max_seq: List[Optional[float]] = list(ind.get("ma_max") or [])
    ma_min_seq: List[Optional[float]] = list(ind.get("ma_min") or [])
    mx = ma_max_seq[prev] if 0 <= prev < len(ma_max_seq) else None
    mn = ma_min_seq[prev] if 0 <= prev < len(ma_min_seq) else None
    band_ok = mx is not None and mn is not None and band_prev is not None
    fmax = float(mx) if mx is not None else None
    fmin = float(mn) if mn is not None else None
    long_break = bool(band_ok and fmax is not None and close > fmax)
    short_break = bool(band_ok and fmin is not None and close < fmin)
    # 顺序与 evaluate_entry 一致：先判多，再看空
    side = 0
    if long_break and allow_long:
        side = 1
    elif short_break and allow_short:
        side = -1
    if not band_ok:
        brk_detail = "均线带在上一根还没算出来（预热不足）"
        _block(WHY_STEP_BREAKOUT, "均线带缺失（预热根数不足）")
    elif side == 0 and (long_break or short_break):
        word = "做多" if long_break else "做空"
        brk_detail = f"收盘 {close:,.2f} 已{'上破' if long_break else '下破'}，但{word}方向开关是关的"
        _block(WHY_STEP_BREAKOUT, f"{word}方向已在面板关闭，破线也不开仓")
    elif side == 0:
        brk_detail = f"收盘 {close:,.2f} 在上轨 {fmax:,.2f} 与下轨 {fmin:,.2f} 之间"
        _block(WHY_STEP_BREAKOUT, f"收盘价没破线：{close:,.2f} 落在上轨 {fmax:,.2f} / 下轨 {fmin:,.2f} 之间")
    elif side == 1:
        brk_detail = f"收盘 {close:,.2f} > 上轨 {fmax:,.2f}"
    else:
        brk_detail = f"收盘 {close:,.2f} < 下轨 {fmin:,.2f}"
    steps.append({"key": WHY_STEP_BREAKOUT, "ok": band_ok and side != 0, "detail": brk_detail})

    # ---------------- ④ 做空趋势过滤：close vs SMA200（short_regime_ma=0 表示不过滤） ----------------
    regime_val: Optional[float] = None
    regime_ok = True
    regime_txt = "不适用（没有做空方向）"
    if side == -1:
        if not short_regime_ma:
            regime_txt = "已关闭趋势过滤（SHORT_REGIME_MA=0）"
        else:
            rkey = f"sma{short_regime_ma}"
            if rkey in ind:
                rseq: List[Optional[float]] = list(ind.get(rkey) or [])
            else:
                # 与 evaluate_entry 同一条兜底：可配置窗口不在固定清单里就现算。
                # **不写回调用方的 ind**（诊断必须无副作用；evaluate_entry 那边拿的是副本）。
                rseq = list(sma_series([b.close for b in bars], short_regime_ma))
            raw_r = rseq[i] if i < len(rseq) else None
            regime_val = float(raw_r) if raw_r is not None else None
            # fail-closed：均线缺失 → 不做空（与 evaluate_entry 同一判据）
            regime_ok = regime_val is not None and close < regime_val
            if regime_ok:
                regime_txt = f"收盘 {close:,.2f} < SMA{short_regime_ma} {regime_val:,.2f}"
            elif regime_val is not None:
                regime_txt = f"收盘 {close:,.2f} ≥ SMA{short_regime_ma} {regime_val:,.2f}"
            else:
                regime_txt = f"SMA{short_regime_ma} 还没算出来"
            if not regime_ok:
                _block(WHY_STEP_REGIME,
                       f"做空趋势过滤不成立：{regime_txt}（做空要求收盘低于 SMA{short_regime_ma}）")
    steps.append({"key": WHY_STEP_REGIME, "ok": regime_ok, "detail": regime_txt})

    # ---------------- ⑤ 止损距离：[stop_min, stop_max] ----------------
    stop_ref: Optional[float] = None
    stop_dist: Optional[float] = None
    stop_ok = True
    stop_txt = "无方向（不适用）"
    if side != 0:
        sseq: List[Optional[float]] = ma_min_seq if side == 1 else ma_max_seq
        raw_s = sseq[i] if i < len(sseq) else None
        stop_ref = float(raw_s) if raw_s is not None else None
        if stop_ref is None:
            stop_ok, stop_txt = False, "止损参考线（本根均线带）还没算出来"
            _block(WHY_STEP_STOP, stop_txt)
        elif (side == 1 and stop_ref >= px) or (side == -1 and stop_ref <= px):
            stop_ok = False
            stop_txt = (f"止损 {stop_ref:,.2f} 在入场价 {'上方' if side == 1 else '下方'}"
                        f"（做{'多' if side == 1 else '空'}方向不对）")
            _block(WHY_STEP_STOP, stop_txt)
        else:
            stop_dist = abs(px - stop_ref) / px
            stop_ok = bool(stop_min <= stop_dist <= stop_max)
            stop_txt = (f"止损 {stop_ref:,.2f}，距离 {stop_dist * 100:.2f}%"
                        f"（允许 {stop_min * 100:.1f}%~{stop_max * 100:.1f}%）")
            if not stop_ok:
                _block(WHY_STEP_STOP,
                       f"止损距离 {stop_dist * 100:.2f}% 不在 {stop_min * 100:.1f}%~"
                       f"{stop_max * 100:.1f}% 之间（太近=被噪音扫，太远=单笔风险过大）")
    steps.append({"key": WHY_STEP_STOP, "ok": stop_ok, "detail": stop_txt})

    # ---------------- 权威结论：直接调 evaluate_entry（同一份入参） ----------------
    # ind 副本：evaluate_entry 会往 ind 里补算 short_regime_ma 序列，诊断不能改调用方的 dict。
    sig = evaluate_entry(symbol, bars, dict(ind), squeeze, i, entry_price=entry_price,
                         stop_min=stop_min, stop_max=stop_max,
                         allow_long=allow_long, allow_short=allow_short,
                         short_regime_ma=short_regime_ma)

    # 六步推导 vs 真实返回值的对账（正常恒一致；不一致说明这份解释已经和判定漂移）
    drift = bool((blocked_by is None) != (sig is not None))
    if side != 0 and sig is not None and int(sig.side) != int(side):
        drift = True
    if sig is not None:
        side = int(sig.side)
        steps.append({"key": WHY_STEP_DECISION, "ok": True,
                      "detail": (f"{'做多' if side == 1 else '做空'}信号成立：入场 "
                                 f"{sig.entry_price:,.2f}、止损 {sig.stop_price:,.2f}"
                                 f"（{sig.stop_dist * 100:.2f}%）")})
        blocked_by, why_txt = WHY_OK, (
            f"信号成立：{'做多' if side == 1 else '做空'}，入场 {sig.entry_price:,.2f}、"
            f"止损 {sig.stop_price:,.2f}（{sig.stop_dist * 100:.2f}%）")
    else:
        if blocked_by is None:
            # 六步全过但 evaluate_entry 仍返回 None：口径漂移，如实报出来而不是替它圆场
            blocked_by, why_txt = "unknown", "六步均通过但 evaluate_entry 未给出信号（口径漂移）"
        steps.append({"key": WHY_STEP_DECISION, "ok": False,
                      "detail": f"不开仓：{why_txt}"})

    values: Dict[str, Any] = {
        "bars": n, "close": close, "entry_px": px,
        "ma_max_prev": fmax, "ma_min_prev": fmin,
        "band_prev": band_prev, "band_thr": thr, "dense_cnt": dense,
        "regime_ma": int(short_regime_ma or 0), "regime_val": regime_val,
        "stop_ref": stop_ref, "stop_dist": stop_dist,
        "stop_min": float(stop_min), "stop_max": float(stop_max),
        "allow_long": bool(allow_long), "allow_short": bool(allow_short),
    }
    return _why_result(symbol, bars, i, steps, blocked_by, why_txt, values, sig, drift)


def _why_result(symbol: str, bars: Sequence[Bar], i: int, steps: List[Dict[str, Any]],
                blocked_by: Optional[str], why_txt: str, values: Dict[str, Any],
                sig: Optional[Signal], drift: bool) -> Dict[str, Any]:
    """把六步展开的结果收成最终 dict（JSON 可序列化；算不出来的数一律保留 None，不补零）。"""
    bar_ms = int(bars[i].open_ms) if 0 <= i < len(bars) else None
    return {
        "symbol": symbol,
        "bar_index": int(i),
        "bar_open_ms": bar_ms,
        "fired": sig is not None,
        "side": int(sig.side) if sig is not None else 0,
        "blocked_by": blocked_by or WHY_OK,
        "reason": why_txt or "—",
        "steps": steps,
        "values": values,
        "signal": ({"side": int(sig.side), "entry_price": float(sig.entry_price),
                    "stop_price": float(sig.stop_price), "stop_dist": float(sig.stop_dist)}
                   if sig is not None else None),
        "drift": bool(drift),
    }
