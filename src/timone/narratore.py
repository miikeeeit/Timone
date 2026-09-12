"""Narratore — il diario settimanale, in italiano, che *spiega* cosa ha fatto
il motore. Non decide e non suggerisce nulla: è una lettura deterministica dei
dati già registrati (Giornale, stato, fiscale). Stessi dati → stesso racconto.

Le sezioni compaiono solo se pertinenti: una settimana tranquilla si racconta
in poche righe. Il silenzio *per assenza di scostamenti* è una buona notizia;
il silenzio *per run non partiti* no — e il Narratore distingue i due casi.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings

ROME = ZoneInfo("Europe/Rome")
_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
         "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
_MESI_BREVI = ["gen", "feb", "mar", "apr", "mag", "giu",
               "lug", "ago", "set", "ott", "nov", "dic"]


def _giorno_label(d: datetime) -> str:
    return f"{d.day} {_MESI_BREVI[d.month - 1]}"


def _runs_in_window(logbook_dir: Path, start: datetime, end: datetime) -> list[dict]:
    """Run REALI (non dry-run) con ts nella finestra [start, end].

    Un run si chiude al `run_start` successivo o a fine file: NON dipende dal
    sigillo (i run possono aver eseguito ordini senza essere ancora sigillati).
    """
    runs: list[dict] = []
    if not logbook_dir.exists():
        return runs

    def _flush(cur: dict | None) -> None:
        if not cur:
            return
        try:
            when = datetime.fromisoformat(cur["ts"]).astimezone(ROME)
        except (ValueError, TypeError):
            return
        if start <= when <= end:
            runs.append(cur)

    for f in sorted(logbook_dir.glob("*.jsonl")):
        cur: dict | None = None
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("kind")
            if kind == "run_start":
                _flush(cur)  # chiude il run precedente, se aperto
                if ev.get("dry_run"):
                    cur = None
                    continue
                cur = {"run_id": ev.get("run_id", f.stem),
                       "ts": ev.get("ora_roma") or ev.get("ts"),
                       "ordini": [], "cambio_stimato": False, "interrotto": None,
                       "ancora_giu": False, "completato": False}
                continue
            if cur is None:
                continue
            if kind == "cambio":
                cur["cambio_stimato"] = bool(ev.get("stimato"))
            elif kind == "ordine":
                cur["ordini"].append({
                    "ticker": ev.get("ticker"), "side": ev.get("side"),
                    "eur": float(ev.get("eur", 0)), "approvato": bool(ev.get("approvato")),
                    "regola": ev.get("regola"), "motivo": ev.get("motivo"),
                })
            elif kind == "guardrail" and ev.get("regola") == "ancora" and not ev.get("approvato"):
                cur["ancora_giu"] = True
            elif kind == "riassunto":
                cur["completato"] = True
                righe = ev.get("testo", "").split("\n")
                cur["interrotto"] = next(
                    (l for l in righe if "interrotto" in l.lower()), None
                )
        _flush(cur)  # ultimo run del file
    return runs


def weekly_report(settings: Settings, *, now: datetime | None = None) -> dict:
    """Costruisce il diario degli ultimi 7 giorni. Ritorna
    {periodo, titolo, paragrafi, attenzione}."""
    from .fiscal import annual_summary
    from .logbook import verify_chain
    from .state import JsonStateStore

    now = (now or datetime.now(ROME)).astimezone(ROME)
    start = now - timedelta(days=7)
    data_dir = Path(settings.data_dir)
    state = JsonStateStore(data_dir / "state.json").read()

    runs = _runs_in_window(data_dir / "logbook", start, now)
    periodo = f"{_giorno_label(start)} – {_giorno_label(now)} {now.year}"

    paragrafi: list[str] = []
    attenzione = False

    # --- 1. Attività della settimana ---
    completati = [r for r in runs if r["completato"] and not r["interrotto"]]
    interrotti = [r for r in runs if r["completato"] and r["interrotto"]]
    falliti = [r for r in runs if not r["completato"]]
    approvati = [o for r in completati for o in r["ordini"] if o["approvato"]]
    rifiutati = [o for r in completati for o in r["ordini"] if not o["approvato"]]

    def _spesa_finestra() -> float:
        tot = 0.0
        for giorno, eur in (state.daily_spend_eur or {}).items():
            try:
                d = datetime.strptime(giorno, "%Y-%m-%d").replace(tzinfo=ROME)
            except (ValueError, TypeError):
                continue
            if start.date() <= d.date() <= now.date():
                tot += eur
        return tot

    if completati:
        per_ticker: dict[str, float] = {}
        for o in approvati:
            if o["side"] == "buy":
                per_ticker[o["ticker"]] = per_ticker.get(o["ticker"], 0) + o["eur"]
        dest = max(per_ticker, key=per_ticker.get) if per_ticker else None
        n = len(completati)
        p = (f"Timone ha eseguito {n} run "
             f"{'regolare' if n == 1 else 'regolari'}, "
             f"investendo {_spesa_finestra():.0f} € complessivi")
        p += f", indirizzati soprattutto su {dest} (il più sottopeso)." if dest else "."
        paragrafi.append(p)
    elif falliti or interrotti:
        attenzione = True
        n = len(falliti) + len(interrotti)
        paragrafi.append(
            f"Timone ha avviato {n} run questa settimana, ma nessuno è arrivato a "
            "termine regolarmente (vedi le eccezioni qui sotto)."
        )
    else:
        attenzione = True
        p = "Questa settimana Timone non ha eseguito alcun run."
        if state.last_run:
            try:
                ult = datetime.strptime(state.last_run["run_id"], "%Y%m%d")
                giorni = (now.date() - ult.date()).days
                quando = f" ({giorni} giorni fa)" if giorni >= 0 else ""
                p += (f" L'ultimo run risale al {ult.day} "
                      f"{_MESI_BREVI[ult.month - 1]}{quando}.")
            except (ValueError, KeyError, TypeError):
                pass
        hb = [a for a in state.avvisi if "battito" in str(a.get("tipo", "")).lower()
              or "heartbeat" in str(a.get("tipo", "")).lower()]
        if hb:
            p += (" Il battito è mancato: verifica che il computer sia acceso"
                  " alle 16:00 nei giorni feriali.")
        paragrafi.append(p)

    # --- 2. Guardrail ed eccezioni ---
    ecc: list[str] = []
    if falliti:
        attenzione = True
        n = len(falliti)
        ecc.append(
            f"{n} run {'si è fermato' if n == 1 else 'si sono fermati'} a metà "
            "(il motore non è arrivato al riassunto): controlla la Diagnostica."
        )
    if interrotti:
        n = len(interrotti)
        ecc.append(
            f"{n} run {'si è interrotto' if n == 1 else 'si sono interrotti'} "
            "prima di operare (es. mercato chiuso): nessun ordine, com'è giusto."
        )
    if rifiutati:
        attenzione = True
        regole = sorted({o["regola"] for o in rifiutati})
        ecc.append(
            f"Un guardrail ha fermato {len(rifiutati)} "
            f"{'ordine' if len(rifiutati) == 1 else 'ordini'} "
            f"(regola: {', '.join(regole)}): non è stato ridimensionato, è stato scartato."
        )
    if state.anchor_down or any(r["ancora_giu"] for r in runs):
        attenzione = True
        motivo = state.anchor_reason or "vedi la sezione Sicurezza"
        ecc.append(f"L'Àncora è calata ({motivo}): il motore è fermo finché non la rialzi.")
    elif completati:
        ecc.append("L'Àncora è rimasta alzata per tutta la settimana.")
    if ecc:
        paragrafi.append(" ".join(ecc))

    # --- 3. Cambio ---
    stimati = [r for r in completati if r["cambio_stimato"]]
    if stimati:
        attenzione = True
        paragrafi.append(
            f"In {len(stimati)} run il cambio EUR/USD è stato stimato "
            "(fonte BCE non raggiungibile): le operazioni sono marcate nel report fiscale."
        )
    elif completati:
        paragrafi.append("Il cambio EUR/USD è stato letto dalla BCE a ogni run.")

    # --- 4. Integrità e fiscale ---
    ok, n_sigilli, _ = verify_chain(data_dir / "logbook")
    vendite = [o for o in approvati if o["side"] == "sell"]
    fisc = annual_summary(data_dir / "fiscale.csv", now.year)
    integ = (f"La catena del Giornale è integra ({n_sigilli} sigilli)."
             if ok else "Attenzione: la catena del Giornale non torna.")
    if not ok:
        attenzione = True
    if vendite:
        integ += (f" {len(vendite)} vendita/e: plusvalenze realizzate finora "
                  f"nel {now.year} pari a {fisc.get('plusvalenze_eur', 0):.2f} €.")
    else:
        integ += " Nessuna vendita: nessuna plusvalenza realizzata."
    paragrafi.append(integ)

    # --- 5. Chiusura ---
    if attenzione:
        paragrafi.append(
            "Le eccezioni qui sopra meritano un'occhiata. "
            "Timone segnala, non agisce al posto tuo."
        )
    else:
        paragrafi.append(
            "Nulla richiede la tua attenzione: il piano è stato mantenuto senza intoppi."
        )

    return {
        "periodo": periodo,
        "titolo": "Diario della settimana",
        "paragrafi": paragrafi,
        "attenzione": attenzione,
    }
