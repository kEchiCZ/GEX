"""Risk framework malého účtu (#1185, varianta A, rozhodnutí uživatele 15. 9. 2026).

Čisté funkce bez I/O; orchestraci (čtení track recordu, zápis do kontextu
setupu, alerty) dělá `SetupEngine`.

Účet se v aplikaci vede v jednotkách plného kontraktu (ES 50 $/b, NQ 20 $/b)
jako **50 000 $** — uživatel obchoduje MES/MNQ (1/10), takže 1 kontrakt v
aplikaci = 1 mikro reálně a body sedí 1:1. Riziko na setup 1 % = 500 $ v
aplikaci (50 $ reálně) → pro 1 kontrakt stop nejvýš 10 b ES / 25 b NQ.
Setup se stopem nad rozpočtem VZNIKÁ dál (měření, varianta A ≠ C), ale je
`unaffordable`: šedý v Setupech, bez pushe, mimo obchodovatelné statistiky.

Brzdy (v R obchodovatelných setupů napříč symboly, `brake_state`): −3 R za
seanci zastaví nové obchodovatelné setupy do konce seance, −6 R za obchodní
týden do konce týdne; po druhém stopu téže šablony za seanci
(`max_template_stops_per_day` = 2) je šablona do konce seance stínová. Konec
seance je otevření Globexu v 17:00 CT (`session_bounds`), ne settle 15:00 CT;
konec týdne je otevření pondělní seance v neděli 17:00 CT (`week_start`).
Brána šablon: obchodovatelná je jen šablona na daném symbolu (klíč šablona ×
symbol, #1325), jejíž dolní mez očekávání (jednostranný 95% interval Ø R) za
posledních N seancí je kladná při n ≥ 30 — ostatní se dál měří, ale neobchodují.
Stádium buňky (#1323): uživatel ji může dát do Stínu (blok `user`, po brzdách)
nebo do Zkoušky, která přebije bránu, dokud nevyčerpá rozpočet (`trial_usage`).
Sizing a brzdy nepřebíjí nic.
"""

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from gexlens_engine.compute.settle import session_bounds

#: Verze pravidel sizingu/brzd/brány — do kontextu setupu, aby šly řádky
#: rozlišit. Zvýšit při změně významu hodnot, které engine do kontextu zapisuje.
#: 1 = #1185: brána podle šablony; kvůli nesdílenému slovníku hodnot bodu brala
#:     řádky s `affordable` z obou symbolů a dopočtené řádky před pravidly jen
#:     z vlastního, takže `template_gate` (+ `_n`, `_lb`) a `tradeable` měly
#:     u ES a NQ různý vstup (#1325).
#: 2 = #1325: brána per šablona × symbol, řádky před pravidly dopočtené
#:     hodnotou bodu vlastního symbolu.
#: 3 = #1323: stádium buňky ticker × šablona. Pořadí bloků sizing → brzdy →
#:     `user` (Stín) → brána; aktivní Zkouška přebije verdikt brány block
#:     i insufficient (`gate_overridden`). Kontext nese `user_stage`,
#:     `gate_overridden`, čerpání zkoušky a důkaz buňky (ØR hrubě/čistě,
#:     n potřebné). `tradeable` tak u v3 může být True i při bráně block.
RISK_RULES_VERSION = 3

TradeBlock = Literal[
    "stop_over_budget",
    "stop_over_cap",
    "daily_brake",
    "weekly_brake",
    "template_stops",
    "user",
    "gate",
]
GateVerdict = Literal["pass", "block", "insufficient", "off"]


@dataclass(frozen=True)
class PositionSize:
    stop_points: float
    point_value_usd: float
    risk_budget_usd: float
    contracts: int
    #: Ztráta na stopu při `contracts` kontraktech (0 při neobchodovatelném)
    max_loss_usd: float
    affordable: bool
    block: TradeBlock | None


