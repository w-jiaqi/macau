#!/usr/bin/env python3
"""Render the illustrated ("Disneyland park map" style) Macau map picture.

The picture is drawn programmatically from the real OpenStreetMap geometry in
data/basemap.geojson (+ the lanes and footpaths in data/basemap-detail.geojson),
in the Web Mercator projection, so it lines up exactly with the real (Leaflet)
map that the frontend keeps underneath it for locations, roads and routing.
Nothing structural is moved: the Macau land fill edge is the real coastline,
and every road, lane, footpath, bridge and the LRT is the real line, simplified
and corner-smoothed by at most ~1.5 px, so routes drawn by the frontend always
run on a drawn way.  Landmark icons stand with their anchor on the attraction's
real coordinates, except where neighbours are so close that the drawings would
pile up: then an icon steps aside (<= ~32 px, ~100 m; up to 130 px for the
sprawling Cotai resorts) under two hard rules - it never covers another sight's
true spot (where the frontend drops its numbered route pin) and its own spot
stays on / within 8 px of its outline.  Names keep >= 28 px off other sights'
spots; the ferry terminals' names sit right beside their true spots, with a
ferry docked in the nearest water.  The picture's border fades to the sea
colour (data/cartoon.json "sea"), so panning past it shows no false coastline.

Georeference (the frontend depends on it - do not change casually):
  bounds  south 22.105, west 113.522, north 22.222, east 113.612 (WGS-84)
  image   3000 x 4211 px (preview 1500 x 2106), Web Mercator:
          x = (lng - W) / (E - W) * width
          y = (mercY(N) - mercY(lat)) / (mercY(N) - mercY(S)) * height
          mercY(lat) = ln(tan(pi/4 + lat_rad/2))

Pipeline:
  tools/cartoon/icons/*.svg
    -> node tools/cartoon/rasterize.mjs --icons  (icon silhouettes, out/icon-shapes.json)
  data/basemap.geojson, data/labels.json, data/places.json, tools/cartoon/icons/*.svg
    -> tools/cartoon/out/cartoon.svg            (one big SVG, gitignored)
    -> node tools/cartoon/rasterize.mjs          (resvg-js + fonts in tools/fonts)
    -> tools/cartoon/out/cartoon.png
    -> assets/map/cartoon.webp     3000 x 4211   (Pillow, quality 82)
       assets/map/cartoon-sm.webp  1500 x 2106   (LANCZOS downscale)
       data/cartoon.json           bounds / sizes / image paths / sea colour

Usage (from the repository root):
  pip install -r tools/requirements.txt     # shapely, requests, pillow, fonttools
  (cd tools && npm install)                 # @resvg/resvg-js
  python3 tools/cartoon/render.py           # full build
  python3 tools/cartoon/render.py --svg-only            # just write the SVG
  python3 tools/cartoon/render.py --places 'dir/*.json' # attractions from JSON arrays
  python3 tools/cartoon/render.py --refresh             # re-download parish boundaries

District colours come from Macau's parish boundaries (OSM, fetched once from
Overpass and cached under tools/.cache/cartoon/); without network a rough
hand-made split is used.  Icons missing from tools/cartoon/icons/ are drawn as
a neutral placeholder badge.  Everything is seeded, so re-runs are identical.

Lettering uses ZCOOL KuaiLe, which has no 氹 (needed for 氹仔 / 路氹城).
load_dang() takes the glyph from tools/fonts/DangHeavy-Subset.otf (a one-glyph
subset of Source Han Sans CN Heavy, Apache-2.0), fits it to KuaiLe's size,
rounds its corners and draws it as a path; the same font is also the resvg
fallback family for 氹 if fontTools is missing.

Map data (c) OpenStreetMap contributors, ODbL 1.0.  Fonts: ZCOOL KuaiLe and
ZCOOL QingKe HuangYou, SIL Open Font License 1.1; the 氹 glyph from Source Han
Sans CN Heavy, (c) 2014 Adobe, Apache License 2.0 (tools/fonts/LICENSE-DangHeavy.txt).
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
from shapely.geometry import LineString, Point, Polygon, box, shape
from shapely.ops import linemerge, polygonize, substring, unary_union
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
# Optional title ribbon drawn in the open sea, e.g. "澳门一日游"; None leaves it out
# (the page already shows the title in its panel).
MAP_TITLE = None


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
    "peninsula": "#feecb0",   # light butter (kept clear of the icons' golds)
    "taipa": "#ffcaa3",       # peach
    "cotai": "#e6d1f4",       # soft lavender
    "coloane": "#dcefc2",     # warm pale green (same family as PARK)
}
PARK = "#a8d98f"
PARK_EDGE = "#74b866"
ROAD_FILL = "#fffaf0"
ROAD_EDGE = "#e0c49c"
LANE_FILL = "#fff6dc"     # mid roads and lanes: no casing, low contrast
PATH_DOT = "#fffdf6"
BRIDGE_EDGE = "#7f9bb5"
BRIDGE_PIER = "#6a87a3"
BRIDGE_TEXT = "#4f7394"
RAIL = "#16a39a"
AIRPORT = "#e3e8ec"
RUNWAY = "#a2abb3"
RUNWAY_EDGE = "#6f7a83"
BEACH = "#f8e2a6"
BEACH_DOT = "#d9b36a"
INLAND_SHALLOW = "#a2e0f0"

FAM_KUAILE = "ZCOOL KuaiLe, Dang Heavy, ZCOOL QingKe HuangYou"
FAM_QINGKE = "ZCOOL QingKe HuangYou, ZCOOL KuaiLe"

FAMOUS = {"ruins-st-paul", "senado-square", "a-ma-temple", "macau-tower",
          "grand-lisboa", "venetian", "parisian", "londoner", "studio-city",
          "a-ma-statue", "panda-pavilion"}
FERRY_IDS = {"outer-harbour": "外港码头", "taipa-ferry": "氹仔码头"}
RESORTS = {"venetian", "parisian", "londoner", "studio-city", "galaxy", "wynn-palace"}
WATER_OK = {"kun-iam", "fishermans-wharf"}   # icons that may stand at / in the water's edge
# icon sizes (px wide in the 3000 px picture): hero scale where there is room
ICON_SIZE = {"venetian": 200, "parisian": 200, "londoner": 200, "studio-city": 200, "galaxy": 200,
             "wynn-palace": 200, "macau-tower": 168, "a-ma-statue": 140, "panda-pavilion": 140,
             "rua-do-cunha": 140, "taipa-houses": 140, "hac-sa-beach": 140, "coloane-village": 140,
             "kun-iam": 120, "fishermans-wharf": 120}
PIN_R_ICON = 22        # px kept clear round other sights' true spots (the frontend's route pins)
PIN_R_LABEL = 28       # ...and round them for text
OWN_SPOT_MAX = 8       # an icon that steps aside must keep its own spot this close to its outline
# shorter display names for a few long data/labels.json entries
LABEL_ALIAS = {"关闸（拱北口岸）": "关闸口岸", "港珠澳大桥澳门口岸": "港珠澳口岸"}

COTAI_SOUTH_LAT = 22.1375   # lavender (Cotai) stops here; south of it is drawn as Coloane
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


def smooth_capped(coords, iters=2, maxdev=1.4):
    """Chaikin-style corner cutting that never moves a line more than ~maxdev px.

    Plain Chaikin cuts every corner by a quarter of its segments, which on a
    sharp junction turn can shift the drawn road 15+ px off the real one.  Here
    the cut at each vertex is limited so the chord stays within maxdev of the
    corner (cut = maxdev / sin(turn / 2)): gentle bends still come out smooth,
    sharp corners and through-junctions stay put.  End points are kept.
    """
    pts = np.asarray(coords, float)
    for _ in range(iters):
        if len(pts) < 3:
            break
        seg = pts[1:] - pts[:-1]
        L = np.hypot(seg[:, 0], seg[:, 1])
        ok = L > 1e-9
        if not ok.all():
            pts = np.vstack([pts[:1], pts[1:][ok]])
            continue
        u = seg / L[:, None]
        cosang = np.clip((u[:-1] * u[1:]).sum(axis=1), -1.0, 1.0)
        sinh = np.sin(np.arccos(cosang) / 2)
        cut = np.minimum(np.minimum(0.25 * L[:-1], 0.25 * L[1:]), maxdev / np.maximum(sinh, 1e-6))
        a = pts[1:-1] - u[:-1] * cut[:, None]
        b = pts[1:-1] + u[1:] * cut[:, None]
        mid = np.empty((2 * len(a), 2))
        mid[0::2] = a
        mid[1::2] = b
        pts = np.vstack([pts[:1], mid, pts[-1:]])
        keep = np.r_[True, np.hypot(*(pts[1:] - pts[:-1]).T) > 0.05]
        pts = pts[keep]
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
DANG = "氹"   # ZCOOL KuaiLe has no 氹 - see load_dang


def _glyph_polys(font, ch, steps=10):
    """A glyph's outline as a shapely geometry in font units (y up, baseline 0)."""
    from fontTools.pens.basePen import BasePen

    class FlatPen(BasePen):
        def __init__(self, gs):
            super().__init__(gs)
            self.rings, self.cur = [], []

        def _moveTo(self, p):
            self.cur = [p]

        def _lineTo(self, p):
            self.cur.append(p)

        def _curveToOne(self, p1, p2, p3):
            p0 = self.cur[-1]
            for i in range(1, steps + 1):
                t = i / steps
                u = 1 - t
                self.cur.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                                 u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))

        def _qCurveToOne(self, p1, p2):
            p0 = self.cur[-1]
            for i in range(1, steps + 1):
                t = i / steps
                u = 1 - t
                self.cur.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                                 u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))

        def _closePath(self):
            if len(self.cur) > 2:
                self.rings.append(self.cur)
            self.cur = []

        _endPath = _closePath

    gs = font.getGlyphSet()
    pen = FlatPen(gs)
    gs[font.getBestCmap()[ord(ch)]].draw(pen)
    g = Polygon()
    for r in pen.rings:
        g = g.symmetric_difference(make_valid(Polygon(r)))
    return make_valid(g)


