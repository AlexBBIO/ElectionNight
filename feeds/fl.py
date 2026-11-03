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

Vote type: neither the tab file nor the pipe-delimited Info/Votes files carry a vote-type split,
and floridaelectionwatch.gov shows none, so rows have no "modes". The site's County Reporting
Status page (https://floridaelectionwatch.gov/CountyReportingStatus) does flag, per county,
whether Precincts / Vote-by-Mail / Early Voting / Provisional counting is completed; for the
live election that becomes row["mode_status"] = {"eday"|"mail"|"early"|"other":
"complete"|"partial"|"none"} (best effort: only the "N" = not-completed marker has been seen;
"Y" / "P" are assumed for completed / partial, any other marker is left out).
"""
from __future__ import annotations

import csv
import html
import io
import re

from . import common

STATE = "FL"
LIVE_ELECTION = "2026-11-03"
STATUS_URL = "https://floridaelectionwatch.gov/CountyReportingStatus"
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
    res = _combine(out)
    if election == LIVE_ELECTION:  # the status page only ever describes the current election
        status = _mode_status()
        for r in res:
            if r["fips"] in status:
                r["mode_status"] = status[r["fips"]]
    return res


_MARK = {"N": "none", "Y": "complete", "P": "partial"}
# status columns after the four numeric ones: Precincts Completed, Vote-by-Mail, Early Voting,
# Provisional, 10-day Overseas VBM (the last is not mapped).
_STATUS_COLS = ("eday", "mail", "early", "other")


def _mode_status() -> dict[str, dict]:
    try:
        page = common.decode(common.http_get(common.bust(STATUS_URL, "live", every=30), memo_seconds=30))
    except common.FeedError:
        return {}
    out: dict[str, dict] = {}
    for tr in re.findall(r'<tr class="reporting-row[^"]*">(.*?)</tr>', page, re.S):
        m = re.search(r"<u>(.*?)</u>", tr, re.S)
        if not m:
            continue
        try:
            fips = common.county_fips(STATE, html.unescape(m.group(1)).strip())
        except KeyError:
            continue
        marks = re.findall(r'<span class=["\']?([A-Za-z]*)["\']?\s*>', tr)
        st = {mode: _MARK[k] for mode, k in zip(_STATUS_COLS, marks) if k in _MARK}
        if st:
            out[fips] = st
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
