"""Test dello StateStore (implementazione JSON)."""

from timone.state import JsonStateStore, TaxLot, TimoneState


def test_stato_vuoto_di_default(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    state = store.read()
    assert state.anchor_down is False
    assert state.last_run is None
    assert state.daily_spend_eur == {}


def test_round_trip_persistenza(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    state = store.read()
    state.anchor_down = True
    state.add_spend("2026-07-06", 120.0)
    state.lots_for("AAA").append(
        TaxLot(date="2026-07-06", qty=1.5, price_usd=100.0, eur_usd=1.1, cost_eur=136.36)
    )
    state.last_run = {"run_id": "20260706", "ts": "x"}
    store.write(state)

    reloaded = store.read()
    assert reloaded.anchor_down is True
    assert reloaded.spent_on("2026-07-06") == 120.0
    assert reloaded.last_run == {"run_id": "20260706", "ts": "x"}
    assert len(reloaded.lots_for("AAA")) == 1
    lot = reloaded.lots_for("AAA")[0]
    assert isinstance(lot, TaxLot)
    assert lot.qty == 1.5


def test_add_spend_accumula():
    state = TimoneState()
    state.add_spend("2026-07-06", 100.0)
    state.add_spend("2026-07-06", 50.0)
    assert state.spent_on("2026-07-06") == 150.0
    assert state.spent_on("2026-07-07") == 0.0


def test_set_anchor_scrive(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    assert store.is_anchor_down() is False
    store.set_anchor(True)
    assert store.is_anchor_down() is True
    store.set_anchor(False)
    assert store.is_anchor_down() is False


def test_scrittura_atomica_non_lascia_tmp(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.write(TimoneState(anchor_down=True))
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
