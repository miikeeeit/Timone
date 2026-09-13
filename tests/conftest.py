"""Fixtures condivise e broker fittizio per i test del motore."""

from datetime import datetime

import pytest

from timone.broker_alpaca import Broker
from timone.guardrails import ROME
from timone.models import Fill, Position, Rotta, Target


class FakeBroker(Broker):
    """Broker in-memory: nessuna rete. Idempotente sui client_order_id."""

    def __init__(self, positions=None, is_open=True, price=100.0, fill=True):
        self._positions = positions or {}
        self._is_open = is_open
        self.price = price
        self.fill = fill
        self.submitted: list[str] = []  # tutti i tentativi (con duplicati)
        self._by_cid: dict[str, Fill] = {}

    def is_market_open(self) -> bool:
        return self._is_open

    def get_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def submit_order(self, *, client_order_id, ticker, side, notional_usd) -> Fill:
        self.submitted.append(client_order_id)
        if client_order_id in self._by_cid:
            return self._by_cid[client_order_id]  # idempotenza: già inviato
        if self.fill:
            qty = notional_usd / self.price
            f = Fill(
                ticker, side, client_order_id, "filled",
                filled_qty=qty, filled_avg_price_usd=self.price,
            )
        else:
            f = Fill(ticker, side, client_order_id, "pending")
        self._by_cid[client_order_id] = f
        return f

    def get_fill(self, *, client_order_id, ticker, side) -> Fill:
        """Stato attuale di un ordine già inviato (per la riconciliazione)."""
        f = self._by_cid.get(client_order_id)
        if f is not None:
            return f
        return Fill(ticker, side, client_order_id, "pending")

    def concludi(self, client_order_id: str, *, qty: float, prezzo: float) -> None:
        """Test helper: il broker riempie un ordine rimasto in sospeso."""
        vecchio = self._by_cid[client_order_id]
        self._by_cid[client_order_id] = Fill(
            vecchio.ticker, vecchio.side, client_order_id, "filled",
            filled_qty=qty, filled_avg_price_usd=prezzo,
        )

    @property
    def unique_orders(self) -> int:
        return len(self._by_cid)


def position(ticker, value_usd, price=100.0):
    return Position(
        ticker=ticker,
        qty=value_usd / price,
        market_value_usd=value_usd,
        avg_entry_price_usd=price,
    )


@pytest.fixture
def rotta():
    return Rotta(
        amount_per_run_eur=100.0,
        rebalance_threshold_pct=5.0,
        targets=(Target("AAA", 50.0), Target("BBB", 30.0), Target("CCC", 20.0)),
    )


@pytest.fixture
def now_in_window():
    # Lunedì 6 luglio 2026, 16:00 ora di Roma -> dentro la finestra.
    return lambda: datetime(2026, 7, 6, 16, 0, tzinfo=ROME)


@pytest.fixture
def fx_one():
    # EUR/USD = 1.0: semplifica i test (EUR == USD).
    return lambda date_iso: 1.0
