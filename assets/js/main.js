// App controller: any number of stops between the fixed ferry terminals, typed in by the user,
// joined by real road routes and drawn over the illustrated map.

import { createMapView } from './mapview.js';
import { createSheet } from './sheet.js';
import { createRouter } from './router.js';
import { createSearch, geocode, parseCoords } from './search.js';
import { arc, escapeHtml as h } from './geo.js';

const MAX_STOPS = 20;
const LS_KEY = 'macau-route:stops:v3';
const LS_MODE = 'macau-route:map-mode';
const LS_PANEL = 'macau-route:panel-collapsed';

const DIGITS = '零一二三四五六七八九';
/** 1 → 第一站, 12 → 第十二站, 20 → 第二十站 */
function ordinal(n) {
  const tens = Math.floor(n / 10), ones = n % 10;
  const t = tens === 0 ? '' : `${tens === 1 ? '' : DIGITS[tens]}十`;
  return `第${t}${ones || !t ? DIGITS[ones] : ''}站`;
}

const $ = (sel) => document.querySelector(sel);
const app = $('#app');

let data, mapView, sheet, router, search;
let stops = [];          // each: { name, short, lat, lng, id?, source } or null (not yet chosen)
let editing = null;      // index of the stop being searched
let routeGen = 0;
let routeAbort = null;
let geoAbort = null;

// ---- helpers -----------------------------------------------------------------------------
async function getJSON(url, { optional = false } = {}) {
  try {
    const res = await fetch(url, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    if (optional) { console.warn(err); return null; }
    throw err;
  }
}

let toastTimer = 0;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('is-on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('is-on'), 2600);
}

function getPadding() {
  if (sheet.isDesktop()) {
    const collapsed = app.classList.contains('panel-collapsed');
    const left = collapsed ? 40 : $('#sheet').getBoundingClientRect().right + 28;
    return { left, top: 40, right: 80, bottom: 40 };
  }
  const visible = Math.min(sheet.visible(), window.innerHeight * 0.55);
  return { left: 28, right: 68, top: safeTop() + 28, bottom: visible + 22 };
}

const safeTop = (() => {
  const probe = document.createElement('div');
  probe.style.cssText = 'position:fixed;top:0;height:env(safe-area-inset-top);visibility:hidden;pointer-events:none';
  document.body.appendChild(probe);
  return () => probe.getBoundingClientRect().height || 0;
})();

function placeToStop(p, source = 'place') {
  return { name: p.name, short: p.short || p.name, lat: p.lat, lng: p.lng, id: p.id, source };
}

function save() {
  try { localStorage.setItem(LS_KEY, JSON.stringify(stops)); } catch { /* private mode */ }
}

function load(defaults) {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
    if (Array.isArray(raw)) {
      return raw.slice(0, MAX_STOPS).map((s) => (s && Number.isFinite(s.lat) && Number.isFinite(s.lng) ? s : null));
    }
  } catch { /* ignore */ }
  return defaults;
}

// ---- stop list ------------------------------------------------------------------------------
function renderList() {
  const fixed = (node, label) => `
    <li class="stop stop--fixed">
      <span class="badge badge--term"><svg aria-hidden="true"><use href="#i-ferry"/></svg></span>
      <div class="stop-text"><small>${label}</small><b>${h(node.name)}</b></div>
      <svg class="stop-lock" aria-hidden="true"><use href="#i-lock"/></svg>
    </li>`;
  const rows = [fixed(data.start, '起点 · 固定')];
  stops.forEach((s, i) => {
    const label = ordinal(i + 1);
    rows.push(`
      <li class="stop${s ? '' : ' is-empty'}" data-slot="${i}">
        <button class="stop-main" type="button" aria-label="${label}：${s ? h(s.name) : '未设置'}，点按修改地点">
          <span class="badge">${i + 1}</span>
          <span class="stop-text">
            <small>${label}</small>
            ${s ? `<b>${h(s.name)}</b>` : '<span class="ph">点此输入地点</span>'}
          </span>
          <svg class="stop-edit" aria-hidden="true"><use href="#i-edit"/></svg>
        </button>
        <button class="icon-btn stop-clear" type="button" data-remove="${i}" aria-label="删除${label}"><svg><use href="#i-close"/></svg></button>
      </li>`);
  });
  rows.push(`
    <li class="stop stop--add">
      <button class="stop-main" type="button" id="btn-add"${stops.length >= MAX_STOPS ? ' disabled' : ''}>
        <span class="badge badge--add"><svg aria-hidden="true"><use href="#i-plus"/></svg></span>
        <span class="stop-text"><span class="add-label">添加一站</span></span>
      </button>
    </li>`);
  rows.push(fixed(data.end, '终点 · 固定'));
  $('#stops').innerHTML = rows.join('');
}

