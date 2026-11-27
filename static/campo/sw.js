const cacheName = 'janotec-campo-v2';
const privateCache = 'janotec-actas-preparadas';
const start = self.registration.scope;
const files = ['index.html', 'visitas.js', 'envios.js'].map(path => new URL('static/campo/' + path, start).href);

self.addEventListener('install', event => {
  event.waitUntil(caches.open(cacheName).then(cache => cache.addAll(files)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key.startsWith('janotec-campo-') && key !== cacheName).map(key => caches.delete(key))
  )).then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  if (event.request.mode === 'navigate' && event.request.url === start) {
    event.respondWith(fetch(event.request).catch(() =>
      caches.open(cacheName).then(cache => cache.match(files[0])).then(response => response || Response.error())
    ));
    return;
  }
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (event.request.mode === 'navigate' && url.searchParams.has('campo')) {
    event.respondWith((async () => {
      const cache = await caches.open(privateCache);
      const response = await cache.match(event.request);
      if (response && Number(response.headers.get('X-Janotec-Vence')) > Date.now()) return response;
      if (response) await cache.delete(event.request);
      return fetch(event.request);
    })());
    return;
  }
  if (url.href.startsWith(start + 'static/') && !files.includes(url.href)) {
    event.respondWith(fetch(event.request).catch(async () => {
      const cache = await caches.open(privateCache);
      return await cache.match(event.request) || Response.error();
    }));
    return;
  }
  if (!files.includes(event.request.url)) return;
  event.respondWith(caches.open(cacheName).then(cache => cache.match(event.request)).then(
    response => response || fetch(event.request)
  ));
});
