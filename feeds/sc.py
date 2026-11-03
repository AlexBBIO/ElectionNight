"""South Carolina live results: SC Election Commission ENR (Clarity / Scytl), linked from scvotes.gov.

  https://results.enr.clarityelections.com/SC/{eid}/current_ver.txt          -> version number
  https://results.enr.clarityelections.com/SC/{eid}/{ver}/reports/detailxml.zip -> detail.xml
  (www.enr-scvotes.org/SC/... is the same Clarity site under SC's own hostname; used as fallback.)
detail.xml: <Contest text=...> -> <ParticipatingCounties><County name precinctsParticipating
precinctsReported/> and <Choice text="REP Name" party="REP"><VoteType name="Election Day|Early
Voting|Absentee By Mail|Failsafe|Provisional|..."><County name votes/>; county votes = sum of vote types.
Election ids: 2026-11-03 general = 127300 ("2026 Statewide General Election", linked from scvotes.gov),
2024-11-05 = 122436, 2022-11-08 = 115412. A numeric election id can be passed instead of a date.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

from . import common

STATE = "SC"
HOSTS = ["https://results.enr.clarityelections.com/SC", "https://www.enr-scvotes.org/SC"]
ELECTIONS = {
    "2026-11-03": "127300",
    "2024-11-05": "122436",
    "2022-11-08": "115412",
    "2026-08-11": "127017",  # U.S. Senate special Republican primary (single contest)
}

# 2026-11-03 general, statewide candidate contests (whitespace-normalised feed names).
RACES = [
    "U.S. Senate",
    "Governor and Lieutenant Governor",
    "Secretary of State",
    "State Treasurer",
    "Attorney General",
    "Comptroller General",
    "State Superintendent of Education",
    "Commissioner of Agriculture",
]


def _eid(election: str) -> str:
    if election.isdigit():
        return election
    try:
        return ELECTIONS[election]
    except KeyError:
        raise LookupError(f"no SC Clarity election id known for {election}; pass the numeric id "
                          f"(see https://scvotes.gov/elections-statistics/election-results/)") from None


def _detail_xml(election: str) -> bytes:
    eid = _eid(election)
    pol = common.cache_policy(election if not election.isdigit() else "live")
    last: Exception | None = None
    for host in HOSTS:
        try:
            ver = common.decode(common.http_get(common.bust(f"{host}/{eid}/current_ver.txt", election),
                                                memo_seconds=10)).strip()
            if not ver.isdigit():
                raise common.FeedError(f"unexpected current_ver {ver[:40]!r}")
            z = common.http_get(f"{host}/{eid}/{ver}/reports/detailxml.zip", timeout=40, memo_seconds=10,
                                cache_name=f"sc/{eid}/{ver}_detailxml.zip", cache_max_age=pol or 3600)
            with zipfile.ZipFile(io.BytesIO(z)) as zf:
                name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
                return zf.read(name)
        except (common.FeedError, zipfile.BadZipFile, StopIteration) as e:
            last = e
    raise common.FeedError(f"SC detail XML unavailable for {eid}: {last}")


def _clean(s: str | None) -> str:
    return " ".join((s or "").split())


def _cand(choice) -> str:
    text, party = _clean(choice.get("text")), (choice.get("party") or "").strip()
    if party and text.upper().startswith(party.upper() + " "):
        text = text[len(party) + 1:]
    return text.replace(" / ", "/").replace("/", " / ")


def fetch(race: str, election: str = "2026-11-03") -> list[dict]:
    """One dict per county for the contest matched by `race` (see common.pick_contests)."""
    root = ET.fromstring(_detail_xml(election))
    contests = {}
    for c in root.iter("Contest"):
        pc = c.find("ParticipatingCounties")
        n = len(pc) if pc is not None else 0
        contests[_clean(c.get("text"))] = (c, n)
    chosen = common.pick_contests(race, {k: v[1] for k, v in contests.items()})

    out: list[list[dict]] = []
    for name in chosen:
        el = contests[name][0]
        per: dict[str, dict] = {}
        pc = el.find("ParticipatingCounties")
        for cty in (pc if pc is not None else []):
            cname = (cty.get("name") or "").strip()
            fips = common.county_fips(STATE, cname)
            per[fips] = {"county": common.county_name(fips, cname), "votes": {},
                         "prt": common.opt_int(cty.get("precinctsReported")),
                         "ptl": common.opt_int(cty.get("precinctsParticipating"))}
        parties: dict[str, str] = {}
        for ch in el.findall("Choice"):
            cand = _cand(ch)
            parties[cand] = (ch.get("party") or "").strip()
            for vt in ch.findall("VoteType"):
                for cty in vt.findall("County"):
                    cname = (cty.get("name") or "").strip()
                    fips = common.county_fips(STATE, cname)
                    c = per.setdefault(fips, {"county": common.county_name(fips, cname), "votes": {},
                                              "prt": None, "ptl": None})
                    c["votes"][cand] = c["votes"].get(cand, 0) + common.to_int(cty.get("votes"))
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
