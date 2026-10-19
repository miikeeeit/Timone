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


def test_bussola_ponte_spento_avvisa(tmp_path):
    """Senza service account la console web non riceve dati: va detto."""
    c = doctor._bussola(_settings(tmp_path))
    assert c.esito == doctor.WARN
    assert "non riceve dati" in c.dettaglio


def test_run_checks_include_la_bussola(tmp_path):
    ids = [c.id for c in doctor.run_checks(_settings(tmp_path))]
    assert "bussola" in ids


# --- Scheduler launchd: caricato non vuol dire funzionante -------------------
# `launchctl list` stampa PID, ultimo codice d'uscita ed etichetta. Il 19/10 i
# job erano caricati ma uscivano con 127 da dieci giorni (progetto spostato,
# script non più al suo posto) e doctor diceva "launchd attivo".

def _plist(cartella: Path, label: str, script: Path) -> None:
    import plistlib

    cartella.mkdir(parents=True, exist_ok=True)
    with (cartella / f"{label}.plist").open("wb") as f:
        plistlib.dump({"Label": label,
                       "ProgramArguments": ["/bin/zsh", str(script), "run"]}, f)


def _script(tmp_path: Path) -> Path:
    s = tmp_path / "progetto" / "scripts" / "timone_scheduled.sh"
    s.parent.mkdir(parents=True, exist_ok=True)
    s.write_text("#!/bin/zsh\n")
    return s


def test_scheduler_launchd_sano_ok(tmp_path):
    agenti, script = tmp_path / "LaunchAgents", _script(tmp_path)
    _plist(agenti, "com.timone.run", script)
    _plist(agenti, "com.timone.heartbeat", script)
    elenco = "PID\tStatus\tLabel\n-\t0\tcom.timone.run\n-\t0\tcom.timone.heartbeat\n"
    c = doctor._scheduler(launchctl_list=elenco, agents_dir=agenti)
    assert c.esito == doctor.OK
    assert "launchd attivo" in c.dettaglio


def test_scheduler_run_uscito_con_127_errore(tmp_path):
    agenti, script = tmp_path / "LaunchAgents", _script(tmp_path)
    _plist(agenti, "com.timone.run", script)
    elenco = "-\t127\tcom.timone.run\n"
    c = doctor._scheduler(launchctl_list=elenco, agents_dir=agenti)
    assert c.esito == doctor.ERR
    assert "127" in c.dettaglio


def test_scheduler_heartbeat_rotto_errore_anche_se_run_ok(tmp_path):
    """Senza heartbeat nessun avviso parte quando i run si fermano."""
    agenti, script = tmp_path / "LaunchAgents", _script(tmp_path)
    _plist(agenti, "com.timone.run", script)
    _plist(agenti, "com.timone.heartbeat", script)
    elenco = "-\t0\tcom.timone.run\n-\t1\tcom.timone.heartbeat\n"
    c = doctor._scheduler(launchctl_list=elenco, agents_dir=agenti)
    assert c.esito == doctor.ERR
    assert "com.timone.heartbeat" in c.dettaglio


def test_scheduler_plist_verso_percorso_sparito_errore_subito(tmp_path):
    """Dopo uno spostamento l'ultimo codice è ancora 0 fino al giro successivo:
    il percorso mancante nel plist si vede già prima."""
    agenti = tmp_path / "LaunchAgents"
    sparito = tmp_path / "vecchio" / "scripts" / "timone_scheduled.sh"
    _plist(agenti, "com.timone.run", sparito)
    c = doctor._scheduler(launchctl_list="-\t0\tcom.timone.run\n", agents_dir=agenti)
    assert c.esito == doctor.ERR
    assert str(sparito) in c.dettaglio


def test_scheduler_etichetta_simile_non_conta(tmp_path):
    """`com.timone.runner` non è `com.timone.run`."""
    c = doctor._scheduler(launchctl_list="-\t0\tcom.timone.runner\n",
                          agents_dir=tmp_path, crontab="")
    assert c.esito == doctor.WARN
