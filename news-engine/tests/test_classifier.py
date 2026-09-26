"""Testy pravidlového klasifikátoru v2 a znaménkových konvencí (#280, #1293).

Titulky jsou parafráze typických zpráv (repo je veřejné); vzory chyb pocházejí
z analýzy #1293 (vzorek 400 zpráv, dva hodnotitelé, holdout 200).
"""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_classifications,
    news_events,
)
from gexlens_news.classification_job import RuleClassificationJob
from gexlens_news.classifier import (
    FEED_BLUESKY,
    FEED_BLUESKY_CURATED,
    FEED_CNBC,
    FEED_FED_RSS,
    FEED_MARKETWATCH,
    FEED_REDDIT,
    FEED_RSS_USER,
    FEED_YAHOO,
    classify,
    classify_category,
    classify_direction,
    clean,
    feed_of,
    headline_importance,
    scheduled_importance,
)
from gexlens_news.conventions import (
    ConventionOutcome,
    check_conventions,
    match_series,
    scheduled_direction,
)

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)
WIRE = "finnhub"


def headline(title: str, feed: str = WIRE) -> tuple[str, int]:
    result = classify(title, feed=feed, kind="headline")
    return result.category, result.importance


# ── Makro, Fed a geopolitika nesmí propadnout (45 typických titulků) ──

MACRO_CASES: list[tuple[str, str, int]] = [
    # (titulek, feed, minimální importance)
    ("Minutes of the Federal Open Market Committee, July 28-29, 2026", FEED_FED_RSS, 3),
    ("Federal Reserve issues FOMC statement", FEED_FED_RSS, 3),
    ("Waller, The Economic Outlook and Some Comments on Policy Communication", FEED_FED_RSS, 2),
    ("Fed leaves rates unchanged, signals two cuts later this year - Reuters", WIRE, 3),
    ("Fed holds rates steady as inflation stays elevated", FEED_CNBC, 3),
    ("Fed raises interest rates by a quarter point, first hike since 2023", FEED_CNBC, 3),
    ("FOMC Minutes: Officials Saw Risks To Inflation Tilted To Upside", "alpaca", 3),
    ("Fed Chair Warsh Says Policy Is Well Positioned", "alpaca", 3),
    ("Fed's Waller says he favors another rate hike in October", WIRE, 2),
    ("U.S. consumer prices rise more than expected in August - Reuters", WIRE, 3),
    ("US CPI rises 0.4% in August, core 0.3%", FEED_CNBC, 3),
    ("USA CPI (YoY) For August 3.4% Vs 3.4% Est.", "alpaca", 3),
    ("USA Core PCE Price Index (MoM) For August 0.3% Vs 0.2% Est.", "alpaca", 3),
    ("U.S. job growth slows sharply in August; unemployment rate rises to 4.3%", WIRE, 3),
    ("Nonfarm payrolls rose 162,000 in August, much more than expected", FEED_CNBC, 3),
    ("USA Nonfarm Payrolls For August 162K Vs 53K Est.", "alpaca", 3),
    ("U.S. retail sales beat expectations in August", WIRE, 3),
    ("US second-quarter GDP growth revised up to 3.3% annualized rate", WIRE, 2),  # revize = 2
    ("USA GDP Growth Rate (QoQ) For Q2 3.0% Vs 2.8% Est.", "alpaca", 3),
    ("ISM manufacturing PMI falls to 48.5 in September", WIRE, 3),
    ("USA ISM Services PMI For September 52.1 Vs 53.0 Est.", "alpaca", 3),
    ("U.S. producer prices jump 0.6% in August", WIRE, 3),
    ("Initial jobless claims rise to 250,000, highest since 2023", FEED_CNBC, 2),
    ("USA Initial Jobless Claims 197K Vs 201K Est.", "alpaca", 2),
    ("Michigan Consumer Sentiment For September 55.1 Vs 57.0 Est.", "alpaca", 2),
    ("Trump announces 25% tariffs on imports from Mexico and Canada", WIRE, 3),
    ("China retaliates with 34% tariffs on all US goods", WIRE, 3),
    ("Trump says he will impose tariffs on EU goods starting next month", FEED_CNBC, 3),
    ("Israel strikes Iran's nuclear facilities in overnight attack", WIRE, 3),
    ("Iran closes Strait of Hormuz to tankers after US strikes", WIRE, 3),
    ("Houthis attack Saudi Aramco facilities with drones", WIRE, 3),
    ("OPEC+ agrees to cut oil output by 1 million barrels per day", WIRE, 2),
    ("Oil prices surge 8% after attack on Saudi oil facilities", FEED_CNBC, 3),
    ("Russia launches invasion of Lithuania, NATO convenes emergency meeting", WIRE, 2),
    ("China conducts military drills around Taiwan, fires missiles", WIRE, 3),
    ("10-year Treasury yield jumps to 5.1% after hot CPI", FEED_CNBC, 2),
    ("Stocks tumble as Fed signals more hikes", WIRE, 2),
    ("Bank of Japan raises rates to 1.25%, highest since 1995", WIRE, 2),
    ("ECB raises interest rates, bolstering bets for further moves", WIRE, 2),
    ("Trump says Iran war will end right after U.S. midterm elections", WIRE, 3),
    ("Iran agrees to ceasefire proposal, Trump says strikes paused", WIRE, 3),
    ("Nvidia earnings beat estimates, revenue guidance tops forecasts", WIRE, 2),
    ("Treasury Secretary says US and China reach trade deal framework", WIRE, 2),
    ("US government shutdown begins after Congress fails to pass funding bill", WIRE, 2),
    ("Moody's downgrades US credit rating to Aa1", WIRE, 2),
]


