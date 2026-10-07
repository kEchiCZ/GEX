"""Empirický model fáze 1 (#279, SPEC 2.4 a 5.2) — čisté agregace bez I/O.

„Učení" první iterace není ML, ale **empirické rozdělení reakcí**: pro nový
event se lookupne, jak se trh choval u stejného bucketu v minulosti. Je to
plně inspektovatelné — dá se ukázat, na kolika případech odhad stojí a jak
byly rozptýlené.

Dvě věci, které rozhodují o tom, jestli model neučí šum:

* **Kontaminovaná okna se nezapočítávají** (SPEC 2.4). Okno, do kterého spadl
  jiný high-impact event, neměří reakci na tuhle zprávu.
* **Deferred okna tvoří vlastní buckety.** Gap na open po víkendu má jinou
  dynamiku než okamžitá reakce; smíchat je znamená rozmazat obojí.
* **Jedno měření = jeden vzorek** (#1293). Vzorky téhož bucketu se stejným
  `ts_event` jsou tentýž pohyb trhu — typicky řádky FF jednoho releasu
  (CPI m/m, Core CPI m/m, y/y) se shodným časem i výnosem. Počítat je zvlášť
  by nafouklo `n` a gate by se otevřel na pseudoreplikacích. U deferred
  reakcí je jedno měření celá **uzavírka trhu** (#1311): všechny zprávy
  víkendu nebo denní pauzy mají tutéž základní cenu před uzavřením i první
  bar po otevření, tedy shodný výnos — klíčem je `closure_open`, ne čas
  zprávy (GEOPOLITICS|1 deferred ~370 vzorků na ~130 uzavírek).

Spolehlivost se reportuje jako `n` a σ, u hit-rate navíc Wilsonova dolní mez —
bodová úspěšnost při malém n je nerozlišitelná od mince.
"""

import datetime as dt
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from gexlens_engine.compute.setupstats import wilson_lower_bound

# Hranice bucketů překvapení v jednotkách σ historických překvapení řady.
# |z| < 0.5 je v šumu konsensu, > 1.5 je skutečné překvapení.
SURPRISE_SMALL = 0.5
SURPRISE_LARGE = 1.5
# Bucket pro eventy bez měřitelného překvapení (headlines, chybějící forecast)
SURPRISE_NONE = "none"


@dataclass(frozen=True)
class ReactionSample:
    """Jedno změřené okno i s atributy události, podle kterých se bucketuje."""

    category: str | None
    importance: int | None
    surprise_z: float | None
    symbol: str
    window_min: int
    ret_bp: float
    contaminated: bool
    deferred: bool
    # Směr z klasifikace (N3); None = zatím neklasifikováno → do hit-rate nejde
    sentiment_dir: int | None = None
    # Režimové dimenze (#402); None = nezjištěno → jen do nepodmíněného agregátu
    state: str | None = None
    gex_regime: str | None = None
    # Čas eventu — klíč sloučení souběžných vzorků téhož bucketu (#1293);
    # None = samostatné měření
    ts_event: dt.datetime | None = None
    # První obchodovaný bar po uzavírce (#1311) — klíč sloučení deferred
    # vzorků jedné uzavírky; None (nedeferred nebo před backfillem) = `ts_event`
    closure_open: dt.datetime | None = None

    @property
    def measurement_key(self) -> dt.datetime | None:
        """Klíč jednoho měření: uzavírka u deferred, jinak čas zprávy."""
        if self.deferred and self.closure_open is not None:
            return self.closure_open
        return self.ts_event


@dataclass(frozen=True)
class BucketKey:
    category: str
    importance: int
    surprise_bucket: str
    deferred: bool
    window_min: int
    symbol: str


@dataclass(frozen=True)
class BucketStats:
    """Agregát jednoho bucketu — odpovídá řádku `news_model_stats`."""

    key: BucketKey
    n: int
    ret_mean_bp: float
    ret_median_bp: float
    ret_sigma_bp: float
    hit_rate: float | None
    hit_rate_lb: float | None

    @property
    def expected_direction(self) -> int:
        """Očekávaný směr reakce podle znaménka průměru; 0 = bez signálu."""
        if self.ret_mean_bp > 0:
            return 1
        if self.ret_mean_bp < 0:
            return -1
        return 0


def surprise_bucket(surprise_z: float | None) -> str:
    """Kategorie překvapení; None → `none` (headlines forecast nemají)."""
    if surprise_z is None:
        return SURPRISE_NONE
    magnitude = abs(surprise_z)
    if magnitude < SURPRISE_SMALL:
        return "flat"
    sign = "pos" if surprise_z > 0 else "neg"
    size = "large" if magnitude >= SURPRISE_LARGE else "small"
    return f"{sign}_{size}"


def aggregate_samples(samples: Sequence[ReactionSample]) -> list[BucketStats]:
    """Nepodmíněné rozdělení reakcí per bucket — režim 'all' z `aggregate_by_regime`.

    Jedna cesta agregace: kontaminovaná okna a nezařazené eventy se zahazují,
    souběžné vzorky téhož bucketu se slučují stejně jako v nočním jobu.
    """
    return [stats for regime, stats in aggregate_by_regime(samples) if regime == "all"]


def lookup(
    stats: Sequence[BucketStats],
    *,
    category: str,
    importance: int,
    surprise_z: float | None,
    deferred: bool,
    window_min: int,
    symbol: str,
) -> BucketStats | None:
    """Historické rozdělení pro nový event (SPEC 5.2) — jádro „učení" fáze 1."""
    key = BucketKey(
        category=category,
        importance=importance,
        surprise_bucket=surprise_bucket(surprise_z),
        deferred=deferred,
        window_min=window_min,
        symbol=symbol,
    )
    for item in stats:
        if item.key == key:
            return item
    return None


