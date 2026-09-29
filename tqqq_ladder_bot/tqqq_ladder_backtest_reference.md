# TQQQ Ladder Backtest — Combinations Tested

Reference sheet for `intraday_sweep.py`. Covers every ATR ladder and share-tranche
plan defined this session, which ones were actually run against each other, and
what the results pointed to. All ladders/plans below have exactly 9 values
(tranche 1 through tranche 9); tranche 1 is your entry (5% dip, or ATR-based for
`armed`), so ladders' tranche-1 slot is unused for pricing.

**Important gotcha, discovered partway through:** for `dip`/`armed` entry, the
script only uses the **first 8** of a ladder's 9 values (`resolve_mults`
truncates to `N_TRANCHES - 1`). Every ladder below was defined with 9 values,
but the 9th number never actually took effect in any run — tranche 9 used the
**8th** number instead. The "as tested" column shows what was actually used.

---

## ATR ladders (tranche 2 → tranche 9 multiples)

| Name | As defined (9 values) | As tested (8 values used) |
|---|---|---|
| A | .25, .5, .75, 1, 1.25, 2, 2.5, 3, 3.5 | .25, .5, .75, 1, 1.25, 2, 2.5, **3** |
| B | .25, .5, .75, 1, 1.5, 2, 2.5, 3, 3.5 | .25, .5, .75, 1, 1.5, 2, 2.5, **3** |
| C | .25, .5, .75, 1, 2, 3, 4, 5, 6 | .25, .5, .75, 1, 2, 3, 4, **5** |
| D | .25, .5, .75, 1, 1.5, 2, 3, 4, 5 | .25, .5, .75, 1, 1.5, 2, 3, **4** |
| **E** | .5, .75, 1, 1.5, 2, 2.5, 3, 3.5, 4 | .5, .75, 1, 1.5, 2, 2.5, 3, **3.5** |
| H | .25, .5, .75, 1, 1.25, 1.5, 2, 2, 2 | .25, .5, .75, 1, 1.25, 1.5, **2**, 2 |
| **J** | .5, .75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5 | .5, .75, 1, 1.25, 1.5, 1.75, 2, **2.25** |

Also built (as `--depth-shape-grid`, programmatic, **not yet run**): depth ∈
{2,3,4,6} × shape ∈ {even, widen}. See `intraday_sweep.py`'s
`build_depth_shape_ladders` docstring for the exact formulas.

**A/B/D are nearly identical** — same first 4 values, only diverge from tranche
6 onward. **C** widens sharply late (up to 6.0) and consistently underperformed
on deep-cycle drawdown. **E and J** (both starting at 0.5, skipping the .25
rung) were the two standout ladders — see Findings below.

---

## Share-tranche plans (shares per tranche 1 → 9)

| Name | Values | Total shares |
|---|---|---|
| default (original) | 5, 10, 15, 20, 20, 20, 20, 40, 40 | 190 |
| yours-1 | 5, 10, 15, 20, 10, 10, 20, 20, 20 | 130 |
| yours-2 | 5, 10, 15, 20, 20, 20, 20, 20, 20 | 150 |
| yours-3 | 5, 10, 15, 20, 10, 10, 10, 40, 40 | 160 |
| yours-4 | 10, 10, 20, 20, 20, 20, 20, 20, 20 | 160 |
| yours-5 | 5, 10, 15, 10, 10, 10, 40, 40, 40 | 180 |
| yours-6 | 15, 20, 20, 20, 20, 20, 20, 20, 20 | 175 |
| **F** | 5, 10, 15, 20, 20, 30, 30, 40, 40 | 210 |
| G | 5, 5, 10, 10, 15, 20, 30, 50, 60 | 205 |
| G_smooth | 5, 10, 10, 15, 20, 25, 30, 40, 50 | 205 |
| G_extreme | 5, 5, 5, 10, 10, 15, 25, 55, 90 | 220 |
| P_flat10 | 10, 10, 10, 10, 10, 10, 30, 30, 40 | 140 |
| P_ramp | 5, 10, 15, 20, 10, 10, 20, 30, 40 | 140 |
| P_bump | 10, 20, 20, 10, 10, 10, 20, 30, 40 | 170 |

