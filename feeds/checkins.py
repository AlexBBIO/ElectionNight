"""Live Election Day in-person turnout BY PARTY (cumulative check-ins / voted credit), stdlib only.

What is machine-readable today (2026-11-03):

* Florida, all 67 counties -- VR Systems "Turnout Quick View" (TQV), the turnout widget that
  county Supervisor of Elections sites embed (https://tqv.vrswebapps.com/?state=FL&county=SEM).
  Static JSON, no auth, updated roughly hourly on Election Day:
      https://tqv.vrswebapps.com/data/FL/{CTY}/index.json          -> [current election id]
      https://tqv.vrswebapps.com/data/FL/{CTY}/{id}/data.json      -> Summary + Turnout
  Turnout.PartyType[party]["ElectionDay"] is the cumulative ED in-person count by voter
  registration party; Summary.LastUpdatedTime is the snapshot time (UTC). The file is a
  *snapshot*: it carries no hour-by-hour history, so same-hour series are built by polling
  (each call to fetch() yields one row per county at its current as_of; run.py/history.jsonl
  or a cron can keep them). Older elections stay published under their own ids (final
  snapshots), which is where the 2022/2024 "final" rows in data/checkins.csv come from
  (`python3 -m election_night.feeds.checkins --history`).

* Washoe County, NV -- the Registrar posts voter-level in-person turnout CSVs (with
  VOTER_REG_PARTY and DATE_VOTED) on https://www.washoecounty.gov/voters/results/2026data.php,
  including Election Day snapshots labelled "In-Person 9:00am Report" (the June primary also
  had 12pm, 3pm and Final). washoe_rows() aggregates DATE_VOTED == 11/3/2026 by party.

Not machine-readable (manual only) -- see SOURCES and data/checkins.csv:
* Maricopa County AZ: party-level ED check-ins are distributed to parties/campaigns through the
  county's login-only "Election Data" portal; the public sees only total check-in counts in
  @MaricopaVote posts / press briefings. The seeded 2022/2024/2026 10:00 rows come from the
  widely shared Stealth Analytics / Data Orbital chart (@treyterry).
* NV, NC, GA, PA, KY: no live party-level ED check-in feed found (see SOURCES).
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

from . import common

ELECTION_DAY = "2026-11-03"
TQV_BASE = "https://tqv.vrswebapps.com/data/FL/{cty}/"
CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "checkins.csv"
COLUMNS = ["jurisdiction", "state", "year", "hour_local", "rep", "dem", "oth", "source", "as_of"]
TQV_SOURCE = "VR Systems Turnout Quick View (tqv.vrswebapps.com) for {name} County SOE"

# FVRS county codes -> county names. Panhandle counties west of the Apalachicola are Central.
FL_COUNTIES = {
    "ALA": "Alachua", "BAK": "Baker", "BAY": "Bay", "BRA": "Bradford", "BRE": "Brevard",
    "BRO": "Broward", "CAL": "Calhoun", "CHA": "Charlotte", "CIT": "Citrus", "CLA": "Clay",
    "CLL": "Collier", "CLM": "Columbia", "DAD": "Miami-Dade", "DES": "DeSoto", "DIX": "Dixie",
    "DUV": "Duval", "ESC": "Escambia", "FLA": "Flagler", "FRA": "Franklin", "GAD": "Gadsden",
    "GIL": "Gilchrist", "GLA": "Glades", "GUL": "Gulf", "HAM": "Hamilton", "HAR": "Hardee",
    "HEN": "Hendry", "HER": "Hernando", "HIG": "Highlands", "HIL": "Hillsborough", "HOL": "Holmes",
    "IND": "Indian River", "JAC": "Jackson", "JEF": "Jefferson", "LAF": "Lafayette", "LAK": "Lake",
    "LEE": "Lee", "LEO": "Leon", "LEV": "Levy", "LIB": "Liberty", "MAD": "Madison",
    "MAN": "Manatee", "MRN": "Marion", "MRT": "Martin", "MON": "Monroe", "NAS": "Nassau",
    "OKA": "Okaloosa", "OKE": "Okeechobee", "ORA": "Orange", "OSC": "Osceola", "PAL": "Palm Beach",
    "PAS": "Pasco", "PIN": "Pinellas", "POL": "Polk", "PUT": "Putnam", "SAN": "Santa Rosa",
    "SAR": "Sarasota", "SEM": "Seminole", "STJ": "St. Johns", "STL": "St. Lucie", "SUM": "Sumter",
    "SUW": "Suwannee", "TAY": "Taylor", "UNI": "Union", "VOL": "Volusia", "WAK": "Wakulla",
    "WAL": "Walton", "WAS": "Washington",
}
FL_CENTRAL = {"BAY", "CAL", "ESC", "HOL", "JAC", "OKA", "SAN", "WAL", "WAS"}  # Gulf is split; Port St. Joe is ET

SOURCES = [
    {"jurisdiction": "Florida (all 67 counties)", "state": "FL", "kind": "live, machine-readable",
     "party": True, "hourly_history": False,
     "url": "https://tqv.vrswebapps.com/data/FL/{CTY}/{election_id}/data.json",
     "notes": "VR Systems Turnout Quick View JSON behind every county SOE turnout widget. "
              "Turnout.PartyType[REP|DEM|NPA|minor]['ElectionDay'] = cumulative ED in-person voters "
              "by registration party; Summary.LastUpdatedTime = snapshot time (~hourly on ED). "
              "No intraday history in the file; past elections (2018-2026) remain at their own ids "
              "as final snapshots. Wayback Machine was unreachable from this environment "
              "(connection resets), so 2022/2024 same-hour FL figures were not recovered."},
    {"jurisdiction": "Maricopa County", "state": "AZ", "kind": "manual (third-party chart); news pages 403",
     "party": True, "hourly_history": True,
     "url": "https://elections.maricopa.gov/results-and-data/election-data.html",
     "notes": "Party-level ED check-in data goes to parties/campaigns via a login-only data portal "
              "(cities, towns, political parties). Public: total check-ins only (@MaricopaVote on X, "
              "press briefings). Party splits circulate via Data Orbital / Stealth Analytics "
              "(@treyterry) charts; seeded into data/checkins.csv by hand."},
    {"jurisdiction": "Washoe County", "state": "NV", "kind": "live (a few ED snapshots), machine-readable",
     "party": True, "hourly_history": True,
     "url": "https://www.washoecounty.gov/voters/results/2026data.php",
     "notes": "Voter-level in-person CSVs (VOTER_REG_PARTY, DATE_VOTED); ED reports labelled 9:00am "
              "(12pm/3pm/Final expected, as in the 2026 primary). Only aggregate party counts are kept. "
              "No 2022/2024 equivalents found on the site (2022data.php/2024data.php 404)."},
    {"jurisdiction": "Clark County / NV statewide", "state": "NV", "kind": "not reachable / next-day",
     "party": True, "hourly_history": False,
     "url": "https://www.nvsos.gov/vivid/",
     "notes": "nvsos.gov sits behind an Incapsula bot check (not circumvented). NV SOS/Clark publish "
              "daily turnout by party, incl. ED in-person, as end-of-day files, not intraday."},
    {"jurisdiction": "North Carolina", "state": "NC", "kind": "none live",
     "party": True, "hourly_history": False,
     "url": "https://dl.ncsbe.gov/?prefix=ENRS/",
     "notes": "NCSBE absentee/one-stop files are daily and pre-ED only; ED check-ins are not "
              "published live. ED voter history (with party) arrives in the voter history file weeks later."},
    {"jurisdiction": "Georgia", "state": "GA", "kind": "manual, totals only",
     "party": False, "hourly_history": False,
     "url": "https://sos.ga.gov/",
     "notes": "SOS (Gabriel Sterling/Raffensperger posts) gives statewide ED check-in totals "
              "intraday; GA has no party registration."},
    {"jurisdiction": "Pennsylvania counties", "state": "PA", "kind": "none",
     "party": False, "hourly_history": False, "url": "",
     "notes": "Poll books are not reported centrally intraday; no party split published."},
    {"jurisdiction": "Kentucky", "state": "KY", "kind": "none live",
     "party": True, "hourly_history": False, "url": "https://elect.ky.gov/",
     "notes": "SOS posts early/absentee by party before ED; no intraday ED check-in feed."},
]


# ------------------------------------------------------------------ TQV helpers
def _tz(cty: str) -> ZoneInfo:
    return ZoneInfo("America/Chicago" if cty in FL_CENTRAL else "America/New_York")


def tqv_index(cty: str) -> list[int]:
    url = common.bust(TQV_BASE.format(cty=cty) + "index.json", "live", every=60)
    return [int(x) for x in common.http_get_json(url, timeout=15, retries=1, memo_seconds=60)]


def _ed_total(row: dict) -> int:
    return int(row["rep"]) + int(row["dem"]) + int(row["oth"])


def tqv_data(cty: str, eid: int, live: bool = True, fresh: bool = False) -> dict:
    url = TQV_BASE.format(cty=cty) + f"{eid}/data.json"
    if fresh:
        return common.http_get_json(url, timeout=30, retries=1)
    if live:
        return common.http_get_json(common.bust(url, "live", every=60), timeout=30, retries=1,
                                    memo_seconds=60)
    return common.http_get_json(url, timeout=30, retries=1, cache_name=f"checkins/FL_{cty}_{eid}.json",
                                cache_max_age=float("inf"))


def _parse_utc(s: str) -> _dt.datetime:
    # '2026-11-03T19:00:24.1782891+00:00' (7-digit fraction: trim to microseconds)
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.strip()).replace("Z", "+00:00")
    t = _dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


def tqv_row(cty: str, data: dict, hour_local: str | None = None) -> dict:
    """One CSV-shaped row: ED in-person by registration party from a TQV data.json."""
    summ = data.get("Summary") or {}
    parties = (data.get("Turnout") or {}).get("PartyType") or {}
    rep = dem = oth = 0
    for p, by_type in parties.items():
        n = common.to_int((by_type or {}).get("ElectionDay"))
        if p == "REP":
            rep += n
        elif p == "DEM":
            dem += n
        else:
            oth += n
    ts = _parse_utc(summ["LastUpdatedTime"]).astimezone(_tz(cty))
    date = summ.get("ElectionDate") or ""
    year = int(date[-4:]) if date[-4:].isdigit() else ts.year
    return {
        "jurisdiction": f"{FL_COUNTIES.get(cty, cty)} County", "state": "FL", "year": year,
        "hour_local": hour_local or ts.strftime("%H:%M"),
        "rep": rep, "dem": dem, "oth": oth,
        "source": TQV_SOURCE.format(name=FL_COUNTIES.get(cty, cty)),
        "as_of": ts.isoformat(timespec="minutes"),
    }


# ------------------------------------------------------------------ Washoe County, NV
WASHOE_PAGE = "https://www.washoecounty.gov/voters/results/2026data.php"
WASHOE_BASE = "https://www.washoecounty.gov/voters/results/"
WASHOE_SOURCE = "Washoe County Registrar of Voters in-person turnout report ({label})"


def _washoe_reports() -> list[tuple[str, str]]:
    """[(hour_local, url)] for the 2026 general's Election Day in-person reports. The page
    labels them 'Download In-Person 9:00am Report' / '... Final Report' (2026 primary pattern:
    9am, 12pm, 3pm, Final); pre-ED daily files carry no time label and are skipped."""
    page = common.decode(common.http_get(WASHOE_PAGE, timeout=20, retries=1, memo_seconds=120))
    out = []
    for href, label in re.findall(r'<a[^>]+href="([^"]*In-Person[^"]*)"[^>]*>(.*?)</a>', page, flags=re.S):
        label = re.sub(r"<[^>]+>|\s+", " ", label).strip()
        if not href.startswith("resultsfiles/"):  # 'datafiles/' = 2026 primary
            continue
        m = re.search(r"(\d{1,2}):(\d{2})\s*([ap])m", label, re.I)
        if m:
            h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
            out.append((f"{h:02d}:{m.group(2)}", WASHOE_BASE + href))
        elif re.search(r"\bfinal\b", label, re.I):
            out.append(("final", WASHOE_BASE + href))
    return out


def washoe_rows(day: str = "11/3/2026") -> list[dict]:
    """ED in-person voters by registration party from each posted Washoe ED report. The files
    are voter-level public lists (names); only party counts are kept. Posted reports don't
    change, so each is cached on disk once downloaded."""
    rows = []
    for hour, url in _washoe_reports():
        name = url.rsplit("/", 1)[-1]
        raw = common.decode(common.http_get(url, timeout=60, retries=1,
                                            cache_name=f"checkins/washoe_2026_{hour.replace(':', '')}_{name}",
                                            cache_max_age=None if hour == "final" else float("inf")))
        rep = dem = oth = 0
        for r in csv.DictReader(io.StringIO(raw)):
            if (r.get("DATE_VOTED") or "").split(" ")[0] != day:
                continue
            p = (r.get("VOTER_REG_PARTY") or "").strip().upper()
            if p == "REP":
                rep += 1
            elif p == "DEM":
                dem += 1
            else:
                oth += 1
        if rep + dem + oth:
            rows.append({"jurisdiction": "Washoe County", "state": "NV", "year": 2026, "hour_local": hour,
                         "rep": rep, "dem": dem, "oth": oth,
                         "source": WASHOE_SOURCE.format(label=name),
                         "as_of": f"2026-11-03T{hour}-08:00" if hour != "final" else "2026-11-03"})
    return rows


# ------------------------------------------------------------------ public API
def fetch(counties: list[str] | None = None, workers: int = 8) -> list[dict]:
    """Current 2026 cumulative ED in-person turnout by party, one row per FL county (live TQV).

    hour_local is the county-local clock time of the snapshot (Summary.LastUpdatedTime), so
    rows collected by repeated polling form the same-hour series. Counties whose feed fails or
    whose current election is not the 2026 general are skipped (errors are printed to stderr).
    """
    codes = [c for c in counties if c in FL_COUNTIES] if counties else list(FL_COUNTIES)

    def one(cty: str) -> dict | None:
        try:
            ids = tqv_index(cty)
            for eid in sorted(ids, reverse=True):
                d = tqv_data(cty, eid)
                s = d.get("Summary") or {}
                if s.get("ElectionDate") == "11/03/2026" and "General" in (s.get("ElectionName") or ""):
                    row = tqv_row(cty, d)
                    # CloudFront edges sometimes serve a copy hours old (query strings are not part
                    # of the cache key): refetch a couple of times and keep the newest snapshot.
                    for _ in range(2):
                        if row["as_of"].startswith(ELECTION_DAY) and _ed_total(row):
                            break
                        time.sleep(1.5)
                        alt = tqv_row(cty, tqv_data(cty, eid, fresh=True))
                        if alt["as_of"] > row["as_of"] or _ed_total(alt) > _ed_total(row):
                            row = alt
                    if not _ed_total(row):
                        print(f"checkins: FL {cty}: no Election Day turnout in TQV "
                              f"(as of {row['as_of']}); county may not upload ED to TQV", file=sys.stderr)
                        return None
                    return row
            print(f"checkins: FL {cty}: no 2026 general in index {ids}", file=sys.stderr)
        except (common.FeedError, KeyError, ValueError, TypeError) as e:
            print(f"checkins: FL {cty}: {e}", file=sys.stderr)
        return None

    with ThreadPoolExecutor(workers) as ex:
        rows = [r for r in ex.map(one, codes) if r]
    if counties is None or "WASHOE" in [c.upper() for c in counties]:
        try:
            rows += washoe_rows()
        except (common.FeedError, csv.Error) as e:
            print(f"checkins: NV Washoe: {e}", file=sys.stderr)
    return rows


def find_history(cty: str, years=(2022, 2024), max_back: int = 120) -> list[dict]:
    """Final ED in-person-by-party rows (hour_local='final') for the November general elections
    in `years`, found by walking election ids down from the county's current one."""
    want = {y: None for y in years}
    top = max(tqv_index(cty))
    out = []
    for eid in range(top - 1, max(top - max_back, 0), -1):
        try:
            d = tqv_data(cty, eid, live=False)
        except common.FeedError:
            continue
        s = d.get("Summary") or {}
        date, name = s.get("ElectionDate") or "", s.get("ElectionName") or ""
        y = int(date[-4:]) if date[-4:].isdigit() else None
        if y in want and want[y] is None and date.startswith("11/") and "General" in name \
                and "Runoff" not in name and "Municipal" not in name:
            want[y] = eid
            row = tqv_row(cty, d, hour_local="final")
            if _ed_total(row):  # some counties never uploaded ED to TQV (e.g. Seminole 2022)
                out.append(row)
            else:
                print(f"checkins: FL {cty} {y}: TQV id {eid} has no Election Day turnout", file=sys.stderr)
        if all(want.values()):  # ids are not chronological (municipal elections interleave)
            break
    missing = [y for y, e in want.items() if e is None]
    if missing:
        print(f"checkins: FL {cty}: no TQV general election found for {missing} "
              f"within {max_back} ids below {top}", file=sys.stderr)
    return out


