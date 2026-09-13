"""Test del retry di rete: un blip di connettività non deve fermare il motore.

Caso reale che ha motivato questo codice: il run schedulato parte mentre il Mac
si è appena svegliato, il DNS non risolve, il run fallisce — e due fallimenti
consecutivi facevano calare l'Àncora senza che nulla fosse rotto.
"""

import socket

import pytest

from timone.broker_alpaca import (
    AlpacaBroker,
    TransientNetworkError,
    is_transient_network_error,
)


class _Stub:
    """Il minimo che serve a `_with_retry`: niente rete, niente alpaca-py."""

    NETWORK_RETRIES = 4
    NETWORK_BACKOFF_S = 0.0

    def __init__(self):
        self.attese: list[float] = []

    def _sleep(self, s):
        self.attese.append(s)


def _retry(stub, fn):
    return AlpacaBroker._with_retry(stub, "prova", fn)


# --- classificazione degli errori --------------------------------------------

def test_riconosce_gli_errori_di_connettivita():
    assert is_transient_network_error(socket.gaierror("nome non risolto"))
    assert is_transient_network_error(TimeoutError())
    assert is_transient_network_error(ConnectionError())


def test_riconosce_connection_error_di_requests():
    requests = pytest.importorskip("requests")
    assert is_transient_network_error(requests.exceptions.ConnectionError("boom"))
    assert is_transient_network_error(requests.exceptions.Timeout())


def test_un_errore_vero_non_e_un_errore_di_rete():
    assert not is_transient_network_error(ValueError("chiavi non valide"))
    assert not is_transient_network_error(RuntimeError("ordine rifiutato"))


# --- comportamento del retry --------------------------------------------------

def test_ritenta_e_poi_riesce():
    stub, tentativi = _Stub(), {"n": 0}

    def fn():
        tentativi["n"] += 1
        if tentativi["n"] < 3:
            raise socket.gaierror("DNS non ancora pronto")
        return "ok"

    assert _retry(stub, fn) == "ok"
    assert tentativi["n"] == 3
    assert len(stub.attese) == 2  # ha atteso tra un tentativo e l'altro


def test_backoff_crescente():
    stub = _Stub()
    stub.NETWORK_BACKOFF_S = 2.0
    with pytest.raises(TransientNetworkError):
        _retry(stub, lambda: (_ for _ in ()).throw(socket.gaierror("mai")))
    assert stub.attese == [2.0, 4.0, 6.0]  # 1x, 2x, 3x


def test_esauriti_i_tentativi_segnala_errore_di_rete_non_del_motore():
    stub = _Stub()
    with pytest.raises(TransientNetworkError):
        _retry(stub, lambda: (_ for _ in ()).throw(socket.gaierror("mai pronto")))
    assert len(stub.attese) == stub.NETWORK_RETRIES - 1


def test_errore_vero_non_viene_ritentato():
    """Ritentare credenziali sbagliate non servirebbe e mascherebbe il problema."""
    stub, tentativi = _Stub(), {"n": 0}

    def fn():
        tentativi["n"] += 1
        raise ValueError("credenziali non valide")

    with pytest.raises(ValueError):
        _retry(stub, fn)
    assert tentativi["n"] == 1
    assert stub.attese == []


# --- la garanzia che conta ----------------------------------------------------

def test_rete_assente_non_conta_come_run_fallito(tmp_path, monkeypatch):
    """Un blip di rete NON deve incrementare failed_runs né calare l'Àncora."""
    from timone import cli
    from timone.config import PAPER_BASE_URL, Settings
    from timone.state import JsonStateStore

    settings = Settings(
        api_key="k", api_secret="s", base_url=PAPER_BASE_URL,
        data_dir=tmp_path, rotta_path=tmp_path / "rotta.yaml",
    )

    def rete_assente(_settings):
        raise TransientNetworkError("calendario: rete non raggiungibile")

    monkeypatch.setattr(cli, "_build_engine", rete_assente)
    monkeypatch.setattr(cli, "_sync_ancora_mobile", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_notify", lambda *a, **k: None)

    assert cli.cmd_run(settings, None) == 1

    state = JsonStateStore(tmp_path / "state.json").read()
    assert state.failed_runs == 0, "la rete assente non è un run fallito"
    assert not state.anchor_down, "l'Àncora non deve calare per un blip di rete"
    assert any("Rete non disponibile" in a.get("tipo", "") for a in state.avvisi)
