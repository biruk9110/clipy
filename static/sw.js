const CACHE_NAME = 'clipy-v3';
const PRECACHE_URLS = [
  '/',
  '/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/favicon.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)))
    ).then(() => clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // For API or video downloads, try network first
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/media') || url.pathname.startsWith('/auth')) {
    e.respondWith(
      fetch(e.request).catch(async () => {
        const cached = await caches.match(e.request);
        return cached || new Response(JSON.stringify({ error: 'Offline', offline: true }), {
          headers: { 'Content-Type': 'application/json' }
        });
      })
    );
    return;
  }

  // Stale-while-revalidate for app shell and icons
  e.respondWith(
    caches.match(e.request).then((cached) => {
      const networked = fetch(e.request).then((resp) => {
        if (resp && resp.status === 200) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(e.request, clone));
        }
        return resp;
      }).catch(() => cached);
      return cached || networked;
    })
  );
});

// Focus the app when a notification is tapped
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ('focus' in client) return client.focus();
      }
      return clients.openWindow('/');
    })
  );
});
