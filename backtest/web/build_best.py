"""Build web/best/data.js: the best strategies across all four grids (MNQ/MES x London/NY).

    cd backtest && uv run python web/build_best.py

Needs results/{default,ny_open,es_london,es_ny_open}/ from `uv run orb grid grids/<spec>.toml`.
Ranking rule: robust score = min(in-sample Sharpe, out-of-sample Sharpe), among configs with
>= min_trades_yr trades a year. Configs with identical trades (e.g. an ATR cap that never binds)
are listed once, and so are variants that differ only in the daily-ATR cap (it rarely binds). Only the top N are re-simulated, for their trade lists and example days.
"""
from __future__ import annotations

import json
import sys
from dataclasses import fields
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orb.data import load_1m, parquet_path, prepare  # noqa: E402
from orb.engine import Params, hhmm, run  # noqa: E402
from orb.grid import GridSpec  # noqa: E402
from orb.metrics import summarize  # noqa: E402

GRIDS = [  # spec, label, market, session
    ("default", "MNQ London", "MNQ", "London"),
    ("ny_open", "MNQ NY", "MNQ", "NY"),
    ("es_london", "MES London", "MES", "London"),
    ("es_ny_open", "MES NY", "MES", "NY"),
]
TOP = 20
TYPES = {f.name: f.type for f in fields(Params)}
PCOLS = list(TYPES)


def clean(x):
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if not np.isfinite(x) else round(float(x), 4)
    return x


def params_of(row) -> Params:
    cast = {"int": int, "float": float, "str": str}
    return Params(**{k: cast[TYPES[k]](row[k]) for k in PCOLS})


def describe(p: Params, session: str) -> str:
    start = f"{p.or_start} NY" if session == "NY" else f"{p.or_start} UTC+2"
    entry = "touch (stop order)" if p.entry == "touch" else f"close on {p.signal_tf}m bar"
    s = f"{start} · {p.or_minutes}m range · {entry} · stop {'opposite side' if p.stop == 'opposite' else 'midpoint'} · {p.rr:g}R · flat {p.hold_end}"
    if p.atr_max < 99:
        s += f" · range ≤ {p.atr_max:g}× daily ATR"
    return s


def monthly(t: pd.DataFrame, months: list[str]) -> tuple[list, list]:
    """Month-end cumulative R, and the deepest point below the running peak (per trade) within each month."""
    cum = t.r.cumsum().values
    dd = cum - np.maximum.accumulate(np.r_[0.0, cum])[1:]
    key = t.date.dt.strftime("%Y-%m").values
    eq, uw, last = [], [], 0.0
    for m in months:
        sel = key == m
        if sel.any():
            last = float(cum[sel][-1])
            uw.append(round(float(dd[sel].min()), 2))
        else:
            uw.append(uw[-1] if uw else 0.0)
        eq.append(round(last, 2))
    return eq, uw


def example_day(bars, p: Params, t: pd.DataFrame, reason: str) -> dict | None:
    """1m bars of one real trade day, plus every number needed to re-derive the trade by hand."""
    rows = t[t.reason == reason]
    if rows.empty:
        return None
    tr = rows.iloc[-1]
    d = int(np.searchsorted(bars.dates, np.datetime64(tr.date.date())))
    a, b = bars.day_start[d], bars.day_end[d]
    mod = bars.mod[a:b]
    or_s = hhmm(p.or_start)
    or_e = or_s + p.or_minutes
    inor = (mod >= or_s) & (mod < or_e)
    H, L = float(bars.h[a:b][inor].max()), float(bars.l[a:b][inor].min())
    ex = int(tr.exit_time[:2]) * 60 + int(tr.exit_time[3:])
    keep = (mod >= or_s - 30) & (mod <= min(ex + 45, hhmm(p.hold_end) + 5))
    idx = np.flatnonzero(keep) + a
    # entry minute: the first bar after the range that reaches the entry trigger
    after = idx[bars.mod[idx] >= or_e]
    if p.entry == "touch":
        hit = (bars.h[after] >= H + p.tick) if tr.side == 1 else (bars.l[after] <= L - p.tick)
    else:  # the 1m bar that closes a signal_tf bucket beyond the range
        bk = (bars.mod[after] - or_e) // p.signal_tf
        closes = np.r_[bk[1:] != bk[:-1], True]
        hit = closes & ((bars.c[after] > H) if tr.side == 1 else (bars.c[after] < L))
    em = int(bars.mod[after[np.argmax(hit)]]) if hit.any() else or_e
    stop_pts = abs(tr.entry - tr.stop)
    return clean({
        "date": str(tr.date.date()), "reason": reason, "side": int(tr.side), "qty": int(tr.qty),
        "or_start": or_s, "or_end": or_e, "H": H, "L": L, "entry": tr.entry, "stop": tr.stop,
        "target": tr.entry + tr.side * p.rr * stop_pts, "exit": tr.exit, "entry_mod": em, "exit_mod": ex,
        "pnl": tr.pnl, "r": tr.r, "risk_usd": p.account * p.risk_pct / 100, "pv": p.point_value,
        "comm": p.commission_rt, "tick": p.tick, "slip": p.slippage_ticks, "rr": p.rr, "max_q": p.max_contracts,
        "entry_mode": p.entry,
        "bars": [[int(bars.mod[i]), bars.o[i], bars.h[i], bars.l[i], bars.c[i]] for i in idx],
    })


