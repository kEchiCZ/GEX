# GEXLens — AGENTS.md

Aplikace pro vizualizaci opčního positioningu (GEX/OI/Vol heatmapa) nad ES futures opcemi
s trading koučem (setupy, briefing, deník, sentiment zpráv). Primární datový zdroj IBKR
TWS/Gateway API, sekundární tastytrade/dxFeed (ADR-0025/0032).
**Tento soubor je jediný vstupní bod pro AI nástroje** (Claude Code, Copilot, Windsurf…);
`CLAUDE.md` na něj jen odkazuje a přidává specifika Claude Code.

## Zdroje pravdy (čti v tomto pořadí, jen relevantní části)
1. `docs/SPEC.md` (v2.0) — **zadání a závazná rozhodnutí R1–R6** (kap. 0). Popisuje jádro M1–M5;
   moduly přidané po červenci 2026 (sentiment, setupy, briefing, deník, paper, kouč) v něm nejsou.
2. `docs/adr/` — každá odchylka od SPEC a každé architektonické rozhodnutí. ADR má přednost
   před textem SPEC, na který se odkazuje (např. retence 90 dní z ADR-0022 místo R3 = 14).
3. `docs/manual/ADMIN-MANUAL.md` — **aktuální stav** architektury, konfigurace, API, provozu.
   `docs/manual/UZIVATELSKY-MANUAL.md` — chování UI a obchodní čtení.
4. GitHub issues — pracovní zadání; roadmapa #629, průběžná aktualizace manuálů #628.

Rozpor mezi zdroji = nález, ne volba: nahlas ho a navrhni opravu (ADR nebo úprava dokumentu).

## Stack
Python 3.12 (uv workspace: `engine/`, `api/`, `news-engine/`) · ib_async · FastAPI + WebSocket ·
PostgreSQL 17 · Parquet (pyarrow) · React 19 + TypeScript + Vite · canvas/WebGL heatmapa ·
Docker Compose. Vývoj na Windows (PowerShell), běh v Linux kontejnerech (mypy `platform = "linux"`).

## Build & validace
```powershell
uv sync --all-packages
uv run ruff check . ; uv run ruff format --check .
uv run mypy engine/src engine/tests api/src api/tests news-engine/src news-engine/tests
uv run pytest                                  # jeden test: uv run pytest engine/tests/test_gex.py::test_x
cd frontend; npm ci; npm run lint; npm run format; npm test; npm run build   # build = tsc -b + vite
```
POSIX: `make test`. CI (GitHub Actions) vyžaduje zelené joby `python`, `frontend`, `security`
(gitleaks celé historie, pip-audit, npm audit; lokálně `pwsh scripts/security-scan.ps1`).
- Frontend ověřuj **`npm run build`**, ne jen `tsc --noEmit` — CI staví `tsc -b` a spadne jinde.
- IBKR se v testech **nikdy** nevolá živě — mock vrstva `engine/ibkr/mock.py`.
- Výpočty (GEX, levels, walls, CumΔ, profil…) mají golden testy v `engine/tests/golden/`;
  změna vzorce = změna golden datasetu ve stejném PR s odůvodněním.

## Prostředí a porty
| | Produkce (`compose.yml`) | Dev (`compose.dev.yml`) |
|---|---|---|
| Frontend | :8080 | :8081 |
| API (host → kontejner 8000) | :8010 | :8011 |
| PostgreSQL | :55432 | :55433 |
| Data | `data/` | `data-dev/` |

- **Výchozí práce je na devu.** Na produkci se sahá jen na výslovnou výzvu; změna enginu na produkci
  = upozornit předem (engine drží živá IBKR data a stav dne).
- Dev stack po testu **vždy** `docker compose -f compose.dev.yml down` (paměťový strop Dockeru).
- Před změnou schématu / migrací PG: `pwsh scripts/backup-postgres.ps1` — data v PG nejdou znovu pořídit.
- Konfigurace přes `.env` (viz `.env.example`, dev odchylky `.env.dev`). **Obsah `.env` se nikdy
  nevypisuje** do konzole ani logu; tajemství se referencují jen názvem proměnné.
  Před appendem do `.env` ověř, že soubor končí newline.

