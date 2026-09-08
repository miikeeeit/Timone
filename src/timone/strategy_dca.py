"""Strategia: DCA con ribilanciamento a bande.

Funzione pura e deterministica: stessi input -> stessi ordini. Nessuna chiamata
di rete, nessuno stato, nessuna casualità. Ragiona interamente in EUR: il motore
converte i valori delle posizioni da USD a EUR prima di chiamarla.

Due modalità, decise dallo scostamento dei pesi correnti dai target:

* **DCA (nessun asset fuori banda)** — l'importo del run viene distribuito
  SOLO in acquisto, indirizzandolo ai ticker sottopesati rispetto al target
  calcolato sul totale post-versamento. Nessuna vendita: si ribilancia con i
  nuovi versamenti, minimizzando i costi di transazione.

* **Ribilanciamento (almeno un asset oltre la soglia)** — si generano ordini
  (acquisti e vendite) per riportare ogni asset esattamente al target sul
  totale post-versamento.
"""

from __future__ import annotations

from .models import OrderSide, ProposedOrder, Rotta

#: Ordini sotto questa soglia vengono scartati (polvere; Alpaca ha un minimo).
MIN_ORDER_EUR: float = 1.0


def _current_weights(values_eur: dict[str, float], total: float) -> dict[str, float]:
    if total <= 0:
        return {t: 0.0 for t in values_eur}
    return {t: v / total * 100.0 for t, v in values_eur.items()}


def compute_orders(
    rotta: Rotta, current_values_eur: dict[str, float]
) -> list[ProposedOrder]:
    """Calcola gli ordini proposti (in EUR) per un run.

    `current_values_eur`: controvalore EUR corrente per ticker della Rotta
    (0 se non posseduto). Ticker fuori Rotta sono ignorati dalla strategia.
    """
    tickers = rotta.tickers
    current = {t: float(current_values_eur.get(t, 0.0)) for t in tickers}
    current_total = sum(current.values())
    amount = rotta.amount_per_run_eur
    new_total = current_total + amount

    target_value = {t: new_total * rotta.weight_of(t) / 100.0 for t in tickers}

    # Scostamento dei pesi CORRENTI (pre-versamento) dai target.
    weights_now = _current_weights(current, current_total)
    out_of_band = current_total > 0 and any(
        abs(weights_now[t] - rotta.weight_of(t)) > rotta.rebalance_threshold_pct
        for t in tickers
    )

    orders: list[ProposedOrder] = []

    if out_of_band:
        # Ribilanciamento completo verso i target sul totale post-versamento.
        for t in sorted(tickers):
            delta = target_value[t] - current[t]
            if abs(delta) < MIN_ORDER_EUR:
                continue
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            orders.append(
                ProposedOrder(
                    ticker=t,
                    side=side,
                    notional_eur=round(abs(delta), 2),
                    reason=(
                        f"Ribilanciamento: peso {weights_now[t]:.1f}% vs target "
                        f"{rotta.weight_of(t):.1f}% (soglia {rotta.rebalance_threshold_pct:.1f}pp)."
                    ),
                )
            )
        return orders

    # Modalità DCA: distribuisci l'importo del run sui soli sottopesati.
    shortfall = {t: max(0.0, target_value[t] - current[t]) for t in tickers}
    total_shortfall = sum(shortfall.values())
    if total_shortfall <= 0:
        return orders  # nessun sottopeso: niente da fare

    for t in sorted(tickers):
        if shortfall[t] <= 0:
            continue
        notional = amount * shortfall[t] / total_shortfall
        if notional < MIN_ORDER_EUR:
            continue
        orders.append(
            ProposedOrder(
                ticker=t,
                side=OrderSide.BUY,
                notional_eur=round(notional, 2),
                reason=(
                    f"DCA: versamento indirizzato al sottopeso "
                    f"(target {rotta.weight_of(t):.1f}%)."
                ),
            )
        )
    return orders
