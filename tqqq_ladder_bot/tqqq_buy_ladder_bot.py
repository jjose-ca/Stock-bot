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
    annotate_support_with_ladder,
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

LADDER_LOG_PATH = Path("ladder_log.jsonl")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buy_ladder_bot")


# ---------- Data fetching ----------

def fetch_daily_bars(symbol: str, lookback_days: int = 250) -> pd.DataFrame:
    """Blocking network call -- must be run via asyncio.to_thread from
    inside the Discord event loop."""
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


def log_ladder(shares: float, basis_price: float, current_tqqq_price: float,
                qqq_atr_pct: float, ladder: list) -> dict:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "shares": shares,
        "tqqq_basis_price": basis_price,
        "tqqq_current_price": current_tqqq_price,
        "qqq_atr_pct": round(qqq_atr_pct * 100, 2),
        "ladder": ladder_to_dicts(ladder),
    }
    with open(LADDER_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


# ---------- Discord bot ----------

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    log.info(f"Logged in as {client.user}, slash commands synced")


@tree.command(name="buyfilled", description="Get your next 3 TQQQ buy levels, anchored to your average cost")
@app_commands.describe(
    shares="Total position size (shares held)",
    price="Your average cost basis (not today's market price)",
)
async def buyfilled(interaction: discord.Interaction, shares: float, price: float):
    await interaction.response.defer(thinking=True)
    try:
        qqq_df, tqqq_df = await asyncio.to_thread(get_market_data)

        qqq_close = float(qqq_df["close"].iloc[-1])
        current_tqqq_price = float(tqqq_df["close"].iloc[-1])
        qqq_atr_pct = compute_atr_pct(qqq_df)

        # The ladder itself uses NO swing-low data (frequency-of-fill decision).
        ladder = build_ladder(
            basis_price=price,
            current_tqqq_price=current_tqqq_price,
            qqq_close=qqq_close,
            qqq_atr_pct=qqq_atr_pct,
            qqq_swing_lows=pd.Series(dtype=float),
        )

        # Support is computed separately, purely for display -- it never
        # feeds into the ladder above. Search the span the ladder covers:
        # from the deepest computed level up to the current price.
        low_bound = min((lvl.price for lvl in ladder), default=0.0)
        qqq_swing_lows = find_confirmed_swing_lows(qqq_df)
        support_levels = find_support_in_range(
            tqqq_reference_price=current_tqqq_price,
            qqq_close=qqq_close,
            qqq_swing_lows=qqq_swing_lows,
            low_bound=low_bound,
            high_bound=current_tqqq_price,
            max_results=MAX_SUPPORT_DISPLAY,
        )

        log_ladder(shares, price, current_tqqq_price, qqq_atr_pct, ladder)

        embed = discord.Embed(
            title=f"TQQQ position: {shares:g} sh @ ${price:.2f} basis",
            description=(
                f"**TQQQ now:** ${current_tqqq_price:.2f} | "
                f"**QQQ close:** ${qqq_close:.2f} | "
                f"**QQQ 14-day ATR:** {qqq_atr_pct * 100:.2f}%"
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        if not ladder:
            embed.add_field(
                name="No levels found",
                value="No valid levels below current price within the search range -- ATR may be very low, or price has moved far below basis already.",
                inline=False,
            )
        else:
            for i, lvl in enumerate(ladder, start=1):
                embed.add_field(
                    name=f"Buy {i}: ${lvl.price:.2f} (-{lvl.tqqq_drop_pct:.1f}% from basis)",
                    value=f"QQQ -{lvl.qqq_drop_pct:.1f}% [{lvl.basis}]",
                    inline=False,
                )

        if support_levels:
            position_labels = annotate_support_with_ladder(support_levels, ladder, current_tqqq_price)
            support_lines = []
            for s, label in zip(support_levels, position_labels):
                support_lines.append(
                    f"${s.tqqq_price:.2f} — {label}\n"
                    f"    QQQ swing low from {s.swing_low_date} (QQQ -{s.qqq_drop_pct:.1f}%)"
                )
            embed.add_field(
                name="📍 Support nearby (info only, not a buy target)",
                value="\n".join(support_lines),
                inline=False,
            )

        embed.set_footer(text="Pure ATR spacing, anchored to your basis -- update it only after a real fill")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        log.exception("buyfilled failed")
        await interaction.followup.send(f"Error computing ladder: {e}")


if __name__ == "__main__":
    client.run(TOKEN)
