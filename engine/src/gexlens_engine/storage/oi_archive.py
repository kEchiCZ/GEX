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
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy import (
    Column,
    ColumnElement,
    Date,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    and_,
    case,
    func,
    inspect,
    or_,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

from gexlens_engine.compute.settle import is_trading_session
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
    # Kdy tasty naposledy změnil hodnotu řádku (#828, varianta A; #1605).
    # Oddělený od `captured_ts` schválně: tasty OI teče průběžně, kdežto IBKR
    # chodí snapshot průchodem — jeden společný čas by rozbil detektor
    # finality (#463), který na `captured_ts` stojí. Původ hodnoty určuje
    # JEN `captured_ts`: vyplněný = hodnotu naposledy zapsalo IBKR (i když
    # řádek předtím zapsal tasty — IBKR zápis tenhle sloupec nemaže, takže
    # řádek nese oba časy); NULL a vyplněný `tasty_captured_ts` = tasty řádek;
    # oba časy NULL = IBKR řádek z doby před #463 (bez času čtení, věčný archiv
    # je nese dál). Tasty nikdy nepřepíše řádek s `captured_ts` (stráž
    # v `upsert_many`). Čas nezměněné hodnoty se
    # neposouvá — sloupec nedokazuje čerstvost, jen poslední změnu.
    Column("tasty_captured_ts", DateTime(timezone=True), nullable=True),
)

#: Sloupce denního snímku řetězce (#519) — sdílené migrací i upsertem
SNAPSHOT_COLUMNS = ("iv", "delta", "gamma", "theta", "vega", "close_prem", "und_price")
#: Řádek s IBKR měřením (#1605): původ hodnoty určuje jen vyplněný `captured_ts`
_IBKR_ROW = oi_eod_table.c.captured_ts.is_not(None)

#: Verze archivu expirace dne (#1605): (max `captured_ts`, max `tasty_captured_ts`)
ArchiveVersion = tuple[dt.datetime | None, dt.datetime | None]


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


def _tasty_guard(excluded_oi: ColumnElement[float]) -> ColumnElement[bool]:
    """Stráž tasty upsertu (#1605): jen tasty řádek a jen změněná hodnota.

    Vyhodnotí se nad zamčenou aktuální verzí řádku, takže IBKR zápis
    commitnutý mezi čtením pokrytí a tasty upsertem ji neobejde.
    `oi` je NOT NULL, prosté `!=` stačí.
    """
    return and_(oi_eod_table.c.captured_ts.is_(None), oi_eod_table.c.oi != excluded_oi)


@dataclass(frozen=True)
class SourcedRow:
    """Řádek archivu dne s původem hodnoty (#1605) — pokrytí širokého OI z tasty."""

    trading_class: str
    strike: float
    right: str
    oi: float
    #: Vyplněný `captured_ts` = hodnotu naposledy zapsalo IBKR
    ibkr: bool


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


class OIMiss(StrEnum):
    """Proč kontrakt v průchodu nedodal OI (#1650); hodnota je text řádku dávky v logu."""

    #: Po requestu nedorazil žádný tick (request neodešel, smyčka stála, IBKR mlčí)
    NO_TICK = "bez ticku"
    #: Ticky chodily (kotace, greeks), ale OI strany kontraktu ne
    NO_OI = "tick bez OI"
    #: Kontrakt nejde kvalifikovat (sec-def farma mlčí nebo kontrakt nezná)
    UNQUALIFIED = "nekvalifikováno"
    #: Čtení spadlo výjimkou (detail v logu „fetch_snapshot selhal“)
    ERROR = "chyba"


@dataclass(frozen=True)
class ArchiveResult:
    """Výsledek denní archivace: kolik záznamů zapsáno a které kontrakty OI nedodaly."""

    written: int
    missing: tuple[OptionContractSpec, ...]
    #: Zapsané hodnoty, které se liší od archivu dne nebo v něm chyběly (#1650)
    changed_values: int = 0
    #: Chybějící kontrakty, které archiv dne už má — nejdou potvrdit (#494, #1650)
    missing_archived: int = 0

    @property
    def changed(self) -> bool:
        """Lišila se nová čtení od toho, co už v archivu bylo? `True` i pro první
        snímek dne (všechny hodnoty jsou nové). Dvě po sobě jdoucí nezměněná
        čtení = OI se ustálilo (#463)."""
        return self.changed_values > 0 or self.missing_archived > 0


