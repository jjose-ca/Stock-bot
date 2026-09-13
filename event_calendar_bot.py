#!/usr/bin/env python3
"""
event_calendar_bot.py

Producer bot for the Stock-bot suite. Runs once per day (early, before the
other bots' first cron slot) and writes a single event_flags.json file that
every other bot can read via event_flags.py instead of re-fetching data
itself.

What it covers, and why each is handled the way it is:

  - FOMC meetings        -> hardcoded schedule (published ~1yr ahead by the
                             Fed). No network call needed; just calendar math.
  - OpEx / quad witching -> pure calendar math (3rd Friday of the month).
  - FRED macro releases  -> CPI, PCE, NFP release *dates* are published
                             months ahead and rarely move, so these are
                             fetched in bulk and cached; the daily run just
                             checks the cache (refreshed ~monthly).
  - Top-holding earnings -> the one category that's NOT stable weeks out
                             (companies confirm/shift dates close to the
                             event), so this is re-fetched every day for a
                             rolling lookahead window.

Data sources: this version uses yfinance (unofficial, reverse-engineered
Yahoo endpoints -- no API key needed, but no SLA either) for both the
macro release calendar and top-holding earnings dates, instead of FRED
and Finnhub. That trade-off is deliberate -- see the docstrings on
refresh_macro_cache_if_stale() and fetch_earnings_window() below for
what to watch out for and how to fall back to FRED/Finnhub if yfinance's
output turns out to be unreliable in practice.

Environment variables (all optional -- the bot degrades gracefully and
flags a section as unavailable rather than crashing on failure):

  DISCORD_URL       - reuse the same webhook the other bots use, for a
                       daily summary + error visibility
  EVENT_BOT_DATA_DIR         - where to write output (default /opt/stock-bot/data)
  EVENT_BOT_LOOKAHEAD_DAYS   - rolling window size (default 5)

Output:
  event_flags.json          - today's flags + a rolling lookahead window
  macro_release_cache.json  - cached macro release dates (refreshed ~monthly)

Suggested cron (staggered ahead of your other bots, matching the pattern
in your existing crontab):

  CRON_TZ=America/Toronto
  0 7 * * 1-5  /usr/bin/python3 /opt/stock-bot/event_calendar_bot.py >> /var/log/event_calendar_bot.log 2>&1
"""

import os
import json
import logging
import calendar
import datetime as dt
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests  # kept for the Discord webhook post
import yfinance as yf

# All "today"/"now" calculations are anchored to US Eastern time, not the
# VPS's local clock. Most cloud providers (AWS, DigitalOcean, etc.) default
# a fresh VPS to UTC, and dt.date.today() on a UTC clock rolls over to the
# next calendar date at 7-8pm ET (depending on DST) -- right in the middle
# of, or just after, the trading session this bot exists to flag. Pinning
# to ET keeps "today" aligned with the market's own day boundary regardless
# of what timezone the box itself is set to.
ET = ZoneInfo("America/New_York")


def today_et() -> dt.date:
    return dt.datetime.now(ET).date()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("EVENT_BOT_DATA_DIR", "/opt/stock-bot/data"))
FLAGS_PATH = DATA_DIR / "event_flags.json"
MACRO_CACHE_PATH = DATA_DIR / "macro_release_cache.json"

DISCORD_URL = os.environ.get("DISCORD_URL")

LOOKAHEAD_DAYS = int(os.environ.get("EVENT_BOT_LOOKAHEAD_DAYS", "5"))

# Update this quarterly alongside Nasdaq-100 rebalances -- pull the current
# top-weight names from Invesco's QQQ holdings page (or any holdings data
# provider) and paste the tickers here. Doesn't need to be exact to the
# last name; it just needs to cover names heavy enough to move QQQ/TQQQ.
# (Snapshot used here: Sep 2026 top-10-ish QQQ/QQHG weights.)
TOP_HOLDINGS = [
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG",
    "AVGO", "META", "TSLA", "NFLX",
]