@pytest.mark.parametrize(("title", "feed", "minimum"), MACRO_CASES)
def test_makro_fed_a_geopolitika_neprepadnou(title: str, feed: str, minimum: int) -> None:
    assert classify(title, feed=feed, kind="headline").importance >= minimum


def test_makro_sada_ma_45_pripadu() -> None:
    assert len(MACRO_CASES) == 45


# ── Falešné shody a zmínky → importance 1 (vzory chyb #1293) ────────


@pytest.mark.parametrize(
    ("title", "feed", "not_category"),
    [
        # `powell` bez Fedu = Colin Powell
        ("Colin Powell's former aide publishes memoir on Iraq diplomacy", WIRE, "FED"),
        # `cpi` v doméně odkazu (icc-cpi.int)
        (
            "Statement of the Prosecutor https://www.icc-cpi.int/news/statement on warrant",
            FEED_BLUESKY_CURATED,
            "MACRO_INFLATION",
        ),
        # `emergency` jako spouštěč vyřazen (počasí, soud, veterinář)
        ("Governor declares state of emergency as storm hits coast", WIRE, None),
        # sankce ve sportu a sankce obecně jsou rutina
        ("Clippers owner apologizes after league sanctions team over salary cap deal", WIRE, None),
        ("House set to pass new sanctions bill targeting shipping firms", WIRE, None),
        # `earnings` mimo firemní výsledky
        ("College graduates with these majors report the highest earnings", FEED_YAHOO, None),
        ("Students from these schools have the highest earnings ten years later", "alpaca", None),
        ("Retiring early? How the Social Security earnings test works", FEED_YAHOO, None),
        ("Acme Corp Q2 2026 Earnings Call Transcript", "alpaca", "EARNINGS"),
        # `war` jako metafora
        ("Raises and inflation are in a tug-of-war for your paycheck", FEED_MARKETWATCH, None),
        ("Price war heats up among streaming services", WIRE, "GEOPOLITICS"),
        ("Bidding war erupts for regional grocery chain", WIRE, "GEOPOLITICS"),
        # `opec` bez hranice slova (alopecia)
        ("Drugmaker reports positive alopecia trial results", "alpaca", "ENERGY"),
        # osobní finance, šablony, stock picking
        (
            "Pet insurance or a savings account: which is better for vet bills?",
            FEED_MARKETWATCH,
            None,
        ),
        ("Will S&P 500 open up or down today?", "alpaca", None),
        ("3 AI stocks to buy before the next Fed rate decision", "alpaca", None),
        # zmínka za spojkou: předmětem je cizí trh nebo krypto
        ("Indian stocks fall as Brent surges past $100", WIRE, None),
        ("Bitcoin dips as Fed hikes rates", "alpaca", "FED"),
        # názor s tématem, místní konflikt, cizí data a menší centrální banky
        ("Vance says Fed should lower rates", WIRE, None),
        ("Israeli strike kills two in Gaza, medics say", WIRE, None),
        ("Canada August CPI rises 3.0% y/y", FEED_BLUESKY_CURATED, None),
        ("Bank of Canada's governor warns growth could be halved by tariffs", "alpaca", None),
        # „close“ bez průlivu, hranice či přístavu není eskalace
        ("Chinese stocks close higher on property support", WIRE, "GEOPOLITICS"),
    ],
)
def test_falesne_shody_a_zminky_maji_importance_1(
    title: str, feed: str, not_category: str | None
) -> None:
    kind = "social" if feed.startswith("bluesky") else "headline"
    result = classify(title, feed=feed, kind=kind)
    assert result.importance == 1, result.reason
    if not_category is not None:
        assert result.category != not_category