function renderSummary(nodes) {
  const n = nodes.length - 2;
  $('#panel-open-label').textContent = n ? `路线 · ${n} 站` : '路线';
}

// ---- route ------------------------------------------------------------------------------------
function buildNodes() {
  const nodes = [{ ...data.start, kind: 'start', baked: true }];
  stops.forEach((s, i) => {
    if (s) nodes.push({ ...s, kind: 'stop', num: i + 1, baked: Boolean(s.id) });
  });
  nodes.push({ ...data.end, kind: 'end', baked: true });
  return nodes;
}

async function updateRoute({ fit = false } = {}) {
  const gen = ++routeGen;
  if (routeAbort) routeAbort.abort();
  routeAbort = new AbortController();
  const { signal } = routeAbort;

  const nodes = buildNodes();
  const legs = nodes.slice(1).map((b, i) => {
    const a = nodes[i];
    return router.quick(a, b) || { pending: true, coords: arc([a.lat, a.lng], [b.lat, b.lng]) };
  });
  mapView.drawRoute(nodes, legs);
  renderSummary(nodes);
  if (fit) mapView.fitRoute();

  const waiting = legs.map((l, i) => (l.pending ? i : -1)).filter((i) => i >= 0);
  if (!waiting.length) return;
  await Promise.all(waiting.map(async (i) => {
    try {
      const leg = await router.leg(nodes[i], nodes[i + 1], { signal });
      if (gen !== routeGen) return;
      legs[i] = leg;
      mapView.drawRoute(nodes, legs);
      renderSummary(nodes);
    } catch { /* superseded */ }
  }));
  if (gen === routeGen && legs.some((l) => l.estimate)) toast('部分路段暂时无法联网计算，先以虚线估算显示');
  if (fit && gen === routeGen) mapView.fitRoute();
}

// ---- search view ---------------------------------------------------------------------------------
function openSearch(slot) {
  editing = slot;
  geoState = 'idle';
  geoResults = [];
  $('#view-list').hidden = true;
  $('#view-search').hidden = false;
  $('#search-title').textContent = `${ordinal(slot + 1)} · 输入地点`;
  const input = $('#search-input');
  input.value = stops[slot] ? stops[slot].name : '';
  input.focus({ preventScroll: true }); // must stay inside the tap handler for iOS to raise the keyboard
  input.select();
  sheet.setSnap(sheet.isDesktop() ? 'half' : 'full');
  renderResults();
}

function closeSearch() {
  if (geoAbort) geoAbort.abort();
  // A stop that was added but never filled in is dropped again.
  if (editing !== null && !stops[editing]) {
    stops.splice(editing, 1);
    save();
    renderList();
  }
  editing = null;
  $('#search-input').blur();
  $('#view-search').hidden = true;
  $('#view-list').hidden = false;
  sheet.setSnap('half');
}

let geoResults = [];
let geoState = 'idle'; // idle | loading | done | error

