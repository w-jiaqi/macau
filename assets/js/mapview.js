/* global L */
// Two aligned maps in one Leaflet view:
//   · underneath — the real map (our vector basemap from OpenStreetMap data, Chinese labels)
//   · on top     — the illustrated "theme-park" map, a single georeferenced picture
// plus the route and stop pins drawn above both.

import { pointAlong, haversine, escapeHtml } from './geo.js';

const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');

function interp(stops, z) {
  if (z <= stops[0][0]) return stops[0][1];
  for (let i = 1; i < stops.length; i++) {
    if (z <= stops[i][0]) {
      const [z0, v0] = stops[i - 1], [z1, v1] = stops[i];
      return v0 + ((v1 - v0) * (z - z0)) / (z1 - z0);
    }
  }
  return stops[stops.length - 1][1];
}

const WIDTH = {
  'road-major': [[12, 1.3], [13, 1.9], [14, 3], [15, 4.4], [16, 6.4], [17, 9], [18, 13]],
  'road-mid': [[12, 0.5], [13, 0.9], [14, 1.7], [15, 2.8], [16, 4.2], [17, 6.2], [18, 9]],
  'road-minor': [[14, 0.5], [15, 1.1], [16, 2], [17, 3.6], [18, 6]],
  path: [[14, 0.5], [15, 0.8], [16, 1.1], [17, 1.6], [18, 2.4]],
  bridge: [[12, 1.6], [13, 2.2], [14, 3.4], [15, 4.8], [16, 6.8], [17, 9.5], [18, 13.5]],
  rail: [[12, 0.9], [14, 1.5], [16, 2.2], [18, 3]],
};
const CASING = [[12, 0.9], [14, 1.4], [16, 2], [18, 2.6]];
const MAX_ZOOM = { cartoon: 16.5, real: 18 }; // one 3000 px picture gets soft beyond ~z16

// Leaflet's ImageOverlay resizes the <img> (CSS width/height) after every zoom. For a 3000×4211 picture
// the browser then needs a frame or two to re-raster it before the next zoom animation can start, so
// during a quick pinch the picture lagged behind the pins and the route (they drifted apart by tens of
// pixels). Keep the element at one fixed size and express all scaling as a transform instead — the same
// way Leaflet animates tiles — so every layer starts each zoom animation in the same frame.
const BASE_WIDTH = 1000;
const PictureOverlay = L.ImageOverlay.extend({
  _reset() {
    const img = this._image;
    const map = this._map;
    if (!img || !map) return;
    // Unrounded projection (latLngToLayerPoint rounds to whole pixels, which skews the aspect ratio).
    const origin = map.getPixelOrigin();
    const tl = map.project(this._bounds.getNorthWest()).subtract(origin);
    const br = map.project(this._bounds.getSouthEast()).subtract(origin);
    if (!this._sized) {
      // Web Mercator keeps the picture's aspect ratio the same at every zoom.
      img.style.width = `${BASE_WIDTH}px`;
      img.style.height = `${(BASE_WIDTH * (br.y - tl.y)) / (br.x - tl.x)}px`;
      this._sized = true;
    }
    this._scale = (br.x - tl.x) / BASE_WIDTH;
    L.DomUtil.setTransform(img, tl, this._scale);
  },
  _animateZoom(e) {
    const offset = this._map._latLngBoundsToNewLayerBounds(this._bounds, e.zoom, e.center).min;
    L.DomUtil.setTransform(this._image, offset, this._scale * this._map.getZoomScale(e.zoom));
  },
});

