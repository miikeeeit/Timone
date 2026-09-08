"""Broker adapter — l'unico punto di contatto con Alpaca.

`Broker` è l'interfaccia astratta usata dal motore (facilmente mockabile nei
test). `AlpacaBroker` è l'implementazione reale, agganciata SOLO all'ambiente
paper: `paper=True` è cablato nel codice, non configurabile.

Alpaca opera in USD: qui gli ordini arrivano già come notional in USD
(la conversione EUR->USD avviene a monte, nel motore, con un unico cambio per run).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from .config import PAPER_BASE_URL, Settings
from .models import Fill, OrderSide, Position


class Broker(ABC):
    @abstractmethod
    def is_market_open(self) -> bool: ...

    @abstractmethod
    def get_positions(self) -> dict[str, Position]: ...

    @abstractmethod
    def submit_order(
        self, *, client_order_id: str, ticker: str, side: OrderSide, notional_usd: float
    ) -> Fill: ...


class AlpacaBroker(Broker):
    """Implementazione paper di Alpaca via alpaca-py."""

    #: quante volte ricontrollare lo stato di un ordine prima di arrendersi
    FILL_POLL_ATTEMPTS = 10
    FILL_POLL_INTERVAL_S = 1.0

    def __init__(self, settings: Settings, *, _sleep=time.sleep):
        if settings.base_url.rstrip("/") != PAPER_BASE_URL:
            raise ValueError(
                f"AlpacaBroker consentito solo su {PAPER_BASE_URL} (paper)."
            )
        if not settings.api_key or not settings.api_secret:
            raise ValueError(
                "Chiavi Alpaca mancanti. Crea il file .env a partire dal modello "
                "(`cp .env.example .env`) e compila ALPACA_API_KEY e "
                "ALPACA_API_SECRET con le chiavi del conto paper."
            )
        # Import ritardato: alpaca-py non serve ai test che usano un fake broker.
        from alpaca.trading.client import TradingClient

        self._client = TradingClient(
            api_key=settings.api_key,
            secret_key=settings.api_secret,
            paper=True,  # cablato: nessun percorso verso il live
        )
        self._sleep = _sleep

    def is_market_open(self) -> bool:
        return bool(self._client.get_clock().is_open)

    def get_positions(self) -> dict[str, Position]:
        positions: dict[str, Position] = {}
        for p in self._client.get_all_positions():
            positions[p.symbol] = Position(
                ticker=p.symbol,
                qty=float(p.qty),
                market_value_usd=float(p.market_value),
                avg_entry_price_usd=float(p.avg_entry_price),
            )
        return positions

    def submit_order(
        self, *, client_order_id: str, ticker: str, side: OrderSide, notional_usd: float
    ) -> Fill:
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        alpaca_side = AlpacaSide.BUY if side is OrderSide.BUY else AlpacaSide.SELL
        request = MarketOrderRequest(
            symbol=ticker,
            notional=round(notional_usd, 2),
            side=alpaca_side,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )

        try:
            self._client.submit_order(request)
        except Exception as exc:  # noqa: BLE001 - vogliamo la rete di sicurezza
            # Idempotenza: un client_order_id duplicato significa che questo
            # ordine è già stato inviato in un run precedente. Non è un errore:
            # recuperiamo lo stato dell'ordine esistente.
            if "client_order_id" not in str(exc).lower():
                raise
        return self._await_fill(client_order_id, ticker, side)

    def _await_fill(self, client_order_id: str, ticker: str, side: OrderSide) -> Fill:
        last_status = "unknown"
        for attempt in range(self.FILL_POLL_ATTEMPTS):
            order = self._client.get_order_by_client_id(client_order_id)
            last_status = str(order.status).split(".")[-1].lower()
            filled_qty = float(order.filled_qty or 0)
            if last_status in {"filled"} and filled_qty > 0:
                return Fill(
                    ticker=ticker,
                    side=side,
                    client_order_id=client_order_id,
                    status="filled",
                    filled_qty=filled_qty,
                    filled_avg_price_usd=float(order.filled_avg_price or 0),
                )
            if last_status in {"rejected", "canceled", "expired"}:
                return Fill(
                    ticker=ticker,
                    side=side,
                    client_order_id=client_order_id,
                    status=last_status,
                )
            if attempt < self.FILL_POLL_ATTEMPTS - 1:
                self._sleep(self.FILL_POLL_INTERVAL_S)

        # Timeout: l'ordine è vivo ma non ancora eseguito. Lo segnaliamo come
        # pending; il prossimo run lo troverà tramite lo stesso client_order_id.
        return Fill(
            ticker=ticker,
            side=side,
            client_order_id=client_order_id,
            status="pending",
        )
