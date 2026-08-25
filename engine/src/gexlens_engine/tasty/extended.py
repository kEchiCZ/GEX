"""Extended expirace z tastytrade (#616 fáze 4a) — šířka mimo strop IBKR lines.

Jednotka vlastnictví je CELÁ expirace (návrh v #616, schváleno 22. 8.):
IBKR drží aktivní + next expiraci (hot zóna, CumΔ, nejnižší latence),
tasty dodává expirace MIMO množinu IBKR do horizontu. Disjunktnost je
vlastnost konstrukce (extended = chain − IBKR) a navíc ji hlídá
`validate_disjoint` — překryv je chyba plánu, ne tichý merge.

Greeks se dopočítávají BS modelem z mid kotace (cesta #547): diagnóza #810
doložila, že dxFeed Greeks eventy na řídkých sériích nechodí (event-driven)
— berou se jen jako bonus, když jsou čerstvé. Volume je vědomě None: flows
a CumΔ zůstávají výhradně IBKR (trade printy celého řetězu sbírá #795 pro
budoucí klasifikaci #615).

Kadence je odstupňovaná (rozhodnutí 22. 8.): expirace ≤ `near_days` dnů se
zapisují každou minutu, vzdálenější každých `far_interval_min` minut —
vzdálené expirace se intraday skoro nehýbou a plná kadence by jen 3×
nafoukla keep-forever archiv (ADR-0029).
"""

import datetime as dt
import logging

from gexlens_engine.compute.gexfield import fallback_greeks
from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.storage.parquet_store import OiMissingRow, SnapshotRow
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols

logger = logging.getLogger(__name__)


class ExpiryOverlapError(RuntimeError):
    """Extended množina se překrývá s IBKR — chyba plánu, nesmí se tiše mergovat."""


def plan_extended_expiries(
    chain: ChainSymbols,
    ibkr_expiries: set[str],
    *,
    today: dt.date,
    horizon_days: int,
) -> list[str]:
    """Expirace pro tasty: (chain − IBKR) do horizontu, vzestupně.

    Prošlé expirace se vynechávají (chain mapa se obnovuje denně, ale plán
    se počítá každou minutu). Prázdná IBKR množina (start bez TWS, #756)
    znamená, že extended pokrývá vše — přesně chování „kompletní množiny"
    z ADR-0025 dodatku.
    """
    horizon = today + dt.timedelta(days=horizon_days)
    chain_expiries = {expiry for (expiry, _strike, _right) in chain.by_contract}
    planned = []
    for expiry in sorted(chain_expiries - ibkr_expiries):
        try:
            expiry_date = dt.datetime.strptime(expiry, "%Y%m%d").date()
        except ValueError:
            logger.warning("Nečitelná expirace %r v chain mapě — přeskakuji", expiry)
            continue
        if today <= expiry_date <= horizon:
            planned.append(expiry)
    return planned


def validate_disjoint(extended: list[str], ibkr_expiries: set[str]) -> None:
    """DoD #616: rozdělení zdrojů je disjunktní z konstrukce — překryv je chyba."""
    overlap = set(extended) & ibkr_expiries
    if overlap:
        raise ExpiryOverlapError(
            f"Extended expirace se překrývají s IBKR: {sorted(overlap)} — "
            "jedna expirace nesmí mít dva zdroje (#616)"
        )


def cadence_due(
    expiry: str, *, today: dt.date, minute_of_day: int, near_days: int, far_interval_min: int
) -> bool:
    """Odstupňovaná kadence: blízké expirace každou minutu, vzdálené každých N."""
    try:
        expiry_date = dt.datetime.strptime(expiry, "%Y%m%d").date()
    except ValueError:
        return False
    if (expiry_date - today).days <= near_days:
        return True
    return minute_of_day % far_interval_min == 0


