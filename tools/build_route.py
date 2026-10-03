#!/usr/bin/env python3
"""Store road routes for the default route's legs that are not already in data/legs.json.

data/route.json may list built-in place ids/names *or* any other place as {"name", "lat", "lng"}.
Legs between two built-in places come from data/legs.json; every other leg is fetched once from OSRM
here and written into data/route.json under "legs", so visitors get the default route instantly
without contacting a routing server. The browser uses the same rules (walk short hops, else drive).

Usage:  python3 tools/build_route.py
"""
import json
import math
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "macau-tourist-map/1.0 (+https://github.com/w-jiaqi/macau)"}
FOOT = "https://routing.openstreetmap.de/routed-foot/route/v1/foot"
CAR = "https://routing.openstreetmap.de/routed-car/route/v1/driving"
WALK_PREFERRED = 1200


def haversine(a, b):
    r = 6371008.8
    dlat, dlng = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    s = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(s)))


def key_part(n):
    return n.get("id") or f"{n['lat']:.5f},{n['lng']:.5f}"


def osrm(base, a, b):
    url = f"{base}/{a['lng']:.6f},{a['lat']:.6f};{b['lng']:.6f},{b['lat']:.6f}?overview=full&geometries=polyline&alternatives=false&steps=false"
    for attempt in range(4):
        try:
            res = requests.get(url, headers=UA, timeout=20)
            if res.status_code == 200 and res.json().get("code") == "Ok":
                r = res.json()["routes"][0]
                return {"d": round(r["distance"]), "g": r["geometry"]}
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"OSRM failed for {url}")


def main():
    places = json.loads((ROOT / "data/places.json").read_text("utf-8"))
    legs_path = ROOT / "data/legs.json"
    pre = json.loads(legs_path.read_text("utf-8")).get("legs", {}) if legs_path.exists() else {}
    route_path = ROOT / "data/route.json"
    route = json.loads(route_path.read_text("utf-8"))
    catalog = {p["id"]: p for p in places["places"]}
    by_name = {p["name"]: p for p in places["places"]}

    def resolve(entry):
        if isinstance(entry, dict):
            return {"lat": entry["lat"], "lng": entry["lng"]}
        p = catalog.get(entry) or by_name.get(entry)
        if not p:
            raise SystemExit(f"Unknown stop in route.json: {entry!r}")
        return {"id": p["id"], "lat": p["lat"], "lng": p["lng"]}

    nodes = [{"id": places["start"]["id"], **{k: places["start"][k] for k in ("lat", "lng")}}]
    nodes += [resolve(e) for e in route.get("stops", [])]
    nodes.append({"id": places["end"]["id"], **{k: places["end"][k] for k in ("lat", "lng")}})

    old = route.get("legs", {})
    out = {}
    for a, b in zip(nodes, nodes[1:]):
        key = f"{key_part(a)}>{key_part(b)}"
        if a.get("id") and b.get("id") and key in pre:
            continue  # already in data/legs.json
        if key in old:
            out[key] = old[key]
            continue
        straight = haversine((a["lat"], a["lng"]), (b["lat"], b["lng"]))
        leg = None
        if straight < 1500:
            w = osrm(FOOT, a, b)
            if w["d"] <= max(WALK_PREFERRED * 1.6, straight * 2.5):
                leg = {"mode": "walk", **w}
            time.sleep(1.1)
        if not leg:
            leg = {"mode": "drive", **osrm(CAR, a, b)}
            time.sleep(1.1)
        out[key] = leg
        print(f"{key}: {leg['mode']} {leg['d']} m")

    route["legs"] = out
    route_path.write_text(json.dumps(route, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"wrote {len(out)} leg(s) to {route_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
