# Automating the London ORB on MNQ: Interactive Brokers + VPS

A step-by-step plan for running the ORB strategy live with a self-hosted Python bot. The bot uses
Interactive Brokers (IB) for both market data and orders and runs on a Linux VPS. The strategy
rules are the ones in `backtest/orb/engine.py` (defaults in `Params`), so live trading should
match the backtest as closely as possible.

> Status: draft plan. Nothing in `automation/` is built yet. Work through the phases in order and
> don't skip paper trading (Phase 6).

---

## 0. Architecture

```
            VPS (Ubuntu, UTC clock)
 ┌──────────────────────────────────────────────┐
 │  systemd: orb-bot (Python, ib_async)        │
 │    ├─ strategy.py  (pure rules, unit-tested) │
 │    ├─ broker.py    (IB bars + orders)        │
 │    └─ notify.py    (Telegram + heartbeat)    │
 │            │ localhost:4002 (paper) / 4001   │
 │  IB Gateway (+ IBC, Docker, headless)        │
 └────────────┬─────────────────────────────────┘
              │ outbound only (no open inbound ports)
              ▼
     Interactive Brokers servers ──► CME Globex (MNQ)
     (bracket SL/TP live here, so they still work if the VPS dies)
```

Design rules:
- **The broker holds the stop and target.** Every entry is sent as a bracket order (parent plus
  OCO stop and target), so the position stays protected if the bot, the VPS or IB Gateway goes down.
- **The rules are pure code.** `strategy.py` takes 1m bars in and returns decisions, with no IB
  calls inside it. You can then replay Databento history through it and compare with `orb run`.
- **The one-trade-per-day lock is saved to disk**, so a restart mid-day can't enter twice.

---

## 1. Interactive Brokers account setup

1. **Open an IBKR Pro account** (not Lite) and fund it. Leave buffer above margin plus about $500
   so market data subscriptions stay allowed. Check IB's current minimum.
2. **Trading permissions** (Settings → Account Settings → Trading Permissions): enable
   **Futures → United States (CME)**.
3. **Create a second username for the bot** (Settings → Users & Access Rights → add user).
   - IB allows one live session per username. If you log into TWS or the mobile app with the bot's
     username, the bot gets disconnected.
   - Your own username stays for manual use. The bot's username is used only by IB Gateway.
   - Give the bot user trading rights. If the option exists, leave out withdrawal and transfer rights.
4. **Market data for the bot username** (Settings → Market Data Subscriptions):
   - Complete the **non-professional** questionnaire.
   - Subscribe to **CME Real-Time (NP, L1)**. CME's non-pro top-of-book fee is about $1.55/month,
     and IB's price may differ slightly.
   - Each username pays its own data subscription.
5. **Paper account:**
   - Request a paper trading account for the bot user.
   - In paper account settings, enable **share real-time market data with paper account**, so
     paper trading runs on live data.
6. **2FA:** install **IB Key** on your phone. Live logins need it. Expect to approve a login at
   least weekly (see step 3.4).

Checklist before moving on: you can log into the paper account in TWS on your PC with the bot
username, and an MNQ chart shows live (not delayed) prices.

---

## 2. VPS hardening (Ubuntu 24.04 LTS assumed)

Sizing: 2 vCPU, 4 GB RAM, 20 GB disk is enough (IB Gateway is Java and uses about 1 GB). A
location near Chicago or New York is nice but not required, because latency doesn't matter for
one trade a day.

```bash
# as root, once
adduser orb && usermod -aG sudo orb
timedatectl set-timezone UTC           # keep the server on UTC; the bot converts to UTC+2 itself
apt update && apt full-upgrade -y
apt install -y ufw fail2ban unattended-upgrades chrony docker.io docker-compose-v2 git
usermod -aG docker orb
```

- **SSH:** key-only login. In `/etc/ssh/sshd_config` set `PasswordAuthentication no` and
  `PermitRootLogin no`, then `systemctl restart ssh`.
- **Firewall:** the bot only makes outbound connections, so allow SSH only:
  `ufw default deny incoming && ufw allow OpenSSH && ufw enable`.
- **Clock:** `chrony` keeps the clock synced. Check with `chronyc tracking`. The OR window is
  minute-exact, so a drifting clock can put bars into the wrong window.
- **Updates:** unattended-upgrades for security patches, with
  `Unattended-Upgrade::Automatic-Reboot "false";`. Reboot by hand on weekends.
- **Secrets:** IB credentials and the Telegram token go in `/etc/orb-bot/env` (`chmod 600`, owned by
  `orb`). Never put them in the repo; `.gitignore` already blocks `.env*`, and keep it that way.

---

## 3. IB Gateway, headless, with IBC

