"""Test del log fiscale: LIFO, cambio con fallback, scrittura CSV."""

import csv

import pytest

from timone.fiscal import FiscalLog, apply_sell_lifo, get_eur_usd
from timone.models import Fill, FxRate, OrderSide
from timone.state import TaxLot, TimoneState


# --- LIFO -------------------------------------------------------------------

def lot(qty, cost_eur, dt="2026-01-01"):
    # price_usd/eur_usd non rilevanti per il calcolo LIFO in EUR.
    return TaxLot(date=dt, qty=qty, price_usd=0.0, eur_usd=1.0, cost_eur=cost_eur)


def test_lifo_vende_dal_lotto_piu_recente():
    lots = [lot(10, 100.0, "2026-01-01"), lot(10, 200.0, "2026-02-01")]
    # Vendo 5 unità che ne valgono 150 EUR. LIFO -> consuma dal lotto 2 (costo 20/u).
    realized, leftover = apply_sell_lifo(lots, 5, 150.0)
    # costo base 5 * 20 = 100; ricavo 150 -> plus 50
    assert realized == pytest.approx(50.0)
    assert leftover == 0.0
    # lotto 2 ridotto a 5 unità / 100 EUR; lotto 1 intatto
    assert lots[-1].qty == pytest.approx(5)
    assert lots[-1].cost_eur == pytest.approx(100.0)
    assert lots[0].qty == pytest.approx(10)


def test_lifo_attraversa_piu_lotti():
    lots = [lot(10, 100.0), lot(5, 150.0)]  # 10@10eur, 5@30eur
    realized, leftover = apply_sell_lifo(lots, 8, 240.0)
    # LIFO: 5 dal lotto2 (costo 150) + 3 dal lotto1 (costo 3*10=30) = 180
    # ricavo 240 -> plus 60
    assert realized == pytest.approx(60.0)
    assert leftover == 0.0
    assert len(lots) == 1
    assert lots[0].qty == pytest.approx(7)


def test_lifo_perdita():
    lots = [lot(10, 200.0)]  # costo 20/u
    realized, _ = apply_sell_lifo(lots, 10, 150.0)
    assert realized == pytest.approx(-50.0)
    assert lots == []


def test_lifo_vendita_scoperta_segnala_leftover():
    lots = [lot(2, 40.0)]
    realized, leftover = apply_sell_lifo(lots, 5, 100.0)
    assert leftover == pytest.approx(3)
    assert lots == []


# --- Cambio / fallback ------------------------------------------------------

def test_fx_usa_fonte_quando_disponibile():
    state = TimoneState()
    fx = get_eur_usd("2026-07-06", state, fetcher=lambda d: 1.1)
    assert fx.estimated is False
    assert fx.eur_usd == 1.1


def test_fetch_eur_usd_manda_user_agent(monkeypatch):
    # Regressione: frankfurter risponde 403 senza User-Agent. La richiesta
    # deve sempre portare un header User-Agent.
    import urllib.request

    from timone import fiscal

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"rates":{"USD":1.2345}}'

    def fake_urlopen(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rate = fiscal.fetch_eur_usd("2026-07-06")
    assert rate == 1.2345
    assert captured["ua"] and "timone" in captured["ua"].lower()


def test_fx_fallback_su_ultimo_noto():
    state = TimoneState(last_fx={"date": "2026-07-01", "eur_usd": 1.05})

    def boom(_d):
        raise RuntimeError("rete giù")

    fx = get_eur_usd("2026-07-06", state, fetcher=boom)
    assert fx.estimated is True
    assert fx.eur_usd == 1.05


def test_fx_fallback_default_senza_storico():
    state = TimoneState()

    def boom(_d):
        raise RuntimeError("rete giù")

    fx = get_eur_usd("2026-07-06", state, fetcher=boom)
    assert fx.estimated is True
    assert fx.eur_usd == pytest.approx(1.08)


# --- Scrittura CSV end-to-end ----------------------------------------------

def test_record_buy_poi_sell_scrive_csv_e_pnl(tmp_path):
    state = TimoneState()
    flog = FiscalLog(tmp_path / "fiscale.csv")
    fx = FxRate(date="2026-07-06", eur_usd=1.0, estimated=False, source="test")

    buy = Fill("AAA", OrderSide.BUY, "cid1", "filled", filled_qty=10, filled_avg_price_usd=10.0)
    pnl_buy = flog.record(state=state, when="2026-07-06", fill=buy, fx=fx)
    assert pnl_buy == 0.0
    assert len(state.lots_for("AAA")) == 1

    sell = Fill("AAA", OrderSide.SELL, "cid2", "filled", filled_qty=4, filled_avg_price_usd=15.0)
    pnl_sell = flog.record(state=state, when="2026-07-07", fill=sell, fx=fx)
    # comprato 4@10eur=40, venduto 4@15eur=60 -> plus 20
    assert pnl_sell == pytest.approx(20.0)

    with (tmp_path / "fiscale.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert rows[0]["side"] == "buy"
    assert rows[1]["side"] == "sell"
    assert float(rows[1]["pnl_realizzato_eur"]) == pytest.approx(20.0)
    assert rows[0]["cambio_stimato"] == "no"
