"""Election Day check-ins by party: compare 2026 with 2022 / 2024 at the same clock hour.

Data (all optional, merged; later sources win for the same jurisdiction/year/hour):
  data/checkins.csv          researched / archived rows
  data/checkins_manual.csv   rows entered on the dashboard form (local server) or by hand
  feeds/checkins.py          live machine-readable feeds, if any (fetch() -> rows)
Columns: jurisdiction,state,year,hour_local,rep,dem,oth,source,as_of   (cumulative ED check-ins)

Per jurisdiction, at the latest hour that 2026 has and a comparison year also has:
  gap_y        = 100 * (dem - rep) / total          party gap of the ED electorate, D-R pts
  d_gap_22/24  = gap_2026 - gap_y                    + = ED electorate more Democratic in mix
  vol_22/24    = total_2026 / total_y                ED volume vs that year at the same hour
  implied_ed_shift_22 = LOYALTY * d_gap_22           rough vote-margin equivalent, assuming
               partisans vote ~88 net for their party and independents split as in 2022
  proj_final   = 2022 final ED by party scaled by each party's 2026/2022 same-hour ratio
Party registration is not vote choice, and Election Day's share of the vote keeps shrinking as
voters (lately more Republicans) move to early voting: read the ED mix together with the
pre-Election-Day ballots by party (turnout panel), not alone.
"""
from __future__ import annotations

import csv
import importlib
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
COLS = ["jurisdiction", "state", "year", "hour_local", "rep", "dem", "oth", "source", "as_of"]
MANUAL = DATA / "checkins_manual.csv"
LOYALTY = 0.88
_LIVE: tuple[float, list] = (0.0, [])


def _hour_key(h: str) -> float:
    h = (h or "").strip().lower()
    if h == "final":
        return 99.0
    try:
        hh, mm = (h.split(":") + ["0"])[:2]
        return int(hh) + int(mm) / 60
    except ValueError:
        return -1.0


def _read(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with open(p, newline="") as fh:
        return [r for r in csv.DictReader(fh)]


def _live_rows() -> list[dict]:
    global _LIVE
    if time.time() - _LIVE[0] < 120:
        return _LIVE[1]
    rows = []
    try:
        mod = importlib.import_module("election_night.feeds.checkins")
        rows = mod.fetch() or []
    except Exception:  # noqa: BLE001
        rows = []
    _LIVE = (time.time(), rows)
    return rows


def append_manual(row: dict) -> None:
    new = not MANUAL.exists()
    with open(MANUAL, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in COLS})


def _num(x):
    try:
        return float(str(x).replace(",", "")) if x not in (None, "") else None
    except ValueError:
        return None


def summary(live: bool = True) -> dict:
    data: dict[tuple, dict] = {}
    for src in (_read(DATA / "checkins.csv"), _read(MANUAL), _live_rows() if live else []):
        for r in src:
            try:
                key = (r["jurisdiction"].strip(), int(r["year"]), _hour_key(r["hour_local"]))
            except (KeyError, ValueError):
                continue
            rep, dem, oth = _num(r.get("rep")), _num(r.get("dem")), _num(r.get("oth"))
            if rep is None or dem is None:
                continue
            data[key] = {"state": r.get("state", ""), "rep": rep, "dem": dem, "oth": oth or 0.0,
                         "hour": r["hour_local"], "source": r.get("source", ""), "as_of": r.get("as_of", "")}
    by_j: dict[str, dict] = defaultdict(lambda: defaultdict(dict))
    for (j, y, hk), v in data.items():
        by_j[j][y][hk] = v
    out = []
    for j, years in by_j.items():
        cur = {hk: v for hk, v in years.get(2026, {}).items() if hk < 99}
        if not cur:
            continue
        # latest 2026 hour that a comparison year also has
        hours = sorted(cur, reverse=True)
        h = next((hk for hk in hours if any(hk in years.get(y, {}) for y in (2022, 2024))), hours[0])
        a = cur[h]
        tot = a["rep"] + a["dem"] + a["oth"]
        row = {"jurisdiction": j, "state": a["state"], "hour": a["hour"], "rep": a["rep"], "dem": a["dem"],
               "oth": a["oth"], "total": tot, "gap_2026": 100 * (a["dem"] - a["rep"]) / tot if tot else None,
               "source": a["source"], "as_of": a["as_of"],
               "series": {str(y): sorted(([hk, (v["dem"] - v["rep"]) / (v["rep"] + v["dem"] + v["oth"]) * 100,
                                          v["rep"] + v["dem"] + v["oth"]]
                                         for hk, v in hs.items() if hk < 99), key=lambda x: x[0])
                          for y, hs in years.items()}}
        for y in (2022, 2024):
            b = years.get(y, {}).get(h)
            if b:
                bt = b["rep"] + b["dem"] + b["oth"]
                g = 100 * (b["dem"] - b["rep"]) / bt if bt else None
                row[f"gap_{y}"] = g
                row[f"d_gap_{y % 100}"] = (row["gap_2026"] - g) if (g is not None and row["gap_2026"] is not None) else None
                row[f"vol_{y % 100}"] = tot / bt if bt else None
            fin = years.get(y, {}).get(99.0)
            if y == 2022 and fin and b:
                proj = {p: fin[p] * (a[p] / b[p]) if b[p] else fin[p] for p in ("rep", "dem", "oth")}
                pt = sum(proj.values())
                row["proj_final_gap"] = 100 * (proj["dem"] - proj["rep"]) / pt if pt else None
                row["proj_final_total"] = pt
                row["final_2022_gap"] = 100 * (fin["dem"] - fin["rep"]) / (fin["rep"] + fin["dem"] + fin["oth"])
        if row.get("d_gap_22") is not None:
            row["implied_ed_shift_22"] = LOYALTY * row["d_gap_22"]
        out.append(row)
    w = [(r["d_gap_22"], r["total"]) for r in out if r.get("d_gap_22") is not None]
    w24 = [(r["d_gap_24"], r["total"]) for r in out if r.get("d_gap_24") is not None]
    agg = (sum(a * b for a, b in w) / sum(b for _, b in w)) if w else None
    agg24 = (sum(a * b for a, b in w24) / sum(b for _, b in w24)) if w24 else None
    return {"jurisdictions": sorted(out, key=lambda r: -r["total"]), "d_gap_22": agg, "d_gap_24": agg24,
            "implied_ed_shift_22": None if agg is None else LOYALTY * agg}
