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
import math
import json
import collections
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def margin(d: float, r: float, t: float) -> float | None:
    return (d - r) / t * 100 if t else None


def mode_bucket(mode: str) -> str | None:
    """Map a MEDSL / feed vote-mode label to pre (early + mail), eday or other."""
    m = (mode or "").upper().replace("_", " ")
    if not m or m == "TOTAL":
        return None
    if "PROVISIONAL" in m or "FAILSAFE" in m or "AFFIDAVIT" in m or "LATE" in m or "POST" in m:
        return "other"
    if "ELECTION DAY" in m or m in ("IN-PERSON", "IN PERSON", "NOT ABSENTEE"):
        return "eday"
    if any(k in m for k in ("ABSENTEE", "MAIL", "EARLY", "ONE STOP", "ADVANCE", "MILITARY", "OVERSEAS")):
        return "pre"
    return "other"


@dataclass
class Baselines:
    pres24: dict[str, dict] = field(default_factory=dict)            # fips -> row
    c22: dict[tuple, list[int]] = field(default_factory=dict)        # (fips, office) -> [d, r, t]
    c22_modes: dict[tuple, dict] = field(default_factory=dict)       # (fips, office) -> {pre|eday|other: [d,r,t]}
    types: dict[str, str] = field(default_factory=dict)              # fips -> county type
    cov: dict[str, list] = field(default_factory=dict)               # fips -> demographic covariates

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
        txv = DATA / "tx_vote_types_2022_2024.json"
        if txv.exists():  # Texas SOS county files: early (EV) vs Election Day by candidate
            try:
                g22 = json.loads(txv.read_text()).get("gov2022") or {}
                for f, v in g22.items():
                    if sum(v["pre"]) and sum(v["ed"]):
                        b.c22_modes[(f, "GOVERNOR")] = {"pre": [v["pre"][0], v["pre"][1], sum(v["pre"])],
                                                        "eday": [v["ed"][0], v["ed"][1], sum(v["ed"])]}
            except (OSError, ValueError):
                pass
        tp = DATA / "county_types.csv"
        if tp.exists():
            with open(tp, newline="") as fh:
                for r in csv.DictReader(fh):
                    b.types[r["fips"]] = r["type"]
                    b.cov[r["fips"]] = [float(r["college"]) / 10, float(r["black"]) / 10, float(r["hisp"]) / 10,
                                        math.log10(max(int(r["pop"]), 1000)), 1.0 if int(r["rucc"]) >= 4 else 0.0]

        # 2022 by vote type (pre = early + mail, eday, other), where the breakdown is complete.
        modes: dict[tuple, dict] = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0, 0]))
        with open(DATA / "county_2022.csv", newline="") as fh:
            for r in csv.DictReader(fh):
                bucket = mode_bucket(r["mode"])
                if not bucket:
                    continue
                a = modes[(r["fips"], r["office"])][bucket]
                a[0] += int(r["dem"]); a[1] += int(r["rep"]); a[2] += int(r["total"])
        for key, m in modes.items():
            tot = sum(v[2] for v in m.values())
            ref = b.c22.get(key, [0, 0, 0])[2]
            if "pre" in m and "eday" in m and ref and abs(tot - ref) / ref < 0.03:
                b.c22_modes[key] = {k: list(v) for k, v in m.items()}
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
    vt = state_vote_type_params(st, office, b, cfg)
    t26 = turnout_2026(st, cfg)
    tfac = vt["turnout_factor"]
    firm_thr = cfg.get("firm_threshold", 0.75)

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
            live[f]["pre_units"] = (r.get("pre_units_reporting"), r.get("pre_units_total"))
        if r.get("modes"):
            lm = live[f].setdefault("modes", {"pre": [0, 0, 0], "eday": [0, 0, 0], "other": [0, 0, 0]})
            for k, v in r["modes"].items():
                bk = {"early": "pre", "mail": "pre", "pre": "pre", "eday": "eday"}.get(k, "other")
                lm[bk][0] += v.get("dem") or 0; lm[bk][1] += v.get("rep") or 0; lm[bk][2] += v.get("total") or 0
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
            "turnout_exp": turnout, "turnout22": turnout / tfac if tfac else turnout,
            "type": b.types.get(f, "Exurban / working metro"), "cov": b.cov.get(f),
            "lm": {k: v for k, v in lv["modes"].items() if v[2]} if lv.get("modes") else None,
        })
        pre_b = _n((t26.get(f) or {}).get("pre_ballots")) if t26 else None
        if pre_b:
            counties[-1]["pre_ballots"] = pre_b
        counties[-1].update(adjust_county(lv, b.c22_modes.get((f, office)), vt, turnout, expected, pre_b))
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
    adj = adjusted_race(counties, forecast, firm_thr, tfac)
    adj["groups"] = race_groups(counties, adj.get("implied_turnout_vs_2022") or tfac)
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
        "turnout": turnout_signals(st, office, b, t26),
        "vote_type": {k: vt[k] for k in ("order", "pre_share", "delta", "turnout_factor", "source", "notes")},
        **adj,
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
        **overall_adjusted(live, gb),
        **reweight_by_type(results, gb),
        **overall_turnout(results),
        "counted": (sum(r["votes"] for r in live) / sum(r.get("exp_vote_total") or 0 for r in live))
        if live and sum(r.get("exp_vote_total") or 0 for r in live) else 0.0,
    }


