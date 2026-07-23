# ADR-0010: Dominance zdí — míra významnosti call/put wall

**Stav:** přijato (2026-07-23, scope zadal uživatel v issue #223)
**Kontext:** Call/put wall je prostý argmax/argmin NetGEX nad/pod spotem
(SPEC 4.2) — maximum vždycky existuje, takže se zeď nakreslí i nad plochým,
bezvýznamným profilem. Setup šablony T1 (wall bounce) a T3 (Max Pain pin,
ADR-0004) na zdi stavěly obchod, aniž ověřily, že zeď vůbec tvoří koncentraci;
pinning přitom funguje jen při pozicování dost velkém vůči likviditě podkladu.
Sekundární zeď práh významnosti má (ADR-0008, 0,7× primární), primární neměla.

## Rozhodnutí

1. **Metrika:** `dominance = síla zdi / Σ kladné síly strany` (0–1].
   Síla strany = NetGEX nad spotem (call), −NetGEX pod spotem (put); záporné
   hodnoty strany zeď netvoří a do jmenovatele nepatří. Koncentrovaný profil
   → ~1, plochý → ~1/N. Počítá se pro primární i sekundární zeď
   (`compute_levels` → `call_wall_dom`, `put_wall_dom`, `*_2_dom`).
2. **Persistence:** vlastní řada `derived/{sym}/{exp}/walldom/{den}.parquet`
   (WALLDOM_SCHEMA) — sloupce do LEVELS/LEVELS2 schémat přidat nejdou
   (rozbily by čtení existujících partic, precedens ADR-0005/0008).
   WS kanál `levels.*` nese dominance aditivně, `/replay` bundle klíč `walldom`.
3. **Setupy:** T1 vyžaduje dominanci dotčené zdi ≥ práh, T3 vyžaduje aspoň
   jednu zeď s dominancí ≥ práh (magnet bez koncentrace neexistuje). Práh
   `GEXLENS_SETUP_MIN_WALL_DOMINANCE` (default **0,15** = zeď drží ≥ 15 %
   kladné síly strany). Neznámá dominance (None, starší data v historii)
   podmínku přeskakuje — nedostupnost dat není slabá zeď. Dominance se ukládá
   do `setups.context` (`wall_dom`, `wall_dom_max`) pro kalibraci Fáze 2.
4. **UI:** cenovka primární zdi nese aktuální dominanci v % („7700 · 34 %");
   úseky linie s dominancí pod prahem (frontend konstanta `WALL_DOM_WEAK`
   = 0,15, zrcadlo engine defaultu) se kreslí ztlumeně a tečkovaně.

## Vědomé limity

- Dominance je RELATIVNÍ koncentrace strany — neříká nic o absolutní velikosti
  pozicování; strana s jediným nenulovým strikem má dominanci 1 i při malém OI.
- Při zapnutém párování sekundárních zdí (ADR-0008) se per-minutové slabé
  úseky zahazují — párování prohazuje hodnoty obou zdí po úrovních a flagy
  by po prohození patřily jiné zdi. Cenovka s dominancí zůstává.
- Prahová hodnota 0,15 je startovní odhad; kalibrace podle výsledků setupů
  (Fáze 2, ADR-0004) může práh posunout.

## Ověření

- Golden/unit testy: koncentrovaný vs. plochý profil, sekundární dominance,
  T1/T3 blokované pod prahem, None = podmínka se přeskakuje, context pole.
- Persistence roundtrip walldom; WS pole; bundle klíč; frontend merge řady,
  agregace weak flagů do košů, stripování při párování.
