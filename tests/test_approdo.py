"""Test dell'Approdo: anteprima LIFO, vendita disciplinata, soglia solo-avviso."""

from datetime import datetime

import pytest

from timone import approdo
from timone.guardrails import ROME
from timone.models import FxRate
from timone.state import JsonStateStore, TaxLot, TimoneState

from conftest import FakeBroker, position

FX = FxRate(date="2026-07-15", eur_usd=1.0, estimated=False, source="t")
NOW = datetime(2026, 7, 15, 16, 0, tzinfo=ROME)


def state_with_lots(qty=2.0, cost=100.0):
    s = TimoneState()
    s.tax_lots["AAA"] = [TaxLot("2026-07-01", qty, cost / qty, 1.0, cost)]
    return s


# --- Anteprima -----------------------------------------------------------------

def test_preview_calcola_lifo_imposta_netto():
    s = state_with_lots(qty=2.0, cost=100.0)
    pos = {"AAA": position("AAA", 150.0, 75.0)}  # vale 150, costo 100
    rows = approdo.preview(s, pos, FX)
    assert len(rows) == 1
    r = rows[0]
    assert r["plusvalenza_eur"] == pytest.approx(50.0)
    assert r["imposta_eur"] == pytest.approx(13.0)
    assert r["netto_eur"] == pytest.approx(137.0)
    # L'anteprima NON muta i lotti reali.
    assert sum(l.qty for l in s.tax_lots["AAA"]) == pytest.approx(2.0)


# --- Vendita -------------------------------------------------------------------

def make_env(tmp_path, state):
    store = JsonStateStore(tmp_path / "state.json")
    store.write(state)
    broker = FakeBroker(positions={"AAA": position("AAA", 150.0, 75.0)}, price=75.0)
    return store, broker


def test_vendita_richiede_nota(tmp_path):
    store, broker = make_env(tmp_path, state_with_lots())
    with pytest.raises(ValueError, match="nota"):
        approdo.execute_sale(
            broker=broker, store=store, data_dir=tmp_path, ticker="AAA",
            notional_eur=None, nota="  ", now=NOW,
        )


def test_vendita_eseguita_scrive_fiscale_e_sigilla(tmp_path):
    s = state_with_lots(qty=2.0, cost=80.0)
    store = JsonStateStore(tmp_path / "state.json")
    store.write(s)
    # Posizione da 100 EUR (entro MAX_ORDER_EUR=120), costo 80 -> plus 20.
    broker = FakeBroker(positions={"AAA": position("AAA", 100.0, 50.0)}, price=50.0)
    esito = approdo.execute_sale(
        broker=broker, store=store, data_dir=tmp_path, ticker="AAA",
        notional_eur=None, nota="obiettivo raggiunto", now=NOW,
        fx_fetcher=lambda d, s: FX,
    )
    assert esito["ok"] is True
    assert esito["pnl_eur"] == pytest.approx(20.0)
    state = store.read()
    assert state.last_seal is not None
    assert (tmp_path / "fiscale.csv").exists()
    assert any(a["tipo"] == "Approdo eseguito" for a in state.avvisi)


def test_vendita_bloccata_con_ancora_giu(tmp_path):
    s = state_with_lots()
    s.anchor_down = True
    store, broker = make_env(tmp_path, s)
    esito = approdo.execute_sale(
        broker=broker, store=store, data_dir=tmp_path, ticker="AAA",
        notional_eur=None, nota="x", now=NOW, fx_fetcher=lambda d, s: FX,
    )
    assert esito["ok"] is False
    assert "Àncora" in esito["motivo"]
    assert broker.submitted == []


def test_vendita_oltre_max_order_rifiutata(tmp_path):
    s = TimoneState()
    s.tax_lots["AAA"] = [TaxLot("2026-07-01", 10.0, 50.0, 1.0, 500.0)]
    store = JsonStateStore(tmp_path / "state.json")
    store.write(s)
    broker = FakeBroker(positions={"AAA": position("AAA", 500.0, 50.0)}, price=50.0)
    esito = approdo.execute_sale(
        broker=broker, store=store, data_dir=tmp_path, ticker="AAA",
        notional_eur=None, nota="tutto", now=NOW, fx_fetcher=lambda d, s: FX,
    )
    assert esito["ok"] is False
    assert "massimo" in esito["motivo"]


def test_vendita_fuori_finestra_rifiutata(tmp_path):
    store, broker = make_env(tmp_path, state_with_lots())
    esito = approdo.execute_sale(
        broker=broker, store=store, data_dir=tmp_path, ticker="AAA",
        notional_eur=None, nota="x",
        now=datetime(2026, 7, 15, 9, 0, tzinfo=ROME),
        fx_fetcher=lambda d, s: FX,
    )
    assert esito["ok"] is False
    assert broker.submitted == []


def test_secondo_approdo_stesso_giorno_rifiutato(tmp_path):
    s = state_with_lots(qty=2.0, cost=80.0)
    store = JsonStateStore(tmp_path / "state.json")
    store.write(s)
    broker = FakeBroker(positions={"AAA": position("AAA", 100.0, 50.0)}, price=50.0)
    kw = dict(broker=broker, store=store, data_dir=tmp_path, ticker="AAA",
              nota="x", now=NOW, fx_fetcher=lambda d, s: FX)
    esito1 = approdo.execute_sale(notional_eur=50.0, **kw)
    assert esito1["ok"] is True
    righe1 = (tmp_path / "fiscale.csv").read_text().count("\n")
    esito2 = approdo.execute_sale(notional_eur=50.0, **kw)
    assert esito2["ok"] is False
    assert "già eseguito" in esito2["motivo"]
    assert (tmp_path / "fiscale.csv").read_text().count("\n") == righe1


# --- Soglia (solo avviso) --------------------------------------------------

def test_soglia_avvisa_una_volta_sola():
    s = TimoneState()
    s.soglia_approdo_eur = 200.0
    assert approdo.check_soglia(s, 210.0, "2026-07-15") is True
    assert any(a["tipo"] == "Approdo in vista" for a in s.avvisi)
    assert approdo.check_soglia(s, 220.0, "2026-07-16") is False  # già notificata
    approdo.check_soglia(s, 150.0, "2026-07-17")  # rientra sotto
    assert approdo.check_soglia(s, 205.0, "2026-07-18") is True  # ri-avvisa


def test_soglia_assente_non_avvisa():
    s = TimoneState()
    assert approdo.check_soglia(s, 999.0, "2026-07-15") is False
    assert s.avvisi == []
