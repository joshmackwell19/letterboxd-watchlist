// The dashboard's service worker — here only for Web Push. It deliberately
// has no fetch handler: the page is rebuilt several times a day and
// pull-to-refresh relies on the network copy, so nothing is cached.
//
// Deployed next to index.html (see daily.yml's "Prepare Pages site"), so its
// scope is the whole dashboard. Payloads come from the daily run
// (html_email.newly_streaming_notifications): {title, body, url, tag}.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : '' };
  }
  // iOS revokes the subscription of a push that shows nothing, so every
  // push shows something, even a malformed one.
  event.waitUntil(self.registration.showNotification(data.title || 'Watchlist', {
    body: data.body || '',
    tag: data.tag,
    icon: 'icons/icon-192.png',
    data: { url: data.url || './' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = new URL(event.notification.data.url, self.registration.scope).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const open = windows.find((w) => w.url.startsWith(self.registration.scope));
    if (open) {
      // The page opens the film itself (see its 'message' listener) — cheaper
      // than a reload of a multi-megabyte page that's already there.
      await open.focus();
      open.postMessage({ type: 'open-url', url });
      return;
    }
    await self.clients.openWindow(url);
  })());
});
