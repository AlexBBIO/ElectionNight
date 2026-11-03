"""Backtest: does reweighting early county returns by county type / demographics improve the
early read of a state's final swing?  (Offline analysis; needs numpy. Not used at runtime.)

  python3 backtest_reweighting.py            # writes data/backtest_reweighting.json + prints report

Pairs (base -> target), swing = target margin - base margin (D-R pts):
  A  2020 president -> 2022 midterm (Senate, else Governor, else all House)  <- closest analog to tonight
  B  2016 president -> 2020 president
  C  2020 president -> 2024 president

Part 1. How much county-level swing do types / demographics explain beyond the state's own swing?
        Weighted R^2 and residual SD for state-FE, +type dummies, +continuous demographics.
        Type effects (deviation from state swing) and how consistent their sign is across states.
Part 2. Replay: counties finish in a realistic order (state poll-close offsets; small counties first,
        with noise). At 5/10/25/50% of the vote counted, estimate each reporting state's final swing:
          E0 raw         vote-weighted mean swing of finished counties (uniform swing)
          E1 type-state  fill unfinished counties with the mean of finished same-type counties in the state
          E1b engine-v1  the dashboard's first reweighting (type shrinkage toward cross-state pooled
                         deviation from expectation; expectation = truth + poll error)
          E2 type-FE     WLS swing ~ state FE + type dummies on all finished counties; fill unfinished
          E3 demo-FE     WLS swing ~ state FE + college + Black + Hispanic + log pop + non-metro; fill
        Error = estimate - true final state swing.  RMSE / mean error per checkpoint, per state and for
        the vote-weighted aggregate over reporting states (the headline number).
"""
from __future__ import annotations

import csv
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if "election_night" not in sys.modules:  # load this folder as the package, whatever it is called
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "election_night", HERE / "__init__.py", submodule_search_locations=[str(HERE)])
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["election_night"] = _pkg
    _spec.loader.exec_module(_pkg)
from election_night import engine  # noqa: E402

CACHE = HERE / "cache" / "baselines"
EARLY8 = {"KY": 0, "IN": 0, "GA": 60, "VA": 60, "SC": 60, "FL": 60, "NC": 90, "OH": 90}  # minutes after 6pm ET
LAMS = [20, 100, 400]
ENSEMBLES = {"ENS raw+demo": ["E0 raw", "E3 demo-FE"],
             "ENS raw+demo400": ["E0 raw", "E3r demo-FE l=400"],
             "ENS raw+type+demo": ["E0 raw", "E2 type-FE", "E3 demo-FE"]}
TYPES = ["Rural", "Exurban / working metro", "College suburbs", "Urban core", "Black", "Hispanic"]


def margin(d, r, t):
    return (d - r) / t * 100 if t else None


def load_pres(year: int) -> dict:
    out = {}
    p = CACHE / f"pres_{year}.csv"
    if year == 2024:
        p = HERE / "data" / "pres_2024.csv"
        for r in csv.DictReader(open(p)):
            out[r["fips"]] = (int(r["dem"]), int(r["rep"]), int(r["total"]), r["state"])
        return out
    for r in csv.DictReader(open(p)):
        if year == 2016:
            f, st = str(int(float(r["combined_fips"]))).zfill(5), r["state_abbr"]
        else:
            f = r["county_fips"].zfill(5)
            st = None
        out[f] = (int(float(r["votes_dem"])), int(float(r["votes_gop"])), int(float(r["total_votes"])), st)
    return out


def load_types() -> dict:
    t = {}
    for r in csv.DictReader(open(HERE / "data" / "county_types.csv")):
        t[r["fips"]] = {"type": r["type"], "college": float(r["college"]), "black": float(r["black"]),
                        "hisp": float(r["hisp"]), "pop": int(r["pop"]), "rucc": int(r["rucc"])}
    return t