Proposed but **not yet run**: t1_5 / t1_10 / t1_20 (isolating tranche-1 size
only — `10,10,15,20,20,20,20,40,40` and `20,10,15,20,20,20,20,40,40`, keeping
tranches 2–9 at the default).

**F is the most consistently strong plan** across every ladder tested. **G**
looked best under `fixed` ATR but that edge mostly evaporated under `live`
(genuine mechanism, not noise). **G_extreme** was worse than G — more extreme
back-loading did not help. **G_smooth only outperformed with ladder E** — it's
not a robust plan on its own. **P_flat10** gives the least capital deployed and
the most discount, but the slowest, sometimes-negative-return deep cycles.
**P_ramp** was the best "least-capital-without-giving-up-return" middle
ground. **P_bump** consistently maximized `avg_tranches_filled` /
`pct_all_filled` (i.e. the ladder actually gets used) on both E and J.

---

## Runs actually executed this session (all `entry=dip0.5%` unless noted)

| # | Ladders | Share plans | ATR modes | Targets |
|---|---|---|---|---|
| 1 | A,B,C,D,E,H | yours-1…6, F, G | fixed | 0.5 |
| 2 | E | G, G_smooth, G_extreme, F | fixed, live | 0.5 |
| 3 | A,B,C,D,E,H | F, G_smooth | fixed, live | 0.5 |
| 4 | A,B,C,D,E,H | P_flat10, P_ramp, F | fixed, live | 0.5 |
| 5 | E | P_ramp, F, G_smooth, yours-1 | fixed, live | 0.25, 0.5, 1.0 |
| 6 | E, J | F, P_bump | fixed, live | 0.5 |
| 7 | E, J | F, G_smooth, yours-1, P_ramp, P_bump | fixed, live | 0.5 |
| 8 | E, J | F | fixed, live | 0.5, plus `dip0.25%`/`dip0.75%`/`armed` entry variants |

---

## Bottom-line findings, in order of confidence

1. **Ladder E wins on everyday/full-population return**, almost regardless of
   share plan (won or tied in 9 of 10 combos in run #1, reconfirmed in #3–5).
2. **Ladder J wins on deep-cycle safety** — smaller worst drawdown and better
   deep-cycle return than E, in **all 10** ladder×share-plan combos in run #7,
   by a consistent 2–4.5 points. Mechanism: J's max rung gap is 0.25 ATR vs
   E's widening gaps up to 0.75+, so J wastes far less distance once a cycle
   exhausts the ladder (`drop below the rung you were still waiting on`: J
   ≈0.9–1.0% vs E ≈2.3–2.6%, stable across every share plan and ATR mode).
3. **Share plan F is the most robust** across every ladder — not just good
   with one pairing (unlike G_smooth, which was E-specific).
4. **T=0.5 is the best profit target** — beat both T=0.25 and T=1.0 on
   return-per-30-days in every share plan tested (run #5).
5. **A looser dip entry (0.75%) outperformed the tighter 0.25%/0.5%** on
   return — counter to the "catch daily volatility" intuition this all
   started from (run #8). `armed` (ATR-based) entry beat every dip-% variant
   on full-population return, but showed worse deep-cycle drawdown — a real
   trade-off, not yet fully resolved.
6. At current real QQQ volatility (~1.1% ATR, per a Sept 2026 reading — well
   below the ~2% used in early illustrative hand-math this session), E/J's
   tranche-2 gap (~1.65% beyond the entry) is comfortably reached by most
   cycles — confirmed empirically: `avg_tranches_filled` ≈ 3.8–4.5, not ≈1.

## Open threads not yet run
- t1_5 / t1_10 / t1_20 tranche-1-size isolation test.
- J vs E across share plans at T=0.25 and T=1.0 (only confirmed at T=0.5).
- `armed` entry's deep-cycle trade-off vs `dip`, fully isolated.
- `--depth-shape-grid` (built, never actually run).
- Everything only covers 2022-02 → 2026-09 (your Alpaca minute-bar window) —
  none of this has been checked against the 2020 COVID crash, which only the
  daily-bar `backtest_tranche_atr.py` script can reach.
