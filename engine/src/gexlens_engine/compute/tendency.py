"""Souhrnný indikátor tendence ceny (#350) — čisté funkce bez I/O.

Strojově vyhodnocuje podmínky růstu/poklesu popsané v legendě grafu (#348):
každá složka hlasuje −1 (short) … +1 (long), skóre je vážený průměr přes
dostupné složky. Rozhodnutí z issue #350:

* **Váha Gamma Flipu je nejvyšší** — mění charakter dne nejvíc.
* **Strop na jednu složku:** žádný samotný hlas nesmí vytlačit skóre do
  „Strong" pásma — čistý průměr by silný signál rozředil, čisté hlasování by
  zahodilo sílu; stropovaný průměr bere z obojího.
* **Prahy pásem:** Strong Short ≤ −0,5 < Short ≤ −0,15 < Neutral < 0,15 ≤
  Long < 0,5 ≤ Strong Long. Neutrální pásmo je schválně široké — dokud
  nejsou váhy kalibrované (#232), má indikátor raději mlčet než křičet.
* **Verzování vah (S11):** každý výsledek nese `weights_version`; po
  překalibrování nesmí staré záznamy vypadat, že vznikly novým modelem.

Není to doporučení k obchodu — popisuje positioning a tok, ne co dělat.
"""

import datetime as dt
from dataclasses import dataclass, field

# Verze vah (S11) — zvednout při každé změně vah nebo pravidel hlasů
# v2 (#397): + charm_flow (časová rampa do close) a vanna_flow (× trend IV)
# v3 (#394): hystereze pásem (margin + dwell) — skóre i hlasy beze změny,
#            ale uložené pásmo už není čisté band_of(score), takže řádky
#            v3 nejsou v pásmech srovnatelné s v2
TENDENCY_WEIGHTS_VERSION = 3

# Nekalibrované výchozí váhy (#350): flip nejvyšší, zbytek rovnocenný.
# Kalibrace #394 (7. 8., 7 dní dat v2): korelace hlasů s pohybem ceny za
# 15/30/60 min jsou slabé (|r| ≤ 0,23) a mezi ES a NQ si odporují znaménkem
# (flip na ES +0,12, na NQ ~0; gamma_at_price na obou záporná) — vzorek je
# jeden trendový týden, ne režimový průřez. Váhy proto ZŮSTÁVAJÍ nekalibrované;
# čísla a možnosti jsou v issue #394 (needs-decision).
WEIGHTS: dict[str, float] = {
    "flip": 3.0,
    "walls_distance": 1.0,
    "walls_dominance": 1.0,
    "max_pain": 1.0,
    "centroid": 1.0,
    "cum_delta_slope": 1.0,
    "divergence": 1.0,
    "delta_flow": 1.0,
    "sentindex": 1.0,
    "gamma_at_price": 1.0,
    "charm_flow": 1.0,
    "vanna_flow": 1.0,
}

# Strop příspěvku jedné složky do výsledného skóre — těsně pod hranicí
# „Strong" (0,5), aby jediná složka nikdy neudělala Strong sama
COMPONENT_CAP = 0.45

# Charm tok (#397): rampa hlasu k close 20:00 UTC — plná síla od T−1 h,
# nula do T−4 h; ráno by charm hlasoval šum, který trh začne řešit až večer
CHARM_RAMP_FULL_MIN = 60.0
CHARM_RAMP_ZERO_MIN = 240.0
# Vanna tok (#397): deadband trendu ATM IV — pod 0,1 vol bodu za okno je IV plochá
VANNA_IV_DEADBAND = 0.001

BAND_STRONG = 0.5
BAND_WEAK = 0.15

BANDS = ("strong_short", "short", "neutral", "long", "strong_long")

