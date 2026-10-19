"""Úložiště a job kalibrace α (#232, ADR-0011 fáze 2).

Job běží v pipeline po finálním OI archivu dne (#1314, hned za FA validací):
včerejší konec dne řady netflow (řez 16:00 CT — konec trade date) se
porovná s ΔOI mezi archivy na stranách změřených po publikaci CME
(#1605), medián poměrů dá denní bod a EMA přes dny
aktualizuje α symbolu. Historie bodů zůstává v `fa_alpha_history` pro audit
(vč. buy/sell mediánů — případná asymetrie se rozhodne až nad daty).
Aktuální α per symbol drží `fa_alpha`; čte ji engine (runtime.flow_alpha)
i API (/fa/alpha pro frontend badge).

Pod tisky (mechanika v6, ADR-0057 bod 6, #1460) se kalibruje řetěz, který byl
celou seanci D sekundárem (první expirace po D, #1580), jen z netflow se zdrojem
tisků, a den s mezerou trade větve bod nedá. Stav i body nesou zdroj (`source`,
NULL = midpoint); stav jiného zdroje se bere jako nekalibrovaný, takže α se při
bumpu resetuje sama a FA jede až po `FA_MIN_DAYS` dnech.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

from gexlens_engine.compute.cumdelta import SOURCE_AGGRESSOR, SOURCE_MIDPOINT
from gexlens_engine.compute.facalibration import (
    FA_MIN_DAYS,
    MIN_ABS_NET,
    AlphaCalibrationPoint,
    Key,
    calibrate_alpha,
    update_alpha,
)
from gexlens_engine.compute.marketclock import daily_close_utc
from gexlens_engine.compute.settle import is_trading_session, session_bounds
from gexlens_engine.compute.setups import CUMDELTA_PRINTS
from gexlens_engine.storage.fa_validation import PublicationUtc, measured_oi_pair
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.parquet_store import (
    read_netflow_last_minute,
    read_netflow_latest,
    read_netgap_minutes,
)

logger = logging.getLogger(__name__)

metadata = MetaData()

# Aktuální kalibrovaná α per symbol + počet započtených validačních dnů
fa_alpha_table = Table(
    "fa_alpha",
    metadata,
    Column("symbol", String(16), primary_key=True),
    Column("alpha", Float, nullable=False),
    Column("days", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # Zdroj netu bodů, ze kterých stav vznikl (ADR-0057 bod 8); NULL = midpoint
    Column("source", String(16), nullable=True),
)

# Historie denních bodů pro audit — surové mediány bez EMA a bez clampu
fa_alpha_history_table = Table(
    "fa_alpha_history",
    metadata,
    Column("symbol", String(16), primary_key=True),
    Column("day", Date, primary_key=True),  # den, jehož netflow se kalibroval
    Column("expiry", String(16), nullable=False),
    Column("samples", Integer, nullable=False),
    Column("ratio_median", Float, nullable=False),
    Column("ratio_buy", Float, nullable=True),
    Column("ratio_sell", Float, nullable=True),
    Column("alpha_after", Float, nullable=False),
    # Zdroj netu bodu (ADR-0057 bod 8); NULL = midpoint (body před #1460)
    Column("source", String(16), nullable=True),
)

#: Sloupce přidané po založení tabulek — jen aditivní ADD COLUMN (vzor journal)
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "fa_alpha": {"source": "VARCHAR(16)"},
    "fa_alpha_history": {"source": "VARCHAR(16)"},
}
#: Den pod tisky je dokončený, má-li netflow řádek nejvýš tolik minut před řezem
#: (po rollu v settle je řetěz aktivní a píše každou minutu)
DAY_END_TOLERANCE_MIN = 5


@dataclass(frozen=True)
class FaAlphaState:
    """Aktuální α symbolu pro engine i API."""

    symbol: str
    alpha: float
    days: int
    updated_at: dt.datetime
    # Zdroj netu (ADR-0057 bod 8); None = midpoint (stav před #1460)
    source: str | None = None


@dataclass(frozen=True)
class AlphaCalibrationResult:
    """Výsledek jednoho ranního kalibračního běhu (pro log a alert)."""

    symbol: str
    day: dt.date
    expiry: str
    point: AlphaCalibrationPoint
    alpha_after: float
    days: int


class FaAlphaRepository:
    """Přístup k tabulkám fa_alpha / fa_alpha_history; upserty jsou idempotentní."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        """Tabulky + aditivní sloupce (`ADDED_COLUMNS`). Volá ho engine i API.

        Na PostgreSQL `ADD COLUMN IF NOT EXISTS`: start enginu a první dotaz API
        můžou migraci spustit souběžně.
        """
        metadata.create_all(self._engine)
        inspector = inspect(self._engine)
        if_not_exists = " IF NOT EXISTS" if self._engine.dialect.name == "postgresql" else ""
        with self._engine.begin() as conn:
            for table, columns in ADDED_COLUMNS.items():
                present = {column["name"] for column in inspector.get_columns(table)}
                for name, sql_type in columns.items():
                    if name not in present:
                        conn.execute(
                            text(f"ALTER TABLE {table} ADD COLUMN{if_not_exists} {name} {sql_type}")
                        )

    def get(self, symbol: str) -> FaAlphaState | None:
        stmt = select(
            fa_alpha_table.c.alpha,
            fa_alpha_table.c.days,
            fa_alpha_table.c.updated_at,
            fa_alpha_table.c.source,
        ).where(fa_alpha_table.c.symbol == symbol)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        return FaAlphaState(
            symbol=symbol, alpha=row[0], days=row[1], updated_at=row[2], source=row[3]
        )

    def list_all(self) -> list[FaAlphaState]:
        stmt = select(
            fa_alpha_table.c.symbol,
            fa_alpha_table.c.alpha,
            fa_alpha_table.c.days,
            fa_alpha_table.c.updated_at,
            fa_alpha_table.c.source,
        ).order_by(fa_alpha_table.c.symbol)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            FaAlphaState(symbol=row[0], alpha=row[1], days=row[2], updated_at=row[3], source=row[4])
            for row in rows
        ]

    def history_exists(self, symbol: str, day: dt.date) -> bool:
        stmt = select(fa_alpha_history_table.c.symbol).where(
            fa_alpha_history_table.c.symbol == symbol,
            fa_alpha_history_table.c.day == day,
        )
        with self._engine.connect() as conn:
            return conn.execute(stmt).first() is not None

    def record(
        self,
        symbol: str,
        day: dt.date,
        expiry: str,
        point: AlphaCalibrationPoint,
        alpha_after: float,
        days: int,
        now: dt.datetime | None = None,
        *,
        source: str,
        update_state: bool = True,
    ) -> None:
        """Uloží denní bod do historie a přepíše aktuální α symbolu (transakčně).

        `update_state=False` zapíše jen historii (audit bodu, který α nemění):
        symbol bez kalibrace tak nedostane řádek `fa_alpha` s α 0 (#1314).
        `source` je povinný (#1460): zapomenutý zdroj by se při čtení tiše
        vydával za midpoint (NULL).
        """
        now = now or dt.datetime.now(dt.UTC)
        history_row = {
            "symbol": symbol,
            "day": day,
            "expiry": expiry,
            "samples": point.samples,
            "ratio_median": point.ratio_median,
            "ratio_buy": point.ratio_buy,
            "ratio_sell": point.ratio_sell,
            "alpha_after": alpha_after,
            "source": source,
        }
        alpha_row = {
            "symbol": symbol,
            "alpha": alpha_after,
            "days": days,
            "updated_at": now,
            "source": source,
        }
        with self._engine.begin() as conn:
            conn.execute(self._upsert(fa_alpha_history_table, history_row, ["symbol", "day"]))
            if update_state:
                conn.execute(self._upsert(fa_alpha_table, alpha_row, ["symbol"]))

    def set_state(
        self,
        symbol: str,
        alpha: float,
        days: int,
        now: dt.datetime | None = None,
        *,
        source: str | None = None,
    ) -> None:
        """Přepíše aktuální α symbolu (přepočet historie `scripts/recompute_fa.py`, #1314).

        Bez `source` zůstane zdroj stavu beze změny (přepočet α nemění).
        """
        row: dict[str, object] = {
            "symbol": symbol,
            "alpha": alpha,
            "days": days,
            "updated_at": now or dt.datetime.now(dt.UTC),
        }
        if source is not None:
            row["source"] = source
        with self._engine.begin() as conn:
            conn.execute(self._upsert(fa_alpha_table, row, ["symbol"]))

    def _upsert(self, table: Table, row: dict[str, object], primary_key: list[str]) -> Executable:
        update_cols = {k: v for k, v in row.items() if k not in primary_key}
        dialect = self._engine.dialect.name
        if dialect == "postgresql":
            pg_stmt = pg_insert(table).values([row])
            return pg_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={k: pg_stmt.excluded[k] for k in update_cols},
            )
        if dialect == "sqlite":
            sqlite_stmt = sqlite_insert(table).values([row])
            return sqlite_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={k: sqlite_stmt.excluded[k] for k in update_cols},
            )
        raise ValueError(f"Nepodporovaný databázový dialekt pro upsert: {dialect!r}")


def live_alpha(state: FaAlphaState | None, *, prints: bool) -> float | None:
    """α pro FA vrstvu runtime; None = platí konfigurace (jen mechanika v5).

    Pod tisky (ADR-0057 bod 6) bere jen stav kalibrovaný z tisků s aspoň
    `FA_MIN_DAYS` dny, jinak 0 = FA vypnutá: stav z midpointu je jiná veličina
    a výchozí α z konfigurace by FA pustila nekalibrovanou.
    """
    if not prints:
        return state.alpha if state is not None else None
    if state is None or state.source != SOURCE_AGGRESSOR or state.days < FA_MIN_DAYS:
        return 0.0
    return state.alpha


def _previous_session(today: dt.date) -> dt.date:
    """Poslední obchodní den před `today` (pondělí → pátek, svátky NYSE přeskočí)."""
    day = today - dt.timedelta(days=1)
    while not is_trading_session(day):
        day -= dt.timedelta(days=1)
    return day


def _calibrated_chain(exp_dirs: list[Path], session: dt.date) -> list[Path]:
    """Řetěz, který byl celou seanci `session` sekundárem: první expirace po ní (#1580).

    Další expirace (D+2) píše netflow dne D až po rollu v settle (~20:05 UTC)
    a její bod by kalibroval 55 minut toku; náhradní řetěz se proto nehledá.
    """
    chains = sorted(
        (path for path in exp_dirs if path.name.isdigit() and len(path.name) == 8),
        key=lambda path: path.name,
    )
    for path in chains:
        if dt.datetime.strptime(path.name, "%Y%m%d").date() > session:
            return [path]
    return []


def _session_gaps(exp_dir: Path, session: dt.date) -> list[dt.datetime]:
    """Minuty mezery trade větve řetězu v seanci `session` do řezu dne (#1460).

    Seance začíná v 17:00 CT předchozího dne, takže leží ve dvou UTC particích.
    """
    start = session_bounds(session)[0]
    cutoff = daily_close_utc(session)
    gaps: list[dt.datetime] = []
    for day in sorted({start.date(), session}):
        path = exp_dir / "netgap" / f"{day.isoformat()}.parquet"
        gaps.extend(ts for ts in read_netgap_minutes(path) if start <= ts <= cutoff)
    return sorted(gaps)


def netflow_at_cutoff(
    path: Path, day: dt.date, *, sources: frozenset[str | None] | None = None
) -> dict[Key, float]:
    """Poslední kumulativní net každé strany k řezu 16:00 CT (konec trade date).

    Stejný řez jako FA validace (`daily_close_utc`: v létě 21:00 UTC, v zimě
    22:00 UTC): po přestávce začíná další seance a kumulativ už patří novému
    trade date (counter volume IBKR se resetuje v 17:00 CT). Řádek přesně
    v řezu nese tok do řezu, proto se počítá (`end` je řez + 1 min, okno
    `read_netflow_latest` je zprava otevřené). Čte se přes
    `schema=` (#1460), staré partice bez sloupce `source` projdou. `sources`
    pod tisky = jen řádky zdroje tisků (den před bumpem bod nedá); None = bez
    filtru (mechanika v5).
    """
    cutoff = daily_close_utc(day)
    return read_netflow_latest(path, end=cutoff + dt.timedelta(minutes=1), sources=sources)


def collect_alpha_calibration(
    symbol: str,
    derived_dir: Path,
    oi_repository: OIEodRepository,
    alpha_repository: FaAlphaRepository,
    today: dt.date,
    *,
    publication_utc: PublicationUtc,
    cumdelta_prints: bool = CUMDELTA_PRINTS,
) -> AlphaCalibrationResult | None:
    """Spočítá a uloží chybějící kalibrační bod symbolu k dnešnímu OI archivu.

    Netflow píše aktivní i sekundární řetěz (#1182); den má typicky dvě expirace
    s daty a projde jen ta, která v den netflow ještě neexpirovala.
    Bere se poslední archivní OBCHODNÍ den < today, který má netflow partici
    a OI v obou dnech; hotové dny přeskakuje (idempotentní dedup v historii).
    Víkendový `today` se nekalibruje (#1309): archiv soboty je kopie pátku,
    bod „pátek → sobota" vyšel s mediánem 0 (fa_alpha_history 18. a 25. 9.
    2026) a dedup dne pak zablokoval poctivý bod „pátek → pondělí".
    ΔOI jen na stranách změřených po publikaci v obou dnech
    (`measured_oi_pair`, #1605); bez nich se bod neukládá a zkusí se znovu
    (i po rollu nad večerním archivem). Blokující (parquet + DB) — volat
    přes to_thread.

    `cumdelta_prints` (mechanika v6, ADR-0057 bod 6): jen řetěz, který byl
    celou seanci D sekundárem (#1580), jen netflow ze zdroje tisků, den
    s mezerou trade větve nebo s řadou ukončenou před řezem bod nedá a stav
    z midpointu se bere jako nekalibrovaný. Pod v5 beze změny.
    """
    base = derived_dir / symbol
    if not is_trading_session(today) or not base.is_dir():
        return None
    exp_dirs = sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)
    session: dt.date | None = None
    sources: frozenset[str | None] | None = None
    if cumdelta_prints:
        session = _previous_session(today)
        exp_dirs = _calibrated_chain(exp_dirs, session)
        sources = frozenset({SOURCE_AGGRESSOR})
    for exp_dir in exp_dirs:
        expiry = exp_dir.name
        netflow_dir = exp_dir / "netflow"
        if not netflow_dir.is_dir():
            continue
        previous = oi_repository.latest_trading_day_before(symbol, expiry, today)
        if previous is None or alpha_repository.history_exists(symbol, previous):
            continue
        if session is not None and previous != session:
            logger.info(
                "Kalibrace α %s %s: poslední archiv před %s je %s, ne seance %s — bez bodu",
                symbol,
                expiry,
                today,
                previous,
                session,
            )
            continue
        # Řetěz, který v den netflow (nebo dřív) expiroval, žádné „ΔOI do
        # dalšího dne" nemá — archiv D+1 pro něj buď chybí, nebo nese tytéž
        # hodnoty (ΔOI = 0 na všech stranách). Aktivní řetěz ES/NQ je každý den
        # 0DTE, takže tudy kalibrace neprojde nikdy; poctivý vstup je jen
        # řetěz s expirací po dni netflow (#1172).
        if dt.datetime.strptime(expiry, "%Y%m%d").date() <= previous:
            logger.info(
                "Kalibrace α %s %s %s: řetěz v den netflow expiroval — bez ΔOI, přeskočeno",
                symbol,
                expiry,
                previous,
            )
            continue
        netflow_path = netflow_dir / f"{previous.isoformat()}.parquet"
        if not netflow_path.exists():
            continue
        oi_before, oi_after, before_tasty = measured_oi_pair(
            oi_repository, symbol, expiry, today, previous, publication_utc
        )
        if not oi_after and not oi_repository.values_for(symbol, expiry, today):
            continue  # mrtvá expirace — dnešní archiv ji nenese
        if session is not None and not _day_complete(
            symbol, expiry, exp_dir, netflow_path, session, sources
        ):
            continue
        netflow = netflow_at_cutoff(netflow_path, previous, sources=sources)
        # ΔOI jen pro strany změřené po publikaci v OBOU dnech (#1605);
        # ostatní zůstávají bez klíče a kalibrace je přeskočí (#1172) — nula
        # za chybějící hodnotu by dala ΔOI = −oi_before nebo přesně 0
        doi = {key: oi_after[key] - oi_before[key] for key in oi_after.keys() & oi_before.keys()}
        qualifying = [key for key, net in netflow.items() if abs(net) >= MIN_ABS_NET]
        no_after = sum(1 for key in qualifying if key not in oi_after)
        no_before = sum(1 for key in qualifying if key in oi_after and key not in oi_before)
        tasty_before = sum(1 for key in qualifying if key in doi and key in before_tasty)
        logger.info(
            "Kalibrace α %s %s %s: %d stran s |net| ≥ %.0f, vyřazeno %d bez měření "
            "po publikaci (D+1 %d, D %d), D tasty %d",
            symbol,
            expiry,
            previous,
            len(qualifying),
            MIN_ABS_NET,
            no_after + no_before,
            no_after,
            no_before,
            tasty_before,
        )
        if not doi:
            logger.info(
                "Kalibrace α %s %s %s: bez stran změřených po publikaci v obou dnech "
                "(D+1 %d, D %d stran) — bod se neukládá, zkusí se znovu",
                symbol,
                expiry,
                previous,
                len(oi_after),
                len(oi_before),
            )
            continue
        if all(value == 0.0 for value in doi.values()):
            logger.info(
                "Kalibrace α %s %s %s: archivy %s a %s jsou shodné (ΔOI všude 0) — bod se neukládá",
                symbol,
                expiry,
                previous,
                previous,
                today,
            )
            continue
        point = calibrate_alpha(netflow, doi)
        if point is None:
            logger.info(
                "Kalibrace α %s %s %s: nedostatečný vzorek — bod se neukládá",
                symbol,
                expiry,
                previous,
            )
            continue
        state = alpha_repository.get(symbol)
        if cumdelta_prints and state is not None and state.source != SOURCE_AGGRESSOR:
            # Stav z midpointu je jiná veličina (ADR-0057 bod 6): první bod
            # z tisků začíná znovu, historie zůstává a nese zdroj
            state = None
        source = SOURCE_AGGRESSOR if cumdelta_prints else SOURCE_MIDPOINT
        if point.ratio_median <= 0.0:
            # Nulový nebo záporný medián = tok se do OI nepropsal nebo jsou
            # data rozbitá; není to důkaz pro α = 0 (vypnutí vrstvy). Bod jde
            # do historie pro audit, α i počet dnů zůstávají (#1172). Symbol
            # bez kalibrace stav nedostane vůbec: dřív se tu zapsal `fa_alpha`
            # s α 0, runtime ho převzal a FA vrstva NQ byla vypnutá (#1314);
            # v historii pak `alpha_after` 0 znamená „ještě nekalibrováno".
            logger.warning(
                "Kalibrace α %s %s %s: medián %.3f ≤ 0 (%d stran) — α beze změny",
                symbol,
                expiry,
                previous,
                point.ratio_median,
                point.samples,
            )
            alpha_repository.record(
                symbol,
                previous,
                expiry,
                point,
                state.alpha if state else 0.0,
                state.days if state else 0,
                source=source,
                update_state=state is not None,
            )
            continue
        # Stav s 0 dny (ruční reset `set_state`) nemá α k vyhlazení — první bod jde přímo
        alpha_after = update_alpha(
            state.alpha if state is not None and state.days > 0 else None, point.ratio_median
        )
        days = (state.days if state else 0) + 1
        alpha_repository.record(symbol, previous, expiry, point, alpha_after, days, source=source)
        return AlphaCalibrationResult(
            symbol=symbol,
            day=previous,
            expiry=expiry,
            point=point,
            alpha_after=alpha_after,
            days=days,
        )
    return None


def _day_complete(
    symbol: str,
    expiry: str,
    exp_dir: Path,
    netflow_path: Path,
    session: dt.date,
    sources: frozenset[str | None] | None,
) -> bool:
    """Je net seance pod tisky úplný? Bez mezery trade větve a s řadou až k řezu.

    Mezeru nese značka `netgap` (i po restartu: první minuta za otevřeného trhu
    je mezera). Engine spadlý přes konec dne značku do seance D nezapíše, proto
    řada netflow musí končit nejvýš `DAY_END_TOLERANCE_MIN` před řezem.
    """
    gaps = _session_gaps(exp_dir, session)
    if gaps:
        logger.info(
            "Kalibrace α %s %s %s: mezera trade větve v %d minutách (první %s UTC) "
            "— bod se neukládá",
            symbol,
            expiry,
            session,
            len(gaps),
            gaps[0].strftime("%H:%M"),
        )
        return False
    cutoff = daily_close_utc(session)
    last = read_netflow_last_minute(
        netflow_path, end=cutoff + dt.timedelta(minutes=1), sources=sources
    )
    if last is None or last < cutoff - dt.timedelta(minutes=DAY_END_TOLERANCE_MIN):
        logger.info(
            "Kalibrace α %s %s %s: netflow končí %s, řez je %s UTC — den nedokončený, "
            "bod se neukládá",
            symbol,
            expiry,
            session,
            "bez řádku" if last is None else last.strftime("%H:%M"),
            cutoff.strftime("%H:%M"),
        )
        return False
    return True