def load_config(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "config.json").read_text())


# ======================================================================================
# Vote-type adjustment ("expected vote" model)
#
# The first votes posted are not a random sample: early + mail ("pre") and Election Day
# ("eday") lean differently, and which comes first varies by state. Per county we estimate
#   s     = 2026 share of the county's vote cast before Election Day
#   delta = margin(pre) - margin(eday), in points
# from 2022 county results by vote type where MEDSL has them (NC, GA, SC, VA, ...), shifted
# to the state's latest research values; otherwise from state-level research
# (vote_type_research.json) or config "states" overrides.
#
# Then for each reporting county:
#   - observed pre and eday votes come from the feed's vote-type split when available, else
#     are inferred from precincts reporting and the state's reporting order;
#   - a vote type that has not reported yet gets its volume from s and its margin from the
#     observed type +/- delta; a partially reported eday is scaled up by 1 / precinct share;
#   - adj_margin = estimated final county margin, exp_vote = estimated final county votes.
# A county is counted as complete (no adjustment) once precincts are ~all in.
# ======================================================================================
_RESEARCH = None


def load_research() -> dict:
    global _RESEARCH
    if _RESEARCH is None:
        p = HERE / "vote_type_research.json"
        try:
            _RESEARCH = json.loads(p.read_text()).get("states", {}) if p.exists() else {}
        except (OSError, ValueError):
            _RESEARCH = {}
    return _RESEARCH


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _delta_from(m: dict | None) -> float | None:
    if not m:
        return None
    pre, eday = _num(m.get("pre")), _num(m.get("eday"))
    if pre is None:
        e, ml = _num(m.get("early")), _num(m.get("mail"))
        pre = (e + ml) / 2 if e is not None and ml is not None else (e if e is not None else ml)
    return None if pre is None or eday is None else pre - eday