def position_size(
    entry: float,
    stop: float,
    point_value_usd: float,
    *,
    account_equity_usd: float,
    risk_pct: float,
    risk_max_pct: float,
) -> PositionSize:
    """Fixed-fractional sizing: kontrakty = ⌊rozpočet / (stop b × hodnota bodu)⌋.

    Rozpočet = účet × risk_pct; tvrdý strop účet × risk_max_pct chrání před
    zvednutím risk_pct nad strop (ztráta jednoho setupu nikdy nad 2 %).
    Stop 0 b nebo nesmyslná hodnota bodu = neobchodovatelné, ne výjimka.
    """
    stop_points = abs(entry - stop)
    budget = max(0.0, account_equity_usd * risk_pct / 100.0)
    cap = max(0.0, account_equity_usd * risk_max_pct / 100.0)
    per_contract = stop_points * point_value_usd
    if per_contract <= 0 or budget <= 0:
        return PositionSize(stop_points, point_value_usd, budget, 0, 0.0, False, "stop_over_budget")
    contracts = int(math.floor(budget / per_contract))
    if contracts < 1:
        return PositionSize(stop_points, point_value_usd, budget, 0, 0.0, False, "stop_over_budget")
    max_loss = contracts * per_contract
    if max_loss > cap:
        return PositionSize(stop_points, point_value_usd, budget, 0, 0.0, False, "stop_over_cap")
    return PositionSize(stop_points, point_value_usd, budget, contracts, max_loss, True, None)


@dataclass(frozen=True)
class RealizedSetup:
    """Uzavřený setup pro brzdy a bránu (podmnožina řádku `setups`)."""

    symbol: str
    template: str
    status: str
    outcome_r: float
    closed_ts: dt.datetime
    #: `context.tradeable` řádku; None = řádek před pravidly (#1185)
    tradeable: bool | None
    #: `context.affordable`; None = řádek před pravidly → dopočet z entry/stop
    affordable: bool | None = None
    entry: float = 0.0
    stop: float = 0.0
    #: Vznik setupu — čerpání zkoušky počítá setupy vzniklé od jejího začátku
    #: (#1323); None = zdroj vznik nenese (paper obchody)
    created_ts: dt.datetime | None = None
    #: `context.gate_overridden` (#1323): obchodovatelný jen díky zkoušce
    gate_overridden: bool = False
    #: `context.contracts` — sizing při vzniku (#1185); None = řádek před
    #: pravidly (Knihovna ho dopočte `position_size` jako brána `affordable`)
    contracts: int | None = None


def week_start(session_day: dt.date) -> dt.datetime:
    """Začátek obchodního týdne = open pondělní seance (neděle 17:00 CT)."""
    monday = session_day - dt.timedelta(days=session_day.weekday())
    return session_bounds(monday)[0]


@dataclass(frozen=True)
class BrakeState:
    day_r: float
    week_r: float
    template_stops: int
    block: TradeBlock | None


def brake_state(
    realized: Sequence[RealizedSetup],
    template: str,
    *,
    session_day: dt.date,
    daily_brake_r: float,
    weekly_brake_r: float,
    max_template_stops_per_day: int,
) -> BrakeState:
    """Brzdy z obchodovatelných uzavřených setupů týdne (napříč symboly).

    `realized` smí nést i starší řádky (volající čte i 84denní okno brány
    šablon), proto se den i týden vymezují tady podle `closed_ts`, stejnou
    konvencí polouzavřených intervalů: den = `session_bounds(session_day)`,
    týden = [`week_start(session_day)`, konec seance). Do #1322 se do týdne
    sčítalo celé okno brány — ztráty staré až 84 dní by týdenní brzdu držely
    měsíce. Řádky bez `tradeable` (před #1185) se nepočítají — brzda je o účtu,
    ne o detektoru. Priorita: den → týden → šablona (první, která platí).
    """
    day_from, day_to = session_bounds(session_day)
    week_from = week_start(session_day)
    day_r = week_r = 0.0
    template_stops = 0
    for row in realized:
        if row.tradeable is not True:
            continue
        if not week_from <= row.closed_ts < day_to:
            continue
        week_r += row.outcome_r
        if day_from <= row.closed_ts:
            day_r += row.outcome_r
            if row.template == template and row.status == "closed_stop":
                template_stops += 1
    block: TradeBlock | None = None
    if daily_brake_r > 0 and day_r <= -daily_brake_r:
        block = "daily_brake"
    elif weekly_brake_r > 0 and week_r <= -weekly_brake_r:
        block = "weekly_brake"
    elif max_template_stops_per_day > 0 and template_stops >= max_template_stops_per_day:
        block = "template_stops"
    return BrakeState(day_r=day_r, week_r=week_r, template_stops=template_stops, block=block)