class OIFetcherLike(Protocol):
    """Zdroj denního snímku kontraktu (OI + IV/greeks/close, #519).

    Produkční implementace: FOP generic tick 101 přes reqMktData snapshot
    (ADR-0001: 588 intraday nechodí); IV/greeks/close se čtou opportunisticky
    z téže subskripce. Bez OI vrací důvod chybění (#1650) — kontrakt je
    missing, i kdyby greeks dorazily.
    """

    async def fetch_snapshot(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> ContractSnapshot | OIMiss: ...


class OIEodRepository:
    """Přístup k tabulce oi_eod. Záměrně bez delete API (R4)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        metadata.create_all(self._engine)
        self._migrate_captured_ts()
        self._migrate_trading_class()

    def _migrate_trading_class(self) -> None:
        """Klíč o sérii (#736): ADD COLUMN + přestavba PK.

        Staré řádky dostanou "" = souhrn přes série (přesně to tehdy zápis
        ukládal). PostgreSQL přestaví PK in-place; sqlite ALTER PK neumí,
        tam se tabulka přestaví přes rename+copy — bez toho by na trvalé
        dev databázi upsert (ON CONFLICT přes 6sloupcový klíč) padal na
        původním 5sloupcovém PK.
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
                # "right" je v PG rezervované slovo — bez uvozovek je DDL
                # syntax error a celá migrace by se při startu odrollovala
                conn.execute(
                    text(
                        f"ALTER TABLE {oi_eod_table.name} ADD PRIMARY KEY "
                        '(symbol, expiry, trading_class, strike, "right", date)'
                    )
                )
            else:
                # _migrate_captured_ts běží dřív, takže tabulka už má všechny
                # sloupce metadat — kopie jmenovaným výčtem je úplná
                names = ", ".join(f'"{c.name}"' for c in oi_eod_table.columns)
                conn.execute(text(f"ALTER TABLE {oi_eod_table.name} RENAME TO oi_eod_migrating"))
                oi_eod_table.create(conn)
                conn.execute(
                    text(
                        f"INSERT INTO {oi_eod_table.name} ({names}) "
                        f"SELECT {names} FROM oi_eod_migrating"
                    )
                )
                conn.execute(text("DROP TABLE oi_eod_migrating"))
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
        additive = {"captured_ts": timestamp, "tasty_captured_ts": timestamp} | {
            name: "FLOAT" for name in SNAPSHOT_COLUMNS
        }
        missing = {name: sql_type for name, sql_type in additive.items() if name not in columns}
        if not missing:
            return
        with self._engine.begin() as conn:
            for name, sql_type in missing.items():
                conn.execute(text(f"ALTER TABLE {oi_eod_table.name} ADD COLUMN {name} {sql_type}"))

    def upsert_many(
        self,
        records: Sequence[OIRecord],
        captured_ts: dt.datetime | None = None,
        *,
        tasty_captured_ts: dt.datetime | None = None,
    ) -> int:
        """Idempotentní zápis (upsert); vrací počet skutečně vložených/změněných řádků.

        IBKR zápis (`captured_ts`) přepíše OI, čas i snímkové sloupce;
        `tasty_captured_ts` nechá, takže řádek přepsaný z IBKR nese oba časy
        a jeho původ je IBKR (vyplněný `captured_ts`).

        `tasty_captured_ts` značí řádky z tasty Summary (#828) — píše se MÍSTO
        `captured_ts`, aby průběžný tasty zápis neposouval čas IBKR snímku
        (na něm stojí finalita #463). Tasty zápis mění jen `oi` a svůj čas
        (snímkové sloupce nemá — vynuloval by greeks IBKR řádku) a stráž v SQL
        ho pustí jen na tasty řádek se změněnou hodnotou (#1605): řádek
        s `captured_ts` je pro tasty nedotknutelný i při souběhu s IBKR
        archivací a nezměněná hodnota se nepřepisuje (do počtu se nezapočte).

        Řádky jdou do DB seřazené podle PK: souběžné dávky IBKR a tasty se
        společnými klíči tak zamykají ve stejném pořadí a neuváznou (PG).
        """
        if not records:
            return 0
        tasty = tasty_captured_ts is not None
        stamped = "tasty_captured_ts" if tasty else "captured_ts"
        updatable = ("oi", stamped) if tasty else ("oi", stamped, *SNAPSHOT_COLUMNS)
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
                "tasty_captured_ts": tasty_captured_ts,
                **{name: getattr(r, name) for name in SNAPSHOT_COLUMNS},
            }
            for r in sorted(
                records,
                key=lambda r: (r.symbol, r.expiry, r.trading_class, r.strike, r.right, r.day),
            )
        ]
        dialect = self._engine.dialect.name
        primary_key = ["symbol", "expiry", "trading_class", "strike", "right", "date"]
        stmt: Executable
        if dialect == "postgresql":
            pg_stmt = pg_insert(oi_eod_table).values(rows)
            stmt = pg_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={name: getattr(pg_stmt.excluded, name) for name in updatable},
                where=_tasty_guard(pg_stmt.excluded.oi) if tasty else None,
            ).returning(oi_eod_table.c.symbol)
        elif dialect == "sqlite":
            sqlite_stmt = sqlite_insert(oi_eod_table).values(rows)
            stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={name: getattr(sqlite_stmt.excluded, name) for name in updatable},
                where=_tasty_guard(sqlite_stmt.excluded.oi) if tasty else None,
            ).returning(oi_eod_table.c.symbol)
        else:
            raise ValueError(f"Nepodporovaný databázový dialekt pro upsert: {dialect!r}")
        # Počet z RETURNING, ne `rowcount`: psycopg u vícehodnotového INSERT …
        # ON CONFLICT vrací −1. RETURNING vrátí vložené i změněné řádky, řádek
        # odmítnutý stráží ne.
        with self._engine.begin() as conn:
            return len(conn.execute(stmt).fetchall())

    def supersede_legacy(self, keys: Iterable[tuple[str, str, float, str, dt.date]]) -> None:
        """Nahrazení legacy souhrnu ('' z doby před #736) per-class čtením.

        Volá se JEN pro kontrakty, jejichž všechny série v průchodu dodaly
        (OIArchiver.archive_day) — smazání souhrnu při chybějící sesterské
        sérii by nevratně ztratilo její Σ příspěvek a finalita by ztrátu
        zamkla. Není to mazání historie (R4): dny archivované před migrací
        se znovu nezapisují a jejich '' řádky žijí dál; maže se jen souhrn
        dne, který právě nahradilo ÚPLNÉ per-class měření téže publikace —
        jinak by Σ čtení den dvojpočítalo (souhrn + série téhož měření).
        """
        key_list = list(keys)
        if not key_list:
            return
        with self._engine.begin() as conn:
            conn.execute(
                oi_eod_table.delete().where(
                    oi_eod_table.c.trading_class == "",
                    tuple_(
                        oi_eod_table.c.symbol,
                        oi_eod_table.c.expiry,
                        oi_eod_table.c.strike,
                        oi_eod_table.c["right"],
                        oi_eod_table.c.date,
                    ).in_(key_list),
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

    def trading_classes_by_expiry(self, symbol: str) -> dict[str, list[str]]:
        """Distinct trading classes per expirace (#513) — pro selektor expirací.

        Legacy souhrn "" (řádky z doby před #736) se vynechává: nenese jméno
        série a v selektoru by matl. Expirace archivovaná jen jako legacy tak
        vyjde bez tříd — UI ukáže holé datum, což je poctivé (jméno neznáme).
        """
        stmt = (
            select(oi_eod_table.c.expiry, oi_eod_table.c.trading_class)
            .where(oi_eod_table.c.symbol == symbol, oi_eod_table.c.trading_class != "")
            .distinct()
            .order_by(oi_eod_table.c.expiry, oi_eod_table.c.trading_class)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        classes: dict[str, list[str]] = {}
        for expiry, trading_class in rows:
            classes.setdefault(expiry, []).append(trading_class)
        return classes

    def captured_at(self, symbol: str, day: dt.date) -> dt.datetime | None:
        """Kdy vznikl IBKR snímek dne; None = archiv chybí, den má jen tasty
        řádky (#828, `captured_ts` NULL — o čtení od 00:00 rozhoduje právě tohle,
        #1609) NEBO je z doby před #463.

        Všechny vedou ke stejnému závěru (IBKR snímek je potřeba přečíst, po publikačním
        okně obnovit), takže je volající nemusí rozlišovat — na existenci
        jakýchkoli řádků dne je `days()`.
        """
        stmt = select(func.max(oi_eod_table.c.captured_ts)).where(
            oi_eod_table.c.symbol == symbol,
            oi_eod_table.c.date == day,
        )
        with self._engine.connect() as conn:
            captured: dt.datetime | None = conn.execute(stmt).scalar_one_or_none()
        return _as_utc(captured)

    def archive_version(self, symbol: str, expiry: str, day: dt.date) -> ArchiveVersion:
        """Verze archivu expirace dne (#1605) — klíč cache čtenářů `values_for`.

        (max `captured_ts`, max `tasty_captured_ts`): IBKR doarchivace posouvá
        první, obnova tasty řádku druhé (tasty zapisuje jen změněné hodnoty,
        takže maximum se hne právě tehdy, když je co přepočítat). Samotný
        `captured_at` obnovu tasty křídel nepoznal a OI zdi i Max Pain zůstaly
        na hodnotě z prvního načtení. Omezení na expiraci: zápisy jiných
        expirací (sekundár) cache zbytečně nezneplatní.
        """
        stmt = select(
            func.max(oi_eod_table.c.captured_ts),
            func.max(oi_eod_table.c.tasty_captured_ts),
        ).where(
            oi_eod_table.c.symbol == symbol,
            oi_eod_table.c.expiry == expiry,
            oi_eod_table.c.date == day,
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one()
        return _as_utc(row[0]), _as_utc(row[1])

    def wings_last_change(self, symbol: str, expiry: str, day: dt.date) -> dt.datetime | None:
        """Poslední změna tasty křídel expirace dne (#1611); None = křídla nejsou.

        Jen řádky bez IBKR `captured_ts`: řádek přepsaný z IBKR si nechá starý
        tasty čas, tasty ho už neobnoví (stráž #1605). Tasty zapisuje jen změněnou
        hodnotu, takže čas je poslední ZMĚNA, ne čerstvost; slouží jako klíč, kdy
        znovu srovnat hodnoty (`wings_changed_vs`).
        """
        stmt = select(func.max(oi_eod_table.c.tasty_captured_ts)).where(
            oi_eod_table.c.symbol == symbol,
            oi_eod_table.c.expiry == expiry,
            oi_eod_table.c.date == day,
            oi_eod_table.c.captured_ts.is_(None),
        )
        with self._engine.connect() as conn:
            changed: dt.datetime | None = conn.execute(stmt).scalar_one_or_none()
        return _as_utc(changed)

    def wings_changed_vs(
        self, symbol: str, expiry: str, day: dt.date, previous: dt.date
    ) -> tuple[int, int]:
        """(společné, změněné) tasty křídla expirace dne proti dni `previous` (#1611).

        Křídla = řádky dne bez IBKR `captured_ts`; protějšek je týž kontrakt
        (série, strike, strana) dne `previous` libovolného původu. Nová publikace
        CME změní 22–55 % společných křídel (ES i NQ 19.–26. 10. 2026, i pondělí
        proti pátku); křídla, která tasty po publikaci neobnovilo, se neliší
        vůbec. Index PK, ~9 ms pro NQ (582 křídel).
        """
        today = oi_eod_table.alias("today")
        before = oi_eod_table.alias("before")
        stmt = (
            select(
                func.count(),
                func.count().filter(today.c.oi != before.c.oi),
            )
            .select_from(today)
            .join(
                before,
                (before.c.symbol == today.c.symbol)
                & (before.c.expiry == today.c.expiry)
                & (before.c.trading_class == today.c.trading_class)
                & (before.c.strike == today.c.strike)
                & (before.c.right == today.c.right)
                & (before.c.date == previous),
            )
            .where(
                today.c.symbol == symbol,
                today.c.expiry == expiry,
                today.c.date == day,
                today.c.captured_ts.is_(None),
            )
        )
        with self._engine.connect() as conn:
            common, changed = conn.execute(stmt).one()
        return int(common), int(changed)

    def snapshot(
        self, symbol: str, day: dt.date, *, ibkr_only: bool = False
    ) -> dict[tuple[str, float, str], float]:
        """Archivované OI dne podle klíče (expirace, strike, strana) — pro porovnání.

        Σ přes trading_class (#736): konzument chce celkový OI striku, přesně
        to, co archiv ukládal před rozlišením sérií. `ibkr_only` vynechá tasty
        řádky (#1605) — srovnání OI IBKR × tasty (#664) by jinak u striků
        nesených tasty srovnávalo tasty sám se sebou.
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
        if ibkr_only:
            stmt = stmt.where(_IBKR_ROW)
        with self._engine.connect() as conn:
            return {
                (row.expiry, float(row.strike), row.right): float(row.oi)
                for row in conn.execute(stmt)
            }

    def snapshot_by_class(
        self, symbol: str, day: dt.date
    ) -> dict[tuple[str, str, float, str], float]:
        """IBKR OI dne per (expirace, trading_class, strike, strana) — finalita #463.

        Bez agregace: porovnání „změnilo se čtení?" musí srovnávat sérii se
        sérií; proti Σ přes série by po #736 nikdy nevyšlo shodně.

        Jen řádky s IBKR měřením (#1605): tasty řádek není „předchozí čtení"
        IBKR — první IBKR čtení klíče neseného tasty je změna a kontrakt, který
        IBKR nikdy nezměřilo, finalitu neblokuje (jinak by ji tasty křídlo
        mimo IBKR čtení drželo celý den a FA joby by neběžely).
        """
        stmt = select(
            oi_eod_table.c.expiry,
            oi_eod_table.c.trading_class,
            oi_eod_table.c.strike,
            oi_eod_table.c["right"],
            oi_eod_table.c.oi,
        ).where(oi_eod_table.c.symbol == symbol, oi_eod_table.c.date == day, _IBKR_ROW)
        with self._engine.connect() as conn:
            return {
                (row.expiry, row.trading_class, float(row.strike), row.right): float(row.oi)
                for row in conn.execute(stmt)
            }

    def latest_trading_day_before(self, symbol: str, expiry: str, day: dt.date) -> dt.date | None:
        """Poslední archivovaný OBCHODNÍ den expirace před `day` — základ každého ΔOI.

        Klíč archivu je UTC den pořízení, takže archiv má i sobotu a neděli
        (čtení na začátku UTC dne, tasty široký OI, doarchivace striků). Jejich
        obsah závisí na hodině pořízení: sobota a neděle před otevřením Globexu
        nesou páteční stav, neděle po otevření už pondělní. Srovnání „proti
        předchozímu dni" proto víkend přeskakuje (#1309): pondělí proti neděli
        dávalo 21. 9. 2026 u většiny expirací ΔOI = 0 na všech stranách.
        """
        stmt = (
            select(oi_eod_table.c.date)
            .where(
                oi_eod_table.c.symbol == symbol,
                oi_eod_table.c.expiry == expiry,
                oi_eod_table.c.date < day,
            )
            .distinct()
            .order_by(oi_eod_table.c.date.desc())
        )
        with self._engine.connect() as conn:
            for row in conn.execute(stmt):
                archived: dt.date = row.date
                if is_trading_session(archived):
                    return archived
        return None

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

    def sourced_rows(self, symbol: str, expiry: str, day: dt.date) -> list[SourcedRow]:
        """Řádky expirace dne s původem hodnoty, bez agregace (#1605).

        Vstup širokého OI z tasty: pokryté jsou jen striky s IBKR řádkem
        a „nezměněno" se porovnává per PK proti tasty řádku, ne proti Σ přes
        série. `values_for` původ nerozlišuje — vracel i vlastní tasty zápisy,
        takže po prvním zápisu dne byl každý strike „pokrytý" a tasty řádek
        se do konce dne neobnovil.
        """
        stmt = select(
            oi_eod_table.c.trading_class,
            oi_eod_table.c.strike,
            oi_eod_table.c["right"],
            oi_eod_table.c.oi,
            oi_eod_table.c.captured_ts,
        ).where(
            oi_eod_table.c.symbol == symbol,
            oi_eod_table.c.expiry == expiry,
            oi_eod_table.c.date == day,
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            SourcedRow(
                trading_class=str(row.trading_class),
                strike=float(row.strike),
                right=str(row.right),
                oi=float(row.oi),
                ibkr=row.captured_ts is not None,
            )
            for row in rows
        ]

    def measured_values_for(
        self,
        symbol: str,
        expiry: str,
        day: dt.date,
        *,
        since: dt.datetime,
        include_tasty: bool = False,
    ) -> list[OIRecord]:
        """Σ OI expirace dne jen pro strany změřené od `since` (#1605).

        Strana (strike, right) se vrátí, jen když VŠECHNY její řádky přes série
        nesou IBKR měření s `captured_ts >= since` (typicky publikace CME dne).
        Strana nesená tasty nebo IBKR čtením před `since` chybí — „nevíme",
        ne hodnota (#1172); stejně strana se smíšenými sériemi.

        `include_tasty` (varianta B+, jen strana D v `measured_oi_pair`): za
        změřený platí i tasty řádek (`captured_ts` NULL) s `tasty_captured_ts
        >= since` — tasty obnoví hodnotu i čas jen při změně OI, takový řádek
        tedy nese publikaci od `since`. U IBKR řádku se `tasty_captured_ts`
        nečte nikdy: řádek tasty → IBKR nese oba časy a IBKR razítkuje
        začátkem cyklu, ne časem zápisu, takže časy dvou zdrojů porovnat nejde;
        ochranu IBKR řádku dává stráž v `upsert_many`.
        """
        if since.tzinfo is None:
            raise ValueError(f"measured_values_for: `since` bez časového pásma ({since!r})")
        # sqlite porovnává uložený UTC čas jako text — práh musí být taky v UTC
        threshold = since.astimezone(dt.UTC)
        measured: ColumnElement[bool] = oi_eod_table.c.captured_ts >= threshold
        if include_tasty:
            measured = or_(
                measured,
                and_(
                    oi_eod_table.c.captured_ts.is_(None),
                    oi_eod_table.c.tasty_captured_ts >= threshold,
                ),
            )
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
            # NULL čas (tasty řádek bez `include_tasty`, řádek před #463) spadne
            # do else → strana vypadne
            .having(func.sum(case((measured, 0), else_=1)) == 0)
        )
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


#: Krok sondy lagu smyčky během dávky (#1650) — týž jako krok čekání na tick
#: v `IbOIFetcher.fetch_snapshot`
LOOP_LAG_PROBE_S = 0.25


class LoopLagProbe:
    """Nejdelší přesah `asyncio.sleep` souběžně s dávkou čtení (#1650, hypotéza 1).

    Smyčka, která stojí (CPU práce v témže vlákně, GIL držený vláknem
    z `to_thread`), nezpracovává příchozí ticky a čekání na OI vyprší
    naprázdno. Přesah spánku ukáže, jak dlouho smyčka nejvýš stála.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._since = self._loop.time()
        self._worst = 0.0

    async def run(self) -> None:
        while True:
            self._since = self._loop.time()
            await asyncio.sleep(LOOP_LAG_PROBE_S)
            self._worst = max(self._worst, self._loop.time() - self._since - LOOP_LAG_PROBE_S)

    def worst(self) -> float:
        """Nejdelší přesah včetně spánku, který právě běží (smyčka mohla stát na konci dávky)."""
        return max(self._worst, self._loop.time() - self._since - LOOP_LAG_PROBE_S)


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """sqlite vrací naivní datetime — archiv je vždy v UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=dt.UTC)


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
            read, absent = await self.fetch_batch(
                contracts[offset : offset + batch_size], day, offset=offset, total=len(contracts)
            )
            records.extend(read)
            missing.extend(absent)
        return await self.commit(records, missing, day, now)

    async def fetch_batch(
        self, batch: Sequence[OptionContractSpec], day: dt.date, *, offset: int, total: int
    ) -> tuple[list[OIRecord], list[OptionContractSpec]]:
        """Jedna dávka čtení z IBKR bez zápisu: záznamy s OI a kontrakty bez něj.

        Dávka drží nejvýš `batch_size` subskripcí naráz (100 lines, ADR-0001
        bod 4). Obnova po publikaci volá dávky po jedné mezi minutovými cykly
        (#1646) a zapíše je až `commit` po celém průchodu. `offset` a `total`
        (pozice dávky v průchodu) jdou jen do řádku dávky v logu (#1650).
        """
        records: list[OIRecord] = []
        missing: list[OptionContractSpec] = []
        loop = asyncio.get_running_loop()
        started = loop.time()
        probe = LoopLagProbe()
        probe_task = asyncio.create_task(probe.run(), name="oi-loop-lag")
        try:
            values = await asyncio.gather(*(self._fetch_one(spec) for spec in batch))
        finally:
            probe_task.cancel()
        elapsed, lag = loop.time() - started, probe.worst()
        reasons: Counter[OIMiss] = Counter()
        # Přečteno/celkem po sériích v pořadí průchodu: vypadávají celé expirace
        # (#1650) — a jsou to ty, které čte i sweep (hypotéza 3)?
        series: dict[str, list[int]] = {}
        for spec, snapshot in zip(batch, values, strict=True):
            tally = series.setdefault(f"{spec.trading_class or ''} {spec.expiry}".strip(), [0, 0])
            tally[1] += 1
            if isinstance(snapshot, OIMiss):
                reasons[snapshot] += 1
                missing.append(spec)
            else:
                tally[0] += 1
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
        logger.info(
            "OI archiv %s %s: dávka %d–%d/%d (%s) — přečteno %d, %s; %.1f s, lag smyčky max %.2f s",
            batch[0].symbol,
            day,
            offset + 1,
            offset + len(batch),
            total,
            ", ".join(f"{name} {read}/{size}" for name, (read, size) in series.items()),
            len(records),
            ", ".join(f"{reason} {reasons[reason]}" for reason in OIMiss),
            elapsed,
            lag,
        )
        return records, missing

    async def commit(
        self,
        records: Sequence[OIRecord],
        missing: Sequence[OptionContractSpec],
        day: dt.date,
        now: dt.datetime | None = None,
    ) -> ArchiveResult:
        """Zápis přečteného průchodu: dedupe, srovnání s archivem, upsert (idempotentní)."""
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
        # čtení znamenají, že IBKR publikaci dokončil a snímek je finální.
        # Srovná se i průchod bez jediné hodnoty — počet chybějících dřív
        # archivovaných jde do logu průchodu (#1650)
        symbol = deduped[0].symbol if deduped else missing[0].symbol if missing else None
        previous = (
            await asyncio.to_thread(self._repository.snapshot_by_class, symbol, day)
            if symbol is not None
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

        # Nový kontrakt (i celý první snímek dne) nemá s čím shodu — je změna
        changed_values = sum(1 for r in deduped if _previous_oi(r) != r.oi)
        # Kontrakt dřív archivovaný, který teď čtení nedodalo, nejde potvrdit
        # jako nezměněný — neúplné čtení nesmí prohlásit snímek za finální (#494).
        # Trvale chybějící striky (OI bez hodnoty celý den) finalitě nebrání,
        # jinak by se archiv obnovoval donekonečna.
        missing_archived = sum(
            1
            for spec in missing
            if (spec.expiry, spec.trading_class or "", spec.strike, spec.right) in previous
            or (spec.expiry, "", spec.strike, spec.right) in previous
        )
        captured_ts = now or dt.datetime.now(dt.UTC)
        await asyncio.to_thread(self._repository.upsert_many, deduped, captured_ts)
        # Supersede legacy souhrnu (#736, přechodový den migrace) — ale AŽ
        # když všechny série kontraktu dodaly: při chybějící sérii souhrn ''
        # zůstává (dočasné Σ dvojpočítání na přechodový den je menší zlo než
        # nevratná ztráta jejího příspěvku) a jeho přítomnost v `previous`
        # drží snímek nefinální, takže se čtení obnovuje, dokud série nedodá.
        missing_keys = {(s.symbol, s.expiry, s.strike, s.right) for s in missing}
        complete_keys = {
            (r.symbol, r.expiry, r.strike, r.right, r.day)
            for r in deduped
            if r.trading_class and (r.symbol, r.expiry, r.strike, r.right) not in missing_keys
        }
        if complete_keys:
            await asyncio.to_thread(self._repository.supersede_legacy, complete_keys)
        if missing:
            logger.warning("OI archivace %s: %d kontraktů bez OI", day, len(missing))
        return ArchiveResult(
            written=len(deduped),
            missing=tuple(missing),
            changed_values=changed_values,
            missing_archived=missing_archived,
        )

    async def _fetch_one(self, spec: OptionContractSpec) -> ContractSnapshot | OIMiss:
        try:
            return await self._fetcher.fetch_snapshot(spec, self._settings.batch_timeout_s)
        except Exception:
            logger.exception("fetch_snapshot selhal pro %s", spec)
            return OIMiss.ERROR
