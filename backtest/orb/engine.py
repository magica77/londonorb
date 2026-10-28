"""London ORB simulation on 1m bars (numba).

Rules mirror london_orb_mnq_strategy.pine, with one deliberate upgrade: breakouts are judged on
`signal_tf`-minute bars (like the 5m chart), but stops and targets are walked on the underlying
1m bars, which cuts down on "both SL and TP inside one bar" ambiguity.

Per local weekday:
  1. Opening range = high/low of 1m bars in [or_start, or_start + or_minutes).
  2. Skip the day unless min_range <= range <= max_range and atr_min <= range / prior-day ATR <= atr_max.
  3. Entry (max one per day) inside [or_end, entry_end):
       close: first signal bar that closes beyond the range; market fill at its close (+slippage).
       touch: stop order one tick beyond the range; fills at max(open, level) (+slippage).
  4. Stop = opposite side or midpoint of the range. Target = fill +/- rr x (fill - stop).
  5. Exit on 1m bars: SL is checked before TP (conservative); gaps fill at the open; EOD at the
     open of the first bar at/after hold_end (or the last bar of the day).
  Size = min(max_contracts, floor(risk$ / (stop_pts * point_value + commission_rt))).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

import numpy as np
import pandas as pd
from numba import njit

from .data import Bars

EXIT_REASONS = np.array(["SL", "TP", "EOD"])


def hhmm(s: str | int) -> int:
    if isinstance(s, int):
        return s
    h, m = s.split(":")
    return int(h) * 60 + int(m)


@dataclass(frozen=True)
class Params:
    tz: str = "Etc/GMT-2"          # = fixed UTC+2, like the Pine default (POSIX sign is inverted)
    or_start: str = "07:00"
    or_minutes: int = 15
    entry_end: str = "11:00"
    hold_end: str = "22:00"
    signal_tf: int = 5             # minutes per signal bar for entry="close"
    entry: str = "close"           # close | touch
    stop: str = "opposite"         # opposite | mid
    rr: float = 2.0
    min_range: float = 10.0
    max_range: float = 80.0
    atr_min: float = 0.0           # range / prior daily ATR(14) bounds
    atr_max: float = 99.0
    account: float = 50_000.0
    risk_pct: float = 1.0
    max_contracts: int = 10
    commission_rt: float = 1.24    # $ per contract round trip
    slippage_ticks: float = 1.0    # on market/stop fills (entries, SL, EOD), not on TP limits
    point_value: float = 2.0       # MNQ
    tick: float = 0.25

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"Unknown parameter(s): {sorted(unknown)}")
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)


@njit(cache=True)
def _simulate(o, h, l, c, mod, day_start, day_end, atr_prev,
              or_s, or_e, entry_end, hold_end, tf, touch, mid_stop, rr,
              min_rng, max_rng, atr_lo, atr_hi, risk_usd, max_q, pv, comm, slip, tick):
    nd = len(day_start)
    out = np.full((nd, 11), np.nan)  # day, side, qty, entry, exit, stop, reason, pnl, r, range, exit_mod
    k = 0
    for d in range(nd):
        a, b = day_start[d], day_end[d]
        H = -1e18
        L = 1e18
        n_or = 0
        first = -1
        for i in range(a, b):
            if mod[i] >= or_s and mod[i] < or_e:
                H = max(H, h[i])
                L = min(L, l[i])
                n_or += 1
            elif mod[i] >= or_e:
                first = i
                break
        if n_or < max(1, (or_e - or_s) // 2) or first < 0:
            continue
        rng = H - L
        if rng < min_rng or rng > max_rng:
            continue
        atr = atr_prev[d]
        if not np.isnan(atr) and atr > 0 and (rng / atr < atr_lo or rng / atr > atr_hi):
            continue

        pos = 0
        q = 0
        ep = 0.0
        sl = 0.0
        tp = 0.0
        i = first
        # ---- entry ----
        while i < b and mod[i] < entry_end:
            if touch:
                up = h[i] >= H + tick
                dn = l[i] <= L - tick
                if up and dn:  # both levels in one bar: assume the one nearer the open filled first
                    up = (H + tick - o[i]) <= (o[i] - (L - tick))
                    dn = not up
            else:
                bucket = (mod[i] - or_e) // tf
                closes = i + 1 >= b or (mod[i + 1] - or_e) // tf != bucket
                up = closes and c[i] > H
                dn = closes and c[i] < L
            if up or dn:
                pos = 1 if up else -1
                if mid_stop:
                    sl = (H + L) / 2
                else:
                    sl = L if up else H
                if touch:
                    ref = H + tick if up else L - tick
                    ep = (max(o[i], ref) if up else min(o[i], ref)) + pos * slip * tick
                else:
                    ref = c[i]
                    ep = c[i] + pos * slip * tick
                per = abs(ref - sl) * pv + comm
                q = min(max_q, int(math.floor(risk_usd / per))) if abs(ref - sl) > 0 else 0
                if q < 1:
                    pos = 0
                    break
                tp = ep + pos * rr * abs(ep - sl)
                break
            i += 1
        if pos == 0:
            continue

        # ---- exit ----
        ex = np.nan
        why = -1
        exit_mod = -1
        if touch and ((pos == 1 and l[i] <= sl) or (pos == -1 and h[i] >= sl)):
            ex, why, exit_mod = sl - pos * slip * tick, 0, mod[i]   # stopped on the fill bar
        j = i + 1
        while why < 0 and j < b:
            if mod[j] >= hold_end:
                ex, why = o[j] - pos * slip * tick, 2
            elif pos == 1:
                if l[j] <= sl:
                    ex, why = min(o[j], sl) - slip * tick, 0
                elif h[j] >= tp:
                    ex, why = max(o[j], tp), 1
            else:
                if h[j] >= sl:
                    ex, why = max(o[j], sl) + slip * tick, 0
                elif l[j] <= tp:
                    ex, why = min(o[j], tp), 1
            if why >= 0:
                exit_mod = mod[j]
                break
            j += 1
        if why < 0:  # data for the day ran out
            ex, why, exit_mod = c[b - 1] - pos * slip * tick, 2, mod[b - 1]

        pnl = (ex - ep) * pos * q * pv - comm * q
        risk = (abs(ep - sl) * pv + comm) * q
        out[k, 0] = d
        out[k, 1] = pos
        out[k, 2] = q
        out[k, 3] = ep
        out[k, 4] = ex
        out[k, 5] = sl
        out[k, 6] = why
        out[k, 7] = pnl
        out[k, 8] = pnl / risk
        out[k, 9] = rng
        out[k, 10] = exit_mod
        k += 1
    return out[:k]


def simulate(bars: Bars, p: Params) -> np.ndarray:
    """Raw trade matrix (fast path for grid search). Columns as in `_simulate`."""
    if bars.tz != p.tz:
        raise ValueError(f"Bars prepared for {bars.tz}, params want {p.tz}")
    or_s = hhmm(p.or_start)
    return _simulate(bars.o, bars.h, bars.l, bars.c, bars.mod, bars.day_start, bars.day_end, bars.atr_prev,
                     or_s, or_s + p.or_minutes, hhmm(p.entry_end), hhmm(p.hold_end), p.signal_tf,
                     p.entry == "touch", p.stop == "mid", p.rr, p.min_range, p.max_range,
                     p.atr_min, p.atr_max, p.account * p.risk_pct / 100, p.max_contracts,
                     p.point_value, p.commission_rt, p.slippage_ticks, p.tick)


def trades_frame(bars: Bars, m: np.ndarray) -> pd.DataFrame:
    di = m[:, 0].astype(int)
    return pd.DataFrame({
        "date": pd.to_datetime(bars.dates[di]),
        "side": m[:, 1].astype(int), "qty": m[:, 2].astype(int),
        "entry": m[:, 3], "exit": m[:, 4], "stop": m[:, 5],
        "reason": EXIT_REASONS[m[:, 6].astype(int)] if len(m) else [],
        "pnl": m[:, 7], "r": m[:, 8], "range": m[:, 9],
        "exit_time": [f"{int(x) // 60:02d}:{int(x) % 60:02d}" for x in m[:, 10]],
    })


def run(bars: Bars, p: Params) -> pd.DataFrame:
    return trades_frame(bars, simulate(bars, p))
