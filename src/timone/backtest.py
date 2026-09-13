"""Backtest — riesegue la strategia sulla storia, per verificarne il comportamento.

**Cosa è, e cosa non è.** Questo non è uno strumento per cercare i parametri
"migliori": ottimizzare su un passato noto produce numeri lusinghieri e nessuna
garanzia. Serve a rispondere a domande di *comportamento*:

  * con questi parametri, quante operazioni avrebbe fatto il motore?
  * quante volte un guardrail avrebbe fermato un ordine, e quale?
  * l'Àncora automatica sarebbe scattata? quando, e per quale drawdown?

Il valore architetturale: la simulazione riusa **le stesse identiche funzioni**
del motore reale — `compute_orders` e `evaluate_order` — senza riscriverle. È
possibile solo perché sono pure e deterministiche: stessi input, stessi ordini.
Se la strategia cambia, il backtest cambia con lei, automaticamente.

`simula()` non tocca la rete, né lo stato, né il broker: prende una serie di
giorni già pronti. Lo scarico dei dati vive in fondo al modulo, separato apposta.

**Limiti dichiarati** (un backtest che li nasconde mente):
  * gli ordini si eseguono al prezzo di chiusura del giorno, senza slippage;
  * nessuna commissione (Alpaca non ne applica, ma un broker vero potrebbe);
  * nessun fill parziale e nessun ordine rifiutato dal mercato;
  * i dividendi non sono considerati.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import guardrails as gr
from . import safety
from .models import OrderSide, Rotta
from .strategy_dca import compute_orders


@dataclass(frozen=True)
class GiornoMercato:
    """Un giorno di mercato: prezzi di chiusura in USD e cambio EUR/USD."""

    data: str  # ISO date
    prezzi_usd: dict[str, float]
    eur_usd: float


@dataclass
class Risultato:
    giorni: int = 0
    ordini_eseguiti: int = 0
    ordini_scartati: dict[str, int] = field(default_factory=dict)
    versato_eur: float = 0.0
    valore_finale_eur: float = 0.0
    pnl_eur: float = 0.0
    pnl_titoli_eur: float = 0.0
    pnl_cambio_eur: float = 0.0
    quote: dict[str, float] = field(default_factory=dict)
    #: drawdown massimo osservato, con la stessa formula della sicurezza attiva
    max_drawdown: float = 0.0
    max_drawdown_il: str | None = None
    #: primo giorno in cui l'Àncora automatica sarebbe scattata per drawdown
    ancora_il: str | None = None


def simula(
    rotta: Rotta,
    giorni: list[GiornoMercato],
    *,
    limiti: dict | None = None,
    ferma_su_ancora: bool = True,
) -> Risultato:
    """Riesegue la strategia giorno per giorno. Nessuna rete, nessuno stato.

    `ferma_su_ancora=True` (predefinito) è la simulazione FEDELE: quando il
    drawdown supera la soglia della sicurezza attiva, il motore reale si
    fermerebbe — e qui la simulazione si ferma con lui. Continuare a comprare
    oltre quel punto produrrebbe numeri che non sarebbero mai esistiti.
    Metterlo a False risponde invece alla domanda ipotetica "e se avessi
    rialzato subito l'Àncora?".
    """
    quote: dict[str, float] = {t: 0.0 for t in rotta.tickers}
    costo_usd = 0.0
    r = Risultato()

    ultimo_simulato: GiornoMercato | None = None
    for g in sorted(giorni, key=lambda x: x.data):
        prezzi = g.prezzi_usd
        if not any(t in prezzi for t in rotta.tickers):
            continue  # giorno senza dati utili: si salta
        r.giorni += 1
        ultimo_simulato = g

        valori_eur = {
            t: quote[t] * prezzi[t] / g.eur_usd
            for t in rotta.tickers
            if t in prezzi
        }

        # --- stesse funzioni del motore reale, non una copia ---
        ordini = compute_orders(rotta, valori_eur)

        spesa_oggi = 0.0
        approvati = 0
        for o in ordini:
            esito = gr.evaluate_order(
                o,
                allowed_tickers=rotta.tickers,
                spent_today_eur=spesa_oggi,
                approved_so_far=approvati,
                limits=limiti,
            )
            if not esito.approved:
                r.ordini_scartati[esito.rule] = r.ordini_scartati.get(esito.rule, 0) + 1
                continue
            if o.side is not OrderSide.BUY:
                continue  # il motore non vende da solo: nulla da simulare
            if o.ticker not in prezzi:
                continue
            approvati += 1
            spesa_oggi += o.notional_eur
            usd = o.notional_eur * g.eur_usd
            quote[o.ticker] += usd / prezzi[o.ticker]
            costo_usd += usd
            r.versato_eur += o.notional_eur
            r.ordini_eseguiti += 1

        # --- drawdown con la stessa formula della sicurezza attiva ---
        valore_usd = sum(
            quote[t] * prezzi[t] for t in rotta.tickers if t in prezzi
        )
        valore_eur = valore_usd / g.eur_usd
        if r.versato_eur > 0:
            dd = (r.versato_eur - valore_eur) / r.versato_eur
            if dd > r.max_drawdown:
                r.max_drawdown = dd
                r.max_drawdown_il = g.data
            if dd > safety.MAX_DRAWDOWN and r.ancora_il is None:
                r.ancora_il = g.data
                if ferma_su_ancora:
                    break  # da qui in poi il motore vero sarebbe rimasto fermo

    # --- chiusura: valori all'ultimo giorno EFFETTIVAMENTE simulato ---
    ultimo = ultimo_simulato
    if ultimo:
        valore_usd = sum(
            quote[t] * ultimo.prezzi_usd[t]
            for t in rotta.tickers
            if t in ultimo.prezzi_usd
        )
        r.valore_finale_eur = valore_usd / ultimo.eur_usd
        r.pnl_eur = r.valore_finale_eur - r.versato_eur
        # Titoli e cambio restano due storie separate, come nel motore.
        r.pnl_titoli_eur = (valore_usd - costo_usd) / ultimo.eur_usd
        r.pnl_cambio_eur = r.pnl_eur - r.pnl_titoli_eur
    r.quote = {t: q for t, q in quote.items() if q > 0}
    return r


# ═══════════════════════════════════════════════════════════════════════════
#  Strato di IO — l'unica parte che tocca la rete. Tenuto fuori da `simula`
#  perché la simulazione resti pura e testabile con dati sintetici.
# ═══════════════════════════════════════════════════════════════════════════

def serie_cambio(da: str, a: str, *, timeout: float = 20.0) -> dict[str, float]:
    """Serie EUR/USD del periodo, in UNA sola chiamata (non una al giorno)."""
    import json
    import urllib.request

    from . import __version__ as _v

    url = f"https://api.frankfurter.app/{da}..{a}?from=EUR&to=USD"
    req = urllib.request.Request(url, headers={"User-Agent": f"timone/{_v}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8"))
    return {
        giorno: float(v["USD"])
        for giorno, v in (payload.get("rates") or {}).items()
        if "USD" in v
    }


def serie_prezzi(settings, tickers, da: str, a: str) -> dict[str, dict[str, float]]:
    """Chiusure giornaliere in USD da Alpaca, per giorno e ticker."""
    from datetime import datetime

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(settings.api_key, settings.api_secret)
    richiesta = StockBarsRequest(
        symbol_or_symbols=list(tickers),
        timeframe=TimeFrame.Day,
        start=datetime.fromisoformat(da),
        end=datetime.fromisoformat(a),
    )
    barre = client.get_stock_bars(richiesta)
    out: dict[str, dict[str, float]] = {}
    for simbolo, serie in (barre.data or {}).items():
        for b in serie:
            giorno = b.timestamp.date().isoformat()
            out.setdefault(giorno, {})[simbolo] = float(b.close)
    return out


def carica_giorni(settings, rotta: Rotta, da: str, a: str) -> list[GiornoMercato]:
    """Assembla i giorni di mercato pronti per `simula`."""
    prezzi = serie_prezzi(settings, rotta.tickers, da, a)
    cambi = serie_cambio(da, a)
    giorni: list[GiornoMercato] = []
    ultimo = None
    for giorno in sorted(prezzi):
        # Il calendario BCE e quello di Wall Street non coincidono sempre:
        # per un giorno senza fixing si usa l'ultimo cambio noto.
        ultimo = cambi.get(giorno, ultimo)
        if ultimo is None:
            continue
        giorni.append(GiornoMercato(giorno, prezzi[giorno], ultimo))
    return giorni
