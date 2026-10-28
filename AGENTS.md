# AGENTS.md

London Opening-Range-Breakout (ORB) strategy for **MNQ** (Micro E-mini Nasdaq-100 futures, $2/pt).
The repo has TradingView Pine Script v6 versions of the strategy and Python scripts that replicate
it bar by bar to backtest it and grid-search entry filters.

## Files

| File | What it is |
|---|---|
| `london_orb_mnq.pine` | **Indicator** (`indicator(...)`). Simulates trades by hand on the chart: range box, entry/SL/TP lines, labels, alerts, info table. This is the reference spec that `backtest_orb.py` copies. |
| `london_orb_mnq_strategy.pine` | **Strategy** (`strategy(...)`). Same rules, but runs on TradingView's broker emulator (`strategy.entry/exit`), with commission and slippage set in the script header. |
| `london_orb_mnq_filters.pine` | Strategy plus optional entry filters (VWAP, EMA, EMA stack, RSI, MACD, FVG, range/daily-ATR). All filters are **off by default**, so with defaults it behaves like `_strategy.pine`. |
| `ny_orb_mnq_strategy.pine` | **Strategy** for the US cash open: a copy of `_strategy.pine` with NY defaults (09:30–10:00 `America/New_York` range, Touch entry, opposite stop, 2R, entries to 12:00, flat by 15:55, no points range filter). Matches `backtest/grids/ny_open.toml` and the "NY open" config in `backtest/web/build.py`. Trading rules are identical to London, so rule changes apply here too. |
| `london_orb_es.pine`, `london_orb_es_strategy.pine`, `london_orb_es_filters.pine`, `ny_orb_es_strategy.pine` | **ES/MES copies** of the four MNQ scripts. Same rules, guard accepts `MES` or `ES`, London range filter scaled to 2–16 pts, MES commission defaults. Backtested by `backtest/grids/es_london.toml` / `es_ny_open.toml` (ES.v.0 prices, MES $5/pt sizing). Rule changes apply here too. |
| `backtest_orb.py` | Python copy of the indicator logic. Runs 5 preset configs and prints stats. |
| `grid_search_filters.py` | Python copy of the filter logic. Tests about 360 filter combinations with a 60/40 in-sample/out-of-sample split. |
| `backtest_trades_default.csv` | Trade log for the default config (49 trades, 2026-08-03 → 2026-10-09). Local only, git-ignored. |
| `grid_results.csv` | Output of `grid_search_filters.py` (one row per filter config; `all_*`, `IS_*`, `OOS_*` metrics). Local only, git-ignored. |
| `backtest/` | **Main backtest suite** (its own uv project): Databento MNQ 1m download, numba engine, grid search with IS/OOS and walk-forward. See `backtest/README.md` and the section below. |
| `pyproject.toml`, `.python-version`, `.venv/` | uv project config, the pinned Python (3.12) and the uv-managed virtualenv. See "Running the Python backtests". |
| `main.py`, `README.md` | Placeholders from `uv init` (`README.md` is empty). |

## Strategy rules (defaults; keep every implementation in sync)

- Timezone `UTC+2`, a **fixed offset with no DST**. In Python it is `Etc/GMT-2`, because the POSIX sign is inverted, so `Etc/GMT-2` means UTC+2. Don't "fix" it to `Etc/GMT+2`.
- Opening range: 07:00–07:15. Entry window: 07:15–11:00. Holding window: 07:15–22:00. Mon–Fri only.
  Python stores these as minutes from midnight: `orw=(420,435)`, `entw=(435,660)`, `holdw=(435,1320)`.
- Range filter: 10 ≤ range ≤ 80 pts, or no trade that day.
- Entry: the first bar that **closes** outside the range (`Close` mode) or trades through it (`Touch` mode). **Max 1 trade per day.** A day whose size works out to < 1 contract counts as traded.
- Stop: opposite side of the range (or the midpoint). Target: 2R from entry.
- Sizing: `qty = min(maxContracts=10, floor(risk$ / (stopPts*2 + 1.24)))`. Risk is 1% of $50k.
- Exit checks start on the bar after entry. If a bar hits both SL and TP, **SL wins** (conservative). Fills: `min/max(open, level)` to model gaps. Any open trade is closed when the holding window ends ("EOD").
- Costs: $1.24 round trip per contract. Pine strategies set `commission_value = 0.62` per side plus `slippage = 1` tick. The Python scripts have **no slippage**.

### Known differences between implementations
- The indicator exits EOD at the *open of the first bar outside* the holding window. The strategies close at the *close of the last bar inside* it (`process_orders_on_close` plus `bars_back = -1` look-ahead on session checks).
- `Touch` mode: the indicator and `backtest_orb.py` fill at `max(open, H)` / `min(open, L)`. The strategies place OCA stop orders one tick beyond the range.
- OR completeness check: `backtest_orb.py` needs `(orw[1]-orw[0])//5` bars, which assumes 5m data. `grid_search_filters.py` hardcodes `>= 3`.
- Filters are evaluated on the breakout bar. The Pine filters script also has a "Skip the day" mode, which the grid search doesn't model (the grid search always waits for a filtered breakout).

If you change a rule, change it in **all** relevant files (the 4 MNQ and 4 ES Pine scripts, `backtest/orb/engine.py`, and the 2 legacy Python scripts) or document the new difference here. The backtest engine doesn't read the `.pine` files. Follow the "If you change a Pine script" checklist and the differences table in `backtest/README.md`.

