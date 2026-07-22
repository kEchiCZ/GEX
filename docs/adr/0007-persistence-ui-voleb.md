# ADR-0007: Persistence uživatelských voleb UI v localStorage

**Stav:** přijato (2026-07-22, rozhodl uživatel — issue #167)
**Kontext:** SPEC v2.0 persistenci klientských voleb nepokrývá. Aplikace po
refreshi vždy naskočila do defaultů (1m timeframe, mód OI, Linear škála,
výchozí přepínače), což uživatele nutilo nastavení opakovat.

## Rozhodnutí

1. **Čistě klientská persistence v `localStorage`** (`frontend/src/state/persist.ts`).
   Žádné API ani DB — volby jsou vlastností prohlížeče/pracoviště, ne účtu.
   Klíče s prefixem `gexlens.` (např. `gexlens.interval`, `gexlens.toggles`).
2. **Co se persistuje:** timeframe (Intraday/Daily), interval (1m…1d), mód/škála/
   walls/styl/contours heatmapy, styl a viditelnost ceny, přepínače (Toggles jako
   celek), téma, aktivní symbol, šířka pravého panelu.
3. **Co se NEpersistuje:** obrazovka (`view` — řídí ji URL deep-link, výchozí je
   vždy Graf), stav replay lišty, anotační nástroj, pan/zoom pohledu (ten se
   odvíjí od dat dne a auto-fitu).
4. **Validace při čtení (revivery):** výčtové volby jen z povolené množiny,
   čísla sevřená do intervalu, přepínače merge známých klíčů přes defaulty.
   Rozbitá nebo zastaralá hodnota tiše spadne na default — persistence nikdy
   nesmí shodit aplikaci. Zápis je best-effort (plné/zakázané úložiště se ignoruje).
5. **URL deep-linky mají přednost** před uloženým stavem (`?price`, `?opacity`,
   `?theme`, `?view`) — automatizované snímky a sdílené odkazy musí být
   deterministické bez ohledu na lokální historii uživatele.

## Důsledky

- Testy: jsdom drží `localStorage` po celý běh souboru — globální
  `beforeEach(localStorage.clear)` v `src/test/setup.ts`, jinak volby prosakují
  mezi testy.
- Změna tvaru ukládané hodnoty nevyžaduje migraci: reviver nevalidní stav
  zahodí a začne se od defaultů.
