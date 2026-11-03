"""Senate control: market baseline (06:00 ET) re-priced for what we've seen so far.

Inputs
  seats     every 2026 Senate seat on the ballot: baseline p_dem (market at 06:00 ET),
            implied margin where brackets exist, incumbent party
  shifts    points of margin to add to each race's expected margin, from the turnout read
            ("turnout vs markets", 2022 baseline) for states with party-registration data,
            and a discounted national read for the others; later, the counted-vote read
Model
  margin_s = mu_s + shift_s + e_nat + e_s,   e_nat ~ N(0, s_nat), e_s ~ N(0, s_state)
  mu_s     = bracket-implied margin if the race has margin markets, else SIGMA * Phi^-1(p)
  P(D control) = P(#D seats >= dem_needed) over N simulations (common national error gives
  the correlation the market prices; s_nat/s_state are calibrated so that with zero shift
  the simulation reproduces the market's own control probability).
Output: baseline control probability (market), model-at-zero-shift (calibration check),
  turnout-adjusted control probability, per-race adjusted win probabilities.
"""
from __future__ import annotations

import math
import random
from statistics import NormalDist

SIGMA = 5.0            # end-of-campaign forecast error for a state race (pts)
_ND = NormalDist()


def p_to_margin(p: float) -> float:
    return SIGMA * _ND.inv_cdf(min(0.98, max(0.02, p)))


def margin_to_p(m: float, sd: float = SIGMA) -> float:
    return _ND.cdf(m / sd)


def simulate(seats: list[dict], shifts: dict[str, float], dem_needed: int, s_nat: float, s_state: float,
             n: int = 8000, seed: int = 11) -> tuple[float, dict[str, float]]:
    rng = random.Random(seed)
    mus = []
    for s in seats:
        mu = s.get("implied_margin")
        if mu is None:
            mu = p_to_margin(s["p_dem"])
        mus.append(mu + (shifts.get(s["race_id"]) or 0.0))
    wins = [0] * len(seats)
    ctrl = 0
    for _ in range(n):
        e = rng.gauss(0, s_nat)
        d = 0
        for i, mu in enumerate(mus):
            if mu + e + rng.gauss(0, s_state) > 0:
                d += 1; wins[i] += 1
        if d >= dem_needed:
            ctrl += 1
    return ctrl / n, {s["race_id"]: wins[i] / n for i, s in enumerate(seats)}


def calibrate(seats: list[dict], dem_needed: int, target: float, frac: float = 0.4) -> tuple[float, float, float]:
    """Hold the error split fixed (frac of variance national, shared) and pick a market-consistency
    offset delta (pts added to every race) so the zero-shift simulation reproduces the market's
    own control probability. delta absorbs the gap between individual race prices and the
    control market (correlation / tail views); it is reported, and the turnout shifts are
    applied on top of it."""
    s_nat = SIGMA * math.sqrt(frac)
    s_state = SIGMA * math.sqrt(1 - frac)
    best = (0.0, 9.0)
    for d10 in range(-60, 61, 5):
        delta = d10 / 10
        p, _ = simulate(seats, {s["race_id"]: delta for s in seats}, dem_needed, s_nat, s_state, n=2500)
        if abs(p - target) < best[1]:
            best = (delta, abs(p - target))
    return s_nat, s_state, best[0]


def control(seats_block: dict, turnout_vs_markets: dict[str, float], national_vs_markets: float | None,
            national_weight: float = 0.5, counted: dict[str, float] | None = None) -> dict:
    """seats_block: markets 'senate_seats' entry. turnout_vs_markets: {state: pts} for states
    with their own turnout read. national_vs_markets: pooled read applied (discounted by
    national_weight) to states without one. counted: {race_id: pts} overrides from counted
    votes (projected margin - market margin) once results exist."""
    seats = [s for s in seats_block.get("seats_up_2026", []) if s.get("p_dem") is not None]
    dem_needed = seats_block.get("dem_needed", 51)
    # seats not up: current D seats minus D seats that are up
    cur_d = seats_block.get("current", {}).get("dem", 47)
    d_up = sum(1 for s in seats if s.get("incumbent_party") in ("D", "I"))
    holdover_d = cur_d - d_up
    need_from_up = dem_needed - holdover_d
    target = seats_block.get("baseline_control_p")
    s_nat, s_state, delta = calibrate(seats, need_from_up, target) if target is not None else (SIGMA * 0.63, SIGMA * 0.77, 0.0)
    shifts = {}
    for s in seats:
        st = s["state"]
        if counted and s["race_id"] in counted:
            shifts[s["race_id"]] = counted[s["race_id"]]
        elif st in turnout_vs_markets:
            shifts[s["race_id"]] = turnout_vs_markets[st]
        elif national_vs_markets is not None:
            shifts[s["race_id"]] = national_weight * national_vs_markets
    base_sh = {s["race_id"]: delta for s in seats}
    p0, w0 = simulate(seats, base_sh, need_from_up, s_nat, s_state)
    p1, w1 = simulate(seats, {k: delta + shifts.get(k, 0.0) for k in base_sh}, need_from_up, s_nat, s_state)
    races = []
    for s in seats:
        st = s["state"]
        races.append({"race_id": s["race_id"], "state": st, "incumbent_party": s.get("incumbent_party"),
                      "p_market": s["p_dem"], "p_model0": w0[s["race_id"]], "shift": shifts.get(s["race_id"], 0.0),
                      "shift_source": "counted votes" if (counted and s["race_id"] in counted) else
                      ("own turnout read" if st in turnout_vs_markets else
                       ("national turnout read x%.1f" % national_weight if s["race_id"] in shifts else "none")),
                      "p_adjusted": w1[s["race_id"]], "no_market": s.get("no_market", False)})
    races.sort(key=lambda r: abs(r["p_market"] - 0.5))
    return {"dem_control_market": target, "dem_control_model0": p0, "dem_control_adjusted": p1,
            "dem_needed": dem_needed, "holdover_d": holdover_d, "need_from_up": need_from_up,
            "s_nat": s_nat, "s_state": s_state, "delta": delta, "national_shift": national_vs_markets,
            "national_weight": national_weight, "races": races,
            "expected_d_seats_adjusted": holdover_d + sum(r["p_adjusted"] for r in races)}


if __name__ == "__main__":
    import json, sys
    from pathlib import Path
    here = Path(__file__).resolve().parent
    data = json.loads((here / "data" / "markets_baseline.json").read_text())
    blk = data.get("senate_seats")
    if not blk:
        sys.exit("markets_baseline.json has no senate_seats block yet")
    blk.setdefault("baseline_control_p", (data.get("national") or {}).get("senate_dem_control"))
    out = control(blk, {"FL": -1.8, "NC": 0.3}, -2.6)
    print(json.dumps({k: v for k, v in out.items() if k != "races"}, indent=1))
    for r in out["races"]:
        print(f"{r['race_id']:8s} {r['incumbent_party'] or '?':2s} market {r['p_market']:.2f} model0 {r['p_model0']:.2f} "
              f"shift {r['shift']:+.1f} ({r['shift_source']}) -> {r['p_adjusted']:.2f}")
