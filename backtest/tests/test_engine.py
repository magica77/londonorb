"""Rule tests on hand-built 1m days (local tz = UTC+2, so local 07:00 = 05:00 UTC)."""
import numpy as np
import pandas as pd
import pytest

from orb.data import prepare
from orb.engine import Params, run

P = Params(slippage_ticks=0, commission_rt=0, signal_tf=1, rr=2.0)


def make_day(date: str, path: dict[str, tuple], iid: int = 1) -> pd.DataFrame:
    """1m bars from local 06:50 to 22:30; flat at 100 except bars given as {"HH:MM": (o, h, l, c)}."""
    idx = pd.date_range(f"{date} 06:50", f"{date} 22:30", freq="1min", tz="Etc/GMT-2")
    o = h = l = c = np.full(len(idx), 100.0)
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)
    for t, bar in path.items():
        df.loc[pd.Timestamp(f"{date} {t}", tz="Etc/GMT-2"), ["open", "high", "low", "close"]] = bar
    df["instrument_id"], df["symbol"], df["volume"] = iid, "MNQZ5", 1
    df.index = df.index.tz_convert("UTC")
    return df


OR = {"07:00": (100, 110, 90, 100)}  # range 90..110 = 20 pts


def trades(df, **kw):
    p = Params(**(P.to_dict() | kw))
    return run(prepare(df, p.tz), p)


def test_long_breakout_hits_target():
    df = make_day("2025-03-04", OR | {"07:20": (100, 112, 100, 112), "07:21": (112, 112, 112, 112),
                                       "08:00": (112, 160, 112, 150)})
    t = trades(df)
    assert len(t) == 1
    r = t.iloc[0]
    assert (r.side, r.entry, r.stop, r.reason) == (1, 112, 90, "TP")
    assert r.exit == 112 + 2 * 22                      # 2R from the fill
    assert r.qty == min(10, int(500 // (22 * 2)))      # 1% of 50k / (22 pts * $2)
    assert r.r == pytest.approx(2.0)


def test_stop_checked_before_target_in_same_bar():
    df = make_day("2025-03-04", OR | {"07:20": (100, 112, 100, 112), "07:30": (112, 200, 50, 100)})
    assert trades(df).iloc[0].reason == "SL"


def test_gap_through_stop_fills_at_open():
    df = make_day("2025-03-04", OR | {"07:20": (100, 112, 100, 112), "07:30": (80, 80, 70, 75)})
    assert trades(df).iloc[0].exit == 80


def test_eod_exit_at_hold_end_open():
    df = make_day("2025-03-04", OR | {"07:20": (100, 112, 100, 112)})
    df.loc[df.index > pd.Timestamp("2025-03-04 07:20", tz="Etc/GMT-2"), ["open", "high", "low", "close"]] = 111
    r = trades(df).iloc[0]
    assert (r.reason, r.exit, r.exit_time) == ("EOD", 111, "22:00")


def test_range_filter_skips_day():
    df = make_day("2025-03-04", OR | {"07:20": (100, 112, 100, 112)})
    assert trades(df, max_range=15).empty


def test_one_trade_per_day():
    df = make_day("2025-03-04", OR | {"07:20": (100, 112, 100, 112), "07:30": (112, 112, 85, 85),
                                       "07:40": (85, 85, 60, 60)})
    assert len(trades(df)) == 1


def test_signal_tf_uses_bucket_close():
    # 1m close above the range at 07:16, but the 5m bar (07:15-07:20) closes back inside.
    df = make_day("2025-03-04", OR | {"07:16": (100, 112, 100, 112), "07:19": (112, 112, 100, 101)})
    assert len(trades(df, signal_tf=1)) == 1
    assert trades(df, signal_tf=5).empty


def test_touch_entry_fills_one_tick_beyond():
    df = make_day("2025-03-04", OR | {"07:20": (105, 115, 105, 114), "08:00": (114, 200, 114, 190)})
    r = trades(df, entry="touch").iloc[0]
    assert (r.entry, r.reason) == (110.25, "TP")


def test_entry_window_end():
    df = make_day("2025-03-04", OR | {"11:05": (100, 112, 100, 112)})
    assert trades(df).empty


def test_atr_filter_uses_prior_day_only():
    # Day 1 has a huge range (ATR spikes); today's filter must not see today's own bar.
    d1 = make_day("2025-03-03", {"12:00": (100, 600, 100, 100)})
    d2 = make_day("2025-03-04", OR | {"07:20": (100, 112, 100, 112)})
    df = pd.concat([d1, d2])
    bars = prepare(df, "Etc/GMT-2")
    assert np.isnan(bars.atr_prev[0]) and bars.atr_prev[1] > 400
    assert len(trades(df, atr_max=0.15)) == 1   # 20 / ~500 passes


def test_sharpe_sign_follows_pnl_even_if_equity_goes_negative():
    from orb.metrics import stats_from_arrays
    daily = np.array([-60_000.0, 100.0, 100.0, 100.0])  # blows through a 50k account, then small wins
    s = stats_from_arrays(daily, daily, daily / 500, 50_000)
    assert s["sharpe"] < 0 and s["net"] < 0


def test_mes_point_value_sizing():
    # ES/MES specs: same day, $5/pt -> 500 // (22 pts * $5 + $1.24) = 4 contracts, P&L scales by $5/pt
    df = make_day("2025-03-04", OR | {"07:20": (100, 112, 100, 112), "08:00": (112, 160, 112, 150)})
    r = trades(df, point_value=5.0, commission_rt=1.24).iloc[0]
    assert r.qty == 4
    assert r.pnl == pytest.approx((156 - 112) * 4 * 5.0 - 1.24 * 4)


@pytest.mark.parametrize("name", ["es_london", "es_ny_open"])
def test_es_specs_load_es_data_with_mes_terms(name):
    from pathlib import Path
    from orb.grid import GridSpec
    spec = GridSpec.load(Path(__file__).resolve().parent.parent / "grids" / f"{name}.toml")
    assert spec.symbol == "ES.v.0"
    assert all(p.point_value == 5.0 and p.commission_rt == 1.24 for p in spec.configs())


def test_mnq_specs_still_default_to_mnq():
    from pathlib import Path
    from orb.grid import GridSpec
    for name in ("default", "ny_open"):
        spec = GridSpec.load(Path(__file__).resolve().parent.parent / "grids" / f"{name}.toml")
        assert spec.symbol == "MNQ.v.0" and Params.from_dict(spec.base).point_value == 2.0
