"""Indiana live results: Indiana Election Division ENR (enr.indianavoters.in.gov).

Static JSON files behind an AngularJS app (no key). The live election is under /site/;
past elections stay up under /archive/<YYYY><General|Primary>/ in the same format:

  {root}/data/settings.json                -> {"Root": {"VersionType": "A"|"B",
                                               "CurrentElection": "11/03/2026",
                                               "IntervalInMinutes": "2", "Certified": "F", ...}}
  {root}/data/statewideElectionsC_{V}.json -> office categories:
      List[{Heading: Federal|State|Local, Items.Item[{OFFICECATEGORYID, OFFICE_CATEGORY_NAME}]}]
      plus JurisdictionsReporting {TOTAL_ALLPRECINCTSREPORTING, TOTAL_ALLPRECINCTS} (statewide)
  {root}/data/OffCatC_{id}_{V}.json        -> one office category:
      OfficeCategory.Regions.Region[]: a county (MAP_FIPS, MAP_JURISDICTION_NAME) for statewide
      offices, or a district for U.S. House; each has Races.Race[] with Jurisdiction (county FIPS
      for district offices), OFFICE_TITLE and Candidates.Candidate[{NAME_ON_BALLOT,
      PARTY_ABBREV "D"/"R"/..., TOTAL_VOTES}]

Quirks: the files are XML converted to JSON, so a one-element list arrives as a bare object;
the version suffix flips between A and B as the site republishes (read settings.json first);
OFFICECATEGORYIDs change between elections, so categories are matched by name. No per-county
precinct counts are published (precincts_* are None); only the statewide count is.
No vote-type split exists in the candidate data either, so rows carry no "modes" (the
statewideTurnout file has county InPerson/Absentee *voter* counts only, not votes by party).
Indiana has no Senate or Governor race in 2026; statewide offices are Secretary of State,
State Comptroller (Auditor) and Treasurer. race="U.S. House" sums every district by county
(uncontested districts contribute only one party). Polls close 6pm local; the two Central-time
corners (NW and SW) close at 7pm ET.
"""
from __future__ import annotations

import datetime as _dt
import json

from . import common

STATE = "IN"
BASE = "https://enr.indianavoters.in.gov"

RACES = [
    "Secretary of State",
    "State Comptroller (Auditor of State)",
    "Treasurer of State",
    "U.S. House",  # all 9 districts, summed by county
]


def _list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _root_candidates(election: str) -> list[str]:
    if election.startswith(("site", "archive/")):
        return [election.strip("/")]
    d = _dt.date.fromisoformat(election)
    kind = "Primary" if d.month in (3, 4, 5, 6) else "General"
    return ["site", f"archive/{d.year}{kind}"]


def _get(root: str, name: str, election: str, memo: float = 10):
    url = f"{BASE}/{root}/data/{name}.json"
    if root == "site":
        url = common.bust(url, election)
    raw = common.http_get(url, timeout=30, memo_seconds=memo,
                          cache_name=f"in/{root.replace('/', '_')}/{name}.json",
                          cache_max_age=None if root == "site" else float("inf"))
    d = json.loads(common.decode(raw))
    return d.get("Root", d.get("root", d)) if isinstance(d, dict) else d


def _resolve(election: str) -> tuple[str, dict]:
    """(root path, settings) for `election` ('YYYY-MM-DD', 'site', or 'archive/2022General')."""
    tried = []
    for root in _root_candidates(election):
        try:
            st = _get(root, "settings", election, memo=5)
        except common.FeedError as e:
            tried.append(f"{root}: {e}")
            continue
        cur = st.get("CurrentElection") or ""
        try:
            cur_iso = _dt.datetime.strptime(cur, "%m/%d/%Y").date().isoformat()
        except ValueError:
            cur_iso = cur
        if election in (root, cur_iso) or not election[:4].isdigit():
            return root, st
        tried.append(f"{root}: serves {cur_iso}")
    raise LookupError(f"IN: no ENR data for {election} ({'; '.join(tried)})")


_STATEWIDE_OFFICES = ("president", "us senator", "governor", "secretary of state", "auditor of state",
                      "state comptroller", "treasurer of state", "attorney general",
                      "superintendent of public instruction")


def _categories(root: str, ver: str, election: str) -> dict[str, tuple[str, int]]:
    """{category name: (OFFICECATEGORYID, weight)}; weight 3 = statewide office, 2 = other
    Federal/State heading, 1 = Local (so 'Auditor' finds the Auditor of State, not County Auditor)."""
    d = _get(root, f"statewideElectionsC_{ver}", election, memo=300)
    out = {}
    for heading in _list(d.get("List")):
        level = 2 if (heading.get("Heading") or "").strip() in ("Federal", "State") else 1
        for item in _list((heading.get("Items") or {}).get("Item")):
            name = item["OFFICE_CATEGORY_NAME"]
            w = 3 if any(o in name.lower().replace(".", "") for o in _STATEWIDE_OFFICES) else level
            out[name] = (item["OFFICECATEGORYID"], w)
    return out


