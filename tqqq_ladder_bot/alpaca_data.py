"""
alpaca_data.py

Thin wrapper around alpaca-py for the position-tracking bot. Kept
deliberately small and source-agnostic in its OUTPUT shape (plain dicts
with ts/open/high/low/close/volume) so position_core.py's
check_bars_against_targets() never needs to know or care whether the
bars it's scanning came from Alpaca or yfinance -- see
yfinance_reconcile.py, which returns the exact same shape.

Feed is IEX (free tier) by default -- see the extended discussion in
chat: SIP is $99/mo for new subscribers, judged disproportionate for
this bot's scale for now. The ~11.5% missing-1-min-bar rate measured
against IEX is handled elsewhere (range-check across the whole window,
plus the independent yfinance reconciliation pass), not here.
"""

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


def get_client() -> StockHistoricalDataClient:
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in environment.")
    return StockHistoricalDataClient(api_key, secret_key)


def fetch_recent_bars(
    client: StockHistoricalDataClient,
    symbol: str,
    start: datetime,
    end: Optional[datetime] = None,
) -> list:
    """1-min bars for [start, end) on the IEX feed, as plain dicts:
    {"ts": "...Z", "open": .., "high": .., "low": .., "close": .., "volume": ..}

    A minute with no IEX trade simply doesn't appear in the returned
    list at all -- it is the CALLER's job (position_core's gap-tracking,
    driven by comparing the requested window against what's actually
    returned) to notice a timestamp is missing. This function makes no
    attempt to fill gaps; filling them is exactly what the yfinance
    reconciliation pass is for, not something to paper over here."""
    end = end or datetime.now(timezone.utc)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    resp = client.get_stock_bars(req)
    df = resp.df

    if df.empty:
        return []

    if hasattr(df.index, "nlevels") and df.index.nlevels > 1:
        df = df.xs(symbol, level=0)

    bars = []
    for ts, row in df.iterrows():
        ts_utc = ts.tz_convert(timezone.utc) if ts.tzinfo else ts.tz_localize(timezone.utc)
        bars.append({
            "ts": ts_utc.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        })
    return bars


def fetch_latest_trade_price(client: StockHistoricalDataClient, symbol: str) -> float:
    """Most recent trade price on IEX, whenever it happened -- NOT tied
    to the current minute. See the chat discussion: this can be a
    minute-or-two stale during a genuinely quiet stretch, never
    "missing" the way a per-minute bar request can be. Used for DISPLAY
    (e.g. the /position embed's current-price line) -- target detection
    itself always goes through fetch_recent_bars + the range-check, not
    this function, since a single point-in-time value (however fetched)
    can miss a brief touch-and-reversion between polls."""
    req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    resp = client.get_stock_latest_trade(req)
    trade = resp[symbol] if isinstance(resp, dict) else resp
    return float(trade.price)


def fetch_latest_trade_price_with_staleness_check(
    client: StockHistoricalDataClient,
    symbol: str,
    max_staleness_minutes: float = 10.0,
) -> float:
    """Same as fetch_latest_trade_price, but raises if the trade is older
    than `max_staleness_minutes` instead of silently returning a stale
    value. Exists specifically for the extended-hours use case in
    tqqq_buy_ladder_bot.py: IEX's extended-hours volume is thinner than
    its already-small regular-session share, so "latest trade" during
    pre/post-market can be meaningfully older than during the regular
    session. Raising here lets the caller's EXISTING try/except fallback
    (drop back to the regular-session close) handle it -- this doesn't
    add a new failure mode, it just makes the existing best-effort
    pattern also catch "technically returned a value, but a stale one,"
    not only outright request failures."""
    req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    resp = client.get_stock_latest_trade(req)
    trade = resp[symbol] if isinstance(resp, dict) else resp

    trade_ts = trade.timestamp
    if trade_ts.tzinfo is None:
        trade_ts = trade_ts.replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - trade_ts).total_seconds() / 60.0
    if age_minutes > max_staleness_minutes:
        raise RuntimeError(
            f"{symbol}'s latest IEX trade is {age_minutes:.1f} min old "
            f"(> {max_staleness_minutes} min threshold) -- likely thin "
            f"extended-hours IEX volume, not a data outage."
        )
    return float(trade.price)


def fetch_daily_bars(
    client: StockHistoricalDataClient,
    symbol: str,
    lookback_days: int = 400,
):
    """Daily OHLCV bars on the IEX feed, as a pandas DataFrame shaped
    exactly like the yfinance version it replaces: columns
    open/high/low/close/volume, DatetimeIndex named "date" -- so
    ladder_core.py's functions (compute_atr_pct, compute_regime_status,
    find_confirmed_swing_lows, etc.) don't need to know or care which
    source produced the DataFrame.

    lookback_days=400 default matches the existing bot's reasoning for
    the same constant: a 200-day SMA needs 200 genuine TRADING days, and
    calendar-day lookback needs headroom over that for weekends/holidays.

    Caveat carried over from the diff-test discussion: a DAILY bar here
    is built entirely from IEX prints for that day. IEX close tracks the
    consolidated close extremely closely (median ~0.014% in the earlier
    1-min diff test), so ATR%/RSI/regime calculations -- all of which key
    off closes -- should be effectively unaffected. Daily high/low could
    in principle miss an intraday extreme that printed on another venue
    but never on IEX; this wasn't specifically measured (the diff test
    only covered 1-min bars), so it's a theoretical gap worth being aware
    of, not one with a measured size the way the close-price diff is."""
    import pandas as pd

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    resp = client.get_stock_bars(req)
    df = resp.df

    if df.empty:
        raise RuntimeError(f"No Alpaca daily data returned for {symbol}")

    if hasattr(df.index, "nlevels") and df.index.nlevels > 1:
        df = df.xs(symbol, level=0)

    df = df.rename(columns={"open": "open", "high": "high", "low": "low",
                             "close": "close", "volume": "volume"})
    df = df[["open", "high", "low", "close", "volume"]]
    df.index = df.index.tz_convert("UTC") if df.index.tz else df.index.tz_localize("UTC")
    df.index.name = "date"
    return df
