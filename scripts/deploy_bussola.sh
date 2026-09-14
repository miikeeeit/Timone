#!/bin/sh
# Pubblica la Bussola su Firebase Hosting.
# La cartella hosting/ è generata: contiene SOLO l'app e l'anagrafica dei
# simboli negoziabili (un elenco pubblico di ticker, non un dato personale).
#
# I DATI DEL PORTAFOGLIO NON SI PUBBLICANO PIÙ QUI. Un file su Hosting è
# leggibile da chiunque conosca l'URL: il login della PWA è un gate JavaScript,
# non protegge i file statici. I dati vivono su Firestore, dove le regole li
# lasciano leggere solo al proprietario autenticato (li scrive `timone export-ui`).
set -e
cd "$(dirname "$0")/.."
rm -rf hosting && mkdir -p hosting/data
cp app/index.html hosting/index.html
# Il service worker DEVE stare nella root: il suo scope dipende dal percorso da
# cui viene servito. Serve solo alle notifiche push, non fa cache dei dati.
cp app/sw.js hosting/sw.js
# Manifest e icone come FILE veri: senza, Chrome non installa la PWA come app
# (WebAPK) e la lascia un collegamento — notifiche comprese, attribuite a Chrome.
cp app/manifest.webmanifest hosting/manifest.webmanifest
mkdir -p hosting/icons && cp app/icons/*.png hosting/icons/
# `if` invece di `[ ... ] && cp`: con `set -e` un test negativo faceva uscire lo
# script PRIMA del deploy, saltandolo in silenzio.
if [ -f data/simboli.json ]; then
  cp data/simboli.json hosting/data/simboli.json
fi
firebase deploy --only hosting --project timone-8699e --non-interactive
