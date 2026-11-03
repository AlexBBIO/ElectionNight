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

    skip = set(cfg.get("skip_feeds") or [])

    def work(i, race):
        t = time.time()
        if race.get("feed") in skip:
            res = engine.analyze_race(race, [], b, cfg)
            res["status"] = "runs on the local version only (this state blocks cloud servers)"
            res["fetch_seconds"] = 0.0
            results[i] = res
            return
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

    # a hard deadline keeps the serverless endpoint inside its time limit: slow feeds are
    # reported as timed out for this poll instead of holding up the whole response
    deadline = cfg.get("deadline_s")
    ex = cf.ThreadPoolExecutor(max_workers=12)
    futs = [ex.submit(work, i, r) for i, r in enumerate(races)]
    cf.wait(futs, timeout=deadline)
    ex.shutdown(wait=False, cancel_futures=True)
    for i, race in enumerate(races):
        if results[i] is None:
            res = engine.analyze_race(race, [], b, cfg)
            res["status"] = f"timed out after {deadline:.0f}s this poll; retrying"
            res["fetch_seconds"] = deadline
            results[i] = res
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
            comb[f"vec26_{y % 100}"] = p26; comb[f"vec_{y}"] = cy
        pnl["combined"] = comb
    try:
        mx = cf.ThreadPoolExecutor(max_workers=1)
        fut = mx.submit(attach_markets, results, snap, cfg)
        try:
            fut.result(timeout=cfg.get("markets_deadline_s"))
        except cf.TimeoutError:
            cfg_b = dict(cfg, markets_mode="baseline")  # fall back to the frozen baseline only
            attach_markets(results, snap, cfg_b)
        mx.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    snap["_pi"] = cfg.get("turnout_share_of_swing", PI_DEFAULT)
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
            if mode in ("live", "baseline"):
                try:
                    mod = importlib.import_module("election_night.feeds.markets")
                    if not base and hasattr(mod, "baseline"):
                        base = mod.baseline() or {}
                    live = (mod.fetch() or {}) if mode == "live" else {}
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


LOYAL_D, LOYAL_R = 85.0, 88.0   # net margin among registered Democrats / Republicans who vote
PI_DEFAULT = 1 / 3              # assumed share of a swing that comes from turnout composition


def _delta_y(res: dict, vt: dict, year: int):
    """Early-minus-Election-Day margin gap for a given year (kept consistent with that year)."""
    if year == 2022 and vt.get("source", "").startswith("2022") and vt.get("delta") is not None:
        return vt["delta"]
    m = (res.get(f"margin_{year}") or {})
    pre, ed = m.get("pre"), m.get("eday")
    return None if pre is None or ed is None else pre - ed