def state_vote_type_params(st: str, office: str, b: Baselines, cfg: dict) -> dict:
    res = load_research().get(st, {}) or {}
    ov = (cfg.get("states") or {}).get(st, {}) or {}
    # 2022 state aggregates from county vote-type data
    agg = {"pre": [0, 0, 0], "eday": [0, 0, 0]}
    t22 = 0
    for f in b.state_counties(st):
        c = b.c22.get((f, office))
        t22 += c[2] if c and c[2] else b.pres24[f]["total"] * 0.75  # same fallback as the county model
        m = b.c22_modes.get((f, office))
        if m:
            for k in agg:
                if k in m:
                    agg[k] = [x + y for x, y in zip(agg[k], m[k])]
    have22 = agg["pre"][2] and agg["eday"][2]
    s22 = agg["pre"][2] / (agg["pre"][2] + agg["eday"][2]) if have22 else _num(res.get("pre_share_2022"))
    d22 = (margin(*agg["pre"]) - margin(*agg["eday"])) if have22 else _delta_from(res.get("margin_2022"))
    d24 = _delta_from(res.get("margin_2024"))
    tf = _num(ov.get("turnout_factor")) or _num(res.get("turnout_factor_guess")) or cfg.get("expected_turnout_vs_2022", 1.0)
    ev = res.get("early_vote_2026") or {}
    s26 = None
    if _num(ev.get("ballots")) and t22:
        s26 = max(0.05, min(0.95, ev["ballots"] / (tf * t22)))
    s26 = _num(ov.get("pre_share")) or s26 or _num(res.get("pre_share_2024")) or s22 or 0.5
    delta = _num(ov.get("delta"))
    src = "config override" if delta is not None else None
    # Midterm prior: the 2022 early-vs-Election-Day gap (20-25 pts in NC/GA/SC) is a better guide for
    # 2026 than 2024's (4-10 pts); fall back to 2024 research only where no 2022 split exists.
    if delta is None and d22 is not None:
        delta, src = d22, ("2022 county vote-type data" if have22 else "2022 research")
    if delta is None and d24 is not None:
        delta, src = d24, "2024 research (no 2022 split)"
    order = ov.get("order") or res.get("order") or "pre_first"
    return {"order": order, "pre_share": s26, "pre_share_2022": s22, "delta": delta, "delta_2022": d22,
            "turnout_factor": tf, "source": src or "none (no vote-type adjustment)",
            "notes": ov.get("notes") or res.get("order_notes"), "have_county_modes": bool(have22)}


def adjust_county(lv: dict, m22: dict | None, vt: dict, turnout: float, expected: float,
                  pre_ballots: float | None = None) -> dict:
    V = lv.get("total") or 0
    out = {"pre_votes": None, "eday_votes": None, "s": None, "delta": None, "adj_margin": None,
           "adj_vs_expect": None, "exp_vote": turnout, "est_in_adj": 0.0, "method": "no votes"}
    # county-specific s and delta
    s, delta = vt["pre_share"], vt["delta"]
    if m22 and m22.get("pre") and m22.get("eday") and m22["pre"][2] and m22["eday"][2]:
        s22c = m22["pre"][2] / (m22["pre"][2] + m22["eday"][2])
        if vt.get("pre_share_2022"):
            s = s22c * vt["pre_share"] / vt["pre_share_2022"]
        else:
            s = s22c
        d22c = margin(*m22["pre"]) - margin(*m22["eday"])
        delta = d22c + ((vt["delta"] - vt["delta_2022"]) if vt["delta"] is not None and vt.get("delta_2022") is not None else 0.0)
    if pre_ballots:  # actual pre-Election-Day ballots in this county beat any modelled share
        turnout = max(turnout, pre_ballots / 0.97)
        out["exp_vote"] = turnout
        s = pre_ballots / turnout
    s = max(0.03, min(0.97, s))
    out["s"], out["delta"] = s, delta
    if not V:
        return out
    d = delta or 0.0
    M_obs = margin(lv["dem"], lv["rep"], V)
    pr, pt = lv.get("precincts_reporting"), lv.get("precincts_total")
    p = (pr / pt) if (pr is not None and pt) else None
    pu = lv.get("pre_units") or (None, None)
    pre_frac = (pu[0] / pu[1]) if (pu[0] is not None and pu[1]) else 1.0
    if pre_ballots and lv.get("modes") and lv["modes"]["pre"][2]:
        pre_frac = min(1.0, lv["modes"]["pre"][2] / pre_ballots)  # share of early/mail ballots counted

    modes = lv.get("modes")
    if modes and (modes["pre"][2] or modes["eday"][2] or modes["other"][2]):
        Vp = modes["pre"][2]
        e = [x + y for x, y in zip(modes["eday"], modes["other"])]
        Ve = e[2]
        Mp = margin(*modes["pre"]) if Vp else None
        Me = margin(*e) if Ve else None
        method = "feed vote types"
    elif p is not None and vt["order"] in ("pre_first", "mixed"):
        if p >= 0.999:
            Vp, Ve, Mp, Me = 0, V, None, M_obs
        else:
            T = V / (s + p * (1 - s))
            Vp = min(V, s * T); Ve = V - Vp
            Mp = M_obs + (Ve / V) * d
            Me = Mp - d if Ve else None
            if not Ve:
                Mp = M_obs
        method = "inferred from precincts"
    elif p is not None and vt["order"] == "eday_first" and p < 0.999:
        Vp, Ve, Mp, Me = 0, V, None, M_obs
        method = "inferred (Election Day first)"
    else:
        out.update({"adj_margin": M_obs, "adj_vs_expect": M_obs - expected,
                    "est_in_adj": min(1.0, V / turnout) if turnout else 0.0, "method": "unadjusted"})
        return out

    if p is not None and p >= 0.999 and pre_frac >= 0.999:
        T, M_hat = V, M_obs
    else:
        # final pre volume / margin
        if Vp:
            Vp_f = Vp / max(pre_frac, 0.05)
            Mp_f = Mp
        else:
            Vp_f = None
            Mp_f = (Me + d) if Me is not None else None
        # final eday volume / margin
        if Ve and p:
            Ve_f = Ve / max(p, 0.02)
            Me_f = Me
        else:
            Ve_f = None
            Me_f = (Mp - d) if Mp is not None else Me
        if Vp_f is None and Ve_f is None:
            Vp_f, Ve_f = s * turnout, (1 - s) * turnout
        elif Vp_f is None:
            Vp_f = Ve_f * s / (1 - s)
        elif Ve_f is None:
            Ve_f = Vp_f * (1 - s) / s
        T = max(V, Vp_f + Ve_f)
        T = min(T, 3 * turnout) if turnout else T
        Mp_f = Mp_f if Mp_f is not None else Me_f
        Me_f = Me_f if Me_f is not None else Mp_f
        M_hat = (Vp_f * Mp_f + Ve_f * Me_f) / (Vp_f + Ve_f)
    out.update({"pre_votes": round(Vp), "eday_votes": round(Ve), "adj_margin": M_hat,
                "adj_vs_expect": M_hat - expected, "exp_vote": T, "est_in_adj": min(1.0, V / T) if T else 0.0,
                "method": method})
    return out


