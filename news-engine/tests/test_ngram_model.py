"""Empirický prediktor zpráv (#740) — model bez sítě a bez DB.

Testy jsou stavěné tak, aby chytily tichý nesmysl: model, který se „naučí"
konstantu, nebo hashování, které míchá rysy dohromady, projdou naivními
kontrolami typu „vrací čísla mezi 0 a 1".
"""

import numpy as np

from gexlens_news.ngram_model import (
    DEFAULT_BUCKETS,
    LogisticModel,
    combine,
    features,
    hash_row,
    tokenize,
    wilson_lower_bound,
)


def rows(titles: list[str]) -> list[np.ndarray]:
    return [hash_row(features(title)) for title in titles]


# ── Rysy ───────────────────────────────────────────────────────────


def test_cisla_se_zobecnuji() -> None:
    """„o 25 bps" a „o 50 bps" mají sdílet signál slova `cuts`."""
    assert tokenize("Fed cuts rates by 25 bps") == ["fed", "cuts", "rates", "by", "<num>", "bps"]


def test_bigramy_zachyti_smysl_ktery_slova_zvlast_nemaji() -> None:
    """`rate` + `cut` zvlášť je něco jiného než `rate_cut`."""
    names = features("Fed rate cut")

    assert "w=rate" in names and "w=cut" in names
    assert "w=rate_cut" in names


def test_zdroj_a_hodina_jsou_rysy() -> None:
    names = features("Cokoli", source="reuters", hour_utc=14)

    assert "src=reuters" in names
    assert "h=14" in names


def test_hash_je_stabilni_napric_procesy() -> None:
    """Vestavěný `hash()` je solený per proces — model natrénovaný dnes by
    zítra ukazoval do jiných vah."""
    assert hash_row(["w=fed"])[0] == hash_row(["w=fed"])[0]
    assert hash_row(["w=fed"])[0] != hash_row(["w=ecb"])[0]


def test_ruzne_titulky_maji_ruzne_rysy() -> None:
    a, b = rows(["Fed cuts rates", "Apple beats estimates"])

    assert not np.array_equal(a, b)


# ── Učení ──────────────────────────────────────────────────────────


def test_model_se_nauci_oddelitelny_signal() -> None:
    """Nejzákladnější kontrola: oddělitelná data → model je oddělí.

    Kdyby se učil konstantu (což je u nevyvážených dat lákavé), obě strany by
    dostaly stejnou pravděpodobnost a test spadne.
    """
    titles = ["stocks surge on strong earnings"] * 40 + ["stocks plunge on weak data"] * 40
    labels = np.array([1.0] * 40 + [0.0] * 40)

    model = LogisticModel(epochs=12).fit(rows(titles), labels)
    up, down = model.predict_proba(
        rows(["stocks surge on strong earnings", "stocks plunge on weak data"])
    )

    assert up > 0.7
    assert down < 0.3


def test_model_zobecnuje_na_neviděný_titulek() -> None:
    """Test nad nevidenou kombinací slov — jinak by šlo o pouhé zapamatování."""
    up_titles = [f"company {i} beats estimates and raises outlook" for i in range(30)]
    down_titles = [f"company {i} misses estimates and cuts outlook" for i in range(30)]
    labels = np.array([1.0] * 30 + [0.0] * 30)

    model = LogisticModel(epochs=12).fit(rows(up_titles + down_titles), labels)
    # Jiná firma, jiná formulace, ale stejná slovní zásoba
    fresh = model.predict_proba(rows(["acme beats estimates", "acme misses estimates"]))

    assert fresh[0] > fresh[1]


def test_bez_signalu_zustane_u_zakladni_cetnosti() -> None:
    """Šum nesmí vyrobit sebejistotu — u nepredikovatelných dat má model
    zůstat blízko základní četnosti, ne tipovat s jistotou."""
    rng = np.random.default_rng(0)
    titles = [f"neutral headline number {i}" for i in range(120)]
    labels = rng.integers(0, 2, size=120).astype(float)

    model = LogisticModel(epochs=6).fit(rows(titles), labels)
    predictions = model.predict_proba(rows(["neutral headline number 7"]))

    assert 0.2 < predictions[0] < 0.8


def test_trenink_je_deterministicky() -> None:
    """Bez determinismu nejde poznat, zda se změnil model, nebo jen šum."""
    titles = ["alpha up"] * 20 + ["beta down"] * 20
    labels = np.array([1.0] * 20 + [0.0] * 20)

    first = LogisticModel(seed=7).fit(rows(titles), labels).predict_proba(rows(["alpha up"]))
    second = LogisticModel(seed=7).fit(rows(titles), labels).predict_proba(rows(["alpha up"]))

    assert first[0] == second[0]


def test_prazdny_radek_nespadne() -> None:
    model = LogisticModel().fit([np.zeros(0, dtype=np.int64)], np.array([1.0]))

    assert model.predict_proba([np.zeros(0, dtype=np.int64)])[0] == 0.5


# ── Spojení hlav ───────────────────────────────────────────────────


def test_nejisty_smer_srazi_silu_i_u_velke_zpravy() -> None:
    """Jádro návrhu #740: bez jistoty směru nemá velikost co dělat v indexu."""
    _, strength = combine(p_direction=0.5, p_big_move=0.95)

    assert strength == 0.0


def test_jisty_smer_a_velky_pohyb_dava_plnou_silu() -> None:
    direction, strength = combine(p_direction=1.0, p_big_move=1.0)

    assert direction == 1
    assert strength == 1.0


def test_jisty_smer_ale_maly_pohyb_dava_malou_silu() -> None:
    direction, strength = combine(p_direction=0.95, p_big_move=0.1)

    assert direction == 1
    assert strength < 0.1


def test_smer_dolu() -> None:
    direction, strength = combine(p_direction=0.05, p_big_move=0.8)

    assert direction == -1
    assert strength > 0.5


# ── Brána ──────────────────────────────────────────────────────────


def test_wilson_penalizuje_maly_vzorek() -> None:
    """6/10 nesmí vypadat jako 600/1000 — právě tohle síto LLM neprošla."""
    small = wilson_lower_bound(6, 10)
    large = wilson_lower_bound(600, 1000)

    assert small < 0.5 < large


def test_wilson_prazdny_vzorek() -> None:
    assert wilson_lower_bound(0, 0) == 0.0


def test_hash_row_deduplikuje() -> None:
    """Opakované slovo nemá vážit víc — rysy jsou binární."""
    assert len(hash_row(["w=fed", "w=fed"])) == 1
    assert hash_row(["w=fed"]).max() < DEFAULT_BUCKETS
