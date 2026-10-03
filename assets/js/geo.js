// Small geometry + formatting helpers (no dependencies).

const R = 6371008.8;
const rad = (d) => (d * Math.PI) / 180;

/** Great-circle distance in metres between [lat, lng] pairs. */
export function haversine(a, b) {
  const dLat = rad(b[0] - a[0]);
  const dLng = rad(b[1] - a[1]);
  const s =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
}

/** Decode a Google encoded polyline into [[lat, lng], ...]. */
export function decodePolyline(str, precision = 5) {
  const factor = 10 ** precision;
  const out = [];
  let i = 0, lat = 0, lng = 0;
  while (i < str.length) {
    for (let k = 0; k < 2; k++) {
      let b, shift = 0, result = 0;
      do {
        b = str.charCodeAt(i++) - 63;
        result |= (b & 0x1f) << shift;
        shift += 5;
      } while (b >= 0x20);
      const delta = result & 1 ? ~(result >> 1) : result >> 1;
      if (k === 0) lat += delta; else lng += delta;
    }
    out.push([lat / factor, lng / factor]);
  }
  return out;
}

/** Total length (m) of a [[lat,lng]...] path. */
export function pathLength(coords) {
  let d = 0;
  for (let i = 1; i < coords.length; i++) d += haversine(coords[i - 1], coords[i]);
  return d;
}

/** Point at fraction f (0..1) along a path, plus the segment it lies on. */
export function pointAlong(coords, f) {
  if (coords.length < 2) return { point: coords[0], a: coords[0], b: coords[0] };
  const total = pathLength(coords);
  let target = total * f, acc = 0;
  for (let i = 1; i < coords.length; i++) {
    const seg = haversine(coords[i - 1], coords[i]);
    if (acc + seg >= target && seg > 0) {
      const t = (target - acc) / seg;
      const a = coords[i - 1], b = coords[i];
      return { point: [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t], a, b };
    }
    acc += seg;
  }
  const n = coords.length;
  return { point: coords[n - 1], a: coords[n - 2], b: coords[n - 1] };
}

/** A gentle arc between two points, used when no precomputed route exists. */
export function arc(a, b, bend = 0.18, steps = 24) {
  const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const dx = b[1] - a[1], dy = b[0] - a[0];
  const ctrl = [mid[0] + dx * bend, mid[1] - dy * bend];
  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const t = i / steps, u = 1 - t;
    pts.push([
      u * u * a[0] + 2 * u * t * ctrl[0] + t * t * b[0],
      u * u * a[1] + 2 * u * t * ctrl[1] + t * t * b[1],
    ]);
  }
  return pts;
}

export function formatDistance(m) {
  if (m < 950) return `${Math.max(10, Math.round(m / 10) * 10)} 米`;
  return `${(m / 1000).toFixed(m < 9950 ? 1 : 0)} 公里`;
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
