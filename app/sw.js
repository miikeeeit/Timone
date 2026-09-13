/* Service worker di Timone — serve solo alle notifiche push.
 *
 * Niente cache offline: la console mostra dati finanziari, e servirli da una
 * cache vecchia senza dirlo sarebbe peggio che non mostrarli. Qui si gestisce
 * unicamente il messaggio in arrivo quando l'app è chiusa.
 *
 * Le notifiche arrivano SOLO per eccezioni (Àncora calata, run fallito, battito
 * mancato): il silenzio resta una buona notizia.
 */
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js');

firebase.initializeApp({
  apiKey: "AIzaSyAaNkOzoVu5sbGALnNjKNtAXpe9hl8x5Pc",
  authDomain: "timone-8699e.firebaseapp.com",
  projectId: "timone-8699e",
  messagingSenderId: "946988635365",
  appId: "1:946988635365:web:5ba7eeeb6326f74a30300c",
});

const messaging = firebase.messaging();

messaging.onBackgroundMessage(payload => {
  const n = payload.notification || {};
  self.registration.showNotification(n.title || 'Timone', {
    body: n.body || '',
    icon: '/icona.png',
    badge: '/icona.png',
    tag: 'timone-eccezione',   // una sola notifica per volta, non una pila
    requireInteraction: false,
  });
});

// Toccando la notifica si apre la console (o si porta in primo piano).
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(lista => {
      for (const c of lista) {
        if ('focus' in c) return c.focus();
      }
      return clients.openWindow ? clients.openWindow('/') : null;
    })
  );
});
