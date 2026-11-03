"""Ohio live results: Ohio Secretary of State (liveresults.ohiosos.gov).

The site is a single-page app over a small JSON API (no key); it always serves the election
it is currently pointed at (2026-11-03 tonight), so archived elections cannot be fetched:

  {BASE}/Api/v1/Contest/getContestList
      -> [{"displayName": "U.S. Senator", "contestGroupings": [{"contestName": "U.S. Senator",
           "displayName": "U.S. Senator - Unexpired Term Ending 01/03/2029",
           "contestObjectId": "_55A7..."}]}, ...]
  {BASE}/Api/v1/Contest/GetContestDetailsObject?contestName=..&contestObjectId=..
        &resultsLevel=&countyId=
      -> {"contestDisplayName", "contestList": [one row per county: {"name": "Adams",
          "reportedPrecincts": 0, "totalPrecincts": 21, "candidatesRunning": [
          {"primaryCandidate": "Sherrod Brown", "candidateParty": "D", "voteCount": "0",
           "isWriteIn": false, "runningMate": ...}]}]}

Fallback: the NIST SP 1500-100 (v1) XML export of the whole election (about 1.2 MB, refreshed
every few minutes on the night):

  {BASE}/Api/v1/download?filename=VSSC1622XmlFileBlob

where each contest's VoteCounts carry one line per county GpUnit (and an aggregate line);
counties appear twice as GpUnits ("Adams", which has TotalSubUnits/SubUnitsReported precinct
counts, and "Adams County", a court district), Type="Total" counts only, and IsTest="true"
marks test files.

The host answered 403 (Cloudflare "security service" block page) to the build container,
so this adapter was checked only against JSON captured from the live API on 2026-10-27 by a
third party (2026 general, zero votes) and a synthetic NIST file, not a live fetch.
`election` may be a local directory with saved getContestList.json and one
<contestObjectId>.json per contest, or a saved NIST .xml file, for testing.
2026 contests: "U.S. Senator - Unexpired Term Ending 01/03/2029" (special, Husted v Brown) and
"Governor and Lieutenant Governor" (Ramaswamy v Acton). Ohio counts early in-person and mail
ballots received by Election Day first (posted shortly after 7:30pm ET), then precincts.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET

from . import common

STATE = "OH"
BASE = "https://liveresults.ohiosos.gov"
API = BASE + "/Api/v1/Contest"
NIST_URL = BASE + "/Api/v1/download?filename=VSSC1622XmlFileBlob"

RACES = [
    "U.S. Senator - Unexpired Term Ending 01/03/2029",
    "Governor and Lieutenant Governor",
    "Attorney General",
    "Auditor of State",
    "Secretary of State",
    "Treasurer of State",
    "U.S. House",  # all 15 districts, summed by county
]


def _api_json(path: str, election: str, memo: float = 10, name: str = ""):
    if os.path.isdir(election):
        fn = os.path.join(election, name or path)
        with open(fn if fn.endswith(".json") else fn + ".json", "rb") as f:
            return json.loads(common.decode(f.read()))
    raw = common.http_get(f"{API}/{path}", timeout=30, memo_seconds=memo,
                          cache_name=f"oh/{(name or path.split('?')[0])}.json")
    return json.loads(common.decode(raw))


def _groupings(contest_list) -> list[dict]:
    return [g for c in contest_list or [] if isinstance(c, dict)
            for g in (c.get("contestGroupings") or []) if isinstance(g, dict) and g.get("contestObjectId")]


def _norm_title(t: str) -> str:
    return " ".join((t or "").replace(" ", " ").split())


def _choose(race: str, titles: list[str]) -> list[str]:
    if common.is_house_query(race):
        out = common.select_house(race, titles)
        if not out:
            raise LookupError(f"OH: no U.S. House contest matching {race!r}")
        return out
    # Statewide contests are the ones without a district suffix.
    sizes = {t: 1 if re.search(r"\bDistrict\b|\bCourt of Appeals\b|County", t, re.I) else 2 for t in titles}
    return common.pick_contests(race, sizes)


def _county_rows(details: list[dict], house: bool, label: str) -> list[dict]:
    per = []
    for d in details:
        counties: dict[str, dict] = {}
        pmap: dict[str, str] = {}
        for row in d.get("contestList") or []:
            cname = (row.get("name") or "").strip()
            try:
                fips = common.county_fips(STATE, cname)
            except KeyError:
                continue  # a statewide/total row, not a county
            rec = counties.setdefault(fips, {"county": common.county_name(fips, cname), "votes": {},
                                             "prt": common.opt_int(row.get("reportedPrecincts")),
                                             "ptl": common.opt_int(row.get("totalPrecincts"))})
            for cand in row.get("candidatesRunning") or []:
                nm = _norm_title(cand.get("primaryCandidate") or "")
                if not nm:
                    continue
                party = cand.get("candidateParty") or ""
                if cand.get("isWriteIn"):
                    party = party or "WI"
                pmap.setdefault(nm, party)
                v = cand.get("voteCount")
                rec["votes"][nm] = rec["votes"].get(nm, 0) + common.to_int(
                    v if v not in (None, "") else cand.get("vCount"))
        per.append(common.build_rows(STATE, counties, pmap))
    per = [p for p in per if p]
    if not per:
        return []
    if len(per) == 1:
        return per[0]
    # contest rows give precincts for the contest's part of the county -> sum across districts
    return common.merge_rows(STATE, per, f"Democrats ({label}, {len(per)} contests)",
                             f"Republicans ({label}, {len(per)} contests)", precincts="sum")


def fetch_api(race: str, election: str = "2026-11-03") -> list[dict]:
    """County rows via the JSON API (getContestList + GetContestDetailsObject)."""
    groups = _groupings(_api_json("getContestList", election, memo=600))
    by_title = {}
    for g in groups:
        by_title.setdefault(_norm_title(g.get("displayName") or g.get("contestName")), g)
    chosen = _choose(race, list(by_title))
    details = []
    for t in chosen:
        g = by_title[t]
        q = urllib.parse.urlencode({"contestName": g.get("contestName") or "",
                                    "contestObjectId": g["contestObjectId"],
                                    "resultsLevel": "", "countyId": ""})
        details.append(_api_json(f"GetContestDetailsObject?{q}", election,
                                 name=g["contestObjectId"]))
    house = common.is_house_query(race)
    return _county_rows(details, house, "U.S. House" if house else race)


# ------------------------------------------------------------------ NIST 1500-100 fallback
def _attr_or_child(el, name: str, ns: str) -> str | None:
    v = el.get(name)
    if v is None:
        c = el.find(f"{ns}{name}")
        if c is not None:
            ls = c.find(f"{ns}LanguageString")
            if ls is None:
                ls = c.find(f"{ns}Text")
            v = ls.text if ls is not None else c.text
    return v.strip() if isinstance(v, str) else v


def parse_nist(xml: bytes | str, race: str) -> list[dict]:
    """County rows for `race` from an Ohio NIST SP 1500-100 v1 ElectionReport."""
    root = ET.fromstring(xml)
    ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    xsi_type = "{http://www.w3.org/2001/XMLSchema-instance}type"
    units: dict[str, dict] = {}
    for u in root.iter(f"{ns}GpUnit"):
        units[u.get("ObjectId")] = {
            "name": _attr_or_child(u, "Name", ns) or "", "type": _attr_or_child(u, "Type", ns) or "",
            "rep": common.opt_int(_attr_or_child(u, "SubUnitsReported", ns)),
            "tot": common.opt_int(_attr_or_child(u, "TotalSubUnits", ns))}
    # County units: keep the one that carries precinct counts when a name appears twice.
    county_units: dict[str, str] = {}
    for oid, u in units.items():
        if u["type"].lower() != "county":
            continue
        try:
            fips = common.county_fips(STATE, u["name"])
        except KeyError:
            continue
        county_units[oid] = fips
    reporting: dict[str, tuple] = {}
    for oid, fips in county_units.items():
        u = units[oid]
        if u["tot"] is not None and (fips not in reporting or reporting[fips][1] is None):
            reporting[fips] = (u["rep"], u["tot"])
    parties = {p.get("ObjectId"): (_attr_or_child(p, "Abbreviation", ns) or _attr_or_child(p, "Name", ns) or "")
               for p in root.iter(f"{ns}Party")}
    people = {}
    for c in root.iter(f"{ns}Candidate"):
        nm = _attr_or_child(c, "BallotName", ns) or ""
        people[c.get("ObjectId")] = (" ".join(nm.split()), parties.get((c.findtext(f"{ns}PartyId") or "").strip(), ""))
    contests = {}
    for ct in root.iter(f"{ns}Contest"):
        title = _norm_title(_attr_or_child(ct, "Name", ns) or "")
        if title and "BallotMeasure" not in (ct.get(xsi_type) or ""):
            contests.setdefault(title, ct)
    chosen = _choose(race, list(contests))
    per = []
    for t in chosen:
        counties: dict[str, dict] = {}
        pmap: dict[str, str] = {}
        seen_unit_fips: dict[tuple, str] = {}
        for sel in contests[t].findall(f"{ns}BallotSelection"):
            ids = (sel.findtext(f"{ns}CandidateId") or sel.findtext(f"{ns}CandidateIds") or "").split()
            if not ids:
                continue
            nm, party = people.get(ids[0], (ids[0], ""))
            if sel.get("IsWriteIn") == "true" and not party:
                party = "WI"
            pmap.setdefault(nm, party)
            typed: dict[str, dict[str, int]] = {}  # fips -> {mode: votes} from non-total counts
            for vc in sel.iter(f"{ns}VoteCounts"):
                vtype = (vc.get("Type") or vc.findtext(f"{ns}Type") or "total").strip().lower()
                uid = (vc.findtext(f"{ns}GpUnitId") or "").strip()
                fips = county_units.get(uid)
                if fips is None:
                    continue
                # both "Adams" and "Adams County" units may carry the same votes: count one
                key = (nm, fips)
                if key in seen_unit_fips and seen_unit_fips[key] != uid:
                    continue
                seen_unit_fips[key] = uid
                prt, ptl = reporting.get(fips, (None, None))
                rec = counties.setdefault(fips, {"county": common.county_name(fips, units[uid]["name"]),
                                                 "votes": {}, "prt": prt, "ptl": ptl})
                n = common.to_int(vc.get("Count") or vc.findtext(f"{ns}Count"))
                if vtype == "total":
                    rec["votes"][nm] = rec["votes"].get(nm, 0) + n
                else:  # NIST CountItemType: election-day, early, absentee(-mail/-in-person), provisional...
                    label = vc.get("OtherType") or vtype if vtype == "other" else vtype
                    m = common.vote_mode(label)
                    typed.setdefault(fips, {})[m] = typed.get(fips, {}).get(m, 0) + n
            for fips, md in typed.items():
                rec = counties[fips]
                if nm not in rec["votes"]:  # no Total line: the typed counts are the total
                    rec["votes"][nm] = sum(md.values())
                for m, n in md.items():
                    mv = rec.setdefault("modes", {}).setdefault(m, {})
                    mv[nm] = mv.get(nm, 0) + n
        if counties:
            per.append(common.add_modes(common.build_rows(STATE, counties, pmap), counties))
    if not per:
        return []
    if len(per) == 1:
        return per[0]
    # NIST precinct counts are county-wide -> 'max', not 'sum'
    return common.merge_rows(STATE, per, f"Democrats ({len(per)} contests)",
                             f"Republicans ({len(per)} contests)", precincts="max")


def fetch_nist(race: str, election: str = "2026-11-03") -> list[dict]:
    if os.path.isfile(election):
        return parse_nist(open(election, "rb").read(), race)
    raw = common.http_get(NIST_URL, timeout=60, memo_seconds=10, cache_name="oh/VSSC1622.xml")
    return parse_nist(raw, race)


def fetch(race: str, election: str = "2026-11-03") -> list[dict]:
    """One dict per county for the contest matched by `race` ('U.S. House' sums districts).
    Uses the JSON API, falling back to the NIST XML export if the API fails."""
    if os.path.isfile(election):
        return fetch_nist(race, election)
    if not os.path.isdir(election) and election != "2026-11-03":
        raise LookupError(f"OH live feed serves only the current election (2026-11-03), not {election}")
    try:
        return fetch_api(race, election)
    except (common.FeedError, ValueError) as api_err:
        if os.path.isdir(election):
            raise
        try:
            return fetch_nist(race, election)
        except common.FeedError as xml_err:
            raise common.FeedError(f"OH: API failed ({api_err}); NIST XML failed ({xml_err})") from xml_err


if __name__ == "__main__":
    common.main(fetch, RACES, STATE)
