"""Test della catena hash del Giornale e della sicurezza attiva."""

from datetime import datetime

import pytest

from timone import safety
from timone.engine import Engine
from timone.guardrails import ROME
from timone.logbook import verify_chain
from timone.models import FxRate, Rotta, Target
from timone.state import JsonStateStore, TaxLot, TimoneState

from conftest import FakeBroker, position


def make_engine(tmp_path, rotta, now_fn, fx, broker=None):
    broker = broker or FakeBroker()
    store = JsonStateStore(tmp_path / "state.json")
    return Engine(
        broker=broker, store=store, rotta=rotta, data_dir=tmp_path,
        now_fn=now_fn, fx_fetcher=fx,
    ), broker, store


def day(d):
    return lambda: datetime(2026, 7, d, 16, 0, tzinfo=ROME)


# --- Catena ------------------------------------------------------------------

def test_catena_integra_su_piu_run(tmp_path, rotta, fx_one):
    # Broker "coerente": aggiorna le posizioni con i fill.
    class CoherentBroker(FakeBroker):
        def get_positions(self):
            pos = {}
            for f in self._by_cid.values():
                if f.is_filled:
                    prev = pos.get(f.ticker)
                    qty = (prev.qty if prev else 0) + f.filled_qty
                    pos[f.ticker] = position(f.ticker, qty * self.price, self.price)
            return pos

    broker = CoherentBroker()
    engine, broker, store = make_engine(tmp_path, rotta, day(6), fx_one, broker)
    engine.run(dry_run=False)
    engine.now_fn = day(7)
    engine.run(dry_run=False)

    ok, n, msg = verify_chain(tmp_path / "logbook")
    assert ok is True
    assert n == 2
    state = store.read()
    assert state.last_seal is not None
    assert state.last_seal["run_id"] == "20260707"


def test_catena_spezzata_da_manomissione(tmp_path, rotta, fx_one):
    engine, broker, store = make_engine(tmp_path, rotta, day(6), fx_one)
    engine.run(dry_run=False)

    f = tmp_path / "logbook" / "20260706.jsonl"
    f.write_text(f.read_text().replace("50.0", "51.0"), encoding="utf-8")

    ok, n, msg = verify_chain(tmp_path / "logbook")
    assert ok is False


def test_dry_run_non_sigilla(tmp_path, rotta, now_in_window, fx_one):
    engine, broker, store = make_engine(tmp_path, rotta, now_in_window, fx_one)
    engine.run(dry_run=True)
    assert store.read().last_seal is None


# --- Sicurezza attiva --------------------------------------------------------

def fx(estimated=False):
    return FxRate(date="2026-07-06", eur_usd=1.0, estimated=estimated, source="t")


def lots_state(qty=1.0, cost=100.0):
    s = TimoneState()
    s.tax_lots["AAA"] = [TaxLot("2026-07-01", qty, 100.0, 1.0, cost)]
    return s


def test_coerenza_primo_avvistamento_solo_avviso():
    s = lots_state()
    reason = safety.check_coherence(s, {}, "20260706", "2026-07-06")
    assert reason is None
    assert s.anchor_down is False
    assert s.coherence_warn == "20260706"
    assert s.avvisi


def test_coerenza_secondo_giorno_cala_ancora():
    s = lots_state()
    safety.check_coherence(s, {}, "20260706", "2026-07-06")
    reason = safety.check_coherence(s, {}, "20260707", "2026-07-07")
    assert reason is not None
    assert s.anchor_down is True


def test_coerenza_rientrata_azzera_warn():
    s = lots_state(qty=2.0)
    safety.check_coherence(s, {}, "20260706", "2026-07-06")
    pos = {"AAA": position("AAA", 200.0, 100.0)}  # qty 2.0: coerente
    reason = safety.check_coherence(s, pos, "20260707", "2026-07-07")
    assert reason is None
    assert s.coherence_warn is None
    assert s.anchor_down is False


def test_fx_stimato_prolungato_cala_ancora():
    s = TimoneState()
    for _ in range(safety.MAX_FX_ESTIMATED_RUNS - 1):
        assert safety.check_fx_streak(s, fx(estimated=True), "d") is None
    assert safety.check_fx_streak(s, fx(estimated=True), "d") is not None
    assert s.anchor_down is True


def test_fx_reale_azzera_streak():
    s = TimoneState()
    safety.check_fx_streak(s, fx(estimated=True), "d")
    safety.check_fx_streak(s, fx(estimated=False), "d")
    assert s.fx_estimated_streak == 0


def test_drawdown_oltre_soglia_cala_ancora():
    s = lots_state(qty=1.0, cost=100.0)
    pos = {"AAA": position("AAA", 80.0, 80.0)}  # -20% vs costo 100
    reason = safety.check_drawdown(s, pos, fx(), "d")
    assert reason is not None
    assert s.anchor_down is True


def test_drawdown_entro_soglia_non_scatta():
    s = lots_state(qty=1.0, cost=100.0)
    pos = {"AAA": position("AAA", 95.0, 95.0)}  # -5%
    assert safety.check_drawdown(s, pos, fx(), "d") is None


def test_perdita_solo_di_cambio_non_cala_l_ancora():
    """Titoli fermi, euro che si rafforza del 33%: in euro il calo supera la
    soglia, ma non è un motivo per smettere di comprare — anzi, ogni versamento
    ora compra più dollari. L'Àncora non deve scattare."""
    s = lots_state(qty=1.0, cost=100.0)          # costo 100 USD / 100 EUR
    pos = {"AAA": position("AAA", 100.0, 100.0)}  # titoli invariati
    cambio = FxRate(date="2026-07-06", eur_usd=1.33, estimated=False, source="t")
    assert safety.check_drawdown(s, pos, cambio, "d") is None
    assert s.anchor_down is False


def test_drawdown_titoli_scatta_anche_con_cambio_favorevole():
    """Il contrario: i titoli crollano ma il dollaro si rafforza, mascherando
    la perdita in euro. L'Àncora deve scattare lo stesso."""
    s = lots_state(qty=1.0, cost=100.0)
    pos = {"AAA": position("AAA", 75.0, 75.0)}    # −25% sui titoli
    cambio = FxRate(date="2026-07-06", eur_usd=0.8, estimated=False, source="t")
    reason = safety.check_drawdown(s, pos, cambio, "d")
    assert reason is not None and s.anchor_down is True


def test_il_motivo_riporta_titoli_e_totale_in_euro():
    s = lots_state(qty=1.0, cost=100.0)
    pos = {"AAA": position("AAA", 70.0, 70.0)}
    reason = safety.check_drawdown(s, pos, fx(), "d")
    assert "titoli" in reason and "USD" in reason and "euro" in reason
