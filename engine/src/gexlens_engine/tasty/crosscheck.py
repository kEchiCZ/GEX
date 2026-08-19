"""Křížová kontrola IBKR × tasty (#517 fáze A) — pasivní, bez jediného requestu.

Heartbeat testuje jen TCP spojení, stall detektory (ADR-0015) měří pasivně
stáří dat — incident 26.–27. 7. (15 h zmrzlé ATM greeks při tekoucích cenách)
ani jedna vrstva nezachytila včas. Chyběla nezávislá reference: „mlčí data,
nebo mlčí trh?"

Shadow (#613) tu referenci už měří — tenhle detektor jen čte, co `compare_minute`
stejně počítá. Nula requestů na IBKR, nula dotazů do PG, žádná linka navíc.

Čtyři stavy per minuta, podle toho, která strana má čerstvá data:

* **IBKR mrtvé ∧ tasty čerstvé** → problém je na straně IBKR (farma NEBO
  subskripce — rozlišit je umí až aktivní sonda fáze B). Tasty čerstvé
  vylučuje „tichý trh" bez jediného requestu navíc → alert.
* **obojí mrtvé** → nikdo neobchoduje; přestávka, svátek, tenká noc → ticho.
* **tasty mrtvé ∧ IBKR čerstvé** → sekundární zdroj → stav a log; alert
  (#764, `backup_dead`) až když se IBKR hodnoty přitom MĚNÍ — čerstvost sama
  nestačí, protože dxFeed posílá eventy jen při změně, takže v noci a v denní
  pauze CME okno stáří vyprší, zatímco IBKR sweep vrací poslední kotaci pořád
  dokola (rozdíl event-driven × poll-driven feedu, ne porucha). Měnící se
  hodnoty tichý trh vylučují: trh běží a mlčící záloha je porucha — a pozná
  se HNED, ne až při výpadku IBKR, kdy fallback nemá z čeho stavět.
* **obojí čerstvé** → OK.

Prahy měřené, ne odhadnuté (shadow data 13.–16. 8. 2026, 3 016 minut):
sweep obchází kontrakty po dávkách `batch_size`, takže KAŽDOU TŘETÍ minutou
podíl „IBKR mrtvé" vyskočí na ~58 % a hned zase spadne — rotační artefakt,
ne porucha. Okamžitý podíl je proto jako signál nepoužitelný. Měření nejdelší
souvislé série minut nad prahem v čisté historii:

    práh 50 % → 2 min (696×)   práh 70 % → 2 min (2×)
    práh 60 % → 2 min (42×)    práh 80 % → 1 min (1×)

Série tří minut v řadě nenastala ani jednou při žádném prahu. Default
70 % / 3 minuty tedy drží dvojnásobnou rezervu proti rotaci na obou osách.
"""

import logging
from collections import deque
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

#: Podíl kontraktů „IBKR mrtvé ∧ tasty čerstvé", od kterého se minuta počítá
#: jako podezřelá. Nad rotačním artefaktem (~58 %) s rezervou.
DEFAULT_SHARE_THRESHOLD = 0.70

#: Rozlišovač „tichý trh × mrtvá záloha" (#764): podíl kontraktů, jejichž IBKR
#: hodnoty se MĚNÍ mezi minutami, od kterého trh považujeme za živý. Měřeno
#: nad celou historií feed_comparison (4 833 minut, 13.–19. 8. 2026): za pauzy
#: CME (20–22 UTC) je medián změn ~0, za aktivního trhu p5 ≈ 0,38–0,42 —
#: práh 0,30 leží pod aktivním p5 a stará větev „tasty mlčí" s ním dává
#: 0 planých epizod (bez rozlišovače jich bylo 6, všechny v pauze CME).
DEFAULT_CHANGE_THRESHOLD = 0.30

#: Kolik podezřelých minut V ŘADĚ spustí alert. Rotační pík trvá vždy právě
#: jednu minutu, nejdelší naměřená série jsou dvě → tři jsou bezpečné dno.
DEFAULT_MINUTES_THRESHOLD = 3

#: Minimum sledovaných kontraktů, aby minuta vůbec něco vypovídala. Pod tím
#: je podíl statisticky bezcenný (pár kontraktů při přestavbě pipeline).
DEFAULT_MIN_CONTRACTS = 20

#: Než se týž stav ohlásí znovu. Výpadek farmy trvá desítky minut — bez
#: cooldownu by alert kanál dostal jeden a týž problém každou minutu.
DEFAULT_COOLDOWN_MINUTES = 15

CrossCheckState = Literal["ok", "ibkr_suspect", "tasty_suspect", "quiet", "insufficient"]


