"""Test di configurazione: blocco endpoint live, validazione Rotta."""

import textwrap
from pathlib import Path

import pytest

from timone.config import PAPER_BASE_URL, load_rotta, load_settings
from timone.models import Rotta, Target


def test_endpoint_paper_ok():
    s = load_settings({"ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"})
    assert s.base_url == PAPER_BASE_URL


def test_endpoint_live_rifiutato():
    with pytest.raises(ValueError):
        load_settings(
            {
                "ALPACA_API_KEY": "k",
                "ALPACA_API_SECRET": "s",
                "ALPACA_BASE_URL": "https://api.alpaca.markets",
            }
        )


def test_rotta_pesi_non_100_rifiutata():
    with pytest.raises(ValueError):
        Rotta(100.0, 5.0, (Target("AAA", 60.0), Target("BBB", 30.0)))


def test_rotta_ticker_duplicati_rifiutati():
    with pytest.raises(ValueError):
        Rotta(100.0, 5.0, (Target("AAA", 50.0), Target("AAA", 50.0)))


def _write(tmp_path, content):
    p = tmp_path / "rotta.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def test_load_rotta_ok(tmp_path):
    p = _write(
        tmp_path,
        """
        amount_per_run_eur: 100.0
        rebalance_threshold_pct: 5.0
        targets:
          - ticker: SPY
            weight_pct: 60.0
          - ticker: QQQ
            weight_pct: 40.0
        """,
    )
    rotta = load_rotta(p)
    assert rotta.tickers == ("SPY", "QQQ")


def test_load_rotta_placeholder_rifiutato(tmp_path):
    p = _write(
        tmp_path,
        """
        amount_per_run_eur: 100.0
        rebalance_threshold_pct: 5.0
        targets:
          - ticker: TICKER_1
            weight_pct: 100.0
        """,
    )
    with pytest.raises(ValueError, match="placeholder"):
        load_rotta(p)


def test_load_rotta_mancante(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_rotta(tmp_path / "non_esiste.yaml")


# --- Percorsi relativi: seguono il .env, non la cartella corrente -----------

_VARIABILI_PERCORSO = ("TIMONE_DATA_DIR", "TIMONE_ROTTA", "FIREBASE_SERVICE_ACCOUNT")


def _progetto_e_altrove(tmp_path, monkeypatch):
    """Un progetto col suo .env e una cartella qualsiasi da cui si lancia il
    comando. Il .env trovato è quello del progetto (come in produzione, dove
    python-dotenv lo cerca risalendo dal codice installato)."""
    import dotenv

    progetto = tmp_path / "progetto"
    altrove = tmp_path / "altrove"
    progetto.mkdir()
    altrove.mkdir()
    (progetto / ".env").write_text("# nessuna variabile: bastano i default\n", encoding="utf-8")
    for nome in _VARIABILI_PERCORSO:
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setattr(dotenv, "find_dotenv", lambda *a, **k: str(progetto / ".env"))
    monkeypatch.chdir(altrove)
    # Guardia: se un domani config.py non passasse più da dotenv.find_dotenv,
    # il test si ferma qui invece di lanciare comandi sui dati veri.
    assert load_settings().data_dir.is_relative_to(tmp_path)
    return progetto, altrove


def test_percorsi_relativi_al_env_non_alla_cartella_corrente(tmp_path, monkeypatch):
    """Regressione: lanciato da un'altra cartella, Timone prendeva le chiavi dal
    .env del progetto ma leggeva e scriveva lo stato in ./data della cartella
    corrente: un altro stato, con un'altra Àncora."""
    progetto, _ = _progetto_e_altrove(tmp_path, monkeypatch)
    s = load_settings()
    assert s.data_dir == progetto / "data"
    assert s.rotta_path == progetto / "rotta.yaml"


def test_ancora_calata_da_altra_cartella_finisce_nello_stato_vero(tmp_path, monkeypatch):
    """Il caso pericoloso: `timone ancora --drop` da un'altra cartella diceva
    "Àncora CALATA" ma calava quella di uno stato nuovo e vuoto, lasciando
    alzata quella vera."""
    from timone.cli import main
    from timone.state import JsonStateStore, TimoneState

    progetto, altrove = _progetto_e_altrove(tmp_path, monkeypatch)
    vero = JsonStateStore(progetto / "data" / "state.json")
    vero.write(TimoneState())
    assert not vero.read().anchor_down

    assert main(["ancora", "--drop"]) == 0

    assert vero.read().anchor_down
    assert not (altrove / "data").exists()


def test_percorsi_assoluti_restano_invariati(tmp_path):
    s = load_settings(
        {"TIMONE_DATA_DIR": str(tmp_path / "dati"), "TIMONE_ROTTA": str(tmp_path / "r.yaml")},
        base_dir=tmp_path / "altro",
    )
    assert s.data_dir == tmp_path / "dati"
    assert s.rotta_path == tmp_path / "r.yaml"


def test_service_account_relativo_segue_la_base(tmp_path):
    s = load_settings({"FIREBASE_SERVICE_ACCOUNT": "sa.json"}, base_dir=tmp_path)
    assert s.firebase_service_account == str(tmp_path / "sa.json")


def test_senza_env_i_percorsi_restano_relativi_come_prima():
    """Con l'ambiente passato a mano (test, script) e nessuna base: invariato."""
    s = load_settings({})
    assert s.data_dir == Path("data")
    assert s.rotta_path == Path("rotta.yaml")
    assert s.firebase_service_account is None
