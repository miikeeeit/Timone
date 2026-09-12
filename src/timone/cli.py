"""CLI di Timone.

Comandi:
  timone run              esegue un ciclo (invia ordini sul conto paper)
  timone dry-run          calcola e mostra gli ordini SENZA inviarli
  timone status           posizioni, ultimo run, stato Àncora
  timone ancora --drop    cala l'Àncora (blocca ogni ordine)
  timone ancora --raise   alza l'Àncora (sblocca)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_rotta, load_settings
from .logbook import build_summary
from .state import JsonStateStore


def _state_store(settings) -> JsonStateStore:
    return JsonStateStore(Path(settings.data_dir) / "state.json")


def _sync_ancora_mobile(settings, store) -> None:
    """Applica un eventuale comando dell'Àncora arrivato dalla PWA (Firestore).
    Best-effort: un ponte spento o irraggiungibile non deve mai fermare il run."""
    try:
        from . import remote

        esito = remote.sync_ancora(settings, store)
        if esito:
            print(f"Àncora {esito['stato']} da mobile (comando '{esito['azione']}').")
    except Exception as exc:  # noqa: BLE001 - il ponte è un extra, mai bloccante
        print(f"(ponte Àncora non disponibile: {exc})", file=sys.stderr)


def _active_rotta(settings, store):
    """La Rotta operativa: la versione attiva nello stato; il file su disco
    serve solo per il varo (v1) e per proporre modifiche (quarantena)."""
    from . import route_control
    from .models import Rotta, Target

    state = store.read()
    active = route_control.active_version(state)
    if active:
        cfg = active["config"]
        return Rotta(
            amount_per_run_eur=float(cfg["amount_per_run_eur"]),
            rebalance_threshold_pct=float(cfg["rebalance_threshold_pct"]),
            targets=tuple(
                Target(t["ticker"], float(t["weight_pct"]))
                for t in cfg["targets"]
            ),
        )
    return load_rotta(settings.rotta_path)


def _build_engine(settings):
    # Import ritardato: alpaca-py serve solo quando si tocca davvero il broker.
    from .broker_alpaca import AlpacaBroker
    from .engine import Engine

    store = _state_store(settings)
    rotta = _active_rotta(settings, store)
    broker = AlpacaBroker(settings)
    return Engine(broker=broker, store=store, rotta=rotta, data_dir=settings.data_dir)


def cmd_run(settings, args) -> int:
    store = _state_store(settings)
    _sync_ancora_mobile(settings, store)  # applica comandi Àncora da mobile
    try:
        engine = _build_engine(settings)
        report = engine.run(dry_run=False)
    except Exception as exc:  # noqa: BLE001 - conta i run falliti
        state = store.read()
        state.failed_runs += 1
        when = __import__("datetime").date.today().isoformat()
        state.add_avviso("Run fallito", f"{type(exc).__name__}: {exc}", when)
        if state.failed_runs >= 2 and not state.anchor_down:
            state.anchor_down = True
            state.anchor_reason = f"{state.failed_runs} run falliti consecutivi."
            state.add_avviso("Àncora auto-calata", state.anchor_reason, when)
        store.write(state)
        _notify("Timone — run fallito", str(exc)[:120])
        raise
    state = store.read()
    if state.failed_runs:
        state.failed_runs = 0
        store.write(state)
    print(build_summary(report))

    # Quarantena: la candidata gira in dry-run ombra, ogni giorno di prova.
    if state.quarantena:
        try:
            from datetime import datetime

            from . import route_control as rc
            from .broker_alpaca import AlpacaBroker
            from .engine import Engine
            from .models import Rotta, Target

            cfg = state.quarantena["candidata"]
            candidata = Rotta(
                amount_per_run_eur=float(cfg["amount_per_run_eur"]),
                rebalance_threshold_pct=float(cfg["rebalance_threshold_pct"]),
                targets=tuple(
                    Target(t["ticker"], float(t["weight_pct"]))
                    for t in cfg["targets"]
                ),
            )
            shadow = Engine(
                broker=AlpacaBroker(settings), store=store, rotta=candidata,
                data_dir=settings.data_dir,
            )
            rep = shadow.run(dry_run=True)
            left = rc.quarantine_days_left(state, datetime.now())
            print(f"\n[quarantena · attiva fra {left} giorni] La candidata avrebbe fatto:")
            print(build_summary(rep))
        except Exception as exc:  # noqa: BLE001 - la prova non ferma il reale
            print(f"(dry-run di quarantena non riuscito: {exc})")
    return 0


def _notify(title: str, text: str) -> None:
    """Notifica locale macOS (best effort): utile quando il run gira via cron."""
    try:
        import subprocess

        script = f'display notification "{text}" with title "{title}"'
        subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=5
        )
    except Exception:  # noqa: BLE001 - mai bloccare per una notifica
        pass


def cmd_verifica(settings, args) -> int:
    from .logbook import verify_chain

    ok, n, msg = verify_chain(Path(settings.data_dir) / "logbook")
    print(("Catena integra — " if ok else "LA CATENA NON TORNA — ") + msg)
    return 0 if ok else 1


def cmd_doctor(settings, args) -> int:
    """Controlli di salute del sistema (endpoint, chiavi, cambio, scheduler,
    dati, catena, Àncora). Diagnostica di sola lettura."""
    from .doctor import ERR, run_checks, summarize

    checks = run_checks(settings)
    s = summarize(checks)
    testa = {
        "ok": "Tutto in bolla",
        "attenzione": "Un'attenzione minore",
        "errore": "Un controllo è fallito",
    }[s["stato"]]
    simbolo = {"ok": "✓", "attenzione": "!", "errore": "✗"}
    print(f"== Timone — diagnostica ==  {testa} · {s['ok']}/{s['totale']} ok")
    for c in checks:
        print(f"  [{simbolo[c.esito]}] {c.nome}: {c.dettaglio}")
    return 1 if s["stato"] == ERR else 0


def cmd_narratore(settings, args) -> int:
    """Il diario settimanale in linguaggio naturale: racconta cosa ha fatto il
    motore, senza mai suggerire cosa fare."""
    from .narratore import weekly_report

    r = weekly_report(settings)
    print(f"== {r['titolo']} · {r['periodo']} ==\n")
    for p in r["paragrafi"]:
        print(p + "\n")
    return 0


def cmd_heartbeat(settings, args) -> int:
    """Controlla che il run di oggi sia avvenuto (da schedulare dopo le 16:00)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Europe/Rome"))
    if now.weekday() >= 5:
        print("Weekend: nessun run atteso.")
        return 0
    store = _state_store(settings)
    _sync_ancora_mobile(settings, store)  # applica comandi Àncora da mobile
    state = store.read()
    today_id = now.strftime("%Y%m%d")
    if state.last_seal and state.last_seal.get("run_id") == today_id:
        print(f"Battito regolare: run di oggi sigillato ({today_id}).")
        return 0
    msg = (
        f"Il run di oggi ({now.strftime('%H:%M')}) non risulta: cron saltato o "
        "computer spento. Il motore non ha dato battito."
    )
    state.add_avviso("Heartbeat mancato", msg, now.date().isoformat())
    store.write(state)
    _notify("Timone — battito mancato", msg)
    print("BATTITO MANCATO — " + msg)
    return 1


