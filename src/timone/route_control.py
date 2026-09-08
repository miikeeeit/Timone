"""Disciplina delle modifiche: Rotta versionata, quarantena, cooling-off.

Principi:
* La Rotta si cambia SOLO passando dalla quarantena (dry-run per N giorni).
* I limiti nel codice (guardrails.py) sono il TETTO invalicabile: gli override
  possono solo restringerli. Restringere è immediato; ri-allargare (mai oltre
  il tetto) richiede 72 ore e una seconda conferma.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from . import guardrails as gr
from .models import Rotta
from .state import TimoneState

QUARANTINE_DAYS = 7
COOLING_HOURS = 72

#: Campi dei limiti soggetti a override (solo più restrittivi del tetto).
LIMIT_FIELDS = {
    "max_order_eur": lambda: gr.MAX_ORDER_EUR,
    "max_daily_eur": lambda: gr.MAX_DAILY_EUR,
    "max_orders_per_run": lambda: gr.MAX_ORDERS_PER_RUN,
}


def rotta_config(rotta: Rotta) -> dict:
    return {
        "amount_per_run_eur": rotta.amount_per_run_eur,
        "rebalance_threshold_pct": rotta.rebalance_threshold_pct,
        "targets": [
            {"ticker": t.ticker, "weight_pct": t.weight_pct}
            for t in rotta.targets
        ],
    }


def config_hash(config: dict) -> str:
    raw = json.dumps(config, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def active_version(state: TimoneState) -> dict | None:
    return state.rotta_versions[-1] if state.rotta_versions else None


def commit_version(state: TimoneState, config: dict, nota: str, when: str) -> dict:
    version = {
        "v": len(state.rotta_versions) + 1,
        "data": when,
        "config": config,
        "config_hash": config_hash(config),
        "nota": nota.strip(),
    }
    state.rotta_versions.append(version)
    return version


def check_rotta_allowed(state: TimoneState, rotta: Rotta, when: str) -> str | None:
    """La rotta su disco deve corrispondere alla versione attiva.

    Prima versione: si registra da sola (bootstrap del varo). Modifiche
    successive: solo via quarantena. Ritorna un motivo di blocco, o None.
    """
    cfg = rotta_config(rotta)
    h = config_hash(cfg)
    active = active_version(state)
    if active is None:
        commit_version(state, cfg, "v1 · varo (registrata automaticamente).", when)
        return None
    if h == active["config_hash"]:
        return None
    if state.quarantena and state.quarantena.get("config_hash") == h:
        return None  # candidata nota: la versione operativa resta al comando
    return (
        "rotta.yaml è cambiata fuori dal processo: nessuna quarantena attiva "
        f"per questa modifica. Ripristina la v{active['v']} oppure proponila "
        "con `timone rotta --proponi --nota \"...\"`."
    )


# --- Quarantena ---------------------------------------------------------------

def propose(state: TimoneState, rotta: Rotta, nota: str, now: datetime) -> dict:
    if not nota.strip():
        raise ValueError("La nota del capitano è obbligatoria per la quarantena.")
    cfg = rotta_config(rotta)
    h = config_hash(cfg)
    active = active_version(state)
    if active and h == active["config_hash"]:
        raise ValueError("La rotta su disco è identica alla versione operativa.")
    state.quarantena = {
        "candidata": cfg,
        "config_hash": h,
        "nota": nota.strip(),
        "inizio": now.date().isoformat(),
        "giorni": QUARANTINE_DAYS,
    }
    return state.quarantena


def quarantine_days_left(state: TimoneState, now: datetime) -> int | None:
    q = state.quarantena
    if not q:
        return None
    end = datetime.fromisoformat(q["inizio"]) + timedelta(days=q["giorni"])
    return max(0, (end.date() - now.date()).days)


def confirm_quarantine(state: TimoneState, now: datetime) -> dict:
    q = state.quarantena
    if not q:
        raise ValueError("Nessuna quarantena attiva.")
    left = quarantine_days_left(state, now)
    if left and left > 0:
        raise ValueError(
            f"La quarantena matura fra {left} giorni: la conferma non è ancora "
            "possibile. (Annullarla è sempre possibile.)"
        )
    version = commit_version(
        state, q["candidata"], q["nota"], now.date().isoformat()
    )
    state.quarantena = None
    return version


def cancel_quarantine(state: TimoneState) -> None:
    state.quarantena = None


# --- Cooling-off sui limiti -----------------------------------------------

def effective_limits(state: TimoneState) -> dict:
    """Limiti effettivi = min(tetto nel codice, override). Mai oltre il tetto."""
    out = {}
    for field, ceiling in LIMIT_FIELDS.items():
        cap = ceiling()
        ov = state.limiti_override.get(field)
        out[field] = min(cap, ov) if ov is not None else cap
    return out


def restrict_limit(state: TimoneState, field: str, value: float) -> None:
    """Restringere è immediato (e annulla una richiesta di allargamento)."""
    if field not in LIMIT_FIELDS:
        raise ValueError(f"Limite sconosciuto: {field}")
    current = effective_limits(state)[field]
    if value >= current:
        raise ValueError(
            f"{field}: {value} non restringe il limite attuale ({current}). "
            "Per allargare serve il cooling-off di 72 ore."
        )
    if value <= 0:
        raise ValueError("Un limite deve essere positivo.")
    state.limiti_override[field] = value
    state.cooling_request = None


def request_widening(
    state: TimoneState, field: str, value: float, now: datetime
) -> dict:
    """Chiede un limite più largo: matura in 72 ore, poi seconda conferma."""
    if field not in LIMIT_FIELDS:
        raise ValueError(f"Limite sconosciuto: {field}")
    cap = LIMIT_FIELDS[field]()
    current = effective_limits(state)[field]
    if value > cap:
        raise ValueError(
            f"{field}: {value} supera il tetto invalicabile nel codice ({cap})."
        )
    if value <= current:
        raise ValueError(
            f"{field}: {value} non allarga il limite attuale ({current}): "
            "puoi restringerlo subito senza attese."
        )
    state.cooling_request = {
        "campo": field,
        "da": current,
        "a": value,
        "richiesta_il": now.isoformat(timespec="seconds"),
    }
    return state.cooling_request


def cooling_hours_left(state: TimoneState, now: datetime) -> float | None:
    req = state.cooling_request
    if not req:
        return None
    end = datetime.fromisoformat(req["richiesta_il"]) + timedelta(
        hours=COOLING_HOURS
    )
    return max(0.0, (end - now).total_seconds() / 3600)


def confirm_widening(state: TimoneState, now: datetime) -> dict:
    req = state.cooling_request
    if not req:
        raise ValueError("Nessuna richiesta in maturazione.")
    left = cooling_hours_left(state, now)
    if left and left > 0:
        raise ValueError(
            f"La richiesta matura fra {left:.0f} ore: fino ad allora vale "
            f"{req['da']}."
        )
    field, value = req["campo"], req["a"]
    cap = LIMIT_FIELDS[field]()
    if value >= cap:
        state.limiti_override.pop(field, None)  # torna al tetto del codice
    else:
        state.limiti_override[field] = value
    state.cooling_request = None
    return {"campo": field, "valore": value}


def cancel_widening(state: TimoneState) -> None:
    state.cooling_request = None
