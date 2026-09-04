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
from collections.abc import Callable
from pathlib import Path

import pandas as pd

# Jedna sdílená definice hranic (ADR-0023 bod 1): seanci definuje engine
# compute/settle; API ji jen re-exportuje pro své testy a konzumenty (#638)
from gexlens_engine.compute.settle import session_bounds
from gexlens_engine.config import Settings

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


class DataRepository:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

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
