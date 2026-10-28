"""Grid search with in-sample / out-of-sample split and walk-forward validation.

Every config is simulated once over the full history; IS/OOS and walk-forward are just
date slices of those trades, so validation costs almost nothing extra.
"""
from __future__ import annotations

import itertools
import time
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from .data import SYMBOL, Bars, prepare
from .engine import Params, hhmm, simulate, trades_frame
from .metrics import daily_pnl, stats_from_arrays

COLS = ["day", "side", "qty", "entry", "exit", "stop", "reason", "pnl", "r", "range", "exit_mod"]


@dataclass
class GridSpec:
    base: dict
    grid: dict
    sort: str = "sharpe"
    min_trades_yr: float = 40
    oos_frac: float = 0.3
    oos_start: str | None = None
    train_months: int = 24
    test_months: int = 6
    symbol: str = SYMBOL       # [data] symbol: which cached Parquet to load (e.g. "ES.v.0")

    @classmethod
    def load(cls, path: str | Path) -> "GridSpec":
        cfg = tomllib.loads(Path(path).read_text())
        v, wf = cfg.get("validation", {}), cfg.get("walkforward", {})
        return cls(base=cfg.get("base", {}), grid=cfg.get("grid", {}),
                   symbol=cfg.get("data", {}).get("symbol", SYMBOL),
                   **{k: v[k] for k in ("sort", "min_trades_yr", "oos_frac", "oos_start") if k in v},
                   **{k: wf[k] for k in ("train_months", "test_months") if k in wf})

    def configs(self) -> list[Params]:
        base = Params.from_dict(self.base)
        keys = list(self.grid)
        out, seen = [], set()
        for vals in itertools.product(*(self.grid[k] for k in keys)):
            p = replace(base, **dict(zip(keys, vals)))
            if p.entry == "touch":
                p = replace(p, signal_tf=1)   # signal_tf is unused by touch entries: dedupe
            if hhmm(p.or_start) + p.or_minutes >= hhmm(p.entry_end) or p in seen:
                continue
            seen.add(p)
            out.append(p)
        return out


def _slice_stats(m: np.ndarray, dates: np.ndarray, lo, hi, account: float) -> dict:
    dmask = (dates >= lo) & (dates < hi)
    if not dmask.any():
        return {}
    d0 = np.argmax(dmask)
    t = m[(m[:, 0] >= d0) & (m[:, 0] < d0 + dmask.sum())]
    daily = daily_pnl(dates[dmask], t[:, 0] - d0, t[:, 7])
    return stats_from_arrays(daily, t[:, 7], t[:, 8], account)


def run_grid(df: pd.DataFrame, spec: GridSpec, log=print):
    cfgs = spec.configs()
    log(f"{len(cfgs)} configs")
    bars: dict[str, Bars] = {}
    results: list[tuple[Params, np.ndarray]] = []
    t0 = time.time()
    for k, p in enumerate(cfgs, 1):
        if p.tz not in bars:
            bars[p.tz] = prepare(df, p.tz)
        results.append((p, simulate(bars[p.tz], p)))
        if k % 250 == 0 or k == len(cfgs):
            log(f"  {k}/{len(cfgs)}  {time.time() - t0:.1f}s")

    any_dates = next(iter(bars.values())).dates
    split = np.datetime64(spec.oos_start, "D") if spec.oos_start else any_dates[int(len(any_dates) * (1 - spec.oos_frac))]
    lo, hi = any_dates[0], any_dates[-1] + 1
    rows = []
    for p, m in results:
        dates = bars[p.tz].dates
        row = p.to_dict()
        for tag, a, b in (("all", lo, hi), ("is", lo, split), ("oos", split, hi)):
            row |= {f"{tag}_{k}": v for k, v in _slice_stats(m, dates, a, b, p.account).items()}
        rows.append(row)
    table = pd.DataFrame(rows)
    return table, results, bars, split


def rank(table: pd.DataFrame, spec: GridSpec, prefix: str = "is") -> pd.DataFrame:
    ok = table[table[f"{prefix}_trades_yr"] >= spec.min_trades_yr]
    return ok.sort_values(f"{prefix}_{spec.sort}", ascending=False)


def is_oos_correlation(table: pd.DataFrame, spec: GridSpec) -> float:
    """Spearman rank correlation of IS vs OOS score across configs. Near 0 = IS ranking is noise."""
    ok = table[table.is_trades_yr >= spec.min_trades_yr]
    return ok[f"is_{spec.sort}"].rank().corr(ok[f"oos_{spec.sort}"].rank())


def walk_forward(results, bars: dict[str, Bars], spec: GridSpec):
    """Rolling: pick the best config on `train_months`, trade it for the next `test_months`."""
    dates = next(iter(bars.values())).dates
    start = pd.Timestamp(dates[0])
    end = pd.Timestamp(dates[-1]) + pd.Timedelta(days=1)
    folds, oos = [], []
    a = start
    while True:
        b = a + pd.DateOffset(months=spec.train_months)
        c = min(b + pd.DateOffset(months=spec.test_months), end)
        if (end - b).days < 28:  # need at least ~a month to test on
            break
        tr = (np.datetime64(a.date()), np.datetime64(b.date()))
        te = (np.datetime64(b.date()), np.datetime64(c.date()))
        best, best_score, best_m = None, -np.inf, None
        for p, m in results:
            s = _slice_stats(m, bars[p.tz].dates, *tr, p.account)
            if not s or s["trades_yr"] < spec.min_trades_yr:
                continue
            score = s[spec.sort]
            if np.isfinite(score) and score > best_score:
                best, best_score, best_m = p, score, m
        if best is None:
            a = a + pd.DateOffset(months=spec.test_months)
            continue
        bd = bars[best.tz]
        test = _slice_stats(best_m, bd.dates, *te, best.account)
        tm = best_m[(bd.dates[best_m[:, 0].astype(int)] >= te[0]) & (bd.dates[best_m[:, 0].astype(int)] < te[1])]
        oos.append(trades_frame(bd, tm))
        changed = {k: v for k, v in best.to_dict().items() if v != Params().to_dict()[k]}
        folds.append({"train": f"{a.date()}..{b.date()}", "test": f"{b.date()}..{c.date()}",
                      f"train_{spec.sort}": best_score, f"test_{spec.sort}": test.get(spec.sort),
                      "test_trades": test.get("trades"), "test_net": test.get("net"),
                      "test_total_r": test.get("total_r"), "params": changed})
        a = a + pd.DateOffset(months=spec.test_months)
    trades = pd.concat(oos, ignore_index=True) if oos else pd.DataFrame()
    return pd.DataFrame(folds), trades
