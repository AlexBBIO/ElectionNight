"""Build county-level baselines for the election-night comparison.

Outputs (committed, small):
  election_night/data/pres_2024.csv      state, fips, county, dem, rep, total
  election_night/data/county_2022.csv    state, fips, county, office, district, special, mode, dem, rep, total

Sources:
  2024 president: tonmcg/US_County_Level_Election_Results_08-24 (county CSV)
  2022 Senate / Governor / US House: MIT Election Data + Science Lab
      MEDSL/2022-elections-official, per-state precinct files aggregated to county.

Run:  python3 -m election_night.build_baselines
"""
from __future__ import annotations

import collections
import csv
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = HERE / "cache" / "baselines"
UA = {"User-Agent": "factor-watch-election-night/0.1"}

PRES_2024_URL = ("https://raw.githubusercontent.com/tonmcg/US_County_Level_Election_Results_08-24/"
                 "master/2024_US_County_Level_Presidential_Results.csv")
MEDSL_2022_URL = ("https://raw.githubusercontent.com/MEDSL/2022-elections-official/main/"
                  "individual_states/2022-{st}-local-precinct-general.zip")

STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
          "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
STATE_NAME_TO_PO = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
OFFICES = {"US SENATE", "GOVERNOR", "US HOUSE"}


def _get(url: str, dest: Path) -> Path | None:
    if dest.exists() and dest.stat().st_size > 100:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
            dest.write_bytes(r.read())
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {url}: {exc}", file=sys.stderr)
        return None
    return dest


def build_pres_2024() -> int:
    path = _get(PRES_2024_URL, CACHE / "pres_2024.csv")
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            st = STATE_NAME_TO_PO.get(r["state_name"])
            if not st:
                continue
            rows.append({"state": st, "fips": r["county_fips"].zfill(5), "county": r["county_name"],
                         "dem": int(float(r["votes_dem"])), "rep": int(float(r["votes_gop"])),
                         "total": int(float(r["total_votes"]))})
    _write(DATA / "pres_2024.csv", rows, ["state", "fips", "county", "dem", "rep", "total"])
    return len(rows)


def _county_fips(row: dict) -> str:
    f = (row.get("county_fips") or "").strip()
    if f and f not in ("NA",):
        return f.split(".")[0].zfill(5)
    return ""


def build_2022() -> int:
    out: dict[tuple, list[int]] = collections.defaultdict(lambda: [0, 0, 0])
    names: dict[tuple, str] = {}
    for st in STATES:
        z = _get(MEDSL_2022_URL.format(st=st.lower()), CACHE / f"medsl_2022_{st}.zip")
        if not z:
            continue
        with zipfile.ZipFile(z) as zf:
            member = next(n for n in zf.namelist() if n.endswith(".csv"))
            with zf.open(member) as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, "utf-8", errors="replace")):
                    office = (r["office"] or "").upper()
                    if office not in OFFICES or r.get("stage", "GEN") not in ("GEN", ""):
                        continue
                    fips = _county_fips(r)
                    if not fips:
                        continue
                    try:
                        v = int(float(r["votes"] or 0))
                    except ValueError:
                        continue
                    district = r["district"] if office == "US HOUSE" else "STATEWIDE"
                    special = "1" if (r.get("special") or "").upper() == "TRUE" else "0"
                    key = (st, fips, office, district, special, (r["mode"] or "TOTAL").upper())
                    names.setdefault((st, fips), (r["county_name"] or "").title())
                    party = (r["party_simplified"] or "").upper()
                    acc = out[key]
                    if party == "DEMOCRAT":
                        acc[0] += v
                    elif party == "REPUBLICAN":
                        acc[1] += v
                    acc[2] += v
        print(f"  {st} ok", file=sys.stderr)
    rows = [{"state": k[0], "fips": k[1], "county": names[(k[0], k[1])], "office": k[2], "district": k[3],
             "special": k[4], "mode": k[5], "dem": v[0], "rep": v[1], "total": v[2]}
            for k, v in sorted(out.items())]
    _write(DATA / "county_2022.csv", rows,
           ["state", "fips", "county", "office", "district", "special", "mode", "dem", "rep", "total"])
    return len(rows)


def _write(path: Path, rows: list[dict], cols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path.relative_to(HERE.parent)} ({len(rows)} rows)", file=sys.stderr)


