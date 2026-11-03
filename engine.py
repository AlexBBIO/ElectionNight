"""Comparison engine: live county results vs 2024 / 2022 baselines vs expectations.

Conventions
-----------
margin = (D - R) / total votes * 100   (positive = Democratic lead)

Per county c in a race's state:
  base24[c]    2024 presidential margin
  base22[c]    2022 margin in the configured baseline office (Senate / Governor / all House)
  turnout[c]   expected 2026 votes = 2022 votes in that office * expected_turnout_vs_2022
  expected[c]  base24[c] + state_swing, where
               state_swing = forecast_margin - state 2024 pres margin            (race forecast set)
                           = generic_ballot - national 2024 pres margin          (fallback: national swing)
  est_in[c]    min(1, votes counted / turnout[c])

Race-level, from counties with any votes:
  vs_expect    vote-weighted mean of (actual - expected)      -> Dems over/under-performing the polls
  swing24/22   vote-weighted mean of (actual - base)          -> raw swing vs prior elections
  firm_*       same, but only counties with est_in >= firm_threshold
  projected    sum_c turnout[c] * m_c / sum turnout, where m_c blends the county's actual margin
               (share est_in) with expected[c] + vs_expect (remaining share)
Overall (all races): vote-weighted mean of race vs_expect; implied national environment =
  generic_ballot + overall vs_expect.

Caveat: early batches are not a random sample (early/mail votes lean D in most states, Election
Day votes lean R). Treat partial-county reads accordingly; the firm_* numbers avoid the bias.
"""
from __future__ import annotations

import csv
import json
import collections
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def margin(d: float, r: float, t: float) -> float | None:
    return (d - r) / t * 100 if t else None


@dataclass
class Baselines:
    pres24: dict[str, dict] = field(default_factory=dict)            # fips -> row
    c22: dict[tuple, list[int]] = field(default_factory=dict)        # (fips, office) -> [d, r, t]

    @classmethod
    def load(cls) -> "Baselines":
        b = cls()
        with open(DATA / "pres_2024.csv", newline="") as fh:
            for r in csv.DictReader(fh):
                b.pres24[r["fips"]] = {"state": r["state"], "county": r["county"],
                                       "dem": int(r["dem"]), "rep": int(r["rep"]), "total": int(r["total"])}
        # Some MEDSL states carry both a TOTAL row and a by-vote-type breakdown for the same
        # county/contest; use TOTAL where present (per district), else sum the breakdown.
        parts: dict[tuple, dict[bool, list[int]]] = collections.defaultdict(
            lambda: {True: [0, 0, 0], False: [0, 0, 0], "has": set()})
        with open(DATA / "county_2022.csv", newline="") as fh:
            for r in csv.DictReader(fh):
                key = (r["fips"], r["office"], r["district"], r["special"])
                is_tot = r["mode"] == "TOTAL"
                p = parts[key]
                p["has"].add(is_tot)
                a = p[is_tot]
                a[0] += int(r["dem"]); a[1] += int(r["rep"]); a[2] += int(r["total"])
        agg: dict[tuple, list[int]] = collections.defaultdict(lambda: [0, 0, 0])
        for (fips, office, _d, _s), p in parts.items():
            src = p[True] if True in p["has"] else p[False]
            a = agg[(fips, office)]
            a[0] += src[0]; a[1] += src[1]; a[2] += src[2]
        b.c22 = dict(agg)
        return b

    def state_counties(self, st: str) -> list[str]:
        return [f for f, r in self.pres24.items() if r["state"] == st]

    def state_pres_margin(self, st: str) -> float:
        d = r = t = 0
        for f in self.state_counties(st):
            x = self.pres24[f]; d += x["dem"]; r += x["rep"]; t += x["total"]
        return margin(d, r, t)


def _wmean(pairs):
    num = den = 0.0
    for v, w in pairs:
        if v is None or not w:
            continue
        num += v * w; den += w
    return num / den if den else None