def cmd_fiscale(settings, args) -> int:
    from .fiscal import annual_summary

    year = __import__("datetime").date.today().year
    s = annual_summary(Path(settings.data_dir) / "fiscale.csv", year)
    print(f"== Fiscale {s['anno']} (regime dichiarativo · LIFO · cambio BCE) ==")
    print(f"Operazioni registrate: {s['operazioni']}")
    print(f"Plusvalenze realizzate: {s['plusvalenze_eur']:.2f} EUR")
    print(f"Minusvalenze realizzate: {s['minusvalenze_eur']:.2f} EUR")
    print(f"Imposta sostitutiva 26%: {s['imposta_26_eur']:.2f} EUR")
    if s["cambi_stimati"]:
        print(f"ATTENZIONE: {s['cambi_stimati']} operazioni con cambio stimato.")
    # Simulatore: "se vendessi ora" (serve il broker per i prezzi correnti).
    try:
        from .broker_alpaca import AlpacaBroker
        from .fiscal import get_eur_usd, today_iso

        store = _state_store(settings)
        state = store.read()
        positions = AlpacaBroker(settings).get_positions()
        fx = get_eur_usd(today_iso(), state)
        value_eur = fx.usd_to_eur(
            sum(p.market_value_usd for p in positions.values())
        )
        invested = sum(
            lot.cost_eur for lots in state.tax_lots.values() for lot in lots
        )
        latente = value_eur - invested
        imposta = max(0.0, latente) * 0.26
        print("-- Se vendessi ora (simulazione, non un invito) --")
        print(f"Plusvalenza latente: {latente:.2f} EUR")
        print(f"Imposta stimata 26%: {imposta:.2f} EUR")
        print(f"Netto in tasca: {value_eur - imposta:.2f} EUR")
    except Exception as exc:  # noqa: BLE001
        print(f"(Simulatore non disponibile: {exc})")
    return 0