REGIME_ORDER = ("all", "RiskOn", "RiskOff", "Neutral", "gamma_positive", "gamma_negative")
_GAMMA_LABELS = {"positive": "gamma_positive", "negative": "gamma_negative"}


class _Accumulator:
    """Minimum, co bucket potřebuje: výnosy a počty zásahů — ne celé vzorky.

    Souběžné vzorky (stejný `measurement_key`: čas zprávy, u deferred první
    bar po uzavírce, #1293/#1311) jsou jedno měření: drží se jako rozpracované
    a zapíší se, až přijde jiný klíč. Směr sloučeného měření je převaha směrů
    (remíza = neposuzuje se), výnos je z prvního vzorku — u souběžných eventů
    i u zpráv jedné uzavírky je shodný (tatáž základní cena i okna nad
    stejnými bary). Proto musí stream jít vzestupně podle `ts_event`
    (`ModelStatsJob.iter_samples`) — deferred vzorky jedné uzavírky jsou pak
    v bucketu za sebou; vzorek bez klíče je vždy samostatné měření.
    """

    __slots__ = ("hits", "judged", "pending_dir", "pending_ret", "pending_ts", "returns")

    def __init__(self) -> None:
        self.returns: list[float] = []
        self.judged = 0
        self.hits = 0
        self.pending_ts: dt.datetime | None = None
        self.pending_ret = 0.0
        self.pending_dir = 0

    def add(self, sample: ReactionSample) -> None:
        # Směr pro hit-rate: ±1, jinak 0 (neklasifikováno nebo neutrální)
        vote = 1 if sample.sentiment_dir == 1 else -1 if sample.sentiment_dir == -1 else 0
        key = sample.measurement_key
        if key is not None and key == self.pending_ts:
            self.pending_dir += vote
            return
        self._flush()
        if key is None:
            self._record(sample.ret_bp, vote)
            return
        self.pending_ts = key
        self.pending_ret = sample.ret_bp
        self.pending_dir = vote

    def _flush(self) -> None:
        if self.pending_ts is not None:
            self._record(self.pending_ret, self.pending_dir)
            self.pending_ts = None

    def _record(self, ret_bp: float, direction_votes: int) -> None:
        self.returns.append(ret_bp)
        # Hit-rate jen z klasifikovaných eventů — u neklasifikovaných není
        # co porovnávat a doplňovat nulou by úspěšnost uměle stlačilo
        direction = (direction_votes > 0) - (direction_votes < 0)
        if direction != 0:
            self.judged += 1
            if (ret_bp > 0 and direction == 1) or (ret_bp < 0 and direction == -1):
                self.hits += 1

    def stats(self, key: BucketKey) -> BucketStats:
        self._flush()
        returns = self.returns
        return BucketStats(
            key=key,
            n=len(returns),
            ret_mean_bp=statistics.fmean(returns),
            ret_median_bp=statistics.median(returns),
            ret_sigma_bp=statistics.pstdev(returns) if len(returns) > 1 else 0.0,
            hit_rate=self.hits / self.judged if self.judged else None,
            hit_rate_lb=wilson_lower_bound(self.hits, self.judged) if self.judged else None,
        )


def aggregate_by_regime(samples: Iterable[ReactionSample]) -> list[tuple[str, BucketStats]]:
    """Agregáty per režim (#402): 'all' + per stav + per GEX režim.

    Podmíněné pohledy jsou PARALELNÍ k nepodmíněnému, ne náhrada — dělení
    ředí n a Wilson gate si každý pohled hlídá sám. Vzorky bez zjištěného
    režimu do dané podmíněné větve prostě nevstupují (žádné dopočítávání).

    Jeden průchod nad streamem (#1105 bod 2): dřív se všech ~2 M oken drželo
    jako seznam dataclass objektů a pak se 5× filtrovalo do dalších seznamů —
    ~1 GB RSS news-engine po půlnoci. Teď bucket drží jen výnosy a čítače.

    Souběžné vzorky téhož bucketu se slučují do jednoho měření (#1293, viz
    `_Accumulator`) — stream musí být seřazený vzestupně podle `ts_event`,
    jinak se sloučí jen sousední.
    """
    grouped: dict[tuple[str, BucketKey], _Accumulator] = {}
    for sample in samples:
        if sample.contaminated:
            continue
        if sample.category is None or sample.importance is None:
            continue
        key = BucketKey(
            category=sample.category,
            importance=sample.importance,
            surprise_bucket=surprise_bucket(sample.surprise_z),
            deferred=sample.deferred,
            window_min=sample.window_min,
            symbol=sample.symbol,
        )
        regimes = ["all"]
        if sample.state in ("RiskOn", "RiskOff", "Neutral"):
            regimes.append(sample.state)
        gamma = _GAMMA_LABELS.get(sample.gex_regime or "")
        if gamma is not None:
            regimes.append(gamma)
        for regime in regimes:
            acc = grouped.get((regime, key))
            if acc is None:
                acc = grouped[(regime, key)] = _Accumulator()
            acc.add(sample)
    out: list[tuple[str, BucketStats]] = []
    for regime in REGIME_ORDER:
        items = [acc.stats(key) for (group, key), acc in grouped.items() if group == regime]
        items.sort(key=lambda s: (s.key.category, s.key.importance, s.key.window_min))
        out.extend((regime, item) for item in items)
    return out
