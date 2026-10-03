// Real road routes between consecutive stops.
// 1) precomputed legs between built-in places (data/legs.json, instant and offline)
// 2) otherwise ask an OSRM server at runtime (walking for short hops, driving otherwise)
// 3) if everything fails, a gently curved placeholder line marked as an estimate.

import { haversine, decodePolyline, arc } from './geo.js';

const WALK_PREFERRED = 1200; // metres: walk when the walking route is this short
const SERVERS = {
  foot: ['https://routing.openstreetmap.de/routed-foot/route/v1/foot'],
  car: ['https://routing.openstreetmap.de/routed-car/route/v1/driving', 'https://router.project-osrm.org/route/v1/driving'],
};
const LS_KEY = 'macau-route:legs:v1';
const LS_MAX = 120;

export function createRouter(legsData) {
  const pre = (legsData && legsData.legs) || {};
  const memo = new Map();
  let stored = {};
  try { stored = JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch { stored = {}; }

  function remember(key, leg) {
    stored[key] = { d: leg.d, mode: leg.mode, g: leg.g };
    const keys = Object.keys(stored);
    if (keys.length > LS_MAX) for (const k of keys.slice(0, keys.length - LS_MAX)) delete stored[k];
    try { localStorage.setItem(LS_KEY, JSON.stringify(stored)); } catch { /* quota / private mode */ }
  }

  function fromPrecomputed(a, b) {
    if (!a.id || !b.id) return null;
    const raw = pre[`${a.id}>${b.id}`];
    if (!raw) return null;
    const walk = raw.walk, drive = raw.drive;
    const useWalk = walk && (!drive || walk.d <= WALK_PREFERRED || walk.t <= drive.t * 1.35 + 360);
    const pick = useWalk ? walk : drive;
    if (!pick) return null;
    return { mode: useWalk ? 'walk' : 'drive', d: pick.d, coords: decodePolyline(pick.g), g: pick.g };
  }

  async function osrm(profile, a, b, signal) {
    const path = `${a.lng.toFixed(6)},${a.lat.toFixed(6)};${b.lng.toFixed(6)},${b.lat.toFixed(6)}`;
    let lastErr;
    for (const base of SERVERS[profile]) {
      try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 9000);
        if (signal) signal.addEventListener('abort', () => ctrl.abort(), { once: true });
        const res = await fetch(`${base}/${path}?overview=full&geometries=polyline&alternatives=false&steps=false`, { signal: ctrl.signal });
        clearTimeout(timer);
        if (!res.ok) throw new Error(`OSRM ${res.status}`);
        const json = await res.json();
        const r = json.routes && json.routes[0];
        if (json.code !== 'Ok' || !r) throw new Error(`OSRM ${json.code}`);
        return { d: Math.round(r.distance), g: r.geometry };
      } catch (err) {
        if (signal && signal.aborted) throw err;
        lastErr = err;
      }
    }
    throw lastErr || new Error('OSRM unavailable');
  }

  async function fromServer(a, b, signal) {
    const straight = haversine([a.lat, a.lng], [b.lat, b.lng]);
    if (straight < 1500) {
      try {
        const w = await osrm('foot', a, b, signal);
        if (w.d <= Math.max(WALK_PREFERRED * 1.6, straight * 2.5)) return { mode: 'walk', ...w };
      } catch (err) { if (signal && signal.aborted) throw err; }
    }
    const c = await osrm('car', a, b, signal);
    return { mode: 'drive', ...c };
  }

  const keyOf = (a, b) => `${a.id || `${a.lat.toFixed(5)},${a.lng.toFixed(5)}`}>${b.id || `${b.lat.toFixed(5)},${b.lng.toFixed(5)}`}`;

  /** Legs shipped with data/route.json for stops that are not built-in places: { key: {mode, d, g} }. */
  function seed(legs) {
    for (const [key, l] of Object.entries(legs || {})) {
      if (l && l.g && !memo.has(key)) memo.set(key, { mode: l.mode, d: l.d, g: l.g, coords: decodePolyline(l.g) });
    }
  }

  /** Synchronous lookup: precomputed, shipped with the route, or cached from an earlier visit. */
  function quick(a, b) {
    const key = keyOf(a, b);
    if (memo.has(key)) return memo.get(key);
    const hit = fromPrecomputed(a, b) || (stored[key] && { mode: stored[key].mode, d: stored[key].d, g: stored[key].g, coords: decodePolyline(stored[key].g) });
    if (hit) memo.set(key, hit);
    return hit || null;
  }

  /** Route between two points ({lat, lng, id?}). Never rejects unless aborted. */
  async function leg(a, b, { signal } = {}) {
    const key = keyOf(a, b);
    const known = quick(a, b);
    if (known) return known;

    try {
      const r = await fromServer(a, b, signal);
      const out = { ...r, coords: decodePolyline(r.g) };
      memo.set(key, out);
      remember(key, out);
      return out;
    } catch (err) {
      if (signal && signal.aborted) throw err;
      const p1 = [a.lat, a.lng], p2 = [b.lat, b.lng];
      return { mode: 'estimate', d: haversine(p1, p2) * 1.35, coords: arc(p1, p2), estimate: true };
    }
  }

  return { leg, quick, seed };
}