def turnout_vs_expectation(results: list[dict], snap: dict, b) -> None:
    """How much of the swing that polls (or markets) expect is the electorate's party mix
    already delivering?  Everything for a comparison year y uses that year's numbers only.

      expected swing   = E - M_y        expected margin minus that year's actual margin
      from turnout     = G_2026 - G_y   change in the electorate's party-registration mix
      from persuasion  = the rest       what independents / party-switchers must supply
    Full electorate (early + Election Day projected to finish at that year's ED volume) where
    Election Day check-ins exist (FL); otherwise the early-vote electorate, where E is turned
    into an expected early-vote margin E + (1 - s) * gap_y and M_y is that year's early margin.
    Registration is not vote choice, and there is no reliable norm for how a swing splits
    between turnout and persuasion, so this is a decomposition, not a verdict.
    """
    panels = {p["state"]: p for p in (snap.get("checkins") or {}).get("panels") or [] if p.get("is_state")}
    research = engine.load_research()
    for r in results:
        t = r.get("turnout") or {}
        st, office, E, mk = r["state"], r.get("baseline_2022_office"), r.get("forecast"), r.get("market_margin")
        res = research.get(st, {}) or {}
        vt = r.get("vote_type") or {}
        agg = [0, 0, 0]
        for f in b.state_counties(st):
            c = b.c22.get((f, office))
            if c:
                agg = [x + y for x, y in zip(agg, c)]
        M = {2024: b.state_pres_margin(st), 2022: engine.margin(*agg) if agg[2] else None}
        cb = (panels.get(st) or {}).get("combined")
        out = {}
        if cb:
            basis = {}
            for y in (2024, 2022):
                k = y % 100
                Gy, G26 = cb.get(f"mix_{y}_final"), cb.get(f"proj_mix_{k}")
                if None in (Gy, G26, M[y]):
                    continue
                basis[y] = {"expected": E - M[y], "market_expected": None if mk is None else mk - M[y],
                            "turnout": G26 - Gy, "mix_2026": G26, "mix_y": Gy,
                            "vec26": cb.get(f"vec26_{k}"), "vecy": cb.get(f"vec_{y}"), "M_y": M[y]}
            if basis:
                out["full"] = {"label": "early + Election Day (projected)", "years": basis}
        if t.get("party_gap_2026") is not None and vt.get("pre_share") is not None:
            s_ = vt["pre_share"]
            pre = [0, 0, 0]
            for f in b.state_counties(st):
                mm = b.c22_modes.get((f, office))
                if mm and mm.get("pre"):
                    pre = [x + y for x, y in zip(pre, mm["pre"])]
            pre_m = {2022: engine.margin(*pre) if pre[2] else (res.get("margin_2022") or {}).get("pre"),
                     2024: (res.get("margin_2024") or {}).get("pre")}
            basis = {}
            for y in (2024, 2022):
                d_y, Gy = _delta_y(res, vt, y), t.get(f"party_gap_{y}")
                if None in (d_y, Gy, pre_m[y]):
                    continue
                shift = (1 - s_) * d_y
                basis[y] = {"expected": E + shift - pre_m[y],
                            "market_expected": None if mk is None else mk + shift - pre_m[y],
                            "turnout": t["party_gap_2026"] - Gy, "mix_2026": t["party_gap_2026"], "mix_y": Gy,
                            "vec26": t.get("party_2026"), "vecy": t.get(f"party_{y}"), "M_y": pre_m[y]}
            if basis:
                out["early"] = {"label": "early vote", "years": basis}
        PI = snap.get("_pi", 1 / 3)
        for v in out.values():
            for x in v["years"].values():
                x["persuasion"] = x["expected"] - x["turnout"]
                x["share"] = x["turnout"] / x["expected"] if abs(x["expected"]) > 0.5 else None
                # composition effect: value the change in party shares at group vote margins
                # (registered D ~ +85, registered R ~ -88, others solved to reproduce year y's result)
                a, z = x.pop("vec26"), x.pop("vecy")
                My = x.pop("M_y")
                if a and z and sum(a) and sum(z):
                    s26 = [q / sum(a) for q in a]; sy = [q / sum(z) for q in z]  # [dem, rep, oth]
                    m_oth = (My - LOYAL_D * sy[0] + LOYAL_R * sy[1]) / sy[2] if sy[2] > 0.02 else 0.0
                    m_oth = max(-60.0, min(60.0, m_oth))
                    eff = LOYAL_D * (s26[0] - sy[0]) - LOYAL_R * (s26[1] - sy[1]) + m_oth * (s26[2] - sy[2])
                    x["comp_effect"] = eff; x["m_oth_y"] = m_oth
                    x["vs_polls"] = eff - PI * x["expected"]
                    x["vs_polls_range"] = [eff - 0.5 * x["expected"], eff - 0.2 * x["expected"]]
                    if x.get("market_expected") is not None:
                        x["vs_markets"] = eff - PI * x["market_expected"]
                        x["vs_markets_range"] = [eff - 0.5 * x["market_expected"], eff - 0.2 * x["market_expected"]]
        r["turnout_split"] = out or None
    # headline: per state (first race with data), full electorate preferred, weighted by electorate
    seen, acc = set(), {y: [0.0, 0.0, 0.0] for y in (2024, 2022)}
    for r in results:
        ts = r.get("turnout_split")
        if not ts or r["state"] in seen:
            continue
        seen.add(r["state"])
        v = ts.get("full") or ts.get("early")
        w = (r.get("turnout") or {}).get("pre_ballots") or 1.0
        for y, x in v["years"].items():
            acc[y][0] += x["turnout"] * w; acc[y][1] += x["expected"] * w; acc[y][2] += w
    o = snap["overall"]
    for y, (tu, ex, w) in acc.items():
        o[f"turnout_part_{y % 100}"] = tu / w if w else None
        o[f"expected_swing_{y % 100}"] = ex / w if w else None
    o["turnout_split_states"] = sorted(seen)
    for y in (2022, 2024):
        for key in ("vs_polls", "vs_markets"):
            num = den = 0.0
            for r in results:
                ts = r.get("turnout_split")
                if not ts:
                    continue
                v = ts.get("full") or ts.get("early")
                x = v["years"].get(y) or {}
                if x.get(key) is None or r["state"] in o.get(f"_seen_{key}_{y}", set()):
                    continue
                o.setdefault(f"_seen_{key}_{y}", set()).add(r["state"])
                w = (r.get("turnout") or {}).get("pre_ballots") or 1.0
                num += x[key] * w; den += w
            o[f"turnout_{key}_{y % 100}"] = num / den if den else None
    for k in [k for k in o if k.startswith("_seen_")]:
        del o[k]
    o["turnout_pi"] = PI_DEFAULT if "_pi" not in snap else snap["_pi"]


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
