"""Performance stats. Returns use daily P&L over every eligible weekday (flat days count as 0)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def daily_pnl(dates: np.ndarray, day_idx: np.ndarray, pnl: np.ndarray) -> np.ndarray:
    out = np.zeros(len(dates))
    np.add.at(out, day_idx.astype(int), pnl)
    return out


def stats_from_arrays(daily: np.ndarray, pnl: np.ndarray, r: np.ndarray, account: float) -> dict:
    """Core metrics; cheap enough to run for every grid config."""
    n = len(pnl)
    years = len(daily) / 252 if len(daily) else np.nan
    eq = account + np.cumsum(daily)
    dd = eq - np.maximum.accumulate(np.r_[account, eq])[1:]
    ret = daily / account  # sizing uses a fixed account, so returns do too (and stay sane if equity < 0)
    sd = ret.std()
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_loss = -losses.sum()
    return {
        "trades": n,
        "trades_yr": n / years if years else np.nan,
        "win_pct": 100 * len(wins) / n if n else np.nan,
        "net": pnl.sum(),
        "total_r": r.sum(),
        "avg_r": r.mean() if n else np.nan,
        "pf": wins.sum() / gross_loss if gross_loss > 0 else np.nan,
        "max_dd": dd.min() if len(dd) else 0.0,
        "max_dd_r": _max_dd(r),
        "sharpe": ret.mean() / sd * np.sqrt(252) if sd > 0 else np.nan,
        "cagr_pct": 100 * ((eq[-1] / account) ** (1 / years) - 1) if n and years and eq[-1] > 0 else np.nan,
    }


def _max_dd(r: np.ndarray) -> float:
    if not len(r):
        return 0.0
    cum = np.cumsum(r)
    return float((cum - np.maximum.accumulate(np.r_[0.0, cum])[1:]).min())


def summarize(trades: pd.DataFrame, dates: np.ndarray, account: float) -> dict:
    """Full stats for one config's trade list (from engine.run)."""
    idx = np.searchsorted(dates, trades.date.values.astype("datetime64[D]"))
    s = stats_from_arrays(daily_pnl(dates, idx, trades.pnl.values), trades.pnl.values, trades.r.values, account)
    s["days"] = len(dates)
    s["period"] = f"{pd.Timestamp(dates[0]).date()} -> {pd.Timestamp(dates[-1]).date()}" if len(dates) else ""
    s["exits"] = trades.reason.value_counts().to_dict()
    s["avg_qty"] = trades.qty.mean() if len(trades) else np.nan
    s["long_r"] = trades.r[trades.side == 1].sum()
    s["short_r"] = trades.r[trades.side == -1].sum()
    return s


def by_year(trades: pd.DataFrame) -> pd.DataFrame:
    g = trades.groupby(trades.date.dt.year)
    return pd.DataFrame({"trades": g.size(), "win_pct": g.pnl.apply(lambda x: 100 * (x > 0).mean()),
                         "net": g.pnl.sum(), "total_r": g.r.sum(), "avg_r": g.r.mean()})


def mc_drawdown_r(r: np.ndarray, n: int = 5000, seed: int = 0) -> dict:
    """Bootstrap the trade order: how bad can the drawdown (in R) plausibly get?"""
    if len(r) < 2:
        return {}
    rng = np.random.default_rng(seed)
    dds = np.array([_max_dd(rng.choice(r, len(r))) for _ in range(n)])
    return {f"p{q}": float(np.percentile(dds, 100 - q)) for q in (50, 95, 99)}