export function createMapView(el, { bounds, getPadding, onPinClick, toast }) {
  const BOUNDS = L.latLngBounds(bounds);
  const map = L.map(el, {
    zoomControl: false,
    attributionControl: false,
    minZoom: 11,
    maxZoom: MAX_ZOOM.cartoon,
    // No CSS zoom transitions: the browser starts the big picture's transition a frame or two after the
    // pins' and the route's, so they visibly drift apart while zooming. Pinch zoom is live anyway, and
    // programmatic moves use flyTo, which positions every layer in the same frame.
    zoomAnimation: false,
    zoomSnap: 0,
    zoomDelta: 0.5,
    wheelPxPerZoomLevel: 110,
    maxBounds: BOUNDS.pad(0.12),
    maxBoundsViscosity: 0.9,
    bounceAtZoomLimits: false,
    inertiaDeceleration: 2400,
    tapTolerance: 18,
  }).fitBounds(BOUNDS);

  // OpenStreetMap credit lives in the panel footer (index.html) rather than on the map.

  const pane = (name, z) => { const p = map.createPane(name); p.style.zIndex = String(z); return p; };
  pane('bm-base', 201);
  pane('bm-detail', 202);
  pane('bm-roads', 203);
  pane('bm-labels', 250);
  pane('cartoon', 300);
  pane('route', 410);
  pane('route-arrows', 420);

  const renderers = {
    base: L.canvas({ pane: 'bm-base', padding: 0.5 }),
    detail: L.canvas({ pane: 'bm-detail', padding: 0.5 }),
    roads: L.canvas({ pane: 'bm-roads', padding: 0.5 }),
  };
  const routeRenderer = L.svg({ pane: 'route', padding: 0.6 });

  let mode = 'cartoon';
  let palette = readPalette();
  const bmLayers = [];
  let detailState = 'idle';
  const detailLayers = [];
  const labelGroup = L.layerGroup().addTo(map);
  const routeGroup = L.layerGroup().addTo(map);
  const arrowGroup = L.layerGroup().addTo(map);
  const pinGroup = L.layerGroup().addTo(map);
  let pins = [];
  let lastNodes = [];
  let lastLegs = [];
  let youAreHere = null;

  // ---- illustrated map (top) -------------------------------------------------------
  function setCartoon(cfg) {
    const b = L.latLngBounds(cfg.bounds);
    if (cfg.sea) el.style.setProperty('--cartoon-sea', cfg.sea);
    const opts = { pane: 'cartoon', interactive: false, className: 'cartoon-img', alt: '澳门插画地图' };
    const preview = new PictureOverlay(cfg.preview || cfg.image, b, opts).addTo(map);
    if (cfg.image && cfg.image !== cfg.preview) {
      // After the small preview is showing, fetch the full-resolution picture and swap it in.
      const loadFull = () => {
        const img = new Image();
        img.decoding = 'async';
        img.onload = () => {
          const full = new PictureOverlay(cfg.image, b, opts).addTo(map);
          full.once('load', () => setTimeout(() => preview.remove(), 400));
        };
        img.src = cfg.image;
      };
      preview.once('load', loadFull);
      preview.once('error', loadFull);
    }
    return new Promise((resolve) => {
      preview.once('load', resolve);
      preview.once('error', resolve);
      setTimeout(resolve, 6000);
    });
  }

  // ---- real map (underneath) ---------------------------------------------------------
  function readPalette() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    return {
      sea: v('--map-sea'), land: v('--map-land'), landOther: v('--map-land-other'),
      coast: v('--map-coast'), coastOther: v('--map-coast-other'), park: v('--map-park'),
      beach: v('--map-beach'), airport: v('--map-airport'), runway: v('--map-runway'),
      road: v('--map-road'), roadCasing: v('--map-road-casing'), roadMinor: v('--map-road-minor'),
      path: v('--map-path'), bridge: v('--map-bridge'), bridgeCasing: v('--map-bridge-casing'),
      rail: v('--map-rail'), border: v('--map-border'),
    };
  }

  function styleFor(id, part, z) {
    const P = palette;
    switch (id) {
      case 'land': return { stroke: true, color: P.coast, weight: z >= 15 ? 1.4 : 1, fill: true, fillColor: P.land, fillOpacity: 1 };
      case 'land-other': return { stroke: true, color: P.coastOther, weight: 0.8, fill: true, fillColor: P.landOther, fillOpacity: 1 };
      case 'park': return { stroke: false, fill: true, fillColor: P.park, fillOpacity: 1 };
      case 'water': return { stroke: true, color: P.coast, weight: 0.8, fill: true, fillColor: P.sea, fillOpacity: 1 };
      case 'beach': return { stroke: false, fill: true, fillColor: P.beach, fillOpacity: 1 };
      case 'airport': return { stroke: false, fill: true, fillColor: P.airport, fillOpacity: 1 };
      case 'runway': return { stroke: false, fill: true, fillColor: P.runway, fillOpacity: 1 };
      case 'border': return { color: P.border, weight: 1.2, dashArray: '5 5', fill: false, opacity: 0.9 };
      case 'rail': {
        const w = interp(WIDTH.rail, z);
        return { color: P.rail, weight: w, dashArray: `${w * 3} ${w * 2}`, fill: false, opacity: z < 13 ? 0.6 : 0.9, lineCap: 'butt' };
      }
      case 'path': {
        const w = interp(WIDTH.path, z);
        return { color: P.path, weight: w, dashArray: z >= 16 ? `${w * 2.5} ${w * 1.6}` : null, fill: false, opacity: 1 };
      }
      case 'road-minor':
        return { color: P.roadMinor, weight: interp(WIDTH['road-minor'], z), fill: false, opacity: 1 };
      case 'road-major':
      case 'road-mid':
      case 'bridge': {
        const w = interp(WIDTH[id], z);
        if (part === 'casing') return { color: id === 'bridge' ? P.bridgeCasing : P.roadCasing, weight: w + interp(CASING, z), fill: false, opacity: 1 };
        return { color: id === 'bridge' ? P.bridge : P.road, weight: w, fill: false, opacity: 1 };
      }
      default: return { stroke: false, fill: false };
    }
  }

  function restyleBasemap() {
    const z = map.getZoom();
    for (const { id, part, layer } of bmLayers.concat(detailLayers)) layer.setStyle(styleFor(id, part, z));
  }

  function groupByLayer(fc) {
    const groups = {};
    for (const f of fc.features || []) {
      const id = f.properties && f.properties.layer;
      if (id) (groups[id] = groups[id] || []).push(f);
    }
    return groups;
  }

  function addLayer(target, groups, id, renderer, part = 'fill') {
    if (!groups[id]) return;
    const z = map.getZoom();
    const layer = L.geoJSON({ type: 'FeatureCollection', features: groups[id] }, {
      renderer, interactive: false, style: () => styleFor(id, part, z),
    }).addTo(map);
    target.push({ id, part, layer });
  }

  function setBasemap(fc) {
    const g = groupByLayer(fc);
    for (const id of ['land-other', 'land', 'beach', 'park', 'airport', 'runway', 'water']) addLayer(bmLayers, g, id, renderers.base);
    addLayer(bmLayers, g, 'border', renderers.roads);
    for (const id of ['road-mid', 'road-major', 'bridge']) addLayer(bmLayers, g, id, renderers.roads, 'casing');
    for (const id of ['road-mid', 'road-major', 'bridge']) addLayer(bmLayers, g, id, renderers.roads);
    addLayer(bmLayers, g, 'rail', renderers.roads);
  }

  function maybeLoadDetail() {
    if (mode !== 'real' || detailState !== 'idle' || map.getZoom() < 14.5) return;
    detailState = 'loading';
    fetch('data/basemap-detail.geojson')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
      .then((fc) => {
        const g = groupByLayer(fc);
        addLayer(detailLayers, g, 'road-minor', renderers.detail);
        addLayer(detailLayers, g, 'path', renderers.detail);
        detailState = 'ready';
        syncDetailVisibility();
      })
      .catch(() => { detailState = 'failed'; });
  }

  function syncDetailVisibility() {
    const show = mode === 'real' && map.getZoom() >= 14.5;
    for (const { layer } of detailLayers) {
      if (show && !map.hasLayer(layer)) layer.addTo(map);
      if (!show && map.hasLayer(layer)) map.removeLayer(layer);
    }
  }

  function setLabels(labels) {
    labelGroup.clearLayers();
    for (const l of labels || []) {
      const icon = L.divIcon({
        className: `mlabel mlabel--${escapeHtml(l.type || 'landmark')}`,
        html: `<span style="--a:${Number(l.angle) || 0}deg">${escapeHtml(l.text)}</span>`,
        iconSize: [0, 0],
      });
      const m = L.marker([l.lat, l.lng], { icon, pane: 'bm-labels', interactive: false, keyboard: false });
      m._zmin = Number.isFinite(l.min) ? l.min : 12;
      m._zmax = Number.isFinite(l.max) ? l.max : 99;
      labelGroup.addLayer(m);
    }
    syncLabels();
  }

  function syncLabels() {
    const z = map.getZoom();
    labelGroup.eachLayer((m) => {
      const node = m.getElement();
      if (node) node.classList.toggle('is-hidden', z < m._zmin - 0.01 || z > m._zmax + 0.99);
    });
  }

  function syncZoomClasses() {
    const z = map.getZoom();
    for (const t of [13, 14, 15, 16]) el.classList.toggle(`z-lt-${t}`, z < t);
  }

  map.on('zoomend', () => {
    if (bmLayers.length) restyleBasemap();
    syncLabels();
    syncZoomClasses();
    maybeLoadDetail();
    syncDetailVisibility();
    placeTags();
  });
  map.on('moveend', placeTags);
  syncZoomClasses();
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { palette = readPalette(); restyleBasemap(); });

  function setMode(next) {
    mode = next;
    el.classList.toggle('mode-real', mode === 'real');
    el.classList.toggle('mode-cartoon', mode === 'cartoon');
    map.setMaxZoom(MAX_ZOOM[mode]);
    if (map.getZoom() > MAX_ZOOM[mode]) map.setZoom(MAX_ZOOM[mode]);
    maybeLoadDetail();
    syncDetailVisibility();
    requestAnimationFrame(placeTags);
  }
  setMode('cartoon');

  // ---- route ------------------------------------------------------------------------
  // nodes: [{lat,lng,name,short,kind:'start'|'stop'|'end', num?, baked?}]
  // legs:  [{coords, d, estimate?, pending?}] (legs[i] joins nodes[i] → nodes[i+1])
  function drawRoute(nodes, legs) {
    lastNodes = nodes;
    lastLegs = legs;
    routeGroup.clearLayers();
    arrowGroup.clearLayers();

    legs.forEach((leg, i) => {
      if (!leg || !leg.coords || leg.coords.length < 2) return;
      const from = [nodes[i].lat, nodes[i].lng];
      const to = [nodes[i + 1].lat, nodes[i + 1].lng];
      const coords = leg.coords;
      const cls = leg.pending ? 'is-pending' : leg.estimate ? 'is-estimate' : '';
      if (!leg.pending && !leg.estimate) {
        // dotted connectors where the road route starts/ends away from the pin (pedestrian squares etc.)
        if (haversine(from, coords[0]) > 15) L.polyline([from, coords[0]], { renderer: routeRenderer, className: 'route-link', interactive: false }).addTo(routeGroup);
        if (haversine(coords[coords.length - 1], to) > 15) L.polyline([coords[coords.length - 1], to], { renderer: routeRenderer, className: 'route-link', interactive: false }).addTo(routeGroup);
      }
      L.polyline(coords, { renderer: routeRenderer, className: `route-casing ${cls}`, interactive: false }).addTo(routeGroup);
      L.polyline(coords, { renderer: routeRenderer, className: `route-line ${cls}`, interactive: false }).addTo(routeGroup);
      if (!leg.pending) {
        L.polyline(coords, { renderer: routeRenderer, className: `route-flow ${cls}`, interactive: false }).addTo(routeGroup);
        const fractions = (leg.d || 0) > 3500 ? [0.3, 0.7] : [0.5];
        for (const f of fractions) {
          const { point, a, b } = pointAlong(coords, f);
          const pa = map.project(a, 16), pb = map.project(b, 16);
          const deg = (Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180) / Math.PI;
          L.marker(point, {
            pane: 'route-arrows', interactive: false, keyboard: false,
            icon: L.divIcon({
              className: 'route-arrow-wrap',
              html: `<span class="route-arrow" style="--a:${deg.toFixed(1)}deg"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M4.2 2.6 9.2 6l-5 3.4z"/></svg></span>`,
              iconSize: [18, 18], iconAnchor: [9, 9],
            }),
          }).addTo(arrowGroup);
        }
      }
    });
    drawPins(nodes);
  }

  function drawPins(nodes) {
    pinGroup.clearLayers();
    pins = nodes.map((n) => {
      const short = escapeHtml(n.short || n.name);
      const isTerm = n.kind !== 'stop';
      const html = isTerm
        ? `<div class="pin pin--term"><svg aria-hidden="true"><use href="#i-ferry"/></svg><i>${n.kind === 'start' ? '起' : '终'}</i></div><div class="pin-tag pin-tag--term">${short}</div>`
        : `<div class="pin pin--stop"><b>${n.num}</b></div><div class="pin-tag">${short}</div>`;
      const m = L.marker([n.lat, n.lng], {
        icon: L.divIcon({
          className: `pin-wrap${isTerm ? ' pin-wrap--term' : ''}${n.baked ? ' has-baked' : ''}`,
          html,
          iconSize: isTerm ? [40, 40] : [34, 34],
          iconAnchor: isTerm ? [20, 20] : [17, 17],
        }),
        title: n.name,
        alt: n.name,
        riseOnHover: true,
        zIndexOffset: isTerm ? 900 : 1000 + (10 - (n.num || 0)),
        keyboard: true,
      }).on('click', () => onPinClick && onPinClick(n));
      m._node = n;
      pinGroup.addLayer(m);
      return m;
    });
    requestAnimationFrame(placeTags);
  }

  // Put each name tag on whichever side of its pin is free.
  function placeTags() {
    const size = map.getSize();
    const boxes = pins.map((m) => {
      const p = map.latLngToContainerPoint(m.getLatLng());
      return { x: p.x - 15, y: p.y - 15, w: 30, h: 30 };
    });
    const overlaps = (r) => boxes.some((b) => r.x < b.x + b.w && r.x + r.w > b.x && r.y < b.y + b.h && r.y + r.h > b.y);
    for (const m of pins) {
      const node = m.getElement();
      const tag = node && node.querySelector('.pin-tag');
      if (!tag) continue;
      tag.classList.remove('tag-r', 'tag-l', 'tag-b', 'tag-t', 'tag-off');
      if (getComputedStyle(tag).display === 'none') continue;
      const p = map.latLngToContainerPoint(m.getLatLng());
      const w = tag.offsetWidth || 60, h = tag.offsetHeight || 22, gap = 21;
      const cands = [
        ['r', p.x + gap, p.y - h / 2],
        ['l', p.x - gap - w, p.y - h / 2],
        ['b', p.x - w / 2, p.y + gap - 2],
        ['t', p.x - w / 2, p.y - gap - h + 2],
      ];
      let chosen = null;
      for (const [side, x, y] of cands) {
        const r = { x, y, w, h };
        if (x < 4 || y < 4 || x + w > size.x - 4 || y + h > size.y - 4) continue;
        if (!overlaps(r)) { chosen = [side, r]; break; }
      }
      if (chosen) { tag.classList.add(`tag-${chosen[0]}`); boxes.push(chosen[1]); }
      else tag.classList.add('tag-off');
    }
  }

  // ---- camera ---------------------------------------------------------------------------
  function fitLatLngs(latlngs, maxZoom = 16) {
    const b = L.latLngBounds(latlngs);
    if (!b.isValid()) return;
    const pad = getPadding();
    const opts = { paddingTopLeft: [pad.left, pad.top], paddingBottomRight: [pad.right, pad.bottom], maxZoom };
    if (reduceMotion.matches) map.fitBounds(b, opts);
    else map.flyToBounds(b, { ...opts, duration: 0.6 });
  }

  function fitRoute() {
    const pts = lastNodes.map((n) => [n.lat, n.lng]);
    for (const leg of lastLegs) if (leg && leg.coords && !leg.pending) pts.push(...leg.coords);
    if (pts.length) fitLatLngs(pts, 16);
  }

  function flyTo(lat, lng, zoom) {
    const z = zoom ?? Math.max(map.getZoom(), 15);
    const pad = getPadding();
    const size = map.getSize();
    const visCx = pad.left + (size.x - pad.left - pad.right) / 2;
    const visCy = pad.top + (size.y - pad.top - pad.bottom) / 2;
    const p = map.project([lat, lng], z).add([size.x / 2 - visCx, size.y / 2 - visCy]);
    const center = map.unproject(p, z);
    if (reduceMotion.matches) map.setView(center, z, { animate: false });
    else map.flyTo(center, z, { duration: 0.65 });
  }

  function locate() {
    if (!navigator.geolocation) { toast('此设备不支持定位'); return; }
    el.classList.add('is-locating');
    navigator.geolocation.getCurrentPosition((pos) => {
      el.classList.remove('is-locating');
      const ll = L.latLng(pos.coords.latitude, pos.coords.longitude);
      const acc = Math.min(pos.coords.accuracy || 50, 2000);
      if (youAreHere) youAreHere.remove();
      youAreHere = L.layerGroup([
        L.circle(ll, { radius: acc, className: 'you-acc', interactive: false, renderer: routeRenderer }),
        L.marker(ll, { interactive: false, keyboard: false, icon: L.divIcon({ className: 'you-wrap', html: '<span class="you-dot"></span>', iconSize: [20, 20], iconAnchor: [10, 10] }) }),
      ]).addTo(map);
      if (BOUNDS.contains(ll)) flyTo(ll.lat, ll.lng, Math.max(map.getZoom(), 15.5));
      else toast('你目前不在澳门地图范围内');
    }, (err) => {
      el.classList.remove('is-locating');
      toast(err.code === 1 ? '未获得定位权限，请在浏览器设置中允许' : '暂时无法获取位置');
    }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 30000 });
  }

  function zoomBy(delta) {
    const z = Math.min(map.getMaxZoom(), Math.max(map.getMinZoom(), map.getZoom() + delta));
    if (reduceMotion.matches) map.setZoom(z);
    else map.flyTo(map.getCenter(), z, { duration: 0.3 });
  }

  // Double-click / double-tap zoom, animated the same frame-synchronous way.
  map.doubleClickZoom.disable();
  map.on('dblclick', (e) => {
    const z = Math.min(map.getMaxZoom(), map.getZoom() + (e.originalEvent.shiftKey ? -1 : 1));
    const p = map.project(e.latlng, z).subtract(map.latLngToContainerPoint(e.latlng).subtract(map.getSize().divideBy(2)));
    if (reduceMotion.matches) map.setView(map.unproject(p, z), z);
    else map.flyTo(map.unproject(p, z), z, { duration: 0.3 });
  });

  // Lowest zoom: the whole picture fits on screen (sea colour fills any margin around it).
  function updateMinZoom() {
    const prev = map.options.minZoom;
    map.options.minZoom = 0; // getBoundsZoom clamps to the current minimum, so lift it first
    const z = map.getBoundsZoom(BOUNDS, false);
    map.options.minZoom = prev;
    map.setMinZoom(Math.max(11, Math.floor(z * 4) / 4));
  }
  updateMinZoom();
  map.on('resize', updateMinZoom);

  return {
    map,
    setCartoon,
    setBasemap,
    setLabels,
    setMode,
    getMode: () => mode,
    hasBasemap: () => bmLayers.length > 0,
    drawRoute,
    fitRoute,
    flyTo,
    locate,
    invalidate: () => { map.invalidateSize({ pan: false }); updateMinZoom(); placeTags(); },
    zoomIn: () => zoomBy(0.75),
    zoomOut: () => zoomBy(-0.75),
  };
}
