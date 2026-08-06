"""Flow-adjusted OI odhad (ADR-0011 fáze 2, #232) — jediná definice odhadu.

OI_est(K, s, t) = max(0, OI_ráno(K, s) + α·net_klasifikovaný_objem(K, s, t)).

FA levels, FA Dyn GEX profil/pole i persistovaná řada `oiest` MUSÍ vycházet
z téhož čísla — dvě nezávislé formule by v UI ukazovaly dvě různé
„flow-adjusted" pravdy. α je kalibrační faktor open-ratia (ne účetnictví
pozic): default z konfigurace, po #232 fázi 2 ho per symbol ladí ranní
kalibrace proti skutečnému ΔOI z věčného archivu.
"""


def oi_estimate(oi: float, net_volume: float, alpha: float) -> float:
    """Odhad OI: ranní archiv + α·čistý klasifikovaný objem, podlaha 0.

    Podlaha 0 — otevřená pozice nemůže být záporná; net prodej větší než
    ranní OI znamená jen, že model přestal věřit zbytku pozice.
    """
    return max(0.0, oi + alpha * net_volume)
