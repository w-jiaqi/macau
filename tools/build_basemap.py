#!/usr/bin/env python3
"""Build the vector basemap for the Macau tourist route map.

The map draws its own basemap with Leaflet (canvas renderer) so that every
label can be Simplified Chinese.  This script fetches OpenStreetMap data via
the Overpass API and writes:

  data/basemap.geojson         land / parks / water / roads / bridges / rail ...
  data/basemap-detail.geojson  minor roads and footpaths (lazy-loaded at high zoom)
  data/labels.json             Simplified-Chinese map labels

Usage (from the repository root):

  pip install -r tools/requirements.txt
  python tools/build_basemap.py                 # fetch (cached) + build
  python tools/build_basemap.py --refresh       # ignore cache, re-download
  python tools/build_basemap.py --cache DIR     # cache dir (default tools/.cache)

The build prints self-checks (unclassified coastline faces, islands whose
orientation disagrees with the land result, labels that are not in water / on
land, overlapping labels).  Output sizes: basemap.geojson <= ~700 KB,
basemap-detail.geojson <= ~1.5 MB.

Map data (c) OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import requests
import shapely
import shapely.affinity
from shapely import make_valid
from shapely.geometry import (GeometryCollection, LineString, MultiLineString,
                              MultiPolygon, Point, Polygon, box, mapping)
from shapely.ops import linemerge, polygonize, unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

S, W, N, E = 22.095, 113.48, 22.235, 113.625
BBOX = f"{S},{W},{N},{E}"
BBOX_POLY = box(W, S, E, N)

LAT0 = (S + N) / 2
KX = math.cos(math.radians(LAT0))      # lon-degree -> "lat-degree" scale
M_PER_DEG = 110_574.0                  # metres per degree of latitude

UA = "macau-tourist-map/1.0 (+https://github.com/w-jiaqi/macau)"
OVERPASS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

# Simplification tolerances (degrees) and output precision
TOL_LAND = 0.00002
TOL_AREA = 0.00004
TOL_ROAD = 0.00002
TOL_MINOR = 0.00003
PREC = 5                      # decimals (~1 m)
MIN_PARK_M2 = 800
MIN_WATER_M2 = 300

# --------------------------------------------------------------------------
# Overpass queries
# --------------------------------------------------------------------------
QUERIES = {
    "coastline": f"""
[out:json][timeout:180];
way["natural"="coastline"]({BBOX});
out geom;
""",
    "macau_boundary": """
[out:json][timeout:180];
relation["ISO3166-1"="MO"]["boundary"="administrative"];
out geom;
""",
    "areas": f"""
[out:json][timeout:180];
(
  nwr["leisure"~"^(park|garden|nature_reserve|golf_course)$"]({BBOX});
  nwr["landuse"~"^(forest|grass|recreation_ground|cemetery|village_green|reservoir|basin|meadow)$"]({BBOX});
  nwr["natural"~"^(wood|scrub|grassland|heath|water|beach|sand|bay|strait)$"]({BBOX});
  nwr["water"]({BBOX});
  nwr["aeroway"~"^(aerodrome|runway)$"]({BBOX});
);
out geom;
""",
    "riverbank": f"""
[out:json][timeout:180];
nwr["waterway"~"^(riverbank|dock)$"]({BBOX});
out geom;
""",
    "piers": f"""
[out:json][timeout:180];
(
  nwr["man_made"~"^(pier|breakwater|groyne)$"]({BBOX});
  way["amenity"="ferry_terminal"]({BBOX});
);
out geom;
""",
    "roads": f"""
[out:json][timeout:180];
way["highway"~"^(motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link)$"]({BBOX});
out geom;
""",
    "minor": f"""
[out:json][timeout:180];
way["highway"~"^(residential|unclassified|living_street|service|pedestrian|footway|steps)$"]({BBOX});
out geom;
""",
    "rail": f"""
[out:json][timeout:180];
way["railway"~"^(light_rail|subway|rail)$"]({BBOX});
out geom;
""",
    "points": f"""