function renderResults() {
  const q = $('#search-input').value.trim();
  $('#search-clear').hidden = !q;
  const out = [];
  const row = (attrs, title, sub, icon = 'pin') => `
    <li><button type="button" class="res" ${attrs}>
      <svg class="res-ic" aria-hidden="true"><use href="#i-${icon}"/></svg>
      <span class="res-text"><b>${title}</b>${sub ? `<small>${sub}</small>` : ''}</span>
    </button></li>`;

  if (!q) {
    out.push('<h3 class="res-title">热门景点</h3><ul class="res-list">');
    for (const [area, label] of Object.entries(data.areas)) {
      const items = data.places.filter((p) => p.area === area);
      if (!items.length) continue;
      out.push(`<li class="res-group">${h(label)}</li>`);
      for (const p of items) out.push(row(`data-place="${h(p.id)}"`, h(p.name), h(p.category || ''), 'star'));
    }
    out.push('</ul>');
  } else {
    const coords = parseCoords(q);
    const hits = search.match(q);
    out.push('<ul class="res-list">');
    if (coords) out.push(row(`data-coords="${coords.lat},${coords.lng}"`, `使用坐标 ${coords.lat.toFixed(5)}, ${coords.lng.toFixed(5)}`, '直接定位到这个经纬度'));
    for (const p of hits) out.push(row(`data-place="${h(p.id)}"`, h(p.name), `${h(data.areas[p.area] || '')} · ${h(p.category || '')}`, 'star'));
    if (geoState === 'loading') out.push('<li class="res-note">正在地图中搜索…</li>');
    if (geoState === 'done' && !geoResults.length) out.push(`<li class="res-note">地图中没有找到「${h(q)}」，换个说法试试（例如加上「澳门」或使用葡文/英文名）。</li>`);
    if (geoState === 'error') out.push('<li class="res-note">网络搜索暂时不可用，请稍后再试或从热门景点中选择。</li>');
    geoResults.forEach((r, i) => out.push(row(`data-geo="${i}"`, h(r.name), h(r.sub), 'pin')));
    if (geoState !== 'done' && geoState !== 'loading') {
      out.push(row('data-geocode="1"', `在地图中搜索「${h(q)}」`, '酒店、餐厅、街道、地址都可以', 'search'));
    }
    out.push('</ul>');
  }
  if (editing !== null && stops[editing]) {
    out.push(`<button type="button" class="btn btn-ghost btn-block res-clear" data-clear-slot="1"><svg><use href="#i-close"/></svg><span>删除${ordinal(editing + 1)}</span></button>`);
  }
  $('#search-results').innerHTML = out.join('');
}

async function runGeocode() {
  const q = $('#search-input').value.trim();
  if (!q) return;
  if (geoAbort) geoAbort.abort();
  geoAbort = new AbortController();
  geoState = 'loading';
  geoResults = [];
  renderResults();
  try {
    geoResults = await geocode(q, { signal: geoAbort.signal });
    geoState = 'done';
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    geoState = 'error';
  }
  renderResults();
}

function pick(stop) {
  const slot = editing;
  if (slot === null || !stop) return;
  stops[slot] = stop;
  save();
  renderList();
  closeSearch();
  toast(`${ordinal(slot + 1)}：${stop.name}`);
  updateRoute({ fit: true });
}

