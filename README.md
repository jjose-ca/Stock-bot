# Stock-Bot Trading System

Discord-based system for manual trading. Signals and tools only — no
automated order placement, ever, anywhere in this system.

**Three passive signal bots**, running on cron, covering TQQQ (swing), TQQQ
(intraday pullback — **see status warning below**), and TQQQ (intraday
above-open — currently the recommended TQQQ intraday strategy). All
signals are Discord alerts — no automated order placement.

**Plus one on-demand tool**: `tqqq_buy_ladder_bot.py`, a Discord slash
command (not a passive alert bot — see its own dedicated section below)
that computes averaging-down buy levels for an existing TQQQ position on
request.

**Plus one daily producer bot**: `event_calendar_bot.py`, which writes a
shared `event_flags.json` file every morning (FOMC, CPI/PCE/NFP, monthly
OpEx/quad witching, top-QQQ-holding earnings) that any other bot —
cron-based or systemd — can read via `event_flags.py` without each one
re-implementing its own calendar-fetch logic. See its own dedicated
section below.

> 📦 **SOXL bots retired and archived separately.** `soxl_bot.py` (swing)
> and `soxl_intraday_bot.py` (intraday) have been removed from this
> document and from the live crontab — no longer running, no longer
> maintained. Full design/backtest details preserved in
> [`README_SOXL.md`](./README_SOXL.md) for reference, in case a similar
> leveraged-ETF strategy is ever revisited. This document now covers only
> what's actually active.

