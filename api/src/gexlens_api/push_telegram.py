"""Push notifikace mimo prohlížeč přes Telegram bota (#1175, #1126 bod 3b, #1284).

Zvonek žije jen v otevřeném tabu; uživatel chce alerty na mobil. Rozhodnutí
15. 9. 2026: Telegram (klienta má, bot = jeden HTTPS POST, žádná knihovna).
Signal nemá bot API, ntfy/Web Push by chtěly další aplikaci nebo hosting.

Jedna fronta, dva výstupy: zdroj je TENTÝŽ kanál `alerts`, který plní
zvonek (engine i news-engine ho posílají přes `/internal/publish`, provozní
alerty API přes `AlertEngine`) — `LiveHub` zavolá posluchače, tady se
rozhodne a pošle. Žádná nová rozhodovací logika: co nezvoní, neodejde.
Zvonek na nastavení Telegramu nezávisí, dostává vždy všechno.

Pravidla:
- Hlavní vypínač `push_telegram_enabled` a přepínač per druh upozornění
  `push_telegram_topic_<key>` (#1284) v serverových nastaveních
  (Settings → Notifikace). Jediný zdroj pravdy je tabulka `PUSH_TOPICS`:
  mapování kind → přepínač, výchozí hodnoty, výjimka z tichých hodin
  a české popisky s tooltipy, které UI i `OpsAlert.ps1` čtou z `/push/status`.
- Přepínač bez vlastní uložené hodnoty dědí dřívější přepínač své kategorie
  (`push_telegram_<kategorie>`, #1175), jinak výchozí hodnotu kategorie —
  kdo měl vypnuté „Zprávy", nedostane je ani po přestavbě.
- Druh bez záznamu v tabulce neodejde (WARNING jednou za druh).
- Setup jen `event=created` a s confidence ≥ práh (env), uzavření ne.
- Tiché hodiny (env, default 23:00–06:00 Europe/Prague) — provozní přepínače
  (kategorie ops) jdou i v noci (pád enginu nepočká), ostatní ne.
- Dedup per (kind, symbol, klíč zprávy) po 10 min, denní strop zpráv.
- Token a chat id jen z `.env`; hlavička ani log token nikdy nenesou,
  URL s tokenem se do logu nepíše (Telegram ho má v cestě).
- Odeslání běží v daemon vlákně mimo publish — WS klienti nečekají na síť.
"""

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT_S = 5.0
RETRIES = 3
DEDUP_WINDOW_S = 600.0
MESSAGE_MAX_CHARS = 900
#: Délka klíče v PG tabulce `settings` (String(64), engine storage/meta.py)
SETTING_KEY_MAX_CHARS = 64

#: Hlavní vypínač Telegramu (#1284): false = neodejde nic, přepínače zůstanou uložené
PUSH_MASTER_KEY = "push_telegram_enabled"
#: Prefix přepínačů per druh; bez `topic_` by se klíč „Nový setup" (`push_telegram_setup`)
#: kryl s dřívějším přepínačem celé kategorie setup (#1175)
TOPIC_KEY_PREFIX = "push_telegram_topic_"
#: Výchozí hodnota přepínače podle kategorie (= dřívější přepínače kategorií #1175)
CATEGORY_DEFAULTS: dict[str, bool] = {"setup": True, "ops": True, "news": True, "info": False}
#: Dřívější přepínače kategorií (#1175) — jen čtení jako záloha při dědění, zápis 422
LEGACY_CATEGORY_KEYS: dict[str, str] = {c: f"push_telegram_{c}" for c in CATEGORY_DEFAULTS}
CATEGORY_PREFIX: dict[str, str] = {"setup": "🎯", "ops": "⚠️", "news": "📰", "info": "ℹ️"}
#: Skupiny v Settings → Notifikace v pořadí vykreslení
PUSH_GROUPS: tuple[tuple[str, str], ...] = (
    ("market", "Setupy a burza"),
    ("app", "Chování aplikace"),
)

