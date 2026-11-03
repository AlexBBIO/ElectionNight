"""Election-night poller + local dashboard.

  python3 -m election_night.server                 # live: poll feeds, dashboard on http://127.0.0.1:8765
  python3 -m election_night.server --demo          # synthetic night from baselines (D +2 vs polls) to test
  python3 -m election_night.server --once          # one poll, print summary table, exit
  python3 -m election_night.server --races KY-SEN,FL-GOV --port 8800

Writes election_night/out/latest.json and appends election_night/out/history.jsonl each cycle.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import importlib
import json
import random
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import checkins_model, engine

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"

STATE = {"snapshot": None, "history": []}
LOCK = threading.Lock()


# ---------------------------------------------------------------- feeds
def live_fetch(race: dict, election: str) -> list[dict]:
    mod = importlib.import_module(f"election_night.feeds.{race['feed']}")
    return mod.fetch(race["race"], election)


class DemoFeed:
    """Synthetic results built from the baselines: Dems beat expectations by `shift` points,
    counties start reporting over `span` seconds, early/mail batch first (D-leaning), then
    Election Day (R-leaning), so partial reads are biased the way they are on a real night."""

    def __init__(self, b: engine.Baselines, cfg: dict, shift: float = 2.0, span: float = 300.0, seed: int = 7):
        self.b, self.cfg, self.shift, self.span = b, cfg, shift, span
        self.t0 = time.time()
        self.seed = seed
        self.plan: dict[tuple, dict] = {}

    def _county_plan(self, race: dict, fips: str, expected: float, turnout: float, order: int) -> dict:
        key = (race["id"], fips)
        if key not in self.plan:
            r = random.Random(f"{self.seed}|{race['id']}|{fips}")  # deterministic per county
            # type-specific true over-performance (suburbs move more than rural areas) so the
            # county-type reweighting has something to correct in the demo
            tshift = {"Rural": -1.5, "Exurban / working metro": 1.5, "College suburbs": 5.0,
                      "Urban core": 3.0, "Black": 2.0, "Hispanic": 3.0}.get(self.b.types.get(fips), 0.0)
            true_m = expected + self.shift + tshift + r.gauss(0, 2.5)
            self.plan[key] = {
                "start": order * 20 + r.uniform(0, self.span * 0.5),
                "dur": r.uniform(self.span * 0.2, self.span * 0.6),
                "early_share": r.uniform(0.35, 0.6), "early_bias": r.uniform(4, 12),
                "true_m": true_m, "turnout": turnout * r.uniform(0.9, 1.1),
            }
        return self.plan[key]

    def fetch(self, race: dict, election: str) -> list[dict]:
        res = engine.analyze_race(race, [], self.b, self.cfg)
        el = time.time() - self.t0
        order = [r["id"] for r in self.cfg["races"]].index(race["id"])
        with_modes = order % 2 == 0  # odd races exercise the precinct-inference path
        rows = []
        for c in res["counties"]:
            p = self._county_plan(race, c["fips"], c["expected"], c["turnout_exp"], order)
            frac = 0.0 if el < p["start"] else min(1.0, 0.15 + (el - p["start"]) / p["dur"])
            base = {"state": race["state"], "county": c["county"], "fips": c["fips"],
                    "dem_name": "Demo Dem", "rep_name": "Demo Rep", "precincts_total": 20}
            if frac <= 0:
                rows.append({**base, "dem": 0, "rep": 0, "total": 0, "precincts_reporting": 0})
                continue
            T = p["turnout"]
            es = c["s"] if c.get("s") else p["early_share"]
            bias = c["delta"] if c.get("delta") is not None else p["early_bias"]
            m_early = p["true_m"] + bias * (1 - es)
            m_day = m_early - bias
            early_v = min(frac, es) * T                     # pre votes post first
            day_frac = max(0.0, frac - es) / (1 - es)
            day_v = day_frac * (1 - es) * T

            def split(v, mm):
                two = v * 0.98
                return {"dem": int(max((two + v * mm / 100) / 2, 0)), "rep": int(max((two - v * mm / 100) / 2, 0)),
                        "total": int(v)}
            pre, day = split(early_v, m_early), split(day_v, m_day)
            row = {**base, "dem": pre["dem"] + day["dem"], "rep": pre["rep"] + day["rep"],
                   "total": pre["total"] + day["total"], "precincts_reporting": int(20 * day_frac)}
            if with_modes:
                row["modes"] = {"pre": pre, "eday": day}
            rows.append(row)
        return rows


# ---------------------------------------------------------------- loop
def poll_once(cfg: dict, b: engine.Baselines, fetcher) -> dict:
    races = cfg["races"]
    results = [None] * len(races)

    def work(i, race):
        t = time.time()
        try:
            rows = fetcher(race, cfg["election"])
            res = engine.analyze_race(race, rows, b, cfg)
            res["status"] = "ok" if res["votes"] else "no votes yet"
        except ModuleNotFoundError:
            res = engine.analyze_race(race, [], b, cfg)
            res["status"] = f"no feed module '{race['feed']}'"
        except Exception as exc:  # noqa: BLE001
            res = engine.analyze_race(race, [], b, cfg)
            res["status"] = f"error: {type(exc).__name__}: {exc}"[:300]
            traceback.print_exc()
        res["fetch_seconds"] = round(time.time() - t, 2)
        results[i] = res

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda a: work(*a), enumerate(races)))
    snap = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "overall": engine.overall(results, cfg), "races": results,
            "national": cfg["national"], "firm_threshold": cfg.get("firm_threshold", 0.9)}
    try:
        snap["checkins"] = checkins_model.summary(live=cfg.get("checkins_live", cfg.get("turnout_mode", "live") != "off"),
                                                  save=cfg.get("checkins_save", True))
    except Exception as exc:  # noqa: BLE001
        snap["checkins"] = {"error": str(exc), "jurisdictions": []}
    # state view: early vote + Election Day so far, by party, vs 2022 early + 2022 final ED
    for pnl in (snap["checkins"].get("panels") or []):
        if not pnl.get("is_state"):
            continue
        t = next((r["turnout"] for r in results if r["state"] == pnl["state"] and r.get("turnout")), None)
        if not t or not t.get("party_2026"):
            continue
        l, e26 = pnl["latest"], t["party_2026"]  # e*: [dem, rep, oth]; finals: [rep, dem, oth]
        g = lambda x: 100 * (x[0] - x[1]) / sum(x) if sum(x) else None
        c26 = [e26[0] + l["dem"], e26[1] + l["rep"], e26[2] + l["oth"]]
        comb = {"mix_2026": g(c26), "votes_2026": sum(c26), "early_mix_2026": g(e26)}
        for y in (2022, 2024):
            fin, ey = pnl["finals"].get(str(y)), t.get(f"party_{y}")
            if not fin or not ey:
                continue
            cy = [ey[0] + fin[1], ey[1] + fin[0], ey[2] + fin[2]]
            # projection: rest of Election Day at that year's final ED volume, with today's ED mix
            k = max(1.0, sum(fin) / l["total"]) if l["total"] else 1.0
            p26 = [e26[0] + l["dem"] * k, e26[1] + l["rep"] * k, e26[2] + l["oth"] * k]
            comb[f"mix_{y}_final"] = g(cy); comb[f"votes_{y}"] = sum(cy)
            comb[f"d_{y % 100}"] = g(c26) - g(cy)
            comb[f"proj_mix_{y % 100}"] = g(p26); comb[f"proj_d_{y % 100}"] = g(p26) - g(cy)
        pnl["combined"] = comb
    try:
        attach_markets(results, snap, cfg)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        turnout_vs_expectation(results, snap, b)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return snap


_MK: tuple[float, dict] = (0.0, {})


def attach_markets(results: list[dict], snap: dict, cfg: dict) -> None:
    """Market expectation = prices going INTO the day (frozen baseline, data/markets_baseline.json,
    built from price history at 06:00 ET), so intraday moves driven by results don't leak into the
    expectation. Live prices are shown only as context ("market move today").
    rw_vs_market = projected margin (adjusted + reweighted) - baseline market-implied margin."""
    global _MK
    mode = cfg.get("markets_mode", "live")
    base, live = {}, {}
    if mode != "off":
        if time.time() - _MK[0] < 60:
            base, live = _MK[1]
        else:
            p = HERE / "data" / "markets_baseline.json"
            try:
                base = json.loads(p.read_text()) if p.exists() else {}
            except ValueError:
                base = {}
            if mode == "live":
                try:
                    mod = importlib.import_module("election_night.feeds.markets")
                    if not base and hasattr(mod, "baseline"):
                        base = mod.baseline() or {}
                    live = mod.fetch() or {}
                except Exception:  # noqa: BLE001
                    live = {}
            _MK = (time.time(), (base, live))
    braces, lraces = (base or {}).get("races") or {}, (live or {}).get("races") or {}
    num = den = 0.0
    for r in results:
        b_, l_ = braces.get(r["id"]) or {}, lraces.get(r["id"]) or {}
        r["market_margin"] = b_.get("implied_margin")
        r["market_p_dem"] = b_.get("p_dem")
        now = l_.get("implied_margin_now")
        r["market_margin_now"] = now
        r["market_move"] = None if (now is None or r["market_margin"] is None) else now - r["market_margin"]
        r["rw_vs_market"] = None
        if r["market_margin"] is not None and r.get("rw_projected") is not None:
            r["rw_vs_market"] = r["rw_projected"] - r["market_margin"]
            num += r["rw_vs_market"] * r["votes"]; den += r["votes"]
    snap["overall"]["rw_vs_market"] = num / den if den else None
    snap["markets"] = {"national": (base or {}).get("national"), "national_now": (live or {}).get("national"),
                       "baseline_ts": (base or {}).get("baseline_ts"), "races": len(braces)}


def turnout_vs_expectation(results: list[dict], snap: dict, b) -> None:
    """Is turnout delivering what the polls (and markets) expect?

    Registration mix G (D-R share of the electorate) is not a vote margin: independents and
    party-switchers decide the rest. For each race:
        needed  = E - G_2026       net D margin the polls need from independents/crossovers
        was_y   = M_y - G_y        what that net actually was in year y (actual result - mix)
    If needed >> was_y, the expectation relies on unusual persuasion (turnout is under-delivering
    for Democrats); if needed << was_y, turnout alone is beating expectations.
    Full electorate (early + Election Day projected) where check-ins exist; else early vote only,
    with E converted to an expected early-vote margin: E + (1 - s) * lean(early - ED).
    """
    panels = {p["state"]: p for p in (snap.get("checkins") or {}).get("panels") or [] if p.get("is_state")}
    research = engine.load_research()
    for r in results:
        t = r.get("turnout") or {}
        out = {}
        E = r.get("forecast")
        mk = r.get("market_margin")
        st, office = r["state"], r.get("baseline_2022_office")
        # actual statewide margins: 2024 president, 2022 baseline office
        m24 = b.state_pres_margin(st)
        agg = [0, 0, 0]
        for f in b.state_counties(st):
            c = b.c22.get((f, office))
            if c:
                agg = [x + y for x, y in zip(agg, c)]
        m22 = engine.margin(*agg) if agg[2] else None
        cb = (panels.get(st) or {}).get("combined")
        if cb and cb.get("proj_mix_24") is not None:
            G = cb["proj_mix_24"]
            out["full"] = {"label": "early + Election Day (projected)", "mix": G, "expect": E, "needed": E - G,
                           "market_needed": None if mk is None else mk - G,
                           "was_24": None if cb.get("mix_2024_final") is None else m24 - cb["mix_2024_final"],
                           "was_22": None if (cb.get("mix_2022_final") is None or m22 is None) else m22 - cb["mix_2022_final"]}
        if t.get("party_gap_2026") is not None:
            vt = r.get("vote_type") or {}
            s_, d_ = vt.get("pre_share"), vt.get("delta")
            if s_ is not None and d_ is not None:
                Epre = E + (1 - s_) * d_
                res = research.get(st, {}) or {}
                pre24 = (res.get("margin_2024") or {}).get("pre")
                # 2022 early-vote margin from county vote-type data, else research
                pre = [0, 0, 0]
                for f in b.state_counties(st):
                    mm = b.c22_modes.get((f, office))
                    if mm and mm.get("pre"):
                        pre = [x + y for x, y in zip(pre, mm["pre"])]
                pre22 = engine.margin(*pre) if pre[2] else (res.get("margin_2022") or {}).get("pre")
                G = t["party_gap_2026"]
                out["early"] = {"label": "early vote", "mix": G, "expect": Epre, "needed": Epre - G,
                                "market_needed": None if mk is None else (mk + (1 - s_) * d_) - G,
                                "was_24": None if (pre24 is None or t.get("party_gap_2024") is None) else pre24 - t["party_gap_2024"],
                                "was_22": None if (pre22 is None or t.get("party_gap_2022") is None) else pre22 - t["party_gap_2022"]}
        r["turnout_vs_exp"] = out or None
        # one turnout read per race: full electorate if available, else early vote; 2024 persuasion
        x = out.get("full") or out.get("early")
        if x and x.get("was_24") is not None:
            T = x["mix"] + x["was_24"]
            r["turnout_implied"] = T
            r["turnout_vs_polls"] = T - x["expect"]
            r["turnout_vs_markets"] = None if x.get("market_needed") is None else T - (x["mix"] + x["market_needed"])
            r["turnout_basis"] = x["label"]
    # aggregate across states (one race per state, weighted by electorate size)
    seen, acc = set(), {"turnout_vs_polls": [0.0, 0.0], "turnout_vs_markets": [0.0, 0.0]}
    for r in results:
        if r["state"] in seen or r.get("turnout_vs_polls") is None:
            continue
        seen.add(r["state"])
        w = (r.get("turnout") or {}).get("pre_ballots") or 1.0
        for k in acc:
            if r.get(k) is not None:
                acc[k][0] += r[k] * w; acc[k][1] += w
    for k, (a_, b_) in acc.items():
        snap["overall"][k] = a_ / b_ if b_ else None
    snap["overall"]["turnout_vs_states"] = sorted(seen)


def history_row(snap: dict) -> dict:
    return {"ts": snap["ts"], "overall": snap["overall"],
            "checkins": {k: (snap.get("checkins") or {}).get(k) for k in ("d_gap_22", "d_gap_24")},
            "races": {r["id"]: {k: r.get(k) for k in ("vs_expect", "firm_vs_expect", "projected",
                                                       "projected_vs_forecast", "est_in", "votes",
                                                       "swing24", "swing22", "current_margin",
                                                       "adj_vs_expect", "adj_firm_vs_expect", "adj_projected",
                                                       "adj_est_in", "implied_turnout_vs_2022", "rw_vs_expect", "rw_projected", "rw_vs_market")}
                      for r in snap["races"]}}


def loop(cfg, b, fetcher, interval, demo):
    OUT.mkdir(exist_ok=True)
    hist_path = OUT / ("history_demo.jsonl" if demo else "history.jsonl")
    if not demo and hist_path.exists():
        with open(hist_path) as fh:
            STATE["history"] = [json.loads(l) for l in fh if l.strip()][-2000:]
    while True:
        snap = poll_once(cfg, b, fetcher)
        snap["demo"] = demo
        h = history_row(snap)
        with LOCK:
            STATE["snapshot"] = snap
            STATE["history"].append(h)
            STATE["history"] = STATE["history"][-2000:]
        (OUT / ("latest_demo.json" if demo else "latest.json")).write_text(json.dumps(snap))
        with open(hist_path, "a") as fh:
            fh.write(json.dumps(h) + "\n")
        print_summary(snap)
        time.sleep(interval)


def fmt(x, nd=1, signed=True):
    if x is None:
        return "—"
    if signed:
        return ("D+" if x >= 0 else "R+") + f"{abs(x):.{nd}f}"
    return f"{x:.{nd}f}"


def print_summary(snap):
    o = snap["overall"]
    print(f"\n[{snap['ts']}] overall vs expectations: {fmt(o['vs_expect'])}  firm: {fmt(o['firm_vs_expect'])}  "
          f"implied generic: {fmt(o['implied_generic'])} (polls {fmt(o['generic_ballot'])})")
    print(f"{'race':<12}{'in%':>6}{'cur':>8}{'vsExp':>8}{'firm':>8}{'sw24':>8}{'sw22':>8}{'proj':>8}{'fcst':>8}  status")
    for r in snap["races"]:
        print(f"{r['id']:<12}{r['est_in']*100:>5.0f}%{fmt(r['current_margin']):>8}{fmt(r['vs_expect']):>8}"
              f"{fmt(r['firm_vs_expect']):>8}{fmt(r['swing24']):>8}{fmt(r['swing22']):>8}"
              f"{fmt(r['projected']):>8}{fmt(r['forecast']):>8}  {r.get('status', '')}")


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/api/checkins"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                row = json.loads(self.rfile.read(n) or b"{}")
                for k in ("jurisdiction", "year", "hour_local", "rep", "dem"):
                    if not str(row.get(k, "")).strip():
                        raise ValueError(f"missing {k}")
                row.setdefault("as_of", datetime.now(timezone.utc).isoformat(timespec="minutes"))
                row.setdefault("source", "manual entry")
                checkins_model.append_manual(row)
                self._send(200, b'{"ok":true}', "application/json")
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/state"):
            with LOCK:
                body = json.dumps({"snapshot": STATE["snapshot"], "history": STATE["history"]})
            self._send(200, body.encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--demo-shift", type=float, default=2.0, help="demo: true D over-performance vs expectations")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--races", help="comma-separated race ids from config.json")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float)
    ap.add_argument("--config", type=Path)
    a = ap.parse_args()

    cfg = engine.load_config(a.config)
    if a.races:
        keep = set(a.races.split(","))
        cfg["races"] = [r for r in cfg["races"] if r["id"] in keep]
    b = engine.Baselines.load()
    if a.demo:
        cfg["turnout_mode"] = "off"
        cfg["markets_mode"] = "off"
    fetcher = DemoFeed(b, cfg, shift=a.demo_shift).fetch if a.demo else live_fetch
    interval = a.interval or (3 if a.demo else cfg.get("poll_seconds", 30))

    if a.once:
        print_summary(poll_once(cfg, b, fetcher))
        return
    threading.Thread(target=loop, args=(cfg, b, fetcher, interval, a.demo), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"dashboard: http://127.0.0.1:{a.port}  ({'DEMO' if a.demo else 'LIVE'}, every {interval}s)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
