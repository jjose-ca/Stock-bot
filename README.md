# Stock-Bot Trading System

Discord-alert system for manual trading on Wealthsimple and IBKR.
Three independent bots covering TQQQ (swing), SOXL (swing), and SOXL (intraday).
All signals are Discord alerts — no automated order placement.

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Bot Summaries](#bot-summaries)
3. [Signal Conditions](#signal-conditions)
4. [Validated Parameters](#validated-parameters)
5. [VPS Crontab Schedule](#vps-crontab-schedule)
6. [Log Files Reference](#log-files-reference)
7. [Monthly Review Guide](#monthly-review-guide)
8. [VPS Operations](#vps-operations)
9. [Architecture Decisions](#architecture-decisions)
10. [Emergency Procedures](#emergency-procedures)
11. [Key Implementation Details](#key-implementation-details)

---

## System Architecture

```
Your Laptop
  └── Edit code → push to GitHub

GitHub (jjose-ca/Stock-bot)
  └── Source of truth for all code (.py, .sh, .yml)
  └── Archive for daily JSON log data (pushed from VPS at 4:15pm ET)
  └── GitHub Actions: workflow_dispatch ONLY (schedules disabled)

Servarica VPS (38.49.214.59, hostname: stock-bot, Montreal)
  └── Primary execution environment — runs all three bots on cron
  └── Source of truth for all JSON log data
  └── Reconciles gate block data daily at 4:05pm ET
  └── Pushes logs to GitHub daily at 4:15pm ET
  └── Ubuntu 22.04 LTS, Python 3.10.12
  └── Repo at /root/Stock-bot/
  └── Logs at /root/logs/
```

### Key Principles

- **VPS is source of truth for data** — never overwrite VPS JSON files with git pull
- **GitHub is source of truth for code** — never push code from VPS to GitHub
- **All trading is manual** — bots send Discord alerts, you execute on Wealthsimple/IBKR
- **`.gitattributes`** contains `*.json merge=ours` — protects VPS JSON during git pull

### Code Update Workflow

```bash
# 1. Edit code on laptop, push to GitHub
# 2. On VPS (via Termius):
cd /root/Stock-bot && git pull origin master
# JSON files are never overwritten (protected by .gitattributes)
```

---

## Bot Summaries

| Bot | File | Ticker | Account | Timeframe | Runs |
|-----|------|--------|---------|-----------|------|
| TQQQ Swing | `tqqq_bot.py` | TQQQ | IBKR (manual) | Daily bars | Every 15 min, 9:30am-4:00pm ET |
| SOXL Swing | `soxl_bot.py` | SOXL | Wife's TFSA (Wealthsimple) | Daily bars | 3:20pm, 3:35pm, 3:45pm ET |
| SOXL Intraday | `soxl_intraday_bot.py` | SOXL | Non-reg (Wealthsimple) | 15-min bars | Every 15 min, 10:00am-3:20pm ET |

### tqqq_bot.py — TQQQ Mean-Reversion Swing

Pure mean-reversion strategy. Buys TQQQ when deeply oversold, holds 5-10 days expecting a bounce back toward the mean. Uses a scoring system (up to 6 points across trend + momentum dimensions) with per-category floor requirements to prevent buying falling knives.

**Why it was silent for 2+ months:** TQQQ RSI was 69-79 (bull market). Path A requires RSI < 35, Path D requires RSI < 50. Both correctly blocked.

### soxl_bot.py — SOXL RSI Ladder Swing

RSI Ladder strategy — deploys capital in three tranches as SOXL RSI deepens into extreme oversold territory. Each tranche has progressively larger position size and wider target multiplier. Fires only once the prior tranche is open. Validated via 5-year walk-forward backtest.

**Account:** Wife's TFSA — gains are tax-free, making this ideal for high-upside oversold recoveries.

### soxl_intraday_bot.py — SOXL Intraday Momentum (v1.3)

Three intraday momentum signals on 15-min bars. Catches opening range breakouts, VWAP reclaims, and previous-day-high breakouts. All trades exit same day (time stop at 3:35pm ET). Validated via 60-day backtest.

---

## Signal Conditions

### tqqq_bot.py Signal Paths

#### Path A — Deep Oversold Bypass (RSI < 35)
Bypasses the scoring system entirely. Fires immediately when RSI drops below 35.
- **Tier 1, 10% position size**
- Stop: 2.5x ATR below support EMA
- Target: 3.5x ATR above entry

#### Path D — Pivot Low Reversal (RSI 35-50)
```
price < 21 EMA AND price < 50 EMA  (dead zone)
35 <= RSI < 50
bar_low > prev_bar_low              (higher low)
bar_close > bar_open                (green close)
RSI turning up vs prior bar
```

#### Path E — 21 EMA Bounce (Tier 2)
```
bar_low <= 21 EMA * 1.015          (wick to EMA)
bar_close > 21 EMA                  (green close above EMA)
35 <= RSI <= 50
price > 50 EMA                      (uptrend intact)
```

#### Path F — MACD Cross (Tier 2)
```
MACD histogram crosses from negative to positive
Both MACD line and signal line still below zero
RSI < 60
price > 21 EMA
green bar
```

#### Path B — DISABLED
Backtested negative expectancy (-6.16% over 4.8 years). Permanently disabled.

#### Scoring System (Paths B/D/E/F)
```
trend_score:    0-3 points (above 21/50/200 EMA)
momentum_score: 0-3 points (RSI, MACD, volume conditions)
penalty:        +1 if VTI below 200 SMA (bearish regime)
                +1 if Friday afternoon
                +2 if earnings within 7 days
threshold:      6 points (score must meet or exceed threshold)
```

---

### soxl_bot.py Signal Paths

#### Path A — RSI Ladder (RSI < 40)
Three tranches deployed as RSI deepens:

| Tranche | RSI Threshold | Deploy | Target | Stop |
|---------|--------------|--------|--------|------|
| T1 | RSI < 40 | 3.3% ($33) | 3.5x ATR | 2.5x ATR |
| T2 | RSI < 32 | 3.3% ($33) | 4.0x ATR | 2.5x ATR |
| T3 | RSI < 25 | 3.4% ($34) | 5.0x ATR | 2.5x ATR |

T2 only fires if T1 is already OPEN. T3 only fires if T2 is already OPEN.

#### Path D — Pivot Low Reversal (RSI 35-45)
```
price < 21 EMA AND price < 50 EMA
35 <= RSI < 45                       (tighter than TQQQ's 50 ceiling)
higher low + green close
RSI turning up
```

#### Path E — 21 EMA Bounce (RSI 35-50, Tier 2)
```
wick touches 21 EMA within 1.5%
green close above 21 EMA
price above 50 EMA
35 <= RSI <= 50
```

#### Path F — MACD Cross (Tier 2)
```
MACD histogram crosses zero
both MACD lines still below zero
RSI < 60, price > 21 EMA, green bar
```

#### Risk Validation
```
BASE_MAX_STOP_PCT    = 10%   (floor)
ABSOLUTE_MAX_STOP_PCT = 20%  (ceiling)
dynamic_max_stop     = max(10%, min(ATR% * 3.5, 20%))
MIN_RR_RATIO         = 1.1
```

---

### soxl_intraday_bot.py Signal Paths (v1.3)

All signals require:
- Daily 50 EMA uptrend (DAILY_TREND_FILTER = True)
- Volume >= 0.75x 10-bar rolling average (VOLUME_MULT = 0.75)
- Signals evaluated on the most recently closed 15-min bar

#### Signal Priority Order: PDH → ORB → VWAP

#### Signal 1 — Previous Day High Breakout (PDH)
```
bar_close > prev_day_high
green bar (close > open)
volume >= 0.75x average
bar_close > VWAP
45 <= RSI <= 65
body >= 0.3% of open price
Only fires once per day, after 10:00am ET
```

#### Signal 2 — Opening Range Breakout (ORB)
```
bar_close > OR_high (9:30-10:00am range high)
green bar
volume >= 0.75x average
bar_close > VWAP
45 <= RSI <= 65
body >= 0.3% of open price
Only fires once per day, after 10:00am ET
⚠️  TIME GATE: only fires before 12:00pm ET (noon cutoff)
    All afternoon ORB signals backtested as losing trades
```

#### Signal 3 — VWAP Reclaim
```
prior bar closed BELOW VWAP (was_below = True)
current bar closes ABOVE VWAP (now_above = True)
green bar
volume >= 0.75x average
35 <= RSI <= 60
9 EMA turning up vs prior bar
prior bar RSI < 50 (genuine pullback, not drift)
MACD histogram turning up
VWAP dip >= 0.5% (meaningful dip, not noise)
Can fire multiple times per day
```

#### Exit Strategy
```
Target:    +4% from entry bar close
Stop:      -2% from entry bar close
Time stop: 3:35pm ET (exit before market close)
R/R ratio: 2:1
```

#### Slippage Gate
If live price has moved more than 0.5% from signal bar close, alert is suppressed. R/R is too degraded to enter.

---

## Validated Parameters

### SOXL Intraday — 60-Day Backtest Results

**Backtest command:**
```bash
python soxl_intraday_backtest.py --no-trend-filter --orb-hours 2 --vol-mult 0.75
```

**Results:**
```
Period:        60 trading days
Trades:        7
Win rate:      50% (3W / 3L / 1 TIME_STOP)
Avg win:       +4.00%
Avg loss:      -2.00%
Expectancy:    +1.00% per trade
Total P&L:     +6.98%
```

**Key findings:**
- `VOLUME_MULT = 1.5` (original): 0-2 trades per 60 days, 0% win rate
- `VOLUME_MULT = 0.75` (current): 7 trades, 50% win rate, +1.00% expectancy
- ORB after noon: all losing trades (structural flaw — no opening momentum)
- VWAP time cutoff: NOT recommended (removing late VWAP trades hurts performance)
- Optimal: `0.75x volume + noon ORB gate` confirmed across 3 test window sizes

### SOXL Swing — 5-Year Walk-Forward Backtest
- Path A (RSI Ladder): positive expectancy — active
- Path B (scoring): -6.16% expectancy — disabled
- Path D/E/F: positive expectancy — active
- Parameters: RSI_PATH_A=40, SWING_ATR_STOP_MULT=2.5, SWING_ATR_TARGET_MULT=3.5

### TQQQ Swing — Validated Thresholds
- score≥7 + RSI≥50: -0.041% expectancy → blocked (consolidation_not_pullback)
- score≥8 + RSI>45: -1.80% expectancy → blocked (no_genuine_pullback)
- Path B: negative expectancy → disabled
- Scoring threshold: 6 points

---

## VPS Crontab Schedule

**Server timezone: UTC (always UTC, does not change with DST)**

```
DISCORD_URL="https://discord.com/api/webhooks/..."

# ── SOXL Intraday — every 15 min, 10:00am-3:20pm ET ──────────────────────────
# EDT (Mar-Nov): 14:00-18:45 UTC + 19:00, 19:20
*/15 14-18 * * 1-5  soxl_intraday_bot.py  → soxl_intraday.log
0,20 19 * * 1-5     soxl_intraday_bot.py  → soxl_intraday.log

# ── SOXL Swing — 3:20pm, 3:35pm, 3:45pm ET (19:20, 19:35, 19:45 UTC) ────────
20,35,45 19 * * 1-5  soxl_bot.py          → soxl_swing.log

# ── TQQQ Swing — every 15 min, 9:30am-4:00pm ET ──────────────────────────────
# 9:30am + 9:45am: 13:30, 13:45 UTC
# 10:00am-3:45pm:  */15 14-19 UTC
# 4:00pm:          20:00 UTC
30,45 13 * * 1-5    tqqq_bot.py           → tqqq.log
*/15 14-19 * * 1-5  tqqq_bot.py           → tqqq.log
0 20 * * 1-5        tqqq_bot.py           → tqqq.log

# ── After-market reconciliation and log push ──────────────────────────────────
# Reconcile SOXL intraday gate blocks with MFE data (4:05pm ET = 20:05 UTC)
5 20 * * 1-5   soxl_intraday_bot.py --reconcile  → soxl_intraday.log

# Reconcile SOXL swing gate blocks with 5d/10d prices (4:05pm ET = 20:05 UTC)
5 20 * * 1-5   soxl_bot.py --reconcile           → soxl_swing.log

# Push all JSON logs to GitHub (4:15pm ET = 20:15 UTC)
15 20 * * 1-5  push_logs.sh
```

### ⚠️ DST Warning — November Adjustment Required

When EST starts (first Sunday of November), ET becomes UTC-5 instead of UTC-4.
**All cron times must shift +1 UTC hour:**

```
EDT → EST changes needed:
  14→15, 18→19, 19→20, 13→14, 20→21
Example: */15 14-18 → */15 15-19
```

Set a calendar reminder for the first Sunday of November each year.

---

## Log Files Reference

### Console Logs (VPS only, not pushed to GitHub)

| File | Bot | Contents |
|------|-----|----------|
| `/root/logs/soxl_intraday.log` | soxl_intraday_bot.py | Every 15-min scan output + reconciliation |
| `/root/logs/soxl_swing.log` | soxl_bot.py | Daily scans at 3:20-3:45pm + swing reconciliation |
| `/root/logs/tqqq.log` | tqqq_bot.py | Every 15-min scan output |
| `/root/logs/git_sync.log` | push_logs.sh | Daily push results and any git errors |

### JSON Files (pushed to GitHub daily at 4:15pm ET)

| File | Bot | Contents |
|------|-----|----------|
| `soxl_intraday_trade_log.json` | soxl_intraday_bot.py | Confirmed intraday trades that passed slippage gate |
| `soxl_gate_blocks.json` | soxl_intraday_bot.py | Every near-miss rejection with MFE simulation data |
| `soxl_trade_log.json` | soxl_bot.py | Confirmed SOXL swing trade entries |
| `soxl_swing_gate_blocks.json` | soxl_bot.py | SOXL swing rejections with 5d/10d price outcomes |
| `trade_log.json` | tqqq_bot.py | Confirmed TQQQ swing trade entries |
| `tqqq_gate_blocks.json` | tqqq_bot.py | TQQQ swing rejections with RSI gap tracking |

### Gate Block Fields — SOXL Intraday (`soxl_gate_blocks.json`)

```json
{
  "id":                  "REJ_20260701_110100",
  "date":                "2026-07-01",
  "run_time":            "11:01:00 ET",
  "signal_type":         "ORB",
  "bar_time":            "11:00 AM ET",
  "bar_close":           219.78,
  "failed_conditions":   ["vol 0.48x < 0.75x required"],
  "indicators":          {"rsi": 50.9, "vol_ratio": 0.48, "vwap": 210.19},
  "take_profit_est":     228.57,
  "stop_loss_est":       215.38,

  "simulated_outcome":   "TIME_STOP",   ← filled by --reconcile at 4:05pm ET
  "simulated_exit_price": 229.57,
  "simulated_exit_time": "03:35 PM ET",
  "simulated_pnl_pct":   +4.5,
  "mfe_pct":             6.2,           ← max % gain before stop hit
  "mae_pct":             -0.3,          ← max % loss before target hit
  "mfe_vs_target_2pct":  true,          ← would +2% target have been hit?
  "mfe_vs_target_3pct":  true,          ← would +3% target have been hit?
  "mfe_vs_target_4pct":  true,          ← would +4% (current) target have been hit?
  "would_have_won":      false,         ← true only if simulated_outcome == WON
  "price_30min_later":   222.50,        ← reference only
  "price_eod":           236.00,
  "notes":               null           ← manual monthly review notes
}
```

### Gate Block Fields — TQQQ Swing (`tqqq_gate_blocks.json`)

```json
{
  "id":                  "TQQQ_REJ_20260701_153003",
  "date":                "2026-07-01",
  "ticker":              "TQQQ",
  "price":               78.36,
  "rsi":                 51.8,
  "score":               6,
  "threshold":           6,
  "failed_reason":       "risk_stop_too_wide",
  "rsi_gap_to_path_a":   16.8,   ← RSI needs to fall 16.8 pts for Path A
  "rsi_gap_to_path_d":   1.8,    ← RSI needs to fall 1.8 pts for Path D
  "extra":               {"actual_stop_pct": 17.39, "dynamic_max_stop_pct": 15.0},
  "price_5d_later":      null,   ← auto-filled after 7 calendar days
  "price_10d_later":     null    ← auto-filled after 14 calendar days
}
```

### Gate Block Fields — SOXL Swing (`soxl_swing_gate_blocks.json`)

```json
{
  "id":                  "SOXL_REJ_20260702_154503",
  "date":                "2026-07-02",
  "ticker":              "SOXL",
  "price":               218.50,
  "rsi":                 38.2,
  "path_attempted":      "D",
  "failed_reason":       "path_d_conditions_failed",
  "rsi_gap_to_path_a":   -1.8,   ← negative = RSI already below Path A threshold
  "rsi_gap_to_path_d":   -6.8,   ← negative = RSI already below Path D threshold
  "extra":               {"failed": ["price above 21 EMA"]},
  "price_5d_later":      null,   ← auto-filled after 7 calendar days
  "price_10d_later":     null,   ← auto-filled after 14 calendar days
  "gain_5d_pct":         null,   ← % change from entry price to 5d price
  "gain_10d_pct":        null    ← % change from entry price to 10d price
}
```

---

## Monthly Review Guide

Run the first monthly review after ~4 weeks of live data.

### SOXL Intraday Review (`soxl_gate_blocks.json`)

**Question 1: Is 0.75x volume threshold right?**
```
Look at mfe_pct for all entries where failed_conditions includes "volume"
mfe_pct >= 4.0%:  would have won at current target → volume was too strict
mfe_pct < 1.0%:   barely moved → volume correctly rejected
mfe_pct 2-4%:     would win at lower target → consider reducing to +3%
```

**Question 2: Should the target be lowered from 4% to 3%?**
```
Count entries where mfe_vs_target_3pct=true but mfe_vs_target_4pct=false
If many entries reach +3% but not +4% → lower the target
```

**Question 3: Are morning signals better than afternoon?**
```
Filter by bar_time: compare 10am-12pm vs 12pm-3pm simulated_outcome
Should confirm that ORB noon cutoff was the right decision
```

### TQQQ Swing Review (`tqqq_gate_blocks.json`)

**Question: Is the score threshold right?**
```
Look at rsi_gap_to_path_a trend over time
Shrinking gap = approaching correction, bot will fire soon
Large consistent gap = sustained bull market, silence is correct
```

**Question: Is the stop too wide?**
```
Look for risk_stop_too_wide entries
Compare actual_stop_pct vs dynamic_max_stop_pct
If frequent, ATR-based stop calculation may need adjustment
```

### SOXL Swing Review (`soxl_swing_gate_blocks.json`)

**Question: Were path_d_conditions_failed entries profitable?**
```
Look at gain_10d_pct for entries where path_attempted=D
Positive gain_10d: condition that blocked it was wrong → consider relaxing
Negative gain_10d: block was correct → keep conditions as-is
```

---

## VPS Operations

### SSH Access
```bash
ssh root@38.49.214.59
```

### Check Latest Bot Output
```bash
# SOXL intraday (last 50 lines)
tail -50 /root/logs/soxl_intraday.log

# TQQQ (last 50 lines)
tail -50 /root/logs/tqqq.log

# SOXL swing (last 50 lines)
tail -50 /root/logs/soxl_swing.log

# Git push log
cat /root/logs/git_sync.log
```

### Watch Live as Bot Runs
```bash
tail -f /root/logs/soxl_intraday.log   # Ctrl+C to stop
```

### Count Total Runs
```bash
grep -c "Last closed bar" /root/logs/soxl_intraday.log
```

### Find All Signals That Fired
```bash
grep "SIGNAL FIRED" /root/logs/soxl_intraday.log
```

### Update Code on VPS
```bash
# Always push logs first (safety)
/bin/bash /root/Stock-bot/push_logs.sh

# Then pull new code
cd /root/Stock-bot && git pull origin master
```

### Manual Reconciliation
```bash
# SOXL intraday gate blocks
cd /root/Stock-bot && python3 soxl_intraday_bot.py --reconcile

# SOXL swing gate blocks
cd /root/Stock-bot && python3 soxl_bot.py --reconcile
```

### Test a Bot Manually
```bash
# Force run (bypass market hours), dry run (no log writes)
cd /root/Stock-bot && python3 soxl_intraday_bot.py --force --dry-run
cd /root/Stock-bot && python3 soxl_bot.py --force --dry-run
cd /root/Stock-bot && python3 tqqq_bot.py --force --dry-run
```

### View Gate Block Data
```bash
cat /root/Stock-bot/soxl_gate_blocks.json
cat /root/Stock-bot/soxl_swing_gate_blocks.json
cat /root/Stock-bot/tqqq_gate_blocks.json
```

---

## Architecture Decisions

### Why VPS over GitHub Actions
GitHub Actions drops ~50% of scheduled runs under load (confirmed from 2+ months of TQQQ bot run history). For a 15-min intraday bot, delays of 7-22 minutes cause missed signals. The VPS cron fires within 2 seconds of the scheduled time every time.

### Why 0.75x Volume (SOXL Intraday)
Backtested across 60 days: original 1.5x threshold produced 0-2 trades with 0% win rate. 0.75x threshold produced 7 trades with 50% win rate and +1.00% expectancy per trade. By the time a 15-min bar shows 1.5x average volume, the momentum move is already exhausted and entry is too late.

### Why Noon ORB Cutoff
Every ORB signal that fired after 12:00pm ET was a losing trade in the 60-day backtest. These were afternoon drift moves above the OR High — not genuine opening momentum. Restricting ORB to 10:00am-12:00pm confirmed as optimal across three separate test window sizes (1.5h, 2.0h, 2.5h).

### Why Separate Gate Log Files
```
soxl_gate_blocks.json:        SOXL intraday — MFE reconciled same-day
soxl_swing_gate_blocks.json:  SOXL swing — 5d/10d price reconciled over weeks
tqqq_gate_blocks.json:        TQQQ swing — RSI gap tracking
```
Different timeframes, different signal types, different reconciliation logic. Mixing them would break the MFE reconciliation and make monthly review impossible to parse.

### Why SOXL Swing Uses Wife's TFSA
SOXL mean-reversion at extreme oversold (RSI < 40) historically produces 20-40% snap-back moves. TFSA tax-free treatment on these large percentage gains maximizes after-tax return. Maximum exposure is $100 (10% of $1,000 baseline) across all three ladder tranches.

### Why TQQQ Uses a Scoring System vs SOXL's Direct Conditions
TQQQ is a broader index (Nasdaq-100) vs SOXL's concentrated semiconductor exposure. TQQQ requires simultaneous alignment of trend health AND momentum exhaustion — a single RSI threshold isn't sufficient. The scoring system also enables precise backtest validation: specific score+RSI combinations were found to have negative expectancy and blocked (score≥7+RSI≥50 = -0.041%, score≥8+RSI>45 = -1.80%).

### Why No Automated Order Placement
Signal quality validation phase. Running Discord-alert-only for 3-6 months generates empirical signal data (trade log, gate blocks, MFE). After validation, IBKR API automation can be added with confidence in the underlying signals. Automated orders before validation would compound errors at machine speed.

---

## Emergency Procedures

### Pause a Single Bot
```bash
crontab -e
# Add # before the bot's cron line(s) to comment them out
# Ctrl+X → Y → Enter to save
```

### Pause All Bots
```bash
crontab -r   # removes entire crontab — use with caution
# To restore: re-run the crontab setup heredoc from this README
```

### Bot Producing Wrong Signals — Roll Back
```bash
cd /root/Stock-bot
git log --oneline -10          # find the last good commit hash
git checkout COMMIT_HASH -- soxl_intraday_bot.py  # restore specific file
```

### VPS Unreachable
1. Log in to Servarica control panel at servarica.com
2. Navigate to your VPS → Service Management
3. Click Reboot (hard reboot, not graceful — only if SSH is completely down)
4. Wait 60 seconds, try SSH again

### git push rejected on VPS
```bash
# Push logs first to preserve data
/bin/bash /root/Stock-bot/push_logs.sh

# Then pull and retry
git pull --no-rebase origin master
git push origin master
```

### DST Update (November — EST starts)
All cron times shift +1 UTC hour. Run the full crontab setup heredoc with updated times. Times change from EDT (UTC-4) to EST (UTC-5):

```
EDT → EST mapping:
  13:xx → 14:xx  (9:30am ET start)
  14:xx → 15:xx  (10:00am ET)
  18:xx → 19:xx  (2:xx pm ET)
  19:xx → 20:xx  (3:xx pm ET)
  20:xx → 21:xx  (4:xx pm ET)
```

---

## Key Implementation Details

These are the non-obvious implementation specifics that are not derivable from
the signal conditions alone. Essential for rebuilding the bots correctly.

---

### soxl_intraday_bot.py

#### VWAP Calculation — Daily Reset
VWAP is not available in the `ta` package. Calculated manually:
```python
df["_TP"]       = (df["High"] + df["Low"] + df["Close"]) / 3
df["_TPVOL"]    = df["_TP"] * df["Volume"]
df["_CUMTPVOL"] = df.groupby(df.index.date)["_TPVOL"].cumsum()
df["_CUMVOL"]   = df.groupby(df.index.date)["Volume"].cumsum()
df["VWAP"]      = df["_CUMTPVOL"] / df["_CUMVOL"]
```
The `groupby(df.index.date)` is critical — it resets the cumulative sum each
trading day so VWAP correctly resets at 9:30am every morning.

#### et_now Consistency Fix
`et_now = datetime.now(et_tz)` is called separately in multiple functions.
If called at 2:14:58pm and again at 2:15:02pm, the two calls can disagree on
which 15-min bar is "last closed" — causing the header to say "2:15 PM" while
bar-selection uses the 2:00pm bar.

**Fix:** `check_market()` computes `et_now` once and passes it into
`fetch_intraday(et_now=et_now)`. The function signature is:
```python
def fetch_intraday(ticker=TICKER, days=7, et_now=None):
    if et_now is None:
        et_now = datetime.now(et_tz)  # fallback for standalone use
```

#### Bar Snap-to-Last-Closed Logic
```python
closed_minute = (et_now.minute // 15) * 15
last_closed   = et_now.replace(minute=closed_minute, second=0, microsecond=0)
df = df[df.index <= last_closed]
```
At 10:22am: `closed_minute = (22 // 15) * 15 = 15` → last_closed = 10:15am.
This correctly excludes the currently-forming 10:15-10:30am bar.

#### yfinance MultiIndex Flattening
yfinance sometimes returns MultiIndex columns when downloading a single ticker.
Every download must be flattened:
```python
if isinstance(df.columns, pd.MultiIndex):
    df.columns = df.columns.get_level_values(0)
```
Without this, column access like `df["Close"]` will raise a KeyError.

#### Live Price Fallback
Primary method: `yf.Ticker(ticker).fast_info.get("last_price")` — fast, single
API call. Fallback: download 1-minute bars and take the last close. The fallback
exists because `fast_info` sometimes fails to populate in the first few minutes
of the trading session (9:31-9:45am).

#### Gate Block Near-Miss Filter
Not every failed signal gets logged — only genuine near-misses where the primary
breakout condition was met. This prevents noise from filling the log with
irrelevant rejections:
```
ORB:  only logged if broke_or_high AND green_bar (price actually crossed OR High)
VWAP: only logged if was_below AND now_above AND green_bar (actual reclaim happened)
PDH:  only logged if broke_pdh AND green_bar (price actually crossed PDH)
```
A bar where price never crossed the level at all is not a near-miss — it's a
correctly ignored setup. These are not logged.

#### Slippage Gate Rejection Logging
Unlike signal-level rejections (logged in `log_signal_rejection()`), slippage
gate blocks are logged with signal type suffixed by `_GATE`:
```
signal_type: "ORB_GATE"   ← slippage blocked after ORB signal fired
signal_type: "VWAP_GATE"  ← slippage blocked after VWAP signal fired
```
These are excluded from MFE reconciliation (the reconciler skips `_GATE` entries).

#### MFE Reconciliation — Unreconciled Marker
The reconciler uses `simulated_outcome is None` as the marker for unreconciled
entries — NOT `price_30min_later is None`. This is intentional:
- `price_30min_later` might be null for legitimate reasons (late-day signal)
- `simulated_outcome` is always set during reconciliation, making it a reliable
  marker for "has this entry been processed?"

Old entries (before MFE was implemented) that have `price_30min_later` filled
but `simulated_outcome` null will be picked up and reconciled.

#### MFE Conservative Same-Bar Logic
When both the target High and stop Low are hit in the same 15-min bar, the stop
wins (conservative/realistic assumption):
```python
both_hit = bar_high >= take_profit and bar_low <= stop_loss
if both_hit or bar_low <= stop_loss:
    outcome = "LOST"   # stop checked first
elif bar_high >= take_profit:
    outcome = "WON"
```
We cannot know which happened first within a 15-min bar, so assuming the worst
prevents overstating the simulated win rate.

#### MFE Stops Updating After Stop Hit
Once `bar_low <= stop_loss` is hit, the loop breaks immediately. This means
`mfe_pct` reflects the highest gain reached BEFORE the stop was hit — not
any phantom recovery that happened after. This is critical for accurate target
sensitivity analysis (`mfe_vs_target_3pct` etc.).

#### fetch_daily_data Fail-Open Behaviour
If the daily data download fails, the function returns `(True, None, None, None, None, None)`.
The first value `True` means "assume bullish" — deliberately failing open so
a data problem never silently blocks ALL signals for the entire day.
```python
fail_open = (True, None, None, None, None, None)
```

---

### soxl_bot.py

#### StopIteration Fix in get_active_ladder_tranche
The original `next()` call had no default — it would crash if tranche numbering
had gaps (e.g. if tranche 2 was removed from config):
```python
# WRONG — crashes with StopIteration if tranche N-1 doesn't exist:
prev_label = next(t["label"] for t in LADDER_TRANCHES
                  if t["tranche"] == tranche["tranche"] - 1)

# CORRECT — safe fallback:
prev_label = next(
    (t["label"] for t in LADDER_TRANCHES
     if t["tranche"] == tranche["tranche"] - 1),
    None  # default if not found
)
if prev_label is None or prev_label not in open_labels:
    continue
```

#### Bulk Download Architecture
Both SOXL and VTI (regime check) are downloaded in a single `yf.download()` call
to minimize API calls. The `extract_ticker_daily()` function handles slicing the
correct ticker from the multi-ticker bulk response, including both MultiIndex
and flat column layouts.

#### Outcome Walking — Bar-by-Bar (Not EOD)
`check_open_trades()` walks each bar from the alert date forward, checking
`bar_high >= target` and `bar_low <= stop` on each bar. It does NOT simply
compare today's close to target/stop. This means a trade can resolve as WON
even if it closed below target by EOD — if the target was touched intraday.
Same for LOST — checks `bar_low <= stop`, not just close price.

#### Regime Check — VTI 200 SMA
The bearish regime penalty (+1 to score threshold) is triggered when VTI's
closing price is below its 200-day SMA. This adds conservatism during broad
market downtrends without blocking signals entirely (threshold rises from 6 to 7,
meaning one more confirmation point is needed).

#### Swing Gate Reconciliation — Trading Day Counting
`nth_trading_day_after()` counts actual trading days using the downloaded
daily bar index — not calendar days. This correctly handles weekends and
holidays:
```python
trading_days = [d for d in df.index if d.date() > start_date]
return trading_days[n - 1]  # nth element = nth trading day
```
7 calendar days is used as the minimum wait before filling `price_5d_later`
to ensure 5 trading days have actually passed (accounting for weekends).

---

### tqqq_bot.py

#### Scoring System — Category Floors
The scoring system has two layers of protection beyond the total score threshold:
1. `trend_floor`: minimum trend_score required (prevents buying with zero trend points)
2. `momentum_floor`: minimum momentum_score required (prevents buying on trend alone)

These floor checks run BEFORE the total score check. A signal with score=7 but
trend_score=0 is rejected by the floor check before even reaching the threshold comparison.

#### Gap Filter — Dynamic Max Gap
ORB signals have a dynamic gap tolerance based on ATR:
```python
atr_pct_gap     = (atr / price) * 100
dynamic_max_gap = max(2.0, min(atr_pct_gap * 1.5, 7.0))
```
On high-ATR days the gap allowance widens (up to 7%). On low-ATR days it
narrows (minimum 2%). This prevents rejecting signals on legitimate high-volatility
gap days while still blocking excessive overnight gaps.

#### Cooldown Files — /tmp Storage
Session-state tracking files (tier cooldowns, sell alert session tracking) are
stored in `/tmp/` — intentionally ephemeral. They reset on VPS reboot, which is
correct behaviour: a reboot should not inherit stale session state from the
previous day's alerts.

#### validate_risk — ticker Parameter
`validate_risk(signal, ticker)` takes a `ticker` parameter so the gate block
logging knows which file to write to. When called from `check_market()`, the
ticker is passed from the signal dict. When the stop is too wide or R/R is too
low, the rejection is logged to `tqqq_gate_blocks.json` only when `ticker == "TQQQ"`.

#### RSI Gap Fields — Negative Values are Good
In `tqqq_gate_blocks.json`:
```
rsi_gap_to_path_a = current_rsi - 35
rsi_gap_to_path_d = current_rsi - 50
```
Positive values = RSI still above threshold (bot correctly silent).
Negative values = RSI already below threshold (Path A/D would activate
  if other conditions are met — score, floors, risk validation).
A shrinking positive gap across consecutive days signals an approaching correction.

---

### push_logs.sh

#### Why --no-rebase Instead of --rebase
`git pull --rebase` fails when there are unstaged local changes (common when
the VPS has written new JSON entries since the last commit). `--no-rebase` uses
merge instead, which handles uncommitted local changes gracefully.

#### Why [skip ci] in Commit Message
GitHub Actions workflows respect `[skip ci]` in commit messages — workflows
triggered by a push event will not run. This prevents GitHub Actions from
running the bots when push_logs.sh commits trade data, which could cause
duplicate log entries.

#### git add with 2>/dev/null
```bash
git add soxl_swing_gate_blocks.json ... 2>/dev/null
```
The `2>/dev/null` suppresses the "pathspec did not match any files" error when
a JSON file doesn't exist yet (e.g. `soxl_swing_gate_blocks.json` before the
first swing rejection is logged). The script continues normally — the file will
simply not be included in that day's commit.

---

### requirements.txt

```
yfinance>=1.4.1    ← market data (15-min bars, daily bars, live price)
pandas>=2.2.0      ← DataFrame operations, groupby for VWAP reset
numpy>=1.26.0      ← required by pandas and ta
ta>=0.11.0         ← technical indicators (RSI, EMA, ATR, MACD, BB)
                      replaces pandas_ta which caused segfault (exit code 139)
requests>=2.31.0   ← Discord webhook HTTP POST
pytz>=2024.1       ← timezone handling (US/Eastern for ET conversion)
mplfinance>=0.12.10b0  ← charting (optional, used in soxl_bot.py)
alpaca-py>=0.29.0  ← legacy (Alpaca removed, kept for potential future use)
```

**Why `ta` instead of `pandas_ta`:**
`pandas_ta` caused a segmentation fault (exit code 139) on Ubuntu 22.04 with
Python 3.10 due to a numpy/pandas version conflict. The `ta` package uses a
different API (class-based rather than DataFrame extension methods) but is
otherwise equivalent. Migration was completed in full across all three bots.

The API difference:
```python
# pandas_ta (old):
df["RSI"] = ta.rsi(df["Close"], length=14)

# ta (new):
df["RSI"] = ta_lib.momentum.RSIIndicator(df["Close"], window=14).rsi()
```

---

## Files Reference

```
Stock-bot/
├── .gitattributes              ← *.json merge=ours (protects VPS JSON on pull)
├── README.md                   ← this file
├── requirements.txt            ← ta>=0.11.0, yfinance, pandas, numpy, requests, pytz
├── push_logs.sh                ← daily JSON push to GitHub (4:15pm ET cron)
├── soxl_intraday_bot.py        ← SOXL intraday momentum bot v1.3
├── soxl_intraday_backtest.py   ← backtest script for SOXL intraday signals
├── soxl_bot.py                 ← SOXL RSI Ladder swing bot v1.1
├── tqqq_bot.py                 ← TQQQ mean-reversion swing bot
├── .github/workflows/
│   ├── soxl_intraday_timer.yml ← workflow_dispatch ONLY (schedule disabled)
│   ├── soxl_swing_timer.yml    ← workflow_dispatch ONLY (schedule disabled)
│   └── tqqq_timer.yml          ← workflow_dispatch ONLY (schedule disabled)
├── soxl_intraday_trade_log.json
├── soxl_gate_blocks.json
├── soxl_swing_gate_blocks.json
├── soxl_trade_log.json
├── trade_log.json
├── tqqq_gate_blocks.json
├── soxl_earnings_cache.json
└── earnings_cache.json
```

---

*Last updated: July 2026*
*VPS: Servarica V3 KVM Slim Slice 2, 38.49.214.59, Montreal*
*Python: 3.10.12 | Ubuntu: 22.04 LTS*
