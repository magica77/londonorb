"""Grid search of entry filters on top of the London ORB (default rules).

Filters are checked on the breakout bar: the trade is the first bar of the day that
breaks out AND passes the filter (still max 1 trade/day). Split: first 60% of days
in-sample (IS), last 40% out-of-sample (OOS).
"""
import sys, math, itertools
import numpy as np, pandas as pd

PV, COMM, ACCT, RISK, MAXC = 2.0, 1.24, 50000, 0.01, 10
TZ = "Etc/GMT-2"  # = UTC+2

def load(p5, p1d):
    d = pd.read_pickle(p5)
    d.columns = d.columns.get_level_values(0)
    d = d[["Open", "High", "Low", "Close", "Volume"]].dropna()
    d.index = d.index.tz_convert(TZ)
    dd = pd.read_pickle(p1d)
    dd.columns = dd.columns.get_level_values(0)
    return d, dd

# ---------------- indicators ----------------
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def rma(s, n): return s.ewm(alpha=1 / n, adjust=False).mean()

def rsi(c, n=14):
    ch = c.diff()
    up, dn = rma(ch.clip(lower=0), n), rma(-ch.clip(upper=0), n)
    return 100 - 100 / (1 + up / dn)

def add_indicators(d, dd):
    c = d.Close
    for n in (9, 20, 50, 100, 200):
        d[f"ema{n}"] = ema(c, n)
    tp = (d.High + d.Low + d.Close) / 3
    day = d.index.date  # CME session opens 00:00 UTC+2 (18:00 ET) in summer
    pv = (tp * d.Volume).groupby(day).cumsum()
    vv = d.Volume.groupby(day).cumsum()
    d["vwap"] = (pv / vv.replace(0, np.nan)).ffill()
    d["rsi"] = rsi(c, 14)
    macd = ema(c, 12) - ema(c, 26)
    d["macd"], d["macd_sig"] = macd, ema(macd, 9)
    d["fvg_bull"] = d.Low > d.High.shift(2)
    d["fvg_bear"] = d.High < d.Low.shift(2)
    # daily ATR(14), only data from completed prior daily bars
    tr = pd.concat([dd.High - dd.Low, (dd.High - dd.Close.shift()).abs(),
                    (dd.Low - dd.Close.shift()).abs()], axis=1).max(axis=1)
    atr = rma(tr, 14)
    atr.index = pd.to_datetime(atr.index).date
    d["datr"] = [atr[atr.index < x].iloc[-1] if (atr.index < x).any() else np.nan for x in day]
    return d

# ---------------- filters: return (long_ok, short_ok) arrays ----------------
def filt(d, kind, p):
    c = d.Close
    T = np.ones(len(d), bool)
    if kind == "none":   return T, T
    if kind == "vwap":   return (c > d.vwap).values, (c < d.vwap).values
    if kind == "ema":    return (c > d[f"ema{p}"]).values, (c < d[f"ema{p}"]).values
    if kind == "emax":   # fast/slow EMA stack
        f, s = p; return (d[f"ema{f}"] > d[f"ema{s}"]).values, (d[f"ema{f}"] < d[f"ema{s}"]).values
    if kind == "rsi_mom":  # momentum: long RSI > p, short RSI < 100-p
        return (d.rsi > p).values, (d.rsi < 100 - p).values
    if kind == "rsi_cap":  # avoid chasing: long RSI < p, short RSI > 100-p
        return (d.rsi < p).values, (d.rsi > 100 - p).values
    if kind == "macd_line": return (d.macd > d.macd_sig).values, (d.macd < d.macd_sig).values
    if kind == "macd_zero": return (d.macd > 0).values, (d.macd < 0).values
    if kind == "fvg":    # FVG in trade direction within last p bars (incl. breakout bar)
        b = d.fvg_bull.rolling(p, min_periods=1).max().astype(bool)
        s = d.fvg_bear.rolling(p, min_periods=1).max().astype(bool)
        return b.values, s.values
    raise ValueError(kind)

# ---------------- backtest ----------------
def run(d, lok, sok, minr=10, maxr=80, atr_lo=0.0, atr_hi=9.9, orw=(420, 435), entw=(435, 660), holdw=(435, 1320), rr=2.0):
    risk_usd = ACCT * RISK
    m_all = d.index.hour.values * 60 + d.index.minute.values
    o, h, l, c, datr = (d[k].values for k in ["Open", "High", "Low", "Close", "datr"])
    trades, days = [], []
    idx = pd.Series(np.arange(len(d)), index=d.index)
    for day, gi in idx.groupby(d.index.date):
        if pd.Timestamp(day).weekday() > 4: continue
        gi = gi.values; m = m_all[gi]
        orm = (m >= orw[0]) & (m < orw[1])
        if orm.sum() < 3: continue
        days.append(day)
        oi = gi[orm]; H, L = h[oi].max(), l[oi].min(); rng = H - L
        if not (minr <= rng <= maxr): continue
        if not np.isnan(datr[oi[-1]]) and not (atr_lo <= rng / datr[oi[-1]] <= atr_hi): continue
        pos = 0
        for i in gi[m >= orw[1]]:
            mi = m_all[i]
            if pos:
                ex = why = None
                if not (holdw[0] <= mi < holdw[1]): ex, why = o[i], "EOD"
                elif pos == 1:
                    if l[i] <= sl: ex, why = min(o[i], sl), "SL"
                    elif h[i] >= tp: ex, why = max(o[i], tp), "TP"
                else:
                    if h[i] >= sl: ex, why = max(o[i], sl), "SL"
                    elif l[i] <= tp: ex, why = min(o[i], tp), "TP"
                if ex is not None:
                    pnl = (ex - ep) * pos * q * PV - COMM * q
                    trades.append(dict(date=day, side=pos, qty=q, pnl=pnl, r=pnl / ((abs(ep - sl) * PV + COMM) * q), why=why))
                    break
                continue
            if mi >= entw[1]: break
            if not (entw[0] <= mi): continue
            up = c[i] > H and lok[i]
            dn = c[i] < L and sok[i]
            if up == dn: continue
            pos = 1 if up else -1
            ep = c[i]; sl = L if up else H; rp = abs(ep - sl)
            q = min(MAXC, math.floor(risk_usd / (rp * PV + COMM)))
            if q < 1: break
            tp = ep + pos * rr * rp
    return pd.DataFrame(trades), days

