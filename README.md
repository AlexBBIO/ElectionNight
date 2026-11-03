# Election night tracker

Polls official state results feeds, compares every reporting county with its 2024
presidential and 2022 midterm result, and scores it against what the final polls
implied. Stdlib-only Python; dashboard on localhost.

## Run

```bash
python3 run.py                       # live → http://127.0.0.1:8765
python3 run.py --demo                # synthetic night (Dems +2 vs polls) to check the screen
python3 run.py --once                # one poll, print table, exit
python3 run.py --races KY-SEN,IN-HOUSE   # subset
```

Run from this folder. Every cycle is logged to `out/history.jsonl`
(the latest snapshot is in `out/latest.json`). Test a single feed with:
`python3 run.py --once --races KY-SEN`.

## Deploy (Vercel)

Import the repo on Vercel with defaults: `vercel.json` serves `dashboard.html` at `/`
and `api/state.py` as the live endpoint (CDN-cached 20s). `/?demo=1` shows the demo.

## What the numbers mean

All margins are D − R in points of total votes.

| Number | Meaning |
|---|---|
| **vs expect** | Vote-weighted average over reporting counties of (actual margin − expected margin). Positive = Democrats beating the polls. |
| expected (county) | 2024 presidential county margin + the statewide swing implied by the race's final polling average (`forecast_margin` in config). If a race has no average, the national swing from the generic ballot (D+8.7 vs 2024's R+1.5) is used instead (marked †). |
| **firm vs expect** | The same, using only counties at least 90% counted (`firm_threshold`). This avoids most vote-type bias. |
| swing vs '24 / '22 | Raw shift from the 2024 presidential and 2022 baseline-office results (Senate, Governor or all House) in the same counties. |
| est. in | Votes counted ÷ 2022 votes in that county (× `expected_turnout_vs_2022`). |
| projected | Statewide margin if uncounted votes follow the expected margin, adjusted by the current vs-expect read. |
| implied national environment | Generic-ballot polls + overall vs expect. This is the trading number. |

**Vote-type bias.** First batches are usually early and mail votes, which lean D in
most states, while Election Day votes lean R. A big D read off 5% of the vote is
mostly that bias. Watch the **firm** line and counties that are fully counted
(small KY/IN counties finish fast after 6pm ET). The demo simulates this bias, so you
can see the headline start D-high and settle down.

## Before polls close

Edit `config.json`: update `forecast_margin` for each race and
`national.generic_ballot_margin` to the averages you trust. Add races
(`feed` = module in `feeds/`, `race` = contest-name substring).

## Data

- `data/pres_2024.csv`: county 2024 president (tonmcg/US_County_Level_Election_Results_08-24).
- `data/county_2022.csv`: 2022 Senate, Governor and U.S. House by county and vote mode,
  aggregated from MIT Election Lab precinct files (MEDSL/2022-elections-official).
  Rebuild with `python3 build_baselines.py`.
- Live: `feeds/<state>.py`, which reads official state election-night feeds.