def expectancy_lower_bound(results: Sequence[float], z: float = 1.645) -> float | None:
    """Dolní mez jednostranného 95% intervalu Ø R (t≈z, n ≥ 2). None pod 2 vzorky."""
    n = len(results)
    if n < 2:
        return None
    mean = sum(results) / n
    variance = sum((value - mean) ** 2 for value in results) / (n - 1)
    return mean - z * math.sqrt(variance / n)


#: Edge, který má buňka prokázat, a kvantily testu (jednostranně 95 %, síla 80 %)
#: pro odhad potřebného vzorku (#1323): n = ((z_α + z_β) · σ / edge)²
PROOF_EDGE_R = 0.2
PROOF_Z_ALPHA = 1.645
PROOF_Z_POWER = 0.84


def samples_needed(results: Sequence[float], *, min_samples: int) -> int | None:
    """Kolik vzorků buňka potřebuje, aby edge +0,2 R prošel bránou s 80% šancí.

    σ je výběrová směrodatná odchylka R téhož vzorku jako brána; None pod
    2 vzorky. Nikdy méně než `min_samples` brány — pod ním brána nerozhodne,
    ať je σ jakékoli. Nulové σ pod `min_samples` nic neříká (stop je vždy
    přesně −1 R a cíl na stropu `max_rr` přesně +3 R, takže dva stopy nebo dva
    cíle dají σ = 0) → None, ne „vzorek stačí“: malý vzorek nesmí svádět."""
    n = len(results)
    if n < 2:
        return None
    mean = sum(results) / n
    sigma = math.sqrt(sum((value - mean) ** 2 for value in results) / (n - 1))
    if sigma == 0 and n < min_samples:
        return None
    needed = math.ceil(((PROOF_Z_ALPHA + PROOF_Z_POWER) * sigma / PROOF_EDGE_R) ** 2)
    return max(min_samples, needed)


@dataclass(frozen=True)
class GateResult:
    verdict: GateVerdict
    n: int
    lower_bound: float | None


def gate_window_start(now: dt.datetime, gate_days: int) -> dt.datetime:
    """Začátek okna brány: N seancí ≈ N × 7/5 kalendářních dnů. Jediná
    definice pro engine i Knihovnu (#1323) — verdikt „teď“ se nesmí rozejít."""
    return now - dt.timedelta(days=gate_days * 7 / 5)


def affordable_results(
    realized: Sequence[RealizedSetup],
    template: str,
    symbol: str,
    *,
    since: dt.datetime,
    point_value_usd: float,
    account_equity_usd: float,
    risk_pct: float,
    risk_max_pct: float,
) -> list[float]:
    """Výsledky buňky šablona × symbol pro bránu (#1325) — R řádků z `affordable_rows`."""
    return [
        row.outcome_r
        for row in affordable_rows(
            realized,
            template,
            symbol,
            since=since,
            point_value_usd=point_value_usd,
            account_equity_usd=account_equity_usd,
            risk_pct=risk_pct,
            risk_max_pct=risk_max_pct,
        )
    ]


