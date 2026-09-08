"""Guardrails — il cuore di Timone.

I limiti vivono QUI, nel codice, come costanti. Non nella config modificabile
dall'utente, non nella UI. Ogni ordine, prima di essere inviato, passa da queste
funzioni; ognuna ritorna un esito strutturato (approvato/rifiutato + motivo) che
finisce nel Giornale di bordo.

Un ordine bocciato non viene "aggiustato": viene scartato e loggato.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, time
from typing import Collection
from zoneinfo import ZoneInfo

from .models import OrderSide, ProposedOrder

# --- Costanti dei guardrail (NON configurabili) -----------------------------

#: Importo massimo per singolo ordine, in EUR (~1,2x l'importo per run tipico).
MAX_ORDER_EUR: float = 120.0

#: Spesa (acquisti) massima cumulata per giornata, in EUR. Persistita nello Stato.
MAX_DAILY_EUR: float = 200.0

#: Si opera SOLO su ticker presenti nella Rotta. Qualsiasi altro simbolo è rifiutato.
TICKER_WHITELIST_REQUIRED: bool = True

#: Tetto al numero di ordini per run (anti-loop; copre buy+sell su piccole rotte).
MAX_ORDERS_PER_RUN: int = 4

#: Finestra oraria consentita (ora italiana). Fuori finestra: nessun ordine.
TRADING_WINDOW_START: time = time(15, 30)
TRADING_WINDOW_END: time = time(22, 0)

ROME = ZoneInfo("Europe/Rome")

#: Prefisso deterministico per i client_order_id (idempotenza).
CLIENT_ORDER_PREFIX = "timone"


@dataclass(frozen=True)
class GuardrailResult:
    approved: bool
    rule: str
    reason: str

    @classmethod
    def ok(cls, rule: str, reason: str = "ok") -> "GuardrailResult":
        return cls(True, rule, reason)

    @classmethod
    def reject(cls, rule: str, reason: str) -> "GuardrailResult":
        return cls(False, rule, reason)


# --- Guardrail a livello di run ---------------------------------------------

def check_anchor(anchor_active: bool) -> GuardrailResult:
    """Àncora (kill switch). Se attiva, nessun ordine è consentito."""
    if anchor_active:
        return GuardrailResult.reject(
            "ancora", "Àncora calata: il run è bloccato, nessun ordine inviato."
        )
    return GuardrailResult.ok("ancora", "Àncora alzata.")


def check_trading_window(now: datetime) -> GuardrailResult:
    """Verifica che l'ora (italiana) sia nella finestra consentita."""
    if now.tzinfo is not None:
        now = now.astimezone(ROME)
    current = now.time()
    if TRADING_WINDOW_START <= current <= TRADING_WINDOW_END:
        return GuardrailResult.ok(
            "trading_window",
            f"Ora {current.strftime('%H:%M')} dentro la finestra.",
        )
    return GuardrailResult.reject(
        "trading_window",
        f"Ora {current.strftime('%H:%M')} fuori dalla finestra "
        f"{TRADING_WINDOW_START.strftime('%H:%M')}-{TRADING_WINDOW_END.strftime('%H:%M')}.",
    )


# --- Guardrail a livello di ordine ------------------------------------------

def check_ticker_whitelist(
    ticker: str, allowed: Collection[str]
) -> GuardrailResult:
    if TICKER_WHITELIST_REQUIRED and ticker not in allowed:
        return GuardrailResult.reject(
            "whitelist",
            f"Ticker {ticker} non è nella Rotta: rifiutato.",
        )
    return GuardrailResult.ok("whitelist", f"Ticker {ticker} ammesso.")


def check_max_order(
    notional_eur: float, limit: float | None = None
) -> GuardrailResult:
    cap = MAX_ORDER_EUR if limit is None else min(limit, MAX_ORDER_EUR)
    if abs(notional_eur) > cap:
        return GuardrailResult.reject(
            "max_order_eur",
            f"Ordine da {notional_eur:.2f} EUR oltre il massimo di "
            f"{cap:.2f} EUR.",
        )
    return GuardrailResult.ok(
        "max_order_eur", f"Ordine da {notional_eur:.2f} EUR entro il massimo."
    )


def check_daily_budget(
    prospective_spend_eur: float,
    spent_today_eur: float,
    limit: float | None = None,
) -> GuardrailResult:
    """`prospective_spend_eur` è la spesa (acquisti) che l'ordine aggiungerebbe.

    Per le vendite il chiamante passa 0: non consumano budget.
    """
    cap = MAX_DAILY_EUR if limit is None else min(limit, MAX_DAILY_EUR)
    total = spent_today_eur + prospective_spend_eur
    if total > cap + 1e-9:
        return GuardrailResult.reject(
            "daily_budget",
            f"Spesa giornaliera {total:.2f} EUR oltre il massimo di "
            f"{cap:.2f} EUR (già spesi {spent_today_eur:.2f}).",
        )
    return GuardrailResult.ok(
        "daily_budget",
        f"Spesa giornaliera {total:.2f} EUR entro il massimo.",
    )


def check_max_orders_per_run(
    approved_so_far: int, limit: int | None = None
) -> GuardrailResult:
    cap = MAX_ORDERS_PER_RUN if limit is None else min(limit, MAX_ORDERS_PER_RUN)
    if approved_so_far >= cap:
        return GuardrailResult.reject(
            "max_orders_per_run",
            f"Raggiunto il tetto di {cap} ordini per run.",
        )
    return GuardrailResult.ok(
        "max_orders_per_run",
        f"{approved_so_far}/{cap} ordini approvati finora.",
    )


def evaluate_order(
    order: ProposedOrder,
    *,
    allowed_tickers: Collection[str],
    spent_today_eur: float,
    approved_so_far: int,
    limits: dict | None = None,
) -> GuardrailResult:
    """Applica in sequenza i guardrail di ordine. Ritorna il PRIMO rifiuto,
    oppure un esito approvato se tutti passano. `limits` (opzionale) porta gli
    override del cooling-off: possono solo restringere, mai superare i tetti.
    """
    limits = limits or {}
    checks = [
        check_ticker_whitelist(order.ticker, allowed_tickers),
        check_max_orders_per_run(
            approved_so_far, limits.get("max_orders_per_run")
        ),
        check_max_order(order.notional_eur, limits.get("max_order_eur")),
        check_daily_budget(
            order.notional_eur if order.side is OrderSide.BUY else 0.0,
            spent_today_eur,
            limits.get("max_daily_eur"),
        ),
    ]
    for result in checks:
        if not result.approved:
            return result
    return GuardrailResult.ok("all", "Tutti i guardrail superati.")


# --- Idempotenza ------------------------------------------------------------

def client_order_id(run_id: str, ticker: str, side: OrderSide) -> str:
    """ID ordine deterministico da (run_id, ticker, side).

    Rieseguendo lo stesso run (stesso run_id) si ottengono gli stessi ID:
    Alpaca rifiuta i duplicati, quindi nessun ordine viene raddoppiato.
    """
    raw = f"{run_id}:{ticker}:{side.value}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{CLIENT_ORDER_PREFIX}-{run_id}-{ticker}-{side.value}-{digest}"
