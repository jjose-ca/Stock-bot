# Stock-Bot Trading System

Discord-alert system for manual trading on Wealthsimple and IBKR.
Four independent bots covering TQQQ (swing), TQQQ (intraday), SOXL (swing),
and SOXL (intraday). All signals are Discord alerts — no automated order placement.

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
  └── Primary execution environment — runs all four bots on cron
  └── Source of truth for all JSON log data
  └── Reconciles SOXL gate block data daily at 4:05pm ET
  └── Reconciles TQQQ intraday trade log daily at 4:30pm ET
  │     (later than SOXL — gives yfinance extra time to finalize the
  │     final 1-min bars of the session before the replay-forward scan)
  └── Pushes logs to GitHub daily at 4:40pm ET
  │     (moved from 4:15pm to run after TQQQ intraday reconcile completes)
  └── Ubuntu 22.04 LTS, Python 3.10.12
  └── Repo at /root/Stock-bot/
  └── Logs at /root/logs/
```

### Key Principles

- **VPS is source of truth for data** — never overwrite VPS JSON files with git pull
- **GitHub is source of truth for code** — never push code from VPS to GitHub
- **All trading is manual** — bots send Discord alerts, you execute on Wealthsimple/IBKR
- **`.gitattributes`** contains `*.json merge=ours` and `*.jsonl merge=ours` — protects VPS JSON/JSONL data during git pull

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
| TQQQ Intraday | `tqqq_intraday_bot.py` | TQQQ | IBKR (manual) | 15-min bars | Every 15 min, 10:00am-3:45pm ET |
| SOXL Swing | `soxl_bot.py` | SOXL | Wife's TFSA (Wealthsimple) | Daily bars | 3:20pm, 3:35pm, 3:45pm ET |
| SOXL Intraday | `soxl_intraday_bot.py` | SOXL | Non-reg (Wealthsimple) | 15-min bars | Every 15 min, 10:00am-3:20pm ET |

### tqqq_bot.py — TQQQ Mean-Reversion Swing

Pure mean-reversion strategy. Buys TQQQ when deeply oversold, holds 5-10 days expecting a bounce back toward the mean. Uses a scoring system (up to 6 points across trend + momentum dimensions) with per-category floor requirements to prevent buying falling knives.

**Why it was silent for 2+ months:** TQQQ RSI was 69-79 (bull market). Path A requires RSI < 35, Path D requires RSI < 50. Both correctly blocked.

### tqqq_intraday_bot.py — TQQQ Momentum Pullback Intraday

Pure momentum continuation strategy — the philosophical opposite of `tqqq_bot.py`'s
mean-reversion approach. Buys a brief pullback to VWAP or the fast EMA (5,
originally 9 — see [Validated Parameters](#why-ema-fast--5-not-9) below)
*within* an
already-established uptrend, on the assumption that the pullback gets bought and
momentum resumes. Every trade exits same day; no overnight risk. Multiple signals
can fire in one day — the bot no longer gates to one alert per day (removed
deliberately; judgment on which setup to take is left to the trader). Validated
via 4.5-year Databento backtest across 1,128 trading days.

**Data source:** `yfinance` (not Databento) — chosen because Databento requires a
paid subscription for live intraday polling, while `yfinance`'s free tier is
sufficient once the strategy is built around 15-min bars (see
[Why 15-min Bars, Not 5-min](#why-15-min-bars-not-5-min) below).

**Account:** IBKR, same as TQQQ swing — large capital deployed briefly per trade,
same-day flat.

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

### tqqq_intraday_bot.py Signal Conditions

All five conditions must be true on the most recently **completed** 15-min bar
(left-labeled — a bar stamped `10:15` covers 10:15am-10:30am and is only
evaluated once it closes at 10:30am):

```
1. close > VWAP                          (price above session VWAP)
2. EMA5 > EMA13                          (short-term uptrend confirmed)
3. volume >= 1.2x time-of-day average    (real participation, not drift)
4. prev bar low within $0.75 of VWAP     (the pullback — dip toward support)
   OR within $0.75 of EMA5
5. close >= VWAP AND close >= EMA5       (the recovery — dip was bought;
                                           note: AND, not OR — both lines
                                           must clear, tested and confirmed
                                           this is intentional, not just
                                           convention — see below)