def main():
    tables, splits, wf = [], {}, {}
    for spec_name, label, market, session in GRIDS:
        spec = GridSpec.load(ROOT / "grids" / f"{spec_name}.toml")
        res = ROOT / "results" / spec_name
        t = pd.read_csv(res / "grid.csv")
        t = t[t.is_trades_yr >= spec.min_trades_yr].copy()
        t["grid"], t["market"], t["session"], t["symbol"] = label, market, session, spec.symbol
        t["robust"] = t[["is_sharpe", "oos_sharpe"]].min(axis=1)
        tables.append(t)
        f = pd.read_csv(res / "walkforward_folds.csv")
        w = pd.read_csv(res / "walkforward_trades.csv", parse_dates=["date"])
        wf[label] = {"folds": f.drop(columns="params").to_dict("records"), "trades": w, "spec": spec_name}
    allc = pd.concat(tables, ignore_index=True)
    allc = allc.sort_values(["robust", "all_sharpe"], ascending=False)
    akey = ["grid"] + [c for c in PCOLS if c not in ("atr_min", "atr_max")]
    allc["dup"] = allc.duplicated(akey)
    uniq = allc[~allc.dup].reset_index(drop=True)

    bars = {}

    def bars_for(sym, tz):
        if (sym, tz) not in bars:
            df = load_1m(parquet_path(sym))
            bars[(sym, tz)] = prepare(df, tz)
            splits[sym] = str(bars[(sym, tz)].dates[int(len(bars[(sym, tz)].dates) * 0.7)])
        return bars[(sym, tz)]

    top, trade_sets = [], []
    for rank, row in uniq.head(TOP).iterrows():
        p = params_of(row)
        b = bars_for(row.symbol, p.tz)
        t = run(b, p)
        s = summarize(t, b.dates, p.account)
        yr = t.groupby(t.date.dt.year).r.sum()
        mo = t.groupby(t.date.dt.strftime("%Y-%m")).r.sum()
        per_contract = (t.entry - t.stop).abs() * p.point_value + p.commission_rt
        top.append({
            "rank": rank + 1, "grid": row.grid, "market": row.market, "session": row.session,
            "desc": describe(p, row.session),
            "params": {k: getattr(p, k) for k in ("tz", "or_start", "or_minutes", "entry", "signal_tf", "stop", "rr",
                                                   "entry_end", "hold_end", "min_range", "max_range", "atr_max", "point_value")},
            "robust": row.robust, "is_sharpe": row.is_sharpe, "oos_sharpe": row.oos_sharpe,
            "is_total_r": row.is_total_r, "oos_total_r": row.oos_total_r,
            "stats": s, "by_year": {int(k): round(float(v), 2) for k, v in yr.items()},
            "years_pos": int((yr > 0).sum()), "years": int(len(yr)),
            "worst_month_r": float(mo.min()), "best_month_r": float(mo.max()),
            "median_contract_risk": float(per_contract.median()),
            "p90_contract_risk": float(per_contract.quantile(0.9)),
            "atr_variants": int((allc[akey] == row[akey]).all(axis=1).sum() - 1),
        })
        trade_sets.append((p, b, t))

    first = min(t.date.min() for _, _, t in trade_sets)
    last = max(t.date.max() for _, _, t in trade_sets)
    months = [str(m) for m in pd.period_range(first, last, freq="M")]
    for s, (_, _, t) in zip(top, trade_sets):
        s["eq"], s["uw"] = monthly(t, months)

    wf_out = {}
    for label, w in wf.items():
        tr = w["trades"]
        eq, _ = monthly(tr, months) if len(tr) else ([], [])
        start = tr.date.min().strftime("%Y-%m") if len(tr) else None
        wf_out[label] = {"folds": w["folds"], "total_r": float(tr.r.sum()), "trades": len(tr),
                         "net": float(tr.pnl.sum()), "start": start,
                         "eq": [v if m >= start else None for m, v in zip(months, eq)] if start else []}

    p1, b1, t1 = trade_sets[0]
    examples = [e for e in (example_day(b1, p1, t1, r) for r in ("TP", "SL", "EOD")) if e]

    scatter = allc[~allc.dup][["grid", "is_sharpe", "oos_sharpe", "all_sharpe", "all_total_r"]].round(3).values.tolist()
    counts = allc.groupby("grid").agg(configs=("robust", "size"), positive=("all_total_r", lambda x: int((x > 0).sum())),
                                      both=("robust", lambda x: int((x > 0).sum())), best_robust=("robust", "max"))
    out = {
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "rule": "robust score = min(in-sample Sharpe, out-of-sample Sharpe); configs with >= 40 trades/yr",
        "splits": splits, "months": months, "top": top, "walkforward": wf_out,
        "grids": counts.reset_index().to_dict("records"), "n_configs": int(len(allc)), "n_unique": int(len(uniq)),
        "scatter": scatter, "examples": examples,
    }
    path = Path(__file__).with_name("best") / "data.js"
    path.parent.mkdir(exist_ok=True)
    path.write_text("window.DATA = " + json.dumps(clean(out), separators=(",", ":")) + ";\n")
    print(f"wrote {path} ({path.stat().st_size / 1e6:.2f} MB)")
    for s in top[:TOP]:
        print(f"{s['rank']:2d} {s['grid']:10} robust {s['robust']:.2f} all {s['stats']['sharpe']:.2f} "
              f"{s['stats']['total_r']:7.1f}R  {s['years_pos']}/{s['years']}y+  {s['desc']}")


if __name__ == "__main__":
    main()