# ── Opačný směr: ropa, Írán a Hormuz s v1 importance 1 → 3 ─────────


@pytest.mark.parametrize(
    ("title", "feed", "category"),
    [
        ("Oil surges on report Saudi pipeline repair will take weeks", WIRE, "ENERGY"),
        ("Oil prices tumble back below $100 as supply fears ease", FEED_CNBC, "ENERGY"),
        ("Iran floats conditions for reopening the Strait of Hormuz", FEED_CNBC, "GEOPOLITICS"),
        (
            "Trump rejects Tehran's seven-day ceasefire offer, expects to resume bombing Iran",
            FEED_BLUESKY_CURATED,
            "GEOPOLITICS",
        ),
        # Pohyb ropy způsobený válkou je ENERGY (ENERGY před GEOPOLITICS)
        ("Oil prices jump 4% as Houthis fire missiles at tankers", WIRE, "ENERGY"),
        (
            "Saudi Arabia shuts East-West crude oil pipeline after drone attacks",
            FEED_CNBC,
            "ENERGY",
        ),
        # Rozhodnutí Fedu ve formátu Benzinga (v1 dávalo OTHER)
        ("USA Fed Interest Rate Decision 4.00% Up From 3.75% Prior", "alpaca", "FED"),
    ],
)
def test_rezim_ropa_iran_a_fed_dostanou_3(title: str, feed: str, category: str) -> None:
    kind = "social" if feed.startswith("bluesky") else "headline"
    result = classify(title, feed=feed, kind=kind)
    assert (result.category, result.importance) == (category, 3), result.reason


# ── Předmět vs. zmínka, názor a otázka ─────────────────────────────


def test_souhrn_americkeho_trhu_nese_driver_jako_2() -> None:
    assert headline("Stocks lower ahead of FOMC decision", "ibkr_brfg") == ("FED", 2)
    assert headline_importance("Stocks lower ahead of FOMC decision")[1] == "fed_event+zmínka"


def test_otazka_a_nazor_shodi_udalost_na_2_a_tema_na_1() -> None:
    assert headline("Will the Fed hike rates next week?")[1] == 2
    assert headline("Could the Fed cut rates in December?")[1] == 2
    assert headline("Oil tops $100 again: how high can it go?")[1] == 2  # událost + otázka
    assert headline("Is inflation data about to surprise?")[1] == 1  # téma + otázka
    # Citace člena Fedu s „should“ zůstává relevantní
    assert headline("Fed's Schmid says rates should stay restrictive", "alpaca") == ("FED", 2)


def test_zastarale_opakovani_a_vyhled_nejvys_2() -> None:
    assert headline("Reported Earlier, Iran strikes tanker near Hormuz", "alpaca")[1] == 2
    assert headline("Fed Meeting Interest Rate Decision Preview: How Will Stocks React?")[1] == 2
    assert headline("Investor warns politics will get even worse if Fed hikes rates")[1] <= 2
    # „Fed for lower rates“ není akce Fedu
    assert headline("Trump pushes Fed for lower rates, but a hike may help savers")[1] <= 2


# ── Recenze #1293: měsíc, cenové úrovně, apostrofy, falešné trojky ──