def cmd_export_ui(settings, args) -> int:
    from .export_ui import build_ui_json

    out = build_ui_json(settings)
    print(f"Dati per la Bussola esportati in {out}")
    return 0


def cmd_approdo(settings, args) -> int:
    from datetime import datetime

    from . import approdo
    from .broker_alpaca import AlpacaBroker
    from .fiscal import get_eur_usd, today_iso

    store = _state_store(settings)
    state = store.read()

    if args.soglia is not None:
        state.soglia_approdo_eur = float(args.soglia)
        state.soglia_notificata = False
        store.write(state)
        print(
            f"Soglia di rientro fissata a {args.soglia:g} EUR. Al raggiungimento "
            "Timone AVVISA soltanto: vendere resta una tua decisione."
        )
        return 0
    if args.rimuovi_soglia:
        state.soglia_approdo_eur = None
        state.soglia_notificata = False
        store.write(state)
        print("Soglia di rientro rimossa.")
        return 0

    broker = AlpacaBroker(settings)
    positions = broker.get_positions()
    fx = get_eur_usd(today_iso(), state)

    if args.vendi:
        ticker = args.vendi.upper()
        if not args.conferma:
            rows = [r for r in approdo.preview(state, positions, fx) if r["ticker"] == ticker]
            if not rows:
                print(f"Nessun lotto registrato per {ticker}.")
                return 1
            r = rows[0]
            importo = args.importo if args.importo else r["value_eur"]
            print(f"== Anteprima Approdo · {ticker} (nessun ordine inviato) ==")
            print(f"Vendita: {importo:.2f} EUR su {r['value_eur']:.2f} EUR posseduti")
            print(f"Plusvalenza LIFO (vendita totale): {r['plusvalenza_eur']:.2f} EUR")
            print(f"Imposta 26%: {r['imposta_eur']:.2f} EUR · Netto: {r['netto_eur']:.2f} EUR")
            print("Per eseguire: aggiungi --conferma (e la --nota resta obbligatoria).")
            return 0
        esito = approdo.execute_sale(
            broker=broker, store=store, data_dir=settings.data_dir,
            ticker=ticker, notional_eur=args.importo, nota=args.nota or "",
            now=datetime.now(__import__("zoneinfo").ZoneInfo("Europe/Rome")),
        )
        if esito["ok"]:
            print(
                f"Approdo eseguito: {ticker}, plusvalenza {esito['pnl_eur']:.2f} EUR. "
                "Sigillato nel Giornale."
            )
            return 0
        print(f"Approdo NON eseguito: {esito.get('motivo', esito.get('stato'))}")
        return 1

    # Vista: anteprima per tutti i ticker + soglia.
    print("== Approdo · se vendessi ora (anteprima, non un invito) ==")
    for r in approdo.preview(state, positions, fx):
        print(
            f"  {r['ticker']}: valore {r['value_eur']:.2f} EUR · plusvalenza "
            f"{r['plusvalenza_eur']:.2f} · imposta {r['imposta_eur']:.2f} · "
            f"netto {r['netto_eur']:.2f}"
        )
    if state.soglia_approdo_eur:
        print(f"Soglia di rientro: {state.soglia_approdo_eur:g} EUR (solo avviso).")
    else:
        print("Nessuna soglia di rientro impostata (`--soglia EUR`).")
    return 0


