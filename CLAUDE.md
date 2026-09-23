# GEXLens — instrukce pro Claude Code

**Zdroj pravdy pro práci v repu je `AGENTS.md`** (kontext, zdroje pravdy zadání, stack, build,
prostředí, kritická pravidla, principy, workflow) — čti ho jako první. Tento soubor jen doplňuje
specifika Claude Code; nic z AGENTS.md neopakuje.

## Specifika Claude Code
- Paměť projektu (`~/.claude/projects/…/memory/`) drží připomínky, stav produkce a strojové limity;
  nic z ní nepatří do repa (je veřejné). Opakující se poučení patří do `docs/lessons-learned.md`.
- Strojová specifika tohoto PC jsou v `CLAUDE.local.md` (negitovaný).
- Merge PR: `scripts/merge-when-green.sh <PR>` po zeleném CI, bez ptaní (trvalé svolení).
- Při ~70 % kontextu nebo po kompakci nabídni novou session; po kompakci ověř den v týdnu, stav trhu
  a stav issues tvrdými daty (`gh issue view`, `/api/status`, `marketclock`), ne ze souhrnu.
- Rozhodnutí předkládej jako varianty s výhodami/nevýhodami a doporučením (viz AGENTS.md
  „Rozhodování"); připomínky s datem zakládej jako issue `Připomínka ~D. M.:` s `prio:*`.