## Backtest suite (`backtest/`)

Use this for any new backtesting work. The top-level `backtest_orb.py` and `grid_search_filters.py` are the older 5m prototypes.

```bash
cd backtest && source ~/.config/databento/env   # BENTO = Databento API key; never print or commit it
uv run orb download --estimate                  # Databento downloads cost money: always estimate and ask the user first
uv run orb run -p rr=1.5                        # one config
uv run orb grid grids/default.toml              # sweep + IS/OOS + walk-forward -> results/
uv run pytest                                   # must pass after engine changes
```

- `backtest/` has its own `pyproject.toml`, `uv.lock` and `.venv`. Run `uv` commands from inside it.
- Data is `GLBX.MDP3` `ohlcv-1m`, `MNQ.v.0` (continuous, unadjusted), cached in `backtest/data/` (git-ignored).
- **ES namespace:** `ES.v.0` data (`orb download --symbol ES.v.0`), specs `grids/es_*.toml` (`[data] symbol`), results `results/es_*/`, `results/es_run_trades.csv`, site `web/es/`. Sizing uses MES terms (`point_value = 5`). Never write ES output to the MNQ paths. See "ES (S&P 500) namespace" in `backtest/README.md`.
- The engine judges breakouts on `signal_tf` bars but checks exits on 1m bars. Rule changes go in `orb/engine.py`, plus a test in `tests/test_engine.py` and a note in `backtest/README.md`.
- Sharpe and returns are computed against the fixed account (sizing doesn't compound). Judge results by the walk-forward and IS/OOS rank correlation, not the top in-sample row.

## Running the Python backtests (legacy prototypes)

The project is managed with **[uv](https://docs.astral.sh/uv/)**:

- `pyproject.toml` declares the project (`requires-python = ">=3.12"`). `.python-version` pins **3.12**.
- `.venv/` is the uv-managed virtualenv (CPython 3.12, created by uv). Don't create other venvs, and don't use bare `pip install` or the system `python`.
- Run everything through uv: `uv run python <script>` uses `.venv` and syncs it first.
- Add dependencies with `uv add <pkg>`, which updates `pyproject.toml` and `uv.lock`. Never edit `.venv` by hand.
- `main.py` is the placeholder that `uv init` creates. It isn't part of the strategy.

**Setup note:** `dependencies = []` is currently empty, but both scripts need `pandas` and `numpy`. Before running them, add them once:

```bash
uv add pandas numpy
```

The scripts also need Python ≥ 3.9 (they use the dict `|` operator). The pinned 3.12 satisfies that. There is no test suite.

```bash
# 5m OHLC pickle (yfinance-style DataFrame, tz-aware index, possibly MultiIndex columns)
uv run python backtest_orb.py data/MNQ_5m.pkl
#   -> prints stats for 5 configs; writes <pkl path minus .pkl>_trades.csv for the default config

# 5m pickle (needs Volume for VWAP) + daily pickle (for ATR) + output csv
uv run python grid_search_filters.py data/MNQ_5m.pkl data/MNQ_1d.pkl grid_results.csv
```

The `.pkl` data files are **not in the repo**. Ask the user where they are, or how they were downloaded, instead of guessing. The Python code expects `Open/High/Low/Close[/Volume]` columns and a tz-aware `DatetimeIndex`.

## Pine Script conventions

- `//@version=6`. Inputs are grouped with `const string G_*` group names. Section headers look like `// ───── Name ─────`.
- Guards: the scripts only trade when `syminfo.root == "MNQ"` and the timeframe is intraday, ≤ 15m, and divides 15 (1/3/5/15m). Strategies call `runtime.error` otherwise.
- Use `syminfo.pointvalue` rather than a hardcoded `2`. Use `format.mintick` for price strings.
- `request.security` for daily ATR uses `ta.atr(14)[1]` with `lookahead_on` (prior completed day, so no repaint). Keep any new HTF data non-repainting the same way.
- No Pine compiler runs locally. Changes have to be checked by pasting into the TradingView Pine Editor, so say this in your summary when you edit a `.pine` file.

## Python conventions

- Terse, numpy-heavy style: short names (`o,h,l,c`, `ep`, `sl`, `tp`, `rng`, `q`), one-line helpers, module constants `PV`, `COMM`, `ACCT`, `RISK`, `MAXC`. Match it.
- Stats are computed from **daily** P&L over all eligible weekdays (zero-P&L days included). Sharpe is annualised with √252.

## Research caveats (mention them when reporting results)

- The sample is small: about 49 trades over about 2.5 months (Aug–Oct 2026). The grid tests about 360 configs on it, so the top grid rows are very likely overfit. Weigh OOS results and the "beat baseline in both IS and OOS" section over the full-period rankings.
- Python results won't exactly match the TradingView strategy tester (slippage, EOD fill and Touch fill differences listed above).

## Git

The repo has no commits yet. Every file is untracked on `master`, and `main` is the intended main branch. Only commit when asked.
`.gitignore` excludes `.venv/`, caches, all data files (`*.pkl`, `*.csv`, `*.parquet`, `data/`, …), secrets and local config (`.env*`, `.claude/`, `*.local.*`), and OS/editor junk (`.DS_Store`, `*:Zone.Identifier`, `.vscode/`, …). Don't force-add (`git add -f`) any of these. Backtest CSVs are regenerated outputs, not source. Do commit `uv.lock` once it exists.