def cmd_rotta(settings, args) -> int:
    from datetime import datetime

    from . import route_control as rc

    store = _state_store(settings)
    state = store.read()
    now = datetime.now()

    if args.proponi:
        disk = load_rotta(settings.rotta_path)
        q = rc.propose(state, disk, args.nota or "", now)
        store.write(state)
        print(
            f"Quarantena avviata — {q['giorni']} giorni di dry-run per la "
            "candidata. La versione operativa resta al comando."
        )
        return 0
    if args.conferma:
        v = rc.confirm_quarantine(state, now)
        state.add_avviso(
            "Rotta aggiornata",
            f"La v{v['v']} è operativa dopo la quarantena.",
            now.date().isoformat(),
        )
        store.write(state)
        print(f"Quarantena maturata: la v{v['v']} prende il mare dal prossimo run.")
        print("Ricorda di allineare rotta.yaml alla nuova versione (già fatto"
              " se la candidata è quella su disco).")
        return 0
    if args.annulla:
        rc.cancel_quarantine(state)
        store.write(state)
        print("Quarantena annullata — la candidata non prenderà il mare.")
        return 0

    # Vista: versione operativa + quarantena + storico.
    active = rc.active_version(state)
    print("== Rotta ==")
    if active:
        cfg = active["config"]
        pesi = " · ".join(
            f"{t['ticker']} {t['weight_pct']:g}" for t in cfg["targets"]
        )
        print(
            f"v{active['v']} · operativa dal {active['data']} — {pesi} · "
            f"{cfg['amount_per_run_eur']:g} €/run · soglia "
            f"{cfg['rebalance_threshold_pct']:g} pp"
        )
    else:
        print("Nessuna versione ancora: la v1 si registra al primo run.")
    if state.quarantena:
        left = rc.quarantine_days_left(state, now)
        stato = "maturata · pronta alla conferma" if left == 0 else f"attiva fra {left} giorni"
        print(f"In quarantena: candidata {state.quarantena['config_hash']} · {stato}")
        print(f"  nota: «{state.quarantena['nota']}»")
    print("-- Storico versioni --")
    for v in reversed(state.rotta_versions):
        print(f"  v{v['v']} · {v['data']} · «{v['nota']}»")
    return 0


