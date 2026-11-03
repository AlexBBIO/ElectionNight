"""Texas early-vote read (no party registration: county geography + primary-voting history).

Inputs: data/turnout_tx.json (county early-vote totals 2026/2022/2024, primary-history shares,
statewide totals), county baselines (2024 president, 2022 governor).

  geo_shift_y     sum_c (share26_c - sharey_c) * margin_y_c : did early turnout grow more in
                  D or R counties than in year y (weighted by county margins), pts
  comp_effect_y   change in the mix of early voters by primary history (R-primary, D-primary,
                  no history) valued at group vote margins (R-primary ~ R+90, D-primary ~ D+90,
                  no-history lean solved to reproduce year y's early-vote margin); pts
  turnout effect  = comp_effect (primary history) if available, else geo_shift
  expected swing  = expected margin (polls / markets at 06:00) - year y's result
  vs polls        = turnout effect - PI * expected swing   (same framework as FL/NC)
Early vote only: TX early vote was ~2/3 of 2022's and ~80% of 2024's total, so the read is
converted to the expected EARLY-vote margin: E + (1 - s) * gap_y, gap_y from research
(vote_type_research.json) where available, else assumed 0 (TX early vote has leaned R in
recent cycles: 2022 early ~ R+13 vs ED ~ R+5 is NOT assumed; set gap in config to override).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import engine

HERE = Path(__file__).resolve().parent
LOYAL = 90.0


def _n(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def cfg_forecast(cfg: dict, rid: str) -> float:
    r = next((x for x in cfg["races"] if x["id"] == rid), None)
    return (r or {}).get("forecast_margin") or 0.0


def read(b: engine.Baselines, cfg: dict, markets: dict | None, pi: float = 1 / 3) -> dict | None:
    p = HERE / "data" / "turnout_tx.json"
    if not p.exists():
        return None
    try:
        tx = json.loads(p.read_text())
    except ValueError:
        return None
    out = {"sources": tx.get("sources"), "statewide": tx.get("statewide"), "eday_live": tx.get("eday_live")}
    ce = tx.get("county_early") or {}
    # --- geography
    geo = {}
    geo_note = None
    for y, key in ((2022, "ev_2022"), (2024, "ev_2024")):
        S26 = Sy = 0.0; rows = []
        for f, r in ce.items():
            f = str(f).zfill(5)
            a, z = _n(r.get("ev_2026")), _n(r.get(key))
            if a and not z and y == 2022:
                # no 2022 county early-vote file: use the county's total 2022 governor vote as the
                # baseline distribution (early vote was ~2/3 of the total, fairly uniformly)
                c = b.c22.get((f, "GOVERNOR")); z = c[2] if c else None
                geo_note = "2022 baseline = county total votes (early-vote file unavailable)"
            if not a or not z:
                continue
            if y == 2024:
                pr = b.pres24.get(f); mm = engine.margin(pr["dem"], pr["rep"], pr["total"]) if pr else None
            else:
                c = b.c22.get((f, "GOVERNOR")); mm = engine.margin(*c) if c else None
            if mm is None:
                continue
            rows.append((a, z, mm)); S26 += a; Sy += z
        if rows and S26 and Sy:
            geo[y] = {"shift": sum((a / S26 - z / Sy) * mm for a, z, mm in rows), "counties": len(rows),
                      "ev_2026": S26, f"ev_{y}": Sy, "ratio": S26 / Sy, "note": geo_note if y == 2022 else None}
    out["geo"] = geo
    # --- primary history (like-for-like: Derek Ryan's 'most recent primary' basis both years)
    ph = tx.get("primary_history") or {}
    cur = ph.get("2026_ryan") or ph.get("2026") or {}
    comp = {}
    res = (engine.load_research().get("TX") or {})
    for y in (2022, 2024):
        base = ph.get(f"{y}_ryan") or ph.get(str(y)) or {}
        if None in (_n(cur.get("rep")), _n(cur.get("dem")), _n(base.get("rep")), _n(base.get("dem"))):
            continue
        s26 = [_n(cur["dem"]), _n(cur["rep"])]; sy = [_n(base["dem"]), _n(base["rep"])]
        s26.append(max(0.0, 1 - sum(s26))); sy.append(max(0.0, 1 - sum(sy)))
        my = ((res.get(f"margin_{y}") or {}).get("pre"))
        if my is None:
            my = (b.state_pres_margin("TX") if y == 2024 else -10.9)
        m_oth = (my - LOYAL * sy[0] + LOYAL * sy[1]) / sy[2] if sy[2] > 0.02 else 0.0
        m_oth = max(-60.0, min(60.0, m_oth))
        eff = LOYAL * (s26[0] - sy[0]) - LOYAL * (s26[1] - sy[1]) + m_oth * (s26[2] - sy[2])
        comp[y] = {"effect": eff, "mix_2026": 100 * (s26[0] - s26[1]), "mix_y": 100 * (sy[0] - sy[1]),
                   "m_oth_y": m_oth, "early_margin_y": my, "shares_2026": s26, "shares_y": sy,
                   "basis": f"{cur.get('basis', '')} vs {base.get('basis', '')}", "estimated": bool(cur.get("estimated"))}
    out["primary_history"] = {"data": ph, "comp": comp}
    # --- what Election Day has to do: early-vote margin estimates x remaining vote
    ev = _n((tx.get("statewide") or {}).get("ev_2026")) or 0.0
    models = ph.get("early_vote_margin_models_2026") or {}
    ev_m = [v for k, v in models.items() if isinstance(v, (int, float))]
    scen = {}
    if ev and ev_m:
        for total in (10.5e6, 11.0e6, 11.5e6):
            ed = total - ev
            row = {}
            for name, em in (("early_D_model", min(ev_m)), ("early_R_pollster", max(ev_m)), ("early_mid", sum(ev_m) / len(ev_m))):
                # Election Day margin needed for the overall result to equal X
                need = lambda X: (X * total - em * ev) / ed
                row[name] = {"early_margin": em, "ED_margin_for_tie": need(0.0),
                             "ED_margin_for_polls": need(cfg_forecast(cfg, "TX-GOV")),
                             "final_if_ED": {str(int(x)): (em * ev + x * ed) / total for x in (-10, -15, -20, -25, -30)}}
            scen[f"{total / 1e6:.1f}M"] = row
    out["election_day_scenarios"] = {"early_votes": ev, "scenarios": scen,
        "remaining_pool_note": (ph.get("2026", {}).get("ryan") or {}).get("remaining_pool")}
    # --- vs expectations, per race
    races = {}
    M = {2024: b.state_pres_margin("TX")}
    agg = [0, 0, 0]
    for f in b.state_counties("TX"):
        c = b.c22.get((f, "GOVERNOR"))
        if c:
            agg = [x + y for x, y in zip(agg, c)]
    M[2022] = engine.margin(*agg) if agg[2] else -10.9
    gap = {2022: (res.get("margin_2022") or {}), 2024: (res.get("margin_2024") or {})}
    s_share = (cfg.get("states") or {}).get("TX", {}).get("pre_share") or 0.65
    mk = ((markets or {}).get("races") or {})
    for rid in ("TX-GOV", "TX-SEN"):
        race = next((r for r in cfg["races"] if r["id"] == rid), None)
        if not race:
            continue
        E = race.get("forecast_margin")
        Em = (mk.get(rid) or {}).get("implied_margin")
        yrs = {}
        for y in (2022, 2024):
            g = gap[y]
            gy = (g.get("pre") - g.get("eday")) if (g.get("pre") is not None and g.get("eday") is not None) else 0.0
            shift = (1 - s_share) * gy
            eff = comp[y]["effect"] if y in comp else (geo[y]["shift"] if y in geo else None)
            if eff is None:
                continue
            base_m = comp[y]["early_margin_y"] if y in comp else M[y]
            x = {"turnout_effect": eff, "effect_source": "primary history" if y in comp else "county geography",
                 "expected": None if E is None else E + shift - base_m,
                 "market_expected": None if Em is None else Em + shift - base_m, "gap_used": gy}
            if x["expected"] is not None:
                x["vs_polls"] = eff - pi * x["expected"]
                x["vs_polls_range"] = [eff - 0.5 * x["expected"], eff - 0.2 * x["expected"]]
            if x["market_expected"] is not None:
                x["vs_markets"] = eff - pi * x["market_expected"]
            yrs[y] = x
        races[rid] = {"forecast": E, "market": Em, "years": yrs}
    out["races"] = races
    return out


if __name__ == "__main__":
    cfg = engine.load_config(); b = engine.Baselines.load()
    mp = HERE / "data" / "markets_baseline.json"
    m = json.loads(mp.read_text()) if mp.exists() else None
    print(json.dumps(read(b, cfg, m), indent=1, default=str)[:4000])


# ======================================================================================
# Pre-close probability model for the Texas Governor (and Senate) race
#
# final margin = s * M_early + (1 - s) * M_eday
#   s        early share of the total vote: early ballots (known, 7.71M) / total turnout T
#   M_early  D-R margin of the early electorate: prior from the two published models of the
#            early vote (D+1 Democratic model, D+6.6 Republican pollster) -> mean 3.8, sd 3
#   M_eday   D-R margin of Election Day voters: the weakest-known quantity. Prior centred on
#            what the poll/market prior implies given M_early (so that with no turnout evidence
#            the model reproduces the prior), then pulled toward the "remaining pool" evidence
#            (Ryan: >2.1M recent GOP-primary voters unvoted vs a much smaller D pool) via ED_POOL.
#   T        total turnout, prior mean 11.0M (early was 68% of the 2022 total), sd 0.4M
# Monte Carlo -> distribution of the final margin, P(D win), and a scenario table over
# M_eday assumptions so the dependence on that assumption is visible. As counties report,
# observed early-vote and Election Day margins replace the priors (update()).
# ======================================================================================
import random as _random
from statistics import NormalDist as _ND

ED_POOL = {"mean": -20.0, "sd": 8.0,
           "note": "Election Day margin: Ryan counts >2.1M recent GOP-primary voters unvoted at the end of early "
                   "voting vs a much smaller D pool, which points to a strongly Republican Election Day (R+20 "
                   "+/- 8 on that evidence). Counter-evidence: in 2022 the early-only count was R+10 and the "
                   "final R+10.9, i.e. Election Day was only a few points more Republican than the early vote. "
                   "The prior used is the average of the pool estimate and the Election Day margin implied by the "
                   "poll/market prior (which equals ~R+14 when the early vote is D+3.8)."}
BENCH_2022 = {"early_only_margin": -10.0, "final": -10.9,
              "note": "2022: Abbott 54-44 with all early votes and ~30% of Election Day precincts (Texas Tribune)"}


def pre_close(cfg: dict, markets: dict | None, early_votes: float = 7_711_462, m_early=(3.8, 3.0),
              total=(11.0e6, 0.4e6), ed=None, n: int = 40000, seed: int = 5, rid: str = "TX-GOV",
              observed: dict | None = None) -> dict:
    rng = _random.Random(seed)
    E_poll = cfg_forecast(cfg, rid)
    E_mkt = (((markets or {}).get("races") or {}).get(rid) or {}).get("implied_margin")
    p_mkt = (((markets or {}).get("races") or {}).get(rid) or {}).get("p_dem")
    prior = E_poll if E_mkt is None else (E_poll + E_mkt) / 2
    split = 0.0
    if rid != "TX-GOV":
        g_poll = cfg_forecast(cfg, "TX-GOV")
        g_mkt = (((markets or {}).get("races") or {}).get("TX-GOV") or {}).get("implied_margin")
        g_prior = g_poll if g_mkt is None else (g_poll + g_mkt) / 2
        split = prior - g_prior            # ticket split vs the governor race, applied to both vote types
        m_early = (m_early[0] + split, m_early[1])
        prior_gov = g_prior
    s0 = early_votes / total[0]
    # Election Day prior: implied by the overall prior given the early-vote prior, blended with pool evidence
    ed_implied = (prior - s0 * m_early[0]) / (1 - s0)
    ed = ed or {"mean": 0.5 * ed_implied + 0.5 * (ED_POOL["mean"] + split), "sd": ED_POOL["sd"],
                "implied_by_prior": ed_implied, "pool": ED_POOL["mean"], "note": ED_POOL["note"]}
    obs = observed or {}
    finals = []
    for _ in range(n):
        T = max(early_votes * 1.02, rng.gauss(*total))
        s = early_votes / T
        if "m_early" in obs:   # observed, with residual uncertainty for the uncounted share
            me = rng.gauss(obs["m_early"], 0.5 + 3.0 * (1 - min(1.0, obs.get("early_counted_share", 1.0))))
        else:
            me = rng.gauss(*m_early)
        if "m_eday" in obs:
            ed_total = max(1.0, T - early_votes)
            frac = min(1.0, obs.get("eday_counted", 0) / ed_total)
            md = rng.gauss(obs["m_eday"], max(1.0, ed["sd"] * (1 - frac)))
        else:
            md = rng.gauss(ed["mean"], ed["sd"])
        finals.append(s * me + (1 - s) * md)
    finals.sort()
    q = lambda f: finals[int(f * (len(finals) - 1))]
    p_d = sum(1 for x in finals if x > 0) / len(finals)
    table = []
    for md0 in (-5, -10, -15, -20, -25, -30):
        md = md0 + split
        row = {"m_eday": md}
        for name, me in (("early low", 1.0 + split), ("early mid", 3.8 + split), ("early high", 6.6 + split)):
            row[name] = s0 * me + (1 - s0) * md
        table.append(row)
    return {"race": rid, "prior_polls": E_poll, "prior_market": E_mkt, "p_dem_market": p_mkt, "prior": prior,
            "early_votes": early_votes, "early_share": s0, "m_early": m_early, "total": total, "ed": ed,
            "median": q(0.5), "p10": q(0.1), "p90": q(0.9), "p_dem": p_d,
            "scenarios": table, "observed": obs, "n": n, "bench_2022": BENCH_2022, "split": split,
            # what the 8 PM early-vote dump has to show for the race to be on track
            "early_needed": {"for_prior": (prior - (1 - s0) * ed["mean"]) / s0,
                             "for_tie_if_ED": {str(md): (0 - (1 - s0) * md) / s0 for md in (-10, -15, -20, -25)}}}
