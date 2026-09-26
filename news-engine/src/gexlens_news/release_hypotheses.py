"""Registr předem stanovených hypotéz o reakci na releasy (#1296 fáze 4, ADR-0044).

Definice, kritéria i historické počty jsou **konstanty v kódu a v ADR-0044**,
ne v DB — dodatečně se měnit nesmějí. Revize = nová hypotéza s novým ID a datem
registrace, ne úprava té staré (jinak by se kritérium ladilo podle výsledku).

* Počítají se jen **živé** releasy od `REGISTERED_AT`; historie výzkumu je jen
  popisná (`historical`).
* Rozhoduje se jen v **kontrolních bodech** n = 10, 20, 30 hodnocených releasů
  (varianta B: falešné ověření při skutečných 50 % 3,7 %, průběžná kontrola od
  n ≥ 10 by dala 8,6 %). Mezi nimi stav trvá.
* **Ověřeno**: dolní mez Wilsonova 95% intervalu > 50 %; **zamítnuto**: horní mez
  < 50 %, nebo n = 30 bez ověření (futilita).
* **Vyhodnocený úsek je zmrazený**: releasy do posledního vyhodnoceného
  kontrolního bodu (u rozhodnuté hypotézy do rozhodnutí) i stav se přebírají
  z předchozího řádku `release_hypotheses` (`FrozenPrefix`) — pozdní oprava dat
  ani přeměření rozhodnutí nepřepíše; přepočet jen přičítá releasy mimo úsek.
* Pravděpodobnost smí do upozornění jen u ověřené hypotézy.
* Vstupy kritérií (řady H1, rodina H3, rodiny a baseline M1) mají tu **vlastní
  kopii** — sdílené konstanty upozornění (`releases`) a news_anomaly
  (`reactions`) se smějí ladit, tyto ne (hlídá `test_strazce_registru`).

Všechno tu jsou čisté funkce nad řádky `release_moves` (`MoveFacts`).
"""

import datetime as dt
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from gexlens_engine.compute.setupstats import wilson_lower_bound

#: Registrace — releasy před tímto okamžikem se živě nepočítají nikdy
REGISTERED_AT = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
#: Kontrolní body (počet hodnocených živých releasů); rozhoduje se jen v nich
CHECKPOINTS = (10, 20, 30)
#: z pro Wilsonův 95% interval
WILSON_Z = 1.96
#: Hranice ověření / zamítnutí (podíl zásahů)
DECISION_THRESHOLD = 0.5

# ── Vstupy kritérií — vlastní zmrazené kopie (registrace 26. 9. 2026) ──
#: H1: headline jádra inflace (upozornění čte směr odsud, ne naopak)
H1_SERIES = frozenset({"Core CPI m/m", "Core PPI m/m", "Core PCE Price Index m/m"})
#: H3: rodina
H3_FAMILY = "CPI"
#: M1: robustní rodiny z výzkumu (slabší řady Retail Sales a ISM Services ne)
M1_FAMILIES = frozenset({"CPI", "NFP", "FOMC", "PPI", "PCE"})
#: M1 baseline běžného dne: kvantil 0,5 (medián) 15min výchylek ±30 min stejné
#: denní doby (ET) za 20 seancí před seancí releasu, aspoň 200 vzorků
M1_BASELINE_SESSIONS = 20
M1_TOD_HALF_WIDTH_MIN = 30
M1_TOD_MIN_SAMPLES = 200
M1_TOD_QUANTILE = 0.5

STATUS_TESTING = "testing"
STATUS_VERIFIED = "verified"
STATUS_REJECTED = "rejected"


@dataclass(frozen=True)
class MoveFacts:
    """Řádek `release_moves` v rozsahu, který hypotézy potřebují."""

    cluster_ts: dt.datetime
    symbol: str
    family: str
    headline: str
    surprise_sign: int | None
    exc_15m_bp: float | None
    ret_15m_bp: float | None
    ret_60m_bp: float | None
    tod_med_15m_bp: float | None


@dataclass(frozen=True)
class Outcome:
    """Výsledek jednoho hodnoceného releasu; `value_bp` = rozhodující veličina."""

    hit: bool
    value_bp: float


def _h1(row: MoveFacts) -> Outcome | None:
    """Teplejší jádro inflace → výnos 15 min < 0."""
    if row.headline not in H1_SERIES or row.surprise_sign != 1:
        return None
    if row.ret_15m_bp is None or row.ret_15m_bp == 0:
        return None
    return Outcome(hit=row.ret_15m_bp < 0, value_bp=row.ret_15m_bp)