def affordable_rows(
    realized: Sequence[RealizedSetup],
    template: str,
    symbol: str,
    *,
    since: dt.datetime,
    point_value_usd: float,
    account_equity_usd: float,
    risk_pct: float,
    risk_max_pct: float,
) -> list[RealizedSetup]:
    """Vzorek buňky šablona × symbol pro bránu (#1325): jen řádky vlastního
    symbolu, které by se daly zobchodovat (stop v rozpočtu).

    `realized` nese řádky napříč symboly (brzdy jsou o účtu), brána si bere jen
    `symbol` — ES a NQ mají u téže šablony jiný stop i skluz, takže každý symbol
    prokazuje edge sám. `symbol` je ticker instance (ADR-0041 bod 3): pinovaný
    `ESZ6` má vlastní buňku a vzorek `ES` nepřebírá. Řádky před pravidly
    (`affordable` None) se dopočítají z entry/stop a `point_value_usd` vlastního
    symbolu. Do #1325 se hodnota bodu
    brala ze slovníku, o kterém komentář tvrdil, že ho instance sdílejí; nesdílely,
    takže brána brala řádky s `affordable` z obou symbolů a dopočtené jen z vlastního.

    Stín (brána, brzda i rozhodnutí uživatele, #1323) se počítá stejně jako
    obchodovatelné — na `tradeable` se nefiltruje, takže stádium vzorek nezkreslí."""
    rows: list[RealizedSetup] = []
    for row in realized:
        if row.symbol != symbol or row.template != template or row.closed_ts < since:
            continue
        affordable = row.affordable
        if affordable is None:
            affordable = position_size(
                row.entry,
                row.stop,
                point_value_usd,
                account_equity_usd=account_equity_usd,
                risk_pct=risk_pct,
                risk_max_pct=risk_max_pct,
            ).affordable
        if affordable:
            rows.append(row)
    return rows


def template_gate(results: Sequence[float], *, min_samples: int, enabled: bool) -> GateResult:
    """Brána buňky šablona × symbol: pass jen s kladnou dolní mezí očekávání
    při n ≥ min_samples (`results` z `affordable_results`)."""
    lb = expectancy_lower_bound(results)
    if not enabled:
        return GateResult("off", len(results), lb)
    if len(results) < min_samples or lb is None:
        return GateResult("insufficient", len(results), lb)
    return GateResult("pass" if lb > 0 else "block", len(results), lb)


@dataclass(frozen=True)
class TrialUsage:
    """Čerpání zkoušky buňky (#1323), odvozené z výsledků jako brzdy."""

    #: Setupy s přebitou bránou od začátku zkoušky — uzavřené i otevřené
    setups: int
    #: Σ R uzavřených z nich (záporné = ztráta); otevřený setup R ještě nemá
    sum_r: float
    spent: bool


def trial_usage(
    realized: Sequence[RealizedSetup],
    symbol: str,
    template: str,
    *,
    started_at: dt.datetime,
    budget_setups: int,
    budget_r: float,
    open_setups: int = 0,
) -> TrialUsage:
    """Čerpání zkoušky buňky ticker × šablona — čistá funkce, nic se nezapisuje.

    Čerpají jen setupy, které zkouška opravdu pustila (`gate_overridden`):
    setup obchodovatelný z vlastní brány (pass) ani setup zastavený sizingem
    nebo brzdou rozpočet neubírá. Počítají se setupy vzniklé od `started_at`
    (obnovená zkouška začíná od nuly); uzavřené z `realized`, otevřené dodá
    volající (`open_setups`, z paměti instance nebo řádků `active`). Vyčerpáno
    = počet ≥ `budget_setups`, nebo Σ R ≤ −`budget_r`. Na šablonu a symbol je
    otevřený nejvýš 1 setup, takže ztráta může rozpočet přečerpat nejvýš o něj.

    Okno čtení musí sahat do `started_at` (engine ho do `_load_realized`
    přidává); řádky jiné verze mechaniky volající nepředává. Zkouška jiné
    mechaniky proto vůbec neplatí (`TrialCell.in_force`) — jinak by zvednutí
    mechaniky vyčerpané zkoušce vrátilo plný rozpočet bez rozhodnutí uživatele.
    """
    count = open_setups
    total = 0.0
    for row in realized:
        if (
            row.gate_overridden
            and row.symbol == symbol
            and row.template == template
            and row.created_ts is not None
            and row.created_ts >= started_at
        ):
            count += 1
            total += row.outcome_r
    spent = count >= budget_setups or total <= -budget_r
    return TrialUsage(setups=count, sum_r=total, spent=spent)
