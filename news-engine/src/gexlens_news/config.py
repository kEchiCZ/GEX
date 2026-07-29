"""Konfigurace news-engine — výhradně z prostředí / `.env` (S10).

V repu je jen `.env.example` s prázdnými placeholdery; skutečné klíče existují
lokálně a `.gitignore` je kryje. Prázdný klíč = zdroj se nespustí (ne degraded)
— nemá smysl hlásit poruchu něčeho, co uživatel vědomě nezapnul.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Oficiální feedy Fedu (Tier A) — statements, projevy, minutes
FED_RSS_URLS = (
    "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "https://www.federalreserve.gov/feeds/speeches.xml",
)

# Tier B redundance k Finnhubu (SPEC kap. 1) — širší pokrytí, dedup je řeší
NEWS_RSS_URLS = (
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "https://finance.yahoo.com/news/rssindex",
)


class NewsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GEXLENS_NEWS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://gexlens:gexlens@localhost:5432/gexlens"
    log_level: str = "INFO"
    # Archiv 1min barů zapisuje datový engine; news-engine si ho jen čte (#276)
    data_dir: Path = Path("data")
    # Jak často se dopočítávají reakce na zprávy s uzavřenými okny
    reaction_interval_s: float = Field(default=300.0, gt=0)
    # Interní ingest API pro WS kanály (#286); prázdné = push vypnutý
    api_base: str = "http://localhost:8000"
    # Ranní retro pass (#284, SPEC 7.4) — čas před EU open, UTC
    retro_pass_hour: int = Field(default=5, ge=0, le=23)
    retro_pass_minute: int = Field(default=30, ge=0, le=59)

    # Intervaly sběru (SPEC kap. 1). RSS jede à 60 s díky conditional GET —
    # nezměněný feed vrací 304, takže krátká perioda nikoho nezatěžuje.
    forexfactory_interval_s: float = Field(default=3600.0, gt=0)
    finnhub_interval_s: float = Field(default=60.0, gt=0)
    rss_interval_s: float = Field(default=60.0, gt=0)
    fed_rss_interval_s: float = Field(default=300.0, gt=0)
    reddit_interval_s: float = Field(default=900.0, gt=0)

    # Hodinový refresh actual hodnot z FF kalendáře (#277, ADR-0018) —
    # widget feed actual nenese; 0 = vypnuto
    ff_actual_refresh_s: float = Field(default=3600.0, ge=0)
    # Kolik týdnů historie stáhne CLI `backfill-ff` bez explicitního --weeks
    ff_backfill_weeks: int = Field(default=156, ge=1, le=520)

    # Gemini batch klasifikace (#281, SPEC kap. 4): dávka à 60 s jen při
    # neprázdné frontě; denní limit s rezervou pod free tierem (~1500 RPD)
    gemini_model: str = "gemini-flash-latest"
    llm_interval_s: float = Field(default=60.0, gt=0)
    llm_daily_limit: int = Field(default=1400, ge=0)
    llm_batch_limit: int = Field(default=200, ge=1, le=500)

    # Okno rolling deduplikace (#273, #351): musí pokrýt republikace téže story
    # (měřeno Δt 23 min – hodiny), ne jen rozdíl rychlosti zdrojů — 10 min ze
    # SPEC 3.3 propouštělo ~19 duplicit/den přes půlnoc UTC (ADR-0017). Strop
    # drží denní rubriky se stejným titulkem (Δt ≈ 24 h) oddělené.
    dedup_window_minutes: int = Field(default=360, ge=1, le=1080)
    cnn_fg_interval_s: float = Field(default=3600.0, gt=0)
    # PCR řada z vlastních snapshot dat (#290, SPEC 5.8) — odvozená, bez klíče
    pcr_interval_s: float = Field(default=300.0, gt=0)
    pcr_symbols: str = "ES,NQ"

    # Klíče a přihlašovací údaje — prázdné = zdroj vypnutý (S10)
    finnhub_api_key: str = ""
    fred_api_key: str = ""
    bls_api_key: str = ""
    bea_api_key: str = ""
    gemini_api_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""

    @property
    def enabled_sources(self) -> dict[str, bool]:
        """Které zdroje mají vyplněné údaje — podklad pro start i CLI status."""
        return {
            "finnhub": bool(self.finnhub_api_key),
            "fred": bool(self.fred_api_key),
            "bls": bool(self.bls_api_key),
            "bea": bool(self.bea_api_key),
            "gemini": bool(self.gemini_api_key),
            "reddit": bool(self.reddit_client_id and self.reddit_client_secret),
            # Bez klíče: veřejný feed / RSS
            "forexfactory": True,
            "fed_rss": True,
            "rss": True,
            "cnn_fg": True,
        }


def load_news_settings() -> NewsSettings:
    return NewsSettings()
