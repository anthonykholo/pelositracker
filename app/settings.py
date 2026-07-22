from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _float(env: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(env.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(env.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _optional_path(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name, "").strip()
    return Path(value) if value else None


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    database_url: str
    ledger_db: Path
    history_db: Path
    state_db: Path
    max_data_age_seconds: float
    odds_poll_seconds: float
    confidence_threshold: float
    edge_threshold: float
    kelly_fraction: float
    allow_uncalibrated_paper: bool
    enable_tennis_model: bool
    odds_api_key: str
    odds_regions: str
    odds_markets: str
    odds_bookmakers: str
    enable_action_network: bool
    enable_pinnacle_guest: bool
    pinnacle_guest_api_key: str
    enable_independent_models: bool
    calibration_artifact: Path | None
    independent_model_artifact: Path | None
    worker_count: int
    authorized_users: str
    admin_username: str
    admin_password: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if env is None else env
        environment = values.get("APP_ENV", "development").strip().casefold()
        settings = cls(
            environment=environment,
            database_url=values.get("DATABASE_URL", "").strip(),
            ledger_db=Path(values.get("LEDGER_DB", "ledger.db")),
            history_db=Path(values.get("HISTORY_DB", "history.db")),
            state_db=Path(values.get("STATE_DB", values.get("LEDGER_DB", "ledger.db"))),
            max_data_age_seconds=_float(values, "MAX_DATA_AGE_SECONDS", 120.0, minimum=1.0),
            odds_poll_seconds=_float(values, "ODDS_POLL_SECONDS", 45.0, minimum=5.0),
            confidence_threshold=_float(values, "SIGNAL_CONFIDENCE_THRESHOLD", 0.0),
            edge_threshold=_float(values, "SIGNAL_EDGE_THRESHOLD", 0.0),
            kelly_fraction=_float(values, "SIGNAL_KELLY_FRACTION", 0.25),
            allow_uncalibrated_paper=_bool(values, "PAPER_ALLOW_UNCALIBRATED", False),
            enable_tennis_model=_bool(values, "ENABLE_TENNIS_MODEL", False),
            odds_api_key=values.get("THE_ODDS_API_KEY", "").strip(),
            odds_regions=values.get("ODDS_REGIONS", "us").strip(),
            odds_markets=values.get("ODDS_MARKETS", "h2h,spreads,totals").strip(),
            odds_bookmakers=values.get("ODDS_BOOKMAKERS", "").strip(),
            enable_action_network=_bool(values, "ENABLE_ACTION_NETWORK", False),
            enable_pinnacle_guest=_bool(values, "ENABLE_PINNACLE_GUEST", False),
            pinnacle_guest_api_key=values.get("PINNACLE_GUEST_API_KEY", "").strip(),
            enable_independent_models=_bool(values, "ENABLE_INDEPENDENT_MODELS", False),
            calibration_artifact=_optional_path(values, "CALIBRATION_ARTIFACT"),
            independent_model_artifact=_optional_path(
                values, "INDEPENDENT_MODEL_ARTIFACT"
            ),
            worker_count=_int(values, "WEB_CONCURRENCY", 1),
            authorized_users=values.get("AUTHORIZED_USERS", "").strip(),
            admin_username=values.get("ADMIN_USERNAME", "admin").strip(),
            admin_password=values.get("ADMIN_PASSWORD", "admin"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.environment in {"production", "prod"} and self.worker_count != 1:
            raise ValueError("WEB_CONCURRENCY must be 1 until distributed feed ownership is configured")
        if (self.environment in {"production", "prod"} and not self.authorized_users
                and (self.admin_username == "admin" or self.admin_password == "admin")):
            raise ValueError("production requires non-default authentication credentials")
        if self.enable_pinnacle_guest and not self.pinnacle_guest_api_key:
            raise ValueError("ENABLE_PINNACLE_GUEST requires PINNACLE_GUEST_API_KEY")