IB Gateway is the lighter, API-only version of TWS. **IBC** fills in the login screen, handles
dialogs and does the daily restart. The simplest way to run both on a headless server is a Docker
image that bundles IB Gateway, IBC and a virtual display. Use the widely used
[`gnzsnz/ib-gateway-docker`](https://github.com/gnzsnz/ib-gateway-docker) image.

1. Create `~/ibgw/docker-compose.yml` from the image README. The key settings are:
   ```yaml
   services:
     ib-gateway:
       image: ghcr.io/gnzsnz/ib-gateway:stable   # pin an exact tag once it works
       restart: always
       env_file: /etc/orb-bot/ibgw.env            # TWS_USERID, TWS_PASSWORD (chmod 600)
       environment:
         TRADING_MODE: paper                      # switch to live in Phase 7
         READ_ONLY_API: "no"
         AUTO_RESTART_TIME: "11:55 PM"            # IBC restart; keep it outside 05:00-20:00 UTC
         TWOFA_TIMEOUT_ACTION: restart
         TIME_ZONE: Etc/UTC
       ports:
         - "127.0.0.1:4002:4004"                  # paper API, bound to localhost only
         - "127.0.0.1:4001:4003"                  # live API, bound to localhost only
   ```
   Check the port mapping and variable names against the README of the tag you pin, because they
   have changed between versions.
2. `docker compose up -d`, then `docker compose logs -f` and watch for a successful login.
3. **Never expose ports 4001/4002 publicly.** They are bound to `127.0.0.1`, and ufw blocks
   inbound traffic anyway.
4. **2FA routine:**
   - Paper logins usually don't need IB Key.
   - Live logins do. IB Gateway logs in, your phone gets a push, and you approve it.
   - With IBC's auto-restart, one approval usually lasts the week, but plan on approving every
     Sunday evening before the week starts.
   - If an approval is missed, the bot has no connection. It must alert you (see step 5.3), not
     trade blind.
5. Optional: if you want to see the Gateway screen, set `VNC_SERVER_PASSWORD` and reach VNC only
   through an SSH tunnel.
6. **Order precautions:** in Gateway's Configuration → API → Precautions, set a per-order size
   limit of **10 contracts**. This is a broker-side backstop to `max_contracts`.

---

## 4. The bot (`automation/`)

### 4.1 Project layout

Give it its own uv project, like `backtest/`:

```
automation/
  start.md            # this file
  pyproject.toml      # uv project: ib_async, pandas, numpy, httpx (Telegram + heartbeat)
  config.toml         # strategy params (same keys as backtest Params) + runtime settings
  bot/
    config.py         # load config.toml -> orb.engine.Params-compatible values
    strategy.py       # pure ORB state machine (1m bars in, actions out)
    contract.py       # front-month MNQ selection + roll rule
    broker.py         # ib_async: connect, live 1m bars, bracket orders, flatten, reconcile
    state.py          # per-day state file (traded flag, order ids) in ~/.local/state/orb-bot/
    notify.py         # Telegram messages + healthchecks.io pings
    main.py           # daily loop / scheduler
  tests/
    test_strategy.py  # replay Databento 1m bars, compare with backtest trades
```

```bash
cd automation && uv init --app && uv add ib_async pandas numpy httpx && uv add --dev pytest
```

### 4.2 Config: same numbers as the backtest

`config.toml` uses the same keys as `Params` in `backtest/orb/engine.py`, so live and backtest
read the same values:

```toml
[strategy]
tz = "Etc/GMT-2"       # fixed UTC+2 (POSIX sign inverted). Do NOT change to Etc/GMT+2.
or_start = "07:00"
or_minutes = 15
entry_end = "11:00"
hold_end = "22:00"
signal_tf = 5
entry = "close"        # start with close; see 4.4 for touch
stop = "opposite"
rr = 2.0
min_range = 10
max_range = 80
account = 50000
risk_pct = 1.0
max_contracts = 10     # set to 1 for the first live weeks (Phase 7)
commission_rt = 1.24
point_value = 2.0
tick = 0.25

[runtime]
mode = "paper"         # paper -> port 4002, live -> port 4001
client_id = 7
max_daily_loss = 600   # $ kill switch, above one full 1R loss
```

### 4.3 The daily timeline (times in UTC+2, which is the server's UTC + 2h)

| Time (UTC+2) | Bot action |
|---|---|
| 06:50 | Connect to Gateway. Check: account reachable, **no open MNQ position or orders** (otherwise alert and stop), front-month contract resolved, today is Mon–Fri, today's state file not already marked traded. Send "ready" message. |
| 06:50 | Start live 1m bars: `reqHistoricalData(..., barSizeSetting="1 min", useRTH=False, keepUpToDate=True)`. |
| 07:00–07:15 | Collect the 15 completed 1m bars. If any are missing, skip the day and alert (same idea as the backtest's completeness check). Compute range high H, low L. |
| 07:15 | Range filter: skip the day unless `min_range ≤ H−L ≤ max_range`. Compute the stop (opposite side, or the midpoint) and `qty = min(max_contracts, floor(account·risk_pct% / (stop_pts·2 + 1.24)))` using the stop distance from the expected entry. **If qty < 1, mark the day traded and stop** (backtest rule). |
| 07:15–11:00 | Build 5m signal bars from the 1m bars (07:15–07:20, 07:20–07:25, …). On the **first 5m close above H or below L**, send the bracket order (4.5) and mark the day traded in the state file *before* sending. |
| After fill | Read the actual fill price, then **modify the take-profit to `fill ± rr × |fill − stop|`** (the backtest sets the target from the real fill). Send a fill message. |
| Until 22:00 | Monitor only. Stop and target are working at IB. Ping the heartbeat every minute. |
| 22:00 | If still in a position: cancel the bracket children and send a market order to flatten. Send an EOD message with the day's P&L. |
| 22:05 | Disconnect bars, write the daily log line (date, side, qty, entry, exit, reason, pnl) in the same columns as `run_trades.csv`. |

Notes:
- **1m bar completeness:** IB keeps updating the latest bar. Treat a bar as closed only once a newer
  bar exists, or the clock is ≥ 5 s past the bar's end. Using a still-forming bar is the most common
  live-vs-backtest bug.
- IB bar timestamps are bar **start** times, as in Databento `ohlcv-1m`.
- Round every price to the 0.25 tick before sending it.

### 4.4 Entry mode
- **`close` (default):** market order right after the 5m close, which is exactly what the backtest
  models (fill at the close plus slippage). Start here.
- **`touch`:** to match the backtest you'd rest two stop orders, at H + 1 tick and L − 1 tick, in one
  OCA group from 07:15, each with its own child stop and target. That's more moving parts, so only
  add it after `close` mode has run cleanly in paper.

### 4.5 Bracket order (ib_async sketch)

```python
from ib_async import IB, MarketOrder, LimitOrder, StopOrder

def bracket(ib: IB, contract, side: str, qty: int, sl: float, tp: float):
    exit_side = "SELL" if side == "BUY" else "BUY"
    parent = MarketOrder(side, qty, orderId=ib.client.getReqId(), transmit=False,
                         outsideRth=True, tif="DAY")
    take = LimitOrder(exit_side, qty, tp, parentId=parent.orderId, transmit=False,
                      outsideRth=True, tif="GTC")
    stop = StopOrder(exit_side, qty, sl, parentId=parent.orderId, transmit=True,   # last one transmits all
                     outsideRth=True, tif="GTC")
    return [ib.placeOrder(contract, o) for o in (parent, take, stop)]
```

- **`outsideRth=True` matters.** By default IB may only trigger stops during regular trading hours.
  The London session (01:00–05:00 ET) is outside them, so without this flag your stop might not fire.
  Confirm in paper by watching a stop trigger during the London session.
- The child orders are automatically one-cancels-other at IB: when one fills, the other is cancelled.
- **Optional server-side EOD backup:** add a third child, a market order with a `TimeCondition` at
  22:00 UTC+2, so IB flattens you even if the bot is dead at 22:00. Test this thoroughly in paper
  (it must cancel when the SL or TP fills, and must never open a new position) before relying on it.

### 4.6 Contract selection and roll
- MNQ expires quarterly (Mar/Jun/Sep/Dec, third Friday).
- Each morning, `reqContractDetails(Future("MNQ", exchange="CME"))` and pick the nearest expiry
  with **more than 8 calendar days left**. That roughly matches when volume moves to the next
  contract, which is what `MNQ.v.0` follows in the backtest.
- The strategy is flat every night, so the roll is just switching contracts on a morning with no
  position. Send a message on roll days.
- `ContFuture` works for data but **not for orders**. Always trade a specific contract month.

### 4.7 Startup and restart reconciliation (`broker.reconcile()`)

Run this on every start, including after a crash or a reboot in the middle of the day:
1. `ib.reqPositions()` and `ib.reqAllOpenOrders()`.
2. **Position with a stop order** → protected. Resume monitoring until 22:00.
3. **Position without a stop** → place a stop at today's stored SL right away (or flatten if
   none is stored) and send an URGENT alert.
4. **No position, state file says traded** → done for the day. Never re-enter.
5. Orders without a position (leftover children) → cancel them.

---

## 5. Running it

### 5.1 systemd service

`/etc/systemd/system/orb-bot.service`:
```ini
[Unit]
Description=London ORB MNQ bot
After=docker.service network-online.target
Wants=network-online.target

[Service]
User=orb
WorkingDirectory=/home/orb/londonorb/automation
EnvironmentFile=/etc/orb-bot/env
ExecStart=/home/orb/.local/bin/uv run python -m bot.main
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```
`systemctl enable --now orb-bot`. View logs with `journalctl -u orb-bot -f`.

### 5.2 Notifications (Telegram bot, free)
Send a message on: ready (06:50), range set or day skipped, entry fill, target change, exit
(SL/TP/EOD) with P&L, contract roll, and **every error or disconnect**.

### 5.3 Dead-man's switch
Ping a free [healthchecks.io](https://healthchecks.io) check every minute between 06:50 and 22:05
(UTC+2) on weekdays. If pings stop (the bot crashed, Gateway hung or the VPS is down),
healthchecks.io alerts your phone. Without this you won't know the bot is dead, because a dead bot
sends no messages.

### 5.4 Kill switches
- `max_daily_loss` in config. If realized plus open loss goes past it, flatten and stop for the day.
- **Manual stop:** `systemctl stop orb-bot`, then flatten in the IB mobile app (using *your*
  username). The bot's resting stop and target stay at IB until you cancel them.
- IB-side backstops: the order size limit (step 3.6) and an account funded only with what this
  strategy needs.

---

## 6. Validation phases (don't skip any)

### Phase A: replay test (local, before any IB connection)
- `tests/test_strategy.py` feeds Databento 1m bars (`backtest/data/`) through `strategy.py` one bar
  at a time, as if they were live.
- Assert the resulting trades (date, side, qty, entry, stop, target, exit reason) match
  `uv run orb run` on the same period.
- **They must match on every day before you continue.** This is where using the same rules for the
  backtest and live trading pays off.

### Phase B: paper trading for 4–8 weeks (`TRADING_MODE=paper`)
- Run the full stack on the VPS against the paper account with live data.
- Every week, download Databento data for those days, run `orb run` on it, and compare with the
  paper log, trade by trade. Write down every difference and its cause: IB's sampled bars vs
  Databento bars, fill slippage, a missed signal, a timing bug.
- Practice the failure cases on purpose:
  - Kill the bot mid-trade and restart it (reconcile should keep the bracket).
  - Restart Gateway during the session.
  - Reboot the VPS.
  - Miss a 2FA prompt.

### Phase C: live with 1 contract for 2–4 weeks
- Set `max_contracts = 1` and `TRADING_MODE=live`, and approve the 2FA prompt.
- Confirm real fills, commissions (should be about $1.24 round trip including exchange fees) and the
  P&L reports.

### Phase D: full size
- Restore `max_contracts = 10` (1% risk sizing).
- Keep the weekly backtest-vs-live comparison going.

---

## 7. Operations checklist

| When | Task |
|---|---|
| Sunday evening | Approve the IB Key login if prompted. Check that the bot's "ready" message arrives Monday at 06:50. |
| Daily | Read the Telegram messages. If a weekday ends without an EOD or skip message, investigate. |
| Weekly | Compare live trades with `orb run` on fresh Databento data. Check that slippage is within what the backtest assumes. |
| Weekend | `apt upgrade`, reboot if needed, check the `docker compose pull` changelog before updating Gateway. |
| Before each quarterly roll | Check the bot picked the new contract (message on roll day). |
| Yearly | Re-confirm IB's non-professional market data status (IB asks every year). |

---

## 8. Monthly cost (rough)

| Item | Cost |
|---|---|
| VPS (2 vCPU / 4 GB) | ~$10–40 (you already have one) |
| IB CME real-time data (NP L1, bot username) | ~$2–5 |
| IB API | free |
| Telegram, healthchecks.io | free |
| Commissions: ~100–120 contracts/month at ~$1.24 RT (from the backtest trade logs) | ~$125–150 |
| Databento Standard | optional (backtests and replay tests only) |

---

## 9. Caveats

- **Small sample:** the recent sample is small (about 49 trades, Aug–Oct 2026). The longer
  `backtest/` history is better, but still judge the strategy by walk-forward and out-of-sample
  results, and expect live results to vary.
- **Bar differences:** IB's 1m bars are built from sampled data. A breakout that clears the range by
  one tick may trigger on one feed and not the other. The paper phase measures how often this happens.
- **Live fills:** live slippage on entries, stops and EOD may differ from the backtest's 1 tick.
- **Unconfirmed details:** IB's account minimums and data prices, the IBC and Docker image settings,
  and `outsideRth` behaviour can change. Check each in paper and against current IB docs before going live.
- **Keep the rules in sync:** if you change a strategy rule, update `strategy.py` along with the
  Pine scripts and `backtest/orb/engine.py` (see `AGENTS.md`), and re-run the Phase A replay test.
