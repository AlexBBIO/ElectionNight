"""Texas live results: Texas Secretary of State election night returns.

The SOS publishes through two systems with the same record shapes (Civix "IVIS" ENR):

Civix ENR (2025 onward; the 2026-11-03 general is election 53815 here):
  {CIVIX}/electionConstants             {"upload": base64(JSON)} -> electionInfo[year][type][id] =
                                        {"ID", "N": "2026 NOVEMBER GENERAL ELECTION", "Y", "ET": "GE"|"P"|"RU"|"S"..., "O"}
  {CIVIX}/election/{id}                 {"Version": "enr/53815/5/", "Home": base64(JSON), "Federal", "StateWide",
                                        "Districted", "OfficeSummary", "Race", "Lookups", "StateWideQ", "ReportList"}
                                        Home: ElecDate "11032026", LastUpdatedTime "Oct 29, 2026 22:13:17" (Central),
                                        RefreshTime 5 (minutes), CountiesReporting {CR, CT}, PollingReporting {PLR, PLT}
  {CIVIX}/election/countyInfo/{id}      {"upload": base64(JSON)} -> {"1".."254" (alphabetical county index):
                                        {"N": "ANDERSON", "TV": registered voters, "Summary": {"PRR": precincts reporting,
                                        "PRP": precincts participating, "PLR"/"NPL": polling places, "VC": ballots, ...},
                                        "Races": {oid: {"N": office, "T": total, "PR": precincts reporting, "TP": precincts,
                                        "C": {cid: {"N": "GREG ABBOTT (I)", "P": "REP"|"DEM"|"LIB"|"GRE"|"W", "V": votes,
                                        "EV": early votes, "PE": pct}}}}}}

Legacy static site (2019-2024 archive; Cloudflare-challenged for scripted requests, so run it
from a normal network):  https://results.texas-election.com/static/data/
  Version.json -> {"___versionNo": n}; ElectionConstants_{n}.json (same electionInfo shape);
  election/{id}/Version.json -> {"___versionNo": ver, "elecDate": "11052024"};
  election/{id}/{ver}/County.json -> {FIPS: county record as above, office name in "ON"}
  (also Home / Federal / StateWide / Districted / OfficeSummary / Race .json).
  Ids: 2024-11-05 = 49664 (v1012), 2022-11-08 = 47009 (v242), 2020-11-03 = 44144 (v262).

Votes: V = total, EV = early voting (Texas reports early in-person + mail as one "Early Voting"
bucket); Election Day = V - EV. Returned per county as modes {"pre", "eday"}. Precincts come from
the county Summary (PRR/PRP), else the race (PR/TP). Statewide roll-ups refresh about every 5
minutes (Home.RefreshTime) once counties report after 7pm CT; counties upload on their own clock.

`election`: YYYY-MM-DD (generals; a primary date means the Republican primary, see ELECTIONS),
"civix:<id>", "legacy:<id>[:<version>]", or a bare id. Unknown dates are discovered from the
Civix election constants.

County fallbacks (fetch_counties / `--counties`): Harris County's own JSON
(appfiles.harrisvotes.com .../Data/{MMYY}/resultsbyballot.json: arrays ab/ev/ed/tv per candidate =
mail / early in person / election day / total) and the Clarity county sites for Dallas, Tarrant,
Bexar, Travis, Denton, Fort Bend, Williamson, El Paso and Hidalgo (results.enr.clarityelections.com/TX/<County>/,
elections.json lists the county's election ids; Collin and Harris are not on Clarity).
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import re

from . import clarity, common

STATE = "TX"
CIVIX = "https://goelect.txelections.civixapps.com/api-ivis-system/api/s3/enr"
LEGACY = "https://results.texas-election.com/static/data"
HARRIS = "https://appfiles.harrisvotes.com/harrisvotes/prd/Data/{code}/resultsbyballot.json"

# 2026-11-03 general: statewide contests as the feed spells them (top of ticket first).
RACES = [
    "GOVERNOR",
    "U. S. SENATOR",
    "LIEUTENANT GOVERNOR",
    "ATTORNEY GENERAL",
    "COMPTROLLER OF PUBLIC ACCOUNTS",
    "COMMISSIONER OF THE GENERAL LAND OFFICE",
    "COMMISSIONER OF AGRICULTURE",
    "RAILROAD COMMISSIONER",
    "U.S. House",  # all 38 districts summed by county
]

# date -> (system, election id, pinned data version or None)
ELECTIONS: dict[str, tuple[str, str, str | None]] = {
    "2026-11-03": ("civix", "53815", None),
    "2026-05-26": ("civix", "58315", None),   # Republican primary runoff (Democratic: civix:58314)
    "2026-03-03": ("civix", "53813", None),   # Republican primary (Democratic: civix:53814)
    "2024-11-05": ("legacy", "49664", "1012"),
    "2022-11-08": ("legacy", "47009", "242"),
    "2020-11-03": ("legacy", "44144", "262"),
}

CLARITY_COUNTIES = ["Dallas", "Tarrant", "Bexar", "Travis", "Denton", "Fort_Bend", "Williamson",
                    "El_Paso", "Hidalgo"]


# ------------------------------------------------------------------ helpers
def _b64json(s):
    if not isinstance(s, str):
        return s
    try:
        return json.loads(base64.b64decode(s))
    except (ValueError, TypeError) as e:
        raise common.FeedError(f"TX: cannot decode base64 JSON payload: {e}") from e


def _unwrap(obj):
    """Civix wraps payloads as {"upload": base64(JSON)}."""
    if isinstance(obj, dict) and set(obj) == {"upload"}:
        return _b64json(obj["upload"])
    return obj


def _civix_json(endpoint: str, election: str, memo: float = 10.0, timeout: float = 60.0):
    url = common.bust(f"{CIVIX}/{endpoint}", election)
    return common.http_get_json(url, timeout=timeout, memo_seconds=memo,
                                cache_name=f"tx/civix/{endpoint}.json",
                                cache_max_age=common.cache_policy(election))


def _legacy_json(path: str, election: str, memo: float = 10.0):
    url = common.bust(f"{LEGACY}/{path}", election)
    try:
        return common.http_get_json(url, timeout=60, memo_seconds=memo, cache_name=f"tx/legacy/{path}",
                                    cache_max_age=common.cache_policy(election))
    except common.FeedError as e:
        if "HTTP 403" in str(e):
            raise common.FeedError(f"{e} (results.texas-election.com answers scripted requests with a "
                                   f"Cloudflare challenge from some networks; run from a normal network)") from e
        raise


def resolve(election: str) -> tuple[str, str, str | None]:
    """-> (system, election id, pinned version or None)."""
    e = (election or "").strip()
    if e in ELECTIONS:
        return ELECTIONS[e]
    m = re.fullmatch(r"(civix|legacy):(\d+)(?::(\d+))?", e, re.I)
    if m:
        return m.group(1).lower(), m.group(2), m.group(3)
    if e.isdigit():
        return ("civix" if int(e) >= 50000 else "legacy"), e, None
    try:
        d = _dt.date.fromisoformat(e)
    except ValueError:
        raise LookupError(f"TX: unknown election {election!r}; use YYYY-MM-DD, civix:<id> or legacy:<id>") from None
    # discover from the Civix constants: same year, general for November dates, else primary/runoff
    ec = _unwrap(_civix_json("electionConstants", "live", memo=1800))
    year = (ec.get("electionInfo") or {}).get(str(d.year)) or {}
    cands = [(t, k, v) for t, grp in year.items() for k, v in (grp or {}).items()]
    want_gen = d.month == 11

    def rank(c):
        t, k, v = c
        n = (v.get("N") or "").upper()
        return (not ((t in ("GE", "G")) == want_gen), "REPUBLICAN" not in n, k)
    if cands:
        t, k, v = sorted(cands, key=rank)[0]
        eid = str(v.get("ID") or k)
        home = _unwrap(_civix_json(f"election/{eid}", e, memo=600).get("Home"))
        if (home or {}).get("ElecDate") == d.strftime("%m%d%Y"):
            return "civix", eid, None
    known = ", ".join(f"{dt}={sy}:{i}" for dt, (sy, i, _) in ELECTIONS.items())
    raise LookupError(f"TX: no election found for {election}. Known: {known}")


def _fips(idx: str, name: str) -> str:
    try:
        return common.county_fips(STATE, name)
    except (KeyError, common.FeedError):
        # Texas FIPS are the odd numbers in alphabetical order (48001 Anderson .. 48507 Zavala)
        return f"48{2 * int(idx) - 1:03d}" if str(idx).isdigit() else idx


def _office(race: dict) -> str:
    return " ".join(str(race.get("N") or race.get("ON") or "").replace(" ", " ").split())


def _precincts(rec: dict, race: dict) -> tuple[int | None, int | None]:
    s = rec.get("Summary") or {}
    if common.opt_int(s.get("PRP")):
        return common.opt_int(s.get("PRR")), common.opt_int(s.get("PRP"))
    tp = common.opt_int(race.get("TP")) or common.opt_int(race.get("TPR"))
    return (common.opt_int(race.get("PR")), tp) if tp else (None, None)


# ------------------------------------------------------------------ county records -> rows
def county_records(election: str = "2026-11-03") -> dict[str, dict]:
    """{fips: county record} for `election` (see module docstring for the record shape)."""
    system, eid, ver = resolve(election)
    if system == "civix":
        raw = _unwrap(_civix_json(f"election/countyInfo/{eid}", election))
        if not isinstance(raw, dict) or not raw:
            raise common.FeedError(f"TX civix {eid}: empty countyInfo")
        out = {}
        for idx, rec in raw.items():
            if not isinstance(rec, dict) or "Races" not in rec:
                continue
            out[_fips(idx, rec.get("N") or "")] = rec
        return out
    if ver is None:
        ver = str(_legacy_json(f"election/{eid}/Version.json", election, memo=60).get("___versionNo"))
    raw = _legacy_json(f"election/{eid}/{ver}/County.json", election)
    if not isinstance(raw, dict) or not raw:
        raise common.FeedError(f"TX legacy {eid}/{ver}: empty County.json")
    return {str(f): rec for f, rec in raw.items() if isinstance(rec, dict) and "Races" in rec}


def _rows_for(records: dict[str, dict], contest: str) -> list[dict]:
    counties: dict[str, dict] = {}
    parties: dict[str, str | None] = {}
    for fips, rec in records.items():
        race = next((r for r in (rec.get("Races") or {}).values() if _office(r) == contest), None)
        if race is None:
            continue
        prt, ptl = _precincts(rec, race)
        d = counties.setdefault(fips, {"county": common.county_name(fips, str(rec.get("N") or "").title()),
                                       "votes": {}, "prt": prt, "ptl": ptl})
        cands = race.get("C") or {}
        for cand in (cands.values() if isinstance(cands, dict) else cands):
            name, suffix = common.split_party_suffix(" ".join(str(cand.get("N") or "").split()))
            if not name:
                continue
            parties.setdefault(name, cand.get("P") or suffix)
            v = common.to_int(cand.get("V"))
            d["votes"][name] = d["votes"].get(name, 0) + v
            if cand.get("EV") is not None:
                ev = common.to_int(cand.get("EV"))
                pre = d.setdefault("modes", {}).setdefault("pre", {})
                eday = d["modes"].setdefault("eday", {})
                pre[name] = pre.get(name, 0) + ev
                eday[name] = eday.get(name, 0) + max(0, v - ev)
    return common.add_modes(common.build_rows(STATE, counties, parties), counties)


def _select(race: str, sizes: dict[str, int], label: str) -> list[str]:
    if common.is_house_query(race):
        chosen = common.select_house(race, sizes)
        if not chosen:
            raise LookupError(f"{label}: no U.S. House contests")
        return chosen
    return common.pick_contests(race, sizes)


def fetch(race: str, election: str = "2026-11-03") -> list[dict]:
    """One dict per county (254) for the contest matched by `race` (case-insensitive substring;
    'U.S. House' sums every U.S. House district by county). Includes modes pre / eday."""
    records = county_records(election)
    sizes: dict[str, int] = {}
    for rec in records.values():
        for r in (rec.get("Races") or {}).values():
            n = _office(r)
            if n:
                sizes[n] = sizes.get(n, 0) + 1
    chosen = _select(race, sizes, f"TX {election}")
    built = [_rows_for(records, c) for c in chosen]
    if len(built) == 1:
        return built[0]
    label = "U.S. House" if common.is_house_query(race) else race
    return common.merge_rows(STATE, built, f"Democrats ({label}, {len(chosen)} contests)",
                             f"Republicans ({label}, {len(chosen)} contests)", precincts="max")


# ------------------------------------------------------------------ county fallbacks
def _harris_code(election: str) -> str:
    if election.isdigit() and len(election) == 4:
        return election
    d = _dt.date.fromisoformat(election)
    return f"{d.month:02d}{d.year % 100:02d}"


def fetch_harris(race: str, election: str = "2026-11-03") -> list[dict]:
    """Harris County (48201) from the county's own results JSON; modes pre (mail + early) / eday."""
    code = _harris_code(election)
    url = common.bust(HARRIS.format(code=code), election)
    d = common.http_get_json(url, timeout=40, memo_seconds=10, cache_name=f"tx/harris/{code}.json",
                             cache_max_age=common.cache_policy(election))
    contests = {" ".join((c.get("t") or "").split()): c for c in (d or {}).get("contests") or [] if c.get("t")}
    chosen = _select(race, {n: common.to_int(c.get("tp")) or 1 for n, c in contests.items()}, f"Harris {code}")
    fips = "48201"
    built = []
    for n in chosen:
        c = contests[n]
        rec = {"county": "Harris", "votes": {}, "prt": common.opt_int(c.get("pr")), "ptl": common.opt_int(c.get("tp"))}
        parties: dict[str, str | None] = {}
        names = c.get("ca") or []
        for i, label in enumerate(names):
            name, party = common.split_party_suffix(label)
            parties.setdefault(name, party)
            tv = common.to_int((c.get("tv") or [0] * len(names))[i])
            rec["votes"][name] = rec["votes"].get(name, 0) + tv
            if c.get("ed") is not None:
                pre = common.to_int((c.get("ab") or [0] * len(names))[i]) + common.to_int((c.get("ev") or [0] * len(names))[i])
                m = rec.setdefault("modes", {})
                m.setdefault("pre", {})[name] = m.get("pre", {}).get(name, 0) + pre
                m.setdefault("eday", {})[name] = m.get("eday", {}).get(name, 0) + common.to_int(c["ed"][i])
        built.append(common.add_modes(common.build_rows(STATE, {fips: rec}, parties), {fips: rec}))
    if len(built) == 1:
        return built[0]
    label = "U.S. House" if common.is_house_query(race) else race
    return common.merge_rows(STATE, built, f"Democrats ({label}, {len(chosen)} contests)",
                             f"Republicans ({label}, {len(chosen)} contests)", precincts="max")


