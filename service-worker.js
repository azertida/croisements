/* Croisements - service worker
 *
 * Strategie : on sert d'abord le cache, puis on rafraichit en arriere-plan.
 * L'appli s'ouvre donc instantanement, y compris dans un tunnel.
 * Contrepartie assumee : apres un depot de nouveaux fichiers, la premiere
 * ouverture montre encore l'ancienne version ; la suivante est a jour.
 *
 * Pour forcer un renouvellement complet, changer VERSION.
 */

const VERSION = "croisements-v3";

const FICHIERS = [
  "./",
  "./index.html",
  "./croisements.json",
  "./manifest.json",
  "./icon-180.png",
  "./icon-192.png",
  "./icon-512.png",
];

self.addEventListener("install", evenement => {
  evenement.waitUntil(
    caches.open(VERSION)
      .then(cache => cache.addAll(FICHIERS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", evenement => {
  evenement.waitUntil(
    caches.keys()
      .then(noms => Promise.all(
        noms.filter(nom => nom !== VERSION).map(nom => caches.delete(nom))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", evenement => {
  const requete = evenement.request;

  // On ne s'occupe que des lectures de cette appli.
  if (requete.method !== "GET") return;
  if (new URL(requete.url).origin !== self.location.origin) return;

  evenement.respondWith(
    caches.match(requete).then(enCache => {
      const surLeReseau = fetch(requete)
        .then(reponse => {
          if (reponse && reponse.ok) {
            const copie = reponse.clone();
            caches.open(VERSION).then(cache => cache.put(requete, copie));
          }
          return reponse;
        })
        .catch(() => enCache);   // hors ligne : le cache fait foi

      return enCache || surLeReseau;
    })
  );
});