// ---- events ------------------------------------------------------------------------------------------
function wireEvents() {
  $('#stops').addEventListener('click', (e) => {
    const remove = e.target.closest('[data-remove]');
    if (remove) {
      const i = +remove.dataset.remove;
      const gone = stops[i];
      stops.splice(i, 1);
      save();
      renderList();
      updateRoute();
      if (gone) toast(`已删除「${gone.short || gone.name}」`);
      return;
    }
    if (e.target.closest('#btn-add')) {
      if (stops.length >= MAX_STOPS) return;
      stops.push(null);
      renderList();
      openSearch(stops.length - 1);
      return;
    }
    const main = e.target.closest('.stop-main');
    if (main && main.closest('.stop').dataset.slot !== undefined) openSearch(+main.closest('.stop').dataset.slot);
  });

  $('#btn-back').addEventListener('click', closeSearch);

  $('#search-input').addEventListener('input', () => {
    if (geoAbort) geoAbort.abort();
    geoState = 'idle';
    geoResults = [];
    renderResults();
  });

  $('#search-clear').addEventListener('click', () => {
    const input = $('#search-input');
    input.value = '';
    input.focus();
    geoState = 'idle';
    geoResults = [];
    renderResults();
  });

  $('#search-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = $('#search-input').value.trim();
    if (!q) return;
    const coords = parseCoords(q);
    if (coords) { pick({ name: `${coords.lat.toFixed(5)}, ${coords.lng.toFixed(5)}`, short: '自选地点', ...coords, source: 'coords' }); return; }
    const local = search.resolveLocal(q);
    if (local) { pick(placeToStop(local)); return; }
    runGeocode();
  });

  $('#search-results').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.place) { pick(placeToStop(data.byId.get(b.dataset.place))); return; }
    if (b.dataset.geo !== undefined) {
      const r = geoResults[+b.dataset.geo];
      if (r) pick({ name: r.name, short: r.name.slice(0, 8), lat: r.lat, lng: r.lng, source: 'search' });
      return;
    }
    if (b.dataset.coords) {
      const [lat, lng] = b.dataset.coords.split(',').map(Number);
      pick({ name: `${lat.toFixed(5)}, ${lng.toFixed(5)}`, short: '自选地点', lat, lng, source: 'coords' });
      return;
    }
    if (b.dataset.geocode) { runGeocode(); return; }
    if (b.dataset.clearSlot) {
      stops.splice(editing, 1);
      editing = null;
      save();
      renderList();
      closeSearch();
      updateRoute();
    }
  });

  $('#btn-mode').addEventListener('click', () => setMapMode(mapView.getMode() === 'cartoon' ? 'real' : 'cartoon', true));
  $('#btn-collapse').addEventListener('click', () => setPanelCollapsed(true));
  $('#btn-expand').addEventListener('click', () => setPanelCollapsed(false));
  $('#btn-fit').addEventListener('click', () => mapView.fitRoute());
  $('#btn-locate').addEventListener('click', () => mapView.locate());
  $('#btn-zoom-in').addEventListener('click', () => mapView.zoomIn());
  $('#btn-zoom-out').addEventListener('click', () => mapView.zoomOut());

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && editing !== null) closeSearch();
  });
}

// ---- map mode -------------------------------------------------------------------------------------------
let basemapPromise = null;
function ensureBasemap() {
  if (!basemapPromise) {
    basemapPromise = Promise.all([getJSON('data/basemap.geojson', { optional: true }), getJSON('data/labels.json', { optional: true })])
      .then(([fc, labels]) => {
        if (fc) mapView.setBasemap(fc);
        if (labels) mapView.setLabels(labels);
        return Boolean(fc);
      });
  }
  return basemapPromise;
}

async function setMapMode(mode, announce = false) {
  const btn = $('#btn-mode');
  if (mode === 'real' && !mapView.hasBasemap()) {
    if (announce) toast('正在加载真实地图…');
    await ensureBasemap();
  }
  mapView.setMode(mode);
  btn.setAttribute('aria-pressed', String(mode === 'real'));
  btn.title = mode === 'real' ? '切换到插画地图' : '切换到真实地图';
  btn.setAttribute('aria-label', btn.title);
  if (announce) toast(mode === 'real' ? '真实地图' : '插画地图');
  try { localStorage.setItem(LS_MODE, mode); } catch { /* ignore */ }
}

// ---- side panel (wide screens) ------------------------------------------------------------------------
function setPanelCollapsed(collapsed, { persist = true } = {}) {
  app.classList.toggle('panel-collapsed', collapsed);
  $('#btn-expand').hidden = !collapsed;
  syncPanelA11y();
  if (persist) { try { localStorage.setItem(LS_PANEL, collapsed ? '1' : '0'); } catch { /* ignore */ } }
  if (persist) setTimeout(() => mapView.fitRoute(), 380);
  (collapsed ? $('#btn-expand') : $('#btn-collapse')).focus({ preventScroll: true });
}