> ⚠️ **`tqqq_intraday_bot.py` (pullback strategy) status: validated edge
> retracted.** A critical backtest bug (lookahead bias) was found and fixed
> after this bot was already live. Under corrected, honest execution
> timing, the pullback strategy's real expectancy is ~$0.00/share —
> essentially a coin flip, not the $0.186/share originally reported and
> used to justify every parameter tuned into it. See
> [Why tqqq_intraday_bot.py's Validated Edge Was Retracted](#why-tqqq_intraday_botpys-validated-edge-was-retracted)
> for the full story. **`tqqq_above_open_bot.py` is the currently
> recommended TQQQ intraday strategy** — built and validated after this
> discovery, using the corrected methodology from the start.

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Bot Summaries](#bot-summaries)
3. [Signal Conditions](#signal-conditions)
4. [Validated Parameters](#validated-parameters)
5. [tqqq_buy_ladder_bot.py — TQQQ Averaging-Down Ladder](#tqqq_buy_ladder_botpy--tqqq-averaging-down-ladder-slash-commands--automatic-dip-alert)
6. [event_calendar_bot.py — Market Event Calendar](#event_calendar_botpy--market-event-calendar-producerconsumer-pattern)
7. [VPS Crontab Schedule](#vps-crontab-schedule)
8. [Log Files Reference](#log-files-reference)
9. [Monthly Review Guide](#monthly-review-guide)
10. [VPS Operations](#vps-operations)
11. [Architecture Decisions](#architecture-decisions)
12. [Emergency Procedures](#emergency-procedures)
13. [Key Implementation Details](#key-implementation-details)

---

## System Architecture

```
Your Laptop
  └── Edit code → push to GitHub

GitHub (<github-username>/Stock-bot)
  └── Source of truth for all code (.py, .sh, .yml)
  └── Archive for daily JSON log data (pushed from VPS at 4:15pm ET)
  └── GitHub Actions: workflow_dispatch ONLY (schedules disabled)

Servarica VPS (<VPS_IP>, hostname: stock-bot, Montreal)
  └── Primary execution environment — runs all four bots on cron
  └── Source of truth for all JSON log data
  └── Reconciles TQQQ intraday trade log daily at 4:30pm ET
  │     (gives yfinance extra time to finalize the final 1-min bars
  │     of the session before the replay-forward scan)
  └── Pushes logs to GitHub daily at 4:40pm ET
  │     (moved from 4:15pm to run after TQQQ intraday reconcile completes)
  └── Ubuntu 22.04 LTS, Python 3.10.12
  └── Repo at /root/Stock-bot/
  └── Logs at /root/logs/
```

### Key Principles

- **VPS is source of truth for data** — never overwrite VPS JSON files with git pull
- **GitHub is source of truth for code** — never push code from VPS to GitHub
- **All trading is manual** — bots send Discord alerts, execution happens manually on the trader's brokerage of choice
- **`.gitattributes`** contains `*.json merge=ours` and `*.jsonl merge=ours` — protects VPS JSON/JSONL data during git pull

### Primary Data Source

**Hybrid, as of this session — was `yfinance`-exclusively before.**
Confirmed by direct code inspection, not just convention:

- **All three cron signal bots** (`tqqq_bot.py`, `tqqq_intraday_bot.py`,
  `tqqq_above_open_bot.py`), `event_calendar_bot.py`, and **all Colab
  research scripts** still fetch exclusively through `yfinance` — unchanged.
- **`tqqq_buy_ladder_bot.py` alone is now hybrid**: its daily/structural
  fetch (`fetch_daily_bars` — feeds ATR%, RSI, regime, swing-lows,
  rolling-extremes) migrated to **Alpaca** (IEX feed). Its extended-hours
  price fetch, its holiday probe, and its volume-ratio calculation
  deliberately stayed on `yfinance` — three separate, reasoned
  exceptions, not an oversight. See each below.

**Why the ladder bot migrated and nothing else did**: the general
"`yfinance` is an undocumented scraper, no SLA" reliability argument —
**not** a rate-limit/call-volume argument. `fetch_daily_bars` is called
once per command invocation (`/buyfilled`, `/marketcheck`), not from any
tight polling loop — an earlier draft of this reasoning incorrectly
attributed the migration to the 5-min dip-alert loop; that loop calls a
different, lighter function (`fetch_live_price_extended_hours`) that was
NOT migrated. The actual motivating context was a separate, related bot
(a position-tracker, designed but not yet deployed elsewhere in this
suite) whose 5-min polling loop genuinely does have rate-limit exposure
on 1-min bars — the ladder bot's migration reuses that project's
validated `alpaca_data.py` wrapper and its measured justification, even
though the ladder bot's own call pattern didn't independently need it.

**Real, measured justification for trusting Alpaca's IEX-only daily bars
for ATR/RSI/regime/swing-lows** — a 5-day, 1-minute TQQQ diff test
(Alpaca IEX vs. `yfinance`), not an assumption:
- **Close price agreement: excellent.** Median 0.014% diff, 95th
  percentile 0.065%, max ~0.28% — effectively noise-level for anything
  keying off closes (ATR%, RSI, regime all do).
- **Volume ratio (Alpaca/yfinance) ≈ 2%**, matching IEX's known ~2-3%
  market share — confirms IEX volume is NOT a usable proxy for real
  market volume. Directly why volume-ratio needed its own carve-out
  (below), rather than assuming close-price accuracy implies volume
  accuracy too.
- **~11.5% of 1-min bars missing entirely** from Alpaca/IEX (no trade
  printed that minute) vs. present in `yfinance`. Low practical impact
  for *daily* bars specifically (whole-day aggregation has no comparable
  per-minute gap risk) — the missing-bar rate matters much more for the
  position-tracker's 1-min polling than for this bot's daily fetch.
- **Not separately measured**: daily high/low accuracy. The diff test
  covered 1-min bars only. `find_confirmed_swing_lows` reads the daily
  `low` column — an intraday extreme that printed on a venue other than
  IEX but never on IEX could in principle be missed. Real, open,
  honestly-carried-forward caveat, not swept under anything.

**Three deliberate exceptions, still on `yfinance`:**

1. **`fetch_live_price_extended_hours`** (pre/post-market price) — IEX's
   extended-hours liquidity is thinner than its already-small
   regular-session share; an Alpaca latest-trade call here is more
   likely to be stale enough to reject, defeating the point of fetching
   a "fresher" price. `yfinance` draws from a broader backend, more
   likely to have a usable print.
2. **`is_regular_market_hours_live`'s SPY holiday probe** — kept for
   consistency with the same pattern already used elsewhere in the wider
   bot suite (`tqqq_bot.py`); low-frequency, low-stakes, fails open
   regardless of source.
3. **`fetch_qqq_volume_series_yfinance`** (new function, feeds
   `/marketcheck`'s volume-ratio field only) — the ~2% IEX volume-share
   finding above means Alpaca's raw volume figure is not usable as-is.
   Whether the *ratio* (5d avg / 20d avg) would still track correctly
   despite the tiny raw numbers is an open, untested question — if IEX's
   market share stays roughly constant across both windows, the constant
   mathematically cancels out of the ratio (`(k×true_5d)/(k×true_20d) =
   true_5d/true_20d`), but this has NOT been validated the way the
   close-price accuracy was. Kept on `yfinance` as the safe default
   until/unless that specific ratio-survives-IEX-only theory gets its
   own diff test — don't let a known limitation quietly degrade
   something it was never measured against.

**Known, structural `yfinance` limitations** (still relevant — it's
still the source for 5 of 6 bots, and 3 of 4 data needs in the ladder
bot itself):
- **No coverage of the Blue Ocean ATS overnight session** (8pm-4am ET,
  Sun-Thu) — confirmed: not among the vendors known to carry that feed.
  Alpaca DOES offer it, per Alpaca's own docs, but as a paid,
  "contact sales," 15-min-delayed, indicative-only add-on — evaluated,
  not adopted (this overnight gap was NOT the reason for the daily-bars
  migration above; that was general reliability, unrelated to overnight
  coverage specifically).
- **Occasional intermittent gaps** — bars not yet published when
  queried, more common right around/after the close and in early
  pre-market.
- **Unofficial and scraping-based** — no uptime guarantee, no support
  contract; a Yahoo-side website change could break it with no warning.

**A real, confirmed documentation-drift lesson from deploying this
migration**: `alpaca-py` was added to code and (per report) installed
and working on the VPS before `requirements.txt` on GitHub was updated
to declare it — confirmed directly via a GitHub screenshot still showing
the old 5-line, `yfinance`-only file after the migration was reportedly
live. Same category of lesson as the `ladder_core.py` file-regression
incident earlier in this document: **"it's running" is not evidence the
committed files actually match** — only checking the file itself
confirms that. `requirements.txt` was corrected in the same session this
was caught.

### Code Update Workflow

```bash
# 1. Edit code on laptop, push to GitHub
# 2. On VPS (via Termius):
cd /root/Stock-bot && git pull origin master
# JSON files are never overwritten (protected by .gitattributes)
```

---

## Bot Summaries

| Bot | File | Ticker | Timeframe | Runs |
|-----|------|--------|-----------|------|
| TQQQ Swing | `tqqq_bot.py` | TQQQ | Daily bars | Every 15 min, 9:30am-4:00pm ET |
| TQQQ Intraday (Pullback) ⚠️ RETIRED | `tqqq_intraday_bot.py` | TQQQ | 15-min bars | Cron entries removed this session -- file/logs left in place, no longer scheduled |
| **TQQQ Intraday (Above-Open)** ✅ | `tqqq_above_open_bot.py` | TQQQ | 15-min bars | Every 15 min, 10:00am-3:30pm ET, staggered 2 min from pullback bot |
| **TQQQ Buy Ladder** | `tqqq_buy_ladder_bot.py` | TQQQ | Daily bars (QQQ) | **Both**: on-demand via `/buyfilled`, `/marketcheck`, `/position` (no schedule, computes only when invoked) **AND** a continuous background poll every 5 min, 6:30am-8:00pm ET, for the automatic dip-entry alert -- not purely on-demand anymore, see its own section for why the polling has to live in this same process |
| **Event Calendar** (producer) | `event_calendar_bot.py` | N/A — cross-cutting | N/A | Once daily, early morning (before all other bots' first run) |

### tqqq_bot.py — TQQQ Mean-Reversion Swing

Pure mean-reversion strategy. Buys TQQQ when deeply oversold, holds 5-10 days expecting a bounce back toward the mean. Uses a scoring system (up to 6 points across trend + momentum dimensions) with per-category floor requirements to prevent buying falling knives.

**Why it was silent for 2+ months:** TQQQ RSI was 69-79 (bull market). Path A requires RSI < 35, Path D requires RSI < 50. Both correctly blocked.

### tqqq_intraday_bot.py — TQQQ Momentum Pullback Intraday ⚠️ VALIDATED EDGE RETRACTED

> **Do not treat the numbers below as trustworthy.** They were the accepted
> validated result for months, but were later found to rest on a critical
> backtest bug (lookahead bias). Corrected, this strategy's real expectancy
> is approximately $0.00/share — see
> [Why tqqq_intraday_bot.py's Validated Edge Was Retracted](#why-tqqq_intraday_botpys-validated-edge-was-retracted)
> below for the full investigation. Description kept here for historical
> record and to document what the bot's code actually does; treat the
> "momentum continuation" thesis as unproven, not confirmed.

Pure momentum continuation strategy — the philosophical opposite of `tqqq_bot.py`'s
mean-reversion approach. Buys a brief pullback to VWAP or the fast EMA (5,
originally 9 — see [Validated Parameters](#why-ema-fast--5-not-9) below)
*within* an
already-established uptrend, on the assumption that the pullback gets bought and
momentum resumes. Every trade exits same day; no overnight risk. Multiple signals
can fire in one day — the bot no longer gates to one alert per day (removed
deliberately; judgment on which setup to take is left to the trader). Originally
validated via 4.5-year Databento backtest across 1,128 trading days — this
validation is the one later found to be compromised.

**Data source:** `yfinance` (not Databento) — chosen because Databento requires a
paid subscription for live intraday polling, while `yfinance`'s free tier is
sufficient once the strategy is built around 15-min bars (see
[Why 15-min Bars, Not 5-min](#why-15-min-bars-not-5-min) below).

Execution happens on a same-day basis — large capital deployed briefly per trade,
same-day flat.

### tqqq_above_open_bot.py — TQQQ Above-Own-Open Momentum Intraday ✅ CURRENT RECOMMENDED STRATEGY

Built after the pullback bot's validated edge was retracted (see above) —
starts from a deliberately much simpler hypothesis: **intraday momentum
persistence**. If TQQQ is trading above its own day's opening price at any
point after 10:00am ET, that day's early direction has a real, validated
tendency to persist through the rest of the session. No VWAP, no EMA, no
volume filter, no pullback pattern — a single boolean condition.

Fires on **every** qualifying bar, not gated to the first signal of the
day — but only the first signal each day was individually backtested and
validated; every alert is clearly labeled `[VALIDATED SIGNAL]` (signal #1)
or `[INFO ONLY — SIGNAL #N TODAY]` (signal #2+), with the info-only alerts
explicitly noting they were tested as a group and found meaningfully
weaker. See [Signal Conditions](#tqqq_above_open_botpy-signal-conditions)
below for the full validated numbers and everything tested and rejected
along the way (volume confirmation, EMA trend confirmation, ORB, both
hurt/underperformed).

Every trade exits same day; no overnight risk. Uses the exact same
corrected backtest methodology (honest 15-minute execution delay, matching
`reconcile()`'s real convention) that was built specifically in response to
finding the pullback bot's bug — this strategy was validated correctly
from the start, not retrofitted.

**Data source:** `yfinance`, same as the pullback bot, same 15-min bar
reasoning.

**Fully independent of `tqqq_intraday_bot.py`** — separate trade log
(`tqqq_above_open_trade_log.json`), separate bot log, separate heartbeat
entry, separate Discord webhook variable (falls back to the shared one if
not set). Deliberately built this way so debugging or modifying one bot
can never affect the other.

## Why tqqq_intraday_bot.py's Validated Edge Was Retracted

This is the single most important finding in this system's history and
should be read before trusting any historical number attributed to the
pullback bot.

### The Bug: Lookahead Execution Bias in the Backtest

The Colab backtest's `simulate_exits_vectorized()` function determined
trade entry using:
```python
after = np.searchsorted(ts_vals, sig_ts.to_datetime64(), side='right')
```
`sig_ts` is a 15-min candle's **left-label timestamp** (e.g. `10:00` for a
candle spanning 10:00–10:15). This searched for the next 1-min bar *after*
`10:00` — landing ~1 minute later, **still inside the same candle** that
generated the signal. But the signal's conditions (VWAP recovery, EMA
trend, volume, pullback) can only be confirmed using that candle's own
**close**, which isn't knowable until the candle fully closes at `10:15`.

**Net effect:** the backtest was entering trades ~14 minutes before the
signal was actually knowable — using information (that this candle was
about to qualify) that would not have been available at that point in
real time. A textbook lookahead bias, not a minor timing quirk.

**The live bot itself was never affected.** `reconcile()` — the function
that determines the real, recorded outcome of every actual live signal —
always used the correct convention (`entry_after = signal_ts + 15min`).
The bug lived only in the Colab backtest used to *select* parameters, not
in the deployed bot's own outcome tracking. This means the live trade
log's real, historical outcomes were always honest; it's the numbers used
to justify EMA5, `VOLUME_MULT=1.2`, `PULLBACK_DIST=0.75`, and every other
tuned parameter that were built on the biased simulation.

### What This Invalidated

Every sweep result produced before the fix used the biased function: EMA
fast/slow grids, the volume threshold sweep, the pullback distance sweep,
the pullback/recovery AND/OR mode sweep, the entry-start-time sweep, the
`N_PRIOR_DAYS` sweep, the ATR-based sweeps, and every out-of-sample
validation built on top of any of these. None of those specific numeric
conclusions should be trusted or referenced going forward, even though the
*methodology* (out-of-sample chronological splitting, exit-mix scrutiny)
used throughout remained sound — it's exactly what eventually caught this.

### The Corrected, Honest Result

Fix applied:
```python
execution_ts = sig_ts + pd.Timedelta(minutes=15)
after = np.searchsorted(ts_vals, execution_ts.to_datetime64(), side='left')
```
At the (now-retracted) "current live" configuration — EMA5/13,
`VOLUME_MULT=1.2`, `PULLBACK_DIST=0.75`, Target=$0.50, Stop=$0.40:

| | Win Rate | Expectancy | Exit Mix (TARGET/STOP/TIME) |
|---|---|---|---|
| Biased (wrong, previously reported) | 67.1% | $0.186/share | 56% / 31% / 14% |
| **Corrected (honest)** | **44.4%** | **$-0.0004/share** | **33% / 44% / 23%** |

44.4% is essentially exactly the mathematical breakeven win rate for a
1.25:1 R:R at $0.50/$0.40 — not a coincidence; that's what "no edge" looks
like at these specific parameters.

### The Disqualifying Finding: Random Entry Beat the Signal

A random-entry baseline (one random 15-min bar per trading day, zero
signal logic, same $0.50/$0.40 target/stop) was benchmarked directly
against the pullback bot's real signal logic:

| | Trades | Win Rate | Expectancy | Exit Mix |
|---|---|---|---|---|
| Random entry | 1,128 | 47.0% | $0.0162–0.0213 | 44–52% / 44–52% / 4–8% |
| Pullback bot's real signal | 642 | 44.4% | $-0.0004 | 33% / 44% / 23% |

**Random entry beat the engineered signal.** The VWAP + EMA trend + volume
+ pullback + recovery conditions were, under honest timing, selecting for
*quieter, less decisive* moments (23% TIME-drift vs. random's 4–8%) rather
than genuine high-conviction setups. The core hypothesis — that a specific
pullback-then-recovery chart shape predicts continuation — does not hold
up under honest execution timing on TQQQ 15-min bars.

### What Was Tried as a Fix, and Also Failed

Several follow-up concepts were tested under the corrected methodology
before the strategy was set aside in favor of building `tqqq_above_open_bot.py`
from scratch:
- **ATR-based pullback distance** — no meaningful edge over the fixed $0.75.
- **ATR-based target/stop** — actively degraded exit quality (44% TIME-exit
  rate on the best-looking combination — capturing general drift with a
  wide, forgiving exit, not real signal).
- **Mean-reversion-to-VWAP** ("buy the stretch, target VWAP reclaim") —
  structurally broken: by the time honest 15-min execution delay allows
  entry, price has typically already reached the VWAP-based target, since
  VWAP moves slowly and the reclaim itself is proof the bounce already
  happened. Not a calibration problem — the target definition doesn't
  survive the mandatory delay.
- **Opening-range pullback to 50 EMA** (a professional-sounding variant
  proposed externally) — structurally the same "wait for confirmation, buy
  the pullback" hypothesis already disproven; not expected to and did not
  outperform.

### Current Status

`tqqq_intraday_bot.py` remains deployed on the VPS but should be treated
as **unvalidated, not recommended for live trading** until/unless a
genuinely different signal concept is found and validated for it under
corrected methodology. `tqqq_above_open_bot.py` (below) is the direct
result of starting over with this lesson applied from the beginning.

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

### tqqq_above_open_bot.py Signal Conditions

A single condition, checked on the most recently **completed** 15-min bar:
```
1. close > day's own opening price   (the only condition — no VWAP, EMA,
                                       volume, or pullback pattern at all)
```
Trading window: 10:00am–3:30pm ET (validated as better than 10:30am start —
see below).

**Gated to exactly one signal per day** — fires only on the **first**
qualifying bar of the day; every later bar that same day is skipped
regardless of price. This matches the backtest exactly:
`find_above_open_signals()` has always used `.groupby('date').first()` to
build the validated result, so this is a restoration of validated
behavior, not a new restriction. (History: the live bot briefly fired on
*every* qualifying bar per an earlier explicit request — see
[Reverted: Every-Bar Alerts](#reverted-every-bar-alerts-restored-to-first-signal-only)
below — and separately, a noon cutoff was briefly added on top of the
first-signal gate, then explicitly removed once it was recognized as an
untested addition with no backtest behind it specifically.)

#### Exit Strategy
```
Target:      +$0.50 per share
Stop:        -$0.40 per share
Time stop:   3:30pm ET (exit before market close regardless of P&L)
R/R ratio:   1.25:1
```

#### Reverted: Every-Bar Alerts Restored to First-Signal-Only
Originally, every alert was labeled `[VALIDATED SIGNAL]` (signal #1) or
`[INFO ONLY — SIGNAL #N TODAY]` (signal #2+) and fired on every qualifying
bar, per an earlier explicit request. This was later reverted back to
first-signal-only, for a specific reason worth being precise about: taking
every qualifying bar (not just the first) was **separately backtested**
and found meaningfully weaker (roughly 12,500 trades, ~54.5% WR vs. the
first-signal-only version's real ~52.0% WR, with exit mix degraded toward
TIME) — but more importantly, first-signal-only **is** the actual
validated strategy; firing on every bar was always the live bot deviating
from what was backtested, not an equally-valid variant of it. The
`validated`/`signal_number` fields remain in the trade log schema for
historical continuity, but as of this gating change, `signal_number` will
always be `1` going forward — no `INFO ONLY` alerts will fire anymore.

---

## Validated Parameters

### TQQQ Swing — Validated Thresholds
- score≥7 + RSI≥50: -0.041% expectancy → blocked (consolidation_not_pullback)
- score≥8 + RSI>45: -1.80% expectancy → blocked (no_genuine_pullback)
- Path B: negative expectancy → disabled
- Scoring threshold: 6 points

### TQQQ Intraday — 4.5-Year Databento Backtest

**Backtest environment:** Google Colab notebook (not a repo `.py` script — the
strategy was iterated live via chat/Colab, not committed as a standalone
backtest file).

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

## tqqq_above_open_bot.py — Validated Parameters

**Backtest environment:** Same Colab notebook lineage as the pullback bot,
rebuilt with the corrected `simulate_exits_vectorized()` from the start —
this strategy was never exposed to the lookahead bias bug.

**Data:** Same Databento 1-min OHLCV, 2022-01-03 to 2026-07-02
(1,128 trading days).

### Final Validated Result (First Signal of Day Only)
```
Signal rate:    76.1% of trading days (858 signals)
Win rate:       52.0%  (446W / 412L)
Expectancy:     +$0.0380/share
Exit breakdown: STOP 36% / TIME 33% / TARGET 31%
Out-of-sample:  Period A (2022-01 to 2024-06): 478 trades, 53.6% WR, $0.0411 exp
                Period B (2024-07 to 2026-07): 380 trades, 50.0% WR, $0.0343 exp
                Gap: $0.0068 — reasonably tight, real edge holds in both
                independent periods
Per-year:       2022 +0.0436 | 2023 +0.0442 | 2024 +0.0441 | 2025 +0.0142 | 2026 +0.0494
                (every year positive; 2025 notably weaker than the rest —
                worth watching, not yet a pattern)
```
**These are the corrected numbers.** See the critical section immediately
below for why they differ substantially from what was originally reported
and traded on.

### CRITICAL: Split-Adjustment Bug Found and Corrected (Second Major Data Bug in This Project)

**Every number in this section prior to this correction — 858 trades,
65.4% WR, $0.1767/share expectancy — was computed on TQQQ price data that
was NOT split-adjusted.** This was discovered by accident, while
investigating an unrelated question (whether trade volume predicts signal
quality), when a backtest re-run using a supposedly-identical dataset
returned 52.0% WR instead of the expected 65.4%.

**Root cause:** TQQQ underwent two confirmed 2-for-1 stock splits within
this project's date range — 2022-01-13 and 2025-11-20. Databento's
standard `ohlcv-1m` schema pull is **raw/unadjusted by default**; split
adjustment requires a separate Reference API call
(`adjustment_factors.get_range()`) that this project's data pipeline never
used. Confirmed directly in the raw data: TQQQ's close went from $152.69
(2022-01-12) to $70.58 (2022-01-13) — an un-adjusted 2:1 split cliff, not
a real market move. **This same discontinuity was found in the ORIGINAL
1-minute CSV file used for every backtest in this entire project** — the
bug did not appear today; it has been present since the very first
above-open backtest.

**Why this matters mechanically:** target/stop are fixed dollar amounts
($0.50/$0.40). A fixed dollar move represents a wildly different
*proportional* move depending on price level — 0.3% of a $150 pre-split
price vs. 0.7% of a $70 post-split price. Every day before a given split
was being measured against a target/stop calibrated for a completely
different relative scale, silently distorting win rate and expectancy
across the whole dataset (not just near the split dates) without changing
signal *count* at all — `close > day_open` is a same-day, scale-invariant
comparison, unaffected by absolute price level, which is exactly why the
signal count (858) stayed identical while every outcome metric changed.

**Fix:**
```python
def apply_split_adjustments(df, ts_col='ts_et'):
    df = df.copy()
    splits = [
        (pd.Timestamp("2022-01-13").date(), 2),
        (pd.Timestamp("2025-11-20").date(), 2),
    ]
    for split_date, ratio in splits:
        mask = df[ts_col].dt.date < split_date
        for col in ['open', 'high', 'low', 'close']:
            df.loc[mask, col] = df.loc[mask, col] / ratio
        df.loc[mask, 'volume'] = df.loc[mask, 'volume'] * ratio
    return df
```
Applied immediately after `load_data()`, before anything else touches
price data. Verified against an independently-documented real price
(TQQQ closed $102.05 pre-split on 2025-11-17; corrected data shows
$51.005 = $102.05 ÷ 2, an exact match) — confirming the corrected dataset
is accurate, not just internally self-consistent.

**Is this a retraction, like the pullback bot's?** No — this is a
correction, not a retraction. Unlike the pullback bot (which lost to
random entry once its bug was fixed), the above-open strategy remains
genuinely, out-of-sample-consistently profitable after correction — every
single year 2022-2026 is positive, and the two independent test periods
both show real edge. **The magnitude was overstated by roughly 4.6x; the
underlying effect (intraday momentum persistence) is real, just
considerably more modest than believed.**

**Downstream impact — every test below this point in the document was run
against the OLD, uncorrected 65.4% baseline.** Their *directional*
findings (volume hurts, EMA hurts, ORB is weaker, the stop-streak breaker
helps) are likely still valid, since they were relative comparisons against
a baseline computed the same (flawed) way — but their specific *magnitudes*
have not been re-verified on corrected data and should not be trusted at
face value until re-run. This is noted individually below where most
relevant, but applies throughout.

### Why the Signal Was Found: Ablation Testing After the Pullback Bot's Failure

After the pullback bot's random-entry baseline was confirmed to beat its
own 5-condition signal, each of those 5 conditions was tested **in
isolation** against the same random baseline, to see if any single piece
had real, standalone value the combined strategy was drowning out:
```
vwap_only:      $0.2099 exp — suspiciously high, investigated further
ema_trend_only: $0.0566 exp — real, modest, standalone edge
volume_only:    $-0.0340 exp — negative, inconsistent between periods
pullback_only:  $0.0217 exp — roughly matches random baseline
recovery_only:  $0.2196 exp — also suspiciously high
```
`vwap_only` and `recovery_only`'s unusually strong results were
investigated rather than trusted outright (per this project's established
rule: unusually good results get scrutinized harder, not celebrated).
Diagnosis: 60% of `vwap_only`'s signals fired on the very first bar of the
trading window, and removing that cluster *increased* the edge further —
ruling out a simple artifact. A parallel, much simpler condition — "is
price above its own day's opening price" — produced nearly identical
results (77.6% WR / $0.2718 exp vs. `vwap_only`'s 75.5% WR / $0.2701 exp),
confirming the effect is **intraday momentum persistence**, a real,
independently-known market property, not anything specific to VWAP as an
indicator.

### Why 10:00am Start, Not 10:30am

> ⚠️ Numbers below computed on the pre-split-adjustment-fix baseline —
> see the split-adjustment correction above. Directional conclusion
> (10:00 beats 10:30) not yet re-verified on corrected data.

The original validated backtest (838 trades, 63.8% WR, $0.1625 exp)
accidentally excluded the bar labeled `10:00` — a diagnostic filter added
while investigating `vwap_only`'s inflated numbers (above) that was never
revisited before the final validation. This meant the live bot's original
config (`TRADE_START_M = 30`, so the earliest evaluated bar closes at
10:30am) matched what was actually tested — but a direct test of
**including** the 10:00-10:30 window (found via external review, then
verified) showed it *improves* results, not degrades them:
```
WITH 10:00 window (current, deployed): 858 trades, 65.4% WR, $0.1767 exp, gap $0.0012
WITHOUT (original):                     838 trades, 63.8% WR, $0.1625 exp, gap $0.0110
```
Live config updated to `TRADE_START_H=10, TRADE_START_M=0` on this
evidence — more trades, higher win rate, higher expectancy, and a
meaningfully tighter out-of-sample gap.

### Why No Volume Confirmation

> ⚠️ Numbers below computed on the pre-split-adjustment-fix baseline.
> Re-checked separately post-fix via a volume-bucket breakdown (see
> below) — directional conclusion (volume doesn't help) confirmed to
> still hold on corrected data specifically, not just inherited from the
> old baseline.

Directly tested as a filter on top of the validated above-open signal —
degraded results, same as it did for the pullback bot:
```
Above-open alone:        858 trades, 65.4% WR, $0.1767 exp
Above-open + volume≥1.2x: 646 trades, 59.8% WR, $0.1226 exp
```
Fewer trades, lower win rate, lower expectancy, TIME-exit rate roughly
doubled (7.1% → 15.5%).

#### Re-Checked Post-Correction: Volume-Level Breakdown

After the split-adjustment fix, the binary volume filter question was
re-examined more granularly — not just "does a ≥1.2x threshold help"
(already answered: no), but "does win rate vary meaningfully across the
full spectrum of volume levels behind each signal, at all":
```
Vol Bucket     Trades  WinRate    Exp      TIME%
<0.5x            78    57.7%   $0.0555   42.3%
0.5-0.75x       199    50.3%   $0.0234   31.2%
0.75-1.0x       226    52.7%   $0.0426   32.7%
1.0-1.25x       131    48.1%   $0.0019   30.5%
1.25-1.5x        91    59.3%   $0.0933   33.0%
>1.5x           132    48.5%   $0.0363   34.1%

Below 1x:   503 trades, 52.5% WR, $0.0370 exp
At/above 1x: 354 trades, 51.1% WR, $0.0382 exp
```
The practically-relevant split (below vs. at/above 1x) is essentially
flat — confirms no clean, actionable volume-based filter exists, on
corrected data specifically, not just inherited from the old baseline.
The two extreme buckets (`<0.5x` and `1.25-1.5x`) show better numbers than
the middle, a mild U-shape — but per-bucket samples here (78-232 trades)
are thin enough that this could be noise; not validated out-of-sample, not
actionable without further testing.

### Why No EMA Trend Confirmation

> ⚠️ Numbers below computed on the pre-split-adjustment-fix baseline; not
> yet re-checked post-fix (unlike volume, above). Directional conclusion
> plausible but unconfirmed on corrected data.

Also directly tested, despite `ema_trend_only` showing real standalone
edge in the ablation test above — the standalone result did not
transfer when layered on top of above-open:
```
Above-open alone:         858 trades, 65.4% WR, $0.1767 exp
+ EMA5>EMA13 confirmation: 810 trades, 45.6% WR, $0.0051 exp
+ EMA9>EMA13 confirmation: 785 trades, 45.2% WR, $0.0006 exp
```
Both EMA variants collapsed win rate to near-breakeven. Likely explanation:
redundancy, not synergy — on most days where price is already above its
own open, EMA_fast is also already above EMA_slow (both are different ways
of detecting the same underlying "is today trending up" fact). Requiring
both simultaneously mostly filters out the specific subset of days where
above-open is true but the EMA trend hasn't caught up yet — which, per the
exit mix, disproportionately excluded good trades rather than bad ones.
This is the second independent condition (after volume) confirmed to hurt
when layered onto above-open, suggesting the strategy's edge specifically
depends on staying simple rather than adding "extra confirmation."

### Why Every-Bar Alerts Were Reverted Back to First-Signal-Only

> ⚠️ Comparative numbers below from the pre-split-adjustment-fix baseline;
> the *decision* to revert stands regardless (see reasoning below), but
> the specific percentages are unverified on corrected data.

Every-bar alerting was implemented once (per an earlier explicit request),
then reverted. Taking every qualifying bar each day, not just the first,
roughly triples signal count but degrades win rate and expectancy
noticeably, with TIME-exit rate climbing substantially (documented at the
time as ~65% → ~55% WR territory, ~32% TIME-exit rate vs. ~7% for
first-only — magnitudes need re-verification post-split-fix, but the
degradation pattern itself is a structural one, not a coincidence of the
old data: taking every re-crossing includes progressively later, lower-
quality, more-whipsaw-prone signals). More fundamentally: first-signal-only
**is** what the backtest has always measured (`.groupby('date').first()`);
every-bar was always the live bot deviating from validated behavior, not
an equally-valid alternative reading of it. The live bot now fires exactly
once per day, matching the backtest precisely — see
[Reverted: Every-Bar Alerts](#reverted-every-bar-alerts-restored-to-first-signal-only)
in Signal Conditions above for the current, deployed behavior.

### ORB (Opening Range Breakout) — Tested, Weaker Than Above-Open

> ⚠️ Numbers below computed on the pre-split-adjustment-fix baseline; not
> yet re-checked post-fix.

Built and corrected (an earlier draft had the same lookahead bug plus a
separate risk/reward anchoring issue, both fixed before this result) as a
candidate second TQQQ intraday strategy:
```
15-min OR:  520 trades, 53.3% WR, $0.0634 exp, TIME 34.4%
30-min OR:  480 trades, 52.3% WR, $0.0254 exp, TIME 51.2%
45-min OR:  431 trades, 50.1% WR, $0.0079 exp, TIME 65.4%
60-min OR:  399 trades, 50.1% WR, $0.0098 exp, TIME 71.9%
```
All four window lengths positive but clearly weaker than above-open on
every metric — wider OR windows show the same TIME-exit degradation
pattern as ATR-based exits. Not pursued further; above-open remains the
stronger, simpler strategy.

### Stop-Streak Circuit Breaker — Validated, Not Yet Deployed

> ⚠️ Numbers below computed on the pre-split-adjustment-fix baseline. This
> is now a HIGHER priority to re-validate and deploy, not lower: the real
> edge is roughly 4.6x smaller than believed when this was tested, meaning
> there's much less margin to absorb clustered-loss days — the exact
> problem this breaker addresses. Re-run before deploying, don't assume
> the old ~4x-improvement ratio holds at the corrected baseline.

Motivated by a real live example (July 14, 2026 — 14 signals fired in one
choppy session, all 14 lost) — tested whether suppressing new signals
after N consecutive same-day STOP-outs improves results:
```
No filter:        13,066 trades, 48.8% WR, $0.0250 exp
Max 3 stops:        7,960 trades, 57.4% WR, $0.0928 exp
Max 2 stops:         6,076 trades, 60.2% WR, $0.1154 exp  <- recommended
Max 1 stop:          3,947 trades, 66.4% WR, $0.1680 exp  (best headline
                                          number, but widest OOS gap $0.0721
                                          — less trustworthy than Max 2)
```
Verified NOT lookahead-biased: an external review raised this concern, and
the filter's sequential, forward-only decision logic was tested head-to-head
against an independently-coded alternative implementation on hand-verifiable
synthetic sequences — both produced identical results in every case,
confirming the improvement is a real, causally valid effect of avoiding
*additional* trades on days that are already showing a genuine
consecutive-loss cluster, not a bug. `Max 2 consecutive stops` recommended
over `Max 1` for the same "prefer the more robust choice, not just the
best headline number" reasoning applied to `VOLUME_MULT` earlier in this
project — smaller out-of-sample gap, healthier trade count, still a >4x
improvement over no filter. **Not yet implemented in the live bot** —
open item, see [Known Gaps](#known-gaps-not-yet-done) below.

### Considered and Rejected: Opening-Range Pullback to 50 EMA

An externally-proposed "better approach" (avoid premarket entries, wait
for the opening range to form, buy a pullback to the 50 EMA with
confirmation, stop below the opening range low) was recognized as
structurally the same "wait for confirmation, buy the pullback" hypothesis
already disproven by the pullback bot's retraction (above) — not expected
to outperform above-open's simpler signal. A backtest cell was built to
verify directly rather than dismiss on pattern-matching alone; result not
yet run/recorded as of this writing.

### Considered and Removed: Noon Signal Cutoff

A noon cutoff (skip the entire day if the first qualifying signal occurs
after 12:00pm) was briefly added alongside the first-signal-only reversion
above, then explicitly removed once the distinction between the two
changes was clarified. **First-signal-only** restores validated backtest
behavior (`.groupby('date').first()` has always meant exactly this). **The
noon cutoff was a genuinely new, untested restriction** — the validated
858-trade result includes first-of-day signals at any time, morning or
afternoon, with no time-of-day restriction. Rather than leave an unproven
addition in place under the assumption it was probably safe, it was
stripped back out once this was recognized. A time-of-day backtest (does
the first signal's own performance vary by what hour it fires) was scoped
during this same investigation but not run to completion before the
split-adjustment bug was discovered and took priority — remains a
legitimate open question, see [Known Gaps](#known-gaps-not-yet-done)
below. If a real time-of-day effect is ever confirmed, it should be added
back deliberately with backtest evidence, not reintroduced by default.

### Not Yet Tested: Below-Open (Short) Mirror Strategy on SQQQ

The above-open effect (momentum persistence) has no obvious reason to be
upside-only — a symmetric "is SQQQ below its own day's open" signal could
capture the same effect on down days, using SQQQ (a long position, no
margin/short-selling mechanics needed) rather than shorting TQQQ directly.
Requires SQQQ's own 1-min Databento data (same schema, `ohlcv-1m`, same
date range as the TQQQ dataset) — not yet pulled/tested as of this
writing. Worth noting TQQQ's general upward long-term bias and SQQQ's
correspondingly worse long-term decay characteristics may mean the two
don't mirror as cleanly as the naming suggests; largely irrelevant for a
same-day-only strategy but worth keeping in mind.

---

## tqqq_buy_ladder_bot.py — TQQQ Averaging-Down Ladder (Slash Commands + Automatic Dip Alert)

**Structurally different from every other bot in this system** — everything
above is a passive Discord-alert bot running on cron, firing signals the
trader reacts to. This is the opposite: an on-demand Discord **slash
command** (`/buyfilled`) the trader invokes deliberately, any time they
want a fresh read of where to add to an existing TQQQ position. No
autonomous signal generation, no cron schedule, nothing fires unless the
trader explicitly asks.

**What it does:** given a TQQQ position (share count + average cost
basis), returns the next 3 recommended buy-down prices, spaced using QQQ
volatility (not TQQQ's own — see below), with any relevant QQQ swing-low
support shown alongside for context. The goal: avoid "random" averaging-down
buys by giving each add a volatility-aware, non-arbitrary price.

### Files

```
tqqq_ladder_bot/
├── ladder_core.py           ← all calculation logic, zero Discord
│                               dependency, zero network calls of its own.
│                               Imported by BOTH the live bot and the
│                               backtest, so whatever the backtest
│                               validates is provably the exact code that
│                               runs live — no drift between the two.
│                               Unaffected by the Alpaca migration below --
│                               it never fetches data itself, only computes
│                               on whatever DataFrame it's handed.
├── tqqq_buy_ladder_bot.py   ← Discord bot wiring: slash commands, buttons,
│                               modals, the dip-alert polling task, embed
│                               formatting, logging, AND now the data-fetch
│                               layer (fetch_daily_bars, now Alpaca-backed;
│                               fetch_live_price_extended_hours, still
│                               yfinance). Contains no ladder math of its
│                               own -- that's all ladder_core.py.
├── alpaca_data.py            ← NEW this session. Thin wrapper around
│                               alpaca-py, ported from a separate,
│                               previously-designed (not yet deployed)
│                               position-tracker bot elsewhere in this
│                               suite -- see Primary Data Source above for
│                               the full migration story and the measured
│                               diff-test numbers behind it.
├── backtest_ladder.py       ← walk-forward backtest (Colab-run, not
│                               deployed on the VPS). Not committed with
│                               secrets; pulls QQQ/TQQQ history live via
│                               yfinance, no local data file needed.
│                               Includes the entry-threshold sweep (§5) --
│                               see its own section below. Still 100%
│                               yfinance -- NOT migrated to Alpaca (Colab
│                               scripts were explicitly out of scope for
│                               that migration).
├── intraday_pullback_analysis.py  ← Colab-run only. One month of real
│                               5-min bars, characterizes typical dip
│                               depth/recovery time. See its own section.
├── intraday_entry_backtest.py     ← Colab-run only. Precise, path-aware
│                               simulation of the live alert logic against
│                               real 5-min bars. See its own section.
├── requirements.txt          ← pinned deps. Includes alpaca-py>=0.30.0
│                               as of this session -- see the
│                               documentation-drift lesson above: this
│                               file was caught lagging behind the actual
│                               VPS environment by one push, corrected in
│                               the same session. yfinance>=0.2.48
│                               specifically to guarantee the
│                               multi_level_index kwarg is supported. No
│                               new package needed for discord.ext.tasks --
│                               ships with discord.py.
├── ladder_log.jsonl          ← TRACKED in git, pushed to GitHub (not
│                               gitignored — see its own section below for
│                               why this changed). Append-only log of every
│                               /buyfilled call and the ladder it produced.
├── position_state.json        ← NOT YET added to .gitignore -- should be
│                               (real-time position data, not a historical
│                               record like ladder_log.jsonl), but this
│                               was never actually done in the session that
│                               built it. Flagged here rather than silently
│                               assumed; add `position_state.json` to
│                               .gitignore before it's ever committed by
│                               accident. {"shares", "price", "updated_at"}.
│                               See Position Tracking below.
├── .gitignore                 ← currently .env, __pycache__/, venv/, *.pyc
│                               only (position_state.json still missing --
│                               see note above). Was missing entirely for a
│                               period during development — see the
│                               corrupted-file incident below.
└── .env                      ← gitignored. DISCORD_BOT_TOKEN,
                                 DISCORD_GUILD_ID, DISCORD_ALERT_CHANNEL_ID,
                                 DIP_ALERT_THRESHOLD_PCT (optional),
                                 ALPACA_API_KEY, ALPACA_SECRET_KEY (both
                                 required as of this session -- the bot
                                 raises immediately at import time via
                                 alpaca_data.get_client() if either is
                                 missing, rather than failing confusingly
                                 on the first command).
```

### Why `ladder_core.py` Is Separate From the Bot File

Same reasoning as keeping backtests and live logic in sync elsewhere in
this system, made structurally enforced rather than just a convention:
`backtest_ladder.py` and `tqqq_buy_ladder_bot.py` both `import` from
`ladder_core.py` — neither reimplements any ladder math. A parameter
change (e.g. `ATR_STEP`) made in one place is what both the backtest and
the live bot use; there is no way for the deployed bot to silently drift
from what was actually validated.

### The Core Design Principle: Anchor to Basis, Never to Live Price

This was the single most important design decision, arrived at after
working through a concrete failure mode (referred to throughout
development as **"moving goalposts"**):

If a computed buy target is re-derived from *today's live price* every
time the trader checks it, the target retreats every time price
approaches it — because "gap size" is being re-measured from a moving
starting point instead of a fixed one. A target defined this way can
perpetually stay just out of reach, since the anchor itself is chasing the
market.

**The fix:** the ladder's anchor is **always** the trader's actual average
cost basis (the `price` argument to `/buyfilled`), and it changes **only**
when a real fill happens — never automatically, never from a live price
lookup. `current_tqqq_price` (today's actual market price) is used **only**
to filter out stale levels (see next section) — it never feeds into where
a level is calculated *from*.

### Filter-and-Extend: Handling Levels the Market Already Passed

Because execution is fully manual here too (same principle as the rest of
this system — no resting/auto-filled orders), a level computed from the
basis can already be behind the market by the time the trader checks —
price may have fallen through it between checks, unnoticed. `build_ladder()`
handles this directly: after computing a level from the basis, it's kept
only if it's still strictly below `current_tqqq_price`; if not, the search
extends to the next ATR multiple and tries again, repeating until
`REQUIRED_LEVELS` (3) genuinely-still-ahead-of-the-market levels are found,
or `MAX_ATR_MULTIPLE` (15) is reached as a safety cap.

This means the *distance being measured* (the ATR gap) is always
basis-anchored and fixed, while *which of those computed levels are worth
showing* is always re-evaluated against live price — the two roles never
get conflated.

### Why Structure Is Computed on QQQ, Never on TQQQ's Own Price History

TQQQ is a 3x daily-reset leveraged ETF — its multi-day return is **not**
simply 3x QQQ's return, due to volatility decay (a.k.a. beta slippage):
daily resets compound asymmetrically, so choppy/sideways stretches erode
value even when the underlying index goes nowhere. Concretely: a 10% QQQ
drop followed by an 11.1% rebound (net flat) leaves a 3x product down
roughly 7%, purely from the compounding mechanics of the daily reset —
confirmed against real market commentary during development, not just
theory.

**Practical consequence:** a swing low measured on TQQQ's *own* price
history is not a clean read of "real" market structure — it's contaminated
by however much decay accumulated between when the low happened and now,
which depends on realized volatility over the whole intervening period, not
just the two price points. Two TQQQ swing lows from different time windows
aren't really comparable the way two QQQ swing lows are.

**The fix:** all structural calculation — ATR% and swing lows — is done
entirely on **QQQ**, the undecayed underlying. A QQQ-based signal is
converted to a **percentage** move, scaled by `LEVERAGE_FACTOR` (3.0), and
applied to a **TQQQ dollar reference price**. Which TQQQ reference price
is used differs by purpose (see next two sections) — this is the one place
in the design where getting the anchor wrong is a real, previously-shipped
bug (see [Key Implementation Details](#tqqq_buy_ladder_botpy-1) below).

### Formula — Buy Ladder (anchored to basis)

```
For each search step, starting at mult = ATR_STEP, incrementing by ATR_STEP,
up to MAX_ATR_MULTIPLE:

  raw_qqq_drop_pct = mult * qqq_atr_pct
  target_qqq_price = qqq_close * (1 - raw_qqq_drop_pct)

  tqqq_drop_pct = raw_qqq_drop_pct * LEVERAGE_FACTOR
  target_price  = round(basis_price * (1 - tqqq_drop_pct), 2)

  KEEP this level only if: 0 < target_price < current_tqqq_price
                            AND target_price not already produced (dedup)

Stop once REQUIRED_LEVELS (3) kept levels are found, or mult exceeds
MAX_ATR_MULTIPLE.
```

`qqq_atr_pct` = 14-day ATR (Wilder smoothing via `.ewm(alpha=1/14,
adjust=False)`), expressed as a percentage of QQQ's latest close — kept as
a percentage (not a raw dollar figure) specifically so it's portable
across price regimes and TQQQ's own split history, rather than tied to a
dollar scale that changes with every split.

### Formula — Support Display (anchored to LIVE price, deliberately different anchor)

```
For each confirmed QQQ swing low below qqq_close:

  qqq_drop_pct  = (qqq_close - swing_low_price) / qqq_close
  tqqq_drop_pct = qqq_drop_pct * LEVERAGE_FACTOR
  translated_price = round(current_tqqq_price * (1 - tqqq_drop_pct), 2)

  KEEP if: low_bound (deepest ladder level) <= translated_price <= high_bound (current price)
```

**This anchors to `current_tqqq_price`, not `basis_price` — intentionally
different from the buy ladder above.** The buy ladder issues an actionable
order commitment, which must not drift with the market (see "moving
goalposts" above). The support display instead answers a factual question
— "where does real QQQ structure sit relative to *today's* price" — which
must be measured from today's price specifically, because that's the only
reference point that already reflects all TQQQ decay realized up to now.
Anchoring this calculation to basis instead was a real bug found and fixed
during development — see
[Key Implementation Details](#tqqq_buy_ladder_botpy-1) below.

Support levels found in range are further cross-referenced against the
ladder itself via `locate_support_relative_to_ladder()`: each support price
is matched to its single nearest reference point (current price, or a
specific ladder rung), with the exact dollar/percent gap — no arbitrary
"is this close enough to count" threshold. The Discord embed groups a
support line directly under the specific `Buy N` field it's nearest to,
rather than as a disconnected list, so a genuine confluence (a QQQ swing
low landing near a pure-ATR-computed level) is immediately visible as
supporting evidence for that specific target.

### Confluence vs. Frequency — A Backtested Design Decision, Not a Guess

An earlier version of `build_ladder()` also **snapped** a computed ATR
level directly to a nearby confirmed QQQ swing low (within
`SWING_SNAP_TOLERANCE_ATR` × ATR%) rather than just displaying it
separately. This was removed after backtesting, and the removal is a
deliberate, evidence-based decision — not just a simplification:

**Raw result:** confluence-snapped levels filled *less* often than plain
ATR levels — `-12.1%` fill-rate gap in-sample, `-6.5%` in the out-of-sample
holdout (consistent direction both periods).

**But this was confounded by depth**, not necessarily a real "confluence
hurts" effect: snapped levels sit ~15-20% deeper on average than
non-snapped ones (deeper targets are mechanically harder to hit regardless
of *why* they're deep). Depth-controlled testing (comparing within matched
`drop_pct` buckets, and separately restricting to `level_num == 3` where
the two groups' depths are naturally closest):
- **In-sample:** the negative effect survived depth control — every
  depth bucket still showed confluence underperforming (-3.3% to -10.6%).
- **Holdout:** mostly reversed — several buckets went slightly *positive*,
  and the cleanest (level-3-only) comparison flipped to **+1.4%**.

**Conclusion:** an in-sample pattern that was consistent and then didn't
replicate out-of-sample is the textbook sign of something specific to that
period's regime, not a persistent property of confluence. The honest
read: **this backtest does not give a reliable answer either direction**
once depth is properly controlled for — the earlier raw numbers were
meaningfully inflated by the depth confound.

**Decision made anyway, on different grounds:** given the data was
genuinely inconclusive, the choice came down to a design philosophy
question — optimize the ladder for *frequency of fill* (more, closer
entries) or for *quality of entry* (fewer, larger adds only at levels the
market has actually defended before)? For a leveraged instrument
specifically, frequency-optimized ladders concentrate their busiest buying
into exactly the choppy, directionless conditions that hurt TQQQ's decay
the most — so **quality-of-entry reasoning would normally favor keeping
confluence**. The system instead shipped with **frequency-of-fill**
(`USE_CONFLUENCE = False` in the bot; swing lows are computed for the
*support display* only, never fed into `build_ladder()`), on the basis
that a manually-executed ladder benefits more from having genuinely
reachable near-term levels than from waiting on rarer, deeper confirmed
levels. This trade-off is explicit and revisitable — not a settled
conclusion — and the swing-low detection code remains fully in
`ladder_core.py`, unused by the live ladder, specifically so this can be
revisited with a larger dataset or a formal significance test without
rebuilding anything.

A parallel test of `SWING_FRACTAL_WINGS` (3 bars of confirmation on each
side, the default, vs. 2 — the "Williams Fractal" convention) found
**negligible difference** in either fill rate (~0.5pp) or the confluence
effect (~0.1-2.4pp) — ruling out "confirmation lag" as a meaningful driver
of the confluence result, contrary to an initial hypothesis.

### Why `ATR_STEP = 0.5`, Not the Original `1.0`

A spacing sweep (`ATR_STEP` ∈ {1.5, 1.0, 0.75, 0.5, 0.25}, confluence off,
20-trading-day fill window) showed a smooth, **monotonic** fill-rate vs.
spacing tradeoff — tighter spacing always fills more often, with no
optimum to find, since closer targets mechanically get touched more.

| step | Level 1 fill rate (holdout) | Level 1 avg drop |
|---|---|---|
| 1.0 | 68.4% | ~4.7% |
| 0.5 | 81.9% | ~2.4% |
| 0.25 | 88.6% | ~1.3% |

`0.25` was rejected: a ~1.3% average drop is inside TQQQ's normal daily
noise band, not a real pullback — the high fill rate there reflects
noise-triggering, not a better strategy. `0.5` was chosen as the
deliberate middle ground: a meaningful step up in fill frequency over the
original `1.0` default, without collapsing into sub-2% triggers.

**Validated against live conditions, not just the 10-year backtest
average:** QQQ's ATR(14) as of the change (per independent sources,
Aug 2026) was running ~1.5% — consistent with the ~1.5-1.8% implied by
the backtest's own 10-year average, meaning current market conditions were
not unusually calm or wild relative to what was actually tested. `0.5` was
not adopted from a stale historical average.

### Current Parameters (`ladder_core.py`)

```python
ATR_PERIOD = 14                  # QQQ ATR lookback, Wilder-smoothed. Swept
                                  # against 5/7/21 (see below) -- kept at 14,
                                  # a deliberate decision not to change, not
                                  # an unexamined default.
SWING_LOOKBACK_DAYS = 90         # QQQ swing-low search window
SWING_FRACTAL_WINGS = 3          # bars each side to confirm a swing low
SWING_SNAP_TOLERANCE_ATR = 0.5   # unused by the live ladder (confluence off);
                                  # reused by /marketcheck's "confirms vs
                                  # nearest to" wording (see below)
REQUIRED_LEVELS = 3              # target ladder size
LEVERAGE_FACTOR = 3.0            # TQQQ vs QQQ, approximate (see caveat below)
ATR_STEP = 0.5                   # search increment — see spacing sweep above
MAX_ATR_MULTIPLE = 15.0          # safety cap on the filter-and-extend search
SMA_REGIME_PERIOD = 200          # QQQ 200-day SMA, used by the regime badge
                                  # and /marketcheck's trend-distance display
RSI_PERIOD = 14                  # matches tqqq_bot.py's live Path A signal
                                  # period exactly (see /marketcheck below)
RSI_TREND_LOOKBACK = 1           # trading days back for rising/falling
                                  # comparison, i.e. yesterday. Cosmetic
                                  # display only (no price/level depends on
                                  # this) — a reasoned default, not a
                                  # backtested one, unlike ATR_STEP.
```

### Why `ATR_PERIOD = 14` Was Tested and Deliberately Kept, Not Changed

Unlike `ATR_STEP` and `SWING_FRACTAL_WINGS`, this parameter was inherited
from Wilder's original convention rather than validated for this specific
use case — worth testing the same way, rather than assuming. Swept against
5, 7, and 21 (confluence off, `ATR_STEP` held at 0.5):

- **Fill rate and Level-1 depth**: essentially flat across all four periods
  (well under 1 percentage point of spread) — this parameter doesn't move
  fill rate the way `ATR_STEP` did, and there's no sign of a short period
  dragging spacing into the noise zone either.
- **Spacing stability** (mean day-to-day % change in the ATR% series):
  monotonic and real — 5-day is ~3.3x noisier than 21-day, ~2.5x noisier
  than the 14-day default. A shorter period means the ladder's rungs
  visibly shift between consecutive `/buyfilled` checks even on days with
  no real news.
- **2022 drawdown reactivity** (ATR% just before the selloff vs. its peak
  60 trading days later): shorter periods do react faster and further —
  5-day widened +2.37 percentage points vs. 21-day's +1.55pp — but this is
  one historical episode (n=1 crash), suggestive not statistically robust,
  same caveat as every other deep-drawdown-regime claim in this system.

**Decision: kept at 14.** Nothing in the data made a compelling case to
move off it — the one place a shorter period helps (crash reactivity) is a
weak signal from a single event, while the day-to-day jumpiness cost is
real and constant. This is a case where the sweep earns its keep by
confirming a default rather than by finding something to change.

### Bot-File Config (`.env`, `tqqq_buy_ladder_bot.py`) — Not `ladder_core.py`

Added for the position-tracking and dip-alert features (all below). Kept
separate from the `ladder_core.py` block above deliberately — these are
Discord/deployment config, not calculation parameters, matching the
existing split between the two files.

```
DISCORD_ALERT_CHANNEL_ID    # required for the dip-alert polling task to
                             # post anywhere -- see its own section below
                             # for why a background task can't just reuse
                             # an interaction's channel the way commands do
DIP_ALERT_THRESHOLD_PCT     # optional, .env override, default 0.5 -- matches
                             # the value actually tested in
                             # intraday_entry_backtest.py, not an arbitrary
                             # pick
```

### Bugs Found and Fixed During Development

Worth documenting explicitly — these are real defects that shipped and
were later caught, not design decisions:

- **Support-anchor bug (the most significant one).** `translate_qqq_price_to_tqqq()`
  originally anchored the QQQ→TQQQ translation to `basis_price` — correct
  for the *ladder* (an actionable buy target that must not chase the
  market) but wrong for the *support display* (a factual "where does
  structure sit relative to today" statement, which must anchor to LIVE
  price to correctly absorb realized decay). In a drawdown scenario (e.g.
  basis $100, live price $50), this bug could silently drop or badly
  misplace genuinely nearby support, because the filter's own bounds
  (`low_bound`/`high_bound`, live-price-based) were being compared against
  a basis-anchored translation — two different coordinate systems.
  **Fixed**: `find_support_in_range()` and `translate_qqq_price_to_tqqq()`
  now take `tqqq_reference_price` explicitly, and the live bot always
  passes the current live TQQQ price, never basis.
- **Dead `USE_CONFLUENCE` flag.** The bot declared this constant but never
  actually read it — the empty-Series-vs-real-swing-lows choice was
  hardcoded inline instead, meaning the flag and the actual behavior could
  silently diverge if either was edited without the other. **Fixed**: the
  flag now genuinely controls whether `qqq_swing_lows_all` or an empty
  Series is passed to `build_ladder()`, mirroring exactly how the backtest's
  `simulate()` makes the same decision.
- **Duplicate logging.** `log_ladder()` was being called twice per
  `/buyfilled` invocation — once before the embed was built, again after —
  writing two near-identical lines to `ladder_log.jsonl` per real call.
  **Fixed**: single call, placed after all the data (including support) it
  needs to log already exists.
- **Relative log path.** `Path("ladder_log.jsonl")` resolved against
  whatever the process's current working directory happened to be at
  runtime — worked only because the systemd unit's `WorkingDirectory`
  happened to be set correctly, fragile against any future manual
  invocation from elsewhere. **Fixed**: `Path(__file__).parent / "ladder_log.jsonl"`.
- **Slash-command re-sync on every reconnect.** `on_ready` fires on every
  Discord gateway reconnect, not just the initial login — calling
  `tree.sync()` there risked hitting Discord's rate limits over a
  long-running process for no benefit (the command set doesn't change
  between reconnects). **Fixed**: sync moved into a custom `Client`
  subclass's `setup_hook()`, called exactly once before the first
  connection; `on_ready` now only logs.
- **No input validation.** Zero or negative `shares`/`price` silently
  produced "No levels found" instead of a clear error. **Fixed**: explicit
  check, clear rejection message, before any computation runs.
- **`"confirms this level"` had no distance threshold.** `locate_support_relative_to_ladder()`
  is pure nearest-point matching — a swing low several dollars from a rung
  still read as "confirms" it. **Fixed**: introduced `CONFIRMS_TOLERANCE_PCT`
  (reusing the existing, previously-unused `SWING_SNAP_TOLERANCE_ATR`
  constant, scaled by live ATR% and leverage) — genuinely close matches say
  "confirms," farther ones say "nearest to."
- **Swallowed support on an empty ladder.** The entire support-rendering
  block was nested inside the `else:` of `if not ladder:` — if
  `build_ladder()` returned empty (e.g. price crashed far below basis,
  exhausting `MAX_ATR_MULTIPLE`), already-computed support was silently
  never shown, in exactly the scenario (a real crash) where it would matter
  most. **Fixed**: support computation and the "unattached" rendering now
  happen unconditionally, outside the ladder-empty branch.
- **`low_bound` cutoff excluded support just past the deepest rung.**
  The support search range was capped exactly at the deepest ladder level
  — a real QQQ support translating to a TQQQ price one dollar past Level 3
  would be invisible even though it's clearly relevant. **Fixed**: search
  range now extends one additional ATR step below the deepest rung
  (`extra_buffer_pct = ATR_STEP * qqq_atr_pct * LEVERAGE_FACTOR`) — an
  ATR-scaled buffer, not an arbitrary flat percentage, consistent with
  every other distance in this module.
- **Corrupted `ladder_core.py` on GitHub (operational incident, not a code
  bug).** At one point, GitHub's copy of `ladder_core.py` was accidentally
  overwritten with the *entire contents* of `tqqq_buy_ladder_bot.py` —
  almost certainly a copy-paste mixup during a manual "edit this file" step
  on GitHub's web UI. Symptom: `ImportError: cannot import name
  'compute_atr_pct' from partially initialized module 'ladder_core' (most
  likely due to a circular import)` — `ladder_core.py` was trying to import
  from itself, because it *was* the bot file. `git checkout` did not fix
  this, since the bad content was already committed and pushed; the fix
  required manually replacing GitHub's file content with the correct
  source. **Lesson**: when editing files via GitHub's web UI across two
  similarly-purposed files in the same PR/session, double-check which file
  is actually open before pasting.
- **Naming/labeling issues, all now fixed**: `LadderLevel.basis` (a string
  label like `"QQQ ATR x1"`) renamed to `label` — was colliding
  conceptually with `basis_price` (a float, the trader's cost basis).
  **Note this is a breaking change to `ladder_log.jsonl`'s schema** — old
  log lines keep the key `"basis"`, new ones use `"label"`; nothing rewrites
  history. Separately, `compute_atr_pct` was refactored to derive from
  `compute_atr_pct_series` instead of duplicating the TR/EWM formula
  (single source of truth). The Discord field "QQQ close" was renamed to
  "QQQ now" (and the misleading unqualified "QQQ -X%" ladder label was
  replaced with the true live-distance figure, "QQQ needs ~X% more drop
  from today") — both were technically inaccurate: `qqq_close`/`QQQ close`
  implied a settled end-of-day print, when during market hours it's
  actually QQQ's live, still-updating quote (the daily bar's last row is
  incomplete until 4pm ET) — see the ATR intraday-partial-bar caveat below
  for the same underlying mechanic.

### Caveat: `LEVERAGE_FACTOR = 3.0` Is an Approximation, Not Exact

TQQQ targets 3x QQQ's *daily* return, not 3x its cumulative return over
however many days a level takes to fill. For a single day, ×3 is exact.
For a multi-day move, the true relationship is path-dependent — a smooth,
one-directional move tracks close to ×3, while a choppy path to the same
net move realizes *more* decline in TQQQ than the naive ×3 predicts, due
to volatility drag compounding on top of the directional move.

**Measured, not just theorized:** backtested drift between actual TQQQ
20-day returns and the naive `3 × QQQ return` prediction — mean drift
**-0.79%**, std dev **1.84%**, mean absolute drift **1.29%** over 20-day
windows. Judged small enough to be a reasonable planning estimate at this
horizon; would need revisiting if the ladder's typical fill horizon grew
substantially beyond ~20 trading days.

### Backtest — Methodology (`backtest_ladder.py`)

Not deployed on the VPS — run manually in Google Colab (network access
required for `yfinance`; the project's own sandboxed dev environment
cannot reach Yahoo Finance directly, unlike Colab).

```
Data:            yfinance, QQQ + TQQQ daily bars, auto_adjust=True
                 (handles TQQQ's split history automatically — unlike the
                 Databento-based bots elsewhere in this system, no manual
                 apply_split_adjustments() equivalent needed here, since
                 this backtest never touches the Databento pipeline)
LOOKAHEAD_DAYS:  20 trading days (how far forward a fill is searched for)
TRAIN_TEST_SPLIT: 0.7 (chronological — first 70% in-sample, rest holdout)
N_RANDOM_TRIALS: 200 (random-baseline sample size)
```

**No-lookahead design, verified by inspection, not just intent:**
- ATR at day *i* uses an EWM (exponential moving average) over true range
  — mathematically causal (recursively depends only on data through day
  *i*), computed once over the full series purely for efficiency; slicing
  it at *i* is identical to recomputing fresh using only data through *i*.
- Swing lows at day *i* use `find_confirmed_swing_lows_asof()`, which
  explicitly truncates the dataframe to rows ≤ *i* before searching, and
  the fractal-confirmation requirement (bars needed on *both* sides) means
  the most recent `wings` days can never be confirmed as a swing low —
  correctly reflecting that a low can't be confirmed until enough time has
  passed.
- Future price data (`tqqq["low"].iloc[i+1 : i+1+LOOKAHEAD_DAYS]`) is used
  **only** to score the outcome of a level already computed from day-*i*
  information — never to compute the level itself.

**One caveat, not a bug:** `auto_adjust=True` downloads and back-adjusts
the *entire* price history in one call, meaning older prices are adjusted
using knowledge of splits that happened after those dates chronologically.
This is standard adjusted-close practice, not predictive lookahead — and
because every quantity the ladder actually computes on (ATR%, swing-low
%, drop_pct) is relative/percentage-based, a split-adjustment (which
scales the whole series proportionally) doesn't distort any of the
backtest's actual conclusions.

**What this backtest does NOT cover** (see
[Known Gaps](#known-gaps--not-yet-done) below): full sequential-fill
portfolio P&L / blended-cost-basis simulation, transaction costs or
slippage, or the reality of manual (non-auto-filled) execution timing.

### Bugs Found and Fixed — Session 2 Additions

- **NaN silently propagating through every calculation.** `fetch_daily_bars()`
  only checked `df.empty` — a non-empty dataframe with `NaN` in the
  *values* of its last row sailed straight through. Real, observed
  incident: a `/buyfilled` call at 8:33pm ET showed `$nan` for every
  price field and "No levels found," traced to `yfinance` returning a
  row for that day with the regular session's bar not yet fully
  published on Yahoo's backend (a known, community-reported
  intermittent issue, more common near/after the close). Root cause:
  pandas/Python comparisons against `NaN` always evaluate `False`, so
  `build_ladder()`'s filter silently rejected every level instead of
  erroring where the problem actually started. **Fixed**: explicit
  `df["close"].iloc[-1:].isna().any()` check raises a clear message
  ("...may still be publishing today's data, try again in a few
  minutes") right at the fetch, instead of a confusing downstream
  symptom.
- **`ladder_core.py` regressed on disk without the running process
  noticing — a second, distinct file-integrity incident** (see the
  GitHub-corruption one above for the first). `compute_rolling_extremes`
  was confirmed present in the deployed bot's *running* behavior
  (`/marketcheck`'s "30-Day Range" field worked), yet `grep -c` against
  the actual file on the VPS returned `0`. Explanation: Python only
  reads a module's source once, at process startup — the file on disk
  had been overwritten (reverted to an older version) *after* the
  process was already running with the correct code loaded in memory.
  This is a ticking-time-bomb class of bug: works fine right up until
  any restart (deliberate, crash, VPS reboot), which would then
  immediately hit `ImportError` and crash-loop. **Lesson generalized**:
  "it's currently working" is not evidence a deployed file is correct —
  only checking the *on-disk* file directly (not just observed live
  behavior) can confirm that. Fixed by restoring the correct content and
  verifying via the same VPS-side `grep -c` check before declaring it
  resolved, not by trusting the app's current behavior.
- **`fetch_live_price_extended_hours` used `interval="1d"` with
  `prepost=True`** — genuinely unclear whether `yfinance` honors
  `prepost` for daily-interval requests at all (could not find
  documentation confirming either way). **Changed to `interval="5m"`**
  regardless of whether the original was actually broken: every source
  checked agrees `prepost` unambiguously works with true intraday
  intervals, so this removes the uncertainty rather than resolve it.
  `fetch_daily_bars` (the main historical/structural fetch) was never
  affected — this only touched the narrower freshness-check function.
- **Entry-threshold backtest methodology, not a ladder-bot code bug**:
  the first version of `intraday_entry_backtest.py`'s dip-cycle detector
  never reset its reference "running high" between trading days —
  a multi-day decline that didn't reclaim its peak until day 3 counted
  as ONE giant cycle, blending several real intraday moves into one
  data point. Result: implausibly few cycles (10 in 30 days) and
  implausibly long "recovery" times (median ~195 min, actually
  measuring multi-day swings). **Fixed** by resetting the peak at each
  new trading day's first bar. Separately, `backtest_ladder.py`'s daily-bar
  entry-threshold test originally checked `(high - close)/high` against
  the trigger threshold — silently missing every day that dipped past
  the threshold and then recovered before the close (which, per the
  intraday analysis, is most of them). **Fixed** to `(high - low)/high`,
  which correctly captures "the real-time alert would have fired at some
  point today" regardless of what happened afterward.

### Log File — `ladder_log.jsonl`

**Tracked in git and pushed to GitHub** — not gitignored/VPS-only as it was
originally set up. The original approach conflated "secret" with "data":
`.env` (a real credential) and `ladder_log.jsonl` (just data, no
credentials in it) were both excluded, which meant no off-VPS backup and no
easy way to pull the log down for analysis. `tqqq_ladder_bot/.gitignore`
now only excludes `.env`, `__pycache__/`, `venv/`, `*.pyc`. This matches
the rest of the system's convention (trade logs pushed daily via
`push_logs.sh`) — though `ladder_log.jsonl` isn't yet added to that script's
automatic push list, so pushing it today is still a manual
`git add`/`commit`/`push` rather than happening on the existing daily
schedule.

One JSON line per `/buyfilled` call (current schema — see the
`basis`→`label` rename note above for why older lines may have a different
key for the same field):
```json
{
  "ts": "2026-09-04T20:26:00+00:00",
  "shares": 20,
  "tqqq_basis_price": 74.50,
  "tqqq_current_price": 71.17,
  "qqq_close": 713.44,
  "qqq_atr_pct": 1.59,
  "market_data_last_date": "2026-09-04",
  "atr_step": 0.5,
  "leverage_factor": 3.0,
  "regime_below_200sma": false,
  "regime_sma_200": 656.78,
  "regime_pct_below": 0.0,
  "ladder": [
    {"price": 70.95, "qqq_drop_pct": 1.6, "tqqq_drop_pct": 4.8, "label": "QQQ ATR x0.5",
     "swing_low_date": null, "tqqq_pct_from_current": 1.2, "qqq_pct_from_current": 0.4},
    {"price": 69.18, "qqq_drop_pct": 2.4, "tqqq_drop_pct": 7.1, "label": "QQQ ATR x1",
     "swing_low_date": null, "tqqq_pct_from_current": 3.5, "qqq_pct_from_current": 1.2},
    {"price": 67.41, "qqq_drop_pct": 3.2, "tqqq_drop_pct": 9.5, "label": "QQQ ATR x1.5",
     "swing_low_date": null, "tqqq_pct_from_current": 6.0, "qqq_pct_from_current": 2.0}
  ],
  "support_displayed": [
    {"tqqq_price": 68.05, "qqq_price": 704.66, "qqq_drop_pct": 1.5, "swing_low_date": "2026-09-01"}
  ]
}
```
`swing_low_date` inside `ladder` entries is always `null` under the current
frequency-of-fill configuration (confluence disabled) — retained in the
schema in case the confluence decision above is ever revisited.
`atr_step`/`leverage_factor` are read directly from `ladder_core.py` at log
time, never hardcoded in the logging call, so historical entries stay
accurate even after either constant is later tuned.
`tqqq_pct_from_current`/`qqq_pct_from_current` are the corrected
live-distance figures (see the "QQQ close" label-fix note above) —
`qqq_drop_pct`/`tqqq_drop_pct` remain the original basis-anchored
structural distances, both kept side by side rather than one replacing the
other. `support_displayed` records whatever the support feature showed at
that moment, independent of and without influencing the `ladder` itself.

### VPS Deployment — Systemd, Not Cron (unlike every other bot here)

Every other bot in this system runs as a scheduled cron job that starts,
does one check, and exits. This bot instead must stay **continuously
connected** to Discord's gateway to receive slash-command interactions —
a cron job that starts and stops repeatedly is the wrong execution model
entirely. It runs as a **systemd service** instead:

```ini
# /etc/systemd/system/tqqq-ladder-bot.service
[Unit]
Description=TQQQ Ladder Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/Stock-bot/tqqq_ladder_bot
ExecStart=/root/Stock-bot/tqqq_ladder_bot/venv/bin/python tqqq_buy_ladder_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now tqqq-ladder-bot
journalctl -u tqqq-ladder-bot -f   # tail live logs
```

**Dedicated virtual environment**, isolated from every other bot's
dependencies — this system's other bots appear to install packages
system-wide with no venv, so this is a deliberate departure, chosen to
prevent a future `pip install` for this bot from silently changing a
package version any other bot depends on. Required
`apt install python3.10-venv` on first setup (Debian/Ubuntu splits the
`venv` module into a separate system package from the base `python3`
interpreter).

`ExecStart` points directly at the venv's own `python` binary by full
path — systemd does not need (and does not use) `source venv/bin/activate`;
that shell convenience is irrelevant to how systemd invokes the process.

### Discord Setup

- Separate Discord Application (Developer Portal), with a Bot user added.
- OAuth2 scopes required for the invite URL: `bot` + `applications.commands`.
- Bot permissions: Send Messages, Use Slash Commands (minimum).
- `DISCORD_BOT_TOKEN` — required, in `.env`, never committed (`.env` is
  gitignored; treated as a credential, equivalent to a password).
- `DISCORD_GUILD_ID` — optional. If set, slash-command registration is
  guild-scoped and appears near-instantly; without it, global command sync
  can take up to an hour to propagate.
- Runs in the same server as the other bots' webhook alerts (no technical
  conflict — webhooks and slash-command interactions are entirely separate
  Discord mechanisms) — deployed to a dedicated text channel within that
  server to keep `/buyfilled` output separate from the other bots' passive
  alert stream.

### `target_avg` — Shares-Needed Calculator (`/buyfilled` extension)

Optional parameter, off by default. Answers a different question than the
ladder itself: not "where's the next price" but "how many shares, bought
at a specific rung, would bring my average down to a number I choose."

**Formula** (pure algebra, no market data needed): if you hold `N1` shares
at basis `P1`, and buy `N2` more at price `P2`, the new blended average is
`(N1×P1 + N2×P2) / (N1+N2)`. Solved for `N2` given a target average `T`:

```
N2 = N1 × (P1 − T) / (T − P2)
```

**Two validity constraints, both mathematically necessary**: `T` must be
below `P1` (buying more can only lower your average, never raise it — the
command rejects a target ≥ current basis outright, before any computation
runs), and `T` must be above the specific rung's own price `P2` (buying at
a price can pull your average *toward* that price but never *past* it — a
target at or below a given rung's price is flagged "not reachable at this
level" rather than silently computed as impossible or infinite).

**Sanity guard on the result itself**, added after a review caught it: as
`T` approaches either boundary, the denominator `(T − P2)` shrinks toward
zero and `N2` blows up toward mathematically-correct-but-absurd numbers
(e.g. needing several times your current position). If the computed share
count exceeds 10x current position size, the response says plainly that
the target isn't realistic at that level instead of presenting a giant,
technically-correct-but-useless number as if it were a real plan.

**Explicitly does not tell you what target to pick, or how to size across
all three rungs as a coherent plan** — that's the sizing/exposure-cap
question, still open (see Known Gaps below). This is a calculator, not a
strategy: you name a number, it tells you the shares required to get
there, at whichever specific rung you're looking at.

### `/marketcheck` — Pre-Trade Context, No Position Required

A second slash command, added to answer a different question than
`/buyfilled`: "is now a reasonable moment to place a *first* order," rather
than "where do I add to a position I already hold." Takes no arguments —
`shares`/`price` aren't relevant before a position exists.

**Deliberately does not produce a composite "safe to buy: yes/no"
verdict.** Considered directly and rejected: a blended
recommendation would be a new, unvalidated signal invented on the spot,
duplicating (worse) the actual validated entry logic that already exists
in `tqqq_bot.py`. Instead, `/marketcheck` surfaces individual real facts
and lets the trader weigh them — same philosophy as the ladder's support
display and regime badge.

**What it shows, and why each piece was chosen:**

- **Price** — QQQ and TQQQ, same line, correctly labeled "now" (not
  "close" — see the label-fix note above).
- **QQQ RSI(2), QQQ RSI(14), TQQQ RSI(14)** — shown together rather than
  picking one, because there's a real, unresolved tension between two
  live reference points in this system: `tqqq_bot.py`'s actual live Path A
  signal checks RSI(14) on **TQQQ's own price** directly (confirmed by
  reading the deployed code — `if rsi < 35:`, checked first, before any
  regime gate), while `tqqq_swing_bot_v2.py` (confirmed **not** currently
  live) uses RSI(2) on **QQQ**. Rather than silently pick one convention,
  both show, clearly labeled, so the trader can see whether they agree or
  diverge. QQQ RSI(2) was added second, explicitly for a "trade at least
  once a day" use case — matching the only existing reference for that
  specific period/asset combination.
  - **Trend arrow** (↑/↓/→) compares today's value to `RSI_TREND_LOOKBACK`
    days back (currently 1, i.e. yesterday) — cosmetic only, no backtest
    behind the lookback choice, unlike the price-affecting parameters
    elsewhere in this bot.
  - **Badges** (🔴 overbought / 🟢 oversold) use **period-correct
    thresholds, not one blanket number for both periods**: RSI(2) at
    ≤10/≥90 (matching `tqqq_swing_bot_v2.py`'s actual convention — RSI(2)
    swings far more than RSI(14) by design), RSI(14) at ≤30/≥70 (the
    standard Wilder convention). An earlier draft of this feature proposed
    a single 80/20 threshold for both, which would have incorrectly
    flagged ordinary RSI(2) readings as "extreme" — caught and corrected
    before shipping.
- **QQQ volume (5d vs 20d average)** — a plain ratio, no "Elevated"/"Drying
  up" style interpretive label. Chosen over a considered
  ATR-based "volatility spike" indicator specifically because it measures
  something ATR doesn't: *participation* behind a move, not price range —
  genuinely new information rather than a restatement of what the ATR%
  field already implies.
- **QQQ 14-day ATR%** — same figure the ladder itself uses, for context.
- **Nearest QQQ support, translated to TQQQ** — reuses
  `find_confirmed_swing_lows()` and `translate_qqq_price_to_tqqq()`
  as-is, anchored to **live** TQQQ price (there's no basis yet, so this is
  necessarily the live-price-anchored case). Purely informational — no
  RSI-divergence or volume-fade gating decides *whether* to show it, and
  no proposed order accompanies it, both deliberately rejected (see below).
  Support lines lead with the TQQQ-equivalent price in bold, since that's
  the number actually needed for a decision, with QQQ source details
  following as context.
- **Signed distance to 200-SMA** — a separate calculation
  (`compute_signed_trend_distance()`) from the ladder's own regime badge
  field (`RegimeStatus.pct_below`), which is deliberately one-directional
  (0.0 when not below, to keep the ladder's asymmetric display simple).
  `/marketcheck`'s whole point is full situational awareness, so this one
  shows positive (above trend) or negative (below trend) either way.
- **Regime warning** — reuses `compute_regime_status()` verbatim, same
  asymmetric display as the ladder (shown only when below the 200-SMA).

**UI iteration**: originally every metric got its own Discord embed field,
which Discord stacks vertically even for fields marked `inline=True` on
mobile — a genuine wall of text requiring heavy scrolling. Consolidated
into 4 grouped fields (Price / Momentum & Volatility / Support / Trend)
using newlines within each field's value instead of one field per metric.

**Explicitly considered and rejected, with reasons:**
- **VWAP.** True VWAP is fundamentally an intraday concept (resets each
  session, computed from intraday price×volume) — this bot has
  deliberately stayed on daily bars throughout (same line already held
  against 15-minute structural-exit and intraday vol-snapshot proposals).
  A "daily-bar anchored VWAP" is a real but different, non-standard
  technique; not built, to avoid shipping something mislabeled.
- **ADX / a generic "momentum & strength" composite score.** No validated
  threshold exists for this bot's specific instrument/timeframe;
  building one would mean inventing a new, unbacktested indicator.
- **RSI-divergence + volume-fade gated support recommendation, with a
  proposed order to place.** A more elaborate version of the support
  feature was proposed: detect "fading strength" via RSI divergence and
  declining volume, then find the nearest support and output an explicit
  "place your buy limit at $X." Rejected on two grounds: (1) it invents a
  new, unbacktested entry-timing strategy wearing already-built functions
  as a costume — bearish divergence detection specifically is a real,
  fussy pattern-recognition problem, not the "quick metric" it was framed
  as; (2) the final "Action: place order at $X" output directly
  contradicts the facts-only, no-verdict design this command was built
  around from the start.
- **A hard gate on the 200-SMA regime** (refuse to show anything if
  below trend, matching `tqqq_swing_bot_v2.py`'s use of the same filter as
  an entry gate). Rejected because `/marketcheck` and the swing bot are
  answering different questions — gating a *new entry* decision makes
  sense (skipping a bad-regime entry costs nothing); this bot is helping
  manage exposure the trader may already have, where going silent removes
  the tool's usefulness without reducing actual market exposure.

### Extended-Hours Price Freshness

Both `/marketcheck` and `/buyfilled` originally showed a frozen
regular-session close whenever checked outside 9:30am-4:00pm ET —
`fetch_daily_bars()` never requests `prepost=True`, so the last row locks
at the official close and silently ignores real after-hours/pre-market
movement. Confirmed directly against two real screenshots taken 34
minutes apart (5:14pm and 5:48pm ET) showing byte-identical prices.

**Fix, deliberately scoped rather than a blanket `prepost=True` everywhere:**

- **`is_regular_market_hours_live()`** — combines a cheap 9:30-4:00 ET
  clock check with a live 1-minute SPY probe (mirroring `tqqq_bot.py`'s
  `is_market_open_today()` — no hardcoded holiday calendar, infers "is
  today a trading day" from whether the data exists) to also catch
  holidays. Clock check runs first, so nights/weekends short-circuit
  before ever touching the network. Fails OPEN on probe error, matching
  the precedent bot's exact reasoning.
- **`fetch_live_price_extended_hours(symbol)`** — a separate, narrow fetch
  using `interval="5m", prepost=True` (see the bug-fix note above for
  why `5m`, not `1d`), used ONLY to refresh the "current price" value
  outside regular hours.
- Wired into both commands: when outside regular hours, this replaces
  `qqq_now`/`tqqq_now` (or `qqq_close`/`current_tqqq_price` in
  `/buyfilled`) **before** `build_ladder()` runs — not just the embed
  display. A display-only fix would leave the filter-and-extend logic
  silently comparing against the stale regular-session close underneath
  a freshly-labeled number.
- **Structural calculations (ATR/RSI/regime/swing-lows/rolling-extremes)
  are deliberately untouched** — they keep using `fetch_daily_bars`'s
  regular-session-only data unconditionally, regardless of when checked.
  This was the explicit alternative to a global `prepost=True`: the
  scoped version fixes exactly the stale-price problem with zero change
  to any backtested indicator's behavior; a global change would also let
  extended-hours moves bleed into ATR/RSI/swing-low detection on every
  fetch, not just the "where is price right now" question.
- **Fails soft, not hard**: if the extended-hours fetch itself errors,
  the exception is caught and logged, silently falling back to the
  regular-session close already in hand — never crashes the command over
  a freshness upgrade that isn't strictly required to answer the user.

**An interesting, non-obvious asymmetry this surfaced**: after the close
but before midnight, *price* is worse (frozen, missing real movement)
while *ATR* is actually *better* than during the trading day — the
formula has zero time-of-day branching, so today's contribution to ATR
goes from a partial/understated range (mid-session) to the real, complete
regular-session range (post-close) for the exact same reason
(`prepost=False` on the structural fetch). Both facts stem from the same
design choice, pulling in opposite directions on trustworthiness.

**Known limitation, explicitly out of scope**: `yfinance`/Yahoo's
standard feed does not cover the newer Blue Ocean ATS overnight session
(8pm-4am ET, Sun-Thu) — confirmed by checking which data vendors actually
carry that feed (Bloomberg, dxFeed, ICE, QUODD, Databento; Alpaca also
confirmed to offer it, but as a **paid, "contact sales," indicative-only,
15-min-delayed** add-on, not part of its free tier). The dip-alert
polling task (below) does not attempt to cover this window — see its own
window discussion.

### Position Tracking — `position_state.json`

Solves a real, structural gap: the bot has no way to observe a sale
happening — by design, execution is entirely manual with no brokerage
connection anywhere in this system. `ladder_log.jsonl`'s last entry is
NOT a reliable "am I flat" signal — it only ever records buys, so after a
real sale it would keep showing the old shares/basis indefinitely, with
nothing to ever correct it.

```
position_state.json = {"shares": float, "price": float, "updated_at": iso}
```

- **`write_position_state(shares, price)`** — called automatically on
  every successful `/buyfilled` call (and by the fill-confirmation flow
  below). Always overwrites rather than merges — `/buyfilled`'s
  shares/price already represent the CURRENT total position, so this is
  correct without any special-casing.
- **`read_position_state()`** — returns `None` if no file exists (i.e.
  flat). Used as the gate for the dip-alert polling task.
- **`clear_position_state()`** — deletes the file. Called ONLY by the
  explicit "Clear Position" button below — **the bot never clears this
  on its own** (e.g. on a price target), since it has no way to verify a
  sale actually happened. Auto-clearing on a price condition was
  explicitly proposed and rejected: price hitting a target doesn't mean
  the trader actually sold, and silently wrong tracked state is worse
  than no tracking.

**`/position` command** — shows the currently tracked shares/basis/
last-updated (or "No position currently tracked" if flat), with a
**persistent** "Clear Position" button attached
(`discord.ui.View(timeout=None)` + a fixed `custom_id`, registered once
via `client.add_view()` in `setup_hook`). Persistence matters here
specifically: a default view times out after a few minutes, which would
silently stop working the first time it was actually needed, potentially
days after the `/position` message was sent.

### Fill Confirmation — Buttons, Modals, and the Shared Response Function

**`build_buyfilled_response(shares, price, target_avg=None)`** — the
entire core of `/buyfilled` (fetch, extended-hours freshness, ladder
computation, regime, support, embed construction, logging, position-state
write) extracted into one reusable async function. `/buyfilled` itself
now just calls it and reports errors — single source of truth, same
principle already applied to `compute_atr_pct`/`compute_atr_pct_series`
and to `/marketcheck`/`/buyfilled` sharing `ladder_core.py`'s functions.

**`compute_blended_average(existing_shares, existing_price, fill_shares,
fill_price)`** — the same weighted-average formula the `target_avg`
calculator already used. Correctly reduces to just `fill_price` when
`existing_shares=0` (a fresh entry from flat).

**Two modals, not one** — a real gap caught before building: knowing the
fill *price* alone isn't enough to update the average; the *share count*
is also needed, and nothing in the original alert design captured it.

- **`FilledSharesModal`** — one field (shares only) — price is already
  known from the alert's own target.
- **`CustomFillModal`** — two fields (shares + actual price) — for when
  the real fill differed from what was alerted.
- Both funnel into **`handle_fill_confirmation()`**, which blends against
  whatever `read_position_state()` currently holds (0/None if genuinely
  flat) and calls `build_buyfilled_response()` — never a second,
  divergent implementation of the ladder math.

**`DipAlertResponseView`** — three buttons, attached to each dip alert
(below): **Filled at Target** (opens `FilledSharesModal`), **Custom
Fill** (opens `CustomFillModal`), **Skip Dip** (a genuine no-op —
confirmed by inspection: does not touch `position_state.json` at all,
just edits the message to acknowledge). **NOT a persistent view** (unlike
`ClearPositionView`) — `target_price` is specific to one alert instance;
a fixed `custom_id` can't route back to per-alert context the way it can
for Clear Position's single, global action.

### Automatic Dip-Entry Alert — `check_dip_alert`

A `discord.ext.tasks.loop(minutes=5)` background task inside
`tqqq_buy_ladder_bot.py` itself — **not** a separate cron script, and not
a repurposed `tqqq_intraday_bot.py`/`tqqq_above_open_bot.py`/`tqqq_bot.py`,
despite those being genuinely considered. The reason is a hard technical
constraint, not a preference: `DipAlertResponseView`'s buttons and modals
require the bot's own live Discord Gateway connection to function at all
— a plain outbound webhook (what all four cron bots use) can only post
static content, it cannot receive or route a button click back to
anything. Since `tqqq_buy_ladder_bot.py` is the only script in this
system running a persistent 24/7 connection, the task has to live here.

**Started once**, guarded against double-starting across reconnects:
```python
@client.event
async def on_ready():
    if not check_dip_alert.is_running():
        check_dip_alert.start()
```

**Gate order**: `is_dip_alert_window()` → flat position
(`read_position_state() is None`) → fetch price (`fetch_dip_alert_price()`,
source split documented below) → track running high → check threshold
(`DIP_ALERT_THRESHOLD_PCT`, default 0.5%).

**`is_dip_alert_window()` — 6:30am-8:00pm ET — is a SEPARATE function from
`is_regular_market_hours_live()`, not a widened version of it.** Widening
the existing function was considered and rejected: `/marketcheck` and
`/buyfilled` use `is_regular_market_hours_live()` specifically to decide
whether to bother with the extended-hours fetch. If that function
considered 6:30am-8pm "regular hours," those commands would wrongly skip
the fresher prepost fetch during exactly the window they need it most.
Two different questions need two different answers. Also deliberately a
**pure clock check, no SPY holiday probe** (unlike its sibling) — same
low-stakes reasoning: worst case on an actual holiday is a few wasted,
harmless polls (price stays frozen, `dip_pct` stays ~0, nothing fires),
not worth a second network-probing mechanism.

**8:00pm end, corrected from an earlier 4:00pm version** — found and
fixed via a direct file comparison against a version built outside this
specific conversation thread (same kind of parallel-edit situation as the
Alpaca migration). The reasoning holds up: `fetch_live_price_extended_hours`
already fetches with `prepost=True`, which covers standard after-hours
trading up to ~8pm — a 4pm cutoff meant the alert stopped watching
exactly when after-hours trading *starts*, even though the fetch it
depends on was already capable of seeing that activity. 8pm aligns the
gate with where that fetch's own real coverage actually ends, not an
arbitrary earlier stop. Widening past 8pm would be pointless with the
current data source — the separate Blue Ocean ATS overnight session
(8pm-4am ET) isn't visible to a plain `yfinance prepost=True` fetch at
all (see Primary Data Source above).

**Uses `TQQQ`'s own price, not QQQ** — deliberate, and consistent with
how `intraday_entry_backtest.py` was built: this is a factual question
about the actual tradable instrument for this specific decision (the
trader would be buying TQQQ directly), same reasoning already applied to
the trailing-stop idea. `ladder_core.py`'s structural calculations stay
QQQ-based for the decay-contamination reasons documented throughout this
whole doc; this alert is a different kind of question.

**`_running_high` is in-memory only** (module-level variable, not
persisted) — resets on a bot restart. Originally called "a minor,
self-correcting inconvenience" here — **that framing turned out to be
wrong, confirmed by a real production incident**, not just a theoretical
concern: running high peaked at $79.36 at 3:37pm on Sept 21, 2026, then a
4:29pm restart reset it to $78.87 — *lower* than the true day's high,
meaning a real pullback from $79.36 would have been measured against the
wrong, understated peak for the rest of that session. Mitigated, not
eliminated, by `seed_running_high_from_today()` — see its own section
below.

**No anti-spam suppression — a deliberate choice, not an oversight.** An
armed/re-arm-on-recovery design (fire once, go quiet until price recovers
above the threshold line, matching the pattern already used and validated
in `intraday_entry_backtest.py`'s `simulate_alerts()`) was built,
considered, and explicitly REJECTED for the live alert specifically
because of a confirmed real failure case: if price keeps falling WITHOUT
recovering in between, a genuinely deeper, better entry than the one
already missed would never get its own alert, since "armed" would never
flip back to `True`. Given the actual plan is "get in at a known
retraceable threshold, use the ladder to average down further if needed,"
never silently missing a better entry was judged to outweigh the accepted
cost of repeated alerts during a sustained dip (every poll where price
remains below threshold fires — e.g. a 3-hour dip could mean ~36
messages). A user-controlled **snooze** (not automatic bot-side
suppression) is the planned way to address alert frequency later — see
Known Gaps.

**Channel**: `client.get_channel(ALERT_CHANNEL_ID)` — required because,
unlike an interactive command, a background task has no `Interaction`
object to derive a channel from. Can be set to the same channel already
used for `/buyfilled`/`/marketcheck` — nothing requires a separate one.

### Dip-Alert Price Source — Split by Regular-Hours Status

Originally `check_dip_alert` used `fetch_live_price_extended_hours`
(`yfinance`) unconditionally, all day. Revisited after calculating the
real volume: ~162 single-symbol calls/day (5-min cadence across the full
6:30am-8pm window) — ~3.5x higher than the only proven-safe precedent on
this VPS (the existing 15-min-cadence bots' combined ~46 calls/day, run
over a month with zero rate-limiting). No incident had actually happened
at the time this was changed — this was a precautionary fix based on
volume math, not a reaction to a confirmed failure, consistent with this
project's general standard of evidence before action; the exception
here is that the blast radius mattered more than usual: **all six bots
on this VPS share one IP**, so a block triggered by this alert's volume
could plausibly take down every other bot simultaneously, not just this
one feature. General ASN/network-range-level blocking is a real,
standard anti-bot industry practice (confirmed against real blocklist
products), though not specifically confirmed as something Yahoo's
backend does — the concrete `yfinance` incident reports found describe
individual-IP blocks, not confirmed range bans. Worth naming directly: a
dedicated IPv4 (already in place on this VPS) protects against
collateral damage from other *tenants* on shared infrastructure, but
does NOT protect against ASN-level blocking, which targets the network
owner, not the individual address within it.

**`fetch_dip_alert_price()`** now branches on `is_regular_hours_clock_only()`
— **corrected from an earlier version that branched on
`is_regular_market_hours_live()`, which quietly defeated the point of
this whole split.** That function's own holiday-aware SPY probe runs on
*every* call within the 9:30-4 clock window, uncached — so gating the
Alpaca/`yfinance` switch on it meant every regular-hours cycle still made
exactly one `yfinance` call, just relabeled from "fetch TQQQ price" to
"probe SPY for a holiday," never actually eliminated. The whole point of
this split was to get regular-hours cycles off `yfinance` entirely; using
the holiday-aware check silently ate back most of the intended reduction.
Found by direct review, not by an incident. **Fixed** with a new,
separate `is_regular_hours_clock_only()` — same 9:30am-4:00pm ET weekday
window, deliberately *without* the SPY probe: pure clock check, zero
network calls, genuinely free to call every cycle. Same low-stakes
reasoning already applied to `is_dip_alert_window()`'s own pure-clock
design: it can't tell an actual holiday from a normal trading day, but a
wrong guess is cheap and already handled downstream — the Alpaca branch
simply raises with no real trades to find, caught by the caller's
existing `try/except`, which skips that cycle with a warning.
`is_regular_market_hours_live()` itself is untouched and still correctly
used by `/marketcheck` and `/buyfilled`, which call it once per command,
not once every 5 minutes all day — its holiday awareness genuinely
matters there, in a context where the caching cost never applied.

- **During regular hours (9:30am-4:00pm ET)**: Alpaca's
  `fetch_latest_trade_price_with_staleness_check` (from `alpaca_data.py`,
  written but previously unused) — real, liquid regular-session trading,
  no staleness concern. Raises if the trade is older than its own
  threshold (default 10 min) rather than silently returning a stale
  value; caught the same way as any other fetch failure.
- **Outside regular hours** (the pre-market/post-market portions of the
  wider dip-alert window): unchanged, stays on `yfinance`'s
  `fetch_live_price_extended_hours` — same reasoning already established
  for why extended-hours price was never migrated to Alpaca elsewhere:
  IEX's extended-hours liquidity is thinner than its already-small
  regular-session share.

**A real, distinct risk this split introduces, and how it's handled**:
splitting sources mid-day means the 9:30am and 4:00pm boundaries could
otherwise produce a FALSE dip or false new-high purely from Alpaca and
`yfinance` quoting slightly different prices at the same instant — not
real market movement. **Fixed** via `_last_price_source` tracking: the
moment the source changes between consecutive polls, `running_high`
resets to that fresh reading instead of being compared across venues —
treated exactly like a bot restart, with its own distinct log line
("price source switched... running high reset") so it's identifiable in
`journalctl` and not confused with an actual price move. Every dip-alert
log line now includes which source produced that reading.

**A second, subtler gap in the same protection, found during the
restart-seeding work below**: `seed_running_high_from_today()` always
seeds from `yfinance` (documented there), but an earlier version of the
seeding code never set `_last_price_source` alongside it. Since the
cross-venue guard above only fires once a *prior* source is already
known, a restart during regular hours meant the very first live poll
(which would use Alpaca) compared a `yfinance`-seeded high against a
fresh Alpaca reading with **zero** cross-venue protection — exactly the
failure mode this section's fix exists to prevent, just on the one cycle
right after a restart. **Fixed**: `on_ready` now sets
`_last_price_source = "yfinance"` in the same block that sets
`_running_high` from the seed, so the guard is armed from the very first
post-restart poll. Given the diff-tested Alpaca/`yfinance` close-price
gap (median 0.014%, 95th pct 0.065% — see Primary Data Source above), the
practical exposure this closes was small, but it was a real, findable gap
in the exact case the protection was built for.

### Dip-Alert Observability — Added After a Real Gap Was Noticed

The original `check_dip_alert` was completely silent on every normal
cycle — only a `log.warning` on an outright fetch failure, nothing else.
First real deployment confirmed this directly: `journalctl -f` showed
only the one-time startup line, with no way to tell whether the loop was
actually running a cycle every 5 minutes versus having silently died.
Two distinct problems, both fixed the same session:

**1. No routine confirmation the loop is alive.** Fixed with an `INFO`
line on every single branch of every cycle — window-closed skip,
in-position skip, fetch failure, new-running-high update, "X% below
high, no alert," and threshold-met. Running `journalctl -u
tqqq-ladder-bot -f` during market hours now shows a real line roughly
every 5 minutes, confirming the loop is genuinely executing, not just
that it started once.

**2. `discord.ext.tasks` loops silently stop forever on any unhandled
exception, unless an error handler is registered** — none existed.
Without one, a single unexpected crash mid-cycle (anywhere in the
function, not just the price fetch) would have killed dip-alert polling
permanently, with nothing in the logs explaining why it just stopped.
**Fixed** with `@check_dip_alert.error`, which logs the crash loudly
(`log.error(..., exc_info=error)`) rather than letting it vanish.
Recovery is usually automatic from there: the next Discord gateway
reconnect fires `on_ready` again, which already checks
`check_dip_alert.is_running()` and restarts it if needed — but only if
the failure was visible enough to notice and investigate in the
meantime, which is exactly what this fix provides.

**3. The alert-firing path itself had a real gap, found by direct code
review, not by an incident**: `THRESHOLD MET ... firing alert` was
logged **before** attempting `channel.send()`, but nothing confirmed the
send actually succeeded — that call wasn't even wrapped in try/except.
A send failure (permissions, a transient Discord API error, rate
limiting) would have looked identical in the logs up to that point as a
genuine success, with the actual delivery failure only discoverable by
checking whether a message actually appeared in Discord. **Fixed**:
`channel.send()` now wrapped in try/except, producing one of two
distinct, unambiguous log lines — `ALERT SENT` (confirmed delivered) or
`ALERT DECIDED BUT SEND FAILED` (the decision was correct, delivery
wasn't, with the real exception attached) — rather than one line that
could silently mean either outcome.

**Still open, raised but not yet decided**: fired alerts currently exist
only in `journalctl` (rotates/expires) and Discord's own message
history — no durable audit trail the way `/buyfilled` has
`ladder_log.jsonl`. Whether dip-alerts deserve the same persistent
record (price, timestamp, later confirmed as filled/skipped) is an open
question, not yet built — see Known Gaps.

### Restart-Reset Seeding — `seed_running_high_from_today()`

Directly addresses the incident described above (`_running_high`
understated after the 4:29pm Sept 21 restart). Without this,
`_running_high` starts at `None` on every process start and silently
re-anchors to whatever price the *first* post-restart poll happens to
see — understating any dip that already happened earlier that session. A
restart can trigger this for any reason, not just a manual one:
`_running_high` lives only in RAM, so a crash-and-systemd-restart cycle
wipes it exactly the same way a manual restart does.

**Reuses `fetch_live_price_extended_hours`'s own 5-day/5m `prepost=True`
`yfinance` pull** — no new data source, no new network pattern, and the
same real limitation carried forward honestly: this still can't see the
Blue Ocean overnight session, so a restart during *that* window would
still seed from a lower, visible-hours-only high. Better than `None`, not
a full fix for the same underlying gap. Deliberately stays on `yfinance`
rather than routing through `fetch_dip_alert_price`'s Alpaca/`yfinance`
split — this runs once, at startup only, not on a 5-min cadence, so the
rate-limit motivation for that split doesn't apply here.

**Filtered to TODAY's ET calendar date only, not the full 5-day
window** — deliberate. Seeding from a multi-day max risks anchoring to a
stale peak from a prior session that's irrelevant to the *current*
dip-watching cycle. Neither `check_dip_alert` nor `check_rung_alert` has
a daily reset of its own, so a stale multi-day peak, once seeded, could
sit there unrevisited for days — silently suppressing real alerts far
worse than the `None`-start problem this function exists to fix.

**Returns `None` (never a fallback price) on any failure or empty
result** — caller must treat that as "couldn't seed, fall back to normal
cold-start behavior," never invent a substitute value.

**Required caller contract, not enforced by the function itself**: `on_ready`
must set `_last_price_source = "yfinance"` in the same block that applies
the seed — see the cross-venue gap this closes, documented in Dip-Alert
Price Source above.

### Rung-Crossing Alert — `check_rung_alert`

Closes a real, confirmed gap: `check_dip_alert` goes silent the moment a
position is tracked ("alert is for flat only" — see Gate order above),
but nothing was ever watching for price reaching the ladder's own Buy
1/2/3 levels *while actually holding*. Those were display-only numbers in
the `/buyfilled` embed, with zero automated monitoring behind them.
**Confirmed live, not just theoretical**: on Sept 21, 2026, TQQQ price
crossed both Buy 1 ($77.38) and Buy 2 ($75.77) with no alert of any kind,
because `check_dip_alert` was already gated off by the tracked position.

**A second, independent `discord.ext.tasks.loop(minutes=5)`** — same
reason it has to live in `tqqq_buy_ladder_bot.py` itself as
`check_dip_alert` does (needs the bot's own live Gateway connection for
the buttons/modals to route). Started in `on_ready` alongside
`check_dip_alert`, guarded the same way against double-starting across
reconnects.

**Gate is the exact opposite of `check_dip_alert`**: fires only when
`read_position_state()` is *not* `None`. Same `is_dip_alert_window()`
window (needs the same extended-hours live price fetch, same coverage
limits). Uses `fetch_dip_alert_price()` — the *same* Alpaca/`yfinance`
split `check_dip_alert` uses, not a separate unconditional `yfinance`
call. This matters: without sharing the split, this task would silently
undo the rate-limit reduction the split exists for on every day the
trader is actually holding a position — arguably the *more* common state
for an averaging-down strategy, not an edge case.

**Checks only the ladder's nearest rung** (`build_ladder()`'s first
returned level), not all three shown in `/buyfilled` — deliberate, and
not a corner cut. No separate "watch Buy 2, watch Buy 3" logic is needed
or would even be correct: once this alert's own Filled/Custom Fill button
updates `position_state.json`, the basis (and often the day's
`qqq_atr_pct`) changes, so `build_ladder()` naturally produces a *new*
nearest rung on the very next cycle. Hardcoding a watch on today's Buy 2
price would in fact be *wrong* the moment Buy 1 fills, since a fresh
basis shifts where Buy 2 actually sits — watching the ladder's own
always-current first level sidesteps that for free.

**Does NOT need `_last_price_source`'s cross-venue reset logic**, unlike
`check_dip_alert` — a deliberate, reasoned omission, not an oversight.
That guard exists to protect a *remembered* `running_high` from a false
comparison against a differently-sourced fresh price. Here, `nearest.price`
is recomputed from scratch every single cycle from `build_ladder()`
(itself always fed by Alpaca's `fetch_daily_bars`, independent of which
source fed `current_tqqq_price`) — there's no persisted value a source
switch could corrupt, only a same-cycle comparison that starts fresh
every time regardless.

**Reuses `DipAlertResponseView` and `handle_fill_confirmation()`
unchanged** — same buttons, same modals, same single confirm-fill
pipeline `check_dip_alert` and `/testdipalert` already use, rather than a
second, divergent recording path. The view's "Filled at Target" /
"Custom Fill" / "Skip Dip" labels stay as-is; a near-duplicate view class
just to rename "Skip Dip" for this context was judged not worth forking.

**Same no-anti-spam policy as `check_dip_alert`, for the same reason**:
if not confirmed within one 5-min cycle, the next cycle recomputes the
(usually near-identical) rung price and fires again rather than going
quiet — silently going dark on a level actively being averaged into
would be worse than a repeated ping.



Three scripts, none of them part of the live bot, all following
`backtest_ladder.py`'s existing pattern (manual Colab execution, pulls
data live, no local data files committed):

- **`backtest_ladder.py` §5 (added)** — daily-bar entry-threshold sweep:
  for a range of pullback thresholds (0.3%-3%) and holding periods (5/10/
  20 days), simulates entering after a same-day dip and measures forward
  returns vs. a random-entry baseline. Two entry-timing modes compared
  side by side: `same_day_threshold` (realistic for the actual live-alert
  plan — enters at the threshold-implied price, no next-day carry) vs.
  `next_open` (kept only for comparison — matches `tqqq_swing_bot_v2.py`'s
  different, after-close-signal workflow). Uses `(high-low)/high` for the
  trigger (see bug-fix note above), years of history, but can't see
  intraday bounce/re-dip sequencing.
- **`intraday_pullback_analysis.py`** (new) — one month of real 5-minute
  TQQQ bars, characterizes how deep dips typically go before recovering
  to a new high (mean/median/percentiles), and what fraction of dips
  round-trip within a single 5-minute bar (directly answers "would 5-min
  polling actually catch this"). Peak resets at each new trading day.
- **`intraday_entry_backtest.py`** (new) — the precise version:
  simulates the EXACT live alert logic (persistent running high across
  days, edge-triggered, re-arms on recovery — this script DOES use the
  armed/re-arm pattern, since it's evaluating outcomes, not the live
  alert's deliberately-different no-suppression choice) against real
  5-minute bars, measuring actual forward returns per threshold. Limited
  to `yfinance`'s ~60-day intraday history — a short, recent, single-regime
  sample, explicitly flagged in its own caveats.

**A real result from running this, worth recording as a caution, not a
conclusion**: the ~60-day window available at the time (Jun 25-Sep 18,
2026) turned out to overlap a confirmed real QQQ correction (10%+
drawdown, mid/late July 2026; TQQQ's own 90-day range spanned $59.69-
$88.09, roughly a 32% swing) — verified via web search, not assumed. Fire
counts were tiny (3-10 alerts per threshold) and forward returns were
consistently negative across almost every threshold/horizon, including
the random baseline at longer holds. **Read as**: likely evidence that
almost any long entry underperformed during this specific stretch,
dip-triggered or not, rather than proof that dip-buying itself doesn't
work — a concrete, data-backed illustration of why the regime badge
exists, not a verdict on the entry-threshold idea. Re-run periodically;
a single ~2-month window is not enough to separate "bad strategy" from
"bad regime."

### Known Gaps — Not Yet Done (Ladder Bot)

- **No persistent audit trail for fired dip-alerts.** Unlike `/buyfilled`
  (which has `ladder_log.jsonl`), fired alerts currently exist only in
  `journalctl` (rotates/expires) and Discord's own message history — no
  durable record of price/timestamp/eventual outcome (filled, custom
  fill, or skipped). Raised directly, not yet decided or built.
- **No full portfolio P&L simulation.** The backtest measures whether
  individual levels get touched, not the resulting blended cost basis from
  actually averaging in across sequential fills, and doesn't model
  position sizing per level at all — every level is currently price-only,
  with no size recommendation attached.
- **No automated daily/self-updating check — RESOLVED for both the "no
  position" and "in a position" cases.** A scheduled daily post was
  deferred pending a persistent position store; that store now exists
  (`position_state.json`, see its own section above) and powers the
  automatic dip-entry alert (`check_dip_alert`, flat only). The
  in-position case — watching for price approaching an already-computed
  rung — was explicitly scoped out at the time as a different mechanism
  (watching a specific computed price vs. a running-high % dip), not a
  natural extension of the flat-position alert. It's since been built as
  `check_rung_alert` — see its own section above — after price was
  confirmed live to cross both Buy 1 and Buy 2 with no alert firing at
  all, since `check_dip_alert` was already gated off by the tracked
  position.
- **Average-cost tracking is now semi-automatic, not fully manual** — a
  fill confirmed via the dip-alert buttons/modals updates
  `position_state.json` and blends the average correctly. Still manual
  in the sense that nothing forces the trader to confirm a fill via the
  bot at all — a fill made outside this flow (e.g. running `/buyfilled`
  directly with hand-typed numbers) works fine but isn't "detected."
  Still manual in the sense that the bot has no way to detect a fill it
  wasn't told about (e.g. running `/buyfilled` directly with hand-typed
  numbers still works, but isn't "confirmed" through the button/modal
  flow) — it can't catch a stale or forgotten update on its own.
- **Confluence-vs-frequency is not conclusively settled** — see above;
  revisit with a larger dataset, a formal significance test on the
  depth-bucketed comparison, and/or a rolling (not single 70/30) validation
  split before treating either direction as proven.
- **No transaction costs, slippage, or manual-execution-timing realism**
  modeled in the backtest.
- **Support display is informational only** — deliberately does not affect
  the ladder; there is no "boost the level toward support" behavior, by
  design (see confluence discussion above).
- **No sizing per rung, no exposure cap.** `target_avg` (above) answers
  "how many shares to hit a target I chose," but nothing tells the trader
  what target to pick, or caps total capital committed across a cycle.
  Three rungs is a *display* count, not a *limit* — after a fill, the
  basis updates and three new rungs appear, with nothing structurally
  stopping indefinite continued averaging into a sustained decline. Flagged
  as the sharpest unaddressed gap from an external review; would need its
  own deliberate design (fixed-dollar vs. volatility-weighted sizing, a
  max-capital-per-cycle or "stop adding below X" rule) rather than a quick
  patch.
- **Levels 2 and 3 are a planning preview, not fixed commitments** — worth
  stating explicitly rather than leaving implicit. ATR is recomputed fresh
  on every `/buyfilled` call, so if Level 1 fills and the trader re-runs
  with a new basis, Levels 2/3 are recalculated from whatever ATR% exists
  *then*, not what was shown when the ladder was first displayed. In
  practice the drift is usually small (mean ATR% day-to-day change ~2.9%
  of its own value at the current `ATR_PERIOD=14`; Level 1's median
  fill time is ~2 days) — but during a genuine volatility regime shift in
  that window, the preview could be meaningfully off. No UI currently
  flags this provisional nature to the trader.
- **`/marketcheck`'s RSI(2)-fires-daily assumption is unverified.** The
  trader's stated goal was "trade at least once a day"; RSI(2) was added
  partly on that basis, but how often it actually crosses its threshold
  historically has not been checked against real data the way `ATR_STEP`
  and `SWING_FRACTAL_WINGS` were — worth a quick addition to
  `backtest_ladder.py` before treating it as delivering on that goal.
- **RSI(2) vs. RSI(14) correlation is reasoned, not measured.** Both should
  track closely under normal conditions (leverage largely cancels out of a
  gain/loss ratio, unlike cumulative price) — divergence between them is
  theorized to signal decay-driving choppiness, but this hasn't been
  backtested against real QQQ/TQQQ RSI series the way other claims in this
  document have been.
- **`ladder_log.jsonl` is tracked in git but not yet in `push_logs.sh`'s
  automatic daily push** — pushing today's log requires a manual
  `git add`/`commit`/`push`, unlike the other bots' logs.
- **ATR "re-anchor" / volatility override (Phase 2) — deferred, not
  built.** A two-phase alert design was proposed: Phase 1 (immediate
  next-level calculation on fill, built) plus Phase 2 (if live ATR
  expands >10% since the last fill, push a follow-up alert suggesting
  the next tier move deeper). The mechanism is sound and reuses existing
  `compute_atr_pct()`, but needs a cap on how many times a single tier
  can be re-anchored — without one, a sustained volatility expansion
  could make the target perpetually retreat (an ATR-driven echo of the
  original moving-goalposts problem this bot was built to avoid).
  Requires storing ATR-at-fill-time in `position_state.json`, which it
  does not currently do.
- **User-controlled alert snooze — deferred, not built.** The explicit,
  chosen alternative to automatic anti-spam suppression (see the
  dip-alert section above) — lets the trader silence repeat alerts on
  their own terms rather than have the bot guess when to go quiet.
- **Overnight (Blue Ocean ATS, 8pm-4am ET) coverage — deferred,
  confirmed infeasible on the current free data source.** `yfinance`
  has zero visibility into this session (confirmed: not among the
  vendors — Bloomberg/dxFeed/ICE/QUODD/Databento — known to carry Blue
  Ocean's feed). Alpaca DOES offer it, per their own docs, but as a
  paid, "contact sales," 15-min-delayed, indicative-only add-on — a
  real, priced decision, not a free-tier swap, if ever pursued.
- **Pre-market data reliability (6:30-9:30am ET) is unverified in
  practice.** The window was extended based on documented `yfinance`
  precedent (real pre-market bars have been shown to work) plus known,
  real timing quirks (occasional "not ready yet" gaps) — the existing
  fetch already fails soft on these, but actual frequency in this
  specific deployment hasn't been observed yet. Alpaca's free tier was
  flagged as a plausible, more-reliable alternative if real problems
  show up — deliberately not implemented pre-emptively for a
  not-yet-confirmed problem.
- **`tqqq_bot.py`'s cron schedule may not match its own documented
  hours — flagged, unconfirmed, NOT yet fixed.** Its three cron lines
  read `30 18`, `30 19`, `47 19` under `CRON_TZ=America/Toronto`
  (meaning direct ET), but the comment directly above them says "2:30pm,
  3:30pm, 3:47pm ET" — a four-hour gap consistent with these being
  leftover values from before `CRON_TZ` was added (when the VPS likely
  ran on plain UTC). Every other job in the same crontab uses direct ET
  hours matching its own comment; only this section doesn't. If
  correct, `tqqq_bot.py` may have been checking its Path A signal after
  the close instead of during market hours. Not part of the ladder bot,
  noted here because it surfaced while cleaning up
  `tqqq_intraday_bot.py`'s cron entries in this same session — needs its
  own investigation/fix, tracked separately.

---

## event_calendar_bot.py — Market Event Calendar (Producer/Consumer Pattern)

A daily producer bot, added to avoid every other bot re-implementing its
own version of "is today a day I should be more careful." Writes one
shared `event_flags.json`; every other bot (cron or systemd) reads it via
a small helper module rather than each fetching its own calendar data.

### Files

```
event_calendar_bot.py   ← producer, run once daily via cron
event_flags.py          ← consumer-side read-only helper, imported by
                            any bot that wants to check today's flags
```

`event_flags.py` needs to be reachable from wherever it's imported —
copied into `tqqq_ladder_bot/` alongside `ladder_core.py` for the
systemd bot, and living at repo root for the cron bots.

### What It Flags

- **FOMC** — meeting days, statement day, whether SEP/dot-plot is
  included, and days-until-next-statement. Computed from a hardcoded
  schedule (the Fed publishes these ~1yr ahead) — no network call.
- **CPI / PCE / NFP** — fetched from FRED's official release-dates API
  (`FRED_API_KEY` required, free signup), cached and refreshed roughly
  monthly since these dates rarely move once published.
- **Monthly OpEx / quarterly quad witching** — pure calendar math (3rd
  Friday of the month), no fetch needed.
- **Top-QQQ-holding earnings** (`NVDA`, `AAPL`, `MSFT`, `AMZN`, `GOOGL`,
  `GOOG`, `AVGO`, `META`, `TSLA`, `NFLX`) — fetched daily via yfinance's
  `Ticker.get_earnings_dates()`, since (unlike the above) earnings dates
  aren't stable weeks out and can shift close to the event. Requires
  `lxml` installed (the underlying scrape parses an HTML table).
- Combined into one `is_high_impact_day` boolean per date, covering
  today plus a rolling lookahead window (`EVENT_BOT_LOOKAHEAD_DAYS`,
  default 5 days).

### Why FRED for Macro Data, But yfinance for Earnings

Originally tried yfinance's `Calendars.get_economic_events_calendar()`
for CPI/PCE/NFP too, to avoid needing a FRED API key. Live-tested and
rejected: a 45-day, 100-row sample of that endpoint returned **zero US
entries** — the calendar covers dozens of other countries but doesn't
reliably surface the exact US releases this bot needs. FRED is the
authoritative, government-sourced calendar for those three specifically,
so it stayed. Earnings dates aren't available from FRED at all, and
yfinance's `get_earnings_dates()` (a different, ticker-scoped endpoint)
tested fine — no need for Finnhub or its separate API key.

### Reliability Fixes Baked In

- **Atomic writes** — `event_flags.json` is written to a `.tmp` file
  then swapped in via `os.replace()`, so a bot reading it mid-write
  never sees a half-written file.
- **ET-anchored dates, not the VPS's local clock** — every "today"
  calculation uses `zoneinfo.ZoneInfo("America/New_York")` explicitly.
  The VPS defaults to UTC; without this fix, "today" would roll over at
  7-8pm ET (mid-session or just after), misdating flags for the rest of
  the evening.
- **Per-category cache preservation** — if the FRED fetch fails for one
  release type (or fails entirely), previously cached dates for that
  category are kept rather than wiped for the next ~25 days until the
  next refresh.
- **Every fetch failure degrades gracefully** — a missing `FRED_API_KEY`,
  a failed earnings lookup for one ticker, or a stale/missing
  `event_flags.json` on the consumer side all fall back to "no flag
  raised" rather than crashing the calling bot.

### Env Vars

```
FRED_API_KEY               required for CPI/PCE/NFP (free: fredaccount.stlouisfed.org/apikeys)
DISCORD_URL                 reused from the other bots — daily summary post
EVENT_BOT_DATA_DIR          default /opt/stock-bot/data
EVENT_BOT_LOOKAHEAD_DAYS    default 5
EVENT_FLAGS_MAX_STALENESS_HOURS   default 20 — consumer-side staleness warning threshold
```

### Consuming It From Another Bot

```python
from event_flags import get_today_flags, is_high_impact_today, earnings_within

if is_high_impact_today():
    # pause new entries, halve size, widen stops -- left to each bot's
    # own risk posture, deliberately not decided inside event_flags.py
    ...
```

A `tqqq_buy_ladder_bot.py` integration was attempted at one point — `📅
{date}` in the `/marketcheck`/`/buyfilled` embeds, plus `event_flags_date`/
`event_flags_summary` on every logged fill (this is why older
`ladder_log.jsonl` lines, from that period, carry those fields). **This
integration is NOT currently present in the deployed ladder bot** —
confirmed directly by reading the live `tqqq_buy_ladder_bot.py` source
(no `event_flags` import or usage anywhere in it). The most likely
explanation: it was added, ran for a while (explaining the old log
entries), then reverted at some point, without this doc being updated to
match. If re-adding this integration is ever wanted, it should be treated
as new work against the current file, not assumed already present.

### Maintenance

- **`TOP_HOLDINGS`** — update quarterly alongside Nasdaq-100 rebalances;
  pull current top weights from Invesco's QQQ holdings page.
- **FOMC schedule** — 2026 dates are Fed-confirmed; 2027 dates are the
  Fed's own "tentative" schedule (published Sept 2025) and should be
  re-verified against federalreserve.gov once 2027 opens.
- **`FRED_RELEASES` release IDs** — stable in practice, but not
  contractually guaranteed; verify against fred.stlouisfed.org/releases
  if a release ever silently stops matching.

### VPS Deployment — Cron, Not Systemd

Unlike the ladder bot, this one runs once a day and exits — a normal
cron job, staggered to run before every other bot's first cron slot so
its output is available all day:

```
0 7 * * 1-5  cd /root/Stock-bot && /usr/bin/python3 event_calendar_bot.py >> /root/logs/event_calendar_bot.log 2>&1
```

---

## VPS Crontab Schedule

**Server timezone: UTC — but `CRON_TZ=America/Toronto` makes all times ET automatically**

**Current live state (above-open bot added, staggered 2 minutes from the
pullback bot to avoid simultaneous `yfinance` request bursts from both
bots hitting the API at the same instant):**

```
CRON_TZ=America/Toronto
DISCORD_URL="https://discord.com/api/webhooks/..."
FRED_API_KEY="..."

# ── Event Calendar (producer, runs before everything else) ──────────────────
0 7 * * 1-5        event_calendar_bot.py           → event_calendar_bot.log

# ── TQQQ Swing — 2:30pm, 3:30pm, 3:47pm ET ───────────────────────────────────
30 18 * * 1-5     tqqq_bot.py           → tqqq.log
30 19 * * 1-5     tqqq_bot.py           → tqqq.log
47 19 * * 1-5     tqqq_bot.py           → tqqq.log

# ── TQQQ Intraday (Pullback, unvalidated — see status warning) ──────────────
0,15,30,45 10-15 * * 1-5  tqqq_intraday_bot.py            → tqqq_intraday.log
30 16 * * 1-5              tqqq_intraday_bot.py --reconcile --notify  → tqqq_intraday.log

# ── TQQQ Above-Open (validated, recommended) — staggered 2 min from pullback ─
2,17,32,47 10-15 * * 1-5  tqqq_above_open_bot.py           → tqqq_above_open.log
32 16 * * 1-5               tqqq_above_open_bot.py --reconcile --notify  → tqqq_above_open.log

# ── Push all JSON logs to GitHub (4:40pm ET) ─────────────────────────────────
40 16 * * 1-5  push_logs.sh
```

**Every job line uses the full pattern** `cd /root/Stock-bot && /usr/bin/python3 <script>` in the actual deployed crontab (abbreviated above for readability) — consistency here matters, see
[Why Every Cron Line Uses the Full `cd && /usr/bin/python3` Pattern](#why-every-cron-line-uses-the-full-cd--usrbinpython3-pattern)
below.

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
| `/root/logs/tqqq.log` | tqqq_bot.py | Every 15-min scan output |
| `/root/logs/tqqq_intraday.log` | tqqq_intraday_bot.py | Every 15-min scan output, full condition-by-condition rejection reasons, reconciliation, and crash tracebacks |
| `/root/logs/heartbeat.log` | all bots (shared file) | One `[timestamp] bot_name OK` line per successful normal-mode run — `tail` this to confirm every bot's cron is still firing across the whole VPS. `tqqq_intraday_bot.py` writes here on every non-reconcile run (including skips); it does **not** write during `--reconcile`, and does **not** write on a crash (a stale heartbeat during market hours is itself the "something's wrong" signal, on top of the direct Discord crash alert — see [Key Implementation Details](#tqqq_intraday_botpy)) |
| `/root/logs/git_sync.log` | push_logs.sh | Daily push results and any git errors |

### JSON Files (pushed to GitHub daily at 4:40pm ET)

| File | Bot | Contents |
|------|-----|----------|
| `trade_log.json` | tqqq_bot.py | Confirmed TQQQ swing trade entries |
| `tqqq_gate_blocks.json` | tqqq_bot.py | TQQQ swing rejections with RSI gap tracking |
| `tqqq_intraday_trade_log.json` | tqqq_intraday_bot.py | **Every** signal that fires (no take/skip filtering), auto-reconciled against real price data at 4:30pm — see schema below. **Note:** validated edge retracted, see status warning above. |
| `tqqq_above_open_trade_log.json` | tqqq_above_open_bot.py | **Every** signal that fires, tagged `validated`/informational by signal number, auto-reconciled at 4:32pm — see schema below |
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

### Trade Log Fields — TQQQ Above-Open (`tqqq_above_open_trade_log.json`)

```json
{
  "date":              "2026-07-14",
  "bar_time":           "11:00 ET",
  "signal_bar_ts":      "2026-07-14T11:00:00-04:00",
  "signal_number":      1,               ← 1st, 2nd, 3rd... signal that day
  "validated":          true,            ← true ONLY for signal_number == 1
  "alert_price":        75.48,           ← signal bar's close
  "target":             75.98,
  "stop":               75.08,
  "day_open":           75.19,           ← that day's actual opening price
  "close":              75.48,           ← same as alert_price, kept for clarity
  "pct_above_open":     0.38,            ← % above day_open at signal time
  "alert_sent_at":      "2026-07-14T11:00:03-04:00",

  "reconciled":         true,            ← filled by --reconcile at 4:32pm ET
  "exit_price":         75.08,
  "exit_time":          "11:05:00",
  "exit_reason":        "STOP",          ← TARGET / STOP / TIME
  "max_favorable":      75.52,
  "max_adverse":        75.05,
  "pnl_per_share":      -0.40
}
```
Same auto-reconciliation approach as the pullback bot (every alert treated
as taken at the alert price, no manual fill logging) — but `--summary`
reports three separate views (validated / informational / combined) given
this bot's own backtest showed those two categories are not statistically
equivalent, unlike the pullback bot where all signals were treated as one
pool.

#### Reconcile Discord Notification — `send_reconcile_summary_to_discord()`
Originally missing entirely — the pullback bot has always had `--notify`
for a post-reconcile Discord summary, but this was not carried over when
`tqqq_above_open_bot.py` was first built. Added after being noticed live
(daily reconcile ran correctly and silently every day, with no
corresponding Discord message). Posts a daily wrap-up split into
`VALIDATED (signal #1)` and `INFO-ONLY (signal #2+)`, matching the same
distinction used everywhere else in this bot — total P&L, win/loss count,
and per-trade detail for each category. Handles the "no signals today"
case gracefully (posts a short "nothing to report" message rather than
staying silent, so the absence of trades is visibly confirmed rather than
looking like the job might have failed). Fails open: if the Discord post
itself fails, logs a warning but never blocks or breaks the reconcile job.
Requires `--notify` on the cron line (`--reconcile --notify`, same flag
name and pattern as the pullback bot) — reconciliation itself runs
identically with or without it; the flag only controls whether a summary
gets posted.

---

### Bug Fixes — External Code Review (Post-Deployment)

A round of external review after deployment raised five concerns. Each was
independently verified against the actual code before acting — two were
real bugs and fixed, two were already correctly handled (the review was
describing an outdated version or a resolved question), and one was a
reasonable minor optimization adopted with adjusted scope.

#### Fixed: `day_open` Silently Corrupted by Truncated Fetch Data
```python
# Was:
day_open = df.groupby("date")["open"].transform("first")
```
Assumed the first row present for any given date is always the 9:30 bar —
true in a clean backtest, not guaranteed live. If `yfinance` ever returns
a day's data with the early session missing (e.g. only starting from
11:30am due to an API hiccup), this would silently treat 11:30's open as
`day_open`, corrupting every signal check that day with no warning at all.
**Fixed** by explicitly locating the 9:30 bar rather than assuming row
order; a day genuinely missing it now correctly gets `NaN` instead of a
wrong number — and `check_signal()` already skips any bar where
`day_open` is `NaN`, so this fails safe (no alert) rather than firing on
a corrupted reference price. Verified with a test reproducing the exact
truncated-data scenario, plus a regression check confirming normal,
complete days are unaffected.

#### Fixed: `date.today()` Reads System Clock, Not ET — Real Bug on Manual Late-Evening Runs
```python
# Was, in three places:
today = date.today().isoformat()
```
`date.today()` reads the OS clock, not the bot's `ET` timezone object. The
VPS runs UTC by default. **`CRON_TZ=America/Toronto` only controls *when*
cron fires a job — it does not change what `date.today()` returns inside
the running Python process.** These are two unrelated things. At roughly
7-8pm ET (when UTC crosses midnight), a bare `date.today()` call would
silently return the *next* calendar day. The scheduled 4:32pm ET reconcile
is safe (well before this crossover) — the real risk is **manual runs
later in the evening** (`--summary`, `--reconcile` run by hand), which
this whole project has a demonstrated pattern of doing. **Fixed** by
replacing all three usages with `datetime.now(ET).date().isoformat()`.
Verified numerically: at 8:30pm ET, confirmed UTC has already rolled to
the next calendar date, and confirmed the fix correctly stays anchored to
the true ET date regardless of the underlying OS clock.

#### Adopted (Reduced Scope): `period="7d"` → `period="3d"` for the Live Signal Check Only
The external review's stated rationale (reduces payload size enough to
meaningfully avoid Yahoo rate-limiting) is overstated — the actual byte
difference between 2-3 days and 7 days of single-symbol 1-min OHLCV is a
few hundred KB at most, unlikely to change rate-limiting behavior
meaningfully. The change was adopted anyway for a different, better
reason: `fetch_15min_bars()` (the live signal check) never actually needs
more than today's own data — `day_open` and the latest bar are the only
things `check_signal()` reads, unlike the original pullback bot, which
genuinely needed a multi-day window to compute its EMA/volume baselines.
Confirmed via code inspection this bot has no such baseline at all before
changing it. **`period="3d"` applied only inside `fetch_15min_bars()`**
(2 locations: the retry loop and its fallback) — `_fetch_reconcile_raw_data()`
correctly stays at `period="7d"`, since reconciling a potentially-missed
day genuinely benefits from the maximum lookback Yahoo's API allows.

#### Not a Bug: Structural Timeframe Mismatch (10:00 vs 10:15 Start)
The review correctly described the mechanism (a 10:00am cron run
evaluates the bar labeled `09:45`, closing at `10:00`) but its conclusion
— that this violates the validated backtest and should be pushed to
`TRADE_START_M=15` — was already directly tested and settled with real
data earlier in this project, the opposite way:
```
WITH the 10:00 window (current, deployed): 858 trades, 65.4% WR, $0.1767 exp, gap $0.0012
WITHOUT (10:30 start):                      838 trades, 63.8% WR, $0.1625 exp, gap $0.0110
```
Including this window wins on every metric — more trades, higher win
rate, higher expectancy, tighter out-of-sample gap. `TRADE_START_M=0` is
the empirically validated choice, not a drift from it. Not changed.

#### Not a Bug: Holiday Cached-Data Check
The review proposed checking `if df["date"].max() != datetime.now(ET).date()`
to catch a holiday where `yfinance` might return stale cached data instead
of an empty response. This check **already exists** in `fetch_15min_bars()`'s
holiday pre-check (`if _pre.index[-1].tz_convert(ET).date() < now.date(): ...`)
— confirmed present in the actual deployed code before responding. The
review appears to have been evaluating an earlier or assumed version of
the file, not the current one. No action needed.

---

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

## Monthly Review Guide

Run the first monthly review after ~4 weeks of live data.

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
ssh root@<VPS_IP>
```

### Check Latest Bot Output
```bash
# TQQQ (last 50 lines)
tail -50 /root/logs/tqqq.log

# TQQQ intraday (last 50 lines)
tail -50 /root/logs/tqqq_intraday.log

# Shared heartbeat across all bots — confirms every cron is still firing
tail -20 /root/logs/heartbeat.log

# Git push log
cat /root/logs/git_sync.log
```

### Watch Live as Bot Runs
```bash
tail -f /root/logs/tqqq_intraday.log   # Ctrl+C to stop
```

### Count Total Runs
```bash
grep -c "Last closed bar" /root/logs/tqqq_intraday.log
```

### Find All Signals That Fired
```bash
grep "SIGNAL FIRED" /root/logs/tqqq_intraday.log
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

### Check TQQQ Above-Open Live Performance
```bash
# Prints THREE separate breakdowns: validated (signal #1 each day),
# informational (signal #2+), and combined — since this bot's own
# backtest showed those categories are not equivalent
cd /root/Stock-bot && python3 tqqq_above_open_bot.py --summary

# Manually reconcile (normally runs automatically at 4:32pm ET via cron)
cd /root/Stock-bot && python3 tqqq_above_open_bot.py --reconcile
cd /root/Stock-bot && python3 tqqq_above_open_bot.py --reconcile --date 2026-07-14

# Same, but also post a Discord wrap-up summary (split into VALIDATED and
# INFO-ONLY sections) — this is what the scheduled cron job actually runs
cd /root/Stock-bot && python3 tqqq_above_open_bot.py --reconcile --notify

# Quick check for duplicate signals in the trade log (relevant after the
# crontab corruption incident — see Architecture Decisions)
cat tqqq_above_open_trade_log.json | python3 -c "
import json, sys
trades = json.load(sys.stdin)
seen = {}
for t in trades:
    key = (t['date'], t['bar_time'], t['signal_number'])
    seen[key] = seen.get(key, 0) + 1
dupes = {k: v for k, v in seen.items() if v > 1}
print(f'Duplicate signal groups: {len(dupes)}')
"
```

### Test a Bot Manually
```bash
# Force run (bypass market hours), dry run (no log writes)
cd /root/Stock-bot && python3 tqqq_bot.py --force --dry-run

# tqqq_intraday_bot.py has no --force/--dry-run flags — running it directly
# outside market hours safely exits with "Outside trading window" (no writes)
cd /root/Stock-bot && python3 tqqq_intraday_bot.py
```

### View Gate Block / Trade Log Data
```bash
cat /root/Stock-bot/tqqq_gate_blocks.json
cat /root/Stock-bot/tqqq_intraday_trade_log.json
```

---

## Architecture Decisions

### Why VPS over GitHub Actions
GitHub Actions drops ~50% of scheduled runs under load (confirmed from 2+ months of TQQQ bot run history). For a 15-min intraday bot, delays of 7-22 minutes cause missed signals. The VPS cron fires within 2 seconds of the scheduled time every time.

### Why Noon ORB Cutoff
Every ORB signal that fired after 12:00pm ET was a losing trade in the 60-day backtest. These were afternoon drift moves above the OR High — not genuine opening momentum. Restricting ORB to 10:00am-12:00pm confirmed as optimal across three separate test window sizes (1.5h, 2.0h, 2.5h).

### Why Separate Gate Log Files
```
tqqq_gate_blocks.json:        TQQQ swing — RSI gap tracking
```
Different timeframes, different signal types, different reconciliation logic. Mixing them would break the MFE reconciliation and make monthly review impossible to parse.

### Why TQQQ Uses a Scoring System, Not a Single Threshold
TQQQ tracks a broad index (Nasdaq-100), not a single sector — a simple RSI threshold alone isn't sufficient. TQQQ requires simultaneous alignment of trend health AND momentum exhaustion. The scoring system also enables precise backtest validation: specific score+RSI combinations were found to have negative expectancy and blocked (score≥7+RSI≥50 = -0.041%, score≥8+RSI>45 = -1.80%).

### Why the TQQQ Buy Ladder Bot Runs as Systemd, Not Cron
Every other bot in this system starts, runs one check, and exits — a model
that fits cron perfectly. The ladder bot instead must stay continuously
connected to Discord's gateway to receive slash-command interactions in
real time; a process that starts and stops on a schedule is the wrong
model for that. See its own dedicated section above for the full
`systemd` unit and reasoning.

### Why event_calendar_bot.py Uses FRED for Macro Dates but yfinance for Earnings
Tried yfinance's economic-events calendar for CPI/PCE/NFP first, to avoid
needing a second API key. Live-tested and rejected: it returned zero US
entries across a 45-day, 100-row sample — it's an aggregated global
calendar, not sourced directly from BLS/BEA the way FRED is. FRED stayed
for those three specifically. Earnings dates aren't on FRED at all, and
yfinance's separate, ticker-scoped `get_earnings_dates()` tested fine —
no need for a third data source (Finnhub) once yfinance covered it. See
event_calendar_bot.py's own dedicated section above for the full test
that led to this split.

### Why the Buy Ladder Anchors to Basis, Never to Live Price
Re-deriving a buy target from today's live price every time it's checked
causes the target to retreat every time price approaches it — the "moving
goalposts" problem. The ladder's anchor is always the trader's actual
average cost, changed only on a real fill; live price is used only to
filter out levels the market has already passed, never to compute a
level's position. Full reasoning and the filter-and-extend mechanism are
documented in the bot's own section above.

### Why the Buy Ladder Bot Uses QQQ for Structure, Not TQQQ's Own Price History
TQQQ's daily-reset leverage mechanics mean its own historical price series
embeds volatility decay accumulated since each swing low — two TQQQ lows
from different windows aren't directly comparable the way two QQQ lows
are. All ATR% and swing-low structure is computed on QQQ (the undecayed
underlying) and translated to a TQQQ price only at the final step. See the
bot's own section above for the exact formulas and the important
distinction between the two different anchors used for buy targets vs. the
informational support display.

### Why No Automated Order Placement
Signal quality validation phase. Running Discord-alert-only for 3-6 months generates empirical signal data (trade log, gate blocks, MFE). After validation, broker API automation can be added with confidence in the underlying signals. Automated orders before validation would compound errors at machine speed.

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
`heartbeat.log` is meant to answer "is the 15-min signal cron still
firing," not "did the once-daily reconcile job run" — gated by `if not
args.dry_run and not args.reconcile`. Mixing the two into one heartbeat
would make a missed reconcile run (e.g. a temporary yfinance outage right
at 4:30pm) look identical to the far more serious problem of the entire
signal-check cron silently dying.

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

### Why Retry Logic and a Holiday Pre-Check Were Added
When `tqqq_intraday_bot.py` was first built, it had neither retry logic nor an
explicit holiday check — relying only on the empty-data path in
`fetch_15min_bars()` as an implicit fallback. Both were added as real
resilience improvements: retry logic (12 retries × 5 seconds) handles the
case where Yahoo's backend hasn't finished publishing the expected bar yet
when the cron first fires, and the holiday pre-check avoids burning the
full retry budget on a day with no fresh intraday data at all. See
[Key Implementation Details](#retry-logic--yahoo-publication-lag) for the
`fetch_15min_bars()`-specific timing details (1-min fetch + manual resample
requires a `-1 minute` adjustment other pipelines wouldn't need).

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

### Why tqqq_above_open_bot.py Is Fully Separate From tqqq_intraday_bot.py
Explicit design requirement — the two bots share zero state: separate
trade log, separate bot log, separate heartbeat entry name, separate
Discord webhook variable. Given `tqqq_intraday_bot.py`'s validated edge
was retracted mid-project (see
[Why tqqq_intraday_bot.py's Validated Edge Was Retracted](#why-tqqq_intraday_botpys-validated-edge-was-retracted)),
keeping the new, trusted bot fully isolated means debugging, modifying, or
even eventually retiring the old bot can never risk affecting the new
one's data or behavior.

### Why the Two TQQQ Intraday Bots' Cron Schedules Are Staggered by 2 Minutes
Both bots independently call `yfinance` on the same underlying symbol
(TQQQ) on the same nominal 15-min cadence. Running them at the exact same
minute would double the request volume at identical moments — not a
severe risk given `yfinance`'s lack of hard published rate limits and both
bots' existing retry infrastructure, but an easy, zero-cost mitigation.
2 minutes was chosen specifically because it exceeds the original
pullback bot's absolute worst-case retry duration (12 retries × 5 seconds
= 60 seconds), guaranteeing no realistic overlap. Considered and rejected:
sharing a single fetch between both bots via a cache file — this would
violate the "fully separate, zero shared state" requirement above for no
real benefit, since `yfinance` is free and staggering already solves the
actual concern.

### Lesson: A Crontab Edit Applied via `crontab -e` Can Silently Fail to Fully Save
During deployment, an edit intended to remove old, inconsistently-styled
duplicate cron lines for the new bot did not fully take — `crontab -l`
showed the corrected version, but the *running* cron daemon continued
executing an old, duplicate line for several minutes (confirmed via
`journalctl`/`systemctl status cron` showing two different command strings
firing at the identical minute) until a `RELOAD (crontabs/root)` event
finally caught up. Separately, a raw-text `crontab -e` paste was found to
have merged two lines with zero separating newline
(`...2>&1CRON_TZ=America/Toronto`), silently breaking the `CRON_TZ`
declaration for the entire file, not just the new lines. **Fix pattern
that resolved both:** rebuild the entire crontab from a clean heredoc file
and load it via `crontab /tmp/new_crontab.txt` (not interactive `crontab -e`
paste) — safer than incremental edits once a file has already shown signs
of corruption. **Verification pattern:** don't trust `crontab -l` alone
after an edit — cross-check against `journalctl`/`systemctl status cron`
for the actual command strings the daemon most recently executed, since
the two can disagree for several minutes after a change.

### Why Duplicated Live Signals Needed Manual Trade-Log Cleanup, Not Just a Cron Fix
The crontab duplication above (before it was caught) caused real signals
to be logged twice, with signal numbers inflating incorrectly (e.g. `1, 3,
5` instead of `1, 2, 3`, since each duplicate pair incremented the
day's counter by 2). Fixing the crontab going forward does not retroactively
fix already-written trade log data — a one-time manual dedup + renumber
pass (keep first occurrence per `(date, bar_time)`, renumber sequentially,
recompute `validated` flag) was required before that day's reconcile job
ran, to avoid reconciling and counting the same real signal twice in
`--summary` output.

---

## Known Gaps — Not Yet Done

Honest list of validated findings or open questions that have not yet been
acted on, so they aren't silently forgotten. (Reconcile Discord
notification for `tqqq_above_open_bot.py` — previously listed here — has
since been built; see [Reconcile Discord Notification](#reconcile-discord-notification--send_reconcile_summary_to_discord)
above. Confirm `--notify` is actually present on the live crontab line
before assuming it's active.)

**Resolved since last update:** the split-adjustment bug (see
[CRITICAL: Split-Adjustment Bug](#critical-split-adjustment-bug-found-and-corrected-second-major-data-bug-in-this-project)
above) is fixed in the backtest pipeline. The live bot itself was never
affected (it trades on current, already-adjusted real-time prices) — only
historical backtest validation was corrupted.

- **Re-validate stop-streak circuit breaker on split-adjustment-corrected
  data** — now HIGHER priority than before, not lower: the real edge is
  ~4.6x smaller than what was believed when this was tested, meaning
  materially less margin to absorb clustered-loss days. Don't assume the
  old ~4x-improvement ratio holds; re-run before deploying.
- **Re-validate EMA confirmation, ORB comparison, and every-bar-alerts
  degradation on corrected data** — all were tested against the old,
  inflated baseline. Directional conclusions (all three hurt/underperform)
  are plausible but unconfirmed at the corrected baseline specifically.
  Volume confirmation is the one exception — already re-checked
  post-correction via a volume-bucket breakdown, conclusion held.
- **Time-of-day effect on the first signal specifically** — scoped
  (backtest cell written) but never run to completion; the split-
  adjustment bug was discovered mid-investigation and took priority. This
  is the legitimate, still-open question behind the noon-cutoff episode
  (added, then explicitly removed for lacking backtest support — see
  [Considered and Removed: Noon Signal Cutoff](#considered-and-removed-noon-signal-cutoff)
  above). Worth running properly on corrected data before considering any
  time-based restriction again.
- **Per-day P&L view for the stop-streak filter** — current validation is
  per-trade win rate/expectancy; a per-day aggregate view was flagged as a
  more decision-relevant metric (since the filter structurally caps
  losses-per-day but not wins-per-day, per-trade win rate alone can
  overstate real account-level impact) and has not yet been built.
- **VIX correlation with signal outcomes** — not yet tested, and the two
  "rough live sessions" cited as motivation (2026-07-14, 2026-07-16) were
  evaluated against the old, inflated expectation of what a normal day
  looks like — worth re-framing against the corrected ~52% WR baseline
  before treating those days as unusually bad rather than fairly typical.
  Backtest cell scoped, not yet run.
- **Opening-range pullback to 50 EMA** — backtest cell built, not yet run;
  expected (based on family resemblance to the retracted pullback
  strategy) to underperform above-open, but not yet confirmed.
- **SQQQ below-open mirror strategy** — SQQQ 1-min Databento data not yet
  pulled; core above-open hypothesis not yet tested on the downside/SQQQ.
  If pursued, must apply the same split-adjustment fix to SQQQ's own data
  (SQQQ may have its own corporate-action history independent of TQQQ's).
- **QQQ-signal / TQQQ-execution hybrid strategy** — considered: compute
  the above-open condition on QQQ (unleveraged, cleaner signal, no
  decay/rebalancing noise) while still executing and measuring P&L on
  TQQQ. Plausible mechanism for a real edge (QQQ's price action may be a
  more honest read of direction than TQQQ's own leverage-distorted
  series) — not yet tested; would need QQQ's own 1-min Databento data.
- **`tqqq_intraday_bot.py`'s ultimate fate** — remains deployed but
  unvalidated; no decision yet made on whether to retire it, attempt a
  genuinely different signal concept for it, or leave it running
  alongside the above-open bot as-is.
- **`tqqq_above_open_bot.log` `.gitignore` entry** — flagged as needed
  (matching the existing `tqqq_intraday_bot.log` pattern) but not
  confirmed committed.

---

## Emergency Procedures

### Pause a Single Bot
```bash
crontab -e
# Add # before the bot's cron line(s) to comment them out
# Ctrl+X → Y → Enter to save
```

### Pause the TQQQ Buy Ladder Bot (systemd, not cron — see its own section)
```bash
systemctl stop tqqq-ladder-bot     # stops it; survives until re-started
systemctl disable tqqq-ladder-bot  # also stops it auto-starting on reboot
# To resume:
systemctl enable --now tqqq-ladder-bot
```

### Pause the Event Calendar Bot
```bash
crontab -e
# Comment out the `0 7 * * 1-5 event_calendar_bot.py` line
```
Other bots' calls into `event_flags.py` degrade safely if `event_flags.json`
goes stale or missing (see its own section above) — pausing this one doesn't
crash anything downstream, it just means every flag reads as "no event."

### Pause All Bots
```bash
crontab -r   # removes entire crontab — use with caution
# To restore: re-run the crontab setup heredoc from this README
```

### Bot Producing Wrong Signals — Roll Back
```bash
cd /root/Stock-bot
git log --oneline -10          # find the last good commit hash
git checkout COMMIT_HASH -- tqqq_intraday_bot.py  # restore specific file
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
`groupby("date")` resets the cumulative sums each trading session — VWAP is
meant to measure activity from that day's open forward, not accumulate
across multiple days.

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

#### Heartbeat Logging
```python
def write_heartbeat():
    with open(HEARTBEAT_PATH, "a") as _hb:
        _hb.write(f"[{now.strftime('%Y-%m-%d %H:%M:%S ET')}] tqqq_intraday_bot OK\n")
```
Written to the same shared `/root/logs/heartbeat.log` used by the other bots,
using the identical `bot_name OK` format — one file to `tail` for a health
check across the whole VPS. Called on every normal-mode run (including
early-return skips like "outside trading window" or "no data"), but
deliberately **not** called during `--reconcile` (a `not args.reconcile`
gate) and **not** called if the run crashes — a stale heartbeat during
market hours is itself a secondary "something's wrong" signal, on top of
the direct Discord crash alert below.

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

#### Retry Logic — Yahoo Publication Lag
`tqqq_intraday_bot.py` retries its fetch if the latest available bar isn't
fresh enough yet, rather than proceeding on stale data or failing outright:
```python
MAX_RETRIES  = 12   # 12 x 5 seconds = 60 second max wait
WAIT_SECONDS = 5

closed_minute = (now.minute // 15) * 15
# -1 minute: see "Off-By-One Fix" below
expected_bar_time = now.replace(minute=closed_minute, second=0, microsecond=0) - timedelta(minutes=1)

for attempt in range(MAX_RETRIES):
    candidate = yf.download(SYMBOL, period="5d", interval="1m", ...)
    last_bar = candidate.index[-1]
    if last_bar >= expected_bar_time:
        raw = candidate
        break
    time.sleep(WAIT_SECONDS)
```

**Off-By-One Fix — Why the `-1 minute` Is Required.** `tqqq_intraday_bot.py`
fetches **1-min** bars and resamples them itself (required for true VWAP —
see above), rather than fetching native 15-min bars directly. This matters
for the retry loop's timing target — the first version of this formula
(without the `-1 minute`) was tried and caught as a bug before shipping:

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

This `-1 minute` adjustment is specific to fetching 1-min data and comparing
against a 15-min-aligned target — a bot that fetches native 15-min bars
directly wouldn't need it, since the bar being checked is already the thing
being waited on, with no resampling-driven offset to correct for.

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
git add tqqq_gate_blocks.json ... 2>/dev/null
```
The `2>/dev/null` suppresses the "pathspec did not match any files" error when
a JSON file doesn't exist yet (e.g. `tqqq_gate_blocks.json` before the
first rejection is logged). The script continues normally — the file will
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
yfinance>=1.4.1    ← market data (15-min bars, daily bars, live price);
                      also used by event_calendar_bot.py for earnings dates
pandas>=2.2.0      ← DataFrame operations, groupby for VWAP reset
numpy>=1.26.0      ← required by pandas and ta
ta>=0.11.0         ← technical indicators (RSI, EMA, ATR, MACD, BB)
                      replaces pandas_ta which caused segfault (exit code 139)
requests>=2.31.0   ← Discord webhook HTTP POST; also used for FRED API calls
                      (event_calendar_bot.py)
pytz>=2024.1       ← timezone handling (US/Eastern for ET conversion)
alpaca-py>=0.29.0  ← legacy (Alpaca removed, kept for potential future use)
lxml               ← required by yfinance's Ticker.get_earnings_dates()
                      (event_calendar_bot.py only — parses an HTML table
                      under the hood; not needed by any other bot here)
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
├── tqqq_bot.py                 ← TQQQ mean-reversion swing bot
├── tqqq_intraday_bot.py        ← TQQQ momentum pullback intraday bot
│                                  ⚠️ VALIDATED EDGE RETRACTED — see status
│                                  warning at top of this README. No
│                                  standalone backtest .py — validated in a
│                                  Google Colab notebook, not committed here.
├── tqqq_above_open_bot.py      ← TQQQ above-own-open momentum bot
│                                  ✅ CURRENT RECOMMENDED TQQQ INTRADAY
│                                  STRATEGY. Fully separate from
│                                  tqqq_intraday_bot.py — no shared state.
│                                  Also no standalone backtest .py — same
│                                  Colab notebook lineage, corrected
│                                  methodology from the start.
├── .github/workflows/
│   └── tqqq_timer.yml          ← workflow_dispatch ONLY (schedule disabled)
│                                  (neither TQQQ intraday bot has a
│                                  corresponding workflow file — VPS cron
│                                  only, consistent with "Why VPS over
│                                  GitHub Actions" above)
├── trade_log.json
├── tqqq_gate_blocks.json
├── tqqq_intraday_trade_log.json
├── tqqq_intraday_rejections.jsonl
├── tqqq_intraday_near_miss_outcomes.jsonl
├── tqqq_above_open_trade_log.json
├── earnings_cache.json
├── event_calendar_bot.py       ← daily producer, writes event_flags.json
│                                  (FOMC/OpEx/CPI/PCE/NFP/top-holding
│                                  earnings) — see its own dedicated
│                                  section above
├── event_flags.py               ← shared read-only consumer helper,
│                                  imported by any bot; a copy also
│                                  lives in tqqq_ladder_bot/ (see below)
└── tqqq_ladder_bot/             ← separate tool, see its own dedicated
    │                              section above. NOT on cron — runs as
    │                              its own systemd service, own venv.
    ├── ladder_core.py
    ├── tqqq_buy_ladder_bot.py
    ├── alpaca_data.py             ← NEW (Session 3) -- Alpaca wrapper for
    │                                 fetch_daily_bars, see Primary Data
    │                                 Source above
    ├── backtest_ladder.py        ← Colab-run only, not deployed on VPS
    ├── requirements.txt          ← pinned deps, see its own section above
    ├── ladder_log.jsonl           ← tracked in git (pushed, not gitignored)
    ├── position_state.json        ← NOT yet gitignored -- should be, flagged
    │                                 as a to-do, not yet done
    ├── intraday_pullback_analysis.py   ← Colab-run only
    ├── intraday_entry_backtest.py      ← Colab-run only
    ├── .env                       ← gitignored (DISCORD_BOT_TOKEN,
    │                                 DISCORD_ALERT_CHANNEL_ID,
    │                                 ALPACA_API_KEY, ALPACA_SECRET_KEY, etc.)
    └── venv/                      ← gitignored, dedicated virtual env
```

---

*Last updated: September 2026 (Session 7) — Retired SOXL entirely:
`soxl_bot.py` and `soxl_intraday_bot.py` removed from the live crontab
(both were already `#`-commented-out/paused, so no behavior change to
anything actually running) and all SOXL content extracted out of this
document into a separate `README_SOXL.md` archive, rather than deleted
outright — both strategies had real, validated backtests (5-year
walk-forward swing, 60-day intraday) worth preserving as a reference in
case a similar leveraged-ETF strategy is ever revisited. Removed: 14
dedicated sections (bot descriptions, signal paths, backtest results,
gate block fields, monthly review guides, two Architecture Decisions
entries, two full Key Implementation Details sections), all SOXL rows
from the Bot Summaries/log-files/JSON-data tables, the SOXL cron block,
SOXL-specific troubleshooting commands, and SOXL file-tree entries
(including a now-orphaned `mplfinance` dependency line, justified only
by the removed `soxl_bot.py`). Separately reworded — not deleted, since
these describe currently-active TQQQ bots' own real design decisions —
every place SOXL was cited as precedent/lineage rather than being the
actual subject: the SPY holiday-probe pattern, the retry-logic/off-by-
one-bug fix, the heartbeat-logging convention, and the TQQQ scoring-
system rationale, each now explained on its own terms. Caught and fixed
two things a simple find-and-delete would have missed: a "Why Separate
Gate Log Files" section whose entire premise (comparing several files)
became moot with only one file left after SOXL's rows were removed, and
a broken internal cross-reference link whose anchor target no longer
existed after the section it pointed to was renamed mid-edit.

*Previously: September 2026 (Session 6) — Split the dip-alert's price
source by regular-hours status: Alpaca during 9:30am-4:00pm ET (real
liquidity, reduces yfinance load), yfinance outside it (unchanged,
same extended-hours-liquidity reasoning as before). Precautionary fix,
not a reaction to a confirmed incident -- driven by volume math (~162
yfinance calls/day from this one feature, ~3.5x the only proven-safe
precedent on this VPS) and the fact all six bots share one IP, so a
block wouldn't just silence this alert. Fixed a real, self-identified
risk the split itself introduces: comparing prices across two different
venues at the 9:30am/4pm boundary could produce a false dip or false
high from quote differences alone, not real movement -- solved via
source-change detection that resets the running high exactly like a
restart, logged distinctly so it's never confused with a real price
move.

*Previously: September 2026 (Session 5) — Found and adopted a real,
well-reasoned correction via a direct file comparison against a version
built outside this conversation thread: `is_dip_alert_window()`'s end
time was 4:00pm, changed to 8:00pm to actually match
`fetch_live_price_extended_hours`'s real coverage (which already fetches
with `prepost=True`, covering standard after-hours to ~8pm) — the 4pm
version stopped watching for dips exactly when after-hours trading
starts, despite the underlying fetch already being capable of seeing it.
Also fixed a stale module-level docstring (`Requires:`/`Env vars:`)
present in both this conversation's copy and the externally-modified one
— neither had been updated since the Alpaca migration or the dip-alert
env vars were added; now lists `alpaca-py` and all four newer `.env`
keys (`DISCORD_ALERT_CHANNEL_ID`, `DIP_ALERT_THRESHOLD_PCT`,
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`).

*Previously: September 2026 (Session 4) — First real deployment of the
dip-alert polling loop (post-Alpaca-migration) confirmed live via
`journalctl -f`, but also surfaced a real observability gap: the loop was
completely silent on every normal cycle, and `discord.ext.tasks` loops
silently stop forever on any unhandled exception unless an error handler
is registered (none existed). Fixed both: an `INFO` heartbeat line on
every branch of every cycle, and a `@check_dip_alert.error` handler so a
crash is loud instead of a silent, permanent stop. Also found and fixed,
by direct code review rather than an incident: `channel.send()` for the
actual alert wasn't wrapped in try/except, so a delivery failure would
have looked identical in the logs to a real success — now produces a
distinct `ALERT SENT` or `ALERT DECIDED BUT SEND FAILED` line. Raised,
not yet decided: whether fired alerts deserve their own persistent audit
trail the way `/buyfilled` has `ladder_log.jsonl` — added to Known Gaps.

*Previously: September 2026 (Session 3) — Migrated `tqqq_buy_ladder_bot.py`'s
`fetch_daily_bars` (feeds ATR%/RSI/regime/swing-lows/rolling-extremes)
from `yfinance` to Alpaca (IEX feed), reusing a wrapper (`alpaca_data.py`)
originally built for a separate, not-yet-deployed position-tracker bot
elsewhere in this suite. Real, measured justification, not a guess: a
5-day/1-min TQQQ diff test found close-price agreement excellent (median
0.014%, 95th pct 0.065%) but IEX volume at only ~2% of `yfinance`'s
(matching IEX's known market share) — daily high/low accuracy was never
separately measured, an honestly-carried-forward open caveat. Three
things deliberately NOT migrated, each with its own reasoning: extended-
hours price (IEX's thin extended-hours liquidity would mean more
rejected/stale reads), the SPY holiday probe (consistency with the rest
of the bot suite), and — new this session — `/marketcheck`'s volume-ratio
field, which now sources from a dedicated `fetch_qqq_volume_series_yfinance()`
rather than inherit the (now Alpaca/IEX) `qqq_df`, since the
ratio-cancels-the-IEX-share-out theory is untested. `ladder_core.py`
needed zero changes — the volume fix was handled entirely by wrapping the
yfinance series into the DataFrame shape `compute_volume_ratio()` already
expected. Corrected a real misattribution while merging this in: the
migration's original justification (encountered via a separate, external
conversation) had cited a rate-limit/5-min-polling argument that,
checked directly, does not actually apply to this bot's `fetch_daily_bars`
(called once per command, not from a tight loop) — the real justification
is the general "undocumented scraper, no SLA" reliability argument,
which applies regardless of call frequency. Also caught and fixed a real
documentation/environment drift in the same session: `alpaca-py` was
reportedly already installed and working on the VPS before
`requirements.txt` on GitHub was updated to declare it — confirmed via a
direct GitHub screenshot, not assumed — corrected before it could cause
a future fresh-install failure.

*Previously: September 2026 (Session 2) — Ladder bot: built position
tracking (`position_state.json`, `/position` command, persistent Clear
Position button), fill confirmation via Discord buttons/modals (Filled at
Target / Custom Fill / Skip Dip, feeding a shared `build_buyfilled_response()`
so nothing duplicates `/buyfilled`'s own logic), and a fully automatic
dip-entry alert (`check_dip_alert`, a `tasks.loop(minutes=5)` background
task inside the bot itself — required, not a preference, since interactive
buttons need the bot's own live Gateway connection, which none of the
cron/webhook bots have). Extended-hours price freshness added for both
`/marketcheck` and `/buyfilled` (was previously showing a frozen
regular-session close after 4pm). Widened the dip-alert's own window to
6:30am ET via a deliberately SEPARATE function from the extended-hours
one, to avoid regressing the other. Found and fixed 4 real bugs: `NaN`
silently propagating through every calculation instead of failing loudly;
`ladder_core.py` regressing on-disk without the running process noticing
(a second, distinct file-integrity incident from the GitHub one);
`interval="1d"` used where `prepost=True` needed a genuine intraday
interval; and the entry-threshold backtest silently missing same-day
dip-then-recover cycles by checking the close instead of the low. Two new
Colab research scripts built (`intraday_pullback_analysis.py`,
`intraday_entry_backtest.py`), the second of which precisely replicates
the live alert's own logic against real 5-minute data — surfaced that the
available ~60-day window overlapped a real, confirmed QQQ correction,
a caution about regime-dependence rather than a verdict on the strategy.
Explicitly deferred, not built: the ATR "re-anchor" volatility override,
user-controlled alert snooze, and overnight (Blue Ocean ATS) coverage —
confirmed infeasible on `yfinance`, confirmed feasible but paid on
Alpaca. Also corrected two now-false claims already in this document
(the event_flags-into-ladder-bot integration is not actually present in
the deployed code) and flagged one issue outside the ladder bot entirely
(`tqqq_bot.py`'s cron schedule appears to be off by 4 hours from its own
documented hours — unconfirmed, not yet fixed, needs its own follow-up).

*Previously: September 2026 — Added `event_calendar_bot.py` +
`event_flags.py`: a daily producer/consumer pattern flagging FOMC, CPI/
PCE/NFP, monthly OpEx/quad witching, and top-QQQ-holding earnings, so
other bots can check "is today a high-impact day" without each
re-implementing calendar fetching. Live-tested and rejected yfinance's
aggregated economic-events calendar for macro dates (zero US entries in
a 45-day sample) in favor of FRED's official API; kept yfinance for
earnings dates specifically, which tested fine. Fixed three reliability
issues along the way: non-atomic JSON writes (now atomic via temp-file +
`os.replace()`), VPS-local-clock date drift (now anchored to
`America/New_York` explicitly), and full-cache wipe on a single failed
fetch (now preserved per-category). A `tqqq_buy_ladder_bot.py`
integration was attempted at one point (`ladder_log.jsonl`'s
`event_flags_date`/`event_flags_summary` fields are a remnant of it) but
is confirmed NOT present in the current deployed bot — see its own
section above for the full correction.

*Previously: July 2026 — MAJOR: found and fixed a second critical data
bug (first was the pullback bot's lookahead bias). TQQQ's raw Databento
data was never split-adjusted for two confirmed 2-for-1 splits
(2022-01-13, 2025-11-20) baked into the ORIGINAL backtest CSV since the
very first above-open validation. Corrected result: 858 trades, 52.0% WR,
$0.0380/share expectancy (was reported as 65.4% WR, $0.1767 exp — overstated
by ~4.6x). This is a CORRECTION, not a retraction — the edge remains real
and out-of-sample consistent (every year 2022-2026 positive), just far
more modest than believed. Re-checked volume confirmation specifically on
corrected data (conclusion held: no clean filter). Reverted
tqqq_above_open_bot.py from "every qualifying bar fires" back to
"first-signal-of-day only", restoring exact alignment with what the
backtest has always measured. Briefly added, then explicitly removed, a
noon signal cutoff after recognizing it was a genuinely new, untested
restriction (unlike the first-signal-only reversion, which restores
validated behavior) layered on top of a correct change. Flagged
every downstream test in this document (EMA, ORB, stop-streak breaker,
every-bar degradation, VIX motivation) as needing re-verification against
the corrected baseline — directional conclusions likely hold, magnitudes
do not. Elevated the stop-streak breaker's re-validation to higher
priority given the smaller real edge has less margin for clustered
losses. Logged a new candidate idea (QQQ-signal/TQQQ-execution hybrid,
motivated by QQQ being a cleaner, less leverage-distorted price series)
to Known Gaps.*
*VPS: Servarica V3 KVM Slim Slice 2, <VPS_IP>, Montreal*
*Python: 3.10.12 | Ubuntu: 22.04 LTS*