def _h3(row: MoveFacts) -> Outcome | None:
    """CPI → ES výš za 60 min (bez ohledu na překvapení)."""
    if row.family != H3_FAMILY or row.ret_60m_bp is None or row.ret_60m_bp == 0:
        return None
    return Outcome(hit=row.ret_60m_bp > 0, value_bp=row.ret_60m_bp)


def _m1(row: MoveFacts) -> Outcome | None:
    """Výchylka za 15 min nad mediánem stejné denní doby běžného dne."""
    if row.family not in M1_FAMILIES:
        return None
    if row.exc_15m_bp is None or row.tod_med_15m_bp is None:
        return None
    return Outcome(hit=row.exc_15m_bp > row.tod_med_15m_bp, value_bp=row.exc_15m_bp)


@dataclass(frozen=True)
class Hypothesis:
    """Předem registrovaná hypotéza — nemění se, revize = nové ID."""

    id: str
    label: str
    rule: str
    symbols: tuple[str, ...]
    #: symbol → (zásahy, n) z výzkumu 30. 7. 2024 – 25. 9. 2026 — jen popisně
    historical: Mapping[str, tuple[int, int]]
    #: Jak smí do upozornění před releasem: „testing“ (hned, bez pravděpodobnosti),
    #: „verified“ (až po ověření), „never“ (bez dalšího rozhodnutí uživatele)
    in_preview: str
    evaluate: Callable[[MoveFacts], Outcome | None] = field(repr=False)


# Změna čehokoli níže = nová hypotéza (nové ID a datum registrace), ne úprava
HYPOTHESES: tuple[Hypothesis, ...] = (
    Hypothesis(
        id="H1",
        label="Teplejší jádro inflace → za 15 min níž",
        rule=(
            "Headline Core CPI/PPI/PCE m/m s překvapením nad odhadem (actual > forecast);"
            " zásah = výnos 15 min < 0"
        ),
        symbols=("ES", "NQ"),
        historical={"ES": (13, 14), "NQ": (13, 14)},
        in_preview="testing",
        evaluate=_h1,
    ),
    Hypothesis(
        id="H3",
        label="Po CPI je ES za 60 min výš",
        rule="Rodina CPI bez ohledu na překvapení; zásah = výnos 60 min > 0",
        symbols=("ES",),
        historical={"ES": (21, 24)},
        in_preview="never",
        evaluate=_h3,
    ),
    Hypothesis(
        id="M1",
        label="Výchylka za 15 min nad běžným dnem",
        rule=(
            "Rodiny CPI, NFP, FOMC, PPI, PCE; zásah = výchylka 15 min nad mediánem"
            " 15min výchylek ±30 min stejné denní doby za 20 předchozích seancí"
        ),
        symbols=("ES", "NQ"),
        historical={"ES": (107, 111), "NQ": (107, 111)},
        in_preview="verified",
        evaluate=_m1,
    ),
)
HYPOTHESIS_BY_ID = {hypothesis.id: hypothesis for hypothesis in HYPOTHESES}


def wilson_bounds(hits: int, n: int) -> tuple[float | None, float | None]:
    """Wilsonův 95% interval; horní mez = 1 − dolní mez neúspěchů. n = 0 → (None, None)."""
    if n <= 0:
        return None, None
    return (
        wilson_lower_bound(hits, n, WILSON_Z),
        1.0 - wilson_lower_bound(n - hits, n, WILSON_Z),
    )


def decide(hits: int, n: int) -> str | None:
    """Rozhodnutí v kontrolním bodě n; None = mimo kontrolní bod nebo bez rozhodnutí."""
    if n not in CHECKPOINTS:
        return None
    lower, upper = wilson_bounds(hits, n)
    if lower is not None and lower > DECISION_THRESHOLD:
        return STATUS_VERIFIED
    if upper is not None and upper < DECISION_THRESHOLD:
        return STATUS_REJECTED
    if n == CHECKPOINTS[-1]:
        return STATUS_REJECTED  # futilita: poslední kontrolní bod bez ověření
    return None


def next_checkpoint(n: int) -> int | None:
    return next((point for point in CHECKPOINTS if point > n), None)


@dataclass(frozen=True)
class FrozenPrefix:
    """Vyhodnocený úsek z předchozího stavu — přepočet ho přebírá beze změny.

    Délka = `decided_at_n` u rozhodnuté hypotézy, jinak poslední kontrolní bod
    ≤ n (kontrola proběhla a skončila „pokračovat“ — ani to se zpětně nemění).
    """

    status: str
    decided_at_n: int | None
    outcomes: tuple[dict[str, Any], ...]