MASTER_LABEL = "Posílat notifikace na Telegram"
MASTER_HELP: tuple[str, ...] = (
    "Hlavní vypínač všech zpráv na Telegram.",
    "",
    "• Vypnuto = na Telegram neodejde nic, ani provozní výpadky a noční údržba",
    "• Zvonek v aplikaci dostává vždy všechno",
    "• Přepínače níže zůstanou uložené, jen zešednou",
    "• Výchozí: zapnuto",
)


@dataclass(frozen=True)
class PushTopic:
    """Jeden přepínač v Settings → Notifikace: druhy alertů se stejnou příčinou.

    `category` je dřívější kategorie (#1175) VŠECH jeho druhů — určuje výchozí
    hodnotu, dědění, výjimku z tichých hodin (ops) a emoji zprávy. Přepínač
    nikdy nemíchá kategorie, jinak by se někomu uložená volba převrátila.
    `bell=False`: druh posílá jen skript mimo Docker (`OpsAlert.ps1`), zvonek ho nemá.
    """

    key: str
    group: str
    category: str
    kinds: tuple[str, ...]
    label: str
    summary: str
    bullets: tuple[str, ...] = ()
    bell: bool = True

    @property
    def setting(self) -> str:
        return TOPIC_KEY_PREFIX + self.key

    @property
    def default(self) -> bool:
        return CATEGORY_DEFAULTS[self.category]

    def help_lines(self) -> list[str]:
        """Tooltip po řádcích (vzor ivRankTooltip): věta, prázdný řádek, odrážky."""
        lines = [self.summary, "", *(f"• {bullet}" for bullet in self.bullets)]
        if self.category == "ops":
            lines.append("• Chodí i v tichých hodinách")
        if self.bell:
            lines.append(f"• Ve zvonku: {', '.join(self.kinds)}")
        lines.append(f"• Výchozí: {'zapnuto' if self.default else 'vypnuto'}")
        return lines