def build_snapshot_rows(
    chain: ChainSymbols,
    expiry: str,
    cache: TastyChainCache,
    *,
    ts_min: dt.datetime,
    spot: float,
    now_utc: dt.datetime,
    max_age_s: float,
) -> tuple[list[SnapshotRow], list[OiMissingRow]]:
    """Minutová konsolidace jedné extended expirace z tasty cache.

    Řádek vzniká jen pro kontrakty s čerstvou kotací — extended expirace nemá
    IBKR obálku, takže „všechny striky" tu nejsou definované jinak než tím,
    co dxFeed reálně dodává. OI bez hodnoty jde do oimissing (#465 — graf
    nesmí tvrdit změřenou nulu tam, kde nikdo neměřil) a do řádku se zapíše
    0.0 jako v IBKR cestě.
    """
    settle = settle_ts(dt.datetime.strptime(expiry, "%Y%m%d").date())
    rows: list[SnapshotRow] = []
    oi_missing: list[OiMissingRow] = []
    for (contract_expiry, strike, right), streamer in chain.by_contract.items():
        if contract_expiry != expiry:
            continue
        state = cache.state(streamer)
        if state is None or state.quote.updated_at is None:
            continue
        age_s = (now_utc - state.quote.updated_at).total_seconds()
        if age_s > max_age_s:
            continue
        bid, ask = state.quote.bid, state.quote.ask
        if bid is None or ask is None or bid <= 0.0 or ask < bid:
            continue
        mid = (bid + ask) / 2.0
        # dxFeed Greeks jen čerstvé (#810: na řídkých sériích nechodí) — jinak
        # vlastní BS dopočet z mid (#547); kraje poctivě bez greeks
        greeks = state.greeks
        greeks_fresh = (
            greeks.updated_at is not None
            and (now_utc - greeks.updated_at).total_seconds() <= max_age_s
            and greeks.iv is not None
            and greeks.delta is not None
            and greeks.gamma is not None
        )
        if greeks_fresh:
            iv, delta, gamma = greeks.iv, greeks.delta, greeks.gamma
            theta, vega = greeks.theta, greeks.vega
        else:
            computed = fallback_greeks(
                spot=spot, strike=strike, right=right, mid=mid, settle=settle, now=now_utc
            )
            if computed is None:
                continue  # mimo no-arbitrage pásmo / nekonvergence — díra, ne výmysl
            iv, delta, gamma = computed.iv, computed.delta, computed.gamma
            theta, vega = computed.theta, computed.vega
        oi = state.summary.open_interest
        if oi is None:
            oi_missing.append(OiMissingRow(ts_min=ts_min, strike=strike, right=right))
        rows.append(
            SnapshotRow(
                ts_min=ts_min,
                strike=strike,
                right=right,
                bid=bid,
                ask=ask,
                last=state.last_price if state.last_price is not None else mid,
                volume=None,  # flows jsou výhradně IBKR (rozhodnutí #616)
                iv=iv,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                oi=oi if oi is not None else 0.0,
                stale_age=age_s,
            )
        )
    return rows, oi_missing


def extended_streamers(
    chain: ChainSymbols,
    planned: list[str],
    *,
    center: float | None,
    band_pct: float,
    near_band_pct: float | None = None,
    near_expiries: frozenset[str] = frozenset(),
) -> set[str]:
    """Streamer symboly extended expirací omezené pásmem kolem ceny (#616).

    Bez pásma ES přeteče kapacitu subskripce: 20 plánovaných expirací × plná
    šířka chainu ≈ 7 400 symbolů jen ES (změřený strop je 6 008/subskripci,
    ADR-0027) — server pak tiše nedodá nic a extended mlčí. ±band kolem
    spotu drží počty ~4–5 tis. a je konzistentní s IBKR obálkou (ADR-0002).
    Pásmo je v % ceny podkladu, ne v bodech: absolutní šířka by na NQ
    (cena ~4× ES) ořezala křídla na zlomek ES pokrytí (lekce ADR-0004).
    `center=None` (spot ještě není) → medián striků nejbližší plánované
    expirace jako náhrada; chain je kolem trhu, medián sedí na desítky bodů.

    Pásmo je odstupňované (#828 varianta A): kapacita subskripce je konečná
    (6 008 symbolů, ADR-0027), takže se rozděluje podle užitečnosti. Masa
    OTM putů, kvůli které je široké pokrytí potřeba, leží u nejbližších
    expirací — u expirace za tři týdny je pro dnešní čtení málo užitečná.
    `near_expiries` proto dostanou `near_band_pct`, zbytek `band_pct`.
    """
    planned_set = set(planned)
    if center is None and planned:
        nearest = min(planned)
        strikes = sorted(strike for (expiry, strike, _r) in chain.by_contract if expiry == nearest)
        center = strikes[len(strikes) // 2] if strikes else None
    if center is None:
        return set()
    far_points = center * band_pct / 100.0
    near_points = center * (near_band_pct if near_band_pct is not None else band_pct) / 100.0
    return {
        streamer
        for (expiry, strike, _right), streamer in chain.by_contract.items()
        if expiry in planned_set
        and abs(strike - center) <= (near_points if expiry in near_expiries else far_points)
    }
