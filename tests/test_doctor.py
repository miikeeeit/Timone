"""Test per `timone doctor` — i controlli deterministici (niente rete)."""

from pathlib import Path
from types import SimpleNamespace

from timone import doctor
from timone.config import PAPER_BASE_URL, Settings


def _settings(tmp_path: Path, base_url: str = PAPER_BASE_URL) -> Settings:
    return Settings(
        api_key="k",
        api_secret="s",
        base_url=base_url,
        data_dir=tmp_path,
        rotta_path=tmp_path / "rotta.yaml",
    )


def test_summarize_tutto_ok():
    checks = [
        doctor.Check("a", "A", "", doctor.OK),
        doctor.Check("b", "B", "", doctor.OK),
    ]
    s = doctor.summarize(checks)
    assert s == {"stato": doctor.OK, "ok": 2, "totale": 2}


def test_summarize_un_errore_domina_su_attenzione():
    checks = [
        doctor.Check("a", "A", "", doctor.OK),
        doctor.Check("b", "B", "", doctor.WARN),
        doctor.Check("c", "C", "", doctor.ERR),
    ]
    s = doctor.summarize(checks)
    assert s["stato"] == doctor.ERR
    assert s["ok"] == 1 and s["totale"] == 3


def test_summarize_attenzione_se_nessun_errore():
    checks = [doctor.Check("a", "A", "", doctor.OK), doctor.Check("b", "B", "", doctor.WARN)]
    assert doctor.summarize(checks)["stato"] == doctor.WARN


def test_endpoint_paper_ok(tmp_path):
    c = doctor._endpoint(_settings(tmp_path))
    assert c.esito == doctor.OK and c.ok


def test_endpoint_non_paper_errore(tmp_path):
    c = doctor._endpoint(_settings(tmp_path, base_url="https://api.alpaca.markets"))
    assert c.esito == doctor.ERR and not c.ok


def test_ancora_alzata_ok():
    state = SimpleNamespace(anchor_down=False, anchor_reason=None)
    assert doctor._ancora(state).esito == doctor.OK


def test_ancora_calata_attenzione_con_motivo():
    state = SimpleNamespace(anchor_down=True, anchor_reason="drawdown")
    c = doctor._ancora(state)
    assert c.esito == doctor.WARN and "drawdown" in c.dettaglio


def test_dati_assente_errore(tmp_path):
    assert doctor._dati(tmp_path).esito == doctor.ERR


def test_dati_senza_backup_attenzione(tmp_path):
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    assert doctor._dati(tmp_path).esito == doctor.WARN


def test_dati_con_backup_ok(tmp_path):
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state.json.bak").write_text("{}", encoding="utf-8")
    assert doctor._dati(tmp_path).esito == doctor.OK