PUSH_TOPICS: tuple[PushTopic, ...] = (
    # ── Setupy a burza ───────────────────────────────────────────────
    PushTopic(
        "setup",
        "market",
        "setup",
        ("setup",),
        "Nový setup",
        "Vznikl obchodní setup LONG/SHORT se vstupem, cílem a stopem.",
        (
            "Jen vznik, uzavření setupu na Telegram nechodí",
            "Stínový setup (stop nad rozpočtem, brzda, brána) se neposílá",
            "Práh confidence v % (0–100) z .env (GEXLENS_PUSH_SETUP_MIN_CONFIDENCE)",
        ),
    ),
    PushTopic(
        "risk_brake",
        "market",
        "setup",
        ("risk_brake",),
        "Brzda ztráty",
        "Byla dosažena denní nebo týdenní brzda ztráty.",
        ("Nové setupy jsou do settle jen stínové", "Jednou za seanci pro každou brzdu"),
    ),
    PushTopic(
        "paper",
        "market",
        "setup",
        ("paper",),
        "Paper účet",
        "Události paper účtu.",
        (
            "Zadání a úprava orderu, fill, zrušení",
            "Uzavření obchodu s výsledkem v R a $",
            "Kill switch",
        ),
    ),
    PushTopic(
        "scenario",
        "market",
        "setup",
        ("scenario_created", "scenario_result"),
        "Scénář dne",
        "Automatický scénář dne a jeho vyhodnocení.",
        (
            "Vznik scénáře před US openem (směr, cíle, termín)",
            "Výsledek po termínu: verdikt, cíl 1/2, max. odchylka",
        ),
    ),
    PushTopic(
        "news_anomaly",
        "market",
        "news",
        ("news_anomaly",),
        "Reakce trhu na zprávu",
        "Trh na zprávu zareagoval nad obvyklou míru.",
        ("Pohyb v bp nad p90 svého bucketu", "Jednou pro každou dvojici zpráva × symbol"),
    ),
    PushTopic(
        "vol_concentration",
        "market",
        "news",
        ("vol_concentration",),
        "Koncentrace opčního objemu",
        "Neobvyklý objem na jednom striku expirace.",
        (
            "Aspoň 3× medián top 10 striků",
            "Put pod trhem = pojistka nebo magnet, call nad trhem = strop",
        ),
    ),
    PushTopic(
        "expiry_calendar",
        "market",
        "news",
        ("expiry_calendar",),
        "Kalendář expirací",
        "Roll futures, OPEX týden a stav po SOQ.",
        ("Každá událost jednou za den",),
    ),
    PushTopic(
        "setup_degraded",
        "market",
        "ops",
        ("setup_degraded",),
        "Setup detektor prodělává",
        "Denní sebekontrola: setupy symbolu za sledované okno prodělávají.",
        ("Doporučí vypnout nejhorší šablonu nebo upravit prahy",),
    ),
    PushTopic(
        "level_proximity",
        "market",
        "info",
        ("level_proximity",),
        "Cena u GEX úrovně",
        "Cena se blíží k flipu nebo ke zdi.",
        ("Po vystřelení úroveň 15 min mlčí", "Může chodit často"),
    ),
    PushTopic(
        "t6_candidate",
        "market",
        "info",
        ("t6_candidate",),
        "Kandidát vzorce T6",
        "Ranní konstelace premarket squeeze po výprodeji.",
        ("Vzorec se zatím jen sbírá",),
    ),
    PushTopic(
        "setup_recovered",
        "market",
        "info",
        ("setup_recovered",),
        "Setup detektor se zotavil",
        "Setupy symbolu se vrátily nad práh výkonnosti.",
        ("Pár k „Setup detektor prodělává“",),
    ),
    PushTopic(
        "drift",
        "market",
        "info",
        ("drift",),
        "Drift vzorců",
        "Vzorec zpráv nebo šablona setupu má poslední výsledky výrazně horší než dlouhodobě.",
        ("Noční kontrola, hlásí jen nové nálezy",),
    ),
    # ── Chování aplikace ─────────────────────────────────────────────
    PushTopic(
        "ibkr_connection",
        "app",
        "ops",
        ("disconnect", "connection_stall", "degraded_start"),
        "Výpadek spojení s IBKR",
        "Spojení s IBKR chybí a sběr dat stojí.",
        (
            "Přechod do výpadku (hlásí API)",
            "Opakované hlášení, dokud výpadek trvá",
            "Start instrumentu bez IBKR (z cache a tastytrade)",
        ),
    ),
    PushTopic(
        "competing_session",
        "app",
        "ops",
        ("competing_session",),
        "IBKR přihlášen jinde",
        "Stejný účet je přihlášený v mobilu nebo v Client Portalu a přetahuje market data.",
        ("Pomůže odhlásit ostatní relace", "Nejvýš jednou za hodinu"),
    ),
    PushTopic(
        "subscription_error",
        "app",
        "ops",
        ("subscription_error",),
        "IBKR odmítá market data",
        "TWS opakovaně odmítá data kontraktů (error 354).",
        (
            "Vypíše dotčené kontrakty",
            "Zkontroluj subskripce v Market Data Subscription Manager",
        ),
    ),
    PushTopic(
        "tasty_fallback",
        "app",
        "ops",
        ("spot_fallback", "chain_fallback"),
        "Přepnutí na zálohu tastytrade",
        "Zdroj dat se přepnul mezi IBKR a tastytrade.",
        (
            "Cena podkladu z tastytrade",
            "Opční řetěz z tastytrade a návrat na IBKR",
            "Během fallbacku stojí CumΔ a net objem",
        ),
    ),
    PushTopic(
        "feed_backup_dead",
        "app",
        "ops",
        ("feed_backup_dead",),
        "Záloha tastytrade nefunguje",
        "Tastytrade mlčí, zatímco data z IBKR chodí.",
        ("Při výpadku IBKR by nebylo na co přepnout",),
    ),
    PushTopic(
        "option_data",
        "app",
        "ops",
        ("greeks_stalled", "strikes_stalled", "greeks_suspect"),
        "Neúplná data opcí",
        "Část striků nemá spolehlivá data, GEX a zdi jsou neúplné.",
        (
            "TWS nedodává Greeks nebo kompletní data striků",
            "Greeks jsou podezřelé ve srovnání s tastytrade",
        ),
    ),
    PushTopic(
        "oi",
        "app",
        "ops",
        ("oi_missing", "oi_refresh_failed"),
        "Chybí OI",
        "Open Interest dne chybí nebo je zastaralý.",
        (
            "OI z IBKR nedorazilo nebo selhala archivace",
            "Obnova OI po publikačním okně selhala",
        ),
    ),
    PushTopic(
        "instrument_error",
        "app",
        "ops",
        ("instrument_error",),
        "Instrument nejde spustit",
        "Instrument z watchlistu se nepodařilo spustit.",
        ("Další pokus po cooldownu",),
    ),
    PushTopic(
        "disk",
        "app",
        "ops",
        ("disk_space", "disk_limit", "scenario_disk"),
        "Dochází místo na disku",
        "Data aplikace se blíží limitu disku.",
        (
            "Volné místo pod prahem",
            "Překročený limit dat ze Settings → Engine",
            "Snímky scénářů nad 1 GB",
        ),
    ),
    # Druhy jen pro dokumentaci přepínače: skripty mimo Docker (#1279) je do API
    # nepublikují, čtou jen efektivní stav z /push/status před zastavením Dockeru
    PushTopic(
        "maintenance",
        "app",
        "ops",
        ("vhdx_compact", "deploy_rollback"),
        "Noční údržba selhala",
        "Skript mimo Docker ohlásil problém a stack může stát.",
        (
            "Po kompaktaci VHDX Docker nenaběhl, chybí kontejnery nebo se místo neuvolnilo",
            "Deploy enginu selhal a proběhl rollback",
            "Do zvonku nejde, protože API může stát; zůstane záznam v logu skriptu",
        ),
        bell=False,
    ),
    PushTopic(
        "feed_checks",
        "app",
        "info",
        ("feed_crosscheck", "feed_probe", "feed_silent"),
        "Kontrola datových toků",
        "Diagnostika zdrojů dat.",
        (
            "Křížová kontrola: IBKR mlčí, tastytrade data má",
            "Výsledek sondy IBKR a obnova subskripcí",
            "Stream tastytrade mlčel a engine ho přepojil",
        ),
    ),
    PushTopic(
        "greeks_bs_fallback",
        "app",
        "info",
        ("greeks_bs_fallback",),
        "Greeks z dopočtu",
        "Velký podíl striků jede na Greeks dopočtených modelem Black-Scholes.",
        ("Začátek a konec epizody", "Vynucená obnova nebo reconnect"),
    ),
    PushTopic(
        "bars",
        "app",
        "info",
        ("bars_stalled", "bars_recovered"),
        "Svíčky nechodí",
        "Real-time bary z TWS nechodí, přestože cena žije.",
        ("Engine obnovuje stream", "Návrat barů a doplnění díry"),
    ),
    PushTopic(
        "option_data_recovered",
        "app",
        "info",
        ("greeks_recovered", "strikes_recovered"),
        "Data opcí zase chodí",
        "Greeks a striky se zotavily.",
        ("Pár k „Neúplná data opcí“",),
    ),
    PushTopic(
        "disk_low",
        "app",
        "info",
        ("disk_low",),
        "Málo místa pro Docker",
        "Úklid Dockeru zjistil málo místa na disku nebo příliš velký VHDX.",
        (
            "Běží po nočním deployi a v sobotu",
            "Podléhá tichým hodinám, noční hlášení po deployi proto na Telegram nedorazí",
        ),
    ),
    PushTopic(
        "diagnostics",
        "app",
        "info",
        ("fa_validation", "fa_calibration", "band_capped"),
        "Diagnostika modelu",
        "Denní technické výstupy modelu.",
        ("FA validace a kalibrace α", "Obálka striků na stropu"),
    ),
)


