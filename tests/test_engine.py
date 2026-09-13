"""Test del motore — copre la Definition of Done sui guardrail."""

from datetime import datetime

import pytest

from timone.engine import Engine
from timone.guardrails import ROME
from timone.models import Rotta, Target
from timone.state import JsonStateStore

from conftest import FakeBroker, position


def make_engine(tmp_path, rotta, now_fn, fx, *, broker=None):
    broker = broker or FakeBroker()
    store = JsonStateStore(tmp_path / "state.json")
    return Engine(
        broker=broker,
        store=store,
        rotta=rotta,
        data_dir=tmp_path,
        now_fn=now_fn,
        fx_fetcher=fx,
    ), broker, store


# --- Dry-run ----------------------------------------------------------------

def test_dry_run_conto_vuoto_mostra_ordini(tmp_path, rotta, now_in_window, fx_one):
    engine, broker, store = make_engine(tmp_path, rotta, now_in_window, fx_one)
    report = engine.run(dry_run=True)

    assert report.halted_reason is None
    assert len(report.decisions) == 3
    assert all(d.approved for d in report.decisions)
    assert report.fills == []             # nessun invio in dry-run
    assert broker.submitted == []
    assert not (tmp_path / "state.json").exists()  # nessuna mutazione di stato


def test_dry_run_con_posizioni(tmp_path, rotta, now_in_window, fx_one):
    positions = {"AAA": position("AAA", 800.0), "BBB": position("BBB", 150.0),
                 "CCC": position("CCC", 50.0)}
    broker = FakeBroker(positions=positions)
    engine, broker, store = make_engine(tmp_path, rotta, now_in_window, fx_one, broker=broker)
    report = engine.run(dry_run=True)
    # AAA fortemente sovrappeso: il motore NON vende, si limita a non alimentarlo
    # e indirizza il versamento sui sottopesati.
    sides = {d.order.ticker: d.order.side.value for d in report.decisions}
    assert "AAA" not in sides, "il sovrappeso non va venduto dal motore"
    assert set(sides.values()) == {"buy"}
    assert broker.submitted == []


def test_run_fallito_lascia_traccia_sigillata_nel_giornale(
    tmp_path, rotta, now_in_window, fx_one
):
    """Un run interrotto da un errore non deve sparire dal Giornale: senza
    riassunto e sigillo resterebbe un frammento, e il registro mentirebbe."""
    import json

    class BrokerRotto(FakeBroker):
        def is_market_open(self):
            raise RuntimeError("broker irraggiungibile")

    engine, _broker, store = make_engine(
        tmp_path, rotta, now_in_window, fx_one, broker=BrokerRotto()
    )
    with pytest.raises(RuntimeError):
        engine.run(dry_run=False)

    righe = [
        json.loads(l)
        for l in (tmp_path / "logbook" / "20260706.jsonl").read_text().splitlines()
        if l.strip()
    ]
    kinds = [r.get("kind") for r in righe]
    assert "run_fallito" in kinds, "il motivo del guasto deve stare nel Giornale"
    assert "riassunto" in kinds
    assert "sigillo" in kinds, "anche un run fallito è un anello della catena"
    assert store.read().last_seal["run_id"] == "20260706"


def test_dry_run_non_scrive_nel_giornale(tmp_path, rotta, now_in_window, fx_one):
    """Un dry-run è una prova, non un run: non deve lasciare NULLA nel registro
    sigillato — nemmeno se fallisce."""
    engine, _broker, store = make_engine(tmp_path, rotta, now_in_window, fx_one)
    engine.run(dry_run=True)
    assert not (tmp_path / "logbook" / "20260706.jsonl").exists()
    assert store.read().last_seal is None

    class BrokerRotto(FakeBroker):
        def get_positions(self):
            raise RuntimeError("broker irraggiungibile")

    engine2, _b, store2 = make_engine(
        tmp_path, rotta, now_in_window, fx_one, broker=BrokerRotto()
    )
    with pytest.raises(RuntimeError):
        engine2.run(dry_run=True)
    assert not (tmp_path / "logbook" / "20260706.jsonl").exists()
    assert store2.read().last_seal is None


# --- Run reale ---------------------------------------------------------------

def test_run_esegue_e_persiste(tmp_path, rotta, now_in_window, fx_one):
    engine, broker, store = make_engine(tmp_path, rotta, now_in_window, fx_one)
    report = engine.run(dry_run=False)

    filled = [f for f in report.fills if f.is_filled]
    assert len(filled) == 3
    assert broker.unique_orders == 3

    state = store.read()
    assert state.last_run is not None
    assert state.last_run["run_id"] == "20260706"
    assert state.spent_on("2026-07-06") == pytest.approx(100.0)

    assert (tmp_path / "fiscale.csv").exists()
    assert (tmp_path / "logbook" / "20260706.jsonl").exists()


def test_run_duplicato_non_duplica_ordini(tmp_path, rotta, now_in_window, fx_one):
    engine, broker, store = make_engine(tmp_path, rotta, now_in_window, fx_one)
    engine.run(dry_run=False)
    first = broker.unique_orders
    s1 = store.read()
    spesa1 = s1.spent_on("2026-07-06")
    lotti1 = sum(len(l) for l in s1.tax_lots.values())

    engine.run(dry_run=False)  # stesso giorno -> stessi client_order_id
    assert broker.unique_orders == first       # nessun ordine nuovo
    assert broker.submitted.count(broker.submitted[0]) == 2  # ritentato ma deduplicato

    # Regressione: il fill deduplicato NON va contato due volte (spesa/lotti).
    s2 = store.read()
    assert s2.spent_on("2026-07-06") == pytest.approx(spesa1)
    assert sum(len(l) for l in s2.tax_lots.values()) == lotti1


