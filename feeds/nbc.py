"""NBC News results pages (AP data) as a fallback results source.

Pages like https://www.nbcnews.com/politics/2026-elections/kentucky-senate-results embed the race
in __NEXT_DATA__: props.pageProps.races[i] -> areas[] (one per county: name, percentIn, votes,
candidates[{name, party 'gop'|'dem'|..., votes}]) and summary.percentInChart.values[] (statewide
votes by vote type: "Election Day", "Early In Person", "Mail"...). AP collects from counties
directly, so this is often ahead of the state's own feed.

fetch(race, election) with race "<state>|<slug>" e.g. "KY|kentucky-senate-results", or a config
race name resolved through SLUGS. Returns the standard county rows; statewide vote-type split is
in last_summary[slug].
"""
from __future__ import annotations

import json
import re
import sys

from . import common

BASE = "https://www.nbcnews.com/politics/2026-elections/"
SLUGS = {
    ("KY", "senate"): "kentucky-senate-results",
    ("IN", "secretary of state"): "indiana-secretary-of-state-results",
    ("TX", "governor"): "texas-governor-results",
    ("TX", "senate"): "texas-senate-results",
    ("FL", "governor"): "florida-governor-results",
    ("FL", "senate"): "florida-senate-results",
    ("GA", "senate"): "georgia-senate-results",
    ("GA", "governor"): "georgia-governor-results",
    ("NC", "senate"): "north-carolina-senate-results",
    ("OH", "senate"): "ohio-senate-results",
    ("OH", "governor"): "ohio-governor-results",
    ("VA", "senate"): "virginia-senate-results",
    ("SC", "senate"): "south-carolina-senate-results",
}
last_summary: dict[str, list] = {}


def _slug(state: str, race: str) -> str:
    r = race.lower()
    for (st, key), slug in SLUGS.items():
        if st == state and key in r:
            return slug
    raise common.FeedError(f"nbc: no page mapped for {state} {race}")


def page(slug: str) -> dict:
    raw = common.http_get(common.bust(BASE + slug, "live"), timeout=30, retries=2, memo_seconds=20)
    t = common.decode(raw)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', t, re.S)
    if not m:
        raise common.FeedError(f"nbc: no __NEXT_DATA__ on {slug}")
    return json.loads(m.group(1))


def fetch_state(state: str, race: str) -> list[dict]:
    slug = _slug(state, race)
    d = page(slug)
    races = ((d.get("props") or {}).get("pageProps") or {}).get("races") or []
    if not races:
        raise common.FeedError(f"nbc: no races on {slug}")
    r = races[0]
    last_summary[slug] = ((r.get("summary") or {}).get("percentInChart") or {}).get("values") or []
    rows = []
    for a in r.get("areas") or []:
        dem = rep = tot = 0
        dn = rn = None
        for c in a.get("candidates") or []:
            v = int(c.get("votes") or 0)
            tot += v
            p = (c.get("party") or "").lower()
            if p in ("dem", "d", "democrat") and v >= dem:
                dem, dn = v, c.get("name")
            elif p in ("gop", "rep", "r", "republican") and v >= rep:
                rep, rn = v, c.get("name")
        name = a.get("name") or ""
        try:
            fips = common.county_fips(state, name)
        except Exception:  # noqa: BLE001
            fips = None
        rows.append({"state": state, "county": name, "fips": fips, "dem": dem, "rep": rep, "total": tot,
                     "precincts_reporting": None, "precincts_total": None, "pct_in": a.get("percentIn"),
                     "dem_name": dn, "rep_name": rn})
    return rows


def fetch(race: str, election: str = "2026-11-03") -> list[dict]:
    if "|" not in race:
        raise common.FeedError("nbc: race must be 'ST|race name'")
    st, name = race.split("|", 1)
    return fetch_state(st.strip().upper(), name.strip())


def statewide_split(slug: str) -> list[dict]:
    out = []
    for v in last_summary.get(slug) or []:
        out.append({"type": v.get("description"), "votes": v.get("votes"),
                    "shares": {c.get("lastName"): c.get("percentInt") for c in v.get("candidates") or []}})
    return out


if __name__ == "__main__":
    for arg in sys.argv[1:] or ["KY|senate"]:
        rows = fetch(arg)
        st = arg.split("|")[0]
        d = sum(r["dem"] for r in rows); rp = sum(r["rep"] for r in rows); t = sum(r["total"] for r in rows)
        n = sum(1 for r in rows if r["total"])
        print(f"{arg}: {t:,} votes, {n}/{len(rows)} counties, D {d:,} R {rp:,} margin {100 * (d - rp) / t if t else 0:+.1f}")
        print("   split:", statewide_split(_slug(st, arg.split('|')[1])))