def build_pairs(b: engine.Baselines) -> dict:
    p16, p20, p24 = load_pres(2016), load_pres(2020), load_pres(2024)
    state_of = {f: v[3] for f, v in p24.items()}
    # 2022 statewide office per state: Senate if full coverage, else Governor, else all House
    mid = {}
    for st in sorted(set(state_of.values())):
        fs = [f for f in state_of if state_of[f] == st]
        for office in ("US SENATE", "GOVERNOR", "US HOUSE"):
            have = [f for f in fs if (f, office) in b.c22 and b.c22[(f, office)][0] and b.c22[(f, office)][1]]
            if len(have) >= 0.95 * len(fs):
                for f in have:
                    mid[f] = b.c22[(f, office)]
                break
    pairs = {}
    for name, base, tgt in (("A: 2020 pres -> 2022 midterm", p20, mid),
                            ("B: 2016 pres -> 2020 pres", p16, p20),
                            ("C: 2020 pres -> 2024 pres", p20, p24)):
        rows = {}
        for f, tv in tgt.items():
            st = state_of.get(f)
            bv = base.get(f)
            if not st or not bv or st in ("AK", "CT"):
                continue
            mb, mt = margin(bv[0], bv[1], bv[2]), margin(tv[0], tv[1], tv[2])
            if mb is None or mt is None or not tv[0] or not tv[1]:
                continue
            rows[f] = {"state": st, "swing": mt - mb, "T": tv[2]}
        pairs[name] = rows
    return pairs


# ------------------------------------------------------------------ design matrices / WLS
def design(fips, rows, cov, states, kind):
    st_idx = {s: i for i, s in enumerate(states)}
    k_fe = len(states)
    if kind == "fe":
        k = k_fe
    elif kind == "type":
        k = k_fe + len(TYPES) - 1
    else:
        k = k_fe + 5
    X = np.zeros((len(fips), k))
    for i, f in enumerate(fips):
        X[i, st_idx[rows[f]["state"]]] = 1.0
        c = cov[f]
        if kind == "type":
            j = TYPES.index(c["type"])
            if j > 0:
                X[i, k_fe + j - 1] = 1.0
        elif kind == "demo":
            X[i, k_fe:] = [c["college"] / 10, c["black"] / 10, c["hisp"] / 10,
                           math.log10(max(c["pop"], 1000)), 1.0 if c["rucc"] >= 4 else 0.0]
    return X


def wls(X, y, w, ridge=1e-6, k_fe=None, lam=0.0):
    """Weighted least squares; weights normalised to mean 1 (so a penalty `lam` is in units of
    'average counties'). `lam` shrinks only the non-FE (type / demographic) coefficients to 0."""
    W = w / w.mean()
    pen = np.full(X.shape[1], ridge)
    if k_fe is not None:
        pen[k_fe:] = max(lam, ridge)
    A = X.T @ (X * W[:, None]) + np.diag(pen)
    return np.linalg.solve(A, X.T @ (W * y))


def r2(fips, rows, cov, kind):
    states = sorted({rows[f]["state"] for f in fips})
    X = design(fips, rows, cov, states, kind)
    y = np.array([rows[f]["swing"] for f in fips]); w = np.array([rows[f]["T"] for f in fips], float)
    beta = wls(X, y, w)
    res = y - X @ beta
    ybar = (w * y).sum() / w.sum()
    ss_res, ss_tot = (w * res ** 2).sum(), (w * (y - ybar) ** 2).sum()
    return 1 - ss_res / ss_tot, math.sqrt(ss_res / w.sum()), beta, states


# ------------------------------------------------------------------ part 1
def explain(rows, cov):
    fips = [f for f in rows if f in cov]
    out = {}
    for kind in ("fe", "type", "demo"):
        rr, sd, beta, states = r2(fips, rows, cov, kind)
        out[kind] = {"r2": round(rr, 3), "resid_sd": round(sd, 2)}
        if kind == "demo":
            out[kind]["coef_per_10pts_or_unit"] = dict(zip(["college", "black", "hisp", "log10_pop", "nonmetro"],
                                                           [round(x, 2) for x in beta[len(states):]]))
    # type deviation from state swing (vote-weighted), and sign consistency across states
    st_sw = defaultdict(lambda: [0.0, 0.0])
    for f in fips:
        st_sw[rows[f]["state"]][0] += rows[f]["swing"] * rows[f]["T"]; st_sw[rows[f]["state"]][1] += rows[f]["T"]
    dev = defaultdict(lambda: [0.0, 0.0]); per_state = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for f in fips:
        s = rows[f]["state"]; d = rows[f]["swing"] - st_sw[s][0] / st_sw[s][1]; t = cov[f]["type"]
        dev[t][0] += d * rows[f]["T"]; dev[t][1] += rows[f]["T"]
        per_state[t][s][0] += d * rows[f]["T"]; per_state[t][s][1] += rows[f]["T"]
    types = {}
    for t in TYPES:
        if not dev[t][1]:
            continue
        pooled = dev[t][0] / dev[t][1]
        sts = {s: v[0] / v[1] for s, v in per_state[t].items() if v[1] > 20000}
        same = sum(1 for v in sts.values() if (v > 0) == (pooled > 0))
        types[t] = {"dev_vs_state": round(pooled, 2), "states": len(sts),
                    "same_sign_share": round(same / len(sts), 2) if sts else None,
                    "early8": {s: round(v, 1) for s, v in sts.items() if s in EARLY8}}
    out["type_effects"] = types
    return out


