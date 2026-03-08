/* Community Call — Service Worker
   Strategy:
     • HTML (/)          — network-first: always try to get the freshest shell,
                           fall back to cache only when offline.
     • CSS / JS / icons  — stale-while-revalidate: serve cache instantly, then
                           update the cache entry in the background so the next
                           load is fresh.  This prevents the "stuck on old version"
                           problem that cache-first causes.
     • /api/* and /ws    — always network, never cached.

   Bump CACHE_VER whenever you do a breaking deploy to force all clients to
   re-fetch everything immediately.
*/

const CACHE_VER  = 'cc-v6';
const CACHE_ASSETS = 'cc-assets-v6';

const PRECACHE = [
  '/static/css/style.css',
  '/static/js/app.js',
  '/static/icons/icon.svg',
  '/manifest.json',
];

// ---------------------------------------------------------------------------
// Install — pre-cache static assets only (not the HTML shell)
// ---------------------------------------------------------------------------
self.addEventListener('install', evt => {
  evt.waitUntil(
    caches.open(CACHE_ASSETS)
      .then(c => c.addAll(PRECACHE))
      .then(() => self.skipWaiting())   // activate immediately, don't wait
  );
});

// ---------------------------------------------------------------------------
// Activate — delete every old cache version
// ---------------------------------------------------------------------------
self.addEventListener('activate', evt => {
  const keep = new Set([CACHE_VER, CACHE_ASSETS]);
  evt.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => !keep.has(k)).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// ---------------------------------------------------------------------------
// Push — show a notification when the app is in the background/closed
// ---------------------------------------------------------------------------

self.addEventListener('push', evt => {
  if (!evt.data) return;
  let payload;
  try { payload = evt.data.json(); } catch { payload = { title: 'Community Call', body: evt.data.text() }; }

  const priority = payload.priority || 'normal';
  const vibrate =
    priority === 'emergency' ? [200, 100, 200, 100, 200, 100, 200] :
    priority === 'urgent'    ? [200, 100, 200] :
                               [200];

  const iconUrl = self.registration.scope.replace(/\/$/, '') + '/static/icons/icon-192.png';
  const options = {
    body:               payload.body || '',
    icon:               iconUrl,
    badge:              iconUrl,
    tag:                payload.tag  || 'commcall',
    renotify:           true,
    requireInteraction: priority === 'emergency',
    vibrate,
    data: { url: '/' },
  };

  evt.waitUntil(
    self.registration.showNotification(payload.title || 'Community Call', options)
  );
});

// Tap notification → focus existing window or open a new one
self.addEventListener('notificationclick', evt => {
  evt.notification.close();
  evt.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(wins => {
      const existing = wins.find(w => w.url.startsWith(self.registration.scope));
      if (existing) return existing.focus();
      return clients.openWindow('/');
    })
  );
});

// ---------------------------------------------------------------------------
// Fetch
// ---------------------------------------------------------------------------
self.addEventListener('fetch', evt => {
  const { request } = evt;
  const url = new URL(request.url);

  // Never intercept API calls or WebSocket upgrades
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws') return;

  // HTML (navigation requests) — network-first, cache as fallback
  if (request.mode === 'navigate' || url.pathname === '/') {
    evt.respondWith(
      fetch(request)
        .then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_VER).then(c => c.put(request, clone));
          }
          return response;
        })
        .catch(() => caches.match(request).then(cached => cached || caches.match('/')))
    );
    return;
  }

  // Static assets — stale-while-revalidate
  evt.respondWith(
    caches.open(CACHE_ASSETS).then(async cache => {
      const cached = await cache.match(request);

      const fetchPromise = fetch(request).then(response => {
        if (response.ok && request.method === 'GET') {
          cache.put(request, response.clone());
        }
        return response;
      }).catch(() => null);

      // Return cached immediately; revalidate in background
      return cached || fetchPromise;
    })
  );
});
