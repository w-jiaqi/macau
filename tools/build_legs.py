#!/usr/bin/env python3
"""Precompute road routes between every pair of route nodes.

The static site draws road-accurate routes for ANY itinerary without calling a
routing API at runtime: every possible leg between the fixed start (外港码头),
the built-in places and the fixed end (氹仔码头) is computed here once, with the
public OSRM servers at routing.openstreetmap.de, and stored in data/legs.json.

  drive  every ordered pair (a, b), a in {start} + places, b in places + {end},
         a != b, plus start -> end.  Each direction is requested separately
         because Macau has many one-way streets.
  walk   the same ordered pairs, but only where the straight-line distance is
         <= 2.5 km.  Each unordered pair is requested once (the reverse leg
         reuses the reversed geometry).  A walk leg is kept only if its length
         is <= 3.5 km and <= max(2.5 x straight-line, straight-line + 1 km):
         pedestrians cannot use the Macau-Taipa bridges, so the foot router
         makes huge detours there, while the 1 km slack keeps genuine short
         walks that wind up a hill or around a casino podium (塔石 -> 东望洋,
         威尼斯人 -> 伦敦人).  After changing these limits run with --force.

Geometries are simplified with Douglas-Peucker (~3 m, in local metres) and
stored as Google encoded polylines (precision 5).

Usage (from the repository root):

  pip install -r tools/requirements.txt
  python3 tools/build_legs.py              # incremental: only new / moved nodes
  python3 tools/build_legs.py --force      # refetch everything
  python3 tools/build_legs.py --dry-run    # just print what would be fetched

Incremental mode reuses every leg of the existing data/legs.json whose two
endpoint coordinates (the "nodes" table) are unchanged, so after adding a
place to data/places.json only the legs touching it are fetched.  Progress is
saved every few requests; if a run is interrupted or some requests fail, simply
run the script again.

Output (compact JSON):

  {"generated": "YYYY-MM-DD", "engine": "...", "attribution": "...",
   "nodes": {"<id>": [lat, lng], ...},
   "legs": {"<fromId>><toId>": {"drive": {"d": metres, "t": seconds,
                                          "g": "<polyline5>"},
                                "walk": {...}  # optional
                               }, ...}}

The route geometry starts / ends where OSRM snapped the node onto the road
network (for pedestrian-only sites such as 大三巴 the drive leg stops at the
nearest road), so the frontend may draw a short connector to the marker.

Routes (c) OpenStreetMap contributors, ODbL 1.0; routing by OSRM.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PLACES = DATA / "places.json"
OUT = DATA / "legs.json"

UA = "macau-tourist-map/1.0 (+https://github.com/w-jiaqi/macau)"
ENGINE = "OSRM routing.openstreetmap.de"
ATTRIBUTION = "路线 © OpenStreetMap 贡献者"
CAR_URL = "https://routing.openstreetmap.de/routed-car/route/v1/driving/{a};{b}"
FOOT_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/foot/{a};{b}"
PARAMS = {"overview": "full", "geometries": "geojson",
          "alternatives": "false", "steps": "false"}

WALK_MAX_STRAIGHT = 2500.0   # only try walking when the crow flies <= 2.5 km
WALK_MAX_DIST = 3500.0       # ... and keep it only if the route is <= 3.5 km
WALK_MAX_RATIO = 2.5         # ... and <= 2.5 x the straight-line distance
WALK_SLACK = 1000.0          # ... or <= straight-line + 1 km (short hilly walks)


def walk_ok(d: float, straight: float) -> bool:
    return d <= WALK_MAX_DIST and d <= max(WALK_MAX_RATIO * straight, straight + WALK_SLACK)

DELAY = 1.1                  # seconds between requests (single host)
TIMEOUT = 30
MAX_TRIES = 6
SAVE_EVERY = 25              # write progress every N requests
COORD_DP = 6                 # node coordinates are compared at 1e-6 degrees

R_EARTH = 6_371_008.8


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------
def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(h))


def douglas_peucker(pts: list[tuple[float, float]], tol_m: float) -> list[tuple[float, float]]:
    """Simplify [(lat, lng), ...] with tolerance in metres (local projection)."""
    n = len(pts)
    if n < 3:
        return list(pts)
    lat0 = sum(p[0] for p in pts) / n
    kx = math.cos(math.radians(lat0)) * math.pi * R_EARTH / 180.0
    ky = math.pi * R_EARTH / 180.0
    xy = [(p[1] * kx, p[0] * ky) for p in pts]
    keep = [False] * n
    keep[0] = keep[-1] = True
    tol2 = tol_m * tol_m
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = xy[i]
        bx, by = xy[j]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        best, best_k = -1.0, -1
        for k in range(i + 1, j):
            px, py = xy[k]
            if seg2 == 0.0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / seg2
                t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                qx, qy = ax + t * dx, ay + t * dy
                d2 = (px - qx) ** 2 + (py - qy) ** 2
            if d2 > best:
                best, best_k = d2, k
        if best > tol2:
            keep[best_k] = True
            stack.append((i, best_k))
            stack.append((best_k, j))
    return [p for p, k in zip(pts, keep) if k]


def _enc_value(v: int) -> str:
    v = ~(v << 1) if v < 0 else (v << 1)
    out = []
    while v >= 0x20:
        out.append(chr((0x20 | (v & 0x1F)) + 63))
        v >>= 5
    out.append(chr(v + 63))
    return "".join(out)


def encode_polyline(pts: list[tuple[float, float]], precision: int = 5) -> str:
    """Google encoded polyline of [(lat, lng), ...]."""
    f = 10 ** precision
    out = []
    plat = plng = 0
    for lat, lng in pts:
        ilat, ilng = int(round(lat * f)), int(round(lng * f))
        out.append(_enc_value(ilat - plat))
        out.append(_enc_value(ilng - plng))
        plat, plng = ilat, ilng
    return "".join(out)


def decode_polyline(s: str, precision: int = 5) -> list[tuple[float, float]]:
    f = 10 ** precision
    pts, idx, lat, lng = [], 0, 0, 0
    while idx < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lng += vals[1]
        pts.append((lat / f, lng / f))
    return pts


def pack_geometry(coords_lnglat: list[list[float]], tol_m: float) -> str:
    pts = [(c[1], c[0]) for c in coords_lnglat]
    # drop consecutive duplicates at polyline precision before simplifying
    dedup: list[tuple[float, float]] = []
    for p in pts:
        q = (round(p[0], 5), round(p[1], 5))
        if not dedup or (round(dedup[-1][0], 5), round(dedup[-1][1], 5)) != q:
            dedup.append(p)
    if len(dedup) == 1:
        dedup.append(dedup[0])
    return encode_polyline(douglas_peucker(dedup, tol_m))


def reverse_polyline(s: str) -> str:
    return encode_polyline(list(reversed(decode_polyline(s))))


# --------------------------------------------------------------------------
# OSRM client
# --------------------------------------------------------------------------
class NoRoute(Exception):
    pass


class Osrm:
    def __init__(self, delay: float):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.delay = delay
        self.last = 0.0
        self.count = 0

    def _wait(self) -> None:
        dt = time.monotonic() - self.last
        if dt < self.delay:
            time.sleep(self.delay - dt)
        self.last = time.monotonic()

    def route(self, url_tpl: str, a: tuple[float, float], b: tuple[float, float]) -> dict:
        url = url_tpl.format(a=f"{a[1]:.6f},{a[0]:.6f}", b=f"{b[1]:.6f},{b[0]:.6f}")
        backoff = 4.0
        last_err = ""
        for attempt in range(1, MAX_TRIES + 1):
            self._wait()
            self.count += 1
            try:
                r = self.s.get(url, params=PARAMS, timeout=TIMEOUT)
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    try:
                        js = r.json()
                    except ValueError:
                        last_err = "invalid JSON"
                    else:
                        if js.get("code") == "Ok" and js.get("routes"):
                            return js["routes"][0]
                        raise NoRoute(js.get("code", "?") + " " + js.get("message", ""))
                elif r.status_code == 400:
                    try:
                        js = r.json()
                    except ValueError:
                        js = {}
                    raise NoRoute(f"HTTP 400 {js.get('code', '')} {js.get('message', '')}")
                else:
                    last_err = f"HTTP {r.status_code}"
                    ra = r.headers.get("Retry-After")
                    if ra and ra.isdigit():
                        backoff = max(backoff, float(ra))
            if attempt < MAX_TRIES:
                print(f"    ! {last_err}; retry {attempt}/{MAX_TRIES - 1} in {backoff:.0f}s",
                      flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120.0)
        raise RuntimeError(f"giving up after {MAX_TRIES} tries: {last_err}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def load_nodes(path: Path) -> list[tuple[str, tuple[float, float], str]]:
    d = json.loads(path.read_text(encoding="utf-8"))
    nodes = [(d["start"]["id"], d["start"], "start")]
    nodes += [(p["id"], p, "place") for p in d["places"]]
    nodes.append((d["end"]["id"], d["end"], "end"))
    out, seen = [], set()
    for nid, p, role in nodes:
        if nid in seen:
            sys.exit(f"duplicate node id in {path}: {nid}")
        seen.add(nid)
        out.append((nid, (round(float(p["lat"]), COORD_DP), round(float(p["lng"]), COORD_DP)),
                    role))
    return out


def required_pairs(nodes) -> list[tuple[str, str]]:
    start = [n for n, _, r in nodes if r == "start"][0]
    end = [n for n, _, r in nodes if r == "end"][0]
    places = [n for n, _, r in nodes if r == "place"]
    pairs = []
    for a in [start] + places:          # includes start -> end
        for b in places + [end]:
            if a != b:
                pairs.append((a, b))
    return pairs


def write_out(path: Path, generated: str, nodes, legs: dict, order: list[str]) -> int:
    doc = {
        "generated": generated,
        "engine": ENGINE,
        "attribution": ATTRIBUTION,
        "nodes": {nid: [ll[0], ll[1]] for nid, ll, _ in nodes},
        "legs": {k: legs[k] for k in order if k in legs},
    }
    txt = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(txt + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return len(txt.encode("utf-8")) + 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true", help="refetch every leg")
    ap.add_argument("--dry-run", action="store_true", help="only report what would be fetched")
    ap.add_argument("--places", type=Path, default=PLACES)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--tolerance", type=float, default=3.0,
                    help="Douglas-Peucker tolerance in metres (default 3)")
    ap.add_argument("--delay", type=float, default=DELAY,
                    help=f"seconds between requests (min {DELAY})")
    args = ap.parse_args()
    delay = max(args.delay, DELAY)

    nodes = load_nodes(args.places)
    coord = {nid: ll for nid, ll, _ in nodes}
    pairs = required_pairs(nodes)
    order = [f"{a}>{b}" for a, b in pairs]

    # ---- reuse from previous output ------------------------------------
    old_legs: dict = {}
    old_generated = None
    old_nodes: dict = {}
    if args.out.exists() and not args.force:
        try:
            old = json.loads(args.out.read_text(encoding="utf-8"))
            old_legs = old.get("legs", {})
            old_nodes = {k: (round(v[0], COORD_DP), round(v[1], COORD_DP))
                         for k, v in old.get("nodes", {}).items()}
            old_generated = old.get("generated")
        except (ValueError, OSError, TypeError, IndexError) as e:
            print(f"! could not read {args.out} ({e}); rebuilding everything")

    def unchanged(nid: str) -> bool:
        return old_nodes.get(nid) == coord[nid]

    legs: dict = {}
    todo: list[tuple[str, str]] = []
    for a, b in pairs:
        k = f"{a}>{b}"
        leg = old_legs.get(k)
        if leg and "drive" in leg and unchanged(a) and unchanged(b):
            legs[k] = leg
        else:
            todo.append((a, b))

    # walk geometry is symmetric: one request per unordered pair.  A reverse
    # leg that is being reused already tells us the walk result.
    walk_cache: dict[frozenset, dict | None] = {}
    for a, b in todo:
        rk = f"{b}>{a}"
        if rk in legs:
            w = legs[rk].get("walk")
            walk_cache[frozenset((a, b))] = (
                {"d": w["d"], "t": w["t"], "g": reverse_polyline(w["g"]), "_from": a}
                if w else None)

    n_walk_req = len({frozenset((a, b)) for a, b in todo
                      if frozenset((a, b)) not in walk_cache
                      and haversine(*coord[a], *coord[b]) <= WALK_MAX_STRAIGHT})
    total_req = len(todo) + n_walk_req
    print(f"nodes: {len(nodes)}   legs required: {len(pairs)}   reused: {len(legs)}   "
          f"to fetch: {len(todo)} drive + {n_walk_req} walk = {total_req} requests "
          f"(~{total_req * delay / 60:.0f} min)", flush=True)
    if args.dry_run:
        for a, b in todo:
            print(f"  {a} > {b}")
        return 0

    if not todo:
        generated = old_generated or _dt.date.today().isoformat()
        if set(old_legs) != set(legs) or old_nodes != {k: coord[k] for k in coord}:
            generated = _dt.date.today().isoformat()
        size = write_out(args.out, generated, nodes, legs, order)
        print(f"nothing to fetch; wrote {args.out} ({size / 1024:.0f} KB)")
        return 0

    today = _dt.date.today().isoformat()
    osrm = Osrm(delay)
    failures: list[str] = []
    done_req = 0
    t0 = time.monotonic()

    def progress(label: str) -> None:
        el = time.monotonic() - t0
        eta = el / max(done_req, 1) * (total_req - done_req)
        print(f"[{done_req:4d}/{total_req}] {label}  (eta {eta / 60:4.1f} min)", flush=True)

    since_save = 0
    for a, b in todo:
        k = f"{a}>{b}"
        A, B = coord[a], coord[b]
        straight = haversine(*A, *B)
        leg: dict = {}
        try:
            r = osrm.route(CAR_URL, A, B)
            done_req += 1
            leg["drive"] = {"d": int(round(r["distance"])), "t": int(round(r["duration"])),
                            "g": pack_geometry(r["geometry"]["coordinates"], args.tolerance)}
            progress(f"drive {k}: {leg['drive']['d']} m, {leg['drive']['t']} s")

            uk = frozenset((a, b))
            if straight <= WALK_MAX_STRAIGHT:
                if uk not in walk_cache:
                    try:
                        r = osrm.route(FOOT_URL, A, B)
                        done_req += 1
                        d, t = r["distance"], r["duration"]
                        if walk_ok(d, straight):
                            walk_cache[uk] = {
                                "d": int(round(d)), "t": int(round(t)),
                                "g": pack_geometry(r["geometry"]["coordinates"], args.tolerance),
                                "_from": a}
                            progress(f"walk  {k}: {int(d)} m, {int(t)} s")
                        else:
                            walk_cache[uk] = None
                            progress(f"walk  {k}: {int(d)} m (straight {int(straight)} m) "
                                     "-> dropped")
                    except NoRoute as e:
                        done_req += 1
                        walk_cache[uk] = None
                        progress(f"walk  {k}: no route ({e}) -> dropped")
                w = walk_cache[uk]
                if w:
                    g = w["g"] if w["_from"] == a else reverse_polyline(w["g"])
                    leg["walk"] = {"d": w["d"], "t": w["t"], "g": g}
        except (NoRoute, RuntimeError) as e:
            done_req += 1
            failures.append(f"{k}: {e}")
            print(f"  !! {k}: {e}", flush=True)
            continue
        legs[k] = leg
        since_save += 1
        if since_save >= SAVE_EVERY:
            write_out(args.out, today, nodes, legs, order)
            since_save = 0

    size = write_out(args.out, today, nodes, legs, order)
    n_walk = sum(1 for v in legs.values() if "walk" in v)
    print(f"\nwrote {args.out}: {len(legs)}/{len(pairs)} legs, {n_walk} with walking, "
          f"{size / 1024:.0f} KB, {osrm.count} HTTP requests, "
          f"{(time.monotonic() - t0) / 60:.1f} min")
    if failures:
        print(f"\n{len(failures)} leg(s) failed -- run the script again to retry:")
        for f in failures:
            print("  " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
