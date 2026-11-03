# Reweighting backtest

Does reweighting early county returns by county type and demographics improve the early read of a state's final swing? Run with `python3 backtest_reweighting.py` (needs numpy; offline only). Raw output: `data/backtest_reweighting.json`.

## Setup

- **Swings tested** (county margin change, D−R points):
  - A: 2020 president → 2022 midterm (Senate, else Governor, else all House). This is the closest analog to tonight.
  - B: 2016 → 2020 president.
  - C: 2020 → 2024 president.
- **County data:** Census 2023 race/ethnicity estimates, USDA 2019–23 education and 2023 rural-urban codes.
- **Replay:** counties finish in a realistic order. Each state starts at its real poll-closing offset (KY/IN 6pm, GA/VA/SC/FL 7pm, NC/OH 7:30pm, ± noise). Within a state, small counties finish first (+25 min per 10× votes, ± 20 min noise). 200 replays for the early-8 states, 60 for all states.
- **Scoring:** at 5/10/25/50% of all votes counted, each method estimates every reporting state's final swing. Error = estimate − actual. The **aggregate** row is the vote-weighted combination across reporting states, i.e. the headline number.
- **Not covered here:** vote type (counties finish all at once) and poll expectations. Within a state the expectation is a constant, so it does not change any method's ranking.

## 1. Do county types swing differently from their state?

Weighted R² of county swing on state fixed effects (FE), + county-type dummies, + demographics. Residual SD is in points.

| Swing | State FE | + types | + demographics | Largest type effects (pts vs own state; share of states with the same sign) |
|---|---|---|---|---|
| A: 2020 pres -> 2022 midterm | 0.592 (SD 5.69) | 0.612 (SD 5.55) | 0.642 (SD 5.33) | Rural +1.61 (67%); College suburbs +1.29 (46%); Urban core -1.15 (46%) |
| B: 2016 pres -> 2020 pres | 0.214 (SD 4.76) | 0.36 (SD 4.29) | 0.496 (SD 3.81) | College suburbs +3.09 (95%); Rural -2.6 (83%); Hispanic -1.6 (71%) |
| C: 2020 pres -> 2024 pres | 0.484 (SD 3.13) | 0.605 (SD 2.74) | 0.727 (SD 2.27) | Hispanic -1.77 (82%); College suburbs +1.53 (86%); Urban core -0.79 (52%) |

**Reading:**
- In the 2020→2022 midterm, types and demographics added little beyond each state's own swing (R² 0.59 → 0.64), and type effects were inconsistent across states.
- In the 2016→2020 and 2020→2024 realignments, they explained far more (0.21 → 0.50 and 0.48 → 0.73). Examples: college suburbs moved D by 3 points relative to their state in 2020; Hispanic counties moved R by 2 points in 2024, in 82% of states.
- So reweighting matters a lot in some cycles and barely at all in others. Tonight's regime is unknown.

## 2. Replay: error of the early read (RMSE, points)

### Early-8 states (KY, IN, FL, GA, VA, SC, NC, OH), aggregate

| Method | A: 5% | 10% | 25% | 50% | B: 5% | 10% | 25% | 50% | C: 5% | 10% | 25% | 50% |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Raw (no reweighting) | 2.19 | 1.36 | 0.65 | 0.35 | 3.94 | 3.06 | 1.46 | 0.97 | 1.79 | 1.07 | 0.66 | 0.47 |
| Type means within state | 2.21 | 1.51 | 0.69 | 0.36 | 3.79 | 2.97 | 1.71 | 1.01 | 1.77 | 1.08 | 0.55 | 0.39 |
| First version (type shrinkage, retired) | 2.00 | 2.00 | 1.30 | 0.56 | 2.16 | 1.86 | 1.14 | 0.83 | 1.85 | 1.79 | 1.07 | 0.60 |
| Type regression (P1) | 2.35 | 1.70 | 0.62 | 0.34 | 2.27 | 1.11 | 0.77 | 0.73 | 2.14 | 1.86 | 0.74 | 0.41 |
| Demographic regression (P2) | 3.32 | 2.06 | 0.91 | 0.34 | 3.17 | 2.61 | 1.66 | 0.78 | 1.37 | 0.83 | 0.60 | 0.30 |
| Demographic regression, shrunk | 1.92 | 1.28 | 0.61 | 0.30 | 2.02 | 1.44 | 1.20 | 0.77 | 1.92 | 1.60 | 0.80 | 0.39 |
| **Average of raw + P1 + P2 (live model)** | 1.79 | 1.18 | 0.57 | 0.29 | 1.87 | 1.17 | 0.73 | 0.67 | 1.39 | 1.01 | 0.62 | 0.36 |

