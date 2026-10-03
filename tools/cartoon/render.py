#!/usr/bin/env python3
"""Render the illustrated ("Disneyland park map" style) Macau map picture.

The picture is drawn programmatically from the real OpenStreetMap geometry in
data/basemap.geojson, in the Web Mercator projection, so it lines up exactly
with the real (Leaflet) map that the frontend keeps underneath it for
locations, roads and routing.  Nothing structural is moved: the Macau land
fill edge is the real coastline, roads are the real roads (simplified and
smoothed by a pixel or two), and every landmark icon stands with its anchor
on the attraction's real coordinates.

Georeference (the frontend depends on it - do not change casually):
  bounds  south 22.105, west 113.522, north 22.222, east 113.612 (WGS-84)
  image   3000 x 4211 px (preview 1500 x 2106), Web Mercator:
          x = (lng - W) / (E - W) * width
          y = (mercY(N) - mercY(lat)) / (mercY(N) - mercY(S)) * height
          mercY(lat) = ln(tan(pi/4 + lat_rad/2))

Pipeline:
  data/basemap.geojson, data/labels.json, data/places.json, tools/cartoon/icons/*.svg
    -> tools/cartoon/out/cartoon.svg            (one big SVG, gitignored)
    -> node tools/cartoon/rasterize.mjs          (resvg-js + fonts in tools/fonts)
    -> tools/cartoon/out/cartoon.png
    -> assets/map/cartoon.webp     3000 x 4211   (Pillow, quality 82)
       assets/map/cartoon-sm.webp  1500 x 2106   (LANCZOS downscale)
       data/cartoon.json           bounds / sizes / image paths / sea colour

Usage (from the repository root):
  pip install -r tools/requirements.txt     # shapely, requests, pillow (fonttools optional)
  (cd tools && npm install)                 # @resvg/resvg-js
  python3 tools/cartoon/render.py           # full build
  python3 tools/cartoon/render.py --svg-only            # just write the SVG
  python3 tools/cartoon/render.py --places 'dir/*.json' # attractions from JSON arrays
  python3 tools/cartoon/render.py --refresh             # re-download parish boundaries

District colours come from Macau's parish boundaries (OSM, fetched once from
Overpass and cached under tools/.cache/cartoon/); without network a rough
hand-made split is used.  Icons missing from tools/cartoon/icons/ are drawn as
a neutral placeholder badge.  Everything is seeded, so re-runs are identical.

Map data (c) OpenStreetMap contributors, ODbL 1.0.  Fonts: ZCOOL KuaiLe and
ZCOOL QingKe HuangYou, SIL Open Font License 1.1.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import shapely
from shapely import affinity, make_valid
from shapely.geometry import (GeometryCollection, LineString, MultiLineString,
                              MultiPolygon, Point, Polygon, box, shape)
from shapely.ops import linemerge, polygonize, unary_union
from shapely.strtree import STRtree

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
ROOT = TOOLS.parent
ICONS = HERE / "icons"
OUT = HERE / "out"
FONTS = TOOLS / "fonts"
DATA = ROOT / "data"

# ---------------------------------------------------------------- georeference
S_LAT, W_LNG, N_LAT, E_LNG = 22.105, 113.522, 22.222, 113.612
W, H = 3000, 4211
SMALL_W, SMALL_H = 1500, 2106
IMAGE_REL = "assets/map/cartoon.webp"
PREVIEW_REL = "assets/map/cartoon-sm.webp"


def _mercy(lat):
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


MY_N = float(_mercy(N_LAT))
MY_S = float(_mercy(S_LAT))


def proj_xy(lng, lat):
    x = (np.asarray(lng, float) - W_LNG) / (E_LNG - W_LNG) * W
    y = (MY_N - _mercy(np.asarray(lat, float))) / (MY_N - MY_S) * H
    return x, y


def P(lat, lng):
    x, y = proj_xy(lng, lat)
    return float(x), float(y)


def unproj_lat(y):
    my = MY_N - y / H * (MY_N - MY_S)
    return math.degrees(2 * math.atan(math.exp(my)) - math.pi / 2)


def proj_geom(g):
    return shapely.transform(g, lambda c: np.column_stack(proj_xy(c[:, 0], c[:, 1])))


def px_per_m(lat):
    """Web Mercator is conformal: the same scale in x and y at a latitude."""
    return W / ((E_LNG - W_LNG) * 111319.49079327357 * math.cos(math.radians(lat)))


# ---------------------------------------------------------------- palette
SEA = "#6cc5e0"
SHALLOW = "#94dbee"
RIPPLE = "#c4ecf6"
FOAM = "#ffffff"
OUTLINE = "#4a3a32"
LAND_EDGE = "#8a6a4f"
CLIFF = "#d6a36f"
CLIFF_DARK = "#b9844f"
OTHER_FILL = "#d5e2c6"
OTHER_EDGE = "#a8ba97"
OTHER_ROAD = "#eef3e5"
DISTRICT_FILL = {
    "peninsula": "#fde38c",   # butter yellow
    "taipa": "#ffcaa3",       # peach
    "cotai": "#e6d1f4",       # soft lavender
    "coloane": "#c6ecd1",     # mint
}
PARK = "#a8d98f"
PARK_EDGE = "#74b866"
ROAD_FILL = "#fffaf0"
ROAD_EDGE = "#dfc29a"
BRIDGE_EDGE = "#7f9bb5"
BRIDGE_PIER = "#6a87a3"
RAIL = "#16a39a"
AIRPORT = "#ebe6f0"
RUNWAY = "#a2abb3"
RUNWAY_EDGE = "#6f7a83"
BEACH = "#f8e2a6"
BEACH_DOT = "#d9b36a"
INLAND_SHALLOW = "#a2e0f0"

FAM_KUAILE = "ZCOOL KuaiLe, ZCOOL QingKe HuangYou"
FAM_QINGKE = "ZCOOL QingKe HuangYou, ZCOOL KuaiLe"

FAMOUS = {"ruins-st-paul", "senado-square", "a-ma-temple", "macau-tower",
          "grand-lisboa", "venetian", "parisian", "londoner", "studio-city",
          "a-ma-statue", "panda-pavilion"}
FERRY_IDS = {"outer-harbour": "外港码头", "taipa-ferry": "氹仔码头"}

CLIFF_DY = 12          # px the raised island's cliff shows below the land top
CLIP = box(-140, -140, W + 140, H + 140)
CANVAS = box(0, 0, W, H)
SEED = 20261003


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- geometry utils
def polys(g):
    if g is None or g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if hasattr(g, "geoms"):
        return [p for sub in g.geoms for p in polys(sub)]
    return []


def lines(g):
    if g is None or g.is_empty:
        return []
    if g.geom_type == "LineString":
        return [g]
    if g.geom_type == "LinearRing":
        return [LineString(g.coords)]
    if hasattr(g, "geoms"):
        return [l for sub in g.geoms for l in lines(sub)]
    return []


def area_clean(g):
    if g is None or g.is_empty:
        return Polygon()
    g = make_valid(g)
    ps = polys(g)
    return unary_union(ps) if ps else Polygon()


def drop_small(g, min_area):
    ps = [p for p in polys(g) if p.area >= min_area]
    return unary_union(ps) if ps else Polygon()


def fmt(v):
    s = f"{v:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return "0" if s == "-0" else s


def ring_d(coords):
    pts = list(coords)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return ""
    return "M" + " ".join(f"{fmt(x)},{fmt(y)}" for x, y in pts) + "Z"


def poly_d(g):
    out = []
    for p in polys(g):
        out.append(ring_d(p.exterior.coords))
        out.extend(ring_d(i.coords) for i in p.interiors)
    return "".join(out)


def line_d(g):
    out = []
    for ls in (g if isinstance(g, list) else lines(g)):
        cs = list(ls.coords)
        if len(cs) < 2:
            continue
        out.append("M" + " ".join(f"{fmt(x)},{fmt(y)}" for x, y in cs))
    return "".join(out)


def chaikin(coords, iters=2):
    pts = np.asarray(coords, float)
    if len(pts) < 3:
        return pts
    closed = np.allclose(pts[0], pts[-1])
    for _ in range(iters):
        if closed:
            p0 = pts[:-1]
            p1 = np.roll(p0, -1, axis=0)
            q = 0.75 * p0 + 0.25 * p1
            r = 0.25 * p0 + 0.75 * p1
            mid = np.empty((2 * len(q), 2))
            mid[0::2] = q
            mid[1::2] = r
            pts = np.vstack([mid, mid[:1]])
        else:
            p0, p1 = pts[:-1], pts[1:]
            q = 0.75 * p0 + 0.25 * p1
            r = 0.25 * p0 + 0.75 * p1
            mid = np.empty((2 * len(q), 2))
            mid[0::2] = q
            mid[1::2] = r
            pts = np.vstack([pts[:1], mid[1:-1], pts[-1:]])
    return pts


def poisson(region, r, rng, density=30, exclude=None, bounds=None):
    """Dart-throwing Poisson-disk sampling inside `region` (seeded)."""
    if region is None or region.is_empty:
        return []
    minx, miny, maxx, maxy = bounds or region.bounds
    n = int((maxx - minx) * (maxy - miny) / (r * r) * density) + 16
    xs = rng.uniform(minx, maxx, n)
    ys = rng.uniform(miny, maxy, n)
    shapely.prepare(region)
    ok = shapely.contains_xy(region, xs, ys)
    if exclude is not None and not exclude.is_empty:
        shapely.prepare(exclude)
        ok &= ~shapely.contains_xy(exclude, xs, ys)
    xs, ys = xs[ok], ys[ok]
    cell = r / math.sqrt(2)
    grid = {}
    out = []
    r2 = r * r
    for x, y in zip(xs.tolist(), ys.tolist()):
        gx, gy = int(x // cell), int(y // cell)
        bad = False
        for ix in range(gx - 2, gx + 3):
            for iy in range(gy - 2, gy + 3):
                q = grid.get((ix, iy))
                if q is not None and (q[0] - x) ** 2 + (q[1] - y) ** 2 < r2:
                    bad = True
                    break
            if bad:
                break
        if not bad:
            grid[(gx, gy)] = (x, y)
            out.append((x, y))
    return out


# ---------------------------------------------------------------- fonts / text
class TextMetrics:
    """Glyph advances from the real fonts (fontTools), with a sane fallback."""

    def __init__(self):
        self.tables = {}
        try:
            from fontTools.ttLib import TTFont
            for key, fn in (("kuaile", "ZCOOLKuaiLe-Regular.ttf"),
                            ("qingke", "ZCOOLQingKeHuangYou-Regular.ttf")):
                t = TTFont(str(FONTS / fn))
                self.tables[key] = (t.getBestCmap(), t["hmtx"].metrics, t["head"].unitsPerEm)
        except Exception as exc:  # pragma: no cover - fontTools is optional
            log(f"  (fontTools unavailable: {exc}; using approximate text widths)")

    def adv(self, ch, fam):
        order = ("kuaile", "qingke") if fam == "kuaile" else ("qingke", "kuaile")
        for key in order:
            if key in self.tables:
                cm, hm, upm = self.tables[key]
                g = cm.get(ord(ch))
                if g:
                    return hm[g][0] / upm
        if ord(ch) >= 0x2E80:
            return 0.92 if fam == "kuaile" else 0.765
        return 0.55

    def width(self, text, fs, fam="kuaile", ls=0.0):
        return sum(self.adv(c, fam) for c in text) * fs + ls * max(0, len(text) - 1)


TM = None


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class Label:
    """A text item: either a straight run or per-character stacked glyphs."""

    def __init__(self, text, cx, cy, fs, fill, halo="#ffffff", halo_w=None,
                 fam="kuaile", ls=0.0, angle=None, opacity=None, kind="",
                 halo_opacity=None):
        self.text, self.cx, self.cy, self.fs = text, cx, cy, fs
        self.fill, self.halo = fill, halo
        self.halo_w = halo_w if halo_w is not None else max(6.0, fs * 0.24)
        self.fam, self.ls, self.angle = fam, ls, angle
        self.opacity, self.kind, self.halo_opacity = opacity, kind, halo_opacity
        self._layout()

    def _layout(self):
        fs, fam = self.fs, self.fam
        if self.angle is None or abs(self.angle) < 4:
            w = TM.width(self.text, fs, fam, self.ls)
            self.glyphs = None
            self.w, self.h = w, fs * 1.0
            self.box = box(self.cx - w / 2, self.cy - fs * 0.5, self.cx + w / 2, self.cy + fs * 0.5)
            return
        a = self.angle
        # keep reading direction left->right or top->bottom
        if abs(a) <= 45:
            if math.cos(math.radians(a)) < 0:
                a += 180
        else:
            if math.sin(math.radians(a)) < 0:
                a += 180
        ux, uy = math.cos(math.radians(a)), math.sin(math.radians(a))
        advs = [TM.adv(c, fam) * fs for c in self.text]
        step_extra = self.ls
        total = sum(advs) + step_extra * (len(advs) - 1)
        # for steep (vertical-ish) runs, glyphs stack with ~1em pitch
        if abs(a) > 45:
            advs = [fs * 0.98 for _ in self.text]
            total = sum(advs) + step_extra * (len(advs) - 1)
        pos = -total / 2
        self.glyphs = []
        for c, ad in zip(self.text, advs):
            centre = pos + ad / 2
            self.glyphs.append((c, self.cx + ux * centre, self.cy + uy * centre, ad))
            pos += ad + step_extra
        pts = []
        for c, gx, gy, ad in self.glyphs:
            pts.append(box(gx - ad / 2, gy - fs / 2, gx + ad / 2, gy + fs / 2))
        self.box = unary_union(pts).envelope
        self.w = self.box.bounds[2] - self.box.bounds[0]
        self.h = self.box.bounds[3] - self.box.bounds[1]

    def move(self, dx, dy):
        self.cx += dx
        self.cy += dy
        self._layout()

    def _runs(self):
        fam = FAM_KUAILE if self.fam == "kuaile" else FAM_QINGKE
        if self.glyphs is None:
            yield self.cx - self.w / 2, self.cy + 0.36 * self.fs, self.text, fam, self.ls
        else:
            for c, gx, gy, ad in self.glyphs:
                yield gx - ad / 2, gy + 0.36 * self.fs, c, fam, 0

    def svg_halo(self):
        if not self.halo:
            return ""
        out = []
        op = f' stroke-opacity="{self.halo_opacity}" fill-opacity="{self.halo_opacity}"' if self.halo_opacity else ""
        for x, y, t, fam, ls in self._runs():
            lsa = f' letter-spacing="{fmt(ls)}"' if ls else ""
            out.append(f'<text x="{fmt(x)}" y="{fmt(y)}" font-family="{fam}" font-size="{fmt(self.fs)}"{lsa} '
                       f'fill="{self.halo}" stroke="{self.halo}" stroke-width="{fmt(self.halo_w)}" '
                       f'stroke-linejoin="round"{op}>{esc(t)}</text>')
        return "".join(out)

    def svg_fill(self):
        out = []
        op = f' opacity="{self.opacity}"' if self.opacity else ""
        if self.kind == "district":
            # chunky 3-D letters: a darker copy offset down-right under the face
            sd = self.fs * 0.045
            for x, y, t, fam, ls in self._runs():
                lsa = f' letter-spacing="{fmt(ls)}"' if ls else ""
                out.append(f'<text x="{fmt(x + sd)}" y="{fmt(y + sd * 1.3)}" font-family="{fam}" '
                           f'font-size="{fmt(self.fs)}"{lsa} fill="#5e3119">{esc(t)}</text>')
        for x, y, t, fam, ls in self._runs():
            lsa = f' letter-spacing="{fmt(ls)}"' if ls else ""
            out.append(f'<text x="{fmt(x)}" y="{fmt(y)}" font-family="{fam}" font-size="{fmt(self.fs)}"{lsa} '
                       f'fill="{self.fill}"{op}>{esc(t)}</text>')
        return "".join(out)


# ---------------------------------------------------------------- obstacles
class Obstacles:
    def __init__(self):
        self.items = []   # (geom, kind)

    def add(self, g, kind):
        self.items.append((g, kind))

    def overlap(self, g, kinds=None, weights=None):
        tot = 0.0
        for o, k in self.items:
            if kinds is not None and k not in kinds:
                continue
            if o.intersects(g):
                a = o.intersection(g).area
                tot += a * (weights.get(k, 1.0) if weights else 1.0)
        return tot

    def union(self, kinds=None, grow=0.0):
        gs = [o.buffer(grow) if grow else o for o, k in self.items if kinds is None or k in kinds]
        return unary_union(gs) if gs else Polygon()


# ---------------------------------------------------------------- icons
class IconLib:
    """Loads tools/cartoon/icons/<name>.svg into <defs> groups (ids prefixed)."""

    def __init__(self):
        self.defs = {}
        self.missing = set()

    def has(self, name):
        return (ICONS / f"{name}.svg").exists()

    def ref(self, name, fallback=None):
        """Return the def id to <use>, loading the icon on first use."""
        key = f"ic-{name}"
        if key in self.defs:
            return key
        path = ICONS / f"{name}.svg"
        if path.exists():
            txt = path.read_text(encoding="utf-8")
            body = self._inner(txt, name)
            if body is not None:
                self.defs[key] = f'<g id="{key}">{body}</g>'
                return key
        self.missing.add(name)
        if fallback:
            return fallback
        return self.placeholder()

    @staticmethod
    def _inner(txt, name):
        txt = re.sub(r"<\?xml.*?\?>", "", txt, flags=re.S)
        txt = re.sub(r"<!--.*?-->", "", txt, flags=re.S)
        m = re.search(r"<svg\b[^>]*>(.*)</svg>", txt, flags=re.S)
        if not m:
            return None
        body = m.group(1)
        pre = re.sub(r"[^A-Za-z0-9_-]", "", name) + "--"
        ids = set(re.findall(r'\bid="([^"]+)"', body))
        for i in sorted(ids, key=len, reverse=True):
            body = body.replace(f'id="{i}"', f'id="{pre}{i}"')
            body = body.replace(f"url(#{i})", f"url(#{pre}{i})")
            body = body.replace(f'href="#{i}"', f'href="#{pre}{i}"')
        body = re.sub(r"\s+", " ", body)
        return body.strip()

    def placeholder(self):
        key = "ic--placeholder"
        if key not in self.defs:
            self.defs[key] = (
                f'<g id="{key}"><ellipse cx="80" cy="148" rx="40" ry="8" fill="{OUTLINE}" opacity="0.18"/>'
                f'<path d="M80 146V96" stroke="{OUTLINE}" stroke-width="8" stroke-linecap="round"/>'
                f'<circle cx="80" cy="66" r="44" fill="#ef7d57" stroke="{OUTLINE}" stroke-width="5"/>'
                f'<circle cx="80" cy="66" r="32" fill="#fff6e3" stroke="{OUTLINE}" stroke-width="3"/>'
                f'<path d="M80 44l6.5 13.5 14.5 2-10.5 10 2.5 14.5-13-7-13 7 2.5-14.5-10.5-10 14.5-2z" '
                f'fill="#f2c14e" stroke="{OUTLINE}" stroke-width="3" stroke-linejoin="round"/></g>')
        return key

    def builtin(self, name):
        """Small built-in drawings used only when an icon file is missing."""
        key = f"bi-{name}"
        if key in self.defs:
            return key
        o = OUTLINE
        shapes = {
            "tree-round": (f'<ellipse cx="80" cy="148" rx="40" ry="8" fill="{o}" opacity="0.18"/>'
                           f'<path d="M70 146l3-46h14l3 46z" fill="#a0674b" stroke="{o}" stroke-width="7" stroke-linejoin="round"/>'
                           f'<circle cx="80" cy="70" r="52" fill="#7cc47f" stroke="{o}" stroke-width="7"/>'
                           f'<ellipse cx="62" cy="50" rx="14" ry="9" fill="#fff" opacity="0.5"/>'),
            "tree-pine": (f'<ellipse cx="80" cy="148" rx="34" ry="7" fill="{o}" opacity="0.18"/>'
                          f'<path d="M72 146v-24h16v24z" fill="#a0674b" stroke="{o}" stroke-width="7" stroke-linejoin="round"/>'
                          f'<path d="M80 10l-50 112h100z" fill="#3f8f5a" stroke="{o}" stroke-width="7" stroke-linejoin="round"/>'),
            "tree-palm": (f'<ellipse cx="80" cy="148" rx="34" ry="7" fill="{o}" opacity="0.18"/>'
                          f'<path d="M76 146c4-40 0-70 8-100" fill="none" stroke="{o}" stroke-width="14" stroke-linecap="round"/>'
                          f'<path d="M76 146c4-40 0-70 8-100" fill="none" stroke="#a0674b" stroke-width="7" stroke-linecap="round"/>'
                          f'<path d="M84 46c-20-20-50-14-62 4 24-6 40 0 62-4zm0 0c20-20 50-14 62 4-24-6-40 0-62-4zm0 0c-10-18-4-34 10-40-2 16-6 28-10 40z" '
                          f'fill="#7cc47f" stroke="{o}" stroke-width="6" stroke-linejoin="round"/>'),
        }
        shapes["dolphin"] = (
            '<ellipse cx="80" cy="146" rx="46" ry="8" fill="#ffffff" opacity="0.5"/>'
            f'<path d="M38 122 L16 128 L26 134 L22 146 L44 134 Z" fill="#e88aa3" stroke="{o}" stroke-width="4" stroke-linejoin="round"/>'
            f'<path d="M70 72 C72 54 80 44 94 40 C90 52 90 60 94 70 Z" fill="#e88aa3" stroke="{o}" stroke-width="4" stroke-linejoin="round"/>'
            f'<path d="M30 128 C34 62 118 34 146 112 C140 110 132 109 124 112 C110 80 64 70 46 132 Z" fill="#f4a7b9" stroke="{o}" stroke-width="4" stroke-linejoin="round"/>'
            '<path d="M124 112 C110 80 64 70 46 132 L52 132 C66 92 104 88 118 112 Z" fill="#ffffff" opacity="0.8"/>'
            f'<path d="M96 84 L92 104 L106 90 Z" fill="#e88aa3" stroke="{o}" stroke-width="3.5" stroke-linejoin="round"/>'
            f'<circle cx="126" cy="88" r="4" fill="{o}"/><circle cx="127.5" cy="86.5" r="1.4" fill="#ffffff"/>'
            '<path d="M60 66 C72 56 88 52 100 54" fill="none" stroke="#ffffff" stroke-width="4" stroke-linecap="round" opacity="0.7"/>'
            '<g fill="none" stroke="#ffffff" stroke-width="4" stroke-linecap="round">'
            '<path d="M8 146 q8 -7 16 0 t16 0"/><path d="M118 146 q8 -7 16 0 t16 0"/></g>'
            f'<g fill="#ffffff" stroke="{o}" stroke-width="2"><circle cx="146" cy="128" r="4"/>'
            '<circle cx="154" cy="120" r="3"/><circle cx="14" cy="114" r="3.5"/></g>')
        if name not in shapes:
            return self.placeholder()
        self.defs[key] = f'<g id="{key}">{shapes[name]}</g>'
        return key

    def use(self, ref, x, y, size, opacity=None):
        """<use> an icon so its anchor (80,148) lands on (x, y), drawn `size` px wide."""
        k = size / 160.0
        op = f' opacity="{opacity}"' if opacity is not None else ""
        return (f'<use href="#{ref}" transform="translate({fmt(x - 80 * k)},{fmt(y - 148 * k)}) '
                f'scale({k:.4f})"{op}/>')


def icon_box(x, y, size, pad=0.0):
    """Approximate visual footprint of an icon anchored at (x, y)."""
    return box(x - size * 0.47 - pad, y - size * 0.93 - pad, x + size * 0.47 + pad, y + size * 0.06 + pad)


# ---------------------------------------------------------------- data loading
def load_basemap():
    js = json.loads((DATA / "basemap.geojson").read_text(encoding="utf-8"))
    layers, bridges = {}, []
    for f in js["features"]:
        lay = f["properties"].get("layer")
        g = shape(f["geometry"])
        if lay == "bridge":
            bridges.append((f["properties"].get("name", ""), g))
        else:
            layers.setdefault(lay, []).append(g)
    return {k: unary_union(v) if len(v) > 1 else v[0] for k, v in layers.items()}, bridges


def load_places(spec):
    cands = []
    if spec:
        cands = sorted(glob.glob(spec))
    else:
        p = DATA / "places.json"
        if p.exists():
            cands = [str(p)]
    places = []
    for fn in cands:
        js = json.loads(Path(fn).read_text(encoding="utf-8"))
        if isinstance(js, dict):
            js = js.get("places", [])
        places.extend(e for e in js if isinstance(e, dict) and "id" in e and "lat" in e)
    seen, out = set(), []
    for e in places:
        if e["id"] not in seen:
            seen.add(e["id"])
            out.append(e)
    return out


PARISH_IDS = {5758865: "taipa", 5758866: "coloane", 5758867: "cotai"}
OVERPASS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def load_parishes(cache_dir: Path, refresh=False):
    """Taipa (嘉模堂区) / Coloane (圣方济各堂区) / Cotai (路氹填海区) polygons in lng/lat."""
    path = cache_dir / "parishes.json"
    js = None
    if path.exists() and not refresh:
        js = json.loads(path.read_text(encoding="utf-8"))
    else:
        try:
            import requests
            q = ("[out:json][timeout:170];relation(id:%s);out geom;"
                 % ",".join(str(i) for i in PARISH_IDS))
            for attempt in range(2):
                for url in OVERPASS:
                    try:
                        log(f"  overpass parishes <- {url}")
                        r = requests.post(url, data={"data": q}, timeout=200,
                                          headers={"User-Agent": "macau-tourist-map/1.0 (cartoon renderer)"})
                        if r.status_code == 200 and r.text.lstrip().startswith("{") and r.json().get("elements"):
                            js = r.json()
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            path.write_text(json.dumps(js, ensure_ascii=False), encoding="utf-8")
                            break
                    except Exception as exc:
                        log(f"    failed: {exc}")
                if js:
                    break
        except ImportError:
            pass
    if not js:
        return None
    out = {}
    for e in js.get("elements", []):
        key = PARISH_IDS.get(e.get("id"))
        if not key:
            continue
        outer, inner = [], []
        for m in e.get("members", []):
            if m.get("type") != "way" or "geometry" not in m:
                continue
            ls = LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
            (inner if m.get("role") == "inner" else outer).append(ls)
        g = unary_union(list(polygonize(unary_union(outer))))
        if inner:
            g = g.difference(unary_union(list(polygonize(unary_union(inner)))))
        out[key] = g
    return out if len(out) == 3 else None


def fallback_parishes():
    """Rough hand-made split used only when the parish boundaries are unavailable."""
    log("  WARNING: parish boundaries unavailable - using a rough hand-made district split")
    cotai = Polygon([(113.5505, 22.1555), (113.5745, 22.1555), (113.5745, 22.1430),
                     (113.5890, 22.1430), (113.5890, 22.1283), (113.5505, 22.1283)])
    coloane = box(113.545, 22.100, 113.600, 22.1335).difference(cotai)
    taipa = box(113.530, 22.120, 113.600, 22.1720).difference(cotai).difference(coloane)
    return {"taipa": taipa, "cotai": cotai, "coloane": coloane}


# ---------------------------------------------------------------- the renderer
class Renderer:
    def __init__(self, args):
        self.args = args
        self.rng = np.random.default_rng(SEED)
        self.prng = random.Random(SEED)
        self.icons = IconLib()
        self.obs = Obstacles()
        self.labels = {k: [] for k in ("hill", "bridge", "landmark", "water", "region", "icon", "district", "deco")}
        self.parts = {}
        self.stand = []          # (y, svg) objects sorted by y (trees, icons, boats)
        self.stats = {}

    # ------------------------------------------------------------ geometry prep
    def prepare(self):
        L, bridges = load_basemap()
        self.L_raw = L
        pg = lambda k: proj_geom(L[k]) if k in L else Polygon()

        mac = area_clean(pg("land")).intersection(CLIP)
        mac = drop_small(mac.simplify(0.45), 30)
        oth = area_clean(pg("land-other")).intersection(CLIP)
        oth = drop_small(oth.simplify(0.8), 200).difference(mac.buffer(0.3))
        oth = drop_small(oth, 200)
        self.mac, self.oth = mac, oth
        self.foot = unary_union([mac, affinity.translate(mac, 0, CLIFF_DY)])
        self.all_land = unary_union([self.foot, oth])
        shapely.prepare(self.all_land)

        # districts
        par = load_parishes(Path(self.args.cache), self.args.refresh) or fallback_parishes()
        parp = {k: proj_geom(v) for k, v in par.items()}
        dist = {k: mac.intersection(parp[k].buffer(0)) for k in ("taipa", "cotai", "coloane")}
        rest = mac.difference(unary_union(list(dist.values())))
        pen = []
        y_split = P(22.175, 113.55)[1]
        extra = {k: [] for k in dist}
        for p in polys(rest):
            if p.area < 1:
                continue
            if p.centroid.y < y_split:
                pen.append(p)
            else:
                best = min(dist, key=lambda k: parp[k].distance(p.centroid))
                extra[best].append(p)
        self.districts = {"peninsula": unary_union(pen)}
        for k in dist:
            self.districts[k] = area_clean(unary_union([dist[k]] + extra[k]))
        for k, g in self.districts.items():
            log(f"  district {k}: {g.area / 1e4:.1f} x1e4 px2")

        # areas
        park = area_clean(pg("park")).intersection(mac)
        park = park.buffer(8, quad_segs=4).buffer(-14, quad_segs=4).buffer(6, quad_segs=4)
        park = drop_small(park.intersection(mac.buffer(-3)), 900).simplify(1.0)
        self.park = park
        water = area_clean(pg("water"))
        self.water_mac = drop_small(water.intersection(mac), 140).simplify(0.8)
        self.water_oth = drop_small(water.intersection(oth), 400).simplify(1.0)
        self.beach = drop_small(area_clean(pg("beach")).intersection(mac), 150).simplify(0.8)
        self.beach_oth = drop_small(area_clean(pg("beach")).intersection(oth), 150).simplify(1.0)
        self.airport = area_clean(pg("airport")).intersection(mac).simplify(0.8)
        self.runway = area_clean(pg("runway"))

        # roads
        mac_b = mac.buffer(6)
        self.road_major = self.prep_roads(pg("road-major"), mac_b, simp=2.2, min_comp=60)
        self.road_mid = self.prep_roads(pg("road-mid"), mac_b, simp=2.2, min_comp=110)
        oth_in = oth.buffer(-3)
        self.road_oth = self.prep_roads(pg("road-major"), oth_in, simp=3.0, min_comp=160, smooth=1)
        self.rail = [LineString(chaikin(ls.simplify(1.5).coords, 2))
                     for ls in lines(linemerge(lines(pg("rail").intersection(CLIP)))) if ls.length > 20]
        self.bridges = []
        for name, g in bridges:
            gp = proj_geom(g).intersection(CLIP)
            merged = lines(linemerge(lines(gp)))
            ls = [LineString(chaikin(l.simplify(1.5).coords, 2)) for l in merged if l.length > 8]
            self.bridges.append((name, ls))
        self.bridge_geom = unary_union([l for _, ls in self.bridges for l in ls])

    def prep_roads(self, g, clip, simp, min_comp, smooth=2):
        g = g.intersection(clip)
        ls = [l for l in lines(linemerge(lines(g))) if l.length > 1]
        if not ls:
            return []
        # drop tiny disconnected fragments (component length < min_comp)
        bufs = unary_union([l.buffer(2.0, quad_segs=2) for l in ls])
        comps = polys(bufs)
        tree = STRtree(ls)
        keep = set()
        for c in comps:
            idx = [i for i in tree.query(c) if ls[i].intersects(c)]
            if sum(ls[i].length for i in idx) >= min_comp:
                keep.update(idx)
        out = []
        for i in sorted(keep):
            l = ls[i].simplify(simp)
            out.append(LineString(chaikin(l.coords, smooth)))
        return out

    # ------------------------------------------------------------ layout pieces
    def place_icons(self, places):
        items = []
        for e in places:
            if e["id"] in FERRY_IDS:
                continue
            x, y = P(e["lat"], e["lng"])
            if not (0 <= x <= W and 0 <= y <= H):
                continue
            base = 128.0 if e["id"] in FAMOUS else 98.0
            items.append({"e": e, "x": x, "y": y, "base": base, "size": base})
        # shrink icons in dense clusters
        for it in items:
            d = min((math.hypot(it["x"] - o["x"], it["y"] - o["y"]) for o in items if o is not it), default=999)
            near = sum(1 for o in items if o is not it and math.hypot(it["x"] - o["x"], it["y"] - o["y"]) < 150)
            f = 1.0
            if d < it["base"] * 0.9:
                f = max(0.88 if it["e"]["id"] in FAMOUS else 0.78, d / (it["base"] * 0.9))
            f *= max(0.92, 1 - 0.02 * near)
            it["size"] = round(it["base"] * f, 1)
        for it in items:
            self.obs.add(icon_box(it["x"], it["y"], it["size"]), "icon")
        self.icon_items = items
        return items

    def place_ferries(self, places):
        out = []
        for e in places:
            if e["id"] not in FERRY_IDS:
                continue
            tx, ty = P(e["lat"], e["lng"])
            size = 104.0
            best = None
            for r in range(60, 460, 12):
                for k in range(24):
                    a = 2 * math.pi * k / 24
                    x, y = tx + r * math.cos(a), ty + r * math.sin(a)
                    b = icon_box(x, y, size, pad=4)
                    if not CANVAS.contains(b) or self.all_land.intersects(b.buffer(3)):
                        continue
                    if self.bridge_geom.intersects(b.buffer(10)):
                        continue
                    pen = self.obs.overlap(b)
                    score = r + pen * 0.05
                    if best is None or score < best[0]:
                        best = (score, x, y)
                if best and best[0] < r:
                    break
            if not best:
                log(f"  ! no water spot for ferry {e['id']}")
                continue
            _, x, y = best
            ref = self.icons.ref(e["id"]) if self.icons.has(e["id"]) else self.icons.ref("ferry")
            self.obs.add(icon_box(x, y, size), "icon")
            out.append({"e": e, "x": x, "y": y, "size": size, "ref": ref, "label": FERRY_IDS[e["id"]],
                        "tx": tx, "ty": ty})
        self.ferries = out

    def place_icon_labels(self):
        todo = []
        for it in self.icon_items:
            todo.append((it, it["e"].get("short") or it["e"].get("name")))
        for f in self.ferries:
            todo.append((f, f["label"]))
        # famous first, then top-to-bottom
        todo.sort(key=lambda t: (0 if t[0]["e"]["id"] in FAMOUS else 1, t[0]["y"]))
        anchors = [(it["x"], it["y"] - it["size"] * 0.45, id(it)) for it, _ in todo]
        icon_boxes = [(icon_box(it["x"], it["y"], it["size"]), id(it),
                       2.8 if it["e"]["id"] in FAMOUS else 1.5) for it, _ in todo]
        static = [o for o, k in self.obs.items if k in ("label", "deco")]
        entries = []
        for it, text in todo:
            fs = 42 if it["e"]["id"] in FAMOUS else 38
            lab = Label(text, it["x"], it["y"], fs, OUTLINE, "#ffffff", 11, kind="icon")
            x, y, s = it["x"], it["y"], it["size"]
            w, h = lab.w, fs
            cands = [
                (0.0, (x, y + 8 + h / 2)),
                (0.5, (x + w * 0.28, y + 8 + h / 2)),
                (0.5, (x - w * 0.28, y + 8 + h / 2)),
                (0.8, (x + s * 0.40 + w / 2 + 4, y - s * 0.30)),
                (0.8, (x - s * 0.40 - w / 2 - 4, y - s * 0.30)),
                (1.0, (x + s * 0.40 + w / 2 + 4, y - h * 0.20)),
                (1.0, (x - s * 0.40 - w / 2 - 4, y - h * 0.20)),
                (1.4, (x, y - s * 0.95 - h / 2 - 2)),
                (1.4, (x + s * 0.40 + w / 2 + 4, y - s * 0.62)),
                (1.4, (x - s * 0.40 - w / 2 - 4, y - s * 0.62)),
                (2.2, (x, y + 10 + h * 1.5)),
            ]
            entries.append({"it": it, "lab": lab, "cands": cands, "box": None})

        def score(e, cx, cy, others):
            it, lab = e["it"], e["lab"]
            w, h = lab.w, lab.fs
            b = box(cx - w / 2 - 3, cy - h / 2 - 2, cx + w / 2 + 3, cy + h / 2 + 2)
            bp = box(cx - w / 2 - 14, cy - h / 2 - 5, cx + w / 2 + 14, cy + h / 2 + 5)   # keep a gap
            out = b.area - b.intersection(CANVAS.buffer(-6)).area
            ov_l = sum(bp.intersection(o).area for o in others if o.intersects(bp))
            ov_l += sum(bp.intersection(o).area for o in static if o.intersects(bp))
            ov_i = sum(b.intersection(ib).area * wt for ib, k, wt in icon_boxes if k != id(it) and ib.intersects(b))
            # association: the label should be nearer its own icon than any other
            own = math.hypot(cx - it["x"], cy - (it["y"] - it["size"] * 0.45))
            near = min((math.hypot(cx - ax, cy - ay) for ax, ay, k in anchors if k != id(it)), default=1e9)
            assoc = 0.0 if own <= near * 0.95 else min(1.0, (own - near * 0.95) / 60.0)
            return (ov_l * 3.0 + ov_i + out * 5) / b.area + assoc * 0.9, b

        for rnd in range(4):
            changed = False
            for e in entries:
                others = [o["box"] for o in entries if o is not e and o["box"] is not None]
                best = None
                for pref, (cx, cy) in e["cands"]:
                    sc, b = score(e, cx, cy, others)
                    sc += pref * 0.16
                    if best is None or sc < best[0] - 1e-9:
                        best = (sc, cx, cy, b)
                if e["box"] is None or not best[3].equals(e["box"]):
                    changed = True
                e["box"], e["pos"] = best[3], (best[1], best[2])
            if not changed:
                break
        for e in entries:
            lab = e["lab"]
            cx, cy = e["pos"]
            lab.move(cx - lab.cx, cy - lab.cy)
            self.obs.add(e["box"], "label")
            self.labels["icon"].append(lab)

    def place_district_labels(self, labels):
        names = {"澳门半岛": "peninsula", "氹仔": "taipa", "路氹城": "cotai", "路环": "coloane"}
        for lb in labels:
            if lb["type"] != "district":
                continue
            key = names.get(lb["text"])
            region = self.districts.get(key) if key else self.mac
            x0, y0 = P(lb["lat"], lb["lng"])
            fs = 128 if len(lb["text"]) > 2 else 140
            lab = Label(lb["text"], x0, y0, fs, "#a15a32", "#ffffff", 22, fam="qingke", ls=fs * 0.18,
                        kind="district")
            w, h = lab.w, fs
            best = None
            for dx in range(-640, 641, 24):
                for dy in range(-480, 481, 24):
                    d = math.hypot(dx, dy)
                    if d > 640:
                        continue
                    cx, cy = x0 + dx, y0 + dy
                    b = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                    if not CANVAS.buffer(-10).contains(b):
                        continue
                    outside = b.area - b.intersection(region).area
                    ov = self.obs.overlap(b, kinds={"icon", "label"})
                    score = (ov * 4 + outside * 2.0) / b.area + d / 640 * 0.35
                    if best is None or score < best[0]:
                        best = (score, cx, cy, b)
            if best is None:
                continue
            _, cx, cy, b = best
            lab.move(cx - lab.cx, cy - lab.cy)
            self.obs.add(b, "label")
            self.labels["district"].append(lab)

    def fit_inside(self, lab, margin=14):
        minx, miny, maxx, maxy = lab.box.bounds
        dx = max(0, margin - minx) - max(0, maxx - (W - margin))
        dy = max(0, margin - miny) - max(0, maxy - (H - margin))
        if dx or dy:
            lab.move(dx, dy)

    def try_place(self, lab, kinds=("icon", "label"), max_frac=0.12, nudges=None, register=True):
        self.fit_inside(lab)
        nudges = nudges or [(0, 0), (0, 30), (0, -30), (30, 0), (-30, 0), (0, 60), (0, -60),
                            (60, 0), (-60, 0), (40, 40), (-40, 40), (40, -40), (-40, -40)]
        base = (lab.cx, lab.cy)
        for dx, dy in nudges:
            lab.move(base[0] + dx - lab.cx, base[1] + dy - lab.cy)
            self.fit_inside(lab)
            b = lab.box.buffer(3, join_style=2)
            ov = self.obs.overlap(b, kinds=set(kinds))
            if ov <= max_frac * b.area:
                if register:
                    self.obs.add(b, "label")
                return True
        return False

    def place_other_labels(self, labels):
        for lb in labels:
            t, text = lb["type"], lb["text"]
            x, y = P(lb["lat"], lb["lng"])
            if t == "region":
                continue
            if t in ("water", "sea"):
                mn = lb.get("min", 13)
                if text.endswith("水库"):
                    fs, ls = 32, 3
                elif t == "sea":
                    fs, ls = 76, 22
                else:
                    fs, ls = {12: 76, 13: 66, 14: 56}.get(mn, 46), {12: 22, 13: 16, 14: 12}.get(mn, 8)
                lab = Label(text, x, y, fs, "#1d6f99", "#ffffff", max(8, fs * 0.2), ls=ls,
                            angle=lb.get("angle"), kind="water", halo_opacity=0.85)
                # narrow channel: stack vertically if the straight run would sit on land
                if lab.angle is None and t == "water" and not text.endswith("水库"):
                    land_frac = lab.box.intersection(self.all_land).area / lab.box.area
                    if land_frac > 0.25:
                        lab2 = Label(text, x, y, fs, "#1d6f99", "#ffffff", max(8, fs * 0.2), ls=ls * 0.5,
                                     angle=90, kind="water", halo_opacity=0.85)
                        if lab2.box.intersection(self.all_land).area / lab2.box.area < land_frac:
                            lab = lab2
                if self.try_place(lab, max_frac=0.10):
                    self.labels["water"].append(lab)
            elif t == "bridge":
                lab = self.bridge_label(text, x, y, lb.get("angle"))
                if lab is not None:
                    self.labels["bridge"].append(lab)
            elif t == "landmark":
                lab = Label(text, x, y, 36, "#5a3d7a", "#ffffff", 10, ls=1, kind="landmark")
                if self.try_place(lab, max_frac=0.10):
                    self.labels["landmark"].append(lab)
            elif t == "hill":
                name, _, height = text.partition(" ")
                lab = Label(name, x, y + 34, 32, "#2c6a43", "#ffffff", 9, ls=2, kind="hill")
                near_icon = any(math.hypot(it["x"] - x, it["y"] - y) < 75 for it in self.icon_items)
                ok = (not near_icon) and self.try_place(
                    lab, max_frac=0.10, nudges=[(0, 0), (0, 26), (26, 0), (-26, 0), (0, 50)])
                if ok:
                    self.labels["hill"].append(lab)
                    if height:
                        sub = Label(height, lab.cx, lab.cy + 30, 22, "#2c6a43", "#ffffff", 7, kind="hill")
                        self.obs.add(sub.box.buffer(2), "label")
                        self.labels["hill"].append(sub)
                self.hills.append({"text": name, "x": x, "y": y, "h": float(height.rstrip("m") or 60),
                                   "labelled": ok})

    def bridge_label(self, text, x, y, angle):
        """Name beside the bridge (not on it), on the visible over-water part."""
        segs = [l for name, ls in self.bridges if name == text for l in ls]
        fs = 34
        if not segs:
            lab = Label(text, x, y, fs, "#3f5f7f", "#ffffff", 9, ls=2, angle=angle, kind="bridge")
            return lab if self.try_place(lab, max_frac=0.10) else None
        g = unary_union(segs)
        vis = g.intersection(CANVAS.buffer(-170)).difference(self.foot.buffer(30))
        if vis.is_empty:
            vis = g.intersection(CANVAS.buffer(-60))
        vl = [l for l in lines(vis) if l.length > 40] or lines(vis)
        if not vl:
            return None
        p = Point(x, y)
        line = min(vl, key=lambda l: l.distance(p))
        t0 = line.project(p)
        best = None
        for along in (0, 40, -40, 80, -80, 130, -130):
            t = t0 + along
            if t < 0 or t > line.length:
                continue
            q = line.interpolate(t)
            qa = line.interpolate(max(0.0, t - 20))
            qb = line.interpolate(min(line.length, t + 20))
            ang = math.degrees(math.atan2(qb.y - qa.y, qb.x - qa.x))
            ux, uy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
            for side in (1, -1):
                off = 8.5 + 7 + fs * 0.5
                cx, cy = q.x - uy * off * side, q.y + ux * off * side
                lab = Label(text, cx, cy, fs, "#3f5f7f", "#ffffff", 9, ls=2, angle=ang, kind="bridge")
                bb = lab.box
                if not CANVAS.buffer(-12).contains(bb):
                    continue
                ov = self.obs.overlap(bb.buffer(3), kinds={"icon", "label", "deco"}) / bb.area
                land = sum(gl.intersection(bb).area for gl in [self.all_land]) / bb.area
                onbridge = g.buffer(8).intersection(bb).area / bb.area
                sc = ov * 3 + land * 0.6 + onbridge * 2 + abs(along) / 260 + (0.05 if side < 0 else 0)
                if best is None or sc < best[0]:
                    best = (sc, lab)
        if best is None or best[0] > 1.5:
            return None
        lab = best[1]
        self.obs.add(lab.box.buffer(3), "label")
        return lab

    def place_region_labels(self, labels):
        for lb in labels:
            if lb["type"] != "region":
                continue
            x0, y0 = P(lb["lat"], lb["lng"])
            # the neighbour polygon nearest to the original point, inside the canvas
            avail = self.oth.intersection(CANVAS.buffer(-30))
            cand = [p for p in polys(avail) if p.area > 20000]
            if not cand:
                continue
            target = min(cand, key=lambda p: p.distance(Point(x0, y0)))
            # trim off the far side so the label stays near its intended place
            fs = 120
            best = None
            for vertical in (False, True):
                lab = Label(lb["text"], 0, 0, fs, "#8aa07d", "#eef4e7", 18, fam="qingke", ls=fs * 0.25,
                            angle=90 if vertical else None, kind="region")
                w, h = lab.w, lab.h
                minx, miny, maxx, maxy = target.bounds
                for cx in np.arange(minx + w / 2, maxx - w / 2 + 1, 20):
                    for cy in np.arange(miny + h / 2, maxy - h / 2 + 1, 20):
                        b = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                        inside = b.intersection(target).area / b.area
                        if inside < 0.9:
                            continue
                        ov = self.obs.overlap(b, kinds={"icon", "label"}) / b.area
                        d = math.hypot(cx - x0, cy - y0)
                        score = ov * 3 + (1 - inside) * 2 + d / 3000
                        if best is None or score < best[0]:
                            best = (score, cx, cy, vertical)
            if best is None:
                continue
            _, cx, cy, vertical = best
            lab = Label(lb["text"], cx, cy, fs, "#8aa07d", "#eef4e7", 18, fam="qingke", ls=fs * 0.25,
                        angle=90 if vertical else None, kind="region")
            self.obs.add(lab.box, "label")
            self.labels["region"].append(lab)

    def place_deco(self):
        """Airplane, LRT train, junk boats, clouds, compass, scale bar."""
        self.deco_svg = []
        # compass + scale bar (south-east open sea)
        cx, cy, cs = 2790, 3640, 230
        self.compass = (cx, cy, cs)
        self.obs.add(box(cx - cs / 2, cy - cs / 2 - 70, cx + cs / 2, cy + cs / 2), "deco")
        lat = unproj_lat(3990)
        L = 500 * px_per_m(lat)
        self.scalebar = (2790 - L / 2, 3990, L)
        self.obs.add(box(2790 - L / 2 - 30, 3930, 2790 + L / 2 + 60, 4030), "deco")
        # clouds
        self.clouds = [(2760, 330, 300), (290, 4000, 280)]
        for x, y, s in self.clouds:
            self.obs.add(box(x - s / 2, y - s * 0.6, x + s / 2, y + s * 0.15), "deco")

        def open_water(b, margin=24):
            return CANVAS.buffer(-10).contains(b) and not self.all_land.intersects(b.buffer(margin)) \
                and not self.bridge_geom.intersects(b.buffer(18))

        def find(pref, size, valid, rmax=500):
            px, py = pref
            best = None
            for r in range(0, rmax, 15):
                for k in range(max(1, int(2 * math.pi * r / 30))):
                    a = 2 * math.pi * k / max(1, int(2 * math.pi * r / 30))
                    x, y = px + r * math.cos(a), py + r * math.sin(a)
                    b = icon_box(x, y, size, pad=4)
                    if not valid(b, x, y):
                        continue
                    ov = self.obs.overlap(b)
                    if ov < 0.02 * b.area:
                        return x, y
                    if best is None or ov < best[0]:
                        best = (ov, x, y)
            return (best[1], best[2]) if best else None

        # airplane on the airport apron beside the runway
        rw = self.runway.minimum_rotated_rectangle
        rc = rw.centroid
        aero = self.airport.buffer(2)
        spot = find((rc.x - 130, rc.y - 260), 120,
                    lambda b, x, y: aero.contains(Point(x, y)) and not self.runway.intersects(b.buffer(-10)),
                    rmax=600)
        if spot:
            self.add_stand("airplane", spot[0], spot[1], 120)
        # LRT train on the line (Taipa section)
        best = None
        for ls in self.rail:
            for t in np.linspace(0.05, 0.95, 60):
                p = ls.interpolate(t, normalized=True)
                d = math.hypot(p.x - P(22.1585, 113.5615)[0], p.y - P(22.1585, 113.5615)[1])
                b = icon_box(p.x, p.y, 84)
                ov = self.obs.overlap(b)
                score = d + ov * 0.2
                if best is None or score < best[0]:
                    best = (score, p.x, p.y)
        if best:
            self.add_stand("lrt", best[1], best[2], 84)
        # junk boats
        for pref, size in (((2600, 1150), 118), ((2010, 1560), 104), ((2620, 2900), 112)):
            spot = find(pref, size, lambda b, x, y: open_water(b), rmax=600)
            if spot:
                self.add_stand("junk", spot[0], spot[1], size)
        # Chinese white ("pink") dolphins live in the Pearl River estuary
        for pref, size in (((2760, 2150), 96), ((1930, 3850), 88)):
            spot = find(pref, size, lambda b, x, y: open_water(b, 40), rmax=500)
            if spot:
                ref = self.icons.ref("dolphin", fallback=self.icons.builtin("dolphin")) \
                    if self.icons.has("dolphin") else self.icons.builtin("dolphin")
                self.stand.append((spot[1], self.icons.use(ref, spot[0], spot[1], size)))
                self.obs.add(icon_box(spot[0], spot[1], size), "icon")

    def add_stand(self, name, x, y, size, kind="deco"):
        ref = self.icons.ref(name)
        self.stand.append((y, self.icons.use(ref, x, y, size)))
        self.obs.add(icon_box(x, y, size), "icon")

    # ------------------------------------------------------------ trees / waves
    def plant_trees(self):
        t0 = time.time()
        region = self.park.buffer(-8)
        roads = unary_union([l.buffer(13, quad_segs=2) for l in self.road_major] +
                            [l.buffer(9, quad_segs=2) for l in self.road_mid] +
                            [l.buffer(10, quad_segs=2) for l in self.rail])
        excl = [roads, self.water_mac.buffer(6), self.beach.buffer(4), self.airport.buffer(8)]
        for o, k in self.obs.items:
            minx, miny, maxx, maxy = o.bounds
            if k == "icon":
                excl.append(box(minx - 14, miny + 10, maxx + 14, maxy + 30))
            elif k in ("label", "deco"):
                excl.append(box(minx - 10, miny - 4, maxx + 10, maxy + 30))
        for h in self.hills:
            if h["labelled"]:
                excl.append(Point(h["x"], h["y"]).buffer(16))
        exclude = unary_union(excl)
        pts = poisson(region, 24, self.rng, density=50, exclude=exclude)
        beach_near = self.beach.buffer(110)
        shapely.prepare(beach_near)
        hill_pts = [(h["x"], h["y"], 60 + h["h"] * 0.9) for h in self.hills]
        refs = {k: self.icons.ref(k, fallback=self.icons.builtin(k)) for k in ("tree-round", "tree-pine", "tree-palm")}
        n = {"tree-round": 0, "tree-pine": 0, "tree-palm": 0}
        for x, y in pts:
            r = self.prng.random()
            on_hill = any(math.hypot(x - hx, y - hy) < hr for hx, hy, hr in hill_pts)
            if shapely.contains_xy(beach_near, x, y) and r < 0.75:
                kind = "tree-palm"
            elif (on_hill and r < 0.55) or r < 0.22:
                kind = "tree-pine"
            else:
                kind = "tree-round"
            size = self.prng.uniform(27, 36)
            n[kind] += 1
            self.stand.append((y, self.icons.use(refs[kind], x, y, size)))
        self.stats["trees"] = n
        log(f"  trees: {len(pts)} {n} ({time.time() - t0:.1f}s)")

    def scatter_waves(self):
        region = CANVAS.buffer(-40).difference(self.all_land.buffer(115))
        excl = [self.bridge_geom.buffer(60)]
        for o, k in self.obs.items:
            excl.append(o.buffer(30))
        pts = poisson(region, 190, self.rng, density=40, exclude=unary_union(excl))
        out = []
        for x, y in pts:
            s = self.prng.uniform(0.8, 1.2)
            out.append(f'<use href="#wave" transform="translate({fmt(x)},{fmt(y)}) scale({s:.2f})"/>')
        self.stats["waves"] = len(pts)
        return "".join(out)

    # ------------------------------------------------------------ drawing
    def svg(self, labels_json, places):
        self.hills = []
        a = []
        self.place_icons(places)
        self.place_ferries(places)
        self.place_icon_labels()
        self.place_district_labels(labels_json)
        self.place_deco()
        self.place_other_labels(labels_json)
        self.place_region_labels(labels_json)
        self.plant_trees()
        for it in self.icon_items:
            ref = self.icons.ref(it["e"]["id"])
            self.stand.append((it["y"], self.icons.use(ref, it["x"], it["y"], it["size"])))
        for f in self.ferries:
            self.stand.append((f["y"], self.icons.use(f["ref"], f["x"], f["y"], f["size"])))
        waves = self.scatter_waves()

        mac_d = poly_d(self.mac)
        foot = self.foot
        al = self.all_land

        # ---- sea, shallows, ripples, foam
        a.append(f'<rect width="{W}" height="{H}" fill="{SEA}"/>')
        for d, op, sw in ((150, 0.30, 3.5), (100, 0.45, 4), (62, 0.65, 4.5)):
            ring = al.buffer(d, quad_segs=8).simplify(1.5)
            a.append(f'<path d="{poly_d(ring)}" fill="none" stroke="{RIPPLE}" stroke-width="{sw}" '
                     f'stroke-opacity="{op}" stroke-linejoin="round"/>')
        shallow = al.buffer(38, quad_segs=8).simplify(1.2)
        a.append(f'<path d="{poly_d(shallow)}" fill="{SHALLOW}" fill-rule="evenodd"/>')
        a.append(f'<path d="{poly_d(al.buffer(22, quad_segs=6).simplify(1.0))}" fill="none" stroke="#b3e6f3" '
                 f'stroke-width="3" stroke-opacity="0.9"/>')
        a.append(waves)
        foam_oth = self.oth.buffer(7, quad_segs=6).simplify(0.8)
        foam_mac = foot.buffer(10, quad_segs=6).simplify(0.8)
        a.append(f'<path d="{poly_d(foam_oth)}" fill="{FOAM}" fill-opacity="0.75" fill-rule="evenodd"/>')
        a.append(f'<path d="{poly_d(foam_mac)}" fill="{FOAM}" fill-rule="evenodd"/>')

        # ---- neighbour land (Zhuhai / Hengqin): muted context
        a.append(f'<path d="{poly_d(self.oth)}" fill="{OTHER_FILL}" stroke="{OTHER_EDGE}" stroke-width="2.5" '
                 f'stroke-linejoin="round" fill-rule="evenodd"/>')
        if self.water_oth and not self.water_oth.is_empty:
            a.append(f'<path d="{poly_d(self.water_oth)}" fill="#b5dbe3" fill-rule="evenodd"/>')
        if self.beach_oth and not self.beach_oth.is_empty:
            a.append(f'<path d="{poly_d(self.beach_oth)}" fill="#ebe3c4" fill-rule="evenodd"/>')
        a.append(f'<path d="{line_d(self.road_oth)}" fill="none" stroke="{OTHER_ROAD}" stroke-width="6" '
                 f'stroke-linecap="round" stroke-linejoin="round" stroke-opacity="0.9"/>')

        # ---- Macau: raised diorama island
        cliff = affinity.translate(self.mac, 0, CLIFF_DY)
        a.append(f'<path d="{poly_d(cliff)}" fill="{CLIFF}" stroke="{LAND_EDGE}" stroke-width="3.5" '
                 f'stroke-linejoin="round" fill-rule="evenodd"/>')
        for k in ("peninsula", "taipa", "cotai", "coloane"):
            g = self.districts.get(k)
            if g is not None and not g.is_empty:
                a.append(f'<path d="{poly_d(g.simplify(0.3))}" fill="{DISTRICT_FILL[k]}" '
                         f'stroke="{DISTRICT_FILL[k]}" stroke-width="1.2" fill-rule="evenodd"/>')
        # dotted district boundaries
        inner_b = unary_union([g.boundary for g in self.districts.values()]).difference(self.mac.boundary.buffer(10))
        inner_b = [l for l in lines(linemerge(lines(inner_b))) if l.length > 30]
        a.append(f'<path d="{line_d(inner_b)}" fill="none" stroke="#ffffff" stroke-width="5" stroke-opacity="0.85" '
                 f'stroke-linecap="round" stroke-dasharray="0.1 13"/>')
        # inner glow + outline
        a.append(f'<path d="{mac_d}" fill="none" stroke="#ffffff" stroke-width="14" stroke-opacity="0.35" '
                 f'clip-path="url(#macclip)" stroke-linejoin="round"/>')

        # ---- areas on Macau land
        if not self.airport.is_empty:
            a.append(f'<path d="{poly_d(self.airport)}" fill="{AIRPORT}" fill-rule="evenodd"/>')
        if not self.beach.is_empty:
            a.append(f'<path d="{poly_d(self.beach)}" fill="url(#sand)" stroke="#e5c27c" stroke-width="2.5" '
                     f'stroke-linejoin="round" fill-rule="evenodd"/>')
        a.append(f'<path d="{poly_d(self.park)}" fill="{PARK}" stroke="{PARK_EDGE}" stroke-width="3.5" '
                 f'stroke-linejoin="round" fill-rule="evenodd"/>')
        a.append(self.hill_mounds())
        if not self.water_mac.is_empty:
            a.append(f'<path d="{poly_d(self.water_mac)}" fill="{INLAND_SHALLOW}" stroke="#5fb2d2" stroke-width="2.5" '
                     f'stroke-linejoin="round" fill-rule="evenodd"/>')
            deep = drop_small(self.water_mac.buffer(-6), 60)
            if not deep.is_empty:
                a.append(f'<path d="{poly_d(deep)}" fill="{SEA}" fill-rule="evenodd"/>')
        a.append(f'<path d="{mac_d}" fill="none" stroke="{LAND_EDGE}" stroke-width="3.5" stroke-linejoin="round"/>')

        # ---- runway
        a.append(self.runway_svg())

        # ---- roads
        mid_d, maj_d = line_d(self.road_mid), line_d(self.road_major)
        rc = 'fill="none" stroke-linecap="round" stroke-linejoin="round"'
        a.append(f'<path d="{mid_d}" {rc} stroke="{ROAD_EDGE}" stroke-width="10"/>')
        a.append(f'<path d="{maj_d}" {rc} stroke="{ROAD_EDGE}" stroke-width="15"/>')
        a.append(f'<path d="{mid_d}" {rc} stroke="{ROAD_FILL}" stroke-width="5.5"/>')
        a.append(f'<path d="{maj_d}" {rc} stroke="{ROAD_FILL}" stroke-width="9.5"/>')

        # ---- bridges
        a.append(self.bridges_svg())

        # ---- LRT
        rail_d = line_d(self.rail)
        a.append(f'<path d="{rail_d}" {rc} stroke="#ffffff" stroke-width="11"/>')
        a.append(f'<path d="{rail_d}" fill="none" stroke="{RAIL}" stroke-width="5.5" stroke-dasharray="16 9"/>')

        # ---- standing objects (trees, landmarks, boats...) sorted by ground y
        self.stand.sort(key=lambda t: t[0])
        a.append("".join(s for _, s in self.stand))

        # ---- labels: halos first, then fills, per class (districts on top)
        for cls in ("hill", "bridge", "water", "landmark", "region", "icon", "district"):
            labs = self.labels[cls]
            a.append("".join(l.svg_halo() for l in labs))
            a.append("".join(l.svg_fill() for l in labs))

        # ---- clouds, compass, scale bar
        for x, y, s in self.clouds:
            ref = self.icons.ref("cloud")
            a.append(self.icons.use(ref, x, y, s, opacity=0.95))
        a.append(self.compass_svg())
        a.append(self.scalebar_svg())

        defs = self.defs_svg(mac_d)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">\n<defs>{defs}</defs>\n' + "\n".join(a) + "\n</svg>\n")

    def defs_svg(self, mac_d):
        wave = ('<g id="wave" fill="none" stroke="#ffffff" stroke-linecap="round" stroke-linejoin="round" '
                'stroke-opacity="0.9"><path d="M-20 0q5-7 10 0t10 0t10 0t10 0" stroke-width="4"/>'
                '<path d="M-8 10q4-6 8 0t8 0t8 0" stroke-width="3.2" stroke-opacity="0.75"/></g>')
        sand = (f'<pattern id="sand" width="18" height="18" patternUnits="userSpaceOnUse">'
                f'<rect width="18" height="18" fill="{BEACH}"/>'
                f'<circle cx="4" cy="5" r="1.7" fill="{BEACH_DOT}"/><circle cx="13" cy="13" r="1.4" fill="{BEACH_DOT}"/>'
                f'<circle cx="14" cy="3" r="1" fill="{BEACH_DOT}"/></pattern>')
        hill_g = ('<radialGradient id="mound" cx="0.42" cy="0.38" r="0.62">'
                  '<stop offset="0" stop-color="#e9f7c9" stop-opacity="0.85"/>'
                  '<stop offset="0.55" stop-color="#b6e09a" stop-opacity="0.55"/>'
                  '<stop offset="1" stop-color="#5f9f55" stop-opacity="0"/></radialGradient>')
        clip = f'<clipPath id="macclip"><path d="{mac_d}" fill-rule="evenodd"/></clipPath>'
        return clip + wave + sand + hill_g + "".join(self.icons.defs.values())

    def hill_mounds(self):
        out = []
        for h in self.hills:
            r = 70 + h["h"] * 0.75
            x, y = h["x"], h["y"]
            out.append(f'<ellipse cx="{fmt(x)}" cy="{fmt(y + r * 0.12)}" rx="{fmt(r * 1.05)}" ry="{fmt(r * 0.8)}" '
                       f'fill="#6aa95c" fill-opacity="0.22"/>')
            out.append(f'<ellipse cx="{fmt(x)}" cy="{fmt(y)}" rx="{fmt(r)}" ry="{fmt(r * 0.74)}" fill="url(#mound)"/>')
            if h["labelled"]:
                # little summit marker
                out.append(f'<path d="M{fmt(x - 13)},{fmt(y + 6)} L{fmt(x)},{fmt(y - 14)} L{fmt(x + 13)},{fmt(y + 6)}Z" '
                           f'fill="#8c6a4f" stroke="{OUTLINE}" stroke-width="3" stroke-linejoin="round"/>'
                           f'<path d="M{fmt(x - 5)},{fmt(y - 6)} L{fmt(x)},{fmt(y - 14)} L{fmt(x + 5)},{fmt(y - 6)}Z" '
                           f'fill="#ffffff"/>')
        return f'<g clip-path="url(#macclip)">{"".join(out)}</g>'

    def runway_svg(self):
        if self.runway.is_empty:
            return ""
        rr = self.runway.minimum_rotated_rectangle
        c = list(rr.exterior.coords)[:4]
        e1 = (c[0], c[1]); e2 = (c[1], c[2])
        l1 = math.dist(*e1); l2 = math.dist(*e2)
        if l1 < l2:
            a0 = ((c[0][0] + c[1][0]) / 2, (c[0][1] + c[1][1]) / 2)
            a1 = ((c[2][0] + c[3][0]) / 2, (c[2][1] + c[3][1]) / 2)
            width = l1
        else:
            a0 = ((c[1][0] + c[2][0]) / 2, (c[1][1] + c[2][1]) / 2)
            a1 = ((c[3][0] + c[0][0]) / 2, (c[3][1] + c[0][1]) / 2)
            width = l2
        cl = LineString([a0, a1])
        L = cl.length
        inner = LineString([cl.interpolate(0.06, normalized=True).coords[0], cl.interpolate(0.94, normalized=True).coords[0]])
        out = [f'<path d="{poly_d(self.runway.buffer(3))}" fill="{RUNWAY}" stroke="{RUNWAY_EDGE}" stroke-width="3" '
               f'stroke-linejoin="round"/>',
               f'<path d="{line_d([inner])}" fill="none" stroke="#ffffff" stroke-width="4" stroke-dasharray="22 16" '
               f'stroke-linecap="round"/>']
        # threshold bars
        ux, uy = (a1[0] - a0[0]) / L, (a1[1] - a0[1]) / L
        nx, ny = -uy, ux
        bars = []
        for t, sgn in ((0.015, 1), (0.985, -1)):
            p = cl.interpolate(t, normalized=True)
            for k in (-2.5, -1.5, -0.5, 0.5, 1.5, 2.5):
                ox, oy = p.x + nx * k * width * 0.14, p.y + ny * k * width * 0.14
                bars.append(LineString([(ox, oy), (ox + ux * sgn * 18, oy + uy * sgn * 18)]))
        out.append(f'<path d="{line_d(bars)}" fill="none" stroke="#ffffff" stroke-width="3"/>')
        return "".join(out)

    def bridges_svg(self):
        piers, cas, fill = [], [], []
        foot = self.foot.buffer(2)
        shapely.prepare(foot)
        for name, ls in self.bridges:
            for l in ls:
                cas.append(l)
                n = int(l.length // 30)
                for i in range(1, n):
                    p = l.interpolate(i * 30)
                    if shapely.contains_xy(foot, p.x, p.y):
                        continue
                    q = l.interpolate(min(l.length, i * 30 + 2))
                    dx, dy = q.x - p.x, q.y - p.y
                    d = math.hypot(dx, dy) or 1
                    nx, ny = -dy / d, dx / d
                    piers.append(LineString([(p.x - nx * 15, p.y - ny * 15 + 4), (p.x + nx * 15, p.y + ny * 15 + 4)]))
        rc = 'fill="none" stroke-linecap="round" stroke-linejoin="round"'
        return (f'<path d="{line_d(piers)}" {rc} stroke="{BRIDGE_PIER}" stroke-width="5"/>'
                f'<path d="{line_d(cas)}" {rc} stroke="{BRIDGE_EDGE}" stroke-width="17"/>'
                f'<path d="{line_d(cas)}" {rc} stroke="#fbf8f1" stroke-width="10"/>')

    def compass_svg(self):
        cx, cy, cs = self.compass
        out = []
        if self.icons.has("compass"):
            ref = self.icons.ref("compass")
            # icon anchor (80,148) -> centre the 160 box on (cx, cy)
            out.append(self.icons.use(ref, cx, cy + cs * (148 - 80) / 160, cs))
        else:
            r = cs * 0.42
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{fmt(r)}" fill="#fff6e3" stroke="{OUTLINE}" stroke-width="5"/>'
                       f'<path d="M{cx},{fmt(cy - r * 0.95)} L{fmt(cx + r * 0.2)},{cy} L{cx},{fmt(cy + r * 0.95)} '
                       f'L{fmt(cx - r * 0.2)},{cy}Z" fill="#e0512b" stroke="{OUTLINE}" stroke-width="4" stroke-linejoin="round"/>')
        lab = Label("北", cx, cy - cs * 0.5 - 30, 54, "#c8372d", "#ffffff", 14, kind="deco")
        return out[0] + lab.svg_halo() + lab.svg_fill()

    def scalebar_svg(self):
        x0, y0, L = self.scalebar
        hgt = 14
        out = [f'<rect x="{fmt(x0 - 14)}" y="{fmt(y0 - 56)}" width="{fmt(L + 28)}" height="{fmt(hgt + 76)}" rx="20" '
               f'fill="#fff6e3" fill-opacity="0.9" stroke="{OUTLINE}" stroke-width="3"/>',
               f'<rect x="{fmt(x0)}" y="{fmt(y0)}" width="{fmt(L / 2)}" height="{hgt}" fill="{OUTLINE}"/>',
               f'<rect x="{fmt(x0 + L / 2)}" y="{fmt(y0)}" width="{fmt(L / 2)}" height="{hgt}" fill="#ffffff"/>',
               f'<rect x="{fmt(x0)}" y="{fmt(y0)}" width="{fmt(L)}" height="{hgt}" fill="none" stroke="{OUTLINE}" '
               f'stroke-width="3"/>']
        lab = Label("500 米", x0 + L / 2, y0 - 26, 34, OUTLINE, None, 0, kind="deco")
        return "".join(out) + lab.svg_fill()


# ---------------------------------------------------------------- build steps
def find_node_path():
    """tools/node_modules (run `npm install` in tools/) wins; else honour NODE_PATH."""
    if (TOOLS / "node_modules" / "@resvg" / "resvg-js").exists():
        return None
    env = os.environ.get("NODE_PATH")
    if env:
        return env
    sys.exit("@resvg/resvg-js not found: run `npm install` in tools/ "
             "(or set NODE_PATH to a node_modules that contains it)")


def rasterize(svg_path, png_path):
    node = shutil.which("node")
    if not node:
        sys.exit("node not found - install Node.js 18+ to rasterize the SVG")
    env = dict(os.environ)
    np_ = find_node_path()
    if np_:
        env["NODE_PATH"] = np_
    cmd = [node, str(HERE / "rasterize.mjs"), str(svg_path), str(png_path)]
    log("  " + " ".join(cmd))
    subprocess.run(cmd, check=True, env=env)


def to_webp(png_path, sea):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(png_path).convert("RGB")
    assert im.size == (W, H), im.size
    big = ROOT / IMAGE_REL
    small = ROOT / PREVIEW_REL
    big.parent.mkdir(parents=True, exist_ok=True)
    for qb in (82, 78, 74, 70):
        im.save(big, "WEBP", quality=qb, method=6)
        if big.stat().st_size <= 2.5 * 1024 * 1024:
            break
    sm = im.resize((SMALL_W, SMALL_H), Image.LANCZOS)
    for qs in (80, 76, 72, 68):
        sm.save(small, "WEBP", quality=qs, method=6)
        if small.stat().st_size <= 700 * 1024:
            break
    log(f"  {IMAGE_REL}: {W}x{H}, {big.stat().st_size / 1024:.0f} KB (q={qb})")
    log(f"  {PREVIEW_REL}: {SMALL_W}x{SMALL_H}, {small.stat().st_size / 1024:.0f} KB (q={qs})")
    meta = {"bounds": [[S_LAT, W_LNG], [N_LAT, E_LNG]], "width": W, "height": H,
            "image": IMAGE_REL, "preview": PREVIEW_REL, "sea": sea}
    (DATA / "cartoon.json").write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")
    log("  data/cartoon.json written")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--places", help="glob of JSON files with attractions (default data/places.json)")
    ap.add_argument("--cache", default=str(TOOLS / ".cache" / "cartoon"),
                    help="cache dir for Overpass responses (default tools/.cache/cartoon)")
    ap.add_argument("--refresh", action="store_true", help="re-download parish boundaries")
    ap.add_argument("--svg-only", action="store_true", help="only write tools/cartoon/out/cartoon.svg")
    ap.add_argument("--out", default=str(OUT), help="work dir for cartoon.svg / cartoon.png")
    args = ap.parse_args()

    global TM
    TM = TextMetrics()
    t0 = time.time()
    places = load_places(args.places)
    if not places:
        log("  WARNING: no attractions found (data/places.json missing?) - map will have no landmark icons")
    labels = json.loads((DATA / "labels.json").read_text(encoding="utf-8"))
    r = Renderer(args)
    log("preparing geometry ...")
    r.prepare()
    log("laying out ...")
    svg = r.svg(labels, places)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    svg_path = out / "cartoon.svg"
    svg_path.write_text(svg, encoding="utf-8")
    log(f"  {svg_path} ({len(svg) / 1048576:.1f} MB) in {time.time() - t0:.1f}s; "
        f"icons missing: {sorted(r.icons.missing) or 'none'}")
    if args.svg_only:
        return
    png_path = out / "cartoon.png"
    rasterize(svg_path, png_path)
    to_webp(png_path, SEA)
    log(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