def adjusted_race(counties: list[dict], forecast: float, firm_thr: float, tfac: float) -> dict:
    rep_ = [c for c in counties if c["votes"] and c["adj_margin"] is not None]
    firm = [c for c in rep_ if c["est_in_adj"] >= firm_thr]
    ve = _wmean((c["adj_vs_expect"], c["votes"]) for c in rep_)
    fve = _wmean((c["adj_vs_expect"], c["votes"]) for c in firm)
    # implied turnout vs 2022 from counties at least 30% in (more stable)
    stable = [c for c in rep_ if c["est_in_adj"] >= 0.3 and c["turnout22"]]
    t_ratio = (sum(c["exp_vote"] for c in stable) / sum(c["turnout22"] for c in stable)) if stable else None
    ratio = t_ratio or tfac
    exp_total = num = 0.0
    for c in counties:
        if c["votes"] and c["adj_margin"] is not None:
            w, mm = c["exp_vote"], c["adj_margin"]
        else:
            w, mm = c["turnout22"] * ratio, c["expected"] + (ve or 0.0)
        exp_total += w; num += w * mm
    proj = (num / exp_total) if (rep_ and exp_total) else None
    methods = collections.Counter(c["method"] for c in rep_)
    return {
        "adj_vs_expect": ve, "adj_firm_vs_expect": fve, "adj_counties_firm": len(firm),
        "adj_projected": proj, "adj_projected_vs_forecast": None if proj is None else proj - forecast,
        "adj_est_in": (sum(c["votes"] for c in counties) / exp_total) if exp_total else 0.0,
        "exp_vote_total": exp_total, "implied_turnout_vs_2022": t_ratio,
        "adj_methods": dict(methods), "adj_firm_votes": sum(c["votes"] for c in firm),
    }


