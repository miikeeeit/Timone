"""Log fiscale — predisposto dal primo giorno, anche in paper trading.

Regime dichiarativo italiano (redditi diversi): plusvalenze al 26%, metodo
**LIFO** sui lotti. Ogni fill produce una riga in `fiscale.csv` (append-only)
con il controvalore in EUR al cambio EUR/USD del giorno (fonte BCE via
frankfurter.app), con fallback marcato come "stimato" se la fonte non risponde.

In paper trading serve a validare che i dati siano completi quando conterà.
"""

from __future__ import annotations

import csv
import json
import urllib.request
from datetime import date
from pathlib import Path

from . import __version__ as _UA_VERSION
from .models import Fill, FxRate, OrderSide
from .state import TaxLot, TimoneState

# Fonte cambio: BCE tramite frankfurter.app (rate ufficiali BCE).
FRANKFURTER_URL = "https://api.frankfurter.app/{date}?from=EUR&to=USD"

#: Fallback di ultima istanza se non c'è né rete né un cambio precedente noto.
DEFAULT_EUR_USD = 1.08

CSV_HEADER = [
    "data",
    "ticker",
    "side",
    "quantita",
    "prezzo_usd",
    "controvalore_usd",
    "cambio_eur_usd",
    "controvalore_eur",
    "pnl_realizzato_eur",
    "cambio_stimato",
]


# --- Cambio EUR/USD ---------------------------------------------------------

def fetch_eur_usd(date_iso: str, *, timeout: float = 8.0) -> float:
    """Scarica il cambio EUR/USD del giorno da frankfurter (BCE). Solleva
    un'eccezione in caso di errore: la gestione del fallback è del chiamante.
    """
    url = FRANKFURTER_URL.format(date=date_iso)
    # frankfurter rifiuta con 403 le richieste senza User-Agent: ne mandiamo uno.
    req = urllib.request.Request(url, headers={"User-Agent": f"timone/{_UA_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8"))
    return float(payload["rates"]["USD"])


def get_eur_usd(
    date_iso: str, state: TimoneState, *, fetcher=fetch_eur_usd
) -> FxRate:
    """Ritorna il cambio del giorno. Se la fonte non risponde, ripiega
    sull'ultimo cambio noto (o su DEFAULT_EUR_USD) marcandolo come stimato.
    """
    try:
        rate = fetcher(date_iso)
        return FxRate(date=date_iso, eur_usd=rate, estimated=False, source="BCE/frankfurter")
    except Exception:  # noqa: BLE001 - qualsiasi errore -> fallback
        last = state.last_fx
        if last and "eur_usd" in last:
            return FxRate(
                date=date_iso,
                eur_usd=float(last["eur_usd"]),
                estimated=True,
                source=f"stimato (ultimo noto del {last.get('date', '?')})",
            )
        return FxRate(
            date=date_iso,
            eur_usd=DEFAULT_EUR_USD,
            estimated=True,
            source="stimato (default)",
        )


# --- Calcolo LIFO (puro) ----------------------------------------------------

def apply_sell_lifo(
    lots: list[TaxLot], sell_qty: float, proceeds_eur: float
) -> tuple[float, float]:
    """Applica una vendita ai lotti col metodo LIFO, mutando `lots` in place.

    Ritorna (plusvalenza_realizzata_eur, quantità_non_coperta).
    La quantità non coperta è > 0 solo se si vende più di quanto tracciato.
    """
    remaining = sell_qty
    cost_basis_eur = 0.0
    while remaining > 1e-12 and lots:
        lot = lots[-1]
        take = min(remaining, lot.qty)
        cost_per_share = lot.cost_eur / lot.qty if lot.qty else 0.0
        cost_basis_eur += take * cost_per_share
        lot.qty -= take
        lot.cost_eur -= take * cost_per_share
        remaining -= take
        if lot.qty <= 1e-9:
            lots.pop()
    realized = proceeds_eur - cost_basis_eur
    return realized, remaining


# --- Scrittura CSV ----------------------------------------------------------

class FiscalLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _ensure_header(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", encoding="utf-8", newline="") as fh:
                csv.writer(fh).writerow(CSV_HEADER)

    def record(
        self, *, state: TimoneState, when: str, fill: Fill, fx: FxRate
    ) -> float:
        """Registra un fill: aggiorna i lotti fiscali e scrive la riga CSV.

        Ritorna la plusvalenza realizzata in EUR (0 per gli acquisti).
        """
        self._ensure_header()

        qty = fill.filled_qty
        price_usd = fill.filled_avg_price_usd
        controvalore_usd = qty * price_usd
        controvalore_eur = fx.usd_to_eur(controvalore_usd)

        lots = state.lots_for(fill.ticker)
        if fill.side is OrderSide.BUY:
            lots.append(
                TaxLot(
                    date=when,
                    qty=qty,
                    price_usd=price_usd,
                    eur_usd=fx.eur_usd,
                    cost_eur=controvalore_eur,
                )
            )
            pnl_eur = 0.0
        else:
            pnl_eur, _leftover = apply_sell_lifo(lots, qty, controvalore_eur)

        with self.path.open("a", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerow(
                [
                    when,
                    fill.ticker,
                    fill.side.value,
                    f"{qty:.6f}",
                    f"{price_usd:.4f}",
                    f"{controvalore_usd:.2f}",
                    f"{fx.eur_usd:.6f}",
                    f"{controvalore_eur:.2f}",
                    f"{pnl_eur:.2f}",
                    "sì" if fx.estimated else "no",
                ]
            )
        return pnl_eur


def today_iso() -> str:
    return date.today().isoformat()


def annual_summary(csv_path: str | Path, year: int) -> dict:
    """Riepilogo fiscale dell'anno da `fiscale.csv` (regime dichiarativo IT)."""
    csv_path = Path(csv_path)
    plus = minus = 0.0
    righe = stimati = 0
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if not row["data"].startswith(str(year)):
                    continue
                righe += 1
                if row.get("cambio_stimato") == "sì":
                    stimati += 1
                pnl = float(row.get("pnl_realizzato_eur") or 0)
                if pnl > 0:
                    plus += pnl
                elif pnl < 0:
                    minus += -pnl
    imponibile = max(0.0, plus - minus)
    return {
        "anno": year,
        "operazioni": righe,
        "plusvalenze_eur": round(plus, 2),
        "minusvalenze_eur": round(minus, 2),
        "imposta_26_eur": round(imponibile * 0.26, 2),
        "cambi_stimati": stimati,
    }
