# London ORB backtest suite (MNQ, 1-minute)

Validates `london_orb_mnq_strategy.pine` on 5 years of Databento MNQ 1m data and grid-searches
its parameters, with in-sample/out-of-sample and walk-forward checks built in.

## Quick start

```bash
cd backtest
source ~/.config/databento/env            # exports BENTO (your Databento API key)

uv run orb download --estimate             # print the cost, download nothing
uv run orb download                        # 5 years to today; asks before spending
uv run orb run                             # Pine defaults
uv run orb run -p rr=1.5 -p entry=touch -p tz=Europe/London -p or_start=08:00
uv run orb grid grids/default.toml         # grid + IS/OOS + walk-forward
uv run pytest                              # rule tests on synthetic bars
```

## ES (S&P 500) namespace

Results and conclusions from the first run (2026-10-27): **`ES_SUMMARY.md`**.

The same suite runs ES with nothing MNQ-owned touched. Prices come from `ES.v.0` (full-size E-mini, deepest
book). P&L and sizing use **MES** terms ($5/pt, $1.24 RT, $50k, 1% risk, max 10 contracts), the micro
equivalent of the MNQ setup. Full-size ES at $50/pt would size most days to 0 contracts on $50k.

| | MNQ | ES |
|---|---|---|
| Raw data / Parquet | `data/raw/MNQ_v_0_*`, `data/MNQ_v_0_1m.parquet` | `data/raw/ES_v_0_*`, `data/ES_v_0_1m.parquet` |
| Grid specs | `grids/default.toml`, `grids/ny_open.toml` | `grids/es_london.toml`, `grids/es_ny_open.toml` (`[data] symbol = "ES.v.0"`) |
| Grid results | `results/default/`, `results/ny_open/` | `results/es_london/`, `results/es_ny_open/` |
| `orb run` trades | `results/run_trades.csv` | `results/es_run_trades.csv` |
| Website | `web/index.html` + `web/data.js` | `web/es/index.html` + `web/es/data.js` |
| Pine | `*_mnq*.pine` | `*_es*.pine` (accepts MES or ES charts; size follows `syminfo.pointvalue`) |

```bash
uv run orb download --symbol ES.v.0 --estimate      # cost first
uv run orb download --symbol ES.v.0                 # 5 years, asks before spending
uv run orb run --config grids/es_london.toml        # always pass an ES --config: it sets point_value = 5
uv run orb grid grids/es_london.toml
uv run orb grid grids/es_ny_open.toml
uv run python web/build.py --spec grids/es_london.toml --out web/es/data.js
```

`orb run --symbol ES.v.0` without `--config` loads ES prices but keeps the MNQ `point_value = 2`. Use the
config. The London points filter is scaled from MNQ's 10..80 to **2..16**: measured 07:00 UTC+2 ranges are
about 1/5 of MNQ's at every percentile (median 2.75 vs 14 pts, 2021–2026).

## Data

| | |
|---|---|
| Dataset / schema | `GLBX.MDP3` (CME Globex), `ohlcv-1m`. `ts_event` is the bar **open**. Minutes with no trades have no bar. |
| Symbol | `MNQ.v.0` with `stype_in="continuous"`: the front month picked by the previous day's volume. Prices are **unadjusted** (no back-adjusting). |
| Method | Streaming `timeseries.get_range`, one request per calendar year, saved to `data/raw/*.dbn.zst`. The full 5 years is about 1.8M bars, about 100 MB and **about $6.50** (checked 2026-10-26). Databento suggests batch jobs only above about 5 GB. |
| Cache | Raw files are kept, and `data/MNQ_v_0_1m.parquet` is rebuilt from them. Years already cached are skipped, and the current year is re-fetched so the data can be topped up. |

Unadjusted prices are fine here. The strategy is flat overnight, and `v.0` rolls at the daily boundary, so a roll never lands inside a trade. The daily ATR filter ignores the previous close across a roll.

## Engine rules (`orb/engine.py`)

Same rules as the Pine strategy, plus one change: **breakouts are judged on `signal_tf`-minute bars,
but stops and targets are checked on the 1m bars underneath**. That removes most cases where a single
bar touches both SL and TP. If it still happens, the stop wins.