# ------------------------------------------------------------------ part 2
def simulate(rows, cov, state_set, n_sims=200, seed=1, poll_sd=3.0, kappa=0.15):
    rng = random.Random(seed)
    fips = [f for f in rows if f in cov and rows[f]["state"] in state_set]
    states = sorted({rows[f]["state"] for f in fips})
    T = {f: rows[f]["T"] for f in fips}
    truth = {}
    for s in states:
        fs = [f for f in fips if rows[f]["state"] == s]
        truth[s] = sum(rows[f]["swing"] * T[f] for f in fs) / sum(T[f] for f in fs)
    by_state = {s: [f for f in fips if rows[f]["state"] == s] for s in states}
    grand_T = sum(T.values())
    checkpoints = [0.05, 0.10, 0.25, 0.50]
    est_names = ["E0 raw", "E1 type-state", "E1b engine-v1", "E2 type-FE", "E3 demo-FE"] + \
        [f"E2r type-FE l={l}" for l in LAMS] + [f"E3r demo-FE l={l}" for l in LAMS] + list(ENSEMBLES)
    err = {c: {e: [] for e in est_names} for c in checkpoints}
    agg_err = {c: {e: [] for e in est_names} for c in checkpoints}
    for _ in range(n_sims):
        offs = {s: EARLY8.get(s, rng.uniform(0, 180)) + rng.gauss(0, 10) for s in states}
        tdone = {f: offs[rows[f]["state"]] + 25 * (math.log10(max(T[f], 100)) - 3) + rng.gauss(0, 20) for f in fips}
        order = sorted(fips, key=tdone.get)
        exp_sw = {s: truth[s] + rng.gauss(0, poll_sd) for s in states}  # poll-implied expectation
        cum, idx = 0.0, 0
        done = []
        for cp in checkpoints:
            while idx < len(order) and cum < cp * grand_T:
                done.append(order[idx]); cum += T[order[idx]]; idx += 1
            dset = set(done)
            rep_states = [s for s in states if any(f in dset for f in by_state[s])
                          and not all(f in dset for f in by_state[s])]
            if not rep_states:
                continue
            ests = {e: {} for e in est_names}
            # E0 / E1
            for s in rep_states:
                fs = by_state[s]; r_ = [f for f in fs if f in dset]
                m0 = sum(rows[f]["swing"] * T[f] for f in r_) / sum(T[f] for f in r_)
                ests["E0 raw"][s] = m0
                tm = defaultdict(lambda: [0.0, 0.0])
                for f in r_:
                    tm[cov[f]["type"]][0] += rows[f]["swing"] * T[f]; tm[cov[f]["type"]][1] += T[f]
                tot = 0.0
                for f in fs:
                    if f in dset:
                        tot += rows[f]["swing"] * T[f]
                    else:
                        g = tm.get(cov[f]["type"])
                        tot += (g[0] / g[1] if g and g[1] else m0) * T[f]
                ests["E1 type-state"][s] = tot / sum(T[f] for f in fs)
            # E1b: engine v1 (deviation from expectation, shrink to cross-state pooled type deviation)
            pooled = defaultdict(lambda: [0.0, 0.0])
            for f in done:
                s = rows[f]["state"]
                pooled[cov[f]["type"]][0] += (rows[f]["swing"] - exp_sw[s]) * T[f]; pooled[cov[f]["type"]][1] += T[f]
            for s in rep_states:
                fs = by_state[s]
                g = defaultdict(lambda: {"exp": 0.0, "v": 0.0, "num": 0.0})
                for f in fs:
                    x = g[cov[f]["type"]]; x["exp"] += T[f]
                    if f in dset:
                        x["v"] += T[f]; x["num"] += (rows[f]["swing"] - exp_sw[s]) * T[f]
                totT = sum(x["exp"] for x in g.values()); rn = rd = 0.0
                for t, x in g.items():
                    rg = x["num"] / x["v"] if x["v"] else None
                    pg = pooled[t][0] / pooled[t][1] if pooled[t][1] else None
                    c = x["v"] / x["exp"]
                    est = ((c * rg + kappa * pg) / (c + kappa)) if (rg is not None and pg is not None) else (rg if rg is not None else pg)
                    if est is not None:
                        rn += x["exp"] / totT * est; rd += x["exp"] / totT
                ests["E1b engine-v1"][s] = exp_sw[s] + rn / rd
            # E2 / E3: pooled WLS with state FE over all finished counties in reporting-or-complete states
            fit_f = [f for f in done]
            fit_states = sorted({rows[f]["state"] for f in fit_f})
            y = np.array([rows[f]["swing"] for f in fit_f]); w = np.array([T[f] for f in fit_f], float)
            variants = [("E2 type-FE", "type", 0.0), ("E3 demo-FE", "demo", 0.0)] + \
                [(f"E2r type-FE l={l}", "type", l) for l in LAMS] + [(f"E3r demo-FE l={l}", "demo", l) for l in LAMS]
            for name, kind, lam in variants:
                if len(fit_f) < len(fit_states) + 8:
                    for s in rep_states:
                        ests[name][s] = ests["E0 raw"][s] if lam else ests["E1 type-state"][s]
                    continue
                beta = wls(design(fit_f, rows, cov, fit_states, kind), y, w, ridge=1.0,
                           k_fe=len(fit_states) if lam else None, lam=lam)
                for s in rep_states:
                    fs = by_state[s]; un = [f for f in fs if f not in dset]
                    pred = design(un, rows, cov, fit_states, kind) @ beta
                    tot = sum(rows[f]["swing"] * T[f] for f in fs if f in dset) + float((pred * np.array([T[f] for f in un])).sum())
                    ests[name][s] = tot / sum(T[f] for f in fs)
            for en, parts in ENSEMBLES.items():
                ests[en] = {s: sum(ests[p][s] for p in parts) / len(parts) for s in rep_states}
            sT = {s: sum(T[f] for f in by_state[s]) for s in rep_states}
            tr_agg = sum(truth[s] * sT[s] for s in rep_states) / sum(sT.values())
            for e in est_names:
                for s in rep_states:
                    err[cp][e].append(ests[e][s] - truth[s])
                agg_err[cp][e].append(sum(ests[e][s] * sT[s] for s in rep_states) / sum(sT.values()) - tr_agg)

    def summ(xs):
        if not xs:
            return None
        a = np.array(xs)
        return {"rmse": round(float(np.sqrt((a ** 2).mean())), 2), "mean": round(float(a.mean()), 2), "n": len(xs)}
    return {"per_state": {str(c): {e: summ(err[c][e]) for e in est_names} for c in checkpoints},
            "aggregate": {str(c): {e: summ(agg_err[c][e]) for e in est_names} for c in checkpoints}}


def main():
    b = engine.Baselines.load()
    cov = load_types()
    pairs = build_pairs(b)
    report = {}
    for name, rows in pairs.items():
        all_states = sorted({r["state"] for r in rows.values()})
        report[name] = {
            "counties": len(rows), "states": len(all_states),
            "explain_all_states": explain(rows, cov),
            "explain_early8": explain({f: r for f, r in rows.items() if r["state"] in EARLY8}, cov),
            "replay_early8": simulate(rows, cov, set(EARLY8)),
            "replay_all_states": simulate(rows, cov, set(all_states), n_sims=60),
        }
        print(f"done {name}", file=sys.stderr)
    out = HERE / "data" / "backtest_reweighting.json"
    out.write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