[out:json][timeout:180];
(
  node["natural"="peak"]({BBOX});
  node["place"]({BBOX});
  nwr["aeroway"="aerodrome"]({BBOX});
  nwr["amenity"="university"]({BBOX});
  nwr["barrier"="border_control"]({BBOX});
  nwr["border_control"]({BBOX});
  nwr["amenity"="ferry_terminal"]({BBOX});
  nwr["man_made"="bridge"]({BBOX});
);
out center tags;
""",
}


def fetch(name: str, cache: Path, refresh: bool = False) -> dict:
    """Run one Overpass query, caching the raw JSON response on disk."""
    q = QUERIES[name].strip()
    key = hashlib.sha1(q.encode()).hexdigest()[:10]
    path = cache / f"{name}-{key}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    cache.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(3):
        for url in OVERPASS:
            try:
                log(f"  overpass {name} <- {url}")
                r = requests.post(url, data={"data": q},
                                  headers={"User-Agent": UA}, timeout=300)
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    js = r.json()
                    remark = js.get("remark") or ""
                    if "error" in remark.lower() or "timed out" in remark.lower():
                        raise RuntimeError(remark)
                    path.write_text(r.text)
                    time.sleep(1.2)
                    return js
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as ex:  # network errors, timeouts, bad JSON
                last_err = repr(ex)
            log(f"    failed: {last_err}")
            time.sleep(2)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Overpass query {name} failed: {last_err}")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------
def coords_of(geom_list):
    return [(p["lon"], p["lat"]) for p in geom_list if p]


def is_closed_way(e):
    nodes = e.get("nodes") or []
    return len(nodes) >= 4 and nodes[0] == nodes[-1]


def polys(g):
    """Iterate the polygons of any geometry."""
    if g is None or g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, (MultiPolygon, GeometryCollection)):
        out = []
        for x in g.geoms:
            out += polys(x)
        return out
    return []


def lines(g):
    """Iterate the LineStrings of any geometry."""
    if g is None or g.is_empty:
        return []
    if isinstance(g, LineString):
        return [g]
    if isinstance(g, (MultiLineString, GeometryCollection)):
        out = []
        for x in g.geoms:
            out += lines(x)
        return out
    return []


def as_multipolygon(g):
    ps = [p for p in polys(g) if not p.is_empty and p.area > 0]
    if not ps:
        return None
    return ps[0] if len(ps) == 1 else MultiPolygon(ps)


def as_multiline(g):
    ls = [l for l in lines(g) if not l.is_empty and l.length > 0]
    if not ls:
        return None
    return ls[0] if len(ls) == 1 else MultiLineString(ls)


def valid(g):
    if g is None or g.is_empty:
        return g
    return g if g.is_valid else make_valid(g)


def area_m2(g):
    """Approximate area in m^2 (local equirectangular)."""
    return g.area * KX * M_PER_DEG * M_PER_DEG


def length_m(g):
    return shapely.length(shapely.transform(g, lambda c: c * [KX, 1.0])) * M_PER_DEG


def to_local(g):
    return shapely.transform(g, lambda c: c * [KX, 1.0])


def from_local(g):
    return shapely.transform(g, lambda c: c / [KX, 1.0])


def rings_to_polygons(line_list):
    """Assemble closed rings from (possibly split) member ways."""
    if not line_list:
        return []
    merged = linemerge(line_list)
    out, leftovers = [], []
    for g in lines(merged):
        if g.is_closed and len(g.coords) >= 4:
            out.append(valid(Polygon(g.coords)))
        else:
            leftovers.append(g)
    if leftovers:  # rings touching at a vertex do not merge: polygonize them
        for p in polygonize(unary_union(leftovers)):
            out.append(p)
    return out


def element_polygon(e):
    """Polygon for a closed way or a multipolygon relation (None otherwise)."""
    if e["type"] == "way":
        if not is_closed_way(e):
            return None
        c = coords_of(e.get("geometry", []))
        if len(c) < 4:
            return None
        return valid(Polygon(c))
    if e["type"] == "relation":
        outers, inners = [], []
        for m in e.get("members", []):
            if m.get("type") != "way" or not m.get("geometry"):
                continue
            c = coords_of(m["geometry"])
            if len(c) < 2:
                continue
            (inners if m.get("role") == "inner" else outers).append(LineString(c))
        op = rings_to_polygons(outers)
        if not op:
            return None
        g = unary_union(op)
        ip = rings_to_polygons(inners)
        if ip:
            g = g.difference(unary_union(ip))
        return valid(g)
    return None


def simplify(g, tol):
    if g is None or g.is_empty:
        return g
    return valid(g.simplify(tol, preserve_topology=True))


def rnd(obj):
    """Round all floats in a (nested) coordinate structure."""
    if isinstance(obj, float):
        r = round(obj, PREC)
        return 0.0 if r == 0 else r
    if isinstance(obj, (list, tuple)):
        return [rnd(x) for x in obj]
    return obj


def finish_polygonal(g, tol):
    """Simplify, snap to the output grid, drop slivers."""
    if g is None or g.is_empty:
        return None
    g = simplify(g, tol)
    g = shapely.set_precision(g, 10 ** -PREC)
    g = valid(g)
    g = as_multipolygon(g)
    if g is None:
        return None
    g = as_multipolygon(MultiPolygon([p for p in polys(g) if area_m2(p) > 20]))
    return g


def finish_lineal(g, tol, min_len_m=0.0):
    if g is None or g.is_empty:
        return None
    g = linemerge(lines(g)) if len(lines(g)) > 1 else g
    g = g.simplify(tol, preserve_topology=False)
    g = shapely.set_precision(g, 10 ** -PREC)
    ls = []
    for l in lines(g):
        cs = []
        for c in l.coords:
            c = (round(c[0], PREC), round(c[1], PREC))
            if not cs or cs[-1] != c:
                cs.append(c)
        if len(cs) >= 2:
            l2 = LineString(cs)
            if length_m(l2) >= min_len_m:
                ls.append(l2)
    return as_multiline(MultiLineString(ls)) if ls else None


def feature(layer, geom, **props):
    p = {"layer": layer}
    p.update({k: v for k, v in props.items() if v is not None})
    gj = mapping(geom)
    return {"type": "Feature", "properties": p,
            "geometry": {"type": gj["type"], "coordinates": rnd(gj["coordinates"])}}


# --------------------------------------------------------------------------
# coastline -> land
# --------------------------------------------------------------------------
def build_land(coast_js):
    """Polygonize the coastline inside the bbox and classify faces.

    OSM convention: land is on the LEFT of a coastline way.  Every coastline
    segment votes: the face just left of its midpoint is land, the face just
    right of it is water (votes weighted by segment length).
    """
    coast = []
    for e in coast_js["elements"]:
        c = coords_of(e.get("geometry", []))
        if len(c) >= 2:
            coast.append(LineString(c))
    log(f"  coastline ways: {len(coast)}")
    clipped = [g for l in coast for g in lines(l.intersection(BBOX_POLY))]
    noded = unary_union([BBOX_POLY.boundary] + clipped)
    faces = list(polygonize(noded))
    log(f"  faces: {len(faces)}")
    tree = STRtree(faces)
    votes = [0.0] * len(faces)
    eps = 2e-7
    for l in coast:
        cs = list(l.coords)
        for (x1, y1), (x2, y2) in zip(cs[:-1], cs[1:]):
            dx, dy = (x2 - x1) * KX, (y2 - y1)
            seg = math.hypot(dx, dy)
            if seg == 0:
                continue
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            if not (W < mx < E and S < my < N):
                continue
            # left normal in local metric space, back to degrees
            nx, ny = -dy / seg, dx / seg
            left = Point(mx + eps * nx / KX, my + eps * ny)
            right = Point(mx - eps * nx / KX, my - eps * ny)
            for pt, sgn in ((left, 1), (right, -1)):
                for i in tree.query(pt, predicate="within"):
                    votes[i] += sgn * seg
    land_faces = [f for f, v in zip(faces, votes) if v > 0]
    undecided = sum(1 for v in votes if v == 0)
    if undecided:
        log(f"  WARNING: {undecided} faces without coastline votes (treated as water)")
    land = valid(unary_union(land_faces))
    log(f"  land faces: {len(land_faces)}  land area {area_m2(land)/1e6:.1f} km2")
    # sanity check: closed coastline rings inside the bbox are islands
    # (counter-clockwise, land inside) or lakes cut out of land (clockwise)
    merged = linemerge(coast)
    for ring in lines(merged):
        if not ring.is_closed or not BBOX_POLY.contains(ring):
            continue
        poly = Polygon(ring.coords)
        if area_m2(poly) < 50:
            continue
        ccw = shapely.is_ccw(ring)
        inside = land.contains(poly.representative_point())
        if ccw != inside:
            log(f"  WARNING: coastline ring at {poly.representative_point().wkt} "
                f"is {'CCW' if ccw else 'CW'} but classified "
                f"{'land' if inside else 'water'}")
    return land


def build_piers(js, land):
    """Ferry-terminal buildings and the piers attached to the shore.

    The OSM coastline runs along the sea wall, so e.g. the 外港客运码头
    building (on piles over the water) would otherwise float in the sea.
    Terminals and shore-connected piers are added to the land so the route's
    terminal markers sit on land.  Breakwaters / groynes (free-standing sea
    structures, some crossing under bridges) are left out.
    """
    terminals, piers = [], []
    for e in js["elements"]:
        t = e.get("tags", {})
        if t.get("man_made") in ("breakwater", "groyne"):
            continue
        g = None
        if e["type"] == "relation" or (e["type"] == "way" and is_closed_way(e)
                                       and t.get("area") != "no"):
            g = element_polygon(e)
        elif e["type"] == "way" and t.get("man_made"):
            c = coords_of(e.get("geometry", []))
            if len(c) >= 2:
                try:
                    wdt = float(str(t.get("width", "")).split()[0])
                except (ValueError, IndexError):
                    wdt = 8.0 if t.get("man_made") == "pier" else 10.0
                g = buffer_line_m(LineString(c), max(4.0, min(wdt, 40.0)))
        if g is None or g.is_empty or area_m2(g) > 5e5:
            continue
        (terminals if t.get("amenity") == "ferry_terminal" else piers).append(g)
    keep = list(terminals)
    anchor = valid(unary_union([land] + terminals)).buffer(0.00002)
    for _ in range(3):          # piers attached to land, terminals or other piers
        added = [g for g in piers if g.intersects(anchor)]
        piers = [g for g in piers if not g.intersects(anchor)]
        if not added:
            break
        keep += added
        anchor = valid(unary_union([anchor] + added)).buffer(0.00002)
    log(f"  ferry terminals: {len(terminals)}, shore piers: {len(keep) - len(terminals)}")
    return valid(unary_union(keep)) if keep else None


def build_macau_area(bnd_js):
    """Macau SAR administrative area (includes sea)."""
    rel = None
    for e in bnd_js["elements"]:
        if e["type"] == "relation":
            rel = e
            break
    if rel is None:
        raise RuntimeError("Macau boundary relation not found")
    g = element_polygon(rel)
    log(f"  Macau boundary relation {rel['id']}: area {area_m2(g)/1e6:.1f} km2")
    return g


# --------------------------------------------------------------------------
# area layers
# --------------------------------------------------------------------------
GREEN_LEISURE = {"park", "garden", "nature_reserve", "golf_course"}
GREEN_LANDUSE = {"forest", "grass", "recreation_ground", "cemetery",
                 "village_green", "meadow"}
GREEN_NATURAL = {"wood", "scrub", "grassland", "heath"}
WATER_LANDUSE = {"reservoir", "basin"}
RIVER_WATER = {"river", "canal", "stream", "tidal_channel", "ditch", "drain",
               "stream_pool", "riverbank"}


def classify_area(t):
    if t.get("aeroway") == "aerodrome":
        return "airport"
    if t.get("aeroway") == "runway":
        return "runway"
    nat = t.get("natural")
    if nat == "beach":          # natural=sand here is mostly reclamation fill
        return "beach"
    if nat == "water" or t.get("landuse") in WATER_LANDUSE or \
            t.get("waterway") in ("riverbank", "dock"):
        return "water"
    if nat in ("bay", "strait", "sand"):
        return None
    if t.get("leisure") in GREEN_LEISURE or t.get("landuse") in GREEN_LANDUSE \
            or nat in GREEN_NATURAL:
        return "park"
    return None


def buffer_line_m(line, width_m):
    loc = to_local(line)
    return from_local(loc.buffer(width_m / 2 / M_PER_DEG, cap_style="flat",
                                 join_style="mitre"))


def build_water(js_list, land):
    """Inland water on land -> (lakes, sea-connected rivers).

    * polygons lying mostly in the sea (e.g. 十字门水道 tagged natural=water)
      are sea, not inland water: skipped (only coastline slivers would remain);
    * rivers / canals that share an edge with the sea (river mouths such as
      前山河 / 马骝洲水道, cut off by the coastline closing line) are returned
      separately so they can be carved out of the land - the coastline then
      follows the river banks instead of drawing a line across the mouth;
    * the rest (reservoirs, ponds, 南湾湖, 西湾湖 ...) is the "water" layer.
    """
    sea = valid(BBOX_POLY.difference(land))
    river_gs, lake_gs = [], []
    for js in js_list:
        for e in js["elements"]:
            if classify_area(e.get("tags", {})) != "water":
                continue
            g = element_polygon(e)
            if g is None or g.is_empty:
                continue
            g = valid(g.intersection(BBOX_POLY))
            if g.is_empty:
                continue
            on = valid(g.intersection(land))
            if area_m2(on) < 0.5 * area_m2(g):     # a sea-type polygon
                continue
            t = e.get("tags", {})
            is_river = t.get("water") in RIVER_WATER or t.get("waterway") == "riverbank"
            (river_gs if is_river else lake_gs).append(on)
    sea_b = sea.buffer(1e-6)
    rivers = []
    for p in polys(valid(unary_union(river_gs))):
        shared = p.boundary.intersection(sea_b)
        if not shared.is_empty and length_m(shared) > 10:
            rivers.append(p)
        else:
            lake_gs.append(p)
    rivers = valid(unary_union(rivers)) if rivers else None
    lakes = valid(unary_union(lake_gs)) if lake_gs else None
    if lakes is not None and rivers is not None:
        lakes = valid(lakes.difference(rivers))
    if lakes is not None:
        lakes = as_multipolygon(MultiPolygon(
            [p for p in polys(lakes) if area_m2(p) >= MIN_WATER_M2]))
    log(f"  water: {len(polys(lakes))} lakes/ponds {area_m2(lakes)/1e6 if lakes else 0:.2f} km2,"
        f" {len(polys(rivers))} sea-connected rivers {area_m2(rivers)/1e6 if rivers else 0:.2f} km2")
    return lakes, rivers


def build_areas(js_list, land_all, land_macau):
    buckets = {"park": [], "beach": [], "airport": [], "runway": []}
    for js in js_list:
        for e in js["elements"]:
            t = e.get("tags", {})
            k = classify_area(t)
            if k is None or k == "water":
                continue
            if k == "runway" and e["type"] == "way" and not is_closed_way(e):
                c = coords_of(e.get("geometry", []))
                if len(c) >= 2:
                    try:
                        wdt = float(str(t.get("width", "45")).split()[0])
                    except ValueError:
                        wdt = 45.0
                    buckets["runway"].append(buffer_line_m(LineString(c), wdt))
                continue
            g = element_polygon(e)
            if g is None or g.is_empty:
                continue
            buckets[k].append(g)
    out = {}
    clip = {"park": land_macau, "beach": land_all,
            "airport": land_macau, "runway": land_all}
    for k, gs in buckets.items():
        if not gs:
            continue
        u = valid(unary_union(gs)).intersection(clip[k])
        min_a = {"park": MIN_PARK_M2}.get(k, 100)
        u = as_multipolygon(MultiPolygon([p for p in polys(u) if area_m2(p) >= min_a]))
        out[k] = u
        if u is not None:
            log(f"  {k}: {len(polys(u))} polygons, {area_m2(u)/1e6:.2f} km2")
    return out


# --------------------------------------------------------------------------
# roads, bridges, rail
# --------------------------------------------------------------------------
MAJOR = {"motorway", "trunk", "primary", "motorway_link", "trunk_link", "primary_link"}
MID = {"secondary", "tertiary", "secondary_link", "tertiary_link"}

# (Simplified Chinese display name, substrings matched in OSM name tags)
BRIDGES = [
    ("嘉乐庇总督大桥", ["嘉樂庇總督大橋", "Governador Nobre de Carvalho"]),
    ("友谊大桥", ["友誼大橋 ", "Ponte da Amizade"]),
    ("西湾大桥", ["西灣大橋", "Ponte de Sai Van"]),
    ("澳门大桥", ["澳門大橋 ", "Ponte Macau"]),
    ("莲花大桥", ["蓮花大橋", "Ponte Flor de L"]),
    ("港珠澳大桥", ["港珠澳大桥", "港珠澳大橋"]),
]


def name_blob(t):
    return " ".join(str(v) for k, v in t.items() if "name" in k) + " "


def bridge_of(t):
    if t.get("bridge") in (None, "no"):
        # some bridge approach ways carry the name but no bridge tag; the
        # sea clip below keeps only the part over water anyway
        pass
    blob = name_blob(t)
    for disp, keys in BRIDGES:
        for kk in keys:
            if kk in blob and "大馬路" not in blob and "Avenida" not in blob:
                return disp
    return None


def build_roads(roads_js, sea):
    major, mid = [], []
    bridges = {d: [] for d, _ in BRIDGES}
    for e in roads_js["elements"]:
        t = e.get("tags", {})
        c = coords_of(e.get("geometry", []))
        if len(c) < 2:
            continue
        g = LineString(c)
        b = bridge_of(t)
        if b:
            bridges[b].append(g)
        if t.get("tunnel") == "yes":   # tunnels are not drawn on the basemap
            continue
        hw = t.get("highway")
        if hw in MAJOR:
            major.append(g)
        elif hw in MID:
            mid.append(g)
    sea_b = sea.buffer(0.00004)
    bridge_feats = {}
    for disp, gs in bridges.items():
        if not gs:
            log(f"  WARNING: bridge {disp} not found")
            continue
        u = unary_union(gs).intersection(sea_b)
        # drop little pieces (crossing small inlets near the abutments)
        ls = [l for l in lines(linemerge(lines(u)) if len(lines(u)) > 1 else u)
              if length_m(l) > 60]
        if not ls:
            log(f"  WARNING: bridge {disp} has no part over water")
            continue
        bridge_feats[disp] = MultiLineString(ls)
        log(f"  bridge {disp}: {len(ls)} parts, {length_m(MultiLineString(ls)):.0f} m over water")
    return unary_union(major), unary_union(mid), bridge_feats


def build_rail(rail_js):
    """Macau LRT running lines -> (surface/elevated, tunnel) geometries."""
    surf, tun = [], []
    for e in rail_js["elements"]:
        t = e.get("tags", {})
        if t.get("railway") != "light_rail":
            continue
        named_line = "輕軌" in t.get("name", "") or "輕軌" in t.get("line", "")
        # depot tracks, sidings, crossovers; keep mis-tagged running lines
        if t.get("service") and not named_line:
            continue
        if t.get("service") in ("siding", "crossover", "spur"):
            continue
        c = coords_of(e.get("geometry", []))
        if len(c) < 2:
            continue
        (tun if t.get("tunnel") == "yes" else surf).append(LineString(c))
    return (unary_union(surf) if surf else None,
            unary_union(tun) if tun else None)


MINOR = {"residential", "unclassified", "living_street", "service"}
PATH = {"pedestrian", "footway", "steps"}


def build_detail(minor_js, area_clip):
    minor, path = [], []
    for e in minor_js["elements"]:
        t = e.get("tags", {})
        hw = t.get("highway")
        if t.get("area") == "yes" and hw != "pedestrian":
            continue
        if hw == "service" and t.get("service") in ("parking_aisle", "driveway",
                                                    "drive-through"):
            continue
        if hw == "footway" and t.get("footway") in ("sidewalk", "crossing",
                                                    "traffic_island", "link"):
            continue
        if t.get("access") in ("private", "no") and hw == "service":
            continue
        if t.get("tunnel") == "yes":
            continue
        c = coords_of(e.get("geometry", []))
        if len(c) < 2:
            continue
        g = LineString(c)
        if hw in MINOR:
            minor.append(g)
        elif hw in PATH:
            path.append(g)
    m = unary_union(minor).intersection(area_clip)
    p = unary_union(path).intersection(area_clip)
    return m, p


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------
# District / region labels: fixed, visually centred positions (validated below)
DISTRICTS = [  # text, lat, lng
    ("澳门半岛", 22.2000, 113.5460),
    ("氹仔", 22.1635, 113.5560),   # kept clear of the 官也街 stop marker
    ("路氹城", 22.1420, 113.5655),
    ("路环", 22.1255, 113.5660),
]
REGIONS = [
    ("珠海", 22.2150, 113.5060),
    ("横琴", 22.1250, 113.5060),
]

# Water labels: (text, type, min zoom, OSM name substrings, search box
# (W, S, E, N) or None, rotate along the channel?, fallback (lat, lng))
WATER_LABELS = [
    ("珠江口", "sea", 12, [], (113.598, 22.172, 113.620, 22.195), False, (22.1850, 113.6080)),
    ("十字门水道", "water", 13, ["十字門水道 Canal"], (113.536, 22.108, 113.553, 22.128), True,
     (22.1180, 113.5440)),
    # south part of the bay, clear of the 外港客运码头 start marker (22.197, 113.559)
    ("外港", "water", 13, ["外港 Porto Exterior"], (113.555, 22.185, 113.570, 22.1915), False,
     (22.1880, 113.5600)),
    ("内港", "water", 14, ["內港 Porto Interior"], (113.524, 22.192, 113.542, 22.206), False,
     (22.1980, 113.5335)),
    ("南湾湖", "water", 15, ["南灣湖"], None, False, (22.1865, 113.5420)),
    ("西湾湖", "water", 15, ["西灣湖"], None, False, (22.1830, 113.5340)),
    ("石排湾水库", "water", 15, ["石排灣水塘"], None, False, (22.1330, 113.5648)),
    ("九澳水库", "water", 15, ["九澳水庫"], None, False, (22.1335, 113.5760)),
]

# natural=peak: OSM name substring -> (Simplified display name, min zoom)
PEAKS = {
    "東望洋山": ("东望洋山", 14),
    "大潭山": ("大潭山", 14),
    "小潭山": ("小潭山", 14),
    "疊石塘山": ("叠石塘山", 14),
    "九澳山": ("九澳山", 14),
    "西望洋山": ("西望洋山", 15),
    "媽閣山": ("妈阁山", 15),
    "望廈山": ("望厦山", 15),
}

# Landmarks: (text, min zoom, finder over OSM point tags, fallback (lat, lng))
LANDMARKS = [
    ("澳门国际机场", 13, None, (22.1560, 113.5770)),
    ("澳门大学", 14, lambda t: t.get("name", "").startswith("澳門大學") and
     t.get("place") == "locality", (22.1285, 113.5438)),
    ("关闸（拱北口岸）", 14, lambda t: t.get("name", "").startswith("關閘廣場"), (22.2152, 113.5490)),
    ("港珠澳大桥澳门口岸", 14, lambda t: t.get("name", "").startswith("港珠澳大橋澳門口岸管理區"),
     (22.2004, 113.5761)),
    ("横琴口岸", 14, lambda t: t.get("name") == "横琴口岸", (22.1406, 113.5442)),
]

# label metrics used for the overlap check, matching assets/css/app.css:
# type -> (font px, letter-spacing em, extra px e.g. the hill "▲ " prefix)
LABEL_METRICS = {"district": (16, 0.4, 6), "region": (15, 0.35, 0), "sea": (14, 0.6, 0),
                 "water": (12.5, 0.3, 0), "bridge": (10.5, 0.08, 0),
                 "hill": (11, 0, 12), "landmark": (11.5, 0, 0)}


def polylabel_local(poly):
    """Pole of inaccessibility computed in a metric-ish projection."""
    from shapely.ops import polylabel
    p = polylabel(to_local(poly), tolerance=1e-6)
    return Point(p.x / KX, p.y)


def channel_angle(region, pt, radius_m=700):
    """Screen angle (CSS rotate, clockwise degrees) of a channel's long axis."""
    loc = to_local(region).intersection(to_local(pt).buffer(radius_m / M_PER_DEG))
    piece = max(polys(loc), key=lambda g: g.area, default=None)
    if piece is None:
        return None
    # principal axis of evenly spaced boundary points (PCA)
    ring = shapely.segmentize(piece.exterior, 10 / M_PER_DEG)
    cs = list(ring.coords)[:-1]
    n = len(cs)
    mx = sum(c[0] for c in cs) / n
    my = sum(c[1] for c in cs) / n
    sxx = sum((c[0] - mx) ** 2 for c in cs)
    syy = sum((c[1] - my) ** 2 for c in cs)
    sxy = sum((c[0] - mx) * (c[1] - my) for c in cs)
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)   # long-axis direction
    return norm_angle(-math.degrees(theta))