DANG_FONT = "DangHeavy-Subset.otf"   # one-glyph subset of Source Han Sans CN Heavy (Apache-2.0)
DANG_BOLD_CUT = 0.035                # its strokes are heavier: fatten them this much (x fs) less


def load_dang(kuaile):
    """氹 for the ZCOOL KuaiLe lettering, from a heavy sans whose 氹 reads clearly.

    KuaiLe lacks the character and both earlier stand-ins (QingKe HuangYou's
    glyph and a 乙+水 composed from KuaiLe strokes) read as 飞 once fattened and
    shadowed at map size.  Source Han Sans Heavy draws 水 big and open; the glyph
    is scaled into KuaiLe's ink box (x 20..900, y -60..780 of a 920 advance) and
    its corners are rounded to sit with KuaiLe's soft terminals.  Font units, y up.
    """
    from fontTools.ttLib import TTFont
    g = _glyph_polys(TTFont(str(FONTS / DANG_FONT)), DANG)
    ref = unary_union([_glyph_polys(kuaile, c).envelope for c in "仔城路水"])
    kx0, ky0, kx1, ky1 = ref.bounds
    gx0, gy0, gx1, gy1 = g.bounds
    s = (ky1 - ky0) / (gy1 - gy0)
    adv = kuaile["hmtx"].metrics[kuaile.getBestCmap()[ord("水")]][0]
    g = affinity.scale(g, s, s, origin=(0, 0))
    gx0, gy0, gx1, gy1 = g.bounds
    g = affinity.translate(g, adv / 2 - (gx0 + gx1) / 2, (ky0 + ky1) / 2 - (gy0 + gy1) / 2)
    g = g.buffer(-16, quad_segs=6).buffer(16, quad_segs=6)      # round the convex corners
    g = g.buffer(8, quad_segs=6).buffer(-8, quad_segs=6)        # ...and soften the inner ones
    return make_valid(g.simplify(1.5)), adv


class TextMetrics:
    """Glyph advances from the real fonts (fontTools), with a sane fallback."""

    def __init__(self):
        self.tables = {}
        self.dang = None
        try:
            from fontTools.ttLib import TTFont
            for key, fn in (("kuaile", "ZCOOLKuaiLe-Regular.ttf"),
                            ("qingke", "ZCOOLQingKeHuangYou-Regular.ttf")):
                t = TTFont(str(FONTS / fn))
                self.tables[key] = (t.getBestCmap(), t["hmtx"].metrics, t["head"].unitsPerEm)
                if key == "kuaile":
                    try:
                        upm = self.tables[key][2]
                        self.dang, adv = load_dang(t)
                        self.dang_adv = adv / upm
                        self.dang_upm = upm
                    except Exception as exc:
                        self.dang = None
                        log(f"  (could not load the 氹 glyph: {exc}; resvg falls back to the Dang Heavy font)")
        except Exception as exc:  # pragma: no cover - fontTools is optional
            log(f"  (fontTools unavailable: {exc}; using approximate text widths, fallback-font 氹)")

    def dang_d(self, x, y, fs):
        """SVG path data for the composed 氹 with its baseline origin at (x, y)."""
        k = fs / self.dang_upm
        out = []
        for p in polys(self.dang):
            for ring in [p.exterior] + list(p.interiors):
                cs = list(ring.coords)[:-1]
                out.append("M" + " ".join(f"{fmt(x + gx * k)},{fmt(y - gy * k)}" for gx, gy in cs) + "Z")
        return "".join(out)

    def adv(self, ch, fam):
        if ch == DANG and fam == "kuaile" and self.dang is not None:
            return self.dang_adv
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
                 halo_opacity=None, bold=0.0):
        self.text, self.cx, self.cy, self.fs = text, cx, cy, fs
        self.fill, self.halo = fill, halo
        self.halo_w = halo_w if halo_w is not None else max(6.0, fs * 0.24)
        self.fam, self.ls, self.angle = fam, ls, angle
        self.opacity, self.kind, self.halo_opacity = opacity, kind, halo_opacity
        self.bold = bold          # extra same-colour stroke that fattens the letters
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
        """(kind, x, baseline y, text, font-family, letter-spacing) pieces.

        kind "t" is ordinary text; kind "g" is the composed 氹 drawn as a path
        (only in the ZCOOL KuaiLe family, which lacks the glyph).
        """
        fam = FAM_KUAILE if self.fam == "kuaile" else FAM_QINGKE
        special = self.fam == "kuaile" and TM.dang is not None and DANG in self.text
        y = self.cy + 0.36 * self.fs
        if self.glyphs is None:
            if not special:
                yield "t", self.cx - self.w / 2, y, self.text, fam, self.ls
                return
            x = self.cx - self.w / 2
            buf, bx = "", x
            for c in self.text:
                if c == DANG:
                    if buf:
                        yield "t", bx, y, buf, fam, self.ls
                    yield "g", x, y, c, fam, 0
                    buf = ""
                else:
                    if not buf:
                        bx = x
                    buf += c
                x += TM.adv(c, self.fam) * self.fs + self.ls
            if buf:
                yield "t", bx, y, buf, fam, self.ls
        else:
            for c, gx, gy, ad in self.glyphs:
                kind = "g" if special and c == DANG else "t"
                yield kind, gx - ad / 2, gy + 0.36 * self.fs, c, fam, 0

    def _piece(self, kind, x, y, t, fam, ls, attrs):
        if kind == "g":
            return f'<path d="{TM.dang_d(x, y, self.fs)}" {attrs}/>'
        lsa = f' letter-spacing="{fmt(ls)}"' if ls else ""
        return (f'<text x="{fmt(x)}" y="{fmt(y)}" font-family="{fam}" font-size="{fmt(self.fs)}"{lsa} '
                f'{attrs}>{esc(t)}</text>')

    def svg_halo(self):
        if not self.halo:
            return ""
        op = f' stroke-opacity="{self.halo_opacity}" fill-opacity="{self.halo_opacity}"' if self.halo_opacity else ""
        attrs = (f'fill="{self.halo}" stroke="{self.halo}" stroke-width="{fmt(self.halo_w + self.bold)}" '
                 f'stroke-linejoin="round"{op}')
        return "".join(self._piece(*r, attrs) for r in self._runs())

    def _fill_attrs(self, col, kind, extra=""):
        """Fill plus the same-colour fattening stroke (thinner on the heavy 氹)."""
        b = self.bold if kind != "g" else max(0.0, self.bold - DANG_BOLD_CUT * self.fs)
        fat = f' stroke="{col}" stroke-width="{fmt(b)}" stroke-linejoin="round"' if b > 0.2 else ""
        return f'fill="{col}"{fat}{extra}'

    def svg_fill(self):
        out = []
        op = f' opacity="{self.opacity}"' if self.opacity else ""
        if self.kind == "district":
            # chunky 3-D letters: a darker copy offset down-right under the face
            sd = self.fs * 0.04
            for k, x, y, t, fam, ls in self._runs():
                f = 0.7 if k == "g" else 1.0
                out.append(self._piece(k, x + sd * f, y + sd * 1.3 * f, t, fam, ls,
                                       self._fill_attrs("#5e3119", k)))
        for k, x, y, t, fam, ls in self._runs():
            out.append(self._piece(k, x, y, t, fam, ls, self._fill_attrs(self.fill, k, op)))
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
        self.shapes = {}      # name -> {"bbox": [x0,y0,x1,y1], "bands": [[y0,y1,x0,x1], ...]} (160 box)

    def load_shapes(self, path):
        try:
            self.shapes = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"  (icon silhouettes unavailable: {exc}; using rough boxes)")
            self.shapes = {}

    def rects(self, name, x, y, size, pad=0.0):
        """Silhouette of icon `name` anchored at (x, y) as an (n, 4) array of px rects."""
        k = size / 160.0
        sh = self.shapes.get(name)
        if not sh:
            b = icon_box(x, y, size, pad).bounds
            return np.array([b], float)
        r = np.array(sh["bands"], float)          # y0, y1, x0, x1 in icon units
        return np.column_stack([x + (r[:, 2] - 80) * k - pad, y + (r[:, 0] - 148) * k - pad,
                                x + (r[:, 3] - 80) * k + pad, y + (r[:, 1] - 148) * k + pad])

    def shape(self, name, x, y, size, pad=0.0):
        rs = self.rects(name, x, y, size, pad)
        return unary_union([box(*r) for r in rs]).simplify(0.5)

    def extent(self, name, x, y, size):
        """(left, top, right, bottom) of the icon's opaque part in px."""
        k = size / 160.0
        sh = self.shapes.get(name)
        bx0, by0, bx1, by1 = sh["bbox"] if sh else (5, 10, 155, 150)
        return x + (bx0 - 80) * k, y + (by0 - 148) * k, x + (bx1 - 80) * k, y + (min(by1, 150) - 148) * k

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


