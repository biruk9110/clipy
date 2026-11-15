const CACHE_NAME = 'clipy-v1';

self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(clients.claim());
});

self.addEventListener('fetch', (e) => {
  // Let network handle all API and media requests directly
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});