def _index_kinds(topics: tuple[PushTopic, ...]) -> dict[str, PushTopic]:
    """kind → přepínač. Chyba v tabulce shodí import (fail fast), ne tichý push.

    Druh ve dvou přepínačích by měl dvě protichůdné volby; neznámá skupina
    nebo kategorie by se v UI nevykreslila nebo neměla výchozí hodnotu.
    """
    groups = {key for key, _ in PUSH_GROUPS}
    seen: set[str] = set()
    index: dict[str, PushTopic] = {}
    for topic in topics:
        if topic.key in seen:
            raise ValueError(f"Přepínač {topic.key!r} je v PUSH_TOPICS dvakrát")
        seen.add(topic.key)
        if topic.group not in groups or topic.category not in CATEGORY_DEFAULTS:
            raise ValueError(f"Přepínač {topic.key!r}: neznámá skupina nebo kategorie")
        if len(topic.setting) > SETTING_KEY_MAX_CHARS:
            raise ValueError(f"Klíč {topic.setting!r} je delší než {SETTING_KEY_MAX_CHARS} znaků")
        for kind in topic.kinds:
            if kind in index:
                raise ValueError(
                    f"Druh {kind!r} je v přepínačích {index[kind].key!r} i {topic.key!r}"
                )
            index[kind] = topic
    return index