@pytest.mark.parametrize(
    ("title", "feed", "importance"),
    [
        # Měsíc „May“ není modální sloveso — release z května nesmí spadnout na 2
        ("USA CPI (MoM) For May 0.3% Vs 0.2% Est.", "alpaca", 3),
        ("USA Nonfarm Payrolls For May 139K Vs 130K Est.", "alpaca", 3),
        ("U.S. retail sales rose 0.6% in May", WIRE, 3),
        ("Oil hits highest since May on Hormuz fears", WIRE, 3),
        ("Trump imposes 25% tariffs on EU goods effective May 1", WIRE, 3),
        ("Fed may cut rates in December", WIRE, 1),  # modální „may“ = názor
        ("Oil may rise as Iran tensions build", WIRE, 2),
        # Cenová úroveň nebo body před „as stocks“ nejsou stock picking
        ("Oil tops $100 as stocks slide", WIRE, 3),
        ("Brent crude hits $110 as Asian stocks slump", WIRE, 3),
        ("Wall Street loses $2 trillion as stocks crater after Iran strike", WIRE, 2),
        ("10 stocks to watch this week", WIRE, 1),
        ("5 AI stocks for the rest of 2026", WIRE, 1),
        # Typografický apostrof: vysvětlující článek agregátoru → 1, člen Fedu → 2
        ("The Fed Just Raised Rates Again: Here’s What It Means for Freight", FEED_YAHOO, 1),
        ("Fed’s Goolsbee Warns AI Data Center Boom Could Overheat Economy", "alpaca", 2),
    ],
)
def test_recenze_mesic_urovne_apostrofy(title: str, feed: str, importance: int) -> None:
    result = classify(title, feed=feed, kind="headline")
    assert result.importance == importance, result.reason


@pytest.mark.parametrize(
    ("title", "feed", "importance"),
    [
        # Firma a emise akcií, týdenní Redbook, firma s „payroll“ v názvu
        (
            "CPI Card Group Prices Underwritten Secondary Public Offering Of 2,337,323 Shares",
            "alpaca",
            1,
        ),
        ("Redbook Retail Sales Index Up 7.6% YoY For Week Ended 9/19/26", "alpaca", 1),
        ("This Payroll Software Company Bought Back 20% of Its Stock", FEED_YAHOO, 1),
        # Ropa: zpracování, ropné akcie a firmy, jedlé oleje, „oil-driven“
        ("China's August oil throughput rises 5%", WIRE, 1),
        ("Offshore Oil Stocks Rally on Iran Fears", FEED_YAHOO, 1),
        ("Oil major Shell hits record profit", WIRE, 1),
        ("Palm oil futures fall", WIRE, 1),
        ("Olive oil prices jump 20%", WIRE, 1),
        ("Oil-driven bond yield surge finally bites equities", FEED_YAHOO, 1),
        # Souhrn cizího trhu i bez spojky
        ("Indian shares decline on crude spike", WIRE, 1),
        ("Most Gulf bourses in red on Houthi attack and Hormuz shipping slump", WIRE, 1),
        # Předseda Fedu jako podmět „says“, ne bývalý, kandidát ani vložená věta
        ("Former Fed Chair Bernanke says inflation is sticky", WIRE, 2),
        ("Fed chair nominee says rates should fall", WIRE, 1),
        (
            "Fed Chair Warsh, In Remarks To G20 Meeting, Says Looking Forward To Learning More",
            "alpaca",
            2,
        ),
        ("The Fed Chair Says AI Is Moving Faster Than Expected", FEED_YAHOO, 1),
        # Sázka trhu a výhled členů nejsou akce Fedu
        ("Traders bet Fed cuts rates in December", WIRE, 2),
        ("FOMC members see two cuts this year", WIRE, 2),
        # Scénář je spekulace, ne release
        ("Elon Musk Says Anthropic’s 15% US GDP Growth Scenario Is Conservative", "alpaca", 2),
    ],
)
def test_recenze_falesne_trojky(title: str, feed: str, importance: int) -> None:
    result = classify(title, feed=feed, kind="headline")
    assert result.importance == importance, result.reason


