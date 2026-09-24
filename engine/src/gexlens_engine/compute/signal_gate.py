"""Gate signálů ze zpráv (sentiment SPEC 6.2, ADR-0042) — jediný zdroj pravdy.

Bucket `news_model_stats` smí založit signál, jen když má dost vzorků,
spolehlivý směr **a** obchodovatelnou velikost reakce. Výsledek zapisuje
news-engine do sloupce `news_model_stats.gate_open` při přepočtu statistik;
signal job, drift hlídka i UI čtou sloupec, pravidlo znovu nepočítají (#1267).
Žije v enginu, protože ho sdílí news-engine i API (prahy pro text ve Stats).
"""

# Bodová hit-rate 55 % při n=20 je od mince nerozlišitelná; při desítkách
# bucketů navíc nějaký „projde" náhodou — proto Wilsonův interval, ne bod.
GATE_MIN_SAMPLES = 30
GATE_WILSON_LB = 0.50
# LB > 0,5 měří jen spolehlivost směru, ne velikost: při n ≈ 14 000 projde
# i bucket s Ø −0,03 bp (NQ OTHER/imp 1, 23. 9. 2026 — 81 šumových signálů
# za den, #1265). 1 bp ≈ 2× round-trip náklad ES (tick + poplatek).
GATE_MIN_EFFECT_BP = 1.0


def gate_open(n: int, hit_rate_lb: float | None, ret_mean_bp: float) -> bool:
    """n ≥ 30 ∧ Wilson LB > 0,50 ∧ |Ø reakce| ≥ 1 bp."""
    if hit_rate_lb is None:
        return False
    return (
        n >= GATE_MIN_SAMPLES
        and hit_rate_lb > GATE_WILSON_LB
        and abs(ret_mean_bp) >= GATE_MIN_EFFECT_BP
    )


def gate_thresholds() -> dict[str, float]:
    """Prahy pro UI (text a progres ve Stats) — API je jen přeposílá."""
    return {
        "min_samples": GATE_MIN_SAMPLES,
        "wilson_lb": GATE_WILSON_LB,
        "min_effect_bp": GATE_MIN_EFFECT_BP,
    }