def stats(t, days):
    out = dict(n=len(t))
    if t.empty: return out | dict(win=np.nan, net=0, pf=np.nan, avgR=np.nan, dd=0, sharpe=np.nan)
    daily = pd.Series(0.0, index=pd.Index(days)); daily.loc[t.date.values] = t.pnl.values
    eq = ACCT + daily.cumsum(); ret = daily / eq.shift(fill_value=ACCT)
    w, lo = t.pnl[t.pnl > 0], t.pnl[t.pnl <= 0]
    return out | dict(win=100 * len(w) / len(t), net=t.pnl.sum(),
                      pf=w.sum() / -lo.sum() if len(lo) else np.inf, avgR=t.r.mean(),
                      dd=(eq - eq.cummax()).min(),
                      sharpe=ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan)

def evaluate(d, lok, sok, split, **kw):
    t, days = run(d, lok, sok, **kw)
    res = {}
    for tag, sel in (("all", lambda x: True), ("IS", lambda x: x < split), ("OOS", lambda x: x >= split)):
        dd_ = [x for x in days if sel(x)]
        tt = t[t.date.map(sel)] if not t.empty else t
        res.update({f"{tag}_{k}": v for k, v in stats(tt, dd_).items()})
    return res

if __name__ == "__main__":
    d, dd = load(sys.argv[1], sys.argv[2])
    d = add_indicators(d, dd)
    alldays = sorted({x for x in d.index.date if pd.Timestamp(x).weekday() < 5})
    split = alldays[int(len(alldays) * 0.6)]
    print("IS:", alldays[0], "→", split, " OOS:", split, "→", alldays[-1])

    singles = [("none", None), ("vwap", None), ("macd_line", None), ("macd_zero", None)]
    singles += [("ema", n) for n in (9, 20, 50, 100, 200)]
    singles += [("emax", fs) for fs in ((9, 20), (20, 50), (50, 200))]
    singles += [("rsi_mom", p) for p in (50, 55, 60)]
    singles += [("rsi_cap", p) for p in (65, 70, 75, 80)]
    singles += [("fvg", k) for k in (1, 2, 3, 6)]
    atr_bands = [(0, 9.9), (0, 0.10), (0, 0.15), (0, 0.20), (0.05, 9.9), (0.08, 9.9), (0.05, 0.15)]

    rows = []
    T = np.ones(len(d), bool)
    for kind, p in singles:
        lok, sok = filt(d, kind, p)
        rows.append(dict(filter=f"{kind}({p})" if p else kind, **evaluate(d, lok, sok, split)))
    for lo, hi in atr_bands[1:]:
        rows.append(dict(filter=f"atr_ratio[{lo},{hi}]", **evaluate(d, T, T, split, atr_lo=lo, atr_hi=hi)))
    # pairs of directional filters (+ optional ATR band)
    for (k1, p1), (k2, p2) in itertools.combinations(singles[1:], 2):
        if k1 == k2: continue
        l1, s1 = filt(d, k1, p1); l2, s2 = filt(d, k2, p2)
        rows.append(dict(filter=f"{k1}({p1}) + {k2}({p2})", **evaluate(d, l1 & l2, s1 & s2, split)))
    for (kind, p), (lo, hi) in itertools.product(singles[1:], atr_bands[1:]):
        lok, sok = filt(d, kind, p)
        rows.append(dict(filter=f"{kind}({p}) + atr_ratio[{lo},{hi}]", **evaluate(d, lok, sok, split, atr_lo=lo, atr_hi=hi)))

    r = pd.DataFrame(rows)
    r.to_csv(sys.argv[3], index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    cols = ["filter", "all_n", "all_win", "all_net", "all_pf", "all_dd", "all_sharpe", "IS_n", "IS_net", "IS_pf", "OOS_n", "OOS_win", "OOS_net", "OOS_pf"]
    print(f"\n{len(r)} configs tested")
    print("\n== Single filters"); print(r.head(len(singles) + len(atr_bands) - 1)[cols].round(2).to_string(index=False))
    print("\n== Top 15 by IN-SAMPLE net (then see how OOS holds)")
    print(r[r.IS_n >= 15].sort_values("IS_net", ascending=False).head(15)[cols].round(2).to_string(index=False))
    print("\n== Top 15 by full-period net (min 25 trades)")
    print(r[r.all_n >= 25].sort_values("all_net", ascending=False).head(15)[cols].round(2).to_string(index=False))
    base = r.iloc[0]
    both = r[(r.IS_net > base.IS_net) & (r.OOS_net > base.OOS_net) & (r.all_n >= 25)]
    print(f"\n== Beat baseline in BOTH IS and OOS: {len(both)} of {len(r)}")
    print(both.sort_values("all_net", ascending=False).head(15)[cols].round(2).to_string(index=False))