## Kritická pravidla
- **Závazná rozhodnutí R1–R6** (SPEC kap. 0) se neporušují: žádná MVP zjednodušení, plná klasifikace
  agresora, věčný OI archiv, IBKR jako primární zdroj.
- **Účet má 100 market data lines** (ne ≥150 z ADR-0001). `batch_size` a počet instrumentů nezvyšovat.
- **Uživatelova data se nemažou.** Po testech mazat jen vlastní záznamy podle id, nikdy hromadný DELETE
  (anotace, deník, verdikty, setupy jsou nenahraditelné).
- **Otevřený trh = vidět vše.** Žádné gating setupů/signálů podle svátku, tenkého trhu nebo seance —
  když je něco špatně, hledej příčinu, ne filtr.
- **Zavřený trh = žádná upozornění na chybějící data.** Každý hlídač výpadku dat (IBKR, tasty,
  OI, greeks, striky, spojení) má bránu „očekávají se data?“ z `compute/marketclock.is_market_closed`
  a test na víkend, denní pauzu a nedělní otevření; při zavřeném trhu jen loguje (#968, #1228, #1307).
- **Podezřelá hodnota na produkci se řeší hned** (issue + příčina), neodkládá se „až se to bude opakovat".
  Demo/mock data nesmí prosáknout do UI.
- **Ověřuj na tvrdých datech**: metriku dohledej v kódu, stav issue z `gh issue view`, ne z paměti;
  cenzurovaná/neúplná data neextrapoluj.
- Explicitní chyby, ne tiché selhání; pacing chyby IBKR nikdy neshodí engine (SPEC kap. 8).
- Skripty: před založením nového fix/probe/measure skriptu prohledej `scripts/`; nový ulož tam.
- **`docs/lessons-learned.md`**: před laděním problému ho projít (vzorce chyb se opakují); po opravě
  s netriviální příčinou přidat záznam; opakující se poučení povýšit na pravidlo sem nebo na kontrolu.
- Na produkčním Docker daemonu se **nebuildí mimo pauzu Globexu** (23:00–24:00 CEST) — build shazuje
  daemon; přes den jen mergovat. Po každém deployi ověřit čerstvost image/kontejneru, ne HTTP 200.

## Principy návrhu a vývoje
**Od prvních principů (first-principles):**
- Při návrhu funkce nebo refaktoringu nejdřív pojmenuj fundamentální **vstupy, výstupy a stavové
  přechody** — bez ohledu na to, jak to dělá stávající kód nebo historická konvence.
- Zpochybni překomplikované vzory. Existuje-li přímé, atomické řešení bez mezivrstvy nebo další
  závislosti, navrhni ho jako **primární** variantu; složitější jen s vyčísleným důvodem.
- Specifikaci osekej na **minimum metadat** nutné pro danou logiku. Sloupec, endpoint, parametr
  nebo konfigurační klíč, který nikdo nečte, se nezakládá (YAGNI).
- **Sokratovská spolupráce:** je-li zadání obecné nebo předpokládá složité řešení, nejdřív pojmenuj
  1–2 předpoklady v zadání, které jde zjednodušit nebo vypustit — a teprve pak navrhni řešení.
  Upozorňuj na balast a zastaralé abstrakce (legacy vrstvy, mrtvé konfigurační klíče, duplicitní
  cesty k témuž datu).

**Kód:**
- **KISS / YAGNI** — nejjednodušší řešení, které splní zadání a testy; žádné „pro budoucí použití".
- **DRY** — jeden zdroj každé konstanty, vzorce a konvence (obchodní den, settle, seance → ADR-0023,
  `compute/marketclock.py`). Než napíšeš helper, hledej existující.
- **SOLID v praxi:** výpočty v `engine/compute/` jsou **čisté funkce** nad daty (testovatelné bez IO,
  vyměnitelný model — např. znaménkový model GEX jako strategie); IO (IBKR, tasty, PG, Parquet) žije
  v adaptérech a `storage/`; API jen čte storage a přeposílá push, nepočítá.
- **Žádné workaroundy** — opravuj příčinu, ne symptom. Workaround je přípustný jen jako dočasný
  s issue na skutečnou opravu, označený v kódu odkazem na issue.
- **Fail fast, viditelně**: chybějící data = viditelná mezera se stářím, ne zmrzlá čísla (SPEC 3.7, #306).
- **Idempotence** jobů a zápisů (retro přepočty, archiv OI, verdikty) — opakované spuštění nesmí duplikovat.
- Rozsah změny = rozsah issue. Žádné drive-by refaktory; nález mimo rozsah → nové issue s `prio:*`.
- Stop loss v setupech se **nezvětšuje**; poučení z nevyšlé teze = přísnější podmínky vstupu.

## Rozhodování
- SPEC/ADR něco nepokrývá → **ADR** v `docs/adr/` (číslo navazuje) + PR s labelem `needs-decision`.
  Nerozhoduj mlčky.
- Každý bod k rozhodnutí předkládej jako **varianty s výhodami/nevýhodami a doporučením**.
- Odložená práce = **issue s `prio:P0–P3`** a `epic:*` labelem, ne poznámka v kódu nebo dokumentu.

## Jazyk
Komentáře, docstringy, dokumentace, commit zprávy, UI texty, issues: **česky**.
Identifikátory v kódu: **anglicky**. Při úpravě souboru zachovej jazyk okolí.
Tooltipy v UI: odrážky a `\n`, ne odstavec (vzor `ivRankTooltip`).

## Architektura (přehled — detail ADMIN-MANUAL kap. 1–8)
```
TWS/IB Gateway ─► engine (python -m gexlens_engine, minutový cyklus)
tastytrade DXLink ┘   ├─ ibkr/ connection, discovery, scheduler, underlying, mock
                      ├─ tasty/ dxFeed TimeAndSale, kotace, ad-hoc pohledy
                      ├─ compute/ gex, levels, walls, cumdelta, profile, setups, coach, …
                      └─ storage/ parquet_store, oi_archive, *_store (PG), retention
news-engine (python -m gexlens_news) ─► sentiment, klasifikace zpráv, signály, SentIndex
        │ Parquet (data/snapshots, derived, tasty_trades) + PostgreSQL
        ▼ HTTP push /internal/*
api (FastAPI, host :8010 → kontejner :8000) — REST + WS /ws/live; jen čte storage, nepočítá
        ▼
frontend (nginx :8080, React SPA) — heatmapa, profil, panely, playback, briefing, deník, stats
```
- Balíky: `gexlens_engine`, `gexlens_api`, `gexlens_news`; frontend `src/{heatmap,panels,profile,
  replay,setups,journal,stats,state,api,components}`.
- Partice: `data/snapshots/{sym}/{exp}/{den}.parquet`, `data/derived/…`, `data/tasty_trades/…`;
  ad-hoc (equity) pohledy píší do `snapshots/`, `levels` pro ně nejsou.
- Docker bind mount je pomalý (~20 ms/soubor): endpointy nad particemi cachovat, měřit v kontejneru.

## Git & workflow
- Jedno issue = jedna větev `feat/{N}-slug` (nebo `fix/`), PR s `Closes #N`; Conventional commits
  česky (`feat(engine): …`, `fix(api): …`, `docs(adr): …`).
- `main` je chráněný: jen PR, jen **squash merge** po zeleném CI aktuálního SHA →
  `scripts/merge-when-green.sh <PR>` (po push počkat ~60 s, `gh pr checks` musí checky vidět).
  `gh pr checks --watch` samotné nestačí (vrací 0 i při failu).
- Delší texty do `gh issue/pr comment` **vždy `--body-file`** (české uvozovky usekávají inline body).
- Milestones M1→M5 (SPEC kap. 9) a dále roadmapa #629: neimplementuj napřed věci z pozdějších fází.
- **Definition of done** pro feature PR:
  1. testy (golden při změně výpočtu), lint, mypy, build zelené;
  2. dokumentace ve stejném PR: ADR při rozhodnutí; manuál (#628) při změně chování UI/konfigurace;
     SPEC kap. 11 (index rozšíření) při novém modulu;
  3. roadmapa #629 odškrtnuta / doplněna;
  4. nasazení na dev a ověření na živých datech, pokud se mění engine nebo API.
- Repo je **veřejné**: žádná tajemství, osobní údaje ani interní zdroje inspirace v kódu, docs, issues
  a commitech.
