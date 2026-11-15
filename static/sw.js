const CACHE = 'clipy-v3';

// App shell: everything the UI needs to render without internet
const PRECACHE = [
  '/',
  '/manifest.json',
  '/static/vendor/tailwind.js',
  '/static/vendor/fonts/inter.css',
  '/static/vendor/fonts/InterVariable.woff2',
  '/static/vendor/phosphor/regular/style.css',
  '/static/vendor/phosphor/regular/Phosphor.woff2',
  '/static/vendor/phosphor/bold/style.css',
  '/static/vendor/phosphor/bold/Phosphor-Bold.woff2',
  '/static/vendor/phosphor/fill/style.css',
  '/static/vendor/phosphor/fill/Phosphor-Fill.woff2',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/favicon.png',
  '/static/icons/apple-touch-icon.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    Promise.allSettled(PRECACHE.map((url) => caches.open(CACHE).then((c) => c.add(url))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return; // let third-party requests pass through

  // Vendored UI assets & icons: cache-first (instant + offline)
  if (url.pathname.startsWith('/static/vendor') || url.pathname.startsWith('/static/icons')) {
    e.respondWith(
      caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      }))
    );
    return;
  }

  // App page: network-first, cached shell as offline fallback (so relaunch always works)
  if (e.request.mode === 'navigate' || url.pathname === '/') {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put('/', copy));
          return res;
        })
        .catch(() => caches.match('/'))
    );
    return;
  }

  // API & media: always live
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
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
