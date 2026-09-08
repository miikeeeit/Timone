"""Test di rotta versionata, quarantena e cooling-off."""

from datetime import datetime, timedelta

import pytest

from timone import guardrails as gr
from timone import route_control as rc
from timone.models import Rotta, Target
from timone.state import TimoneState


def rotta(amount=100.0, w=(50.0, 50.0)):
    return Rotta(
        amount_per_run_eur=amount,
        rebalance_threshold_pct=5.0,
        targets=(Target("AAA", w[0]), Target("BBB", w[1])),
    )


NOW = datetime(2026, 7, 15, 10, 0)


# --- Versioni -----------------------------------------------------------------

def test_bootstrap_v1_al_primo_controllo():
    s = TimoneState()
    assert rc.check_rotta_allowed(s, rotta(), "2026-07-15") is None
    assert len(s.rotta_versions) == 1
    assert s.rotta_versions[0]["v"] == 1


def test_rotta_identica_passa():
    s = TimoneState()
    rc.check_rotta_allowed(s, rotta(), "2026-07-15")
    assert rc.check_rotta_allowed(s, rotta(), "2026-07-16") is None
    assert len(s.rotta_versions) == 1


def test_rotta_cambiata_senza_quarantena_bloccata():
    s = TimoneState()
    rc.check_rotta_allowed(s, rotta(), "2026-07-15")
    block = rc.check_rotta_allowed(s, rotta(amount=150.0), "2026-07-16")
    assert block is not None and "quarantena" in block


def test_candidata_in_quarantena_non_blocca():
    s = TimoneState()
    rc.check_rotta_allowed(s, rotta(), "2026-07-15")
    rc.propose(s, rotta(amount=150.0), "provo 150", NOW)
    assert rc.check_rotta_allowed(s, rotta(amount=150.0), "2026-07-16") is None


# --- Quarantena ---------------------------------------------------------------

def test_proponi_richiede_nota():
    s = TimoneState()
    rc.check_rotta_allowed(s, rotta(), "2026-07-15")
    with pytest.raises(ValueError, match="nota"):
        rc.propose(s, rotta(amount=150.0), "  ", NOW)


def test_proponi_identica_rifiutata():
    s = TimoneState()
    rc.check_rotta_allowed(s, rotta(), "2026-07-15")
    with pytest.raises(ValueError, match="identica"):
        rc.propose(s, rotta(), "stessa", NOW)


def test_conferma_prima_della_maturazione_rifiutata():
    s = TimoneState()
    rc.check_rotta_allowed(s, rotta(), "2026-07-15")
    rc.propose(s, rotta(amount=150.0), "provo", NOW)
    with pytest.raises(ValueError, match="matura"):
        rc.confirm_quarantine(s, NOW + timedelta(days=3))


def test_conferma_a_maturazione_attiva_v2():
    s = TimoneState()
    rc.check_rotta_allowed(s, rotta(), "2026-07-15")
    rc.propose(s, rotta(amount=150.0), "provo 150", NOW)
    v = rc.confirm_quarantine(s, NOW + timedelta(days=rc.QUARANTINE_DAYS))
    assert v["v"] == 2
    assert s.quarantena is None
    assert rc.active_version(s)["config"]["amount_per_run_eur"] == 150.0


def test_annulla_quarantena():
    s = TimoneState()
    rc.check_rotta_allowed(s, rotta(), "2026-07-15")
    rc.propose(s, rotta(amount=150.0), "provo", NOW)
    rc.cancel_quarantine(s)
    assert s.quarantena is None
    assert len(s.rotta_versions) == 1


# --- Cooling-off ----------------------------------------------------------

def test_limiti_default_sono_i_tetti():
    s = TimoneState()
    lim = rc.effective_limits(s)
    assert lim["max_order_eur"] == gr.MAX_ORDER_EUR
    assert lim["max_daily_eur"] == gr.MAX_DAILY_EUR


def test_restringere_e_immediato():
    s = TimoneState()
    rc.restrict_limit(s, "max_order_eur", 50.0)
    assert rc.effective_limits(s)["max_order_eur"] == 50.0


def test_allargare_subito_e_vietato():
    s = TimoneState()
    rc.restrict_limit(s, "max_order_eur", 50.0)
    with pytest.raises(ValueError, match="cooling-off"):
        rc.restrict_limit(s, "max_order_eur", 80.0)


def test_oltre_il_tetto_mai():
    s = TimoneState()
    with pytest.raises(ValueError, match="tetto"):
        rc.request_widening(s, "max_order_eur", gr.MAX_ORDER_EUR + 1, NOW)


def test_allargamento_matura_in_72h():
    s = TimoneState()
    rc.restrict_limit(s, "max_order_eur", 50.0)
    rc.request_widening(s, "max_order_eur", 80.0, NOW)
    with pytest.raises(ValueError, match="matura"):
        rc.confirm_widening(s, NOW + timedelta(hours=10))
    out = rc.confirm_widening(s, NOW + timedelta(hours=rc.COOLING_HOURS))
    assert out["valore"] == 80.0
    assert rc.effective_limits(s)["max_order_eur"] == 80.0


def test_restringere_annulla_richiesta_pendente():
    s = TimoneState()
    rc.restrict_limit(s, "max_order_eur", 50.0)
    rc.request_widening(s, "max_order_eur", 80.0, NOW)
    rc.restrict_limit(s, "max_order_eur", 40.0)
    assert s.cooling_request is None


def test_guardrail_usa_override():
    from timone.models import OrderSide, ProposedOrder

    s = TimoneState()
    rc.restrict_limit(s, "max_order_eur", 30.0)
    order = ProposedOrder("AAA", OrderSide.BUY, 50.0, "test")
    res = gr.evaluate_order(
        order, allowed_tickers={"AAA"}, spent_today_eur=0.0,
        approved_so_far=0, limits=rc.effective_limits(s),
    )
    assert res.approved is False
    assert res.rule == "max_order_eur"
