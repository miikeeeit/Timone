"""Test del ponte Àncora (PWA -> motore) con un RemoteStore finto."""

from pathlib import Path

from timone import remote
from timone.config import PAPER_BASE_URL, Settings
from timone.state import JsonStateStore


class FakeRemote(remote.RemoteStore):
    def __init__(self, cmd):
        self.cmd = cmd
        self.confermato = None

    def get_comando_ancora(self):
        return self.cmd

    def conferma_ancora(self, applicato_il, stato_motore):
        self.confermato = (applicato_il, stato_motore)
        if self.cmd is not None:
            self.cmd["applicato_il"] = applicato_il
            self.cmd["stato_motore"] = stato_motore


def _settings(tmp_path: Path, sa: str | None = None) -> Settings:
    return Settings(
        api_key="k", api_secret="s", base_url=PAPER_BASE_URL,
        data_dir=tmp_path, rotta_path=tmp_path / "rotta.yaml",
        firebase_service_account=sa,
    )


def _store(tmp_path: Path) -> JsonStateStore:
    return JsonStateStore(tmp_path / "state.json")


def test_set_anchor_registra_e_azzera_motivo(tmp_path):
    store = _store(tmp_path)
    store.set_anchor(True, reason="drawdown")
    s = store.read()
    assert s.anchor_down and s.anchor_reason == "drawdown"
    store.set_anchor(False)  # rialzare azzera il motivo
    s = store.read()
    assert not s.anchor_down and s.anchor_reason is None


def test_ponte_spento_senza_service_account(tmp_path):
    store = _store(tmp_path)
    # nessun service account -> firestore_store None -> no-op
    assert remote.sync_ancora(_settings(tmp_path), store) is None


def test_comando_cala_applicato_una_volta(tmp_path):
    store = _store(tmp_path)
    fake = FakeRemote({"azione": "cala", "richiesto_il": "2026-01-01T10:00:00",
                       "richiesto_da": "me@example.com", "motivo": "sto controllando",
                       "applicato_il": None})
    esito = remote.sync_ancora(_settings(tmp_path), store, remote=fake)
    assert esito == {"azione": "cala", "stato": "calata"}
    s = store.read()
    assert s.anchor_down and "mobile" in s.anchor_reason and "sto controllando" in s.anchor_reason
    assert fake.confermato[1] == "calata"
    # secondo sync: comando ormai confermato -> nessuna nuova applicazione
    assert remote.sync_ancora(_settings(tmp_path), store, remote=fake) is None


def test_comando_rialza(tmp_path):
    store = _store(tmp_path)
    store.set_anchor(True, reason="prima era giù")
    fake = FakeRemote({"azione": "rialza", "richiesto_il": "2026-01-02T09:00:00",
                       "richiesto_da": "me@example.com", "applicato_il": None})
    esito = remote.sync_ancora(_settings(tmp_path), store, remote=fake)
    assert esito == {"azione": "rialza", "stato": "alzata"}
    s = store.read()
    assert not s.anchor_down and s.anchor_reason is None


def test_azione_non_valida_ignorata(tmp_path):
    store = _store(tmp_path)
    fake = FakeRemote({"azione": "compra_tutto", "richiesto_il": "2026-01-03T09:00:00",
                       "applicato_il": None})
    assert remote.sync_ancora(_settings(tmp_path), store, remote=fake) is None
    assert not store.read().anchor_down


def test_nessun_comando(tmp_path):
    store = _store(tmp_path)
    assert remote.sync_ancora(_settings(tmp_path), store, remote=FakeRemote(None)) is None
