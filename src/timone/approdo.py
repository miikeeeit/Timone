"""Approdo — vendere con la stessa disciplina degli acquisti.

Il software non decide MAI quando vendere: esegue la decisione dell'utente.
Le soglie di rientro sono SOLO avvisi ("Approdo in vista"): al raggiungimento
Timone segnala, non vende. Vendere resta un gesto manuale, con nota del
capitano obbligatoria, anteprima fiscale LIFO, guardrail e sigillo.
"""

from __future__ import annotations

import copy
from datetime import datetime

from . import guardrails as gr
from .broker_alpaca import Broker
from .fiscal import FiscalLog, apply_sell_lifo, get_eur_usd
from .logbook import GENESIS, Logbook
from .models import OrderSide
from .state import StateStore, TimoneState

TAX_RATE = 0.26


def preview(state: TimoneState, positions: dict, fx) -> list[dict]:
    """Anteprima per ticker: se vendessi ORA, plusvalenza LIFO, imposta, netto."""
    out = []
    for ticker in sorted(state.tax_lots):
        lots = state.tax_lots[ticker]
        qty_lots = sum(lot.qty for lot in lots)
        if qty_lots <= 1e-9:
            continue
        pos = positions.get(ticker)
        value_eur = fx.usd_to_eur(pos.market_value_usd) if pos else 0.0
        lots_copy = copy.deepcopy(lots)
        realized, _ = apply_sell_lifo(lots_copy, qty_lots, value_eur)
        imposta = max(0.0, realized) * TAX_RATE
        out.append({
            "ticker": ticker,
            "qty": qty_lots,
            "value_eur": round(value_eur, 2),
            "plusvalenza_eur": round(realized, 2),
            "imposta_eur": round(imposta, 2),
            "netto_eur": round(value_eur - imposta, 2),
        })
    return out


def execute_sale(
    *,
    broker: Broker,
    store: StateStore,
    data_dir,
    ticker: str,
    notional_eur: float | None,
    nota: str,
    now: datetime,
    fx_fetcher=None,
) -> dict:
    """Vende `notional_eur` (o tutto, se None) di `ticker`, con la stessa
    disciplina di un run: àncora, mercato, finestra, whitelist, max ordine,
    fill, riga fiscale LIFO, Giornale sigillato. Ritorna l'esito."""
    if not nota.strip():
        raise ValueError("La nota del capitano è obbligatoria per l'Approdo.")

    state = store.read()
    run_id = now.strftime("%Y%m%d")
    date_iso = now.date().isoformat()
    logbook = Logbook(f"{data_dir}/logbook/{run_id}.jsonl", run_id)
    logbook.event("approdo_start", ticker=ticker, nota=nota.strip())

    def _fail(reason: str) -> dict:
        logbook.event("approdo_rifiutato", motivo=reason)
        _seal(logbook, state, store)
        return {"ok": False, "motivo": reason}

    if state.anchor_down:
        return _fail("Àncora calata: nessun ordine, nemmeno di vendita.")
    if not broker.is_market_open():
        return _fail("Mercato chiuso: l'Approdo attende un giorno di borsa aperta.")
    window = gr.check_trading_window(now)
    if not window.approved:
        return _fail(window.reason)

    lots = state.tax_lots.get(ticker, [])
    qty_lots = sum(lot.qty for lot in lots)
    if qty_lots <= 1e-9:
        return _fail(f"Nessun lotto registrato per {ticker}: nulla da vendere.")

    positions = broker.get_positions()
    if ticker not in positions:
        return _fail(f"{ticker} non risulta tra le posizioni del broker.")

    if fx_fetcher is None:
        fx = get_eur_usd(date_iso, state)
    else:
        fx = fx_fetcher(date_iso, state)

    value_eur = fx.usd_to_eur(positions[ticker].market_value_usd)
    sell_eur = value_eur if notional_eur is None else min(notional_eur, value_eur)
    limit = gr.check_max_order(sell_eur)
    if not limit.approved:
        return _fail(limit.reason + " Vendi in più giorni, con calma.")

    cid = gr.client_order_id(run_id, ticker, OrderSide.SELL) + "-approdo"
    if state.order_processed(cid):
        return _fail(
            f"Approdo per {ticker} già eseguito e registrato oggi: un solo "
            "approdo per titolo al giorno, per non contare due volte."
        )
    fill = broker.submit_order(
        client_order_id=cid,
        ticker=ticker,
        side=OrderSide.SELL,
        notional_usd=fx.eur_to_usd(sell_eur),
    )
    logbook.event(
        "approdo_fill", ticker=ticker, stato=fill.status,
        qty=fill.filled_qty, prezzo_usd=fill.filled_avg_price_usd,
        client_order_id=cid,
    )

    pnl = 0.0
    if fill.is_filled:
        fiscal = FiscalLog(f"{data_dir}/fiscale.csv")
        pnl = fiscal.record(state=state, when=date_iso, fill=fill, fx=fx)
        state.mark_order(cid, date_iso)
        state.add_avviso(
            "Approdo eseguito",
            f"Venduto {ticker}: {fill.filled_qty:.6f} quote, plusvalenza "
            f"{pnl:.2f} EUR (LIFO). Nota: «{nota.strip()}»",
            date_iso,
        )
    logbook.event("approdo_esito", ticker=ticker, pnl_eur=round(pnl, 2))
    logbook.summary(
        f"Approdo — vendita {ticker}: stato {fill.status}, "
        f"plusvalenza realizzata {pnl:.2f} EUR. Nota: «{nota.strip()}»"
    )
    _seal(logbook, state, store)
    return {"ok": fill.is_filled, "stato": fill.status, "pnl_eur": round(pnl, 2)}


def _seal(logbook: Logbook, state: TimoneState, store: StateStore) -> None:
    prev = (state.last_seal or {}).get("hash", GENESIS)
    digest = logbook.seal(prev)
    state.last_seal = {"run_id": logbook.run_id, "hash": digest}
    store.write(state)


def check_soglia(state: TimoneState, value_eur: float, when: str) -> bool:
    """Soglia di rientro: SOLO avviso ("Approdo in vista"), mai vendita.

    Anti-ripetizione: un solo avviso finché il valore resta sopra soglia.
    Ritorna True se l'avviso è stato emesso ora."""
    soglia = state.soglia_approdo_eur
    if not soglia or soglia <= 0:
        return False
    if value_eur >= soglia:
        if not state.soglia_notificata:
            state.soglia_notificata = True
            state.add_avviso(
                "Approdo in vista",
                f"Il valore di bordo ({value_eur:.2f} EUR) ha raggiunto la "
                f"soglia che avevi dichiarato ({soglia:.2f} EUR). Timone non "
                "vende: la decisione è tua. `timone approdo` per l'anteprima.",
                when,
            )
            return True
        return False
    state.soglia_notificata = False
    return False
