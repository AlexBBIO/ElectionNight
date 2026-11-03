"""Georgia live results: Georgia Secretary of State, results.sos.ga.gov (Enhanced Voting).

Georgia left Clarity ENR (results.enr.clarityelections.com/GA) for an Enhanced Voting site in
2024; its archive goes back to 2012 on the new site. Public JSON API (no key):

  {BASE}/results/public/api/jurisdictions/Georgia
      -> {"elections": [{"publicElectionId": "11032026GeneralAndSpecialElection",
                         "electionDate": "2026-11-03", ...}, ...], "childLocalities": [...159]}
  {BASE}/results/public/api/elections/Georgia/{publicElectionId}
      -> election metadata: asOf / lastUpdated / publishDate, count groups (vote types)
  {BASE}/results/public/api/elections/Georgia/{publicElectionId}/ballot-items
      -> {"data": [{"id", "name": [{"text": "US Senate"}], "reportingStatus":
                    {"reportingUnits": counties reporting, "totalUnits": 159}, ...}]}
  {BASE}/results/public/api/elections/Georgia/{publicElectionId}/ballot-items/{id}
      -> one contest with "breakdownResults": one entry per county: locality.name
         ("Appling County"), precinctReportingStatus {reportingUnits, totalUnits},
         ballotOptions [{name "Jon Ossoff (I) (Dem)", party.abbreviation "Dem", voteCount,
         groupResults per vote type}]
  {BASE}/cdn/results/Georgia/export-{publicElectionId}.json
      -> full precinct-level media export (46 MB for 2026; not used, ballot-items is ~0.5 MB)

Ids: 2026-11-03 = 11032026GeneralAndSpecialElection, 2024-11-05 = 2024NovGen,
2022-11-08 = 2022NovGen. Vote types for 2026: Election Day, Advance Voting, Absentee by Mail,
Provisional, returned per county and candidate as "modes" (eday / early / mail / other).
Georgia reports advance + mail first, from 7pm ET. The API is CDN-cached
(Cache-Control max-age=60), so a poll can be up to a minute behind.

`election` may also be a publicElectionId, or "clarity:<id>" for an old Clarity election
(GA/<id>), which goes through the generic clarity adapter.
"""
from __future__ import annotations

from . import clarity, common

STATE = "GA"
BASE = "https://results.sos.ga.gov"
API = BASE + "/results/public/api"

# 2026-11-03 general: statewide contest names as the feed spells them (top of ticket first).
RACES = [
    "US Senate",
    "Governor",
    "Lieutenant Governor",
    "Secretary of State",
    "Attorney General",
    "Commissioner of Agriculture",
    "Commissioner of Insurance",
    "State School Superintendent",
    "Commissioner of Labor",
    "PSC - District 3",
    "PSC - District 5",
    "U.S. House",  # all 14 districts summed by county
]


def _text(v) -> str:
    if isinstance(v, list):
        for t in v:
            if isinstance(t, dict) and t.get("languageId", "en") == "en":
                return (t.get("text") or "").strip()
        return (v[0].get("text") or "").strip() if v and isinstance(v[0], dict) else ""
    return (v or "").strip() if isinstance(v, str) else ""


def _json(url: str, cache_name: str, election: str, memo: float = 10):
    import json
    raw = common.http_get(url, timeout=30, memo_seconds=memo, cache_name=cache_name,
                          cache_max_age=common.cache_policy(election))
    return json.loads(common.decode(raw))


def election_id(election: str) -> str:
    """Map 'YYYY-MM-DD' (or a publicElectionId) to Georgia's publicElectionId."""
    jur = _json(f"{API}/jurisdictions/Georgia", "ga/jurisdiction.json", "live", memo=1800)
    elections = jur.get("elections") or []
    ids = {e.get("publicElectionId") for e in elections}
    if election in ids:
        return election
    same_day = [e for e in elections if (e.get("electionDate") or "")[:10] == election]
    if not same_day:
        known = ", ".join(f"{e.get('electionDate')}={e.get('publicElectionId')}" for e in elections[:8])
        raise LookupError(f"GA: no election on {election}. Recent: {known} ...")

    def rank(e):
        n = (_text(e.get("name")) + " " + e.get("publicElectionId", "")).lower()
        return ("recount" in n, "runoff" in n, "general" not in n and "gen" not in n)
    return sorted(same_day, key=rank)[0]["publicElectionId"]