KIND_TOPIC: dict[str, PushTopic] = _index_kinds(PUSH_TOPICS)
#: Zapisovatelné klíče (PUT /settings, jen bool); dřívější kategorie mezi nimi nejsou
PUSH_SETTING_KEYS: frozenset[str] = frozenset(
    {PUSH_MASTER_KEY} | {topic.setting for topic in PUSH_TOPICS}
)


def effective(stored: dict[str, Any]) -> tuple[bool, dict[str, bool]]:
    """Efektivní stav Telegramu z uložených nastavení — čistá funkce (#1284).

    Hlavní vypínač: uložený bool, jinak zapnuto. Přepínač: vlastní klíč →
    dřívější přepínač jeho kategorie (#1175) → výchozí hodnota kategorie.
    Hodnota, která není bool, se přeskočí. Nic se nezapisuje: starý klíč
    zůstává, jak ho uživatel nechal, zápis z UI jde jen na vlastní klíč.
    """
    master = stored.get(PUSH_MASTER_KEY)
    topics: dict[str, bool] = {}
    for topic in PUSH_TOPICS:
        value = topic.default
        for key in (topic.setting, LEGACY_CATEGORY_KEYS[topic.category]):
            candidate = stored.get(key)
            if isinstance(candidate, bool):
                value = candidate
                break
        topics[topic.key] = value
    return (master if isinstance(master, bool) else True), topics


def parse_quiet_hours(spec: str) -> tuple[dt.time, dt.time] | None:
    """`HH:MM-HH:MM` v lokálním čase; prázdné/nesmyslné = bez tichých hodin."""
    try:
        start_text, end_text = spec.strip().split("-")
        start = dt.time.fromisoformat(start_text.strip())
        end = dt.time.fromisoformat(end_text.strip())
    except ValueError:
        return None
    return (start, end)


def in_quiet_hours(now_local: dt.time, window: tuple[dt.time, dt.time] | None) -> bool:
    """Okno přes půlnoc (23:00–06:00) i v rámci dne (12:00–13:00)."""
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    if start < end:
        return start <= now_local < end
    return now_local >= start or now_local < end


@dataclass(frozen=True)
class PushOptions:
    token: str = field(default="", repr=False)
    chat_id: str = field(default="", repr=False)
    quiet_hours: str = "23:00-06:00"
    daily_cap: int = 200
    setup_min_confidence: float = 0.0
    timezone: str = "Europe/Prague"

    @property
    def configured(self) -> bool:
        return bool(self.token.strip() and self.chat_id.strip())

    @classmethod
    def from_settings(cls, settings: Any) -> "PushOptions":
        return cls(
            token=str(settings.push_telegram_token or "").strip(),
            chat_id=str(settings.push_telegram_chat_id or "").strip(),
            quiet_hours=str(settings.push_quiet_hours or ""),
            daily_cap=int(settings.push_daily_cap),
            setup_min_confidence=float(settings.push_setup_min_confidence),
        )


