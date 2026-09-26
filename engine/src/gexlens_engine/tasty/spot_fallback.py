"""Spot z tastytrade při výpadku IBKR (#614) — aby se graf nezastavil.

Typický spouštěč je banální: uživatel se přihlásí na mobilu do IBKR a market
data se přepnou tam (`error 10197`), protože jsou **per uživatel**. Engine
zůstane připojený, jen mu přestanou chodit ticky — a cenový graf zamrzne, aniž
by cokoli spadlo. Druhý případ je výpadek datové farmy IBKR.

Rozhodování je schválně **oddělené od publishe** (`SpotStreamer`) i od IBKR
callbacku: tady je jen čistá logika „ze kterého zdroje brát cenu", kterou jde
otestovat bez sítě, bez ib_async a bez event loopu.

Pravidla vycházejí z ADR-0025 a zkušeností #517:

* **Hystereze na obě strany.** Přepnutí tam po `stale_after_s` bez IBKR ticku,
  zpět až po `recover_after_s` souvislých ticků. Bez druhé půlky by zdroj při
  kolísavém spojení blikal sem a tam a graf by střídal dvě mírně odlišné ceny.
* **Tichý trh není výpadek.** Když mlčí oba zdroje (pauza CME 16:00–17:00 CT,
  svátek), fallback se nezapíná — přepnutí by nic nezlepšilo a jen by zamlžilo,
  co se děje. Stejný princip jako `quiet` v křížové kontrole (#517 fáze A).
* **Zavřený trh podle rozvrhu** (#1307). „Mlčí i tasty" samo nestačí: čerstvost
  tasty se měří časem PŘIJETÍ eventu a DXLink po každé (re)subskripci pošle
  snímek posledních hodnot — po sobotním reconnectu tak tasty vypadala živě,
  zatímco IBKR od pátku správně mlčel (26. 9. 2026 02:13). Při zavřeném trhu
  (`compute/marketclock.is_market_closed`) se proto nepřepíná; fallback běžící
  už před uzávěrkou zůstává do návratu IBKR. Po otevření se ticho IBKR měří
  od okamžiku otevření, ne od pátečního ticku — jinak by v neděli 17:00:00 CT
  vystřelil hned.
* **Nikdy se nemíchají hodnoty.** Cena je vždy celá z jednoho zdroje; průměr
  dvou feedů by byl číslo, které neexistuje ani na jednom trhu (pravidlo 2
  z ADR-0025).
* **Tichý fallback je zakázaný.** `source` jde do statusu a do UI — uživatel
  musí poznat, odkud se dívá.
"""

from dataclasses import dataclass
from typing import Literal

#: Jak dlouho smí chybět IBKR tick, než se sáhne po tasty. ES obchoduje skoro
#: nepřetržitě a `SpotStreamer` throttluje na 0,2 s, takže půlminutové ticho je
#: už mimo normál. Kratší práh by reagoval na běžné mezery v tenkém trhu.
DEFAULT_STALE_AFTER_S = 30.0

#: Jak dlouho musí IBKR souvisle dodávat, než se převezme zpátky. Delší než
#: práh výpadku schválně: vracet se při prvním ticku by při kolísavém spojení
#: znamenalo přepínání každých pár sekund.
DEFAULT_RECOVER_AFTER_S = 60.0

SpotSourceName = Literal["ibkr", "tasty", "none"]


@dataclass(frozen=True)
class SpotDecision:
    """Cena k publikaci a odkud je; `price is None` = nemá se publikovat nic."""

    price: float | None
    source: SpotSourceName
    #: True jen v okamžiku změny zdroje — podklad pro log a alert, ať se
    #: přepnutí nezaloguje na každém ticku
    switched: bool = False


class SpotFallback:
    """Vybírá zdroj spotu podle čerstvosti obou feedů.

    Volá se z IBKR callbacku (`on_ibkr`) i z minutové smyčky (`resolve`), takže
    musí být levný — žádné I/O, jen porovnání časů.
    """

    def __init__(
        self,
        *,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        recover_after_s: float = DEFAULT_RECOVER_AFTER_S,
    ) -> None:
        self._stale_after_s = stale_after_s
        self._recover_after_s = recover_after_s
        self._last_ibkr_ts: float | None = None
        #: Od kdy IBKR souvisle dodává (nuluje se každou dírou)
        self._ibkr_healthy_since: float | None = None
        self._active: SpotSourceName = "ibkr"
        #: Stav rozvrhu z minulého `resolve` a okamžik posledního otevření trhu
        #: (#1307) — ticho IBKR se po otevření měří nejdřív od něj
        self._market_closed = False
        self._opened_at: float | None = None

    @property
    def active_source(self) -> SpotSourceName:
        return self._active

    def on_ibkr(self, price: float, now: float) -> SpotDecision:
        """IBKR tick. Cena se publikuje jen když je IBKR aktivní zdroj.

        Během fallbacku se ticky zaznamenávají (kvůli zotavení), ale
        nepublikují — jinak by se do grafu vedle sebe míchaly dvě řady.
        """
        if price != price:  # NaN bez importu math
            return SpotDecision(price=None, source=self._active)
        if self._last_ibkr_ts is None or now - self._last_ibkr_ts > self._stale_after_s:
            # Po díře začíná zotavovací okno znovu
            self._ibkr_healthy_since = now
        self._last_ibkr_ts = now

        if self._active == "ibkr":
            return SpotDecision(price=price, source="ibkr")
        healthy_for = now - (self._ibkr_healthy_since or now)
        if healthy_for >= self._recover_after_s:
            self._active = "ibkr"
            return SpotDecision(price=price, source="ibkr", switched=True)
        return SpotDecision(price=None, source=self._active)

    def resolve(
        self,
        now: float,
        *,
        tasty_price: float | None,
        tasty_fresh: bool,
        market_closed: bool = False,
    ) -> SpotDecision:
        """Rozhodnutí mimo IBKR tick — volá se pravidelně, i když IBKR mlčí.

        `tasty_fresh` říká, jestli tasty dodala cenu v rozumném okně; sama
        hodnota nestačí, protože cache drží poslední známý stav i po výpadku.
        `market_closed` je rozvrh CME v okamžiku volání (#1307): při zavřeném
        trhu se na tasty nepřepíná, ticho IBKR tam není výpadek.
        """
        if market_closed:
            self._market_closed = True
            if self._active != "tasty":
                return SpotDecision(price=None, source=self._active)
        elif self._market_closed:
            # Hrana otevření: IBKR mlčí od uzávěrky legitimně, 30 s se počítá odsud
            self._market_closed = False
            self._opened_at = now
        reference = self._last_ibkr_ts
        if self._opened_at is not None and (reference is None or reference < self._opened_at):
            reference = self._opened_at
        ibkr_stale = reference is None or now - reference >= self._stale_after_s
        if not ibkr_stale:
            return SpotDecision(price=None, source=self._active)

        # Oba zdroje ticho → tichý trh, ne porucha. Přepnutí by nic nepřineslo
        # a v UI by vypadalo jako výpadek IBKR (#517 fáze A, stav `quiet`).
        if not tasty_fresh or tasty_price is None:
            return SpotDecision(price=None, source=self._active)

        switched = self._active != "tasty"
        self._active = "tasty"
        self._ibkr_healthy_since = None
        return SpotDecision(price=tasty_price, source="tasty", switched=switched)
