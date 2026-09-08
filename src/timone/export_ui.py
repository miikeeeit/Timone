"""Esporta i dati reali del motore per la Bussola (data/ui.json).

Il ponte più semplice possibile: un file JSON statico che la PWA carica se
presente (altrimenti mostra i dati d'esempio). La Bussola resta in sola
lettura: legge ciò che il motore ha fatto, non decide nulla.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import Settings, load_rotta
from .fiscal import annual_summary, get_eur_usd, today_iso
from .logbook import verify_chain
from .state import JsonStateStore

_GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
_MESI = ["gen", "feb", "mar", "apr", "mag", "giu",
         "lug", "ago", "set", "ott", "nov", "dic"]


def _label(run_id: str, ts: str | None) -> str:
    try:
        d = datetime.strptime(run_id, "%Y%m%d")
        ora = "16:00"
        if ts:
            try:
                from zoneinfo import ZoneInfo

                ora = (
                    datetime.fromisoformat(ts)
                    .astimezone(ZoneInfo("Europe/Rome"))
                    .strftime("%H:%M")
                )
            except ValueError:
                ora = ts[11:16]
        return f"{_GIORNI[d.weekday()]} {d.day} {_MESI[d.month - 1]} · {ora}"
    except ValueError:
        return run_id


def _short(h: str) -> str:
    return f"⌗ {h[:4]}…{h[-4:]}" if h else "⌗ ????"


def _parse_runs(logbook_dir: Path) -> list[dict]:
    """Estrae dai file JSONL un riassunto per ogni run sigillato (o meno)."""
    runs: list[dict] = []
    for f in sorted(logbook_dir.glob("*.jsonl"), reverse=True):
        pending: dict | None = None
        segments: list[dict] = []
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("kind")
            if (kind == "run_start" and not ev.get("dry_run")) or kind == "approdo_start":
                pending = {"run_id": ev.get("run_id", f.stem), "ts": ev.get("ts")}
            if pending is None:
                continue
            if kind == "riassunto":
                pending["riassunto"] = ev.get("testo", "")
            elif kind == "sigillo":
                pending["sigillo"] = (
                    f"{_short(ev.get('hash', ''))} ← {_short(ev.get('prev', ''))}"
                )
                segments.append(pending)
                pending = None
        segments.reverse()
        for seg in segments:
            testo = seg.get("riassunto", "")
            lines = [l for l in testo.split("\n") if l.strip()]
            titolo = lines[1] if len(lines) > 1 else (lines[0] if lines else "Run")
            runs.append({
                "run_id": seg["run_id"],
                "data": _label(seg["run_id"], seg.get("ts")),
                "titolo": titolo,
                "amber": "interrotto" in titolo.lower(),
                "riassunto": testo,
                "sigillo": seg.get("sigillo", ""),
            })
    return runs


def _fmt_cfg(cfg: dict) -> str:
    pesi = " · ".join(
        f"{t['ticker']} {t['weight_pct']:g}" for t in cfg["targets"]
    )
    return (
        f"{pesi} · {cfg['amount_per_run_eur']:g} €/giorno · soglia "
        f"{cfg['rebalance_threshold_pct']:g} pp"
    )


def _rotta_block(state) -> dict:
    from datetime import datetime as _dt

    from . import route_control as rc

    active = rc.active_version(state)
    q = state.quarantena
    return {
        "attiva": (
            {"v": active["v"], "data": active["data"], "descr": _fmt_cfg(active["config"]), "nota": active["nota"]}
            if active else None
        ),
        "versioni": [
            {"v": v["v"], "data": v["data"], "descr": _fmt_cfg(v["config"]), "nota": v["nota"]}
            for v in reversed(state.rotta_versions)
        ],
        "quarantena": (
            {
                "descr": _fmt_cfg(q["candidata"]),
                "nota": q["nota"],
                "inizio": q["inizio"],
                "giorni": q["giorni"],
                "giorni_residui": rc.quarantine_days_left(state, _dt.now()),
            }
            if q else None
        ),
    }


def _limiti_block(state) -> dict:
    from datetime import datetime as _dt

    from . import guardrails as gr
    from . import route_control as rc

    req = state.cooling_request
    return {
        "effettivi": rc.effective_limits(state),
        "tetti": {
            "max_order_eur": gr.MAX_ORDER_EUR,
            "max_daily_eur": gr.MAX_DAILY_EUR,
            "max_orders_per_run": gr.MAX_ORDERS_PER_RUN,
        },
        "finestra": "15:30 – 22:00",
        "cooling": (
            {
                "campo": req["campo"], "da": req["da"], "a": req["a"],
                "richiesta_il": req["richiesta_il"],
                "ore_residue": round(rc.cooling_hours_left(state, _dt.now()) or 0, 1),
            }
            if req else None
        ),
    }


def build_ui_json(settings: Settings) -> Path:
    data_dir = Path(settings.data_dir)
    store = JsonStateStore(data_dir / "state.json")
    state = store.read()

    # Posizioni: broker se raggiungibile, altrimenti ultimo snapshot noto.
    positions: dict[str, dict] = {}
    try:
        from .broker_alpaca import AlpacaBroker

        for t, p in AlpacaBroker(settings).get_positions().items():
            positions[t] = {
                "qty": p.qty,
                "market_value_usd": p.market_value_usd,
                "avg_entry_price_usd": p.avg_entry_price_usd,
            }
        fonte_posizioni = "broker"
    except Exception:  # noqa: BLE001
        positions = dict(state.positions_snapshot)
        fonte_posizioni = "snapshot"

    fx = get_eur_usd(today_iso(), state)

    try:
        rotta = load_rotta(settings.rotta_path)
        targets = {t.ticker: t.weight_pct for t in rotta.targets}
    except Exception:  # noqa: BLE001
        targets = {}

    value_usd = sum(p["market_value_usd"] for p in positions.values())
    value_eur = fx.usd_to_eur(value_usd) if value_usd else 0.0
    invested = sum(
        lot.cost_eur for lots in state.tax_lots.values() for lot in lots
    )
    cost_usd = sum(
        lot.qty * lot.price_usd
        for lots in state.tax_lots.values()
        for lot in lots
    )
    pnl = value_eur - invested
    pnl_titoli = fx.usd_to_eur(value_usd - cost_usd) if value_usd else 0.0
    pnl_cambio = pnl - pnl_titoli

    posizioni = []
    for t, p in sorted(positions.items()):
        v_eur = fx.usd_to_eur(p["market_value_usd"])
        peso = (v_eur / value_eur * 100.0) if value_eur > 0 else 0.0
        posizioni.append({
            "ticker": t,
            "qty": round(p["qty"], 6),
            "value_usd": round(p["market_value_usd"], 2),
            "value_eur": round(v_eur, 2),
            "peso": round(peso, 1),
            "target": targets.get(t),
        })

    ok, n_sigilli, msg = verify_chain(data_dir / "logbook")
    fisc = annual_summary(data_dir / "fiscale.csv", datetime.now().year)
    latente = pnl
    imposta_latente = max(0.0, latente) * 0.26

    ui = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fonte_posizioni": fonte_posizioni,
        "anchor_down": state.anchor_down,
        "anchor_reason": state.anchor_reason,
        "valore_eur": round(value_eur, 2),
        "versati_eur": round(sum(state.daily_spend_eur.values()), 2),
        "pnl_eur": round(pnl, 2),
        "pnl_titoli_eur": round(pnl_titoli, 2),
        "pnl_cambio_eur": round(pnl_cambio, 2),
        "cambio": {"rate": fx.eur_usd, "stimato": fx.estimated, "date": fx.date},
        "posizioni": posizioni,
        "runs": _parse_runs(data_dir / "logbook"),
        "catena": {"integra": ok, "sigilli": n_sigilli, "msg": msg},
        "last_seal": state.last_seal,
        "last_run": state.last_run,
        "fiscale": {
            **fisc,
            "latente_eur": round(latente, 2),
            "imposta_latente_eur": round(imposta_latente, 2),
            "netto_eur": round(value_eur - imposta_latente, 2),
        },
        "avvisi": state.avvisi,
        "rotta": _rotta_block(state),
        "limiti": _limiti_block(state),
    }

    out = data_dir / "ui.json"
    out.write_text(
        json.dumps(ui, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _refresh_symbols(settings, data_dir)
    return out


def _refresh_symbols(settings: Settings, data_dir: Path) -> None:
    """Anagrafica dei simboli negoziabili (verifica ticker nel Varo).

    TUTTI i simboli frazionabili, nessuna selezione: è un elenco, non un
    consiglio. Rinfrescata al massimo una volta a settimana, in silenzio."""
    import time

    out = data_dir / "simboli.json"
    if out.exists() and (time.time() - out.stat().st_mtime) < 7 * 86400:
        return
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        client = TradingClient(
            api_key=settings.api_key, secret_key=settings.api_secret, paper=True
        )
        assets = client.get_all_assets(
            GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
        )
        rows = [
            {"s": a.symbol, "n": (a.name or "")[:60]}
            for a in assets
            if a.tradable and a.fractionable
        ]
        rows.sort(key=lambda x: x["s"])
        out.write_text(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - l'anagrafica è un extra, mai bloccante
        pass