def test_recenze_akce_fedu_a_ropa_zustavaji_3() -> None:
    assert headline("Fed Chair Warsh says rates will stay high") == ("FED", 3)
    assert headline("FOMC unexpectedly holds rates steady") == ("FED", 3)
    assert headline("Fed cuts rates; traders see Fed pausing next") == ("FED", 3)
    assert headline("Oil prices jump 5% as Iran closes Strait of Hormuz") == ("ENERGY", 3)
    assert clean("Here’s why ‘rates’ matter") == "Here's why 'rates' matter"


def test_mzdy_jsou_prace_a_jackson_hole_fed() -> None:
    """Mzdy z reportu o zaměstnanosti nejsou výsledky firem (EARNINGS by je vyřadilo
    z významných a při K1 by kontaminovaly okno NFP)."""
    from gexlens_news.reactions import contaminates

    line = classify(
        "USA Average Hourly Earnings For September 0.3% Vs 0.3% Est", feed="alpaca", kind="headline"
    )
    assert (line.category, line.importance) == ("MACRO_LABOR", 2)
    calendar = classify(
        "USD Average Hourly Earnings m/m", feed="forexfactory", kind="scheduled", ff_impact="High"
    )
    assert (calendar.category, calendar.importance) == ("MACRO_LABOR", 3)
    nfp = classify_category("USD Non-Farm Employment Change")
    assert not contaminates(nfp, line.category)
    assert classify_category("USD Employment Cost Index q/q") == "MACRO_LABOR"
    assert classify_category("ALL Jackson Hole Symposium") == "FED"
    assert classify_category("Nvidia earnings beat estimates") == "EARNINGS"


# ── Strop podle feedu ──────────────────────────────────────────────


def test_strop_socialnich_siti_bez_kuratora_a_agregatoru() -> None:
    fed = "Fed raises rates by 25 basis points"
    assert classify(fed, feed=FEED_BLUESKY, kind="social").importance == 1
    assert classify(fed, feed=FEED_REDDIT, kind="social").importance == 1
    assert classify(fed, feed=FEED_BLUESKY_CURATED, kind="social").importance == 3
    # Agregátory jen události: 3 → 2, téma 2 → 1 (rozhodnutí uživatele 26. 9.)
    assert classify(fed, feed=FEED_YAHOO, kind="headline").importance == 2
    assert classify(fed, feed=FEED_MARKETWATCH, kind="headline").importance == 2
    assert classify(fed, feed=FEED_RSS_USER, kind="headline").importance == 2
    yields = "Treasury yields climb to 5% for first time since 2007"
    assert classify(yields, feed=FEED_YAHOO, kind="headline").importance == 1
    assert classify(yields, feed=FEED_CNBC, kind="headline").importance == 2
    assert classify(fed, feed=FEED_CNBC, kind="headline").importance == 3


def test_feed_ze_zdroje_a_surového_payloadu() -> None:
    assert feed_of("bluesky", {"curated": True}) == FEED_BLUESKY_CURATED
    assert feed_of("bluesky", {"did": "did:plc:x"}) == FEED_BLUESKY
    assert feed_of("reddit_rss", {}) == FEED_REDDIT
    # Syndikované WSJ/Barron's přes Yahoo rssindex: rozhoduje URL feedu, ne odkaz
    yahoo = {"feed": "https://finance.yahoo.com/news/rssindex", "link": "https://www.wsj.com/x"}
    assert feed_of("rss_news", yahoo) == FEED_YAHOO
    marketwatch = {"feed": "https://feeds.content.dowjones.io/public/rss/mw_topstories"}
    assert feed_of("rss_news", marketwatch) == FEED_MARKETWATCH
    assert feed_of("rss_news", {"feed": "https://www.cnbc.com/id/1/device/rss"}) == FEED_CNBC
    assert feed_of("rss_news", {"link": "https://www.cnbc.com/2026/09/x.html"}) == FEED_CNBC
    assert feed_of("rss_user", {"feed": "https://example.org/rss"}) == FEED_RSS_USER
    assert feed_of("alpaca", None) == "alpaca"


