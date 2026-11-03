"""Kentucky live results: Kentucky State Board of Elections ENR (vrsws.sos.ky.gov/liveresults).

The SBE's election-night site (linked from elect.ky.gov as "2026 General Election Night
Reporting Results") publishes WCF DataContract XML datasets for the current election only:

  {BASE}/Elections                  <Election><ElectionDate>2026-11-03T00:00:00</ElectionDate>
                                    <ElectionName>2026 General Election</ElectionName>...
  {BASE}/Contests                   <Contest><ContestId>..<ContestName>UNITED STATES SENATOR..
                                    <ContestScopeUnitId>, <IsPartisan>, <DisplayOrder>
  {BASE}/Candidates                 <Candidate><BallotName>..<CandidateId>..<ContestId>..
                                    <PoliticalPartyId>, <IsWriteIn>  (ids unique per contest)
  {BASE}/CurrentResultsExcludeLocal <SummaryReport><CandidateData><CandidateData>
                                    {CandidateId, ContestId, ReportingUnitId, TotalVotes,
                                    ElectionDayVotes}... <ReportData><ReportData>{ReportingUnitId,
                                    PrecinctsParticipating, PrecinctsReporting, BallotsCast,
                                    RegisteredVoters, ReportingStatus}  (state, county and
                                    precinct units; CurrentResults also has local contests)
  {BASE}/GeopoliticalUnits          <GeopoliticalUnit>{UnitId, UnitName "Adair County",
                                    UnitType Country|State|County|City|School|Precinct, ParentId}
  {BASE}/PoliticalParties           <PoliticalParty>{PartyId, Abbreviation DEM/REP/..., PartyName}

No ElectionId exists in the feed; it always describes the current election, so archived
elections (e.g. 2024-11-05) cannot be fetched from it. `election` may instead be a local
directory holding saved copies (<Dataset>.xml) for testing.

The host sits behind an Akamai-style WAF that answered 403 ("KY SBE Acceptable Use Policy")
to the build container, so this parser was checked only against XML captured from the live
feed on 2026-11-01 by a third party (zero votes), not against a live fetch.
Each CandidateData row carries ElectionDayVotes, returned as "modes": eday = ElectionDayVotes,
pre = TotalVotes - ElectionDayVotes (early in-person + mail; the feed does not separate them).
Polls close 6pm local: most of the state at 6pm ET, the western (Central-time) third at 7pm ET.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET

from . import common

STATE = "KY"
BASE = "https://vrsws.sos.ky.gov/liveresults/Data"

RACES = [
    "UNITED STATES SENATOR",
    "U.S. House",  # all 6 districts, summed by county
]

_STATIC = ("Elections", "Contests", "Candidates", "GeopoliticalUnits", "PoliticalParties")


def _dataset(name: str, election: str) -> ET.Element:
    if os.path.isdir(election):
        for fn in (name + ".xml", name):
            p = os.path.join(election, fn)
            if os.path.exists(p):
                return ET.fromstring(open(p, "rb").read())
        raise FileNotFoundError(f"{name}.xml not in {election}")
    url = f"{BASE}/{name}"
    static = name in _STATIC
    raw = common.http_get(url if static else common.bust(url, "live"), timeout=45,
                          memo_seconds=900 if static else 10,
                          cache_name=f"ky/{name}.xml")
    return ET.fromstring(raw)


def _check_election(election: str) -> None:
    if os.path.isdir(election):
        return
    recs = common.xml_records(_dataset("Elections", election), "Election")
    dates = [(r.get("ElectionDate") or r.get("election_date") or "")[:10] for r in recs]
    if election not in dates:
        names = [r.get("ElectionName") for r in recs]
        raise LookupError(f"KY live feed serves only the current election ({names} {dates}); "
                          f"{election} is not available from it")


def _b(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "t", "y")


def fetch(race: str, election: str = "2026-11-03") -> list[dict]:
    """One dict per county for the contest matched by `race` ('U.S. House' sums districts)."""
    _check_election(election)
    contests = {r["ContestId"]: r for r in common.xml_records(_dataset("Contests", election), "Contest")}
    cands = {(r["ContestId"], r["CandidateId"]): r
             for r in common.xml_records(_dataset("Candidates", election), "Candidate")}
    units = {r["UnitId"]: r for r in common.xml_records(_dataset("GeopoliticalUnits", election),
                                                        "GeopoliticalUnit")}
    parties = {r["PartyId"]: r.get("Abbreviation") or r.get("PartyName") or ""
               for r in common.xml_records(_dataset("PoliticalParties", election), "PoliticalParty")}
    try:
        res = _dataset("CurrentResultsExcludeLocal", election)
    except (common.FeedError, FileNotFoundError):
        res = _dataset("CurrentResults", election)
    cand_rows = common.xml_records(res, "CandidateData")
    reports = {r.get("ReportingUnitId"): r for r in common.xml_records(res, "ReportData")}

    def county_of(uid: str | None) -> str | None:
        seen = 0
        while uid and uid in units and seen < 10:
            if units[uid].get("UnitType") == "County":
                return uid
            uid = units[uid].get("ParentId")
            seen += 1
        return None

    # Contest name -> ids. Names repeat for local offices; partisan statewide ones are unique.
    by_name: dict[str, list[str]] = {}
    for cid, c in contests.items():
        by_name.setdefault(" ".join((c.get("ContestName") or "").split()), []).append(cid)
    counties_in: dict[str, set] = {}
    for r in cand_rows:
        cu = r.get("ReportingUnitId")
        if units.get(cu, {}).get("UnitType") == "County":
            counties_in.setdefault(r.get("ContestId"), set()).add(cu)

    house = common.is_house_query(race)
    if house:
        chosen = [cid for n in common.select_house(race, by_name) for cid in by_name[n]]
        if not chosen:
            raise LookupError("KY: no U.S. House contests in feed")
    else:
        with_rows = {r.get("ContestId") for r in cand_rows}
        sizes = {n: max(len(counties_in.get(i, ())) for i in ids) or 1 for n, ids in by_name.items()
                 if any(i in with_rows for i in ids)} or {n: 1 for n in by_name}
        name = common.pick_contests(race, sizes)
        chosen = [i for n in name for i in by_name[n]]
        if len(name) == 1 and len(chosen) > 1:  # same title twice: keep the widest one
            chosen = [max(chosen, key=lambda i: len(counties_in.get(i, ())))]

    by_contest: dict[str, list[dict]] = {}
    for r in cand_rows:
        by_contest.setdefault(r.get("ContestId"), []).append(r)

    per = []
    for cid in chosen:
        rows_c = by_contest.get(cid, [])
        has_county = any(units.get(r.get("ReportingUnitId"), {}).get("UnitType") == "County" for r in rows_c)
        counties: dict[str, dict] = {}
        pmap: dict[str, str] = {}
        for r in rows_c:
            uid = r.get("ReportingUnitId")
            utype = units.get(uid, {}).get("UnitType")
            if has_county and utype != "County":
                continue
            if not has_county and utype != "Precinct":
                continue
            cu = uid if utype == "County" else county_of(uid)
            if cu is None:
                continue
            cname = units[cu].get("UnitName") or ""
            fips = common.county_fips(STATE, cname)
            rep = reports.get(cu) or {}
            rec = counties.setdefault(fips, {
                "county": common.county_name(fips, cname), "votes": {},
                "prt": common.opt_int(rep.get("PrecinctsReporting")),
                "ptl": common.opt_int(rep.get("PrecinctsParticipating"))})
            cand = cands.get((cid, r.get("CandidateId"))) or {}
            nm = " ".join((cand.get("BallotName") or f"candidate {r.get('CandidateId')}").split())
            if _b(cand.get("IsWriteIn")):
                nm = f"Write-in ({nm})" if nm.lower() not in ("write-in", "write in") else "Write-in"
            pmap.setdefault(nm, parties.get(cand.get("PoliticalPartyId") or "", ""))
            tot = common.to_int(r.get("TotalVotes"))
            rec["votes"][nm] = rec["votes"].get(nm, 0) + tot
            ed = r.get("ElectionDayVotes")
            if ed not in (None, ""):  # eday = ElectionDayVotes, pre = everything else
                ed = min(common.to_int(ed), tot)
                md = rec.setdefault("modes", {})
                for m, v in (("eday", ed), ("pre", tot - ed)):
                    mv = md.setdefault(m, {})
                    mv[nm] = mv.get(nm, 0) + v
        if counties:
            per.append(common.add_modes(common.build_rows(STATE, counties, pmap), counties))
    if not per:
        return []
    if len(per) == 1:
        return per[0]
    label = "U.S. House" if house else race
    # KY precinct counts are county-wide (ReportData is per unit, not per contest) -> 'max'
    return common.merge_rows(STATE, per, f"Democrats ({label}, {len(per)} contests)",
                             f"Republicans ({label}, {len(per)} contests)", precincts="max")


if __name__ == "__main__":
    common.main(fetch, RACES, STATE)
