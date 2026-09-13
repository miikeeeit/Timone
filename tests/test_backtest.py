"""Test del backtest — nucleo puro, dati sintetici, nessuna rete."""

import pytest

from timone import safety
from timone.backtest import GiornoMercato, simula
from timone.models import OrderSide, Rotta, Target


def rotta_singola(importo=100.0, soglia=5.0):
    return Rotta(
        amount_per_run_eur=importo,
        rebalance_threshold_pct=soglia,
        targets=(Target("AAA", 100.0),),
    )


def giorni(prezzi, fx=1.0, ticker="AAA"):
    return [
        GiornoMercato(f"2026-01-{i + 1:02d}", {ticker: p}, fx)
        for i, p in enumerate(prezzi)
    ]


def test_prezzo_costante_accumula_senza_risultato():
    r = simula(rotta_singola(), giorni([100.0, 100.0, 100.0]))
    assert r.giorni == 3
    assert r.ordini_eseguiti == 3
    assert r.versato_eur == pytest.approx(300.0)
    assert r.valore_finale_eur == pytest.approx(300.0)
    assert r.pnl_eur == pytest.approx(0.0, abs=1e-6)
    assert r.quote["AAA"] == pytest.approx(3.0)
    assert r.max_drawdown == pytest.approx(0.0)
    assert r.ancora_il is None


def test_crollo_fa_scattare_l_ancora_automatica():
    """Il backtest deve dire anche QUANDO il motore si sarebbe fermato."""
    r = simula(rotta_singola(), giorni([100.0, 40.0]))
    # dopo il crollo: versato 200, valore 140 -> drawdown 30%
    assert r.max_drawdown > safety.MAX_DRAWDOWN
    assert r.ancora_il == "2026-01-02"
    assert r.max_drawdown_il == "2026-01-02"


def test_guardrail_ferma_gli_ordini_troppo_grandi():
    r = simula(rotta_singola(importo=500.0), giorni([100.0, 100.0]))
    assert r.ordini_eseguiti == 0
    assert r.versato_eur == pytest.approx(0.0)
    assert r.ordini_scartati.get("max_order_eur") == 2


def test_il_backtest_non_vende_mai():
    """Coerente col motore: nessuno scenario produce una vendita."""
    from timone.strategy_dca import compute_orders

    r = Rotta(
        amount_per_run_eur=100.0, rebalance_threshold_pct=5.0,
        targets=(Target("AAA", 50.0), Target("BBB", 50.0)),
    )
    for valori in ({"AAA": 900.0, "BBB": 10.0}, {"AAA": 0.0, "BBB": 500.0}):
        for o in compute_orders(r, valori):
            assert o.side is OrderSide.BUY


def test_il_cambio_e_tenuto_separato_dai_titoli():
    """Prezzo invariato ma euro che si rafforza: la perdita è solo di cambio."""
    g = [
        GiornoMercato("2026-01-01", {"AAA": 100.0}, 1.0),
        GiornoMercato("2026-01-02", {"AAA": 100.0}, 1.25),
    ]
    r = simula(rotta_singola(), g)
    assert r.pnl_titoli_eur == pytest.approx(0.0, abs=1e-6), "i titoli non si sono mossi"
    assert r.pnl_cambio_eur < 0, "la perdita deve essere attribuita al cambio"


def test_e_deterministico():
    g = giorni([100.0, 90.0, 110.0, 105.0])
    a, b = simula(rotta_singola(), g), simula(rotta_singola(), g)
    assert (a.versato_eur, a.valore_finale_eur, a.quote) == (
        b.versato_eur, b.valore_finale_eur, b.quote
    )


def test_giorni_senza_dati_vengono_saltati():
    g = [
        GiornoMercato("2026-01-01", {"AAA": 100.0}, 1.0),
        GiornoMercato("2026-01-02", {}, 1.0),          # nessun prezzo
        GiornoMercato("2026-01-03", {"ZZZ": 50.0}, 1.0),  # ticker fuori Rotta
    ]
    r = simula(rotta_singola(), g)
    assert r.giorni == 1
