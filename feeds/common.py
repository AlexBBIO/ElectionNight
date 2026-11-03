"""Shared helpers for the state live-results adapters (stdlib only).

- http_get():      urllib GET with User-Agent, timeout, retries/backoff, transparent gzip,
                   optional short in-memory memo (so two races polled at the same moment
                   share one download) and optional disk cache (archived elections only).
- county_fips():   county / county-equivalent name -> 5-digit FIPS, from the Census 2020
                   national county file (downloaded once, cached under election_night/cache/).
- pick_contests(): contest matching shared by all adapters (case-insensitive substring after
                   normalising "U.S."/"United States"/"Senator"/... spellings).
- build_rows():    turns {county: {candidate: votes}} into the adapter output dicts.
- main():          tiny CLI used by each module's `python3 -m election_night.feeds.xx`.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "factor-watch-election-night/0.1"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CENSUS_COUNTY_URL = "https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt"
_PROXY_CA = "/root/.ccr/ca-bundle.crt"

DEM_PARTIES = {"DEM", "D", "DEMOCRAT", "DEMOCRATIC", "DEMOCRATIC PARTY"}
REP_PARTIES = {"REP", "R", "REPUBLICAN", "REPUBLICAN PARTY", "GOP"}


# ------------------------------------------------------------------ HTTP
class FeedError(RuntimeError):
    """Feed unreachable / unexpected (message says which URL and why)."""


_ssl_ctx: ssl.SSLContext | None = None


def _context(force_proxy_ca: bool = False) -> ssl.SSLContext:
    global _ssl_ctx
    if force_proxy_ca and os.path.exists(_PROXY_CA):
        _ssl_ctx = ssl.create_default_context(cafile=_PROXY_CA)
    elif _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


_memo: dict[str, tuple[float, bytes]] = {}
_memo_lock = threading.Lock()
_url_locks: dict[str, threading.Lock] = {}


def _download(url: str, timeout: float, retries: int, backoff: float) -> bytes:
    last: Exception | None = None
    tried_proxy_ca = False
    attempt = 0
    while attempt <= retries:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Cache-Control": "no-cache",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_context()) as resp:
                data = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
            # Some hosts (Clarity) gzip even when not asked; detect by magic as well.
            if enc == "gzip" or data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except OSError:
                    if enc == "gzip":
                        raise
            return data
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 404, 410):  # don't hammer: not there / not allowed
                raise FeedError(f"HTTP {e.code} for {url}") from e
        except ssl.SSLCertVerificationError as e:
            last = e
            if not tried_proxy_ca and os.path.exists(_PROXY_CA):
                tried_proxy_ca = True
                _context(force_proxy_ca=True)
                continue  # retry immediately with the proxy CA bundle, not counted
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
        attempt += 1
        if attempt <= retries:
            time.sleep(backoff * attempt)
    raise FeedError(f"GET {url} failed after {retries + 1} tries: {last}")


def http_get(url: str, *, timeout: float = 20.0, retries: int = 2, backoff: float = 1.5,
             memo_seconds: float = 0.0, cache_name: str | None = None,
             cache_max_age: float | None = None) -> bytes:
    """GET `url` and return the (gunzipped) body.

    memo_seconds  reuse an in-memory copy younger than this (concurrent race polls share it).
    cache_name    path under election_night/cache/ to keep a copy of the body. It is only
                  *read back* when cache_max_age is given and the file is younger than that
                  (None = never read back; use float('inf') for archived, immutable data).
    """
    if memo_seconds:
        with _memo_lock:
            lock = _url_locks.setdefault(url, threading.Lock())
        with lock:
            hit = _memo.get(url)
            if hit and time.time() - hit[0] < memo_seconds:
                return hit[1]
            data = http_get(url, timeout=timeout, retries=retries, backoff=backoff,
                            cache_name=cache_name, cache_max_age=cache_max_age)
            _memo[url] = (time.time(), data)
            return data

    path = CACHE_DIR / cache_name if cache_name else None
    if path is not None and cache_max_age is not None and path.exists():
        if time.time() - path.stat().st_mtime < cache_max_age:
            return path.read_bytes()
    data = _download(url, timeout, retries, backoff)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError:
            pass
    return data


def http_get_json(url: str, **kw):
    """http_get + json.loads; on a parse error (file caught mid-publish) refetch once, fresh."""
    import json
    try:
        return json.loads(decode(http_get(url, **kw)) or "null")
    except json.JSONDecodeError:
        time.sleep(1.0)
        kw = {k: v for k, v in kw.items() if k not in ("memo_seconds", "cache_max_age")}
        with _memo_lock:
            _memo.pop(url, None)
        try:
            return json.loads(decode(http_get(url, **kw)) or "null")
        except json.JSONDecodeError as e:
            raise FeedError(f"bad JSON from {url}: {e}") from e


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def is_archived(election: str, days: int = 45) -> bool:
    """True when `election` (YYYY-MM-DD) is old enough that its results no longer change,
    so downloads can be cached on disk indefinitely. Non-date election ids -> False."""
    try:
        d = _dt.date.fromisoformat(election)
    except ValueError:
        return False
    return (_dt.date.today() - d).days > days


def cache_policy(election: str) -> float | None:
    """cache_max_age to pass to http_get for this election's result files."""
    return float("inf") if is_archived(election) else None