def cmd_limiti(settings, args) -> int:
    from datetime import datetime

    from . import route_control as rc

    store = _state_store(settings)
    state = store.read()
    now = datetime.now()

    def _parse(kv: str):
        campo, _, val = kv.partition("=")
        return campo.strip(), float(val)

    if args.restringi:
        campo, val = _parse(args.restringi)
        rc.restrict_limit(state, campo, val)
        store.write(state)
        print(f"Limite ristretto subito: {campo} = {val:g}.")
        return 0
    if args.richiedi:
        campo, val = _parse(args.richiedi)
        req = rc.request_widening(state, campo, val, now)
        state.add_avviso(
            "Cooling-off in maturazione",
            f"{campo}: {req['da']:g} → {req['a']:g}. Seconda conferma fra 72 ore.",
            now.date().isoformat(),
        )
        store.write(state)
        print(
            f"Richiesta avviata: {campo} {req['da']:g} → {req['a']:g}. "
            "Matura in 72 ore, poi `timone limiti --conferma`."
        )
        return 0
    if args.conferma:
        out = rc.confirm_widening(state, now)
        store.write(state)
        print(f"Limite aggiornato: {out['campo']} = {out['valore']:g} dal prossimo run.")
        return 0
    if args.annulla:
        rc.cancel_widening(state)
        store.write(state)
        print("Richiesta annullata — restano i limiti attuali.")
        return 0

    print("== Limiti effettivi (tetto nel codice · mai superabile) ==")
    from . import guardrails as gr

    caps = {
        "max_order_eur": gr.MAX_ORDER_EUR,
        "max_daily_eur": gr.MAX_DAILY_EUR,
        "max_orders_per_run": gr.MAX_ORDERS_PER_RUN,
    }
    for campo, val in rc.effective_limits(state).items():
        print(f"  {campo}: {val:g}  (tetto {caps[campo]:g})")
    if state.cooling_request:
        left = rc.cooling_hours_left(state, now)
        req = state.cooling_request
        stato = "maturata · `timone limiti --conferma`" if left == 0 else f"matura fra {left:.0f} h"
        print(f"In maturazione: {req['campo']} {req['da']:g} → {req['a']:g} · {stato}")
    return 0


def cmd_dry_run(settings, args) -> int:
    engine = _build_engine(settings)
    report = engine.run(dry_run=True)
    print(build_summary(report))
    print("\n(dry-run: nessun ordine è stato inviato.)")
    return 0


def cmd_status(settings, args) -> int:
    store = _state_store(settings)
    state = store.read()

    print("== Timone — stato ==")
    print(f"Àncora: {'CALATA (bloccato)' if state.anchor_down else 'alzata (operativo)'}")
    if state.last_run:
        lr = state.last_run
        print(
            f"Ultimo run: {lr.get('run_id')} @ {lr.get('ts')} "
            f"(ordini eseguiti: {lr.get('ordini_eseguiti', 0)})"
        )
    else:
        print("Ultimo run: nessuno.")

    if state.daily_spend_eur:
        print("Spesa per giorno (EUR):")
        for day, eur in sorted(state.daily_spend_eur.items()):
            print(f"  {day}: {eur:.2f}")

    # Posizioni: prova a leggerle dal broker; se non disponibile, usa lo snapshot.
    try:
        from .broker_alpaca import AlpacaBroker

        positions = AlpacaBroker(settings).get_positions()
        print("Posizioni (dal broker):")
        if not positions:
            print("  nessuna.")
        for t, p in sorted(positions.items()):
            print(
                f"  {t}: qty {p.qty:.6f}, valore {p.market_value_usd:.2f} USD, "
                f"PMC {p.avg_entry_price_usd:.2f} USD"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"Posizioni dal broker non disponibili ({exc}).")
        if state.positions_snapshot:
            print("Ultimo snapshot noto:")
            for t, p in sorted(state.positions_snapshot.items()):
                print(f"  {t}: qty {p.get('qty')}, valore {p.get('market_value_usd')} USD")
    return 0