- The opening range is `[or_start, or_start + or_minutes)` in `tz`. The default `Etc/GMT-2` is a fixed UTC+2, like the Pine default.
- Skip the day if the range is outside `[min_range, max_range]` points, or `range / prior-day ATR(14)` is outside `[atr_min, atr_max]`.
- `entry="close"` fills at the close of the first signal bar that closes outside the range. `entry="touch"` uses a stop order one tick beyond the range.
- Stop is the opposite side of the range or the midpoint. Target is `rr` × the actual risk from the fill. Max one trade per day. Any open trade exits at `hold_end`.
- Size is `min(max_contracts, floor(account × risk_pct / (stop_pts × $2 + commission_rt)))`. The account size is fixed, with no compounding.
- Costs: `commission_rt` ($1.24 by default) plus `slippage_ticks` (1 by default), charged on entries, stops and EOD exits. Limit targets have no slippage.

All parameters, with defaults, are listed in `Params` in `orb/engine.py`.

## Differences from the Pine scripts

None of the Pine scripts were changed when this suite was built. The engine is a **re-implementation** of
`london_orb_mnq_strategy.pine` (the closest match) and does not read the `.pine` files, so editing a Pine
script does **not** update the backtest. Expect small gaps against the TradingView strategy tester, for
these reasons:

| Area | Pine scripts | This engine |
|---|---|---|
| **Exit checks** (the main difference) | SL/TP are checked on chart bars (e.g. 5m). The indicator counts a bar that touches both as SL. The strategy leaves it to TradingView's broker emulator. | Breakouts use `signal_tf` bars, but SL/TP are walked on **1m bars**, so most "both in one bar" cases are settled by what actually happened first. If one 1m bar still touches both, SL wins. |
| EOD exit | Strategy: close of the last bar inside `holdSession`. Indicator: open of the first bar outside it. | Open of the first 1m bar at or after `hold_end`, like the indicator. On 1m bars that's about the strategy's price too. |
| Touch entry price | Strategy: stop orders one tick beyond the range. Indicator: `max(open, orHigh)` with no extra tick. | One tick beyond, like the strategy. If one bar crosses both levels, the side nearer the open fills first, and SL is checked on the fill bar itself. |
| Touch sizing | Each side sized separately. A side with size < 1 just isn't placed. | If the side that triggers has size < 1, the day is skipped. |
| Slippage / costs | Strategy: `commission_value = 0.62` per side, `slippage = 1` tick. The indicator has no slippage. | `commission_rt` = 1.24 and `slippage_ticks` = 1, on entries, SL and EOD. TP limit fills have no slippage. |
| Sizing base | Strategy and filters scripts can size from strategy equity (compounding). | Fixed account only. |
| Opening-range check | None. | Skips the day if fewer than half of the OR minutes have a 1m bar (thin overnight data). |
| Daily ATR | `request.security("D", ta.atr(14)[1])`, the TradingView daily bar. | ATR(14) from 1m data grouped by CME trade date (session opens 17:00 CT), prior day only. TR ignores the previous close across a roll. Should be close but not identical. |
| Entry filters | `_filters.pine` has VWAP, EMA, EMA stack, RSI, MACD, FVG and range/ATR. | **Only** the range/ATR filter (`atr_min` / `atr_max`). The others aren't ported. |
| Guards | Trades only on MNQ, on timeframes ≤ 15m that divide 15. | No symbol check. `signal_tf` can be any number of minutes. |

Same in both: the range filter in points, close-breakout entry at the bar close, stop at the opposite side
or midpoint, the target re-anchored to the actual fill (`rr × |fill − stop|`), one trade per day (a day whose
size works out to < 1 contract counts as traded), and the contract-sizing formula.

`ny_orb_mnq_strategy.pine` is the same strategy script with NY-open defaults (`grids/ny_open.toml`, the "NY open" featured config). Everything in the table applies to it too. One more case: on CME early-close days (e.g. the day after Thanksgiving) the market closes before 15:55. The script then exits on the first bar outside the holding window (the 18:00 ET reopen), and the engine does the same at that bar's open.

### If you change a Pine script

1. Decide whether the change is a **rule** change (entries, exits, filters, sizing, sessions) or only
   **display/alerts**. Display changes need nothing here.