def patch_indiana() -> None:
    """MEDSL's 2022 Indiana file covers only ~35 of 92 counties; replace IN Senate / SoS / House
    with county totals from Indiana's official results archive (matches certified totals)."""
    import importlib.util
    if "election_night" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "election_night", HERE / "__init__.py", submodule_search_locations=[str(HERE)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["election_night"] = pkg
        spec.loader.exec_module(pkg)
    from election_night.feeds import in_
    path = DATA / "county_2022.csv"
    cols = ["state", "fips", "county", "office", "district", "special", "mode", "dem", "rep", "total"]
    with open(path, newline="") as fh:
        rows = [r for r in csv.DictReader(fh)
                if not (r["state"] == "IN" and r["office"] in ("US SENATE", "US HOUSE", "SECRETARY OF STATE"))]
    for race, office in (("U.S. Senate", "US SENATE"), ("Secretary of State", "SECRETARY OF STATE"),
                         ("U.S. House", "US HOUSE")):
        for r in in_.fetch(race, "2022-11-08"):
            rows.append({"state": "IN", "fips": r["fips"], "county": r["county"], "office": office,
                         "district": "ALL" if office == "US HOUSE" else "STATEWIDE", "special": "0",
                         "mode": "TOTAL", "dem": r["dem"], "rep": r["rep"], "total": r["total"]})
    _write(path, rows, cols)


RUCC_URL = "https://ers.usda.gov/sites/default/files/_laserfiche/DataFiles/53251/Ruralurbancontinuumcodes2023.csv"
EDU_URL = "https://ers.usda.gov/sites/default/files/_laserfiche/DataFiles/48747/Education2023.csv"
RACE_URL = ("https://www2.census.gov/programs-surveys/popest/datasets/2020-2023/counties/asrh/"
            "cc-est2023-alldata.csv")
TYPE_ORDER = ["Hispanic", "Black", "Urban core", "College suburbs", "Exurban / working metro", "Rural"]


def county_type(c: dict) -> str:
    """Precedence: Hispanic >= 30%; large urban core (RUCC 1, pop >= 750k); Black >= 35%;
    metro with >= 35% bachelor's; other metro; nonmetro (RUCC >= 4)."""
    if c["hisp"] >= 30:
        return "Hispanic"
    if c["rucc"] == 1 and c["pop"] >= 750_000:
        return "Urban core"
    if c["black"] >= 35:
        return "Black"
    if c["rucc"] <= 3 and c["college"] >= 35:
        return "College suburbs"
    if c["rucc"] <= 3:
        return "Exurban / working metro"
    return "Rural"


def build_county_types() -> int:
    rucc = _get(RUCC_URL, CACHE / "rucc2023.csv")
    edu = _get(EDU_URL, CACHE / "education2023.csv")
    race = _get(RACE_URL, CACHE / "cc-est2023-alldata.csv")
    c: dict[str, dict] = collections.defaultdict(dict)
    with open(rucc, newline="", encoding="latin-1") as fh:
        for r in csv.DictReader(fh):
            f = r["FIPS"].zfill(5)
            if r["Attribute"] == "RUCC_2023" and r["Value"]:
                c[f]["rucc"] = int(r["Value"])
            elif r["Attribute"] == "Population_2020" and r["Value"]:
                c[f]["pop"] = int(float(r["Value"]))
    with open(edu, newline="", encoding="latin-1") as fh:
        for r in csv.DictReader(fh):
            if r["Attribute"] == "Percent of adults with a bachelor's degree or higher, 2019-23":
                c[r["FIPS Code"].zfill(5)]["college"] = float(r["Value"] or 0)
    with open(race, newline="", encoding="latin-1") as fh:
        for r in csv.DictReader(fh):
            if r["YEAR"] != "5" or r["AGEGRP"] != "0":
                continue
            f = r["STATE"].zfill(2) + r["COUNTY"].zfill(3)
            tot = int(r["TOT_POP"]) or 1
            c[f]["pop"] = tot
            c[f]["black"] = 100 * (int(r["BA_MALE"]) + int(r["BA_FEMALE"])) / tot
            c[f]["hisp"] = 100 * (int(r["H_MALE"]) + int(r["H_FEMALE"])) / tot
    rows = []
    for f, x in sorted(c.items()):
        if not all(k in x for k in ("rucc", "college", "black", "hisp", "pop")):
            continue
        rows.append({"fips": f, "type": county_type(x), "pop": x["pop"], "rucc": x["rucc"],
                     "college": round(x["college"], 1), "black": round(x["black"], 1), "hisp": round(x["hisp"], 1)})
    _write(DATA / "county_types.csv", rows, ["fips", "type", "pop", "rucc", "college", "black", "hisp"])
    return len(rows)


if __name__ == "__main__":
    build_pres_2024()
    build_2022()
    patch_indiana()
    build_county_types()
