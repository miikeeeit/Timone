# Timone

> Esecuzione disciplinata di una strategia d'investimento su azioni ed ETF — un motore deterministico che compra a orari fissi secondo una rotta decisa a monte, dentro limiti di sicurezza invalicabili. *Tu decidi la rotta, lui la mantiene.*

**Stato del progetto:** prototipo funzionante, in **paper trading** (denaro simulato) su [Alpaca](https://alpaca.markets). Sviluppato come base che può evolvere verso un prodotto reale; la roadmap verso la gestione di capitale vero è esplicita e vincolata a criteri di affidabilità (vedi *Prossimi passi*).

- **Cosa funziona oggi:** motore di calcolo ordini (DCA + ribilanciamento a soglie), guardrail di sicurezza nel codice, esecuzione via API Alpaca, giornale di bordo con catena hash verificabile, log fiscale italiano (LIFO + cambio BCE), sicurezza attiva (kill switch automatico), versionamento della strategia con quarantena, diagnostica di sistema (`timone doctor`), diario settimanale in linguaggio naturale, CLI completa, console web di sola lettura con Àncora azionabile da mobile. 147 test automatici.
- **Cosa manca / è in corso:** nulla di strutturale; resta la valutazione del passaggio a capitale reale. Nessun percorso verso il trading *live*: l'endpoint reale è bloccato nel codice.

---

## Il problema

Chi investe con un piano di accumulo affronta due nemici silenziosi: **la propria emotività** (comprare o vendere nel momento sbagliato, cambiare strategia sull'onda di una notizia) e **i costi di transazione** che erodono i piccoli capitali. Gli strumenti disponibili tendono a peggiorare il primo problema: notifiche di prezzo, grafici in tempo reale, meccaniche che spingono a operare di più.

Timone nasce da un'inversione: **separare la decisione dall'esecuzione**. L'utente decide una volta, a mente fredda, *quale* strategia seguire; il software la esegue con precisione, ogni giorno, senza interpretazioni e senza sollecitare l'utente ad agire. La qualità del sistema si misura in **affidabilità e trasparenza**, non in rendimento.

---

## Come funziona

Ad ogni esecuzione schedulata (una al giorno, nei giorni di mercato aperto):

1. **Verifica il kill switch (Àncora).** Se è attivo — manualmente o perché scattato in automatico — logga il motivo e si ferma.
2. **Verifica il contesto:** mercato aperto (calendario Alpaca) e finestra oraria consentita.
3. **Legge la Rotta:** la strategia versionata (ticker, pesi target, importo per run, soglia di ribilanciamento).
4. **Scarica posizioni e prezzi** dal broker e calcola il cambio EUR/USD del giorno (fonte BCE).
5. **Calcola gli ordini** in modo deterministico: distribuisce il versamento sui titoli sottopesati. **Solo acquisti**: se un peso è uscito dalla banda, il motore smette di alimentarlo e lo segnala, ma non vende mai per ribilanciare.
6. **Fa passare ogni ordine dai guardrail.** Un ordine che viola un limite non viene "aggiustato": viene scartato e loggato con la regola violata.
7. **Esegue gli ordini approvati** con un identificativo deterministico (idempotenza: rieseguire non duplica), verifica il fill, aggiorna lo stato.
8. **Scrive il Giornale di bordo** (registro strutturato per ogni run) e il **log fiscale**, e sigilla il run nella catena hash.

Tutto è ispezionabile da riga di comando (`timone status`, `verifica`, `fiscale`, `rotta`, `limiti`, `approdo`, …) e da un'interfaccia web di sola lettura.

Un `dry-run` calcola gli ordini e li mostra **senza inviarli**. Dati d'esempio (fittizi):

```text
$ timone dry-run

Giornale di bordo — run 20260305
Ordini proposti: 2 (approvati 1, rifiutati 1).
  APPROVATO BUY AGGH 70.00 EUR — DCA: versamento indirizzato al sottopeso (target 40.0%).
  RIFIUTATO BUY VWCE 130.00 EUR — regola 'max_order_eur': Ordine da 130.00 EUR oltre il massimo di 120.00 EUR.
Nessun ordine eseguito in questo run.
Nota: [dry-run] Ora 16:05 dentro la finestra 15:30-22:00.

(dry-run: nessun ordine è stato inviato.)
```

La riga **RIFIUTATO** è il punto: un ordine che sfora un limite non viene ridotto per rientrare — viene **scartato e registrato** con la regola violata. I guardrail vivono nel codice, non nella configurazione.

---

## Decisioni di progetto

Questa è la sezione che spiega *come ragiono*: le scelte non ovvie, e cosa ho scartato.

### 1. Ribilanciamento a soglie di scostamento, non a calendario

**Scelta:** si ribilancia quando il peso effettivo di un titolo si allontana dal target oltre una soglia in punti percentuali (es. ±5 pp), non a date fisse.

**Alternativa scartata:** il ribilanciamento a calendario (es. "ogni trimestre riporta tutto ai pesi target").

**Perché:** il calendario è arbitrario rispetto a ciò che conta davvero, cioè *quanto* il portafoglio ha deviato. Ribilancia anche quando non serve (generando costi inutili) e ignora derive importanti tra una data e l'altra. Le soglie legano l'azione alla causa reale — lo scostamento — riducendo il numero di operazioni su un capitale piccolo, dove i costi di transazione sono il vero avversario. In più, il ribilanciamento avviene **solo indirizzando i nuovi versamenti** verso i titoli sottopesati: il motore non vende mai per tornare ai pesi target. Se un titolo è sovrappeso oltre la soglia, semplicemente smette di alimentarlo e lo segnala — ridurlo resta una decisione tua.

### 2. Nessuna regola di ingresso discrezionale, nessun consiglio sui titoli

**Scelta:** il motore è **puramente deterministico**. Non prevede il mercato, non genera segnali, non suggerisce quali titoli comprare. I ticker e i pesi sono una *dichiarazione dell'utente*, non un output del software.

**Alternativa scartata:** aggiungere indicatori tecnici, "market timing", o una lista di "titoli del momento".

**Perché:** tre ragioni convergenti. *Tecnica* — una regola deterministica è testabile e riproducibile (stessi input → stessi ordini); un decisore discrezionale o predittivo non lo è, e diventa impossibile capire a posteriori perché il sistema ha agito. *Di prodotto* — l'affidabilità e la fiducia sono il valore; un motore di raccomandazioni trasformerebbe Timone nell'ennesima app che spinge a operare. *Normativa* — raccomandare strumenti finanziari specifici è consulenza, un'attività riservata. La separazione tra *chi decide* (l'utente, a monte) e *chi esegue* (il motore) è la scelta architetturale più importante del progetto.

### 3. Rimozione deliberata degli elementi di gamification

**Scelta:** l'interfaccia non ha notifiche di prezzo, grafici a candele, colori-ricompensa o meccaniche di ingaggio. Gli avvisi arrivano **solo per eccezioni** (un guardrail scattato, il kill switch, un run fallito); il colore verde/rosso appare *solo* sui numeri di rendimento, mai come decorazione. *Il silenzio è una buona notizia.*

**Alternativa scartata:** la dashboard "coinvolgente" standard delle app di trading.

**Perché:** la gamification ottimizza per la frequenza d'uso, che è esattamente il comportamento dannoso per un piano di accumulo di lungo periodo. Un'interfaccia che invita a guardare e ad agire lavora *contro* l'obiettivo dell'utente. Progettare per la calma è una scelta di prodotto coerente con la strategia sottostante.

### Altre scelte coerenti

- **Guardrail nel codice, non nella configurazione.** Importo massimo per ordine, budget giornaliero, whitelist dei ticker, finestra oraria, tetto agli ordini per run: sono costanti nel motore, verificate prima di ogni ordine. Renderli più permissivi richiede un periodo di attesa di 72 ore e una doppia conferma; restringerli è immediato. Il tempo è parte della protezione.
- **Solo paper trading, per costruzione.** L'endpoint reale di Alpaca è rifiutato dal codice: nessun percorso verso denaro vero, nemmeno per errore di configurazione.
- **Rischio di cambio tracciato a parte.** Con titoli quotati in USD, il rendimento in euro è due storie distinte — i titoli e il cambio EUR/USD — e il sistema le tiene separate invece di confonderle in un unico numero. Vale anche per la sicurezza: l'Àncora automatica misura il drawdown **sui soli titoli**, perché un calo dovuto al cambio non è un motivo per smettere di comprare — anzi, con l'euro più forte ogni versamento compra più dollari.
- **Vendere è sempre un gesto manuale.** L'unica azione che il motore compie da solo è *fermarsi* (calare l'Àncora); comprare è schedulato, vendere è deciso dall'utente — **ribilanciamento incluso**: la strategia emette solo ordini di acquisto, per costruzione. Le soglie di uscita, quando impostate, **avvisano soltanto**.
- **Il backtest verifica il comportamento, non insegue il rendimento.** Cercare i parametri "migliori" su un passato noto produce numeri lusinghieri e nessuna garanzia. `timone backtest` risponde ad altre domande: quante operazioni avrebbe fatto il motore, quali guardrail sarebbero scattati, e soprattutto **se l'Àncora automatica sarebbe calata** — e in quel caso la simulazione si ferma lì, dove si sarebbe fermato il motore vero. Riusa le stesse funzioni del motore, non una copia: è possibile perché la strategia è pura e deterministica.
- **Tracciabilità a prova di manomissione.** Ogni run è sigillato in una catena hash (SHA-256 concatenato): lo storico è verificabile e una modifica retroattiva spezza la catena.

