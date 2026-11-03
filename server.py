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

from . import engine

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
            true_m = expected + self.shift + r.gauss(0, 2.5)
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
    return snap


def history_row(snap: dict) -> dict:
    return {"ts": snap["ts"], "overall": snap["overall"],
            "races": {r["id"]: {k: r.get(k) for k in ("vs_expect", "firm_vs_expect", "projected",
                                                       "projected_vs_forecast", "est_in", "votes",
                                                       "swing24", "swing22", "current_margin",
                                                       "adj_vs_expect", "adj_firm_vs_expect", "adj_projected",
                                                       "adj_est_in", "implied_turnout_vs_2022")}
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
