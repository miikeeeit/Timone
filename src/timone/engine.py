"""Engine — orchestrazione di un run.

Sequenza (paper trading):
  1. Àncora (primo controllo utile).
  2. Mercato aperto (calendario Alpaca).
  3. Finestra oraria consentita.
  4. Legge la Rotta, scarica posizioni, calcola il cambio del giorno.
  5. Calcola gli ordini (strategia deterministica, in EUR).
  6. Ogni ordine passa dai guardrail: un bocciato viene scartato e loggato.
  7. Esegue gli approvati, verifica il fill, aggiorna lo Stato.
  8. Scrive Giornale di bordo e log fiscale.

`dry_run=True` calcola e mostra gli ordini senza inviarli e senza mutare lo Stato.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import guardrails as gr
from . import route_control, safety
from .broker_alpaca import Broker
from .fiscal import FiscalLog, fetch_eur_usd, get_eur_usd
from .guardrails import ROME
from .logbook import GENESIS, Logbook, build_summary
from .models import OrderDecision, OrderSide, Rotta, RunReport
from .state import StateStore
from .strategy_dca import compute_orders


def _now_rome() -> datetime:
    return datetime.now(ROME)


class Engine:
    def __init__(
        self,
        *,
        broker: Broker,
        store: StateStore,
        rotta: Rotta,
        data_dir: str | Path,
        now_fn=_now_rome,
        fx_fetcher=fetch_eur_usd,
    ):
        self.broker = broker
        self.store = store
        self.rotta = rotta
        self.data_dir = Path(data_dir)
        self.now_fn = now_fn
        self.fx_fetcher = fx_fetcher

    def run(self, *, dry_run: bool = False) -> RunReport:
        now = self.now_fn()
        run_id = now.strftime("%Y%m%d")
        date_iso = now.date().isoformat()

        report = RunReport(run_id=run_id, started_at=now.isoformat())
        logbook = Logbook(self.data_dir / "logbook" / f"{run_id}.jsonl", run_id)
        logbook.event("run_start", dry_run=dry_run, ora_roma=now.isoformat())

        state = self.store.read()

        # --- Gate a livello di run (in dry-run si annotano ma non fermano) ---
        anchor = gr.check_anchor(state.anchor_down)
        logbook.event("guardrail", **_gr(anchor))
        if not anchor.approved:
            if dry_run:
                report.notes.append(f"[dry-run] {anchor.reason}")
            else:
                return self._halt(report, logbook, anchor.reason, state)

        # --- Rotta versionata: si cambia solo via quarantena ---
        rotta_block = route_control.check_rotta_allowed(state, self.rotta, date_iso)
        if rotta_block:
            logbook.event("rotta", bloccata=True, motivo=rotta_block)
            if not dry_run:
                state.add_avviso("Rotta non riconosciuta", rotta_block, date_iso)
                return self._halt(report, logbook, rotta_block, state)
            report.notes.append(f"[dry-run] {rotta_block}")

        if not dry_run:
            if not self.broker.is_market_open():
                reason = "Mercato chiuso (calendario Alpaca): nessun ordine."
                logbook.event("mercato", aperto=False)
                return self._halt(report, logbook, reason, state)

            window = gr.check_trading_window(now)
            logbook.event("guardrail", **_gr(window))
            if not window.approved:
                return self._halt(report, logbook, window.reason, state)
        else:
            window = gr.check_trading_window(now)
            if not window.approved:
                report.notes.append(f"[dry-run] {window.reason}")

        # --- Cambio del giorno (una sola volta) ---
        fx = get_eur_usd(date_iso, state, fetcher=self.fx_fetcher)
        logbook.event(
            "cambio", eur_usd=fx.eur_usd, stimato=fx.estimated, fonte=fx.source
        )
        if fx.estimated:
            report.notes.append(f"Cambio EUR/USD stimato ({fx.source}).")

        # --- Posizioni correnti -> valori EUR ---
        positions = self.broker.get_positions()

        # --- Sicurezza attiva: l'àncora cala da sola, se serve ---
        if not dry_run:
            reason = safety.evaluate(state, positions, fx, run_id, date_iso)
            if reason:
                logbook.event("ancora_automatica", motivo=reason)
                return self._halt(
                    report, logbook, f"Àncora auto-calata: {reason}", state
                )
            self.store.write(state)  # persiste eventuali avvisi/warn

        current_values_eur = {
            t: fx.usd_to_eur(positions[t].market_value_usd)
            for t in self.rotta.tickers
            if t in positions
        }
        logbook.event(
            "posizioni",
            valori_eur={t: round(v, 2) for t, v in current_values_eur.items()},
        )

        # --- Calcolo ordini (deterministico) ---
        orders = compute_orders(self.rotta, current_values_eur)
        logbook.event(
            "ordini_proposti",
            ordini=[
                {"ticker": o.ticker, "side": o.side.value, "eur": o.notional_eur}
                for o in orders
            ],
        )
        if not orders:
            report.notes.append("Nessun ordine necessario: portafoglio in linea.")

        # --- Guardrail per ordine + esecuzione ---
        spent_running = state.spent_on(date_iso)
        approved_count = 0
        fiscal = FiscalLog(self.data_dir / "fiscale.csv")

        limits = route_control.effective_limits(state)
        for order in orders:
            result = gr.evaluate_order(
                order,
                allowed_tickers=self.rotta.tickers,
                spent_today_eur=spent_running,
                approved_so_far=approved_count,
                limits=limits,
            )
            decision = OrderDecision(
                order=order,
                approved=result.approved,
                rule=result.rule,
                reason=result.reason,
            )
            report.decisions.append(decision)
            logbook.event(
                "ordine",
                ticker=order.ticker,
                side=order.side.value,
                eur=order.notional_eur,
                approvato=result.approved,
                regola=result.rule,
                motivo=result.reason,
            )
            if not result.approved:
                continue

            approved_count += 1
            if order.side is OrderSide.BUY:
                spent_running += order.notional_eur

            if dry_run:
                continue

            cid = gr.client_order_id(run_id, order.ticker, order.side)
            notional_usd = fx.eur_to_usd(order.notional_eur)
            fill = self.broker.submit_order(
                client_order_id=cid,
                ticker=order.ticker,
                side=order.side,
                notional_usd=notional_usd,
            )
            report.fills.append(fill)
            logbook.event(
                "fill",
                ticker=fill.ticker,
                side=fill.side.value,
                stato=fill.status,
                qty=fill.filled_qty,
                prezzo_usd=fill.filled_avg_price_usd,
                client_order_id=cid,
            )

            if fill.is_filled:
                if state.order_processed(cid):
                    # Run rieseguito nello stesso giorno: il broker ha
                    # deduplicato l'ordine, noi non lo contiamo due volte.
                    logbook.event(
                        "fiscale_saltato", ticker=fill.ticker,
                        motivo="ordine già registrato in un run precedente",
                        client_order_id=cid,
                    )
                    continue
                controvalore_eur = fx.usd_to_eur(
                    fill.filled_qty * fill.filled_avg_price_usd
                )
                if fill.side is OrderSide.BUY:
                    state.add_spend(date_iso, controvalore_eur)
                pnl = fiscal.record(state=state, when=date_iso, fill=fill, fx=fx)
                state.mark_order(cid, date_iso)
                logbook.event("fiscale", ticker=fill.ticker, pnl_eur=round(pnl, 2))

        # --- Soglia di rientro (Approdo): SOLO avviso, mai vendita ---
        if not dry_run:
            from .approdo import check_soglia

            value_eur = fx.usd_to_eur(
                sum(p.market_value_usd for p in positions.values())
            )
            if check_soglia(state, value_eur, date_iso):
                logbook.event("approdo_in_vista", valore_eur=round(value_eur, 2))
                report.notes.append(
                    "Approdo in vista: soglia raggiunta — nessuna vendita "
                    "automatica, la decisione è tua."
                )

        # --- Persistenza dello Stato e sigillo (mai in dry-run) ---
        summary = build_summary(report)
        logbook.summary(summary)
        if not dry_run:
            state.positions_snapshot = {
                t: {
                    "qty": p.qty,
                    "market_value_usd": p.market_value_usd,
                    "avg_entry_price_usd": p.avg_entry_price_usd,
                }
                for t, p in positions.items()
            }
            state.last_run = {
                "run_id": run_id,
                "ts": now.isoformat(),
                "ordini_eseguiti": len([f for f in report.fills if f.is_filled]),
            }
            if not fx.estimated:
                state.last_fx = {"date": date_iso, "eur_usd": fx.eur_usd}
            self._seal_and_save(logbook, state)
        return report

    def _halt(
        self,
        report: RunReport,
        logbook: Logbook,
        reason: str,
        state=None,
    ) -> RunReport:
        report.halted_reason = reason
        logbook.event("halt", motivo=reason)
        logbook.summary(build_summary(report))
        if state is not None:
            # Anche un run interrotto è un anello della catena.
            self._seal_and_save(logbook, state)
        return report

    def _seal_and_save(self, logbook: Logbook, state) -> None:
        prev = (state.last_seal or {}).get("hash", GENESIS)
        digest = logbook.seal(prev)
        state.last_seal = {"run_id": logbook.run_id, "hash": digest}
        self.store.write(state)


def _gr(result) -> dict:
    return {"regola": result.rule, "approvato": result.approved, "motivo": result.reason}
