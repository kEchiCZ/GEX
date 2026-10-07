"""Trvalé úložiště setupů (ADR-0004): historie analýz pro kalibraci.

Tabulka záměrně nemá delete API (jako oi_eod) — výsledky setupů jsou dataset,
ze kterého se časem kalibruje confidence. Mutace po uzavření jsou tři: ruční
hodnocení uživatele (rating + poznámka), oprava výsledku přepočtem z barů
(`correct_outcome`, #1320) — ta původní hodnoty uchová v kontextu — a značka
vyřazení ze statistik (`exclude`, #1346), která výsledek nemění, jen označí.
"""

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.sql.elements import ColumnElement

from gexlens_engine.compute.confidence import CalibrationRow
from gexlens_engine.compute.risk import RealizedSetup
from gexlens_engine.compute.setup_summary import SetupFact, fact_from_record
from gexlens_engine.compute.setups import (
    EXCLUDED_KEY,
    SETUP_MECHANICS_VERSION,
    born_after_settle,
    counts_in_stats,
    excluded_reason,
)
from gexlens_engine.compute.setupstats import ClosedSetup


def _naive_utc(value: dt.datetime) -> dt.datetime:
    """sqlite vrací naivní čas — je to UTC."""
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value


def _born_after_settle(expiry: str, created_ts: dt.datetime) -> bool:
    """`born_after_settle` nad řádkem DB (#1324) — označení řádku v tabulce."""
    return born_after_settle(expiry, _naive_utc(created_ts))


def row_in_stats(expiry: str, created_ts: dt.datetime, context: object) -> bool:
    """`counts_in_stats` nad řádkem DB (#1324, #1346).

    Setup vzniklý po settle vlastní expirace nebo se značkou `context.excluded`
    čtení pro brzdy, bránu šablon, kalibraci, sebekontrolu, kouče i gamma
    útes (`next_setups`, #1331) vynechá, výpis tabulky ho jen označí. V DB
    řádek zůstává.
    """
    return counts_in_stats(expiry, _naive_utc(created_ts), context)


def entry_bar_ts(context: object) -> dt.datetime | None:
    """Bar vstupu setupu z kontextu (`entry_bar_ts`, #1320) — bar, jehož close je entry.

    Odtud začíná cesta ceny po restartu enginu i v offline přepočtu. None =
    setup vznikl nad spotem (dávka cyklu bez baru) nebo je starší než #1320.
    """
    if not isinstance(context, dict):
        return None
    value = context.get("entry_bar_ts")
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)


