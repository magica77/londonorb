"""`uv run orb <command>`: download | run | grid."""
from __future__ import annotations

import argparse
import json
from dataclasses import fields
from pathlib import Path

import numpy as np
import pandas as pd

from . import data
from .engine import Params, run
from .metrics import by_year, mc_drawdown_r, summarize

RESULTS = Path(__file__).resolve().parent.parent / "results"


def _parse_params(pairs: list[str], base: dict | None = None) -> Params:
    types = {f.name: f.type for f in fields(Params)}
    d = dict(base or {})
    for kv in pairs:
        k, v = kv.split("=", 1)
        if k not in types:
            raise SystemExit(f"Unknown param {k!r}. Valid: {', '.join(types)}")
        t = types[k]
        d[k] = int(v) if t == "int" else float(v) if t == "float" else v
    return Params.from_dict(d)


def _fmt(d: dict) -> str:
    return "\n".join(f"  {k:12} {v:,.2f}" if isinstance(v, float) else f"  {k:12} {v}" for k, v in d.items())


def cmd_download(a):
    end = a.end or pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    start = a.start or (pd.Timestamp(end) - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    est = data.estimate(start, end, a.symbol)
    print(f"{a.symbol} {data.SCHEMA} {start} -> {end}: ~${est['cost_usd']:.2f}, "
          f"{est['size_mb']:.0f} MB, {est['records']:,} records (before cache hits)")
    if a.estimate:
        return
    if not a.yes and input("Download? [y/N] ").strip().lower() != "y":
        return
    data.download(start, end, a.symbol, force=a.force)


def _out_name(symbol: str, name: str) -> str:
    """MNQ keeps the original file names; other symbols get a prefix so MNQ results are never overwritten."""
    return name if symbol == data.SYMBOL else f"{symbol.split('.')[0].lower()}_{name}"


def cmd_run(a):
    base, symbol = {}, data.SYMBOL
    if a.config:
        import tomllib
        cfg = tomllib.loads(Path(a.config).read_text())
        base, symbol = cfg.get("base", {}), cfg.get("data", {}).get("symbol", symbol)
    symbol = a.symbol or symbol
    p = _parse_params(a.param, base)
    df = data.load_1m(a.data or data.parquet_path(symbol))
    if a.start or a.end:
        df = df.loc[a.start:a.end]
    bars = data.prepare(df, p.tz)
    t = run(bars, p)
    print("Params (non-default):", {k: v for k, v in p.to_dict().items() if v != Params().to_dict()[k]} or "defaults")
    if t.empty:
        print("No trades.")
        return
    print(_fmt(summarize(t, bars.dates, p.account)))
    print("\nBy year:\n" + by_year(t).round(2).to_string())
    print("\nBootstrap max drawdown (R):", {k: round(v, 1) for k, v in mc_drawdown_r(t.r.values).items()})
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / (a.out or _out_name(symbol, "run_trades.csv"))
    t.to_csv(out, index=False)
    print(f"\nTrades -> {out}")


def cmd_grid(a):
    from .grid import GridSpec, is_oos_correlation, rank, run_grid, walk_forward

    spec = GridSpec.load(a.spec)
    df = data.load_1m(a.data or data.parquet_path(spec.symbol))
    table, results, bars, split = run_grid(df, spec)
    out = RESULTS / Path(a.spec).stem
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "grid.csv", index=False)

    varied = list(spec.grid)
    s = spec.sort
    cols = varied + [f"is_{s}", "is_trades_yr", "is_total_r", f"oos_{s}", "oos_total_r", "oos_pf", "all_max_dd"]
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    print(f"\nIS < {split} <= OOS.  Ranked by IS {s} (min {spec.min_trades_yr} trades/yr)")
    print(rank(table, spec).head(a.top)[cols].round(2).to_string(index=False))
    rho = is_oos_correlation(table, spec)
    print(f"\nIS vs OOS rank correlation ({s}): {rho:.2f}  (≈0 means IS ranking doesn't carry over)")
    base = table[(table[varied] == pd.Series(Params.from_dict(spec.base).to_dict())[varied]).all(axis=1)]
    if len(base):
        print("Baseline (base params) IS/OOS:", base[[f"is_{s}", f"oos_{s}", "is_total_r", "oos_total_r"]].round(2).to_dict("records")[0])

    folds, wf = walk_forward(results, bars, spec)
    if len(folds):
        folds.to_csv(out / "walkforward_folds.csv", index=False)
        wf.to_csv(out / "walkforward_trades.csv", index=False)
        print(f"\nWalk-forward ({spec.train_months}m train / {spec.test_months}m test):")
        print(folds.drop(columns="params").round(2).to_string(index=False))
        for _, f in folds.iterrows():
            print(f"  {f.test}: {json.dumps(f.params)}")
        if len(wf):
            dates = np.unique(np.concatenate([b.dates for b in bars.values()]))
            dates = dates[dates >= np.datetime64(wf.date.min().date())]
            print("\nStitched walk-forward OOS performance:")
            print(_fmt(summarize(wf, dates, Params.from_dict(spec.base).account)))
    print(f"\nResults -> {out}/")


def main():
    ap = argparse.ArgumentParser(prog="orb", description="London/NY ORB backtest suite (MNQ, ES; 1m)")
    sub = ap.add_subparsers(required=True)

    d = sub.add_parser("download", help="download 1m bars from Databento (asks before spending); --symbol ES.v.0 for ES")
    d.add_argument("--start", help="YYYY-MM-DD (default: 5 years before --end)")
    d.add_argument("--end", help="YYYY-MM-DD, exclusive (default: today)")
    d.add_argument("--symbol", default=data.SYMBOL)
    d.add_argument("--estimate", action="store_true", help="only print the cost")
    d.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    d.add_argument("--force", action="store_true", help="re-download cached years")
    d.set_defaults(fn=cmd_download)

    r = sub.add_parser("run", help="backtest one parameter set")
    r.add_argument("-p", "--param", action="append", default=[], metavar="KEY=VALUE",
                   help="override a parameter, e.g. -p rr=1.5 -p entry=touch (repeatable)")
    r.add_argument("--config", help="TOML file whose [base] table sets parameters")
    r.add_argument("--start", help="first date to include")
    r.add_argument("--end", help="last date to include")
    r.add_argument("--symbol", help="cached symbol to load, e.g. ES.v.0 (default: --config's [data] symbol, else MNQ.v.0)")
    r.add_argument("--data", help="Parquet file (default: data/<symbol>_1m.parquet)")
    r.add_argument("--out", help="trades CSV filename inside results/ (default: run_trades.csv, es_run_trades.csv for ES.v.0, ...)")
    r.set_defaults(fn=cmd_run)

    g = sub.add_parser("grid", help="grid search + IS/OOS + walk-forward from a TOML spec")
    g.add_argument("spec", help="e.g. grids/default.toml")
    g.add_argument("--data", help="Parquet file (default: the spec's [data] symbol, else MNQ.v.0)")
    g.add_argument("--top", type=int, default=20)
    g.set_defaults(fn=cmd_grid)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