def overall_adjusted(live: list[dict], gb: float) -> dict:
    a = [r for r in live if r.get("adj_vs_expect") is not None]
    ve = _wmean((r["adj_vs_expect"], r["votes"]) for r in a)
    f = [r for r in a if r.get("adj_firm_vs_expect") is not None]
    fve = _wmean((r["adj_firm_vs_expect"], r["adj_firm_votes"]) for r in f)
    tr = [r for r in a if r.get("implied_turnout_vs_2022")]
    return {
        "adj_vs_expect": ve, "adj_firm_vs_expect": fve,
        "adj_implied_generic": None if ve is None else gb + ve,
        "adj_implied_generic_firm": None if fve is None else gb + fve,
        "implied_turnout_vs_2022": _wmean((r["implied_turnout_vs_2022"], r["votes"]) for r in tr),
    }


# ======================================================================================
# County-composition reweighting (backtested: see BACKTEST.md / backtest_reweighting.py)
#
# Early returns over-represent whatever finishes first (tonight: small rural KY/IN counties).
# For every county not yet (fully) counted we predict its adjusted deviation from expectation
# three ways and average them -- the combination that backtested best across 2020->2022,
# 2016->2020 and 2020->2024 county swings:
#   P0  the race's own vote-weighted read so far (plain uniform swing)
#   P1  WLS on all reporting counties (all races): race fixed effects + county-type dummies
#   P2  WLS: race fixed effects + college %, Black %, Hispanic %, log population, non-metro
# A county's value = est_in * its adjusted read + (1 - est_in) * prediction; the race read is
# the expected-vote-weighted average over all of its counties. P1/P2 fall back to P0 until
# enough counties have reported to fit them.
# ======================================================================================
TYPE_LIST = ["Rural", "Exurban / working metro", "College suburbs", "Urban core", "Black", "Hispanic"]


def race_groups(counties: list[dict], ratio: float) -> dict:
    g: dict[str, dict] = {}
    for c in counties:
        x = g.setdefault(c["type"], {"exp": 0.0, "votes": 0, "n": 0, "n_rep": 0, "num": 0.0, "raw_num": 0.0})
        reporting = c["votes"] and c.get("adj_margin") is not None
        x["exp"] += c["exp_vote"] if reporting else c["turnout22"] * ratio
        x["n"] += 1
        if reporting:
            x["votes"] += c["votes"]; x["n_rep"] += 1
            x["num"] += c["adj_vs_expect"] * c["votes"]
            x["raw_num"] += (c["vs_expect"] or 0.0) * c["votes"]
    tot = sum(x["exp"] for x in g.values()) or 1.0
    return {k: {"weight": x["exp"] / tot, "counted": x["votes"] / x["exp"] if x["exp"] else 0.0,
                "votes": x["votes"], "n": x["n"], "n_rep": x["n_rep"],
                "adj_vs_expect": x["num"] / x["votes"] if x["votes"] else None,
                "vs_expect": x["raw_num"] / x["votes"] if x["votes"] else None}
            for k, x in g.items()}


def _solve(A: list[list[float]], b: list[float]) -> list[float] | None:
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None
        M[col], M[piv] = M[piv], M[col]
        for r in range(n):
            if r != col and M[r][col]:
                f = M[r][col] / M[col][col]
                M[r] = [a - f * c for a, c in zip(M[r], M[col])]
    return [M[i][n] / M[i][i] for i in range(n)]


def _fit(rows: list[tuple[int, list[float], float, float]], k_fe: int, k_x: int) -> list[float] | None:
    """WLS with race fixed effects. rows: (fe_index, x, y, w). Small ridge for stability."""
    k = k_fe + k_x
    if len(rows) < k + 8:
        return None
    wbar = sum(r[3] for r in rows) / len(rows) or 1.0
    A = [[0.0] * k for _ in range(k)]
    bb = [0.0] * k
    for fe, x, y, w in rows:
        w /= wbar
        idx = [fe] + [k_fe + j for j in range(k_x)]
        val = [1.0] + x
        for i, vi in zip(idx, val):
            bb[i] += w * vi * y
            for j, vj in zip(idx, val):
                A[i][j] += w * vi * vj
    for i in range(k):
        A[i][i] += 1.0
    return _solve(A, bb)


