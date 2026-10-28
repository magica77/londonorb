"""Build web/data.js for the results viewer (web/index.html).

    cd backtest && uv run python web/build.py [--spec grids/default.toml]
    cd backtest && uv run python web/build.py --spec grids/es_london.toml --out web/es/data.js   # ES page

Runs the featured configs, the grid and the walk-forward on the cached Parquet and writes
everything the page needs into one JS file, so index.html opens straight from disk (file://).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orb.data import load_1m, parquet_path, prepare  # noqa: E402
from orb.engine import Params, run, trades_frame  # noqa: E402
from orb.grid import GridSpec, is_oos_correlation, run_grid, walk_forward  # noqa: E402
from orb.metrics import summarize  # noqa: E402

FEATURED = {
    "Pine default (07:00 UTC+2)": {},
    "Candidate (10:00 UTC+2, 30m, touch, 3R)": dict(or_start="10:00", or_minutes=30, entry="touch", rr=3.0),
    # grids/ny_open.toml: same rules at the US cash open (times are New York local, DST-aware)
    "NY open (09:30 ET, 30m, touch, 2R)": dict(tz="America/New_York", or_start="09:30", or_minutes=30, entry="touch",
                                               entry_end="12:00", hold_end="15:55", min_range=0, max_range=1000),
}
WF_NAME = "Walk-forward (picked blind)"


def clean(x):
    """JSON-safe: NaN/inf -> None, numpy -> python."""
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if not np.isfinite(x) else round(float(x), 4)
    return x


def trades_payload(t: pd.DataFrame) -> list[list]:
    return [[d.strftime("%Y-%m-%d"), int(s), int(q), e, x, st, why, round(p, 2), round(r, 3), rg, et]
            for d, s, q, e, x, st, why, p, r, rg, et in
            t[["date", "side", "qty", "entry", "exit", "stop", "reason", "pnl", "r", "range", "exit_time"]]
            .itertuples(index=False)]


def monthly_r(t: pd.DataFrame, months: list[str]) -> list[float]:
    m = t.groupby(t.date.dt.strftime("%Y-%m")).r.sum()
    return [round(float(m.get(k, 0.0)), 3) for k in months]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=str(ROOT / "grids/default.toml"))
    ap.add_argument("--out", default=str(Path(__file__).with_name("data.js")),
                    help="output JS (use web/es/data.js for ES so the MNQ page is not overwritten)")
    a = ap.parse_args()

    spec = GridSpec.load(a.spec)
    df = load_1m(parquet_path(spec.symbol))
    print("grid ...")
    table, results, bars, split = run_grid(df, spec, log=lambda *_: None)
    folds, wf = walk_forward(results, bars, spec)
    dates = next(iter(bars.values())).dates
    months = sorted({str(d)[:7] for d in dates})

    print("featured ...")
    featured = []
    for name, kw in FEATURED.items():
        p = Params(**(spec.base | kw))
        b = bars.get(p.tz) or prepare(df, p.tz)
        t = run(b, p)
        featured.append({"name": name, "params": {k: v for k, v in kw.items()},
                         "stats": summarize(t, b.dates, p.account), "trades": trades_payload(t)})
    wf_dates = dates[dates >= np.datetime64(wf.date.min().date())]
    featured.append({"name": WF_NAME, "params": {}, "stats": summarize(wf, wf_dates, Params().account),
                     "trades": trades_payload(wf)})

    print("grid curves ...")
    varied = list(spec.grid)
    grid_rows = table[varied + [c for c in table.columns if c.startswith(("all_", "is_", "oos_"))]]
    grid_monthly = []
    for p, m in results:
        grid_monthly.append(monthly_r(trades_frame(bars[p.tz], m), months))

    # data quality summary
    ch = df.instrument_id.ne(df.instrument_id.shift())
    quality = {
        "bars": len(df), "first": str(df.index[0]), "last": str(df.index[-1]),
        "contracts": int(df.instrument_id.nunique()),
        "duplicates": int(df.index.duplicated().sum()),
        "nan": int(df[["open", "high", "low", "close"]].isna().sum().sum()),
        "bad_ohlc": int(((df.high < df[["open", "close"]].max(axis=1)) | (df.low > df[["open", "close"]].min(axis=1))).sum()),
        "roll_hours_utc": sorted({int(h) for h in df.index[ch][1:].hour}),
        "bars_per_year": {int(k): int(v) for k, v in df.groupby(df.index.year).size().items()},
        "weekdays": len(dates),
        "daily_close": [[str(d.date()), round(float(c), 2)] for d, c in df.close.resample("1D").last().dropna().items()],
    }

    out = {
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "split": str(split), "spec": Path(a.spec).name, "symbol": spec.symbol,
        "validation": {"sort": spec.sort, "min_trades_yr": spec.min_trades_yr,
                       "train_months": spec.train_months, "test_months": spec.test_months,
                       "rank_corr": is_oos_correlation(table, spec)},
        "trade_cols": ["date", "side", "qty", "entry", "exit", "stop", "reason", "pnl", "r", "range", "exit_time"],
        "featured": featured,
        "grid": {"varied": varied, "cols": list(grid_rows.columns),
                 "rows": grid_rows.values.tolist(), "months": months, "monthly_r": grid_monthly},
        "folds": folds.assign(params=folds.params.map(json.dumps)).to_dict("records"),
        "quality": quality,
    }
    path = Path(a.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("window.DATA = " + json.dumps(clean(out), separators=(",", ":")) + ";\n")
    print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