def _ballot_items(eid: str, election: str) -> list[dict]:
    d = _json(f"{API}/elections/Georgia/{eid}/ballot-items", f"ga/{eid}/ballot-items.json", election,
              memo=600)  # only names/ids are used from this list
    return d.get("data") or []


def _contest(eid: str, item_id: str, election: str) -> dict:
    return _json(f"{API}/elections/Georgia/{eid}/ballot-items/{item_id}",
                 f"ga/{eid}/item_{item_id}.json", election)


def _rows_for_item(item: dict) -> list[dict]:
    counties: dict[str, dict] = {}
    parties: dict[str, str | None] = {}
    for br in item.get("breakdownResults") or []:
        loc = br.get("locality") or {}
        cname = _text(loc.get("name")) or (loc.get("mapFeatureId") or "")
        fips = common.county_fips(STATE, cname)
        prs = br.get("precinctReportingStatus") or {}
        rec = counties.setdefault(fips, {"county": common.county_name(fips, cname), "votes": {},
                                         "prt": common.opt_int(prs.get("reportingUnits")),
                                         "ptl": common.opt_int(prs.get("totalUnits"))})
        for opt in br.get("ballotOptions") or []:
            name, suffix = common.split_party_suffix(_text(opt.get("name")))
            party = ((opt.get("party") or {}).get("abbreviation")) or suffix
            parties.setdefault(name, party)
            rec["votes"][name] = rec["votes"].get(name, 0) + common.to_int(opt.get("voteCount"))
            # vote types (count groups): Election Day / Advance Voting / Absentee by Mail / Provisional
            for g in opt.get("groupResults") or []:
                m = common.vote_mode(_text(g.get("groupName")))
                mv = rec.setdefault("modes", {}).setdefault(m, {})
                mv[name] = mv.get(name, 0) + common.to_int(g.get("voteCount"))
    return common.add_modes(common.build_rows(STATE, counties, parties), counties)


def fetch(race: str, election: str = "2026-11-03") -> list[dict]:
    """One dict per county for the contest matched by `race` (case-insensitive substring;
    'U.S. House' sums every U.S. House district by county)."""
    if election.startswith("clarity:"):
        return clarity.fetch("GA/" + election.split(":", 1)[1], race, STATE, election)
    eid = election_id(election)
    items = [it for it in _ballot_items(eid, election) if it.get("contestType", "Candidate") == "Candidate"]
    by_name: dict[str, dict] = {}
    for it in items:
        n = _text(it.get("name"))
        if n and n not in by_name:
            by_name[n] = it
    house = common.is_house_query(race)
    if house:
        chosen = common.select_house(race, by_name)
        if not chosen:
            raise LookupError(f"GA {eid}: no U.S. House contests")
    else:
        sizes = {n: common.to_int((it.get("reportingStatus") or {}).get("totalUnits")) or 1
                 for n, it in by_name.items()}
        chosen = common.pick_contests(race, sizes)

    per_contest = []
    for n in chosen:
        item = _contest(eid, by_name[n]["id"], election)
        got, want = len(item.get("breakdownResults") or []), common.to_int(item.get("breakdownResultCount"))
        if want and got < want:
            raise common.FeedError(f"GA {n}: only {got} of {want} county breakdowns returned")
        per_contest.append(_rows_for_item(item))
    if len(per_contest) == 1:
        return per_contest[0]
    label = "U.S. House" if house else race
    return common.merge_rows(STATE, per_contest, f"Democrats ({label}, {len(chosen)} contests)",
                             f"Republicans ({label}, {len(chosen)} contests)", precincts="sum")


if __name__ == "__main__":
    common.main(fetch, RACES, STATE)
