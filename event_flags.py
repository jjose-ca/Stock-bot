#!/usr/bin/env python3
"""
event_flags.py

Shared read-only helper for consuming event_calendar_bot.py's output.
Import this from any bot (tqqq_bot.py, tqqq_buy_ladder_bot.py, the SOXL
bots, etc.) instead of re-implementing calendar/earnings-fetch logic in
each one -- same idea as tqqq_data_loaders.py centralizing session
filtering and split adjustment.

Usage:

    from event_flags import get_today_flags, is_high_impact_today, days_to_next_fomc

    if is_high_impact_today():
        # pause new ladder rungs, halve size, widen stops -- whatever
        # fits this particular bot's risk posture
        ...

Deliberately does zero network calls -- it only reads the JSON file that
event_calendar_bot.py already wrote. If that file is missing or stale
(e.g. the producer cron job failed silently overnight), every function
here fails *safe*: it returns "no flag raised" rather than raising an
exception, so a bad event-calendar run degrades a consuming bot's risk
awareness rather than crashing it outright. Bots should still surface the
staleness warning (it's logged here) so the gap actually gets noticed.
"""

import os
import json
import logging
import datetime as dt
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Matches the producer's anchor -- see event_calendar_bot.py's comment on
# why "today" is pinned to ET rather than the VPS's local/UTC clock.
ET = ZoneInfo("America/New_York")


def today_et() -> dt.date:
    return dt.datetime.now(ET).date()


DATA_DIR = Path(os.environ.get("EVENT_BOT_DATA_DIR", "/opt/stock-bot/data"))
FLAGS_PATH = DATA_DIR / "event_flags.json"

MAX_STALENESS_HOURS = int(os.environ.get("EVENT_FLAGS_MAX_STALENESS_HOURS", "20"))

log = logging.getLogger(__name__)

_EMPTY_FLAGS = {
    "date": None,
    "is_fomc_meeting_day": False,
    "is_fomc_statement_day": False,
    "fomc_statement_time_et": None,
    "fomc_has_sep_dot_plot": False,
    "is_cpi_day": False,
    "is_pce_day": False,
    "is_nfp_day": False,
    "is_opex_day": False,
    "is_quad_witching_day": False,
    "top_holding_earnings": [],
    "days_to_next_fomc_statement": None,
    "is_high_impact_day": False,
}


def _load() -> Optional[dict]:
    if not FLAGS_PATH.exists():
        log.warning("event_flags.json not found at %s", FLAGS_PATH)
        return None
    try:
        data = json.loads(FLAGS_PATH.read_text())
    except Exception as exc:
        log.error("event_flags.json unreadable: %s", exc)
        return None

    try:
        generated_at = dt.datetime.fromisoformat(data["generated_at"])
        # generated_at is tz-aware (ET) as written by the producer; compare
        # against an equally tz-aware "now" rather than a naive one, or
        # this raises instead of just warning.
        age_hours = (dt.datetime.now(ET) - generated_at).total_seconds() / 3600
        if age_hours > MAX_STALENESS_HOURS:
            log.warning(
                "event_flags.json is %.1fh old (max %sh) -- producer cron may have failed",
                age_hours, MAX_STALENESS_HOURS,
            )
    except Exception:
        log.warning("event_flags.json missing/invalid generated_at timestamp")

    return data


def get_today_flags() -> dict:
    """Flags for today. Never returns None -- falls back to an all-False
    dict if the producer's output is missing/unreadable, so callers can
    index into the result unconditionally."""
    data = _load()
    if data is None:
        return dict(_EMPTY_FLAGS, date=today_et().isoformat())
    return data["today"]


def get_flags_for(target_date: dt.date) -> dict:
    """Flags for a specific date within the producer's lookahead window
    (today + its configured LOOKAHEAD_DAYS). Falls back to all-False if
    the date is out of range or data is unavailable."""
    data = _load()
    if data is None:
        return dict(_EMPTY_FLAGS, date=target_date.isoformat())
    iso = target_date.isoformat()
    if data["today"]["date"] == iso:
        return data["today"]
    for day in data.get("lookahead", []):
        if day["date"] == iso:
            return day
    return dict(_EMPTY_FLAGS, date=iso)


def is_high_impact_today() -> bool:
    return get_today_flags().get("is_high_impact_day", False)


def is_high_impact_tomorrow() -> bool:
    tomorrow = today_et() + dt.timedelta(days=1)
    return get_flags_for(tomorrow).get("is_high_impact_day", False)


def days_to_next_fomc() -> Optional[int]:
    return get_today_flags().get("days_to_next_fomc_statement")


def earnings_today() -> list:
    return get_today_flags().get("top_holding_earnings", [])


def earnings_within(days: int) -> dict:
    """{ticker: date_iso} for any top holding reporting within the next
    `days` days (inclusive of today), for bots that want a wider window
    than just today/tomorrow before deciding to size down."""
    out = {}
    today = today_et()
    for i in range(days + 1):
        flags = get_flags_for(today + dt.timedelta(days=i))
        for sym in flags.get("top_holding_earnings", []):
            out.setdefault(sym, flags["date"])
    return out
