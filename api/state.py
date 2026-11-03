"""Vercel serverless endpoint: GET /api/state[?demo=1][&races=KY-SEN,FL-GOV]

Polls the state feeds on demand and returns the same snapshot the local server builds.
Responses are CDN-cached for CACHE_SECONDS so upstream election sites are hit at most
about once per window no matter how many people have the dashboard open. History is kept
client-side by the dashboard (serverless instances have no durable state).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if "election_night" not in sys.modules:  # load ROOT as the election_night package wherever it is deployed
    spec = importlib.util.spec_from_file_location(
        "election_night", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["election_night"] = pkg
    spec.loader.exec_module(pkg)

from election_night import engine, server  # noqa: E402

CACHE_SECONDS = 20
DEMO_CYCLE = 600  # demo night replays every 10 minutes
_B = None
_MEMO: dict[str, tuple[float, bytes]] = {}


def build(query: dict) -> bytes:
    global _B
    cfg = engine.load_config()
    if query.get("races"):
        keep = set(query["races"][0].split(","))
        cfg["races"] = [r for r in cfg["races"] if r["id"] in keep]
    if _B is None:
        _B = engine.Baselines.load()
    demo = query.get("demo", ["0"])[0] not in ("0", "")
    # big turnout files don't fit the serverless time limit: use the committed snapshot there
    cfg["turnout_mode"] = "off" if demo else os.environ.get("TURNOUT_MODE", "snapshot")
    # check-in snapshots are pushed to data/checkins.csv every 30 min; read those, don't write
    cfg["checkins_live"] = os.environ.get("CHECKINS_LIVE", "0") == "1"
    cfg["checkins_save"] = False
    cfg["markets_mode"] = "off" if demo else os.environ.get("MARKETS_MODE", "live")
    # stay well inside the function time limit; KY and OH block cloud servers outright
    cfg["deadline_s"] = float(os.environ.get("FEED_DEADLINE_S", "18"))
    cfg["markets_deadline_s"] = float(os.environ.get("MARKETS_DEADLINE_S", "6"))
    cfg["skip_feeds"] = [] if demo else [x for x in os.environ.get("SKIP_FEEDS", "ky,oh").split(",") if x]
    if demo:
        feed = server.DemoFeed(_B, cfg, span=DEMO_CYCLE * 0.5)
        now = time.time()
        feed.t0 = now - (now % DEMO_CYCLE)
        fetcher = feed.fetch
    else:
        fetcher = server.live_fetch
    snap = server.poll_once(cfg, _B, fetcher)
    snap["demo"] = demo
    snap["serverless"] = True
    return json.dumps({"snapshot": snap, "history": None}).encode()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        raw = parse_qs(urlparse(self.path).query)
        # Only these params matter; ignoring the rest stops junk query strings from forcing fresh upstream fetches.
        q = {k: raw[k] for k in ("demo", "races") if k in raw}
        if "races" in q:
            q["races"] = [",".join(sorted(set(q["races"][0].split(","))))]
        key = json.dumps(sorted(q.items()))
        hit = _MEMO.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            body = hit[1]
        else:
            try:
                body = build(q)
                _MEMO[key] = (time.time(), body)
            except Exception as exc:  # noqa: BLE001
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode())
                return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", f"public, s-maxage={CACHE_SECONDS}, stale-while-revalidate={CACHE_SECONDS * 2}")
        self.end_headers()
        self.wfile.write(body)
