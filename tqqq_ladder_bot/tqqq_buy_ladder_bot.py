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
    return df[["open", "high", "low", "close", "volume"]]


def get_market_data() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch QQQ (for structure) and TQQQ (for the live-price filter)."""
    qqq_df = fetch_daily_bars("QQQ")
    tqqq_df = fetch_daily_bars("TQQQ")
    return qqq_df, tqqq_df


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


# ---------- Discord bot ----------

intents = discord.Intents.default()


class LadderBotClient(discord.Client):
    """Custom Client subclass so slash-command sync happens exactly once,
    in setup_hook (called once before the first connection) -- NOT in
    on_ready, which Discord can fire multiple times over a long-running
    bot's life (initial connect AND every subsequent reconnect/resume).
    Repeated tree.sync() calls on every reconnect risk hitting Discord's
    rate limits for no benefit, since the command set doesn't change
    between reconnects."""

    async def setup_hook(self):
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


@tree.command(name="buyfilled", description="Get your next 3 TQQQ buy levels, anchored to your average cost")
@app_commands.describe(
    shares="Total position size (shares held)",
    price="Your average cost basis (not today's market price)",
)
async def buyfilled(interaction: discord.Interaction, shares: float, price: float):
    await interaction.response.defer(thinking=True)
    if shares <= 0 or price <= 0:
        await interaction.followup.send(
            f"Both shares ({shares:g}) and price (${price:.2f}) must be positive numbers -- "
            f"check your input and try again."
        )
        return
    try:
        qqq_df, tqqq_df = await asyncio.to_thread(get_market_data)

        qqq_close = float(qqq_df["close"].iloc[-1])
        current_tqqq_price = float(tqqq_df["close"].iloc[-1])
        qqq_atr_pct = compute_atr_pct(qqq_df)
        market_data_last_date = qqq_df.index[-1].strftime("%Y-%m-%d")
        regime = compute_regime_status(qqq_df)

        # The ladder itself uses swing-low data only if USE_CONFLUENCE is
        # True -- this flag now actually controls the behavior (previously
        # dead: the empty Series was hardcoded here regardless of the
        # flag's value, so build_ladder's snap-to-swing-low logic and this
        # flag could silently diverge). Mirrors exactly how
        # backtest_ladder.py's simulate() decides the same thing.
        qqq_swing_lows_all = find_confirmed_swing_lows(qqq_df)
        ladder = build_ladder(
            basis_price=price,
            current_tqqq_price=current_tqqq_price,
            qqq_close=qqq_close,
            qqq_atr_pct=qqq_atr_pct,
            qqq_swing_lows=qqq_swing_lows_all if USE_CONFLUENCE else pd.Series(dtype=float),
        )

        # Support display is unconditional -- independent of USE_CONFLUENCE,
        # since it never fed into the ladder, only informational display.
        # Reuses the same swing-low fetch above rather than re-computing.
        # Search range extends one extra ATR step below the deepest rung --
        # a support level sitting just past your last order is still worth
        # knowing about. The buffer is ATR-scaled (not a fixed %) to stay
        # consistent with every other distance in this bot, which all
        # scale with current volatility rather than a flat guess.
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

        embed.add_field(name="TQQQ now", value=f"${current_tqqq_price:.2f}", inline=True)
        embed.add_field(name="QQQ close", value=f"${qqq_close:.2f}", inline=True)
        embed.add_field(name="QQQ 14-day ATR", value=f"{qqq_atr_pct * 100:.2f}%", inline=True)

        support_positions = locate_support_relative_to_ladder(support_levels, ladder, current_tqqq_price)
        # Group support lines by which ladder level they're nearest to, so
        # a confirming support level shows up directly under the buy
        # target it backs up, instead of a disconnected list. Computed
        # BEFORE the ladder-empty check below, and unattached_support is
        # rendered unconditionally after it (see fix note there) -- a
        # crash-scenario empty ladder must not silently drop support that
        # was already computed and is arguably most useful right then.
        # "Confirms" is reserved for genuinely close matches (within
        # CONFIRMS_TOLERANCE_PCT); anything farther says "nearest to"
        # instead -- locate_support_relative_to_ladder is pure
        # nearest-point matching with no distance cutoff, so without
        # this a support level several dollars away would misleadingly
        # read as "confirming" a rung it's not actually near.
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
            # Note: when ladder is empty, EVERY support level's nearest
            # point is "current price" (there are no rungs to be nearer
            # to), so all of it ends up in unattached_support below --
            # correctly surfaced instead of silently dropped.
        else:
            for i, lvl in enumerate(ladder, start=1):
                # lvl.qqq_drop_pct / lvl.tqqq_drop_pct describe the STRUCTURAL
                # distance from basis (mult x ATR%) -- correct and unchanged,
                # but NOT the same as "how far QQQ/TQQQ must still move from
                # today." Once current price has drifted from basis (true
                # after any real move since the fill), those two distances
                # diverge -- see the labeling issue this fixes. Compute the
                # TRUE distance from current price separately, purely for
                # display; the underlying target price itself is untouched.
                tqqq_pct_from_now = (current_tqqq_price - lvl.price) / current_tqqq_price * 100
                qqq_pct_from_now = tqqq_pct_from_now / LEVERAGE_FACTOR

                field_value = (
                    f"QQQ needs ~{qqq_pct_from_now:.1f}% more drop from today "
                    f"[{lvl.label}, {lvl.tqqq_drop_pct:.1f}% below basis]"
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
        await interaction.followup.send(embed=embed)

    except Exception as e:
        log.exception("buyfilled failed")
        await interaction.followup.send(f"Error computing ladder: {e}")


if __name__ == "__main__":
    client.run(TOKEN)