def cmd_ancora(settings, args) -> int:
    store = _state_store(settings)
    if getattr(args, "sync", False):
        esito = None
        try:
            from . import remote

            esito = remote.sync_ancora(settings, store)
        except Exception as exc:  # noqa: BLE001
            print(f"Ponte Àncora non disponibile: {exc}", file=sys.stderr)
            return 1
        print(
            f"Comando da mobile applicato: Àncora {esito['stato']}."
            if esito else "Nessun comando dell'Àncora in attesa dalla PWA."
        )
        return 0
    if args.drop:
        store.set_anchor(True)
        print("Àncora CALATA: ogni ordine è bloccato finché non la rialzi.")
    elif args.raise_:
        store.set_anchor(False)
        print("Àncora alzata: Timone torna operativo.")
    else:  # nessun flag: mostra lo stato
        print(f"Àncora: {'CALATA' if store.is_anchor_down() else 'alzata'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="timone",
        description="Timone — motore di esecuzione (Fase 1, paper trading).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="esegue un ciclo e invia gli ordini approvati")
    sub.add_parser("dry-run", help="calcola e mostra gli ordini senza inviarli")
    sub.add_parser("status", help="posizioni, ultimo run, stato Àncora")
    sub.add_parser("verifica", help="riverifica la catena hash del Giornale")
    sub.add_parser("doctor", help="controlli di salute del sistema (diagnostica)")
    sub.add_parser("narratore", help="diario settimanale in linguaggio naturale")
    sub.add_parser("heartbeat", help="controlla che il run di oggi sia avvenuto")
    sub.add_parser("fiscale", help="riepilogo fiscale annuale + simulatore")
    sub.add_parser("export-ui", help="esporta i dati reali per la Bussola (ui.json)")

    p_rotta = sub.add_parser("rotta", help="versioni della Rotta e quarantena")
    g_r = p_rotta.add_mutually_exclusive_group()
    g_r.add_argument("--proponi", action="store_true",
                     help="metti in quarantena la rotta.yaml modificata (7 giorni di dry-run)")
    g_r.add_argument("--conferma", action="store_true",
                     help="attiva la candidata (solo a quarantena maturata)")
    g_r.add_argument("--annulla", action="store_true", help="scarta la candidata")
    p_rotta.add_argument("--nota", help="nota del capitano (obbligatoria con --proponi)")

    p_app = sub.add_parser("approdo", help="anteprima/vendita disciplinata e soglia di rientro")
    p_app.add_argument("--vendi", metavar="TICKER", help="ticker da vendere")
    p_app.add_argument("--importo", type=float, help="controvalore EUR da vendere (default: tutto)")
    p_app.add_argument("--nota", help="nota del capitano (obbligatoria per eseguire)")
    p_app.add_argument("--conferma", action="store_true", help="esegue davvero la vendita")
    p_app.add_argument("--soglia", type=float, help="soglia di rientro in EUR (solo avviso)")
    p_app.add_argument("--rimuovi-soglia", action="store_true", help="rimuove la soglia")

    p_lim = sub.add_parser("limiti", help="limiti effettivi e cooling-off 72h")
    g_l = p_lim.add_mutually_exclusive_group()
    g_l.add_argument("--restringi", metavar="CAMPO=VALORE",
                     help="restringe subito un limite (es. max_order_eur=50)")
    g_l.add_argument("--richiedi", metavar="CAMPO=VALORE",
                     help="chiede un limite più largo (matura in 72 ore, mai oltre il tetto)")
    g_l.add_argument("--conferma", action="store_true",
                     help="seconda conferma a maturazione avvenuta")
    g_l.add_argument("--annulla", action="store_true", help="annulla la richiesta")

    p_anc = sub.add_parser("ancora", help="gestisce il kill switch")
    g = p_anc.add_mutually_exclusive_group()
    g.add_argument("--drop", action="store_true", help="cala l'Àncora (blocca)")
    g.add_argument(
        "--raise", dest="raise_", action="store_true", help="alza l'Àncora (sblocca)"
    )
    g.add_argument(
        "--sync", action="store_true",
        help="applica un comando dell'Àncora arrivato dalla PWA (Firestore)",
    )

    return parser


_DISPATCH = {
    "run": cmd_run,
    "dry-run": cmd_dry_run,
    "status": cmd_status,
    "ancora": cmd_ancora,
    "verifica": cmd_verifica,
    "doctor": cmd_doctor,
    "narratore": cmd_narratore,
    "heartbeat": cmd_heartbeat,
    "fiscale": cmd_fiscale,
    "export-ui": cmd_export_ui,
    "rotta": cmd_rotta,
    "limiti": cmd_limiti,
    "approdo": cmd_approdo,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        return _DISPATCH[args.command](settings, args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
