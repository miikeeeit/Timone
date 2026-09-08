"""Test di configurazione: blocco endpoint live, validazione Rotta."""

import textwrap

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