def bust(url: str, election: str, every: int = 10) -> str:
    """Append a cache-busting query param for live elections (CDN-fronted feeds)."""
    if is_archived(election):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}v={int(time.time()) // every}"


# ------------------------------------------------------------------ county FIPS
_fips_lock = threading.Lock()
_fips_by_name: dict[tuple[str, str], str] | None = None
_name_by_fips: dict[str, str] = {}


def norm_county(name: str) -> str:
    """'St. Johns County' -> 'st johns'; 'Alexandria city' / 'ALEXANDRIA CITY' -> 'alexandria city';
    'DeSoto' -> 'desoto'; 'Miami-Dade' -> 'miami dade'. Keeps 'city' (VA independent cities) but
    drops the 'county'/'parish'/'borough' suffix so 'FAIRFAX COUNTY' != 'FAIRFAX CITY'."""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^saint ", "st ", s)
    s = re.sub(r" (county|parish|borough|census area|municipality)$", "", s)
    return s


def _load_census() -> None:
    global _fips_by_name
    with _fips_lock:
        if _fips_by_name is not None:
            return
        raw = http_get(CENSUS_COUNTY_URL, timeout=30, retries=3,
                       cache_name="national_county2020.txt", cache_max_age=float("inf"))
        table: dict[tuple[str, str], str] = {}
        for line in decode(raw).splitlines()[1:]:
            parts = line.split("|")
            if len(parts) < 5:
                continue
            st, stfp, cofp, _ns, cname = parts[:5]
            fips = stfp + cofp
            table[(st, norm_county(cname))] = fips
            _name_by_fips[fips] = re.sub(r" County$", "", cname)
        _fips_by_name = table


_ALIASES = {
    # feed spelling -> census spelling (normalised)
    ("FL", "dade"): "miami dade",
}


def county_fips(state: str, name: str) -> str:
    """5-digit FIPS for a county / county-equivalent. Raises KeyError if unknown."""
    _load_census()
    key = (state.upper(), norm_county(name))
    key = (key[0], _ALIASES.get(key, key[1]))
    assert _fips_by_name is not None
    try:
        return _fips_by_name[key]
    except KeyError:
        raise KeyError(f"no FIPS for {state} county {name!r} (normalised {key[1]!r})") from None


def county_name(fips: str, default: str | None = None) -> str:
    """Census name without the ' County' suffix ('Alachua', 'Alexandria city', 'Miami-Dade')."""
    try:
        _load_census()
    except FeedError:
        pass
    return _name_by_fips.get(fips, default if default is not None else fips)


# ------------------------------------------------------------------ contests
_CONTEST_SUBS = [
    (r"\(vote for \d+\)", " "),
    (r"\bu\s*\.\s*s\s*\.?(?=\s|$)", " us "),
    (r"\bunited states\b", " us "),
    (r"\bsenator\b", "senate"),
    (r"\brepresentative in congress\b", "us house"),
    (r"\bus house of representatives\b", "us house"),
    (r"\bmember,? house of representatives\b", "us house"),
    (r"\bmember,? us senate\b", "us senate"),
    (r"\bgov\b", "governor"),
    (r"\blt\.? gov(ernor)?\b", "lieutenant governor"),
]


def norm_contest(name: str) -> str:
    s = " " + name.lower() + " "
    for pat, rep in _CONTEST_SUBS:
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def pick_contests(query: str, contests: dict[str, int]) -> list[str]:
    """Pick contest(s) matching `query` (case-insensitive substring after normalisation).

    contests: {contest name: number of counties it appears in}.
    If any match is statewide (appears in the max number of counties), return the single best
    statewide match (earliest match position, then shortest name: 'Governor' beats
    'Lieutenant Governor'). Otherwise return every match (e.g. 'U.S. House' -> all districts;
    the engine sums rows that share a FIPS)."""
    q = norm_contest(query)
    if not q:
        raise LookupError("empty race query")
    hits = [(norm_contest(n).find(q), n) for n in contests]
    hits = [(pos, n) for pos, n in hits if pos >= 0]
    if not hits:
        sample = sorted(contests, key=lambda n: -contests[n])[:25]
        raise LookupError(f"no contest matching {query!r}. Statewide-ish contests: {sample}")
    full = max(contests.values())
    statewide = [(pos, n) for pos, n in hits if contests[n] == full]
    if statewide:
        return [min(statewide, key=lambda h: (h[0], len(h[1])))[1]]
    return [n for _, n in sorted(hits, key=lambda h: h[1])]


def party_kind(party: str | None) -> str | None:
    p = (party or "").strip().upper()
    if p in DEM_PARTIES or p.startswith("DEMOCRAT"):
        return "D"
    if p in REP_PARTIES or p.startswith("REPUBLICAN"):
        return "R"
    return None


def build_rows(state: str, counties: dict[str, dict], cand_party: dict[str, str | None]) -> list[dict]:
    """counties: {fips: {"county": name, "votes": {candidate: int}, "prt": int|None, "ptl": int|None}}
    cand_party: {candidate: party code}. The Dem/Rep candidate is the party's statewide leader
    (so names are consistent across counties); total includes every candidate / write-ins."""
    statewide: dict[str, int] = {}
    for c in counties.values():
        for cand, v in c["votes"].items():
            statewide[cand] = statewide.get(cand, 0) + v

    def leader(kind: str) -> str | None:
        cands = [c for c, p in cand_party.items() if party_kind(p) == kind]
        return max(cands, key=lambda c: (statewide.get(c, 0), c)) if cands else None

    dem, rep = leader("D"), leader("R")
    rows = []
    for fips in sorted(counties):
        c = counties[fips]
        votes = c["votes"]
        rows.append({
            "state": state, "county": c["county"], "fips": fips,
            "dem": int(votes.get(dem, 0)) if dem else 0,
            "rep": int(votes.get(rep, 0)) if rep else 0,
            "total": int(sum(votes.values())),
            "precincts_reporting": c.get("prt"), "precincts_total": c.get("ptl"),
            "dem_name": dem or "", "rep_name": rep or "",
        })
    return rows


def to_int(x) -> int:
    if x is None:
        return 0
    if isinstance(x, (int, float)):
        return int(x)
    s = str(x).replace(",", "").strip()
    return int(float(s)) if s else 0


def opt_int(x) -> int | None:
    try:
        return None if x is None or str(x).strip() == "" else to_int(x)
    except ValueError:
        return None


# ------------------------------------------------------------------ CLI
def print_table(rows: list[dict], limit: int | None = None) -> None:
    if not rows:
        print("(no rows)")
        return
    dn, rn = rows[0]["dem_name"], rows[0]["rep_name"]
    contests = sorted({r.get("contest", "") for r in rows} - {""})
    if contests:
        print("contests:", "; ".join(contests))
    print(f"D = {dn}   R = {rn}")
    hdr = f"{'county':<22}{'fips':>6}{'dem':>12}{'rep':>12}{'total':>12}{'D-R %':>8}{'prec':>13}"
    print(hdr)
    print("-" * len(hdr))
    shown = rows if limit is None else rows[:limit]
    for r in shown:
        m = (r["dem"] - r["rep"]) / r["total"] * 100 if r["total"] else 0.0
        pr = (f"{r['precincts_reporting']}/{r['precincts_total']}"
              if r["precincts_total"] is not None else "-")
        print(f"{r['county'][:21]:<22}{r['fips']:>6}{r['dem']:>12,}{r['rep']:>12,}{r['total']:>12,}"
              f"{m:>+8.1f}{pr:>13}")
    if limit is not None and len(rows) > limit:
        print(f"... ({len(rows) - limit} more counties; --all to show)")
    D = sum(r["dem"] for r in rows)
    R = sum(r["rep"] for r in rows)
    T = sum(r["total"] for r in rows)
    pr = [r for r in rows if r["precincts_total"] is not None]
    prs = (f"{sum(r['precincts_reporting'] or 0 for r in pr):,}/{sum(r['precincts_total'] for r in pr):,}"
           if pr else "-")
    m = (D - R) / T * 100 if T else 0.0
    print("-" * len(hdr))
    print(f"{'STATEWIDE (' + str(len(rows)) + ' rows)':<28}{D:>12,}{R:>12,}{T:>12,}{m:>+8.1f}{prs:>13}")


def main(module_fetch, races: list[str], state: str, default_race: str | None = None) -> None:
    ap = argparse.ArgumentParser(description=f"{state} live results adapter")
    ap.add_argument("--race", default=default_race or races[0])
    ap.add_argument("--election", default="2026-11-03")
    ap.add_argument("--all", action="store_true", help="print every county")
    a = ap.parse_args()
    t = time.time()
    try:
        rows = module_fetch(a.race, a.election)
    except (LookupError, FeedError) as e:
        raise SystemExit(f"{state} {a.election}: {e}")
    print(f"{state} {a.election} race={a.race!r}: {len(rows)} rows in {time.time() - t:.1f}s")
    print_table(rows, None if a.all else 15)


# ------------------------------------------------------------------ added for the KY/IN/GA/OH adapters
_HOUSE_QUERY_RE = re.compile(
    r"\b(u\.?\s*s\.?\s*house|us house|house of representatives|congress(ional)?|"
    r"u\.?\s*s\.?\s*representatives?|united states representatives?)\b", re.I)
_HOUSE_CONTEST_RE = re.compile(
    r"\b(u\.?\s*s\.?|us|united states)\s+(house|representative)|representative (in|to) congress|"
    r"\bcongressional district\b|\bmember of congress\b", re.I)


def is_house_query(query: str) -> bool:
    """'U.S. House', 'US House', 'Congress', 'U.S. Representative' -> True ('State House' -> False)."""
    return bool(_HOUSE_QUERY_RE.search(query or "")) and not re.search(r"\bstate\b", query or "", re.I)


def is_house_contest(name: str) -> bool:
    """True for U.S. House contests in any common spelling (not state legislative ones)."""
    return bool(_HOUSE_CONTEST_RE.search(name or "")) and not re.search(r"\bstate\s+(house|rep)", name or "", re.I)


def split_party_suffix(text: str) -> tuple[str, str | None]:
    """'Jon Ossoff (I) (Dem)' -> ('Jon Ossoff', 'Dem'); 'Brian Jack (I)(Rep)' -> ('Brian Jack', 'Rep').
    '(I)' (incumbent marker) is dropped; the trailing party, if any, is returned."""
    s = (text or "").strip()
    party = None
    m = re.search(r"\(\s*([A-Za-z.]{1,12})\s*\)\s*$", s)
    if m and m.group(1).upper().rstrip(".") not in ("I", "INC", "W/I", "WI"):
        party = m.group(1)
        s = s[: m.start()].rstrip()
    s = re.sub(r"\s*\((?:I|Inc|INC|i)\)\s*", " ", s)
    return " ".join(s.split()), party


def xml_local(tag: str) -> str:
    """'{ns}Contest' -> 'Contest'."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def xml_records(root, tag: str) -> list[dict]:
    """Flat records for WCF/DataContract-style XML (<ArrayOfX><X><Field>v</Field>...</X>): every
    element named `tag` (any namespace) that has child elements but no child named `tag`,
    as {child local name: text}. Attributes are merged in too (older attribute-style feeds)."""
    out = []
    for el in root.iter():
        if xml_local(el.tag) != tag:
            continue
        kids = list(el)
        if kids and any(xml_local(k.tag) == tag for k in kids):
            continue  # a wrapper like <CandidateData><CandidateData>..</CandidateData></CandidateData>
        rec = {xml_local(k): v for k, v in el.attrib.items()}
        for k in kids:
            rec[xml_local(k.tag)] = (k.text or "").strip()
        if rec:
            out.append(rec)
    return out


def merge_rows(state: str, row_lists: list[list[dict]], dem_name: str, rep_name: str,
               precincts: str = "sum") -> list[dict]:
    """Combine several contests' build_rows() output into one row per county (e.g. every U.S.
    House district -> county D and R totals). precincts: 'sum' (contest precinct sets are
    disjoint within a county), 'max' (feeds give county-wide precinct counts), or 'none'."""
    acc: dict[str, dict] = {}
    for rows in row_lists:
        for r in rows:
            a = acc.get(r["fips"])
            if a is None:
                a = acc[r["fips"]] = {"state": state, "county": r["county"], "fips": r["fips"],
                                      "dem": 0, "rep": 0, "total": 0,
                                      "precincts_reporting": None, "precincts_total": None,
                                      "dem_name": dem_name, "rep_name": rep_name}
            for k in ("dem", "rep", "total"):
                a[k] += int(r.get(k) or 0)
            if precincts == "none":
                continue
            for k in ("precincts_reporting", "precincts_total"):
                v = r.get(k)
                if v is None:
                    continue
                if a[k] is None:
                    a[k] = int(v)
                elif precincts == "max":
                    a[k] = max(a[k], int(v))
                else:
                    a[k] += int(v)
    return [acc[f] for f in sorted(acc)]


_ORDINALS = {w: i for i, w in enumerate(
    "zero first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
    "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split())}
_ORDINALS.update({w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split()) if i})


def district_number(name: str) -> int | None:
    """'District 01' / '1st Congressional District' / 'First District' / 'CD-2' -> int."""
    s = (name or "").lower()
    m = re.search(r"(\d+)(st|nd|rd|th)?\b", s)
    if m:
        return int(m.group(1))
    for w in re.findall(r"[a-z]+", s):
        if w in _ORDINALS and w not in ("one",):
            return _ORDINALS[w]
    return None


def select_house(query: str, names) -> list[str]:
    """U.S. House contests among `names` for a House query. A district number in the query
    ('U.S. House 2', 'Congress 2nd') narrows to that district; special/unexpired-term contests
    are left out unless the query says 'special'."""
    special = "special" in (query or "").lower() or "unexpired" in (query or "").lower()
    out = [n for n in names if is_house_contest(n)
           and (special or not re.search(r"special|unexpired", n, re.I))]
    want = district_number(re.sub(r"(?i)\bu\.?\s*s\.?\b", " ", query or ""))
    if want is not None:
        out = [n for n in out if district_number(n) == want]
    return out
