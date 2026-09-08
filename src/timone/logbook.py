"""Giornale di bordo — log strutturato di ogni run.

Un file JSONL per run: ogni riga è un evento (decisione, ordine, fill, errore).
In coda, un riassunto leggibile in italiano, pensato per essere compreso da un
umano sei mesi dopo, senza contesto.

Anche "non ho fatto nulla" è un evento loggato, con la sua motivazione.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .models import RunReport


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Logbook:
    def __init__(self, path: str | Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **fields) -> None:
        record = {"ts": _now_iso(), "run_id": self.run_id, "kind": kind}
        record.update(fields)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def summary(self, testo: str) -> None:
        self.event("riassunto", testo=testo)

    def seal(self, prev_hash: str) -> str:
        """Sigilla il run: hash sha256 di (sigillo precedente + contenuto del file
        fin qui). Rieseguire lo stesso giorno aggiunge un nuovo anello: il sigillo
        successivo copre anche il precedente. La catena rende lo storico
        verificabile: un byte cambiato la spezza.
        """
        content = self.path.read_bytes() if self.path.exists() else b""
        digest = hashlib.sha256(prev_hash.encode("utf-8") + content).hexdigest()
        self.event("sigillo", prev=prev_hash, hash=digest)
        return digest


GENESIS = "0" * 64


def verify_chain(logbook_dir: str | Path) -> tuple[bool, int, str]:
    """Riverifica l'intera catena, run per run, dal varo.

    Ritorna (integra, sigilli_verificati, messaggio). La verifica ricalcola ogni
    hash dai byte reali dei file: qualunque modifica retroattiva spezza la catena.
    """
    logbook_dir = Path(logbook_dir)
    files = sorted(logbook_dir.glob("*.jsonl"))
    prev = GENESIS
    checked = 0
    for f in files:
        raw = f.read_bytes()
        offset = 0
        for line in raw.splitlines(keepends=True):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                return False, checked, f"Riga illeggibile in {f.name}."
            if record.get("kind") == "sigillo":
                expected = hashlib.sha256(
                    prev.encode("utf-8") + raw[:offset]
                ).hexdigest()
                if record.get("prev") != prev or record.get("hash") != expected:
                    return (
                        False,
                        checked,
                        f"Il run {record.get('run_id', f.stem)} non corrisponde "
                        "al sigillo precedente.",
                    )
                prev = expected
                checked += 1
            offset += len(line)
    if checked == 0:
        return True, 0, "Nessun sigillo ancora: il Giornale è vuoto."
    return True, checked, f"{checked} sigilli verificati · nessuna discrepanza."


def build_summary(report: RunReport) -> str:
    """Compone il riassunto in italiano a partire dal RunReport."""
    lines: list[str] = [f"Giornale di bordo — run {report.run_id}"]

    if report.halted_reason:
        lines.append(f"Run interrotto: {report.halted_reason}")
        return "\n".join(lines)

    approved = [d for d in report.decisions if d.approved]
    rejected = [d for d in report.decisions if not d.approved]
    filled = [f for f in report.fills if f.is_filled]

    lines.append(
        f"Ordini proposti: {len(report.decisions)} "
        f"(approvati {len(approved)}, rifiutati {len(rejected)})."
    )

    for d in approved:
        o = d.order
        lines.append(
            f"  APPROVATO {o.side.value.upper()} {o.ticker} "
            f"{o.notional_eur:.2f} EUR — {o.reason}"
        )
    for d in rejected:
        o = d.order
        lines.append(
            f"  RIFIUTATO {o.side.value.upper()} {o.ticker} "
            f"{o.notional_eur:.2f} EUR — regola '{d.rule}': {d.reason}"
        )

    if filled:
        lines.append(f"Eseguiti {len(filled)} ordini:")
        for f in filled:
            lines.append(
                f"  FILL {f.side.value.upper()} {f.ticker} "
                f"qty {f.filled_qty:.6f} @ {f.filled_avg_price_usd:.2f} USD"
            )
    else:
        lines.append("Nessun ordine eseguito in questo run.")

    for note in report.notes:
        lines.append(f"Nota: {note}")

    return "\n".join(lines)
