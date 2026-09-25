"""
tqqq_buy_ladder_bot.py

Discord slash-command bot for the TQQQ averaging-down ladder.

All calculation logic lives in ladder_core.py (imported below) so the
exact same code that gets validated in backtest_ladder.py is what runs
live here -- nothing gets reimplemented between the two.

Design decision: frequency of fill over swing-low confluence (see
backtest_ladder.py's confluence/depth-controlled sections). Levels are
pure QQQ-ATR spacing, anchored to your basis price.

Usage in Discord:
    /buyfilled shares:40 price:74.00

    `price` = your current average cost basis (not today's market price).
    Run this any time you want a fresh read of the next 3 levels -- the
    QQQ structure and ATR% are recalculated live every call, but the
    anchor (`price`) only changes when you actually update it after a
    real fill.

Requires:
    pip install discord.py yfinance alpaca-py pandas numpy python-dotenv

Env vars (.env or exported):
    DISCORD_BOT_TOKEN
    DISCORD_GUILD_ID           (optional, for instant guild-scoped command sync)
    DISCORD_ALERT_CHANNEL_ID   (required for the dip-alert polling task --
                                a background task has no Interaction to
                                derive a channel from, unlike a slash command)
    DIP_ALERT_THRESHOLD_PCT    (optional, default 0.5 -- see
                                intraday_entry_backtest.py for how this
                                value was actually tested, not guessed)
    ALPACA_API_KEY              (required -- fetch_daily_bars is Alpaca-backed
    ALPACA_SECRET_KEY            as of this session; bot fails at import time,
                                before Discord login, if either is missing)
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf  # used ONLY by is_regular_market_hours_live's holiday
                        # probe and fetch_live_price_extended_hours below --
                        # both deliberately kept on yfinance, not migrated
                        # with fetch_daily_bars -- see each function's own
                        # docstring for why
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
import alpaca_data as ad

from ladder_core import (
    compute_atr_pct,
    find_confirmed_swing_lows,
    build_ladder,
    ladder_to_dicts,
    find_support_in_range,
    support_to_dicts,
    locate_support_relative_to_ladder,
    compute_regime_status,
    compute_signed_trend_distance,
    compute_rsi_status,
    compute_volume_ratio,
    compute_rolling_extremes,
    translate_qqq_price_to_tqqq,
    ATR_STEP,
    LEVERAGE_FACTOR,
    SWING_SNAP_TOLERANCE_ATR,
)

# Decision (backed by backtest_ladder.py): optimize for frequency of fill,
# not swing-low confluence. The in-sample confluence penalty didn't
# replicate out-of-sample once depth-controlled, so there's no evidence
# it earns its added complexity -- pure ATR spacing is simpler and was
# already the better fit for a manually-executed ladder. Swing-low
# detection is used ONLY for informational display now (see
# find_support_in_range) -- it never feeds into or alters build_ladder.
USE_CONFLUENCE = False
MAX_SUPPORT_DISPLAY = 3

load_dotenv()

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = os.environ.get("DISCORD_GUILD_ID")
ALERT_CHANNEL_ID = int(os.environ.get("DISCORD_ALERT_CHANNEL_ID", 0))
DIP_ALERT_THRESHOLD_PCT = float(os.environ.get("DIP_ALERT_THRESHOLD_PCT", "0.5"))
# 0.5% default -- matches the value actually tested in
# intraday_entry_backtest.py. Configurable via .env rather than hardcoded,
# so it can be tuned without a code change/redeploy.

LADDER_LOG_PATH = Path(__file__).parent / "ladder_log.jsonl"
POSITION_STATE_PATH = Path(__file__).parent / "position_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buy_ladder_bot")

# Module-level Alpaca client -- created once at import time, reused across
# every fetch_daily_bars call, same pattern as the confirmed-working
# version on the other branch of this bot. Requires ALPACA_API_KEY and
# ALPACA_SECRET_KEY in .env; raises immediately at startup if missing,
# rather than failing confusingly on the first /buyfilled call.
alpaca_client = ad.get_client()


# ---------- Data fetching ----------

def fetch_daily_bars(symbol: str, lookback_days: int = 400) -> pd.DataFrame:
    """Blocking network call -- must be run via asyncio.to_thread from
    inside the Discord event loop.

    Primary source: Alpaca (IEX feed) via alpaca_data.fetch_daily_bars.
    Migrated from yfinance -- real, measured justification, not a guess:
    a 5-day/1-min diff test found close-price agreement excellent
    (median 0.014%, 95th pct 0.065%) -- effectively noise-level for
    ATR%/RSI/regime, all of which key off closes. Daily high/low accuracy
    was NOT separately measured (the diff test covered 1-min bars only)
    -- a real, still-open, honestly-carried-forward caveat: an intraday
    extreme that printed on a venue other than IEX could in principle be
    missed by find_confirmed_swing_lows, which reads the daily low
    column. The underlying justification for the migration itself is the
    general "yfinance is an undocumented scraper, no SLA" reliability
    argument -- NOT a rate-limit/call-volume argument (this function is
    called once per command invocation, not from any tight polling loop;
    an earlier draft of this comment incorrectly attributed the
    migration to a 5-min loop that in fact calls a different function,
    fetch_recent_bars, for a different bot's different purpose).

    Returns the same shape the yfinance version did (columns open/high/
    low/close/volume, DatetimeIndex named "date"), so nothing downstream
    in this file or in ladder_core.py needed to change.

    lookback_days=400: a 200-day SMA needs 200 genuine TRADING days, and
    calendar-day lookback needs headroom over that for weekends/holidays
    -- same reasoning as before the source swap, unchanged by it."""
    df = ad.fetch_daily_bars(alpaca_client, symbol, lookback_days=lookback_days)
    if df["close"].iloc[-1:].isna().any():
        # Same defensive check as the pre-migration yfinance version --
        # kept even though Alpaca is a documented API with a different
        # failure profile than Yahoo's scraped backend. Cheap insurance:
        # if Alpaca ever returns a non-empty frame with an unsettled last
        # row for any reason, this still fails loudly here instead of
        # letting NaN silently propagate through every downstream
        # calculation (ATR, RSI, filter comparisons, which always
        # evaluate False against NaN).
        raise RuntimeError(
            f"{symbol}'s latest bar has no valid price data yet -- try again in a few minutes."
        )
    return df


def fetch_qqq_volume_series_yfinance(lookback_days: int = 30) -> pd.Series:
    """QQQ daily volume, sourced from yfinance specifically -- NOT
    Alpaca. Deliberate, narrow exception to the Alpaca migration above.

    Why: Alpaca's free tier is IEX-only, carrying ~2-3% of real US
    equity volume (confirmed via the same diff test that validated
    close-price accuracy -- volume ratio Alpaca/yfinance measured at
    ~2%, matching IEX's known market share). Close-price accuracy
    surviving that gap does NOT imply volume-based metrics do too --
    untested, so this function exists specifically to keep
    compute_volume_ratio() meaningful rather than silently comparing one
    tiny, single-venue volume figure against itself. Same precautionary
    principle already applied to extended-hours price staying on
    yfinance: don't let Alpaca's known IEX limitation quietly degrade
    something that was never actually measured against it. If the
    IEX-share-cancels-out-of-a-ratio theory is ever validated with its
    own diff test, this exception could be revisited -- not assumed
    safe without that test."""
    df = yf.download(
        "QQQ",
        period=f"{lookback_days}d",
        interval="1d",
        progress=False,
        auto_adjust=True,
        multi_level_index=False,
    )
    if df.empty:
        raise RuntimeError("No yfinance volume data returned for QQQ")
    df.columns = [str(c).lower() for c in df.columns]
    return df["volume"]


def get_market_data() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch QQQ (for structure) and TQQQ (for the live-price filter)."""
    qqq_df = fetch_daily_bars("QQQ")
    tqqq_df = fetch_daily_bars("TQQQ")
    return qqq_df, tqqq_df


def fetch_live_price_extended_hours(symbol: str) -> float:
    """Blocking network call -- must be run via asyncio.to_thread.

    Used ONLY when outside regular market hours (see
    is_regular_market_hours_live below), to get a fresher current price
    than fetch_daily_bars provides -- that function deliberately never
    passes prepost=True, so its last row freezes at the regular-session
    close and silently ignores real after-hours/pre-market movement.

    Scoped narrowly on purpose: this ONLY overrides the "current price"
    display/filter value in the calling command. It does NOT feed
    ATR/RSI/regime/swing-low/rolling-extreme calculations -- those keep
    using fetch_daily_bars's regular-session-only historical data
    unconditionally, so nothing about the backtested indicator behavior
    changes. This is the deliberately more conservative alternative to
    passing prepost=True globally in fetch_daily_bars, which would also
    be a legitimate fix but would let extended-hours moves bleed into
    ATR/RSI/swing-low detection on every fetch, not just the "where is
    price right now" question this function answers.

    interval="5m" (not "1d"): a true intraday interval is the
    universally-documented way prepost actually applies in yfinance --
    daily-interval + prepost is at best unclear, so this avoids relying
    on that combination at all. period="5d" stays generous even at 5m
    granularity, comfortably covering a long holiday weekend."""
    df = yf.download(
        symbol,
        period="5d",
        interval="5m",
        progress=False,
        auto_adjust=True,
        multi_level_index=False,
        prepost=True,
    )
    if df.empty:
        raise RuntimeError(f"No extended-hours data returned for {symbol}")
    df.columns = [str(c).lower() for c in df.columns]
    if df["close"].iloc[-1:].isna().any():
        raise RuntimeError(f"{symbol}'s extended-hours price is not available right now")
    return float(df["close"].iloc[-1])


def get_extended_hours_prices() -> Tuple[float, float]:
    """(qqq_price, tqqq_price), both including pre/post-market activity.
    Callers should wrap this in try/except and fall back to the regular-
    session close on any failure -- this is a best-effort freshness
    upgrade, not something that should ever crash a command outright when
    it fails, since a valid regular-session price is already in hand by
    the time this gets called."""
    return fetch_live_price_extended_hours("QQQ"), fetch_live_price_extended_hours("TQQQ")


_ET = ZoneInfo("America/New_York")


def is_regular_market_hours_live(now: datetime = None) -> bool:
    """Combines two checks, both required to return True:
      1. Is the current time within the 9:30am-4:00pm ET clock window --
         cheap, no network, checked FIRST so nights/weekends short-circuit
         before ever touching the network.
      2. Is today actually a trading day (not a market holiday) -- a live
         1-minute SPY probe, mirroring tqqq_bot.py's is_market_open_today()
         (which itself mirrors soxl_intraday_bot.py) -- reused for
         consistency with that already-proven precedent rather than a
         third, different implementation. No hardcoded holiday calendar:
         infers today's status from whether the data actually exists.

    A plain clock check alone isn't enough for our purpose: it can't tell
    a holiday from a normal trading day. But tqqq_bot.py's probe ALONE
    isn't enough either -- it only answers "did the market open today,"
    not "are we within the 9:30-4 window right now" (at 5:48pm on an
    ordinary trading day, their function alone would still say "market
    opened today," missing the after-hours case entirely).

    Blocking network call (when the clock check passes) -- must be run
    via asyncio.to_thread. Fails OPEN if the SPY probe itself errors,
    matching tqqq_bot.py's exact reasoning -- for our use here, "open"
    just means we'll attempt one extra, already-fallback-protected
    freshness fetch, low cost either way if this guess is wrong."""
    now = (now or datetime.now(_ET)).astimezone(_ET)

    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if not (market_open <= now <= market_close):
        return False

    # Within the clock window -- confirm today isn't a holiday via a live probe.
    try:
        probe = yf.download("SPY", period="1d", interval="1m",
                             auto_adjust=True, progress=False)
        if isinstance(probe.columns, pd.MultiIndex):
            probe.columns = probe.columns.get_level_values(0)
        if probe.empty:
            return False  # holiday -- no data at all today
        last = probe.index[-1]
        if hasattr(last, "tz_convert"):
            last = last.tz_convert(_ET)
        if last.date() < now.date():
            return False  # stale -- no trading has happened today
        return True
    except Exception:
        return True  # fail open, matching tqqq_bot.py


def is_regular_hours_clock_only(now: datetime = None) -> bool:
    """Same 9:30am-4:00pm ET weekday window as is_regular_market_hours_live,
    deliberately WITHOUT its live SPY holiday probe. Exists specifically
    for fetch_dip_alert_price, which runs every cycle of a 5-min polling
    loop -- reusing the full holiday-aware version there would silently
    defeat that function's own stated purpose: is_regular_market_hours_live
    makes its SPY probe call EVERY time it's invoked within the clock
    window, uncached, so gating the Alpaca/yfinance switch on it would
    still cost one yfinance call per regular-hours cycle, just relabeled
    from "fetch TQQQ price" to "probe SPY for a holiday" rather than
    actually eliminated. No network at all here -- pure clock check,
    genuinely free to call every cycle.

    Same low-stakes reasoning already applied to is_dip_alert_window's
    own pure-clock design: this can't tell an actual holiday from a
    normal trading day, but the cost of guessing wrong is low and
    already handled downstream -- fetch_dip_alert_price's Alpaca branch
    simply raises (no real trades to find), caught by the caller's
    existing try/except, which skips that cycle with a warning. Not a
    replacement for is_regular_market_hours_live -- that function's own
    holiday awareness still matters for /marketcheck and /buyfilled,
    which call it once per command, not once every 5 minutes all day."""
    now = (now or datetime.now(_ET)).astimezone(_ET)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


def is_dip_alert_window(now: datetime = None) -> bool:
    """A DIFFERENT, wider window than is_regular_market_hours_live -- 6:30am
    to 8:00pm ET, covering standard pre-market through standard post-market
    close. Used ONLY by check_dip_alert's polling gate.

    Deliberately NOT the same function as is_regular_market_hours_live,
    even though it would be tempting to just widen that one -- doing so
    would break /marketcheck and /buyfilled: they use
    is_regular_market_hours_live specifically to decide whether to bother
    with the extended-hours fetch. If that function considered 6:30am-8pm
    "regular hours," those commands would wrongly skip the fresher prepost
    fetch during exactly the window they need it most, showing yesterday's
    stale close instead. Two different questions ("is this pre/post-market
    worth checking for a dip" vs "should I bother re-checking price
    freshness") need two different answers.

    8:00pm ET end, not 4:00pm as an earlier version of this function had it:
    fetch_live_price_extended_hours already fetches with prepost=True,
    which covers standard after-hours trading up to ~8pm -- a 4pm cutoff
    meant the alert stopped watching exactly when after-hours trading
    STARTS, even though the fetch it relies on was already capable of
    seeing that activity. 8pm is where that fetch's own real coverage
    actually runs out too, so this aligns the gate with what the
    underlying data can actually see, not an arbitrary earlier stop.
    Widening past 8pm would just mean polling on a frozen price with
    dip_pct stuck at 0 all night -- the separate Blue Ocean ATS overnight
    session (8pm-4am ET) isn't visible to a plain yfinance prepost=True
    fetch at all. Overnight is a genuinely different, currently-unbuilt
    problem (see Known Gaps) -- Alpaca does offer that session, but only
    as a paid, "contact sales," 15-min-delayed add-on, deliberately not
    adopted.

    Deliberately a PURE CLOCK CHECK, no SPY holiday probe (unlike
    is_regular_market_hours_live) -- same low-stakes reasoning already
    applied once before: worst case on an actual holiday is a few wasted,
    harmless polls (price stays frozen, running_high stays flat, dip_pct
    stays ~0, nothing fires) rather than a real risk worth a second
    network-probing mechanism for.

    check_dip_alert's own price fetch (fetch_live_price_extended_hours)
    already uses prepost=True unconditionally, so no change was needed
    there -- only this gate needed to widen."""
    now = (now or datetime.now(_ET)).astimezone(_ET)
    if now.weekday() >= 5:
        return False
    window_start = now.replace(hour=6, minute=30, second=0, microsecond=0)
    window_end = now.replace(hour=20, minute=0, second=0, microsecond=0)
    return window_start <= now <= window_end


def enrich_ladder_for_log(ladder: list, current_tqqq_price: float, leverage: float) -> list:
    """Adds the TRUE distance-from-current-price alongside each level's
    existing structural (distance-from-basis) fields, so historical log
    entries aren't subject to the same misleading-label issue the Discord
    embed had -- see buyfilled()'s comment for the full explanation.
    Does not modify ladder or LadderLevel -- purely additive, for logging."""
    enriched = []
    for d in ladder_to_dicts(ladder):
        tqqq_pct_from_now = (current_tqqq_price - d["price"]) / current_tqqq_price * 100
        d["tqqq_pct_from_current"] = round(tqqq_pct_from_now, 2)
        d["qqq_pct_from_current"] = round(tqqq_pct_from_now / leverage, 2)
        enriched.append(d)
    return enriched


def log_ladder(shares: float, basis_price: float, current_tqqq_price: float,
                qqq_close: float, qqq_atr_pct: float, market_data_last_date: str,
                ladder: list, support_levels: list, regime) -> dict:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "shares": shares,
        "tqqq_basis_price": basis_price,
        "tqqq_current_price": current_tqqq_price,
        "qqq_close": qqq_close,
        "qqq_atr_pct": round(qqq_atr_pct * 100, 2),
        "market_data_last_date": market_data_last_date,
        "atr_step": ATR_STEP,          # pulled from ladder_core, never redeclared here --
        "leverage_factor": LEVERAGE_FACTOR,  # keeps this log truthful if either is ever tuned
        "regime_below_200sma": regime.below_sma,
        "regime_sma_200": regime.sma_200,
        "regime_pct_below": regime.pct_below,
        "ladder": enrich_ladder_for_log(ladder, current_tqqq_price, LEVERAGE_FACTOR),
        "support_displayed": support_to_dicts(support_levels),
    }
    with open(LADDER_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


# ---------- Position state (for the "no open position" dip alert) ----------
#
# Solves a real gap: the bot has no way to observe a sale happening --
# by design, execution is entirely manual with no brokerage connection
# anywhere in this system. ladder_log.jsonl's last entry is NOT a
# reliable signal for "am I flat" -- it only ever records buys, so after
# a real sale it would keep showing your old shares/basis indefinitely,
# with nothing to ever correct it. This tiny state file exists
# specifically to let the trader tell the bot "I'm flat now," since
# nothing else in the system ever could.

def read_position_state() -> dict:
    """Returns {"shares", "price", "updated_at"} or None if no position
    is currently tracked (i.e. you're flat)."""
    if not POSITION_STATE_PATH.exists():
        return None
    try:
        return json.loads(POSITION_STATE_PATH.read_text())
    except Exception:
        return None


def write_position_state(shares: float, price: float) -> None:
    """Called automatically by /buyfilled on every successful call --
    shares/price there always represent your CURRENT total position, so
    simply overwriting is correct, no merging needed."""
    POSITION_STATE_PATH.write_text(json.dumps({
        "shares": shares,
        "price": price,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))


def clear_position_state() -> None:
    """Called only by the explicit 'Clear Position' button -- the bot
    never clears this on its own (e.g. on a price target), since it has
    no way to verify a sale actually happened. See the module note above.

    Also resets _running_high and _last_price_source back to None --
    without this, a real, confirmed gap: check_dip_alert is completely
    frozen the entire time a position is tracked (its own gate returns
    immediately whenever read_position_state() is not None), so it never
    observes anything that happens while you're holding -- including any
    new high reached during the hold itself. Clearing only the position
    file left _running_high sitting at whatever stale value predates the
    hold (potentially the high from BEFORE you even bought, hours or days
    earlier), so the very next poll after clearing could immediately fire
    a false "dip" purely by comparing today's current price against that
    stale number -- not a real pullback from anything that's happened
    since you went flat.

    Resetting to None here reuses the bot's own existing cold-start path
    (`if _running_high is None or current_price > _running_high:` in
    check_dip_alert) rather than adding new logic: the next poll simply
    seeds _running_high from whatever price it sees THEN, and ratchets up
    normally from there -- exactly the right reference point for
    re-entry monitoring, which is what this alert is actually for
    ("tell me when price pulls back so I can consider buying again"),
    not a historical high-of-day figure that may predate or have nothing
    to do with your next re-entry decision.

    Deliberately does NOT reseed from seed_running_high_from_today() the
    way a bot restart does -- that function exists to recover a real
    high the bot already should have been watching for but momentarily
    lost (a restart). Here, the bot deliberately wasn't watching for a
    market-wide high during the hold (that's not its job while a
    position is tracked) -- a cold start from now is not a bug being
    patched, it's the correct semantics of "start watching again."""
    if POSITION_STATE_PATH.exists():
        POSITION_STATE_PATH.unlink()
    global _running_high, _last_price_source
    _running_high = None
    _last_price_source = None


def compute_blended_average(existing_shares: float, existing_price: float,
                              fill_shares: float, fill_price: float) -> tuple:
    """(new_total_shares, new_blended_avg). Standard weighted-average
    formula -- same math already used by the target_avg calculator.
    Works correctly even from a fresh flat position (existing_shares=0):
    the blended average simply reduces to fill_price."""
    new_total = existing_shares + fill_shares
    new_avg = (existing_shares * existing_price + fill_shares * fill_price) / new_total
    return new_total, new_avg


# ---------- Discord bot ----------

intents = discord.Intents.default()


class FilledSharesModal(discord.ui.Modal, title="Confirm fill"):
    """One field only -- price is already known (the alert's own target
    price), we just need how many shares to correctly blend the new
    average. Triggered by 'Filled at Target'."""

    shares_input = discord.ui.TextInput(label="Shares bought", placeholder="e.g. 10", required=True)

    def __init__(self, target_price: float):
        super().__init__()
        self.target_price = target_price

    async def on_submit(self, interaction: discord.Interaction):
        await handle_fill_confirmation(interaction, self.target_price, self.shares_input.value)


class CustomFillModal(discord.ui.Modal, title="Record custom fill"):
    """Both fields -- neither price nor shares can be assumed here, since
    this is specifically for when the actual fill differed from the
    alerted target price. Triggered by 'Custom Fill'."""

    shares_input = discord.ui.TextInput(label="Shares bought", placeholder="e.g. 10", required=True)
    price_input = discord.ui.TextInput(label="Actual fill price", placeholder="e.g. 68.42", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await handle_fill_confirmation(interaction, self.price_input.value, self.shares_input.value)


async def handle_fill_confirmation(interaction: discord.Interaction, price_str, shares_str: str):
    """Shared by both modals -- parses input, blends the average against
    whatever's currently tracked in position_state.json (0/None if
    genuinely flat, in which case the blend correctly reduces to just the
    fill price), then calls build_buyfilled_response -- the SAME function
    /buyfilled itself uses, so the ladder posted here is never a
    second, divergent implementation."""
    await interaction.response.defer(thinking=True)
    try:
        fill_price = float(price_str)
        fill_shares = float(shares_str)
    except ValueError:
        await interaction.followup.send("Shares and price must both be numbers -- please try again.")
        return
    if fill_shares <= 0 or fill_price <= 0:
        await interaction.followup.send("Shares and price must both be positive numbers.")
        return

    existing = read_position_state()
    existing_shares = existing["shares"] if existing else 0.0
    existing_price = existing["price"] if existing else 0.0

    new_shares, new_avg = compute_blended_average(existing_shares, existing_price, fill_shares, fill_price)

    try:
        embed = await build_buyfilled_response(new_shares, new_avg)
        await interaction.followup.send(
            content=f"✅ Fill recorded: {fill_shares:g} sh @ ${fill_price:.2f} "
                    f"(new position: {new_shares:g} sh @ ${new_avg:.2f} avg)",
            embed=embed,
        )
    except ValueError as e:
        await interaction.followup.send(str(e))
    except Exception as e:
        log.exception("handle_fill_confirmation failed")
        await interaction.followup.send(f"Error computing ladder: {e}")


class DipAlertResponseView(discord.ui.View):
    """Attached to each dip alert. NOT a persistent view (unlike
    ClearPositionView) -- target_price is specific to one particular
    alert instance, so a fixed custom_id can't route back to the right
    context after a bot restart the way it can for Clear Position's
    single, global action. The 5-min polling task that posts these
    alerts (check_dip_alert, below) is built and live -- this view is
    attached to its real alerts, not just standing ready for one."""

    def __init__(self, target_price: float):
        super().__init__(timeout=None)
        self.target_price = target_price

    @discord.ui.button(label="Filled at Target", style=discord.ButtonStyle.success)
    async def filled_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FilledSharesModal(self.target_price))

    @discord.ui.button(label="Custom Fill", style=discord.ButtonStyle.primary)
    async def custom_fill_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CustomFillModal())

    @discord.ui.button(label="Skip Dip", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # No state change at all -- position_state.json untouched, so the
        # polling loop's gate check keeps watching for the next dip
        # exactly as before. Just acknowledges the alert visually.
        await interaction.response.edit_message(content="⏭️ Skipped -- still watching for the next dip.", view=None)


class ClearPositionView(discord.ui.View):
    """Persistent view (timeout=None + a fixed custom_id) so the button
    keeps working even hours or days after the /position message was
    sent, and survives bot restarts -- a default view times out after a
    few minutes, which would silently break this the first time you
    actually needed it days later. Must be registered once via
    client.add_view() in setup_hook for the "survives restarts" part to
    actually hold -- see LadderBotClient.setup_hook below."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Clear Position (I've sold)", style=discord.ButtonStyle.danger,
                        custom_id="clear_position_button")
    async def clear_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        clear_position_state()
        await interaction.response.edit_message(
            content="✅ Position cleared -- you're now tracked as flat.",
            embed=None,
            view=None,
        )


class LadderBotClient(discord.Client):
    """Custom Client subclass so slash-command sync happens exactly once,
    in setup_hook (called once before the first connection) -- NOT in
    on_ready, which Discord can fire multiple times over a long-running
    bot's life (initial connect AND every subsequent reconnect/resume).
    Repeated tree.sync() calls on every reconnect risk hitting Discord's
    rate limits for no benefit, since the command set doesn't change
    between reconnects."""

    async def setup_hook(self):
        # Re-register the persistent view's callback here too -- required
        # once per process start so a button click still works correctly
        # even if the bot restarted since the /position message was sent.
        self.add_view(ClearPositionView())

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        log.info("Slash commands synced")


client = LadderBotClient(intents=intents)
tree = app_commands.CommandTree(client)

# ---------- Dip-entry polling task (no open position, edge-based, NO anti-spam) ----------
#
# Deliberately fires on EVERY poll where price sits below the threshold
# below the running high -- no armed/re-arm suppression. Considered and
# explicitly rejected: an armed/re-arm-on-recovery design (fire once, go
# quiet until price recovers above the threshold line) was proposed, but
# has a real, confirmed failure case -- if price keeps falling WITHOUT
# recovering in between, a genuinely deeper, better entry than the one
# you missed would never get its own alert. Given the actual plan is "get
# in at a known retraceable threshold, average down further with the
# ladder if needed," never silently missing a better entry outweighs the
# real, accepted cost of repeated alerts during a sustained dip. A
# user-controlled snooze (not automatic bot-side suppression) is the
# planned way to address alert frequency later, so the choice of when to
# go quiet stays with the trader, not a guess baked into the bot.
#
# running_high is in-memory only (module-level, not persisted) --
# resets on a bot restart. Deliberate: unlike position_state.json (real
# financial data, must survive restarts), losing this just means the
# tracker re-initializes from whatever price it next sees -- a minor,
# self-correcting inconvenience, not a real loss.
_running_high = None

# Tracks which source fed the last accepted price -- "alpaca" or
# "yfinance". Needed because the price source now switches at the
# 9:30am/4:00pm regular-hours boundary (see fetch_dip_alert_price below)
# -- without this, comparing a fresh Alpaca price against a running_high
# set from yfinance (or vice versa) risks a FALSE dip or false new-high
# purely from two different venues quoting slightly different prices at
# the same moment, not real market movement.
_last_price_source = None


def seed_running_high_from_today() -> float:
    """Best-effort recovery of today's true running high after a bot
    restart -- without this, _running_high starts at None and silently
    re-anchors to whatever price the FIRST post-restart poll happens to
    see, understating any dip that already happened earlier today.
    Independently confirmed as a real, live bug from this bot's own
    Sept 21, 2026 log: running high peaked at $79.36 at 3:37pm, then a
    4:29pm restart reset it to $78.87 -- LOWER than the true day's high,
    meaning a real pullback from $79.36 would have been measured against
    the wrong, understated peak for the rest of that session. A restart
    can trigger this on any ordinary day, crash or manual -- _running_high
    lives only in RAM (see its own comment above) and any
    crash-and-systemd-restart cycle wipes it exactly the same way a
    manual restart does.

    Reuses fetch_live_price_extended_hours's own 5-day/5m prepost=True
    yfinance pull -- no new data source, no new network pattern, and the
    same real limitation: this still can't see the Blue Ocean overnight
    session, so a restart during THAT window would still seed from a
    lower, visible-hours-only high. Better than None, not a full fix for
    the same underlying gap. Deliberately stays on yfinance rather than
    routing through fetch_dip_alert_price's Alpaca/yfinance split -- this
    runs once, at startup only, not on a 5-min cadence, so the
    rate-limit motivation for that split doesn't apply here.

    IMPORTANT: the caller (on_ready) MUST also set _last_price_source =
    "yfinance" alongside _running_high = <this return value>. This
    function always seeds from yfinance, but if the bot restarts during
    regular hours, the very first live poll after startup would use
    Alpaca (via fetch_dip_alert_price) -- without _last_price_source
    already set here, that first poll's cross-venue-mismatch guard in
    check_dip_alert never fires (it only triggers when a prior source is
    already known), so a yfinance-seeded high could get compared
    straight against a fresh Alpaca price with no protection at all,
    defeating the exact mechanism built to prevent that. This was a real
    gap in an earlier version of this seeding function -- fixed by
    documenting it as a required part of the caller's contract rather
    than trying to set module globals from inside this function.

    Deliberately filtered down to bars from TODAY's ET calendar date
    only, NOT the full 5-day window: seeding from a multi-day max risks
    anchoring to a stale peak from a prior session that's irrelevant to
    the CURRENT dip-watching cycle. Neither check_dip_alert nor
    check_rung_alert has a daily reset of its own -- so a stale
    multi-day peak, once seeded, could sit there unrevisited for days,
    silently suppressing real alerts far worse than the None-start
    problem this function exists to fix.

    Returns None (not a fallback price) on any failure or empty result
    -- caller must treat that as "couldn't seed, fall back to normal
    cold-start behavior (None)," never invent a substitute value."""
    try:
        df = yf.download(
            "TQQQ",
            period="5d",
            interval="5m",
            progress=False,
            auto_adjust=True,
            multi_level_index=False,
            prepost=True,
        )
        if df.empty:
            return None
        df.columns = [str(c).lower() for c in df.columns]
        idx = df.index.tz_convert(_ET) if df.index.tz else df.index.tz_localize(_ET)
        today = datetime.now(_ET).date()
        todays_bars = df[idx.date == today]
        if todays_bars.empty:
            return None
        high = float(todays_bars["high"].max())
        return high if high > 0 else None
    except Exception as e:
        log.warning(f"Could not seed running high from today's data: {e}")
        return None


async def fetch_dip_alert_price() -> tuple:
    """Returns (price, source_label). Splits the price source by regular-
    hours status -- added specifically to reduce this bot's yfinance call
    volume, after review found the dip-alert's ~162 calls/day (single
    symbol, 5-min cadence across the whole 6:30am-8pm window) was ~3.5x
    higher than the only proven-safe precedent on this VPS (the existing
    15-min-cadence bots' combined ~46 calls/day). A yfinance IP block
    isn't necessarily isolated to this one bot either -- all six bots on
    this system share one VPS IP, so a block from this alert's volume
    could plausibly take down tqqq_bot.py, tqqq_above_open_bot.py, and
    both SOXL bots simultaneously, not just silence this one feature.

    Uses is_regular_hours_clock_only() to decide the split, NOT
    is_regular_market_hours_live() -- this was a real bug in an earlier
    version of this function: is_regular_market_hours_live's own SPY
    holiday probe runs on EVERY call within the 9:30-4 clock window, with
    no caching. Gating the Alpaca/yfinance switch on that function meant
    every regular-hours cycle still made exactly one yfinance call --
    just moved from "fetch TQQQ price" to "probe SPY for a holiday,"
    never actually eliminated. The whole point of this function was to
    get regular-hours cycles off yfinance entirely; using the holiday-
    aware check silently defeated that. is_regular_hours_clock_only()
    is a pure clock check, no network at all, so during regular hours
    this function now makes ZERO yfinance calls, genuinely delivering
    the ~162/day reduction described above rather than just relabeling
    the same call.

    During regular hours (9:30am-4:00pm ET): Alpaca's latest trade --
    real, liquid regular-session trading, no staleness concern.
    fetch_latest_trade_price_with_staleness_check raises if the trade is
    older than its own threshold (default 10 min) rather than silently
    returning a stale value; caught the same way as any other fetch
    failure below. A guessed-wrong holiday (clock check alone can't tell
    a holiday from a normal trading day) means this raises with no real
    trades to find -- caught by the caller's existing try/except, which
    just skips that cycle with a warning. Same low-stakes "wrong guess
    is cheap" reasoning already applied to is_dip_alert_window's own
    pure-clock design, extended here to the same class of guess.

    Outside regular hours (the pre-market/post-market portions of the
    wider dip-alert window): stays on yfinance's
    fetch_live_price_extended_hours, unchanged. Same reasoning already
    established for why extended-hours price was never migrated to
    Alpaca in the first place -- IEX's extended-hours liquidity is
    thinner than its already-small regular-session share, so a latest-
    trade call there is more likely to be stale enough to reject."""
    if is_regular_hours_clock_only():
        price = await asyncio.to_thread(
            ad.fetch_latest_trade_price_with_staleness_check, alpaca_client, "TQQQ"
        )
        return price, "alpaca"
    else:
        price = await asyncio.to_thread(fetch_live_price_extended_hours, "TQQQ")
        return price, "yfinance"


@tasks.loop(minutes=5)
async def check_dip_alert():
    global _running_high, _last_price_source

    # Heartbeat -- previously this function was COMPLETELY silent on every
    # normal cycle (only a warning on fetch failure, or the alert itself
    # firing). That meant there was no way to confirm from logs alone that
    # polling was actually happening, short of waiting for an error or a
    # real dip. One INFO line per cycle, always, regardless of which
    # branch below is taken -- cheap, and directly answers "is this
    # running" via `journalctl -u tqqq-ladder-bot -f`.
    if not await asyncio.to_thread(is_dip_alert_window):
        log.info("Dip-alert poll: outside window (pre-6:30am, post-8pm, or weekend) -- skipped")
        return

    if read_position_state() is not None:
        log.info("Dip-alert poll: position currently tracked -- skipped (alert is for flat only)")
        return

    try:
        current_price, source = await fetch_dip_alert_price()
    except Exception as e:
        log.warning(f"Dip-alert poll: price fetch failed, skipping this cycle: {e}")
        return

    # Source just changed (crossing the 9:30am or 4:00pm boundary) --
    # don't compare across venues THIS cycle (still returns early below,
    # same as before) to avoid a false dip/new-high purely from Alpaca and
    # yfinance quoting slightly different prices at the same instant, not
    # real market movement.
    #
    # BUT: unconditionally overwriting _running_high with current_price
    # here was a real, separate bug (distinct from the cross-venue-noise
    # risk this block already guards against) -- confirmed by trace, not
    # theoretical. A genuine pre-market peak (e.g. $85 seen via yfinance)
    # gets silently erased the instant the clock crosses 9:30 and the
    # source flips to alpaca, even if the opening/current alpaca price
    # ($84, say) is lower but the market hasn't actually round-tripped
    # down that far -- DIP_ALERT_THRESHOLD_PCT then measures the dip from
    # the wrong, artificially-lowered baseline, and a real drop from the
    # true pre-market high can silently fail to fire. Preserving
    # max(_running_high, current_price) fixes the peak-erasure without
    # reopening the cross-venue-noise risk: the two prices are still never
    # DIRECTLY compared against each other for a dip decision this cycle
    # (that comparison only happens on a later cycle, against whichever
    # venue is now current) -- this line only decides which of the two
    # numbers is a real high worth remembering going forward.
    if _last_price_source is not None and source != _last_price_source:
        preserved_high = max(_running_high, current_price) if _running_high is not None else current_price
        if preserved_high != current_price:
            log.info(f"Dip-alert poll: price source switched to {source} (TQQQ ${current_price:.2f}) "
                      f"-- keeping prior running high ${preserved_high:.2f} (not erasing it for the new venue)")
        else:
            log.info(f"Dip-alert poll: price source switched to {source} (TQQQ ${current_price:.2f}) "
                      f"-- running high set to this reading (no higher prior value to preserve)")
        _running_high = preserved_high
        _last_price_source = source
        return
    _last_price_source = source

    if _running_high is None or current_price > _running_high:
        _running_high = current_price
        log.info(f"Dip-alert poll: TQQQ ${current_price:.2f} ({source}) -- new running high, no dip to check yet")
        return

    dip_pct = (_running_high - current_price) / _running_high * 100
    if dip_pct < DIP_ALERT_THRESHOLD_PCT:
        log.info(f"Dip-alert poll: TQQQ ${current_price:.2f} ({source}), {dip_pct:.2f}% below "
                  f"running high ${_running_high:.2f} (threshold {DIP_ALERT_THRESHOLD_PCT}%) -- no alert")
        return

    log.info(f"Dip-alert poll: THRESHOLD MET -- TQQQ ${current_price:.2f} ({source}), "
              f"{dip_pct:.2f}% below ${_running_high:.2f} -- firing alert")
    channel = client.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        log.warning(f"Dip-alert poll: channel {ALERT_CHANNEL_ID} not found -- check DISCORD_ALERT_CHANNEL_ID")
        return

    embed = discord.Embed(
        title="🚨 TQQQ dip threshold reached",
        description=(
            f"TQQQ is at **${current_price:.2f}**, {dip_pct:.2f}% below the recent high "
            f"of **${_running_high:.2f}**.\n\nPlace your order manually, then confirm below."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="No anti-spam suppression -- this will keep firing every 5 min while the dip persists")
    try:
        await channel.send(embed=embed, view=DipAlertResponseView(target_price=current_price))
    except Exception as e:
        # Distinct from the "THRESHOLD MET" log above on purpose: that
        # line only confirms the DECISION to fire, not that the alert
        # actually reached Discord. Without this try/except, a send
        # failure (permissions, rate limit, a transient API error) would
        # be an unhandled exception here -- caught by check_dip_alert's
        # error handler, which logs it, but the alert itself would be
        # silently lost with no distinct record of that specific failure,
        # on exactly the cycle it mattered most.
        log.error(f"Dip-alert poll: ALERT DECIDED BUT SEND FAILED (TQQQ ${current_price:.2f} ({source}), "
                   f"{dip_pct:.2f}% below ${_running_high:.2f}): {e}", exc_info=e)
        return

    log.info(f"Dip-alert poll: ALERT SENT -- TQQQ ${current_price:.2f} ({source}), "
              f"{dip_pct:.2f}% below ${_running_high:.2f}")


@check_dip_alert.error
async def check_dip_alert_error(error):
    # tasks.loop SILENTLY STOPS the loop on any unhandled exception unless
    # an error handler is registered -- without this, a single unexpected
    # crash mid-cycle would kill dip-alert polling permanently, with
    # NOTHING in the logs to explain why it just stopped. This handler
    # doesn't fix whatever went wrong, but it guarantees the failure is
    # loud, not silent. Recovery: the next Discord gateway reconnect fires
    # on_ready again, which already checks is_running() and restarts the
    # loop if it's not -- so this is usually self-healing, but only if the
    # failure is visible enough to notice in the meantime.
    log.error(f"Dip-alert polling loop crashed and stopped: {error}", exc_info=error)


# ---------- Rung-crossing alert (IN a tracked position, opposite gate from check_dip_alert) ----------
#
# Closes a real, confirmed gap: check_dip_alert goes silent the moment a
# position is tracked ("alert is for flat only"), but nothing was ever
# watching for price reaching the ladder's own Buy 1/2/3 levels while
# you're actually holding -- Buy 1/2/3 were display-only numbers in the
# /buyfilled embed, with zero automated monitoring behind them. Confirmed
# live on Sept 21, 2026: price crossed BOTH Buy 1 ($77.38) and Buy 2
# ($75.77) with no alert of any kind, because check_dip_alert was already
# gated off by the tracked position.

@tasks.loop(minutes=5)
async def check_rung_alert():
    """Deliberately checks ONLY the ladder's nearest rung (build_ladder's
    first returned level), not all three shown in /buyfilled -- no
    separate "watch Buy 2, watch Buy 3" logic is needed or even correct:
    once this alert's own Filled/Custom Fill button updates
    position_state.json, the basis (and often the day's qqq_atr_pct)
    changes, so build_ladder() naturally produces a NEW nearest rung on
    the very next cycle. Hardcoding a watch on today's Buy 2 price would
    in fact be WRONG the moment Buy 1 fills, since a fresh basis shifts
    where Buy 2 actually sits -- watching the ladder's own always-current
    first level sidesteps that for free, rather than needing to be told
    to re-target after every fill.

    Uses the SAME view (DipAlertResponseView) and the SAME
    handle_fill_confirmation() plumbing the dip-alert and /testdipalert
    already use -- single source of truth for "a fill happened," not a
    second, divergent recording path. The view's "Filled at Target" /
    "Custom Fill" / "Skip Dip" labels stay as-is rather than forking a
    near-duplicate view class just to rename them for this context.

    Same NO-ANTI-SPAM policy as check_dip_alert, for the same reason:
    if you don't confirm within one 5-min cycle, the next cycle
    recomputes the (usually near-identical) rung price and fires again
    rather than going quiet -- silently going dark on a level you're
    actively trying to average into would be worse than a repeated ping.

    Same is_dip_alert_window gate as check_dip_alert (6:30am-8pm ET,
    weekdays only) -- this needs the same extended-hours live price
    fetch, so the same coverage limits apply.

    Uses fetch_dip_alert_price() -- the SAME Alpaca/yfinance regular-
    hours split check_dip_alert uses (via is_regular_hours_clock_only,
    not the holiday-probing version -- see fetch_dip_alert_price's own
    docstring), NOT a separate, unconditional yfinance call. This
    matters: without sharing the split, this task would silently undo
    the rate-limit reduction the split exists for on every day you're
    actually holding a position -- arguably the MORE common state for
    an averaging-down strategy, not an edge case. Unlike check_dip_alert,
    this does NOT need the _last_price_source cross-venue reset logic:
    that exists to protect a REMEMBERED running_high from a false
    comparison against a differently-sourced fresh price. Here,
    `nearest.price` is recomputed from scratch every cycle from
    build_ladder() (itself always fed by Alpaca's fetch_daily_bars,
    independent of which source fed current_tqqq_price) -- there's no
    persisted value that a source switch could corrupt, only a
    same-cycle comparison that starts fresh each time regardless."""
    if not await asyncio.to_thread(is_dip_alert_window):
        log.info("Rung-alert poll: outside window (pre-6:30am, post-8pm, or weekend) -- skipped")
        return

    state = read_position_state()
    if state is None:
        log.info("Rung-alert poll: no position tracked -- skipped (alert is for in-position only)")
        return

    try:
        qqq_df, tqqq_df = await asyncio.to_thread(get_market_data)
        current_tqqq_price, source = await fetch_dip_alert_price()
    except Exception as e:
        log.warning(f"Rung-alert poll: data fetch failed, skipping this cycle: {e}")
        return

    qqq_close = float(qqq_df["close"].iloc[-1])
    qqq_atr_pct = compute_atr_pct(qqq_df)
    qqq_swing_lows_all = find_confirmed_swing_lows(qqq_df)

    ladder = build_ladder(
        basis_price=state["price"],
        current_tqqq_price=current_tqqq_price,
        qqq_close=qqq_close,
        qqq_atr_pct=qqq_atr_pct,
        qqq_swing_lows=qqq_swing_lows_all if USE_CONFLUENCE else pd.Series(dtype=float),
    )

    if not ladder:
        log.info(f"Rung-alert poll: TQQQ ${current_tqqq_price:.2f} ({source}) -- no valid ladder level below current price")
        return

    nearest = ladder[0]
    log.info(f"Rung-alert poll: TQQQ ${current_tqqq_price:.2f} ({source}), nearest rung ${nearest.price:.2f} "
              f"({nearest.label}) -- {'AT/BELOW, firing' if current_tqqq_price <= nearest.price else 'above, no alert'}")

    if current_tqqq_price > nearest.price:
        return

    channel = client.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        log.warning(f"Rung-alert poll: channel {ALERT_CHANNEL_ID} not found -- check DISCORD_ALERT_CHANNEL_ID")
        return

    embed = discord.Embed(
        title="🎯 TQQQ buy rung reached",
        description=(
            f"TQQQ is at **${current_tqqq_price:.2f}**, at or below your nearest buy rung "
            f"**${nearest.price:.2f}** ({nearest.label}, {nearest.tqqq_drop_pct:.1f}% below your "
            f"${state['price']:.2f} basis).\n\nPlace your order manually, then confirm below."
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="No anti-spam suppression -- this will keep firing every 5 min while price stays at/below this rung")
    try:
        await channel.send(embed=embed, view=DipAlertResponseView(target_price=nearest.price))
    except Exception as e:
        log.error(f"Rung-alert poll: ALERT DECIDED BUT SEND FAILED (TQQQ ${current_tqqq_price:.2f} ({source}), "
                   f"rung ${nearest.price:.2f}): {e}", exc_info=e)
        return

    log.info(f"Rung-alert poll: ALERT SENT -- TQQQ ${current_tqqq_price:.2f} ({source}) at/below rung ${nearest.price:.2f}")


@check_rung_alert.error
async def check_rung_alert_error(error):
    # Same reasoning as check_dip_alert_error -- tasks.loop silently
    # stops on any unhandled exception without this handler.
    log.error(f"Rung-alert polling loop crashed and stopped: {error}", exc_info=error)


@client.event
async def on_ready():
    # Sync already happened once in setup_hook -- this just logs connection
    # status, safe to fire on every reconnect.
    log.info(f"Logged in as {client.user}")
    if not check_dip_alert.is_running():
        global _running_high, _last_price_source
        seeded = await asyncio.to_thread(seed_running_high_from_today)
        if seeded is not None:
            _running_high = seeded
            # Required alongside the seed itself -- see
            # seed_running_high_from_today's own docstring for why:
            # without this, the first live poll after a regular-hours
            # restart would use Alpaca and skip the cross-venue-mismatch
            # guard entirely, since that guard only fires once a prior
            # source is already known.
            _last_price_source = "yfinance"
            log.info(f"Dip-alert: seeded running high from today's data -- ${seeded:.2f}")
        else:
            log.info("Dip-alert: could not seed running high from today's data -- starting cold (None)")
        check_dip_alert.start()
        log.info("Dip-alert polling loop started.")
    if not check_rung_alert.is_running():
        check_rung_alert.start()
        log.info("Rung-alert polling loop started.")


@tree.command(name="marketcheck", description="Current QQQ regime, trend distance, and volatility -- no position needed")
async def marketcheck(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        qqq_df, tqqq_df = await asyncio.to_thread(get_market_data)

        qqq_now = float(qqq_df["close"].iloc[-1])
        tqqq_now = float(tqqq_df["close"].iloc[-1])
        qqq_atr_pct = compute_atr_pct(qqq_df)
        market_data_last_date = qqq_df.index[-1].strftime("%Y-%m-%d")
        regime = compute_regime_status(qqq_df)
        signed_trend_distance = compute_signed_trend_distance(qqq_df)

        # Outside regular market hours, qqq_now/tqqq_now above are frozen
        # at the regular-session close (fetch_daily_bars never requests
        # prepost data). Try a fresher, extended-hours price here -- best
        # effort only: on any failure, silently keep the regular-session
        # close already in hand rather than erroring the whole command.
        # This ONLY affects the price shown/used below; ATR, RSI, regime,
        # and rolling-extremes above are untouched, still computed from
        # the regular-session-only qqq_df/tqqq_df.
        if not await asyncio.to_thread(is_regular_market_hours_live):
            try:
                fresh_qqq, fresh_tqqq = await asyncio.to_thread(get_extended_hours_prices)
                qqq_now, tqqq_now = fresh_qqq, fresh_tqqq
            except Exception as e:
                log.warning(f"Extended-hours price fetch failed, using regular-session close instead: {e}")

        # RSI shown for BOTH assets, clearly labeled, by explicit choice --
        # tqqq_bot.py's live signal uses RSI(14) on TQQQ directly (no QQQ
        # involved), while everything else in this bot deliberately uses
        # QQQ. Rather than pick one and silently favor a convention, both
        # are shown so you can see whether they agree or diverge. Neither
        # is asserted as a decision rule here -- purely descriptive,
        # unlike tqqq_bot.py's actual validated `< 35` threshold.
        # RSI(2) on QQQ -- fast mean-reversion oscillator, matches the
        # convention from tqqq_swing_bot_v2.py (not live, but the only
        # existing reference for this period/asset combo). Reuses
        # compute_rsi_status with period=2 -- no new function needed, this
        # is exactly what the period argument already exists for.
        qqq_rsi2 = compute_rsi_status(qqq_df, period=2)

        qqq_rsi = compute_rsi_status(qqq_df)
        tqqq_rsi = compute_rsi_status(tqqq_df)

        # Volume ratio deliberately sourced from yfinance, NOT the
        # (now Alpaca-sourced) qqq_df above -- see
        # fetch_qqq_volume_series_yfinance's docstring for why. Wrapped
        # into the same single-column DataFrame shape compute_volume_ratio
        # already expects, so ladder_core.py needed zero changes.
        qqq_volume_series = await asyncio.to_thread(fetch_qqq_volume_series_yfinance)
        qqq_volume_ratio = compute_volume_ratio(pd.DataFrame({"volume": qqq_volume_series}))
        qqq_rolling = compute_rolling_extremes(qqq_df, qqq_now)

        # Nearest historical QQQ support, translated to TQQQ -- a FACT, not
        # a recommendation. Anchored to live TQQQ price (not a basis --
        # there's no position yet in this command), same reasoning as the
        # ladder's own support display: this describes where structure
        # sits relative to today, it doesn't tell you what to do with it.
        # Reuses find_confirmed_swing_lows and translate_qqq_price_to_tqqq
        # as-is -- no new logic, no momentum/RSI gating, no proposed order.
        MAX_SUPPORT_SHOWN = 2
        all_swing_lows = find_confirmed_swing_lows(qqq_df)
        supports_below = all_swing_lows[all_swing_lows < qqq_now].sort_values(ascending=False)
        nearest_supports = []
        for date, qqq_support_price in supports_below.head(MAX_SUPPORT_SHOWN).items():
            tqqq_equiv = translate_qqq_price_to_tqqq(float(qqq_support_price), qqq_now, tqqq_now, LEVERAGE_FACTOR)
            qqq_gap_pct = (qqq_now - float(qqq_support_price)) / qqq_now * 100
            date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)
            # TQQQ figure leads the line -- that's the number actually
            # needed for a decision; QQQ source details follow as context,
            # instead of being buried at the end of a long sentence.
            nearest_supports.append(
                f"**TQQQ ≈ ${tqqq_equiv:.2f}** — QQQ ${float(qqq_support_price):.2f} "
                f"({qqq_gap_pct:.1f}% below today, swing low {date_str})"
            )

        embed = discord.Embed(
            title="Market check",
            color=discord.Color.orange() if regime.below_sma else discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Price", value=f"QQQ ${qqq_now:.2f}  |  TQQQ ${tqqq_now:.2f}", inline=False)

        # Badges use period-CORRECT thresholds, not one blanket number for
        # both -- RSI(2) swings far more than RSI(14) by design. RSI(2)
        # bounds (<10/>90) match tqqq_swing_bot_v2.py's actual live
        # convention; RSI(14) bounds (<30/>70) are the standard Wilder
        # convention. Applying RSI(14)'s bounds to RSI(2) would badge
        # ordinary RSI(2) noise as "extreme," and vice versa. Silent (no
        # badge) in between -- same asymmetric-display principle as the
        # regime badge: only flag what's actually notable.
        def rsi_badge(value: float, oversold: float, overbought: float) -> str:
            if value <= oversold:
                return "🟢 "
            if value >= overbought:
                return "🔴 "
            return ""

        rsi_arrow = {"rising": "↑", "falling": "↓", "flat": "→"}

        def rsi_line(label: str, status, oversold: float, overbought: float) -> str:
            badge = rsi_badge(status.value, oversold, overbought)
            return (f"{badge}{label}: {status.value:.1f} {rsi_arrow[status.direction]} "
                    f"({status.direction}, was {status.prior_value:.1f})")

        momentum_lines = [
            rsi_line("QQQ RSI(2)", qqq_rsi2, oversold=10, overbought=90),
            rsi_line("QQQ RSI(14)", qqq_rsi, oversold=30, overbought=70),
            rsi_line("TQQQ RSI(14)", tqqq_rsi, oversold=30, overbought=70),
            f"QQQ volume (5d vs 20d avg): {qqq_volume_ratio:.2f}x",
            f"QQQ 14-day ATR: {qqq_atr_pct * 100:.2f}%",
        ]
        embed.add_field(name="Momentum & Volatility", value="\n".join(momentum_lines), inline=False)

        # Rolling 30-day extremes (Donchian-style) -- a genuinely different
        # question than ATR%: how far has price fallen from its own recent
        # peak, cumulatively, not how big is a typical single day's move.
        new_low_badge = "🔴 " if qqq_rolling.is_new_low else ""
        dip_lines = [
            f"{new_low_badge}30-day low: {'YES -- today is a new 30-day low' if qqq_rolling.is_new_low else 'no'} "
            f"(prior 30d low: ${qqq_rolling.prior_low:.2f})",
            f"Drawdown from 30-day high (${qqq_rolling.high:.2f}): {qqq_rolling.drawdown_from_high_pct:.2f}%",
        ]
        embed.add_field(name="30-Day Range", value="\n".join(dip_lines), inline=False)

        if nearest_supports:
            embed.add_field(
                name="📍 Nearest QQQ support (info only, not a target)",
                value="\n".join(nearest_supports),
                inline=False,
            )

        # 200-SMA distance and regime warning grouped into ONE field, kept
        # last -- background/context checked last, not the first thing
        # needed to act.
        trend_direction = "above" if signed_trend_distance >= 0 else "below"
        trend_lines = [f"Distance to 200-SMA: {signed_trend_distance:+.2f}% ({trend_direction} trend, SMA ${regime.sma_200:.2f})"]
        if regime.below_sma:
            trend_lines.append(
                "⚠️ Bearish/distressed regime. TQQQ's decay compounds fastest in "
                "choppy, directionless conditions -- this is the exact scenario "
                "the ladder's own regime badge exists to flag. Treat any new "
                "entry with extra caution."
            )
        embed.add_field(name="Trend", value="\n".join(trend_lines), inline=False)

        embed.set_footer(text=f"Data as of {market_data_last_date} -- informational only, no buy/sell verdict")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        log.exception("marketcheck failed")
        await interaction.followup.send(f"Error computing market check: {e}")


@tree.command(name="position", description="Show your currently tracked TQQQ position, with an option to clear it")
async def position(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    state = read_position_state()

    if state is None:
        await interaction.followup.send("No position currently tracked -- you're flat.")
        return

    embed = discord.Embed(
        title="Tracked position",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Shares", value=f"{state['shares']:g}", inline=True)
    embed.add_field(name="Basis", value=f"${state['price']:.2f}", inline=True)
    embed.add_field(name="Last updated", value=state["updated_at"], inline=False)
    embed.set_footer(text="This is what the bot has on file -- if it's stale or wrong, use /buyfilled to update it, or clear it below once you've actually sold")

    await interaction.followup.send(embed=embed, view=ClearPositionView())


@tree.command(name="testdipalert", description="Manually trigger a test dip alert -- verifies the buttons/modals work without waiting for a real dip")
async def testdipalert(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        current_price = await asyncio.to_thread(fetch_live_price_extended_hours, "TQQQ")
    except Exception as e:
        await interaction.followup.send(f"Could not fetch a live TQQQ price for the test: {e}")
        return

    # Uses the REAL DipAlertResponseView -- same buttons, same modals, same
    # handle_fill_confirmation() / build_buyfilled_response() the actual
    # polling task would use. This tests the entire downstream pipeline
    # for real, not a mock -- worth knowing plainly: clicking "Filled at
    # Target" or "Custom Fill" below WILL actually write to
    # position_state.json, exactly as a real fill would. Use /position's
    # Clear Position button afterward if this was only a test and you
    # don't want it to stick. "Skip Dip" is the fully inert option if you
    # just want to confirm the message/buttons render correctly.
    embed = discord.Embed(
        title="🧪 TEST dip alert (not a real trigger)",
        description=(
            f"TQQQ is currently **${current_price:.2f}**.\n\n"
            f"This is a manually-triggered test, not a real threshold cross. "
            f"The buttons below are fully real -- clicking Filled/Custom "
            f"Fill WILL update your tracked position for real. Use /position's "
            f"Clear Position button afterward if you don't want the test to stick."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Manual test -- does not touch the real polling loop's running_high or armed state")
    await interaction.followup.send(embed=embed, view=DipAlertResponseView(target_price=current_price))


async def build_buyfilled_response(shares: float, price: float, target_avg: float = None) -> discord.Embed:
    """Core of /buyfilled, extracted so both the slash command AND the
    dip-alert button callbacks (Filled at Target / Custom Fill) call this
    ONE implementation -- single source of truth, same principle already
    applied everywhere else in this bot (compute_atr_pct deriving from
    compute_atr_pct_series, /marketcheck and /buyfilled sharing
    ladder_core's functions, etc). Raises on invalid input or failure --
    callers are responsible for catching and reporting to the user in
    whatever way fits their context (slash command vs. button response)."""
    if shares <= 0 or price <= 0:
        raise ValueError(
            f"Both shares ({shares:g}) and price (${price:.2f}) must be positive numbers -- "
            f"check your input and try again."
        )
    if target_avg is not None and target_avg >= price:
        raise ValueError(
            f"Target average (${target_avg:.2f}) must be below your current basis (${price:.2f}) -- "
            f"buying more shares can only lower your average, not raise it."
        )

    qqq_df, tqqq_df = await asyncio.to_thread(get_market_data)

    qqq_close = float(qqq_df["close"].iloc[-1])
    current_tqqq_price = float(tqqq_df["close"].iloc[-1])
    qqq_atr_pct = compute_atr_pct(qqq_df)
    market_data_last_date = qqq_df.index[-1].strftime("%Y-%m-%d")
    regime = compute_regime_status(qqq_df)

    if not await asyncio.to_thread(is_regular_market_hours_live):
        try:
            fresh_qqq, fresh_tqqq = await asyncio.to_thread(get_extended_hours_prices)
            qqq_close, current_tqqq_price = fresh_qqq, fresh_tqqq
        except Exception as e:
            log.warning(f"Extended-hours price fetch failed, using regular-session close instead: {e}")

    qqq_swing_lows_all = find_confirmed_swing_lows(qqq_df)
    ladder = build_ladder(
        basis_price=price,
        current_tqqq_price=current_tqqq_price,
        qqq_close=qqq_close,
        qqq_atr_pct=qqq_atr_pct,
        qqq_swing_lows=qqq_swing_lows_all if USE_CONFLUENCE else pd.Series(dtype=float),
    )

    deepest_ladder_price = min((lvl.price for lvl in ladder), default=0.0)
    extra_buffer_pct = ATR_STEP * qqq_atr_pct * LEVERAGE_FACTOR
    low_bound = deepest_ladder_price * (1 - extra_buffer_pct) if deepest_ladder_price else 0.0
    support_levels = find_support_in_range(
        tqqq_reference_price=current_tqqq_price,
        qqq_close=qqq_close,
        qqq_swing_lows=qqq_swing_lows_all,
        low_bound=low_bound,
        high_bound=current_tqqq_price,
        max_results=MAX_SUPPORT_DISPLAY,
    )

    log_ladder(shares, price, current_tqqq_price, qqq_close, qqq_atr_pct,
               market_data_last_date, ladder, support_levels, regime)
    write_position_state(shares, price)  # marks "in position" for the dip-alert gate

    embed = discord.Embed(
        title=f"TQQQ position: {shares:g} sh @ ${price:.2f} basis",
        color=discord.Color.orange() if regime.below_sma else discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if regime.below_sma:
        embed.add_field(
            name="⚠️ Regime: QQQ below 200-day SMA",
            value=(
                f"QQQ ${regime.qqq_close:.2f} is {regime.pct_below:.1f}% below its "
                f"200-SMA (${regime.sma_200:.2f}) — bearish/distressed regime. This "
                f"backtest's sample size for sustained downtrends is thin; treat "
                f"these levels with more caution than usual."
            ),
            inline=False,
        )

    embed.add_field(name="Price", value=f"TQQQ ${current_tqqq_price:.2f}  |  QQQ ${qqq_close:.2f}", inline=False)
    embed.add_field(name="QQQ 14-day ATR", value=f"{qqq_atr_pct * 100:.2f}%", inline=True)

    support_positions = locate_support_relative_to_ladder(support_levels, ladder, current_tqqq_price)
    CONFIRMS_TOLERANCE_PCT = SWING_SNAP_TOLERANCE_ATR * qqq_atr_pct * LEVERAGE_FACTOR * 100
    support_by_level = {}
    unattached_support = []
    for s, pos in zip(support_levels, support_positions):
        direction = "above" if pos.gap > 0 else "below"
        verb = "confirms" if abs(pos.gap_pct) <= CONFIRMS_TOLERANCE_PCT else "nearest to"
        line = (
            f"📍 ${s.tqqq_price:.2f} {verb} this level "
            f"(${abs(pos.gap):.2f} {direction}, {abs(pos.gap_pct):.2f}%) "
            f"— QQQ swing low {s.swing_low_date}"
        )
        if pos.level_index is not None:
            support_by_level.setdefault(pos.level_index, []).append(line)
        else:
            unattached_support.append(
                f"${s.tqqq_price:.2f} — near current price, "
                f"${abs(pos.gap):.2f} {direction} "
                f"(QQQ swing low {s.swing_low_date})"
            )

    if not ladder:
        embed.add_field(
            name="No levels found",
            value="No valid levels below current price within the search range -- ATR may be very low, or price has moved far below basis already.",
            inline=False,
        )
    else:
        for i, lvl in enumerate(ladder, start=1):
            tqqq_pct_from_now = (current_tqqq_price - lvl.price) / current_tqqq_price * 100
            qqq_pct_from_now = tqqq_pct_from_now / LEVERAGE_FACTOR
            qqq_target_price = qqq_close * (1 - qqq_pct_from_now / 100)

            field_value = (
                f"QQQ needs ~{qqq_pct_from_now:.1f}% more drop from today (to ~${qqq_target_price:.2f}) "
                f"[{lvl.label}, {lvl.tqqq_drop_pct:.1f}% below basis]"
            )

            if target_avg is not None:
                if target_avg <= lvl.price:
                    field_value += (
                        f"\n🎯 To reach ${target_avg:.2f} avg: not reachable at this level "
                        f"(target is at or below this price -- buying here can only approach "
                        f"${lvl.price:.2f}, never go below it)"
                    )
                else:
                    shares_needed = shares * (price - target_avg) / (target_avg - lvl.price)
                    if shares_needed > shares * 10:
                        field_value += (
                            f"\n🎯 To reach ${target_avg:.2f} avg: not realistic here -- "
                            f"would require ~{shares_needed:.0f} shares "
                            f"({shares_needed / shares:.0f}x your current position). "
                            f"Target is too close to this level's price or your current basis."
                        )
                    else:
                        field_value += (
                            f"\n🎯 To reach ${target_avg:.2f} avg: buy ~{shares_needed:.1f} shares here "
                            f"(new total: {shares + shares_needed:.1f} sh)"
                        )

            if i in support_by_level:
                field_value += "\n" + "\n".join(support_by_level[i])
            embed.add_field(
                name=f"🎯 Buy {i}: ${lvl.price:.2f} (-{lvl.tqqq_drop_pct:.1f}% from basis)",
                value=field_value,
                inline=False,
            )

    if unattached_support:
        embed.add_field(
            name="📍 Other support nearby (info only)",
            value="\n".join(unattached_support),
            inline=False,
        )

    embed.set_footer(text="Pure ATR spacing, anchored to your basis -- update it only after a real fill")
    return embed


@tree.command(name="buyfilled", description="Get your next 3 TQQQ buy levels, anchored to your average cost")
@app_commands.describe(
    shares="Total position size (shares held)",
    price="Your average cost basis (not today's market price)",
    target_avg="Optional: see how many shares at each level would bring your average down to this price",
)
async def buyfilled(interaction: discord.Interaction, shares: float, price: float, target_avg: float = None):
    await interaction.response.defer(thinking=True)
    try:
        embed = await build_buyfilled_response(shares, price, target_avg)
        await interaction.followup.send(embed=embed)
    except ValueError as e:
        await interaction.followup.send(str(e))
    except Exception as e:
        log.exception("buyfilled failed")
        await interaction.followup.send(f"Error computing ladder: {e}")


if __name__ == "__main__":
    client.run(TOKEN)
