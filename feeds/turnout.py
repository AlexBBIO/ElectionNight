"""Pre-Election-Day turnout (early in-person + mail ballots returned) by county, 2026 vs 2022.

    fetch(state) -> [{"state","fips","county","pre_ballots","pre_dem","pre_rep","pre_oth",
                      "eday_checkins","as_of"}]           (None where the source has no figure)
    python3 -m election_night.feeds.turnout --state FL [--all]

Party columns are voter *registration* party (not vote choice) and are only filled where the
state registers by party (FL, NC, KY). pre_oth = every other registration (NPA/UNA/minor).
Downloads are cached under election_night/cache/turnout/ and re-fetched at most every
CACHE_SECONDS. If the live source fails, fetch() falls back to data/turnout_2026_pre_snapshot.csv.

Sources (2026):
  FL  Division of Elections statistics page (county x party, "Voted Vote-by-Mail" + "Voted Early"),
      https://countyfilesvbm-ev.floridados.gov/VoteByMailEarlyVotingReports/PublicStats
      (refreshed 9am/noon/3pm/6pm ET from the counties' daily files; counts through the prior day.
      The Stats_<n>_*.txt "Download File" links on that page lag the HTML by days - not used.)
  NC  NCSBE absentee_counts_county_<date>.csv (accepted one-stop + mail ballots, county x party),
      https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/2026_11_03/ (rebuilt daily ~4:40am ET).
  KY  State Board of Elections "2026 General Election Current Voter Turnout" xlsx linked from
      https://elect.ky.gov/ (Absentee_Public_MMDDYY.xlsx; county x D/R; unofficial).
  SC  SC Election Commission EV-Totals-MM-DD-YY.csv (early in-person by county; no mail, no party).
  GA, VA, OH, IN: no county-level 2026 source wired up (GA SOS data hub/absentee file sit behind a
      Cloudflare challenge -> HTTP 403; VA ENR has statewide absentee statistics only at the top level).

2022 baseline: data/turnout_2022_pre.csv (see BASELINE_NOTES).
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import html
import io
import re
from pathlib import Path

from . import common

CACHE_SECONDS = 600.0
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BASELINE_CSV = DATA_DIR / "turnout_2022_pre.csv"
BASELINE_2024_CSV = DATA_DIR / "turnout_2024_pre.csv"
SNAPSHOT_CSV = DATA_DIR / "turnout_2026_pre_snapshot.csv"
FIELDS = ["state", "fips", "county", "pre_ballots", "pre_dem", "pre_rep", "pre_oth",
          "eday_checkins", "as_of"]

FL_STATS_URL = "https://countyfilesvbm-ev.floridados.gov/VoteByMailEarlyVotingReports/PublicStats"
NC_BUCKET = "https://s3.amazonaws.com/dl.ncsbe.gov"
NC_ELECTION = "2026_11_03"

BASELINE_NOTES = {
    "FL": "FL DOS archived EV/VBM status-count report, 2022 General (election 26906), 'Voted Vote-by-Mail' "
          "+ 'Voted Early' by county x party (https://files.floridados.gov/media/706191/"
          "2022-ge-rptstatuscountsarchive-ev-vbm.pdf). Final archive: mail includes ballots dropped off "
          "on Election Day (2026 live figures are through the day before ED).",
    "NC": "NCSBE absentee_20221108.zip, ballots ACCEPTED/ACCEPTED - CURED with return date before 11/08/2022 "
          "(one-stop + mail), by county x voter party. Excludes 28k mail ballots received after ED.",
    "GA": "2022 official results by vote method (Governor: Advanced + Absentee votes) via data/county_2022.csv "
          "(MEDSL precinct files) - votes in the race, not ballots; no party registration.",
    "SC": "2022 official results by vote method (Governor: Early + Absentee votes) via data/county_2022.csv - "
          "votes, not ballots. NB the 2026 live SC file is early in-person only (no mail).",
    "KY": "KY SBE https://elect.ky.gov/Documents/Absentee_Public_110422.xlsx (mail returned + excused & "
          "no-excuse in-person + FPCA; D/R from the file, oth = total - D - R). Total 337,975 = SBE's final "
          "2022 pre-ED figure. Marked unofficial by SBE.",
}


BASELINE_2024_NOTES = {
    "FL": "FL DOS EV/VBM archive report, 2024 General (election 43888), https://files.floridados.gov/media/708588/"
          "2024-general-election-43888.pdf; Voted VBM + Voted Early by county x party (statewide NPA cell "
          "overflowed as #### in the PDF -> total - R - D - other). Final archive: mail may include ED drop-offs.",
    "NC": "NCSBE absentee_20241105.zip, ACCEPTED/ACCEPTED - CURED returned before 11/05/2024 (early + mail); "
          "excludes 25k ballots returned on/after ED.",
    "KY": "KY SBE https://elect.ky.gov/Documents/Absentee_Public_110524.xlsx (ED-morning file: mail returned "
          "through Nov 4). The widely quoted 792,476 is the 11/03 file (before the last mail day).",
    "GA": "2024 official results (GA SOS Enhanced Voting, President): advance in-person + absentee-by-mail votes "
          "by county via feeds/ga.py - votes, not ballots; no party registration.",
    "SC": "2024 official results (SC Clarity detail XML, election 122436, President): Early Voting + Absentee By "
          "Mail votes by county - votes, not ballots.",
}

# Short source labels for the dashboard (2026 live data): name, link, and "data through".
SOURCES = {
    "FL": {"name": "Florida Division of Elections",
           "url": FL_STATS_URL, "through": "Nov 2"},
    "NC": {"name": "NC State Board of Elections",
           "url": f"{NC_BUCKET}/index.html?prefix=ENRS/{NC_ELECTION}/", "through": "Nov 2"},
    "KY": {"name": "KY State Board of Elections",
           "url": "https://elect.ky.gov/Documents/Absentee_Public_103126.xlsx", "through": "Oct 31"},
    "SC": {"name": "SC Election Commission",
           "url": "https://scvotes.gov/wp-content/uploads/2026/11/EV-Totals-10-31-26.csv",
           "through": "Oct 31 (in-person only)"},
}


def _get(url: str, name: str, max_age: float = CACHE_SECONDS) -> bytes:
    return common.http_get(url, timeout=60, retries=2, cache_name=f"turnout/{name}", cache_max_age=max_age)


def _row(state: str, county: str, fips: str | None = None, total=None, dem=None, rep=None, oth=None,
         eday=None, as_of=None) -> dict:
    fips = fips or common.county_fips(state, county)
    return {"state": state, "fips": fips, "county": common.county_name(fips, county),
            "pre_ballots": total, "pre_dem": dem, "pre_rep": rep, "pre_oth": oth,
            "eday_checkins": eday, "as_of": as_of}


# ------------------------------------------------------------------ FL
def _fl() -> list[dict]:
    t = common.decode(_get(FL_STATS_URL, "fl_publicstats.html"))
    acc: dict[str, list[int]] = {}
    compiled: list[_dt.datetime] = []
    for div in ("county_abl", "county_evs"):  # Voted Vote-by-Mail, Voted Early (county tables)
        i = t.find(f'id="{div}"')
        if i < 0:
            raise common.FeedError(f"FL stats page: no #{div} table ({FL_STATS_URL})")
        j = t.find('<div id="', i + 10)
        seg = t[i: j if j > 0 else len(t)]
        n = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
            cells = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split())
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            if len(cells) < 7 or cells[0] in ("County", "") or not re.match(r"[\d,]+$", cells[6]):
                continue
            rep, dem, oth, npa, tot = (common.to_int(x) for x in cells[2:7])
            a = acc.setdefault(cells[0], [0, 0, 0, 0])
            for k, v in enumerate((tot, dem, rep, oth + npa)):
                a[k] += v
            n += 1
            if len(cells) > 7:
                try:
                    compiled.append(_dt.datetime.strptime(cells[7], "%m/%d/%Y %I:%M%p"))
                except ValueError:
                    pass
        if n < 60:
            raise common.FeedError(f"FL stats page: only {n} county rows in #{div}")
    as_of = (max(compiled).strftime("%Y-%m-%d %H:%M ET (DOS compile; counts through prior day)")
             if compiled else None)
    return [_row("FL", c, total=a[0], dem=a[1], rep=a[2], oth=a[3], as_of=as_of) for c, a in acc.items()]


# ------------------------------------------------------------------ NC
def _nc() -> list[dict]:
    listing = common.decode(_get(f"{NC_BUCKET}/?prefix=ENRS/{NC_ELECTION}/absentee_counts_county_",
                                 "nc_listing.xml"))
    keys = re.findall(r"<Key>(.*?)</Key><LastModified>(.*?)</LastModified>", listing)
    if not keys:
        raise common.FeedError(f"NCSBE: no absentee_counts_county file under ENRS/{NC_ELECTION}/")
    key, modified = max(keys, key=lambda k: k[1])
    raw = _get(f"{NC_BUCKET}/{key}", "nc_" + key.rsplit("/", 1)[-1])
    r = csv.reader(io.StringIO(common.decode(raw)))
    hdr = next(r)
    ci, pi, ni = hdr.index("county_name"), hdr.index("party_cd"), len(hdr) - 1
    acc: dict[str, list[int]] = {}
    for row in r:
        if len(row) <= ni:
            continue
        n = common.to_int(row[ni])
        if not n:
            continue
        a = acc.setdefault(row[ci].strip(), [0, 0, 0, 0])
        k = {"DEM": 1, "REP": 2}.get(row[pi].strip(), 3)
        a[0] += n
        a[k] += n
    stamp = _dt.datetime.fromisoformat(modified.replace("Z", "+00:00"))
    as_of = f"{key.rsplit('/', 1)[-1]} posted {stamp:%Y-%m-%d %H:%M}Z (counts through prior day)"
    return [_row("NC", c, total=a[0], dem=a[1], rep=a[2], oth=a[3], as_of=as_of) for c, a in acc.items()]


# ------------------------------------------------------------------ KY
KY_HOME = "https://elect.ky.gov/"


def xlsx_rows(data: bytes, sheet: str = "xl/worksheets/sheet1.xml") -> list[dict[str, str]]:
    """Minimal stdlib .xlsx reader: [{column letter: cell text}] for one sheet."""
    import xml.etree.ElementTree as ET
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(data))
    m = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        ss = ["".join(t.text or "" for t in si.iter(m + "t"))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(m + "si")]
    out = []
    for row in ET.fromstring(z.read(sheet)).iter(m + "row"):
        r = {}
        for c in row.iter(m + "c"):
            col = re.match(r"[A-Z]+", c.get("r", "")).group(0)
            v = c.find(m + "v")
            val = v.text if v is not None else "".join(x.text or "" for x in c.iter(m + "t"))
            if c.get("t") == "s" and val:
                val = ss[int(val)]
            r[col] = (val or "").strip()
        out.append(r)
    return out


def ky_parse(data: bytes) -> dict[str, list[int]]:
    """KY SBE 'Absentee_Public_MMDDYY.xlsx' -> {COUNTY: [total, dem, rep, oth]}.
    Columns located by header text: mail RETURNED + excused in-person + no-excuse in-person
    (+ FPCA returned, party unknown -> oth); total = 'UNOFFICIAL Total Absentee'."""
    rows = xlsx_rows(data)
    hi = next(i for i, r in enumerate(rows) if r.get("A", "").lower() == "county")
    h = {v.lower(): k for k, v in rows[hi].items() if v}

    def col(*names):
        for n in names:
            if n.lower() in h:
                return h[n.lower()]
        raise common.FeedError(f"KY absentee xlsx: no column {names[0]!r}")
    dc = [col("DEM Ballots RETURNED"), col("DEM Excused In-person"), col("DEM No Excuse In-person")]
    rc = [col("REP Ballots RETURNED"), col("REP Excused In-person"), col("REP No Excuse In-person")]
    tc = col("UNOFFICIAL Total Absentee")
    out = {}
    for r in rows[hi + 1:]:
        name = r.get("A", "")
        if not name or name.upper().startswith("TOTAL") or not r.get(tc):
            continue
        t = common.to_int(r[tc])
        d = sum(common.to_int(r.get(c)) for c in dc)
        rp = sum(common.to_int(r.get(c)) for c in rc)
        out[name] = [t, d, rp, t - d - rp]
    return out


def _ky() -> list[dict]:
    home = common.decode(_get(KY_HOME, "ky_home.html"))
    links = re.findall(r'href="([^"]*Absentee_Public_(\d{6})\.xlsx)"', home, re.I)
    if not links:
        raise common.FeedError(f"KY: no Absentee_Public_*.xlsx link on {KY_HOME}")
    href, mmddyy = max(links, key=lambda l: (l[1][4:], l[1][:4]))
    url = href if href.startswith("http") else KY_HOME.rstrip("/") + "/" + href.lstrip("/")
    acc = ky_parse(_get(url, f"ky_Absentee_Public_{mmddyy}.xlsx"))
    as_of = f"KY SBE {url.rsplit('/', 1)[-1]} (unofficial; file dated {mmddyy[:2]}/{mmddyy[2:4]}/20{mmddyy[4:]})"
    return [_row("KY", c, total=a[0], dem=a[1], rep=a[2], oth=a[3], as_of=as_of) for c, a in acc.items()]


# ------------------------------------------------------------------ SC
SC_UPLOADS = "https://scvotes.gov/wp-content/uploads"


def _sc() -> list[dict]:
    """SC Election Commission 'EV-Totals-MM-DD-YY.csv' (early in-person by county x site x day).
    Newest file found by probing the last 10 days in the WordPress uploads folders.
    In-person early voting only: returned mail absentee ballots (~42k) are NOT in this file."""
    today = _dt.date.today()
    raw = url = None
    for back in range(10):
        d = today - _dt.timedelta(days=back)
        for folder in {d.strftime("%Y/%m"), (d + _dt.timedelta(days=3)).strftime("%Y/%m")}:
            u = f"{SC_UPLOADS}/{folder}/EV-Totals-{d:%m-%d-%y}.csv"
            try:
                raw = _get(u, f"sc_EV-Totals-{d:%m-%d-%y}.csv", max_age=float("inf") if back else CACHE_SECONDS)
                url = u
                break
            except common.FeedError:
                continue
        if raw:
            break
    if not raw:
        raise common.FeedError(f"SC: no EV-Totals-*.csv under {SC_UPLOADS} in the last 10 days")
    rows = list(csv.reader(io.StringIO(common.decode(raw))))
    hi = next(i for i, r in enumerate(rows[:20]) if r and r[0].strip() == "Jurisdiction")
    ti = [c.strip() for c in rows[hi]].index("Total")
    report = rows[1][1].strip() if len(rows) > 1 and len(rows[1]) > 1 else "?"
    out = []
    for r in rows[hi + 1:]:
        if len(r) > ti and r[0].strip() and r[0].strip() != "South Carolina" and r[1].strip():
            out.append(_row("SC", r[0].strip(), total=common.to_int(r[ti]),
                            as_of=f"SC EV-Totals report date {report} (in-person early only; {url})"))
    return out


# ------------------------------------------------------------------ registry / fallback
_LIVE = {"FL": _fl, "NC": _nc, "KY": _ky, "SC": _sc}


def _opt(x):
    return common.opt_int(x)


def load_snapshot(state: str) -> list[dict]:
    if not SNAPSHOT_CSV.exists():
        return []
    out = []
    with SNAPSHOT_CSV.open(newline="") as fh:
        for r in csv.DictReader(fh):
            if r["state"] != state:
                continue
            out.append({"state": state, "fips": r["fips"], "county": r["county"],
                        **{k: _opt(r.get(k)) for k in ("pre_ballots", "pre_dem", "pre_rep", "pre_oth",
                                                         "eday_checkins")},
                        "as_of": (r.get("as_of") or None)})
    return out


# States with county-level 2026 data (live adapter, snapshot fallback). GA, VA, OH and IN are not
# supported: no reachable official county-level file (see module docstring).
STATES = ["FL", "NC", "KY", "SC"]


def fetch(state: str) -> list[dict]:
    """Latest 2026 pre-Election-Day ballots by county. Live where an adapter exists; on failure (or
    for states without one) the committed snapshot. Raises FeedError if neither has the state."""
    state = state.upper()
    err = None
    if state in _LIVE:
        try:
            rows = _LIVE[state]()
            return sorted(rows, key=lambda r: r["fips"])
        except (common.FeedError, KeyError, ValueError) as e:
            err = e
    rows = load_snapshot(state)
    if rows:
        if err is not None:
            for r in rows:
                r["as_of"] = f"{r['as_of']} [snapshot; live failed: {err}]"
        return sorted(rows, key=lambda r: r["fips"])
    raise common.FeedError(f"turnout {state}: " + (str(err) if err else "no live source or snapshot"))


def baseline(state: str, year: int = 2022) -> dict[str, dict]:
    """{fips: row} from data/turnout_<year>_pre.csv (2022 or 2024); {} if the file/state is missing."""
    path = BASELINE_CSV if year == 2022 else DATA_DIR / f"turnout_{year}_pre.csv"
    if not path.exists():
        return {}
    out = {}
    with path.open(newline="") as fh:
        for r in csv.DictReader(fh):
            if r["state"] == state.upper():
                out[r["fips"]] = {k: (_opt(r[k]) if k.startswith("pre_") else r[k]) for k in r}
    return out


def baseline_2022(state: str) -> dict[str, dict]:
    return baseline(state, 2022)


# ------------------------------------------------------------------ CLI
def _split(d, r, o, t) -> str:
    if d is None or r is None or not t:
        return "D/R/oth n/a"
    return f"D {d:,} ({d / t:.1%}) R {r:,} ({r / t:.1%}) oth {o or 0:,} ({(o or 0) / t:.1%})  R-D {(r - d) / t * 100:+.1f}pt"


def main() -> None:
    ap = argparse.ArgumentParser(description="2026 pre-Election-Day ballots by county vs 2022")
    ap.add_argument("--state", default="FL")
    ap.add_argument("--all", action="store_true", help="print every county")
    a = ap.parse_args()
    st = a.state.upper()
    try:
        rows = fetch(st)
    except common.FeedError as e:
        raise SystemExit(str(e))
    base = baseline(st, 2022)
    base24 = baseline(st, 2024)
    tot = lambda rs, k: sum((r.get(k) or 0) for r in rs) if any(r.get(k) is not None for r in rs) else None
    T, D, R, O = (tot(rows, k) for k in ("pre_ballots", "pre_dem", "pre_rep", "pre_oth"))
    E = tot(rows, "eday_checkins")
    print(f"{st} 2026 pre-ED ballots: {T or 0:,} in {len(rows)} counties   as of: {rows[0]['as_of']}")
    print("  " + _split(D, R, O, T))
    if E is not None:
        print(f"  Election Day check-ins so far: {E:,}")
    if base:
        bl = list(base.values())
        T2, D2, R2, O2 = (tot(bl, k) for k in ("pre_ballots", "pre_dem", "pre_rep", "pre_oth"))
        print(f"{st} 2022 pre-ED ballots: {T2 or 0:,}   ({BASELINE_NOTES.get(st, 'see data/turnout_2022_pre.csv')})")
        print("  " + _split(D2, R2, O2, T2))
        if T and T2:
            print(f"  2026 / 2022 = {T / T2:.3f}")
            for k, n1, n2 in (("D", D, D2), ("R", R, R2), ("oth", O, O2)):
                if n1 is not None and n2:
                    print(f"     {k:>3}: {n1 / n2:.3f}")
    if base24:
        bl = list(base24.values())
        T4, D4, R4, O4 = (tot(bl, k) for k in ("pre_ballots", "pre_dem", "pre_rep", "pre_oth"))
        print(f"{st} 2024 pre-ED ballots: {T4 or 0:,}   ({BASELINE_2024_NOTES.get(st, 'see data/turnout_2024_pre.csv')})")
        print("  " + _split(D4, R4, O4, T4))
        if T and T4:
            print(f"  2026 / 2024 = {T / T4:.3f}")
    hdr =f"{'county':<20}{'fips':>6}{'2026 pre':>11}{'2022 pre':>11}{'ratio':>7}{'D%':>7}{'R%':>7}{'D% 22':>7}{'R% 22':>7}"
    print(hdr)
    print("-" * len(hdr))
    rows_sorted = sorted(rows, key=lambda r: -(r["pre_ballots"] or 0))
    for r in rows_sorted if a.all else rows_sorted[:20]:
        b = base.get(r["fips"], {})
        t1, t2 = r["pre_ballots"] or 0, b.get("pre_ballots") or 0
        pct = lambda n, t: f"{n / t * 100:7.1f}" if n is not None and t else f"{'':>7}"
        print(f"{r['county'][:19]:<20}{r['fips']:>6}{t1:>11,}{t2:>11,}{(t1 / t2 if t2 else 0):>7.2f}"
              f"{pct(r['pre_dem'], t1)}{pct(r['pre_rep'], t1)}{pct(b.get('pre_dem'), t2)}{pct(b.get('pre_rep'), t2)}")
    if not a.all and len(rows) > 20:
        print(f"... ({len(rows) - 20} more; --all)")


if __name__ == "__main__":
    main()
