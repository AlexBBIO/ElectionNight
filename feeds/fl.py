"""Florida live results: Florida Department of State election-night file.

Source (official, FL DOS / floridaelectionwatch.gov "Downloads" page):
  https://flelectionfiles.floridados.gov/enightfilespublic/{YYYYMMDD}_ElecResultsFL.txt
Tab-delimited, one row per (race, county, candidate), UTF-8, CRLF. Columns:
  ElectionDate PartyCode PartyName RaceCode RaceName CountyCode CountyName Juris1num Juris2num
  Precincts PrecinctsReporting CanNameLast CanNameFirst CanNameMiddle CanVotes
Archived elections live at the same path (20241105, 20221108, 20260818 ... verified).
The 2026-11-03 file is published (all zeros) before polls close. FL releases no results
before 8:00 pm ET (panhandle is on Central time).
Ticket races (Governor and Lieutenant Governor) put the governor's surname in CanNameFirst,
the running mate's in CanNameLast and "/" in CanNameMiddle -> shown as "DeSantis / Nuñez".
"""
from __future__ import annotations

import csv
import io

from . import common

STATE = "FL"
URL = "https://flelectionfiles.floridados.gov/enightfilespublic/{ymd}_ElecResultsFL.txt"

# 2026-11-03 general, statewide candidate contests as named in the feed (top of ticket first).
RACES = [
    "United States Senator",
    "Governor and Lieutenant Governor",
    "Attorney General",
    "Chief Financial Officer",
    "Commissioner of Agriculture",
]


def _cand_name(r: dict) -> str:
    last = (r.get("CanNameLast") or "").strip()
    first = (r.get("CanNameFirst") or "").strip()
    mid = (r.get("CanNameMiddle") or "").strip()
    if mid == "/":
        return f"{first} / {last}"
    return " ".join(p for p in (first, mid, last) if p)


def _load(election: str) -> list[dict]:
    ymd = election.replace("-", "")
    url = common.bust(URL.format(ymd=ymd), election)
    raw = common.http_get(url, memo_seconds=10, cache_name=f"fl/{ymd}_ElecResultsFL.txt",
                          cache_max_age=common.cache_policy(election))
    text = common.decode(raw)
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def fetch(race: str, election: str = "2026-11-03") -> list[dict]:
    """One dict per county for the contest matched by `race` (see common.pick_contests)."""
    rows = _load(election)
    # contest = RaceName, disambiguated by jurisdiction numbers only if a name repeats
    juris: dict[str, set] = {}
    for r in rows:
        name = (r.get("RaceName") or "").strip()
        j = ((r.get("Juris1num") or "").strip(), (r.get("Juris2num") or "").strip())
        r["_name"], r["_juris"] = name, j
        juris.setdefault(name, set()).add(j)
    contests: dict[str, set] = {}
    for r in rows:
        name, j = r["_name"], r["_juris"]
        r["_contest"] = name if len(juris[name]) == 1 else f"{name} [{j[0]}|{j[1]}]"
        contests.setdefault(r["_contest"], set()).add(r.get("CountyCode"))
    chosen = common.pick_contests(race, {k: len(v) for k, v in contests.items()})

    out: list[list[dict]] = []
    for contest in chosen:
        counties: dict[str, dict] = {}
        parties: dict[str, str] = {}
        for r in rows:
            if r["_contest"] != contest:
                continue
            cname = (r.get("CountyName") or "").strip()
            fips = common.county_fips(STATE, cname)
            c = counties.setdefault(fips, {
                "county": common.county_name(fips, cname), "votes": {},
                "prt": common.opt_int(r.get("PrecinctsReporting")),
                "ptl": common.opt_int(r.get("Precincts")),
            })
            cand = _cand_name(r)
            parties[cand] = (r.get("PartyCode") or "").strip()
            c["votes"][cand] = c["votes"].get(cand, 0) + common.to_int(r.get("CanVotes"))
        out.append(common.build_rows(STATE, counties, parties))
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
