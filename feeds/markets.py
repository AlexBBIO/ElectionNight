"""Prediction-market expectations (Kalshi + Polymarket) for the tracked races (stdlib only).

fetch() -> {"races": {race_id: {...}}, "national": {...}, "as_of": iso}

Per race:
  p_dem           probability the Democrat wins (volume-weighted mean of the sources; each
                  source's D/R prices are normalised so p_D + p_R = 1). None for IN-HOUSE.
  sources         {"kalshi"|"polymarket": {p_dem, ticker|slug, volume, updated, ...}}
  margin_brackets [{"lo", "hi", "p"}] D-R points, open ends None, p normalised to sum 1,
                  from the venue with more bracket volume (margin_brackets_source); each
                  venue's own set is under sources[venue]["brackets"].
  implied_margin  D-R points: probability-weighted bracket midpoints (open ends at edge +/-5),
                  volume-weighted across venues that list brackets; otherwise
                  SIGMA * NormalInverse(clamp(p_dem, .02, .98)).
  margin_from_p   the SIGMA formula even when brackets exist (to compare the two).

Market sources found 2026-11-03 (seeded in DEFAULT_MAP and written to cache/markets/map.json):
  Kalshi winner   SENATE{ST}-26 / SENATE{ST}S-26 (specials) / GOVPARTY{ST}-26 / KXSECSTATEIN-26,
                  markets -D/-R (-DEM/-GOP); no Kalshi KY Senate 2026 event exists.
  Kalshi margin   series KXGENERALMOV, events KXGENERALMOV-{winner event} ("Name a% to b%"
                  brackets, 2-pt wide) for NC/GA/OH/FL/SC Senate and FL/OH Governor.
  Kalshi national CONTROLS-2026, CONTROLH-2026, KXHOUSEPOPVOTEMARGIND-26NOV03 (>=X ladder).
  Polymarket      {state}-senate-election-winner, {state}-senate-margin-of-victory-2026,
                  {state}-governor-winner-2026, {state}-governor-margin-of-victory-2026, ...

Prices: mid of best bid/ask when the spread is <= MAX_SPREAD, else last trade.
Volumes: Kalshi volume is contracts (<= $1 each), Polymarket volume is USD; both are used
as-is for weighting.

Politeness: Kalshi answered HTTP 429 from this host, so a refresh makes at most 2 Kalshi
requests (one /markets?tickers=... for winners/control/popular vote, one KXGENERALMOV
events call for the margin brackets) and 1 Polymarket request (all slugs in one /events
call). Bodies are cached under cache/markets/ and reused for CACHE_SECONDS; a 429 starts a
cool-down (2, 4, 8 ... 15 min) during which only cached Kalshi data is served (flagged
stale). Ticker discovery for unmapped races runs at most once per RESOLVE_EVERY seconds.

CLI: python3 -m election_night.feeds.markets [--resolve] [--json] [--snapshot PATH]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import statistics
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import common

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
CACHE = common.CACHE_DIR / "markets"
MAP_PATH = CACHE / "map.json"
COOLDOWN_PATH = CACHE / "kalshi_cooldown.json"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "markets_snapshot.json"
BASELINE_PATH = Path(__file__).resolve().parent.parent / "data" / "markets_baseline.json"

CACHE_SECONDS = 60.0
RESOLVE_EVERY = 6 * 3600.0
SIGMA = 5.0            # state-race forecast error (points) at the end of a campaign
P_CLAMP = (0.02, 0.98)
OPEN_END = 5.0         # open-ended bracket "X%+" is valued at X + 5
MAX_SPREAD = 0.10      # wider than this -> use the last trade instead of the mid
KALSHI_GAP = 3.0       # seconds between Kalshi requests within one refresh
PM_GAP = 0.25          # seconds between Polymarket CLOB history requests (baseline build only)

# Baseline = the market going into the day: 2026-11-03 06:00 ET (11:00 UTC), before polls
# opened. The price is the hourly point at that hour (ts <= BASELINE_TS + BASELINE_SLACK),
# else the last one before it.
BASELINE_TS = _dt.datetime(2026, 11, 3, 11, 0, tzinfo=_dt.timezone.utc).timestamp()
BASELINE_SLACK = 300.0
BASELINE_RETRY = 600.0
HIST_LOOKBACK = 24 * 3600.0

# ------------------------------------------------------------------ market map (seed)
# kalshi: winner event, its D/R market tickers, and the KXGENERALMOV margin event (if any).
# polymarket: winner / margin event slugs. Guessed slugs that do not exist are harmless
# (the multi-slug request just omits them).
DEFAULT_MAP: dict = {
    "races": {
        "KY-SEN": {"kalshi": {"series": ["SENATEKY"], "event": None, "D": None, "R": None,
                              "margin_event": None},
                   "polymarket": {"winner": "kentucky-senate-election-winner",
                                  "margin": "kentucky-senate-margin-of-victory-2026"}},
        "IN-SOS": {"kalshi": {"series": ["KXSECSTATEIN"], "event": "KXSECSTATEIN-26",
                              "D": "KXSECSTATEIN-26-DEM", "R": "KXSECSTATEIN-26-GOP",
                              "margin_event": None},
                   "polymarket": {"winner": "indiana-secretary-of-state-election-winner-2026",
                                  "margin": None}},
        "IN-HOUSE": {"kalshi": None, "polymarket": None},
        "FL-GOV": {"kalshi": {"series": ["GOVPARTYFL"], "event": "GOVPARTYFL-26",
                              "D": "GOVPARTYFL-26-D", "R": "GOVPARTYFL-26-R",
                              "margin_event": "KXGENERALMOV-GOVPARTYFL-26"},
                   "polymarket": {"winner": "florida-governor-winner-2026",
                                  "margin": "florida-governor-margin-of-victory-2026"}},
        "FL-SEN": {"kalshi": {"series": ["SENATEFLS"], "event": "SENATEFLS-26",
                              "D": "SENATEFLS-26-D", "R": "SENATEFLS-26-R",
                              "margin_event": "KXGENERALMOV-SENATEFLS-26"},
                   "polymarket": {"winner": "florida-senate-election-winner",
                                  "margin": "florida-senate-margin-of-victory-2026"}},
        "GA-SEN": {"kalshi": {"series": ["SENATEGA"], "event": "SENATEGA-26",
                              "D": "SENATEGA-26-D", "R": "SENATEGA-26-R",
                              "margin_event": "KXGENERALMOV-SENATEGA-26"},
                   "polymarket": {"winner": "georgia-senate-election-winner",
                                  "margin": "georgia-senate-election-first-round-margin-of-victory-20260804175717495"}},
        "GA-GOV": {"kalshi": {"series": ["GOVPARTYGA"], "event": "GOVPARTYGA-26",
                              "D": "GOVPARTYGA-26-D", "R": "GOVPARTYGA-26-R",
                              "margin_event": None},
                   "polymarket": {"winner": "georgia-governor-winner-2026",
                                  "margin": "georgia-governor-margin-of-victory-2026"}},
        "VA-SEN": {"kalshi": {"series": ["SENATEVA"], "event": "SENATEVA-26",
                              "D": "SENATEVA-26-D", "R": "SENATEVA-26-R",
                              "margin_event": None},
                   "polymarket": {"winner": "virginia-senate-election-winner",
                                  "margin": "virginia-senate-margin-of-victory-2026"}},
        "SC-SEN": {"kalshi": {"series": ["SENATESC"], "event": "SENATESC-26",
                              "D": "SENATESC-26-D", "R": "SENATESC-26-R",
                              "margin_event": "KXGENERALMOV-SENATESC-26"},
                   "polymarket": {"winner": "south-carolina-senate-election-winner",
                                  "margin": "south-carolina-senate-margin-of-victory-2026"}},
        "NC-SEN": {"kalshi": {"series": ["SENATENC"], "event": "SENATENC-26",
                              "D": "SENATENC-26-D", "R": "SENATENC-26-R",
                              "margin_event": "KXGENERALMOV-SENATENC-26"},
                   "polymarket": {"winner": "north-carolina-senate-election-winner",
                                  "margin": "north-carolina-senate-margin-of-victory-2026"}},
        "OH-SEN": {"kalshi": {"series": ["SENATEOHS"], "event": "SENATEOHS-26",
                              "D": "SENATEOHS-26-D", "R": "SENATEOHS-26-R",
                              "margin_event": "KXGENERALMOV-SENATEOHS-26"},
                   "polymarket": {"winner": "ohio-senate-election-winner",
                                  "margin": "ohio-senate-margin-of-victory-2026"}},
        "OH-GOV": {"kalshi": {"series": ["GOVPARTYOH"], "event": "GOVPARTYOH-26",
                              "D": "GOVPARTYOH-26-D", "R": "GOVPARTYOH-26-R",
                              "margin_event": "KXGENERALMOV-GOVPARTYOH-26"},
                   "polymarket": {"winner": "ohio-governor-winner-2026",
                                  "margin": "ohio-governor-margin-of-victory-2026"}},
    },
    "national": {
        "senate": {"kalshi": {"D": "CONTROLS-2026-D", "R": "CONTROLS-2026-R"},
                   "polymarket": "which-party-will-win-the-senate-in-2026"},
        "house": {"kalshi": {"D": "CONTROLH-2026-D", "R": "CONTROLH-2026-R"},
                  "polymarket": "which-party-will-win-the-house-in-2026"},
        "house_popular_vote": {
            "kalshi_series": "KXHOUSEPOPVOTEMARGIND",
            "kalshi_event": "KXHOUSEPOPVOTEMARGIND-26NOV03",
            "kalshi_ladder": ["KXHOUSEPOPVOTEMARGIND-26NOV03-AL%d" % k
                              for k in (2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16)],
            "polymarket": "2026-midterms-house-popular-vote-margin-of-victory-224"},
    },
    "kalshi_margin_series": "KXGENERALMOV",
}

_lock = threading.Lock()


# ------------------------------------------------------------------ small helpers
def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return _dt.datetime.fromtimestamp(ts if ts is not None else _now(), _dt.timezone.utc) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")


def _f(x) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, obj) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1, sort_keys=False))
        tmp.replace(path)
    except OSError:
        pass


def price(bid, ask, last, fallback=None) -> float | None:
    """Mid of bid/ask when both quoted and the spread is tight, else last trade."""
    bid, ask, last, fallback = _f(bid), _f(ask), _f(last), _f(fallback)
    if ask is not None and ask > 0 and (bid or 0) >= 0 and ask - (bid or 0) <= MAX_SPREAD:
        return ((bid or 0.0) + ask) / 2
    if last is not None and last > 0:
        return last
    if fallback is not None:
        return fallback
    if ask is not None and ask > 0:
        return ((bid or 0.0) + ask) / 2
    return None


def margin_from_p(p: float | None, sigma: float = SIGMA) -> float | None:
    if p is None:
        return None
    p = min(max(p, P_CLAMP[0]), P_CLAMP[1])
    return sigma * statistics.NormalDist().inv_cdf(p)


def bracket_mean(brackets: list[dict] | None) -> float | None:
    if not brackets:
        return None
    tot = sum(b["p"] for b in brackets)
    if tot <= 0:
        return None
    s = 0.0
    for b in brackets:
        lo, hi = b["lo"], b["hi"]
        if lo is None and hi is None:
            continue
        mid = (hi - OPEN_END) if lo is None else (lo + OPEN_END) if hi is None else (lo + hi) / 2
        s += b["p"] * mid
    return s / tot


def _normalise(brackets: list[dict]) -> list[dict]:
    tot = sum(b["p"] for b in brackets)
    if tot <= 0:
        return []
    out = [{"lo": b["lo"], "hi": b["hi"], "p": round(b["p"] / tot, 4)} for b in brackets]
    return sorted(out, key=lambda b: (b["lo"] if b["lo"] is not None else -1e9,
                                      b["hi"] if b["hi"] is not None else 1e9))


def _signed(party: str, a: float, b: float | None) -> tuple[float | None, float | None]:
    """'party wins by a..b points' -> (lo, hi) in D-R points (None = open end)."""
    if party == "D":
        return a + 0.0, b
    return (-b + 0.0 if b is not None else None), -a + 0.0  # + 0.0: no "-0.0"


def _party_of(who: str, d_name: str, r_name: str) -> str | None:
    w = who.strip().lower()
    if w.startswith("democrat"):
        return "D"
    if w.startswith("republican"):
        return "R"
    toks = [t for t in re.findall(r"[a-z]+", w) if len(t) > 2]
    for party, name in (("D", d_name), ("R", r_name)):
        n = (name or "").lower()
        if toks and all(t in n for t in toks):
            return party
    for party, name in (("D", d_name), ("R", r_name)):
        n = (name or "").lower()
        if any(t in n for t in toks):
            return party
    return None


def combine_brackets(sets: list[tuple[list[dict], float, str]]
                     ) -> tuple[list[dict] | None, float | None, str | None]:
    """[(brackets, volume, venue)] -> (brackets of the highest-volume venue, volume-weighted
    mean of each venue's bracket mean, venue). The venues' bracket edges differ, so the
    returned set is one venue's (non-overlapping, for display); the mean uses both."""
    sets = [x for x in sets if x[0]]
    if not sets:
        return None, None, None
    best = max(sets, key=lambda x: x[1])
    mean = wavg([(bracket_mean(b), max(v, 1.0)) for b, v, _ in sets])
    return best[0], mean, best[2]


def wavg(pairs: list[tuple[float | None, float | None]]) -> float | None:
    """Weighted mean of (value, weight); weights unknown -> plain mean."""
    pairs = [(v, w) for v, w in pairs if v is not None]
    if not pairs:
        return None
    if all(w and w > 0 for _, w in pairs):
        return sum(v * w for v, w in pairs) / sum(w for _, w in pairs)
    return sum(v for v, _ in pairs) / len(pairs)


# ------------------------------------------------------------------ HTTP with cache
class _Got:
    def __init__(self, data, fetched: float | None, stale: bool, error: str | None = None):
        self.data, self.fetched, self.stale, self.error = data, fetched, stale, error


def _cooldown_left() -> float:
    c = _read_json(COOLDOWN_PATH, {}) or {}
    return max(0.0, float(c.get("until", 0)) - _now())


def _note_429() -> None:
    c = _read_json(COOLDOWN_PATH, {}) or {}
    strikes = int(c.get("strikes", 0)) + 1 if _now() - float(c.get("until", 0)) < 1800 else 1
    _write_json(COOLDOWN_PATH, {"until": _now() + min(60 * 2 ** strikes, 900), "strikes": strikes,
                                "at": _iso()})


def _get(url: str, cache_name: str, *, kalshi: bool) -> _Got:
    """Cached GET -> parsed JSON. Fresh cache (< CACHE_SECONDS) is served without a request;
    on failure (or during a Kalshi cool-down) the last cached body is served, flagged stale."""
    path = CACHE / cache_name
    age = _now() - path.stat().st_mtime if path.exists() else None
    if age is not None and age < CACHE_SECONDS:
        d = _read_json(path)
        if d is not None:
            return _Got(d, path.stat().st_mtime, False)
    err = None
    if kalshi and _cooldown_left() > 0:
        err = f"Kalshi cool-down after HTTP 429 ({_cooldown_left():.0f}s left)"
    else:
        try:
            raw = common.http_get(url, timeout=20, retries=0 if kalshi else 1, backoff=3,
                                  cache_name=f"markets/{cache_name}")
            return _Got(json.loads(common.decode(raw)), _now(), False)
        except (common.FeedError, ValueError) as e:
            err = str(e)
            if kalshi and "429" in err:
                _note_429()
    d = _read_json(path) if path.exists() else None
    return _Got(d, path.stat().st_mtime if d is not None else None, True, err)


# ------------------------------------------------------------------ mapping
def load_map() -> dict:
    m = _read_json(MAP_PATH)
    if not m or "races" not in m:
        m = json.loads(json.dumps(DEFAULT_MAP))
        m["created"] = _iso()
        _write_json(MAP_PATH, m)
    return m


def resolve(m: dict | None = None, budget: int = 4, force: bool = False) -> dict:
    """Look up Kalshi winner events for races without one (series events call per race,
    at most `budget` requests, no more than once per RESOLVE_EVERY unless force)."""
    m = m or load_map()
    if not force and _now() - float(m.get("resolve_attempt_ts", 0)) < RESOLVE_EVERY:
        return m
    m["resolve_attempt_ts"] = _now()
    used = 0
    for rid, spec in m["races"].items():
        k = spec.get("kalshi")
        if not k or k.get("D"):
            continue
        for series in k.get("series") or []:
            if used >= budget or _cooldown_left() > 0:
                break
            if used:
                time.sleep(KALSHI_GAP)
            used += 1
            g = _get(f"{KALSHI}/events?series_ticker={quote(series)}&status=open"
                     f"&with_nested_markets=true", f"kalshi_series_{series}.json", kalshi=True)
            for ev in (g.data or {}).get("events", []):
                if not re.search(r"-(26|2026)$", ev.get("event_ticker", "")):
                    continue
                for mk in ev.get("markets", []):
                    t = mk.get("ticker", "")
                    rules = (mk.get("rules_primary") or "") + " " + (mk.get("subtitle") or "")
                    if t.endswith(("-D", "-DEM")) or "Democratic" in rules:
                        k["D"] = k["D"] or t
                    elif t.endswith(("-R", "-GOP", "-REP")) or "Republican" in rules:
                        k["R"] = k["R"] or t
                if k.get("D"):
                    k["event"] = ev["event_ticker"]
                    break
    m["resolved_at"] = _iso()
    _write_json(MAP_PATH, m)
    if used:
        time.sleep(KALSHI_GAP)  # space the refresh's own Kalshi calls after discovery
    return m


# ------------------------------------------------------------------ prices (live or baseline)
def _k_price_live(mk: dict) -> float | None:
    return price(mk.get("yes_bid_dollars"), mk.get("yes_ask_dollars"), mk.get("last_price_dollars"))


def _pm_token(mk: dict) -> str | None:
    """CLOB token id of the market's first outcome ("Yes")."""
    try:
        return str(json.loads(mk.get("clobTokenIds") or "null")[0])
    except (ValueError, TypeError, IndexError, KeyError):
        return None


def _pm_price_live(mk: dict) -> float | None:
    try:
        fallback = json.loads(mk.get("outcomePrices") or "null")[0]
    except (ValueError, TypeError, IndexError):
        fallback = None
    return price(mk.get("bestBid"), mk.get("bestAsk"), mk.get("lastTradePrice"), fallback)


class Pricer:
    """Prices for market objects: live (from the objects themselves) or, when history maps
    are given, the baseline price {ticker|token: (price, unix ts)}. Records the timestamps
    of the baseline prices it hands out (pop_ts) so each source can report them."""

    def __init__(self, k_hist: dict | None = None, pm_hist: dict | None = None):
        self.k_hist, self.pm_hist = k_hist, pm_hist
        self._ts: list[float] = []

    @property
    def is_baseline(self) -> bool:
        return self.k_hist is not None or self.pm_hist is not None

    def _hist(self, table, key):
        h = (table or {}).get(key or "")
        if not h or h[0] is None:
            return None
        self._ts.append(h[1])
        return h[0]

    def k(self, mk: dict) -> float | None:
        return self._hist(self.k_hist, mk.get("ticker")) if self.is_baseline else _k_price_live(mk)

    def pm(self, mk: dict) -> float | None:
        return self._hist(self.pm_hist, _pm_token(mk)) if self.is_baseline else _pm_price_live(mk)

    def pop_ts(self) -> dict | None:
        t, self._ts = self._ts, []
        if not self.is_baseline or not t:
            return None
        return {"oldest": _iso(min(t)), "newest": _iso(max(t))}


LIVE = Pricer()


# ------------------------------------------------------------------ Kalshi parsing
def _k_vol(mk: dict) -> float:
    return _f(mk.get("volume_fp")) or _f(mk.get("volume")) or 0.0


_K_OUTCOME = re.compile(r"^(.*?)\s+([\d.]+)%\s+to\s+([\d.]+)%\s*$")


def kalshi_brackets(markets: list[dict], d_name: str, r_name: str,
                    pr: Pricer = LIVE) -> tuple[list[dict], float]:
    out, vol = [], 0.0
    for mk in markets:
        if mk.get("status") not in (None, "active", "open"):
            continue
        oc = ((mk.get("custom_strike") or {}).get("Outcome") or "")
        mo = _K_OUTCOME.match(oc)
        if not mo:
            continue
        party = _party_of(mo.group(1), d_name, r_name)
        if party is None:
            continue
        p = pr.k(mk)
        if p is None:
            continue
        a, b = float(mo.group(2)), float(mo.group(3))
        lo, hi = _signed(party, a, None if b >= 100 else b)
        out.append({"lo": lo, "hi": hi, "p": p})
        vol += _k_vol(mk)
    return _normalise(out), vol


def ladder_brackets(markets: list[dict], pr: Pricer = LIVE) -> tuple[list[dict], float]:
    """'D-R >= X' ladder (greater_or_equal) -> brackets; survival made monotone."""
    pts = []
    vol = 0.0
    for mk in markets:
        x = _f(mk.get("floor_strike"))
        if x is None or mk.get("strike_type") not in ("greater_or_equal", "greater"):
            continue
        if _k_vol(mk) <= 0 and not _f(mk.get("yes_bid_dollars")):
            continue  # never traded and no bid: skip
        p = pr.k(mk)
        if p is None:
            continue
        pts.append((x, p))
        vol += _k_vol(mk)
    pts.sort()
    if not pts:
        return [], 0.0
    surv, run = [], 1.0
    for x, p in pts:
        run = min(run, max(0.0, min(1.0, p)))
        surv.append((x, run))
    out = [{"lo": None, "hi": surv[0][0], "p": 1 - surv[0][1]}]
    for (x0, s0), (x1, s1) in zip(surv, surv[1:]):
        out.append({"lo": x0, "hi": x1, "p": max(0.0, s0 - s1)})
    out.append({"lo": surv[-1][0], "hi": None, "p": surv[-1][1]})
    return _normalise(out), vol


# ------------------------------------------------------------------ Polymarket parsing
def _pm_live(mk: dict) -> bool:
    return bool(mk.get("active", True)) and not mk.get("closed") and \
        ((_f(mk.get("volumeNum")) or 0) > 0 or (_f(mk.get("bestBid")) or 0) > 0)


def _pm_winner_markets(ev: dict) -> tuple[dict | None, dict | None]:
    d = r = None
    for mk in ev.get("markets", []):
        if not _pm_live(mk):
            continue
        t = (mk.get("groupItemTitle") or mk.get("question") or "").strip()
        if re.search(r"\(D\)|^Democrat", t, re.I):
            d = d or mk
        elif re.search(r"\(R\)|^Republican", t, re.I):
            r = r or mk
    return d, r


def _two_way(pd: float | None, pr_: float | None) -> float | None:
    if pd is not None and pr_ is not None and pd + pr_ > 0:
        return pd / (pd + pr_)
    return pd if pd is not None else (1 - pr_ if pr_ is not None else None)


def pm_winner(ev: dict, pr: Pricer = LIVE) -> dict | None:
    """{p_dem, d_name, r_name, volume, ...} from a winner/control event (groupItemTitle
    'Name (D)' / 'Democratic Party')."""
    d, r = _pm_winner_markets(ev)
    if not d and not r:
        return None
    pd, prr = (pr.pm(d) if d else None), (pr.pm(r) if r else None)
    clean = lambda m: re.sub(r"\s*\((D|R)\)\s*$", "", (m or {}).get("groupItemTitle") or "").strip()
    return {"p_dem": _two_way(pd, prr), "p_d_raw": pd, "p_r_raw": prr,
            "d_name": clean(d), "r_name": clean(r),
            "volume": sum(_f(x.get("volumeNum")) or 0 for x in (d, r) if x),
            "markets": [x.get("slug") for x in (d, r) if x]}


_PM_RANGE = re.compile(r"^(?P<who>.+?)\s+(?P<a>\d+(?:\.\d+)?)\s*%?\s*(?:-\s*(?P<b>\d+(?:\.\d+)?))?\s*%"
                       r"\s*(?P<plus>\+|or more)?\s*$", re.I)
_PM_WINS = re.compile(r"^(?P<who>.+?)\s+wins?\s*$", re.I)


def pm_brackets(ev: dict, d_name: str, r_name: str, pr: Pricer = LIVE) -> tuple[list[dict], float]:
    out, vol = [], 0.0
    for mk in ev.get("markets", []):
        if not _pm_live(mk):
            continue
        t = (mk.get("groupItemTitle") or "").strip()
        m = _PM_RANGE.match(t)
        if m:
            a = float(m.group("a"))
            b = float(m.group("b")) if m.group("b") else None
            if b is None and not m.group("plus"):
                continue
            who = m.group("who")
        else:
            w = _PM_WINS.match(t)
            if not w:
                continue
            who, a, b = w.group("who"), 0.0, None
        party = _party_of(who, d_name, r_name)
        if party is None:
            continue
        p = pr.pm(mk)
        if p is None:
            continue
        lo, hi = _signed(party, a, b)
        out.append({"lo": lo, "hi": hi, "p": p})
        vol += _f(mk.get("volumeNum")) or 0
    return _normalise(out), vol


# ------------------------------------------------------------------ build
def _kalshi_winner(by_ticker: dict, d: str | None, r: str | None, pr: Pricer = LIVE) -> dict | None:
    md, mr = by_ticker.get(d or ""), by_ticker.get(r or "")
    if not md and not mr:
        return None
    pd, prr = (pr.k(md) if md else None), (pr.k(mr) if mr else None)
    return {"p_dem": _two_way(pd, prr), "p_d_raw": pd, "p_r_raw": prr,
            "d_name": (md or {}).get("yes_sub_title") or "", "r_name": (mr or {}).get("yes_sub_title") or "",
            "volume": sum(_k_vol(x) for x in (md, mr) if x),
            "volume_24h": sum(_f(x.get("volume_24h_fp")) or 0 for x in (md, mr) if x)}


def _r(x, n=4):
    return None if x is None else round(x, n)


def _structure(m: dict) -> dict:
    """Current market objects (cached <= CACHE_SECONDS): Polymarket events by slug, Kalshi
    winner/control/ladder markets by ticker, Kalshi margin events by event ticker."""
    races_map, nat = m["races"], m["national"]
    status: dict = {}
    slugs = []
    for spec in races_map.values():
        pm = spec.get("polymarket") or {}
        slugs += [s for s in (pm.get("winner"), pm.get("margin")) if s]
    slugs += [nat["senate"]["polymarket"], nat["house"]["polymarket"],
              nat["house_popular_vote"]["polymarket"]]
    slugs = list(dict.fromkeys(slugs))
    pg = _get(f"{GAMMA}/events?limit={len(slugs) + 10}&" + "&".join(f"slug={quote(s)}" for s in slugs),
              "polymarket_events.json", kalshi=False)
    pm_ev = {e.get("slug"): e for e in (pg.data or []) if isinstance(e, dict)}
    status["polymarket"] = {"updated": _iso(pg.fetched) if pg.fetched else None,
                            "stale": pg.stale, "error": pg.error, "events": len(pm_ev)}

    tickers = []
    for spec in races_map.values():
        k = spec.get("kalshi") or {}
        tickers += [t for t in (k.get("D"), k.get("R")) if t]
    for key in ("senate", "house"):
        tickers += [nat[key]["kalshi"]["D"], nat[key]["kalshi"]["R"]]
    tickers += nat["house_popular_vote"].get("kalshi_ladder") or []
    kg = _get(f"{KALSHI}/markets?limit=200&tickers=" + ",".join(tickers),
              "kalshi_markets.json", kalshi=True)
    by_ticker = {mk["ticker"]: mk for mk in (kg.data or {}).get("markets", []) if mk.get("ticker")}
    if not kg.stale and kg.fetched and _now() - kg.fetched < KALSHI_GAP:
        time.sleep(KALSHI_GAP)  # we just hit Kalshi; space the next call
    mg = _get(f"{KALSHI}/events?series_ticker={m.get('kalshi_margin_series', 'KXGENERALMOV')}"
              f"&status=open&with_nested_markets=true", "kalshi_margin_events.json", kalshi=True)
    k_mov = {e.get("event_ticker"): e for e in (mg.data or {}).get("events", [])}
    status["kalshi"] = {"updated": _iso(kg.fetched) if kg.fetched else None, "stale": kg.stale,
                        "error": kg.error, "markets": len(by_ticker),
                        "margin_updated": _iso(mg.fetched) if mg.fetched else None,
                        "margin_stale": mg.stale, "margin_error": mg.error,
                        "margin_events": len(k_mov)}
    return {"pm_ev": pm_ev, "by_ticker": by_ticker, "k_mov": k_mov, "status": status}


def _margin_event(m: dict, k: dict, k_mov: dict) -> dict | None:
    return k_mov.get(k.get("margin_event") or f"{m.get('kalshi_margin_series')}-{k.get('event')}")


def _build(m: dict, st: dict, pr: Pricer) -> dict:
    """races + national from market objects `st` priced by `pr` (live or baseline)."""
    races_map, nat = m["races"], m["national"]
    pm_ev, by_ticker, k_mov, status = st["pm_ev"], st["by_ticker"], st["k_mov"], st["status"]
    k_upd = None if pr.is_baseline else status["kalshi"]["updated"]
    p_upd = None if pr.is_baseline else status["polymarket"]["updated"]

    races = {}
    for rid, spec in races_map.items():
        k, pm = spec.get("kalshi") or {}, spec.get("polymarket") or {}
        sources: dict = {}
        bsets: list[tuple[list[dict], float, str]] = []
        kw = _kalshi_winner(by_ticker, k.get("D"), k.get("R"), pr) if k else None
        if kw and kw["p_dem"] is not None:
            src = {"p_dem": _r(kw["p_dem"]), "ticker": k.get("event"), "volume": round(kw["volume"]),
                   "volume_24h": round(kw["volume_24h"]), "candidates": {"D": kw["d_name"], "R": kw["r_name"]}}
            if pr.is_baseline:
                src["price_ts"] = pr.pop_ts()
            else:
                src.update(updated=k_upd, stale=status["kalshi"]["stale"])
            ev = _margin_event(m, k, k_mov)
            if ev:
                br, v = kalshi_brackets(ev.get("markets", []), kw["d_name"], kw["r_name"], pr)
                if br:
                    bsets.append((br, v, "kalshi"))
                    src.update(margin_ticker=ev.get("event_ticker"), margin_volume=round(v),
                               implied_margin_brackets=_r(bracket_mean(br), 2), brackets=br)
                    if pr.is_baseline:
                        src["margin_price_ts"] = pr.pop_ts()
            sources["kalshi"] = src
        pr.pop_ts()
        pw = pm_winner(pm_ev[pm["winner"]], pr) if pm.get("winner") in pm_ev else None
        if pw and pw["p_dem"] is not None:
            src = {"p_dem": _r(pw["p_dem"]), "slug": pm["winner"], "volume": round(pw["volume"]),
                   "candidates": {"D": pw["d_name"], "R": pw["r_name"]}}
            if pr.is_baseline:
                src["price_ts"] = pr.pop_ts()
            else:
                src.update(updated=p_upd, stale=status["polymarket"]["stale"])
            if pm.get("margin") in pm_ev:
                dn = pw["d_name"] or (kw or {}).get("d_name", "")
                rn = pw["r_name"] or (kw or {}).get("r_name", "")
                br, v = pm_brackets(pm_ev[pm["margin"]], dn, rn, pr)
                if br:
                    bsets.append((br, v, "polymarket"))
                    src.update(margin_slug=pm["margin"], margin_volume=round(v),
                               implied_margin_brackets=_r(bracket_mean(br), 2), brackets=br)
                    if pr.is_baseline:
                        src["margin_price_ts"] = pr.pop_ts()
            sources["polymarket"] = src
        pr.pop_ts()
        p = wavg([(s["p_dem"], s.get("volume")) for s in sources.values()])
        brackets, bmean, bvenue = combine_brackets(bsets)
        mp = margin_from_p(p)
        im = bmean if brackets else mp
        races[rid] = {"p_dem": _r(p), "sources": sources, "margin_brackets": brackets,
                      "margin_brackets_source": bvenue,
                      "implied_margin": _r(im, 2), "margin_from_p": _r(mp, 2),
                      "margin_method": "brackets" if brackets else ("p_dem" if mp is not None else None)}

    national: dict = {}
    for key, out in (("senate", "senate_dem_control"), ("house", "house_dem_control")):
        kw = _kalshi_winner(by_ticker, nat[key]["kalshi"]["D"], nat[key]["kalshi"]["R"], pr)
        kts = pr.pop_ts()
        pw = pm_winner(pm_ev[nat[key]["polymarket"]], pr) if nat[key]["polymarket"] in pm_ev else None
        pts = pr.pop_ts()
        kw = kw if kw and kw["p_dem"] is not None else None
        pw = pw if pw and pw["p_dem"] is not None else None
        national[out] = _r(wavg([(x["p_dem"], x["volume"]) for x in (kw, pw) if x]))
        national[out + "_sources"] = {
            "kalshi": {"p_dem": _r(kw["p_dem"]), "ticker": nat[key]["kalshi"]["D"].rsplit("-", 1)[0],
                       "volume": round(kw["volume"]), **({"price_ts": kts} if kts else {})} if kw else None,
            "polymarket": {"p_dem": _r(pw["p_dem"]), "slug": nat[key]["polymarket"],
                           "volume": round(pw["volume"]), **({"price_ts": pts} if pts else {})} if pw else None}
    hp = nat["house_popular_vote"]
    bsets, srcs = [], {}
    kl = [by_ticker[t] for t in hp.get("kalshi_ladder") or [] if t in by_ticker]
    if kl:
        br, v = ladder_brackets(kl, pr)
        ts = pr.pop_ts()
        if br:
            bsets.append((br, v, "kalshi"))
            srcs["kalshi"] = {"ticker": hp.get("kalshi_event"), "volume": round(v),
                              "implied_margin": _r(bracket_mean(br), 2), "brackets": br,
                              **({"price_ts": ts} if ts else {})}
    if hp["polymarket"] in pm_ev:
        br, v = pm_brackets(pm_ev[hp["polymarket"]], "Democrats", "Republicans", pr)
        ts = pr.pop_ts()
        if br:
            bsets.append((br, v, "polymarket"))
            srcs["polymarket"] = {"slug": hp["polymarket"], "volume": round(v),
                                  "implied_margin": _r(bracket_mean(br), 2), "brackets": br,
                                  **({"price_ts": ts} if ts else {})}
    hb, hmean, hvenue = combine_brackets(bsets)
    national["house_popular_vote_margin"] = _r(hmean, 2)
    national["house_popular_vote_brackets"] = hb
    national["house_popular_vote_brackets_source"] = hvenue
    national["house_popular_vote_sources"] = srcs
    return {"races": races, "national": national}


_METHOD = {"sigma": SIGMA, "p_clamp": list(P_CLAMP), "open_end": OPEN_END, "max_spread": MAX_SPREAD,
           "note": "implied_margin = bracket-weighted mean if margin brackets exist, "
                   "else SIGMA*NormalInverse(p_dem); D-R points"}


# ------------------------------------------------------------------ baseline (pre-day expectation)
def _hist_get(url: str, name: str, *, kalshi: bool):
    """History before BASELINE_TS never changes: cache it on disk for good."""
    path = CACHE / "hist" / name
    if path.exists():
        d = _read_json(path)
        if d is not None:
            return d
    if kalshi and _cooldown_left() > 0:
        raise common.FeedError(f"Kalshi cool-down ({_cooldown_left():.0f}s left)")
    try:
        raw = common.http_get(url, timeout=20, retries=0 if kalshi else 1, backoff=3,
                              cache_name=f"markets/hist/{name}")
    except common.FeedError as e:
        if kalshi and "429" in str(e):
            _note_429()
        raise
    return json.loads(common.decode(raw))


def _cents(x) -> float | None:
    """Kalshi candle field: '0.5600' dollars string, or integer cents."""
    v = _f(x)
    if v is None:
        return None
    return v / 100 if (isinstance(x, (int,)) or (isinstance(x, float) and v > 1)) else v


def _ohlc_close(d: dict | None) -> float | None:
    if not d:
        return None
    for key in ("close_dollars", "close"):
        if d.get(key) is not None:
            return _cents(d[key])
    return None


def _kalshi_candle_price(c: dict) -> float | None:
    bid, ask = _ohlc_close(c.get("yes_bid")), _ohlc_close(c.get("yes_ask"))
    last = _ohlc_close(c.get("price"))
    if last is None:
        p = c.get("price") or {}
        last = _cents(p.get("previous_dollars") if p.get("previous_dollars") is not None else p.get("previous"))
    return price(bid, ask, last)


def _pick(points: list[tuple[float, float | None]]) -> tuple[float | None, float | None]:
    """Last (price, ts) at or before the baseline hour (ts <= BASELINE_TS + BASELINE_SLACK)."""
    ok = sorted((t, p) for t, p in points if p is not None and t <= BASELINE_TS + BASELINE_SLACK)
    return (ok[-1][1], ok[-1][0]) if ok else (None, None)


def kalshi_history(tickers: list[str], event_of: dict[str, str]) -> tuple[dict, list[str]]:
    """{ticker: (price, ts)} at the baseline from hourly candlesticks.
    Batch endpoint first (/markets/candlesticks, 1 request per 100 tickers); if Kalshi does
    not offer it (HTTP 400/404), one /series/{s}/events/{e}/candlesticks request per event.
    Stops at the first HTTP 429 (the caller retries later; responses are cached on disk)."""
    out, errors = {}, []
    start, end = int(BASELINE_TS - HIST_LOOKBACK), int(BASELINE_TS + 3600)
    todo = list(dict.fromkeys(tickers))
    fallback = False
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        if i:
            time.sleep(KALSHI_GAP)
        url = (f"{KALSHI}/markets/candlesticks?market_tickers={','.join(chunk)}"
               f"&start_ts={start}&end_ts={end}&period_interval=60")
        try:
            d = _hist_get(url, f"kalshi_batch_{int(BASELINE_TS)}_{i // 100}_{len(chunk)}.json", kalshi=True)
        except (common.FeedError, ValueError) as e:
            errors.append(str(e)[:200])
            fallback = "HTTP 404" in str(e) or "HTTP Error 400" in str(e)
            break
        for mc in d.get("markets") or []:
            pts = [(c.get("end_period_ts"), _kalshi_candle_price(c)) for c in mc.get("candlesticks") or []]
            out[mc.get("market_ticker") or mc.get("ticker")] = _pick([(t, p) for t, p in pts if t])
    if fallback:
        events = list(dict.fromkeys(event_of[t] for t in todo if t in event_of))
        for n, ev in enumerate(events):
            if _cooldown_left() > 0:
                errors.append("Kalshi cool-down: per-event history stopped")
                break
            if n:
                time.sleep(KALSHI_GAP)
            series = ev.split("-")[0]
            url = (f"{KALSHI}/series/{series}/events/{ev}/candlesticks"
                   f"?start_ts={start}&end_ts={end}&period_interval=60")
            try:
                d = _hist_get(url, f"kalshi_{int(BASELINE_TS)}_{ev}.json", kalshi=True)
            except (common.FeedError, ValueError) as e:
                errors.append(str(e)[:200])
                continue
            for t, cs in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
                pts = [(c.get("end_period_ts"), _kalshi_candle_price(c)) for c in cs or []]
                out[t] = _pick([(t_, p) for t_, p in pts if t_])
    return out, errors


def polymarket_history(tokens: list[str]) -> tuple[dict, list[str]]:
    """{token: (price, ts)} at the baseline from CLOB /prices-history (hourly fidelity)."""
    out, errors = {}, []
    start, end = int(BASELINE_TS - HIST_LOOKBACK), int(BASELINE_TS + 3600)
    for n, tok in enumerate(dict.fromkeys(tokens)):
        url = f"{CLOB}/prices-history?market={tok}&startTs={start}&endTs={end}&fidelity=60"
        name = f"pm_{int(BASELINE_TS)}_{tok[-24:]}.json"
        if n and not (CACHE / "hist" / name).exists():
            time.sleep(PM_GAP)
        try:
            d = _hist_get(url, name, kalshi=False)
        except (common.FeedError, ValueError) as e:
            errors.append(str(e)[:200])
            continue
        out[tok] = _pick([(_f(h.get("t")), _f(h.get("p"))) for h in d.get("history") or []])
    return out, errors


def build_baseline(write: bool = True) -> dict:
    """Price every mapped market at BASELINE_TS (Kalshi candlesticks, Polymarket CLOB
    history) and write the frozen expectation to BASELINE_PATH."""
    m = load_map()
    st = _structure(m)
    races_map, nat = m["races"], m["national"]
    by_ticker, k_mov, pm_ev = st["by_ticker"], st["k_mov"], st["pm_ev"]
    event_of: dict[str, str] = {}
    k_tickers: list[str] = []
    for mk in by_ticker.values():
        if re.search(r"-(26|2026|26NOV03)$", mk.get("event_ticker", "")):
            k_tickers.append(mk["ticker"])
            event_of[mk["ticker"]] = mk["event_ticker"]
    for spec in races_map.values():
        k = spec.get("kalshi") or {}
        ev = _margin_event(m, k, k_mov) if k else None
        for mk in (ev or {}).get("markets", []):
            k_tickers.append(mk["ticker"])
            event_of[mk["ticker"]] = mk["event_ticker"]
    tokens = []
    for e in pm_ev.values():
        for mk in e.get("markets", []):
            if _pm_live(mk) and _pm_token(mk):
                tokens.append(_pm_token(mk))
    k_hist, k_err = kalshi_history(k_tickers, event_of)
    p_hist, p_err = polymarket_history(tokens)
    res = _build(m, st, Pricer(k_hist, p_hist))
    complete = (not k_err and not p_err and not st["status"]["kalshi"]["stale"]
                and not st["status"]["polymarket"]["stale"])
    res.update(baseline_ts=_iso(BASELINE_TS), built_at=_iso(), complete=complete, method=_METHOD,
               history={"kalshi": {"markets": len(k_tickers),
                                   "priced": sum(1 for v in k_hist.values() if v[0] is not None),
                                   "errors": k_err[:10]},
                        "polymarket": {"markets": len(tokens),
                                       "priced": sum(1 for v in p_hist.values() if v[0] is not None),
                                       "errors": p_err[:10]}})
    if write:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(res, indent=1) + "\n")
    return res


