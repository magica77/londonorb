"""Bar-by-bar replica of london_orb_mnq.pine on 5m data."""
import sys, math
import numpy as np, pandas as pd

PV, COMM = 2.0, 1.24

def load(path, tz):
    d = pd.read_pickle(path)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d[["Open", "High", "Low", "Close"]].dropna()
    d.index = d.index.tz_convert(tz)
    return d

def hm(t): return t.hour * 60 + t.minute

def run(d, orw=(420, 435), entw=(435, 660), holdw=(435, 1320), mode="Close",
        stop="Opposite side", rr=2.0, minr=10, maxr=80, acct=50000, riskpct=1.0, maxc=10):
    risk_usd = acct * riskpct / 100
    size = lambda pts: min(maxc, math.floor(risk_usd / (pts * PV + COMM)))
    trades, days = [], set()
    for day, g in d.groupby(d.index.date):
        if pd.Timestamp(day).weekday() > 4: continue
        m = np.array([hm(t) for t in g.index])
        o, h, l, c = (g[k].values for k in ["Open", "High", "Low", "Close"])
        orm = (m >= orw[0]) & (m < orw[1])
        if orm.sum() < (orw[1] - orw[0]) // 5: continue
        days.add(day)
        H, L = h[orm].max(), l[orm].min(); rng = H - L
        if not (minr <= rng <= maxr): continue
        after = np.where(m >= orw[1])[0]
        pos = 0
        for i in after:
            inE = entw[0] <= m[i] < entw[1]; inH = holdw[0] <= m[i] < holdw[1]
            if pos:
                ex = why = None
                if not inH: ex, why = o[i], "EOD"
                elif pos == 1:
                    if l[i] <= sl: ex, why = min(o[i], sl), "SL"
                    elif h[i] >= tp: ex, why = max(o[i], tp), "TP"
                else:
                    if h[i] >= sl: ex, why = max(o[i], sl), "SL"
                    elif l[i] <= tp: ex, why = min(o[i], tp), "TP"
                if ex is not None:
                    pnl = (ex - ep) * pos * q * PV - COMM * q
                    trades.append(dict(date=day, side=pos, qty=q, entry=ep, exit=ex, why=why,
                                       risk=(abs(ep - sl) * PV + COMM) * q, pnl=pnl,
                                       r=pnl / ((abs(ep - sl) * PV + COMM) * q), range=rng))
                    break
                continue
            if not inE:
                if m[i] >= entw[1]: break
                continue
            up = c[i] > H if mode == "Close" else h[i] > H
            dn = c[i] < L if mode == "Close" else l[i] < L
            if up == dn: continue
            pos = 1 if up else -1
            ep = c[i] if mode == "Close" else (max(o[i], H) if up else min(o[i], L))
            sl = (L if up else H) if stop == "Opposite side" else (H + L) / 2
            rp = abs(ep - sl); q = size(rp) if rp > 0 else 0
            if q < 1: break
            tp = ep + pos * rr * rp
        else:
            if pos:  # data ended while in trade
                pass
    return pd.DataFrame(trades), sorted(days)

def stats(t, days, acct=50000):
    if t.empty: return dict(trades=0)
    daily = pd.Series(0.0, index=pd.Index(days))
    daily.loc[t.date.values] = t.pnl.values
    eq = acct + daily.cumsum()
    dd = (eq - eq.cummax()); ddp = (eq / eq.cummax() - 1)
    ret = daily / (acct + daily.cumsum().shift(fill_value=0))
    w, lo = t[t.pnl > 0], t[t.pnl <= 0]
    return {
        "days": len(days), "trades": len(t), "win%": 100 * len(w) / len(t),
        "net $": t.pnl.sum(), "return %": 100 * t.pnl.sum() / acct,
        "avg win $": w.pnl.mean() if len(w) else 0, "avg loss $": lo.pnl.mean() if len(lo) else 0,
        "realised RR": (w.pnl.mean() / -lo.pnl.mean()) if len(w) and len(lo) else float("nan"),
        "avg R": t.r.mean(), "profit factor": w.pnl.sum() / -lo.pnl.sum() if len(lo) else float("inf"),
        "max DD $": dd.min(), "max DD %": 100 * ddp.min(),
        "Sharpe (ann.)": ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else float("nan"),
        "exits": t.why.value_counts().to_dict(), "avg qty": t.qty.mean(),
    }

if __name__ == "__main__":
    path = sys.argv[1]
    cfgs = {
        "Default: 07:00 UTC+2, Close, opp-side, 2R": ("Etc/GMT-2", {}),
        "Touch entry": ("Etc/GMT-2", dict(mode="Touch")),
        "Midpoint stop": ("Etc/GMT-2", dict(stop="Range midpoint")),
        "1R target": ("Etc/GMT-2", dict(rr=1.0)),
        "Real London open 08:00 Europe/London": ("Europe/London",
            dict(orw=(480, 495), entw=(495, 720), holdw=(495, 1260))),
    }
    for name, (tz, kw) in cfgs.items():
        t, days = run(load(path, tz), **kw)
        print(f"\n== {name}")
        for k, v in stats(t, days).items():
            print(f"  {k:15} {v:,.2f}" if isinstance(v, float) else f"  {k:15} {v}")
        if name.startswith("Default"):
            t.to_csv(path.replace(".pkl", "_trades.csv"), index=False)