# ── Kalendář FF a Fed RSS ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "impact", "importance"),
    [
        ("USD CPI m/m", "High", 3),
        ("USD PPI m/m", "high", 3),  # v1 regex dával High PPI importance 1
        ("USD Retail Sales m/m", "Medium", 2),
        ("USD FOMC Member Speaks", "Low", 1),  # v1 regex dával Low importance 3
        ("ALL Jackson Hole Symposium", "Medium", 2),
        ("GBP CPI y/y", "High", 1),
        ("CAD Employment Change", "High", 1),
        ("EUR German Prelim CPI m/m", "High", 1),
        ("EUR Main Refinancing Rate", "High", 2),
        ("EUR ECB Press Conference", "High", 2),
        ("GBP Official Bank Rate", "High", 2),
        ("JPY BOJ Policy Rate", "High", 2),
        ("JPY Monetary Policy Statement", "High", 2),
        ("GBP Monetary Policy Report Hearings", "High", 1),
        ("EUR Main Refinancing Rate", "Medium", 1),
        ("USD CPI m/m", None, 1),
    ],
)
def test_kalendar_podle_meny(title: str, impact: str | None, importance: int) -> None:
    assert scheduled_importance(title, impact) == importance
    assert classify(title, feed="forexfactory", kind="scheduled", ff_impact=impact).importance == (
        importance
    )


def test_fed_rss_statement_3_projevy_2() -> None:
    statement = classify(
        "Federal Reserve issues FOMC statement", feed=FEED_FED_RSS, kind="headline"
    )
    assert (statement.category, statement.importance) == ("FED", 3)
    projections = classify(
        "Federal Reserve Board and Federal Open Market Committee release economic "
        "projections from the September 15-16 FOMC meeting",
        feed=FEED_FED_RSS,
        kind="headline",
    )
    assert projections.importance == 3
    speech = classify("Barr, A Long-Term View on Housing Costs", feed=FEED_FED_RSS, kind="headline")
    assert (speech.category, speech.importance) == ("FED", 2)


# ── Kategorie, čištění, směr ───────────────────────────────────────


def test_kategorie_z_predmetu_titulku() -> None:
    assert classify_category("FOMC Statement") == "FED"
    assert classify_category("Core PCE Price Index m/m") == "MACRO_INFLATION"
    assert classify_category("Crude Oil Inventories") == "ENERGY"
    # Holé „inflation“ v souhrnu trhu není MACRO_INFLATION
    assert classify_category("Gold edges higher as inflation worries linger") == "OTHER"
    assert classify_category("Company announces new logo") == "OTHER"
    # Bot „reported earnings today“ není EARNINGS
    assert classify_category("$XYZ reported earnings today") != "EARNINGS"


def test_cisteni_titulku() -> None:
    assert clean("S&amp;P 500 <b>rises</b> - Reuters") == "S&P 500 rises"
    assert clean("Inflation outta control submitted by /u/Existing_Inflation17") == (
        "Inflation outta control"
    )
    assert "icc-cpi" not in clean("See https://www.icc-cpi.int/news and icc-cpi.int/x")


def test_shrnuti_o_dulezitosti_nerozhoduje() -> None:
    """v2 čte jen titulek — spouštěč ve shrnutí byl relevantní ve 14 % případů."""
    result = classify("Update from the central bank", feed=WIRE, kind="headline")
    assert (result.category, result.importance) == ("OTHER", 1)


def test_direction_from_phrases() -> None:
    assert classify_direction("Stocks surge as earnings beat estimates")[0] == 1
    assert classify_direction("Nasdaq plunges after guidance miss")[0] == -1
    # Geopolitická eskalace je risk-off bez ohledu na sloveso
    assert classify_direction("Russia launches missile attack")[0] == -1
    assert classify_direction("Company publishes annual report")[0] == 0


def test_mixed_signals_do_not_pretend_certainty() -> None:
    """Protichůdné signály: buď nula, nebo aspoň snížená síla."""
    balanced = classify_direction("Stocks fall as chipmakers beat estimates")
    assert balanced == (0, 0.0)

    leaning = classify(
        "Stocks drop, slide and tumble though earnings beat", feed=WIRE, kind="headline"
    )
    assert leaning.direction == -1
    assert leaning.strength < 0.4  # snížená proti jednoznačnému případu


# ── Znaménkové konvence ────────────────────────────────────────────


