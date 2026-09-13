"""timone doctor — controlli di salute del sistema.

Diagnostica di sola lettura: non tocca ordini né stato. Ogni controllo ritorna
un esito strutturato (ok / attenzione / errore) con un dettaglio leggibile.
Riusa ciò che già esiste (verifica della catena, fonte del cambio) e verifica
l'ambiente (endpoint paper, schedulatore, dati, Àncora).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import PAPER_BASE_URL, Settings

OK = "ok"
WARN = "attenzione"
ERR = "errore"


@dataclass(frozen=True)
class Check:
    id: str
    nome: str
    dettaglio: str
    esito: str  # OK | WARN | ERR

    @property
    def ok(self) -> bool:
        return self.esito == OK


def _endpoint(settings: Settings) -> Check:
    if settings.base_url.rstrip("/") == PAPER_BASE_URL:
        return Check("endpoint", "Endpoint broker",
                     "paper-api.alpaca.markets · mai live", OK)
    return Check("endpoint", "Endpoint broker",
                 f"endpoint non paper: {settings.base_url}", ERR)


def _chiavi(settings: Settings) -> Check:
    if not settings.api_key or not settings.api_secret:
        return Check("chiavi", "Chiavi API", "assenti · compila .env dal modello", ERR)
    try:
        from .broker_alpaca import AlpacaBroker

        AlpacaBroker(settings).is_market_open()
        return Check("chiavi", "Chiavi API", "valide · conto paper raggiungibile", OK)
    except Exception:  # noqa: BLE001 - la diagnostica non deve mai sollevare
        return Check("chiavi", "Chiavi API", "presenti · broker non raggiungibile ora", WARN)


def _cambio(settings: Settings, state) -> Check:
    try:
        from .fiscal import get_eur_usd, today_iso

        fx = get_eur_usd(today_iso(), state)
        if fx.estimated:
            return Check("cambio", "Fonte cambio BCE",
                         "non raggiungibile · uso l'ultimo tasso noto", WARN)
        return Check("cambio", "Fonte cambio BCE",
                     f"raggiungibile · EUR/USD {fx.eur_usd:.4f}", OK)
    except Exception:  # noqa: BLE001
        return Check("cambio", "Fonte cambio BCE", "non verificabile ora", WARN)


def _scheduler() -> Check:
    """Best-effort e specifico per host: prima launchd (assetto attuale),
    poi cron. Se non rileva nulla, avvisa senza fallire."""
    import subprocess

    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=5)
        if "com.timone.run" in out.stdout:
            return Check("scheduler", "Scheduler dei run",
                         "launchd attivo · com.timone.run caricato", OK)
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=5)
        if "timone" in out.stdout:
            return Check("scheduler", "Scheduler dei run", "cron attivo", OK)
    except Exception:  # noqa: BLE001
        pass
    return Check("scheduler", "Scheduler dei run",
                 "nessuno schedulatore rilevato · i run vanno lanciati a mano", WARN)


def _dati(data_dir: Path) -> Check:
    state_file = data_dir / "state.json"
    if not state_file.exists():
        return Check("dati", "Dati e stato", "state.json assente", ERR)
    kb = state_file.stat().st_size / 1024
    bak = (data_dir / "state.json.bak").exists()
    return Check("dati", "Dati e stato",
                 f"state {kb:.0f} KB · backup {'presente' if bak else 'assente'}",
                 OK if bak else WARN)


def _catena(data_dir: Path) -> Check:
    from .logbook import verify_chain

    ok, n, _msg = verify_chain(data_dir / "logbook")
    if ok:
        return Check("catena", "Integrità del Giornale",
                     f"catena verificata · {n} sigilli", OK)
    return Check("catena", "Integrità del Giornale", "la catena non torna", ERR)


def _bussola(settings: Settings) -> Check:
    """La console web sta ricevendo i dati?

    Il deploy poteva fallire in silenzio (credenziali Firebase scadute, script
    interrotto): la Bussola online restava ferma per settimane senza che nessuno
    se ne accorgesse. Qui si confronta la pubblicazione con l'export locale.
    """
    import json
    from pathlib import Path

    try:
        from .remote import firestore_store

        remote = firestore_store(settings)
    except Exception:  # noqa: BLE001
        return Check("bussola", "Bussola online", "non verificabile ora", WARN)
    if remote is None:
        return Check("bussola", "Bussola online",
                     "ponte spento: la console web non riceve dati", WARN)
    try:
        remoto = remote.get_ui_generated_at()
    except Exception:  # noqa: BLE001
        return Check("bussola", "Bussola online", "non raggiungibile ora", WARN)
    if not remoto:
        return Check("bussola", "Bussola online",
                     "mai pubblicata: la console web è vuota", ERR)

    locale = None
    f = Path(settings.data_dir) / "ui.json"
    if f.exists():
        try:
            locale = json.loads(f.read_text(encoding="utf-8")).get("generated_at")
        except Exception:  # noqa: BLE001
            locale = None
    quando = str(remoto)[:16].replace("T", " ")
    if locale and str(remoto) < str(locale):
        return Check("bussola", "Bussola online",
                     f"pubblicazione ferma al {quando}: il deploy non passa", WARN)
    return Check("bussola", "Bussola online", f"aggiornata · {quando}", OK)


def _ancora(state) -> Check:
    if state.anchor_down:
        return Check("ancora", "Àncora",
                     f"calata · {state.anchor_reason or 'motivo non registrato'}", WARN)
    return Check("ancora", "Àncora", "alzata · motore operativo", OK)


def run_checks(settings: Settings) -> list[Check]:
    from .state import JsonStateStore

    data_dir = Path(settings.data_dir)
    state = JsonStateStore(data_dir / "state.json").read()
    return [
        _endpoint(settings),
        _chiavi(settings),
        _cambio(settings, state),
        _scheduler(),
        _dati(data_dir),
        _catena(data_dir),
        _bussola(settings),
        _ancora(state),
    ]


def summarize(checks: list[Check]) -> dict:
    n_ok = sum(1 for c in checks if c.esito == OK)
    if any(c.esito == ERR for c in checks):
        stato = ERR
    elif any(c.esito == WARN for c in checks):
        stato = WARN
    else:
        stato = OK
    return {"stato": stato, "ok": n_ok, "totale": len(checks)}
