"""Čtení denních partic pro REST endpoints (SPEC kap. 6).

Repository jen čte, co engine zapsal (snapshots/derived Parquet) — API server
nemá vlastní stav ani zápis.

Obchodní den = Globex seance (ADR-0023 bod 3, #512): osa dne D je
[17:00 America/Chicago dne D−1, 17:00 CT dne D). Úložiště zůstává klíčované
UTC kalendářním dnem; sešití probíhá tady ve čtecí vrstvě — `session_frame`
spojí partici D s večerem partice D−1 a ořízne na okno seance. Polouzavřený
interval zaručuje, že každá minuta patří právě jedné seanci (žádné dvojí
započtení na hranici z konstrukce).
"""

import datetime as dt
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd

from gexlens_api.candles import partials_from_frame

# Jedna sdílená definice hranic (ADR-0023 bod 1): seanci definuje engine
# compute/settle; API ji jen re-exportuje pro své testy a konzumenty (#638)
from gexlens_engine.compute.settle import session_bounds
from gexlens_engine.config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "DataRepository",
    "OutsideDataDirError",
    "PartitionNotFoundError",
    "session_bounds",
]


class PartitionNotFoundError(FileNotFoundError):
    """Požadovaná denní partice neexistuje → HTTP 404."""


class OutsideDataDirError(PartitionNotFoundError):
    """Cesta by vedla mimo datový adresář (pokus o traversal, #542 M6).

    Dědí z `PartitionNotFoundError`, takže se ven tváří jako běžné 404 —
    útočník se z odpovědi nedozví, že narazil na kontrolu.
    """


#: Paralelní čtení partic při studeném průchodu svíček (#1089); pyarrow pouští GIL.
PARTITION_READ_WORKERS = 8
#: Partice mladší než tolik dnů se považují za dopisované (stat mtime), starší za neměnné.
MUTABLE_PARTITION_DAYS = 3
#: Výpis adresáře partic se drží tolik sekund.
LISTING_TTL_S = 60.0
#: Surové partice pro intradenní svíčky — jednotky souborů per symbol, strop napříč symboly.
BARS_FRAME_CACHE_MAX = 64