def _utc_iso(value: dt.datetime) -> str:
    """ISO čas v UTC; naivní čas (sqlite) je UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat()


setups_metadata = MetaData()

setups_table = Table(
    "setups",
    setups_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(16), nullable=False),
    Column("expiry", String(8), nullable=False),
    Column("template", String(32), nullable=False),
    Column("direction", String(8), nullable=False),
    Column("created_ts", DateTime(timezone=True), nullable=False),
    Column("entry", Float, nullable=False),
    Column("target", Float, nullable=False),
    Column("stop", Float, nullable=False),
    Column("confidence", Integer, nullable=False),
    Column("reason", Text, nullable=False),
    Column("context", JSON, nullable=False, default=dict),
    Column("status", String(16), nullable=False, default="active"),
    Column("closed_ts", DateTime(timezone=True), nullable=True),
    Column("outcome_r", Float, nullable=True),
    Column("mfe", Float, nullable=True),
    Column("mae", Float, nullable=True),
    Column("user_rating", Integer, nullable=True),  # null / +1 / −1
    Column("user_note", Text, nullable=True),
    # Verze mechaniky, která setup vyrobila (#311) — statistiky a kalibrace
    # počítají jen aktuální, aby se nemíchaly výsledky různých systémů.
    # Řádky z doby před zavedením sloupce dostanou 1 (viz `ensure_schema`).
    Column("mechanics_version", Integer, nullable=False, server_default="1"),
    # Verze parametrů (#794 fáze 2, ADR-0033): prahy šablon, se kterými setup
    # vznikl. Odděleno od mechaniky — změna prahů srovnatelnost neláme, jen
    # dovolí track record rozdělit podle verzí. NULL = před parameter store.
    Column("params_version", Integer, nullable=True),
)


@dataclass(frozen=True)
class StoredSetup:
    id: int
    symbol: str
    expiry: str
    template: str
    direction: str
    created_ts: dt.datetime
    entry: float
    target: float
    stop: float
    confidence: int
    reason: str
    status: str
    #: Bar vstupu z kontextu (#1320); None = nad spotem nebo řádek před #1320
    entry_bar_ts: dt.datetime | None = None
    #: `context.gate_overridden` (#1323): otevřený setup zkoušky čerpá její
    #: rozpočet i po restartu enginu
    gate_overridden: bool = False


class SetupsRepository:
    """CRUD nad setups (bez delete — R4 duch platí i tady)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        setups_metadata.create_all(self._engine)
        self._ensure_mechanics_version()
        self._ensure_column("params_version", "INTEGER NULL")

    def _ensure_column(self, name: str, ddl_type: str) -> None:
        """Idempotentní ALTER pro sloupce přidané po vzniku tabulky (vzor #311)."""
        inspector = inspect(self._engine)
        if not inspector.has_table(setups_table.name):
            return
        columns = {col["name"] for col in inspector.get_columns(setups_table.name)}
        if name in columns:
            return
        with self._engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {setups_table.name} ADD COLUMN {name} {ddl_type}"))

    def _ensure_mechanics_version(self) -> None:
        """Doplní sloupec `mechanics_version` do existující tabulky (#311).

        `create_all` existující tabulku nemění, takže nasazené instance by
        sloupec nedostaly. ALTER je idempotentní přes kontrolu inspektorem
        a běží na PG i sqlite (testy). Staré řádky tím dostanou verzi 1 —
        hranice tak vyjde přirozeně správně: všechno před touto migrací
        vzniklo starou mechanikou nebo nad zmrzlými daty (ADR-0015).
        """
        inspector = inspect(self._engine)
        if not inspector.has_table(setups_table.name):
            return
        columns = {col["name"] for col in inspector.get_columns(setups_table.name)}
        if "mechanics_version" in columns:
            return
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {setups_table.name} "
                    "ADD COLUMN mechanics_version INTEGER NOT NULL DEFAULT 1"
                )
            )

    def create(
        self,
        *,
        symbol: str,
        expiry: str,
        template: str,
        direction: str,
        created_ts: dt.datetime,
        entry: float,
        target: float,
        stop: float,
        confidence: int,
        reason: str,
        context: dict[str, Any],
        params_version: int | None = None,
    ) -> int:
        stmt = insert(setups_table).values(
            symbol=symbol,
            expiry=expiry,
            template=template,
            direction=direction,
            created_ts=created_ts,
            entry=entry,
            target=target,
            stop=stop,
            confidence=confidence,
            reason=reason,
            context=json.loads(json.dumps(context, default=str)),
            status="active",
            mechanics_version=SETUP_MECHANICS_VERSION,
            params_version=params_version,
        )
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        key = result.inserted_primary_key
        if key is None:
            raise RuntimeError("Insert setupu nevrátil primární klíč")
        return int(key[0])

    def close(
        self,
        setup_id: int,
        *,
        status: str,
        closed_ts: dt.datetime,
        outcome_r: float,
        mfe: float,
        mae: float,
    ) -> None:
        stmt = (
            update(setups_table)
            .where(setups_table.c.id == setup_id)
            .values(status=status, closed_ts=closed_ts, outcome_r=outcome_r, mfe=mfe, mae=mae)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def correct_outcome(
        self,
        setup_id: int,
        *,
        status: str,
        closed_ts: dt.datetime,
        outcome_r: float,
        mfe: float,
        mae: float,
        reason: str,
        corrected_ts: dt.datetime,
    ) -> bool:
        """Oprava výsledku uzavřeného setupu přepočtem z barů (#1320).

        Nic se nemaže: přepíše se status, `closed_ts`, `outcome_r` a MFE/MAE
        (ze stejné cesty ceny jako výsledek — stop s MAE menším než riziko by
        si protiřečil) a původní hodnoty se uloží do `context.outcome_correction`
        (`old_status`, `old_outcome_r`, `old_closed_ts`, `old_mfe`, `old_mae`,
        `corrected_ts`, `reason`). Opakovaná oprava drží PRVNÍ původní hodnoty
        (ty z enginu), obnoví jen čas a důvod. Aktivní setup se neopravuje
        (vyhodnotí ho engine). Vrací False, když řádek neexistuje nebo je aktivní.
        """
        with self._engine.begin() as conn:
            row = conn.execute(
                select(
                    setups_table.c.status,
                    setups_table.c.closed_ts,
                    setups_table.c.outcome_r,
                    setups_table.c.mfe,
                    setups_table.c.mae,
                    setups_table.c.context,
                ).where(setups_table.c.id == setup_id)
            ).fetchone()
            if row is None or row.status == "active":
                return False
            context = dict(row.context) if isinstance(row.context, dict) else {}
            previous = context.get("outcome_correction")
            if isinstance(previous, dict) and "old_status" in previous:
                original = {
                    key: previous.get(key)
                    for key in (
                        "old_status",
                        "old_outcome_r",
                        "old_closed_ts",
                        "old_mfe",
                        "old_mae",
                    )
                }
            else:
                old_closed = row.closed_ts
                if isinstance(old_closed, dt.datetime):
                    old_closed = _utc_iso(old_closed)
                original = {
                    "old_status": row.status,
                    "old_outcome_r": row.outcome_r,
                    "old_closed_ts": old_closed,
                    "old_mfe": row.mfe,
                    "old_mae": row.mae,
                }
            context["outcome_correction"] = {
                **original,
                "corrected_ts": _utc_iso(corrected_ts),
                "reason": reason,
            }
            conn.execute(
                update(setups_table)
                .where(setups_table.c.id == setup_id)
                .values(
                    status=status,
                    closed_ts=closed_ts,
                    outcome_r=outcome_r,
                    mfe=mfe,
                    mae=mae,
                    context=json.loads(json.dumps(context, default=str)),
                )
            )
        return True

    def exclude(self, setup_id: int, *, reason: str, detail: str, excluded_ts: dt.datetime) -> bool:
        """Trvale vyřadí uzavřený setup ze statistik značkou `context.excluded` (#1346).

        Nic se nemaže ani nepřepisuje: výsledek, časy i ostatní klíče kontextu
        zůstávají, přibude `{"reason", "detail", "ts"}`. Čtenáři historie se
        ptají `counts_in_stats`. Idempotentní: řádek se značkou se nemění (drží
        první čas a důvod). Vrací False, když řádek neexistuje, je aktivní
        (vyhodnocuje ho engine) nebo značku už má.
        """
        if not reason:
            raise ValueError("Značka vyřazení potřebuje důvod")
        with self._engine.begin() as conn:
            row = conn.execute(
                select(setups_table.c.status, setups_table.c.context).where(
                    setups_table.c.id == setup_id
                )
            ).fetchone()
            if row is None or row.status == "active" or excluded_reason(row.context) is not None:
                return False
            context = dict(row.context) if isinstance(row.context, dict) else {}
            context[EXCLUDED_KEY] = {
                "reason": reason,
                "detail": detail,
                "ts": _utc_iso(excluded_ts),
            }
            conn.execute(
                update(setups_table)
                .where(setups_table.c.id == setup_id)
                .values(context=json.loads(json.dumps(context, default=str)))
            )
        return True

    def review(self, setup_id: int, rating: int | None, note: str | None) -> bool:
        stmt = (
            update(setups_table)
            .where(setups_table.c.id == setup_id)
            .values(user_rating=rating, user_note=note)
        )
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        return result.rowcount > 0

    def enrich_context(
        self, setup_id: int, extra: dict[str, Any], *, drop: Iterable[str] = ()
    ) -> None:
        """Doplní klíče do `context` JSON — backfill pásmových metrik (#575).

        Merge, ne replace: existující klíče (atr, risk, gex_regime…) zůstávají;
        stejné klíče se přepíšou (idempotentní opakovaný backfill). `drop`
        odstraní klíče, které nová verze metrik už neměří (#1057: ostrost
        z v2 nesmí zůstat vedle verze 3 jen proto, že ji v3 nevydala).
        """
        if not extra:
            return
        with self._engine.begin() as conn:
            row = conn.execute(
                select(setups_table.c.context).where(setups_table.c.id == setup_id)
            ).fetchone()
            if row is None:
                return
            current = {k: v for k, v in (row.context or {}).items() if k not in set(drop)}
            merged = {**current, **json.loads(json.dumps(extra, default=str))}
            conn.execute(
                update(setups_table).where(setups_table.c.id == setup_id).values(context=merged)
            )

    def active_for(self, symbol: str) -> list[StoredSetup]:
        stmt = select(setups_table).where(
            setups_table.c.symbol == symbol, setups_table.c.status == "active"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            StoredSetup(
                id=row.id,
                symbol=row.symbol,
                expiry=row.expiry,
                template=row.template,
                direction=row.direction,
                created_ts=row.created_ts,
                entry=row.entry,
                target=row.target,
                stop=row.stop,
                confidence=row.confidence,
                reason=row.reason,
                status=row.status,
                entry_bar_ts=entry_bar_ts(row.context),
                gate_overridden=isinstance(row.context, dict)
                and row.context.get("gate_overridden") is True,
            )
            for row in rows
        ]

    def closed_between(
        self,
        since: dt.datetime,
        until: dt.datetime,
        *,
        mechanics_version: int | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Uzavřené setupy napříč symboly s `created_ts` v [since, until) — vstup
        kouče (#1201): řádek jako dict s ISO časy a kontextem. Setupy mimo
        statistiky (`counts_in_stats`: po settle #1324, značka #1346) se vynechají."""
        stmt = select(setups_table).where(
            setups_table.c.status != "active",
            setups_table.c.created_ts >= since,
            setups_table.c.created_ts < until,
        )
        if mechanics_version is not None:
            stmt = stmt.where(setups_table.c.mechanics_version == mechanics_version)
        if symbol is not None:
            stmt = stmt.where(setups_table.c.symbol == symbol)
        stmt = stmt.order_by(setups_table.c.created_ts.asc())
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            if not row_in_stats(row.expiry, row.created_ts, row.context):
                continue
            record = dict(row._mapping)
            for key in ("created_ts", "closed_ts"):
                value = record.get(key)
                if isinstance(value, dt.datetime):
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=dt.UTC)
                    record[key] = value.isoformat()
            result.append(record)
        return result

    def closed_since(
        self, symbol: str, since: dt.datetime, *, mechanics_version: int | None = None
    ) -> list[ClosedSetup]:
        """Uzavřené setupy s `closed_ts` od `since` — podklad sebekontroly (#309).

        Řadí se podle času uzavření, ne vzniku: setup otevřený před oknem, ale
        uzavřený v něm, do bilance okna patří.

        `mechanics_version` omezí bilanci na jeden systém (#311) — bez něj by se
        míchaly výsledky staré a nové mechaniky a verdikt by mluvil o minulosti.
        Setupy mimo statistiky (`counts_in_stats`: po settle #1324, značka
        #1346) se vynechají.
        """
        stmt = select(
            setups_table.c.expiry,
            setups_table.c.created_ts,
            setups_table.c.template,
            setups_table.c.direction,
            setups_table.c.status,
            setups_table.c.outcome_r,
            setups_table.c.context,
        ).where(
            setups_table.c.symbol == symbol,
            setups_table.c.status != "active",
            setups_table.c.closed_ts.is_not(None),
            setups_table.c.closed_ts >= since,
        )
        if mechanics_version is not None:
            stmt = stmt.where(setups_table.c.mechanics_version == mechanics_version)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            ClosedSetup(
                template=row.template,
                direction=row.direction,
                status=row.status,
                outcome_r=float(row.outcome_r or 0.0),
            )
            for row in rows
            if row_in_stats(row.expiry, row.created_ts, row.context)
        ]

    def realized_since(self, since: dt.datetime, *, mechanics_version: int) -> list[RealizedSetup]:
        """Uzavřené setupy napříč symboly s `closed_ts >= since` — brzdy, brána
        šablon (#1185) a čerpání zkoušek (#1323). Brzdy sčítají napříč symboly,
        brána si vybere vlastní symbol (`affordable_results`, #1325). `tradeable`,
        `contracts` a `gate_overridden` z kontextu; řádky před pravidly nesou
        None / None / False.

        Setupy mimo statistiky (`counts_in_stats`) se vynechají: vznik po settle
        vlastní expirace (#1324) nemohl existovat, setup se značkou
        `context.excluded` (#1346) stál na ceně, kterou trh neměl — nesmí
        nafukovat `n` brány, čerpat zkoušku ani pohnout brzdami. V DB zůstávají."""
        stmt = select(
            setups_table.c.symbol,
            setups_table.c.expiry,
            setups_table.c.template,
            setups_table.c.status,
            setups_table.c.created_ts,
            setups_table.c.outcome_r,
            setups_table.c.closed_ts,
            setups_table.c.entry,
            setups_table.c.stop,
            setups_table.c.context,
        ).where(
            setups_table.c.status != "active",
            setups_table.c.closed_ts.is_not(None),
            setups_table.c.closed_ts >= since,
            setups_table.c.mechanics_version == mechanics_version,
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result: list[RealizedSetup] = []
        for row in rows:
            if not row_in_stats(row.expiry, row.created_ts, row.context):
                continue
            context = row.context if isinstance(row.context, dict) else {}
            tradeable = context.get("tradeable")
            affordable = context.get("affordable")
            contracts = context.get("contracts")
            closed_ts = row.closed_ts
            if closed_ts.tzinfo is None:  # sqlite vrací naivní čas
                closed_ts = closed_ts.replace(tzinfo=dt.UTC)
            created_ts = row.created_ts
            if created_ts.tzinfo is None:
                created_ts = created_ts.replace(tzinfo=dt.UTC)
            result.append(
                RealizedSetup(
                    symbol=str(row.symbol),
                    template=str(row.template),
                    status=str(row.status),
                    outcome_r=float(row.outcome_r or 0.0),
                    closed_ts=closed_ts,
                    tradeable=tradeable if isinstance(tradeable, bool) else None,
                    affordable=affordable if isinstance(affordable, bool) else None,
                    entry=float(row.entry),
                    stop=float(row.stop),
                    created_ts=created_ts,
                    gate_overridden=context.get("gate_overridden") is True,
                    contracts=(
                        contracts
                        if isinstance(contracts, int) and not isinstance(contracts, bool)
                        else None
                    ),
                )
            )
        return result

    def closed_for_calibration(self, *, mechanics_version: int) -> list[CalibrationRow]:
        """Uzavřené setupy aktuální mechaniky napříč symboly (#794 fáze 2B).

        Výhra = `closed_target` (stejně jako `setupstats`); gamma režim z
        `context.gex_regime` (None u řádků bez něj). Timeout není výhra.
        Setup mimo statistiky (`counts_in_stats`) se vynechá — vznik po settle
        (#1324) by okamžitým timeoutem snižoval confidence košů a s ní práh
        pushe, setup se značkou `context.excluded` (#1346) nešel zadat.
        """
        stmt = select(
            setups_table.c.symbol,
            setups_table.c.expiry,
            setups_table.c.template,
            setups_table.c.status,
            setups_table.c.created_ts,
            setups_table.c.context,
        ).where(
            setups_table.c.status != "active",
            setups_table.c.mechanics_version == mechanics_version,
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result: list[CalibrationRow] = []
        for row in rows:
            if not row_in_stats(row.expiry, row.created_ts, row.context):
                continue
            context = row.context if isinstance(row.context, dict) else {}
            regime = context.get("gex_regime")
            result.append(
                CalibrationRow(
                    symbol=str(row.symbol),
                    template=str(row.template),
                    gex_regime=str(regime) if isinstance(regime, str) else None,
                    win=row.status == "closed_target",
                )
            )
        return result

    @staticmethod
    def _list_filters(
        symbol: str, date: dt.date | None, status: str | None
    ) -> list[ColumnElement[bool]]:
        """Podmínky výpisu — sdílí je stránka (`list_for`) i její počet (`count_for`)."""
        filters: list[ColumnElement[bool]] = [setups_table.c.symbol == symbol]
        if status is not None:
            filters.append(setups_table.c.status == status)
        if date is not None:
            start = dt.datetime.combine(date, dt.time.min, tzinfo=dt.UTC)
            filters.append(setups_table.c.created_ts >= start)
            filters.append(setups_table.c.created_ts < start + dt.timedelta(days=1))
        return filters

    def count_for(
        self, symbol: str, *, date: dt.date | None = None, status: str | None = None
    ) -> int:
        """Počet řádků, ze kterých `list_for` bere stránku — „zobrazeno N z M" (#1319)."""
        stmt = (
            select(func.count())
            .select_from(setups_table)
            .where(*self._list_filters(symbol, date, status))
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def summary_facts(self, symbols: Sequence[str]) -> list[SetupFact]:
        """Všechny setupy symbolů BEZ stropu — vstup serverového souhrnu (#1319).

        Stránka `list_for` (limit 200) je jen pro tabulku; agregace nad ní byla
        klouzavé okno posledních 200 setupů. Čtou se jen sloupce, které souhrn
        potřebuje (bez `reason`, `mfe`/`mae`, hodnocení).
        """
        stmt = select(
            setups_table.c.id,
            setups_table.c.symbol,
            setups_table.c.expiry,
            setups_table.c.template,
            setups_table.c.status,
            setups_table.c.created_ts,
            setups_table.c.closed_ts,
            setups_table.c.outcome_r,
            setups_table.c.entry,
            setups_table.c.stop,
            setups_table.c.mechanics_version,
            setups_table.c.context,
        ).where(setups_table.c.symbol.in_(list(symbols)))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [fact_from_record(dict(row._mapping)) for row in rows]

    def list_for(
        self,
        symbol: str,
        *,
        date: dt.date | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        # id jako druhý klíč: 31 dvojic setupů téhož symbolu má shodný
        # created_ts (dvě šablony v jedné minutě) — bez něj je pořadí náhodné
        stmt = (
            select(setups_table)
            .where(*self._list_filters(symbol, date, status))
            .order_by(setups_table.c.created_ts.desc(), setups_table.c.id.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row._mapping)
            # Tabulka řádek ukáže, ale označí — ze souhrnu, brzd i brány je
            # vyřazený (#1324, #1346); filtr „Jen obchodovatelné“ ho skryje
            record["after_settle"] = _born_after_settle(row.expiry, row.created_ts)
            record["excluded"] = excluded_reason(row.context)
            for key in ("created_ts", "closed_ts"):
                value = record.get(key)
                if isinstance(value, dt.datetime):
                    record[key] = value.isoformat()
            result.append(record)
        return result
