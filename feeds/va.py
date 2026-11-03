"""Virginia live results: Virginia Department of Elections ENR (Enhanced Voting platform).

Public site: https://enr.elections.virginia.gov/results/public/virginia/elections/{slug}
JSON API used by that site:
  {API}/jurisdictions/virginia                        -> "elections": [{publicElectionId, electionDate}]
  {API}/elections/virginia/{slug}/data                -> election + "ballotItems" (statewide summary)
  {API}/elections/virginia/{slug}/data/ballot-item/{id} -> "ballotItemWithBreakdown.breakdownResults":
        one entry per locality (133 = 95 counties + 38 independent cities) with
        locality.mapFeatureId (3-digit county FIPS), precinctReportingStatus{reportingUnits,totalUnits},
        ballotOptions[{name, party.abbreviation (D/R/...), voteCount, isWriteIn}]
  API = https://enr.elections.virginia.gov/results/public/api
2026-11-03 slug: "2026-November-General"; 2024-11-05: "2024NovemberGeneral";
2025-11-04: "2025-November-General" (all resolved from the jurisdiction's election list by date).
row["modes"] from each option's groupResults (count groups = vote method): Election Day -> eday,
Early Voting -> early, Mailed Absentee -> mail, Provisional + Post-Election -> other. VA's ENR
reports by vote method, not by central-absentee-precinct, so no CAP counts; instead
row["mode_status"] = {mode: "complete"|"partial"|"none"} from the locality's count-group status.
Independent cities are their own FIPS county-equivalents (e.g. Fairfax city 51600 vs Fairfax
County 51059) - taken straight from mapFeatureId.
"""
from __future__ import annotations

import re

from . import common

STATE = "VA"
API = "https://enr.elections.virginia.gov/results/public/api"
JURIS = "virginia"

# 2026-11-03 general: the only statewide candidate contest. (Also statewide but yes/no:
# "Proposed Constitutional Amendment Question 1/2/3".)
RACES = ["Member, United States Senate"]


def _en(names) -> str:
    if isinstance(names, str):
        return names
    for n in names or []:
        if n.get("languageId") == "en":
            return (n.get("text") or "").strip()
    return ((names or [{}])[0].get("text") or "").strip()


def _get(path: str, election: str, memo: float, cache_name: str | None = None):
    return common.http_get_json(f"{API}/{path}", timeout=40, memo_seconds=memo, cache_name=cache_name,
                                cache_max_age=common.cache_policy(election)) or {}


def resolve_slug(election: str) -> str:
    """'2026-11-03' -> '2026-November-General'. A non-date string is taken as the slug itself."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", election):
        return election
    j = _get(f"jurisdictions/{JURIS}", "live", memo=3600, cache_name="va/jurisdiction.json")
    ids = [e["publicElectionId"] for e in j.get("elections", []) if e.get("electionDate") == election]
    if not ids:
        raise LookupError(f"VA ENR has no election dated {election}")
    gen = [i for i in ids if "general" in i.lower()]
    if len(gen) == 1 or len(ids) == 1:
        return (gen or ids)[0]
    raise LookupError(f"VA ENR has several elections on {election}: {ids}; pass the slug as election")


def fetch(race: str, election: str = "2026-11-03") -> list[dict]:
    """One dict per county / independent city for the contest matched by `race`."""
    slug = resolve_slug(election)
    data = _get(f"elections/{JURIS}/{slug}/data", election, memo=300,
                cache_name=f"va/{slug}/data.json")
    items = {}
    for b in data.get("ballotItems", []):
        rs = b.get("reportingStatus") or {}
        items[_en(b.get("name"))] = (b["id"], rs.get("totalUnits") or len(b.get("crossCounties") or []) or 1)
    chosen = common.pick_contests(race, {k: v[1] for k, v in items.items()})

    out: list[list[dict]] = []
    status: dict[str, dict] = {}
    for contest in chosen:
        bid = items[contest][0]
        d = _get(f"elections/{JURIS}/{slug}/data/ballot-item/{bid}", election, memo=10,
                 cache_name=f"va/{slug}/{bid}.json")
        bi = d.get("ballotItemWithBreakdown") or {}
        parties: dict[str, str | None] = {}
        for o in (bi.get("summaryResults") or {}).get("ballotOptions", []):
            parties[_en(o.get("name"))] = (o.get("party") or {}).get("abbreviation")
        per: dict[str, dict] = {}
        for br in bi.get("breakdownResults") or []:
            loc = br.get("locality") or {}
            lname = _en(loc.get("name"))
            fid = (loc.get("mapFeatureId") or "").strip()
            if re.fullmatch(r"\d{3}", fid):
                fips = "51" + fid
            else:
                fips = common.county_fips(STATE, lname)
            prs = br.get("precinctReportingStatus") or {}
            c = per.setdefault(fips, {"county": common.county_name(fips, lname.title()), "votes": {},
                                      "prt": common.opt_int(prs.get("reportingUnits")),
                                      "ptl": common.opt_int(prs.get("totalUnits"))})
            for o in br.get("ballotOptions") or []:
                cand = _en(o.get("name"))
                parties.setdefault(cand, (o.get("party") or {}).get("abbreviation"))
                c["votes"][cand] = c["votes"].get(cand, 0) + common.to_int(o.get("voteCount"))
                for g in o.get("groupResults") or []:
                    m = c.setdefault("modes", {}).setdefault(common.vote_mode(_en(g.get("groupName"))), {})
                    m[cand] = m.get(cand, 0) + common.to_int(g.get("voteCount"))
        status.update(_mode_status(d))
        out.append(common.add_modes(common.build_rows(STATE, per, parties), per))
    rows = _combine(out)
    for r in rows:
        if r["fips"] in status:
            r["mode_status"] = status[r["fips"]]
    return rows


_STATUS = {"reporting-status-fully-reported": "complete", "reporting-status-election-night-complete": "complete",
           "reporting-status-partially-reported": "partial", "reporting-status-not-reported": "none"}
_RANK = {"none": 0, "partial": 1, "complete": 2}


def _mode_status(d: dict) -> dict[str, dict]:
    """{fips: {mode: "complete"|"partial"|"none"}} from each locality's count-group (vote method)
    reporting status. Several groups in one mode (Provisional + Post-Election -> other) take the
    least-complete status. Empty before the night starts (the feed sends null)."""
    ids = {}
    for loc in (d.get("jurisdiction") or {}).get("childLocalities") or []:
        fid = (loc.get("mapFeatureId") or "").strip()
        if re.fullmatch(r"\d{3}", fid):
            ids[(loc.get("id") or "").lower()] = "51" + fid
    out: dict[str, dict] = {}
    for le in d.get("localityElections") or []:
        fips = ids.get((le.get("jurisdictionId") or "").lower())
        if not fips:
            continue
        for g in le.get("groupReportingStatus") or []:
            st = _STATUS.get(g.get("reportingStatusClientTextKey") or "")
            if st is None:
                continue
            mode = common.vote_mode(_en(g.get("groupName")))
            cur = out.setdefault(fips, {}).get(mode)
            if cur is None or _RANK[st] < _RANK[cur]:
                out[fips][mode] = st
    return out


def _combine(built: list[list[dict]]) -> list[dict]:
    """One contest -> its rows. Several (district contests, e.g. "U.S. House") -> one row per
    county with D / R / total and precincts summed across the contests."""
    if len(built) == 1:
        return built[0]
    n = len(built)
    return common.merge_rows(STATE, built, f"Democrats ({n} contests)", f"Republicans ({n} contests)")


if __name__ == "__main__":
    common.main(fetch, RACES, STATE)