---

## Stack tecnologico

- **Python 3.11+** — motore, strategia, guardrail, CLI
- **[alpaca-py](https://github.com/alpacahq/alpaca-py)** — API broker (dati e ordini, solo paper)
- **PyYAML** — configurazione della strategia
- **python-dotenv** — gestione delle credenziali via variabili d'ambiente
- **pytest** — 147 test automatici (guardrail, calcolo ordini, LIFO fiscale, sicurezza attiva, motore, diagnostica, resilienza di rete)
- **HTML/CSS/JS vanilla** + **Firebase Hosting/Auth** — interfaccia web di sola lettura (PWA)
- Fonte cambio: **BCE** via [frankfurter.app](https://frankfurter.app)

Architettura a moduli con responsabilità separate: `strategy_dca` (calcolo puro), `guardrails` (limiti), `state` (persistenza astratta), `broker_alpaca` (unico punto di contatto col broker), `engine` (orchestrazione), `fiscal`, `safety`, `route_control`, `approdo`, `logbook`, `cli`.

---

## Installazione

Richiede Python 3.11+ e un conto **paper** su Alpaca (chiavi API create *senza* permessi di prelievo).

```bash
# 1. Ambiente virtuale
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Dipendenze + comando `timone`
pip install -r requirements.txt
pip install -e .

# 3. Credenziali (mai versionate)
cp .env.example .env             # poi inserisci le chiavi paper Alpaca

# 4. Strategia
cp rotta.example.yaml rotta.yaml # poi scegli i tuoi ticker, pesi, importo, soglia

# 5. Prova a vuoto (calcola e mostra gli ordini senza inviarli)
timone dry-run
```

Comandi principali:

```
timone dry-run     calcola e mostra gli ordini senza inviarli
timone run         esegue un ciclo sul conto paper
timone status      posizioni, ultimo run, stato Àncora
timone verifica    riverifica la catena hash del Giornale
timone fiscale     riepilogo annuale (LIFO, 26%) + simulatore
timone rotta       versioni della strategia e quarantena delle modifiche
timone limiti      limiti effettivi e richiesta di allargamento (72h)
timone approdo     anteprima vendita e soglie di rientro (solo avviso)
timone narratore   diario settimanale di cosa ha fatto il motore
timone doctor      controlli di salute del sistema
timone backtest    riesegue la strategia sulla storia (--da / --a)
timone ancora --drop / --raise    kill switch manuale
```

L'esecuzione è pensata per essere schedulata (es. `cron`, un run al giorno nei giorni feriali).

---

## Prossimi passi

- [x] Motore DCA + ribilanciamento a soglie, con test
- [x] Guardrail nel codice + kill switch (manuale e automatico)
- [x] Giornale di bordo con catena hash verificabile
- [x] Log fiscale italiano (LIFO, cambio BCE, scomposizione del rischio di cambio)
- [x] Strategia versionata con quarantena e cooling-off sui limiti
- [x] Interfaccia web di sola lettura (PWA) con accesso protetto (i dati stanno dietro autenticazione, non su file pubblici)
- [x] Diagnostica di sistema `timone doctor` (endpoint, chiavi, cambio, scheduler, dati, catena, pubblicazione, Àncora)
- [x] Àncora azionabile da mobile: la console invia l'intento, il motore resta l'unico che lo applica
- [x] Resilienza di rete: un blip di connettività non fa fallire il run né calare l'Àncora
- [x] Report settimanale in linguaggio naturale che *spiega* le operazioni (senza mai deciderle)
- [x] Riconciliazione degli ordini non riempiti nei run successivi
- [x] Backtest storico che verifica il *comportamento* (guardrail scattati, Àncora calata), non il rendimento
- [x] Notifiche push su mobile (solo eccezioni: Àncora calata, run fallito, battito mancato)
- [ ] Valutazione del passaggio a capitale reale — **solo** dopo un periodo prolungato di paper trading pulito (zero violazioni dei guardrail, catena integra, log fiscale completo). Decisione esplicita, mai automatica.

---

## Nota

Timone è un **progetto personale a scopo di studio**, in paper trading. **Non è consulenza finanziaria** e non fornisce raccomandazioni d'investimento: esegue una strategia decisa dall'utente. Le scelte di allocazione mostrate negli esempi sono placeholder tecnici, non suggerimenti.