def _baseline_usable(d) -> bool:
    if not d or d.get("baseline_ts") != _iso(BASELINE_TS):
        return False
    if d.get("complete"):
        return True  # frozen
    try:  # incomplete (e.g. Kalshi 429 during the build): retry at most every 10 min
        built = _dt.datetime.fromisoformat(d["built_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return False
    return _now() - built < BASELINE_RETRY


def baseline() -> dict:
    """The frozen pre-day expectation: read from BASELINE_PATH, built once if missing.
    A build that missed some history (rate limit) is marked complete=false and is retried
    (only the missing pieces are requested; history responses are cached on disk)."""
    d = _read_json(BASELINE_PATH)
    if _baseline_usable(d):
        return d
    with _lock:
        d = _read_json(BASELINE_PATH)
        if _baseline_usable(d):
            return d
        return build_baseline()


# ------------------------------------------------------------------ fetch (live + baseline)
def live() -> dict:
    """Current prices only (same shape as the baseline)."""
    with _lock:
        m = resolve(load_map())
        st = _structure(m)
        res = _build(m, st, LIVE)
    res.update(as_of=_iso(), status=st["status"], method=_METHOD)
    return res


def fetch() -> dict:
    """Baseline expectation + live prices. Per race: p_dem / implied_margin /
    margin_brackets / sources are the BASELINE (pre-day) values; *_now are live;
    move = implied_margin_now - implied_margin."""
    base = baseline()
    now = live()
    races = {}
    for rid, r in now["races"].items():
        b = base.get("races", {}).get(rid) or {}
        im, imn = b.get("implied_margin"), r["implied_margin"]
        races[rid] = {
            "p_dem": b.get("p_dem"), "implied_margin": im,
            "margin_brackets": b.get("margin_brackets"),
            "margin_brackets_source": b.get("margin_brackets_source"),
            "margin_from_p": b.get("margin_from_p"), "margin_method": b.get("margin_method"),
            "sources": b.get("sources") or {},
            "p_dem_now": r["p_dem"], "implied_margin_now": imn,
            "margin_brackets_now": r["margin_brackets"],
            "margin_brackets_source_now": r["margin_brackets_source"],
            "margin_from_p_now": r["margin_from_p"], "margin_method_now": r["margin_method"],
            "sources_now": r["sources"],
            "move": _r(imn - im, 2) if im is not None and imn is not None else None,
        }
    national = dict(base.get("national") or {})
    for k, v in now["national"].items():
        national[k + "_now"] = v
    for k in ("senate_dem_control", "house_dem_control", "house_popular_vote_margin"):
        a, b = national.get(k), national.get(k + "_now")
        national[k + "_move"] = _r(b - a, 4 if "control" in k else 2) if a is not None and b is not None else None
    return {"races": races, "national": national, "as_of": now["as_of"],
            "baseline_ts": base.get("baseline_ts"), "baseline_built_at": base.get("built_at"),
            "status": now["status"], "method": _METHOD}


# ------------------------------------------------------------------ CLI
def _fmt(x, spec="{:.2f}", none="-"):
    return none if x is None else spec.format(x)


def print_table(res: dict) -> None:
    try:
        cfg = {r["id"]: r for r in json.loads(CONFIG_PATH.read_text())["races"]}
    except (OSError, ValueError, KeyError):
        cfg = {}
    hdr = (f"{'race':<9}{'Kalshi p':>9}{'Poly p':>8}{'p_dem':>7}  {'brackets':<9}"
           f"{'impl.mgn':>9}{'from p':>8}{'poll fcst':>10}{'mkt-poll':>9}")
    print(hdr)
    print("-" * len(hdr))
    for rid, r in res["races"].items():
        s = r["sources"]
        kb = "K" if (s.get("kalshi") or {}).get("brackets") else ""
        pb = "P" if (s.get("polymarket") or {}).get("brackets") else ""
        fc = (cfg.get(rid) or {}).get("forecast_margin")
        im = r["implied_margin"]
        diff = im - fc if (im is not None and fc is not None) else None
        print(f"{rid:<9}{_fmt((s.get('kalshi') or {}).get('p_dem'), '{:.3f}'):>9}"
              f"{_fmt((s.get('polymarket') or {}).get('p_dem'), '{:.3f}'):>8}"
              f"{_fmt(r['p_dem'], '{:.3f}'):>7}  {(kb + pb) or 'no':<9}"
              f"{_fmt(im, '{:+.1f}'):>9}{_fmt(r['margin_from_p'], '{:+.1f}'):>8}"
              f"{_fmt(fc, '{:+.1f}'):>10}{_fmt(diff, '{:+.1f}'):>9}")
    n = res["national"]
    print()
    print(f"Senate D control {_fmt(n.get('senate_dem_control'), '{:.3f}')}   "
          f"House D control {_fmt(n.get('house_dem_control'), '{:.3f}')}   "
          f"House popular vote (D-R) {_fmt(n.get('house_popular_vote_margin'), '{:+.1f}')}")
    try:
        gb = json.loads(CONFIG_PATH.read_text())["national"]["generic_ballot_margin"]
        print(f"config generic_ballot_margin {gb:+.1f}")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    st = res["status"]
    for k in ("kalshi", "polymarket"):
        x = st.get(k) or {}
        flag = " STALE" if x.get("stale") else ""
        print(f"{k}: updated {x.get('updated')}{flag}" + (f"  ({x['error']})" if x.get("error") else ""))
    print("brackets: K = Kalshi, P = Polymarket margin-of-victory brackets; "
          "'from p' = SIGMA*NormalInverse(p_dem)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Prediction-market expectations (Kalshi + Polymarket)")
    ap.add_argument("--resolve", action="store_true", help="retry Kalshi ticker discovery now")
    ap.add_argument("--json", action="store_true", help="print the full fetch() result")
    ap.add_argument("--snapshot", nargs="?", const=str(SNAPSHOT_PATH), default=None,
                    help=f"also write the result to PATH (default {SNAPSHOT_PATH})")
    a = ap.parse_args()
    if a.resolve:
        resolve(load_map(), force=True)
    res = fetch()
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        print_table(res)
    if a.snapshot:
        Path(a.snapshot).parent.mkdir(parents=True, exist_ok=True)
        Path(a.snapshot).write_text(json.dumps(res, indent=1) + "\n")
        print(f"snapshot -> {a.snapshot}")


if __name__ == "__main__":
    main()
