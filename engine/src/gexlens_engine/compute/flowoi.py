"""Flow-adjusted OI odhad (ADR-0011 fáze 2, #232, ADR-0061) — jediná definice odhadu.

OI_est(K, s, t) = max(0, OI_ráno(K, s) + k·x(K, s, t)), kde podle mechaniky:
- v5: x = čistý klasifikovaný objem (net), k = α (ADR-0011);
- v6: x = objem strany za seanci (maximum denního kumulativu, #1602), k = ρ
  (ADR-0061). ρ ≥ 0 a objem ≥ 0, takže podlaha 0 se neuplatní.

FA levels, FA Dyn GEX profil/pole i persistovaná řada `oiest` MUSÍ vycházet
z téhož čísla — dvě nezávislé formule by v UI ukazovaly dvě různé
„flow-adjusted" pravdy. Faktor ladí per symbol ranní kalibrace proti
skutečnému ΔOI z věčného archivu (`storage/fa_calibration`).
"""


def oi_estimate(oi: float, net_volume: float, alpha: float) -> float:
    """Odhad OI: ranní archiv + faktor·tok strany, podlaha 0.

    Pod v6 je `net_volume` objem strany za seanci a `alpha` je ρ (ADR-0061).
    Podlaha 0 — otevřená pozice nemůže být záporná; net prodej větší než
    ranní OI (v5) znamená jen, že model přestal věřit zbytku pozice.
    """
    return max(0.0, oi + alpha * net_volume)
