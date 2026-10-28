"""Databento download + local Parquet cache + per-timezone bar prep.

One streaming request per calendar year (about 20 MB each) keeps downloads resumable.
Raw `.dbn.zst` files are kept so that rebuilding the Parquet never costs money twice.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"

DATASET = "GLBX.MDP3"          # CME Globex
SCHEMA = "ohlcv-1m"            # ts_event = bar open; minutes without trades are not emitted
SYMBOL = "MNQ.v.0"             # front month by previous day's volume, unadjusted prices


def parquet_path(symbol: str = SYMBOL) -> Path:
    return DATA_DIR / f"{symbol.replace('.', '_')}_1m.parquet"


def _client():
    import databento as db

    key = os.environ.get("BENTO") or os.environ.get("DATABENTO_API_KEY")
    if not key:
        raise SystemExit("Set BENTO (source ~/.config/databento/env) or DATABENTO_API_KEY.")
    return db.Historical(key)


def _year_chunks(start: str, end: str):
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    for y in range(s.year, e.year + 1):
        a, b = max(s, pd.Timestamp(f"{y}-01-01")), min(e, pd.Timestamp(f"{y + 1}-01-01"))
        if a < b:
            yield y, a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")


def estimate(start: str, end: str, symbol: str = SYMBOL) -> dict:
    c = _client()
    kw = dict(dataset=DATASET, symbols=[symbol], stype_in="continuous", schema=SCHEMA, start=start, end=end)
    return {"cost_usd": c.metadata.get_cost(**kw),
            "size_mb": c.metadata.get_billable_size(**kw) / 1e6,
            "records": c.metadata.get_record_count(**kw)}


def download(start: str, end: str, symbol: str = SYMBOL, force: bool = False) -> Path:
    """Stream each year to data/raw/*.dbn.zst (skipping existing complete years), then build Parquet.

    The current, still-growing year is always re-downloaded so the cache can be topped up.
    """
    c = _client()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    this_year = pd.Timestamp.now(tz="UTC").year
    for y, a, b in _year_chunks(start, end):
        f = RAW_DIR / f"{symbol.replace('.', '_')}_{SCHEMA}_{y}.dbn.zst"
        if f.exists() and not force and y != this_year:
            print(f"  {f.name}: cached")
            continue
        print(f"  {f.name}: downloading {a} -> {b}")
        c.timeseries.get_range(dataset=DATASET, symbols=[symbol], stype_in="continuous",
                               schema=SCHEMA, start=a, end=b, path=f)
    return build_parquet(symbol)


def build_parquet(symbol: str = SYMBOL) -> Path:
    import databento as db

    files = sorted(RAW_DIR.glob(f"{symbol.replace('.', '_')}_{SCHEMA}_*.dbn.zst"))
    if not files:
        raise SystemExit(f"No raw files in {RAW_DIR}; run `orb download` first.")
    frames = []
    for f in files:
        df = db.DBNStore.from_file(f).to_df(price_type="float", pretty_ts=True, map_symbols=True)
        frames.append(df[["instrument_id", "symbol", "open", "high", "low", "close", "volume"]])
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "ts"
    out = parquet_path(symbol)
    df.to_parquet(out)
    print(f"  wrote {out.name}: {len(df):,} bars {df.index[0]} -> {df.index[-1]}, "
          f"{df.instrument_id.nunique()} contracts")
    return out


def load_1m(path: str | Path | None = None) -> pd.DataFrame:
    """UTC-indexed 1m bars: instrument_id, symbol, open, high, low, close, volume."""
    df = pd.read_parquet(path or parquet_path())
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def daily_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """ATR(n) on CME trade-date daily bars (session opens 17:00 CT), indexed by trade date.

    True range ignores the previous close across a contract roll, since prices are unadjusted.
    """
    ct = df.index.tz_convert("America/Chicago")
    trade_date = (ct + pd.Timedelta(hours=7)).normalize().tz_localize(None)
    g = df.groupby(trade_date)
    d = pd.DataFrame({"h": g.high.max(), "l": g.low.min(), "c": g.close.last(),
                      "iid": g.instrument_id.last(), "iid0": g.instrument_id.first()})
    pc = d.c.shift().where(d.iid.shift() == d.iid0)
    tr = pd.concat([d.h - d.l, (d.h - pc).abs(), (d.l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


@dataclass(frozen=True)
class Bars:
    """1m bars in a local timezone, as plain numpy arrays for the engine."""
    tz: str
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    mod: np.ndarray        # minute of day, local time
    day_start: np.ndarray  # index of the first bar of each local weekday
    day_end: np.ndarray    # exclusive
    dates: np.ndarray      # datetime64[D] per day
    atr_prev: np.ndarray   # prior completed trade date's daily ATR, per day (nan if unknown)


def prepare(df: pd.DataFrame, tz: str) -> Bars:
    local = df.index.tz_convert(tz)
    keep = local.day_of_week < 5
    local, sub = local[keep], df[keep]
    day = local.normalize().tz_localize(None).values.astype("datetime64[D]")
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
    ends = np.r_[starts[1:], len(day)]
    dates = day[starts]
    atr = daily_atr(df)
    # last trade date strictly before the local date = last completed daily bar (no lookahead)
    pos = np.searchsorted(atr.index.values.astype("datetime64[D]"), dates, side="left") - 1
    atr_prev = np.where(pos >= 0, atr.values[np.clip(pos, 0, None)], np.nan)
    f = lambda col: np.ascontiguousarray(sub[col].to_numpy(np.float64))
    return Bars(tz, f("open"), f("high"), f("low"), f("close"),
                (local.hour * 60 + local.minute).to_numpy(np.int32),
                starts.astype(np.int64), ends.astype(np.int64), dates, atr_prev)
