"""Ponte con la PWA, via Firestore.

Due usi: l'Àncora azionabile da mobile e la pubblicazione protetta dei dati
che la console web legge (prima erano un file statico pubblico su Hosting).

La console web (autenticata) scrive un *intento* su Firestore; il motore, a
inizio run e heartbeat, lo legge e lo applica con `set_anchor()`. Il motore
resta l'unico che *applica*: la PWA esprime solo l'intenzione.

Il ponte è **opzionale**: senza service-account configurato (`FIREBASE_SERVICE_
ACCOUNT`), tutte le funzioni sono no-op e il motore lavora in locale come prima.

Perché è sicuro: l'unico comando trasportato è calare/rialzare l'Àncora.
Calarla può solo *fermare* il motore, mai generare un ordine. Rialzarla riprende
il DCA dentro i guardrail cablati nel codice. Nessun percorso verso un trade.

Documento Firestore: collezione `timone`, documento `ancora`:
    { azione: "cala"|"rialza", richiesto_il: <iso>, richiesto_da: <email>,
      motivo: <str|null>, applicato_il: <iso|null>, stato_motore: <str|null> }
Un comando è "nuovo" finché `applicato_il != richiesto_il`. Riapplicare lo
stesso stato dell'Àncora è idempotente, quindi il ponte è sicuro anche se la
conferma di scrittura fallisce.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from .config import Settings

_COLLECTION = "timone"
_DOC = "ancora"
_UI_DOC = "ui"


class RemoteStore(ABC):
    @abstractmethod
    def get_comando_ancora(self) -> dict | None: ...

    @abstractmethod
    def conferma_ancora(self, applicato_il: str, stato_motore: str) -> None: ...

    def publish_ui(self, payload: dict) -> None:  # pragma: no cover - interfaccia
        raise NotImplementedError

    def get_ui_generated_at(self) -> str | None:  # pragma: no cover - interfaccia
        raise NotImplementedError


class _FirestoreStore(RemoteStore):
    def __init__(self, db):
        self._db = db
        self._ref = db.collection(_COLLECTION).document(_DOC)

    def get_comando_ancora(self) -> dict | None:
        snap = self._ref.get()
        return snap.to_dict() if snap.exists else None

    def conferma_ancora(self, applicato_il: str, stato_motore: str) -> None:
        self._ref.set(
            {"applicato_il": applicato_il, "stato_motore": stato_motore},
            merge=True,
        )

    def publish_ui(self, payload: dict) -> None:
        self._db.collection(_COLLECTION).document(_UI_DOC).set(payload)

    def get_ui_generated_at(self) -> str | None:
        snap = self._db.collection(_COLLECTION).document(_UI_DOC).get()
        if not snap.exists:
            return None
        return (snap.to_dict() or {}).get("generated_at")


def firestore_store(settings: Settings) -> RemoteStore | None:
    """Crea il RemoteStore reale se il service-account è configurato, altrimenti
    None (ponte spento). L'import di firebase-admin è ritardato: è una
    dipendenza opzionale."""
    path = settings.firebase_service_account
    if not path or not Path(path).expanduser().exists():
        return None
    import firebase_admin
    from firebase_admin import credentials, firestore

    if not firebase_admin._apps:  # inizializza una sola volta
        firebase_admin.initialize_app(
            credentials.Certificate(str(Path(path).expanduser()))
        )
    return _FirestoreStore(firestore.client())


def publish_ui(
    settings: Settings, payload: dict, *, remote: RemoteStore | None = None
) -> bool:
    """Pubblica i dati della Bussola su Firestore, protetti dalle regole.

    Sostituisce la pubblicazione di `data/ui.json` come file statico su Hosting,
    che era leggibile da CHIUNQUE conoscesse l'URL: il login della PWA è un gate
    JavaScript, non protegge i file. Su Firestore invece le regole lasciano
    leggere solo il proprietario autenticato.

    Ritorna True se pubblicato, False se il ponte è spento (nessun service
    account): in quel caso resta solo il file locale, che non esce dal Mac.
    """
    remote = remote if remote is not None else firestore_store(settings)
    if remote is None:
        return False
    remote.publish_ui(payload)
    return True


def sync_ancora(
    settings: Settings,
    store,
    *,
    remote: RemoteStore | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Legge il comando pendente dell'Àncora e lo applica una volta.

    Ritorna {azione, stato} se ha applicato qualcosa, altrimenti None (nessun
    comando nuovo o ponte spento). Non solleva per problemi di configurazione
    del ponte: quelli tornano None. `remote` iniettabile per i test.
    """
    remote = remote if remote is not None else firestore_store(settings)
    if remote is None:
        return None

    cmd = remote.get_comando_ancora()
    if not cmd:
        return None

    richiesto = cmd.get("richiesto_il")
    if not richiesto or richiesto == cmd.get("applicato_il"):
        return None  # nessun comando nuovo

    azione = cmd.get("azione")
    if azione not in ("cala", "rialza"):
        return None

    down = azione == "cala"
    if down:
        da = cmd.get("richiesto_da") or "console"
        motivo = cmd.get("motivo")
        reason = f"Richiesta da mobile · {da}" + (f" · {motivo}" if motivo else "")
        store.set_anchor(True, reason=reason)
    else:
        store.set_anchor(False)

    remote.conferma_ancora(richiesto, "calata" if down else "alzata")
    return {"azione": azione, "stato": "calata" if down else "alzata"}