def reweight_by_type(results: list[dict], gb: float) -> dict:
    live = [r for r in results if r.get("votes")]
    # pooled per-type summary (descriptive)
    pooled: dict[str, dict] = {}
    for r in live:
        for k, x in (r.get("groups") or {}).items():
            p = pooled.setdefault(k, {"votes": 0, "num": 0.0, "raw": 0.0, "n_rep": 0, "races": 0})
            if x["votes"]:
                p["votes"] += x["votes"]; p["n_rep"] += x["n_rep"]; p["races"] += 1
                p["num"] += x["adj_vs_expect"] * x["votes"]; p["raw"] += (x["vs_expect"] or 0.0) * x["votes"]
    types = {k: {"adj_vs_expect": p["num"] / p["votes"] if p["votes"] else None,
                 "vs_expect": p["raw"] / p["votes"] if p["votes"] else None,
                 "votes": p["votes"], "counties_reporting": p["n_rep"], "races": p["races"]}
             for k, p in pooled.items()}

    fe = {r["id"]: i for i, r in enumerate(live)}
    rep_rows = [(r, c) for r in live for c in r["counties"]
                if c["votes"] and c.get("adj_vs_expect") is not None and c.get("cov")]

    def tdummies(c):
        return [1.0 if c["type"] == t else 0.0 for t in TYPE_LIST[1:]]
    b_type = _fit([(fe[r["id"]], tdummies(c), c["adj_vs_expect"], c["votes"]) for r, c in rep_rows], len(fe), 5)
    b_demo = _fit([(fe[r["id"]], c["cov"], c["adj_vs_expect"], c["votes"]) for r, c in rep_rows], len(fe), 5)

    num = den = 0.0
    for r in results:
        r["rw_vs_expect"] = r["rw_projected"] = None
        r["rw_components"] = None
        if not r.get("votes") or r.get("adj_vs_expect") is None:
            continue
        p0 = r["adj_vs_expect"]
        i = fe[r["id"]]
        comp = {"P0 race read": p0, "P1 type model": None, "P2 demographic model": None}
        tot_w = {k: 0.0 for k in comp}; tot = {k: 0.0 for k in comp}
        gsum: dict[str, list[float]] = {}
        ratio = r.get("implied_turnout_vs_2022") or 1.0
        for c in r["counties"]:
            rep = c["votes"] and c.get("adj_vs_expect") is not None
            w = c["exp_vote"] if rep else c["turnout22"] * ratio
            share = c["est_in_adj"] if rep else 0.0
            preds = {"P0 race read": p0,
                     "P1 type model": (b_type[i] + sum(a * x for a, x in zip(b_type[len(fe):], tdummies(c))))
                     if b_type else p0,
                     "P2 demographic model": (b_demo[i] + sum(a * x for a, x in zip(b_demo[len(fe):], c["cov"])))
                     if (b_demo and c.get("cov")) else p0}
            vals = []
            for k, pr in preds.items():
                v = share * c["adj_vs_expect"] + (1 - share) * pr if rep else pr
                tot[k] += w * v; tot_w[k] += w; vals.append(v)
            g = gsum.setdefault(c["type"], [0.0, 0.0])
            g[0] += w * sum(vals) / len(vals); g[1] += w
        for k in comp:
            comp[k] = tot[k] / tot_w[k] if tot_w[k] else None
        comp["P1 type model"] = comp["P1 type model"] if b_type else None
        comp["P2 demographic model"] = comp["P2 demographic model"] if b_demo else None
        used = [tot[k] / tot_w[k] for k in tot if tot_w[k]]
        r["rw_vs_expect"] = sum(used) / len(used)
        r["rw_projected"] = r["forecast"] + r["rw_vs_expect"]
        r["rw_components"] = comp
        for k, x in (r.get("groups") or {}).items():
            x["est"] = gsum[k][0] / gsum[k][1] if k in gsum and gsum[k][1] else None
        num += r["rw_vs_expect"] * r["votes"]; den += r["votes"]
    rw = num / den if den else None
    return {"rw_vs_expect": rw, "rw_implied_generic": None if rw is None else gb + rw, "types": types,
            "rw_models_fitted": {"type": b_type is not None, "demographic": b_demo is not None,
                                 "counties": len(rep_rows)}}