def norm_angle(a):
    while a > 90:
        a -= 180
    while a <= -90:
        a += 180
    return round(a, 1)


def text_px(text, typ):
    fs, ls, extra = LABEL_METRICS.get(typ, (12, 0, 0))
    w = sum((fs if ord(ch) > 0x2E80 else fs * 0.6) + fs * ls for ch in text) + extra
    return w, fs * 1.3


def label_overlaps(labels, zmin=12, zmax=18):
    """Pairs of labels whose (rotated) boxes overlap at a zoom both show."""
    out = []
    for z in range(zmin, zmax + 1):
        deg_px = 360 / (256 * 2 ** z)            # lon degrees per pixel
        boxes = []
        for L in labels:
            if L["min"] > z or ("max" in L and L["max"] < z):
                continue
            w, h = text_px(L["text"], L["type"])
            r = box(-w / 2 - 3, -h / 2 - 2, w / 2 + 3, h / 2 + 2)
            if L.get("angle"):
                r = shapely.affinity.rotate(r, -L["angle"], origin=(0, 0))
            # pixel -> degrees (screen y down; lat px shrinks by cos(lat))
            r = shapely.transform(r, lambda c: c * [deg_px, deg_px * KX])
            r = shapely.affinity.translate(r, L["lng"], L["lat"])
            boxes.append((L, r))
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if boxes[i][1].intersects(boxes[j][1]):
                    pair = (boxes[i][0]["text"], boxes[j][0]["text"])
                    if not any(p == pair for _, p in out):
                        out.append((z, pair))
    return out


