"""
ladder_core.py

Pure calculation logic for the TQQQ averaging-down ladder. No Discord
dependency here on purpose -- this module is imported by both the live
bot (tqqq_buy_ladder_bot.py) and the backtest (backtest_ladder.py), so
whatever gets validated in the backtest is the exact code that runs live.

Design rules encoded here (from the full design discussion):
1. Structure (ATR%, swing lows) is computed on QQQ only. TQQQ's own price
   history never feeds the structural calculation -- it's a decayed
   series and not a clean read of "real" support.
2. The ladder is anchored to your average cost basis, and ONLY your
   basis. It is never re-derived from today's live price -- that's the
   "moving goalposts" bug: recomputing the gap from wherever price
   happens to be pushes the target away from the market instead of
   letting the market approach a fixed target.
3. Because execution is manual (no resting/auto-filled orders), some
   computed levels can already be stale by the time you check --
   price may have fallen through them unnoticed. So after computing a
   level from the basis, it's discarded if it's not below the current
   live price, and the search extends to further ATR multiples until
   REQUIRED_LEVELS levels are found that are still genuinely ahead of
   the market.
4. QQQ % drop is scaled by LEVERAGE_FACTOR (~3x) and applied to your
   TQQQ basis price -- so decay-to-date is automatically absorbed by
   using your actual current basis, not a static historical TQQQ price.
"""

from dataclasses import dataclass, asdict
from typing import Optional
import pandas as pd

ATR_PERIOD = 14
SWING_LOOKBACK_DAYS = 90
SWING_FRACTAL_WINGS = 3          # bars on each side required to confirm a swing low
SWING_SNAP_TOLERANCE_ATR = 0.5   # snap window, in multiples of QQQ ATR%
REQUIRED_LEVELS = 3              # target ladder count
LEVERAGE_FACTOR = 3.0            # TQQQ leverage factor vs QQQ
ATR_STEP = 0.5                   # increment per search iteration (backtest-confirmed:
                                  # meaningfully higher fill rate than 1.0 without
                                  # dropping into 0.25's noise-level ~1.3% triggers)
MAX_ATR_MULTIPLE = 15.0          # safety cap so the search can't run forever


# ---------- Structure calculations (QQQ only) ----------

