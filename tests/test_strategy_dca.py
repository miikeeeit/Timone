"""Test della strategia DCA + ribilanciamento (funzione pura)."""

import pytest

from timone.models import OrderSide, Rotta, Target
from timone.strategy_dca import compute_orders


def make_rotta(threshold=5.0, amount=100.0):
    return Rotta(
        amount_per_run_eur=amount,
        rebalance_threshold_pct=threshold,
        targets=(
            Target("AAA", 50.0),
            Target("BBB", 30.0),
            Target("CCC", 20.0),
        ),
    )


def as_map(orders):
    return {o.ticker: o for o in orders}


def test_conto_vuoto_compra_ai_pesi_target():
    rotta = make_rotta(amount=100.0)
    orders = compute_orders(rotta, {})
    m = as_map(orders)
    assert set(m) == {"AAA", "BBB", "CCC"}
    assert all(o.side is OrderSide.BUY for o in orders)
    assert m["AAA"].notional_eur == pytest.approx(50.0)
    assert m["BBB"].notional_eur == pytest.approx(30.0)
    assert m["CCC"].notional_eur == pytest.approx(20.0)
    # L'intero importo del run viene distribuito.
    assert sum(o.notional_eur for o in orders) == pytest.approx(100.0)


def test_dca_indirizza_ai_sottopesati_senza_vendere():
    # Portafoglio in banda (nessuno oltre soglia 5pp) ma leggermente sbilanciato.
    # Pesi correnti: AAA 50%, BBB 30%, CCC 20% -> esattamente a target.
    rotta = make_rotta(threshold=5.0, amount=100.0)
    current = {"AAA": 500.0, "BBB": 300.0, "CCC": 200.0}
    orders = compute_orders(rotta, current)
    assert all(o.side is OrderSide.BUY for o in orders)
    # new_total = 1100; target AAA=550 (+50), BBB=330(+30), CCC=220(+20)
    m = as_map(orders)
    assert m["AAA"].notional_eur == pytest.approx(50.0)
    assert m["BBB"].notional_eur == pytest.approx(30.0)
    assert m["CCC"].notional_eur == pytest.approx(20.0)


def test_dca_nessuna_vendita_quando_in_banda():
    # AAA leggermente sovrappeso ma entro soglia: nessuna vendita, solo buy sugli altri.
    rotta = make_rotta(threshold=10.0, amount=100.0)
    current = {"AAA": 560.0, "BBB": 280.0, "CCC": 160.0}  # totale 1000
    # pesi: 56/28/16; scostamenti +6/-2/-4, tutti entro 10pp -> DCA
    orders = compute_orders(rotta, current)
    assert all(o.side is OrderSide.BUY for o in orders)
    # AAA è sovrappeso rispetto al target sul nuovo totale? target su 1100 = 550.
    # current AAA 560 > 550 -> shortfall 0 -> nessun ordine su AAA.
    m = as_map(orders)
    assert "AAA" not in m
    assert sum(o.notional_eur for o in orders) == pytest.approx(100.0)


def test_fuori_banda_ribilancia_senza_mai_vendere():
    # AAA fortemente sovrappeso oltre soglia: il motore NON vende. Indirizza
    # tutto il versamento sui sottopesati e lo motiva come ribilanciamento.
    rotta = make_rotta(threshold=5.0, amount=100.0)
    current = {"AAA": 800.0, "BBB": 150.0, "CCC": 50.0}  # totale 1000
    # pesi 80/15/5 vs target 50/30/20 -> fuori banda
    orders = compute_orders(rotta, current)
    m = as_map(orders)
    # new_total 1100: target AAA 550 (già sopra -> nessun ordine),
    # BBB 330 (shortfall 180), CCC 220 (shortfall 170); totale shortfall 350.
    assert all(o.side is OrderSide.BUY for o in orders), "il motore non deve mai vendere"
    assert "AAA" not in m, "il sovrappeso non viene venduto, solo non alimentato"
    assert m["BBB"].notional_eur == pytest.approx(100.0 * 180 / 350, abs=0.01)
    assert m["CCC"].notional_eur == pytest.approx(100.0 * 170 / 350, abs=0.01)
    # si investe esattamente il versamento, mai di più
    assert sum(o.notional_eur for o in orders) == pytest.approx(100.0, abs=0.02)
    assert "senza vendite" in m["BBB"].reason


def test_nessun_ordine_di_vendita_in_nessuno_scenario():
    """Rete di sicurezza: qualunque configurazione, mai un SELL."""
    rotta = make_rotta(threshold=5.0, amount=100.0)
    scenari = [
        {"AAA": 800.0, "BBB": 150.0, "CCC": 50.0},   # AAA enorme sovrappeso
        {"AAA": 0.0, "BBB": 0.0, "CCC": 990.0},      # CCC quasi tutto
        {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0},     # portafoglio minuscolo
        {"AAA": 0.0, "BBB": 0.0, "CCC": 0.0},        # primo run
    ]
    for current in scenari:
        for o in compute_orders(rotta, current):
            assert o.side is OrderSide.BUY, f"vendita generata con {current}"


def test_scarta_ordini_polvere():
    # Importo minuscolo: gli ordini sotto MIN_ORDER_EUR non vengono generati.
    rotta = Rotta(
        amount_per_run_eur=1.0,
        rebalance_threshold_pct=5.0,
        targets=(Target("AAA", 50.0), Target("BBB", 50.0)),
    )
    orders = compute_orders(rotta, {"AAA": 500.0, "BBB": 500.0})
    # ogni buy sarebbe ~0.50 EUR < 1.0 -> scartati
    assert orders == []


def test_ordini_deterministici_e_ordinati():
    rotta = make_rotta()
    a = compute_orders(rotta, {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0})
    b = compute_orders(rotta, {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0})
    assert a == b
    assert [o.ticker for o in a] == sorted(o.ticker for o in a)