def format_message(payload: dict[str, Any]) -> str:
    kind = str(payload.get("kind", "alert"))
    symbol = str(payload.get("symbol") or "").strip()
    text = str(payload.get("message") or "").strip()
    topic = KIND_TOPIC.get(kind)
    prefix = CATEGORY_PREFIX[topic.category] if topic is not None else "ℹ️"
    head = f"{prefix} GEXLens"
    if symbol and symbol != "*":
        head += f" · {symbol}"
    body = f"{head}\n{text}" if text else f"{head}\n{kind}"
    return body[:MESSAGE_MAX_CHARS]


class TelegramPush:
    """Rozhodne, jestli alert odejde na Telegram, a pošle ho (mimo publish vlákno)."""

    def __init__(
        self,
        options: PushOptions,
        settings_reader: Callable[[], dict[str, Any]],
        *,
        post: Callable[..., httpx.Response] | None = None,
        clock: Callable[[], float] = time.time,
        run_in_thread: bool = True,
    ) -> None:
        self._options = options
        self._settings_reader = settings_reader
        self._post = post
        self._clock = clock
        self._run_in_thread = run_in_thread
        self._quiet = parse_quiet_hours(options.quiet_hours)
        self._tz = ZoneInfo(options.timezone)
        self._lock = threading.Lock()
        self._recent: dict[tuple[str, str, str], float] = {}
        self._day: dt.date | None = None
        self._sent_today = 0
        self._unknown_kinds: set[str] = set()
        self.last_error: str | None = None
        self.last_sent_at: float | None = None

    # ── stav pro /push/status ─────────────────────────────────────
    def status(self) -> dict[str, Any]:
        """Stav bota + efektivní přepínače s popisky pro Settings a OpsAlert.

        Bez tokenu a chat id. Nastavení se čte mimo zámek — o ten se `decide`
        dělí při dedupu a DB dotaz by ho zbytečně držel.
        """
        master, topics = effective(self._read_settings())
        with self._lock:
            sent_today = self._sent_today if self._day == self._today() else 0
            last_sent_at = self.last_sent_at
            last_error = self.last_error
        return {
            "configured": self._options.configured,
            "quiet_hours": self._options.quiet_hours,
            "daily_cap": self._options.daily_cap,
            "sent_today": sent_today,
            "last_sent_at": last_sent_at,
            "last_error": last_error,
            "enabled": master,
            "master": {
                "setting": PUSH_MASTER_KEY,
                "label": MASTER_LABEL,
                "help": list(MASTER_HELP),
            },
            "groups": [
                {
                    "key": group_key,
                    "label": group_label,
                    "topics": [
                        {
                            "key": topic.key,
                            "setting": topic.setting,
                            "label": topic.label,
                            "help": topic.help_lines(),
                            "enabled": topics[topic.key],
                        }
                        for topic in PUSH_TOPICS
                        if topic.group == group_key
                    ],
                }
                for group_key, group_label in PUSH_GROUPS
            ],
        }

    def _today(self) -> dt.date:
        return dt.datetime.fromtimestamp(self._clock(), self._tz).date()

    def _read_settings(self) -> dict[str, Any]:
        try:
            return self._settings_reader()
        except Exception:  # noqa: BLE001 — DB výpadek nesmí shodit publish
            logger.exception("Push: čtení nastavení selhalo — beru výchozí hodnoty")
            return {}

    # ── rozhodnutí ────────────────────────────────────────────────
    def decide(self, payload: dict[str, Any]) -> str | None:
        """Vrátí důvod, proč alert NEODEJDE, nebo None (odejde). Čisté rozhodnutí."""
        if not self._options.configured:
            return "bez tokenu/chat id"
        kind = str(payload.get("kind") or "")
        if not kind:
            return "bez kind"
        topic = KIND_TOPIC.get(kind)
        if topic is None:
            # Nový alert bez přepínače: zvonek ho má, Telegram ne, dokud se
            # nezařadí do PUSH_TOPICS — WARNING jednou, ať se na to přijde
            if kind not in self._unknown_kinds:
                self._unknown_kinds.add(kind)
                logger.warning(
                    "Push: druh %r nemá přepínač v PUSH_TOPICS — na Telegram neodejde", kind
                )
            return "neznámý druh"
        if kind == "setup":
            if payload.get("event") not in (None, "created"):
                return "setup: jen vznik"
            # Stínový setup (#1185: stop nad rozpočtem, brzda, brána) push nedostane
            if payload.get("tradeable") is False:
                return "setup: neobchodovatelný (stín)"
            confidence = payload.get("confidence")
            if (
                isinstance(confidence, int | float)
                and float(confidence) < self._options.setup_min_confidence
            ):
                return f"setup: confidence {confidence} < práh"
        master, topics = effective(self._read_settings())
        if not master:
            return "Telegram vypnut"
        if not topics[topic.key]:
            return f"{topic.key} vypnuto"
        now = self._clock()
        local = dt.datetime.fromtimestamp(now, self._tz)
        if topic.category != "ops" and in_quiet_hours(local.time(), self._quiet):
            return "tiché hodiny"
        key = (kind, str(payload.get("symbol") or ""), str(payload.get("message") or "")[:80])
        with self._lock:
            last = self._recent.get(key)
            if last is not None and now - last < DEDUP_WINDOW_S:
                return "duplicita do 10 min"
            today = local.date()
            if self._day != today:
                self._day = today
                self._sent_today = 0
            if self._options.daily_cap > 0 and self._sent_today >= self._options.daily_cap:
                return "denní strop"
            # Rezervace: počítá se hned, ať souběžné alerty strop nepřekročí
            self._recent[key] = now
            self._sent_today += 1
            if len(self._recent) > 500:
                cutoff = now - DEDUP_WINDOW_S
                self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}
        return None

    # ── odeslání ──────────────────────────────────────────────────
    def handle(self, payload: dict[str, Any]) -> None:
        """Posluchač `LiveHub` (kanál alerts): rozhodne synchronně, pošle mimo vlákno."""
        reason = self.decide(payload)
        if reason is not None:
            logger.debug("Push přeskočen (%s): %s", reason, payload.get("kind"))
            return
        text = format_message(payload)
        if self._run_in_thread:
            threading.Thread(target=self._send, args=(text,), daemon=True).start()
        else:
            self._send(text)

    def _send(self, text: str) -> bool:
        post = self._post or httpx.post
        url = TELEGRAM_URL.format(token=self._options.token)
        body = {"chat_id": self._options.chat_id, "text": text, "disable_web_page_preview": True}
        for attempt in range(1, RETRIES + 1):
            try:
                response = post(url, json=body, timeout=REQUEST_TIMEOUT_S)
            except httpx.HTTPError as exc:
                self.last_error = f"{type(exc).__name__}"
                logger.warning(
                    "Push Telegram pokus %d/%d: %s", attempt, RETRIES, type(exc).__name__
                )
                time.sleep(min(2.0 * attempt, 5.0))
                continue
            if response.status_code == 200:
                with self._lock:
                    self.last_sent_at = self._clock()
                    self.last_error = None
                return True
            # 4xx = chyba konfigurace (token, chat id) — opakovat nemá smysl
            self.last_error = f"HTTP {response.status_code}"
            logger.error(
                "Push Telegram odmítnut: HTTP %d %.200s", response.status_code, response.text
            )
            if 400 <= response.status_code < 500:
                return False
            time.sleep(min(2.0 * attempt, 5.0))
        return False
