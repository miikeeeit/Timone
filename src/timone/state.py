"""StateStore — lo stato persistente di Timone.

Interfaccia unica e astratta (`StateStore`) su un singolo documento di stato
(`TimoneState`). In Fase 1 l'implementazione è su file JSON locale; l'interfaccia
è pensata per essere reimplementata su Firestore in Fase 2 (un documento = uno
stato) senza toccare il motore.

Contiene: flag Àncora, ultimo run, spesa giornaliera (per il budget), snapshot
posizioni, lotti fiscali (per il LIFO), ultimo cambio EUR/USD noto (fallback).
"""

from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class TaxLot:
    """Un lotto d'acquisto ancora aperto, per il calcolo LIFO."""

    date: str  # ISO date dell'acquisto
    qty: float
    price_usd: float
    eur_usd: float  # cambio del giorno d'acquisto
    cost_eur: float  # controvalore in EUR pagato per il lotto


@dataclass
class TimoneState:
    anchor_down: bool = False
    last_run: dict | None = None
    daily_spend_eur: dict[str, float] = field(default_factory=dict)
    positions_snapshot: dict[str, dict] = field(default_factory=dict)
    tax_lots: dict[str, list[TaxLot]] = field(default_factory=dict)
    last_fx: dict | None = None
    # Catena del Giornale: ultimo sigillo emesso {run_id, hash}.
    last_seal: dict | None = None
    # Sicurezza attiva.
    anchor_reason: str | None = None
    fx_estimated_streak: int = 0
    coherence_warn: str | None = None  # run_id del primo avvistamento
    failed_runs: int = 0
    avvisi: list = field(default_factory=list)
    # Rotta versionata: lista di {v, data, config, config_hash, nota}.
    rotta_versions: list = field(default_factory=list)
    # Quarantena: {candidata(config), config_hash, nota, inizio, giorni} | None.
    quarantena: dict | None = None
    # Cooling-off: override PIÙ RESTRITTIVI dei tetti nel codice, applicati subito.
    limiti_override: dict = field(default_factory=dict)
    # Richiesta di allargamento in maturazione: {campo, da, a, richiesta_il} | None.
    cooling_request: dict | None = None
    # Approdo: soglia di rientro (SOLO avviso, mai auto-vendita) e flag anti-ripetizione.
    soglia_approdo_eur: float | None = None
    soglia_notificata: bool = False
    # Ordini già registrati (client_order_id -> data): un fill si contabilizza
    # UNA volta sola, anche se il run viene rieseguito nello stesso giorno.
    processed_orders: dict = field(default_factory=dict)
    # Ordini inviati ma non ancora riempiti (client_order_id -> {ticker, side,
    # data}). Senza questo un ordine riempito in ritardo non verrebbe MAI
    # registrato: i lotti divergerebbero dal broker e la sicurezza attiva
    # calerebbe l'Àncora per "dati incoerenti", puntando nella direzione sbagliata.
    pending_orders: dict = field(default_factory=dict)

    def add_pending(self, cid: str, *, ticker: str, side: str, data: str) -> None:
        self.pending_orders[cid] = {"ticker": ticker, "side": side, "data": data}

    def remove_pending(self, cid: str) -> None:
        self.pending_orders.pop(cid, None)

    def order_processed(self, cid: str) -> bool:
        return cid in self.processed_orders

    def mark_order(self, cid: str, date_iso: str) -> None:
        self.processed_orders[cid] = date_iso
        if len(self.processed_orders) > 200:
            for k in sorted(self.processed_orders, key=self.processed_orders.get)[:100]:
                del self.processed_orders[k]

    def add_avviso(self, tipo: str, testo: str, quando: str) -> None:
        self.avvisi.insert(0, {"tipo": tipo, "testo": testo, "quando": quando})
        del self.avvisi[50:]  # tetto: niente archivi infiniti

    # --- helper di dominio ---

    def spent_on(self, date: str) -> float:
        return float(self.daily_spend_eur.get(date, 0.0))

    def add_spend(self, date: str, eur: float) -> None:
        self.daily_spend_eur[date] = self.spent_on(date) + eur

    def lots_for(self, ticker: str) -> list[TaxLot]:
        return self.tax_lots.setdefault(ticker, [])

    # --- (de)serializzazione ---

    def to_dict(self) -> dict:
        d = asdict(self)
        # asdict converte già i TaxLot in dict; nulla da fare.
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "TimoneState":
        tax_lots = {
            ticker: [TaxLot(**lot) for lot in lots]
            for ticker, lots in (raw.get("tax_lots") or {}).items()
        }
        return cls(
            anchor_down=bool(raw.get("anchor_down", False)),
            last_run=raw.get("last_run"),
            daily_spend_eur=dict(raw.get("daily_spend_eur") or {}),
            positions_snapshot=dict(raw.get("positions_snapshot") or {}),
            tax_lots=tax_lots,
            last_fx=raw.get("last_fx"),
            last_seal=raw.get("last_seal"),
            anchor_reason=raw.get("anchor_reason"),
            fx_estimated_streak=int(raw.get("fx_estimated_streak", 0)),
            coherence_warn=raw.get("coherence_warn"),
            failed_runs=int(raw.get("failed_runs", 0)),
            avvisi=list(raw.get("avvisi") or []),
            rotta_versions=list(raw.get("rotta_versions") or []),
            quarantena=raw.get("quarantena"),
            limiti_override=dict(raw.get("limiti_override") or {}),
            cooling_request=raw.get("cooling_request"),
            soglia_approdo_eur=raw.get("soglia_approdo_eur"),
            soglia_notificata=bool(raw.get("soglia_notificata", False)),
            processed_orders=dict(raw.get("processed_orders") or {}),
            pending_orders=dict(raw.get("pending_orders") or {}),
        )


class StateStore(ABC):
    """Interfaccia astratta. Fase 2: implementazione Firestore."""

    @abstractmethod
    def read(self) -> TimoneState:  # pragma: no cover - interfaccia
        ...

    @abstractmethod
    def write(self, state: TimoneState) -> None:  # pragma: no cover - interfaccia
        ...

    # --- comodità: operazioni atomiche di alto livello ---

    def is_anchor_down(self) -> bool:
        return self.read().anchor_down

    def set_anchor(self, down: bool, reason: str | None = None) -> None:
        state = self.read()
        state.anchor_down = down
        if not down:
            state.anchor_reason = None  # rialzare azzera il motivo
        elif reason is not None:
            state.anchor_reason = reason
        self.write(state)


class JsonStateStore(StateStore):
    """Implementazione su singolo file JSON locale, con scrittura atomica."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> TimoneState:
        if not self.path.exists():
            return TimoneState()
        with self.path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return TimoneState.from_dict(raw)

    def write(self, state: TimoneState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(state.to_dict(), indent=2, ensure_ascii=False)
        # Scrittura atomica: file temporaneo nella stessa cartella + rename.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
