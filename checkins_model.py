"""Election Day check-ins by party vs 2022 / 2024.

Data (merged; later sources win for the same jurisdiction/year/hour):
  data/checkins.csv          researched rows + hourly 2026 snapshots saved by the poller
                             (python3 -m election_night.feeds.checkins --save)
  data/checkins_manual.csv   rows typed into the dashboard form, or by hand
  feeds/checkins.py          live fetch (Florida county VR Systems feeds, Washoe NV)
Columns: jurisdiction,state,year,hour_local,rep,dem,oth,source,as_of   (cumulative ED in-person)

For each jurisdiction, and for each state with several counties (sum of counties that have
2026 data and both 2022 and 2024 finals):
  series[year]  cumulative [hour, R, D, O] points (hour in local time; Eastern for state sums)
  finals[year]  final ED totals by party, where known
  mix           (D - R) / total of the ED electorate, 2026 now vs the same hour in 2022/2024
                where we have it, else vs that year's final ED electorate
  pace          2026 so far / that year's final, by party
Party registration is not vote choice, and Election Day's share keeps shrinking as voters
(lately more Republicans) move to early voting: read alongside the early-vote party mix.
"""
from __future__ import annotations

import csv
import datetime as _dt
import importlib
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
COLS = ["jurisdiction", "state", "year", "hour_local", "rep", "dem", "oth", "source", "as_of"]
MANUAL = DATA / "checkins_manual.csv"
SAVED = DATA / "checkins.csv"
ET = _dt.timezone(_dt.timedelta(hours=-5))
_LIVE: tuple[float, list] = (0.0, [])


def _num(x):
    try:
        return float(str(x).replace(",", "")) if x not in (None, "") else None
    except ValueError:
        return None


def _hours(r: dict, eastern: bool) -> float | None:
    h = (r.get("hour_local") or "").strip().lower()
    if h == "final":
        return None
    if eastern and r.get("as_of"):
        try:
            t = _dt.datetime.fromisoformat(r["as_of"].replace(" ", "T")[:22])
            if t.tzinfo:
                t = t.astimezone(ET)
                return t.hour + t.minute / 60
        except ValueError:
            pass
    try:
        hh, mm = (h.split(":") + ["0"])[:2]
        return int(hh) + int(mm) / 60
    except ValueError:
        return None


def _read(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def _live_rows(save: bool) -> list[dict]:
    """Live rows (cached 10 min). When `save`, new snapshots are merged into data/checkins.csv
    so the same-day hourly series builds up (no-op on read-only deploys)."""
    global _LIVE
    if time.time() - _LIVE[0] < 600:
        return _LIVE[1]
    rows = []
    try:
        mod = importlib.import_module("election_night.feeds.checkins")
        rows = mod.fetch() or []
        if save and rows:
            try:
                mod.merge_into_csv(rows)
            except OSError:
                pass
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


def _gap(r, d, o):
    t = r + d + o
    return 100 * (d - r) / t if t else None


def _panel(name, state, series, finals, note=""):
    s26 = series.get(2026) or []
    if not s26:
        return None
    t, R, D, O = s26[-1]
    out = {"name": name, "state": state, "note": note, "series": {str(y): v for y, v in series.items()},
           "finals": {str(y): v for y, v in finals.items()},
           "latest": {"hour": t, "rep": R, "dem": D, "oth": O, "total": R + D + O},
           "mix_2026": _gap(R, D, O)}
    for y in (2022, 2024):
        same = next((p for p in series.get(y, []) if abs(p[0] - t) < 0.26), None)
        fin = finals.get(y)
        if same:
            out[f"mix_{y}"] = _gap(*same[1:]); out[f"basis_{y}"] = "same hour"
            out[f"vol_{y}"] = (R + D + O) / sum(same[1:])
        elif fin:
            out[f"mix_{y}"] = _gap(*fin); out[f"basis_{y}"] = "final"
        if fin:
            out[f"mix_{y}_final"] = _gap(*fin)
            out[f"pace_{y}"] = {"rep": R / fin[0] if fin[0] else None, "dem": D / fin[1] if fin[1] else None,
                                "oth": O / fin[2] if fin[2] else None, "total": (R + D + O) / sum(fin) if sum(fin) else None}
        if out.get(f"mix_{y}") is not None and out["mix_2026"] is not None:
            out[f"d_mix_{y % 100}"] = out["mix_2026"] - out[f"mix_{y}"]
    return out


def summary(live: bool = True, save: bool = True) -> dict:
    data: dict[tuple, dict] = {}
    for src in (_read(SAVED), _read(MANUAL), _live_rows(save) if live else []):
        for r in src:
            try:
                key = (r["jurisdiction"].strip(), r.get("state", ""), int(r["year"]), (r["hour_local"] or "").strip().lower())
            except (KeyError, ValueError):
                continue
            rep, dem, oth = _num(r.get("rep")), _num(r.get("dem")), _num(r.get("oth"))
            if rep is None or dem is None:
                continue
            data[key] = {**r, "rep": rep, "dem": dem, "oth": oth or 0.0}
    per: dict[tuple, dict] = defaultdict(lambda: {"series": defaultdict(list), "series_et": defaultdict(list), "finals": {}})
    for (j, st, y, h), r in data.items():
        p = per[(j, st)]
        if h == "final":
            p["finals"][y] = [r["rep"], r["dem"], r["oth"]]
            continue
        for key, east in (("series", False), ("series_et", True)):
            t = _hours(r, east)
            if t is not None:
                p[key][y].append([t, r["rep"], r["dem"], r["oth"]])
    panels = []
    for (j, st), p in per.items():
        for y in p["series"]:
            p["series"][y].sort(); p["series_et"][y].sort()
        pn = _panel(j, st, dict(p["series"]), p["finals"])
        if pn:
            pn["key"] = f"{st}|{j}"
            panels.append(pn)
    # state sums (Eastern time), over counties with 2026 data and both finals
    by_state = defaultdict(list)
    for (j, st), p in per.items():
        if p["series_et"].get(2026) and 2022 in p["finals"] and 2024 in p["finals"]:
            by_state[st].append(p)
    states = []
    for st, ps in by_state.items():
        if len(ps) < 5:
            continue
        times = sorted({round(pt[0] * 2) / 2 for p in ps for pt in p["series_et"][2026]})
        ser = []
        for t in times:
            tot = [0.0, 0.0, 0.0]
            for p in ps:
                last = [pt for pt in p["series_et"][2026] if round(pt[0] * 2) / 2 <= t]
                if last:
                    tot = [a + b for a, b in zip(tot, last[-1][1:])]
            ser.append([t] + tot)
        finals = {y: [sum(p["finals"][y][i] for p in ps) for i in range(3)] for y in (2022, 2024)}
        pn = _panel(f"{st} — {len(ps)} counties", st, {2026: ser}, finals,
                    note="sum of counties reporting today that also have 2022 and 2024 finals; Eastern time")
        if pn:
            pn["key"] = f"{st}|__state"
            pn["is_state"] = True
            states.append(pn)
    # headline: state sums + jurisdictions in states without a sum
    summed = {s["state"] for s in states}
    head = states + [p for p in panels if p["state"] not in summed]
    def wavg(k):
        xs = [(p[k], p["latest"]["total"]) for p in head if p.get(k) is not None]
        return sum(a * b for a, b in xs) / sum(b for _, b in xs) if xs else None
    panels.sort(key=lambda p: -p["latest"]["total"])
    return {"panels": states + panels, "d_mix_22": wavg("d_mix_22"), "d_mix_24": wavg("d_mix_24"),
            "jurisdictions": len(panels)}