# FOMC schedule. 2026 dates are confirmed by the Fed; 2027 dates are the
# Fed's own "tentative" schedule (published Sept 2025) -- historically
# rarely changed, but re-verify against federalreserve.gov once 2027 opens.
# Tuple: (meeting_day1, meeting_day2/statement_day, has_SEP_dot_plot)
FOMC_MEETINGS = [
    # 2026 -- confirmed
    (dt.date(2026, 1, 27), dt.date(2026, 1, 28), False),
    (dt.date(2026, 3, 17), dt.date(2026, 3, 18), True),
    (dt.date(2026, 4, 28), dt.date(2026, 4, 29), False),
    (dt.date(2026, 6, 16), dt.date(2026, 6, 17), True),
    (dt.date(2026, 7, 28), dt.date(2026, 7, 29), False),
    (dt.date(2026, 9, 15), dt.date(2026, 9, 16), True),
    (dt.date(2026, 10, 27), dt.date(2026, 10, 28), False),
    (dt.date(2026, 12, 8), dt.date(2026, 12, 9), True),
    # 2027 -- tentative, re-verify closer to the year
    (dt.date(2027, 1, 26), dt.date(2027, 1, 27), False),
    (dt.date(2027, 3, 16), dt.date(2027, 3, 17), True),
    (dt.date(2027, 4, 27), dt.date(2027, 4, 28), False),
    (dt.date(2027, 6, 8), dt.date(2027, 6, 9), True),
    (dt.date(2027, 7, 27), dt.date(2027, 7, 28), False),
    (dt.date(2027, 9, 14), dt.date(2027, 9, 15), True),
    (dt.date(2027, 10, 26), dt.date(2027, 10, 27), False),
    (dt.date(2027, 12, 7), dt.date(2027, 12, 8), False),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [event_calendar_bot] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Calendar math (no network needed)
# ---------------------------------------------------------------------------

def third_friday(year: int, month: int) -> dt.date:
    """Third Friday of the given month (standard monthly options expiration)."""
    c = calendar.Calendar()
    fridays = [
        d for d in c.itermonthdates(year, month)
        if d.month == month and d.weekday() == calendar.FRIDAY
    ]
    return fridays[2]


def is_opex_day(d: dt.date) -> bool:
    return d == third_friday(d.year, d.month)


def is_quad_witching_day(d: dt.date) -> bool:
    return d.month in (3, 6, 9, 12) and is_opex_day(d)


def fomc_flags_for(d: dt.date) -> dict:
    for start, end, has_sep in FOMC_MEETINGS:
        if start <= d <= end:
            return {
                "is_fomc_meeting_day": True,
                "is_fomc_statement_day": d == end,
                "fomc_statement_time_et": "14:00" if d == end else None,
                "fomc_has_sep_dot_plot": has_sep,
            }
    return {
        "is_fomc_meeting_day": False,
        "is_fomc_statement_day": False,
        "fomc_statement_time_et": None,
        "fomc_has_sep_dot_plot": False,
    }


def days_to_next_fomc_statement(d: dt.date) -> Optional[int]:
    upcoming = [end for _, end, _ in FOMC_MEETINGS if end >= d]
    if not upcoming:
        return None
    return (min(upcoming) - d).days


# ---------------------------------------------------------------------------
# FRED macro release dates (bulk-fetched, cached ~monthly)
# ---------------------------------------------------------------------------

def refresh_macro_cache_if_stale(max_age_days: int = 25) -> dict:
    # Always defined up front (not just inside the `if exists` branch) so
    # a fetch failure below has something to fall back to even on a
    # first-ever run or a corrupt cache file, instead of wiping whatever
    # we had for the next ~25 days.
    cached = {"releases": {}}

    if MACRO_CACHE_PATH.exists():
        try:
            cached = json.loads(MACRO_CACHE_PATH.read_text())
            fetched_at = dt.date.fromisoformat(cached["fetched_at"])
            if (today_et() - fetched_at).days < max_age_days:
                return cached
        except Exception as exc:
            log.warning("Macro cache unreadable, refetching: %s", exc)

    today = today_et()
    horizon = today + dt.timedelta(days=180)
    old_releases = cached.get("releases", {})
    releases = {"cpi": old_releases.get("cpi", []),
                "pce": old_releases.get("pce", []),
                "nfp": old_releases.get("nfp", [])}

    # NOTE: yfinance is an unofficial, reverse-engineered client for Yahoo's
    # undocumented endpoints -- it has broken before when Yahoo changed
    # something server-side, and this economic-events calendar is
    # aggregated from third-party contributors rather than sourced directly
    # from BLS/BEA the way FRED is. The column names below ("Event",
    # "Event Date") are per yfinance's public docs but haven't been
    # confirmed against live output from this environment -- the first run
    # logs the raw columns so you can sanity-check them once, and the
    # try/except means a bad or reshaped response degrades to "keep
    # whatever was cached before" rather than silently wiping all three
    # categories at once.
    try:
        calendars = yf.Calendars(start=today.isoformat(), end=horizon.isoformat())
        # yfinance caps this endpoint at 100 regardless of what's requested
        events_df = calendars.get_economic_events_calendar(limit=100)

        if events_df is not None and not events_df.empty:
            log.info("yfinance economic events columns: %s", list(events_df.columns))
            fresh = {"cpi": [], "pce": [], "nfp": []}
            for _, row in events_df.iterrows():
                event_name = str(row.get("Event", "")).lower()
                event_date = row["Event Date"].date().isoformat()

                if "cpi" in event_name or "consumer price index" in event_name:
                    fresh["cpi"].append(event_date)
                elif "pce" in event_name or "personal consumption expenditure" in event_name:
                    fresh["pce"].append(event_date)
                elif "nonfarm payrolls" in event_name:
                    fresh["nfp"].append(event_date)

            # Only overwrite a category if the fresh fetch actually found
            # something for it -- an empty list for "pce" this run more
            # likely means the string match missed than that there's truly
            # no PCE release in the next 180 days.
            for name in ("cpi", "pce", "nfp"):
                if fresh[name]:
                    releases[name] = fresh[name]
        else:
            log.warning("yfinance economic events calendar returned no rows")
    except Exception as exc:
        log.error("yfinance macro fetch failed, keeping previous cache: %s", exc)

    cache = {"fetched_at": today.isoformat(), "releases": releases}
    MACRO_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    return cache


def macro_flags_for(d: dt.date, macro_cache: dict) -> dict:
    releases = macro_cache.get("releases", {})
    iso = d.isoformat()
    return {
        "is_cpi_day": iso in releases.get("cpi", []),
        "is_pce_day": iso in releases.get("pce", []),
        "is_nfp_day": iso in releases.get("nfp", []),
    }


# ---------------------------------------------------------------------------
# Earnings calendar for top QQQ/TQQQ holdings (fetched daily -- these dates
# aren't fully stable until close to the event, unlike the above)
# ---------------------------------------------------------------------------

def fetch_earnings_window(start: dt.date, end: dt.date) -> dict:
    """Return {ticker: earnings_date_iso} for TOP_HOLDINGS reporting between
    start and end (inclusive), via yfinance's Ticker.get_earnings_dates().

    NOTE: get_earnings_dates(limit=N) returns N rows from Yahoo without a
    documented, confirmed guarantee here about ordering (most-recent-past
    vs. next-upcoming first). A limit that's too small risks returning
    only past dates for a ticker that just reported, silently missing its
    next one. To guard against that: pull a larger batch than you'd think
    you need and explicitly filter for date >= today, rather than trusting
    the first few rows returned.
    """
    out = {}
    today = today_et()
    for symbol in TOP_HOLDINGS:
        try:
            ticker = yf.Ticker(symbol)
            dates_df = ticker.get_earnings_dates(limit=12)

            if dates_df is None or dates_df.empty:
                continue

            future_dates = sorted(
                idx.date() for idx in dates_df.index if idx.date() >= today
            )
            in_window = [d for d in future_dates if start <= d <= end]
            if in_window:
                out[symbol] = in_window[0].isoformat()  # nearest upcoming
        except Exception as exc:
            log.error("yfinance earnings fetch failed for %s: %s", symbol, exc)

    return out


# ---------------------------------------------------------------------------
# Discord summary (reuses the same webhook pattern as the other bots)
# ---------------------------------------------------------------------------

def post_to_discord(message: str) -> None:
    if not DISCORD_URL:
        return
    try:
        requests.post(DISCORD_URL, json={"content": message}, timeout=10)
    except Exception as exc:
        log.error("Discord post failed: %s", exc)


def summarize_day(day_flags: dict) -> str:
    bits = []
    if day_flags.get("is_fomc_statement_day"):
        bits.append(
            "FOMC statement"
            + (" (+SEP/dot plot)" if day_flags.get("fomc_has_sep_dot_plot") else "")
        )
    elif day_flags.get("is_fomc_meeting_day"):
        bits.append("FOMC meeting (day 1)")
    if day_flags.get("is_cpi_day"):
        bits.append("CPI")
    if day_flags.get("is_pce_day"):
        bits.append("PCE")
    if day_flags.get("is_nfp_day"):
        bits.append("NFP")
    if day_flags.get("is_quad_witching_day"):
        bits.append("quad witching")
    elif day_flags.get("is_opex_day"):
        bits.append("monthly OpEx")
    if day_flags.get("top_holding_earnings"):
        bits.append("earnings: " + ", ".join(day_flags["top_holding_earnings"]))
    return "; ".join(bits) if bits else "no flagged events"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_day_flags(d: dt.date, macro_cache: dict, earnings_by_date: dict) -> dict:
    flags = {"date": d.isoformat()}
    flags.update(fomc_flags_for(d))
    flags.update(macro_flags_for(d, macro_cache))
    flags["is_opex_day"] = is_opex_day(d)
    flags["is_quad_witching_day"] = is_quad_witching_day(d)
    flags["top_holding_earnings"] = [
        sym for sym, edate in earnings_by_date.items() if edate == d.isoformat()
    ]
    flags["days_to_next_fomc_statement"] = days_to_next_fomc_statement(d)
    flags["is_high_impact_day"] = bool(
        flags["is_fomc_statement_day"]
        or flags["is_cpi_day"]
        or flags["is_nfp_day"]
        or flags["is_pce_day"]
        or flags["is_quad_witching_day"]
        or flags["top_holding_earnings"]
    )
    return flags


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    today = today_et()
    horizon = today + dt.timedelta(days=LOOKAHEAD_DAYS)

    macro_cache = refresh_macro_cache_if_stale()
    earnings_by_date = fetch_earnings_window(today, horizon)

    days = []
    d = today
    while d <= horizon:
        days.append(build_day_flags(d, macro_cache, earnings_by_date))
        d += dt.timedelta(days=1)

    output = {
        "generated_at": dt.datetime.now(ET).isoformat(timespec="seconds"),
        "today": days[0],
        "lookahead": days[1:],
    }

    tmp_path = FLAGS_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(output, indent=2))
    tmp_path.replace(FLAGS_PATH)  # atomic swap -- a concurrent reader
    # never sees a partially-written file
    log.info("Wrote %s", FLAGS_PATH)

    summary_lines = [f"**Event calendar -- {today.isoformat()}**"]
    for day in days:
        label = "TODAY" if day["date"] == today.isoformat() else day["date"]
        summary_lines.append(f"{label}: {summarize_day(day)}")
    post_to_discord("\n".join(summary_lines))


if __name__ == "__main__":
    main()