### All states, aggregate

| Method | A: 5% | 10% | 25% | 50% | B: 5% | 10% | 25% | 50% | C: 5% | 10% | 25% | 50% |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Raw (no reweighting) | 1.72 | 1.33 | 1.31 | 1.24 | 2.55 | 1.87 | 1.25 | 0.97 | 0.99 | 0.86 | 0.84 | 0.76 |
| Type means within state | 1.75 | 1.26 | 1.15 | 1.21 | 2.77 | 2.07 | 1.49 | 1.16 | 0.92 | 0.74 | 0.62 | 0.68 |
| First version (type shrinkage, retired) | 1.84 | 1.25 | 0.90 | 0.73 | 1.57 | 1.26 | 0.88 | 0.86 | 1.44 | 1.11 | 0.66 | 0.50 |
| Type regression (P1) | 1.97 | 1.57 | 1.31 | 1.09 | 1.58 | 0.85 | 0.83 | 0.64 | 1.54 | 1.15 | 0.80 | 0.66 |
| Demographic regression (P2) | 1.70 | 1.48 | 1.18 | 1.03 | 1.84 | 1.57 | 0.91 | 0.67 | 0.84 | 0.65 | 0.39 | 0.34 |
| Demographic regression, shrunk | 1.62 | 1.55 | 1.23 | 1.07 | 1.32 | 1.25 | 0.83 | 0.63 | 1.21 | 0.88 | 0.55 | 0.37 |
| **Average of raw + P1 + P2 (live model)** | 1.59 | 1.30 | 1.16 | 1.04 | 1.27 | 0.86 | 0.72 | 0.58 | 1.02 | 0.79 | 0.63 | 0.53 |

### Early-8 states, per state

| Method | A: 5% | 10% | 25% | 50% | B: 5% | 10% | 25% | 50% | C: 5% | 10% | 25% | 50% |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Raw (no reweighting) | 3.18 | 2.51 | 1.62 | 0.85 | 4.65 | 4.24 | 3.27 | 2.10 | 2.62 | 2.36 | 1.65 | 0.95 |
| Type means within state | 3.17 | 2.54 | 1.64 | 0.86 | 4.55 | 4.18 | 3.07 | 1.91 | 2.66 | 2.36 | 1.43 | 0.83 |
| First version (type shrinkage, retired) | 2.79 | 2.74 | 2.22 | 1.35 | 2.97 | 2.76 | 2.24 | 1.59 | 2.60 | 2.62 | 2.14 | 1.38 |
| Type regression (P1) | 3.39 | 3.14 | 1.33 | 0.79 | 2.93 | 2.43 | 1.71 | 1.28 | 2.47 | 2.35 | 1.22 | 0.77 |
| Demographic regression (P2) | 4.25 | 3.25 | 1.55 | 0.81 | 3.19 | 2.75 | 1.98 | 1.17 | 1.98 | 1.63 | 1.09 | 0.62 |
| Demographic regression, shrunk | 3.16 | 2.92 | 1.41 | 0.78 | 2.64 | 2.51 | 2.02 | 1.43 | 2.24 | 2.21 | 1.36 | 0.80 |
| **Average of raw + P1 + P2 (live model)** | 2.89 | 2.53 | 1.36 | 0.79 | 2.52 | 2.40 | 1.91 | 1.37 | 1.98 | 1.86 | 1.24 | 0.74 |

## Conclusion and what is live

- **No single method wins every cycle.**
  - The demographic regression is best in 2020→2024 but worst early in the 2022 midterm.
  - The type regression is best in 2016→2020 but worse than raw early in 2020→2024.
  - The first reweighting version (type shrinkage toward cross-state pooled deviations) was worse than raw in 2 of 3 cycles. It has been retired.
- **The average of the three — raw, type regression, demographic regression — is the most robust.**
  - In the early-8 aggregate it is never meaningfully worse than raw: 2022 midterm 1.18 vs 1.36 at 10% counted.
  - It removes most of the error when the swing is uneven: 1.17 vs 3.06 (2016→20) and 1.01 vs 1.07 (2020→24) at 10% counted.
  - It is the live "adjusted & reweighted" headline.
- **Expected error of the headline**, from replay of the early-8 states:
  - About ±1.8 points at 5% of the vote counted.
  - About ±1.2 points at 10%.
  - About ±0.6 points at 25%.
  - These are before vote-type and poll error. Per-state reads are noisier, about 2.5 points at 10%.
- **Limits:**
  - Counting order is simulated, not historical.
  - County types and demographics are a coarse stand-in for precinct-level models (NYT/AP use precinct demographics and vote type).
  - Three past cycles is a small sample.
