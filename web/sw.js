/* Acta dashboard service worker — minimal, network-first.
   Exists mainly to satisfy PWA installability; the app shell is cached
   so the UI still opens if the server is briefly unreachable.
   Never intercepts non-GET, so POST/PUT/DELETE to /api/* pass through. */
/* Bump the version on every shell change (index.html, css/, js/).
   Any change to the string works — activate deletes every cache that does not
   match, so the installed PWA picks up new code instead of a stale copy. */
const CACHE = 'acta-dashboard-v3.0';
const SHELL = ['/',
  '/css/base.css', '/css/phone.css', '/css/workout.css', '/css/auspex.css', '/css/refinement.css',
  '/js/app/chart-scale.js', '/js/app/core.js', '/js/app/health-charts.js', '/js/app/day-context.js',
  '/js/app/live.js', '/js/app/info-modal.js', '/js/app/chart-text.js',
  '/js/workout.js', '/js/productivity.js', '/js/finance.js', '/js/finance_spend.js', '/js/auspex.js',
  '/manifest.json',
  '/icons/icon-192.png', '/icons/icon-512.png',
  // Self-hosted faces — cached so the installed PWA renders correctly offline.
  // Both families must be here: a font missing from this list is lost offline.
  '/fonts/geist-300.woff2', '/fonts/geist-400.woff2', '/fonts/geist-500.woff2', '/fonts/geist-600.woff2',
  '/fonts/jetbrains-mono-latin.woff2', '/fonts/jetbrains-mono-latin-ext.woff2'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;  // never touch API writes
  const url = new URL(e.request.url);
  // Never cache API responses or uploaded videos — always go to network.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/exercise-videos/')) return;
  e.respondWith(
    fetch(e.request)
      .then(resp => {
        if (SHELL.includes(url.pathname)) {
          const copy = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return resp;
      })
      // ignoreSearch because index.html requests scripts with a cache-busting query
      // (js/workout.js?v=6) while SHELL pre-caches them without one. Offline, a
      // slightly older cached copy is right; online the network still wins, since
      // this only runs after fetch() has failed.
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