def fetch(race: str, election: str = "2026-11-03") -> list[dict]:
    """One dict per county for the office matched by `race`; 'U.S. House' sums all districts."""
    root, st = _resolve(election)
    ver = st.get("VersionType") or "A"
    cats = _categories(root, ver, election)
    house = common.is_house_query(race)
    if house:
        cat = next((n for n in cats if common.is_house_contest(n)
                    or n.strip().lower() in ("us representative", "u.s. representative")), None)
        if cat is None:
            raise LookupError(f"IN: no U.S. Representative category in {sorted(cats)}")
    else:
        top = max(w for _, w in cats.values())
        try:  # statewide offices first, then everything
            cat = common.pick_contests(race, {n: 1 for n, (_, w) in cats.items() if w == top})[0]
        except LookupError:
            cat = common.pick_contests(race, {n: w for n, (_, w) in cats.items()})[0]
    data = _get(root, f"OffCatC_{cats[cat][0]}_{ver}", election)

    # (office title) -> {fips: {"county", "votes"}}, and candidate -> party
    contests: dict[str, dict[str, dict]] = {}
    parties: dict[str, dict[str, str]] = {}
    regions = _list(((data.get("OfficeCategory") or {}).get("Regions") or {}).get("Region"))
    for reg in regions:
        for rc in _list((reg.get("Races") or {}).get("Race")):
            title = " ".join((rc.get("OFFICE_TITLE") or cat).split())
            jur = rc.get("Jurisdiction") or {}
            fips = jur.get("FIPS") or reg.get("MAP_FIPS")
            cname = jur.get("JURISDICTION_NAME") if jur.get("FIPS") else reg.get("MAP_JURISDICTION_NAME")
            if not fips:
                fips = common.county_fips(STATE, cname or "")
            fips = str(fips).zfill(5)
            if house and "special" in (title + " " + str(reg.get("MAP_JURISDICTION_NAME"))).lower() \
                    and "special" not in race.lower():
                continue  # e.g. 2022 CD-2 special alongside the regular CD-2 race
            c = contests.setdefault(title, {}).setdefault(
                fips, {"county": common.county_name(fips, cname or fips), "votes": {},
                       "prt": None, "ptl": None})
            pmap = parties.setdefault(title, {})
            for cand in _list((rc.get("Candidates") or {}).get("Candidate")):
                name = (cand.get("NAME_ON_BALLOT") or cand.get("CandidateName") or "").strip()
                pmap.setdefault(name, cand.get("PARTY_ABBREV") or cand.get("PARTY") or "")
                c["votes"][name] = c["votes"].get(name, 0) + common.to_int(
                    cand.get("TOTAL_VOTES", cand.get("TOTAL")))
    if not contests:
        raise LookupError(f"IN: category {cat!r} has no races")

    if house:
        titles = common.select_house(race, sorted(contests)) or (
            sorted(contests) if common.district_number(race) is None else [])
        if not titles:
            raise LookupError(f"IN: no U.S. House contest matching {race!r} in {sorted(contests)}")
    elif len(contests) == 1:
        titles = list(contests)
    else:
        try:
            titles = common.pick_contests(race, {t: len(v) for t, v in contests.items()})
        except LookupError:
            titles = [max(contests, key=lambda t: len(contests[t]))]
    per = [common.build_rows(STATE, contests[t], parties[t]) for t in titles]
    if len(per) == 1:
        return per[0]
    label = "U.S. House" if house else cat
    return common.merge_rows(STATE, per, f"Democrats ({label}, {len(titles)} contests)",
                             f"Republicans ({label}, {len(titles)} contests)", precincts="none")


def statewide_precincts(election: str = "2026-11-03") -> tuple[int | None, int | None]:
    """(precincts reporting, total precincts) statewide; Indiana gives no county breakdown."""
    root, st = _resolve(election)
    d = _get(root, f"statewideElectionsC_{st.get('VersionType') or 'A'}", election)
    jr = d.get("JurisdictionsReporting") or {}
    return common.opt_int(jr.get("TOTAL_ALLPRECINCTSREPORTING")), common.opt_int(jr.get("TOTAL_ALLPRECINCTS"))


if __name__ == "__main__":
    common.main(fetch, RACES, STATE)