// The collapsed side panel only exists on wide screens; keep it out of the accessibility tree there.
function syncPanelA11y() {
  const hide = app.classList.contains('panel-collapsed') && sheet.isDesktop();
  $('#sheet').toggleAttribute('inert', hide);
  $('#sheet').setAttribute('aria-hidden', String(hide));
}

// ---- boot ---------------------------------------------------------------------------------------------------
async function boot() {
  let places, route, legs, cartoon;
  try {
    [places, route, legs, cartoon] = await Promise.all([
      getJSON('data/places.json'),
      getJSON('data/route.json'),
      getJSON('data/legs.json', { optional: true }),
      getJSON('data/cartoon.json', { optional: true }),
    ]);
  } catch (err) {
    console.error(err);
    $('#loading').innerHTML = '<p>数据加载失败，请检查网络后刷新页面。</p>';
    return;
  }

  data = { start: places.start, end: places.end, areas: places.areas, places: places.places, byId: new Map() };
  for (const p of [data.start, data.end, ...data.places]) data.byId.set(p.id, p);
  router = createRouter(legs);
  search = createSearch(data.places);

  if (route.title) {
    $('#title').textContent = route.title;
    document.title = `${route.title} · 路线地图`;
  }

  // Default stops from data/route.json: built-in place ids or names, or {name, lat, lng} for any other place.
  const defaults = (route.stops || []).slice(0, MAX_STOPS).map((entry) => {
    if (entry && typeof entry === 'object' && Number.isFinite(entry.lat) && Number.isFinite(entry.lng)) {
      return { name: entry.name, short: entry.short || entry.name, lat: entry.lat, lng: entry.lng, source: 'search' };
    }
    const p = data.byId.get(entry) || search.resolveLocal(entry);
    return p ? placeToStop(p) : null;
  }).filter(Boolean);
  if (route.legs) router.seed(route.legs);
  stops = load(defaults);

  sheet = createSheet($('#sheet'), {
    grip: $('#sheet-grip'),
    getView: () => (editing !== null ? $('#view-search') : $('#view-list')),
    onChange: ({ visible, desktop }) => app.style.setProperty('--sheet-visible', `${desktop ? 0 : Math.round(visible)}px`),
  });

  const bounds = cartoon ? cartoon.bounds : [[22.105, 113.522], [22.222, 113.612]];
  mapView = createMapView($('#map'), {
    bounds,
    getPadding,
    toast,
    onPinClick: (n) => {
      if (n.kind === 'stop') openSearch(n.num - 1);
      else toast(`${n.kind === 'start' ? '起点' : '终点'}：${n.name}`);
    },
  });

  wireEvents();
  renderList();
  let collapsed = false;
  try { collapsed = localStorage.getItem(LS_PANEL) === '1'; } catch { /* ignore */ }
  if (collapsed && sheet.isDesktop()) { app.classList.add('panel-collapsed'); $('#btn-expand').hidden = false; }
  syncPanelA11y();
  matchMedia('(min-width: 900px)').addEventListener('change', syncPanelA11y);

  let ready = Promise.resolve();
  let preferred = 'cartoon';
  try { preferred = localStorage.getItem(LS_MODE) || 'cartoon'; } catch { /* ignore */ }
  if (cartoon) ready = mapView.setCartoon(cartoon);
  if (!cartoon || preferred === 'real') await setMapMode('real');

  sheet.setSnap('peek', false);
  await ready;
  setTimeout(() => {
    mapView.invalidate();
    updateRoute({ fit: true });
    app.classList.remove('is-loading');
  }, 0);

  if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {
    navigator.serviceWorker.register('sw.js').catch((err) => console.warn('SW registration failed', err));
  }
}

boot();
