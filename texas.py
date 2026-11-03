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