@dataclass(frozen=True)
class MinuteTally:
    """Rozpad kontraktů jedné minuty podle čerstvosti obou stran.

    Součet čtyř kategorií = `contracts`. Kategorie „obojí mrtvé" existuje jen
    tady: `compare_minute` takové řádky zahazuje (nulová informace pro report),
    takže z uložené `feed_comparison` ji zpětně sestavit NELZE — proto se
    počítá v témže průchodu, ne dodatečným dotazem.
    """

    contracts: int = 0
    both_fresh: int = 0
    ibkr_only_dead: int = 0
    tasty_only_dead: int = 0
    both_dead: int = 0
    #: Kolik kontraktů mělo IBKR hodnotu v této I předchozí minutě (#764) —
    #: jen ty vypovídají o tom, jestli se trh hýbe. 0 = změny se neměří
    #: (první minuta po startu, výpadek předchozí minuty).
    ibkr_comparable: int = 0
    #: Z komparabilních: kolika kontraktům se aspoň jedno pole změnilo.
    ibkr_changed: int = 0

    @property
    def ibkr_dead_share(self) -> float:
        """Podíl kontraktů, kde mlčí jen IBKR — nositel signálu fáze A."""
        if self.contracts <= 0:
            return 0.0
        return self.ibkr_only_dead / self.contracts

    @property
    def tasty_dead_share(self) -> float:
        if self.contracts <= 0:
            return 0.0
        return self.tasty_only_dead / self.contracts

    @property
    def both_dead_share(self) -> float:
        if self.contracts <= 0:
            return 0.0
        return self.both_dead / self.contracts

    @property
    def ibkr_changed_share(self) -> float:
        """Podíl kontraktů s měnícími se IBKR hodnotami — „hýbe se trh?" (#764)."""
        if self.ibkr_comparable <= 0:
            return 0.0
        return self.ibkr_changed / self.ibkr_comparable


@dataclass(frozen=True)
class CrossCheckVerdict:
    """Výsledek jedné minuty; `alert` je True jen na hraně (ne každou minutu)."""

    state: CrossCheckState
    tally: MinuteTally
    streak: int
    alert: bool
    message: str
    #: Mrtvá tastytrade záloha (#764): tasty mlčí, zatímco se IBKR hodnoty
    #: mění — trh běží, ticho druhého zdroje je porucha, ne pauza. Vlastní
    #: příznak vedle `state`, protože je to jiná zpráva než `tasty_suspect`
    #: („záloha není k dispozici", ne „data jsou špatná").
    backup_dead: bool = False


