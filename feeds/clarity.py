"""Generic Clarity / Scytl ENR adapter (results.enr.clarityelections.com).

Many states and counties publish election night results through Clarity ENR. Every
election has a path under the host, e.g. ``GA/115465`` (a state site) or
``KY/Jefferson/123456`` (a county site). The files used here:

  {BASE}/{path}/current_ver.txt                  -> current version number, e.g. "331412"
  {BASE}/{path}/{ver}/json/en/summary.json       -> contest summary (not used here)
  {BASE}/{path}/{ver}/json/en/electionsettings.json -> participating counties (state sites)
  {BASE}/{path}/{ver}/reports/detailxml.zip      -> detail.xml, the full results

detail.xml (state site):

  <ElectionResult>
    <Timestamp>11/13/2014 2:58:41 PM CST</Timestamp> <Region>AR</Region>
    <ElectionVoterTurnout ...><Counties>
      <County name="Arkansas" precinctsParticipating="30" precinctsReported="30" .../>
    </Counties></ElectionVoterTurnout>
    <Contest key="100" text="U.S. Senate" precinctsParticipating=".." precinctsReported="..">
      <ParticipatingCounties><County name="Arkansas" precinctsParticipating="30"
                                     precinctsReported="30"/>...</ParticipatingCounties>
      <Choice key="001" text="Tom Cotton" party="REP" totalVotes="477734">
        <VoteType name="Election Day" votes="248263"><County name="Arkansas" votes="1144"/>...
        <VoteType name="Early Vote" ...> <VoteType name="Absentee" ...> ...

A county site has the same shape with <Precinct> in place of <County> under VoteType, and
<VoterTurnout><Precincts> instead of <ElectionVoterTurnout><Counties>; it is reported as a
single county row. Vote types (and their order) differ by jurisdiction; they are summed into
the row and also returned as "modes" via common.vote_mode() (e.g. GA's old "Election Day Votes"
/ "Advance Voting Votes" / "Absentee by Mail Votes" / "Provisional Votes" -> eday/early/mail/other).

Clarity answers 403 to requests from some networks (it did from the build container), so
this adapter was verified only against the openelections/clarify test fixtures
(AR 2014 general state site, KY Greenup 2014 primary county site), not live.
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile

from . import common

BASE = "https://results.enr.clarityelections.com"


def _url(path: str, rest: str) -> str:
    return f"{BASE}/{path.strip('/')}/{rest.lstrip('/')}"


def current_version(path: str, election: str = "") -> str:
    raw = common.http_get(common.bust(_url(path, "current_ver.txt"), election or "live"),
                          memo_seconds=10)
    ver = common.decode(raw).strip()
    if not ver.isdigit():
        raise common.FeedError(f"unexpected current_ver.txt for {path}: {ver[:60]!r}")
    return ver


def detail_xml(path: str, election: str = "", version: str | None = None) -> bytes:
    """Download and unzip detail.xml for `path` (latest version unless `version` given)."""
    ver = version or current_version(path, election)
    cache = "clarity/" + path.strip("/").replace("/", "_") + f"_{ver}_detailxml.zip"
    # A given version never changes, so the zip may be reused from the cache indefinitely.
    raw = common.http_get(_url(path, f"{ver}/reports/detailxml.zip"), timeout=60,
                          memo_seconds=10, cache_name=cache, cache_max_age=float("inf"))
    if raw[:2] != b"PK":
        if raw.lstrip()[:1] == b"<":
            return raw
        raise common.FeedError(f"detailxml.zip for {path} v{ver} is not a zip")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
        if not name:
            raise common.FeedError(f"no xml inside detailxml.zip for {path} v{ver}")
        return z.read(name)


def parse_detail(xml: bytes | str) -> dict:
    """Parse Clarity detail.xml into
    {"timestamp", "region", "level": "County"|"Precinct",
     "turnout": {unit: (reported, participating)},
     "contests": {name: {"choices": [{"name","party","votes": {unit: int}}],
                         "reporting": {unit: (reported, participating)}}}}"""
    root = ET.fromstring(xml)
    out = {"timestamp": (root.findtext("Timestamp") or "").strip(),
           "region": (root.findtext("Region") or "").strip(),
           "level": "County", "turnout": {}, "contests": {}}
    # State sites: ElectionVoterTurnout/Counties/County; county sites: VoterTurnout/Precincts/Precinct
    for unit in root.findall("ElectionVoterTurnout/Counties/County"):
        out["turnout"][unit.get("name", "")] = (common.opt_int(unit.get("precinctsReported")),
                                                common.opt_int(unit.get("precinctsParticipating")))
    if root.find("VoterTurnout") is not None and root.find("ElectionVoterTurnout") is None:
        out["level"] = "Precinct"

    names_seen: dict[str, int] = {}
    for contest in root.iter("Contest"):
        name = " ".join((contest.get("text") or "").split())
        if not name:
            continue
        if name in names_seen:  # identical titles (rare) -> keep both, disambiguated
            names_seen[name] += 1
            name = f"{name} [{contest.get('key')}]"
        else:
            names_seen[name] = 1
        reporting = {}
        pc = contest.find("ParticipatingCounties")
        if pc is not None:
            for c in pc:
                reporting[c.get("name", "")] = (common.opt_int(c.get("precinctsReported")),
                                                common.opt_int(c.get("precinctsParticipating")))
        choices = []
        for ch in contest.findall("Choice"):
            text = " ".join((ch.get("text") or "").split())
            clean, suffix_party = common.split_party_suffix(text)
            votes: dict[str, int] = {}
            modes: dict[str, dict[str, int]] = {}  # unit -> {mode: votes}
            sub_found = False
            for vt in ch.findall("VoteType"):
                mode = common.vote_mode(vt.get("name", ""))
                for sub in vt:
                    if sub.tag in ("County", "Precinct"):
                        sub_found = True
                        n = sub.get("name", "")
                        v = common.to_int(sub.get("votes"))
                        votes[n] = votes.get(n, 0) + v
                        um = modes.setdefault(n, {})
                        um[mode] = um.get(mode, 0) + v
                        if sub.tag == "Precinct":
                            out["level"] = "Precinct"
            if not sub_found:  # no breakdown at all: put the total on the region
                votes[out["region"]] = common.to_int(ch.get("totalVotes"))
            choices.append({"name": clean or text, "party": ch.get("party") or suffix_party,
                            "votes": votes, "modes": modes, "total": common.to_int(ch.get("totalVotes"))})
        out["contests"][name] = {"choices": choices, "reporting": reporting,
                                 "is_question": contest.get("isQuestion") == "true"}
    return out


def rows_from_parsed(parsed: dict, race: str, state: str, county: str | None = None) -> list[dict]:
    """One row per county for the contest(s) matching `race`.

    county: for a county-level Clarity site, the county name (defaults to <Region>)."""
    contests = parsed["contests"]
    house = common.is_house_query(race)
    if house:
        chosen = common.select_house(race, contests)
        if not chosen:
            raise LookupError(f"no U.S. House contests in Clarity feed ({len(contests)} contests)")
    else:
        sizes = {n: max(len(c["reporting"]), max((len(ch["votes"]) for ch in c["choices"]), default=0))
                 for n, c in contests.items() if not c["is_question"]}
        chosen = common.pick_contests(race, sizes)

    per_contest = []
    for name in chosen:
        c = contests[name]
        counties: dict[str, dict] = {}
        parties: dict[str, str | None] = {}
        for ch in c["choices"]:
            parties[ch["name"]] = ch["party"]
            for unit, v in ch["votes"].items():
                cname = (county or parsed["region"]) if parsed["level"] == "Precinct" else unit
                fips = common.county_fips(state, cname)
                rec = counties.get(fips)
                if rec is None:
                    if parsed["level"] == "Precinct":
                        prt, ptl = _sum_reporting(c["reporting"] or parsed["turnout"])
                    else:
                        prt, ptl = c["reporting"].get(unit) or parsed["turnout"].get(unit) or (None, None)
                    rec = counties[fips] = {"county": common.county_name(fips, cname), "votes": {},
                                            "prt": prt, "ptl": ptl}
                rec["votes"][ch["name"]] = rec["votes"].get(ch["name"], 0) + v
                for m, mv in (ch.get("modes") or {}).get(unit, {}).items():
                    d = rec.setdefault("modes", {}).setdefault(m, {})
                    d[ch["name"]] = d.get(ch["name"], 0) + mv
        per_contest.append(common.add_modes(common.build_rows(state, counties, parties), counties))
    if len(per_contest) == 1:
        return per_contest[0]
    label = "U.S. House" if house else race
    return common.merge_rows(state, per_contest, f"Democrats ({label}, {len(chosen)} contests)",
                             f"Republicans ({label}, {len(chosen)} contests)", precincts="sum")


def _sum_reporting(rep: dict) -> tuple[int | None, int | None]:
    vals = [v for v in rep.values() if v and v[1] is not None]
    if not vals:
        return None, None
    return sum(v[0] or 0 for v in vals), sum(v[1] for v in vals)


def contest_names(parsed: dict) -> list[str]:
    return list(parsed["contests"])


def fetch(path: str, race: str, state: str, election: str = "", county: str | None = None) -> list[dict]:
    """Rows for `race` from the Clarity election at `path` ('GA/115465', 'KY/Jefferson/123456').
    `state` is the 2-letter code used for FIPS lookups."""
    if re.fullmatch(r"[A-Za-z]{2}(/[A-Za-z0-9_. -]+)?/\d+", path.strip("/")) is None:
        raise ValueError(f"not a Clarity election path: {path!r}")
    return rows_from_parsed(parse_detail(detail_xml(path, election)), race, state, county)


if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Generic Clarity ENR adapter")
    ap.add_argument("path", help="Clarity election path, e.g. GA/115465 or KY/Jefferson/123456, "
                                 "or a local detail.xml / detailxml.zip file")
    ap.add_argument("--state", required=True)
    ap.add_argument("--race", required=True)
    ap.add_argument("--county", default=None, help="county name for a county-level site")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    t = time.time()
    import os
    if os.path.exists(a.path):
        data = open(a.path, "rb").read()
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                data = z.read(next(n for n in z.namelist() if n.lower().endswith(".xml")))
        rows = rows_from_parsed(parse_detail(data), a.race, a.state.upper(), a.county)
    else:
        rows = fetch(a.path, a.race, a.state.upper(), county=a.county)
    print(f"Clarity {a.path} race={a.race!r}: {len(rows)} rows in {time.time() - t:.1f}s")
    common.print_table(rows, None if a.all else 15)
