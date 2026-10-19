"""Configuration loading for Timone.

Two sources, kept strictly separate:
  * environment (.env)  -> secrets and infrastructure (API keys, paths)
  * rotta.yaml          -> the user's strategy (tickers, weights, amounts)

The Alpaca endpoint is hard-locked to the paper environment here: any other
base URL raises. In Fase 1 there is no path to live money, not even by
misconfiguration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import Rotta, Target

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_secret: str
    base_url: str
    data_dir: Path
    rotta_path: Path
    #: Percorso del file service-account Firebase (opzionale). Se assente, il
    #: ponte con la PWA (Àncora da mobile) è semplicemente spento.
    firebase_service_account: str | None = None


def _load_dotenv() -> Path | None:
    """Load .env if present and return its path (None if there is none).

    Optional dependency, no hard failure if missing. The lookup is the same as
    a bare `load_dotenv()`: python-dotenv walks up from this package's folder.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a declared dependency
        return None
    found = find_dotenv()
    if not found:
        return None
    load_dotenv(found)
    return Path(found)


def load_settings(
    env: dict[str, str] | None = None, *, base_dir: str | Path | None = None
) -> Settings:
    """Build Settings from the environment.

    Passing `env` explicitly is used by tests; production reads os.environ.

    Relative paths (data dir, rotta, service account, defaults included) are
    resolved against `base_dir`. In production that is the folder of the .env
    the API keys come from, so keys and state always belong to the same
    installation whatever the current directory: launched from elsewhere,
    Timone used to read and write a fresh ./data there, with its own Àncora.
    Without a .env (and no `base_dir`) they stay relative to the current
    directory, as before.
    """
    if env is None:
        dotenv_path = _load_dotenv()
        env = dict(os.environ)
        if base_dir is None and dotenv_path is not None:
            base_dir = dotenv_path.parent
    base = Path(base_dir) if base_dir is not None else None

    def _path(value: str) -> Path:
        p = Path(value).expanduser()
        return base / p if base is not None and not p.is_absolute() else p

    api_key = env.get("ALPACA_API_KEY", "").strip()
    api_secret = env.get("ALPACA_API_SECRET", "").strip()
    base_url = env.get("ALPACA_BASE_URL", PAPER_BASE_URL).strip()

    if base_url.rstrip("/") != PAPER_BASE_URL:
        raise ValueError(
            "Endpoint non consentito: Timone Fase 1 opera SOLO su "
            f"{PAPER_BASE_URL} (paper trading). Trovato: {base_url!r}."
        )

    data_dir = _path(env.get("TIMONE_DATA_DIR", "./data"))
    rotta_path = _path(env.get("TIMONE_ROTTA", "./rotta.yaml"))
    service_account = env.get("FIREBASE_SERVICE_ACCOUNT", "").strip() or None
    if service_account is not None:
        service_account = str(_path(service_account))

    return Settings(
        api_key=api_key,
        api_secret=api_secret,
        base_url=PAPER_BASE_URL,
        data_dir=data_dir,
        rotta_path=rotta_path,
        firebase_service_account=service_account,
    )


def load_rotta(path: str | Path) -> Rotta:
    """Parse and validate rotta.yaml into a Rotta (validation in the model)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Rotta non trovata: {path}. Copia rotta.example.yaml in "
            f"{path.name} e compilala."
        )

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    try:
        targets = tuple(
            Target(ticker=str(t["ticker"]).strip(), weight_pct=float(t["weight_pct"]))
            for t in raw["targets"]
        )
        rotta = Rotta(
            amount_per_run_eur=float(raw["amount_per_run_eur"]),
            rebalance_threshold_pct=float(raw["rebalance_threshold_pct"]),
            targets=targets,
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Rotta malformata ({path}): campo mancante o errato: {exc}")

    # Guardia contro i placeholder: rifiuta ticker d'esempio non sostituiti.
    placeholder = [t.ticker for t in rotta.targets if t.ticker.upper().startswith("TICKER_")]
    if placeholder:
        raise ValueError(
            "La Rotta contiene ancora i placeholder d'esempio "
            f"({', '.join(placeholder)}): sostituiscili con i tuoi ticker reali."
        )

    return rotta