def clarity_election(county: str, election: str) -> str | None:
    """Clarity election id for `county` (path spelling, e.g. 'Fort_Bend') on `election`, if listed."""
    try:
        d = _dt.date.fromisoformat(election)
    except ValueError:
        return election if election.isdigit() else None
    url = common.bust(f"{clarity.BASE}/TX/{county}/elections.json", election)
    lst = common.http_get_json(url, timeout=30, memo_seconds=600) or []
    want = f"{d.month}/{d.day}/{d.year}"
    hits = [e for e in lst if str(e.get("Date") or "").split(" ")[0] == want]
    hits.sort(key=lambda e: "general" not in (e.get("ElectionName") or "").lower())
    return str(hits[0]["EID"]) if hits else None


fallback_notes: dict[str, str] = {}  # county -> why it is missing from the last fetch_counties()


def fetch_counties(race: str, election: str = "2026-11-03") -> list[dict]:
    """Fallback when the SOS feed is unavailable: Harris County's JSON plus the Clarity county
    sites that list `election`. Counties that fail are skipped (see fallback_notes)."""
    fallback_notes.clear()
    rows: list[dict] = []
    try:
        rows += fetch_harris(race, election)
    except (LookupError, common.FeedError, ValueError) as e:
        fallback_notes["Harris"] = str(e)
    for county in CLARITY_COUNTIES:
        try:
            eid = clarity_election(county, election)
            if not eid:
                fallback_notes[county] = "election not listed on Clarity yet"
                continue
            rows += clarity.fetch(f"TX/{county}/{eid}", race, STATE, election, county=county.replace("_", " "))
        except (LookupError, common.FeedError, ValueError) as e:
            fallback_notes[county] = str(e)
    if not rows:
        raise common.FeedError(f"TX county fallbacks: nothing available ({fallback_notes})")
    return sorted(rows, key=lambda r: r["fips"])


if __name__ == "__main__":
    import sys
    if "--counties" in sys.argv:
        sys.argv.remove("--counties")
        common.main(fetch_counties, RACES, STATE)
        if fallback_notes:
            print("skipped:", fallback_notes)
    else:
        common.main(fetch, RACES, STATE)