# Hystereze pásem (#394): bez ní pásmo kmitalo ~198× denně (medián běhu
# 2 minuty), protože skóre osciluje těsně kolem prahů ±0,15. Změřeno na
# historii tendency 29. 7.–6. 8. (ES i NQ ~7 100 minut):
#   margin 0,05           → ~96 přepnutí/den
#   dwell 3 min           → ~46 přepnutí/den
#   margin 0,05 + dwell 3 → ~28 přepnutí/den, medián běhu 16–18 min
# Kombinace drží obojí: skóre musí prahy překročit znatelně (margin) a nový
# stav vydržet (dwell). Cena je zpoždění přepnutí o `dwell` minut — pro
# indikátor s horizontem 15–60 min přijatelné.
BAND_HYSTERESIS_MARGIN = 0.05
BAND_DWELL_MINUTES = 3

# Meze pásem pro hysterezi: (dolní, horní) hranice skóre daného pásma
_BAND_BOUNDS: dict[str, tuple[float, float]] = {
    "strong_short": (float("-inf"), -BAND_STRONG),
    "short": (-BAND_STRONG, -BAND_WEAK),
    "neutral": (-BAND_WEAK, BAND_WEAK),
    "long": (BAND_WEAK, BAND_STRONG),
    "strong_long": (BAND_STRONG, float("inf")),
}


@dataclass(frozen=True)
class TendencyInputs:
    """Minutové vstupy; None = složka bez dat (hlas se přeskočí, ne nuluje)."""

    ts_min: dt.datetime
    spot: float
    flip: float | None = None
    call_wall: float | None = None
    put_wall: float | None = None
    call_wall_dom: float | None = None
    put_wall_dom: float | None = None
    max_pain: float | None = None
    centroid: float | None = None
    # Sklon/rozchod: hodnota teď a před lookback oknem
    cum_delta_now: float | None = None
    cum_delta_then: float | None = None
    price_then: float | None = None
    # Delta-vážené přírůstky opčního volume za minutu, per strana
    call_flow: float | None = None
    put_flow: float | None = None
    # SentIndex: aktuální hodnota a hodnota před lookback oknem
    sent_value: float | None = None
    sent_value_then: float | None = None
    # Gamma modelového profilu v místě ceny (gexfield.gamma_at_price)
    gamma_at_price: float | None = None
    # Charm/vanna plochy v místě ceny (#397) — tytéž modely jako Dyn plochy
    charm_at_price: float | None = None
    vanna_at_price: float | None = None
    # Minuty do close seance (20:00 UTC) — časová rampa charm hlasu
    minutes_to_close: float | None = None
    # ATM IV teď a před oknem — směr pro vanna hlas
    iv_now: float | None = None
    iv_then: float | None = None


@dataclass(frozen=True)
class ComponentVote:
    """Hlas jedné složky — rozpad pro UI (podmínka „žádná černá skříňka")."""

    name: str
    vote: float  # −1 … +1
    weight: float
    detail: str


@dataclass(frozen=True)
class TendencyResult:
    ts_min: dt.datetime
    score: float
    band: str
    votes: tuple[ComponentVote, ...] = field(default_factory=tuple)
    weights_version: int = TENDENCY_WEIGHTS_VERSION


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _sign(value: float) -> float:
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


