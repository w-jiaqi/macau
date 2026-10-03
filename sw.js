// Offline support: network-first for this site's own files, falling back to the cache
// (so the map still opens on spotty roaming data). Map tiles from other hosts are not cached.
const VERSION = 'macau-route-v3';
const SHELL = [
  './',
  'index.html',
  'manifest.webmanifest',
  'assets/css/app.css',
  'assets/js/main.js',
  'assets/js/geo.js',
  'assets/js/sheet.js',
  'assets/js/mapview.js',
  'assets/js/router.js',
  'assets/js/search.js',
  'assets/map/cartoon-sm.webp',
  'assets/map/cartoon.webp',
  'data/cartoon.json',
  'assets/icons/favicon.svg',
  'assets/icons/icon-192.png',
  'vendor/leaflet/leaflet.js',
  'vendor/leaflet/leaflet.css',
  'data/places.json',
  'data/route.json',
  'data/legs.json',
];
// The real (underneath) map is cached on first use rather than up front.

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => Promise.all(SHELL.map((url) => cache.add(url).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  event.respondWith(networkFirst(req));
});

async function networkFirst(req) {
  const cache = await caches.open(VERSION);
  // Always revalidate with the server (cheap 304s via ETag) so a new deploy shows up on the next visit
  // instead of waiting out GitHub Pages' 10-minute browser cache.
  // (A navigation Request can't be re-initialised, so fetch its URL instead.)
  const network = fetch(req.mode === 'navigate' ? req.url : req, { cache: 'no-cache' }).then((res) => {
    if (res && res.ok && res.type === 'basic') cache.put(req, res.clone());
    return res;
  });
  const cached = await cache.match(req, { ignoreSearch: true });
  if (!cached) {
    return network.catch(async () => (req.mode === 'navigate' ? (await cache.match('index.html')) || Response.error() : Response.error()));
  }
  // Prefer fresh content, but don't keep a tourist waiting on a slow connection.
  const timeout = new Promise((resolve) => setTimeout(() => resolve(cached), 3500));
  try {
    return await Promise.race([network, timeout]);
  } catch {
    return cached;
  }
}