def build_labels(points_js, areas_js, land_macau, land_other, water, sea,
                 bridge_feats, airport):
    labels = []
    problems = []
    water_any = valid(unary_union([g for g in (sea, water) if g is not None]))
    land_any = valid(unary_union([land_macau, land_other]))

    def add(text, typ, pt, mn, mx=None, angle=None):
        d = {"text": text, "type": typ, "lat": round(pt.y, 5), "lng": round(pt.x, 5),
             "min": mn}
        if mx is not None:
            d["max"] = mx
        if angle is not None:
            d["angle"] = angle
        labels.append(d)

    def ll(lat, lng):
        return Point(lng, lat)

    # districts / regions ------------------------------------------------
    for text, lat, lng in DISTRICTS:
        p = ll(lat, lng)
        if not land_macau.contains(p) or (water is not None and water.contains(p)):
            problems.append(f"district {text} not on Macau land")
        add(text, "district", p, 12, 15)
    for text, lat, lng in REGIONS:
        p = ll(lat, lng)
        if not land_other.contains(p) or (water is not None and water.contains(p)):
            problems.append(f"region {text} not on non-Macau land")
        add(text, "region", p, 12)

    # water ----------------------------------------------------------------
    named = {}
    for e in areas_js["elements"]:
        blob = name_blob(e.get("tags", {}))
        for _, _, _, keys, *_ in WATER_LABELS:
            for k in keys:
                if k in blob:
                    g = element_polygon(e)
                    if g is not None and not g.is_empty:
                        named.setdefault(k, []).append(g)
    for text, typ, mn, keys, sbox, rotate, fb in WATER_LABELS:
        region = None
        geoms = [g for k in keys for g in named.get(k, [])]
        if geoms:
            region = unary_union(geoms).intersection(water_any)
        elif sbox:
            region = water_any
        if region is not None and sbox:
            region = region.intersection(box(*sbox))
        piece = max(polys(region), key=lambda g: g.area, default=None) \
            if region is not None else None
        if piece is not None:
            # ignore islets so the label sits in the visual middle of a lake
            pt = polylabel_local(Polygon(piece.exterior))
            if not water_any.contains(pt):
                pt = polylabel_local(piece)
        else:
            pt = ll(*fb)
            problems.append(f"water {text}: OSM geometry not found, fallback used")
        if not water_any.contains(pt):
            problems.append(f"water {text} not in water")
        ang = channel_angle(region, pt) if (rotate and region is not None) else None
        add(text, typ, pt, mn, None, ang)

    # bridges: midpoint + direction of the part over water ---------------
    for disp, ml in bridge_feats.items():
        longest = max(lines(ml), key=length_m)
        mid = longest.interpolate(0.5, normalized=True)
        (x1, y1), (x2, y2) = longest.coords[0], longest.coords[-1]
        ang = norm_angle(-math.degrees(math.atan2(y2 - y1, (x2 - x1) * KX)))
        add(disp, "bridge", mid, 14, None, ang)

    # hills ----------------------------------------------------------------
    seen = set()
    for e in points_js["elements"]:
        t = e.get("tags", {})
        if t.get("natural") != "peak" or "lat" not in e:
            continue
        name = t.get("name", "")
        hit = next((v for k, v in PEAKS.items() if name.startswith(k)), None)
        if hit is None or hit[0] in seen:
            continue
        seen.add(hit[0])
        text = hit[0]
        try:
            text = f"{text} {round(float(str(t['ele']).lower().rstrip('m').strip()))}m"
        except (KeyError, ValueError):
            pass
        add(text, "hill", ll(e["lat"], e["lon"]), hit[1])
    for k, (disp, _) in PEAKS.items():
        if disp not in seen:
            problems.append(f"peak {disp} not found in OSM")

    # landmarks ------------------------------------------------------------
    for text, mn, finder, fb in LANDMARKS:
        pt = None
        if finder is not None:
            for e in points_js["elements"]:
                if finder(e.get("tags", {})):
                    c = e.get("center") or e
                    if "lat" in c:
                        pt = ll(c["lat"], c["lon"])
                        break
        if pt is None:
            pt = ll(*fb)
            if finder is not None:
                problems.append(f"landmark {text}: OSM point not found, fallback used")
        if not land_any.contains(pt):
            problems.append(f"landmark {text} not on land")
        add(text, "landmark", pt, mn)

    for z, (a, b) in label_overlaps(labels):
        problems.append(f"labels overlap at z{z}: {a} / {b}")
    for p in problems:
        log("  LABEL CHECK: " + p)
    return labels


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    path.write_text(txt, encoding="utf-8")
    log(f"  wrote {path.relative_to(ROOT)}  {len(txt.encode()) / 1024:.0f} KB")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(Path(__file__).resolve().parent / ".cache"),
                    help="directory for raw Overpass responses (default tools/.cache)")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache, re-download")
    ap.add_argument("--fetch-only", action="store_true",
                    help="only download / refresh the Overpass cache")
    a = ap.parse_args()
    cache = Path(a.cache)

    log("fetching OSM data (cached in %s)" % cache)
    js = {n: fetch(n, cache, a.refresh) for n in QUERIES}
    if a.fetch_only:
        return

    log("land")
    land_coast = build_land(js["coastline"])
    land_all = land_coast
    piers = build_piers(js["piers"], land_coast)
    if piers is not None:
        land_all = valid(unary_union([land_coast, piers.intersection(BBOX_POLY)]))
    lakes, rivers = build_water([js["areas"], js["riverbank"]], land_all)
    if rivers is not None:
        land_all = valid(land_all.difference(rivers))
    macau = build_macau_area(js["macau_boundary"])
    # simplify before splitting so land / land-other share an identical edge
    land_all_s = simplify(land_all, TOL_LAND)
    macau_s = simplify(macau, TOL_LAND)
    land_macau = valid(land_all_s.intersection(macau_s))
    land_other = valid(land_all_s.difference(macau_s))
    sea = valid(BBOX_POLY.difference(land_all))

    log("areas")
    areas = build_areas([js["areas"], js["riverbank"]], land_all_s, land_macau)
    areas["water"] = valid(lakes.intersection(land_all_s)) if lakes is not None else None

    log("roads")
    # bridges: the parts over open water (coastline-only sea, so a pier under
    # a deck does not cut the bridge)
    major, mid, bridge_feats = build_roads(js["roads"],
                                           valid(BBOX_POLY.difference(land_coast)))
    rail = build_rail(js["rail"])
    border = valid(land_macau.buffer(1e-6).intersection(land_other.buffer(1e-6)))
    border = linemerge(lines(shapely.intersection(macau_s.boundary, border)))

    feats = []

    def addf(layer, g, kind, tol, **props):
        if g is None or g.is_empty:
            return
        if kind == "poly":
            g = finish_polygonal(g, tol)
        else:
            g = finish_lineal(g, tol, props.pop("min_len_m", 0.0))
        if g is not None and not g.is_empty:
            feats.append(feature(layer, g, **props))

    addf("land-other", land_other, "poly", 0)
    addf("land", land_macau, "poly", 0)
    # simplified green/beach/airport edges are re-clipped to the land outline
    for k, clip_to in (("park", land_macau), ("beach", land_all_s), ("airport", land_macau)):
        g = finish_polygonal(areas.get(k), TOL_AREA)
        if g is not None:
            g = as_multipolygon(valid(g.intersection(clip_to)))
            g = as_multipolygon(MultiPolygon([p for p in polys(g) if area_m2(p) > 50])) \
                if g is not None else None
        areas[k] = g
    addf("park", areas.get("park"), "poly", 0)
    addf("beach", areas.get("beach"), "poly", 0)
    addf("airport", areas.get("airport"), "poly", 0)
    addf("runway", areas.get("runway"), "poly", TOL_LAND)
    addf("water", areas.get("water"), "poly", TOL_LAND)
    addf("border", border, "line", TOL_LAND)
    addf("road-mid", mid, "line", TOL_ROAD, min_len_m=15)
    addf("road-major", major, "line", TOL_ROAD, min_len_m=15)
    addf("rail", rail[0], "line", TOL_ROAD)
    addf("rail", rail[1], "line", TOL_ROAD, tunnel=True)
    for disp, ml in bridge_feats.items():
        addf("bridge", ml, "line", TOL_ROAD, name=disp)

    fc = {"type": "FeatureCollection",
          "attribution": "© OpenStreetMap contributors (ODbL)",
          "bbox": [W, S, E, N], "features": feats}
    write_json(DATA / "basemap.geojson", fc)

    log("detail")
    m, p = build_detail(js["minor"], BBOX_POLY)
    dfe = []
    for layer, g in (("road-minor", m), ("path", p)):
        g = finish_lineal(g, TOL_MINOR, 10)
        if g is not None:
            dfe.append(feature(layer, g))
    write_json(DATA / "basemap-detail.geojson",
               {"type": "FeatureCollection",
                "attribution": "© OpenStreetMap contributors (ODbL)",
                "bbox": [W, S, E, N], "features": dfe})

    log("labels")
    labels = build_labels(js["points"], js["areas"], land_macau, land_other,
                          areas.get("water"), sea, bridge_feats, areas.get("airport"))
    write_json(DATA / "labels.json", labels)


if __name__ == "__main__":
    main()