class DataRepository:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Cache pro svíčky (#1089): denní agregáty per partice, surové partice
        # posledních dnů pro intradenní koše, výpis adresáře s TTL
        self._daily_partials_cache: dict[tuple[Path, int], pd.DataFrame] = {}
        self._bars_frame_cache: dict[tuple[Path, int], pd.DataFrame] = {}
        self._listing_cache: dict[str, tuple[float, list[dt.date]]] = {}

    def _resolve(self, path: Path) -> Path:
        """Ověří, že cesta zůstala uvnitř datového adresáře.

        `symbol` a `expiry` chodí z URL a skládají se do cest bez validace —
        `..` se přes ně dostane až sem. Kontrola je záměrně v jednom místě
        pod všemi metodami, ne u každého path parametru zvlášť.
        """
        root = self._settings.data_dir.resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise OutsideDataDirError(str(path))
        return resolved

    def list_symbols(self) -> list[str]:
        return self._list_dirs(self._settings.snapshots_dir)

    def list_expiries(self, symbol: str) -> list[str]:
        return self._list_dirs(self._settings.snapshots_dir / symbol)

    def list_days(self, symbol: str) -> list[dict[str, str]]:
        """Uložené dny napříč expiracemi (Daily pohled) — den nese svou expiraci.

        0DTE řetěz: každý den má typicky vlastní expiraci; při více expiracích
        se stejným dnem vyhrává nejbližší (nejmenší) expirace.
        """
        by_date: dict[str, str] = {}
        for expiry in self.list_expiries(symbol):
            try:
                expiry_dir = self._resolve(self._settings.snapshots_dir / symbol / expiry)
            except OutsideDataDirError:
                continue
            for partition in expiry_dir.glob("*.parquet"):
                day = partition.stem
                current = by_date.get(day)
                if current is None or expiry < current:
                    by_date[day] = expiry
        return [{"date": day, "expiry": by_date[day]} for day in sorted(by_date)]

    def snapshots(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        path = self._settings.snapshots_dir / symbol / expiry / f"{day.isoformat()}.parquet"
        return self._read(path)

    def levels(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        path = (
            self._settings.derived_dir / symbol / expiry / "levels" / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def levels2(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        """Sekundární zdi (ADR-0008, #92) — vlastní řada vedle levels."""
        path = (
            self._settings.derived_dir / symbol / expiry / "levels2" / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def oi_missing(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        """Striky bez OI (#465) — v běžný den řada neexistuje a čtení skončí prázdné."""
        path = (
            self._settings.derived_dir
            / symbol
            / expiry
            / "oimissing"
            / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def oi_filled(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        """Striky s OI doplněným z tasty Summary (#664) — bez fillu řada neexistuje."""
        path = (
            self._settings.derived_dir / symbol / expiry / "oifilled" / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def catch_up(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        """Catch-up minuty (#518, ADR-0024) — když engine běžel celý den, řada neexistuje."""
        path = (
            self._settings.derived_dir / symbol / expiry / "catchup" / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def ladder(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        """GEX žebřík (#244) — top-N významných striků per strana a minutu."""
        path = (
            self._settings.derived_dir / symbol / expiry / "ladder" / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def levelsfa(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        """Flow-adjusted levels (ADR-0011, #222) — OI odhad z klasifikovaného toku."""
        path = (
            self._settings.derived_dir / symbol / expiry / "levelsfa" / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def oiest(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        """OI odhad z klasifikovaného toku (#232, ADR-0011 fáze 2).

        Jen strany lišící se od měřeného OI; bez toku řada neexistuje a čtení
        skončí prázdné (PartitionNotFoundError → bundle drží tvar).
        """
        path = self._settings.derived_dir / symbol / expiry / "oiest" / f"{day.isoformat()}.parquet"
        return self._read(path)

    def printvol(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        """Podíl objemu mimo tisk (#1007): přírůstek per kontrakt a minutu
        rozložený na tisky TimeAndSale a zbytek (spready, bloky). NULL =
        trade větev neběžela. Jen řádky s přírůstkem; bez sběru řada chybí."""
        path = (
            self._settings.derived_dir / symbol / expiry / "printvol" / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def oiwalls(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        """OI zdi (#851) — hladiny z denního OI, vlastní řada vedle levels.

        Jiná veličina než gamma zdi: maximum otevřeného zájmu, ne maximum
        NetGEX profilu. Kreslí se proto odlišeně a nese vlastní podíl (share).
        """
        path = (
            self._settings.derived_dir / symbol / expiry / "oiwalls" / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def walldom(self, symbol: str, expiry: str, day: dt.date) -> pd.DataFrame:
        """Dominance zdí (ADR-0010, #223) — vlastní řada vedle levels."""
        path = (
            self._settings.derived_dir / symbol / expiry / "walldom" / f"{day.isoformat()}.parquet"
        )
        return self._read(path)

    def gexprofile(
        self, symbol: str, expiry: str, day: dt.date, *, subdir: str = "gexprofile"
    ) -> pd.DataFrame:
        """Profil plochy per minuta (ADR-0009, #203/#204): gexprofile/charmprofile/vannaprofile."""
        path = self._settings.derived_dir / symbol / expiry / subdir / f"{day.isoformat()}.parquet"
        return self._read(path)

    def gexfield(
        self, symbol: str, expiry: str, day: dt.date, *, subdir: str = "gexfield"
    ) -> pd.DataFrame:
        """Modelované pole plochy (ADR-0009 fáze 2) — partice drží jen poslední stav."""
        path = self._settings.derived_dir / symbol / expiry / subdir / f"{day.isoformat()}.parquet"
        return self._read(path)

    def gexforward(self, symbol: str, day: dt.date) -> pd.DataFrame:
        """Forward GEX (#519): bloky per budoucí obchodní den — poslední stav dne."""
        path = self._settings.derived_dir / symbol / "gexforward" / f"{day.isoformat()}.parquet"
        return self._read(path)

    def flow(self, symbol: str, day: dt.date) -> pd.DataFrame:
        path = self._settings.derived_dir / symbol / "flow" / f"{day.isoformat()}.parquet"
        return self._read(path)

    def bars(self, symbol: str, day: dt.date) -> pd.DataFrame:
        path = self._settings.derived_dir / symbol / "bars" / f"{day.isoformat()}.parquet"
        return self._read(path)

    def bars_partition_days(self, symbol: str) -> list[dt.date]:
        """UTC dny, pro které existuje partice barů (vzestupně).

        Výpis adresáře se drží `LISTING_TTL_S` — přes bind mount Docker Desktopu
        stojí každý souborový syscall milisekundy a Briefing se ptá každou minutu.
        """
        cached = self._listing_cache.get(symbol)
        now = time.monotonic()
        if cached is not None and now - cached[0] < LISTING_TTL_S:
            return cached[1]
        root = self._settings.derived_dir / symbol / "bars"
        try:
            resolved = self._resolve(root)
        except OutsideDataDirError:
            return []
        if not resolved.exists():
            return []
        days: list[dt.date] = []
        for entry in resolved.iterdir():
            if entry.suffix != ".parquet":
                continue
            try:
                days.append(dt.date.fromisoformat(entry.stem))
            except ValueError:
                continue
        days.sort()
        self._listing_cache[symbol] = (now, days)
        return days

    def _partition_key(self, symbol: str, day: dt.date) -> tuple[Path, int] | None:
        """Klíč cache partice: (cesta, mtime) jen pro dopisované dny, jinak (cesta, 0).

        Engine přepisuje jen dnešní partici a backfill po výpadku nejbližší dny
        (#221) — starší jsou neměnné a stat per soubor by přes bind mount stál
        víc než samotné čtení (změřeno 9. 9.: 563 statů ≈ 12 s). None = soubor zmizel.
        """
        path = self._bars_path(symbol, day)
        if day < dt.datetime.now(dt.UTC).date() - dt.timedelta(days=MUTABLE_PARTITION_DAYS):
            return (path, 0)
        try:
            return (path, self._resolve(path).stat().st_mtime_ns)
        except FileNotFoundError:
            return None

    def _forget_other_versions(
        self, cache: dict[tuple[Path, int], Any], key: tuple[Path, int]
    ) -> None:
        """Přepsaná partice: zahodí záznamy téže cesty s jiným mtime."""
        for other in [k for k in cache if k[0] == key[0] and k != key]:
            del cache[other]

    def _bars_path(self, symbol: str, day: dt.date) -> Path:
        return self._settings.derived_dir / symbol / "bars" / f"{day.isoformat()}.parquet"

    def bars_recent(self, symbol: str, calendar_days: int) -> pd.DataFrame:
        """Bary posledních `calendar_days` partic sešité vzestupně (#1089).

        Pro intradenní svíčky — jednotky partic, čte se rovnou. Bez jediné
        partice → 404 jako jinde.
        """
        days = self.bars_partition_days(symbol)[-calendar_days:]
        if not days:
            raise PartitionNotFoundError(f"{symbol}/bars")
        frames: list[pd.DataFrame] = []
        for day in days:
            key = self._partition_key(symbol, day)
            if key is None:
                continue
            frame = self._bars_frame_cache.get(key)
            if frame is None:
                frame = self._read(key[0])
                self._forget_other_versions(self._bars_frame_cache, key)
                if len(self._bars_frame_cache) >= BARS_FRAME_CACHE_MAX:
                    self._bars_frame_cache.pop(next(iter(self._bars_frame_cache)))
                self._bars_frame_cache[key] = frame
            frames.append(frame)
        if not frames:
            raise PartitionNotFoundError(f"{symbol}/bars")
        return pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]

    def daily_partials(self, symbol: str, calendar_days: int) -> pd.DataFrame:
        """Denní agregáty per partice pro posledních `calendar_days` dnů (#1089).

        Partice se ke svíčkám D/W redukuje na ≤ 2 řádky (část seance D, část
        seance D+1) a ty se drží v paměti pod klíčem (cesta, mtime) — týdenní
        trend z 2 let barů tak nečte 500 souborů při každém obnovení Briefingu.
        Klíč přes mtime drží cache správnou i pro dnešní dopisovanou partici.
        Studený průchod čte paralelně (pyarrow pouští GIL).
        """
        days = self.bars_partition_days(symbol)[-calendar_days:]
        if not days:
            raise PartitionNotFoundError(f"{symbol}/bars")
        keys = [key for key in (self._partition_key(symbol, day) for day in days) if key]
        missing = [key for key in keys if key not in self._daily_partials_cache]
        if missing:
            with ThreadPoolExecutor(max_workers=PARTITION_READ_WORKERS) as pool:
                loaded = list(
                    pool.map(lambda key: partials_from_frame(self._read(key[0])), missing)
                )
            for key, partial in zip(missing, loaded, strict=True):
                self._forget_other_versions(self._daily_partials_cache, key)
                self._daily_partials_cache[key] = partial
        frames = [self._daily_partials_cache[key] for key in keys]
        return (
            pd.concat(frames, ignore_index=True) if frames else partials_from_frame(pd.DataFrame())
        )

    def warm_daily_partials(self) -> None:
        """Zahřeje cache denních agregátů pro všechny symboly s bary (start API).

        Běží v démonovém vlákně při startu; chyby jen loguje — Briefing si
        chybějící partici přečte sám při prvním dotazu.
        """
        for symbol in self._list_dirs(self._settings.derived_dir):
            if not self.bars_partition_days(symbol):
                continue
            try:
                self.daily_partials(symbol, 10_000)
            except Exception:  # noqa: BLE001 — zahřátí nesmí shodit start
                logger.exception("Zahřátí cache svíček %s selhalo", symbol)

    def bars_session(self, symbol: str, day: dt.date) -> pd.DataFrame:
        """Bary seance sešité z partic D−1 + D, jedna minuta jednou (#1002).

        Engine do 3. 9. 2026 zapisoval půlnoční bar a rekonstruovaný večerní blok
        i do partice sousedního dne; po sešití byla minuta dvakrát a objem
        dvojnásobný. Vyhrává první výskyt = partice D−1, kam večerní minuty
        podle UTC dne patří. Obrana i pro staré partice, které čistící skript
        `scripts/fix_bar_partitions.py` ještě neprošel.
        """
        frame = self.session_frame(lambda d: self.bars(symbol, d), day)
        return frame.drop_duplicates(subset="ts_min", keep="first").reset_index(drop=True)

    def session_frame(
        self,
        read: Callable[[dt.date], pd.DataFrame],
        day: dt.date,
        *,
        ts_col: str = "ts_min",
    ) -> pd.DataFrame:
        """Sešije osu obchodního dne (#512): partice D−1 + D oříznuté na seanci.

        `read` je čtečka jedné denní partice (např. `lambda d: self.levels(...)`).
        Chybějící partice na jedné straně nevadí (nedělní seance má jen večer
        v sobotní/nedělní partici, pondělní ráno zase jen D); chybí-li obě,
        letí PartitionNotFoundError — stejné 404 chování jako dosud.
        """
        start, end = session_bounds(day)
        frames: list[pd.DataFrame] = []
        for partition_day in (day - dt.timedelta(days=1), day):
            try:
                frames.append(read(partition_day))
            except PartitionNotFoundError:
                continue
        if not frames:
            raise PartitionNotFoundError(f"{day.isoformat()} (seance {start}–{end})")
        joined = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        mask = (joined[ts_col] >= start) & (joined[ts_col] < end)
        return joined.loc[mask].reset_index(drop=True)

    def _list_dirs(self, root: Path) -> list[str]:
        try:
            resolved = self._resolve(root)
        except OutsideDataDirError:
            return []
        if not resolved.exists():
            return []
        return sorted(entry.name for entry in resolved.iterdir() if entry.is_dir())

    def _read(self, path: Path) -> pd.DataFrame:
        resolved = self._resolve(path)
        if not resolved.exists():
            raise PartitionNotFoundError(str(path))
        return pd.read_parquet(resolved)