```

No RSI filter — tested and removed (see
[Validated Parameters](#tqqq-intraday--45-year-databento-backtest) below).
Trading window: 10:00am-3:30pm ET. No signals before 10:00am (needs time for
EMA5/EMA13/volume baseline to populate) or after 3:30pm (not enough time left
for the trade to develop before the forced 3:30pm exit).

#### Exit Strategy
```
Target:      +$0.50 per share
Stop:        -$0.40 per share
Time stop:   3:30pm ET (exit before market close regardless of P&L)
R/R ratio:   1.25:1
```

#### Multiple Signals Per Day — By Design
Unlike every other bot in this system, `tqqq_intraday_bot.py` does **not** gate
to one signal per day. If a second (or third) valid setup appears, a new alert
fires with a `[SIGNAL #2 TODAY]` tag and a note that an earlier signal already
fired. The trader decides which signal (if any) to act on — the bot's job is
to flag every valid setup, not to pre-select one.

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
Target:         +4% from entry bar close
Stop:           -2% from entry bar close
Time stop:      3:35pm ET (exit before market close)
R/R ratio:      2:1
Position size:  5% of portfolio baseline per trade
                ($50 at $1,000 baseline — scale up after signal validation)
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

### TQQQ Intraday — 4.5-Year Databento Backtest

**Backtest environment:** Google Colab notebook (not a repo `.py` script — the
strategy was iterated live via chat/Colab, not committed as a standalone
backtest file like `soxl_intraday_backtest.py`).

**Data:** Databento 1-min OHLCV, 2022-01-03 to 2026-07-02 (1,128 trading days),
resampled to 15-min bars for signal evaluation.

**Original validated result** (VOLUME_MULT=1.0, PULLBACK_DIST=0.50 — since
superseded, see methodology correction below):
```
Timeframe:      15-min bars
Signal rate:    57.7% of trading days (651 signals)
Win rate:       65.4%  (426W / 225L)
Avg win:        +$0.46/share
Avg loss:       -$0.36/share
Expectancy:     +$0.173/share
Total P&L:      +$112.37/share over 4.5 years (+$11,237 at 100 shares)
Max drawdown:   $2.00/share
Exit breakdown: TARGET 56% / STOP 31% / TIME 14%
```

**Per-year consistency at original settings** (win rate never dropped below
63% in any year, including the 2022 bear market):
```
2022: 139 trades, 64.7% WR, +$23.48, exp +0.169
2023: 152 trades, 63.2% WR, +$23.31, exp +0.153
2024: 148 trades, 66.2% WR, +$25.45, exp +0.172
2025: 141 trades, 68.8% WR, +$28.24, exp +0.200
2026:  71 trades, 63.4% WR, +$11.89, exp +0.167
```

#### CRITICAL: Backtest/Live Volume Methodology Mismatch (Discovered and Fixed)

The result above was produced using a **rolling 20-bar volume average**
(`df['volume'].rolling(20, min_periods=5).mean()`) in the Colab backtest —
the same cross-day-leakage pattern that was identified and fixed in the live
bot's `add_indicators()` early on (see [Volume Baseline](#volume-baseline--time-of-day-averaging-not-simple-rolling)
below), but the fix was **never carried back into the backtest**. This meant
the validated `VOLUME_MULT` threshold was tuned against a systematically
different — and flawed — volume measure than the one the live bot actually
runs. Caught during a review of `add_indicators()`'s own self-referencing
bug (today's own bar contaminating its own baseline — see
[Task A](#live-bot-volume-fetch-and-baseline-fixes-task-a) below), which
prompted checking whether the *backtest's* volume logic matched live at all.
It didn't.

**The backtest was rebuilt** to match live logic exactly: time-of-day
averaging, using only prior trading days (never today's own bar, never
future days — a trailing window, not a whole-dataset average, to avoid
lookahead bias). Re-running the volume threshold sweep under this corrected
methodology **reversed the original conclusion**:
```
                    ORIGINAL (rolling-20, flawed)    CORRECTED (time-of-day, matches live)
Best VolMult:       1.0x                             1.2x-1.3x (see below)
1.0x expectancy:    $0.173                            $0.157
Peak expectancy:    $0.173 (at 1.0x)                  $0.165 (at 1.3x)
```
The original "quiet, low-volume pullbacks are the edge" narrative does not
hold up under corrected methodology — some real volume confirmation
(1.2x-1.3x) now outperforms 1.0x. The original backtest was also modestly
**overstating** performance across the board (not just picking the wrong
threshold), since the flawed rolling window inflated numbers at every
setting, not only at 1.0x.

#### Why VOLUME_MULT = 1.2, Not 1.0 or 1.3

Corrected-methodology sweep (time-of-day averaging, prior-days-only,
trailing window — matches live bot exactly):
```
1.0x: 706 trades, 63.3% WR, $0.157 expectancy  (old default)
1.1x: 652 trades, 63.0% WR, $0.158 expectancy
1.2x: 590 trades, 64.6% WR, $0.164 expectancy  ← selected
1.3x: 519 trades, 64.9% WR, $0.165 expectancy  (mathematical peak)
1.4x: 474 trades, 64.8% WR, $0.160 expectancy
1.5x: 426 trades, 64.8% WR, $0.159 expectancy
1.8x: 301 trades, 65.1% WR, $0.162 expectancy
2.0x: 238 trades, 64.7% WR, $0.158 expectancy
```
1.3x is the mathematical peak, but the gap between 1.2x and 1.3x
($0.164 vs $0.165 — a $0.001 difference) sits well within the noise floor
for a sample this size, while 1.3x sacrifices 71 trades (12% fewer signals)
for that statistically indistinguishable gain. When two adjacent settings
are this close in quality, the one with the larger, more robust sample size
is the safer real-world choice — 1.2x was selected on that basis, not
because 1.3x is wrong.

**Full stats at VOLUME_MULT=1.2 combined with PULLBACK_DIST=0.75** (the
exact live combination) were confirmed via the 2D grid validation below —
`Exp=$0.163, WR=64.4%, Trades=596` — resolving the standalone-validation gap
that existed here. A dedicated exit-reason breakdown, per-year table, and
equity curve specifically for this combination (the same depth of detail
shown for 1.3x below) have not been separately generated — the 2D grid
confirms overall quality and joint validity, but that finer-grained detail
remains outstanding if a full audit is wanted.

**Full stats at VOLUME_MULT=1.3** (corrected methodology, for reference —
the mathematical peak, not the selected live setting):
```
Signal rate:    46.0% of trading days (519 signals)
Win rate:       64.9%  (337W / 182L)
Avg win:        +$0.44/share
Avg loss:       -$0.35/share
Expectancy:     +$0.165/share
Total P&L:      +$85.72/share over 4.5 years (+$8,572 at 100 shares)
Max drawdown:   $2.40/share
Exit breakdown: TARGET 53% / STOP 29% / TIME 18%

2022: 100 trades, 61.0% WR, +$14.18, exp +0.142
2023: 131 trades, 62.6% WR, +$16.67, exp +0.127
2024: 118 trades, 60.2% WR, +$15.35, exp +0.130
2025: 116 trades, 69.0% WR, +$23.57, exp +0.203
2026:  54 trades, 79.6% WR, +$15.94, exp +0.295
```
Note 2026's 79.6% win rate sits well above every other year (60-69% range)
— with only 54 trades from a partial year, this is plausibly a small-sample
effect inflating the blended average rather than a genuine regime shift.
Excluding 2026, the 2022-2025 core expectancy sits closer to $0.13-$0.15,
a more conservative and probably more reliable estimate of steady-state
performance than the headline $0.165 blended number.

#### Why 15-min Bars, Not 5-min

The strategy was originally built and backtested on 5-min bars, then switched
to 15-min. Two independent reasons converged on 15-min:

1. **Signal quality.** 15-min bars filter noise a 5-min bar can't. At identical
   $0.50/$0.40 target/stop, 5-min produced 45% TARGET exits / 63% win rate;
   15-min produced 53% TARGET exits / 65-67% win rate on the same underlying
   data. A completed 15-min bar represents a stronger, more confirmed move.
2. **Data delay compatibility.** `yfinance`'s free tier has a ~15-20 minute
   delay on intraday US equity data — a dealbreaker for a 5-min strategy
   (the signal would fire on stale, already-completed price action), but a
   non-issue for 15-min bars, since a 15-min-delayed feed showing a *completed*
   15-min bar is simply... a completed 15-min bar. The delay disappears into
   the bar's own duration.

#### Why EMA_SLOW = 13, Not 21

EMA21 was the original slow EMA. Sweep testing (`EMA_SLOW` held at 13 vs 21,
all else constant) showed EMA13 outperforming across every metric at the same
$0.50/$0.40 target/stop:
```
EMA21: expectancy $0.131/share, 63% win rate, $45.1 total P&L (344 signals)
EMA13: expectancy $0.163/share, 67% win rate, $56.2 total P&L (344 signals)
```
Same signal count, better quality — EMA13 confirms trend reversals faster
without adding noise. 8/13 (or 9/13) EMA pairs are also a known convention
in leveraged-ETF intraday trading (Fibonacci-adjacent periods), so this result
independently converged on established practice rather than contradicting it.

#### Why EMA_FAST = 5, Not 9

Unlike `EMA_SLOW`, the fast leg had never been swept — it was left at the
conventional value (9) while only the slow leg was tested. A user question
("is EMA9 the fastest option, should we try something else") prompted a
proper 2D grid sweep (fast x slow, all combinations with fast < slow):
```
EMA9/13  (old default): 596 trades, 64.4% WR, $0.163 expectancy
EMA8/13:                611 trades, 65.8% WR, $0.172 expectancy
EMA7/13:                621 trades, 65.9% WR, $0.174 expectancy
EMA6/13:                631 trades, 65.9% WR, $0.176 expectancy
EMA5/13:                642 trades, 67.1% WR, $0.186 expectancy
EMA4/13:                657 trades, 67.6% WR, $0.189 expectancy
EMA3/13:                665 trades, 68.7% WR, $0.196 expectancy
```
**Monotonic improvement all the way down to the edge of the tested range** —
every metric (trades, win rate, expectancy) improved as the fast EMA got
faster, with no peak in sight even at EMA3. A cleanly monotonic curve that
never turns over, especially one converging toward the fastest possible
setting, is a classic overfitting warning sign, not reassuring evidence —
an EMA3 on 15-min bars only represents 45 minutes of price history, at
which point it barely smooths anything and starts tracking price itself
rather than a genuine trend.

**Before trusting this, an out-of-sample validation was run**: the 4.5-year
dataset was split chronologically into two independent, non-overlapping
periods (2022-01 to 2024-06, and 2024-07 to 2026-07), and the top candidates
were re-tested on each half **separately** — a real edge should hold up in
both periods independently; a curve-fit result typically shows up strongly
in only one:
```
                Period A (625 days)      Period B (503 days)
EMA3/17:        68.7% WR, exp $0.179     71.4% WR, exp $0.230
EMA5/13:        66.7% WR, exp $0.176     67.7% WR, exp $0.198
EMA9/13:        62.1% WR, exp $0.140     67.7% WR, exp $0.193
```
The ranking (3/17 > 5/13 > 9/13) held **identically** in both independent
periods — not just in the blended full-dataset average. This is meaningfully
stronger evidence than the single-period sweep alone, and argues against
this being pure curve-fitting.

**EMA5/13 was selected over EMA3/17**, despite EMA3/17 testing marginally
better in both periods, using the same reasoning already applied to
`VOLUME_MULT` (1.2x over the mathematically-superior-but-edge-of-range
1.3x): when a more extreme, edge-of-tested-range setting is close in
performance to a more conservative one, and both pass validation, the more
conservative setting is the safer real-world choice — less exposure to a
regime this specific setting hasn't been tested against, closer to
established Fibonacci-family convention, and further from the point where
the indicator stops meaningfully representing a "trend."

**A known gap:** the out-of-sample split only tested EMA3/17 and EMA5/13
against the old EMA_SLOW options (13/17/21) already in the original sweep
range — it did not re-run the full fast x slow 2D grid with out-of-sample
validation for every combination. EMA5/13 specifically was chosen partly
on convenience (it was the best non-edge-case candidate from the first
sweep), not because every possible fast/slow pairing was independently
out-of-sample tested.

#### VOLUME_MULT Note

See [Backtest/Live Volume Methodology Mismatch](#critical-backtestlive-volume-methodology-mismatch-discovered-and-fixed)
above for the full history — the original `VOLUME_MULT = 1.0` finding
(monotonically decreasing performance above 1.0x) was based on a flawed
backtest volume calculation and has been superseded. Current live setting
is `1.2`, validated against corrected methodology.

#### Why No RSI Filter

RSI (45-65 band) was part of the original design but removed after direct
comparison at identical $0.50/$0.40 parameters:
```
RSI 45-65 filter ON:  34% TARGET / 33% STOP / 34% TIME exits, mean P&L $0.06
RSI filter OFF:       45% TARGET / 34% STOP / 21% TIME exits, mean P&L $0.10
```
RSI was blocking valid momentum setups where price action was fine but RSI
happened to sit outside the band — a filter with no positive signal value
for this strategy, only cost.

#### Why $0.50 Target / $0.40 Stop, Not $1.00+ Targets

Two separate sweeps ($1.00, $1.50 targets) were tested and rejected:
```
$0.50 target:  53-56% TARGET exits, 10-14% TIME exits   ← healthy, selected
$1.00 target:  17% TARGET exits,    58% TIME exits       ← target rarely reached
$1.50 target:  11% TARGET exits,    37% TIME exits, 52% STOP exits (worst)
```
On a 15-min chart, TQQQ rarely delivers a clean $1.00+ move within a single
session. Larger targets don't increase expectancy — they just convert clean
TARGET exits into directionless TIME exits (or, at $1.50, into STOP exits,
since the trade sits open longer waiting for a move that doesn't come).

#### Why PULLBACK_DIST = 0.75, Not 0.50

`PULLBACK_DIST` was originally set to $0.50 as an initial reasonable-sounding
value when the strategy was first designed, and — unlike every other
parameter — was never actually swept until much later. A sweep from $0.20 to
$1.00 showed every metric improving monotonically as the threshold widened,
then plateauing right around $0.75-$1.00:
```
$0.20: 570 trades, 63.7% WR, $0.153 expectancy
$0.30: 616 trades, 64.1% WR, $0.159 expectancy
$0.40: 642 trades, 65.0% WR, $0.168 expectancy
$0.50: 651 trades, 65.4% WR, $0.173 expectancy  (old default)
$0.60: 654 trades, 66.2% WR, $0.180 expectancy
$0.75: 660 trades, 66.4% WR, $0.181 expectancy  ← selected (plateau)
$1.00: 662 trades, 66.3% WR, $0.181 expectancy  (identical to $0.75 — confirms plateau)
```
A tighter pullback requirement was quietly filtering out genuine setups
where price dipped just slightly further than the old $0.50 cutoff before
recovering — the same "tighter isn't automatically better" lesson as the
volume threshold, though this sweep produced a clean monotonic-then-plateau
result rather than the volume threshold's inverted-U shape.

**Historical note:** this sweep was originally run using the **flawed**
rolling-20-bar volume methodology, before the volume methodology correction
above — meaning at the time, `PULLBACK_DIST` and `VOLUME_MULT` had only ever
been validated independently, against different volume baselines, never
jointly. **This gap has since been closed** — see
[2D Grid Validation](#2d-grid-validation-volume--pullback-jointly-under-corrected-methodology)
immediately below.

#### 2D Grid Validation — Volume x Pullback, Jointly, Under Corrected Methodology

A 36-combination grid (`VOLUME_MULT` 1.0-1.5x x `PULLBACK_DIST` $0.50-$1.25),
run entirely under the corrected time-of-day volume methodology, confirms
the current live combination:
```
Current live (1.2x, $0.75):  Exp=$0.163  WR=64.4%  Trades=596
Grid peak     (1.5x, $0.85):  Exp=$0.168  WR=65.7%  Trades=431
```
The gap ($0.005) is smaller than the 1.2x-vs-1.3x difference already treated
as noise-level in the single-parameter sweep above, and every one of the 36
cells in the grid falls within a narrow $0.153-$0.168 expectancy band — a
flat, non-fragile surface rather than a sharp, easily-overfit peak. The
"best" cell also sacrifices 28% of trade count (431 vs 596) for that
marginal gain, and produces meaningfully *less* total P&L ($71.3 vs
$96.8-98.2/share) as a result. Current settings were kept unchanged based on
this result — the joint combination is confirmed solid, not just each
parameter validated in isolation.

#### ATR-Based Pullback Distance — Considered, Not Implemented
A fixed dollar amount for `PULLBACK_DIST` doesn't scale with price level or
day-to-day volatility — $0.75 is a different percentage of price at $40
than at $90, and the same in a calm week as a violent one. An ATR-based
threshold (e.g., 0.5x today's ATR instead of a flat dollar figure) would be
the more rigorous fix, self-correcting for both price drift and volatility
regime changes automatically. Not implemented: it requires a genuinely new
indicator (ATR itself), and turns a clean 1D sweep into a 2D grid (ATR
period × multiplier) — meaningfully more backtest work for what, given the
$0.75 fixed-dollar plateau already closed most of the gap, is likely a
smaller marginal improvement on top of diminishing returns. Treated as a
candidate future project, not a near-term change.

---

## VPS Crontab Schedule

**Server timezone: UTC — but `CRON_TZ=America/Toronto` makes all times ET automatically**

```
CRON_TZ=America/Toronto
DISCORD_URL="https://discord.com/api/webhooks/..."

# ── SOXL Intraday — every 15 min, 10:00am-3:20pm ET ──────────────────────────
*/15 10-14 * * 1-5  soxl_intraday_bot.py  → soxl_intraday.log
0,20 15 * * 1-5     soxl_intraday_bot.py  → soxl_intraday.log

# ── SOXL Swing — 3:20pm, 3:35pm, 3:45pm ET ───────────────────────────────────
20,35,45 15 * * 1-5  soxl_bot.py          → soxl_swing.log

# ── TQQQ Swing — every 15 min, 9:30am-4:00pm ET ──────────────────────────────
30,45 9 * * 1-5     tqqq_bot.py           → tqqq.log
*/15 10-15 * * 1-5  tqqq_bot.py           → tqqq.log
0 16 * * 1-5        tqqq_bot.py           → tqqq.log

# ── TQQQ Intraday — every 15 min, 10:00am-3:45pm ET ──────────────────────────
0,15,30,45 10-15 * * 1-5  tqqq_intraday_bot.py  → tqqq_intraday.log

# ── After-market reconciliation and log push ──────────────────────────────────
# Reconcile SOXL intraday gate blocks with MFE data (4:05pm ET)
5 16 * * 1-5   soxl_intraday_bot.py --reconcile  → soxl_intraday.log

# Reconcile SOXL swing gate blocks with 5d/10d prices (4:05pm ET)
5 16 * * 1-5   soxl_bot.py --reconcile           → soxl_swing.log

# Reconcile TQQQ intraday trade log outcomes + Discord summary (4:30pm ET)
# Deliberately later than the SOXL 4:05pm slot — gives yfinance extra time
# to fully settle the final 1-min bars of the session (Yahoo is known to be
# sluggish right after the closing bell). Reconcile only needs data up to
# 3:30pm ET (the strategy's hard exit time) but the buffer costs nothing.
30 16 * * 1-5  tqqq_intraday_bot.py --reconcile --notify  → tqqq_intraday.log

# Push all JSON logs to GitHub (4:40pm ET — moved from 4:15pm so it runs
# after the TQQQ intraday reconcile above has finished)
40 16 * * 1-5  push_logs.sh
```

**DST is handled automatically** by `CRON_TZ=America/Toronto` — no manual
November/March updates needed. The OS timezone database handles the transition.

### ✅ DST Handling — Automatic via CRON_TZ

The crontab includes `CRON_TZ=America/Toronto` at the top, which tells the cron
daemon to interpret all schedule times as Eastern Time. The OS timezone database
handles DST transitions automatically — no manual crontab updates needed in
November or March.

```bash
# Top of crontab:
CRON_TZ=America/Toronto
DISCORD_URL="https://discord.com/api/webhooks/..."

# Times are written directly in ET — no UTC conversion needed:
*/15 10-15 * * 1-5         soxl_intraday_bot.py   # 10:00am-3:45pm ET
0,15,30,45 10-15 * * 1-5   tqqq_intraday_bot.py   # 10:00am-3:45pm ET
```

If `CRON_TZ` is ever removed or the crontab is rebuilt without it, revert to
UTC times and set a calendar reminder for the first Sunday of November (EST)
and second Sunday of March (EDT) each year.

---

## Log Files Reference

### Console Logs (VPS only, not pushed to GitHub)

| File | Bot | Contents |
|------|-----|----------|
| `/root/logs/soxl_intraday.log` | soxl_intraday_bot.py | Every 15-min scan output + reconciliation |
| `/root/logs/soxl_swing.log` | soxl_bot.py | Daily scans at 3:20-3:45pm + swing reconciliation |
| `/root/logs/tqqq.log` | tqqq_bot.py | Every 15-min scan output |
| `/root/logs/tqqq_intraday.log` | tqqq_intraday_bot.py | Every 15-min scan output, full condition-by-condition rejection reasons, reconciliation, and crash tracebacks |
| `/root/logs/heartbeat.log` | all bots (shared file) | One `[timestamp] bot_name OK` line per successful normal-mode run — `tail` this to confirm every bot's cron is still firing across the whole VPS. `tqqq_intraday_bot.py` writes here on every non-reconcile run (including skips), matching `soxl_intraday_bot.py`'s convention; it does **not** write during `--reconcile`, and does **not** write on a crash (a stale heartbeat during market hours is itself the "something's wrong" signal, on top of the direct Discord crash alert — see [Key Implementation Details](#tqqq_intraday_botpy)) |
| `/root/logs/git_sync.log` | push_logs.sh | Daily push results and any git errors |

### JSON Files (pushed to GitHub daily at 4:40pm ET)

| File | Bot | Contents |
|------|-----|----------|
| `soxl_intraday_trade_log.json` | soxl_intraday_bot.py | Confirmed intraday trades that passed slippage gate |
| `soxl_gate_blocks.json` | soxl_intraday_bot.py | Every near-miss rejection with MFE simulation data |
| `soxl_trade_log.json` | soxl_bot.py | Confirmed SOXL swing trade entries |
| `soxl_swing_gate_blocks.json` | soxl_bot.py | SOXL swing rejections with 5d/10d price outcomes |
| `trade_log.json` | tqqq_bot.py | Confirmed TQQQ swing trade entries |
| `tqqq_gate_blocks.json` | tqqq_bot.py | TQQQ swing rejections with RSI gap tracking |
| `tqqq_intraday_trade_log.json` | tqqq_intraday_bot.py | **Every** signal that fires (no take/skip filtering), auto-reconciled against real price data at 4:30pm — see schema below |
| `tqqq_intraday_rejections.jsonl` | tqqq_intraday_bot.py | Every bar that failed one or more signal conditions, with actual value / required threshold / %-of-required for each — see schema below |
| `tqqq_intraday_near_miss_outcomes.jsonl` | tqqq_intraday_bot.py | Hypothetical outcomes for rejections that were near-misses (≥80% of threshold), auto-reconciled alongside the real trade log at 4:30pm — see schema below |

### Trade Log Fields — TQQQ Intraday (`tqqq_intraday_trade_log.json`)

Every alert is logged and treated as if it were taken at the alert price —
there is no manual "did I actually trade this" flag. This keeps the log fully
automatic and directly comparable to the backtest, which makes the same
assumption:

```json
{
  "date":            "2026-07-08",
  "bar_time":         "10:15 ET",
  "signal_bar_ts":    "2026-07-08T10:15:00-04:00",
  "signal_number":    1,             ← 1st, 2nd, 3rd... signal that day
  "alert_price":      80.40,         ← assumed entry = signal bar close
  "target":           80.90,
  "stop":             80.00,
  "vwap":             80.05,
  "ema_fast":         80.20,
  "ema_slow":         80.10,
  "vol_mult":         1.7,
  "pullback_src":     "VWAP + EMA5", ← which reference the pullback was near (label reflects EMA_FAST, currently 5)
  "prev_low":         80.10,
  "prev_vwap":        80.05,
  "prev_ema9":        80.12,
  "ema_spread_cur":     0.10,        ← EMA_FAST-EMA13 spread, current bar (logged only, not on alert)
  "ema_spread_prev":    0.10,        ← same spread, prior bar — see Momentum Trend note below
  "momentum_note":      "Steady (spread unchanged)",
  "vwap_extension_pct": 0.42,        ← % price is above VWAP — shown on live alert
  "alert_sent_at":    "2026-07-08T10:15:03-04:00",

  "reconciled":       true,          ← filled by --reconcile at 4:30pm ET
  "exit_price":       80.90,
  "exit_time":        "10:47:00",
  "exit_reason":      "TARGET",      ← TARGET / STOP / TIME
  "max_favorable":    81.05,         ← highest price reached after entry
  "max_adverse":      80.15,         ← lowest price reached after entry
  "pnl_per_share":    0.50
}
```

`max_favorable` / `max_adverse` answer the question "was this a clean move or
a zigzag?" — a TARGET-hit trade where `max_adverse` sat close to `alert_price`
was a clean, low-drama winner; one where `max_adverse` dropped close to the
stop before recovering was a genuine zigzag that could easily have gone the
other way on a slightly different fill.

#### Live Alert Context Fields (Discord Message)

Three pieces of context were added to the live Discord alert, beyond the
core signal conditions — each chosen because it answered a real decision the
trader had actually needed to reason through manually, not because it seemed
reasonable in the abstract:

- **`Time left`** — hours/minutes remaining until the 3:30pm forced exit,
  computed at alert time. A signal at 2:45pm has much less room to develop
  than one at 10:15am; this was judged closer to essential information than
  optional analysis.
- **`Earlier signal(s) today`** — for signal #2+ only (invisible on a normal
  single-signal day), a live status check (`check_earlier_signals_status()`)
  on any prior same-day signal: `CLOSED — hit target $X` / `CLOSED — hit stop
  $X` / `OPEN — currently $X (+/-$Y unrealized)`. Uses the 15-min bars already
  fetched for the current run (no extra API call) — a lightweight, same-day
  estimate, not the authoritative outcome (`--reconcile` at 4:30pm remains
  the precise source of truth, using 1-min data). Built directly in response
  to a real averaging-down question that came up live — both signals that
  day went on to hit STOP, which would have compounded a loss if acted on
  without knowing the first trade's status.
- **`VWAP ext`** — % price has stretched above session VWAP. A stretch/risk
  gauge, not a quality signal (every valid setup is positive by definition of
  condition 1) — small extension (~0.2-0.5%) suggests a fresh move with room
  left; large extension (2%+) suggests price may already be stretched, with
  higher odds of entering late. No backtested threshold exists for this
  strategy specifically — shown as context for judgment, not a filter.

**Considered and rejected for the live alert:**
- **RSI** — tested and found to actively hurt performance as a filter (see
  [Why No RSI Filter](#why-no-rsi-filter) above); showing it live risked
  the trader unconsciously using it as an informal veto on setups the data
  says are fine.
- **MACD histogram** — redundant with EMA spread trend (both measure
  "momentum of the momentum" from the same underlying EMA data) and uses an
  unvalidated 12/26/9 period pairing never tested against this strategy.
- **Volume trend (bar-over-bar)** — logged silently (`ema_spread_cur`/
  `ema_spread_prev`/`momentum_note` fields above cover the parallel EMA
  version), but deliberately kept off the live alert. A live example
  surfaced the exact risk: a genuine signal showed volume declining from
  1.3x to 1.1x between two same-day alerts — both numbers individually sit
  comfortably above the validated threshold, but displaying the *decline* as
  a "fading" warning risked teaching the trader to veto exactly the kind of
  quiet, healthy setup the backtest validates. Standard technical-analysis
  volume-divergence intuition doesn't transfer cleanly to a pullback
  strategy the way it does to breakout trading.
- **EMA spread trend (accelerating/decelerating)** — computed and logged
  (see schema above) for future `--summary` analysis, but not shown live.
  Unlike VWAP extension, this didn't answer a decision that had actually
  come up in practice, and there's no backtest evidence that the *trend* of
  the spread (as opposed to the EMA_FAST>EMA_SLOW threshold itself, which is
  tested) predicts anything — a plausible-sounding idea without validation
  behind it, held to a different bar than the three fields that shipped.
- **52-week high distance** — considered, rejected outright. TQQQ's daily
  rebalancing/volatility-decay distorts what this normally means for a
  stock (a "far from 52-week high" reading can just mean choppy sideways
  action, not "due for a bounce"), and the concept operates on a
  multi-month timeframe with no clean mechanical link to a same-day 15-min
  pullback pattern.
- **VIX / gap-and-go entries** — see [Architecture Decisions](#architecture-decisions)
  below.

### Rejection Log Fields — TQQQ Intraday (`tqqq_intraday_rejections.jsonl`)

JSON Lines format (one JSON object per line) — cheap to append, safe to
grow indefinitely, no read-rewrite-whole-file cost. Written every time a
15-min bar fails one or more signal conditions:

```json
{"date": "2026-07-09", "time": "13:15", "failed": {
  "volume": {"actual": 1450838, "required": 1733827.0, "pct_of_required": 83.7}
}}
```

Built after repeatedly diagnosing "why no alert today" by hand from the
plain-text log — this gives the same information in structured, queryable
form. `pct_of_required` is normalized to mean the **same thing for every
condition**: higher % = closer to passing, 100% = right at the boundary.

For "must meet/exceed" conditions (`volume`, `vwap`, `ema_trend`, `recovery`)
this is `actual/required` directly. For `pullback` — a distance that must
stay *under* the threshold to pass — this is inverted (`required/actual`),
so the interpretation direction stays uniform without the reader needing to
remember an exception. **This uniformity was not correct on the first pass**
— `pullback` originally used the naive (uninverted) formula, meaning a
higher % there actually meant *further* from passing, backwards from every
other condition. Caught before it could corrupt near-miss selection (which
depends on one simple `>=` comparison working identically across all
conditions) and fixed.

```bash
python3 tqqq_intraday_bot.py --blocker-summary        # last 30 days (default)
python3 tqqq_intraday_bot.py --blocker-summary 90      # last 90 days
```
Prints a ranked tally of which condition blocks the most often, plus the
average %-of-required for each — distinguishing a condition that fails
*often but narrowly* (worth reconsidering) from one that fails *often but
by a wide margin* (correctly filtering out non-setups).

### Near-Miss Outcomes — TQQQ Intraday (`tqqq_intraday_near_miss_outcomes.jsonl`)

```json
{
  "date":                     "2026-07-09",
  "time":                     "13:15",
  "failed_conditions":        ["volume"],
  "closest_pct":              83.7,
  "hypothetical_entry":       80.42,
  "hypothetical_exit":        80.90,
  "hypothetical_exit_reason": "TARGET",
  "hypothetical_pnl":         0.48,
  "max_favorable":            81.05,
  "max_adverse":              80.15
}
```

For every rejection where at least one failed condition scored
`pct_of_required >= NEAR_MISS_THRESHOLD` (80, configurable), replays 1-min
price data forward **as if a trade had been entered at the rejected bar's
close** — same replay logic as real-trade `--reconcile`, just against a
hypothetical entry. Runs automatically as part of `--reconcile` (reuses the
same 1-min data fetch — no extra API call), deduplicated against previously
reconciled near-misses so nothing is ever replayed twice.

**Purpose:** directly answers "would loosening the current threshold
actually have won more trades, or is the rejection correctly protecting
us" — from real, live, forward-tested data, not backtest inference. This
is a materially different question from the backtest sweeps: the sweeps
show aggregate historical behavior across 4.5 years of Databento data;
this shows what's actually happening in current live conditions, bar by
bar.

```bash
python3 tqqq_intraday_bot.py --near-miss-summary        # last 30 days
python3 tqqq_intraday_bot.py --near-miss-summary 90
```
Reports hypothetical win rate, expectancy, and total P&L — directly
comparable against real performance via `--summary`. If near-miss
performance is comparably healthy, the threshold may be too strict; if
meaningfully worse, the current threshold is correctly filtering these out.

**Explicitly not built as a threshold-change trigger.** The existence of
near-misses is expected, by design — a selective strategy is supposed to
reject most setups, and the 1D/2D backtest sweeps already showed loosening
thresholds doesn't improve aggregate performance. This tool exists to
surface a **different, narrower kind of evidence** (live near-miss
performance) that could support a *specific, surgical* future adjustment —
not as grounds to second-guess an already-validated threshold just because
rejections are occurring.

#### Bug Found and Fixed: Gap-Through-Target/Stop Mispricing

Discovered while testing near-miss reconciliation with synthetic data, but
the bug lives in the **shared replay logic** (`_replay_forward()`) used by
both real-trade `reconcile()` and this near-miss feature — meaning it
affected already-deployed, currently-running trade reconciliation too, not
just the new code.

**Root cause:** `target`/`stop` and the actual `entry_price` used in the
replay can be derived from different bars up to 15 minutes apart (a
signal's alert price vs. the next 1-min bar's open where the trade
actually enters; or, for near-misses, a rejected bar's close vs. its own
+15min entry point). If price moves in that gap, `entry_price` can land
outside the `[stop, target]` range the exit logic assumes — producing an
exit labeled `TARGET` but priced *below* entry (a "win" with negative P&L),
or labeled `STOP` but priced *above* entry (a "loss" with positive P&L).

**Fix:** clamp the exit price so it's always economically consistent with
its own label:
```python
if bar["low"] <= stop:
    exit_price = min(stop, entry_price)    # STOP never prices above entry
if bar["high"] >= target:
    exit_price = max(target, entry_price)  # TARGET never prices below entry
```
Verified with three tests: the exact original bug scenario (gap up through
target at entry) now correctly clamps to $0.00 instead of a nonsensical
negative P&L; the symmetric STOP-side gap clamps the same way; and the
normal, no-gap case is completely unaffected — still prices exactly at the
fixed target/stop as before, confirming the fix doesn't change behavior for
the vast majority of (non-gapping) trades.

#### Bug Found and Fixed: Near-Miss Reconciliation Silently Skipped on No-Signal Days

Found in production, not testing — a live day with zero real signals but
several rejections (several genuine near-misses among them) produced
`--near-miss-summary` output of "No near-miss outcomes yet," even though
`--reconcile` had run and logged "No unreconciled trades to process."

**Root cause:** `reconcile()`'s original structure checked for unreconciled
real trades first, and returned early — before ever reaching the
`reconcile_near_misses()` call at the bottom of the function — if none
existed. Near-miss reconciliation reads a completely different data source
(the rejection log, not the trade log), so it had no logical reason to
depend on whether real trades existed that day, but the code accidentally
coupled the two.

**Fix:** restructured so near-miss reconciliation always runs, independent
of the real-trade branch's outcome. The shared 1-min data fetch is still
reused when possible (if real trades needed reconciling, that same `raw`
data is passed to near-miss reconciliation too — no duplicate API call; if
there were no real trades, a fresh fetch happens specifically for the
near-miss job). Verified with a test reproducing the exact failure — an
empty trade log with a near-miss rejection present — confirming the fix
correctly reconciles the near-miss instead of exiting early, and a second
test confirming the normal case (real trades + near-misses same day) still
shares one single fetch, not two.

#### Bug Found and Fixed: Near-Miss Selection Let Multi-Condition Failures Through as False Positives

Found by inspecting real reconciled output after the above fix — several
"near-misses" had failed **4-5 conditions simultaneously**
(`vwap, ema_trend, volume, pullback, recovery` all at once), yet were
included in the near-miss set and dragging the aggregate hypothetical win
rate down to a misleading 33%.

**Root cause:** the original selection logic used `any()` — a bar qualified
as a near-miss if **at least one** failed condition scored
`>= NEAR_MISS_THRESHOLD`, regardless of how many other conditions failed or
how badly. A bar failing volume at 40% but recovery at 99% still passed,
even though that setup was never realistically close to firing as a whole.

**Fix — combines two independent corrections, evaluated together:**
```python
is_near_miss = (
    bool(failed)
    and len(failed) <= 2                          # count cap: readability/intent
    and all(                                        # correctness: EVERY failed
        d.get("pct_of_required", 0) >= NEAR_MISS_THRESHOLD  # condition must be close,
        for d in failed.values()                    # not just one of several
    )
)
```
The count cap alone (a fix initially proposed as sufficient) leaves a real
gap: a 2-condition failure where one is 95% and the other is 30% would
still pass a `len(failed) <= 2` + `any()` check, incorrectly counting a
setup with a genuinely wide miss on one condition as a "near-miss." The
`all()` requirement closes this — every failed condition must individually
clear the threshold, not just one of however many failed. Verified with a
7-case test suite reproducing the exact real-world noise pattern (three
5-condition failures, two genuine 1-2 condition near-misses, plus a
synthetic "one close + one wide miss" case specifically constructed to
catch the gap the count-cap-only version would have missed) — all seven
resolved correctly.

**Operational note:** because deduplication (`_load_near_miss_outcomes()`)
tracks already-processed rejections by `(date, time)`, fixing the filter
logic did not automatically correct outcomes already reconciled under the
old, looser rule — those stale records needed to be manually cleared from
`tqqq_intraday_near_miss_outcomes.jsonl` before re-running `--reconcile` to
get corrected results for the affected day.

#### Cleanup: Hardcoded "EMA9" Labels Found When EMA_FAST Changed

When `EMA_FAST` was changed from 9 to 5 (see
[Why EMA_FAST = 5](#why-ema_fast--5-not-9) above), several live-facing
strings were found to hardcode the literal text "EMA9" instead of reading
the actual configured value — meaning Discord alerts and rejection log
messages would have kept saying "EMA9" while the bot was actually computing
EMA5, silently misleading rather than just cosmetically stale. Fixed in:
the Discord alert's `pullback_src` label, the `recovery`/`pullback`
rejection reason messages, and the `check_signal()` docstring (which was
also separately stale on `VOLUME_MULT`/`PULLBACK_DIST`, corrected at the
same time). Also renamed the trade log's `prev_ema9` field to
`prev_ema_fast` — confirmed safe (nothing internal reads that field back)
before renaming; historical records written before this change retain the
old key name, new records use the new one, both represent the same
underlying value (prev bar's fast EMA) under whatever period was live at
signal time.

---

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

**Question 3: Is the -2% stop too tight?**
```
Look at mae_pct for entries where simulated_outcome=WON
mae_pct = the deepest % loss reached before the trade recovered and won

Example: mae_pct=-1.8% on a WON trade means price touched -1.8% before
recovering to hit the +4% target. If many winners show mae_pct near -2%,
the stop is dangerously close to the normal price oscillation range and
will be hit too often before the trade has time to recover.

mae_pct consistently -0.5% to -1.0% on winners → stop at -2% is fine
mae_pct frequently -1.5% to -1.9% on winners → consider widening to -3%
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

### TQQQ Intraday Review (`tqqq_intraday_trade_log.json`)

**Question 1: Is live performance tracking the backtest?**
```
Run: python3 tqqq_intraday_bot.py --summary
Compare win rate and expectancy against backtest baseline:
  Backtest: 65.4% win rate, $0.173/share expectancy
If live win rate drifts meaningfully below ~55-60% over 30+ trades,
something has changed (market regime, data quality, or a bug) — investigate
before assuming the strategy has simply had a bad stretch.
```

**Question 2: Are later signals in the day worse than the first?**
```
Group entries by signal_number in tqqq_intraday_trade_log.json
Compare win rate / pnl_per_share for signal_number=1 vs 2+
If later signals underperform meaningfully, that's useful judgment context
for which alerts to prioritize acting on — but not a reason to change the bot,
since the multi-signal design is intentional (see Architecture Decisions).
```

**Question 3: Are winners clean or zigzaggy? Are losers spikes or slow bleeds?**
```
For exit_reason=TARGET trades: look at max_adverse relative to alert_price.
  max_adverse close to alert_price  → clean, low-drama winner
  max_adverse close to the stop     → zigzag winner, could easily have lost
                                       on a slightly different fill/slippage

For exit_reason=STOP trades: look at max_favorable relative to alert_price.
  max_favorable barely above alert_price → straight-down loser, no false hope
  max_favorable well above alert_price   → price moved favorably first, then
                                            reversed — may indicate a slightly
                                            too-early entry or too-tight stop
```

**Question 4: Is the TIME exit rate still low?**
```
Backtest baseline: 14% TIME exits (56% TARGET / 31% STOP / 14% TIME)
A rising TIME percentage over live data suggests either the target is
becoming too ambitious for current volatility, or entries are firing later
in developing moves than they used to.
```

**Question 5: Are near-misses close to breakeven with real signals?**
```
Run: python3 tqqq_intraday_bot.py --near-miss-summary
Compare hypothetical win rate/expectancy against real --summary numbers.
Comparably healthy near-miss performance is a signal a threshold may be
worth revisiting (narrowly, not as a blanket loosening — the 1D/2D
backtest sweeps already tested blanket loosening and found it doesn't
help). Meaningfully worse near-miss performance confirms the current
threshold is correctly filtering these out — expected and healthy, not
a problem to fix.
```

**Question 6: Which condition blocks the most, and how close are the misses?**
```
Run: python3 tqqq_intraday_bot.py --blocker-summary
A condition failing often AND with a high average %-of-required (near 100%)
is worth cross-referencing against --near-miss-summary for that specific
condition. A condition failing often but with a LOW average %-of-required
is working as intended — cleanly separating real setups from noise, not
marginally clipping good trades.
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

# TQQQ intraday (last 50 lines)
tail -50 /root/logs/tqqq_intraday.log

# SOXL swing (last 50 lines)
tail -50 /root/logs/soxl_swing.log

# Shared heartbeat across all bots — confirms every cron is still firing
tail -20 /root/logs/heartbeat.log

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

# TQQQ intraday trade log — replays 1-min price data forward from each
# signal to auto-determine TARGET/STOP/TIME outcome (no manual fill entry)
cd /root/Stock-bot && python3 tqqq_intraday_bot.py --reconcile

# Same, but also post the day's results to Discord
cd /root/Stock-bot && python3 tqqq_intraday_bot.py --reconcile --notify

# Reconcile a specific past date (yfinance retains ~7 days of 1-min history —
# reconcile won't work for anything older than that)
cd /root/Stock-bot && python3 tqqq_intraday_bot.py --reconcile --date 2026-07-07
```

### Check TQQQ Intraday Live Performance
```bash
# Prints win rate, expectancy, total P&L, and a per-trade detail table
# across every reconciled signal to date
cd /root/Stock-bot && python3 tqqq_intraday_bot.py --summary

# Ranked tally of which condition blocks a signal most often, with average
# closeness (%-of-required) per condition
cd /root/Stock-bot && python3 tqqq_intraday_bot.py --blocker-summary
cd /root/Stock-bot && python3 tqqq_intraday_bot.py --blocker-summary 90   # last 90 days

# Hypothetical win rate/expectancy for near-miss rejections (auto-reconciled
# as part of --reconcile) — answers "would loosening the threshold help?"
cd /root/Stock-bot && python3 tqqq_intraday_bot.py --near-miss-summary
```

### Test a Bot Manually
```bash
# Force run (bypass market hours), dry run (no log writes)
cd /root/Stock-bot && python3 soxl_intraday_bot.py --force --dry-run
cd /root/Stock-bot && python3 soxl_bot.py --force --dry-run
cd /root/Stock-bot && python3 tqqq_bot.py --force --dry-run

# tqqq_intraday_bot.py has no --force/--dry-run flags — running it directly
# outside market hours safely exits with "Outside trading window" (no writes)
cd /root/Stock-bot && python3 tqqq_intraday_bot.py
```

### View Gate Block / Trade Log Data
```bash
cat /root/Stock-bot/soxl_gate_blocks.json
cat /root/Stock-bot/soxl_swing_gate_blocks.json
cat /root/Stock-bot/tqqq_gate_blocks.json
cat /root/Stock-bot/tqqq_intraday_trade_log.json
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

### Why TQQQ Intraday Uses `yfinance` Instead of Databento
Databento's historical data (used for the backtest) requires a paid subscription
for live intraday polling. `yfinance` is free but carries a ~15-20 minute data
delay on intraday US equities. This delay would break a fast (5-min) strategy,
which is the core reason the bot runs on 15-min bars instead — a completed
15-min bar is valid regardless of a 15-20 minute delay in receiving it, since
the bar's own 15-minute duration absorbs the lag. See
[Why 15-min Bars](#why-15-min-bars-not-5-min) for the full reasoning.

### Why TQQQ Intraday Has No Daily Signal Gate (Unlike Every Other Bot)
Every other bot in this system fires at most once per signal type per day.
`tqqq_intraday_bot.py` deliberately does not — multiple valid setups can occur
in one session, and the trader is better positioned than the bot to judge
which one (if any) is worth acting on in the moment. The trade-off: every
signal is still logged and auto-reconciled regardless of whether it was acted
on, so the log reflects raw signal quality rather than the trader's actual
results. This was a deliberate simplification — an earlier design considered
a manual `--mark-taken` flag to separate "signal fired" from "I actually
traded this," but that required an SSH round-trip after every fill, which
defeated the goal of a fully hands-off alert system. Every alert is now
logged and reconciled as if taken at the alert price, matching the backtest's
own assumption.

### Why TQQQ Intraday Reconciles Automatically Instead of Manual Fill Logging
An earlier design had the trader manually run `--log-fill entry <price>` and
`--log-fill exit <price>` after each trade, to measure real slippage against
the alert price. This was replaced with fully automatic reconciliation: after
market close, `--reconcile` re-fetches 1-min price data and replays it forward
from the entry point, determining TARGET/STOP/TIME outcome and P&L without any
manual step. The trade-off is that reconciled P&L reflects the alert price as
entry, not a slippage-adjusted real fill — but this exactly matches the
backtest's own assumption, keeping live results directly comparable to the
validated backtest numbers, and removes all manual bookkeeping.

### Why TQQQ Intraday's Heartbeat Excludes `--reconcile`
Matches `soxl_intraday_bot.py`'s existing convention (`if not args.dry_run and
not args.reconcile`) — heartbeat.log is meant to answer "is the 15-min signal
cron still firing," not "did the once-daily reconcile job run." Mixing the two
into one heartbeat would make a missed reconcile run (e.g. a temporary
yfinance outage right at 4:30pm) look identical to the far more serious
problem of the entire signal-check cron silently dying.

### Why a Crash Alerts Directly to Discord Instead of Relying on Heartbeat Silence Alone
`tqqq_intraday_bot.py` wraps its main logic in try/except: on any unhandled
exception, it logs the full traceback, then posts the error directly to
Discord before exiting — rather than only relying on a future person noticing
a stale heartbeat.log. A stale heartbeat can take up to 15 minutes (one cron
cycle) to become noticeable, and only if someone is actively checking it. The
direct Discord alert surfaces the failure within seconds, at the moment it
happens.

### Why `.gitattributes` Was Extended to Cover `.jsonl`, Not Just `.json`
The original `*.json merge=ours` rule (protecting VPS trade-log data from
being overwritten by a `git pull`) only covered files ending in `.json` —
not `.jsonl` (JSON Lines), the format chosen for the rejection log and
near-miss outcomes log specifically because it's cheap to append without
a read-rewrite-whole-file cost. This meant those two files had **no**
pull-protection at all, unlike every other piece of VPS trading data.
Caught during an unrelated review of what `.gitattributes`/`.gitignore`
actually do, and fixed by simply adding a matching `*.jsonl merge=ours`
line — same protection, same reasoning, just extended to cover the newer
file format.

### Why Retry Logic and the Holiday Pre-Check Were Ported, Not Reinvented
When `tqqq_intraday_bot.py` was first built, it had neither retry logic nor an
explicit holiday check — relying only on the empty-data path in
`fetch_15min_bars()` as an implicit fallback. A review of `soxl_intraday_bot.py`
found it already had both, well-tested, in production. Rather than design a
new approach, the exact same pattern (12 retries × 5 seconds, holiday
pre-check before entering the retry loop) was ported across — keeping the two
intraday bots' resilience behavior consistent, and avoiding two independently
-drifting implementations of the same problem. The one adaptation was for
`fetch_15min_bars()`'s different data pipeline (1-min fetch + manual resample,
vs SOXL's direct 15-min fetch) — see
[Key Implementation Details](#retry-logic--yahoo-publication-lag-ported-from-soxl_intraday_botpy).

### Why the Bot Doesn't Chase Gap-and-Go Days
On a day where TQQQ gaps up hard at the open and grinds continuously higher
without dipping, the bot correctly declines to fire — condition 4 (a genuine
pullback toward VWAP/EMA_FAST) has nothing to trigger on, since there's no dip to
recover from. This was confirmed live (a 4.66% gap-up day produced zero
signals until a genuine pullback eventually formed). Deliberately not
"fixed": gap-and-go entries have no natural stop-loss reference point the
way a pullback low provides, and the backtest has never tested this entry
type — bolting it onto a validated pullback strategy without its own
dedicated backtest risks quietly eroding the tested edge. Treated as a
candidate for a separate, future strategy/bot if pursued at all, not a
modification to this one.

### Why VIX Is Not Used as an Entry Filter
Considered explicitly and rejected as a blocking filter, though it remains a
reasonable candidate for **position-sizing context** (not yet implemented).
The core reason: this strategy's own backtest data across 2022 (a
high-volatility bear market) through 2025-2026 shows consistent positive
expectancy in every single year, without needing to change behavior by
volatility regime — there's no evidence in this strategy's own history that
elevated volatility hurts it, which is the opposite of the common trader
intuition that motivates VIX-based blackout filters generally. The same
reasoning applies to macro-event blackout days (FOMC, CPI) — some of the
strategy's best-performing periods likely coincide with high-volatility
catalyst days, so filtering them out risks removing edge rather than risk.

### Why 52-Week High Distance Was Not Added as Context
Considered and rejected. TQQQ's 3x daily-reset structure means a "far from
52-week high" reading can simply reflect choppy, sideways underlying price
action (volatility decay erodes value even without a real decline) rather
than genuine oversold conditions — the number doesn't cleanly mean for a
leveraged product what it means for a normal stock. It also operates on a
multi-month timeframe with no obvious mechanical connection to a same-day
15-min pullback pattern. Unlike VWAP extension (same underlying "how
stretched is this move" question, but measured at the timeframe the
strategy actually trades), this didn't have a clean, testable hypothesis
behind it.

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

### tqqq_intraday_bot.py

#### True VWAP Calculated on 1-min Data, Then Carried Into 15-min Bars
A naive approach would resample OHLCV to 15-min bars first, then compute VWAP
from the 15-min typical price × 15-min volume — this is only an approximation
and drifts from true VWAP, especially in volatile sessions. The correct
approach (used here) computes VWAP on the raw 1-min data first, where it
accumulates correctly bar-by-bar, and only *then* resamples — carrying
forward the last (most accurate) VWAP value within each 15-min window:
```python
raw["date"]       = raw.index.date
raw["tp"]         = (raw["high"] + raw["low"] + raw["close"]) / 3
raw["tp_vol"]     = raw["tp"] * raw["volume"]
raw["cum_tp_vol"] = raw.groupby("date")["tp_vol"].cumsum()
raw["cum_vol"]    = raw.groupby("date")["volume"].cumsum()
raw["vwap"]       = raw["cum_tp_vol"] / raw["cum_vol"]

df = raw.resample("15min", label="left", closed="left").agg(
    ...,
    vwap=("vwap", "last"),   # true VWAP at bar close, not a 15-min approximation
)
```
`groupby("date")` resets the cumulative sums each session, exactly matching
`soxl_intraday_bot.py`'s VWAP reset pattern below.

#### Volume Baseline — Time-of-Day Averaging, Not Simple Rolling
A naive `df["volume"].rolling(20).mean()` across multiple days of stacked
15-min bars has two separate, opposite failure modes, both caught during
development:

1. **Cross-day leakage:** a simple rolling window crosses day boundaries, so
   the first few bars of a new session get compared against *yesterday
   afternoon's* naturally quiet volume — producing false "high volume" signals
   at the open when nothing unusual is actually happening.
2. **Intraday starvation:** the fix for (1) — resetting the rolling window
   each day via `groupby("date")` — creates a new problem: with `min_periods=5`
   and the trading window opening at 10:00am (bar #3-4 of the day), the first
   signals of every session would see `vol_avg = NaN` and never fire until
   ~10:45-11:00am, silently shrinking the effective trading window.

**Final fix** — average volume for each specific time-of-day slot across the
whole multi-day dataset, so a 10:00am bar today is compared against *historical
10:00am bars*, not yesterday's quiet afternoon and not an empty rolling window:
```python
df["time_of_day"] = df.index.time
time_vol_avg       = df.groupby("time_of_day")["volume"].mean()
df["vol_avg"]      = df["time_of_day"].map(time_vol_avg)
```
This is also simpler than the intermediate rolling-with-shift version that
was tried first — same result, fewer moving parts.

#### Live Bot Volume Fetch and Baseline Fixes (Task A)
Two further issues were caught in the version above, both fixed together:

1. **`period="5d"` was leaving data on the table.** Yahoo's hard limit for
   1-min interval data is 7 calendar days, not 5 — `yfinance` will not serve
   more regardless of what's requested (a 15-day request would simply fail
   or truncate, not deliver 15 days). Changed to `period="7d"` in all three
   fetch locations (live signal check, retry loop, timeout fallback) plus
   `reconcile()`'s own fetch — a small, free improvement in baseline
   stability with no downside.
2. **Self-referencing bias:** the time-of-day average above, as originally
   written, included *today's own bar* in its own comparison baseline. With
   only ~3-6 trading days in the fetch window (vs. the 1,128-day backtest
   dataset, where any single day is negligible), today's own value could
   carry 15-30% of the weight in its own average — a high-volume bar
   partially inflates the very average it's being measured against, and
   vice versa. Fixed by excluding the current calendar date from the
   baseline calculation entirely:
```python
df["cal_date"] = df.index.date
today = date.today()
prior_days_only = df[df["cal_date"] < today]
if prior_days_only.empty:
    # Extreme edge case (first-ever run, long outage): fall back to
    # same-day averaging rather than leaving vol_avg all-NaN and
    # blocking every signal check that day.
    time_vol_avg = df.groupby("time_of_day")["volume"].mean()
else:
    time_vol_avg = prior_days_only.groupby("time_of_day")["volume"].mean()
```
Verified with a test: an extreme 9M-share volume spike planted on today's
own bar no longer inflates its own 1M-share historical baseline after the
fix (confirmed unaffected, vs. leaking through before). The zero-prior-data
fallback path was also tested independently and confirmed to populate
`vol_avg` rather than going all-`NaN`.

#### Candle-End-Time Trading Window Check (Left-Label Bar Gotcha)
Bars are left-labeled: a bar stamped `09:45` covers 09:45am-10:00am. A naive
window check comparing the bar's own label against the 10:00am start time
(`bar_time >= 10:00`) would incorrectly reject the very first valid bar of
the day, since its label (`09:45`) is technically before 10:00am even though
the bar itself *closes* at 10:00am and is the correct first bar to evaluate.
Fixed by checking the bar's **end** time instead of its label:
```python
candle_end_time = (cur.name + pd.Timedelta(minutes=15)).time()
if not (start <= candle_end_time <= end):
    return None
```

#### Completed-Bars-Only Fetch Filtering
`fetch_15min_bars()` pulls 5 days of 1-min data (not 1 day) specifically to
seed EMA13 and the time-of-day volume baseline with enough history from the
very start of each session — a 1-day fetch would starve both indicators until
mid-afternoon (only ~26 bars exist in a single trading day; EMA13 needs 13+
just to stabilize, and the volume baseline needs multiple days of the same
time-slot to be meaningful at all). After resampling to 15-min, any bar whose
window hasn't fully closed yet is explicitly dropped:
```python
now = datetime.now(ET)
df  = df[df.index + pd.Timedelta(minutes=15) <= now].copy()
```
The trailing `.copy()` is required — without it, this filter returns a pandas
*view* rather than an independent DataFrame, and the subsequent column
assignments in `add_indicators()` raise a `SettingWithCopyWarning`.

#### Multi-Signal Design — No Daily Flag File
An earlier version used a `.tqqq_intraday_traded_YYYY-MM-DD` flag file to
enforce one signal per day, matching the other bots' convention. This was
removed entirely once the multi-signal design was adopted (see
[Architecture Decisions](#why-tqqq-intraday-has-no-daily-signal-gate-unlike-every-other-bot)).
Signal counting for the `[SIGNAL #2 TODAY]` Discord label is now derived
directly from the trade log itself (`signals_today_count()`), not a separate
flag file — one less piece of state to keep in sync.

#### Automatic Reconciliation — Replay Logic
`--reconcile` scans every trade log entry where `reconciled: false`, and for
each one:
1. Re-fetches 7 days of 1-min data (Yahoo's hard limit — see
   [Task A](#live-bot-volume-fetch-and-baseline-fixes-task-a) below; same
   call the live bot uses)
2. Locates the entry point: the first 1-min bar **at or after**
   (`signal_bar_ts + 15min`) — this exactly matches the entry-timing
   assumption used in the original backtest
3. Walks forward minute-by-minute from there, tracking running high
   (`max_favorable`) and running low (`max_adverse`)
4. First touch of stop or target wins; if the 3:30pm cutoff is reached first,
   exits at that bar's open price with reason `TIME`

```python
day_bars = raw[raw.index.date == signal_ts.date()]
future = day_bars[day_bars.index >= entry_after]   # >= , not >  — see below
...
for ts, bar in future.iterrows():
    max_favorable = max(max_favorable, bar["high"])
    max_adverse   = min(max_adverse, bar["low"])
    if bar_end_time >= cutoff:
        exit_price, exit_reason = bar["open"], "TIME";  break
    if bar["low"] <= sl:
        exit_price, exit_reason = sl, "STOP";            break
    if bar["high"] >= tp:
        exit_price, exit_reason = tp, "TARGET";          break
```

**Off-by-one bug, caught during review, now fixed.** The filter originally
used strict `>` (`future = day_bars[day_bars.index > entry_after]`). Since
`entry_after` lands exactly on the label of the 1-min bar covering the very
first minute the trade is actually open (e.g. the `10:30:00` bar, if the
signal's 15-min bar closed at `10:30`), strict `>` excluded that bar
entirely — meaning entry price was taken from one minute *later* than
intended, and any target/stop hit within that first minute was silently
missed. Verified with a test: a target placed to hit only within that
excluded first minute went undetected as `STOP`/no-exit under the old code,
and correctly resolved as `TARGET` after switching to `>=`. The exact same
class of bug, at a coarser 15-min-bar scale, was found simultaneously in
`check_earlier_signals_status()` below and fixed the same way.

Note the exit scan includes the entry bar itself (not the bar *after* entry,
which is what the original Colab backtest did for simplicity) — meaning a
same-bar stop-out is possible if price moves sharply within the very minute
of entry. This is a deliberate divergence from the backtest: it's a more
realistic model of a real fill (you can be stopped out later within the same
bar you entered), at the cost of being *slightly* more conservative than the
backtest numbers quoted above.

**Known limitation:** `yfinance` retains only ~7 days of 1-min history. A
signal that hasn't been reconciled within that window (VPS downtime, a missed
cron run, etc.) can never be retroactively reconciled — it will simply remain
`reconciled: false` forever. The 4:30pm same-day cron slot is designed to
avoid this in normal operation; `--date` exists as a manual catch-up option
but only works within the 7-day retention window.

#### Live Status of Earlier Same-Day Signals — `check_earlier_signals_status()`
Powers the `Earlier signal(s) today` block in the Discord alert (see
[Live Alert Context Fields](#live-alert-context-fields-discord-message)
above). Reuses the 15-min bars already fetched for the current cron run —
no extra API call. For each of today's earlier signals, checks whether the
target or stop was touched anywhere in the bars since that signal's own
entry point:
```python
future = df[df.index >= entry_after]   # >= , not >  — same off-by-one class
                                        # of bug as reconcile() above, found
                                        # and fixed at the same time
hit_target = (future["high"] >= tp).any()
hit_stop   = (future["low"]  <= sl).any()
```
If both target and stop were touched somewhere in the (coarser, 15-min)
window, reports `LIKELY CLOSED` rather than guessing which came first —
the precise answer still comes from `--reconcile`'s 1-min replay. This
function is explicitly a same-day estimate for live decision-making context,
not a replacement for the authoritative reconcile outcome.

#### Heartbeat Logging — Matches `soxl_intraday_bot.py` Convention
```python
def write_heartbeat():
    with open(HEARTBEAT_PATH, "a") as _hb:
        _hb.write(f"[{now.strftime('%Y-%m-%d %H:%M:%S ET')}] tqqq_intraday_bot OK\n")
```
Written to the same shared `/root/logs/heartbeat.log` used by the other bots,
using the identical `bot_name OK` format — one file to `tail` for a health
check across the whole VPS. Called on every normal-mode run (including
early-return skips like "outside trading window" or "no data"), but
deliberately **not** called during `--reconcile` (matches `soxl_intraday_bot.py`'s
`not args.reconcile` gate) and **not** called if the run crashes — a stale
heartbeat during market hours is itself a secondary "something's wrong" signal,
on top of the direct Discord crash alert below.

#### Crash Handling — Catch, Log, Alert, Don't Double-Alert
The entire `main()` body (and the `--reconcile`/`--summary` CLI dispatch,
separately) is wrapped in try/except. On any unhandled exception:
1. Full traceback logged to `tqqq_intraday_bot.log` (`exc_info=True`)
2. Error message posted directly to Discord (not just logged) — surfaces
   the failure within seconds, rather than waiting for someone to notice a
   stale heartbeat or manually check logs
3. `sys.exit(1)` — deliberately does **not** re-raise the exception, since
   `main()`'s own except block already fully handles logging + alerting; an
   earlier draft re-raised after handling, which caused the *outer* CLI
   dispatcher's except block to catch it a second time and send a duplicate
   Discord alert for the same crash

#### Retry Logic — Yahoo Publication Lag (Ported from `soxl_intraday_bot.py`)
Added after review confirmed `soxl_intraday_bot.py` already had this and
`tqqq_intraday_bot.py` didn't — the two intraday bots now share the same
overall pattern, with one important timing adaptation described below.
```python
MAX_RETRIES  = 12   # 12 x 5 seconds = 60 second max wait
WAIT_SECONDS = 5

closed_minute = (now.minute // 15) * 15
# -1 minute: see "Off-By-One Fix" below — this is NOT the same formula
# used in soxl_intraday_bot.py, and porting it unchanged would have been
# a live bug.
expected_bar_time = now.replace(minute=closed_minute, second=0, microsecond=0) - timedelta(minutes=1)

for attempt in range(MAX_RETRIES):
    candidate = yf.download(SYMBOL, period="5d", interval="1m", ...)
    last_bar = candidate.index[-1]
    if last_bar >= expected_bar_time:
        raw = candidate
        break
    time.sleep(WAIT_SECONDS)
```

**Off-By-One Fix — Why the `-1 minute` Is Required.** `soxl_intraday_bot.py`
fetches native **15-min** bars directly from `yfinance`. `tqqq_intraday_bot.py`
fetches **1-min** bars and resamples them itself (required for true VWAP —
see above). This difference matters for the retry loop's timing target, and
porting SOXL's `expected_bar_time` formula unchanged (without the `-1 minute`)
was tried first and caught as a bug before shipping:

At `10:15:02`, `closed_minute` lands on `15`, giving `expected_bar_time =
10:15:00` with the unadjusted formula. For the loop to see `last_bar >=
10:15:00`, a **1-minute bar labeled `10:15`** would need to exist — but that
minute had only just started 2 seconds earlier. That bar cannot exist yet,
by definition, regardless of any Yahoo publication lag. The loop would wait
for something structurally impossible until the `10:15` minute itself closes
(~`10:16:00`+), burning through most or all of the 60-second retry budget on
every single run — not because of Yahoo being slow, but because the target
timestamp itself was one window ahead of what was actually needed.

The fix subtracts one minute, so the loop instead waits for the **last**
1-minute bar of the *already-closed* 15-min window (`10:14`, which closed at
`10:15:00`) — a bar that, in the realistic case, already exists by the time
the cron fires:
```
Before fix: waiting for 10:15 bar → doesn't exist until ~10:16:00+ → times out most runs
After fix:  waiting for 10:14 bar → already exists at 10:15:02      → succeeds on attempt 1
```
Verified via a mocked-clock test firing at exactly `10:15:02`: with the fix,
a realistic dataset (latest 1-min bar = `10:14`) resolves on the **first**
retry attempt instead of exhausting the full 60-second budget.

One adaptation from SOXL's version beyond the `-1 minute` fix: since
`tqqq_intraday_bot.py` fetches 1-min data, the freshness check naturally
compares against the latest **1-min** bar timestamp, not a 15-min bar label
directly. SOXL's native 15-min fetch doesn't need this adjustment — its
15-min bars are the thing being checked directly, so `soxl_intraday_bot.py`'s
original (unmodified) formula is correct for its own pipeline.

If all 12 retries are exhausted without fresh data arriving, the bot does
**not** give up — it makes one final `yfinance` call and proceeds with
whatever's available (fail-open), logging a timeout warning so the gap is
visible in the logs without blocking the entire signal-check cycle.

#### Holiday/Weekend Pre-Check
A lightweight `period="1d"` pre-check runs before the retry loop, checking
whether today's date actually has any fresh intraday data at all:
```python
_pre = yf.download(SYMBOL, period="1d", interval="1m", ...)
if _pre.empty:
    log.warning("No intraday data available — market likely closed (holiday or weekend).")
    return pd.DataFrame()
if _pre.index[-1].date() < now.date():
    log.warning("No today's data — market closed (holiday or early close).")
    return pd.DataFrame()
```
Without this, a holiday would send the bot into the full 60-second retry loop
every single cron cycle that day (up to ~24 times between 10:00am-3:45pm),
burning time and API calls waiting for bars that will never be published.
The pre-check exits in one call instead.

**Tested paths** (verified via mocked `yf.download` + a fixed `datetime.now()`
firing at exactly `10:15:02`, matching the exact scenario the off-by-one bug
above would have hit on every run): holiday short-circuit (1 call, no retry
loop entered), immediate success with the off-by-one fix applied (realistic
data resolves on the first attempt instead of exhausting the retry budget),
retry-then-succeed (genuinely stale data on early attempts, catches up
mid-loop, breaks early), and full-timeout-fallback (all 12 retries exhausted,
one final call, proceeds with whatever's available rather than failing the
entire run).

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

#### Why --rebase (Not --no-rebase)
`git pull --rebase` is the correct choice here. The script runs `git add` and
`git commit` BEFORE the pull, so the working tree is clean at pull time — no
unstaged changes exist. Rebase then lifts the local log commit, pulls any remote
code changes from GitHub, and stacks the log commit cleanly on top.

Using `--no-rebase` would create a merge commit every single day, cluttering the
repository history with hundreds of meaningless `Merge branch 'master' of
github.com/...` entries over months of operation.

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

#### Why the Cron Time Moved from 4:15pm to 4:40pm
Adding `tqqq_intraday_trade_log.json` to the `git add` line meant `push_logs.sh`
now needs to run *after* `tqqq_intraday_bot.py --reconcile` (4:30pm ET) finishes
writing that day's outcomes — otherwise the push would ship yesterday's
reconciled data, with today's results delayed until the following day's push.
10 minutes of buffer between reconcile (4:30pm) and push (4:40pm) was judged
sufficient; the reconcile itself only needs price data up to 3:30pm ET (the
strategy's hard exit time), so it isn't waiting on anything time-sensitive.

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
├── .gitattributes              ← *.json + *.jsonl merge=ours (protects VPS data on pull)
├── README.md                   ← this file
├── requirements.txt            ← ta>=0.11.0, yfinance, pandas, numpy, requests, pytz
├── push_logs.sh                ← daily JSON push to GitHub (4:40pm ET cron)
├── soxl_intraday_bot.py        ← SOXL intraday momentum bot v1.3
├── soxl_intraday_backtest.py   ← backtest script for SOXL intraday signals
├── soxl_bot.py                 ← SOXL RSI Ladder swing bot v1.1
├── tqqq_bot.py                 ← TQQQ mean-reversion swing bot
├── tqqq_intraday_bot.py        ← TQQQ momentum pullback intraday bot
│                                  (no standalone backtest .py — the strategy
│                                  was validated in a Google Colab notebook,
│                                  not committed to this repo)
├── .github/workflows/
│   ├── soxl_intraday_timer.yml ← workflow_dispatch ONLY (schedule disabled)
│   ├── soxl_swing_timer.yml    ← workflow_dispatch ONLY (schedule disabled)
│   └── tqqq_timer.yml          ← workflow_dispatch ONLY (schedule disabled)
│                                  (tqqq_intraday_bot.py has no corresponding
│                                  workflow file — VPS cron only, consistent
│                                  with "Why VPS over GitHub Actions" above)
├── soxl_intraday_trade_log.json
├── soxl_gate_blocks.json
├── soxl_swing_gate_blocks.json
├── soxl_trade_log.json
├── trade_log.json
├── tqqq_gate_blocks.json
├── tqqq_intraday_trade_log.json
├── soxl_earnings_cache.json
└── earnings_cache.json
```

---

*Last updated: July 2026 — re-validated EMA_FAST via 2D grid sweep + out-of-
-sample chronological split (2022-2024 vs 2024-2026 tested independently,
not just blended average), changed EMA_FAST 9→5 after the ranking held
consistently in both periods; fixed a bug where near-miss reconciliation
was silently skipped on any day with zero real signals (early-return in
reconcile() never reached the near-miss call); tightened near-miss
selection to require ALL failed conditions close AND capped at 2 max
(the original "any()" check let 4-5-condition failures through as false
positives, corrupting aggregate near-miss stats); found and fixed several
hardcoded "EMA9" labels in live Discord alerts/logs left stale by the
EMA_FAST change, renamed prev_ema9 trade-log field to prev_ema_fast.
Also: added structured rejection logging (which condition failed, how
close it was, normalized so higher %-of-required always means "closer to
passing" across every condition — fixed a direction bug in the pullback
condition's calculation), added automatic near-miss reconciliation
(hypothetical outcomes for close rejections, answering whether current
thresholds are correctly filtering or costing real trades), fixed a
gap-through-target/stop mispricing bug shared by real-trade and near-miss
reconciliation, extended `.gitattributes` pull-protection to cover
`.jsonl` files (previously only `.json` was protected). Also: corrected
TQQQ intraday volume baseline methodology (backtest now matches live:
time-of-day, prior-days-only, no lookahead bias), re-validated VOLUME_MULT
(1.0→1.2) and PULLBACK_DIST (0.50→0.75) against corrected methodology,
closed the joint-validation gap with a 36-combination 2D grid sweep
(confirms current live settings sit on a flat, non-fragile expectancy
surface — no change needed), added live alert context (VWAP extension,
time-until-cutoff, earlier-signal status), fixed off-by-one bugs in
reconcile() and check_earlier_signals_status(), widened yfinance fetch to
7d (Yahoo's true limit), fixed volume-baseline self-referencing bias*
*VPS: Servarica V3 KVM Slim Slice 2, 38.49.214.59, Montreal*
*Python: 3.10.12 | Ubuntu: 22.04 LTS*
