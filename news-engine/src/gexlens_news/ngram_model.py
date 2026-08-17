"""Empirický prediktor zpráv (#740): učí se ze skutečné reakce trhu.

Nahrazuje LLM vrstvu, která hodnotu neprokázala (0 z 20 řádků `news_weights`
prošlo Wilson gate). Rozdíl není v chytrosti modelu, ale v učiteli: LLM
predikuje podle obecné intuice ze svého tréninku, tenhle model podle toho, jak
se choval **náš** trh. Rozdíl je měřitelný — zprávy typu „misses/plunge" u nás
končí nahoru v 55 % případů, protože se výprodej vykupuje (týž jev jako #563).

**Dvě hlavy nad společnými rysy**, obě jako klasifikace, takže sdílejí stroj:

* **směr** — `P(ret_bp > 0)`
* **velikost** — `P(|ret_bp| > medián)`

Spojení do `strength` je v `combine`: nejistý směr srazí sílu k nule i u velké
zprávy, takže do SentIndexu neteče šum. Tohle LLM vrstvě chybělo — hlásila
`direction` a `strength` nezávisle a nic je nespojovalo.

**Bez nové závislosti.** Logistická regrese s hashing trickem nad `numpy`
(scikit-learn + scipy by přidaly ~70 MB do image kvůli jednomu lineárnímu
modelu). AdaGrad proto, že rysy jsou řídké a mají velmi nerovnoměrnou
frekvenci — konstantní krok učení by vzácná slova buď přeskočil, nebo
rozhoupal.
"""

import hashlib
import re
from dataclasses import dataclass, field

import numpy as np

#: Velikost hashovacího prostoru. 2^18 na ~18 tis. vzorků: kolizí je málo a
#: paměť váhového vektoru je 2 MB.
DEFAULT_BUCKETS = 2**18

#: Slova kratší než 2 znaky nesou šum, delší n-gramy při 18 tis. vzorcích už
#: jen přeučují (každý tri-gram je skoro unikátní).
_TOKEN = re.compile(r"[a-z0-9$%]+")
MAX_NGRAM = 2


def tokenize(title: str) -> list[str]:
    """Titulek → tokeny. Čísla se zobecňují, přesná hodnota je pro směr šum.

    „Fed cuts rates by 25 bps" a „…by 50 bps" mají sdílet signál slova `cuts`;
    kdyby každé číslo bylo vlastní rys, model by se učil konkrétní hodnoty
    z hrstky výskytů.
    """
    tokens: list[str] = []
    for raw in _TOKEN.findall(title.lower()):
        tokens.append("<num>" if raw.replace(".", "").isdigit() else raw)
    return tokens


def features(title: str, *, source: str | None = None, hour_utc: int | None = None) -> list[str]:
    """Rysy jedné zprávy: n-gramy z titulku + zdroj + hodina.

    Vědomě NEPOUŽÍVÁ nic, co vzniká až po zprávě (reakce, kontaminace, režim
    spočtený zpětně) — look-ahead by nafoukl offline čísla a v provozu by se
    nedostavil.
    """
    tokens = tokenize(title)
    out = [f"w={token}" for token in tokens]
    for size in range(2, MAX_NGRAM + 1):
        out.extend("w=" + "_".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1))
    if source:
        out.append(f"src={source}")
    if hour_utc is not None:
        out.append(f"h={hour_utc}")
    out.append("<bias>")
    return out


def hash_row(names: list[str], *, buckets: int = DEFAULT_BUCKETS) -> np.ndarray:
    """Jména rysů → indexy. Stabilní hash (ne `hash()`, ten je per proces solený)."""
    if not names:
        return np.zeros(0, dtype=np.int64)
    indices = [
        int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), "big") % buckets
        for name in names
    ]
    return np.unique(np.asarray(indices, dtype=np.int64))


@dataclass
class LogisticModel:
    """Logistická regrese nad řídkými rysy, trénovaná AdaGradem.

    `fit` je deterministický při stejném `seed` — bez toho by nešlo porovnat
    dva běhy experimentu a rozhodnout, jestli se změnil model, nebo jen šum.
    """

    buckets: int = DEFAULT_BUCKETS
    lr: float = 0.3
    l2: float = 1e-6
    epochs: int = 6
    seed: int = 0
    weights: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    _sq_grad: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)

    def fit(self, rows: list[np.ndarray], labels: np.ndarray) -> "LogisticModel":
        self.weights = np.zeros(self.buckets, dtype=np.float64)
        self._sq_grad = np.full(self.buckets, 1e-8, dtype=np.float64)
        rng = np.random.default_rng(self.seed)
        order = np.arange(len(rows))
        for _ in range(self.epochs):
            rng.shuffle(order)
            for i in order:
                idx = rows[i]
                if idx.size == 0:
                    continue
                score = float(self.weights[idx].sum())
                pred = 1.0 / (1.0 + np.exp(-max(-30.0, min(30.0, score))))
                grad = pred - labels[i]
                # L2 se aplikuje jen na dotčené rysy — plný decay přes 262 tis.
                # vah by na každý vzorek stál víc než samotné učení
                g = grad + self.l2 * self.weights[idx]
                self._sq_grad[idx] += g * g
                self.weights[idx] -= self.lr * g / np.sqrt(self._sq_grad[idx])
        return self

    def predict_proba(self, rows: list[np.ndarray]) -> np.ndarray:
        out = np.empty(len(rows), dtype=np.float64)
        for i, idx in enumerate(rows):
            score = float(self.weights[idx].sum()) if idx.size else 0.0
            out[i] = 1.0 / (1.0 + np.exp(-max(-30.0, min(30.0, score))))
        return out


def combine(p_direction: float, p_big_move: float) -> tuple[int, float]:
    """Dvě pravděpodobnosti → (`direction`, `strength`) do `news_classifications`.

    `strength = jistota směru × očekávaná velikost`. Když si model směrem není
    jistý (P ≈ 0,5), síla padá k nule i u zprávy, po které se trh hodně hne —
    do indexu tedy nejde nic, co by jen zvyšovalo rozptyl. Velikost samotná
    zůstává použitelná zvlášť (riziko, alerty), proto se vrací nezmenšená ve
    druhé složce součinu.
    """
    confidence = 2.0 * abs(p_direction - 0.5)
    strength = confidence * p_big_move
    if p_direction > 0.5:
        direction = 1
    elif p_direction < 0.5:
        direction = -1
    else:
        direction = 0
    return direction, float(min(1.0, max(0.0, strength)))


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    """Dolní mez Wilsonova intervalu — stejná brána, jakou používá `news_weights`.

    Bez ní vypadá 6/10 stejně dobře jako 600/1000; právě tohle síto neprošla
    LLM vrstva ani jednou.
    """
    if total <= 0:
        return 0.0
    phat = successes / total
    denominator = 1 + z * z / total
    center = phat + z * z / (2 * total)
    margin = z * np.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return float((center - margin) / denominator)
