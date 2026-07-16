"""Konfigurační vrstva enginu (SPEC kap. 3 a 5) — Pydantic Settings nad .env.

Engine s nevalidní konfigurací odmítá nastartovat: `load_settings` vyhodí
`ConfigError` se srozumitelným výpisem všech chybných položek.
"""

import datetime as dt
from pathlib import Path

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Nevalidní konfigurace — engine s ní nesmí běžet."""


class Settings(BaseSettings):
    """Konfigurace datového enginu; zdrojem jsou proměnné prostředí GEXLENS_* a soubor .env."""

    model_config = SettingsConfigDict(
        env_prefix="GEXLENS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # IBKR připojení (SPEC 3.1)
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = Field(default=7496, ge=1, le=65535)
    ibkr_client_id: int = Field(default=1, ge=0)
    # 1 = live; delayed data (3) engine za běhu odmítne (SPEC 3.1)
    market_data_type: int = Field(default=1, ge=1, le=4)
    connect_timeout_s: float = Field(default=10.0, gt=0)
    # Exponenciální backoff reconnectu 2 → 60 s (SPEC 3.1)
    reconnect_backoff_base_s: float = Field(default=2.0, gt=0)
    reconnect_backoff_max_s: float = Field(default=60.0, gt=0)

    # Instrumenty (ADR-0003): základní sada futures podkladů; watchlist z DB se přidává za běhu
    symbols: str = "ES"
    # Strop souběžně běžících instrumentů (market data lines rozpočet, ADR-0001/0003)
    max_instruments: int = Field(default=3, ge=1)
    # Watchlist z DB se čte každý k-tý minutový cyklus
    watchlist_poll_cycles: int = Field(default=5, ge=1)

    # Opční řetězec a rotační scheduler (SPEC 3.2, 3.3)
    strike_range_points: float = Field(default=200.0, gt=0)
    # Auto-rozšíření obálky, když se spot přiblíží k okraji na < tento podíl šířky
    strike_range_expand_threshold: float = Field(default=0.25, gt=0, lt=1)
    # Strop šířky denní obálky strikes (ADR-0002); při dosažení se obálka posouvá
    strike_range_max_points: float = Field(default=800.0, gt=0)
    batch_size: int = Field(default=80, ge=1)
    batch_timeout_s: float = Field(default=4.0, gt=0)
    # Křídla řetězce se sweepují každý k-tý cyklus (ATM±atm_sweep_width každý cyklus)
    wings_sweep_every: int = Field(default=3, ge=1)
    atm_sweep_width: int = Field(default=30, ge=1)
    # Max pokusů repair fronty na kontrakt za sweep, pak stale označení
    repair_max_attempts: int = Field(default=3, ge=1)
    # Kapacita market data lines účtu (ADR-0001: naměřeno ≥ 150; default konzervativní)
    market_data_lines: int = Field(default=100, ge=1)

    # Hot zóna (SPEC 3.4; limit streamů naměřen v ADR-0001)
    hot_zone_width: int = Field(default=15, ge=1)
    tick_by_tick_max_streams: int = Field(default=5, ge=1)

    # Storage a retence (SPEC kap. 5)
    # PostgreSQL DSN (OI archiv, metadata); default odpovídá docker compose dev instanci
    database_url: str = "postgresql+psycopg://gexlens:gexlens@localhost:5432/gexlens"
    data_dir: Path = Path("data")
    retention_days: int = Field(default=14, ge=1)
    disk_limit_gb: float = Field(default=2.0, gt=0)
    # Čas nočního purge jobu (UTC, po zavření US seance)
    retention_purge_time_utc: dt.time = dt.time(21, 30)

    @model_validator(mode="after")
    def _validate_backoff(self) -> "Settings":
        if self.reconnect_backoff_max_s < self.reconnect_backoff_base_s:
            raise ValueError("reconnect_backoff_max_s musí být ≥ reconnect_backoff_base_s")
        if self.strike_range_max_points < 2 * self.strike_range_points:
            raise ValueError(
                "strike_range_max_points musí být ≥ 2× strike_range_points (výchozí obálka)"
            )
        if not self.symbol_list:
            raise ValueError("symbols nesmí být prázdný seznam (alespoň jeden podklad)")
        return self

    @property
    def symbol_list(self) -> list[str]:
        """Základní sada podkladů z GEXLENS_SYMBOLS (čárkami oddělený seznam)."""
        seen: list[str] = []
        for raw in self.symbols.split(","):
            symbol = raw.strip().upper()
            if symbol and symbol not in seen:
                seen.append(symbol)
        return seen

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def ticks_dir(self) -> Path:
        return self.data_dir / "ticks"

    @property
    def derived_dir(self) -> Path:
        return self.data_dir / "derived"


def load_settings() -> Settings:
    """Načte a zvaliduje konfiguraci (proměnné prostředí + `.env` v pracovním adresáři).

    Při nevalidních hodnotách vyhodí `ConfigError` s výpisem každé chybné
    proměnné (název ve tvaru GEXLENS_*, důvod, zadaná hodnota).
    """
    try:
        return Settings()
    except ValidationError as exc:
        rows = []
        for err in exc.errors():
            loc = "_".join(str(part) for part in err["loc"])
            var = f"GEXLENS_{loc.upper()}" if loc else "(kombinace hodnot)"
            rows.append(f"  {var}: {err['msg']} (zadáno: {err.get('input')!r})")
        raise ConfigError(
            "Nevalidní konfigurace enginu (.env / proměnné prostředí):\n" + "\n".join(rows)
        ) from exc
