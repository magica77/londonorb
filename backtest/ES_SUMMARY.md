# ES ORB backtest summary (London + NY open)

Run 2026-10-27. Prices: Databento `GLBX.MDP3` `ohlcv-1m` `ES.v.0`, 2021-10-27 → 2026-10-27 (1,293 weekdays).
P&L and sizing use **MES** terms: $5/pt, $1.24 round trip, 1 tick of slippage on entries, stops and EOD exits,
a $50k fixed account, 1% risk and max 10 contracts. The rules, grids and validation are the same as the MNQ runs
(`grids/default.toml`, `grids/ny_open.toml`), so the numbers compare directly. IS = up to 2025-04-30, OOS = after it.

| Output | Path |
|---|---|
| Specs | `grids/es_london.toml`, `grids/es_ny_open.toml` |
| Grid + walk-forward | `results/es_london/`, `results/es_ny_open/` |
| London default trades | `results/es_run_trades.csv` |
| Results page | `web/es/index.html` |

## Bottom line

- **London ORB doesn't work on ES.** All 864 grid configs lose money over five years, and none is positive both in-sample and out-of-sample. The walk-forward made **−72.4R**. Don't trade `london_orb_es*.pine`.
- **NY-open ORB on ES has a weak edge that is fading.** It is about half of MNQ's edge for the same settings. Nearly all of it came from 2022–2024; 2025 and 2026 lost money. The walk-forward broke even (**−1.4R over 741 trades, PF 1.01**). Not good enough to trade on its own.
- The MNQ results hold up a little better in a cross-market check. ES and MNQ rank the grid configs similarly (Spearman 0.61 London, 0.68 NY), and 101 of the 104 profitable ES NY configs are profitable on MNQ too. Still, MNQ is the better instrument for this strategy at every setting tested.

## London (07:00 UTC+2, `es_london`)

Range filter: MNQ's 10–80 pts scaled to **2–16 pts**. Measured 07:00–07:15 ranges are about 1/5 of MNQ's at every
percentile (p5/25/50/75/95: ES 1 / 1.75 / 2.75 / 4.5 / 9, MNQ 5.25 / 9 / 14 / 22.9 / 50.6).

| Config | ES net of costs | ES with no costs | MNQ (same rules) |
|---|---|---|---|
| Pine default (07:00, 15m, close, 2R) | −155.8R, PF 0.81, 946 trades | −40.0R, PF 0.96 | +33.3R, PF 1.08 |
| MNQ "candidate" (10:00, 30m, touch, 3R) | −34.0R, PF 0.97 | +54.0R, PF 1.09 | +118.5R, PF 1.18 |

- **Pine default by year:** 2021 −0.2R (partial year), 2022 −83.6, 2023 −0.0, 2024 −42.7, 2025 −35.6, 2026 +6.3.
- **Grid:** 0 of 864 configs are positive. The best is 10:00 / 30m / touch / 3R at −31.8R. IS/OOS rank correlation is 0.15. Every parameter value averages a loss of 0.11–0.24R per trade, in-sample and out-of-sample.
- **Walk-forward (24m train / 6m test):** −72.4R over 512 trades, PF 0.81, Sharpe −1.26. Five of six folds lost.
- **Why it fails:**
  - **Costs eat the edge.** A 2–5 pt ES stop is only 8–20 ticks. One tick of slippage each way plus $1.24 comes to about 0.75 pt a trade, roughly 0.1–0.25R. Even with no costs, the default rules still lose.
  - **Positions are capped.** Size hits the 10-contract cap almost every day (average 9.8), so real risk is about 0.3% of the account, not 1%. R results are unaffected, but dollar P&L understates what full 1% risk would do.

## NY open (09:30 New York, `es_ny_open`)

| Config | ES net of costs | ES with no costs | MNQ (same rules) |
|---|---|---|---|
| Grid baseline (15m, close on 5m, 2R) | +47.4R, PF 1.09, Sharpe 0.59 | +98.0R | +96.2R, PF 1.18, Sharpe 1.09 |
| Pine NY default (30m, touch, 2R) | +18.5R, PF 1.04, Sharpe 0.25 | +67.3R | +91.3R, PF 1.19, Sharpe 1.12 |

- **Pine NY default by year:** 2021 −5.2R (partial year), 2022 +24.5, 2023 +5.0, 2024 +13.2, 2025 −5.1, 2026 −14.0.
- **Grid baseline:** +51.1R in-sample, −3.7R out-of-sample.
- **Grid:** 104 of 336 configs are positive, and 47 are positive both in-sample and out-of-sample. IS/OOS rank correlation is 0.30. The top full-period row is 20m / close on 1m / opposite / 2R: +78.4R, but its out-of-sample Sharpe is only 0.04, so it is an in-sample result.
- **Settings that do better in both periods** (average R/trade, IS/OOS):
  - **Range length:** 20–45m ranges (about 0.00 / −0.01) beat 5m (−0.11 / −0.09) and 10m (−0.07 / −0.06).
  - **Entry:** close (−0.017 / −0.015) beats touch (−0.058 / −0.075).
  - **Signal bar:** 5m (−0.015 / −0.006) beats 1m (−0.039 / −0.049).
  - **Mixed between periods:** stop placement, R multiple and hold_end. The same direction holds on MNQ.
- **Walk-forward:** −1.4R over 741 trades, PF 1.01, Sharpe 0.05. The first and last folds lost (−5.3R and −8.5R); the four in between made +1 to +5R each.
- **Note on Pine defaults:** the NY Pine default uses touch entries, which is the weaker choice on ES. The more robust region is a 20–45m range with close entries on 5m bars. That choice comes from in-sample data, so treat it as a hypothesis, not a validated setting.

## Caveats

- **Results won't match TradingView.** The engine checks exits on 1m bars and fills EOD at the open; slippage and touch fills also differ (see "Differences from the Pine scripts" in `README.md`). The ES Pine scripts haven't been compiled or run in TradingView.
- **Many settings were tested.** Treat the top grid rows as overfit. The walk-forward and IS/OOS numbers are the honest ones, and for ES they are flat to negative.
- **Data and costs are assumptions.** Prices are ES; trades are modelled as MES at the same price. The 1-tick slippage assumption matters a lot here: on London ES it is 0.1R or more per trade.

## Possible next steps

1. Drop London on ES.
2. Combining ES NY with MNQ NY adds little. Their profitable configs mostly overlap, so expect them to be correlated rather than diversifying.
3. To keep looking at ES NY, re-run `es_ny_open` on a narrow 20–45m / close / 5m-signal grid with `oos_start = "2025-01-01"`, and require the walk-forward to be positive before changing `ny_orb_es_strategy.pine`.
