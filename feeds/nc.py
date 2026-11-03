"""North Carolina live results: NC State Board of Elections ENR (er.ncsbe.gov).

The public ENR site reads JSON arrays (served as .txt) from:
  https://er.ncsbe.gov/enr/{YYYYMMDD}/data/county.txt         counties: cid, cnm, prt/ptl, imp (last import)
  https://er.ncsbe.gov/enr/{YYYYMMDD}/data/results_{cid}.txt  one row per (contest, candidate) in county cid
       fields: cnm contest, bnm candidate, pty party, vct votes, prt/ptl precincts reporting/total,
               evc/ovc/avc/pvc = election-day / one-stop(early) / absentee-mail / provisional votes,
               ogl contest group (FED, COS, NCS, NCH, JUD, REF, ...)
  (results_0.txt is the statewide roll-up; enr/elections.txt lists available election dates.)
cid 1..100 is alphabetical (FIPS ~ 37000 + 2*cid - 1, except McDowell). One small file per county, fetched in
parallel (~100 requests per poll). Archived elections use the same paths (20241105 verified).
Bulk precinct alternative: https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/{YYYY_MM_DD}/results_pct_{YYYYMMDD}.zip
"""
from __future__ import annotations

import concurrent.futures as cf

from . import common

STATE = "NC"
BASE = "https://er.ncsbe.gov/enr/{ymd}/data/"

RACES = [
    "US SENATE (VOTE FOR 1)",
    "NC SUPREME COURT ASSOCIATE JUSTICE SEAT 01 (VOTE FOR 1)",
    "NC COURT OF APPEALS JUDGE SEAT 01 (VOTE FOR 1)",
    "NC COURT OF APPEALS JUDGE SEAT 02 (VOTE FOR 1)",
    "NC COURT OF APPEALS JUDGE SEAT 03 (VOTE FOR 1)",
]

WORKERS = 8


def _get_json(election: str, name: str):
    ymd = election.replace("-", "")
    url = common.bust(BASE.format(ymd=ymd) + name, election, every=10)
    return common.http_get_json(url, memo_seconds=10, cache_name=f"nc/{ymd}/{name}",
                                cache_max_age=common.cache_policy(election)) or []


def _fips(cid: str, cnm: str) -> str:
    try:
        return common.county_fips(STATE, cnm)
    except (KeyError, common.FeedError):
        # fallback if the Census file is unreachable: cid is alphabetical, FIPS nearly so
        # (FIPS sorts McDowell before Macon/Madison/Martin)
        n = int(cid)
        return "37" + {56: "113", 57: "115", 58: "117", 59: "111"}.get(n, f"{2 * n - 1:03d}")


def fetch(race: str, election: str = "2026-11-03") -> list[dict]:
    """One dict per county for the contest matched by `race` (see common.pick_contests)."""
    counties = [c for c in _get_json(election, "county.txt") if str(c.get("cid")) not in ("0", "-1")]
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        results = dict(zip([c["cid"] for c in counties],
                           ex.map(lambda c: _get_json(election, f"results_{c['cid']}.txt"), counties)))

    seen: dict[str, set] = {}
    for cid, rows in results.items():
        for r in rows:
            seen.setdefault(r.get("cnm", "").strip(), set()).add(cid)
    chosen = common.pick_contests(race, {k: len(v) for k, v in seen.items()})

    out: list[list[dict]] = []
    for contest in chosen:
        per: dict[str, dict] = {}
        parties: dict[str, str] = {}
        for c in counties:
            rows = [r for r in results[c["cid"]] if r.get("cnm", "").strip() == contest]
            if not rows:
                continue
            fips = _fips(c["cid"], c["cnm"])
            d = per.setdefault(fips, {"county": common.county_name(fips, c["cnm"].title()), "votes": {},
                                      "prt": common.opt_int(rows[0].get("prt")),
                                      "ptl": common.opt_int(rows[0].get("ptl"))})
            for r in rows:
                cand = (r.get("bnm") or "").strip()
                parties[cand] = (r.get("pty") or "").strip()
                d["votes"][cand] = d["votes"].get(cand, 0) + common.to_int(r.get("vct"))
        out.append(common.build_rows(STATE, per, parties))
    return _combine(out)


def _combine(built: list[list[dict]]) -> list[dict]:
    """One contest -> its rows. Several (district contests, e.g. "U.S. House") -> one row per
    county with D / R / total and precincts summed across the contests."""
    if len(built) == 1:
        return built[0]
    n = len(built)
    return common.merge_rows(STATE, built, f"Democrats ({n} contests)", f"Republicans ({n} contests)")


if __name__ == "__main__":
    common.main(fetch, RACES, STATE)