def frozen_prefix(
    status: str, decided_at_n: int | None, outcomes: Sequence[Mapping[str, Any]]
) -> FrozenPrefix | None:
    """Zmrazený úsek z uloženého řádku `release_hypotheses`; None = není co zmrazit."""
    if status != STATUS_TESTING and decided_at_n is not None:
        length = decided_at_n
    else:
        status, decided_at_n = STATUS_TESTING, None
        length = max((point for point in CHECKPOINTS if point <= len(outcomes)), default=0)
    if length == 0:
        return None
    return FrozenPrefix(
        status=status,
        decided_at_n=decided_at_n,
        outcomes=tuple(dict(item) for item in outcomes[:length]),
    )


@dataclass(frozen=True)
class HypothesisState:
    hypothesis: str
    symbol: str
    n: int
    hits: int
    wilson_lb: float | None
    wilson_ub: float | None
    status: str
    decided_at_n: int | None
    outcomes: tuple[dict[str, Any], ...]
    #: Releasy zmrazeného úseku, u kterých by dnešní data dala jiný výsledek (jen log)
    ignored_changes: int = 0

    def to_row(self, computed_at: dt.datetime) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "symbol": self.symbol,
            "n": self.n,
            "hits": self.hits,
            "wilson_lb": self.wilson_lb,
            "wilson_ub": self.wilson_ub,
            "status": self.status,
            "decided_at_n": self.decided_at_n,
            "outcomes": list(self.outcomes),
            "computed_at": computed_at,
        }


def evaluate(
    hypothesis: Hypothesis,
    symbol: str,
    rows: Iterable[MoveFacts],
    *,
    registered_at: dt.datetime = REGISTERED_AT,
    frozen: FrozenPrefix | None = None,
) -> HypothesisState:
    """Stav hypotézy na instrumentu z živých releasů (chronologicky).

    Rozhoduje se jen v okamžiku, kdy počet hodnocených releasů dosáhne
    kontrolního bodu; první rozhodnutí je konečné (další releasy jen přičítají
    k/n pro zobrazení). `frozen` = už vyhodnocený úsek z předchozího stavu:
    jeho releasy a stav se převezmou beze změny, ostatní hodnocené releasy se
    přičtou za něj. `registered_at` mění jen replay (ADR-0044: živě nikdy).
    """
    outcomes: list[dict[str, Any]] = list(frozen.outcomes) if frozen else []
    frozen_hits = {str(item["cluster_ts"]): bool(item["hit"]) for item in outcomes}
    hits = sum(frozen_hits.values())
    n = len(outcomes)
    status = frozen.status if frozen else STATUS_TESTING
    decided_at = frozen.decided_at_n if frozen else None
    ignored = 0
    for row in sorted(rows, key=lambda item: item.cluster_ts):
        if row.symbol != symbol or row.cluster_ts < registered_at:
            continue
        outcome = hypothesis.evaluate(row)
        key = row.cluster_ts.isoformat()
        if key in frozen_hits:
            ignored += int(outcome is None or outcome.hit != frozen_hits[key])
            continue
        if outcome is None:
            continue
        n += 1
        hits += int(outcome.hit)
        outcomes.append(
            {
                "cluster_ts": key,
                "family": row.family,
                "hit": outcome.hit,
                "value_bp": round(outcome.value_bp, 2),
            }
        )
        if decided_at is None:
            decision = decide(hits, n)
            if decision is not None:
                status, decided_at = decision, n
    lower, upper = wilson_bounds(hits, n)
    return HypothesisState(
        hypothesis=hypothesis.id,
        symbol=symbol,
        n=n,
        hits=hits,
        wilson_lb=lower,
        wilson_ub=upper,
        status=status,
        decided_at_n=decided_at,
        outcomes=tuple(outcomes),
        ignored_changes=ignored,
    )


def evaluate_all(
    rows: Sequence[MoveFacts],
    *,
    registered_at: dt.datetime = REGISTERED_AT,
    frozen: Mapping[tuple[str, str], FrozenPrefix] | None = None,
) -> list[HypothesisState]:
    """Stav všech hypotéz × jejich instrumentů (řádky `release_hypotheses`).

    `frozen` = (hypotéza, symbol) → zmrazený úsek z předchozího stavu.
    """
    prefixes = frozen or {}
    return [
        evaluate(
            hypothesis,
            symbol,
            rows,
            registered_at=registered_at,
            frozen=prefixes.get((hypothesis.id, symbol)),
        )
        for hypothesis in HYPOTHESES
        for symbol in hypothesis.symbols
    ]