# --- Àncora -----------------------------------------------------------------

def test_ancora_blocca_tutto(tmp_path, rotta, now_in_window, fx_one):
    engine, broker, store = make_engine(tmp_path, rotta, now_in_window, fx_one)
    store.set_anchor(True)
    report = engine.run(dry_run=False)
    assert report.halted_reason is not None
    assert broker.submitted == []


# --- Finestra oraria --------------------------------------------------------

def test_fuori_finestra_nessun_ordine(tmp_path, rotta, fx_one):
    now_fn = lambda: datetime(2026, 7, 6, 9, 0, tzinfo=ROME)  # 09:00, fuori finestra
    engine, broker, store = make_engine(tmp_path, rotta, now_fn, fx_one)
    report = engine.run(dry_run=False)
    assert report.halted_reason is not None
    assert broker.submitted == []


# --- Mercato chiuso ---------------------------------------------------------

def test_mercato_chiuso_nessun_ordine(tmp_path, rotta, now_in_window, fx_one):
    broker = FakeBroker(is_open=False)
    engine, broker, store = make_engine(tmp_path, rotta, now_in_window, fx_one, broker=broker)
    report = engine.run(dry_run=False)
    assert report.halted_reason is not None
    assert broker.submitted == []


# --- Guardrail di importo e budget ------------------------------------------

def test_ordine_oltre_max_rifiutato(tmp_path, now_in_window, fx_one):
    # Un unico ticker, importo run 1000 EUR -> ordine oltre MAX_ORDER_EUR.
    big = Rotta(amount_per_run_eur=1000.0, rebalance_threshold_pct=5.0,
                targets=(Target("AAA", 100.0),))
    engine, broker, store = make_engine(tmp_path, big, now_in_window, fx_one)
    report = engine.run(dry_run=True)
    assert len(report.decisions) == 1
    assert report.decisions[0].approved is False
    assert report.decisions[0].rule == "max_order_eur"


def test_budget_esaurito_blocca(tmp_path, rotta, now_in_window, fx_one):
    engine, broker, store = make_engine(tmp_path, rotta, now_in_window, fx_one)
    state = store.read()
    state.add_spend("2026-07-06", 500.0)  # budget giornaliero già saturo
    store.write(state)

    report = engine.run(dry_run=True)
    assert report.decisions  # ci sono ordini proposti
    assert all(not d.approved for d in report.decisions)
    assert all(d.rule == "daily_budget" for d in report.decisions)


# --- Riconciliazione degli ordini non riempiti --------------------------------

def test_ordine_pendente_viene_riconciliato_al_run_successivo(
    tmp_path, rotta, now_in_window, fx_one
):
    """Un ordine riempito in ritardo deve rientrare nei lotti fiscali.

    Senza riconciliazione resterebbe fuori per sempre (il client_order_id
    contiene la data, quindi domani è un ID diverso) e il divario con il broker
    finirebbe per far calare l'Àncora per "dati incoerenti".
    """
    broker = FakeBroker(fill=False)  # nessun ordine si riempie entro il polling
    engine, broker, store = make_engine(
        tmp_path, rotta, now_in_window, fx_one, broker=broker
    )
    engine.run(dry_run=False)

    s1 = store.read()
    assert len(s1.pending_orders) == 3, "gli ordini non riempiti vanno annotati"
    assert s1.tax_lots == {}
    assert s1.spent_on("2026-07-06") == 0.0

    # Più tardi il broker li riempie davvero.
    for cid in list(s1.pending_orders):
        broker.concludi(cid, qty=1.0, prezzo=50.0)

    # Run del giorno dopo: la riconciliazione li recupera.
    now_dopo = lambda: datetime(2026, 7, 7, 16, 0, tzinfo=ROME)
    engine2, _b, _s = make_engine(tmp_path, rotta, now_dopo, fx_one, broker=broker)
    engine2.run(dry_run=False)

    s2 = store.read()
    assert not [c for c in s2.pending_orders if c in s1.pending_orders]
    assert sum(len(l) for l in s2.tax_lots.values()) >= 3
    # Attribuiti al giorno dell'ORDINE (6 luglio), non a quello della scoperta.
    assert s2.spent_on("2026-07-06") == pytest.approx(150.0)
    date_lotti = {lot.date for lots in s2.tax_lots.values() for lot in lots}
    assert "2026-07-06" in date_lotti


def test_ordine_pendente_poi_rifiutato_non_viene_contabilizzato(
    tmp_path, rotta, now_in_window, fx_one
):
    from timone.models import Fill

    broker = FakeBroker(fill=False)
    engine, broker, store = make_engine(
        tmp_path, rotta, now_in_window, fx_one, broker=broker
    )
    engine.run(dry_run=False)
    pendenti = list(store.read().pending_orders)
    assert pendenti

    # Il broker li scarta.
    for cid in pendenti:
        vecchio = broker._by_cid[cid]
        broker._by_cid[cid] = Fill(vecchio.ticker, vecchio.side, cid, "rejected")

    now_dopo = lambda: datetime(2026, 7, 7, 16, 0, tzinfo=ROME)
    engine2, _b, _s = make_engine(tmp_path, rotta, now_dopo, fx_one, broker=broker)
    engine2.run(dry_run=False)

    s2 = store.read()
    assert not [c for c in s2.pending_orders if c in pendenti], "vanno tolti dai pendenti"
    assert s2.spent_on("2026-07-06") == 0.0, "un ordine rifiutato non si contabilizza"