def test_scheduled_direction_uses_series_convention() -> None:
    # Vyšší inflace než konsensus = risk-off
    assert scheduled_direction("USD CPI m/m", 1.5) == -1
    assert scheduled_direction("USD CPI m/m", -1.5) == 1
    # Silnější payrolls = risk-on
    assert scheduled_direction("USD Non-Farm Employment Change", 2.0) == 1
    # Vyšší nezaměstnanost = risk-off
    assert scheduled_direction("USD Unemployment Rate", 1.0) == -1
    # Překvapení přesně na konsensu směr nedává
    assert scheduled_direction("USD CPI m/m", 0.0) == 0
    # Neznámá řada nebo chybějící překvapení → None, ne tipování
    assert scheduled_direction("AUD Building Approvals", 1.0) is None
    assert scheduled_direction("USD CPI m/m", None) is None


def test_match_series_prefers_specific() -> None:
    assert match_series("USD Unemployment Rate") is not None
    assert match_series("USD Unemployment Rate").sign == -1  # type: ignore[union-attr]
    assert match_series("USD Non-Farm Employment Change").sign == 1  # type: ignore[union-attr]


def test_check_conventions_flags_series_that_stopped_working() -> None:
    """„Good news is bad news": konvence se může celé měsíce mýlit — musí se to poznat."""
    # Řada predikovala +1, ale trh šel 8× z 10 dolů
    wrong = [ConventionOutcome("zaměstnanost", 1, -5.0) for _ in range(8)]
    wrong += [ConventionOutcome("zaměstnanost", 1, 5.0) for _ in range(2)]
    ok = [ConventionOutcome("inflace", -1, -3.0) for _ in range(12)]

    checks = {c.series: c for c in check_conventions(wrong + ok)}
    assert checks["zaměstnanost"].suspicious
    assert checks["zaměstnanost"].hit_rate == pytest.approx(0.2)
    assert not checks["inflace"].suspicious
    assert checks["inflace"].hit_rate == pytest.approx(1.0)


def test_small_sample_is_not_flagged() -> None:
    """Tři minutí konvenci nevyvrátí — jinak by se flagovalo pořád."""
    few = [ConventionOutcome("růst", 1, -1.0) for _ in range(3)]
    assert not check_conventions(few)[0].suspicious


def test_outcomes_without_direction_are_ignored() -> None:
    assert check_conventions([ConventionOutcome("inflace", 0, 5.0)]) == []


# ── Job ────────────────────────────────────────────────────────────


def add_event(engine, **values) -> int:  # type: ignore[no-untyped-def]
    payload = {
        "ts_event": NOW,
        "ts_ingested": NOW,
        "source": "rss_news",
        "kind": "headline",
        "symbols": [],
        "market_closed": False,
        "raw": {},
    }
    payload.update(values)
    with engine.begin() as conn:
        key = conn.execute(insert(news_events).values(**payload)).inserted_primary_key
    assert key is not None
    return int(key[0])


