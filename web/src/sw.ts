/// <reference lib="webworker" />
import { cleanupOutdatedCaches, precacheAndRoute } from 'workbox-precaching'

declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<{ url: string; revision: string | null }>
}

precacheAndRoute(self.__WB_MANIFEST)
cleanupOutdatedCaches()

self.addEventListener('push', (event) => {
  let title = 'Pollen'
  let body = ''
  let url = '/ui/'
  try {
    const payload = event.data?.json() as { title?: string; body?: string; url?: string }
    if (payload.title) title = payload.title
    if (payload.body) body = payload.body
    if (payload.url) url = payload.url
  } catch {
    body = event.data?.text() ?? ''
  }
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: '/pwa-192.png',
      badge: '/pwa-192.png',
      data: { url },
    }),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = (event.notification.data as { url?: string } | undefined)?.url ?? '/ui/'
  event.waitUntil(self.clients.openWindow(url))
})