def rect_area(a):
    return float(((a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])).sum())


def rect_overlap(a, b):
    """Total overlap area of two sets of axis-aligned rects (rects within a set don't overlap)."""
    if len(a) == 0 or len(b) == 0:
        return 0.0
    ix = np.minimum(a[:, None, 2], b[None, :, 2]) - np.maximum(a[:, None, 0], b[None, :, 0])
    iy = np.minimum(a[:, None, 3], b[None, :, 3]) - np.maximum(a[:, None, 1], b[None, :, 1])
    return float((np.clip(ix, 0, None) * np.clip(iy, 0, None)).sum())


def rects_side(r, y0, y1, side):
    """Rightmost (side>0) / leftmost (side<0) x of the rects that meet the rows y0..y1."""
    m = (r[:, 3] > y0) & (r[:, 1] < y1)
    if not m.any():
        return None
    return float(r[m, 2].max()) if side > 0 else float(r[m, 0].min())


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


def load_detail():
    """Minor streets and footpaths (data/basemap-detail.geojson), which the real
    map and the routes use; drawn faintly so routes always run on a drawn way."""
    p = DATA / "basemap-detail.geojson"
    if not p.exists():
        log("  (data/basemap-detail.geojson missing: no lanes or footpaths drawn)")
        return {}
    js = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for f in js["features"]:
        out.setdefault(f["properties"].get("layer"), []).append(shape(f["geometry"]))
    return {k: unary_union(v) if len(v) > 1 else v[0] for k, v in out.items()}


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
        if isinstance(js, dict):   # data/places.json: {start, end, places: [...]}
            js = [js.get("start"), js.get("end")] + list(js.get("places", []))
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
        self.beach = drop_small(area_clean(pg("beach")).intersection(mac), 150).simplify(0.8)
        # the raised island's cliff shows only over the sea: not over the
        # neighbouring land (UM campus, the Hengqin port) and not under beaches
        cliff = affinity.translate(mac, 0, CLIFF_DY).difference(mac).difference(oth)
        if not self.beach.is_empty:
            cliff = cliff.difference(self.beach.buffer(30))
        self.cliff = drop_small(cliff, 4)
        self.foot = unary_union([mac, self.cliff])
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
        # the parish's thin southern tail (石排湾) would read as a stray lavender
        # strip down Coloane's west side: colour it with Coloane, keep lavender
        # for the Cotai Strip itself
        tail = self.districts["cotai"].intersection(box(-200, P(COTAI_SOUTH_LAT, 113.55)[1], W + 200, H + 200))
        if not tail.is_empty:
            self.districts["cotai"] = area_clean(self.districts["cotai"].difference(tail))
            self.districts["coloane"] = area_clean(unary_union([self.districts["coloane"], tail]))
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
        self.beach_oth = drop_small(area_clean(pg("beach")).intersection(oth), 150).simplify(1.0)
        self.airport = area_clean(pg("airport")).intersection(mac).simplify(0.8)
        self.runway = area_clean(pg("runway"))

        # roads: simplified by <= 1 px and corner-capped smoothing (<= ~1.4 px), so
        # the drawn centrelines stay within ~3 px of the real ones (routes align)
        mac_b = mac.buffer(6)
        self.road_major = self.prep_roads(pg("road-major"), mac_b, simp=1.0, min_comp=30)
        self.road_mid = self.prep_roads(pg("road-mid"), mac_b, simp=1.0, min_comp=40)
        det = load_detail()
        dg = lambda k: proj_geom(det[k]) if k in det else Polygon()
        self.road_minor = self.prep_roads(dg("road-minor"), mac_b, simp=1.0, min_comp=20, maxdev=1.2)
        # footpaths: skip the ones that only shadow a street (separately mapped
        # pavements, crossings) - those are drawn by the street itself
        streets = unary_union([l.buffer(5.5, quad_segs=2) for l in self.road_major + self.road_mid + self.road_minor])
        self.paths = self.prep_roads(dg("path").difference(streets), mac_b, simp=1.0, min_comp=18,
                                     maxdev=1.2, min_piece=10)
        oth_in = oth.buffer(-3)
        self.road_oth = self.prep_roads(pg("road-major"), oth_in, simp=3.0, min_comp=160, maxdev=3.0)
        self.rail = [LineString(smooth_capped(ls.simplify(1.0).coords, 2, 1.4))
                     for ls in lines(linemerge(lines(pg("rail").intersection(CLIP)))) if ls.length > 20]
        self.bridges = []
        for name, g in bridges:
            gp = proj_geom(g).intersection(CLIP)
            merged = lines(linemerge(lines(gp)))
            ls = [LineString(smooth_capped(l.simplify(1.0).coords, 2, 1.4)) for l in merged if l.length > 8]
            self.bridges.append((name, ls))
        self.bridge_geom = unary_union([l for _, ls in self.bridges for l in ls])

    def prep_roads(self, g, clip, simp, min_comp, maxdev=1.4, min_piece=1):
        g = g.intersection(clip)
        ls = [l for l in lines(linemerge(lines(g))) if l.length > min_piece]
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
            out.append(LineString(smooth_capped(l.coords, 2, maxdev)))
        return out

    # ------------------------------------------------------------ layout pieces
    def place_icons(self, places):
        """Landmark icons stand on their attraction's real coordinates.

        Where neighbours are so close that the drawings would pile up (the
        historic centre, the Cotai resorts) an icon may step aside a little:
        up to ~32 px (about 100 m) for ordinary sights, and further for the
        huge resorts, whose buildings really do cover that much ground.  Two
        rules are kept hard, because the frontend drops its numbered route pin
        on every true spot: no icon may cover another sight's spot (grown by
        the pin radius), and every icon keeps its own spot on or within
        OWN_SPOT_MAX px of its outline.
        """
        items = []
        for e in places:
            if e["id"] in FERRY_IDS:
                continue
            x, y = P(e["lat"], e["lng"])
            if not (0 <= x <= W and 0 <= y <= H):
                continue
            pid = e["id"]
            base = float(ICON_SIZE.get(pid, 132.0 if pid in FAMOUS else 106.0))
            maxd = 130.0 if pid in RESORTS else (34.0 if pid in FAMOUS else 32.0)
            items.append({"e": e, "name": pid, "ax": x, "ay": y, "x": x, "y": y,
                          "base": base, "size": base, "maxd": maxd})
        # a little smaller where many sights crowd together (not the roomy resorts)
        for it in items:
            if it["name"] in RESORTS:
                continue
            near = sum(1 for o in items if o is not it and
                       math.hypot(it["ax"] - o["ax"], it["ay"] - o["ay"]) < 170)
            lo = 0.9 if it["name"] in FAMOUS else 0.84
            it["size"] = round(it["base"] * max(lo, 1 - 0.035 * near), 1)

        land = self.mac
        shapely.prepare(land)
        spots = [(it["ax"], it["ay"], id(it)) for it in items] + \
                [(*P(e["lat"], e["lng"]), "ferry") for e in places if e["id"] in FERRY_IDS]
        rings = [(0.0, 0.0)] + [(r * math.cos(a), r * math.sin(a))
                                for r in (0.12, 0.24, 0.36, 0.5, 0.64, 0.78, 0.9, 1.0)
                                for a in np.linspace(0, 2 * math.pi, 24, endpoint=False)]

        def rect_dist(r, px, py):
            dx = np.maximum(np.maximum(r[:, 0] - px, px - r[:, 2]), 0)
            dy = np.maximum(np.maximum(r[:, 1] - py, py - r[:, 3]), 0)
            return float(np.hypot(dx, dy).min())

        order = sorted(items, key=lambda it: (it["name"] not in FAMOUS, -it["size"]))
        for rnd in range(12):
            moved = 0
            for it in order:
                nb = [o for o in items if o is not it and abs(o["x"] - it["x"]) < 460 and abs(o["y"] - it["y"]) < 460]
                orects = [self.icons.rects(o["name"], o["x"], o["y"], o["size"], pad=5) for o in nb]
                nspots = [(sx, sy) for sx, sy, k in spots if k != id(it)
                          and abs(sx - it["ax"]) < 400 and abs(sy - it["ay"]) < 400]
                best = None
                for ux, uy in rings:
                    dx, dy = ux * it["maxd"], uy * it["maxd"]
                    x, y = it["ax"] + dx, it["ay"] + dy
                    r = self.icons.rects(it["name"], x, y, it["size"])
                    a = rect_area(r)
                    ov = sum(rect_overlap(r, o) for o in orects) / a
                    # hard: never cover another sight's true spot (its route pin)
                    pins = sum(1 for sx, sy in nspots if rect_dist(r, sx, sy) < PIN_R_ICON)
                    # hard: keep its own spot on / right next to the drawing
                    own = rect_dist(r, it["ax"], it["ay"])
                    own_pen = 0.0 if own <= OWN_SPOT_MAX else 4.0 + own / 20
                    wet = 0 if it["name"] in WATER_OK or shapely.contains_xy(land, x, y - 6) else 1
                    out = 0 if (r[:, 0].min() > 8 and r[:, 2].max() < W - 8 and r[:, 1].min() > 8) else 1
                    cost = (ov * 6 + pins * 6 + own_pen + 0.35 * (dx * dx + dy * dy) / it["maxd"] ** 2
                            + wet * 3 + out * 3)
                    if best is None or cost < best[0] - 1e-9:
                        best = (cost, x, y, pins, own)
                if abs(best[1] - it["x"]) > 0.01 or abs(best[2] - it["y"]) > 0.01:
                    moved += 1
                it["x"], it["y"] = best[1], best[2]
                it["_pins"], it["_own"] = best[3], best[4]
            if not moved:
                break
        for it in items:
            d = math.hypot(it["x"] - it["ax"], it["y"] - it["ay"])
            if d > 1:
                log(f"  icon {it['name']}: stepped {d:.0f} px aside (own spot {it['_own']:.0f} px from outline)")
            if it["_pins"]:
                log(f"  ! icon {it['name']} still covers {it['_pins']} other true spot(s)")
            self.obs.add(self.icons.shape(it["name"], it["x"], it["y"], it["size"]), "icon")
            # the frontend drops its numbered route pin on the true spot: keep text off it
            self.obs.add(Point(it["ax"], it["ay"]).buffer(PIN_R_ICON, quad_segs=4), "pin")
        self.icon_items = items
        return items

    def place_ferries(self, places):
        """Ferry terminals: the name goes right beside the true spot (where the
        frontend's start / end pin sits) and a ferry is drawn docked in the
        nearest water, within ~140 px; if no water is that close, no boat."""
        out = []
        sea_ok = self.foot.buffer(-2)
        shapely.prepare(sea_ok)
        others = [(it["ax"], it["ay"]) for it in self.icon_items]
        for e in places:
            if e["id"] not in FERRY_IDS:
                continue
            tx, ty = P(e["lat"], e["lng"])
            name = e["id"] if self.icons.has(e["id"]) else "ferry"
            size = 84.0
            best = None
            for r in range(34, 141, 6):
                for k in range(36):
                    a = 2 * math.pi * k / 36
                    x, y = tx + r * math.cos(a), ty + r * math.sin(a)
                    rs = self.icons.rects(name, x, y, size)
                    if rs[:, 0].min() < 10 or rs[:, 2].max() > W - 10 or rs[:, 1].min() < 10:
                        continue
                    # the hull's waterline must be in water (the bow may touch a pier)
                    if shapely.contains_xy(sea_ok, x, y - 6) or shapely.contains_xy(sea_ok, x - size * 0.3, y - 8) \
                            or shapely.contains_xy(sea_ok, x + size * 0.3, y - 8):
                        continue
                    sh = self.icons.shape(name, x, y, size)
                    if self.bridge_geom.intersects(sh.buffer(6)):
                        continue
                    landf = sh.intersection(self.foot).area / sh.area
                    if landf > 0.35:
                        continue
                    pins = sum(1 for ox, oy in others if sh.distance(Point(ox, oy)) < PIN_R_ICON)
                    ownc = 1 if sh.distance(Point(tx, ty)) < 14 else 0
                    pen = self.obs.overlap(sh, kinds={"icon", "label"}) / sh.area
                    score = r + landf * 160 + pen * 300 + pins * 400 + ownc * 60
                    if best is None or score < best[0]:
                        best = (score, x, y)
            item = {"e": e, "name": name, "x": tx, "y": ty, "size": 0.0, "ref": None, "boat": None,
                    "label": FERRY_IDS[e["id"]], "tx": tx, "ty": ty, "ax": tx, "ay": ty}
            if best:
                _, x, y = best
                item["boat"] = (x, y, size)
                item["ref"] = self.icons.ref(name)
                self.obs.add(self.icons.shape(name, x, y, size), "icon")
                log(f"  ferry {e['id']}: boat {math.hypot(x - tx, y - ty):.0f} px from the terminal")
            else:
                log(f"  ferry {e['id']}: no water within 140 px - name only")
            self.obs.add(Point(tx, ty).buffer(PIN_R_ICON, quad_segs=4), "pin")
            out.append(item)
        self.ferries = out

    def place_icon_labels(self):
        todo = []
        for it in self.icon_items:
            todo.append((it, it["e"].get("short") or it["e"].get("name")))
        for f in self.ferries:
            todo.append((f, f["label"]))
        # famous first, then top-to-bottom
        todo.sort(key=lambda t: (0 if t[0]["e"]["id"] in FAMOUS else 1, t[0]["y"]))
        is_ferry = lambda it: it["e"]["id"] in FERRY_IDS

        def anchor(it):
            return (it["tx"], it["ty"]) if is_ferry(it) else (it["x"], it["y"] - it["size"] * 0.45)

        anchors = [(*anchor(it), id(it)) for it, _ in todo]
        icon_boxes = []
        for it, _ in todo:
            wt = 2.8 if it["e"]["id"] in FAMOUS else 1.5
            if is_ferry(it):
                if it["boat"]:
                    bx, by, bs = it["boat"]
                    icon_boxes.append((self.icons.shape(it["name"], bx, by, bs), "boat", 1.5))
            else:
                icon_boxes.append((self.icons.shape(it["name"], it["x"], it["y"], it["size"]), id(it), wt))
        # base strips of every icon: a name just under a neighbour's base reads as that icon's name
        bases = []
        for it, _ in todo:
            if is_ferry(it):
                continue
            L_, T_, R_, B_ = self.icons.extent(it["name"], it["x"], it["y"], it["size"])
            bases.append((box(L_ + 6, B_, R_ - 6, B_ + 30), id(it)))
        static = [o for o, k in self.obs.items if k in ("label", "deco")]
        # every sight's true spot (its route pin): a hard keep-out for other sights'
        # names, and a smaller one for its own name so the pin doesn't hide it
        spots = [(it["ax"], it["ay"], id(it)) for it in self.icon_items] + \
                [(f["tx"], f["ty"], id(f)) for f in self.ferries]
        entries = []
        for it, text in todo:
            fs = 48 if it["e"]["id"] in FAMOUS else 44
            lab = Label(text, it["x"], it["y"], fs, OUTLINE, "#ffffff", 11, kind="icon", bold=fs * 0.04)
            w, h = lab.w, fs
            cands = []
            if is_ferry(it):
                tx, ty = it["tx"], it["ty"]
                g = 31          # just clear of the 40 px terminal pin at z15+
                cands += [(0.0, (tx, ty + g + h / 2)), (0.15, (tx + g + w / 2, ty)), (0.15, (tx - g - w / 2, ty)),
                          (0.3, (tx, ty - g - h / 2)), (0.4, (tx + w * 0.3, ty + g + h / 2)),
                          (0.4, (tx - w * 0.3, ty + g + h / 2)), (0.5, (tx + g + w / 2, ty + h * 0.6)),
                          (0.5, (tx - g - w / 2, ty + h * 0.6)), (0.5, (tx + g + w / 2, ty - h * 0.6)),
                          (0.5, (tx - g - w / 2, ty - h * 0.6)), (1.2, (tx, ty + g + h * 1.5))]
            else:
                x, y, s = it["x"], it["y"], it["size"]
                R = self.icons.rects(it["name"], x, y, s)
                L_, T_, R_, B_ = self.icons.extent(it["name"], x, y, s)

                def side(cy, sgn):
                    e = rects_side(R, cy - h / 2 - 4, cy + h / 2 + 4, sgn)
                    e = e if e is not None else x
                    return e + sgn * (w / 2 + 7)

                below = max(B_ + 5, it["ay"] + 16)
                for pref, cy in ((0.0, below + h / 2), (2.2, below + 5 + h * 1.5)):
                    cands.append((pref, (x, cy)))
                    if pref == 0.0:
                        cands += [(0.5, (x + w * 0.28, cy)), (0.5, (x - w * 0.28, cy))]
                for pref, cy in ((0.8, y - s * 0.30), (1.0, y - h * 0.20), (1.4, y - s * 0.62)):
                    cands += [(pref, (side(cy, 1), cy)), (pref, (side(cy, -1), cy))]
                cands.append((1.4, (x, T_ - h / 2 - 4)))
            entries.append({"it": it, "lab": lab, "cands": cands, "box": None})

        def score(e, cx, cy, others):
            it, lab = e["it"], e["lab"]
            w, h = lab.w, lab.fs
            b = box(cx - w / 2 - 3, cy - h / 2 - 2, cx + w / 2 + 3, cy + h / 2 + 2)
            bp = box(cx - w / 2 - 14, cy - h / 2 - 9, cx + w / 2 + 14, cy + h / 2 + 9)   # keep a gap
            out = b.area - b.intersection(CANVAS.buffer(-6)).area
            ov_l = sum(bp.intersection(o).area for o in others if o.intersects(bp))
            ov_l += sum(bp.intersection(o).area for o in static if o.intersects(bp))
            ov_i = sum(b.intersection(ib).area * wt for ib, k, wt in icon_boxes if k != id(it) and ib.intersects(b))
            hard = 0.0
            near_other = 1e9
            for sx, sy, k in spots:
                dd = b.distance(Point(sx, sy))
                if k == id(it):
                    if dd < 14:
                        hard += 0.6          # its own pin would hide part of the name
                else:
                    near_other = min(near_other, dd)
                    if dd < PIN_R_LABEL:
                        hard += 3.0          # never under another sight's pin
            # ...and the nearest true spot should be its own
            own_d = b.distance(Point(it["ax"], it["ay"]))
            if near_other < own_d - 4:
                hard += 0.5
            under = sum(1 for bb, k in bases if k != id(it) and bb.intersects(b))
            hard += 0.6 * under
            # association: the label should be nearer its own icon than any other
            ax_, ay_ = anchor(it)
            own = math.hypot(cx - ax_, cy - ay_)
            near = min((math.hypot(cx - ax, cy - ay) for ax, ay, k in anchors if k != id(it)), default=1e9)
            assoc = 0.0 if own <= near * 0.95 else min(1.0, (own - near * 0.95) / 60.0)
            # ...and, all else equal, sit on the side facing away from the neighbours
            soft = max(0.0, own / max(near, 1.0) - 0.55) * 0.25
            return (ov_l * 3.0 + ov_i + out * 5) / b.area + assoc * 0.9 + soft + hard, b

        for rnd in range(5):
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
                e["box"], e["pos"], e["score"] = best[3], (best[1], best[2]), best[0]
            if not changed:
                break
        for e in entries:
            lab = e["lab"]
            cx, cy = e["pos"]
            lab.move(cx - lab.cx, cy - lab.cy)
            if e["score"] > 2.5:
                log(f"  ! label {lab.text}: no clean spot (score {e['score']:.2f})")
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
            fs = 112 if len(lb["text"]) > 2 else 120
            # chunky ZCOOL KuaiLe (fattened with a same-colour stroke); its 氹 is composed
            lab = Label(lb["text"], x0, y0, fs, "#a15a32", "#ffffff", 20, fam="kuaile", ls=fs * 0.07,
                        kind="district", bold=fs * 0.05)
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
                    ov = self.obs.overlap(b, kinds={"icon", "label", "pin", "summit"})
                    # prefer open space: things within ~45 px of the label count a little too
                    ov_near = self.obs.overlap(b.buffer(60, join_style=2), kinds={"icon", "label"}) - ov
                    score = (ov * 4 + ov_near * 2.0 + outside * 2.0) / b.area + d / 640 * 0.3
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

    def try_place(self, lab, kinds=("icon", "label", "pin"), max_frac=0.12, nudges=None, register=True,
                  land=False):
        """First nudge that is clear enough; with land=True (water names) the
        least-on-land of the clear nudges wins instead."""
        self.fit_inside(lab)
        nudges = nudges or [(0, 0), (0, 30), (0, -30), (30, 0), (-30, 0), (0, 60), (0, -60),
                            (60, 0), (-60, 0), (40, 40), (-40, 40), (40, -40), (-40, -40)]
        base = (lab.cx, lab.cy)
        best = None
        for i, (dx, dy) in enumerate(nudges):
            lab.move(base[0] + dx - lab.cx, base[1] + dy - lab.cy)
            self.fit_inside(lab)
            b = lab.box.buffer(3, join_style=2)
            ov = self.obs.overlap(b, kinds=set(kinds))
            if ov <= max_frac * b.area:
                if not land:
                    best = (0, lab.cx, lab.cy)
                    break
                lf = b.intersection(self.all_land).area / b.area + i * 0.004
                if best is None or lf < best[0]:
                    best = (lf, lab.cx, lab.cy)
        if best is None:
            return False
        lab.move(best[1] - lab.cx, best[2] - lab.cy)
        if register:
            self.obs.add(lab.box.buffer(3, join_style=2), "label")
        return True

    def place_other_labels(self, labels):
        for lb in labels:
            t, text = lb["type"], LABEL_ALIAS.get(lb["text"], lb["text"])
            x, y = P(lb["lat"], lb["lng"])
            if t == "region":
                continue
            if t in ("water", "sea"):
                if text.endswith("水库"):
                    continue            # reservoirs: nothing a visitor needs
                mn = lb.get("min", 13)
                if t == "sea":
                    fs, ls = 62, 14
                else:
                    fs, ls = {12: 60, 13: 52, 14: 48}.get(mn, 44), 8
                mk = lambda **kw: Label(text, x, y, fs, "#3b8dbb", "#ffffff", max(8, fs * 0.2), kind="water",
                                        halo_opacity=0.7, **kw)
                lab = mk(ls=ls, angle=lb.get("angle"))
                # narrow channel: stack vertically if the straight run would sit on land
                if lab.angle is None and t == "water":
                    land_frac = lab.box.intersection(self.all_land).area / lab.box.area
                    if land_frac > 0.25:
                        lab2 = mk(ls=ls * 0.5, angle=90)
                        if lab2.box.intersection(self.all_land).area / lab2.box.area < land_frac:
                            lab = lab2
                if self.try_place(lab, max_frac=0.10, land=t == "water"):
                    self.labels["water"].append(lab)
            elif t == "bridge":
                lab = self.bridge_label(text, x, y, lb.get("angle"))
                if lab is not None:
                    self.labels["bridge"].append(lab)
            elif t == "landmark":
                lab = Label(text, x, y, 40, "#5a3d7a", "#ffffff", 10, ls=1, kind="landmark", bold=1.2)
                if self.try_place(lab, max_frac=0.10):
                    self.labels["landmark"].append(lab)
            elif t == "hill":
                name, _, height = text.partition(" ")
                lab = Label(name, x, y + 36, 36, "#2c6a43", "#ffffff", 9, ls=2, kind="hill", bold=1.0)
                near_icon = any(math.hypot(it["x"] - x, it["y"] - y) < 75 for it in self.icon_items)
                ok = (not near_icon) and self.try_place(
                    lab, max_frac=0.10, nudges=[(0, 0), (0, 26), (26, 0), (-26, 0), (0, 50)])
                if ok:
                    self.labels["hill"].append(lab)
                self.hills.append({"text": name, "x": x, "y": y, "h": float(height.rstrip("m") or 60),
                                   "labelled": ok})

    def bridge_label(self, text, x, y, angle):
        """Name beside the bridge (not on it), on the visible over-water part."""
        segs = [l for name, ls in self.bridges if name == text for l in ls]
        fs = 32
        if not segs:
            lab = Label(text, x, y, fs, BRIDGE_TEXT, "#ffffff", 9, ls=2, angle=angle, kind="bridge")
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
                lab = Label(text, cx, cy, fs, BRIDGE_TEXT, "#ffffff", 9, ls=2, angle=ang, kind="bridge")
                bb = lab.box
                if not CANVAS.buffer(-12).contains(bb):
                    continue
                ov = self.obs.overlap(bb.buffer(3), kinds={"icon", "label", "deco", "pin"}) / bb.area
                land = sum(gl.intersection(bb).area for gl in [self.all_land]) / bb.area
                onbridge = g.buffer(8).intersection(bb).area / bb.area
                sc = ov * 3 + land * 2.0 + onbridge * 2 + abs(along) / 260 + (0.05 if side < 0 else 0)
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
            avail = self.oth.intersection(CANVAS.buffer(-130))
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
        # title ribbon in the open north-east sea (clear of the frontend's
        # top-right tool buttons at the start-up view, and of every feature)
        self.banner = self.make_banner(MAP_TITLE, 2290, 1010, 78) if MAP_TITLE else None
        if self.banner:
            self.obs.add(self.banner["box"], "deco")
        # clouds
        self.clouds = [(2790, 300, 280), (290, 4000, 280)]
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

    def make_banner(self, text, cx, cy, fs):
        lab = Label(text, cx, cy - 4, fs, "#c8372d", "#ffffff", 14, ls=fs * 0.08, kind="deco", bold=fs * 0.035)
        w = lab.w + 90
        h = fs * 1.3
        x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
        return {"lab": lab, "x0": x0, "x1": x1, "y0": y0, "y1": y1, "cx": cx,
                "box": box(x0 - 80, y0 - 30, x1 + 80, y1 + 34)}

    def banner_svg(self):
        b = self.banner
        x0, x1, y0, y1, cx = b["x0"], b["x1"], b["y0"], b["y1"], b["cx"]
        arch, drop, tail = 20, 24, 78
        o = f'stroke="{OUTLINE}" stroke-width="5" stroke-linejoin="round"'
        out = []
        for sgn, xe in ((-1, x0), (1, x1)):
            xi = xe - sgn * 26          # where the tail tucks under the band
            xt = xe + sgn * tail        # swallow-tail tip
            ym = (y0 + y1) / 2 + drop
            out.append(f'<path d="M{fmt(xi)},{fmt(y0 + drop)} L{fmt(xt)},{fmt(y0 + drop)} L{fmt(xt - sgn * 26)},{fmt(ym)} '
                       f'L{fmt(xt)},{fmt(y1 + drop)} L{fmt(xi)},{fmt(y1 + drop)} Z" fill="#e0512b" {o}/>')
            out.append(f'<path d="M{fmt(xe)},{fmt(y1)} L{fmt(xi)},{fmt(y1 + drop)} L{fmt(xi)},{fmt(y1)} Z" '
                       f'fill="#c8372d" {o}/>')
        out.append(f'<path d="M{fmt(x0)},{fmt(y0)} Q{fmt(cx)},{fmt(y0 - arch)} {fmt(x1)},{fmt(y0)} L{fmt(x1)},{fmt(y1)} '
                   f'Q{fmt(cx)},{fmt(y1 - arch)} {fmt(x0)},{fmt(y1)} Z" fill="#fff6e3" {o}/>')
        out.append(f'<path d="M{fmt(x0 + 14)},{fmt(y0 + 12)} Q{fmt(cx)},{fmt(y0 - arch + 12)} {fmt(x1 - 14)},{fmt(y0 + 12)}" '
                   f'fill="none" stroke="#f2c14e" stroke-width="4" stroke-linecap="round" stroke-dasharray="2 12"/>')
        lab = b["lab"]
        return "".join(out) + lab.svg_halo() + lab.svg_fill()

    def add_stand(self, name, x, y, size, kind="deco"):
        ref = self.icons.ref(name)
        self.stand.append((y, self.icons.use(ref, x, y, size)))
        self.obs.add(icon_box(x, y, size), "icon")

    # ------------------------------------------------------------ trees / waves
    def value_noise(self, cell=280, seed=7):
        """Smooth seeded noise in 0..1 (bilinear value noise), for open meadows."""
        rng = np.random.default_rng(SEED + seed)
        gw, gh = int(W / cell) + 3, int(H / cell) + 3
        g = rng.random((gh, gw))

        def f(x, y):
            fx, fy = x / cell, y / cell
            ix, iy = int(fx), int(fy)
            tx, ty = fx - ix, fy - iy
            tx, ty = tx * tx * (3 - 2 * tx), ty * ty * (3 - 2 * ty)
            a = g[iy, ix] * (1 - tx) + g[iy, ix + 1] * tx
            b = g[iy + 1, ix] * (1 - tx) + g[iy + 1, ix + 1] * tx
            return a * (1 - ty) + b * ty
        return f

    def plant_trees(self):
        """Fewer, bigger trees: parks and hills get a loose forest with open
        meadows and bare hilltops; palms by the beaches and in rows along the
        Cotai boulevards; a few clumps in Cotai's empty blocks."""
        t0 = time.time()
        roads = unary_union([l.buffer(13, quad_segs=2) for l in self.road_major] +
                            [l.buffer(9, quad_segs=2) for l in self.road_mid] +
                            [l.buffer(7, quad_segs=2) for l in self.road_minor] +
                            [l.buffer(6, quad_segs=2) for l in self.paths] +
                            [l.buffer(10, quad_segs=2) for l in self.rail])
        excl = [roads, self.water_mac.buffer(8), self.beach.buffer(6), self.airport.buffer(10)]
        for o, k in self.obs.items:
            minx, miny, maxx, maxy = o.bounds
            if k == "icon":
                excl.append(box(minx - 18, miny + 10, maxx + 18, maxy + 40))
            elif k in ("label", "deco"):
                excl.append(box(minx - 12, miny - 6, maxx + 12, maxy + 44))
            elif k == "pin":
                excl.append(o.buffer(6))
        hill_pts = [(h["x"], h["y"], 70 + h["h"] * 0.75) for h in self.hills]
        for hx, hy, hr in hill_pts:          # bare summits so the mounds read as hills
            excl.append(Point(hx, hy).buffer(hr * 0.45))
        exclude = unary_union(excl)
        noise = self.value_noise()
        refs = {k: self.icons.ref(k, fallback=self.icons.builtin(k)) for k in ("tree-round", "tree-pine", "tree-palm")}
        n = {"tree-round": 0, "tree-pine": 0, "tree-palm": 0}
        beach_near = self.beach.buffer(120)
        shapely.prepare(beach_near)
        placed = []

        def put(kind, x, y, size):
            n[kind] += 1
            placed.append((x, y))
            self.stand.append((y, self.icons.use(refs[kind], x, y, size)))

        # parks / forest
        for x, y in poisson(self.park.buffer(-10), 42, self.rng, density=40, exclude=exclude):
            if noise(x, y) < 0.3:
                continue                       # an open meadow
            r = self.prng.random()
            on_hill = any(math.hypot(x - hx, y - hy) < hr * 1.15 for hx, hy, hr in hill_pts)
            if shapely.contains_xy(beach_near, x, y) and r < 0.7:
                kind = "tree-palm"
            elif (on_hill and r < 0.6) or r < 0.1:
                kind = "tree-pine"
            else:
                kind = "tree-round"
            put(kind, x, y, self.prng.uniform(40, 52))
        self.stats["forest"] = sum(n.values())

        # palm rows along the Cotai Strip's boulevards
        cotai = self.districts.get("cotai", Polygon()).difference(self.airport.buffer(20))
        shapely.prepare(cotai)
        shapely.prepare(exclude)
        rows = []
        for l in self.road_major:
            if not l.intersects(cotai):
                continue
            for t in np.arange(30, l.length - 30, 58):
                p, q = l.interpolate(t), l.interpolate(min(l.length, t + 2))
                dx, dy = q.x - p.x, q.y - p.y
                d = math.hypot(dx, dy) or 1
                for sgn in (1, -1):
                    x, y = p.x - dy / d * 20 * sgn, p.y + dx / d * 20 * sgn
                    rows.append((x, y))
        for x, y in rows:
            if not shapely.contains_xy(cotai, x, y) or shapely.contains_xy(exclude, x, y):
                continue
            if any((x - px) ** 2 + (y - py) ** 2 < 36 ** 2 for px, py in placed[-400:]):
                continue
            put("tree-palm", x, y, 44)
        # a few round-tree clumps in Cotai's big empty blocks
        open_ = cotai.difference(exclude).buffer(-34)
        for x, y in poisson(open_, 46, self.rng, density=30):
            if noise(x + 1000, y) < 0.55:
                continue
            put("tree-round", x, y, self.prng.uniform(40, 50))
        self.stats["trees"] = n
        log(f"  trees: {sum(n.values())} {n} ({time.time() - t0:.1f}s)")

    def sea_rings(self, d, step=6.0):
        """Ripple line `d` px off every coast, rounded, and left out wherever the
        opposite shore is close (narrow channels get just the shallow band)."""
        al = self.all_land
        ring = al.buffer(d + 20, quad_segs=8).buffer(-20, quad_segs=8)
        ls = [l.simplify(1.2) for l in lines(ring.boundary.intersection(CANVAS.buffer(-8))) if l.length > 60]
        if not ls:
            return []
        # nearest-shore lookup through small pieces of the coastline
        chunks = []
        for c in lines(al.boundary):
            cs = list(c.coords)
            for i in range(0, len(cs) - 1, 24):
                seg = cs[i:i + 25]
                if len(seg) > 1:
                    chunks.append(LineString(seg))
        tree = STRtree(chunks)
        carr = np.array(chunks, dtype=object)
        reach = d * 1.2 + 60
        res = []
        for l in ls:
            ts = np.arange(0, l.length, step)
            pts = shapely.line_interpolate_point(l, ts)
            near = shapely.shortest_line(pts, carr[tree.nearest(pts)])
            xy = shapely.get_coordinates(near).reshape(-1, 2, 2)
            v = xy[:, 0] - xy[:, 1]
            u = v / np.maximum(np.hypot(v[:, 0], v[:, 1]), 1e-6)[:, None]
            rays = shapely.linestrings(np.stack([xy[:, 0] + u * 3, xy[:, 0] + u * reach], axis=1))
            keep = ~shapely.intersects(rays, al)      # no opposite shore within reach
            runs, run = [], []
            for ok, t in zip(keep.tolist(), ts.tolist()):
                if ok:
                    run.append(t)
                elif run:
                    runs.append(run)
                    run = []
            if run:
                runs.append(run)
            for r in runs:
                if len(r) > 1 and r[-1] - r[0] >= 90:
                    piece = substring(l, r[0], r[-1])
                    res.append(LineString(chaikin(piece.coords, 2)) if len(piece.coords) > 2 else piece)
        return res

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
        for lb in labels_json:      # keep the big district names off the hill summits
            if lb["type"] == "hill":
                self.obs.add(Point(*P(lb["lat"], lb["lng"])).buffer(30), "summit")
        self.place_district_labels(labels_json)
        self.place_deco()
        self.place_other_labels(labels_json)
        self.place_region_labels(labels_json)
        self.plant_trees()
        for it in self.icon_items:
            ref = self.icons.ref(it["e"]["id"])
            self.stand.append((it["y"], self.icons.use(ref, it["x"], it["y"], it["size"])))
        for f in self.ferries:
            if f["boat"]:
                bx, by, bs = f["boat"]
                self.stand.append((by, self.icons.use(f["ref"], bx, by, bs)))
        waves = self.scatter_waves()

        mac_d = poly_d(self.mac)
        foot = self.foot
        al = self.all_land

        # ---- sea, shallows, ripples, foam
        a.append(f'<rect width="{W}" height="{H}" fill="{SEA}"/>')
        for d, op in ((100, 0.38), (62, 0.5)):
            a.append(f'<path d="{line_d(self.sea_rings(d))}" fill="none" stroke="#ffffff" stroke-width="4" '
                     f'stroke-opacity="{op}" stroke-dasharray="28 20" stroke-linecap="round" stroke-linejoin="round"/>')
        shallow = al.buffer(38, quad_segs=8).simplify(1.2)
        a.append(f'<path d="{poly_d(shallow)}" fill="{SHALLOW}" fill-rule="evenodd"/>')
        a.append(f'<path d="{poly_d(al.buffer(22, quad_segs=6).simplify(1.0))}" fill="none" stroke="#b3e6f3" '
                 f'stroke-width="3" stroke-opacity="0.9"/>')
        a.append(waves)
        foam_oth = self.oth.buffer(7, quad_segs=6).simplify(0.8)
        foam_mac = foot.buffer(10, quad_segs=6).simplify(0.8)
        a.append(f'<path d="{poly_d(foam_oth)}" fill="{FOAM}" fill-opacity="0.75" fill-rule="evenodd"/>')
        a.append(f'<path d="{poly_d(foam_mac)}" fill="{FOAM}" fill-rule="evenodd"/>')
        a.append("<!--BRIDGE_SHADOW-->")

        # ---- neighbour land (Zhuhai / Hengqin): muted context
        a.append(f'<path d="{poly_d(self.oth)}" fill="{OTHER_FILL}" stroke="{OTHER_EDGE}" stroke-width="2.5" '
                 f'stroke-linejoin="round" fill-rule="evenodd"/>')
        if self.water_oth and not self.water_oth.is_empty:
            a.append(f'<path d="{poly_d(self.water_oth)}" fill="#b5dbe3" fill-rule="evenodd"/>')
        if self.beach_oth and not self.beach_oth.is_empty:
            a.append(f'<path d="{poly_d(self.beach_oth)}" fill="#ebe3c4" fill-rule="evenodd"/>')
        a.append(f'<path d="{line_d(self.road_oth)}" fill="none" stroke="{OTHER_ROAD}" stroke-width="6" '
                 f'stroke-linecap="round" stroke-linejoin="round" stroke-opacity="0.9"/>')

        # ---- Macau: raised diorama island (cliff only over the sea, never under beaches)
        a.append(f'<path d="{poly_d(self.cliff)}" fill="{CLIFF}" stroke="{LAND_EDGE}" stroke-width="3.5" '
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
        beach_draw = Polygon()
        if not self.beach.is_empty:
            # the sand reaches ~7 px past the coastline into the surf (still well inside
            # alignment tolerance) so a beach reads as a beach, not a cliff edge
            other_land = self.mac.difference(self.beach.buffer(1))
            beach_draw = unary_union([self.beach, self.beach.buffer(7, quad_segs=4).difference(other_land)])
            beach_draw = drop_small(beach_draw, 150)
            a.append(f'<path d="{poly_d(beach_draw)}" fill="url(#sand)" stroke="#e5c27c" stroke-width="2.5" '
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
        coast = self.mac.boundary
        if not beach_draw.is_empty:
            coast = coast.difference(beach_draw.buffer(2))
        a.append(f'<path d="{line_d(coast)}" fill="none" stroke="{LAND_EDGE}" stroke-width="3.5" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
        if not beach_draw.is_empty:
            # white scalloped surf line just off each beach
            surf = beach_draw.buffer(9, quad_segs=6).boundary.difference(self.mac.buffer(6))
            surf = [l for l in lines(surf) if l.length > 30]
            a.append(f'<path d="{line_d(surf)}" fill="none" stroke="#ffffff" stroke-width="5" '
                     f'stroke-dasharray="14 10" stroke-linecap="round"/>')

        # ---- runway
        a.append(self.runway_svg())

        # ---- roads: footpaths (dots) < lanes < mid roads < main roads
        mid_d, maj_d = line_d(self.road_mid), line_d(self.road_major)
        rc = 'fill="none" stroke-linecap="round" stroke-linejoin="round"'
        a.append(f'<path d="{line_d(self.paths)}" {rc} stroke="{PATH_DOT}" stroke-width="3.4" '
                 f'stroke-dasharray="0.1 7.5" stroke-opacity="0.95"/>')
        a.append(f'<path d="{line_d(self.road_minor)}" {rc} stroke="{LANE_FILL}" stroke-width="3.4" stroke-opacity="0.9"/>')
        a.append(f'<path d="{mid_d}" {rc} stroke="{LANE_FILL}" stroke-width="5.5"/>')
        a.append(f'<path d="{maj_d}" {rc} stroke="{ROAD_EDGE}" stroke-width="13"/>')
        a.append(f'<path d="{maj_d}" {rc} stroke="{ROAD_FILL}" stroke-width="10"/>')

        # ---- bridges (their shadow went in under the land, see BRIDGE_SHADOW)
        a.append(self.bridges_svg())
        a[a.index("<!--BRIDGE_SHADOW-->")] = self.bridge_shadow

        # ---- LRT
        rail_d = line_d(self.rail)
        a.append(f'<path d="{rail_d}" {rc} stroke="#ffffff" stroke-width="11"/>')
        a.append(f'<path d="{rail_d}" fill="none" stroke="{RAIL}" stroke-width="5.5" stroke-dasharray="16 9"/>')

        # ---- soft edge: neighbour land and water fade into the sea colour at the
        # picture's border, so panning past it shows no hard false coastline
        a.append(self.edge_fade_svg())

        # ---- cream plazas under the landmarks, so they sit on a little stage
        a.append(self.plazas_svg())

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
        if self.banner:
            a.append(self.banner_svg())

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
        clip = (f'<clipPath id="macclip"><path d="{mac_d}" fill-rule="evenodd"/></clipPath>'
                f'<clipPath id="parkclip"><path d="{poly_d(self.park)}" fill-rule="evenodd"/></clipPath>')
        hill_g += ('<radialGradient id="moundshade" cx="0.5" cy="0.5" r="0.5">'
                   '<stop offset="0.55" stop-color="#3f8f5a" stop-opacity="0.16"/>'
                   '<stop offset="1" stop-color="#3f8f5a" stop-opacity="0"/></radialGradient>')
        return clip + wave + sand + hill_g + "".join(self.icons.defs.values())

    def hill_mounds(self):
        out, marks = [], []
        for h in self.hills:
            r = 70 + h["h"] * 0.75
            x, y = h["x"], h["y"]
            out.append(f'<ellipse cx="{fmt(x + r * 0.08)}" cy="{fmt(y + r * 0.1)}" rx="{fmt(r * 1.08)}" ry="{fmt(r * 0.82)}" '
                       f'fill="url(#moundshade)"/>')
            out.append(f'<ellipse cx="{fmt(x)}" cy="{fmt(y)}" rx="{fmt(r)}" ry="{fmt(r * 0.74)}" fill="url(#mound)"/>')
            if h["labelled"]:
                # little summit marker
                marks.append(f'<path d="M{fmt(x - 13)},{fmt(y + 6)} L{fmt(x)},{fmt(y - 14)} L{fmt(x + 13)},{fmt(y + 6)}Z" '
                           f'fill="#8c6a4f" stroke="{OUTLINE}" stroke-width="3" stroke-linejoin="round"/>'
                           f'<path d="M{fmt(x - 5)},{fmt(y - 6)} L{fmt(x)},{fmt(y - 14)} L{fmt(x + 5)},{fmt(y - 6)}Z" '
                           f'fill="#ffffff"/>')
        # shading only on the green (park/forest) areas; summit markers on top
        return f'<g clip-path="url(#parkclip)">{"".join(out)}</g>' + "".join(marks)

    def edge_fade_svg(self, fade=110):
        g = []
        for gid, (x1, y1, x2, y2), rect in (("fadeW", (0, 0, 1, 0), (0, 0, fade, H)),
                                            ("fadeE", (1, 0, 0, 0), (W - fade, 0, fade, H)),
                                            ("fadeN", (0, 0, 0, 1), (0, 0, W, fade)),
                                            ("fadeS", (0, 1, 0, 0), (0, H - fade, W, fade))):
            g.append(f'<linearGradient id="{gid}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">'
                     f'<stop offset="0" stop-color="{SEA}" stop-opacity="1"/>'
                     f'<stop offset="0.25" stop-color="{SEA}" stop-opacity="0.85"/>'
                     f'<stop offset="1" stop-color="{SEA}" stop-opacity="0"/></linearGradient>')
            g.append(f'<rect x="{rect[0]}" y="{rect[1]}" width="{rect[2]}" height="{rect[3]}" fill="url(#{gid})"/>')
        return "".join(g)

    def plazas_svg(self):
        out = []
        for it in self.icon_items:
            sz, x, y = it["size"], it["x"], it["y"]
            out.append(f'<ellipse cx="{fmt(x)}" cy="{fmt(y - sz * 0.01)}" rx="{fmt(sz * 0.42)}" ry="{fmt(sz * 0.11)}" '
                       f'fill="#fff6e3" fill-opacity="0.75"/>')
            out.append(f'<ellipse cx="{fmt(x)}" cy="{fmt(y)}" rx="{fmt(sz * 0.27)}" ry="{fmt(sz * 0.05)}" '
                       f'fill="{OUTLINE}" fill-opacity="0.1"/>')
        return "".join(out)

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
        """Decks with a soft shadow on the water and small round piers (not
        sleeper-like ticks, which would clash with the LRT line)."""
        piers, cas = [], []
        foot = self.foot.buffer(2)
        shapely.prepare(foot)
        for name, ls in self.bridges:
            for l in ls:
                cas.append(l)
                n = int(l.length // 70)
                for i in range(1, n):
                    p = l.interpolate(i * 70)
                    if shapely.contains_xy(foot, p.x, p.y):
                        continue
                    q = l.interpolate(min(l.length, i * 70 + 2))
                    dx, dy = q.x - p.x, q.y - p.y
                    d = math.hypot(dx, dy) or 1
                    nx, ny = -dy / d, dx / d
                    if ny < 0:                       # always the lower (south) side
                        nx, ny = -nx, -ny
                    piers.append((p.x + nx * 11, p.y + ny * 11 + 3))
        rc = 'fill="none" stroke-linecap="round" stroke-linejoin="round"'
        deck = line_d(cas)
        # the shadow is drawn early (under the land), so it only shows on the water
        self.bridge_shadow = (f'<path d="{deck}" transform="translate(6,8)" {rc} stroke="#1f4e79" '
                              f'stroke-opacity="0.18" stroke-width="17"/>')
        pier_svg = "".join(f'<circle cx="{fmt(x)}" cy="{fmt(y)}" r="4"/>' for x, y in piers)
        return (f'<g fill="{BRIDGE_PIER}" stroke="{OUTLINE}" stroke-width="1.2" stroke-opacity="0.5">{pier_svg}</g>'
                f'<path d="{deck}" {rc} stroke="{BRIDGE_EDGE}" stroke-width="17"/>'
                f'<path d="{deck}" {rc} stroke="#fbf8f1" stroke-width="10"/>')

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


def _node(args):
    node = shutil.which("node")
    if not node:
        sys.exit("node not found - install Node.js 18+ to rasterize the SVG")
    env = dict(os.environ)
    np_ = find_node_path()
    if np_:
        env["NODE_PATH"] = np_
    cmd = [node, str(HERE / "rasterize.mjs")] + [str(a) for a in args]
    log("  " + " ".join(cmd))
    subprocess.run(cmd, check=True, env=env)


def rasterize(svg_path, png_path):
    _node([svg_path, png_path])


def measure_icons(out_dir):
    """Icon silhouettes (from resvg renders) for overlap-free layout."""
    path = Path(out_dir) / "icon-shapes.json"
    _node(["--icons", ICONS, path])
    return path


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
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    r = Renderer(args)
    try:
        r.icons.load_shapes(measure_icons(out))
    except (SystemExit, subprocess.CalledProcessError, OSError) as exc:
        log(f"  (could not measure icon silhouettes: {exc}; using rough boxes)")
    log("preparing geometry ...")
    r.prepare()
    log("laying out ...")
    svg = r.svg(labels, places)
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
