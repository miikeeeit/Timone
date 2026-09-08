#!/bin/sh
# Pubblica la Bussola su Firebase Hosting con i dati correnti.
# La cartella hosting/ è generata: contiene SOLO l'app e ui.json
# (mai .env, state.json, fiscale.csv o il Giornale).
set -e
cd "$(dirname "$0")/.."
rm -rf hosting && mkdir -p hosting/data
cp app/index.html hosting/index.html
cp data/ui.json hosting/data/ui.json
[ -f data/simboli.json ] && cp data/simboli.json hosting/data/simboli.json
firebase deploy --only hosting --project timone-8699e --non-interactive