def analyze_race(race: dict, rows: list[dict], b: Baselines, cfg: dict) -> dict:
    st = race["state"]
    nat = cfg["national"]
    office = race["baseline_2022"]["office"]
    st_pres = b.state_pres_margin(st)
    if race.get("forecast_margin") is not None:
        state_swing = race["forecast_margin"] - st_pres
        expect_source = "race polls"
    else:
        state_swing = nat["generic_ballot_margin"] - nat["pres_2024_margin"]
        expect_source = "national generic ballot"
    forecast = st_pres + state_swing
    tfac = cfg.get("expected_turnout_vs_2022", 1.0)
    firm_thr = cfg.get("firm_threshold", 0.9)

    live = {}
    for r in rows:
        f = (r.get("fips") or "").zfill(5)
        if f in live:  # feeds that split a county (e.g. House districts) -> sum
            for k in ("dem", "rep", "total"):
                live[f][k] += r.get(k) or 0
        else:
            live[f] = {k: (r.get(k) or 0) for k in ("dem", "rep", "total")}
            live[f]["precincts_reporting"] = r.get("precincts_reporting")
            live[f]["precincts_total"] = r.get("precincts_total")
        live[f].setdefault("dem_name", r.get("dem_name"))
        live[f].setdefault("rep_name", r.get("rep_name"))

    counties = []
    for f in b.state_counties(st):
        p = b.pres24[f]
        base24 = margin(p["dem"], p["rep"], p["total"])
        c22 = b.c22.get((f, office))
        base22 = margin(*c22) if c22 else None
        turnout = (c22[2] if c22 and c22[2] else p["total"] * 0.75) * tfac
        expected = base24 + state_swing
        lv = live.get(f, {"dem": 0, "rep": 0, "total": 0})
        actual = margin(lv["dem"], lv["rep"], lv["total"])
        est_in = min(1.0, lv["total"] / turnout) if turnout else 0.0
        counties.append({
            "fips": f, "county": p["county"], "votes": lv["total"], "dem": lv["dem"], "rep": lv["rep"],
            "est_in": est_in, "prec_in": lv.get("precincts_reporting"), "prec_tot": lv.get("precincts_total"),
            "actual": actual, "expected": expected, "base24": base24, "base22": base22,
            "vs_expect": None if actual is None else actual - expected,
            "swing24": None if actual is None else actual - base24,
            "swing22": None if actual is None or base22 is None else actual - base22,
            "turnout_exp": turnout,
        })
    rep_ = [c for c in counties if c["votes"]]
    firm = [c for c in rep_ if c["est_in"] >= firm_thr]

    def agg(cs, key):
        return _wmean((c[key], c["votes"]) for c in cs)

    vs_exp = agg(rep_, "vs_expect")
    tot_d = sum(c["dem"] for c in counties); tot_r = sum(c["rep"] for c in counties)
    tot_v = sum(c["votes"] for c in counties)
    exp_tot = sum(c["turnout_exp"] for c in counties)
    proj = None
    if rep_:
        adj = vs_exp or 0.0
        num = 0.0
        for c in counties:
            share = c["est_in"] if c["actual"] is not None else 0.0
            m = share * (c["actual"] or 0.0) + (1 - share) * (c["expected"] + adj)
            num += c["turnout_exp"] * m
        proj = num / exp_tot if exp_tot else None
    names = next(((v.get("dem_name"), v.get("rep_name")) for v in live.values() if v.get("dem_name")), (None, None))
    return {
        "id": race["id"], "label": race["label"], "state": st, "polls_close_et": race.get("polls_close_et"),
        "dem_name": names[0], "rep_name": names[1],
        "forecast": forecast, "expect_source": expect_source, "low_confidence": bool(race.get("low_confidence")),
        "state_pres24": st_pres, "baseline_2022_office": office,
        "votes": tot_v, "est_in": tot_v / exp_tot if exp_tot else 0.0,
        "counties_reporting": len(rep_), "counties_firm": len(firm), "counties_total": len(counties),
        "current_margin": margin(tot_d, tot_r, tot_v),
        "vs_expect": vs_exp, "swing24": agg(rep_, "swing24"), "swing22": agg(rep_, "swing22"),
        "firm_vs_expect": agg(firm, "vs_expect"), "firm_swing24": agg(firm, "swing24"),
        "firm_swing22": agg(firm, "swing22"),
        "projected": proj, "projected_vs_forecast": None if proj is None else proj - forecast,
        "counties": counties,
    }


def overall(results: list[dict], cfg: dict) -> dict:
    live = [r for r in results if r.get("votes") and r.get("vs_expect") is not None]
    ve = _wmean((r["vs_expect"], r["votes"]) for r in live)
    firm = [r for r in live if r.get("firm_vs_expect") is not None]
    fve = _wmean((r["firm_vs_expect"], sum(c["votes"] for c in r["counties"] if c["est_in"] >= cfg.get("firm_threshold", 0.9))) for r in firm)
    gb = cfg["national"]["generic_ballot_margin"]
    return {
        "vs_expect": ve, "firm_vs_expect": fve,
        "swing24": _wmean((r["swing24"], r["votes"]) for r in live),
        "swing22": _wmean((r["swing22"], r["votes"]) for r in live if r["swing22"] is not None),
        "implied_generic": None if ve is None else gb + ve,
        "implied_generic_firm": None if fve is None else gb + fve,
        "generic_ballot": gb, "races_reporting": len(live), "votes": sum(r["votes"] for r in live),
    }


def load_config(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "config.json").read_text())
