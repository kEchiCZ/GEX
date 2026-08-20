"""Konfigurační vrstva enginu (SPEC kap. 3 a 5) — Pydantic Settings nad .env.

Engine s nevalidní konfigurací odmítá nastartovat: `load_settings` vyhodí
`ConfigError` se srozumitelným výpisem všech chybných položek.
"""

import datetime as dt
import logging
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    # Heartbeat spojení: interval a timeout odpovědi TWS. Timeout musí snést
    # zpoždění během sweep dávek (80 souběžných subskripcí) — 5 s bylo málo
    # a spojení se zbytečně recyklovalo
    heartbeat_interval_s: float = Field(default=30.0, gt=0)
    heartbeat_timeout_s: float = Field(default=15.0, gt=0)
    # Jak dlouho smí spojení viset mimo stav connected, než se to ohlásí jako
    # porucha (#770). 18. 8. byl engine osm hodin offline a nikde to nezaznělo —
    # výpadek se poznal až tím, že si člověk všiml zamrzlého grafu. Práh je
    # velkorysý: běžný restart TWS je otázka desítek sekund, denní údržba IBKR
    # (23:45–00:45 ET) taky nemá trvat déle než pár minut.
    reconnect_stall_alert_s: float = Field(default=300.0, gt=0)

    # Instrumenty (ADR-0003): základní sada futures podkladů; watchlist z DB se přidává za běhu
    symbols: str = "ES"
    # Strop souběžně běžících instrumentů (market data lines rozpočet, ADR-0001/0003)
    max_instruments: int = Field(default=3, ge=1)
    # Watchlist z DB se čte každý k-tý minutový cyklus
    watchlist_poll_cycles: int = Field(default=5, ge=1)
    # Ranní OI archiv pokrývá N nejbližších expirací (ΔOI vs. včera potřebuje
    # stejný kontrakt archivovaný ve dvou dnech — 0DTE řetěz jinak srovnání nemá)
    oi_archive_expiries: int = Field(default=5, ge=1)
    # Sweep i následující expirace (čtení positioningu příští seance na konci dne);
    # běží v nižší kadenci — každý k-tý minutový cyklus
    sweep_next_expiry: bool = True
    next_expiry_sweep_every: int = Field(default=3, ge=1)
    # Setup detektor (ADR-0004) — obchodní analýzy s auto-vyhodnocováním
    # Živé broker headlines přes generic tick 292 (#291, Tier D). Stojí jednu
    # market data line na symbol — proti rezervě 80/≥150 (ADR-0001) nic.
    ibkr_news_enabled: bool = True
    setups_enabled: bool = True
    # Minutový feature log (#796): vstupní vektor detektoru + ATR + band metriky
    # do derived/{symbol}/features/ — trénovací matice samoučící smyčky (#794).
    # Objem ~1 řádek/min/symbol (jednotky KB/den), partice mimo retenci (ADR-0029)
    feature_log_enabled: bool = True
    # Indikátor tendence (#350) — minutový výpočet + zápis historie
    tendency_enabled: bool = True
    # Sběrač kandidátů T6 Premarket squeeze (#256) — jen sběr, žádný setup
    t6_collector_enabled: bool = True
    # Gamma útes po expiraci (#576, fáze 1 jen měření)
    gamma_cliff_enabled: bool = True
    # Cenové alerty na úrovně (#675): práh přiblížení v násobcích kroku striků
    # (škáluje per symbol — ES krok 5 b, NQ širší); 0 = vypnuto
    level_alert_near_steps: float = Field(default=1.0, ge=0)
    # Cooldown per úroveň; po vystřelení navíc musí cena zónu opustit
    # (re-arm hystereze), jinak by alert pálil každou minutu konsolidace u zdi
    level_alert_cooldown_s: float = Field(default=900.0, gt=0)
    t6_trigger_pct: float = -1.0
    # Minimální dominance zdi pro šablony T1/T3 (ADR-0010, #223)
    setup_min_wall_dominance: float = Field(default=0.15, ge=0, le=1)
    # Kontra-režimový filtr (#252 B+C): okno CumΔ konfluence pro fade proti
    # gammě a delší cooldown šablony po stopu kontra-setupu
    setup_counter_flow_lookback: int = Field(default=30, ge=1)
    setup_counter_stop_cooldown_minutes: int = Field(default=45, ge=0)
    # Vypnuté šablony (#303): čárkami oddělené kódy; prázdné = běží všechny.
    # T5 divergence_spring je default vypnutá (8,7 % úspěšnost, Ø −0,69R)
    setup_disabled_templates: str = "divergence_spring"
    # R-mechanika setupů (#302): minimální risk v násobcích ATR (stop pod ním je
    # v minutovém šumu) a strop vzdálenosti cíle v násobcích risku (dál = cíl
    # nedosažitelný, vždy se trefí dřív stop)
    setup_min_risk_atr: float = Field(default=2.0, gt=0)
    setup_max_rr: float = Field(default=3.0, gt=0)
    # Strop pokusů per směr: po N stopech v řadě se směr na X minut zablokuje
    setup_max_stops_per_direction: int = Field(default=3, ge=1)
    setup_direction_block_minutes: int = Field(default=90, ge=0)
    # Sebekontrola detektoru (#309): denní vyhodnocení klouzavého okna a alert
    # při propadu. 27. 7. detektor týden ztrácel (−43,5R) a nikdo si toho
    # nevšiml — stránka Setupy čísla ukazovala, ale nic nekřičelo.
    setup_selfcheck_days: int = Field(default=7, ge=1)
    setup_selfcheck_min_samples: int = Field(default=10, ge=1)
    setup_selfcheck_max_drawdown_r: float = Field(default=-10.0, lt=0)
    # Flow-adjusted OI odhad (ADR-0011, #222): OI_est = OI + α·čistý klasifikovaný
    # objem. α z validace open-ratio (~0,39 na čistém dni); 0 = vrstva vypnutá
    flow_oi_alpha: float = Field(default=0.4, ge=0, le=1)
    # Vol leadeři (#208): alert, když top strana příští expirace ≥ ratio × medián
    # top-10 a zároveň ≥ min_volume kontraktů (absolutní podlaha proti ránu)
    vol_leader_ratio: float = Field(default=3.0, gt=0)
    vol_leader_min_volume: float = Field(default=500.0, ge=0)
    # GEX žebřík (#244): top-N významných striků per strana, filtr podílu na síle
    ladder_top_n: int = Field(default=3, ge=1)
    ladder_min_share: float = Field(default=0.1, ge=0, le=1)

    # Opční řetězec a rotační scheduler (SPEC 3.2, 3.3)
    strike_range_points: float = Field(default=200.0, gt=0)
    # Auto-rozšíření obálky, když se spot přiblíží k okraji na < tento podíl šířky
    strike_range_expand_threshold: float = Field(default=0.25, gt=0, lt=1)
    # Strop šířky denní obálky strikes (ADR-0002); při dosažení se obálka posouvá
    strike_range_max_points: float = Field(default=800.0, gt=0)
    batch_size: int = Field(default=80, ge=1)

    # tastytrade větev (#613, M7): OAuth session, DXLink stream, chain mapa,
    # křížová kontrola (#517 A), oba fallbacky (#614) a doplnění OI (#664).
    # TRVALÁ součást provozu, proto default `true` — bez tajemství se stejně
    # nespustí, takže nic nezapíná omylem.
    tasty_enabled: bool = True
    # Zápis porovnávacích řádků do `feed_comparison` (#613). DOČASNÉ: vypne se
    # po vyhodnocení M7 fáze 2. Tally pro detektor a fallbacky běží dál (#763).
    tasty_comparison_write: bool = True
    # Zastaralý společný vypínač (#763). Hlídal měření i fallbacky naráz, takže
    # „vypínám doběhnuté měření" tiše bralo odolnost proti výpadku IBKR.
    # `None` = nenastaveno; když nastaven, řídí `tasty_enabled` (viz validátor).
    tasty_shadow: bool | None = None
    tasty_client_secret: str = ""
    tasty_refresh_token: str = ""
    # Doplnění chybějícího OI z tasty Summary (#664, předsunutý kus #614 dle
    # rozhodnutí uživatele 15. 8.): nastupuje JEN tam, kde denní archiv mlčí
    # (typicky 0DTE do publikace CME) — hodnota IBKR má vždy přednost.
    # Vyžaduje běžící shadow větev (bez ní není z čeho číst).
    tasty_oi_fill: bool = True
    # Záznam surových TimeAndSale printů opčního řetězu (#795): učicí data pro
    # samoučící smyčku (#794) a klasifikaci agresora (#615). Jediná data, která
    # dnes nenávratně mizí — proto default zapnuto; podklad se nezaznamenává
    # (miliony printů/den, CumΔ podkladu nese IBKR větev).
    tasty_trades_record: bool = True
    # Dev laboratoř jen s tastytrade (#623, start-dev.ps1 -LiveTasty): engine
    # přeskočí IBKR úplně a jen streamuje chain do cache s heartbeat logem.
    # Produkce se flagu nedotýká — default vypnuto.
    tasty_only: bool = False
    # Strop subskribovaných streamer symbolů; 0 = bez stropu (produkce jede na
    # maximum, ADR-0027: 6 008 změřeno bez degradace). Konzervativní hodnotu
    # nastavuje jen dev (-LiveTasty), aby experimenty neujídaly kapacitu účtu.
    tasty_max_subscriptions: int = Field(default=0, ge=0)
    # Křížová kontrola feedů (#517 fáze A): pasivní detektor nad shadow daty,
    # žádný request navíc. Bez běžící shadow větve se tiše nezapne.
    # Prahy jsou MĚŘENÉ na 3 016 minutách shadow historie (13.–16. 8. 2026):
    # sweep rotace vyhodí podíl na ~58 % každou třetí minutu, série tří minut
    # v řadě nenastala ani jednou — proto 0,70 / 3 min.
    # Spot fallback na tasty (#614): když IBKR přestane posílat ticky (mobil
    # přetáhl market data — error 10197, nebo výpadek farmy), přebírá cenu
    # podkladu tastytrade. Bez toho zamrzne cenový graf, aniž by cokoli spadlo.
    tasty_spot_fallback: bool = True
    # Jak dlouho smí chybět IBKR tick, než se sáhne po tasty
    tasty_spot_stale_after_s: float = Field(default=30.0, gt=0)
    # Jak dlouho musí IBKR souvisle dodávat, než se převezme zpět (hystereze
    # proti blikání zdroje při kolísavém spojení)
    tasty_spot_recover_after_s: float = Field(default=60.0, gt=0)
    # Max stáří tasty kotace, aby se dala použít jako spot
    tasty_spot_max_age_s: float = Field(default=30.0, gt=0)
    # Fallback CELÉHO opčního řetězu (#614 fáze 2b): fáze 2a zachránila cenu,
    # ale heatmapa i GEX stojí na řetězu — bez tohohle graf při výpadku IBKR
    # pořád zamrzne, jen se pod ním hýbe cena. Spouští ho verdikt křížové
    # kontroly (#517 A), takže dědí její MĚŘENÉ prahy místo nových odhadů.
    tasty_chain_fallback: bool = True
    # Kolik čistých minut v řadě vrátí řetěz zpět na IBKR. Delší než zapínací
    # série (crosscheck_minutes): přepnutí zdroje překreslí celý profil, takže
    # kmitání stojí víc než o pár minut pozdější návrat.
    tasty_chain_recover_minutes: int = Field(default=5, ge=1)
    # Max stáří tasty hodnoty, aby kontrakt vstoupil do fallbackového řetězu
    tasty_chain_max_age_s: float = Field(default=120.0, gt=0)

    # Jak dlouho se při startu čeká na IBKR, než engine rozjede zbytek i bez něj
    # (#756). Nejde o timeout spojení — supervisor se pokouší dál; jen se za
    # čekáním nesmí zaseknout tastytrade větev a spot fallback, které IBKR
    # nepotřebují. Po startu Windows nebývá TWS spuštěná vůbec.
    startup_connect_wait_s: float = Field(default=60.0, ge=0)

    crosscheck_enabled: bool = True
    crosscheck_share_threshold: float = Field(default=0.70, gt=0, le=1)
    crosscheck_minutes: int = Field(default=3, ge=1)
    crosscheck_cooldown_minutes: int = Field(default=15, ge=1)
    # Rozlišovač „tichý trh × mrtvá záloha" (#764): podíl kontraktů s měnícími
    # se IBKR hodnotami, od kterého je trh živý a mlčící tasty porucha. Měřeno
    # nad 4 833 min historie: pauza CME má medián ~0, aktivní trh p5 ≈ 0,38 —
    # default 0,30 dal na celé historii 0 planých poplachů.
    crosscheck_change_threshold: float = Field(default=0.30, gt=0, le=1)
    # Aktivní IBKR sonda (#517 fáze B): na alert `ibkr_suspect` jednorázový
    # snapshot referenčního kontraktu → rozliší výpadek farmy od potichu
    # mrtvých subskripcí (a ty rovnou cíleně obnoví). Neběží periodicky.
    probe_enabled: bool = True

    batch_timeout_s: float = Field(default=4.0, gt=0)
    # Burzovní čas, po kterém IBKR publikuje kompletní denní OI (#463, #511).
    # Snímek pořízený dřív nese předpublikační čísla — engine ho po tomto čase
    # povinně obnoví a teprve dvě shodná čtení bere jako finální. Naměřeno
    # 4. 8. 2026: publikace dorazila 12:45–14:00 SELČ (10:45–12:00 UTC), tedy
    # do 7:00 chicagského času. CME publikuje podle svého času — fixní UTC
    # hodina by se přes DST půl roku míjela o hodinu (#511); default odpovídá
    # dřívějším 12:00 UTC v letním čase.
    oi_publication_time_local: dt.time = dt.time(7, 0)
    oi_publication_tz: str = "America/Chicago"
    # DEPRECATED (#511): stará fixní UTC hodina. Je-li nastavená, má přednost
    # (zpětná kompatibilita .env) a při startu se zaloguje deprecation warning.
    oi_publication_hour_utc: int | None = Field(default=None, ge=0, le=23)
    # Finalita OI snímku (#664): dvě shodná čtení smí snímek prohlásit za
    # finální jen při pokrytí aspoň tohoto podílu řetězce. 12. 8. čtyři
    # kontrakty ze 160 dvakrát shodně přečtené zastavily obnovu na celý den.
    oi_final_min_coverage: float = Field(default=0.9, gt=0, le=1)
    # Křídla řetězce se sweepují každý k-tý cyklus (ATM±atm_sweep_width každý cyklus)
    wings_sweep_every: int = Field(default=3, ge=1)
    atm_sweep_width: int = Field(default=30, ge=1)
    # Max pokusů repair fronty na kontrakt za sweep, pak stale označení
    repair_max_attempts: int = Field(default=3, ge=1)
    # Repair backoff per kontrakt (#547): odklad po k. neúspěšném kole je
    # 0 → base → 2·base → … → strop. Bez něj mlel repair trvale vadné kontrakty
    # à 4 s celé hodiny (pacing zátěž bez jediného úspěchu — 7. 8. ATM pásmo NQ).
    repair_backoff_base_s: float = Field(default=4.0, gt=0)
    repair_backoff_max_s: float = Field(default=300.0, gt=0)
    # Po N neúspěšných repair kolech alert strikes_stalled (hint: restart TWS)
    repair_stall_rounds: int = Field(default=10, ge=1)
    # Fallback vlastních greeks (#547): po N sweepech s živými kotacemi bez TWS
    # modelGreeks se greeks dopočítají BS modelem z mid ceny (IV inverzí)
    greeks_fallback_sweeps: int = Field(default=3, ge=1)
    # Kapacita market data lines účtu (ADR-0001: naměřeno ≥ 150; default konzervativní)
    market_data_lines: int = Field(default=100, ge=1)
    # Maximální stáří kotace použitelné pro výpočty (#306). Zmrzlá cache se
    # nesmí započítat do GEX, úrovní ani Dyn GEX profilu — 27. 7. servírovala
    # 15 h staré ATM Greeks a zdi/flip/Max Pain se z nich celý den počítaly.
    # Řádek snapshotu se zapíše dál, ale se skutečným stářím (ne sentinelem),
    # aby bylo zpětně poznat, co bylo čerstvé.
    quote_max_age_s: float = Field(default=900.0, gt=0)
    # Alert greeks_stalled: podíl stale kontraktů nad prahem po N sweepech
    greeks_stall_share: float = Field(default=0.1, gt=0, le=1)
    greeks_stall_cycles: int = Field(default=3, ge=1)

    # Tichá ztráta 5s barů (#221): alert po N minutách bez baru při živém spotu
    bars_stall_alert_minutes: int = Field(default=3, ge=1)

    # Chyby subskripce market data (#417): alert až při shluku error 354 v okně —
    # jednotlivý výskyt je přechodný výpadek farmy, ne chybějící subskripce.
    # Alert jde vypnout za běhu klíčem `subscription_alert_enabled` v Settings UI.
    subscription_error_threshold: int = Field(default=5, ge=1)
    subscription_error_window_s: float = Field(default=60.0, gt=0)
    subscription_error_cooldown_s: float = Field(default=900.0, ge=0)
    # Konkurenční relace (#495): error 10197 chodí při přetahované session
    # ~2× za minutu (naměřeno 4. 8., viz connection.py) — sdílený práh 5/60 s
    # by se nikdy nenaplnil a alert competing_session by se neodpálil
    competing_session_threshold: int = Field(default=2, ge=1)
    competing_session_window_s: float = Field(default=120.0, gt=0)

    # Hot zóna (SPEC 3.4; limit streamů naměřen v ADR-0001)
    hot_zone_width: int = Field(default=15, ge=1)
    tick_by_tick_max_streams: int = Field(default=5, ge=1)

    # Storage a retence (SPEC kap. 5)
    # PostgreSQL DSN (OI archiv, metadata); default odpovídá docker compose dev instanci
    database_url: str = "postgresql+psycopg://gexlens:gexlens@localhost:5432/gexlens"
    data_dir: Path = Path("data")
    # Okno purge partic (ADR-0022: 90 dní, odchylka od R3). Řídí UŽ JEN mazání —
    # rozsah startovního backfillu má vlastní `bars_backfill_days`, jinak by
    # zvětšení retence vyžádalo desítky historických dotazů při každém startu.
    retention_days: int = Field(default=90, ge=1)
    # Kolik dní barů dotáhnout při startu (SPEC 3.6). Záměrně nezávislé na
    # retenci: backfill nepřeskakuje dny, které už na disku jsou, takže širší
    # okno = víc dotazů na IBKR při každém restartu.
    bars_backfill_days: int = Field(default=14, ge=0)
    # Věčný archiv 1min barů podkladu (SentimentLens S4, #275): výjimka z retence
    # jako u OI archivu. Bez něj nejde volume z-score (20 seancí > 14denní okno)
    # ani zpětný přepočet reakcí na zprávy; objem desítky MB/rok.
    keep_bars_forever: bool = True
    # Věčný archiv učicích dat (#762, ADR-0029): snapshots/ a derived/ se nemažou —
    # jsou nenahraditelné (IBKR je zpětně nedá) a samoučící smyčka (#794) se nad
    # nimi učí replayem. Objem ~6 GB/rok (změřeno: 491 MB za měsíc ES+NQ).
    # Retence pak maže jen dopočitatelné řady (ticks/). Vypnutelné jako u barů.
    keep_learning_data_forever: bool = True
    # Keep-forever režim (ADR-0029) roste ~6 GB/rok — limit je alert na revizi
    # (komprese starých partic / větší disk), skutečné volné místo hlídá #773.
    disk_limit_gb: float = Field(default=20.0, gt=0)
    # Dohled nad volným místem (#773) — jen měření a alerty, úklid řeší #757.
    # Prahy na SKUTEČNÉM volném místě disku s datovým adresářem (bind mount
    # ukazuje čísla hostitele); velikost DB má vlastní práh, protože WSL disk
    # s PostgreSQL volume z kontejneru změřit nejde; na tomhle stroji leží
    # vhdx na D: vedle dat (růst tak ukusuje z měřeného volného místa přímo),
    # práh na DB je časná výstraha na tahouna růstu.
    disk_free_warn_gb: float = Field(default=15.0, gt=0)
    disk_free_crit_gb: float = Field(default=5.0, gt=0)
    db_size_alert_gb: float = Field(default=4.0, gt=0)
    # Čas nočního purge jobu (UTC, po zavření US seance)
    retention_purge_time_utc: dt.time = dt.time(21, 30)

    @model_validator(mode="after")
    def _validate_backoff(self) -> "Settings":
        if self.reconnect_backoff_max_s < self.reconnect_backoff_base_s:
            raise ValueError("reconnect_backoff_max_s musí být ≥ reconnect_backoff_base_s")
        if self.repair_backoff_max_s < self.repair_backoff_base_s:
            raise ValueError("repair_backoff_max_s musí být ≥ repair_backoff_base_s")
        try:
            ZoneInfo(self.oi_publication_tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"oi_publication_tz musí být platná IANA zóna (zadáno: {self.oi_publication_tz!r})"
            ) from exc
        if self.tasty_shadow is not None:
            # Zpětná kompatibilita (#763): starý flag dál platí, ale řídí jen
            # trvalou větev. Zápis porovnání má nově vlastní vypínač, jinak by
            # se s vypnutým měřením ztratily i fallbacky.
            self.tasty_enabled = self.tasty_shadow
            logging.getLogger(__name__).warning(
                "GEXLENS_TASTY_SHADOW je zastaralé (#763) — hlídalo i fallbacky "
                "z #614, takže jeho vypnutím se tiše ztrácela odolnost proti "
                "výpadku IBKR. Použij GEXLENS_TASTY_ENABLED (trvalá větev) "
                "a GEXLENS_TASTY_COMPARISON_WRITE (dočasné měření). "
                "Zatím se z něj přebírá tasty_enabled=%s",
                self.tasty_enabled,
            )
        if self.oi_publication_hour_utc is not None:
            logging.getLogger(__name__).warning(
                "GEXLENS_OI_PUBLICATION_HOUR_UTC je zastaralé (#511) — použij "
                "GEXLENS_OI_PUBLICATION_TIME_LOCAL + GEXLENS_OI_PUBLICATION_TZ "
                "(burzovní čas, DST-korektní); fixní UTC hodina zatím platí dál"
            )
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
    def setup_disabled_template_set(self) -> frozenset[str]:
        """Vypnuté šablony setupů z GEXLENS_SETUP_DISABLED_TEMPLATES (#303)."""
        return frozenset(
            raw.strip().lower() for raw in self.setup_disabled_templates.split(",") if raw.strip()
        )

    def oi_publication_utc(self, day: dt.date) -> dt.datetime:
        """UTC okamžik publikačního okna OI pro den `day` (#511).

        Default je burzovní čas (`oi_publication_time_local` v `oi_publication_tz`,
        DST řeší zoneinfo); zastaralý klíč `oi_publication_hour_utc` má z důvodu
        zpětné kompatibility přednost.
        """
        if self.oi_publication_hour_utc is not None:
            return dt.datetime.combine(day, dt.time(self.oi_publication_hour_utc, 0), tzinfo=dt.UTC)
        local = dt.datetime.combine(
            day, self.oi_publication_time_local, tzinfo=ZoneInfo(self.oi_publication_tz)
        )
        return local.astimezone(dt.UTC)

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def ticks_dir(self) -> Path:
        return self.data_dir / "ticks"

    @property
    def derived_dir(self) -> Path:
        return self.data_dir / "derived"

    @property
    def trades_dir(self) -> Path:
        """Surové opční trady z dxFeed (#795) — mimo retenci (ADR-0029)."""
        return self.data_dir / "trades"


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
