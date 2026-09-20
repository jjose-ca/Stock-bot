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
    pip install discord.py yfinance pandas numpy python-dotenv

Env vars (.env or exported):
    DISCORD_BOT_TOKEN
    DISCORD_GUILD_ID   (optional, for instant guild-scoped command sync)
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
import yfinance as yf
import discord
from discord import app_commands
from dotenv import load_dotenv

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

LADDER_LOG_PATH = Path(__file__).parent / "ladder_log.jsonl"
POSITION_STATE_PATH = Path(__file__).parent / "position_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buy_ladder_bot")


# ---------- Data fetching ----------

def fetch_daily_bars(symbol: str, lookback_days: int = 400) -> pd.DataFrame:
    """Blocking network call -- must be run via asyncio.to_thread from
    inside the Discord event loop.

    lookback_days=400 (not the earlier 250): a 200-day SMA needs 200
    genuine TRADING days, and yfinance's period="Nd" counts CALENDAR
    days -- weekends and holidays mean 250 calendar days only works out
    to roughly 170-180 trading days, not enough for a reliable 200-day
    SMA. 400 calendar days comfortably clears 200 trading days with
    margin for holidays."""
    df = yf.download(
        symbol,
        period=f"{lookback_days}d",
        interval="1d",
        progress=False,
        auto_adjust=True,
        multi_level_index=False,
    )
    if df.empty:
        raise RuntimeError(f"No data returned for {symbol}")
    df.columns = [str(c).lower() for c in df.columns]
    df.index.name = "date"
    df = df[["open", "high", "low", "close", "volume"]]
    if df["close"].iloc[-1:].isna().any():
        # A non-empty dataframe with a NaN last row is a real, distinct
        # failure mode from df.empty -- seen in practice when Yahoo's
        # backend hasn't finished publishing today's completed bar yet
        # (a lag can exist right around/after the close). Left unchecked,
        # NaN silently propagates through every downstream calculation --
        # ATR, price, filter comparisons (which always evaluate False
        # against NaN) -- surfacing only as a confusing "No levels found"
        # with literal "nan" shown in the embed, far from the actual cause.
        raise RuntimeError(
            f"{symbol}'s latest bar has no valid price data yet (Yahoo's feed "
            f"may still be publishing today's data) -- try again in a few minutes."
        )
    return df


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
    no way to verify a sale actually happened. See the module note above."""
    if POSITION_STATE_PATH.exists():
        POSITION_STATE_PATH.unlink()


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
    alerts is still to be built -- this view is ready for it to attach."""

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


@client.event
async def on_ready():
    # Sync already happened once in setup_hook -- this just logs connection
    # status, safe to fire on every reconnect.
    log.info(f"Logged in as {client.user}")


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

        qqq_volume_ratio = compute_volume_ratio(qqq_df)
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
