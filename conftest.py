"""Společné fixtury všech testovacích sad (rootdir = kořen repa, testpaths v pyproject).

Testy nesmí číst skutečný `.env` vývojáře (#1254): holé `Settings()` ho přes
pydantic-settings načte, test s nastaveným klíčem pak padne a pytest hodnotu
vypíše do výstupu — tajemství v konzoli a v každém logu z běhu sady. V CI
`.env` není, takže tam rozdíl nebyl vidět. Jeden soubor v kořeni místo tří
`conftest.py` v sadách: mypy je bere jako duplicitní modul.

Výjimka: `test_config` ověřuje načítání `.env` z izolovaného adresáře a sám
si cwd přepíná do tmp_path.
"""

import pytest

from gexlens_engine.config import Settings
from gexlens_news.config import NewsSettings


@pytest.fixture(autouse=True)
def _no_dotenv(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.module.__name__ == "test_config":
        return
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setitem(NewsSettings.model_config, "env_file", None)