2. For rule changes, update `orb/engine.py`. If it needs a new setting, add a `Params` field and pass it
   into `_simulate`.
3. Add or adjust a test in `tests/test_engine.py` that pins down the new rule on a hand-built day.
4. Update the table above so the remaining differences stay documented.
5. Optional cross-check: export the TradingView strategy tester's trade list over the same dates and
   compare it with `uv run orb run -p signal_tf=<chart tf> --start … --end …`. Trade dates and sides should
   match. Exit prices can differ, mostly because of the 1m exit checks.

There's no "exits on chart bars" option yet for an exact Pine match. Adding one would mean replacing the
1m exit loop with one that checks SL/TP only at signal-bar boundaries using that bar's high and low.

## Grid search and validation (`grids/*.toml`)

Specs: `grids/default.toml` (London session, UTC+2) and `grids/ny_open.toml` (the same rules at the
09:30 New York cash open, points range filter off). ES copies: `grids/es_london.toml`, `grids/es_ny_open.toml`.

`[base]` holds fixed parameters, `[grid]` holds lists to sweep, `[validation]` sets the ranking metric and the OOS split, and `[walkforward]` sets the window sizes. Each config is simulated **once**; IS/OOS and walk-forward just slice those trades by date, so validation adds almost no run time. About 860 configs over 5 years run in a few seconds.

Output goes to `results/<spec name>/`:
- `grid.csv`: parameters plus `all_*`, `is_*`, `oos_*` metrics for every config.
- `walkforward_folds.csv` and `walkforward_trades.csv`: the config picked in each training window, and its trades in the test window that follows.

How to read the output:
1. **IS vs OOS rank correlation.** Near 0 or negative means picking parameters on past data doesn't help, so the top of the table is noise.
2. **Stitched walk-forward performance.** The most honest number you get here: every trade in it was chosen without looking at its own period.
3. **Parameter stability.** Good parameters sit on a plateau (their neighbours in `grid.csv` also do well), not on an isolated spike. If the walk-forward picks very different parameters in every fold, the "edge" is the optimiser fitting noise.

As a check, a random-walk dataset gives a rank correlation of about 0 and a negative walk-forward result, which is what the suite should show when there's no edge.

## Results website (`web/`)

```bash
uv run python web/build.py            # re-runs featured configs, grid and walk-forward -> web/data.js (~10 s)
explorer.exe "$(wslpath -w web/index.html)"   # WSL: open in your Windows browser (or open the file directly)
```

**Best strategies across all four grids** (MNQ/MES × London/NY): `web/best/index.html`. It ranks every
config by robust score = min(in-sample Sharpe, out-of-sample Sharpe), compares the top 3, lists the top 20,
walks through three real trades by hand, and shows the IS/OOS split and walk-forward folds. Rebuild it after
re-running any of the four grids:

```bash
uv run python web/build_best.py        # reads results/{default,ny_open,es_london,es_ny_open}/ -> web/best/data.js
```

A single static page (Chart.js from a CDN, no server needed) with sections for: findings, equity curves, R by year,
parameter sensitivity (in-sample vs out-of-sample), an IS-vs-OOS scatter, a filterable/sortable grid explorer,
walk-forward folds, a trade browser, and data quality. The featured configs are listed in `FEATURED` in
`web/build.py`. Rebuild after a new download or a grid change. `web/data.js` is git-ignored.

## Layout

```
orb/data.py      Databento download, Parquet cache, timezone/day prep, daily ATR
orb/engine.py    Params + numba simulation
orb/metrics.py   stats (Sharpe on daily P&L, R-based drawdown, bootstrap DD)
orb/grid.py      grid expansion, IS/OOS, walk-forward
orb/cli.py       `orb download | run | grid`
web/             results website: build.py -> data.js, index.html
grids/           grid specs (TOML)
tests/           rule tests on hand-built bars
data/, results/  git-ignored
```

## Why not NautilusTrader or VectorBT?

The strategy places at most one bracket order a day on one instrument. A ~150-line numba loop models it
exactly (SL-first, gap fills, slippage, one-tick stop entries) and runs a full grid in seconds, and you can
read the entire engine in one sitting. If this moves toward live trading, porting to NautilusTrader is the
natural next step. Use this suite to decide whether that's worth doing.
