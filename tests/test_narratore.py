"""Test per il Narratore — categorizzazione deterministica dei run."""

import json
from datetime import datetime
from pathlib import Path

from timone.config import PAPER_BASE_URL, Settings
from timone.narratore import ROME, weekly_report

NOW = datetime(2026, 1, 15, 18, 0, tzinfo=ROME)


def _settings(tmp_path: Path) -> Settings:
    (tmp_path / "logbook").mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "fiscale.csv").write_text(
        "data,ticker,side,quantita,prezzo_usd,controvalore_usd,"
        "cambio_eur_usd,controvalore_eur,pnl_realizzato_eur,cambio_stimato\n",
        encoding="utf-8",
    )
    return Settings(
        api_key="k", api_secret="s", base_url=PAPER_BASE_URL,
        data_dir=tmp_path, rotta_path=tmp_path / "rotta.yaml",
    )


def _write_log(tmp_path: Path, name: str, eventi: list[dict]) -> None:
    (tmp_path / "logbook").mkdir(parents=True, exist_ok=True)
    p = tmp_path / "logbook" / f"{name}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in eventi), encoding="utf-8")


def _run_completato(ts: str, *, ticker="NVDA") -> list[dict]:
    return [
        {"kind": "run_start", "run_id": "r", "ts": ts, "ora_roma": ts, "dry_run": False},
        {"kind": "ordine", "ticker": ticker, "side": "buy", "eur": 100.0,
         "approvato": True, "regola": "all"},
        {"kind": "riassunto", "testo": "Giornale — run\nOrdini proposti: 1 (approvati 1, rifiutati 0)."},
        {"kind": "sigillo", "hash": "a" * 8, "prev": "0" * 8},
    ]


def test_run_completato_raccontato(tmp_path):
    _write_log(tmp_path, "20260114", _run_completato("2026-01-14T16:00:00+01:00"))
    r = weekly_report(_settings(tmp_path), now=NOW)
    testo = " ".join(r["paragrafi"])
    assert "ha eseguito 1 run" in testo
    assert "NVDA" in testo


def test_run_fallito_a_meta_segnalato(tmp_path):
    # run_start reale senza riassunto = fallito a metà
    _write_log(tmp_path, "20260114", [
        {"kind": "run_start", "run_id": "r", "ts": "2026-01-14T16:00:00+01:00",
         "ora_roma": "2026-01-14T16:00:00+01:00", "dry_run": False},
        {"kind": "guardrail", "regola": "ancora", "approvato": True},
    ])
    r = weekly_report(_settings(tmp_path), now=NOW)
    testo = " ".join(r["paragrafi"])
    assert r["attenzione"] is True
    assert "fermato a metà" in testo


def test_dry_run_non_contato(tmp_path):
    _write_log(tmp_path, "20260114", [
        {"kind": "run_start", "run_id": "r", "ts": "2026-01-14T16:00:00+01:00",
         "ora_roma": "2026-01-14T16:00:00+01:00", "dry_run": True},
        {"kind": "ordine", "ticker": "SPY", "side": "buy", "eur": 100.0, "approvato": True},
        {"kind": "riassunto", "testo": "x\ny"},
    ])
    r = weekly_report(_settings(tmp_path), now=NOW)
    assert "non ha eseguito alcun run" in " ".join(r["paragrafi"])


def test_settimana_vuota_e_attenzione(tmp_path):
    r = weekly_report(_settings(tmp_path), now=NOW)
    assert r["attenzione"] is True
    assert "non ha eseguito alcun run" in " ".join(r["paragrafi"])


def test_run_fuori_finestra_ignorato(tmp_path):
    # run di 30 giorni prima: fuori dalla finestra di 7 giorni
    _write_log(tmp_path, "20251216", _run_completato("2025-12-16T16:00:00+01:00"))
    r = weekly_report(_settings(tmp_path), now=NOW)
    assert "non ha eseguito alcun run" in " ".join(r["paragrafi"])