class CrossCheckDetector:
    """Stavový automat nad minutovými tally — hystereze a cooldown v jednom.

    Alert padne na hraně: až `minutes_threshold`-tá podezřelá minuta v řadě,
    pak nejdřív po `cooldown_minutes`. Návrat pod práh sérii nuluje, takže
    další výpadek se ohlásí znovu okamžitě (re-arm hysterezí, jako #675).
    """

    def __init__(
        self,
        *,
        share_threshold: float = DEFAULT_SHARE_THRESHOLD,
        minutes_threshold: int = DEFAULT_MINUTES_THRESHOLD,
        min_contracts: int = DEFAULT_MIN_CONTRACTS,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
        change_threshold: float = DEFAULT_CHANGE_THRESHOLD,
    ) -> None:
        self._share_threshold = share_threshold
        self._minutes_threshold = max(1, minutes_threshold)
        self._min_contracts = min_contracts
        self._cooldown_minutes = cooldown_minutes
        self._change_threshold = change_threshold
        self._ibkr_streak = 0
        self._tasty_streak = 0
        #: Minuty v řadě, kdy tasty mlčí A trh se přitom hýbe (#764)
        self._backup_streak = 0
        self._since_alert: dict[str, int] = {}
        #: Čisté minuty v řadě — po `minutes_threshold` se cooldown re-armuje
        self._clean_streak = 0
        #: Poslední verdikt pro /status — čte orchestrátor, ne detektor sám
        self.last: CrossCheckVerdict | None = None
        #: Posledních 10 minut pro diagnostiku v logu
        self.history: deque[MinuteTally] = deque(maxlen=10)

    def observe(self, tally: MinuteTally) -> CrossCheckVerdict:
        """Zpracuje minutu a vrátí verdikt; volá se 1× za minutu ze shadow smyčky."""
        self.history.append(tally)
        for state in list(self._since_alert):
            self._since_alert[state] += 1

        if tally.contracts < self._min_contracts:
            # Přestavba pipeline nebo start — sérii nulujeme, aby náběh
            # nedodal falešnou minutu do rozjetého počítadla
            self._ibkr_streak = 0
            self._tasty_streak = 0
            self._backup_streak = 0
            return self._finish(
                "insufficient",
                tally,
                0,
                False,
                f"Sledováno jen {tally.contracts} kontraktů — na výrok je to málo",
            )

        if tally.ibkr_dead_share >= self._share_threshold:
            self._ibkr_streak += 1
            self._tasty_streak = 0
            self._backup_streak = 0
            if self._ibkr_streak >= self._minutes_threshold:
                message = (
                    f"IBKR mlčí na {tally.ibkr_dead_share * 100:.0f} % kontraktů "
                    f"({tally.ibkr_only_dead}/{tally.contracts}) už {self._ibkr_streak} min, "
                    f"tastytrade přitom data má — problém je na straně IBKR"
                )
                return self._finish(
                    "ibkr_suspect",
                    tally,
                    self._ibkr_streak,
                    self._may_alert("ibkr_suspect"),
                    message,
                )
            return self._finish(
                "ok",
                tally,
                self._ibkr_streak,
                False,
                f"IBKR mlčí na {tally.ibkr_dead_share * 100:.0f} % kontraktů "
                f"({self._ibkr_streak}/{self._minutes_threshold} min do alertu)",
            )

        self._ibkr_streak = 0

        # Obojí mrtvé nad prahem = nikdo neobchoduje. Vědomě NEalertujeme:
        # tichý trh je normální stav, ne porucha (hlavní zdroj falešných
        # poplachů čistě-IBKR sondy, kvůli kterému fáze A vůbec vznikla).
        if tally.both_dead_share >= self._share_threshold:
            self._tasty_streak = 0
            self._backup_streak = 0
            return self._finish(
                "quiet",
                tally,
                0,
                False,
                f"Oba zdroje mlčí na {tally.both_dead_share * 100:.0f} % kontraktů — tichý trh",
            )

        if tally.tasty_dead_share >= self._share_threshold:
            self._tasty_streak += 1
            # Rozlišovač „tichý trh × mrtvá záloha" (#764): mlčící tasty je
            # porucha jen tehdy, když se IBKR hodnoty přitom MĚNÍ — trh běží
            # a druhý zdroj by měl co streamovat. Vlastní série: minuta bez
            # pohybu trhu důkaz o mrtvé záloze nepřináší, tak sérii nuluje.
            market_moving = (
                tally.ibkr_comparable >= self._min_contracts
                and tally.ibkr_changed_share >= self._change_threshold
            )
            self._backup_streak = self._backup_streak + 1 if market_moving else 0
            if self._backup_streak >= self._minutes_threshold:
                message = (
                    f"Mrtvá tastytrade záloha: tasty mlčí na "
                    f"{tally.tasty_dead_share * 100:.0f} % kontraktů už "
                    f"{self._backup_streak} min, zatímco se IBKR hodnoty mění "
                    f"({tally.ibkr_changed_share * 100:.0f} % kontraktů) — fallback na "
                    f"tastytrade teď NENÍ k dispozici; při výpadku IBKR graf zamrzne"
                )
                return self._finish(
                    "tasty_suspect",
                    tally,
                    self._backup_streak,
                    self._may_alert("backup_dead"),
                    message,
                    backup_dead=True,
                )
            if self._tasty_streak >= self._minutes_threshold:
                message = (
                    f"tastytrade mlčí na {tally.tasty_dead_share * 100:.0f} % kontraktů "
                    f"({tally.tasty_only_dead}/{tally.contracts}) už {self._tasty_streak} min, "
                    f"IBKR data má — sekundární zdroj, jen se poznamenává"
                )
                # Bez rozlišovače vědomě BEZ alertu: přehrání historie dalo
                # 6 epizod „tasty mlčí" (dřív 41 na kratší historii), všechny
                # v pauze CME 21–22 UTC. Příčina je konstrukční, ne porucha —
                # dxFeed posílá eventy jen při změně, takže v tichu okno stáří
                # vyprší, zatímco IBKR sweep vrací poslední kotaci pořád dokola.
                # Stav zůstává čitelný v /status a v logu; alertuje až větev
                # `backup_dead` výš, která tichý trh vyloučí měřením změn.
                return self._finish("tasty_suspect", tally, self._tasty_streak, False, message)
            return self._finish("ok", tally, self._tasty_streak, False, "tastytrade zaostává")

        self._tasty_streak = 0
        self._backup_streak = 0
        return self._finish("ok", tally, 0, False, "Oba zdroje dodávají data")

    def _may_alert(self, key: str) -> bool:
        """Alert jen na hraně série a nejvýš jednou za cooldown."""
        elapsed = self._since_alert.get(key)
        if elapsed is not None and elapsed < self._cooldown_minutes:
            return False
        self._since_alert[key] = 0
        return True

    def _finish(
        self,
        state: CrossCheckState,
        tally: MinuteTally,
        streak: int,
        alert: bool,
        message: str,
        *,
        backup_dead: bool = False,
    ) -> CrossCheckVerdict:
        if state in ("ok", "quiet") and streak == 0:
            # Uzdravení re-armuje cooldown, ale až po stejně dlouhé čisté sérii,
            # jaká alert spustila — jinak by kolísání kolem prahu vyrobilo
            # alert každé tři minuty dokola.
            self._clean_streak += 1
            if self._clean_streak >= self._minutes_threshold:
                self._since_alert.clear()
        else:
            self._clean_streak = 0
        verdict = CrossCheckVerdict(
            state=state,
            tally=tally,
            streak=streak,
            alert=alert,
            message=message,
            backup_dead=backup_dead,
        )
        self.last = verdict
        if alert:
            logger.warning("Křížová kontrola feedů: %s", message)
        elif state == "tasty_suspect" and streak == self._minutes_threshold:
            # Tlumené hlášení: jednou na začátku epizody do logu, ne do alertů
            logger.info("Křížová kontrola feedů: %s", message)
        return verdict