def _collect_votes(inputs: TendencyInputs) -> list[ComponentVote]:
    votes: list[ComponentVote] = []

    def add(name: str, vote: float, detail: str) -> None:
        votes.append(ComponentVote(name=name, vote=vote, weight=WEIGHTS[name], detail=detail))

    spot = inputs.spot
    if inputs.flip is not None:
        add(
            "flip",
            _sign(spot - inputs.flip),
            f"cena {'nad' if spot > inputs.flip else 'pod' if spot < inputs.flip else 'na'} flipem {inputs.flip:.0f}",  # noqa: E501
        )
    if (
        inputs.call_wall is not None
        and inputs.put_wall is not None
        and inputs.call_wall > inputs.put_wall
    ):
        # Blíž k put zdi = podpora pod cenou (long); blíž k call zdi zrcadlově
        position = (inputs.call_wall - spot) / (inputs.call_wall - inputs.put_wall)
        add(
            "walls_distance",
            _clamp(2.0 * position - 1.0),
            f"poloha mezi zdmi {inputs.put_wall:.0f}–{inputs.call_wall:.0f}",
        )
    if inputs.call_wall_dom is not None and inputs.put_wall_dom is not None:
        strongest = max(inputs.call_wall_dom, inputs.put_wall_dom)
        if strongest > 0:
            add(
                "walls_dominance",
                _clamp((inputs.put_wall_dom - inputs.call_wall_dom) / strongest),
                f"dominance put {inputs.put_wall_dom:.0%} vs. call {inputs.call_wall_dom:.0%}",
            )
    if inputs.max_pain is not None:
        add(
            "max_pain",
            _sign(inputs.max_pain - spot),
            f"cena {'pod' if spot < inputs.max_pain else 'nad' if spot > inputs.max_pain else 'na'} Max Pain {inputs.max_pain:.0f}",  # noqa: E501
        )
    if inputs.centroid is not None:
        add(
            "centroid",
            _sign(inputs.centroid - spot),
            f"cena {'pod' if spot < inputs.centroid else 'nad' if spot > inputs.centroid else 'na'} těžištěm {inputs.centroid:.0f}",  # noqa: E501
        )
    if inputs.cum_delta_now is not None and inputs.cum_delta_then is not None:
        slope = inputs.cum_delta_now - inputs.cum_delta_then
        add("cum_delta_slope", _sign(slope), f"Cum Δ {slope:+.0f} za okno")
        if inputs.price_then is not None:
            price_direction = _sign(spot - inputs.price_then)
            cum_direction = _sign(slope)
            # Rozchod: cena dole a Cum Δ nahoře = akumulace (long); zrcadlově short
            divergence = 0.0
            if price_direction < 0 and cum_direction > 0:
                divergence = 1.0
            elif price_direction > 0 and cum_direction < 0:
                divergence = -1.0
            add(
                "divergence",
                divergence,
                "cena a Cum Δ v rozchodu" if divergence else "cena a Cum Δ ve shodě",
            )
    if inputs.call_flow is not None and inputs.put_flow is not None:
        total = inputs.call_flow + inputs.put_flow
        if total > 0:
            add(
                "delta_flow",
                _clamp((inputs.call_flow - inputs.put_flow) / total),
                f"Δ Flow C {inputs.call_flow:.0f} / P {inputs.put_flow:.0f}",
            )
    if inputs.sent_value is not None:
        rising = inputs.sent_value_then is None or inputs.sent_value >= inputs.sent_value_then
        vote = 0.0
        if inputs.sent_value > 0 and rising:
            vote = 1.0
        elif inputs.sent_value < 0 and not rising:
            vote = -1.0
        add(
            "sentindex",
            vote,
            f"SentIndex {inputs.sent_value:+.2f}, {'roste' if rising else 'klesá'}",
        )
    if inputs.gamma_at_price is not None:
        add(
            "gamma_at_price",
            _sign(inputs.gamma_at_price),
            f"gamma v místě ceny {'kladná (tlumení)' if inputs.gamma_at_price > 0 else 'záporná (zesilování)' if inputs.gamma_at_price < 0 else 'nulová'}",  # noqa: E501
        )
    if inputs.charm_at_price is not None and inputs.minutes_to_close is not None:
        # Dealer hedge tok = −d(delta knihy)/dt: záporný net charm (vyhnívající
        # put masa pod cenou) → nákupy do close → long; zrcadlově short.
        ramp = charm_time_factor(inputs.minutes_to_close)
        add(
            "charm_flow",
            -_sign(inputs.charm_at_price) * ramp,
            f"charm {'záporný → nákupy' if inputs.charm_at_price < 0 else 'kladný → prodeje' if inputs.charm_at_price > 0 else 'nulový'} do close, síla {ramp:.0%} ({max(0.0, inputs.minutes_to_close):.0f} min do 20:00 UTC)",  # noqa: E501
        )
    if (
        inputs.vanna_at_price is not None
        and inputs.iv_now is not None
        and inputs.iv_then is not None
    ):  # noqa: E501
        # Hedge tok při pohybu IV = −vanna·Δσ: kladná vanna + klesající IV →
        # nákupy → long. Plochá IV (deadband) → hlas mlčí.
        iv_change = inputs.iv_now - inputs.iv_then
        vote = 0.0
        if abs(iv_change) >= VANNA_IV_DEADBAND:
            vote = _sign(inputs.vanna_at_price) * -_sign(iv_change)
        add(
            "vanna_flow",
            vote,
            f"vanna {'kladná' if inputs.vanna_at_price > 0 else 'záporná' if inputs.vanna_at_price < 0 else 'nulová'}, ATM IV {iv_change * 100:+.2f} b za okno",  # noqa: E501
        )
    return votes