def compute_atr_pct(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """14-day ATR expressed as a % of the latest close (scale invariant,
    portable across price regimes and splits)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1 / period, adjust=False).mean()
    return float(atr_series.iloc[-1]) / float(close.iloc[-1])


def compute_atr_pct_series(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Same as compute_atr_pct but returns the full historical series --
    needed for the backtest to walk forward day by day."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1 / period, adjust=False).mean()
    return atr_series / close


def find_confirmed_swing_lows(df: pd.DataFrame, wings: int = SWING_FRACTAL_WINGS,
                                lookback_days: int = SWING_LOOKBACK_DAYS) -> pd.Series:
    """Confirmed swing-low prices: low[i] is the minimum of the surrounding
    `wings` bars on each side. Only bars with `wings` bars of confirmation
    AFTER them are included -> no lookahead bias."""
    recent = df.tail(lookback_days + wings)
    lows = recent["low"]
    swing_lows = {}
    for i in range(wings, len(lows) - wings):
        window = lows.iloc[i - wings: i + wings + 1]
        if lows.iloc[i] == window.min():
            swing_lows[recent.index[i]] = lows.iloc[i]
    return pd.Series(swing_lows, dtype=float)


def find_confirmed_swing_lows_asof(df: pd.DataFrame, as_of_idx: int,
                                     wings: int = SWING_FRACTAL_WINGS,
                                     lookback_days: int = SWING_LOOKBACK_DAYS) -> pd.Series:
    """Same as find_confirmed_swing_lows, but computed using only data up
    to and including row `as_of_idx` -- for walk-forward backtesting so no
    future data leaks into a given day's swing-low set."""
    window_df = df.iloc[max(0, as_of_idx - lookback_days - wings): as_of_idx + 1]
    return find_confirmed_swing_lows(window_df, wings=wings, lookback_days=lookback_days)


# ---------- Ladder ----------

@dataclass
class LadderLevel:
    price: float
    qqq_drop_pct: float
    tqqq_drop_pct: float
    basis: str
    swing_low_date: Optional[str] = None


def build_ladder(
    basis_price: float,
    current_tqqq_price: float,
    qqq_close: float,
    qqq_atr_pct: float,
    qqq_swing_lows: pd.Series,
    leverage: float = LEVERAGE_FACTOR,
    required_levels: int = REQUIRED_LEVELS,
    snap_tolerance_atr: float = SWING_SNAP_TOLERANCE_ATR,
    max_mult: float = MAX_ATR_MULTIPLE,
    step: float = ATR_STEP,
) -> list:
    """
    Build the next `required_levels` buy prices.

    - Anchor is ALWAYS basis_price. It is never replaced by
      current_tqqq_price -- current_tqqq_price is only used to filter out
      levels that are no longer ahead of the market.
    - Structure (ATR%, swing lows) comes from QQQ, translated to a %
      drop, scaled by `leverage`, and applied to basis_price.
    - A level is only kept if it lands strictly below current_tqqq_price
      (otherwise the market has already passed it) and hasn't already
      been produced (dedup).
    """
    levels = []
    seen_prices = set()
    swing_below = qqq_swing_lows[qqq_swing_lows < qqq_close].sort_values(ascending=False)

    mult = step
    while len(levels) < required_levels and mult <= max_mult:
        raw_qqq_drop_pct = mult * qqq_atr_pct
        target_qqq_price = qqq_close * (1.0 - raw_qqq_drop_pct)

        snap_date = None
        final_qqq_drop_pct = raw_qqq_drop_pct

        if not swing_below.empty:
            diffs = (swing_below - target_qqq_price).abs()
            closest_date = diffs.idxmin()
            closest_price = swing_below.loc[closest_date]
            if diffs.loc[closest_date] <= (snap_tolerance_atr * qqq_atr_pct * qqq_close):
                final_qqq_drop_pct = (qqq_close - closest_price) / qqq_close
                snap_date = closest_date.strftime("%Y-%m-%d") if hasattr(closest_date, "strftime") else str(closest_date)

        tqqq_drop_pct = final_qqq_drop_pct * leverage
        target_price = round(basis_price * (1.0 - tqqq_drop_pct), 2)

        # Filter: must be a real price, below current market, and not a duplicate
        if 0 < target_price < current_tqqq_price and target_price not in seen_prices:
            seen_prices.add(target_price)
            levels.append(LadderLevel(
                price=target_price,
                qqq_drop_pct=round(final_qqq_drop_pct * 100, 2),
                tqqq_drop_pct=round(tqqq_drop_pct * 100, 2),
                basis=f"QQQ ATR x{mult:g}",
                swing_low_date=snap_date,
            ))

        mult += step

    return levels


def ladder_to_dicts(ladder: list) -> list:
    return [asdict(l) for l in ladder]


# ---------- Support visibility (informational only -- does NOT alter the ladder) ----------

@dataclass
class SupportLevel:
    tqqq_price: float
    qqq_price: float
    qqq_drop_pct: float
    swing_low_date: str


def translate_qqq_price_to_tqqq(qqq_price: float, qqq_close: float, tqqq_reference_price: float,
                                  leverage: float = LEVERAGE_FACTOR) -> float:
    """Translate a QQQ price into the equivalent TQQQ price.

    Anchored to `tqqq_reference_price`, which should be the CURRENT live
    TQQQ price, not your cost basis. This is deliberately different from
    build_ladder's anchor: the ladder anchors to basis because it's
    issuing actionable buy targets (an order commitment shouldn't drift
    with the market -- that's the moving-goalposts problem). This
    function instead answers "where does real QQQ structure sit relative
    to TODAY's price" -- a factual statement, not a commitment -- so it
    must anchor to today's live price to correctly absorb realized decay.
    Anchoring this to basis instead would silently misplace support by
    the full basis-vs-live gap, which can be large after any drawdown or
    rally since your original fill."""
    qqq_drop_pct = (qqq_close - qqq_price) / qqq_close
    tqqq_drop_pct = qqq_drop_pct * leverage
    return round(tqqq_reference_price * (1.0 - tqqq_drop_pct), 2)


def find_support_in_range(
    tqqq_reference_price: float,
    qqq_close: float,
    qqq_swing_lows: pd.Series,
    low_bound: float,
    high_bound: float,
    leverage: float = LEVERAGE_FACTOR,
    max_results: int = 3,
) -> list:
    """Find confirmed QQQ swing lows whose translated TQQQ price falls
    within [low_bound, high_bound] -- i.e. within the span the ladder
    covers. Purely informational: this does NOT feed into build_ladder or
    alter any computed price. Sorted nearest-to-current-price first.

    `tqqq_reference_price` should be the CURRENT live TQQQ price (same
    anchor used for `high_bound`, which is also current price) -- keeping
    every price in this comparison in the same coordinate system. Passing
    a basis price here instead would compare basis-anchored translations
    against a live-price bound, which can silently drop or misplace
    valid nearby support whenever basis has diverged from live price."""
    results = []
    swing_below = qqq_swing_lows[qqq_swing_lows < qqq_close]

    for date, qqq_price in swing_below.items():
        tqqq_price = translate_qqq_price_to_tqqq(qqq_price, qqq_close, tqqq_reference_price, leverage)
        if low_bound <= tqqq_price <= high_bound:
            qqq_drop_pct = (qqq_close - qqq_price) / qqq_close
            results.append(SupportLevel(
                tqqq_price=tqqq_price,
                qqq_price=float(qqq_price),
                qqq_drop_pct=round(qqq_drop_pct * 100, 2),
                swing_low_date=date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date),
            ))

    results.sort(key=lambda s: -s.tqqq_price)  # nearest to current price first
    return results[:max_results]


def support_to_dicts(support: list) -> list:
    return [asdict(s) for s in support]


# ---------- Cross-referencing support against specific ladder rungs ----------

def annotate_support_with_ladder(support_levels: list, ladder: list, current_price: float) -> list:
    """For each SupportLevel, report the nearest reference point (current
    price or a specific ladder rung) and the exact dollar/percent gap to
    it -- no threshold, no "aligned vs not" judgment call. Purely
    descriptive -- returns a list of label strings parallel to
    support_levels; doesn't alter support_levels, the ladder, or any price."""
    points = [("current price", current_price)] + [(f"Level {i}", lvl.price) for i, lvl in enumerate(ladder, start=1)]

    labels = []
    for s in support_levels:
        nearest_name, nearest_price = min(points, key=lambda p: abs(s.tqqq_price - p[1]))
        gap = s.tqqq_price - nearest_price
        gap_pct = (gap / nearest_price * 100) if nearest_price else 0.0
        direction = "above" if gap > 0 else "below" if gap < 0 else "at"
        labels.append(
            f"closest to {nearest_name} (${nearest_price:.2f}), "
            f"${abs(gap):.2f} {direction} ({abs(gap_pct):.2f}%)"
        )

    return labels