# ======================================================================================
# Turnout before counts
#
# Ballots cast before Election Day are published by county (and by party where the state
# registers by party) before any votes are counted. Two pre-count signals, vs 2022:
#   geo_shift   = sum_c (pre26_c / P26 - pre22_c / P22) * m_c    (m_c = 2022 county pre-ED margin
#                 where known, else 2022 county margin): did the early electorate's geography
#                 tilt D or R compared with 2022?
#   party_shift = (D - R) / ballots among 2026 pre-ED voters minus the same for 2022 (FL/NC/KY):
#                 registration mix of the early electorate.
# The county pre-ED ballot counts also feed the vote-type model: s_c = pre ballots / expected
# votes, and the share of pre-ED ballots already counted = counted early/mail / pre ballots.
# ======================================================================================
_TURNOUT_CACHE: dict[str, tuple[float, dict]] = {}
_TBASE: dict[int, dict] = {}


def turnout_base(year: int) -> dict:
    """fips -> row of data/turnout_{year}_pre.csv (final pre-Election-Day ballots that year)."""
    if year not in _TBASE:  # build fully before publishing: races load this concurrently
        d = {}
        p = DATA / f"turnout_{year}_pre.csv"
        if p.exists():
            with open(p, newline="") as fh:
                for r in csv.DictReader(fh):
                    d[r["fips"].zfill(5)] = r
        _TBASE[year] = d
    return _TBASE[year]


def turnout_2022() -> dict:
    return turnout_base(2022)


def turnout_2026(st: str, cfg: dict) -> dict:
    """fips -> row from feeds.turnout (cached 5 min). Empty if unsupported / unavailable."""
    import time as _t
    mode = cfg.get("turnout_mode", "live")
    if mode == "off":
        return {}
    hit = _TURNOUT_CACHE.get(st)
    if hit and _t.time() - hit[0] < 300:
        return hit[1]
    rows = {}
    if st == "TX":  # county early ballots from the SOS report captured in data/turnout_tx.json
        try:
            tx = json.loads((DATA / "turnout_tx.json").read_text())
            rows = {str(f).zfill(5): {"fips": str(f).zfill(5), "county": v.get("county"), "pre_ballots": v.get("ev_2026"),
                                      "as_of": (tx.get("statewide") or {}).get("ev_2026_as_of")}
                    for f, v in (tx.get("county_early") or {}).items() if v.get("ev_2026")}
        except (OSError, ValueError):
            rows = {}
        _TURNOUT_CACHE[st] = (_t.time(), rows)
        return rows
    try:
        import importlib
        mod = importlib.import_module("election_night.feeds.turnout")
        if st in getattr(mod, "STATES", []):
            data = mod.fetch(st) if mode == "live" else mod.load_snapshot(st)
            rows = {(r.get("fips") or "").zfill(5): r for r in data or []}
    except Exception:  # noqa: BLE001
        rows = {}
    _TURNOUT_CACHE[st] = (_t.time(), rows)
    return rows