def charm_time_factor(minutes_to_close: float) -> float:
    """Rampa charm hlasu: 0 do T−4 h, lineárně k 1 od T−1 h; po close 0."""
    if minutes_to_close < 0:
        return 0.0
    if minutes_to_close <= CHARM_RAMP_FULL_MIN:
        return 1.0
    if minutes_to_close >= CHARM_RAMP_ZERO_MIN:
        return 0.0
    return (CHARM_RAMP_ZERO_MIN - minutes_to_close) / (CHARM_RAMP_ZERO_MIN - CHARM_RAMP_FULL_MIN)


def band_of(score: float) -> str:
    """Pásmo dle prahů z issue #350."""
    if score <= -BAND_STRONG:
        return "strong_short"
    if score <= -BAND_WEAK:
        return "short"
    if score < BAND_WEAK:
        return "neutral"
    if score < BAND_STRONG:
        return "long"
    return "strong_long"


@dataclass
class BandHysteresis:
    """Stavový filtr pásma (#394) — drží ho TendencyEngine mezi minutami.

    Pásmo se přepne, až když skóre opustí stávající pásmo o `margin` A nový
    kandidát vydrží `dwell` po sobě jdoucích minut. Skóre samotné se nemění —
    hystereze filtruje jen prezentované/ukládané pásmo.
    """

    margin: float = BAND_HYSTERESIS_MARGIN
    dwell: int = BAND_DWELL_MINUTES
    band: str | None = None
    _pending: str | None = None
    _pending_count: int = 0

    def update(self, score: float) -> str:
        raw = band_of(score)
        if self.band is None:
            self.band = raw
            return raw
        if raw == self.band or not self._beyond_margin(score):
            # Návrat do stávajícího pásma (nebo jen ťuknutí do prahu) maže
            # rozpracované přepnutí — kmit se nesčítá přes přestávky
            self._pending = None
            self._pending_count = 0
            return self.band
        if self._pending == raw:
            self._pending_count += 1
        else:
            self._pending = raw
            self._pending_count = 1
        if self._pending_count >= self.dwell:
            self.band = raw
            self._pending = None
            self._pending_count = 0
        return self.band

    def _beyond_margin(self, score: float) -> bool:
        assert self.band is not None
        low, high = _BAND_BOUNDS[self.band]
        return score < low - self.margin or score > high + self.margin


def evaluate_tendency(
    inputs: TendencyInputs, hysteresis: BandHysteresis | None = None
) -> TendencyResult | None:
    """Skóre a pásmo z dostupných složek; None = žádná složka nemá data.

    S `hysteresis` se pásmo filtruje proti kmitání (#394); bez ní je čisté
    `band_of(score)` — testy a jednorázová vyhodnocení stav nepotřebují.
    """
    votes = _collect_votes(inputs)
    if not votes:
        return None
    total_weight = sum(vote.weight for vote in votes)
    # Stropovaný vážený průměr: příspěvek každé složky po normalizaci
    # nesmí sám přesáhnout hranici Strong pásma
    score = sum(
        _clamp(vote.weight * vote.vote / total_weight, -COMPONENT_CAP, COMPONENT_CAP)
        for vote in votes
    )
    score = _clamp(score)
    return TendencyResult(
        ts_min=inputs.ts_min,
        score=score,
        band=hysteresis.update(score) if hysteresis is not None else band_of(score),
        votes=tuple(votes),
    )
