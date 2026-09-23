# tqqq_buy_ladder_bot.py — TQQQ Averaging-Down Ladder

Discord slash-command tool (not a passive cron alert bot) that computes
averaging-down buy levels for an existing TQQQ position, tracks whether
a position is currently held, and runs an automatic dip-entry alert.
Runs as its own systemd service, separate from every other bot.

**For the other TQQQ bots** (`tqqq_bot.py` swing,
`tqqq_intraday_bot.py` pullback, `tqqq_above_open_bot.py`
above-open, and `event_calendar_bot.py`), see
[`README_TQQQ_SWING_INTRADAY.md`](./README_TQQQ_SWING_INTRADAY.md) —
this document now covers only the ladder bot.

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

## Architecture Decisions (Ladder Bot)

### Why the TQQQ Buy Ladder Bot Runs as Systemd, Not Cron
Every other bot in this system starts, runs one check, and exits — a model
that fits cron perfectly. The ladder bot instead must stay continuously
connected to Discord's gateway to receive slash-command interactions in
real time; a process that starts and stops on a schedule is the wrong
model for that. See its own dedicated section above for the full
`systemd` unit and reasoning.

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

## Emergency Procedures (Ladder Bot)

### Pause the TQQQ Buy Ladder Bot (systemd, not cron — see its own section)
```bash
systemctl stop tqqq-ladder-bot     # stops it; survives until re-started
systemctl disable tqqq-ladder-bot  # also stops it auto-starting on reboot
# To resume:
systemctl enable --now tqqq-ladder-bot
```

## Files Reference (Ladder Bot)

```
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

*Last updated: September 2026 (Session 8) — Split this document in two:
`README.md` now covers only `tqqq_buy_ladder_bot.py`; everything about
the three cron signal bots (`tqqq_bot.py` swing, `tqqq_intraday_bot.py`
pullback, `tqqq_above_open_bot.py` above-open) and `event_calendar_bot.py`
moved to a new `README_TQQQ_SWING_INTRADAY.md`. System Architecture
(VPS/GitHub principles, Primary Data Source) duplicated in both, since
it's genuinely shared context for either document read on its own. Two
sections were mixed (ladder-specific and non-ladder entries interleaved)
and required splitting by entry, not by section: Architecture Decisions
(three ladder entries kept here: buy-ladder-anchors-to-basis, uses-QQQ-
for-structure, runs-as-systemd; the rest moved) and Emergency Procedures
(only "Pause the TQQQ Buy Ladder Bot" kept here). The full changelog
history stays here, in the more actively-developed document, rather than
duplicated in both — the new file points back here for it instead.

*Previously: September 2026 (Session 7) — Retired SOXL entirely:
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