def test_job_writes_version_one_and_denormalises(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    add_event(engine, title="Nasdaq plunges as chip guidance misses", dedup_hash="a")

    job = RuleClassificationJob(engine)
    assert job.run(NOW) == 1

    with engine.connect() as conn:
        cls = conn.execute(select(news_classifications)).fetchone()
        event = conn.execute(select(news_events)).fetchone()
    assert cls is not None and event is not None
    assert cls.version == 1
    assert cls.source == "rule"
    assert cls.direction == -1
    # Denormalizace do news_events pro rychlé čtení feedu
    # „guidance miss" je EARNINGS — specifičtější vzor vyhrává nad TECH
    assert event.category == "EARNINGS"
    assert event.sentiment_dir == -1
    assert event.sentiment_source == "rule"
    assert event.sentiment_score is not None and event.sentiment_score < 0

    # Dávka pro WS push (#335) nese celý řádek, ne jen kategorii — UI z ní
    # skládá feed bez dalšího dotazu
    assert len(job.last_batch) == 1
    pushed = job.last_batch[0]
    assert pushed["category"] == "EARNINGS"
    assert pushed["sentiment_dir"] == -1
    assert pushed["title"] == "Nasdaq plunges as chip guidance misses"
    assert isinstance(pushed["ts_event"], str)

    # Opakovaný běh nepřepisuje ani nepřidává druhou pravidlovou verzi
    assert job.run(NOW) == 0
    # …a nesmí pushnout starou dávku znovu
    assert job.last_batch == []


def test_scheduled_event_takes_direction_from_convention(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    add_event(
        engine,
        title="USD CPI m/m",
        kind="scheduled",
        source="forexfactory",
        surprise_z=2.0,
        dedup_hash="cpi",
    )

    RuleClassificationJob(engine).run(NOW)

    with engine.connect() as conn:
        event = conn.execute(select(news_events)).fetchone()
    assert event is not None
    # Titulek „USD CPI m/m" sám směr nenese; konvence + překvapení dají risk-off
    assert event.sentiment_dir == -1
    assert event.category == "MACRO_INFLATION"


def test_scheduled_importance_from_ff_impact_not_regex(tmp_path: Path) -> None:
    """Kalendář: importance z FF impactu podle měny (živý `impact` i backfill `impactName`)."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    ppi = add_event(
        engine,
        title="USD PPI m/m",
        kind="scheduled",
        source="forexfactory",
        raw={"impact": "High", "country": "USD"},
        dedup_hash="ppi",
    )
    speaks = add_event(
        engine,
        title="USD FOMC Member Speaks",
        kind="scheduled",
        source="forexfactory",
        raw={"impactName": "low"},
        dedup_hash="speaks",
    )
    cad = add_event(
        engine,
        title="CAD CPI m/m",
        kind="scheduled",
        source="forexfactory",
        raw={"impact": "High"},
        dedup_hash="cad",
    )

    job = RuleClassificationJob(engine)
    job.run(NOW)

    with engine.connect() as conn:
        rows = conn.execute(select(news_events.c.id, news_events.c.importance)).all()
    importance = {int(row.id): row.importance for row in rows}
    assert importance == {ppi: 3, speaks: 1, cad: 1}
    # WS dávka nese stupeň významnosti ze sdílené definice (#1305)
    significance = {row["id"]: row["significance"] for row in job.last_batch}
    assert significance == {ppi: 0, speaks: None, cad: None}


def test_fed_rss_is_not_overwritten_by_headline_rules(tmp_path: Path) -> None:
    """Projev člena Fedu z fed_rss dostával v1 OTHER/1 — v2 FED/2."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    add_event(
        engine,
        title="Jefferson, Discount Window Modernization and Treasury Market Functioning",
        source="fed_rss",
        dedup_hash="speech",
    )

    RuleClassificationJob(engine).run(NOW)

    with engine.connect() as conn:
        event = conn.execute(select(news_events)).fetchone()
    assert event is not None
    assert (event.category, event.importance) == ("FED", 2)


def test_feed_cap_from_raw_payload(tmp_path: Path) -> None:
    """Strop feedu: Bluesky bez kurátora 1, s kurátorem beze stropu, Yahoo jen události."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    title = "Fed raises rates by 25 basis points"
    crowd = add_event(
        engine, title=title, source="bluesky", kind="social", raw={"did": "x"}, dedup_hash="1"
    )
    curated = add_event(
        engine,
        title=title,
        source="bluesky",
        kind="social",
        raw={"did": "y", "curated": True},
        dedup_hash="2",
    )
    yahoo = add_event(
        engine,
        title=title,
        raw={"feed": "https://finance.yahoo.com/news/rssindex", "link": "https://www.wsj.com/a"},
        dedup_hash="3",
    )
    cnbc = add_event(
        engine,
        title=title,
        raw={"feed": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
        dedup_hash="4",
    )

    job = RuleClassificationJob(engine)
    job.run(NOW)

    with engine.connect() as conn:
        rows = conn.execute(select(news_events.c.id, news_events.c.importance)).all()
    importance = {int(row.id): row.importance for row in rows}
    assert importance == {crowd: 1, curated: 3, yahoo: 2, cnbc: 3}
    significance = {row["id"]: row["significance"] for row in job.last_batch}
    assert significance == {crowd: None, curated: 2, yahoo: 3, cnbc: 2}
