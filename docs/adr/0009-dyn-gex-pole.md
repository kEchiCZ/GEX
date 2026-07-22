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
4. **Fáze 2 (implementováno 2026-07-22):** 2D pole jako heatmap mód „Dyn GEX"
   vedle stávajících módů (OI, Vol OTM, …).
   - **Minulé sloupce** = uložená historie 1D profilů (poctivé: tehdejší
     IV/OI/τ); minuty bez profilu se forward-fillují, hodnoty se vzorkují
     na cenách strikes lineární interpolací mřížky.
   - **Budoucí sloupce** = nová řada `gexfield`: engine 1×/min spočítá pole
     z POSLEDNÍHO snapshotu s klesajícím τ (sloupce po 10 min, strop 24 h
     = PROJECTION_MAX_MINUTES frontendu; numpy vektorizace). Partice
     `derived/{sym}/{exp}/gexfield/{den}.parquet` drží JEN poslední stav
     (replace, ne append) — „co model kdy tvrdil" se nearchivuje, poctivá
     je jen historie profilů. WS kanál `gexfield.{symbol}.{expiry}`,
     bundle klíč `gexfield`.
   - **Kreslí se do projekční zóny** (ADR-0006): ztlumení + svislý předěl
     zdarma signalizují „model, ne měření". Bez pole (starší engine,
     výpadek) spadne mód na konstantní projekci. Render musí u dynamické
     projekce vypnout zkratku „zkopíruj poslední sloupec" (`projectionDynamic`).
   - **Normalizace:** obě části sdílejí jmenovatel p99 naměřené části,
     aby projekce nepřebila minulost jinou škálou; škály Linear/√/Log/Pow⅓
     platí i pro tento mód.
   - **Playback:** přetáčení ukazuje jen naměřenou historii profilů
     (projekce se při přetáčení nekreslí, shodně s ADR-0006) — model je
     nástroj pohledu dopředu „od teď".
   - **Přejmenování:** přepínač overlay zdí se historicky jmenoval
     „Dyn GEX", ale ukazuje zdi — přejmenován na „Zdi", název patří módu.

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
