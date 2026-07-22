# ADR-0009: Dyn GEX — modelovaný gamma profil a pole (fázovaně)

**Stav:** přijato (2026-07-22, rozhodl uživatel — issue #203)
**Kontext:** SPEC v2.0 popisuje heatmapu nad NAMĚŘENÝMI daty (OI/volume per
strike). Referenční nástroj (Moodix) kreslí navíc modelované pole „Dyn GEX":
dealer gamma expozici přepočtenou pro hypotetické ceny přes celé pásmo a čas.
Odpovídá na otázku „jakou gammu potká cena na úrovni X v čase T", kterou
měřená mapa z principu zodpovědět neumí.

## Rozhodnutí

1. **Model doplňuje měření, nenahrazuje ho.** Zdi, levels, setupy i stávající
   heatmap módy zůstávají počítané z naměřených dat. Dyn GEX je další
   vizualizační vrstva/mód.
2. **Výpočet:** NetGEX(S) = Σ_call Γ_BS(S, K, IV, τ)·OI·M − Σ_put Γ_BS·OI·M —
   stejný znaménkový model jako levels (`NaiveDealerModel`, SPEC 4.1).
   Black-Scholes gamma nad ULOŽENOU IV kontraktu, r = 0, q = 0, τ = čas do
   settle expirace (20:00 UTC) s podlahou 5 minut (τ→0 by dala nekonečnou
   ATM gammu). Kontrakty bez IV nebo OI se vynechávají.
3. **Fáze 1 (tato ADR implementuje):** 1D profil pro aktuální minutu.
   - Engine počítá per minutu a expiraci profil přes cenovou mřížku
     (rozsah = obálka strikes, krok = polovina strike kroku).
   - Persistence: vlastní řada `derived/{sym}/{exp}/gexprofile/{den}.parquet`
     (ts_min, grid_start, grid_step, values list<float64>) — historie per
     minuta je zároveň levý (naměřený) díl budoucího 2D pole.
   - WS kanál `gexprofile.{symbol}.{expiry}` (aditivní), `/replay` bundle
     klíč `gexprofile`.
   - Frontend: křivka v pravém profilu (sdílená osa Y) — kladná část doprava
     (zelená, tlumení), záporná doleva (červená, akcelerace), průchod nulou =
     dynamický flip. Přepínatelná chipem, persistováno (ADR-0007).
4. **Fáze 2 (navazující):** 2D pole jako heatmap mód — minulé sloupce ze
   uložených profilů (poctivé: tehdejší IV/OI), budoucí sloupce modelem
   z posledního snapshotu s klesajícím τ. Není součástí této fáze.

## Vědomé limity modelu (zobrazit v UI nápovědě)

- Znaménko dealera je konvence (long call / short put gamma), ne fakt.
- IV je snímek — vol spike krajinu přeskládá; pole platí „za dnešních podmínek".
- OI je z ranního archivu — dnešní nový positioning model nevidí.
- Model neukazuje tok (volume/CumΔ/ΔOI zůstávají v měřené vrstvě).

## Ověření

- Golden/unit testy: ATM vrchol, znaménka C/P, monotonie s τ (crunch),
  podlaha τ, prázdný vstup.
- Persistence roundtrip list sloupce; WS rámec; bundle klíč.
- Frontend: geometrie křivky (nula uprostřed, škála na max |hodnota|),
  playback minuta, chip přepínač.