def history(counties: list[str] | None = None, workers: int = 8) -> list[dict]:
    codes = counties or list(FL_COUNTIES)

    def one(cty):
        try:
            return find_history(cty)
        except common.FeedError as e:
            print(f"checkins: FL {cty} history: {e}", file=sys.stderr)
            return []

    with ThreadPoolExecutor(workers) as ex:
        return [r for rows in ex.map(one, codes) for r in rows]


def load_csv(path: Path = CSV_PATH) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def merge_into_csv(rows: list[dict], path: Path = CSV_PATH) -> int:
    """Add rows to data/checkins.csv, replacing rows with the same (jurisdiction, year, hour_local)."""
    have = load_csv(path) if path.exists() else []
    key = lambda r: (r["jurisdiction"], r["state"], str(r["year"]), r["hour_local"])
    new = {key(r): r for r in rows}
    kept = [r for r in have if key(r) not in new]
    allrows = kept + list(new.values())
    allrows.sort(key=lambda r: (r["state"], r["jurisdiction"], str(r["year"]),
                                "99" if r["hour_local"] == "final" else r["hour_local"]))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(allrows)
    return len(new)


def print_rows(rows: list[dict]) -> None:
    if not rows:
        print("(no rows)")
        return
    hdr = f"{'jurisdiction':<26}{'st':<4}{'year':<6}{'hour':<7}{'rep':>9}{'dem':>9}{'oth':>9}{'R-D %':>8}  as_of"
    print(hdr)
    print("-" * len(hdr))
    R = D = O = 0
    for r in sorted(rows, key=lambda r: -(int(r["rep"]) + int(r["dem"]) + int(r["oth"]))):
        rep, dem, oth = int(r["rep"]), int(r["dem"]), int(r["oth"])
        R, D, O = R + rep, D + dem, O + oth
        t = rep + dem + oth
        m = (rep - dem) / t * 100 if t else 0.0
        print(f"{r['jurisdiction'][:25]:<26}{r['state']:<4}{r['year']!s:<6}{r['hour_local']:<7}"
              f"{rep:>9,}{dem:>9,}{oth:>9,}{m:>+8.1f}  {r['as_of']}")
    T = R + D + O
    print("-" * len(hdr))
    print(f"{'TOTAL (' + str(len(rows)) + ' rows)':<43}{R:>9,}{D:>9,}{O:>9,}"
          f"{((R - D) / T * 100 if T else 0):>+8.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live ED in-person turnout by party (FL via VR Systems TQV)")
    ap.add_argument("--county", action="append", help="FVRS code(s), e.g. DAD, HIL (default: all 67)")
    ap.add_argument("--save", action="store_true", help="append/replace these rows in data/checkins.csv")
    ap.add_argument("--history", action="store_true",
                    help="fetch 2022/2024 general-election FINAL ED rows instead of live 2026")
    ap.add_argument("--sources", action="store_true", help="print SOURCES and exit")
    a = ap.parse_args()
    if a.sources:
        print(json.dumps(SOURCES, indent=1))
        return
    codes = [c.upper() for c in a.county] if a.county else None
    rows = history(codes) if a.history else fetch(codes)
    print_rows(rows)
    if a.save and rows:
        print(f"saved {merge_into_csv(rows)} rows -> {CSV_PATH}")


if __name__ == "__main__":
    main()