def _n(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _turnout_vs(st, office, b, t26, base, year):
    P26 = P0 = 0.0
    pairs = []
    party26 = [0.0, 0.0, 0.0]; party0 = [0.0, 0.0, 0.0]; have_party = True
    for f in b.state_counties(st):
        a = t26.get(f); z = base.get(f)
        if not a or not z:
            continue
        p26, p0 = _n(a.get("pre_ballots")), _n(z.get("pre_ballots"))
        if not p26 or not p0:
            continue
        P26 += p26; P0 += p0
        if year == 2022:
            m = b.c22_modes.get((f, office))
            mm = margin(*m["pre"]) if m and m.get("pre") and m["pre"][2] else (margin(*b.c22[(f, office)]) if b.c22.get((f, office)) else None)
        else:
            pr = b.pres24.get(f)
            mm = margin(pr["dem"], pr["rep"], pr["total"]) if pr else None
        if mm is not None:
            pairs.append((p26, p0, mm))
        d26, r26, o26 = _n(a.get("pre_dem")), _n(a.get("pre_rep")), _n(a.get("pre_oth"))
        d0, r0, o0 = _n(z.get("pre_dem")), _n(z.get("pre_rep")), _n(z.get("pre_oth"))
        if None in (d26, r26, d0, r0):
            have_party = False
        else:
            party26 = [party26[0] + d26, party26[1] + r26, party26[2] + (o26 or 0)]
            party0 = [party0[0] + d0, party0[1] + r0, party0[2] + (o0 or 0)]
    if not P0:
        return None
    S26 = sum(p[0] for p in pairs); S0 = sum(p[1] for p in pairs)
    gap = lambda x: 100 * (x[0] - x[1]) / sum(x) if sum(x) else None
    ok = have_party and sum(party26) and sum(party0)
    return {"ballots": P0, "ratio": P26 / P0,
            "geo_shift": sum((a / S26 - c / S0) * mm for a, c, mm in pairs) if S26 and S0 else None,
            "party_gap": gap(party0) if ok else None,
            "party_shift": (gap(party26) - gap(party0)) if ok else None,
            "party": party0 if ok else None}


def turnout_signals(st: str, office: str, b: Baselines, t26: dict) -> dict | None:
    if not t26:
        return None
    rows = [t26[f] for f in b.state_counties(st) if f in t26]
    P26 = sum(_n(r.get("pre_ballots")) or 0 for r in rows)
    if not P26:
        return None
    pd_ = [sum(_n(r.get(k)) or 0 for r in rows) for k in ("pre_dem", "pre_rep", "pre_oth")]
    has_party = all(_n(r.get("pre_dem")) is not None for r in rows)
    ed = [_n(r.get("eday_checkins")) for r in rows]
    v22 = _turnout_vs(st, office, b, t26, turnout_base(2022), 2022)
    v24 = _turnout_vs(st, office, b, t26, turnout_base(2024), 2024)
    try:
        import importlib
        src = (getattr(importlib.import_module("election_night.feeds.turnout"), "SOURCES", {}) or {}).get(st)
    except Exception:  # noqa: BLE001
        src = None
    out = {"pre_ballots": P26, "as_of": rows[0].get("as_of"), "source": src,
           "party_2026": pd_ if has_party else None,
           "party_gap_2026": 100 * (pd_[0] - pd_[1]) / sum(pd_) if has_party and sum(pd_) else None,
           "eday_checkins": sum(x for x in ed if x is not None) if any(x is not None for x in ed) else None}
    for y, v in ((2022, v22), (2024, v24)):
        k = str(y)[2:]
        out[f"pre_ballots_{y}"] = v["ballots"] if v else None
        out[f"pre_ratio_{k}"] = v["ratio"] if v else None
        out[f"geo_shift_{k}"] = v["geo_shift"] if v else None
        out[f"party_gap_{y}"] = v["party_gap"] if v else None
        out[f"party_shift_{k}"] = v["party_shift"] if v else None
        out[f"party_{y}"] = v["party"] if v else None
    # backwards-compatible names (vs 2022)
    out.update({"pre_ratio": out["pre_ratio_22"], "geo_shift": out["geo_shift_22"], "party_shift": out["party_shift_22"]})
    return out


def overall_turnout(results: list[dict]) -> dict:
    seen, acc = set(), {k: [0.0, 0.0] for k in ("geo_shift_22", "party_shift_22", "geo_shift_24", "party_shift_24")}
    for r in results:
        t = r.get("turnout")
        if not t or r["state"] in seen:
            continue
        seen.add(r["state"])
        w = t.get("pre_ballots") or 0
        for k in acc:
            if t.get(k) is not None:
                acc[k][0] += t[k] * w; acc[k][1] += w
    val = {k: (a / b if b else None) for k, (a, b) in acc.items()}
    return {"turnout_geo_shift": val["geo_shift_22"], "turnout_party_shift": val["party_shift_22"],
            "turnout_geo_shift_24": val["geo_shift_24"], "turnout_party_shift_24": val["party_shift_24"],
            "turnout_states": len(seen)}
