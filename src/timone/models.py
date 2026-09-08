"""Shared domain models for Timone.

Kept in a single module to avoid circular imports between engine, strategy,
guardrails and the persistence layer. All monetary strategy reasoning happens
in EUR (the user's home currency); conversion to USD for Alpaca notional
orders happens at the broker boundary using a single per-run FX rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Target:
    """One line of the Rotta: a ticker and its target weight in percent."""

    ticker: str
    weight_pct: float


@dataclass(frozen=True)
class Rotta:
    """The user's strategy configuration. Never suggested by the code."""

    amount_per_run_eur: float
    rebalance_threshold_pct: float
    targets: tuple[Target, ...]

    def __post_init__(self) -> None:
        if not self.targets:
            raise ValueError("La Rotta deve contenere almeno un target.")
        if self.amount_per_run_eur <= 0:
            raise ValueError("amount_per_run_eur deve essere positivo.")
        if self.rebalance_threshold_pct < 0:
            raise ValueError("rebalance_threshold_pct non può essere negativo.")

        tickers = [t.ticker for t in self.targets]
        if len(tickers) != len(set(tickers)):
            raise ValueError("La Rotta contiene ticker duplicati.")
        for t in self.targets:
            if t.weight_pct < 0:
                raise ValueError(f"Peso negativo per {t.ticker}.")

        total = sum(t.weight_pct for t in self.targets)
        # Tolleranza minima per errori di arrotondamento nello YAML.
        if abs(total - 100.0) > 1e-6:
            raise ValueError(
                f"La somma dei pesi deve essere 100, trovato {total:.4f}."
            )

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(t.ticker for t in self.targets)

    def weight_of(self, ticker: str) -> float:
        for t in self.targets:
            if t.ticker == ticker:
                return t.weight_pct
        raise KeyError(ticker)


@dataclass(frozen=True)
class Position:
    """A current holding as reported by the broker (values in USD)."""

    ticker: str
    qty: float
    market_value_usd: float
    avg_entry_price_usd: float


@dataclass(frozen=True)
class ProposedOrder:
    """An order the strategy would like to place, expressed as a EUR notional."""

    ticker: str
    side: OrderSide
    notional_eur: float
    reason: str


@dataclass
class OrderDecision:
    """A proposed order after passing (or failing) the guardrails."""

    order: ProposedOrder
    approved: bool
    rule: str  # guardrail that decided the outcome
    reason: str


@dataclass(frozen=True)
class Fill:
    """The result of submitting an order to the broker."""

    ticker: str
    side: OrderSide
    client_order_id: str
    status: str  # e.g. "filled", "rejected", "pending"
    filled_qty: float = 0.0
    filled_avg_price_usd: float = 0.0

    @property
    def is_filled(self) -> bool:
        return self.status == "filled" and self.filled_qty > 0


@dataclass(frozen=True)
class FxRate:
    """EUR/USD rate for a given day. `estimated` marks a fallback value."""

    date: str  # ISO date YYYY-MM-DD
    eur_usd: float  # 1 EUR = eur_usd USD
    estimated: bool
    source: str

    def eur_to_usd(self, eur: float) -> float:
        return eur * self.eur_usd

    def usd_to_eur(self, usd: float) -> float:
        return usd / self.eur_usd


@dataclass
class RunReport:
    """In-memory summary of a run, used to build the logbook."""

    run_id: str
    started_at: str
    decisions: list[OrderDecision] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    halted_reason: str | None = None
