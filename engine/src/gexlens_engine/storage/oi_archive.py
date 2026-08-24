"""OI archiv (SPEC 3.5 + R4): EOD/ranní snapshot řetězce do PostgreSQL, navždy.

Tabulka `oi_eod` se NIKDY nemaže (R4) — repository záměrně nenabízí žádné delete
API a RetentionJob (issue #12) se jí nesmí dotknout. Zápis je idempotentní upsert
přes primární klíč (symbol, expiry, trading_class, strike, right, date) —
trading_class od #736 rozlišuje weekly/daily série (\"\" = souhrn/legacy).

Od #519 archiv nese vedle OI i denní IV, model greeks, závěrečnou prémii
a referenční spot — kontrakt je při ranním průchodu stejně subskribovaný,
hodnoty chodí zdarma a jsou surovinou Forward GEX i budoucí skew analytiky.
Ranní bid/ask se záměrně NEukládá (předotevírací spready by lhaly
o likviditě — rozhodnutí uživatele 13. 8. 2026).
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    func,
    inspect,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec

logger = logging.getLogger(__name__)

metadata = MetaData()

oi_eod_table = Table(
    "oi_eod",
    metadata,
    Column("symbol", String(16), primary_key=True),
    Column("expiry", String(8), primary_key=True),
    # Série weekly/daily opcí (#736): MES má víc tradingClass se STEJNOU
    # expirací a bez klíče se jejich OI slévalo. "" = souhrn/legacy (řádky
    # z doby před migrací) — proto NOT NULL s defaultem, PK nesmí nést NULL.
    Column("trading_class", String(16), primary_key=True, server_default=""),
    Column("strike", Float, primary_key=True),
    Column("right", String(1), primary_key=True),
    Column("date", Date, primary_key=True),
    Column("oi", Float, nullable=False),
    # Kdy snímek vznikl (#463). Archiv pořízený před publikačním oknem IBKR
    # nese předpublikační čísla a musí se po okně přepsat — bez času pořízení
    # to nejde poznat, protože i neúplná data jsou validní nenulová hodnota.
    Column("captured_ts", DateTime(timezone=True), nullable=True),
    # Denní snímek řetězce (#519): NULL = model v okně snapshotu nedodal.
    Column("iv", Float, nullable=True),
    Column("delta", Float, nullable=True),
    Column("gamma", Float, nullable=True),
    Column("theta", Float, nullable=True),
    Column("vega", Float, nullable=True),
    # Závěrečná prémie předchozí seance (ticker.close) — trvalá historie
    # premium-weighted metrik za horizontem 14denní retence snapshotů
    Column("close_prem", Float, nullable=True),
    # Spot podkladu, ke kterému se IV/greeks vztahují (undPrice z modelu)
    Column("und_price", Float, nullable=True),
)

#: Sloupce denního snímku řetězce (#519) — sdílené migrací i upsertem
SNAPSHOT_COLUMNS = ("iv", "delta", "gamma", "theta", "vega", "close_prem", "und_price")


@dataclass(frozen=True)
class OIRecord:
    symbol: str
    expiry: str
    strike: float
    right: str
    day: dt.date
    oi: float
    # Denní snímek řetězce (#519) — volitelné, staré řádky mají NULL
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    close_prem: float | None = None
    und_price: float | None = None
    # Série (#736); "" = souhrn/legacy. Na konci kvůli pozičním konstruktorům.
    trading_class: str = ""


@dataclass(frozen=True)
class ContractSnapshot:
    """Hodnoty přečtené z jedné subskripce při ranním průchodu (#519).

    OI je povinné (bez něj se kontrakt hlásí jako missing); zbytek chodí
    zdarma z téže subskripce a chybět smí.
    """

    oi: float
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    close_prem: float | None = None
    und_price: float | None = None


@dataclass(frozen=True)
class ArchiveResult:
    """Výsledek denní archivace: kolik záznamů zapsáno a které kontrakty OI nedodaly."""

    written: int
    missing: tuple[OptionContractSpec, ...]
    #: Lišila se nová čtení od toho, co už v archivu bylo? `True` i pro první
    #: snímek dne. Dvě po sobě jdoucí nezměněná čtení = OI se ustálilo (#463).
    changed: bool = True


class OIFetcherLike(Protocol):
    """Zdroj denního snímku kontraktu (OI + IV/greeks/close, #519).

    Produkční implementace: FOP generic tick 101 přes reqMktData snapshot
    (ADR-0001: 588 intraday nechodí); IV/greeks/close se čtou opportunisticky
    z téže subskripce. Vrací None, když OI není k dispozici — bez OI je
    kontrakt missing, i kdyby greeks dorazily.
    """

    async def fetch_snapshot(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> ContractSnapshot | None: ...


class OIEodRepository:
    """Přístup k tabulce oi_eod. Záměrně bez delete API (R4)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        metadata.create_all(self._engine)
        self._migrate_captured_ts()
        self._migrate_trading_class()

    def _migrate_trading_class(self) -> None:
        """Klíč o sérii (#736): ADD COLUMN + přestavba PK — jen PostgreSQL.

        Staré řádky dostanou "" = souhrn přes série (přesně to tehdy zápis
        ukládal). Sqlite PK přestavět neumí — tam vzniká tabulka vždy čerstvě
        přes create_all (testy), takže větev chybět nevadí.
        """
        inspector = inspect(self._engine)
        if not inspector.has_table(oi_eod_table.name):
            return
        columns = {col["name"] for col in inspector.get_columns(oi_eod_table.name)}
        if "trading_class" in columns:
            return
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {oi_eod_table.name} "
                    "ADD COLUMN trading_class VARCHAR(16) NOT NULL DEFAULT ''"
                )
            )
            if self._engine.dialect.name == "postgresql":
                conn.execute(text(f"ALTER TABLE {oi_eod_table.name} DROP CONSTRAINT oi_eod_pkey"))
                conn.execute(
                    text(
                        f"ALTER TABLE {oi_eod_table.name} ADD PRIMARY KEY "
                        "(symbol, expiry, trading_class, strike, right, date)"
                    )
                )
        logger.info("oi_eod: doplněn klíč trading_class (#736) — staré řádky = souhrn ('')")

    def _migrate_captured_ts(self) -> None:
        """Aditivní migrace sloupců přidaných po založení tabulky.

        `captured_ts` (#463) a snímek řetězce (#519). Staré řádky zůstanou
        s NULL — u `captured_ts` se berou jako předpublikační a po okně se
        přepíšou; NULL greeks znamenají „tehdy se neměřily".
        """
        inspector = inspect(self._engine)
        if not inspector.has_table(oi_eod_table.name):
            return
        columns = {col["name"] for col in inspector.get_columns(oi_eod_table.name)}
        timestamp = "TIMESTAMPTZ" if self._engine.dialect.name == "postgresql" else "TIMESTAMP"
        additive = {"captured_ts": timestamp} | {name: "FLOAT" for name in SNAPSHOT_COLUMNS}
        missing = {name: sql_type for name, sql_type in additive.items() if name not in columns}
        if not missing:
            return
        with self._engine.begin() as conn:
            for name, sql_type in missing.items():
                conn.execute(text(f"ALTER TABLE {oi_eod_table.name} ADD COLUMN {name} {sql_type}"))

    def upsert_many(
        self, records: Sequence[OIRecord], captured_ts: dt.datetime | None = None
    ) -> None:
        """Idempotentní zápis: opakovaný běh týž den aktualizuje hodnoty (upsert)."""
        if not records:
            return
        updatable = ("oi", "captured_ts", *SNAPSHOT_COLUMNS)
        rows = [
            {
                "symbol": r.symbol,
                "expiry": r.expiry,
                "trading_class": r.trading_class,
                "strike": r.strike,
                "right": r.right,
                "date": r.day,
                "oi": r.oi,
                "captured_ts": captured_ts,
                **{name: getattr(r, name) for name in SNAPSHOT_COLUMNS},
            }
            for r in records
        ]
        dialect = self._engine.dialect.name
        primary_key = ["symbol", "expiry", "trading_class", "strike", "right", "date"]
        stmt: Executable
        if dialect == "postgresql":
            pg_stmt = pg_insert(oi_eod_table).values(rows)
            stmt = pg_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={name: getattr(pg_stmt.excluded, name) for name in updatable},
            )
        elif dialect == "sqlite":
            sqlite_stmt = sqlite_insert(oi_eod_table).values(rows)
            stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={name: getattr(sqlite_stmt.excluded, name) for name in updatable},
            )
        else:
            raise ValueError(f"Nepodporovaný databázový dialekt pro upsert: {dialect!r}")
        with self._engine.begin() as conn:
            conn.execute(stmt)
            # Supersede legacy souhrnu (#736, přechodový den migrace): per-class
            # zápis NAHRAZUJE řádek trading_class='' téhož kontraktu a dne —
            # jinak by Σ čtení den dvojpočítalo (souhrn + série téhož měření).
            # Není to mazání historie (R4): dny archivované před migrací se
            # nikdy znovu nezapisují, takže jejich '' řádky žijí dál.
            class_keys = {
                (r.symbol, r.expiry, r.strike, r.right, r.day) for r in records if r.trading_class
            }
            if class_keys:
                conn.execute(
                    oi_eod_table.delete().where(
                        oi_eod_table.c.trading_class == "",
                        tuple_(
                            oi_eod_table.c.symbol,
                            oi_eod_table.c.expiry,
                            oi_eod_table.c.strike,
                            oi_eod_table.c["right"],
                            oi_eod_table.c.date,
                        ).in_(list(class_keys)),
                    )
                )

    def days(self, symbol: str) -> list[dt.date]:
        stmt = (
            select(oi_eod_table.c.date)
            .where(oi_eod_table.c.symbol == symbol)
            .distinct()
            .order_by(oi_eod_table.c.date)
        )
        with self._engine.connect() as conn:
            return [row.date for row in conn.execute(stmt)]

    def captured_at(self, symbol: str, day: dt.date) -> dt.datetime | None:
        """Kdy vznikl snímek dne; None = archiv chybí NEBO je z doby před #463.

        Obě situace vedou ke stejnému závěru (snímek je potřeba po publikačním
        okně obnovit), takže je volající nemusí rozlišovat — na existenci
        archivu je `days()`.
        """
        stmt = select(func.max(oi_eod_table.c.captured_ts)).where(
            oi_eod_table.c.symbol == symbol,
            oi_eod_table.c.date == day,
        )
        with self._engine.connect() as conn:
            captured: dt.datetime | None = conn.execute(stmt).scalar_one_or_none()
        if captured is None:
            return None
        # sqlite vrací naivní datetime — archiv je vždy v UTC
        return captured if captured.tzinfo is not None else captured.replace(tzinfo=dt.UTC)

    def snapshot(self, symbol: str, day: dt.date) -> dict[tuple[str, float, str], float]:
        """Archivované OI dne podle klíče (expirace, strike, strana) — pro porovnání.

        Σ přes trading_class (#736): konzument chce celkový OI striku, přesně
        to, co archiv ukládal před rozlišením sérií.
        """
        stmt = (
            select(
                oi_eod_table.c.expiry,
                oi_eod_table.c.strike,
                oi_eod_table.c["right"],
                func.sum(oi_eod_table.c.oi).label("oi"),
            )
            .where(oi_eod_table.c.symbol == symbol, oi_eod_table.c.date == day)
            .group_by(oi_eod_table.c.expiry, oi_eod_table.c.strike, oi_eod_table.c["right"])
        )
        with self._engine.connect() as conn:
            return {
                (row.expiry, float(row.strike), row.right): float(row.oi)
                for row in conn.execute(stmt)
            }

    def snapshot_by_class(
        self, symbol: str, day: dt.date
    ) -> dict[tuple[str, str, float, str], float]:
        """OI dne per (expirace, trading_class, strike, strana) — finalita #463.

        Bez agregace: porovnání „změnilo se čtení?" musí srovnávat sérii se
        sérií; proti Σ přes série by po #736 nikdy nevyšlo shodně.
        """
        stmt = select(
            oi_eod_table.c.expiry,
            oi_eod_table.c.trading_class,
            oi_eod_table.c.strike,
            oi_eod_table.c["right"],
            oi_eod_table.c.oi,
        ).where(oi_eod_table.c.symbol == symbol, oi_eod_table.c.date == day)
        with self._engine.connect() as conn:
            return {
                (row.expiry, row.trading_class, float(row.strike), row.right): float(row.oi)
                for row in conn.execute(stmt)
            }

    def latest_day_before(self, symbol: str, expiry: str, day: dt.date) -> dt.date | None:
        """Poslední archivovaný den dané expirace před `day` (základ pro ΔOI)."""
        stmt = select(func.max(oi_eod_table.c.date)).where(
            oi_eod_table.c.symbol == symbol,
            oi_eod_table.c.expiry == expiry,
            oi_eod_table.c.date < day,
        )
        with self._engine.connect() as conn:
            result = conn.execute(stmt).scalar_one_or_none()
        return result

    def values_for(
        self, symbol: str, expiry: str, day: dt.date, trading_class: str | None = None
    ) -> list[OIRecord]:
        """OI záznamy expirace pro daný den (ΔOI, Max Pain).

        Default Σ přes série (#736 — chování před rozlišením); `trading_class`
        vybere jedinou sérii (datová strana #513).
        """
        stmt = (
            select(
                oi_eod_table.c.strike,
                oi_eod_table.c["right"],
                func.sum(oi_eod_table.c.oi).label("oi"),
            )
            .where(
                oi_eod_table.c.symbol == symbol,
                oi_eod_table.c.expiry == expiry,
                oi_eod_table.c.date == day,
            )
            .group_by(oi_eod_table.c.strike, oi_eod_table.c["right"])
        )
        if trading_class is not None:
            stmt = stmt.where(oi_eod_table.c.trading_class == trading_class)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            OIRecord(symbol, expiry, float(row.strike), str(row.right), day, float(row.oi))
            for row in rows
        ]

    def chain_for_day(self, symbol: str, day: dt.date) -> list[OIRecord]:
        """Celý denní snímek řetězce (všechny expirace) — vstup Forward GEX (#519)."""
        stmt = select(oi_eod_table).where(
            oi_eod_table.c.symbol == symbol, oi_eod_table.c.date == day
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        # Agregace přes trading_class (#736): Σ OI, snímkové hodnoty od série
        # s největším OI — přesně to, co do #736 dělal zápis (write-merge #215),
        # takže Forward GEX vidí stejná čísla jako nad starou historií.
        merged: dict[tuple[str, float, str], OIRecord] = {}
        heaviest: dict[tuple[str, float, str], float] = {}
        for row in rows:
            key = (row.expiry, float(row.strike), str(row.right))
            record = OIRecord(
                symbol=row.symbol,
                expiry=row.expiry,
                strike=float(row.strike),
                right=str(row.right),
                day=row.date,
                oi=float(row.oi),
                iv=float(row.iv) if row.iv is not None else None,
                delta=float(row.delta) if row.delta is not None else None,
                gamma=float(row.gamma) if row.gamma is not None else None,
                theta=float(row.theta) if row.theta is not None else None,
                vega=float(row.vega) if row.vega is not None else None,
                close_prem=float(row.close_prem) if row.close_prem is not None else None,
                und_price=float(row.und_price) if row.und_price is not None else None,
            )
            existing = merged.get(key)
            if existing is None:
                merged[key] = record
                heaviest[key] = record.oi
                continue
            dominant = record if record.oi >= heaviest[key] else existing
            heaviest[key] = max(heaviest[key], record.oi)
            merged[key] = OIRecord(
                symbol=record.symbol,
                expiry=record.expiry,
                strike=record.strike,
                right=record.right,
                day=record.day,
                oi=existing.oi + record.oi,
                iv=dominant.iv,
                delta=dominant.delta,
                gamma=dominant.gamma,
                theta=dominant.theta,
                vega=dominant.vega,
                close_prem=dominant.close_prem,
                und_price=dominant.und_price,
            )
        return list(merged.values())

    def count_for_day(self, symbol: str, day: dt.date) -> int:
        stmt = (
            select(func.count())
            .select_from(oi_eod_table)
            .where(oi_eod_table.c.symbol == symbol, oi_eod_table.c.date == day)
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def get_oi(
        self, symbol: str, day: dt.date, strike: float, right: str, expiry: str | None = None
    ) -> float | None:
        """OI kontraktu pro daný den; expiry filtr je nutný — archiv od zavedení
        ΔOI drží týž den pro více expirací a bez filtru by dotaz našel víc řádků.
        Bez expiry (starší volání v testech) se bere nejbližší expirace."""
        stmt = (
            select(oi_eod_table.c.expiry, func.sum(oi_eod_table.c.oi).label("oi"))
            .where(
                oi_eod_table.c.symbol == symbol,
                oi_eod_table.c.date == day,
                oi_eod_table.c.strike == strike,
                oi_eod_table.c.right == right,
            )
            .group_by(oi_eod_table.c.expiry)
        )
        if expiry is not None:
            stmt = stmt.where(oi_eod_table.c.expiry == expiry)
        # Σ přes trading_class (#736); bez expiry se bere nejbližší expirace
        stmt = stmt.order_by(oi_eod_table.c.expiry).limit(1)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
            return float(row.oi) if row is not None else None


class OIArchiver:
    """Denní archivace OI celého řetězce do oi_eod (bez retence, R4)."""

    def __init__(
        self, repository: OIEodRepository, fetcher: OIFetcherLike, settings: Settings
    ) -> None:
        self._repository = repository
        self._fetcher = fetcher
        self._settings = settings

    async def archive_day(
        self,
        contracts: Sequence[OptionContractSpec],
        day: dt.date,
        now: dt.datetime | None = None,
    ) -> ArchiveResult:
        """Stáhne OI všech kontraktů (po dávkách) a idempotentně zapíše do DB."""
        records: list[OIRecord] = []
        missing: list[OptionContractSpec] = []
        batch_size = self._settings.batch_size
        for offset in range(0, len(contracts), batch_size):
            batch = contracts[offset : offset + batch_size]
            values = await asyncio.gather(*(self._fetch_one(spec) for spec in batch))
            for spec, snapshot in zip(batch, values, strict=True):
                if snapshot is None:
                    missing.append(spec)
                else:
                    records.append(
                        OIRecord(
                            symbol=spec.symbol,
                            expiry=spec.expiry,
                            strike=spec.strike,
                            right=spec.right,
                            day=day,
                            oi=snapshot.oi,
                            iv=snapshot.iv,
                            delta=snapshot.delta,
                            gamma=snapshot.gamma,
                            theta=snapshot.theta,
                            vega=snapshot.vega,
                            close_prem=snapshot.close_prem,
                            und_price=snapshot.und_price,
                            trading_class=spec.trading_class or "",
                        )
                    )
        # Dedupe přes klíč archivu (#215 → #736): klíč nově nese trading_class,
        # takže série se STEJNOU expirací (MES) už se neslévají — každá má svůj
        # řádek. Merge zůstává jen jako pojistka proti skutečnému duplikátu
        # (táž série dvakrát v dávce), kde by upsert spadl CardinalityViolation.
        merged: dict[tuple[str, str, str, float, str], OIRecord] = {}
        for record in records:
            key = (record.symbol, record.expiry, record.trading_class, record.strike, record.right)
            existing = merged.get(key)
            if existing is None:
                merged[key] = record
            else:
                # OI sérií se sčítá; snímkové hodnoty (IV/greeks/prémie) nese
                # série s větším OI — vážený průměr by předstíral přesnost,
                # kterou snapshot nemá
                dominant = record if record.oi >= existing.oi else existing
                merged[key] = OIRecord(
                    symbol=record.symbol,
                    expiry=record.expiry,
                    strike=record.strike,
                    right=record.right,
                    day=record.day,
                    oi=existing.oi + record.oi,
                    iv=dominant.iv,
                    delta=dominant.delta,
                    gamma=dominant.gamma,
                    theta=dominant.theta,
                    vega=dominant.vega,
                    close_prem=dominant.close_prem,
                    und_price=dominant.und_price,
                    trading_class=record.trading_class,
                )
        deduped = list(merged.values())
        if len(deduped) < len(records):
            logger.info(
                "OI archivace %s: %d duplicitních sérií sloučeno (Σ OI per strike)",
                day,
                len(records) - len(deduped),
            )
        # Porovnání s archivem PŘED zápisem (#463): dvě po sobě jdoucí nezměněná
        # čtení znamenají, že IBKR publikaci dokončil a snímek je finální
        previous = (
            await asyncio.to_thread(self._repository.snapshot_by_class, deduped[0].symbol, day)
            if deduped
            else {}
        )  # prettier-ignore

        def _previous_oi(r: OIRecord) -> float | None:
            """Hodnota z archivu; legacy '' řádek (před migrací #736) platí jako
            srovnání pro sérii — u jednoseriových symbolů je to táž hodnota,
            u víceseriových se Σ od série liší a čtení se korektně obnoví."""
            exact = previous.get((r.expiry, r.trading_class, r.strike, r.right))
            if exact is not None:
                return exact
            return previous.get((r.expiry, "", r.strike, r.right))

        changed = not previous or any(_previous_oi(r) != r.oi for r in deduped)
        # Kontrakt dřív archivovaný, který teď čtení nedodalo, nejde potvrdit
        # jako nezměněný — neúplné čtení nesmí prohlásit snímek za finální (#494).
        # Trvale chybějící striky (OI bez hodnoty celý den) finalitě nebrání,
        # jinak by se archiv obnovoval donekonečna.
        changed = changed or any(
            (spec.expiry, spec.trading_class or "", spec.strike, spec.right) in previous
            or (spec.expiry, "", spec.strike, spec.right) in previous
            for spec in missing
        )
        captured_ts = now or dt.datetime.now(dt.UTC)
        await asyncio.to_thread(self._repository.upsert_many, deduped, captured_ts)
        if missing:
            logger.warning("OI archivace %s: %d kontraktů bez OI", day, len(missing))
        return ArchiveResult(written=len(deduped), missing=tuple(missing), changed=changed)

    async def _fetch_one(self, spec: OptionContractSpec) -> ContractSnapshot | None:
        try:
            return await self._fetcher.fetch_snapshot(spec, self._settings.batch_timeout_s)
        except Exception:
            logger.exception("fetch_snapshot selhal pro %s", spec)
            return None
