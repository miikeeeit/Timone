"""Sicurezza attiva — l'àncora che cala da sola, se serve.

Regole deterministiche valutate ad ogni run reale. L'unica azione autonoma di
Timone è FERMARSI: calare l'àncora è sempre sicuro, agire da soli mai. Ogni
scatto produce un avviso persistito (solo eccezioni) e finisce nel Giornale.

Regole:
* Dati broker incoerenti — quantità del broker vs registro interno (lotti)
  oltre tolleranza per DUE run in giorni diversi (il primo avvistamento è solo
  un avviso: i ritardi di settlement del conto paper creano falsi positivi).
* Cambio stimato prolungato — la fonte BCE non risponde da N giorni di run.
* Drawdown oltre soglia — valore attuale sotto il costo investito oltre soglia.
"""

from __future__ import annotations

from .models import FxRate, Position
from .state import TimoneState

#: Tolleranza relativa tra qty broker e qty registro interno.
COHERENCE_TOLERANCE = 0.02
#: Run consecutivi (giorni diversi) con cambio stimato prima di calare l'àncora.
MAX_FX_ESTIMATED_RUNS = 5
#: Drawdown massimo tollerato (valore vs costo investito), in frazione.
MAX_DRAWDOWN = 0.15


def _drop_anchor(state: TimoneState, reason: str, when: str) -> str:
    state.anchor_down = True
    state.anchor_reason = reason
    state.add_avviso("Àncora auto-calata", reason, when)
    return reason


def check_coherence(
    state: TimoneState, positions: dict[str, Position], run_id: str, when: str
) -> str | None:
    """Confronta le quantità del broker col registro interno (lotti)."""
    mismatches = []
    for ticker, lots in state.tax_lots.items():
        qty_lots = sum(lot.qty for lot in lots)
        if qty_lots <= 1e-9:
            continue
        qty_broker = positions[ticker].qty if ticker in positions else 0.0
        if abs(qty_broker - qty_lots) / qty_lots > COHERENCE_TOLERANCE:
            mismatches.append(
                f"{ticker}: broker {qty_broker:.6f} vs registro {qty_lots:.6f}"
            )
    if not mismatches:
        state.coherence_warn = None
        return None

    detail = "Dati broker incoerenti · " + " · ".join(mismatches)
    if state.coherence_warn and state.coherence_warn != run_id:
        # Secondo avvistamento in un run di un giorno diverso: si cala.
        return _drop_anchor(state, detail, when)
    if not state.coherence_warn:
        state.coherence_warn = run_id
        state.add_avviso(
            "Verifica dati broker",
            detail + " · primo avvistamento: se persiste al prossimo run, "
            "l'àncora cala da sola.",
            when,
        )
    return None


def check_fx_streak(state: TimoneState, fx: FxRate, when: str) -> str | None:
    """Cambio stimato per troppi run consecutivi -> àncora."""
    if not fx.estimated:
        state.fx_estimated_streak = 0
        return None
    state.fx_estimated_streak += 1
    if state.fx_estimated_streak >= MAX_FX_ESTIMATED_RUNS:
        return _drop_anchor(
            state,
            f"Cambio EUR/USD stimato da {state.fx_estimated_streak} run: "
            "fonte BCE irraggiungibile troppo a lungo.",
            when,
        )
    return None


def check_drawdown(
    state: TimoneState, positions: dict[str, Position], fx: FxRate, when: str
) -> str | None:
    """Valore attuale sotto il costo investito oltre MAX_DRAWDOWN -> àncora."""
    invested = sum(
        lot.cost_eur for lots in state.tax_lots.values() for lot in lots
    )
    if invested <= 0 or not positions:
        return None
    value_eur = fx.usd_to_eur(
        sum(p.market_value_usd for p in positions.values())
    )
    drawdown = (invested - value_eur) / invested
    if drawdown > MAX_DRAWDOWN:
        return _drop_anchor(
            state,
            f"Drawdown {drawdown * 100:.1f}% oltre la soglia del "
            f"{MAX_DRAWDOWN * 100:.0f}% (valore {value_eur:.2f} EUR vs "
            f"investiti {invested:.2f} EUR).",
            when,
        )
    return None


def evaluate(
    state: TimoneState,
    positions: dict[str, Position],
    fx: FxRate,
    run_id: str,
    when: str,
) -> str | None:
    """Applica tutte le regole. Ritorna il motivo se l'àncora è stata calata."""
    for check in (
        lambda: check_coherence(state, positions, run_id, when),
        lambda: check_fx_streak(state, fx, when),
        lambda: check_drawdown(state, positions, fx, when),
    ):
        reason = check()
        if reason:
            return reason
    return None
