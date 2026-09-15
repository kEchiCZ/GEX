# ADR-0038: Risk framework malého účtu — účet v jednotkách plného kontraktu, 1 % na setup, brzdy a brána šablon

- **Stav:** přijato (uživatel 15. 9. 2026, #1185 varianta A, riziko 1 %)
- **Datum:** 2026-09-15
- **Souvisí:** #1185, #679 (kalkulačka v prohlížeči), #794 (walk-forward), #1060 (brána polohy), #453, ADR-0033 (parameter store), ADR-0004 (setupy)

## Kontext

Track record setupů (1 128 uzavřených, všechny mechaniky) obsahuje 71 setupů
se ztrátou přes 1 000 $ na plný kontrakt (Σ −114 717 $ proti celkovým +13 896 $).
Všechny mají stop ≥ 32 b ES / ≥ 73 b NQ; ve v5 jsou setupy se stopem nad
stropem zároveň nejhorší (n = 29, −0,34 R, 3 % cílů) proti setupům se stopem
v rozpočtu (n = 339, +0,06 R). Uživatel bude obchodovat účet ~5 000 $ na
**MES/MNQ**, aplikace počítá v ES/NQ.

## Rozhodnutí

1. **Jednotky.** Aplikace zůstává v plných kontraktech; účet se vede jako
   **50 000 $** (10× reálný účet na mikro). 1 kontrakt v aplikaci = 1 mikro
   reálně, body 1:1, dolary ×10. `ACCOUNT_START_USD` ve frontendu 5 000 → 50 000.
2. **Sizing** (`compute/risk.py`): `contracts = ⌊účet × risk_pct / (stop b ×
   hodnota bodu)⌋`; riziko **1 %** (500 $), tvrdý strop **2 %**. Stop 10 b ES /
   25 b NQ = 1 kontrakt. Stop nad rozpočtem = `unaffordable`.
3. **Brzdy** v R obchodovatelných setupů napříč symboly: −3 R seance, −6 R
   týden (od pondělní seance), 2 stopy téže šablony za seanci. Alert
   `risk_brake` jednou per brzda a seance.
4. **Brána šablon:** obchodovatelná jen šablona s kladnou dolní mezí
   jednostranného 95% intervalu Ø R (t≈1,645) při n ≥ 30 za 60 seancí, ze
   setupů se stopem v rozpočtu (starší řádky dopočtené z entry/stop).
5. **Setup vzniká vždy** (varianta A, ne C): nese `tradeable`/`trade_block`
   v kontextu; stín je šedý, bez pushe, mimo bilanci účtu. Detektory se
   nemění — poučení zůstává „stop nezvětšovat, zpřesnit vstup".
6. Parametry jsou součástí verzované `setup_params` (ADR-0033), editace
   v Settings → Risk management s povinným důvodem.

## Důsledky

- Ztráta jednoho setupu > 1 000 $ v aplikaci (100 $ reálně) je nemožná
  z definice; simulace nad v5: max DD 19 % (1 %) vs. 39 % (2 %).
- Žádný slib zisku: edge v5 ≈ +0,03 R, brána zpočátku označí většinu setupů
  za stín. Přepnutí na 2 % až po ≥ 50 živých obchodech s edge ≥ +0,2 R;
  živě až po ~60 seancích kladné bilance obchodovatelných setupů.
- 10× zhodnocení není otázka risk managementu, ale edge (#794, #1060, #453).
