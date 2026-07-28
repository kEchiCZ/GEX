"""Konfigurace news-engine — výhradně z prostředí / `.env` (S10).

V repu je jen `.env.example` s prázdnými placeholdery; skutečné klíče existují
lokálně a `.gitignore` je kryje. Prázdný klíč = zdroj se nespustí (ne degraded)
— nemá smysl hlásit poruchu něčeho, co uživatel vědomě nezapnul.
"""

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

    # Intervaly sběru (SPEC kap. 1). RSS jede à 60 s díky conditional GET —
    # nezměněný feed vrací 304, takže krátká perioda nikoho nezatěžuje.
    forexfactory_interval_s: float = Field(default=3600.0, gt=0)
    finnhub_interval_s: float = Field(default=60.0, gt=0)
    rss_interval_s: float = Field(default=60.0, gt=0)
    fed_rss_interval_s: float = Field(default=300.0, gt=0)
    reddit_interval_s: float = Field(default=900.0, gt=0)
    cnn_fg_interval_s: float = Field(default=3600.0, gt=0)

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
